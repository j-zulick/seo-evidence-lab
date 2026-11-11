import copy
import hashlib
import json
import pytest
from seo_qa.external_diagnostics import import_saved, compare


def payload():
    return {'schema_version': 1, 'command': 'audit', 'ok': True, 'error': None,
            'cli_version': '1.2.1', 'scoring_version': '4.0', 'data_version': '2026.09',
            'normalizer_version': 3, 'run_id': 'synthetic-test', 'observed_at': '2026-11-11T10:00:00Z',
            'evidence': {'content_hash': 'a'*64, 'normalizer_version': 3},
            'completeness': {'computed': 2, 'total': 3, 'missing': ['render']},
            'scores': {'composite': 50},
            'signals': [{'class': 'deterministic', 'id': 'structure', 'value': 5, 'max': 10},
                        {'class': 'deterministic', 'id': 'metadata', 'value': 5, 'max': 10},
                        {'class': 'heuristic', 'id': 'render', 'value': None, 'max': 10, 'skipped_reason': 'browser unavailable'}]}


def admitted(data=None, **bindings):
    raw = json.dumps(data or payload()).encode()
    options = dict(expected_raw_sha256=hashlib.sha256(raw).hexdigest(), source_sha256='b'*64,
                   engine_commit='c'*40, scope={'target': 'https://example.test', 'rendering': 'raw', 'page_set_sha256': 'd'*64}, evidence_kind='synthetic')
    options.update(bindings)
    return import_saved(raw, **options)


def test_roundtrip_pins_bytes_and_preserves_unknown():
    result = admitted()
    assert result['result'][2]['value'] is None
    assert result['result'][2]['outcome'] == 'unmeasured'
    assert result['raw_output'] == payload()
    assert result['review'] == 'external_diagnostic_candidate'
    assert result['input']['binding'] == 'caller_asserted'
    assert admitted() == result


@pytest.mark.parametrize('key,value', [('schema_version', 2), ('ok', False), ('command', 'crawl'), ('observed_at', '2026-11-11'), ('scoring_version', None)])
def test_rejects_unadmitted_contracts(key, value):
    data = payload(); data[key] = value
    with pytest.raises(ValueError): admitted(data)


def test_detached_bytes_rejected():
    with pytest.raises(ValueError, match='hash mismatch'): admitted(expected_raw_sha256='0'*64)


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -1, 11, True])
def test_invalid_values_rejected(value):
    data = payload(); data['signals'][0]['value'] = value
    with pytest.raises(ValueError): admitted(data)


def test_missing_denominator_and_duplicate_signals_rejected():
    for mutate in (lambda d: d['completeness'].update(computed=3), lambda d: d['signals'].append(d['signals'][0]), lambda d: d['signals'][1].update(id='structure'), lambda d: d['evidence'].update(normalizer_version=4)):
        data = payload(); mutate(data)
        with pytest.raises(ValueError): admitted(data)


def test_rule_changes_cannot_cancel():
    data = payload(); data['signals'][0]['value'] = 7; data['signals'][1]['value'] = 3
    result = compare(admitted(), admitted(data))
    assert result['compatible']
    assert [r['delta'] for r in result['changes']] == [2, -2]


@pytest.mark.parametrize('change', ['scoring', 'scope', 'coverage', 'kind', 'denominator'])
def test_incompatible_comparisons_refused(change):
    before = admitted(); after = copy.deepcopy(before)
    if change == 'scoring': after['engine']['scoring_version'] = '5'
    if change == 'scope': after['input']['scope']['rendering'] = 'rendered'
    if change == 'coverage': after['measurement']['completeness']['computed'] = 1
    if change == 'kind': after['provenance']['evidence_kind'] = 'saved_external'
    if change == 'denominator': after['result'][0]['maximum'] = 20
    assert compare(before, after)['compatible'] is False


def test_advisory_and_not_applicable_stay_outside_denominator():
    data = payload()
    data['signals'].extend([
        {'class': 'advisory', 'id': 'helpfulness', 'value': None, 'max': 0},
        {'class': 'deterministic', 'id': 'inapplicable', 'value': None, 'max': 10, 'skipped_reason': 'not applicable: scope'}])
    result = admitted(data)
    assert result['measurement']['total'] == 3
    assert len(result['result']) == 5


def test_contradictory_missing_signals_refused():
    data = payload(); data['completeness']['missing'] = []
    with pytest.raises(ValueError, match='Missing signal'): admitted(data)


def test_changed_signal_page_denominator_refused():
    before = payload(); before['signals'][0]['detail'] = {'pages_measured': 5, 'pages_total': 5}
    after = copy.deepcopy(before); after['signals'][0]['detail']['pages_measured'] = 1
    after['signals'][0]['value'] = 9
    assert not compare(admitted(before), admitted(after))['compatible']


def test_changed_failed_pages_refused():
    before = payload(); before['evidence']['pages_failed'] = [{'url': 'https://example.test/a', 'status': 404}]
    after = copy.deepcopy(before); after['evidence']['pages_failed'][0]['url'] = 'https://example.test/b'
    assert not compare(admitted(before), admitted(after))['compatible']
