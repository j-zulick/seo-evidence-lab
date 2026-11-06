"""Local operator connection; management MCP credentials never enter the app."""
from __future__ import annotations

import json
import time
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from .connection_config import load_config
from .supabase_adapter import SupabaseAdapter, UncertainWrite, SupabaseError
from .supabase_auth import SupabaseAuth, AuthError


class SessionAdapter(SupabaseAdapter):
    """Refresh the user's session before expiry, preserving the owner boundary."""
    def __init__(self, auth, config):
        self._auth = auth
        status = auth.status()
        self._session_version = (status.get('owner_verified_at'), status.get('expires_at'))
        super().__init__(url=config.url, project_ref=config.project_ref,
                         publishable_key=config.publishable_key, user_jwt=config.user_jwt,
                         owner_id=config.owner_id, enabled=True)

    def _request(self, method, path, **kwargs):
        status = self._auth.status()
        if status.get('owner_id') != self.owner_id:
            raise AuthError('The active account changed; reconnect the workspace')
        version = (status.get('owner_verified_at'), status.get('expires_at'))
        if (not status.get('authenticated') or version != self._session_version
                or status.get('expires_at', 0) <= time.time() + 30):
            config = self._auth.connection_config()
            if config.owner_id != self.owner_id or config.project_ref != self.project_ref:
                raise AuthError('The active account changed; reconnect the workspace')
            self._headers['Authorization'] = 'Bearer ' + config.user_jwt
            status = self._auth.status()
            self._session_version = (status.get('owner_verified_at'), status.get('expires_at'))
        return super()._request(method, path, **kwargs)


class ConnectionRuntime:
    def __init__(self, local_store, *, config=None, auth=None, adapter_factory=None,
                 hosted_factory=None, receipt_path=None):
        self.local_store = local_store
        self.backend = local_store
        self.config = config if config is not None else load_config()
        self.auth = auth if auth is not None else SupabaseAuth(self.config, enabled=self.config.enabled)
        self._adapter_factory = adapter_factory or SessionAdapter
        self._hosted_factory = hosted_factory
        self._api = None
        self._receipt = Path(receipt_path or Path(__file__).resolve().parents[1] / 'state/hosted-connection.json')

    def status(self):
        try:
            session = self.auth.status()
        except AuthError:
            session = {'authenticated': False, 'owner_id': None, 'state': 'session_needs_attention'}
        return {'project_configured': bool(self.config.url and self.config.project_ref and self.config.publishable_key),
                'enabled': self.config.enabled, 'project_ref': self.config.project_ref,
                'project_url': self.config.url, 'session': session,
                'hosted_store_active': self.backend is not self.local_store,
                'data_mode': 'dummy', 'paid_calls_enabled': False}

    def connect(self):
        if not getattr(self.local_store, 'supports_hosted_switch', True):
            raise ValueError('Knowledge audit pins require the local store; hosted knowledge admission is unavailable')
        # A previously connected account must not remain selected while the
        # current saved session is being verified or has changed identity.
        self.backend = self.local_store
        if self._api is not None:
            self._api.close()
            self._api = None
        config = self.auth.connection_config()
        api = self._adapter_factory(self.auth, config)
        root_id = str(uuid5(NAMESPACE_URL, 'seo-qa-tool:' + config.project_ref + ':' + config.owner_id))
        try:
            try:
                root = api.create_root(root_id)
            except UncertainWrite as error:
                root = api.reconcile_root(error.operation)
                if root.get('state') != 'confirmed':
                    raise SupabaseError('Workspace creation is uncertain; reconnect to reconcile') from None
            if self._hosted_factory is None:
                from .hosted_store import HostedStore
                factory = HostedStore
            else:
                factory = self._hosted_factory
            hosted = factory(api, root_id)
            # Exercise actual application reads before switching persistence.
            hosted.list_requirements()
            hosted.list_runs()
            from .fixtures import seed
            seed(hosted)
            receipt = {'project_ref': config.project_ref, 'root_id': root_id,
                       'owner_id': config.owner_id, 'verified_at': time.time(),
                       'backend': 'supabase', 'data_mode': 'dummy',
                       'verification': 'owner auth, root read/create, workflow reads and dummy requirement seed'}
            self._receipt.parent.mkdir(parents=True, exist_ok=True)
            self._receipt.write_text(json.dumps(receipt, indent=2) + '\n')
            previous = self._api
            self._api = api
            self.backend = hosted
            if previous is not None:
                previous.close()
            return {'state': 'connected', 'hosted_store_active': True, 'data_mode': 'dummy'}
        except BaseException:
            api.close()
            raise

    def sign_up(self, email, password):
        result = self.auth.sign_up(email, password)
        if result.get('authenticated'):
            return self.connect()
        return result

    def sign_in(self, email, password):
        self.auth.sign_in(email, password)
        return self.connect()

    def sign_out(self):
        result = self.auth.sign_out()
        self.backend = self.local_store
        if self._api is not None:
            self._api.close()
            self._api = None
        return result
