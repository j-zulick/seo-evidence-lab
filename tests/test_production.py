"""Production boundary tests: disposable store, no network or credentials."""
import importlib
import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from seo_qa.production import (ProductionConfigError, ProductionSettings,
                               create_from_env, create_production_app)
from seo_qa.store import Store


TOKEN = 'synthetic-owner-token-for-tests-only-' + 'a' * 32


def environment():
    return {'SEO_AUTH_MODE': 'bearer', 'SEO_OWNER_BEARER_TOKEN': TOKEN,
            'SEO_OWNER_TOKEN_EXPIRES_AT': '2000',
            'SEO_ALLOWED_HOSTS': 'owner.example',
            'SEO_INSPECTION_HOSTS': 'authorized.example',
            'SEO_DEPLOYMENT_PLATFORM': 'local_verification'}


def build(tmp_path, *, clock=None, platform='local_verification'):
    values = environment()
    values['SEO_DEPLOYMENT_PLATFORM'] = platform
    settings = ProductionSettings.from_environ(values, now=1000)
    state = {'now': 1000}
    store = Store(tmp_path / 'audit.sqlite')
    app = create_production_app(store, settings, clock=clock or (lambda: state['now']))
    client = TestClient(app, base_url='http://owner.example',
                        headers={'Authorization': 'Bearer ' + TOKEN})
    return client, store, state, app


def csrf(client):
    page = client.get('/requirements/new')
    assert page.status_code == 200
    return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)


@pytest.mark.parametrize('replacement', [
    {'SEO_OWNER_BEARER_TOKEN': ''}, {'SEO_OWNER_BEARER_TOKEN': 'too-short'},
    {'SEO_OWNER_BEARER_TOKEN': 'é' * 60}, {'SEO_OWNER_TOKEN_EXPIRES_AT': 'invalid'},
    {'SEO_OWNER_TOKEN_EXPIRES_AT': '999'}, {'SEO_OWNER_TOKEN_EXPIRES_AT': '999999'},
    {'SEO_ALLOWED_HOSTS': '*'}, {'SEO_ALLOWED_HOSTS': 'https://owner.example'},
    {'SEO_INSPECTION_HOSTS': ''}, {'SEO_INSPECTION_HOSTS': 'example.com:443'},
    {'SEO_DEPLOYMENT_PLATFORM': 'auto'},
])
def test_invalid_configuration_is_secret_safe(replacement):
    values = environment() | replacement
    with pytest.raises(ProductionConfigError) as caught:
        ProductionSettings.from_environ(values, now=1000)
    assert TOKEN not in str(caught.value)


def test_missing_configuration_does_not_import_store(monkeypatch):
    def forbidden(*args):
        raise AssertionError('Store import must not happen')
    monkeypatch.setattr(importlib, 'import_module', forbidden)
    client = TestClient(create_from_env({}))
    assert client.get('/health').json() == {'status': 'live'}
    assert client.get('/ready').status_code == 503
    assert client.post('/requirements').status_code == 503


def test_factory_is_allowlisted_and_backend_errors_are_redacted(monkeypatch):
    from seo_qa import production
    monkeypatch.setattr(production.time, 'time', lambda: 1000)
    calls = []
    def backend_import(name):
        calls.append(name)
        def fail(*, environ):
            assert environ == environment()
            raise RuntimeError('private token=' + TOKEN)
        return SimpleNamespace(create_store=fail)
    monkeypatch.setattr(importlib, 'import_module', backend_import)
    client = TestClient(create_from_env(environment() | {'SEO_PRODUCTION_STORE_FACTORY': 'os:system'}))
    assert client.get('/ready').status_code == 503
    assert not calls
    client = TestClient(create_from_env(environment()))
    response = client.get('/ready')
    assert response.status_code == 503 and TOKEN not in response.text
    assert calls == ['seo_qa.production_storage']


def test_unauthorized_route_matrix_and_cookie_query_refusal(tmp_path):
    client, store, _, app = build(tmp_path)
    anonymous = TestClient(app, base_url='http://owner.example')
    for path in ('/', '/ready', '/imports', '/requirements/new',
                 '/runs/fake/report.json', '/connections', '/openapi.json'):
        assert anonymous.get(path).status_code == 401
    assert anonymous.post('/requirements').status_code == 401
    anonymous.cookies.set('token', TOKEN)
    assert anonymous.get('/?token=' + TOKEN).status_code == 401
    assert anonymous.get('/health').json() == {'status': 'live'}
    assert not store.list_requirements()
    assert client.get('/ready').json()['inspection'] == 'real-html'


def test_expired_or_rotated_token_is_refused(tmp_path):
    client, _, state, _ = build(tmp_path)
    assert client.get('/').status_code == 200
    state['now'] = 2000
    assert client.get('/').status_code == 401
    state['now'] = 1000
    assert client.get('/', headers={'Authorization': 'Bearer changed'}).status_code == 401


