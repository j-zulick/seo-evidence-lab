"""Saved news exports to a local review inbox. No collection or guidance activation."""
import argparse
from contextlib import contextmanager
from datetime import date, datetime, timezone
from hashlib import sha256
from html.parser import HTMLParser
import ipaddress
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit, urlunsplit

MAX_BYTES = 2_000_000
MAX_ITEMS = 200
EMAIL = re.compile(r'[A-Z0-9.!#$%&*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}', re.I)


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                             allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def source_url(value):
    if not isinstance(value, str) or len(value) > 2048 or any(c.isspace() for c in value):
        raise ValueError('Expected a bounded public HTTPS source URL')
    try:
        p = urlsplit(value)
        port = p.port
    except ValueError as exc:
        raise ValueError('Invalid source URL') from exc
    if p.scheme != 'https' or not p.hostname or p.username or p.password or '\\' in value:
        raise ValueError('Expected a public HTTPS URL without credentials')
    host = p.hostname.lower()
    if host in {'localhost', 'docs.google.com', 'drive.google.com'} or host.endswith(('.local', '.internal')):
        raise ValueError('Private/navigation source is not an article observation')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError('Private source address is not supported')
    netloc = ('[' + host + ']' if ':' in host else host) + (':' + str(port) if port and port != 443 else '')
    return urlunsplit(('https', netloc, p.path or '/', p.query, ''))


