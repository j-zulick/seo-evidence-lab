"""Fail-closed, single-owner ASGI adapter; no dotenv, sessions or fixture seeding.

Browser sign-in uses per-request owner identity and durable session revocation.
The explicitly selected legacy bearer mode remains available to operator clients.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hmac
import hashlib
import importlib
import os
import re
import time
from urllib.parse import urlsplit

from fastapi import FastAPI
from starlette.requests import Request
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse, RedirectResponse
from .request_context import RequestStore, active_store, active_csrf


class ProductionConfigError(ValueError):
    """Configuration errors never contain rejected values or credentials."""


def _hosts(value, field_name):
    hosts = tuple(x.strip().lower() for x in value.split(',') if x.strip())
    if not hosts or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', x)
                        or '..' in x for x in hosts):
        raise ProductionConfigError(field_name + ' requires exact hostnames')
    return tuple(dict.fromkeys(hosts))


def _deployment_hosts(values, platform):
    explicit = values.get('SEO_ALLOWED_HOSTS', '')
    system = values.get('VERCEL_URL', '') if platform == 'vercel' else ''
    if system:
        if (not isinstance(system, str) or not system.endswith('.vercel.app')
                or len(_hosts(system, 'VERCEL_URL')) != 1):
            raise ProductionConfigError('VERCEL_URL must be an exact Vercel deployment hostname')
    return _hosts(','.join(x for x in (explicit, system) if x), 'SEO_ALLOWED_HOSTS')


@dataclass(frozen=True)
class ProductionSettings:
    owner_token: str = field(repr=False)
    expires_at: int
    allowed_hosts: tuple[str, ...]
    inspection_hosts: tuple[str, ...]
    platform: str
    auth_mode: str = 'bearer'

    @classmethod
    def from_environ(cls, environ=None, *, now=None):
        values = os.environ if environ is None else environ
        mode = values.get('SEO_AUTH_MODE', 'browser')
        if mode not in ('browser', 'bearer'):
            raise ProductionConfigError('SEO_AUTH_MODE must be browser or bearer')
        token = values.get('SEO_OWNER_BEARER_TOKEN', '')
        if mode == 'bearer' and (not isinstance(token, str) or not re.fullmatch(r'[!-~]{43,256}', token)):
            raise ProductionConfigError('SEO_OWNER_BEARER_TOKEN requires a strong ASCII credential')
        expires = 0
        if mode == 'bearer':
            try:
                expires = int(values.get('SEO_OWNER_TOKEN_EXPIRES_AT', ''))
            except (ValueError, TypeError):
                raise ProductionConfigError('SEO_OWNER_TOKEN_EXPIRES_AT requires a UTC epoch') from None
        clock = time.time() if now is None else now
        if mode == 'bearer' and not clock < expires <= clock + 86400:
            raise ProductionConfigError('Owner token must expire within the next 24 hours')
        platform = values.get('SEO_DEPLOYMENT_PLATFORM', '')
        if platform not in ('vercel', 'single_host', 'local_verification'):
            raise ProductionConfigError('SEO_DEPLOYMENT_PLATFORM must be explicitly selected')
        return cls(token if mode == 'bearer' else '', expires,
                   _deployment_hosts(values, platform),
                   _hosts(values.get('SEO_INSPECTION_HOSTS', ''), 'SEO_INSPECTION_HOSTS'),
                   platform, mode)


class _DisabledConnections:
    def status(self):
        return {'enabled': False, 'project_configured': False,
                'hosted_store_active': False, 'data_mode': 'real-inspection',
                'paid_calls_enabled': False, 'session': {'authenticated': False}}


class _BoundedStore:
    """Conservative one-URL preview bound; queued jobs use an explicit worker."""
    def __init__(self, store):
        self._store = store

    def __getattr__(self, name):
        return getattr(self._store, name)

    @staticmethod
    def _check(urls):
        if len(urls) != 1:
            raise ValueError('Hosted private preview supports exactly one URL per requirement')

    def create_requirement(self, title, urls, expected):
        self._check(urls)
        if 'seo_qa' in expected and getattr(self._store, 'scoped_workflow_enabled', False) is not True:
            raise ValueError('General scope storage has not been enabled for this hosted preview')
        return self._store.create_requirement(title, urls, expected)

    def create_run(self, requirement_id):
        self._check(self._store.get_requirement(requirement_id)['urls'])
        return self._store.create_run(requirement_id)


class _OwnerBoundary:
    def __init__(self, app, *, settings, clock=time.time, browser_service=None):
        self.app, self.settings, self.clock = app, settings, clock
        self.browser_service = browser_service

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'websocket':
            return await send({'type': 'websocket.close', 'code': 1008})
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        settings = self.settings
        headers = {}
        for key, value in scope.get('headers', []):
            headers.setdefault(key.lower(), []).append(value)

        async def deny(code, message):
            response = JSONResponse({'detail': message}, status_code=code,
                                    headers={'Cache-Control': 'no-store'})
            await response(scope, receive, send)

        # Ambiguous Host / Authorization requests are rejected, not normalized.
        host_values = headers.get(b'host', [])
        if len(host_values) != 1:
            return await deny(400, 'Invalid host')
        host_header = host_values[0].decode('latin1')
        if not re.fullmatch(r'[A-Za-z0-9.-]+(?::[0-9]{1,5})?', host_header):
            return await deny(400, 'Invalid host')
        host = host_header.split(':', 1)[0].lower()
        if host not in settings.allowed_hosts:
            return await deny(400, 'Invalid host')
        if settings.platform != 'local_verification' and scope.get('scheme') != 'https':
            return await deny(400, 'HTTPS required')
        store_token = csrf_token = None
        request_store = None
        public = {'/health'}
        if self.browser_service is not None:
            public |= {'/login', '/logout'}
        if scope.get('path') not in public:
            if self.browser_service is not None:
                from .browser_auth import BrowserBackendUnavailable
                try:
                    principal = await run_in_threadpool(
                        self.browser_service.authenticate_request, Request(scope, receive))
                except BrowserBackendUnavailable:
                    return await deny(503, 'Sign-in service temporarily unavailable')
                except Exception:
                    # HTML navigation goes to the sign-in page; API calls keep
                    # an explicit 401. Never reflect a token/provider error.
                    accepts_html = any(b'text/html' in value for value in headers.get(b'accept', []))
                    if scope['method'] in ('GET', 'HEAD') and accepts_html and scope.get('path') != '/ready':
                        response = RedirectResponse('/login', status_code=303,
                                                    headers={'Cache-Control': 'no-store'})
                        return await response(scope, receive, send)
                    return await deny(401, 'Sign in required')
                try:
                    request_store = await run_in_threadpool(
                        self.browser_service.backend.create_store, principal.token)
                except Exception:
                    return await deny(503, 'Workspace temporarily unavailable')
                store_token = active_store.set(request_store)
                csrf_token = active_csrf.set(principal.csrf_token)
            else:
                values = headers.get(b'authorization', [])
                expected = ('Bearer ' + settings.owner_token).encode('ascii')
                if (len(values) != 1 or self.clock() >= settings.expires_at
                        or not hmac.compare_digest(values[0], expected)):
                    return await deny(401, 'Owner authorization required')
        # All browser mutations, including login/logout, retain origin checks.
        if scope.get('path') != '/health':
            # Header credentials are not ambient browser cookies. Existing form
            # CSRF remains enforced; additionally reject cross-origin mutations.
            if (scope['method'] not in ('GET', 'HEAD', 'OPTIONS')
                    and (self.browser_service is not None or b'origin' in headers)):
                origins = headers.get(b'origin', [])
                try:
                    origin = urlsplit(origins[0].decode('latin1')) if len(origins) == 1 else None
                except ValueError:
                    origin = None
                if (origin is None or origin.scheme != scope.get('scheme')
                        or origin.netloc.lower() != host_values[0].decode('latin1').lower()
                        or origin.path or origin.query or origin.fragment):
                    if csrf_token is not None:
                        active_csrf.reset(csrf_token)
                    if store_token is not None:
                        active_store.reset(store_token)
                    if request_store is not None:
                        await run_in_threadpool(request_store.api.close)
                    return await deny(403, 'Cross-origin request refused')

        async def secure_send(message):
            if message['type'] == 'http.response.start':
                response_headers = [(k, v) for k, v in message.get('headers', [])
                                    if k.lower() != b'cache-control']
                response_headers.extend([(b'cache-control', b'no-store'),
                                         (b'x-content-type-options', b'nosniff'),
                                         (b'x-frame-options', b'DENY'),
                                         # Preserve a same-origin form's Origin
                                         # while suppressing external referrers.
                                         (b'referrer-policy', b'same-origin')])
                message = dict(message, headers=response_headers)
            await send(message)
        try:
            await self.app(scope, receive, secure_send)
        finally:
            if csrf_token is not None:
                active_csrf.reset(csrf_token)
            if store_token is not None:
                active_store.reset(store_token)
            if request_store is not None:
                await run_in_threadpool(request_store.api.close)


def build_inspector(inspection_hosts):
    """Real bounded capture, shared by web composition and explicit workers."""
    from . import inspector

    def inspect(url, expected):
        if 'seo_qa' in expected:
            from .scope_criteria import observe
            # The deterministic scope evaluator replaces legacy findings; its
            # capture still uses the same allowlist/DNS/redirect/timeout policy.
            return observe(url, expected, capture=lambda target, _expected:
                           inspector.inspect_url(target, expected={'indexable': True},
                                                 allowed_hosts=list(inspection_hosts),
                                                 fixture_mode=False))
        result = inspector.inspect_url(url, expected=expected,
                                       allowed_hosts=list(inspection_hosts),
                                       fixture_mode=False)
        # The Store requires every configured check even on a capture failure.
        # Preserve the original capture reason and mark missing checks unresolved;
        # never invent passing observations from absent page evidence.
        if result.get('status') == 0:
            outcome = ('inconclusive' if any(f.get('outcome') == 'inconclusive'
                                            for f in result.get('findings', [])) else 'blocked')
            checks = {'canonical': 'canonical', 'indexable': 'indexing_directives',
                      'crawlable': 'robots_access', 'redirect_final': 'redirect_destination',
                      'max_redirects': 'redirect_count', 'allowed_statuses': 'http_status'}
            pending = [(checks[key], value) for key, value in expected.items() if key in checks]
            pending.extend(('required_text', text) for text in expected.get('required_text', []))
            for check, value in pending:
                if not any(f.get('check') == check and f.get('expected') == value
                           for f in result.get('findings', [])):
                    result.setdefault('findings', []).append({
                        'check': check, 'outcome': outcome, 'expected': value, 'observed': None,
                        'reason': 'No complete capture; configured check remains unresolved.'})
        return result
    return inspect


def create_production_app(store, settings, *, clock=time.time, browser_service=None):
    """Compose explicit storage, queued admission and the owner boundary.

    Tests may inject disposable storage; production factory must provide durable
    storage. Nothing here seeds records or reads the development connection.
    """
    from .web import create_app
    inspect = build_inspector(settings.inspection_hosts)
    if browser_service is not None:
        store = RequestStore()
    elif store is None:
        raise ProductionConfigError('Explicit storage is required')

    app = create_app(_BoundedStore(store), inspector_module=inspect,
                     connection_runtime=_DisabledConnections(), defer_runs=True)
    app.state.inspector_callable = inspect
    # Shared across stateless instances without a local session file. This token
    # alone grants no authority; every form request also requires the bearer.
    app.state.csrf_token = hmac.new(settings.owner_token.encode('ascii'),
                                    b'seo-owner-csrf-v1', hashlib.sha256).hexdigest()
    if browser_service is not None:
        from .browser_auth import install_browser_routes
        install_browser_routes(app, browser_service)
        app.state.browser_auth = True
    app.user_middleware = [item for item in app.user_middleware
                           if item.cls is not TrustedHostMiddleware]
    app.router.routes[:] = [route for route in app.router.routes
                           if not (getattr(route, 'path', '').startswith(('/connections', '/operations'))
                                   or getattr(route, 'path', '').endswith('/notion')
                                   or getattr(route, 'path', '') in ('/health', '/openapi.json'))]

    @app.get('/health')
    def health():
        return {'status': 'live'}

    @app.get('/ready')
    def ready():
        try:
            store.list_requirements()
            store.list_runs()
        except Exception:
            return JSONResponse({'status': 'not_ready'}, status_code=503)
        return {'status': 'ready', 'scope': 'owner-only', 'inspection': 'real-html'}

    app.add_middleware(_OwnerBoundary, settings=settings, clock=clock, browser_service=browser_service)
    app.state.production_settings = settings
    return app


def _unconfigured_app(detail="Production configuration unavailable"):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get('/health')
    def health():
        return {'status': 'live'}

    @app.api_route('/{path:path}', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'])
    def unavailable(path):
        return JSONResponse({'status': 'not_ready', 'detail': detail},
                            status_code=503, headers={'Cache-Control': 'no-store'})
    return app


def create_from_env(environ=None):
    """Fail closed without accessing a store when configuration is incomplete."""
    values = os.environ if environ is None else environ
    try:
        settings = ProductionSettings.from_environ(values)
        if settings.auth_mode == 'browser':
            from .browser_auth import BrowserAuth, BrowserSettings
            from .browser_storage import BrowserStorage
            browser_settings = BrowserSettings.from_environ(values)
            backend = BrowserStorage.from_environ(values)
            service = BrowserAuth(browser_settings, backend)
            return create_production_app(None, settings, browser_service=service)
        factory_path = values.get('SEO_PRODUCTION_STORE_FACTORY', 'seo_qa.production_storage:create_store')
        if factory_path != 'seo_qa.production_storage:create_store':
            raise ProductionConfigError('Production store factory is not allowlisted')
        module = importlib.import_module('seo_qa.production_storage')
        store = module.create_store(environ=values)
        return create_production_app(store, settings)
    except Exception:
        # Import/credential/backend failures never expose exception bodies.
        # In particular there is no fallback to SQLite or the dummy inspector.
        return _unconfigured_app('Knowledge repository unavailable'
                                 if values.get('SEO_KNOWLEDGE_ENABLED') == 'true'
                                 else 'Production configuration unavailable')
