"""G02 behavior, provenance, contract compatibility and failure-path checks."""
import copy
import html
import json
import re
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from seo_qa.inspector import inspect_html
from seo_qa.models import RequirementInput
from seo_qa.scope_criteria import SCOPES, bundle, configuration, observe, review, validate_config
from seo_qa.simple_qa import SAMPLE
from seo_qa.store import Store
from seo_qa.web import create_app

ALL = [s[0] for s in SCOPES]


def client_for(tmp_path, capture=None):
    store = Store(tmp_path/'audit.sqlite')
    return TestClient(create_app(store, qa_capture=capture)), store


def token(client):
    return re.search(r'name="csrf_token" value="([^"]+)"', client.get('/qa').text)[1]


def submit(client, **changes):
    values = dict(title='A sample article', source_kind='supplied', content_format='html',
                  content=SAMPLE, scopes=ALL, csrf_token=token(client))
    values.update(changes)
    return client.post('/qa/audits', data=values, follow_redirects=False)


def rows_for(content=SAMPLE, scopes=ALL, **kwargs):
    return review(content, scopes=scopes, source_kind='supplied', content_format='html',
                  url='https://example.test/a', **kwargs)


def test_bundle_is_stable_and_defensive():
    a, b = bundle(), bundle()
    assert a == b and len(a['sha256']) == 64
    a['rules']['content'].clear()
    assert bundle() == b


@pytest.mark.parametrize('change', [
    {'scopes': []}, {'scopes': ['invented']}, {'scopes': ['content', 'content']},
    {'scopes': [None]}, {'schema': 'v100'}, {'source_kind': 'auto'},
    {'content_format': 'markdown'}, {'content': ''}, {'content': 'é'*100001},
    {'source_kind': []}, {'content_format': []},
])
def test_invalid_contract_rejected(change):
    value = configuration(['content'], 'supplied', 'example', 'text')
    value.update(change)
    with pytest.raises(ValueError):
        validate_config(value)


def test_bundle_tampering_and_url_content_refused():
    value = configuration(['content'], 'supplied', 'example', 'text')
    value['criteria']['rules']['content'].clear()
    with pytest.raises(ValueError, match='criteria'):
        validate_config(value)
    with pytest.raises(ValueError, match='own content'):
        configuration(['technical'], 'url', 'not fetched', 'html')


def test_legacy_contract_preserved_and_scoped_extension_is_opt_in():
    RequirementInput(title='Legacy', urls=['https://example.test'], expected={'indexable': True})
    config = configuration(['content'], 'supplied', SAMPLE)
    assert RequirementInput(title='Scoped', urls=['https://supplied.invalid/test'], expected={'seo_qa': config})
    with pytest.raises(ValueError, match='mixed'):
        RequirementInput(title='Mixed', urls=['https://example.test'], expected={'seo_qa': config, 'indexable': True})


def test_all_scopes_have_evidence_and_unknowns_not_a_score():
    rows = rows_for()
    assert len(rows) == 16
    assert {r['scope'] for r in rows} == set(ALL)
    assert all(r['evidence']['content_sha256'] and r['recommendation'] for r in rows)
    by_id = {r['id']: r for r in rows}
    assert by_id['technical.http_response']['outcome'] == 'inconclusive'
    assert by_id['links.destinations']['outcome'] == 'inconclusive'
    assert by_id['schema.syntax']['outcome'] == 'pass'
    assert by_id['readiness.claims']['outcome'] == 'inconclusive'


def test_no_arbitrary_length_faq_or_h1_failure():
    content = '<title>' + 'long '*100 + '</title><h1>A</h1><h1>B</h1><p>Short.</p>'
    rows = rows_for(content)
    assert all(r['outcome'] != 'fail' for r in rows)
    assert next(r for r in rows if r['id']=='schema.syntax')['outcome'] == 'not_applicable'


@pytest.mark.parametrize('raw', ['{bad}', 'null', 'NaN', '"Article"'])
def test_invalid_jsonld_is_a_measured_failure(raw):
    content = '<script type="application/ld+json">'+raw+'</script>'
    assert rows_for(content, ['schema'])[0]['outcome'] == 'fail'


def test_hidden_content_does_not_become_readable_text_or_schema():
    rows = rows_for('<template><script type="application/ld+json">{bad}</script><h1>Hidden</h1></template><style>body{}</style><script>alert(1)</script>', ['content','schema'])
    by_id = {r['id']: r for r in rows}
    assert by_id['content.body']['outcome'] == 'fail'
    assert by_id['schema.syntax']['outcome'] == 'not_applicable'


