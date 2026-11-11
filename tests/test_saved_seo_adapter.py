from copy import deepcopy
import json
import pytest
from seo_qa.saved_seo_adapter import ENGINE, admit, canonical, compare, sha


def fixture(repaired=False):
    page = '<html><head><title>Evidence review</title></head></html>' if repaired else '<html><head><meta name="description" content="Evidence"></head></html>'
    statuses = ['pass', 'fail'] if repaired else ['fail', 'pass']
    rows = [{'ruleId': rule, 'status': status, 'message': 'Authored synthetic fixture for supplied HTML',
             'score': 100 if status == 'pass' else 0, 'weight': 1}
            for rule, status in zip(['core-title-present', 'core-description-present'], statuses)]
    rows.append({'ruleId': 'core-title-unique', 'status': 'not-measured', 'message': 'Single page; no other pages for uniqueness comparison', 'score': 0, 'weight': 0})
    raw = {'schemaVersion': 2, 'url': 'https://fixture.example/article', 'overallScore': 50,
           'timestamp': '2026-11-11T00:00:00-05:00', 'crawledPages': 1,
           'run': {'crawl': False, 'maxPages': 1, 'concurrency': 1, 'measureCwv': False, 'mobile': False,
                   'simulateInteraction': False, 'staging': True, 'categories': ['core'], 'scoringVersion': 4, 'timeout': 1000},
           'coverage': {'pages': ['https://fixture.example/article'], 'detail': 'per-page'},
           'categoryResults': [{'categoryId': 'core', 'score': 50, 'passCount': 1, 'warnCount': 0, 'failCount': 1,
                                'notMeasuredCount': 1, 'results': rows}],
           'waivedCaps': [{'id': 'robots', 'ceiling': 0, 'reason': 'Synthetic staging waiver retained'}]}
    envelope = {'adapter_schema': 'saved-seo/v1', 'engine': deepcopy(ENGINE),
                'operation': {'id': 'synthetic-repaired' if repaired else 'synthetic-broken', 'status': 'completed', 'captured_at': raw['timestamp']},
                'input': {'requested_url': raw['url'], 'final_url': raw['url'], 'scope': 'single_page',
                          'rendering': 'raw', 'page_sha256': sha(page.encode()), 'page_version': sha(page.encode())},
                'provenance': {'raw_sha256': sha(canonical(raw)), 'evidence_kind': 'synthetic'}}
    return raw, envelope, page


def test_fix_and_new_regression_do_not_cancel():
    old, new = [admit(canonical(r), e, p) for r, e, p in (fixture(), fixture(True))]
    result = compare(old, new)
    assert old['measurement']['score'] == new['measurement']['score'] == 50
    assert {x['rule_id']: (x['before'], x['after']) for x in result['changes']} == {
        'core-title-present': ('fail', 'pass'), 'core-description-present': ('pass', 'fail')}
    assert result['aggregate_cancellation_allowed'] is False
    assert result['waived_caps']['after'][0]['id'] == 'robots'
    assert old['measurement']['measured_denominator'] == 2
    assert old['measurement']['not_measured'] == 1
    assert old['review'].startswith('external diagnostic candidate')
    assert all(r['source_locator']['raw_sha256'] == old['provenance']['raw_sha256'] for r in old['result'])


@pytest.mark.parametrize('fault', ['engine', 'raw_hash', 'page_hash', 'cancelled', 'timezone', 'schema', 'empty', 'duplicate', 'count', 'aggregate', 'missing_run', 'nan'])
def test_detached_or_incomplete_results_rejected(fault):
    raw, envelope, page = fixture()
    if fault == 'engine': envelope['engine']['scoring'] = 5
    if fault == 'raw_hash': envelope['provenance']['raw_sha256'] = '0' * 64
    if fault == 'page_hash': envelope['input']['page_sha256'] = '0' * 64
    if fault == 'cancelled': envelope['operation']['status'] = 'cancelled'
    if fault == 'timezone': envelope['operation']['captured_at'] = '2026-11-11T00:00:00'
    if fault == 'schema': raw['schemaVersion'] = 1
    if fault == 'empty': raw['categoryResults'] = []
    if fault == 'duplicate': raw['categoryResults'][0]['results'].append(raw['categoryResults'][0]['results'][0])
    if fault == 'count': raw['categoryResults'][0]['failCount'] = 0
    if fault == 'aggregate': raw['coverage']['detail'] = 'aggregated'
    if fault == 'missing_run': raw.pop('run')
    if fault == 'nan': raw['overallScore'] = float('nan')
    encoded = json.dumps(raw).encode()
    if fault != 'raw_hash': envelope['provenance']['raw_sha256'] = sha(encoded)
    with pytest.raises(ValueError): admit(encoded, envelope, page)


@pytest.mark.parametrize('fault', ['scoring', 'target', 'rendering', 'mode', 'synthetic'])
def test_incompatible_comparisons_refused(fault):
    old, new = [admit(canonical(r), e, p) for r, e, p in (fixture(), fixture(True))]
    if fault == 'scoring': new['engine']['scoring'] = 5
    if fault == 'target': new['input']['final_url'] = 'https://other.example/article'
    if fault == 'rendering': new['input']['rendering'] = 'rendered'
    if fault == 'mode': new['raw_result']['run']['measureCwv'] = True
    if fault == 'synthetic': new['provenance']['evidence_kind'] = 'saved_external_diagnostic'
    with pytest.raises(ValueError): compare(old, new)


def test_raw_output_and_not_measured_preserved():
    raw, envelope, page = fixture()
    result = admit(canonical(raw), envelope, page)
    assert result['raw_result'] == raw
    assert result['result'][-1]['outcome'] == 'not-measured'
    assert result['measurement']['completeness'] == 'selected checks only'
    assert envelope['provenance']['evidence_kind'] == 'synthetic'
