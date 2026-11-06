"""Environment-only owner login, durable browser sessions and request Stores.

All network operations require explicit calls. No credentials are read from disk,
refresh tokens retained, root rows seeded, or migrations executed here. Production
calls create their own HTTP clients; tests may inject a mock transport client.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
import os
import re
import time
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx

from .hosted_store import HostedStore
from .production_storage import PROJECT_REF
from .supabase_adapter import AccessDenied, SupabaseAdapter, SupabaseError, UncertainWrite


@dataclass(frozen=True)
class BrowserStorageConfig:
    project_ref: str
    url: str
    publishable_key: str = field(repr=False)
    owner_id: str
    scoped_workflow_enabled: bool = False

    def __post_init__(self):
        if type(self.scoped_workflow_enabled) is not bool:
            raise ValueError('Scoped workflow selection must be explicit boolean')
        if (self.project_ref != PROJECT_REF
                or self.url != f'https://{PROJECT_REF}.supabase.co'):
            raise ValueError('Only the existing dedicated SEO QA project is supported')
        if not isinstance(self.publishable_key, str) or not self.publishable_key.startswith('sb_publishable_'):
            raise ValueError('A publishable Supabase key is required')
        try:
            owner = str(UUID(self.owner_id))
        except (ValueError, TypeError, AttributeError):
            raise ValueError('Explicit application owner identity is required') from None
        if owner != self.owner_id:
            raise ValueError('Owner identity must use canonical UUID format')

    @classmethod
    def from_environ(cls, environ=None):
        values = os.environ if environ is None else environ
        if values.get('SEO_DATA_MODE', 'dummy') != 'dummy':
            raise ValueError('Only the existing hosted dummy workflow is supported')
        required = ('SEO_SUPABASE_PROJECT_REF', 'SUPABASE_URL',
                    'SUPABASE_PUBLISHABLE_KEY', 'SEO_SUPABASE_OWNER_ID')
        if any(not isinstance(values.get(key), str) or not values[key] for key in required):
            raise ValueError('Browser storage configuration is incomplete')
        from .scoped_workflow import flag
        return cls(*(values[key] for key in required), scoped_workflow_enabled=flag(values))


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
        raise ValueError('A SHA-256 session identifier is required')
    return value


def _expiry(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('A finite session expiration is required')
    return float(value)


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _epoch(value):
    if not isinstance(value, str):
        raise ValueError('Invalid session expiration response')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.timestamp()
    except (ValueError, OverflowError):
        raise ValueError('Invalid session expiration response') from None


def _token_expiry(token):
    try:
        encoded = token.split('.')[1]
        claims = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
        return _expiry(claims['exp'])
    except (ValueError, KeyError, TypeError, IndexError, UnicodeError):
        raise AccessDenied('Invalid application-user token') from None


class BrowserStorage:
    def __init__(self, config: BrowserStorageConfig, *, client=None, clock=time.time):
        self.config = config
        self._client = client
        self.clock = clock
        self.knowledge_values = None

    @classmethod
    def from_environ(cls, environ=None, *, client=None, clock=time.time):
        instance = cls(BrowserStorageConfig.from_environ(environ), client=client, clock=clock)
        values = os.environ if environ is None else environ
        instance.knowledge_values = {key: value for key, value in values.items()
                                     if key.startswith('SEO_KNOWLEDGE_')}
        return instance

    def _adapter(self, token):
        return SupabaseAdapter(url=self.config.url, project_ref=self.config.project_ref,
                               publishable_key=self.config.publishable_key,
                               user_jwt=token, owner_id=self.config.owner_id,
                               enabled=True, client=self._client)

    @contextmanager
    def _api(self, token):
        api = self._adapter(token)
        try:
            api._owner()
            yield api
        finally:
            api.close()

    def sign_in(self, email, password):
        if (not isinstance(email, str) or not 3 <= len(email) <= 320
                or '@' not in email or any(ord(c) < 32 for c in email)
                or not isinstance(password, str) or not 1 <= len(password) <= 4096):
            raise AccessDenied('Sign-in was not accepted')
        client = self._client or httpx.Client(timeout=15, follow_redirects=False)
        try:
            try:
                response = client.post(self.config.url + '/auth/v1/token',
                                       params={'grant_type': 'password'},
                                       headers={'apikey': self.config.publishable_key},
                                       json={'email': email, 'password': password},
                                       timeout=15, follow_redirects=False)
                if response.status_code != 200:
                    raise AccessDenied('Sign-in was not accepted')
                value = response.json()
                token = value.get('access_token') if isinstance(value, dict) else None
                if (not isinstance(token, str) or not token
                        or not isinstance(value.get('user'), dict)
                        or value['user'].get('id') != self.config.owner_id):
                    raise AccessDenied('Sign-in was not accepted')
                # JWT parsing alone is never authentication: the real Auth user
                # endpoint must confirm this exact owner before issuing a cookie.
                with self._api(token):
                    expires = _token_expiry(token)
                return {'access_token': token, 'owner_id': self.config.owner_id,
                        'expires_at': expires}
            except (httpx.RequestError, ValueError, TypeError, KeyError):
                raise AccessDenied('Sign-in was not accepted') from None
        finally:
            if self._client is None:
                client.close()

    def _read(self, api, session_hash):
        response = api._request('GET', '/rest/v1/seo_browser_sessions', params={
            'session_hash': 'eq.' + session_hash,
            'owner_id': 'eq.' + self.config.owner_id,
            'select': 'session_hash,owner_id,expires_at,revoked_at', 'limit': '2'})
        rows = api._json(response)
        if not isinstance(rows, list) or len(rows) > 1:
            raise SupabaseError('Invalid browser session response')
        if not rows:
            return None
        row = rows[0]
        if (not isinstance(row, dict) or row.get('session_hash') != session_hash
                or row.get('owner_id') != self.config.owner_id):
            raise SupabaseError('Browser session identity mismatch')
        expires = _epoch(row.get('expires_at'))
        revoked = _epoch(row['revoked_at']) if row.get('revoked_at') is not None else None
        return {'session_hash': session_hash, 'owner_id': self.config.owner_id,
                'expires_at': expires, 'revoked_at': revoked}

    def session_create(self, access_token, session_id_hash, expires_at):
        session_id_hash = _hash(session_id_hash)
        expires_at = _expiry(expires_at)
        now = self.clock()
        if not now < expires_at <= min(now + 3600, _token_expiry(access_token)):
            raise ValueError('Browser session must expire within the token lifetime and one hour')
        body = {'session_hash': session_id_hash, 'owner_id': self.config.owner_id,
                'expires_at': _iso(expires_at)}
        with self._api(access_token) as api:
            try:
                api._request('POST', '/rest/v1/seo_browser_sessions',
                             operation={'kind': 'browser_session_create',
                                        'session_hash': session_id_hash}, json=body)
            except UncertainWrite:
                # A lost reply never causes another insert. Verify stable hash.
                pass
            row = self._read(api, session_id_hash)
            if (row is None or row['revoked_at'] is not None
                    or abs(row['expires_at'] - expires_at) > .000001):
                raise SupabaseError('Browser session creation could not be verified')

    def session_valid(self, access_token, session_id_hash):
        session_id_hash = _hash(session_id_hash)
        with self._api(access_token) as api:
            row = self._read(api, session_id_hash)
            return bool(row is not None and row['revoked_at'] is None and self.clock() < row['expires_at']
                        <= _token_expiry(access_token))

    def session_delete(self, access_token, session_id_hash):
        session_id_hash = _hash(session_id_hash)
        with self._api(access_token) as api:
            row = self._read(api, session_id_hash)
            if row is None or row['revoked_at'] is not None:
                return
            try:
                api._request('PATCH', '/rest/v1/seo_browser_sessions',
                             operation={'kind': 'browser_session_revoke',
                                        'session_hash': session_id_hash},
                             json={'revoked_at': _iso(self.clock())},
                             params={'session_hash': 'eq.' + session_id_hash,
                                     'owner_id': 'eq.' + self.config.owner_id})
            except SupabaseError:
                # Concurrent sign-out can hit the immutable-tombstone guard.
                # A readback, never an automatic second PATCH, decides success.
                pass
            row = self._read(api, session_id_hash)
            if row is not None and row['revoked_at'] is None:
                raise SupabaseError('Browser session sign-out could not be verified')

    def create_store(self, access_token):
        api = self._adapter(access_token)
        root = str(uuid5(NAMESPACE_URL, 'seo-qa-tool:' + self.config.project_ref + ':' + self.config.owner_id))
        try:
            from .knowledge.config import compose_hosted
            return compose_hosted(HostedStore(api, root), scoped_enabled=self.config.scoped_workflow_enabled,
                                  environ=self.knowledge_values or {})
        except BaseException:
            api.close()
            raise
