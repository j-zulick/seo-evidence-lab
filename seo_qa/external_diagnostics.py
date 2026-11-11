"""Saved GEO diagnostics intake. No fetching, rule activation or inferred approval."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re

SCHEMA = 'external-diagnostic-v1'
ENGINE = 'seo-skills/geo-audit-skill'
MAX_BYTES = 2_000_000


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
        raise ValueError('Expected a lowercase SHA256 identity')
    return value


def _number(value, *, minimum=0, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('Expected finite numeric measurement')
    if value < minimum or (maximum is not None and value > maximum):
        raise ValueError('Measurement outside declared range')
    return value


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def import_saved(raw: bytes, *, expected_raw_sha256: str, source_sha256: str,
                 engine_commit: str, scope: dict, evidence_kind: str):
    """Bind saved bytes to caller-declared source/scope, retaining upstream output.

    Caller bindings are provenance assertions, not independently verified capture
    evidence. Intake never promotes diagnostics into guidance or an audit verdict.
    """
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        raise ValueError('Saved output must be bytes, at most 2 MB')
    digest = hashlib.sha256(raw).hexdigest()
    if digest != _hash(expected_raw_sha256):
        raise ValueError('Raw artifact hash mismatch')
    _hash(source_sha256)
    if not re.fullmatch(r'[a-f0-9]{40}', engine_commit):
        raise ValueError('Pin an exact engine commit')
    if evidence_kind not in {'synthetic', 'saved_external'}:
        raise ValueError('Declare evidence kind')
    if not isinstance(scope, dict) or set(scope) != {'target', 'rendering', 'page_set_sha256'}:
        raise ValueError('Declare target, rendering and exact page-set hash')
    if not isinstance(scope['target'], str) or not scope['target'].startswith(('https://', 'http://')):
        raise ValueError('Declare HTTP(S) target')
    if scope['rendering'] not in {'raw', 'rendered', 'mixed'}:
        raise ValueError('Unknown rendering scope')
    _hash(scope['page_set_sha256'])
    data = json.loads(raw, object_pairs_hook=_unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))
    if not isinstance(data, dict) or type(data.get('schema_version')) is not int or data['schema_version'] != 1:
        raise ValueError('Unsupported upstream schema')
    if data.get('ok') is not True or data.get('command') not in {'audit', 'rescore'} or data.get('error') is not None:
        raise ValueError('Only successful saved audit/rescore output is admitted')
    for key in ('cli_version', 'scoring_version', 'data_version', 'normalizer_version', 'run_id', 'observed_at'):
        if data.get(key) is None or str(data[key]).strip() == '':
            raise ValueError('Missing upstream identity: ' + key)
    stamp = datetime.fromisoformat(data['observed_at'].replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('Observation time needs timezone')
    evidence = data.get('evidence', {})
    _hash(evidence.get('content_hash'))
    if evidence.get('normalizer_version') != data['normalizer_version']:
        raise ValueError('Normalizer identity mismatch')
    completeness = data.get('completeness', {})
    computed = _number(completeness.get('computed'))
    total = _number(completeness.get('total'))
    if type(computed) is not int or type(total) is not int or computed > total:
        raise ValueError('Invalid measured denominator')
    signals = data.get('signals')
    if not isinstance(signals, list):
        raise ValueError('Signal denominator mismatch')
    findings, seen = [], set()
    eligible = []
    for index, signal in enumerate(signals):
        identity = (signal.get('id'), signal.get('page'))
        if not isinstance(identity[0], str) or not identity[0] or identity in seen:
            raise ValueError('Missing or duplicate signal identity')
        seen.add(identity)
        signal_class = signal.get('class')
        if signal_class not in {'deterministic', 'heuristic', 'live', 'advisory'}:
            raise ValueError('Unknown signal class')
        applicable = not (signal.get('skipped_reason') or '').startswith('not applicable')
        if signal_class != 'advisory' and applicable:
            eligible.append(signal)
        maximum = _number(signal.get('max'))
        value = signal.get('value')
        if value is not None:
            _number(value, maximum=maximum)
        findings.append({'rule_id': identity[0], 'page': identity[1], 'value': value,
                         'class': signal_class, 'maximum': maximum,
                         'coverage': {k: signal.get('detail', {}).get(k) for k in ('pages_measured', 'pages_total')}, 'outcome': 'unmeasured' if value is None else 'measured',
                         'reason': signal.get('skipped_reason'), 'locator': '/signals/' + str(index)})
    if len(eligible) != total or sum(s.get('value') is not None for s in eligible) != computed:
        raise ValueError('Computed signal count mismatch')
    missing = [s['id'] for s in eligible if s.get('value') is None]
    if sorted(completeness.get('missing', [])) != sorted(missing):
        raise ValueError('Missing signal identities mismatch')
    scores = data.get('scores', {})
    score = scores.get('composite')
    if score is not None:
        _number(score, maximum=100)
    if computed == 0 and score is not None:
        raise ValueError('Unmeasured audit cannot carry a passing/zero score')
    return {'adapter_schema': SCHEMA,
            'engine': {'repo': ENGINE, 'commit': engine_commit, **{k: data[k] for k in ('cli_version', 'schema_version', 'scoring_version', 'data_version', 'normalizer_version')}},
            'operation': {'id': data['run_id'], 'command': data['command'], 'observed_at': data['observed_at']},
            'input': {'source_sha256': source_sha256, 'scope': deepcopy(scope), 'upstream_content_hash': evidence['content_hash'], 'binding': 'caller_asserted'},
            'provenance': {'raw_sha256': digest, 'evidence_kind': evidence_kind},
            'measurement': {'score': score, 'computed': computed, 'total': total, 'page_coverage': {k: deepcopy(evidence.get(k)) for k in ('pages_ok', 'pages_failed')}, 'completeness': deepcopy(completeness)},
            'result': findings, 'review': 'external_diagnostic_candidate', 'raw_output': data}


def compare(before: dict, after: dict):
    """Refuse incompatible comparisons and report per-rule changes independently."""
    for key in ('adapter_schema', 'engine'):
        if before[key] != after[key]:
            return {'compatible': False, 'reason': key + ' changed', 'changes': []}
    for key in ('scope',):
        if before['input'][key] != after['input'][key]:
            return {'compatible': False, 'reason': key + ' changed', 'changes': []}
    if before['provenance']['evidence_kind'] != after['provenance']['evidence_kind']:
        return {'compatible': False, 'reason': 'evidence kind changed', 'changes': []}
    if any(before['measurement'][k] != after['measurement'][k] for k in ('completeness', 'page_coverage')):
        return {'compatible': False, 'reason': 'measured coverage changed', 'changes': []}
    left = {(r['rule_id'], r['page']): r for r in before['result']}
    right = {(r['rule_id'], r['page']): r for r in after['result']}
    if left.keys() != right.keys() or any((left[k]['maximum'], left[k]['class'], left[k]['coverage']) != (right[k]['maximum'], right[k]['class'], right[k]['coverage']) for k in left):
        return {'compatible': False, 'reason': 'rule set or denominator changed', 'changes': []}
    changes = [{'rule_id': k[0], 'page': k[1], 'before': left[k]['value'], 'after': right[k]['value'],
                'delta': right[k]['value'] - left[k]['value'] if left[k]['value'] is not None and right[k]['value'] is not None else None}
               for k in left if left[k]['value'] != right[k]['value']]
    return {'compatible': True, 'reason': None, 'changes': changes,
            'before_raw_sha256': before['provenance']['raw_sha256'], 'after_raw_sha256': after['provenance']['raw_sha256'],
            'causality': 'diagnostic difference only; no search or citation outcome claim'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('artifact', type=Path)
    parser.add_argument('bindings', type=Path, help='JSON keyword bindings for import_saved')
    args = parser.parse_args()
    with args.artifact.open('rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    print(json.dumps(import_saved(raw, **json.loads(args.bindings.read_text())), indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
