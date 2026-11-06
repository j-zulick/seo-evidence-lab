import re
from fastapi.testclient import TestClient
from seo_qa.web import create_app
from seo_qa.store import Store


def client_for(tmp_path):
    store=Store(tmp_path/'db.sqlite')
    return TestClient(create_app(store)),store


def token(client,path='/'):
    # Token appears in forms, so use the actual requirement-entry page.
    response=client.get('/requirements/new')
    return re.search(r'name="csrf_token" value="([^"]+)"',response.text).group(1)


def create(client,title='Fixture canonical'):
    response=client.post('/requirements',data={'csrf_token':token(client),'title':title,'urls':'https://fixture.example/category','expected':'{"canonical":"https://fixture.example/category","indexable":true}'},follow_redirects=False)
    assert response.status_code==303
    return response.headers['location'].rsplit('/',1)[1]


def run(client,rid):
    response=client.post(f'/requirements/{rid}/runs',data={'csrf_token':token(client)},follow_redirects=False)
    assert response.status_code==303
    return response.headers['location'].rsplit('/',1)[1]


def test_full_operator_review_and_export(tmp_path):
    client,store=client_for(tmp_path)
    rid=create(client);run_id=run(client,rid)
    page=client.get('/runs/'+run_id)
    assert page.status_code==200 and '1 of 1 expected URLs' in page.text
    assert 'No current acceptance' in page.text
    response=client.post(f'/runs/{run_id}/review',data={'csrf_token':token(client),'decision':'accepted','expected_version':1,'reason':''})
    assert response.status_code==200 and 'Current acceptance recorded' in response.text
    report=client.get(f'/runs/{run_id}/report.json')
    assert report.status_code==200 and report.json()['acceptance_current']
    assert 'dummy' in report.json()['data_mode']
    assert client.get(f'/runs/{run_id}/report').status_code==200


def test_csrf_and_host_restrictions(tmp_path):
    client,store=client_for(tmp_path)
    assert client.get('/',headers={'host':'attacker.example'}).status_code==400
    response=client.post('/requirements',data={'csrf_token':'wrong','title':'bad','urls':'https://example.test','expected':'{}'})
    assert response.status_code==403 and not store.list_requirements()


def test_escaping_persisted_title(tmp_path):
    client,store=client_for(tmp_path)
    rid=create(client,'<script>alert("x")</script>')
    page=client.get('/requirements/'+rid)
    assert '<script>alert' not in page.text
    assert '&lt;script&gt;' in page.text


def test_stale_review_is_rejected_and_visible(tmp_path):
    client,store=client_for(tmp_path)
    rid=create(client);run_id=run(client,rid)
    store.revise_requirement(rid,{'canonical':'https://fixture.example/new'})
    page=client.get('/runs/'+run_id)
    assert 'Stale:' in page.text and 'disabled' in page.text
    response=client.post(f'/runs/{run_id}/review',data={'csrf_token':token(client),'decision':'accepted','expected_version':1})
    assert response.status_code==400
    assert not store.export_report(run_id)['acceptance_current']


def test_missing_evidence_report_fails_closed(tmp_path):
    client,store=client_for(tmp_path)
    rid=create(client);run_id=run(client,rid)
    for p in store.evidence_dir.glob('*.json'):p.unlink()
    response=client.get(f'/runs/{run_id}/report')
    assert response.status_code==400 and 'Missing or changed evidence' in response.text


def test_injected_callable_is_used_and_error_is_not_pass(tmp_path):
    store=Store(tmp_path/'db.sqlite')
    def broken(url,expected):raise ValueError('fixture capture unavailable')
    client=TestClient(create_app(store,inspector_module=broken))
    rid=create(client);run_id=run(client,rid)
    report=client.get(f'/runs/{run_id}/report.json').json()
    assert report['state']=='error' and not report['acceptance_current']
    assert 'fixture capture unavailable' in report['error']


def test_invalid_json_does_not_create_requirement(tmp_path):
    client,store=client_for(tmp_path)
    response=client.post('/requirements',data={'csrf_token':token(client),'title':'bad','urls':'https://example.test','expected':'not json'})
    assert response.status_code==400 and not store.list_requirements()