def test_plain_text_does_not_execute_or_parse_embedded_html():
    config = configuration(['content','schema'], 'supplied', '<script>alert(1)</script>', 'text')
    result = observe('https://supplied.invalid/plain', {'seo_qa': config})
    assert result['evidence']['provenance']['kind'] == 'supplied'
    assert result['status'] is None
    assert next(r for r in result['evidence']['scope_review'] if r['id']=='schema.syntax')['outcome']=='not_applicable'
    assert next(r for r in result['evidence']['scope_review'] if r['id']=='content.body')['outcome']=='pass'


def test_unsafe_blank_and_unverified_links_are_distinct():
    rows = rows_for('<a href="javascript:alert(1)">Click</a><a href="https://"> </a>', ['links'])
    assert [r['outcome'] for r in rows] == ['fail', 'inconclusive', 'inconclusive']
    assert rows_for('<a href="/article#section">Read more</a>', ['links'])[0]['outcome']=='pass'


def test_error_page_and_blocked_capture_never_supply_passing_content():
    config = configuration(ALL, 'url')
    result = observe('https://example.test/article', {'seo_qa': config}, capture=lambda *_:
        dict(inspect_html(SAMPLE, 'https://example.test/article'), status=403))
    assert not any(r['outcome']=='pass' for r in result['evidence']['scope_review'])
    assert result['evidence']['provenance']['kind']=='fetched'


def test_denied_url_is_an_attempt_not_a_fetched_page(tmp_path, monkeypatch):
    import socket
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: pytest.fail('Private address reached DNS'))
    client,store=client_for(tmp_path)
    response=submit(client,source_kind='url',url='http://127.0.0.1/private')
    report=store.export_report(response.headers['location'].split('/')[-1])
    observation=report['observations'][0]
    assert observation['evidence']['provenance']['kind']=='url_attempt'
    assert not any(f['outcome']=='pass' for f in report['findings'])
    assert not report['acceptance_current']


@pytest.mark.parametrize('target', ['javascript:alert(1)', 'https://', 'https://user:pass@example.test'])
def test_canonical_declaration_is_not_a_pass_for_invalid_web_reference(target):
    rows=rows_for('<link rel="canonical" href="'+target+'">', ['technical'])
    assert next(r for r in rows if r['id']=='technical.canonical')['outcome']=='inconclusive'


def test_supplied_audit_persists_bundle_provenance_and_exports(tmp_path, monkeypatch):
    import socket
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: pytest.fail('Pasted input reached DNS'))
    client, store = client_for(tmp_path)
    response = submit(client)
    assert response.status_code == 303
    run_id = response.headers['location'].split('/')[-1]
    page = client.get(response.headers['location'])
    assert page.status_code == 200 and 'no website fetched' in page.text
    assert 'Unknown / review' in page.text and 'Criteria score:' in page.text
    assert 'Incomplete assessment' in page.text
    reopened = Store(store.path).export_report(run_id)
    assert reopened['state']=='completed' and not reopened['acceptance_current']
    assert reopened['requirement']['expected']['seo_qa']['criteria']==bundle()
    assert reopened['observations'][0]['evidence']['provenance']['kind']=='supplied'
    for ext in ('json','csv','md'):
        result=client.get(f'/runs/{run_id}/report.{ext}')
        assert result.status_code==200
    assert 'A sample article' in client.get('/qa').text


def test_url_capture_uses_own_retained_evidence_and_selected_scope(tmp_path):
    seen=[]
    def capture(url, expected):
        seen.append((url, expected))
        return inspect_html(SAMPLE, url, expected=expected)
    client, store=client_for(tmp_path, capture)
    response=submit(client, source_kind='url', url='https://example.test/page', scopes=['schema'], content='IGNORED INPUT')
    assert response.status_code==303 and seen==[('https://example.test/page', {})]
    report=store.export_report(response.headers['location'].split('/')[-1])
    assert {f['check'].split('.')[0] for f in report['findings']}=={'schema'}
    assert 'IGNORED INPUT' not in report['observations'][0]['evidence']['input']


@pytest.mark.parametrize('changes', [
    {'url':'file:///private', 'source_kind':'url'}, {'url':'https://u:p@example.test', 'source_kind':'url'},
    {'url':'https://example.test#fragment', 'source_kind':'url'}, {'content':''},
    {'scopes':[]}, {'scopes':['invented']}, {'content':'x'*200001}, {'source_kind':'sample'},
])
def test_invalid_intake_creates_no_requirement(tmp_path, changes):
    client, store=client_for(tmp_path)
    result=submit(client, **changes)
    assert result.status_code==400 and not store.list_requirements()
    assert 'role="alert"' in result.text


