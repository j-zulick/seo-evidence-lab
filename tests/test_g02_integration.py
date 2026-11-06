"""G02 scope/input matrix through HTTP intake, sessions, queue and persisted results."""
import pytest
from test_simple_qa import client_for, submit, token
from test_browser_production import setup, client, login
from seo_qa.inspector import inspect_html
from seo_qa.scope_criteria import SCOPES, RULES, configuration, observe, validate_observation
from seo_qa.simple_qa import SAMPLE
from seo_qa.worker import work_once

@pytest.mark.parametrize('scope', [s[0] for s in SCOPES])
@pytest.mark.parametrize('kind,fmt', [('url','html'),('supplied','html'),('supplied','text')])
@pytest.mark.parametrize('queued', [False, True])
def test_every_scope_input_and_execution_path(tmp_path, monkeypatch, scope, kind, fmt, queued):
    captures=[]
    def capture(url, **kwargs):
        captures.append(url)
        return inspect_html(SAMPLE, url)
    monkeypatch.setattr('seo_qa.inspector.inspect_url', capture)
    if queued:
        app, backend, _, _ = setup(tmp_path)
        backend.store.scoped_workflow_enabled = True
        c = client(app); login(c)
        store = backend.store
    else:
        c, store = client_for(tmp_path, capture=lambda url, expected: capture(url))
    response = submit(c, source_kind=kind, content_format=fmt,
                      content=SAMPLE if fmt=='html' else 'Plain text with no HTML metadata.',
                      url='https://authorized.example/article', scopes=[scope])
    assert response.status_code == 303
    location=response.headers['location']; rid=location.rsplit('/',1)[-1]
    if queued:
        assert store.get_run(rid)['state']=='queued' and not captures
        assert 'Your audit is queued' in c.get(location).text
        result=work_once(store,app.state.inspector_callable)
        assert result['results']==[{'id':rid,'state':'completed'}]
    report=store.export_report(rid); observation=report['observations'][0]
    assert report['state']=='completed'
    assert len(report['findings'])==len(RULES[scope])
    assert all(f['check'].startswith(scope+'.') for f in report['findings'])
    assert observation['evidence']['provenance']['kind']==('fetched' if kind=='url' else 'supplied')
    assert len(captures)==(1 if kind=='url' else 0)
    page=c.get(location).text
    assert 'Capture and review limitations' in page
    assert 'Criteria score:' in page and 'traffic' in page
    assert c.get('/qa').status_code==200
    assert c.get('/runs/'+rid+'/report.json').status_code==200
    assert observation['evidence']['criteria']==report['requirement']['expected']['seo_qa']['criteria']

@pytest.mark.parametrize('capture', [
    {'status':200,'evidence':{'html':''}},
    {'status':200,'evidence':{'html':SAMPLE,'error':'Incomplete capture'}},
    {'status':200,'evidence':{'html':SAMPLE},'findings':[{'check':'capture','outcome':'blocked','reason':'Challenge encountered'}]},
    {'status':403,'evidence':{'html':SAMPLE}},
    {'status':0,'evidence':{'error':'Authorized host refused'}},
])
def test_failed_capture_never_passes_any_scope_and_survives_validation(tmp_path, capture):
    config=configuration([s[0] for s in SCOPES],'url')
    observation=observe('https://authorized.example/a',{'seo_qa':config},capture=lambda *args:capture)
    assert all(f['outcome']!='pass' for f in observation['findings'])
    validate_observation(config,'https://authorized.example/a',observation)
    c,store=client_for(tmp_path, capture=lambda *args:capture)
    response=submit(c,source_kind='url',url='https://authorized.example/a')
    page=c.get(response.headers['location']).text
    assert 'Observed passes</span>' in page
    if capture['status']==0:
        assert 'Authorized host refused' in page and 'Blocked' in page
    if capture.get('findings'):
        assert 'Challenge encountered' in page and 'Blocked' in page

def test_hosted_intake_describes_actual_storage(tmp_path):
    app,backend,_,_=setup(tmp_path)
    backend.store.scoped_workflow_enabled=True
    c=client(app);login(c)
    assert 'private hosted workspace' in c.get('/qa').text


def test_prior_criteria_and_valid_history_remain_readable(tmp_path):
    from seo_qa.scope_criteria import bundle
    from seo_qa.store import Store
    cfg=configuration(['content'],'supplied',SAMPLE)
    cfg['criteria']=bundle()
    store=Store(tmp_path/'history.sqlite')
    req=store.create_requirement('Previous criteria',[ 'https://supplied.invalid/old'],{'seo_qa':cfg})
    run=store.create_run(req['id']); store.execute_run(run['id'],observe)
    report=Store(store.path).export_report(run['id'])
    assert report['requirement']['expected']['seo_qa']['criteria']['version']=='seo-qa-general/1.0.0'
    assert report['observations'][0]['evidence']['criteria']==cfg['criteria']
