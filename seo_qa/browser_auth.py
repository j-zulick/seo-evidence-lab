"""Owner-only browser sign-in with durable, revocable cross-instance sessions.

Signed cookies carry a short-lived access token, never a refresh token. They are
transported only in Secure/HttpOnly/SameSite cookies. The backend verifies the
owner and authoritative session row on every authenticated request. No private
session, credential file or in-memory authentication registry is used.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import hmac
import html
import json
import math
import os
import re
import secrets
import time
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

SESSION_COOKIE = '__Host-seo_session'
LOGIN_COOKIE = '__Host-seo_login'
COOKIE_LIMIT = 3800


class BrowserAuthError(ValueError):
    """Auth failures intentionally omit credentials and provider responses."""


class BrowserBackendUnavailable(BrowserAuthError):
    """An unconfirmed session state cannot be presented as successful logout."""


@dataclass(frozen=True)
class BrowserSettings:
    signing_key: str = field(repr=False)
    owner_id: str
    session_ttl: int = 3600

    @classmethod
    def from_environ(cls, environ=None):
        values = os.environ if environ is None else environ
        key = values.get('SEO_BROWSER_SIGNING_KEY', '')
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43,128}', key):
            raise BrowserAuthError('A private browser signing key is required')
        try:
            owner = str(UUID(values.get('SEO_SUPABASE_OWNER_ID', '')))
            ttl = int(values.get('SEO_BROWSER_SESSION_TTL', '3600'))
        except (ValueError, TypeError, AttributeError):
            raise BrowserAuthError('Browser owner/session configuration is invalid') from None
        if not 60 <= ttl <= 3600:
            raise BrowserAuthError('Browser session lifetime must be 60–3600 seconds')
        return cls(key, owner, ttl)


@dataclass(frozen=True)
class BrowserPrincipal:
    token: str = field(repr=False)
    owner_id: str
    session_hash: str = field(repr=False)
    csrf_token: str = field(repr=False)
    expires_at: int


def _encode(value):
    return base64.urlsafe_b64encode(value).decode('ascii').rstrip('=')


def _decode(value):
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


class BrowserAuth:
    def __init__(self, settings, backend, *, clock=time.time):
        self.settings, self.backend, self.clock = settings, backend, clock

    def _sign(self, purpose, body):
        return _encode(hmac.new(self.settings.signing_key.encode('ascii'),
                                purpose.encode('ascii') + b':' + body.encode('ascii'),
                                hashlib.sha256).digest())

    def _seal(self, purpose, payload):
        body = _encode(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8'))
        value = body + '.' + self._sign(purpose, body)
        if len(value) > COOKIE_LIMIT:
            raise BrowserAuthError('Session cannot be represented safely')
        return value

    def _open(self, purpose, value):
        try:
            if not isinstance(value, str) or not 1 <= len(value) <= COOKIE_LIMIT:
                raise ValueError()
            body, signature = value.split('.')
            if not re.fullmatch(r'[A-Za-z0-9_-]+', body) or not hmac.compare_digest(signature, self._sign(purpose, body)):
                raise ValueError()
            payload = json.loads(_decode(body))
            if not isinstance(payload, dict):
                raise ValueError()
            return payload
        except (ValueError, TypeError, UnicodeError):
            raise BrowserAuthError('Sign-in required') from None

    def login_challenge(self):
        return self._seal('login', {'nonce': secrets.token_urlsafe(32),
                                    'expires': int(self.clock()) + 600})

    def validate_login_challenge(self, cookie, submitted):
        if (not isinstance(cookie, str) or not isinstance(submitted, str)
                or not hmac.compare_digest(cookie, submitted)):
            raise BrowserAuthError('Reload sign-in and try again')
        payload = self._open('login', cookie)
        if (set(payload) != {'nonce', 'expires'} or type(payload['expires']) is not int
                or not self.clock() < payload['expires'] <= self.clock() + 600
                or not isinstance(payload['nonce'], str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{43}', payload['nonce'])):
            raise BrowserAuthError('Reload sign-in and try again')

    def sign_in(self, email, password):
        if (not isinstance(email, str) or not 3 <= len(email) <= 320 or '@' not in email
                or any(ord(c) < 32 for c in email)
                or not isinstance(password, str) or not 1 <= len(password) <= 4096):
            raise BrowserAuthError('Sign-in failed; check your account and try again')
        try:
            session = self.backend.sign_in(email.strip(), password)
            token, owner = session['access_token'], session['owner_id']
            expires = session['expires_at']
            if (owner != self.settings.owner_id or not isinstance(token, str)
                    or not 1 <= len(token) <= 2400 or type(expires) not in (int, float)
                    or not math.isfinite(expires) or expires <= self.clock()):
                raise ValueError()
            expiry = int(min(expires, self.clock() + self.settings.session_ttl))
            sid = secrets.token_urlsafe(32)
            session_hash = hashlib.sha256(sid.encode('ascii')).hexdigest()
            cookie = self._seal('session', {'v': 1, 'sid': sid, 'token': token,
                                           'owner': owner, 'expires': expiry})
            self.backend.session_create(token, session_hash, expiry)
            return cookie, expiry
        except Exception:
            raise BrowserAuthError('Sign-in failed; check your account or try again later') from None

    def authenticate(self, cookie):
        payload = self._open('session', cookie)
        if (set(payload) != {'v', 'sid', 'token', 'owner', 'expires'}
                or type(payload['v']) is not int or payload['v'] != 1
                or payload['owner'] != self.settings.owner_id
                or type(payload['expires']) is not int
                or not self.clock() < payload['expires'] <= self.clock() + 3600
                or not isinstance(payload['sid'], str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{43}', payload['sid'])
                or not isinstance(payload['token'], str) or not 1 <= len(payload['token']) <= 2400):
            raise BrowserAuthError('Sign-in required')
        session_hash = hashlib.sha256(payload['sid'].encode('ascii')).hexdigest()
        try:
            active = self.backend.session_valid(payload['token'], session_hash)
        except Exception:
            raise BrowserBackendUnavailable('Session verification unavailable') from None
        if active is not True:
            raise BrowserAuthError('Sign-in required')
        csrf = self._sign('csrf', session_hash)
        return BrowserPrincipal(payload['token'], payload['owner'], session_hash,
                                csrf, payload['expires'])

    def authenticate_request(self, request):
        return self.authenticate(cookie_from_request(request, SESSION_COOKIE))

    def logout(self, principal):
        try:
            self.backend.session_delete(principal.token, principal.session_hash)
        except Exception:
            raise BrowserAuthError('Sign-out could not be confirmed; try again') from None


def _origin_allowed(request):
    values = request.headers.getlist('origin')
    try:
        value = urlsplit(values[0]) if len(values) == 1 else None
        return (value is not None and value.scheme == request.url.scheme
                and value.netloc.lower() == request.headers.get('host', '').lower()
                and not value.path and not value.query and not value.fragment)
    except ValueError:
        return False


def cookie_from_request(request, name):
    """Reject duplicate credential cookies instead of silently choosing one."""
    found = []
    for header in request.headers.getlist('cookie'):
        for part in header.split(';'):
            key, separator, value = part.strip().partition('=')
            if separator and key == name:
                found.append(value)
    if len(found) != 1:
        raise BrowserAuthError('Sign-in required')
    return found[0]


def _security(response):
    response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                             'Referrer-Policy': 'same-origin', 'X-Frame-Options': 'DENY'})
    return response


def _login_page(service, *, error='', status=200):
    challenge = service.login_challenge()
    body = ('<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Sign in · SEO QA</title><style>'
            ':root{font:16px/1.5 system-ui,sans-serif;color:#173135;background:#f1f5f3}'
            'body{margin:0;padding:24px}main{max-width:420px;margin:8vh auto;padding:32px;'
            'background:white;border:1px solid #cdded8;border-radius:12px}'
            'h1{font-size:28px;margin:0 0 12px}label{display:block;margin:18px 0;font-weight:600}'
            'input{display:block;box-sizing:border-box;width:100%;padding:12px;margin-top:6px;'
            'font:inherit;border:1px solid #9bb6ac;border-radius:6px}'
            'button{font:inherit;background:#176657;color:white;border:0;border-radius:7px;'
            'padding:12px 20px;cursor:pointer;width:100%}[role=alert]{padding:12px;background:#fff3d5}'
            '</style><body><main><h1>Sign in to SEO QA</h1>'
            '<p>Use your existing SEO QA owner account.</p>'
            + ('<p role="alert">' + html.escape(error) + '</p>' if error else '')
            + '<form method="post" action="/login">'
            '<input type="hidden" name="csrf_token" value="' + html.escape(challenge, quote=True) + '">'
            '<label>Email <input type="email" name="email" autocomplete="username" maxlength="320" required></label>'
            '<label>Password <input type="password" name="password" autocomplete="current-password" maxlength="4096" required></label>'
            '<button type="submit">Sign in</button></form></main></body></html>')
    response = _security(HTMLResponse(body, status_code=status))
    response.set_cookie(LOGIN_COOKIE, challenge, max_age=600, path='/', secure=True,
                        httponly=True, samesite='strict')
    return response


def install_browser_routes(app, service):
    """Install public /login and authenticated, CSRF-protected /logout routes.

    The outer production boundary must enforce allowed Host/HTTPS and authorize
    all other routes using service.authenticate_request + per-request storage.
    """
    @app.get('/login', include_in_schema=False)
    def login_form():
        return _login_page(service)

    @app.post('/login', include_in_schema=False)
    def login(request: Request, email: str = Form(...), password: str = Form(...),
              csrf_token: str = Form(...)):
        if not _origin_allowed(request):
            return _security(JSONResponse({'detail': 'Cross-origin request refused'}, status_code=403))
        try:
            service.validate_login_challenge(cookie_from_request(request, LOGIN_COOKIE), csrf_token)
        except BrowserAuthError:
            return _login_page(service, error='Reload sign-in and try again.', status=403)
        try:
            cookie, expires = service.sign_in(email, password)
        except BrowserAuthError:
            return _login_page(service, error='Sign-in failed. Check your account or try again later.', status=401)
        response = _security(RedirectResponse('/', status_code=303))
        response.set_cookie(SESSION_COOKIE, cookie, max_age=max(1, int(expires - service.clock())),
                            path='/', secure=True, httponly=True, samesite='strict')
        response.delete_cookie(LOGIN_COOKIE, path='/', secure=True, httponly=True, samesite='strict')
        return response

    @app.post('/logout', include_in_schema=False)
    def logout(request: Request, csrf_token: str = Form(...)):
        if not _origin_allowed(request):
            return _security(JSONResponse({'detail': 'Cross-origin request refused'}, status_code=403))
        try:
            principal = service.authenticate_request(request)
        except BrowserBackendUnavailable:
            return _security(JSONResponse({'detail': 'Sign-out could not be confirmed; try again'}, status_code=503))
        except BrowserAuthError:
            response = _security(RedirectResponse('/login', status_code=303))
            response.delete_cookie(SESSION_COOKIE, path='/', secure=True, httponly=True, samesite='strict')
            return response
        if not hmac.compare_digest(csrf_token, principal.csrf_token):
            return _security(JSONResponse({'detail': 'Invalid form token; reload the page'}, status_code=403))
        try:
            service.logout(principal)
        except BrowserAuthError:
            return _security(JSONResponse({'detail': 'Sign-out could not be confirmed; try again'}, status_code=503))
        response = _security(RedirectResponse('/login', status_code=303))
        response.delete_cookie(SESSION_COOKIE, path='/', secure=True, httponly=True, samesite='strict')
        return response
