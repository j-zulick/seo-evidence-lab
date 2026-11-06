"""Bounded, explicit-release Supabase read adapter. Never imports audit data."""
import base64
from contextlib import contextmanager
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import time
from uuid import UUID
import math
from tempfile import TemporaryDirectory

import httpx

from .contracts import KnowledgeRelease, sha256
from .repository import KnowledgeRepository


class KnowledgeUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class RemoteConfig:
    project_ref: str
    release_id: str
    table: str
    publishable_key: str = field(repr=False)
    user_jwt: str = field(repr=False)
    timeout: float = 5
    max_bytes: int = 4_000_000

    def __post_init__(self):
        if (not re.fullmatch(r'[a-z0-9]{20}', self.project_ref)
                or not re.fullmatch(r'rel:[a-f0-9]{64}', self.release_id)
                or not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', self.table)
                or not self.publishable_key.startswith('sb_publishable_')
                or not self.user_jwt or any(c.isspace() for c in self.user_jwt)
                or not 0 < self.timeout <= 10
                or not 1024 <= self.max_bytes <= 8_000_000):
            raise KnowledgeUnavailable('Invalid knowledge configuration')
        try:
            claims = json.loads(base64.urlsafe_b64decode(self.user_jwt.split('.')[1] + '==='))
            UUID(claims['sub'])
            if (claims['role'] != 'authenticated'
                    or claims['iss'] != f'https://{self.project_ref}.supabase.co/auth/v1'
                    or type(claims['exp']) not in (int, float)
                    or not math.isfinite(claims['exp'])):
                raise ValueError()
        except (IndexError, KeyError, TypeError, ValueError):
            raise KnowledgeUnavailable('Unexpired application-user knowledge token required') from None



class SupabaseKnowledgeRepository:
    def __init__(self, config, *, transport=None):
        self.config = config
        self.transport = transport

    def _bundle(self, release_id):
        config = self.config
        claims = json.loads(base64.urlsafe_b64decode(config.user_jwt.split('.')[1] + '==='))
        if claims['exp'] <= time.time():
            raise KnowledgeUnavailable('Knowledge reader session expired')
        if not re.fullmatch(r'rel:[a-f0-9]{64}', release_id):
            raise KnowledgeUnavailable('Invalid knowledge release selection')
        try:
            # No redirects, retries or unbounded response buffering.
            with httpx.Client(transport=self.transport, timeout=config.timeout,
                              follow_redirects=False) as client:
                with client.stream('GET', f'https://{config.project_ref}.supabase.co/rest/v1/{config.table}',
                                   params={'select': 'release_id,bundle_sha256,bundle_base64',
                                           'release_id': 'eq.' + release_id, 'limit': '2'},
                                   headers={'apikey': config.publishable_key,
                                            'Authorization': 'Bearer ' + config.user_jwt}) as response:
                    if response.status_code != 200:
                        raise KnowledgeUnavailable('Knowledge service unavailable')
                    deadline = time.monotonic() + config.timeout
                    payload = bytearray()
                    for chunk in response.iter_bytes(65536):
                        if time.monotonic() > deadline:
                            raise KnowledgeUnavailable("Knowledge response deadline exceeded")
                        payload.extend(chunk)
                        if len(payload) > config.max_bytes:
                            raise KnowledgeUnavailable('Knowledge response exceeds limit')
            rows = json.loads(payload)
            if (not isinstance(rows, list) or len(rows) != 1
                    or set(rows[0]) != {'release_id', 'bundle_sha256', 'bundle_base64'}
                    or rows[0]['release_id'] != release_id):
                raise KnowledgeUnavailable('Knowledge release unavailable')
            data = base64.b64decode(rows[0]['bundle_base64'], validate=True)
            if sha256(data) != rows[0]['bundle_sha256']:
                raise KnowledgeUnavailable('Knowledge bundle integrity failure')
            return data
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            raise KnowledgeUnavailable('Knowledge fetch or validation failed') from None

    @contextmanager
    def open_verified(self, release_id=None):
        selected = release_id or self.config.release_id
        data = self._bundle(selected)
        try:
            with TemporaryDirectory(prefix='seo-knowledge-') as directory:
                repo = KnowledgeRepository(Path(directory) / 'knowledge.sqlite',
                                           authorize=lambda operation, *_: operation == 'import')
                repo.import_bundle(data)
                repo.graph(selected)
                yield repo
        except (ValueError, KeyError, OSError) as exc:
            raise KnowledgeUnavailable('Knowledge graph unavailable') from None

    def graph(self, release_id=None):
        selected = release_id or self.config.release_id
        with self.open_verified(selected) as repo:
            return repo.graph(selected)

    def read_blob(self, digest):
        with self.open_verified() as repo:
            return repo.read_blob(digest)
