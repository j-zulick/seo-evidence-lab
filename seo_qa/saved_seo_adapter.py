"""Offline saved SEOmator schema-2 intake. No engine execution or approval."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
from urllib.parse import urlsplit

ENGINE = {'repo': 'seo-skills/seo-audit-skill', 'commit': 'fac4b75f2059b6d74bb05a865fe1b0ca5de3ba66',
          'package': '7.0.0', 'schema': 2, 'scoring': 4}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError('Bounded URL required')
    parsed = urlsplit(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Public URL identity required')
    return value


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 100


def admit(raw_bytes, envelope, page_text):
    """Validate original bytes and page evidence before exposing diagnostic rows.

    This deliberately supports completed single-page schema-2 output only;
    aggregated crawls and older encodings require a separate adapter version.
    """
    if not isinstance(raw_bytes, bytes) or len(raw_bytes) > 2_000_000:
        raise ValueError('Raw JSON bytes exceed bound')
    if not isinstance(page_text, str) or len(page_text) > 200_000:
        raise ValueError('Retained bounded page text required')
    if not isinstance(envelope, dict) or envelope.get('adapter_schema') != 'saved-seo/v1':
        raise ValueError('Adapter schema required')
    if envelope.get('engine') != ENGINE:
        raise ValueError('Unsupported exact engine identity')
    operation = envelope.get('operation', {})
    if not isinstance(operation, dict):
        raise ValueError('Operation object required')
    if operation.get('status') != 'completed' or not isinstance(operation.get('id'), str) or not operation['id']:
        raise ValueError('Completed operation identity required')
    try:
        stamp = datetime.fromisoformat(operation['captured_at'])
        if stamp.tzinfo is None:
            raise ValueError('Capture offset required')
    except (KeyError, TypeError):
        raise ValueError('Capture timestamp required') from None
    provenance = envelope.get('provenance', {})
    if not isinstance(provenance, dict):
        raise ValueError('Provenance object required')
    if provenance.get('raw_sha256') != sha(raw_bytes) or provenance.get('evidence_kind') not in ('synthetic', 'saved_external_diagnostic'):
        raise ValueError('Raw evidence hash or kind mismatch')
    source = envelope.get('input', {})
    if not isinstance(source, dict):
        raise ValueError('Input evidence object required')
    if source.get('page_sha256') != sha(page_text.encode()) or source.get('rendering') not in ('raw', 'rendered'):
        raise ValueError('Page evidence hash or rendering mismatch')
    if not isinstance(source.get('page_version'), str) or not source['page_version']:
        raise ValueError('Page version required')
    requested, final = (_url(source.get(k)) for k in ('requested_url', 'final_url'))
    if source.get('scope') != 'single_page':
        raise ValueError('Only explicit single page scope is supported')
    raw = json.loads(raw_bytes)
    if not isinstance(raw, dict) or raw.get('schemaVersion') != 2 or raw.get('url') != final:
        raise ValueError('Upstream schema or target mismatch')
    if not _number(raw.get('overallScore')) or (type(raw.get('crawledPages')) is not int or raw.get('crawledPages') != 1):
        raise ValueError('Bounded single page measurement required')
    run = raw.get('run')
    if not isinstance(run, dict) or run.get('scoringVersion') != 4 or run.get('crawl') is not False:
        raise ValueError('Explicit scoring and non-crawl run required')
    if any(type(run.get(k)) is not bool for k in ('measureCwv', 'mobile', 'simulateInteraction')):
        raise ValueError('Explicit run measurement modes required')
    if raw.get('coverage') != {'pages': [final], 'detail': 'per-page'}:
        raise ValueError('Exact page coverage required')
    categories = raw.get('categoryResults')
    if not isinstance(categories, list) or not categories or len(categories) > 30:
        raise ValueError('Nonempty bounded categories required')
    rows, seen, seen_categories = [], set(), set()
    for category in categories:
        if not isinstance(category, dict) or not isinstance(category.get('categoryId'), str) or category['categoryId'] in seen_categories:
            raise ValueError('Unique category required')
        seen_categories.add(category['categoryId'])
        if not _number(category.get('score')) or not isinstance(category.get('results'), list) or len(category['results']) > 1000:
            raise ValueError('Invalid category score/results')
        counts = dict.fromkeys(('pass', 'warn', 'fail', 'not-measured'), 0)
        for result in category['results']:
            if not isinstance(result, dict) or not isinstance(result.get('ruleId'), str) or not result['ruleId'] or result['ruleId'] in seen:
                raise ValueError('Unique rule identity required')
            seen.add(result['ruleId'])
            status = result.get('status')
            if not isinstance(status, str) or status not in counts or not _number(result.get('score')) or not isinstance(result.get('message'), str):
                raise ValueError('Unsupported rule outcome')
            if status == 'not-measured' and result.get('weight') != 0:
                raise ValueError('Not measured must be excluded from scoring')
            counts[status] += 1
            rows.append({'rule_id': result['ruleId'], 'category_id': category['categoryId'],
                         'outcome': status, 'message': result['message'], 'score': result['score'],
                         'source_locator': {'raw_sha256': sha(raw_bytes), 'rule_id': result['ruleId'], 'page_version': source['page_version']}})
        for status, key in [('pass', 'passCount'), ('warn', 'warnCount'), ('fail', 'failCount'), ('not-measured', 'notMeasuredCount')]:
            if type(category.get(key, 0)) is not int or category.get(key, 0) != counts[status]:
                raise ValueError('Rule count mismatch')
    if not rows:
        raise ValueError('Empty audit is not an all-clear result')
    return {'adapter_schema': 'saved-seo/v1', 'engine': deepcopy(ENGINE), 'operation': deepcopy(operation),
            'input': deepcopy(source), 'provenance': deepcopy(provenance), 'result': rows,
            'raw_result': raw, 'normalized_sha256': sha(canonical(rows)),
            'measurement': {'score': raw['overallScore'], 'measured_denominator': sum(r['outcome'] != 'not-measured' for r in rows),
                            'not_measured': sum(r['outcome'] == 'not-measured' for r in rows),
                            'classification': provenance['evidence_kind'], 'completeness': 'selected checks only'},
            'review': 'external diagnostic candidate; approval and activation absent'}


def compare(before, after):
    """Diff by rule ID, refusing incompatible identity/modes and retained scope."""
    for key in ('engine',):
        if before[key] != after[key]:
            raise ValueError('Incompatible engine identity')
    for key in ('requested_url', 'final_url', 'scope', 'rendering'):
        if before['input'][key] != after['input'][key]:
            raise ValueError('Incompatible measured scope')
    for key in ('run', 'coverage'):
        if before['raw_result'][key] != after['raw_result'][key]:
            raise ValueError('Incompatible run configuration or coverage')
    if before['provenance']['evidence_kind'] != after['provenance']['evidence_kind']:
        raise ValueError('Synthetic and external measurements cannot be mixed')
    old, new = ({r['rule_id']: r for r in x['result']} for x in (before, after))
    changes = []
    for rule_id in sorted(old.keys() | new.keys()):
        left, right = old.get(rule_id), new.get(rule_id)
        if left is None or right is None:
            outcome = 'added' if left is None else 'removed'
        elif left['outcome'] == right['outcome']:
            continue
        else:
            outcome = 'changed'
        changes.append({'rule_id': rule_id, 'change': outcome,
                        'before': left['outcome'] if left else None, 'after': right['outcome'] if right else None,
                        'before_locator': left['source_locator'] if left else None,
                        'after_locator': right['source_locator'] if right else None})
    return {'compatible': True, 'changes': changes, 'aggregate_cancellation_allowed': False,
            'waived_caps': {'before': before['raw_result'].get('waivedCaps', []), 'after': after['raw_result'].get('waivedCaps', [])}}