def test_host_origin_csrf_and_security_headers(tmp_path):
    client, store, _, app = build(tmp_path)
    assert client.get('/', headers={'Host': 'attacker.example'}).status_code == 400
    payload = {'title': 'Preview', 'urls': 'https://authorized.example/article',
               'expected': '{}', 'csrf_token': csrf(client)}
    assert client.post('/requirements', data=payload,
                       headers={'Origin': 'https://attacker.example'}).status_code == 403
    assert client.post('/requirements', data=payload,
                       headers={'Origin': 'https://['}).status_code == 403
    assert client.post('/requirements', data=payload,
                       headers={'Origin': 'null'}).status_code == 403
    payload['csrf_token'] = 'wrong'
    assert client.post('/requirements', data=payload).status_code == 403
    assert not store.list_requirements()
    response = client.get('/')
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['x-frame-options'] == 'DENY'
    assert response.headers['referrer-policy'] == 'same-origin'
    assert 'set-cookie' not in response.headers


def test_multi_instance_csrf_works_without_local_session(tmp_path):
    first, _, _, _ = build(tmp_path / 'a')
    second, _, _, _ = build(tmp_path / 'b')
    assert csrf(first) == csrf(second)


def test_connection_and_fixture_write_controls_are_unavailable(tmp_path):
    client, _, _, _ = build(tmp_path)
    for path in ('/connections', '/connections/sign-in', '/connections/sign-up',
                 '/connections/connect', '/operations/fake/apply', '/runs/fake/notion'):
        assert client.get(path).status_code == 404
        assert client.post(path).status_code == 404


def test_https_required_for_hosted_platform(tmp_path):
    client, _, _, app = build(tmp_path, platform='vercel')
    assert client.get('/health').status_code == 400
    secure = TestClient(app, base_url='https://owner.example',
                        headers={'Authorization': 'Bearer ' + TOKEN})
    assert secure.get('/ready').status_code == 200


def test_real_inspector_selected_and_no_seed_or_fixture_network_bypass(tmp_path, monkeypatch):
    from seo_qa import inspector
    seen = []
    def fake_capture(url, **kwargs):
        seen.append((url, kwargs))
        return inspector.inspect_html('<title>Authorized fixture</title>', url,
                                      expected=kwargs['expected'])
    monkeypatch.setattr(inspector, 'inspect_url', fake_capture)
    client, store, _, app = build(tmp_path)
    assert not store.list_requirements()  # No implicit demo seeding.
    create = client.post('/requirements', data={
        'title': 'Preview', 'urls': 'https://authorized.example/article',
        'expected': '{"allowed_statuses":[200]}', 'csrf_token': csrf(client)},
        follow_redirects=False)
    assert create.status_code == 303
    rid = create.headers['location'].rsplit('/', 1)[1]
    run = client.post('/requirements/' + rid + '/runs',
                      data={'csrf_token': csrf(client)}, follow_redirects=False)
    assert run.status_code == 303
    assert not seen
    assert store.list_runs()[0]['state'] == 'queued'
    from seo_qa.worker import work_once
    work_once(store, app.state.inspector_callable)
    assert seen[0][1]['allowed_hosts'] == ['authorized.example']
    assert seen[0][1]['fixture_mode'] is False
    assert 'Synthetic local HTML' not in client.get(run.headers['location']).text


def test_unapproved_target_is_blocked_before_network_and_not_accepted(tmp_path, monkeypatch):
    import socket
    def forbidden(*args, **kwargs):
        raise AssertionError('Unapproved target must not reach DNS')
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)
    client, store, _, app = build(tmp_path)
    requirement = store.create_requirement('Blocked private target',
                                           ['http://127.0.0.1/private'],
                                           {'allowed_statuses': [200]})
    response = client.post('/requirements/' + requirement['id'] + '/runs',
                           data={'csrf_token': csrf(client)}, follow_redirects=False)
    assert response.status_code == 303
    assert store.list_runs()[0]['state'] == 'queued'
    from seo_qa.worker import work_once
    work_once(store, app.state.inspector_callable)
    report = client.get(response.headers['location'] + '/report.json').json()
    assert not report['acceptance_current']
    assert report['findings'][0]['outcome'] == 'blocked'


def test_bulk_requirements_and_existing_bulk_runs_are_refused(tmp_path):
    client, store, _, _ = build(tmp_path)
    urls = ['https://authorized.example/a', 'https://authorized.example/b']
    response = client.post('/requirements', data={
        'title': 'Bulk', 'urls': '\n'.join(urls), 'expected': '{}',
        'csrf_token': csrf(client)})
    assert response.status_code == 400
    assert not store.list_requirements()
    existing = store.create_requirement('Old bulk', urls, {'allowed_statuses': [200]})
    response = client.post('/requirements/' + existing['id'] + '/runs',
                           data={'csrf_token': csrf(client)})
    assert response.status_code == 400
    assert not store.list_runs()


def test_readiness_failure_has_no_exception_body(tmp_path, monkeypatch):
    client, store, _, _ = build(tmp_path)
    def fail():
        raise RuntimeError('private connection detail')
    monkeypatch.setattr(store, 'list_runs', fail)
    result = client.get('/ready')
    assert result.status_code == 503
    assert 'private' not in result.text
