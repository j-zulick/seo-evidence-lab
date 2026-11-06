"""Exercise real explanation download routes with marked synthetic knowledge."""
import csv
import html
import io
import json

import pytest
from test_site_workspace import setup
from seo_qa.scope_criteria import configuration, observe
from seo_qa.workspace_exports import projection

MARKERS = ('SYNTHETICRAWCAPTURE619', 'SYNTHETICGRAPHTEXT619', 'SYNTHETICPRIVATESIDECAR619')

def knowledge():
    return {
        'release_id': 'rel:' + '1' * 64,
        'blobs': {'2' * 64: MARKERS[0]},
        'records': [{'text': MARKERS[1]}],
        'freshness_evidence': {'checks': [MARKERS[2]]},
        'snapshots': [{'id': 'snap:' + '3' * 64, 'text_sha256': '4' * 64}],
        'explanations': [{'citations': [{'quote': 'Synthetic retained citation', 'snapshot_id': 'snap:' + '3' * 64}]}],
    }

def test_direct_knowledge_projection():
    original = knowledge()
    result = projection({'canonical': {'knowledge': original}})
    for marker in MARKERS:
        assert marker not in json.dumps(result)
    projected = result['facts']['canonical']['knowledge']
    for key in ('release_id', 'snapshots', 'explanations'):
        assert projected[key] == original[key]
    assert all(key in original for key in ('blobs', 'records', 'freshness_evidence'))

def test_unrelated_record_summaries_and_freshness_remain():
    document = {'records': [{'summary': 'Keep measured observation'}],
                'freshness': {'status': 'unknown'}, 'rendering': {'content': 'changed'}}
    assert projection(document)['facts'] == document

@pytest.mark.parametrize('mode', ['beginner', 'practitioner', 'engineer'])
@pytest.mark.parametrize('format', ['csv', 'md'])
def test_actual_explanation_download_omits_raw_graph_and_sidecar(tmp_path, monkeypatch, mode, format):
    store, client = setup(tmp_path)
    requirement = store.create_requirement('Synthetic privacy audit', ['https://supplied.invalid/'],
        {'seo_qa': configuration(['content'], 'supplied', 'Synthetic text', 'text')})
    run = store.create_run(requirement['id'])
    store.execute_run(run['id'], observe)
    stored_export = store.export_report
    def report_with_knowledge(run_id):
        report = stored_export(run_id)
        report['knowledge'] = knowledge()
        return report
    monkeypatch.setattr(store, 'export_report', report_with_knowledge)
    before = store.export_report(run['id'])
    route = f'/qa/audits/{run["id"]}/explanations'
    canonical_response = client.get(route, params={'format': 'json', 'level': mode})
    assert canonical_response.status_code == 200
    authoritative = canonical_response.json()
    assert authoritative['canonical'] == before
    assert authoritative['uncertainty'] == before['knowledge']
    response = client.get(route, params={'format': format, 'level': mode})
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert 'attachment;' in response.headers['content-disposition']
    text = response.content.decode('utf-8-sig')
    for marker in MARKERS:
        assert marker not in html.unescape(text), f'{marker} escaped into {mode}/{format}'
    assert before['knowledge']['release_id'] in text
    assert 'Synthetic retained citation' in text
    assert '4' * 64 in text
    if format == 'csv':
        projected = json.loads(list(csv.reader(io.StringIO(text)))[1][2])
        assert projected == projection(authoritative)
    else:
        assert json.dumps(projection(authoritative), sort_keys=True, separators=(',', ':'), ensure_ascii=False) in html.unescape(text)
    omitted = projection(authoritative)['omitted_paths']
    for parent in ('/canonical/knowledge', '/uncertainty'):
        for key in ('blobs', 'records', 'freshness_evidence'):
            assert parent + '/' + key in omitted
    assert store.export_report(run['id']) == before
