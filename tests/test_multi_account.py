"""Second-account isolation: allowlist parsing, sign-in, sessions, storage roots
and every production route. Offline only: mock HTTP and injected backends."""
from types import SimpleNamespace
import json
import re
import time
from uuid import NAMESPACE_URL, uuid5

import httpx
import pytest
from fastapi.testclient import TestClient

from seo_qa.accounts import AccountPolicy, AccountPolicyError, token_subject
from seo_qa.browser_auth import BrowserAuth, BrowserAuthError, BrowserSettings, SESSION_COOKIE
from seo_qa.browser_storage import BrowserStorage
from seo_qa.production import ProductionSettings, create_production_app
from seo_qa.production_storage import PROJECT_REF
from seo_qa.request_context import active_csrf, active_store
from seo_qa.store import Store
from seo_qa.supabase_adapter import AccessDenied

OWNER = '11111111-1111-4111-8111-111111111111'
MEMBER = '33333333-3333-4333-8333-33333333abcd'
STRANGER = '44444444-4444-4444-8444-444444444444'
URL = f'https://{PROJECT_REF}.supabase.co'
HASH = 'c' * 64
KEY = 'synthetic-browser-key-' + 'x' * 43


# ---------------------------------------------------------------- policy

def test_policy_defaults_to_single_owner():
    policy = AccountPolicy.from_environ({'SEO_SUPABASE_OWNER_ID': OWNER})
    assert policy.account_ids == {OWNER} and not policy.multi_account
    assert AccountPolicy.from_environ({'SEO_SUPABASE_OWNER_ID': OWNER,
                                       'SEO_MEMBER_ACCOUNT_IDS': '  '}).account_ids == {OWNER}


def test_policy_allows_only_listed_members():
    policy = AccountPolicy.from_environ({'SEO_SUPABASE_OWNER_ID': OWNER,
                                         'SEO_MEMBER_ACCOUNT_IDS': MEMBER})
    assert policy.allows(OWNER) and policy.allows(MEMBER)
    assert not policy.allows(STRANGER) and not policy.allows(None) and not policy.allows(MEMBER.upper())
    with pytest.raises(AccountPolicyError):
        policy.require(STRANGER)


@pytest.mark.parametrize('members', [
    MEMBER + ',', ',' + MEMBER, MEMBER + ',' + MEMBER, OWNER, MEMBER.upper(),
    'not-a-uuid', '{' + MEMBER + '}', ','.join(str(uuid5(NAMESPACE_URL, str(i))) for i in range(26)),
])
def test_malformed_member_lists_fail_closed(members):
    with pytest.raises(AccountPolicyError) as error:
        AccountPolicy.from_environ({'SEO_SUPABASE_OWNER_ID': OWNER, 'SEO_MEMBER_ACCOUNT_IDS': members})
    assert MEMBER not in str(error.value)


def test_browser_settings_carry_members_and_refuse_bad_lists():
    base = {'SEO_BROWSER_SIGNING_KEY': KEY, 'SEO_SUPABASE_OWNER_ID': OWNER}
    assert BrowserSettings.from_environ(base).account_ids == {OWNER}
    settings = BrowserSettings.from_environ(base | {'SEO_MEMBER_ACCOUNT_IDS': MEMBER})
    assert settings.account_ids == {OWNER, MEMBER} and settings.owner_id == OWNER
    with pytest.raises(BrowserAuthError):
        BrowserSettings.from_environ(base | {'SEO_MEMBER_ACCOUNT_IDS': 'bad'})


# ---------------------------------------------------------------- storage (mock HTTP)

def jwt(sub, exp=None):
    import base64
    body = {'sub': sub, 'role': 'authenticated', 'iss': URL + '/auth/v1',
            'exp': exp if exp is not None else time.time() + 1800}
    payload = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip('=')
    return 'fixture.' + payload + '.unverified-signature'


def env(members=MEMBER):
    return {'SEO_SUPABASE_PROJECT_REF': PROJECT_REF, 'SUPABASE_URL': URL,
            'SUPABASE_PUBLISHABLE_KEY': 'sb_publishable_synthetic',
            'SEO_SUPABASE_OWNER_ID': OWNER, 'SEO_MEMBER_ACCOUNT_IDS': members}


def storage(handler, members=MEMBER):
    return BrowserStorage.from_environ(env(members), client=httpx.Client(transport=httpx.MockTransport(handler)))