def timestamp(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError('Expected a date, timezone-aware timestamp, or null')
    try:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            return date.fromisoformat(value).isoformat()
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is not None:
            return parsed.isoformat()
    except ValueError:
        pass
    raise ValueError('Expected a valid date or timezone-aware timestamp')


class DigestHTML(HTMLParser):
    """Extract paragraph/headline text without retaining mail headers or raw HTML."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks, self.current, self.anchor, self.hidden = [], None, None, 0

    def flush(self):
        if self.current:
            self.current['text'] = ' '.join(self.current['text'].split())
            self.blocks.append(self.current)
        self.current, self.anchor = None, None

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style'}:
            self.hidden += 1
        if self.hidden:
            return
        if tag in {'h1', 'h2', 'h3', 'p', 'li'}:
            self.flush()
            self.current = {'tag': tag, 'text': '', 'links': []}
        if tag == 'a' and self.current:
            self.anchor = {'url': dict(attrs).get('href', ''), 'text': ''}
            self.current['links'].append(self.anchor)

    def handle_endtag(self, tag):
        if tag in {'script', 'style'} and self.hidden:
            self.hidden -= 1
            return
        if self.hidden:
            return
        if tag == 'a':
            self.anchor = None
        if tag in {'h1', 'h2', 'h3', 'p', 'li'}:
            self.flush()

    def handle_data(self, data):
        if self.current and not self.hidden:
            self.current['text'] += data
            if self.anchor is not None:
                self.anchor['text'] += data


def email_items(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('payload'), dict):
        raise ValueError('Expected a saved news email envelope')
    envelope = payload['payload']
    if envelope.get('mime_type') != 'text/html' or not isinstance(envelope.get('body'), dict):
        raise ValueError('Expected saved text/html digest content')
    content = envelope['body'].get('content')
    if not isinstance(content, str):
        raise ValueError('Expected saved digest body text')
    parser = DigestHTML()
    parser.feed(content)
    parser.flush()
    stories, current, topic = [], None, None
    for block in parser.blocks:
        if block['tag'] in {'h1', 'h2', 'h3'}:
            current = None
            topic = block['text'][:120] or None
            continue
        links = block['links']
        headline = links[0] if len(links) == 1 and ' '.join(links[0]['text'].split()) == block['text'] else None
        if headline:
            current = None
            try:
                url = source_url(headline['url'])
            except ValueError:
                continue
            current = {'url': url, 'title': block['text'], 'paragraphs': [], 'topics': [topic] if topic else []}
            stories.append(current)
        elif current and block['text']:
            current['paragraphs'].append(block['text'])
    result = []
    for item in stories:
        paragraphs = item.pop('paragraphs')
        metadata = paragraphs[0] if paragraphs else ''
        published = re.search(r'\bPublished\s*:?\s*(\d{4}-\d{2}-\d{2})\b', metadata)
        result.append(item | {'summary': '\n\n'.join(paragraphs),
                              'published_at': published[1] if published else None,
                              'observed_at': None, 'source_kind': 'saved_digest_excerpt'})
    if not result:
        raise ValueError('No supported article-headline blocks in saved digest')
    # Digest footers can repeat recipient addresses inside body paragraphs.
    # These are minimized excerpts, never a claim to retain publisher verbatim text.
    for row in result:
        row['title'] = EMAIL.sub('[email address omitted]', row['title'])
        row['summary'] = EMAIL.sub('[email address omitted]', row['summary'])
    return result


def normalize(data, format, synthetic):
    if type(synthetic) is not bool:
        raise ValueError('Synthetic origin must be explicit boolean')
    if format == 'digest-email':
        items = email_items(data)
    elif format == 'normalized':
        if not isinstance(data, dict) or data.get('schema') != 'seo-news-export/v1' or data.get('synthetic') is not synthetic:
            raise ValueError('Expected matching seo-news-export/v1 and synthetic declaration')
        items = data.get('items')
    else:
        raise ValueError('Unsupported saved export format')
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        raise ValueError('Expected 1–200 saved observations')
    rows = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('Expected observation object')
        title, summary, topics = item.get('title'), item.get('summary', ''), item.get('topics', [])
        if not isinstance(title, str) or not title.strip() or len(title) > 500:
            raise ValueError('Expected title of 1–500 characters')
        if not isinstance(summary, str) or len(summary) > 12_000:
            raise ValueError('Summary exceeds 12000 characters')
        if not isinstance(topics, list) or len(topics) > 10 or any(not isinstance(t, str) or len(t) > 120 for t in topics):
            raise ValueError('Expected bounded topic names')
        kind = item.get('source_kind', 'saved_digest_excerpt')
        if kind not in {'saved_digest_excerpt', 'saved_page_capture'}:
            raise ValueError('Unsupported source kind')
        row = {'url': source_url(item.get('url')), 'title': title.strip(), 'summary': summary,
               'topics': list(topics), 'published_at': timestamp(item.get('published_at')),
               'observed_at': timestamp(item.get('observed_at')), 'source_kind': kind, 'synthetic': synthetic}
        if row['observed_at'] and len(row['observed_at']) == 10:
            raise ValueError('Observation time needs timezone-aware precision')
        if row['observed_at'] and row['published_at'] and len(row['published_at']) > 10:
            if datetime.fromisoformat(row['observed_at']) < datetime.fromisoformat(row['published_at']):
                raise ValueError('Observation precedes publication time')
        row['story_id'] = digest(row['url'])
        row['version_id'] = digest(row)
        rows.append(row)
    return rows


@contextmanager
def database(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript('''
        CREATE TABLE IF NOT EXISTS news_imports(id TEXT PRIMARY KEY, format TEXT, imported_at TEXT, synthetic INTEGER);
        CREATE TABLE IF NOT EXISTS news_import_provenance(import_id TEXT PRIMARY KEY REFERENCES news_imports(id), export_hash TEXT);
        CREATE TABLE IF NOT EXISTS news_versions(id TEXT PRIMARY KEY, story_id TEXT, record TEXT);
        CREATE TABLE IF NOT EXISTS news_observations(import_id TEXT REFERENCES news_imports(id), version_id TEXT REFERENCES news_versions(id), PRIMARY KEY(import_id,version_id));
        CREATE TABLE IF NOT EXISTS news_attempts(id INTEGER PRIMARY KEY, export_hash TEXT, status TEXT, recorded_at TEXT);
        CREATE TABLE IF NOT EXISTS news_decisions(id INTEGER PRIMARY KEY, version_id TEXT REFERENCES news_versions(id), action TEXT, actor TEXT, note TEXT, recorded_at TEXT);
        CREATE TABLE IF NOT EXISTS news_proposals(id TEXT PRIMARY KEY, version_id TEXT REFERENCES news_versions(id), record TEXT);
    ''')
    try:
        with db:
            yield db
    finally:
        db.close()


def import_saved(path, raw, format='digest-email', synthetic=False):
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        raise ValueError('Saved export must be at most 2 MB')
    export_hash = sha256(raw).hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    try:
        def bad_constant(value):
            raise ValueError('Nonfinite JSON refused')
        def unique_keys(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError('Duplicate JSON key refused')
                value[key] = item
            return value
        rows = normalize(json.loads(raw, parse_constant=bad_constant, object_pairs_hook=unique_keys), format, synthetic)
    except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
        with database(path) as db:
            db.execute('INSERT INTO news_attempts(export_hash,status,recorded_at) VALUES(?,?,?)', (export_hash, 'rejected', now))
        raise ValueError('Invalid saved news export: ' + str(exc)) from exc
    key = digest([export_hash, format, synthetic])
    with database(path) as db:
        duplicate = db.execute('SELECT 1 FROM news_imports WHERE id=?', (key,)).fetchone() is not None
        db.execute('INSERT OR IGNORE INTO news_imports VALUES(?,?,?,?)', (key, format, now, synthetic))
        db.execute('INSERT OR IGNORE INTO news_import_provenance VALUES(?,?)', (key, export_hash))
        for row in rows:
            db.execute('INSERT OR IGNORE INTO news_versions VALUES(?,?,?)', (row['version_id'], row['story_id'], json.dumps(row, ensure_ascii=False)))
            db.execute('INSERT OR IGNORE INTO news_observations VALUES(?,?)', (key, row['version_id']))
        db.execute('INSERT INTO news_attempts(export_hash,status,recorded_at) VALUES(?,?,?)', (export_hash, 'duplicate' if duplicate else 'imported', now))
    return {'import_id': key, 'export_hash': export_hash, 'observations': len(rows),
            'unique_versions': len({r['version_id'] for r in rows}), 'duplicate_import': duplicate,
            'synthetic': synthetic, 'network_calls': 0, 'guidance_published': False}


def report(path):
    if not Path(path).exists():
        return {'schema': 'saved-news-inbox/v1', 'items': [], 'imports': 0, 'attempts': 0, 'network_calls': 0}
    with database(path) as db:
        rows = db.execute('SELECT v.id,v.record,MAX(o.rowid) AS recent FROM news_versions v JOIN news_observations o ON v.id=o.version_id GROUP BY v.id ORDER BY recent DESC LIMIT 200').fetchall()
        items = []
        for record in rows:
            item = json.loads(record['record'])
            decision = db.execute('SELECT action,actor,note,recorded_at FROM news_decisions WHERE version_id=? ORDER BY id DESC LIMIT 1', (record['id'],)).fetchone()
            item['decision'] = dict(decision) if decision else None
            item['guidance_approved'] = False
            items.append(item)
        return {'schema': 'saved-news-inbox/v1', 'items': items,
                'imports': db.execute('SELECT COUNT(*) FROM news_imports').fetchone()[0],
                'attempts': db.execute('SELECT COUNT(*) FROM news_attempts').fetchone()[0],
                'network_calls': 0, 'order': 'recently imported observation, not publisher chronology'}


def decide(path, version_id, action, actor, note):
    if action not in {'accept_for_review', 'defer', 'reject'} or not isinstance(actor, str) or not actor.strip() or len(actor) > 100:
        raise ValueError('Expected review-only action and declared actor')
    if not isinstance(note, str) or not note.strip() or len(note) > 2000:
        raise ValueError('Expected review note of 1–2000 characters')
    with database(path) as db:
        if not db.execute('SELECT 1 FROM news_versions WHERE id=?', (version_id,)).fetchone():
            raise ValueError('Unknown saved version')
        db.execute('INSERT INTO news_decisions(version_id,action,actor,note,recorded_at) VALUES(?,?,?,?,?)',
                   (version_id, action, actor.strip(), note, datetime.now(timezone.utc).isoformat()))


def propose(path, version_id, audit_inputs, rule, actor, reason):
    """Pin a caller-proposed guidance change and replay; never activate it."""
    from .competitor import audit, compare
    if rule not in {'heading_present', 'question_heading'}:
        raise ValueError('Only implemented heading rules can be proposed')
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 100:
        raise ValueError('Expected bounded declared proposal author')
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise ValueError('Expected an explicit source-to-rule mapping reason')
    # Canonical copying avoids retaining aliases to caller-owned mutable inputs.
    before = json.loads(json.dumps(audit_inputs, allow_nan=False))
    baseline = audit(before)
    after = before | {'guidance': {'rule': rule}}
    replay = compare(before, after)
    with database(path) as db:
        row = db.execute('SELECT record FROM news_versions WHERE id=?', (version_id,)).fetchone()
        if row is None:
            raise ValueError('Unknown saved news version')
        source = json.loads(row['record'])
        provenance = [dict(x) for x in db.execute('''SELECT p.import_id,p.export_hash
            FROM news_import_provenance p JOIN news_observations o ON o.import_id=p.import_id
            WHERE o.version_id=? ORDER BY p.import_id''', (version_id,))]
        if not provenance:
            raise ValueError('Reimport the original export to bind its source hash before proposing')
        record = {'schema': 'news-guidance-proposal/v1', 'news_version_id': version_id,
                  'story_id': source['story_id'], 'source_url': source['url'],
                  'source_kind': source['source_kind'], 'synthetic': source['synthetic'],
                  'import_provenance': provenance, 'audit_id': baseline['id'],
                  'audit_pins': baseline['pins'], 'proposed_guidance': after['guidance'],
                  'proposed_guidance_hash': digest(after['guidance']), 'replay': replay,
                  'declared_author': actor.strip(), 'mapping_reason': reason,
                  'status': 'proposed_unreviewed', 'human_approval': None,
                  'active': False, 'source_truth_verified': False, 'network_calls': 0}
        record['id'] = digest(record)
        db.execute('INSERT OR IGNORE INTO news_proposals VALUES(?,?,?)', (record['id'], version_id, json.dumps(record)))
    return record


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['import', 'report', 'propose'])
    p.add_argument('--db', type=Path, required=True)
    p.add_argument('--input', type=Path)
    p.add_argument('--format', choices=['digest-email', 'normalized'], default='digest-email')
    p.add_argument('--synthetic', action='store_true')
    p.add_argument('--version-id')
    p.add_argument('--audit-inputs', type=Path)
    p.add_argument('--rule', choices=['heading_present', 'question_heading'])
    p.add_argument('--actor')
    p.add_argument('--reason')
    args = p.parse_args()
    if args.action == 'import':
        if args.input is None:
            p.error('--input is required for import')
        with args.input.open('rb') as f:
            raw = f.read(MAX_BYTES + 1)
        result = import_saved(args.db, raw, args.format, args.synthetic)
    elif args.action == 'propose':
        if not all([args.version_id, args.audit_inputs, args.rule, args.actor, args.reason]):
            p.error('propose requires --version-id, --audit-inputs, --rule, --actor and --reason')
        with args.audit_inputs.open('rb') as f:
            content = f.read(MAX_BYTES + 1)
        if len(content) > MAX_BYTES:
            p.error('Audit input file exceeds 2 MB')
        result = propose(args.db, args.version_id, json.loads(content), args.rule, args.actor, args.reason)
    else:
        result = report(args.db)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
