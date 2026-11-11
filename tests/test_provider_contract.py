"""Offline saved-export regressions; every observation is synthetic."""
from copy import deepcopy
import json
from pathlib import Path
import pytest
from seo_qa.provider_contract import SCHEMA, normalize, compare
ROOT = Path(__file__).resolve().parents[1]


def envelope(provider='gsc'):
    return dict(schema=SCHEMA, provider=provider, synthetic=True,
                exported_at='2026-11-01T12:00:00+00:00',
                payload=json.loads((ROOT/'fixtures/measurement/gsc.json').read_text()))


def pair():
    left = envelope(); right = deepcopy(left)
    right['exported_at'] = '2026-11-26T00:00:00+00:00'
    right['payload']['request'].update(startDate='2026-10-29', endDate='2026-11-25')
    right['payload']['response']['rows'][0]['clicks'] = 21
    return normalize(left)['series'][0], normalize(right)['series'][0]


def test_reproduce_and_compare():
    raw = envelope(); assert normalize(raw) == normalize(deepcopy(raw))
    assert len(normalize(raw)['series']) == 3
    result = compare(*pair())
    assert result['status'] == 'comparable' and result['delta'] == 7
    assert len(result['sources']) == 2 and 'causal' in result['interpretation']


@pytest.mark.parametrize('key,value', [('schema','v99'),('synthetic',None),('exported_at','2026-11-01')])
def test_envelope_contract(key, value):
    raw = envelope(); raw[key] = value
    with pytest.raises(ValueError): normalize(raw)


@pytest.mark.parametrize('field,value', [('clicks',True),('clicks',1.5),('clicks',201),('impressions',-1)])
def test_gsc_invalid_counts(field, value):
    raw = envelope(); raw['payload']['response']['rows'][0][field] = value
    with pytest.raises(ValueError): normalize(raw)


@pytest.mark.parametrize('key,value', [('type','image'),('dataState','all'),('aggregationType','byPage'),('dimensionFilterGroups',[{'groupType':'and'}])])
def test_gsc_request_context(key, value):
    left, _ = pair(); raw = envelope(); raw['payload']['request'][key] = value
    assert compare(left, normalize(raw)['series'][0])['status'] == 'incompatible identity'


@pytest.mark.parametrize('field,value', [('schema','unknown'),('normalizer','future'),('unit','ratio'),('synthetic',False)])
def test_identity_changes(field, value):
    left, right = pair(); right[field] = value
    assert compare(left, right)['delta'] is None


@pytest.mark.parametrize('period', [['2026-10-28','2026-11-24'],['2026-10-29','2026-11-24'],['2026-11-25','2026-10-29']])
def test_bad_windows(period):
    left, right = pair(); right['period'] = period
    assert compare(left, right)['delta'] is None


def test_missing_is_not_zero():
    raw = envelope(); raw['payload']['response'] = {}
    assert normalize(raw)['coverage'] == 'unavailable' and normalize(raw)['series'] == []
    left, right = pair(); right['value'] = None
    assert compare(left, right) == dict(status='unavailable', delta=None)
    right['value'] = 0; assert compare(left, right)['delta'] == -14


def test_date_dimension_is_daily():
    raw = envelope(); raw['payload']['request']['dimensions'].append('date')
    raw['payload']['response']['rows'][0]['keys'].append('2026-10-03')
    row = normalize(raw)['series'][0]
    assert row['period'] == ['2026-10-03','2026-10-03']
    assert 'date' not in row['identity']['dimensions']


def test_crux_unavailable_and_partial():
    raw = envelope('crux'); raw['payload'] = {'error': {'status':'NOT_FOUND'}}
    assert normalize(raw)['series'] == []
    raw['payload'] = json.loads((ROOT/'fixtures/measurement/crux.json').read_text())
    rows = normalize(raw)['series']; assert rows
    assert not any(row['metric'] == 'interaction_to_next_paint' for row in rows)


def test_ahrefs_saved_boundaries():
    raw = envelope('ahrefs-saved-top10')
    raw['payload'] = dict(request=dict(mode='exact',target='https://owned.example/guide',country='us',history_grouping='daily',date_from='2026-10-01',date_to='2026-10-02'),keywords=[dict(date='2026-10-01',top3=2,top4_10=3),dict(date='2026-10-02',top3=3,top4_10=4)])
    assert compare(*normalize(raw)['series'])['delta'] == 2
    raw['payload']['keywords'].append(deepcopy(raw['payload']['keywords'][0]))
    with pytest.raises(ValueError, match='Duplicate'): normalize(raw)
    raw['payload']['keywords'].pop(); raw['payload']['request']['mode'] = 'domain'
    with pytest.raises(ValueError, match='exact'): normalize(raw)


def test_hash_required():
    left, right = pair(); right.pop('source_hash')
    assert compare(left, right)['status'] == 'invalid observation'


def test_observation_cannot_postdate_export():
    raw = envelope(); raw['exported_at'] = '2026-10-01T00:00:00+00:00'
    with pytest.raises(ValueError, match='after export'): normalize(raw)


def test_invalid_request_context_is_not_a_measurement():
    raw = envelope(); raw['payload']['request']['type'] = None
    with pytest.raises(ValueError, match='context'): normalize(raw)


def test_empty_identity_is_not_comparable():
    left, right = pair(); left['identity'] = right['identity'] = {}
    assert compare(left, right)['status'] == 'invalid observation'