def auth_handler(user_id, token_value, extra=None):
    def handler(request):
        if request.url.path == '/auth/v1/token':
            return httpx.Response(200, json={'access_token': token_value, 'user': {'id': user_id}})
        if request.url.path == '/auth/v1/user':
            return httpx.Response(200, json={'id': token_subject(request.headers['authorization'][7:])})
        return extra(request)
    return handler


def test_member_signs_in_as_itself_never_as_owner():
    token_value = jwt(MEMBER)
    result = storage(auth_handler(MEMBER, token_value)).sign_in('member@example.test', 'pw')
    assert result['owner_id'] == MEMBER and result['access_token'] == token_value


@pytest.mark.parametrize('members', [MEMBER, ''])
def test_unlisted_account_is_refused(members):
    with pytest.raises(AccessDenied):
        storage(auth_handler(STRANGER, jwt(STRANGER)), members).sign_in('x@example.test', 'pw')
    if not members:
        with pytest.raises(AccessDenied):
            storage(auth_handler(MEMBER, jwt(MEMBER)), members).sign_in('m@example.test', 'pw')


def test_member_cannot_claim_owner_identity_in_signin_response():
    # Provider body names the owner, but the verified token belongs to the member.
    with pytest.raises(AccessDenied):
        storage(auth_handler(OWNER, jwt(MEMBER))).sign_in('m@example.test', 'pw')


def test_session_rows_are_written_and_read_under_the_signed_in_account():
    rows, seen = {}, []
    def rest(request):
        seen.append(request.url.params.get('owner_id') or json.loads(request.content)['owner_id'])
        if request.method == 'POST':
            body = json.loads(request.content)
            rows[(body['owner_id'], body['session_hash'])] = body
            return httpx.Response(201, json={})
        owner = request.url.params['owner_id'][3:]
        row = rows.get((owner, request.url.params['session_hash'][3:]))
        return httpx.Response(200, json=[row] if row else [])
    member_token, owner_token = jwt(MEMBER), jwt(OWNER)
    backend = storage(auth_handler(None, None, rest))
    backend.session_create(member_token, HASH, int(time.time() + 600))
    assert backend.session_valid(member_token, HASH)
    # Same session hash presented with the owner's token finds no row.
    assert not backend.session_valid(owner_token, HASH)
    assert set(seen) == {MEMBER, 'eq.' + MEMBER, 'eq.' + OWNER}


def test_each_account_gets_its_own_storage_root():
    roots = {}
    def rest(request):
        owner = token_subject(request.headers['authorization'][7:])
        roots[owner] = request.url.params['id'][3:]
        return httpx.Response(200, json=[{'id': roots[owner], 'owner_id': owner,
                                         'name': 'SEO QA Tool', 'mode': 'dummy'}])
    backend = storage(auth_handler(None, None, rest))
    a, b = backend.create_store(jwt(OWNER)), backend.create_store(jwt(MEMBER))
    assert a.owner == OWNER and b.owner == MEMBER and a.project_id != b.project_id
    assert b.project_id == str(uuid5(NAMESPACE_URL, 'seo-qa-tool:' + PROJECT_REF + ':' + MEMBER))
    with pytest.raises(AccessDenied):
        backend.create_store(jwt(STRANGER))


# ---------------------------------------------------------------- full production app

class TwoAccountBackend:
    """Per-account SQLite stores, mirroring per-account hosted roots."""
    users = {'owner@example.test': OWNER, 'member@example.test': MEMBER,
             'stranger@example.test': STRANGER}

    def __init__(self, tmp_path, clock):
        self.path, self.clock = tmp_path / 'audit.sqlite', clock
        self.rows, self.counter, self.closed = {}, 0, []

    def token_account(self, token):
        return token.split(':')[1]

    def sign_in(self, email, password):
        self.counter += 1
        return {'owner_id': self.users[email], 'access_token': f'tok:{self.users[email]}:{self.counter}',
                'expires_at': 2000}

    def session_create(self, token, digest, expires_at):
        self.rows[digest] = (token, expires_at)

    def session_valid(self, token, digest):
        row = self.rows.get(digest)
        return bool(row and row[0] == token and row[1] > self.clock())

    def session_delete(self, token, digest):
        self.rows.pop(digest, None)

    def create_store(self, token):
        store, closed = Store(self.path, owner_id=self.token_account(token)), self.closed
        class PerRequest:
            api = SimpleNamespace(close=lambda: closed.append(token))
            def __getattr__(self, name):
                return getattr(store, name)
        return PerRequest()


