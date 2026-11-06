"""Opt-in scoped-storage prototype. Existing hosted RPC and startup stay unchanged.

Transport: authenticated seo_scoped_checkpoint_v1 RPC. The adapter validates
deterministic evidence on both write and read; SQL enforces durable identity,
integrity, bounds, immutable input and append-only observation/lifecycle rules.
"""
from copy import deepcopy
from hashlib import sha256
import json
from uuid import UUID

from .models import Observation, RequirementInput
from .scope_criteria import canonical, validate_observation

MAX_RECORD_BYTES = 2_000_000
PROFILE = 'scoped-storage-prototype/v1'
STATES = {'queued', 'running', 'completed', 'error', 'interrupted'}


def validate_record(record):
    if not isinstance(record, dict) or set(record) != {
            'profile', 'id', 'owner', 'project_id', 'state', 'requirement',
            'observations', 'error'}:
        raise ValueError('Invalid checkpoint shape')
    if record['profile'] != PROFILE or record['state'] not in STATES:
        raise ValueError('Invalid checkpoint profile/state')
    for key in ('id', 'owner', 'project_id'):
        if str(UUID(record[key])) != record[key]:
            raise ValueError('Canonical UUID required')
    req = RequirementInput.model_validate(record['requirement']).model_dump()
    if req != record['requirement'] or set(req['expected']) != {'seo_qa'}:
        raise ValueError('This adapter accepts scoped requirements only; use legacy HostedStore otherwise')
    if not isinstance(record['observations'], list) or len(record['observations']) > len(req['urls']):
        raise ValueError('Invalid observation coverage')
    if record['error'] is not None and (type(record['error']) is not str or
                                       len(record['error']) > 200):
        raise ValueError('Invalid bounded error')
    if record['state'] == 'error' and not record['error']:
        raise ValueError('Error checkpoint needs a reason')
    if record['state'] != 'error' and record['error'] is not None:
        raise ValueError('Error reason only allowed in error state')
    seen = set()
    for raw in record['observations']:
        obs = Observation.model_validate(raw).model_dump()
        if raw != obs or obs['url'] not in req['urls'] or obs['url'] in seen:
            raise ValueError('Observation shape/URL/duplicate mismatch')
        validate_observation(req['expected']['seo_qa'], obs['url'], obs)
        expected_kind = req['expected']['seo_qa']['source_kind']
        provenance = obs['evidence']['provenance']
        if provenance.get('url') != (obs['url'] if expected_kind == 'url' else None):
            raise ValueError('Provenance URL mismatch')
        seen.add(obs['url'])
    if record['state'] == 'queued' and seen:
        raise ValueError('Queued checkpoint cannot have observations')
    if record['state'] == 'completed' and seen != set(req['urls']):
        raise ValueError('Completed checkpoint lacks full coverage')
    if len(canonical(record)) > MAX_RECORD_BYTES:
        raise ValueError('Checkpoint exceeds 2 MB prototype bound')
    return deepcopy(record)


class ScopedCheckpointAdapter:
    """Explicit opt-in; rpc(action, payload) must use normal authenticated access.

    This is not a worker lease or a replacement for HostedStore. Revisions provide
    optimistic checkpoint concurrency; worker lease integration is a separate step.
    """
    def __init__(self, rpc, *, owner_id, project_id):
        self.rpc = rpc
        self.owner = str(UUID(owner_id))
        self.project = str(UUID(project_id))

    def _readback(self, response):
        if not isinstance(response, dict) or set(response) != {'canonical_body', 'sha256', 'revision'}:
            raise ValueError('Invalid checkpoint response')
        text = response['canonical_body']
        if type(text) is not str or sha256(text.encode()).hexdigest() != response['sha256']:
            raise ValueError('Checkpoint digest mismatch')
        record = validate_record(json.loads(text))
        if canonical(record).decode() != text:
            raise ValueError('Noncanonical checkpoint bytes')
        if record['owner'] != self.owner or record['project_id'] != self.project:
            raise ValueError('Checkpoint owner/project mismatch')
        if type(response['revision']) is not int or response['revision'] < 1:
            raise ValueError('Invalid revision')
        return dict(record=record, revision=response['revision'])

    def save(self, record, *, expected_revision=0):
        record = validate_record(record)
        if record['owner'] != self.owner or record['project_id'] != self.project:
            raise ValueError('Checkpoint outside selected owner/project')
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError('Invalid expected revision')
        body = canonical(record).decode()
        result = self._readback(self.rpc('save', {
            'project_id': self.project, 'run_id': record['id'],
            'canonical_body': body, 'sha256': sha256(body.encode()).hexdigest(),
            'expected_revision': expected_revision}))
        if result['record'] != record or result['revision'] != expected_revision + 1:
            raise ValueError('Checkpoint acknowledgement mismatch; reconcile before retry')
        return result

    def get(self, run_id):
        rid = str(UUID(run_id))
        result = self._readback(self.rpc('get', {'project_id': self.project, 'run_id': rid}))
        if result['record']['id'] != rid:
            raise ValueError('Checkpoint identity mismatch')
        return result
