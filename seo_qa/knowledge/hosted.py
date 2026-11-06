"""Versioned hosted scoped admission with an atomic, owner-bound knowledge pin."""
import base64
from datetime import datetime, date
import json
from uuid import uuid4

from pydantic import TypeAdapter

from ..scoped_workflow import ScopedStore
from ..scoped_storage import PROFILE
from ..supabase_adapter import SupabaseError, UncertainWrite
from .audit_pin import prepare_admission
from .contracts import AuditImpact, KnowledgeRelease, canonical_bytes, record_bytes, sha256
from .policy import ReleaseRecord, validate_release

CONTRACT = {'contract': 'seo-knowledge-workflow/v2', 'pin_version': 2,
            'admission': 'atomic-owner-scoped', 'activation': 'disabled'}


def validate_pin(envelope, run_id):
    try:
        wire = envelope['canonical_body']
        body = json.loads(wire)
        if (len(wire.encode()) > 4_000_000 or canonical_bytes(body).decode() != wire
                or sha256(wire.encode()) != envelope['sha256']):
            raise ValueError()
        impact = AuditImpact.model_validate(body['impact'])
        if impact.run_id != run_id or impact.mode != 'advisory':
            raise ValueError()
        if datetime.fromisoformat(body['admitted_at']).tzinfo is None:
            raise ValueError()
        annotation = body['annotation']
        manifest = KnowledgeRelease.model_validate(annotation['manifest'])
        records = [TypeAdapter(ReleaseRecord).validate_python(item['value'])
                   for item in annotation['records']]
        if (manifest.id != impact.release_id or any(
                sha256(record_bytes(record)) != item['sha256']
                for record, item in zip(records, annotation['records']))):
            raise ValueError()
        blobs = {digest: base64.b64decode(data, validate=True)
                 for digest, data in annotation['blobs'].items()}
        validate_release(manifest, records, blobs.__getitem__)
        from .audit_pin import annotate
        from .retrieval import Context
        from .freshness import assess_release, decode_inputs
        fields = body['context']
        context = Context(**{**fields, 'evaluated_on': date.fromisoformat(fields['evaluated_on']),
                             'true_conditions': frozenset(fields['true_conditions'])})
        if context.evaluated_on != impact.evaluated_on:
            raise ValueError()
        class Pinned:
            def graph(self, release_id=None):
                if release_id not in (None, manifest.id):
                    raise KeyError(release_id)
                return manifest, records

            def read_blob(self, digest):
                return blobs[digest]
        revision = annotation.get('freshness_revision', 'unknown')
        if revision not in {'unknown', 'source-freshness/v1'}:
            raise ValueError('Unsupported freshness implementation')
        expected = annotate(Pinned(), manifest, records, context,
                            evaluated_at=datetime.fromisoformat(body['admitted_at']),
                            freshness=assess_release if revision == 'source-freshness/v1' else None,
                            freshness_inputs=decode_inputs(annotation.get('freshness_evidence', {})))
        if expected != annotation:
            raise ValueError('Knowledge annotation does not match pinned evidence')
        return dict(**body['impact'], admitted_at=body['admitted_at'], **annotation)
    except (KeyError, TypeError, ValueError):
        raise SupabaseError('Hosted knowledge pin integrity failure') from None


