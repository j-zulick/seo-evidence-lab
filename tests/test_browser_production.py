"""Full browser composition against injected Auth/storage; no live credentials."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import re

import pytest
from fastapi.testclient import TestClient

from seo_qa.browser_auth import BrowserAuth, BrowserSettings, SESSION_COOKIE
from seo_qa.production import ProductionConfigError, ProductionSettings, create_production_app, create_from_env
from seo_qa.request_context import RequestStore, active_csrf, active_store
from seo_qa.store import Store

OWNER = '11111111-1111-4111-8111-111111111111'


class BrowserBackend:
    def __init__(self, path, clock):
        self.store = Store(path)
        self.clock = clock
        self.rows, self.opened, self.closed = {}, [], []
        self.counter = 0
        self.available = True

    def sign_in(self, email, password):
        if email != 'owner@example.test' or password != 'synthetic-password':
            raise ValueError('Private provider diagnostic')
        self.counter += 1
        return {'owner_id': OWNER, 'access_token': f'synthetic-access-{self.counter}', 'expires_at': 2000}

    def session_create(self, token, digest, expires_at):
        self.rows[digest] = (token, expires_at)

    def session_valid(self, token, digest):
        if not self.available:
            raise ValueError('database unavailable')
        row = self.rows.get(digest)
        return bool(row and row[0] == token and row[1] > self.clock())

    def session_delete(self, token, digest):
        self.rows.pop(digest, None)

    def create_store(self, token):
        self.opened.append(token)
        underlying, closed = self.store, self.closed
        class PerRequestStore:
            api = SimpleNamespace(close=lambda: closed.append(token))
            def __getattr__(self, name):
                return getattr(underlying, name)
        return PerRequestStore()


def setup(tmp_path):
    time = {'now': 1000}
    backend = BrowserBackend(tmp_path / 'audit.sqlite', lambda: time['now'])
    service = BrowserAuth(BrowserSettings('synthetic-browser-key-' + 'x' * 43, OWNER),
                          backend, clock=lambda: time['now'])
    settings = ProductionSettings('', 0, ('owner.example',), ('authorized.example',),
                                  'local_verification', 'browser')
    app = create_production_app(None, settings, browser_service=service)
    return app, backend, service, time


def client(app):
    return TestClient(app, base_url='https://owner.example', headers={'Origin': 'https://owner.example'})


def token(response):
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)


def login(c):
    csrf = token(c.get('/login'))
    result = c.post('/login', data={'email': 'owner@example.test', 'password': 'synthetic-password',
                                   'csrf_token': csrf}, headers={'Origin': 'https://owner.example'},
                    follow_redirects=False)
    assert result.status_code == 303


def test_regular_signin_private_form_queue_exports_logout_and_replay(tmp_path):
    app, backend, service, _ = setup(tmp_path)
    browser = client(app)
    assert browser.get('/', headers={'Accept': 'text/html'}, follow_redirects=False).headers['location'] == '/login'
    assert browser.get('/ready').status_code == 401
    login(browser)
    cookie = browser.cookies.get(SESSION_COOKIE)
    assert 'Sign out' in browser.get('/').text
    csrf = token(browser.get('/requirements/new'))
    created = browser.post('/requirements', data={'csrf_token': csrf, 'title': 'Browser audit',
                                                 'urls': 'https://authorized.example/a',
                                                 'expected': '{"indexable":true}'}, follow_redirects=False)
    assert created.status_code == 303
    queued = browser.post(created.headers['location'] + '/runs', data={'csrf_token': csrf}, follow_redirects=False)
    assert queued.status_code == 303 and backend.store.list_runs()[0]['state'] == 'queued'
    assert browser.get(queued.headers['location'] + '/report.md').status_code == 200
    second = client(app)
    login(second)
    second_cookie = second.cookies.get(SESSION_COOKIE)
    assert second_cookie != cookie
    assert browser.post('/requirements', data={'csrf_token': token(second.get('/requirements/new')),
                                              'title': 'Wrong CSRF', 'urls': 'https://authorized.example/b',
                                              'expected': '{"indexable":true}'}).status_code == 403
    result = browser.post('/logout', data={'csrf_token': csrf},
                          headers={'Origin': 'https://owner.example'}, follow_redirects=False)
    assert result.status_code == 303
    replay = client(app)
    replay.cookies.set(SESSION_COOKIE, cookie)
    assert replay.get('/ready').status_code == 401
    assert second.get('/ready').status_code == 200  # Separate owner session remains valid.
    assert len(backend.opened) == len(backend.closed)
    assert active_store.get() is None and active_csrf.get() is None


def test_session_expiry_backend_failure_and_cross_origin_fail_closed(tmp_path):
    app, backend, _, time = setup(tmp_path)
    browser = client(app)
    login(browser)
    csrf = token(browser.get('/requirements/new'))
    for origin in ('null', 'https://attacker.example'):
        assert browser.post('/logout', data={'csrf_token': csrf}, headers={'Origin': origin}).status_code == 403
        assert browser.post('/requirements', data={'csrf_token': csrf}, headers={'Origin': origin}).status_code == 403
    browser.headers.pop('Origin')
    assert browser.post('/requirements', data={'csrf_token': csrf}).status_code == 403
    backend.available = False
    assert browser.get('/ready').status_code == 503
    backend.available = True
    time['now'] = 2000
    assert browser.get('/ready').status_code == 401
    assert active_store.get() is None and active_csrf.get() is None


def test_request_context_and_csrf_remain_isolated_under_concurrent_sessions(tmp_path):
    app, backend, _, _ = setup(tmp_path)
    first, second = client(app), client(app)
    login(first)
    login(second)
    def inspect(c):
        return [token(c.get('/requirements/new')) for _ in range(5)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = list(pool.map(inspect, (first, second)))
    assert len(set(a)) == len(set(b)) == 1 and a[0] != b[0]
    assert len(backend.opened) == len(backend.closed) == 10
    with pytest.raises(ValueError, match='authenticated request'):
        RequestStore().list_runs()


def test_vercel_runtime_hostname_is_exact_and_no_wildcard():
    base = {'SEO_AUTH_MODE': 'browser', 'SEO_DEPLOYMENT_PLATFORM': 'vercel',
            'VERCEL_URL': 'seo-preview-a1.vercel.app', 'SEO_INSPECTION_HOSTS': 'approved.example'}
    settings = ProductionSettings.from_environ(base)
    assert settings.allowed_hosts == ('seo-preview-a1.vercel.app',)
    assert settings.owner_token == ''
    for hostname in ('https://seo.vercel.app', '*.vercel.app', 'seo.vercel.app,attacker.example',
                     'seo.vercel.app.evil.example', '127.0.0.1'):
        with pytest.raises(ProductionConfigError):
            ProductionSettings.from_environ(dict(base, VERCEL_URL=hostname))


def test_browser_default_initializes_without_static_jwt_or_network(monkeypatch):
    from seo_qa.browser_storage import BrowserStorage
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError('Startup must not authenticate or initialize a Store')
    monkeypatch.setattr(BrowserStorage, 'sign_in', forbidden)
    monkeypatch.setattr(BrowserStorage, 'create_store', forbidden)
    values = {'SEO_DEPLOYMENT_PLATFORM': 'local_verification',
              'SEO_ALLOWED_HOSTS': 'owner.example', 'SEO_INSPECTION_HOSTS': 'approved.example',
              'SEO_BROWSER_SIGNING_KEY': 'x' * 43, 'SEO_SUPABASE_OWNER_ID': OWNER,
              'SEO_SUPABASE_PROJECT_REF': 'mqzslbwdszcbsyppdlfz',
              'SUPABASE_URL': 'https://mqzslbwdszcbsyppdlfz.supabase.co',
              'SUPABASE_PUBLISHABLE_KEY': 'sb_publishable_fixture'}
    browser = client(create_from_env(values))
    assert browser.get('/login').status_code == 200
    assert browser.get('/ready').status_code == 401
    assert not calls
