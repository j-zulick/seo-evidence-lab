"""Versioned, deterministic reporting projection; never a stored acceptance decision.

Applicable criterion instances have equal weight. Unknown/blocked/missing evidence
receives no credit; not-applicable checks are excluded. No I/O or model calls.
"""
from __future__ import annotations

import json
import re

VERSION = 'seo-qa-criterion-attainment/1'
FORMULA = 'floor_to_0.1(1 + 9 * passed / applicable); no applicable criteria => 1.0'
OUTCOMES = {'pass', 'fail', 'blocked', 'inconclusive', 'not_applicable'}
LEGACY_CHECKS = {'canonical': 'canonical', 'indexable': 'indexing_directives',
                 'crawlable': 'robots_access', 'redirect_final': 'redirect_destination',
                 'max_redirects': 'redirect_count', 'allowed_statuses': 'http_status'}


def score_report(report):
    """Derive one canonical payload from an already authorized, verified report.

    Numeric threshold and report completeness are separate: even a high score
    cannot establish a passing assessment when evidence or currentness is missing.
    This projection does not change the Store's stricter human-review acceptance.
    """
    requirement = report.get('requirement') or {}
    expected = requirement.get('expected') or {}
    config = expected.get('seo_qa') or {}
    criteria = config.get('criteria') or {}
    expected_urls = requirement.get('urls') or []
    observations = report.get('observations') or []
    blockers, items = [], []
    if report.get('state') != 'completed':
        blockers.append('Run is not completed.')
    if report.get('stale') or report.get('superseded'):
        blockers.append('Run is historical or superseded; it is not current.')
    if not expected_urls:
        blockers.append('Expected source coverage is unavailable.')
    observed_urls = [o.get('url') for o in observations]
    if len(set(observed_urls)) != len(observed_urls):
        blockers.append('Duplicate source observations cannot establish coverage.')
    if set(observed_urls) != set(expected_urls):
        blockers.append('Expected source coverage is incomplete or different.')
    coverage = report.get('coverage') or {}
    if coverage.get('sampled') is True:
        blockers.append('Sampled coverage cannot establish complete attainment.')

    def evidence_identity(evidence):
        # The Store verifies retained bytes. Scoring also requires an explicit
        # capture/content identity before a retained pass can earn credit.
        return next((value for key in ('content_sha256', 'sha256')
                     if isinstance(value := evidence.get(key), str)
                     and re.fullmatch(r'[0-9a-fA-F]{64}', value)), None)

    def context(observation=None, index=None):
        observation = observation or {}
        evidence = observation.get('evidence') or {}
        return {'requirement_id': requirement.get('id'),
                'requirement_version': report.get('version', requirement.get('version')),
                'requirement_content_hash': requirement.get('content_hash'),
                'criteria_version': criteria.get('version') or 'legacy-approved-expectations/v1',
                'criteria_sha256': criteria.get('sha256'),
                'source_kind': config.get('source_kind') or 'url',
                'content_format': config.get('content_format'),
                'observation_index': index, 'url': observation.get('url'),
                'final_url': observation.get('final_url'),
                'observed_at': observation.get('observed_at'),
                'evidence_sha256': evidence_identity(evidence)}

    def add(finding, ctx, detail=None, *, evidence_supported=None):
        detail = detail or {}
        check = finding.get('check') or 'unidentified_criterion'
        scope = check.split('.', 1)[0] if config and '.' in check else 'technical'
        pinned_title = dict(criteria.get('rules', {}).get(scope, [])).get(check.split('.', 1)[-1])
        outcome = finding.get('outcome')
        outcome = outcome if outcome in OUTCOMES else 'inconclusive'
        reason = finding.get('reason') or 'No explanation was retained.'
        if evidence_supported is None:
            evidence_supported = bool(ctx['evidence_sha256'])
        if outcome == 'pass' and not evidence_supported:
            outcome = 'inconclusive'
            reason = 'No valid retained evidence supports score credit. Original finding: ' + reason
        items.append({'criterion_id': finding.get('check') or 'unidentified_criterion',
                      'criterion_title': detail.get('title') or pinned_title or check,
                      'scope': detail.get('scope') or scope, 'outcome': outcome,
                      'expected': finding.get('expected'), 'observed': finding.get('observed'),
                      'reason': reason,
                      'next_step': detail.get('recommendation') or
                          'Compare the retained observation with this approved criterion; gather missing evidence before review.',
                      'context': ctx})

    def required_checks():
        if config:
            return [(f'{scope}.{key}', title)
                    for scope in config.get('scopes', [])
                    for key, title in criteria.get('rules', {}).get(scope, [])]
        return [(check, expected[key]) for key, check in LEGACY_CHECKS.items() if key in expected] + [
            ('required_text', phrase) for phrase in expected.get('required_text', [])]

    required = required_checks()
    seen_sources = set()
    for index, observation in enumerate(observations):
        url = observation.get('url')
        if url in seen_sources:
            continue  # Repeated source bytes cannot raise the aggregate score.
        seen_sources.add(url)
        ctx = context(observation, index)
        evidence_supported = bool(ctx['evidence_sha256']) and not (observation.get('evidence') or {}).get('error')
        if not evidence_supported:
            blockers.append('Retained source evidence is missing, invalid, or records capture failure.')
        detail_by_id = {row.get('id'): row for row in
                        (observation.get('evidence') or {}).get('scope_review', [])}
        seen = set()
        findings = observation.get('findings') or []
        for finding in findings:
            identity = (finding.get('check'), json.dumps(finding.get('expected'), sort_keys=True))
            if identity in seen:
                blockers.append('Duplicate criterion instances were excluded from scoring.')
                continue
            seen.add(identity)
            add(finding, ctx, detail_by_id.get(finding.get('check')), evidence_supported=evidence_supported)
        for check, target in required:
            if not any(f.get('check') == check and
                       f.get('expected') == target for f in findings):
                add({'check': check, 'expected': target, 'outcome': 'inconclusive',
                     'reason': 'No retained finding exists for this approved criterion.'}, ctx)
    for url in expected_urls:
        if url not in seen_sources:
            ctx = context({'url': url})
            for check, target in required or [('source_capture', 'complete authorized observation')]:
                add({'check': check, 'expected': target, 'outcome': 'inconclusive',
                     'reason': 'The expected source has no retained observation.'}, ctx)
    if not observations and not expected_urls:
        for finding in report.get('findings') or []:
            add(finding, context())
    applicable = [item for item in items if item['outcome'] != 'not_applicable']
    passed = sum(item['outcome'] == 'pass' for item in applicable)
    counts = {outcome: sum(item['outcome'] == outcome for item in items) for outcome in sorted(OUTCOMES)}
    # Integer arithmetic and downward quantization prevent a rounded 6.99 passing.
    score = (10 + 90 * passed // len(applicable)) / 10 if applicable else 1.0
    if not applicable:
        blockers.append('No applicable criteria have retained evidence.')
    unresolved = counts['blocked'] + counts['inconclusive']
    threshold_met = score >= 7
    status = ('incomplete' if blockers or unresolved else 'pass' if threshold_met else 'fail')
    return {'schema': VERSION, 'score': score, 'scale': [1, 10], 'passing_threshold': 7,
            'threshold_met': threshold_met, 'status': status, 'passed': status == 'pass',
            'formula': FORMULA, 'weighting': 'equal weight per applicable criterion per source',
            'meaning': 'Evidence-supported criterion attainment; not overall SEO quality or human acceptance.',
            'applicable': len(applicable), 'counts': counts,
            'completeness_blockers': list(dict.fromkeys(blockers)),
            'missed_items': [item for item in items if item['outcome'] not in {'pass', 'not_applicable'}]}


def with_scoring(report):
    """Return a shallow report copy with a freshly derived (never trusted) score."""
    return dict(report, scoring=score_report(report))