def app_for(tmp_path, members=(MEMBER,)):
    now = {'t': 1000}
    backend = TwoAccountBackend(tmp_path, lambda: now['t'])
    service = BrowserAuth(BrowserSettings(KEY, OWNER, member_ids=tuple(members)), backend,
                          clock=lambda: now['t'])
    settings = ProductionSettings('', 0, ('app.example',), ('authorized.example',),
                                  'local_verification', 'browser')
    return create_production_app(None, settings, browser_service=service), backend, service


def browser(app):
    return TestClient(app, base_url='https://app.example', headers={'Origin': 'https://app.example'})


def csrf(response):
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)


def sign_in(c, email):
    result = c.post('/login', data={'email': email, 'password': 'pw', 'csrf_token': csrf(c.get('/login'))},
                    follow_redirects=False)
    return result.status_code


def create_requirement(c, title):
    created = c.post('/requirements', data={'csrf_token': csrf(c.get('/requirements/new')), 'title': title,
                                           'urls': 'https://authorized.example/' + title,
                                           'expected': '{"indexable":true}'}, follow_redirects=False)
    assert created.status_code == 303
    return created.headers['location']


def test_owner_and_member_see_only_their_own_records(tmp_path):
    app, backend, _ = app_for(tmp_path)
    owner, member = browser(app), browser(app)
    assert sign_in(owner, 'owner@example.test') == 303
    assert sign_in(member, 'member@example.test') == 303
    owner_req = create_requirement(owner, 'owner-only')
    queued = owner.post(owner_req + '/runs', data={'csrf_token': csrf(owner.get('/requirements/new'))},
                        follow_redirects=False).headers['location']
    member_req = create_requirement(member, 'member-only')
    assert 'owner-only' in owner.get('/').text and 'member-only' not in owner.get('/').text
    assert 'member-only' in member.get('/').text and 'owner-only' not in member.get('/').text
    for path in (owner_req, queued, queued + '/report', queued + '/report.json',
                 queued + '/report.md', queued + '/report.csv'):
        assert owner.get(path).status_code == 200, path
        assert member.get(path).status_code in (400, 404), path
    # Member cannot start a run on the owner's requirement, even with its own CSRF.
    attempt = member.post(owner_req + '/runs', data={'csrf_token': csrf(member.get('/requirements/new'))})
    assert attempt.status_code in (400, 404)
    assert Store(backend.path, owner_id=MEMBER).list_runs() == []
    assert member.get(member_req).status_code == 200 and owner.get(member_req).status_code in (400, 404)
    assert len(backend.closed) > 0 and active_store.get() is None and active_csrf.get() is None


def test_csrf_and_cookies_do_not_transfer_between_accounts(tmp_path):
    app, _, _ = app_for(tmp_path)
    owner, member = browser(app), browser(app)
    sign_in(owner, 'owner@example.test')
    sign_in(member, 'member@example.test')
    owner_csrf = csrf(owner.get('/requirements/new'))
    assert owner_csrf != csrf(member.get('/requirements/new'))
    stolen = member.post('/requirements', data={'csrf_token': owner_csrf, 'title': 'x',
                                               'urls': 'https://authorized.example/x',
                                               'expected': '{"indexable":true}'})
    assert stolen.status_code == 403


def test_unlisted_and_removed_accounts_are_refused(tmp_path):
    app, backend, _ = app_for(tmp_path)
    assert sign_in(browser(app), 'stranger@example.test') == 401
    member = browser(app)
    assert sign_in(member, 'member@example.test') == 303
    cookie = member.cookies.get(SESSION_COOKIE)
    # Same signing key and session rows, but the member was removed from the allowlist.
    narrowed = BrowserAuth(BrowserSettings(KEY, OWNER), backend, clock=lambda: 1000)
    request = SimpleNamespace(headers=SimpleNamespace(getlist=lambda name: [SESSION_COOKIE + '=' + cookie]
                                                      if name == 'cookie' else []))
    with pytest.raises(BrowserAuthError):
        narrowed.authenticate_request(request)


def test_cookie_account_must_match_token_account(tmp_path):
    _, backend, service = app_for(tmp_path)
    cookie, _ = service.sign_in('member@example.test', 'pw')
    payload = service._open('session', cookie)
    forged = service._seal('session', payload | {'owner': OWNER})  # needs the server key
    with pytest.raises(BrowserAuthError):
        service.authenticate(forged)
    assert service.authenticate(cookie).owner_id == MEMBER


