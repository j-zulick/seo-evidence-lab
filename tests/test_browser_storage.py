"""Browser storage contracts use mock HTTP, never live Auth/Postgres/Storage."""
import base64
from datetime import datetime, timezone
import json
import time
from uuid import NAMESPACE_URL, uuid5

import httpx
import pytest

from seo_qa.browser_storage import BrowserStorage, BrowserStorageConfig
from seo_qa.production_storage import PROJECT_REF
from seo_qa.supabase_adapter import AccessDenied, SupabaseError

OWNER = '11111111-1111-4111-8111-111111111111'
OTHER = '22222222-2222-4222-8222-222222222222'
URL = f'https://{PROJECT_REF}.supabase.co'
HASH = 'a' * 64


def token(owner=OWNER, *, exp=None):
    body = {'sub': owner, 'role': 'authenticated', 'iss': URL + '/auth/v1',
            'exp': exp if exp is not None else time.time() + 1800}
    payload = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip('=')
    return 'fixture.' + payload + '.unverified-signature'


def environment():
    return {'SEO_SUPABASE_PROJECT_REF': PROJECT_REF, 'SUPABASE_URL': URL,
            'SUPABASE_PUBLISHABLE_KEY': 'sb_publishable_synthetic',
            'SEO_SUPABASE_OWNER_ID': OWNER}


