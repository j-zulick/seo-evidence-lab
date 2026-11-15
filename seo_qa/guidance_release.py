"""Approve a reviewed news proposal into an immutable, versioned guidance release.

This closes the v0.2 chain: saved discovery -> human triage decision ->
proposal -> human approval -> guidance release -> affected-audit comparison.

Rules:
- A release needs a proposal AND a later-or-equal `accept_for_review` decision
  on the same news version by the same declared reviewer. The reviewer must
  differ from the proposal's declared author. Declarations are not
  authenticated identities; the report says so.
- Releases are append-only. SQLite triggers refuse UPDATE and DELETE.
  Each release links to the previous one, forming a verifiable chain.
- Approving a release changes no stored audit. `affected` replays supplied audit
  inputs under the released guidance and reports which findings would change.
No network calls, no model calls.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from .competitor import audit, compare
from .news_bridge import database, digest

SCHEMA = 'guidance-release/v1'
_DDL = '''
CREATE TABLE IF NOT EXISTS guidance_releases(
    id TEXT PRIMARY KEY, sequence INTEGER UNIQUE NOT NULL, proposal_id TEXT UNIQUE NOT NULL
        REFERENCES news_proposals(id), record TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS guidance_releases_no_update BEFORE UPDATE ON guidance_releases
    BEGIN SELECT RAISE(ABORT, 'guidance releases are immutable'); END;
CREATE TRIGGER IF NOT EXISTS guidance_releases_no_delete BEFORE DELETE ON guidance_releases
    BEGIN SELECT RAISE(ABORT, 'guidance releases are immutable'); END;
'''


class ReleaseError(ValueError):
    pass


def _bounded(value, name, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ReleaseError(f'Expected {name} of 1-{limit} characters')
    return value.strip()


def approve(path, proposal_id, reviewer, note, *, clock=None):
    reviewer = _bounded(reviewer, 'reviewer', 100)
    note = _bounded(note, 'approval note', 2000)
    now = (clock or (lambda: datetime.now(timezone.utc)))().isoformat()
    with database(path) as db:
        db.executescript(_DDL)
        row = db.execute('SELECT version_id, record FROM news_proposals WHERE id=?', (proposal_id,)).fetchone()
        if row is None:
            raise ReleaseError('Unknown proposal')
        proposal = json.loads(row['record'])
        if digest({k: v for k, v in proposal.items() if k != 'id'}) != proposal_id:
            raise ReleaseError('Proposal record does not match its id')
        if proposal.get('active') is not False or proposal.get('status') != 'proposed_unreviewed':
            raise ReleaseError('Only inactive, unreviewed proposals can be approved')
        if reviewer.casefold() == str(proposal.get('declared_author', '')).strip().casefold():
            raise ReleaseError('The reviewer must differ from the proposal author')
        decisions = db.execute('SELECT action, actor, note, recorded_at FROM news_decisions '
                               'WHERE version_id=? ORDER BY id', (row['version_id'],)).fetchall()
        mine = [d for d in decisions if d['actor'] == reviewer]
        if not mine or mine[-1]['action'] != 'accept_for_review':
            raise ReleaseError("Reviewer's latest triage decision on this source must be accept_for_review")
        existing = db.execute('SELECT record FROM guidance_releases WHERE proposal_id=?', (proposal_id,)).fetchone()
        if existing:
            return json.loads(existing['record'])
        last = db.execute('SELECT id, sequence FROM guidance_releases ORDER BY sequence DESC LIMIT 1').fetchone()
        record = {
            'schema': SCHEMA, 'sequence': (last['sequence'] + 1) if last else 1,
            'previous_release_id': last['id'] if last else None,
            'proposal_id': proposal_id, 'news_version_id': row['version_id'],
            'source_url': proposal['source_url'], 'source_synthetic': proposal['synthetic'],
            'import_provenance': proposal['import_provenance'],
            'guidance': proposal['proposed_guidance'],
            'guidance_hash': proposal['proposed_guidance_hash'],
            'proposal_author': proposal['declared_author'], 'mapping_reason': proposal['mapping_reason'],
            'reviewer': reviewer, 'approval_note': note,
            'triage_decision': dict(mine[-1]), 'approved_at': now,
            'identity_authenticated': False, 'source_truth_verified': False,
            'audits_changed': False, 'network_calls': 0,
        }
        if digest(record['guidance']) != record['guidance_hash']:
            raise ReleaseError('Proposed guidance does not match its hash')
        record['id'] = digest(record)
        db.execute('INSERT INTO guidance_releases VALUES(?,?,?,?)',
                   (record['id'], record['sequence'], proposal_id, json.dumps(record, sort_keys=True)))
    return record


def releases(path):
    with database(path) as db:
        db.executescript(_DDL)
        rows = [json.loads(r['record']) for r in
                db.execute('SELECT record FROM guidance_releases ORDER BY sequence')]
    previous = None
    for record in rows:
        body = {k: v for k, v in record.items() if k != 'id'}
        if digest(body) != record['id'] or record['previous_release_id'] != previous:
            raise ReleaseError('Guidance release chain failed verification')
        previous = record['id']
    return {'schema': 'guidance-release-chain/v1', 'releases': rows, 'verified': True,
            'current': rows[-1] if rows else None}


def affected(path, release_id, audit_inputs):
    """Replay each supplied audit under the released guidance. Changes nothing stored."""
    chain = releases(path)['releases']
    release = next((r for r in chain if r['id'] == release_id), None)
    if release is None:
        raise ReleaseError('Unknown guidance release')
    if not isinstance(audit_inputs, list) or not 1 <= len(audit_inputs) <= 500:
        raise ReleaseError('Expected a list of 1-500 audit inputs')
    rows = []
    for inputs in audit_inputs:
        before = json.loads(json.dumps(inputs, allow_nan=False))
        after = before | {'guidance': release['guidance']}
        replay = compare(before, after)
        rows.append({'audit_id': audit(before)['id'], 'before': replay['before']['finding'],
                     'after': replay['after']['finding'], 'status': replay['status'],
                     'changed': replay['before']['finding'] != replay['after']['finding']})
    return {'schema': 'guidance-impact/v1', 'release_id': release_id, 'sequence': release['sequence'],
            'guidance': release['guidance'], 'audits': rows,
            'changed': sum(r['changed'] for r in rows), 'total': len(rows),
            'interpretation': 'Counterfactual replay of supplied inputs; stored audits are unchanged.',
            'network_calls': 0}


def main(argv=None):
    p = argparse.ArgumentParser(prog='python -m seo_qa.guidance_release')
    p.add_argument('action', choices=['approve', 'list', 'affected'])
    p.add_argument('--db', type=Path, required=True)
    p.add_argument('--proposal-id')
    p.add_argument('--release-id')
    p.add_argument('--reviewer')
    p.add_argument('--note')
    p.add_argument('--audits', type=Path, help='JSON list of audit inputs')
    a = p.parse_args(argv)
    if a.action == 'approve':
        if not all([a.proposal_id, a.reviewer, a.note]):
            p.error('approve requires --proposal-id, --reviewer and --note')
        result = approve(a.db, a.proposal_id, a.reviewer, a.note)
    elif a.action == 'affected':
        if not (a.release_id and a.audits):
            p.error('affected requires --release-id and --audits')
        result = affected(a.db, a.release_id, json.loads(a.audits.read_text()))
    else:
        result = releases(a.db)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