# Every production route must be classified. A new route fails this test until
# someone decides whether it is public, account-scoped or local-only.
PUBLIC = {'/health', '/login', '/logout'}
ACCOUNT_SCOPED = {  # served from the per-request, per-account Store
    '/', '/ready', '/requirements/new', '/requirements', '/requirements/{rid}',
    '/requirements/{rid}/runs', '/runs/{run_id}', '/runs/{run_id}/review',
    '/runs/{run_id}/report', '/runs/{run_id}/report.csv', '/runs/{run_id}/report.json',
    '/runs/{run_id}/report.md', '/imports', '/imports/compare',
    '/qa', '/qa/audits', '/qa/sample', '/qa/audits/{run_id}', '/qa/audits/{run_id}/explanations',
}
LOCAL_ONLY = {  # must refuse in any production/browser composition
    '/qa/sites', '/qa/sites/{identifier}', '/qa/sites/{identifier}/report.json',
    '/qa/sites/{identifier}/report.{format}',
    '/qa/botworthiness', '/qa/botworthiness/assess', '/qa/botworthiness/sample',
    '/qa/botworthiness/from-run/{run_id}', '/qa/botworthiness/compare',
    '/qa/botworthiness/{identity}', '/qa/botworthiness/{identity}/edit',
    '/qa/botworthiness/{identity}/visual', '/qa/botworthiness/{identity}/reviews.json',
    '/qa/botworthiness/{identity}/report.{format}', '/qa/botworthiness/{identity}/review',
    '/qa/saved-diagnostics', '/qa/news-imports', '/qa/news-imports/decision',
    '/qa/news-imports/report.json', '/qa/lab', '/qa/walkthrough', '/qa/walkthrough/{kind}.json',
    '/qa/competitors', '/qa/evaluations', '/qa/evaluations/report.json', '/qa/community-news',
    '/qa/evidence-search', '/qa/competitors/comparison.json', '/qa/competitors/evidence/{audit_id}',
    '/qa/source-inbox', '/qa/community-retained', '/qa/community-retained/import',
    '/qa/community-retained/remove',
}

INFORMATIONAL_GET = {'/qa/sites'}  # renders a disabled notice, never data


def test_every_production_route_is_classified(tmp_path):
    app, _, _ = app_for(tmp_path)
    paths = {route.path for route in app.router.routes if hasattr(route, 'methods')}
    assert sorted(paths - (PUBLIC | ACCOUNT_SCOPED | LOCAL_ONLY)) == []
    assert not PUBLIC & ACCOUNT_SCOPED and not ACCOUNT_SCOPED & LOCAL_ONLY
    assert not any('mcp' in path.lower() for path in paths)


def test_local_only_routes_refuse_signed_in_accounts(tmp_path):
    app, _, _ = app_for(tmp_path)
    member = browser(app)
    sign_in(member, 'member@example.test')
    token = csrf(member.get('/requirements/new'))
    checked = 0
    for route in app.router.routes:
        if getattr(route, 'path', None) not in LOCAL_ONLY:
            continue
        concrete = re.sub(r'\{[^}]+\}', 'x', route.path).replace('report.x', 'report.csv')
        for method in route.methods:
            if method == 'GET':
                response = member.get(concrete)
                status = response.status_code
                if route.path in INFORMATIONAL_GET and status == 200:
                    # Explanatory page only: no records and a disabled form.
                    assert 'Hosted upload persistence is not enabled' in response.text
                    checked += 1
                    continue
            else:
                status = member.post(concrete, data={'csrf_token': token}).status_code
            assert status in (400, 403, 404, 405, 422, 503), (method, route.path, status)
            assert status != 200
            checked += 1
    assert checked >= len(LOCAL_ONLY)


def test_scoped_explanations_and_qa_audit_views_do_not_cross_accounts(tmp_path):
    app, _, _ = app_for(tmp_path)
    owner, member = browser(app), browser(app)
    sign_in(owner, 'owner@example.test')
    sign_in(member, 'member@example.test')
    req = create_requirement(owner, 'owner-secret-title')
    run = owner.post(req + '/runs', data={'csrf_token': csrf(owner.get('/requirements/new'))},
                     follow_redirects=False).headers['location']
    run_id = run.rsplit('/', 1)[1]
    for path in ('/qa/audits/' + run_id, '/qa/audits/' + run_id + '/explanations',
                 '/qa/audits/' + run_id + '/explanations?format=json'):
        response = member.get(path)
        assert response.status_code != 200 and 'owner-secret-title' not in response.text, path