def storage(handler, **kwargs):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return BrowserStorage.from_environ(environment(), client=client, **kwargs)


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def test_environment_constructor_has_no_io_or_static_session(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('No constructor I/O permitted')
    monkeypatch.setattr(httpx, 'Client', forbidden)
    value = BrowserStorage.from_environ(environment())
    assert value.config.owner_id == OWNER
    assert 'sb_publishable' not in repr(value.config)


@pytest.mark.parametrize('change', [
    {'SEO_SUPABASE_PROJECT_REF': 'abcdefghijklmnopqrst'}, {'SUPABASE_URL': URL + '/'},
    {'SUPABASE_URL': 'https://other.example'}, {'SUPABASE_PUBLISHABLE_KEY': 'sb_secret_no'},
    {'SEO_SUPABASE_OWNER_ID': ''}, {'SEO_DATA_MODE': 'production'},
])
def test_wrong_configuration_refused_before_io(change):
    with pytest.raises(ValueError):
        BrowserStorage.from_environ(environment() | change)


def test_password_signin_verifies_owner_server_side_and_retains_no_refresh():
    access_token = token()
    calls = []
    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == '/auth/v1/token':
            assert request.url.params['grant_type'] == 'password'
            assert json.loads(request.content) == {'email': 'owner@example.test', 'password': 'synthetic-password'}
            assert 'authorization' not in request.headers
            return httpx.Response(200, json={'access_token': access_token,
                'refresh_token': 'never-retained', 'user': {'id': OWNER}})
        assert request.url.path == '/auth/v1/user'
        assert request.headers['authorization'] == 'Bearer ' + access_token
        return httpx.Response(200, json={'id': OWNER})
    backend = storage(handler)
    result = backend.sign_in('owner@example.test', 'synthetic-password')
    assert result['access_token'] == access_token and result['owner_id'] == OWNER
    assert 'refresh' not in str(result)
    assert not hasattr(backend, 'access_token')
    assert calls == [('POST', '/auth/v1/token'), ('GET', '/auth/v1/user')]


@pytest.mark.parametrize('response_owner,token_owner', [(OTHER, OWNER), (OWNER, OTHER)])
def test_login_owner_mismatch_is_denied(response_owner, token_owner):
    def handler(request):
        if request.url.path == '/auth/v1/token':
            return httpx.Response(200, json={'access_token': token(token_owner), 'user': {'id': OWNER}})
        return httpx.Response(200, json={'id': response_owner})
    with pytest.raises(AccessDenied, match='Sign-in was not accepted'):
        storage(handler).sign_in('owner@example.test', 'synthetic-password')


@pytest.mark.parametrize('status', [400, 401, 429, 500])
def test_signin_error_bodies_are_not_exposed(status):
    backend = storage(lambda request: httpx.Response(status, json={'message': 'sensitive-provider-detail'}))
    with pytest.raises(AccessDenied) as error:
        backend.sign_in('owner@example.test', 'synthetic-password')
    assert 'sensitive' not in str(error.value)


def test_session_revocation_is_durable_across_storage_instances():
    rows = {}
    access_token = token()
    expires = int(time.time() + 600)
    calls = []
    def handler(request):
        calls.append(request.method)
        if request.url.path == '/auth/v1/user':
            return httpx.Response(200, json={'id': OWNER})
        assert request.url.path == '/rest/v1/seo_browser_sessions'
        if request.method == 'POST':
            body = json.loads(request.content)
            assert set(body) == {'session_hash', 'owner_id', 'expires_at'}
            assert access_token not in request.content.decode()
            if body['session_hash'] in rows:
                return httpx.Response(409, json={})
            rows[body['session_hash']] = body
            return httpx.Response(201, json={})
        assert request.url.params['owner_id'] == 'eq.' + OWNER
        assert request.url.params['session_hash'] == 'eq.' + HASH
        if request.method == 'PATCH':
            rows[HASH]['revoked_at'] = json.loads(request.content)['revoked_at']
            return httpx.Response(204)
        return httpx.Response(200, json=[rows[HASH]] if HASH in rows else [])
    first, second = storage(handler), storage(handler)
    first.session_create(access_token, HASH, expires)
    assert second.session_valid(access_token, HASH)
    second.session_delete(access_token, HASH)
    assert not first.session_valid(access_token, HASH)
    with pytest.raises(SupabaseError):
        first.session_create(access_token, HASH, expires)
    assert not second.session_valid(access_token, HASH)
    assert calls.count('PATCH') == 1
    second.session_delete(access_token, HASH)  # Already revoked is idempotent.
    assert calls.count('PATCH') == 1


@pytest.mark.parametrize('operation', ['insert', 'delete'])
def test_uncertain_mutation_reconciles_without_replay(operation):
    access_token = token()
    expires = int(time.time() + 600)
    saved = {'session_hash': HASH, 'owner_id': OWNER, 'expires_at': iso(expires)}
    rows = {} if operation == 'insert' else {HASH: saved}
    writes = []
    def handler(request):
        if request.url.path == '/auth/v1/user':
            return httpx.Response(200, json={'id': OWNER})
        if request.method in {'POST', 'PATCH'}:
            writes.append(request.method)
            if request.method == 'POST':
                rows[HASH] = json.loads(request.content)
            else:
                rows[HASH]['revoked_at'] = json.loads(request.content)['revoked_at']
            raise httpx.ReadTimeout('reply lost', request=request)
        return httpx.Response(200, json=list(rows.values()))
    backend = storage(handler)
    if operation == 'insert':
        backend.session_create(access_token, HASH, expires)
    else:
        backend.session_delete(access_token, HASH)
    assert len(writes) == 1


def test_logout_not_claimed_if_session_row_still_present():
    expires = int(time.time() + 600)
    def handler(request):
        if request.url.path == '/auth/v1/user':
            return httpx.Response(200, json={'id': OWNER})
        if request.method == 'PATCH':
            return httpx.Response(204)
        return httpx.Response(200, json=[{'session_hash': HASH, 'owner_id': OWNER, 'expires_at': iso(expires)}])
    with pytest.raises(SupabaseError, match='sign-out could not be verified'):
        storage(handler).session_delete(token(), HASH)


def test_missing_migration_fails_closed():
    def handler(request):
        if request.url.path == '/auth/v1/user':
            return httpx.Response(200, json={'id': OWNER})
        return httpx.Response(404, json={'message': 'private missing table detail'})
    with pytest.raises(SupabaseError) as error:
        storage(handler).session_valid(token(), HASH)
    assert 'private' not in str(error.value)


@pytest.mark.parametrize('row', [
    {'session_hash': HASH, 'owner_id': OTHER, 'expires_at': iso(time.time()+600)},
    {'session_hash': 'b'*64, 'owner_id': OWNER, 'expires_at': iso(time.time()+600)},
    {'session_hash': HASH, 'owner_id': OWNER, 'expires_at': 'invalid'},
])
def test_session_response_identity_integrity(row):
    def handler(request):
        return httpx.Response(200, json={'id': OWNER} if request.url.path == '/auth/v1/user' else [row])
    with pytest.raises(ValueError):
        storage(handler).session_valid(token(), HASH)


def test_expired_session_is_not_valid_and_overlong_session_refused():
    now = time.time()
    def handler(request):
        return httpx.Response(200, json={'id': OWNER} if request.url.path == '/auth/v1/user' else
            [{'session_hash': HASH, 'owner_id': OWNER, 'expires_at': iso(now-1)}])
    backend = storage(handler)
    assert not backend.session_valid(token(), HASH)
    with pytest.raises(ValueError, match='one hour'):
        backend.session_create(token(exp=now+7200), HASH, now+7200)


def test_request_store_uses_only_this_browser_owner_token_and_existing_root():
    access_token = token()
    root = str(uuid5(NAMESPACE_URL, 'seo-qa-tool:' + PROJECT_REF + ':' + OWNER))
    calls = []
    def handler(request):
        calls.append((request.method, request.url.path))
        assert request.method == 'GET'
        assert request.headers['authorization'] == 'Bearer ' + access_token
        if request.url.path == '/auth/v1/user':
            return httpx.Response(200, json={'id': OWNER})
        assert request.url.path == '/rest/v1/seo_qa_tool'
        assert request.url.params['id'] == 'eq.' + root
        return httpx.Response(200, json=[{'id': root, 'owner_id': OWNER,
                                         'name': 'SEO QA Tool', 'mode': 'dummy'}])
    backend = storage(handler)
    store = backend.create_store(access_token)
    assert store.owner == OWNER and store.project_id == root
    assert calls == [('GET', '/auth/v1/user'), ('GET', '/rest/v1/seo_qa_tool')]
