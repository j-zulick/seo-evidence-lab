"""Saved-source measurement identity and reporting-window regressions."""
from copy import deepcopy

import pytest
from seo_qa.measurement import gsc


def capture():
    return {
        'request': {'property': 'sc-domain:owned.example',
                    'dimensions': ['page', 'date', 'query'],
                    'startDate': '2026-10-01', 'endDate': '2026-10-07'},
        'response': {'rows': [{'keys': ['https://owned.example/guide', '2026-10-03', 'guide'],
                               'clicks': 2, 'impressions': 10, 'position': 3.5}]},
    }


def test_gsc_refuses_duplicate_dimension_identity():
    payload = capture()
    payload['response']['rows'].append(deepcopy(payload['response']['rows'][0]))
    with pytest.raises(ValueError, match='Duplicate'):
        gsc(payload)


@pytest.mark.parametrize('observed', ['2026-09-30', '2026-10-08', 'not-a-date'])
def test_gsc_requires_row_date_inside_declared_request_window(observed):
    payload = capture()
    payload['response']['rows'][0]['keys'][1] = observed
    with pytest.raises(ValueError):
        gsc(payload)


@pytest.mark.parametrize('dimensions', ['page', ['page', []]])
def test_gsc_requires_dimension_name_list(dimensions):
    payload = capture()
    payload['request']['dimensions'] = dimensions
    with pytest.raises(ValueError):
        gsc(payload)


@pytest.mark.parametrize('keys', ['abc', ['https://owned.example/guide', '2026-10-03', []]])
def test_gsc_requires_string_dimension_keys(keys):
    payload = capture()
    payload['response']['rows'][0]['keys'] = keys
    with pytest.raises(ValueError):
        gsc(payload)


def test_gsc_preserves_distinct_dimension_identity_and_window_edges():
    payload = capture()
    first = payload['response']['rows'][0]
    first['keys'][1] = '2026-10-01'
    payload['response']['rows'].append(deepcopy(first))
    payload['response']['rows'][1]['keys'][1:] = ['2026-10-07', 'other guide']
    result = gsc(payload)
    assert len(result['rows']) == 2
    assert result['period'] == ['2026-10-01', '2026-10-07']
    assert [r['date'] for r in result['rows']] == ['2026-10-01', '2026-10-07']
    assert result['rows'][0]['position'] == 3.5


def test_gsc_missing_rows_remain_unavailable_not_zero_observations():
    payload = capture()
    payload['response'] = {}
    result = gsc(payload)
    assert result['rows'] == []
    assert result['coverage'] == 'unavailable'


def crux_capture():
    return {
        'request': {'url': 'https://owned.example/guide', 'formFactor': 'PHONE'},
        'record': {
            'key': {'url': 'https://owned.example/guide', 'formFactor': 'PHONE'},
            'collectionPeriod': {'firstDate': {'year': 2026, 'month': 10, 'day': 1},
                                 'lastDate': {'year': 2026, 'month': 10, 'day': 28}},
            'metrics': {'cumulative_layout_shift': {'percentiles': {'p75': '0.08'}}},
        },
    }


@pytest.mark.parametrize('requested', [
    {'url': 'https://other.example/guide', 'formFactor': 'PHONE'},
    {'origin': 'https://owned.example', 'formFactor': 'PHONE'},
    {'url': 'https://owned.example/guide', 'formFactor': 'DESKTOP'},
])
def test_crux_rejects_request_response_identity_mismatch(requested):
    from seo_qa.measurement import crux
    payload = crux_capture()
    payload['request'] = requested
    with pytest.raises(ValueError, match='request'):
        crux(payload)


def test_crux_accepts_explicit_matching_url_normalization_and_decimal_string():
    from seo_qa.measurement import crux
    payload = crux_capture()
    payload['request']['url'] = 'https://owned.example/guide?tracking=demo'
    payload['urlNormalizationDetails'] = {
        'originalUrl': payload['request']['url'],
        'normalizedUrl': payload['record']['key']['url'],
    }
    result = crux(payload)
    assert result['target'] == 'https://owned.example/guide'
    assert result['p75']['cumulative_layout_shift'] == 0.08


def test_crux_normalization_must_bind_both_original_and_returned_target():
    from seo_qa.measurement import crux
    payload = crux_capture()
    payload['request']['url'] = 'https://other.example/guide'
    payload['urlNormalizationDetails'] = {
        'originalUrl': 'https://unrelated.example/guide',
        'normalizedUrl': payload['record']['key']['url'],
    }
    with pytest.raises(ValueError, match='request'):
        crux(payload)


@pytest.mark.parametrize('value', [[], {}, 10**1000], ids=['list', 'object', 'overflowing_integer'])
def test_crux_malformed_percentile_uses_validation_error(value):
    from seo_qa.measurement import crux
    payload = crux_capture()
    payload['record']['metrics']['cumulative_layout_shift']['percentiles']['p75'] = value
    with pytest.raises(ValueError):
        crux(payload)


def test_crux_saved_response_without_request_remains_supported():
    from seo_qa.measurement import crux
    payload = crux_capture()
    del payload['request']
    assert crux(payload)['p75']['cumulative_layout_shift'] == 0.08