def test_csrf_sample_and_xss_are_safe(tmp_path):
    client, store=client_for(tmp_path)
    assert submit(client, csrf_token='wrong').status_code==403
    assert not store.list_runs()
    response=client.post('/qa/sample', data={'csrf_token':token(client)}, follow_redirects=False)
    assert response.status_code==303 and 'Synthetic sample' in client.get(response.headers['location']).text
    malicious='</textarea><script>window.__INJECTED__=1</script>'
    response=submit(client,title=malicious,content=malicious,scopes=['content'])
    page=client.get(response.headers['location'])
    assert '<script>window.__INJECTED__' not in page.text
    assert '&lt;script&gt;' in page.text


def test_changed_evidence_and_cross_owner_are_unavailable(tmp_path):
    client, store=client_for(tmp_path)
    response=submit(client)
    other=TestClient(create_app(Store(store.path, owner_id='other-owner')))
    assert other.get(response.headers['location']).status_code==409
    for path in store.evidence_dir.glob('*.json'):
        path.write_bytes(b'corrupt')
    assert client.get(response.headers['location']).status_code==409
    assert 'cannot be verified' in client.get(response.headers['location']).text


def test_hosted_preview_refuses_new_contract_without_writes(tmp_path):
    from seo_qa.production import ProductionSettings, create_production_app
    from test_production import environment, TOKEN
    store=Store(tmp_path/'audit.sqlite')
    settings=ProductionSettings.from_environ(environment(),now=1000)
    client=TestClient(create_production_app(store,settings,clock=lambda:1000),
                      base_url='http://owner.example',headers={'Authorization':'Bearer '+TOKEN})
    assert client.get('/qa').status_code==200
    assert 'not connected to hosted storage' in client.get('/qa').text
    response=client.post('/qa/sample',data={'csrf_token':token(client)})
    assert response.status_code==503 and not store.list_requirements()
    config=configuration(['content'],'supplied','Example','text')
    response=client.post('/requirements',data={'title':'Bypass','urls':'https://example.test',
                         'expected':json.dumps({'seo_qa':config}),'csrf_token':token(client)})
    assert response.status_code==400 and not store.list_requirements()


def test_capture_exception_stays_error_and_has_no_passing_summary(tmp_path):
    def fail(*args):
        raise RuntimeError('capture failed')
    client, store=client_for(tmp_path,fail)
    response=submit(client,source_kind='url',url='https://example.test/page')
    assert store.list_runs()[0]['state']=='error'
    page=client.get(response.headers['location'])
    assert 'did not complete' in page.text and 'No verified findings' in page.text


@pytest.mark.parametrize('tamper', ['legacy', 'findings', 'bundle', 'source'])
def test_scope_result_cannot_commit_unrelated_or_tampered_evidence(tmp_path, tamper):
    store=Store(tmp_path/'audit.sqlite')
    config=configuration(['content'],'supplied',SAMPLE)
    url='https://supplied.invalid/test'
    req=store.create_requirement('Scoped',[url],{'seo_qa':config})
    run=store.create_run(req['id'])
    def inspect(target, expected):
        if tamper=='legacy':
            return inspect_html(SAMPLE,target)
        value=observe(target,expected)
        if tamper=='findings':
            value['findings'][0]['outcome']='fail'
        elif tamper=='bundle':
            value['evidence']['criteria']['version']='invented'
        else:
            value['status']=200
        return value
    with pytest.raises(ValueError):
        store.execute_run(run['id'],inspect)
    assert store.get_run(run['id'])['state']=='error'
    assert not store.get_run(run['id'])['observations']


def test_minimized_exports_do_not_embed_the_entire_pasted_input(tmp_path):
    client,store=client_for(tmp_path)
    content='<h1>Article</h1><p>PRIVATE_BODY_SENTINEL not copied to minimized exports.</p>'
    response=submit(client,content=content,scopes=['content'])
    run_id=response.headers['location'].split('/')[-1]
    assert 'PRIVATE_BODY_SENTINEL' in client.get(f'/runs/{run_id}/report.json').text
    for ext in ('csv','md'):
        data=client.get(f'/runs/{run_id}/report.{ext}').text
        assert 'PRIVATE_BODY_SENTINEL' not in data
        assert bundle()['version'] in html.unescape(data)
