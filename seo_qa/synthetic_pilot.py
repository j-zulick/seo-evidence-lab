"""Offline review-card pilot; expected outcomes are never imported here."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from seo_qa.inspector import inspect_html
from seo_qa.models import RequirementInput
from seo_qa.store import Store

CHECK_KEYS = {'canonical', 'indexable', 'crawlable', 'required_text',
              'allowed_statuses', 'redirect_final', 'max_redirects'}


def review_card(case, state_dir):
    if case['domain'] != 'seo' or case['origin'] != 'synthetic':
        raise ValueError('This pilot accepts synthetic SEO cases only')
    promises = case['promises']
    counts = Counter(p['key'] for p in promises)
    conflicts = {key for key, n in counts.items() if n > 1}
    gaps, expected = [], {}
    for promise in promises:
        key = promise['key']
        if key in conflicts or key not in CHECK_KEYS:
            gaps.append(dict(promise, reason='duplicate/contradictory key' if key in conflicts else 'unsupported observation'))
        else:
            expected[key] = promise['value']
    findings, evidence_hash, report = [], None, None
    if expected:
        RequirementInput(title=case['id'], urls=[case['url']], expected=expected)
        store = Store(Path(state_dir) / 'seo.sqlite', owner_id='s10-synthetic-seo')
        req = store.create_requirement(case['brief'], [case['url']], expected)
        run = store.create_run(req['id'])
        store.execute_run(run['id'], lambda url, exp: inspect_html(url=url, expected=exp, **case['capture']))
        report = store.export_report(run['id'])
        findings = [f for f in report['findings'] if f['outcome'] != 'not_applicable']
        evidence_hash = report['observations'][0]['evidence']['sha256']
    outcomes = {f['outcome'] for f in findings}
    decision = ('clarify' if conflicts else 'scope_incomplete' if gaps else
                'recapture' if outcomes & {'blocked', 'inconclusive'} else
                'reject_or_scoped_waiver' if 'fail' in outcomes else 'ready_for_review')
    # Full-brief readiness is deliberately stricter than supported-subset acceptance.
    accept_allowed = decision == 'ready_for_review'
    review_error = None
    if report and not gaps:
        try:
            store.review(run['id'], 'accepted', 'Synthetic pilot technical review only', expected_version=1)
        except ValueError as error:
            review_error = str(error)
        report = store.export_report(run['id'])
        if report['acceptance_current'] != accept_allowed:
            raise AssertionError('Review-card and Store acceptance disagree')
    return {'id': case['id'], 'domain': 'seo', 'origin': 'synthetic',
            'brief': case['brief'], 'decision': decision, 'findings': findings,
            'gaps': gaps, 'supported': len(promises) - len(gaps),
            'promise_count': len(promises), 'evidence_sha256': evidence_hash,
            'accept_allowed': accept_allowed, 'store_review_error': review_error,
            'report': report, 'review_items': len(findings) + len(gaps)}


def summarize(cards):
    return {'promises': sum(c['promise_count'] for c in cards),
            'supported': sum(c['supported'] for c in cards),
            'gaps': sum(len(c['gaps']) for c in cards),
            'findings': sum(len(c['findings']) for c in cards),
            'exceptions': sum(f['outcome'] != 'pass' for c in cards for f in c['findings']),
            'ready_cases': sum(c['accept_allowed'] for c in cards)}


def load_cases():
    return json.loads((Path(__file__).resolve().parents[1] / 'fixtures/synthetic-pilot/briefs.v1.json').read_text())
