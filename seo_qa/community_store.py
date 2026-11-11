"""Private offline lead retention. Tombstones prevent late-export resurrection.

Stores only normalized review fields, never raw posts/usernames. No collection,
network, scheduling, or guidance approval is performed. Existing backup copies
need their own retention/deletion handling; this store cannot revoke them.
"""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

from .community_news import review


def _time(value):
    if not isinstance(value, str):
        raise ValueError('Expected timezone-aware observation time')
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError('Invalid observation time') from exc
    if result.tzinfo is None:
        raise ValueError('Expected timezone-aware observation time')
    return result.astimezone(timezone.utc)


class CommunityStore:
    def __init__(self, path):
        self.path = Path(path)
        if self.path.is_symlink():
            raise ValueError('Symlink database is forbidden')
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(self._connect()) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS community_leads (
                id TEXT PRIMARY KEY, observed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL, state TEXT NOT NULL,
                body TEXT NOT NULL)''')
        self.path.chmod(0o600)

    def _connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA secure_delete=ON')
        db.execute('PRAGMA journal_mode=DELETE')
        return db

    def ingest(self, payload, *, received_at, retention_days=30):
        """Validate whole batch, then atomically admit normalized fields.

        Removal is sticky even for later exports. Retention expiry is sticky too;
        collecting again requires a separately reviewed identity/policy change.
        Observation times order updates; equal-time conflicting bodies fail.
        """
        received = _time(received_at)
        if type(retention_days) is not int or not 1 <= retention_days <= 90:
            raise ValueError('Retention must be between 1 and 90 days')
        normalized = review(payload)
        events = []
        for raw, lead in zip(payload['posts'], normalized['leads']):
            observed = _time(raw.get('observed_at'))
            if observed > received:
                raise ValueError('Observation cannot follow receipt time')
            expires = observed + timedelta(days=retention_days)
            state = 'removed' if lead['status'] == 'removed' else 'active'
            if state == 'active' and expires <= received:
                state = 'expired'
            body = lead if state == 'active' else {'id': lead['id'], 'status': state, 'text_retained': False}
            # Synthetic origin is preserved for every persisted active lead.
            if state == 'active':
                body = dict(body, synthetic=normalized['synthetic'])
            events.append((lead['id'], observed.isoformat(), expires.isoformat(), state,
                           json.dumps(body, sort_keys=True, separators=(',', ':'))))
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            self._expire(db, received)
            for event in events:
                key, observed, expires, state, body = event
                old = db.execute('SELECT * FROM community_leads WHERE id=?', (key,)).fetchone()
                if old:
                    if old['state'] == 'removed':
                        continue
                    if old['state'] == 'expired' and state != 'removed':
                        continue
                    # A removal beats ordering: any known removal must scrub.
                    if state != 'removed':
                        if observed < old['observed_at']:
                            continue
                        if observed == old['observed_at']:
                            if body != old['body']:
                                raise ValueError('Conflicting equal-time observation')
                            continue
                        expires = min(expires, old['expires_at'])
                db.execute('INSERT OR REPLACE INTO community_leads VALUES (?,?,?,?,?)',
                           (key, observed, expires, state, body))
        return self.snapshot(as_of=received_at)

    def remove(self, identity, *, as_of):
        """Scrub a known lead and retain a sticky removal tombstone."""
        if not isinstance(identity, str) or not identity or len(identity) > 100:
            raise ValueError('Invalid retained identity')
        observed = _time(as_of).isoformat()
        body = json.dumps({'id': identity, 'status': 'removed', 'text_retained': False}, sort_keys=True, separators=(',', ':'))
        with closing(self._connect()) as db, db:
            changed = db.execute("UPDATE community_leads SET state='removed',body=?,observed_at=? WHERE id=?", (body, observed, identity)).rowcount
        return bool(changed)

    @staticmethod
    def _expire(db, now):
        for row in db.execute("SELECT id FROM community_leads WHERE state='active' AND expires_at <= ?", (now.isoformat(),)).fetchall():
            body = json.dumps({'id': row['id'], 'status': 'expired', 'text_retained': False}, sort_keys=True, separators=(',', ':'))
            db.execute("UPDATE community_leads SET state='expired',body=? WHERE id=?", (body, row['id']))

    def snapshot(self, *, as_of):
        with closing(self._connect()) as db, db:
            self._expire(db, _time(as_of))
            leads = [json.loads(row[0]) for row in db.execute('SELECT body FROM community_leads ORDER BY id')]
        return {'schema': 'community-retention/v1', 'leads': leads,
                'network_calls': 0, 'publication': 'human review pending'}


def main(argv=None):
    """Manually process private saved inputs; never fetch remote content."""
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    parser.add_argument('--as-of', required=True)
    parser.add_argument('--input', help='Private saved community input JSON')
    parser.add_argument('--retention-days', type=int, default=30)
    args = parser.parse_args(argv)
    try:
        store = CommunityStore(args.database)
        if args.input:
            path = Path(args.input)
            if path.stat().st_size > 500_000:
                raise ValueError('Community input exceeds limit')
            result = store.ingest(json.loads(path.read_text()), received_at=args.as_of,
                                  retention_days=args.retention_days)
        else:
            result = store.snapshot(as_of=args.as_of)
    except (ValueError, OSError, sqlite3.Error):
        print(json.dumps({'status': 'refused', 'network_calls': 0}))
        return 1
    # The CLI prints counts only: review text stays in the private database.
    counts = {state: sum(p.get('status') == state for p in result['leads'])
              for state in ('removed', 'expired')}
    print(json.dumps({'status': 'complete', 'leads': len(result['leads']),
                      **counts, 'network_calls': 0, 'guidance_approved': False}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