class HostedKnowledgeStore(ScopedStore):
    def __init__(self, api, project_id, *, repository, context, freshness=None, freshness_inputs=None, admissions_enabled=True):
        self.admissions_enabled = admissions_enabled
        self.repository = repository
        self.context = context
        self.freshness = freshness
        self.freshness_inputs = freshness_inputs
        super().__init__(api, project_id)
        if self._rpc('knowledge_contract') != CONTRACT:
            raise SupabaseError('Hosted knowledge migration contract unavailable')

    def _rpc(self, action, **payload):
        self.api._owner()
        body = {'project_id': self.project_id, **payload}
        reads = {'contract', 'knowledge_contract', 'lookup', 'get_requirement',
                 'list_requirements', 'get_run', 'list_runs', 'list_queue_states', 'latest_run'}
        operation = None if action in reads else {
            'kind': 'knowledge_workflow_rpc', 'action': action, 'payload': body}
        if len(canonical_bytes(body)) > 6_500_000:
            raise SupabaseError('Combined knowledge checkpoint payload exceeds limit')
        response = self.api._request('POST', '/rest/v1/rpc/seo_knowledge_workflow_v1',
                                     operation=operation, json={'action': action, 'payload': body})
        try:
            value = self.api._json(response)
            if not isinstance(value, (dict, list)):
                raise SupabaseError('Invalid knowledge workflow response')
            return value
        except SupabaseError:
            if operation:
                raise UncertainWrite(operation) from None
            raise

    def _validate_wire(self, wire, run_id=None):
        result = super()._validate_wire(wire, run_id)
        if type(wire.get('knowledge_pin_required')) is not bool:
            raise SupabaseError('Knowledge admission marker unavailable')
        if (wire.get('knowledge_pin') is not None) != wire['knowledge_pin_required']:
            raise SupabaseError('Hosted knowledge pin and admission marker disagree')
        if wire.get('knowledge_pin') is not None:
            validate_pin(wire['knowledge_pin'], result[1]['id'])
            body = json.loads(wire['knowledge_pin']['canonical_body'])
            expected = dict(owner_id=self.owner, project_id=self.project_id,
                            requirement_id=result[2]['id'], requirement_version=result[2]['version'],
                            requirement_sha256=result[2]['content_hash'])
            if body.get('admission_binding') != expected:
                raise SupabaseError('Hosted knowledge requirement binding mismatch')
        return result

    def _run(self, wire, record, req):
        run = super()._run(wire, record, req)
        run['knowledge'] = (validate_pin(wire['knowledge_pin'], record['id'])
                            if wire.get('knowledge_pin') is not None else
                            dict(mode='legacy', limitations=['Historical run has no knowledge pin']))
        return run

    def create_run(self, requirement_id):
        if not self.admissions_enabled:
            raise SupabaseError('New knowledge admissions are paused; historical pins remain readable')
        req = self.get_requirement(requirement_id)
        run_id = str(uuid4())
        pin = prepare_admission(self.repository, self.context, run_id,
                                freshness=self.freshness, freshness_inputs=self.freshness_inputs)
        pin['admission_binding'] = dict(
            owner_id=self.owner, project_id=self.project_id, requirement_id=req['id'],
            requirement_version=req['current_version'], requirement_sha256=req['content_hash'])
        pin_bytes = canonical_bytes(pin)
        if len(pin_bytes) > 4_000_000:
            raise SupabaseError('Knowledge admission pin exceeds limit')
        envelope = dict(canonical_body=pin_bytes.decode(), sha256=sha256(pin_bytes))
        record = dict(profile=PROFILE, id=run_id, owner=self.owner, project_id=self.project_id,
                      state='queued', requirement={k: req[k] for k in ('title', 'urls', 'expected')},
                      observations=[], error=None)
        payload = self._checkpoint_payload(record, 0)
        wire = self._rpc('create_run', requirement_id=req['id'],
                         requirement_version=req['current_version'], knowledge_pin=envelope, **payload)
        try:
            validated = self._validate_wire(wire, run_id)
            if (wire.get('knowledge_pin_required') is not True
                    or validated[1] != record
                    or wire['checkpoint']['canonical_body'] != payload['canonical_body']
                    or wire['checkpoint']['sha256'] != payload['sha256']
                    or wire.get('token') is not None or wire.get('lease') is not None
                    or wire.get('knowledge_pin') != envelope or wire['checkpoint']['revision'] != 1
                    or wire['requirement']['id'] != req['id']
                    or wire['requirement']['version'] != req['current_version']):
                raise SupabaseError('Knowledge admission acknowledgement mismatch')
            return self._run(*validated)
        except (SupabaseError, ValueError, KeyError):
            raise UncertainWrite({'kind': 'knowledge_admission', 'run_id': run_id,
                                  'pin_sha256': envelope['sha256'],
                                  'next_action': 'read_existing_run_before_retry'}) from None

    def execute_run(self, run_id, inspector_callable):
        wire, _, _ = self._load(run_id)
        if wire.get('knowledge_pin') is None:
            raise SupabaseError('Knowledge-enabled worker requires an admitted pin')
        return super().execute_run(run_id, inspector_callable)


from ..scoped_workflow import CompositeStore


class KnowledgeCompositeStore(CompositeStore):
    def create_run(self, requirement_id):
        selected = self._store('requirement', requirement_id)
        if selected is self.legacy:
            raise SupabaseError('Knowledge-enabled admission requires scoped requirements')
        return selected.create_run(requirement_id)

    def execute_run(self, run_id, inspector_callable):
        selected = self._store('run', run_id)
        if selected is self.legacy:
            raise SupabaseError('Knowledge-enabled execution requires a scoped admitted pin')
        return selected.execute_run(run_id, inspector_callable)
