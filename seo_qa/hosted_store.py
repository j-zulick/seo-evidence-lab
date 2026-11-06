"""Full normalized hosted dummy workflow through authenticated transaction RPCs.

No management token, SQL-editor API, SQL snapshot or local database mirror is
used. SQL migration 002 owns lifecycle mutations; Storage owns evidence bytes.
"""
from __future__ import annotations

import hashlib
import json
import time
from uuid import UUID, uuid4

from .models import Observation, RequirementInput
from .supabase_adapter import SupabaseAdapter, SupabaseError, UncertainWrite


def packed(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(packed(value).encode('utf-8')).hexdigest()


class HostedStore:
    def __init__(self, api: SupabaseAdapter, project_id):
        self.api = api
        self.project_id = str(UUID(str(project_id)))
        self.owner = api.owner_id
        if api.read_root(self.project_id) is None:
            raise SupabaseError('Selected SEO dummy project root is not readable')

    def _rpc(self, action, **payload):
        self.api._owner()
        body = dict(payload, project_id=self.project_id)
        operation = {'kind': 'workflow_rpc', 'action': action, 'payload': body}
        mutating = action not in {'get_requirement', 'list_requirements', 'get_run', 'list_runs', 'list_imports', 'export_report'}
        response = self.api._request('POST', '/rest/v1/rpc/seo_workflow',
                                     operation=operation if mutating else None,
                                     json={'action': action, 'payload': body})
        try:
            data = self.api._json(response)
        except SupabaseError as exc:
            if mutating:
                raise UncertainWrite(operation) from exc
            raise
        if not isinstance(data, (dict, list)):
            if mutating:
                raise UncertainWrite(operation)
            raise SupabaseError('Unexpected workflow response')
        return data

    def _requirement(self, value):
        if not isinstance(value, dict):
            raise SupabaseError('Invalid requirement response')
        try:
            str(UUID(value['id']))
            body = RequirementInput(title=value['title'], urls=value['urls'], expected=value['expected']).model_dump()
            if digest(body) != value['content_hash'] or type(value['version']) is not int or type(value['current_version']) is not int:
                raise ValueError('Requirement content/version integrity failure')
        except (KeyError, ValueError, TypeError) as exc:
            raise SupabaseError('Requirement response integrity failure') from exc
        return value

    def create_requirement(self, title, urls, expected):
        body = RequirementInput(title=title, urls=urls, expected=expected).model_dump()
        rid = str(uuid4())
        value = self._requirement(self._rpc('create_requirement', id=rid, body=body, canonical_body=packed(body), content_hash=digest(body)))
        if value['id'] != rid or value['version'] != 1:
            raise SupabaseError('Created requirement identity/version mismatch')
        return value

    def get_requirement(self, rid):
        value = self._requirement(self._rpc('get_requirement', id=str(UUID(str(rid)))))
        if value['id'] != rid:
            raise SupabaseError('Requirement identity mismatch')
        return value

    def list_requirements(self):
        data = self._rpc('list_requirements')
        if not isinstance(data, list):
            raise SupabaseError('Expected requirement list')
        return [self._requirement(x) for x in data]

    def revise_requirement(self, rid, expected):
        old = self.get_requirement(rid)
        body = RequirementInput(title=old['title'], urls=old['urls'], expected=expected).model_dump()
        value = self._requirement(self._rpc('revise_requirement', id=rid, expected_version=old['current_version'], body=body, canonical_body=packed(body), content_hash=digest(body)))
        if value['id'] != rid or value['version'] != old['current_version'] + 1:
            raise SupabaseError('Revised requirement identity/version mismatch')
        return value

    def _hydrate_run(self, run):
        if not isinstance(run, dict) or run.get('owner') != self.owner or run.get('data_mode') != 'dummy/supabase-hosted':
            raise SupabaseError('Hosted run identity/profile mismatch')
        req = self._requirement(run['requirement'])
        if run.get('requirement_id') != req['id'] or run.get('version') != req['version']:
            raise SupabaseError('Run requirement binding mismatch')
        if run.get('state') not in {'queued', 'running', 'completed', 'error', 'interrupted'}:
            raise SupabaseError('Invalid run lifecycle state')
        findings, seen = [], set()
        for value in run.get('observations', []):
            obs = Observation.model_validate(value).model_dump()
            if obs['url'] not in req['urls'] or obs['url'] in seen:
                raise SupabaseError('Observation URL mismatch/duplicate')
            seen.add(obs['url'])
            h = digest(obs)
            path = self.owner + '/' + self.project_id + '/' + run['id'] + '/' + h + '.json'
            self.api.reconcile_evidence({'kind': 'upload_evidence', 'owner_id': self.owner, 'path': path, 'sha256': h})
            findings.extend(obs['findings'])
        if run['state'] == 'completed' and len(seen) != len(req['urls']):
            raise SupabaseError('Completed run lacks full URL coverage')
        run['findings'] = findings
        return run

    def create_run(self, requirement_id):
        run_id = str(uuid4())
        value = self._hydrate_run(self._rpc('create_run', id=run_id, requirement_id=str(UUID(str(requirement_id)))))
        if value['id'] != run_id or value['requirement_id'] != requirement_id:
            raise SupabaseError('Created run identity mismatch')
        return value

    def get_run(self, run_id):
        run = self._hydrate_run(self._rpc('get_run', run_id=str(UUID(str(run_id)))))
        if run['id'] != run_id:
            raise SupabaseError('Run identity mismatch')
        return run

    def list_runs(self):
        data = self._rpc('list_runs')
        if not isinstance(data, list):
            raise SupabaseError('Expected run list')
        return [self._hydrate_run(run) for run in data]

    def _run_metadata(self, run):
        # Validate identity and requirement binding before exposing navigation.
        if not isinstance(run, dict) or run.get('owner') != self.owner or run.get('data_mode') != 'dummy/supabase-hosted':
            raise SupabaseError('Hosted run identity/profile mismatch')
        req = self._requirement(run['requirement'])
        run_id = str(UUID(run['id']))
        if run.get('requirement_id') != req['id'] or run.get('version') != req['version']:
            raise SupabaseError('Run requirement binding mismatch')
        if run.get('state') not in {'queued', 'running', 'completed', 'error', 'interrupted'}:
            raise SupabaseError('Invalid run lifecycle state')
        return {'id': run_id, 'requirement_id': req['id'], 'requirement': req,
                'version': run['version'], 'state': run['state'],
                'stale': run['version'] != req['current_version']}

    def _raw_run_list(self):
        data = self._rpc('list_runs')
        if not isinstance(data, list):
            raise SupabaseError('Expected run list')
        # Invalid identity fails the whole list, never exposes another owner's row.
        return [(run, self._run_metadata(run)) for run in data]

    def list_queue_states(self):
        """Validated owner-scoped lifecycle metadata without evidence downloads.

        Queue/status selection is not report acceptance. execute_run/get_run still
        verify the selected run and its stored evidence before exposing results.
        """
        return [dict(id=summary['id'], state=summary['state'],
                     created=run.get('created'), lease=run.get('lease'))
                for run, summary in self._raw_run_list()]

    def list_run_summaries(self):
        rows = self._raw_run_list()
        for run, summary in rows:
            try:
                self._hydrate_run(run)
                summary['evidence_status'] = 'verified'
            except (ValueError, OSError):
                summary['evidence_status'] = 'unavailable'
        return [summary for _, summary in rows]

    def execute_run(self, run_id, inspector_callable):
        token = str(uuid4())
        run = self._hydrate_run(self._rpc('claim_run', run_id=run_id, token=token))
        if run['state'] == 'completed':
            return run
        if run.get('token') != token or run['state'] != 'running':
            raise SupabaseError('Run claim not established')
        try:
            for url in run['requirement']['urls']:
                current = self.get_run(run_id)
                if (current['state'] != 'running' or current.get('token') != token or
                    current.get('lease') is None or current['lease'] <= time.time()):
                    raise SupabaseError('Worker ownership expired before capture')
                obs = Observation.model_validate(inspector_callable(url, run['requirement']['expected'])).model_dump()
                if obs['url'] != url or not obs['findings'] or not obs['evidence']:
                    raise ValueError('Incomplete/mismatched observation')
                h = digest(obs)
                path = self.owner + '/' + self.project_id + '/' + run_id + '/' + h + '.json'
                self.api.upload_evidence(path, packed(obs).encode('utf-8'), 'application/json')
                self._rpc('append_observation', run_id=run_id, token=token, id=str(uuid4()), observation=obs, canonical_body=packed(obs), hash=h, evidence_path=path)
            self._rpc('complete_run', run_id=run_id, token=token)
        except Exception as error:
            try:
                self._rpc('fail_run', run_id=run_id, token=token, error=type(error).__name__)
            except Exception:
                # Preserve the original failure. If failure marking is uncertain,
                # lease recovery is the safe next step, never re-dispatch a write.
                pass
            raise
        return self.get_run(run_id)

    def recover_expired(self):
        return self._rpc('recover_expired')['count']

    def review(self, run_id, decision, reason='', expected_version=None):
        self.get_run(run_id)  # Verify actual stored bytes before server-side review.
        payload = dict(run_id=run_id, id=str(uuid4()), decision=decision, reason=reason)
        if expected_version is not None:
            payload['expected_version'] = expected_version
        return self._rpc('review', **payload)

    def export_report(self, run_id):
        run = self._hydrate_run(self._rpc('export_report', run_id=run_id))
        latest = next((summary['id'] for _, summary in self._raw_run_list()
                       if summary['requirement_id'] == run['requirement_id']), None)
        if latest is None:
            raise SupabaseError('Latest run unavailable')
        run['latest_run_id'] = latest
        run['superseded'] = latest != run_id
        if run['superseded']:
            run['acceptance_current'] = False
        run['coverage'] = {'expected': len(run['requirement']['urls']), 'observed': len(run['observations']), 'sampled': False}
        run['limits'] = ['Dummy data in Supabase hosted workflow; business outcomes are not established.',
                        'Technical checks do not prove indexing, citations, visits or conversions.']
        from .scoring import with_scoring
        return with_scoring(run)

    def save_import(self, kind, raw, result):
        if kind not in {'crawl', 'queries', 'comparison'} or not isinstance(raw, str) or len(raw.encode()) > 2_000_000:
            raise ValueError('Invalid bounded import')
        h = digest({'kind': kind, 'raw': raw, 'result': result})
        return self._rpc('save_import', id=str(uuid4()), kind=kind, raw=raw, result=result, hash=h)['id']

    def list_imports(self):
        rows = self._rpc('list_imports')
        if not isinstance(rows, list):
            raise SupabaseError('Expected import list')
        for row in rows:
            if digest({'kind': row['kind'], 'raw': row['raw'], 'result': row['result']}) != row['hash']:
                raise SupabaseError('Import integrity failure')
        return rows

    def propose_notion(self, run_id):
        if not self.export_report(run_id)['acceptance_current']:
            raise ValueError('Current acceptance required')
        return self._rpc('propose_notion', run_id=run_id, id=str(uuid4()))

    def apply_fixture_notion(self, operation_id, lose_response=False):
        return self._rpc('apply_fixture_notion', operation_id=operation_id, lose_response=lose_response)

    def reconcile_notion(self, operation_id):
        return self._rpc('reconcile_notion', operation_id=operation_id)

    def reconcile_rpc(self, operation):
        """Read stable IDs after uncertain writes; never replay the mutation."""
        if operation.get('kind') != 'workflow_rpc' or operation.get('payload', {}).get('project_id') != self.project_id:
            raise ValueError('Operation is outside selected project')
        payload, action = operation['payload'], operation['action']
        if action in {'create_requirement', 'revise_requirement'}:
            value = self.get_requirement(payload['id'])
            if value['content_hash'] != payload['content_hash']:
                raise ValueError('Reconciled requirement conflicts')
            return {'state': 'confirmed', 'result': value}
        if action == 'create_run':
            return {'state': 'confirmed', 'result': self.get_run(payload['id'])}
        if action == 'save_import':
            found = [r for r in self.list_imports() if r['hash'] == payload['hash']]
            if len(found) == 1:
                return {'state': 'confirmed', 'result': found[0]}
        if action == 'review':
            rows = [r for r in self.get_run(payload['run_id'])['reviews'] if r['id'] == payload['id']]
            if len(rows) == 1:
                return {'state': 'confirmed', 'result': rows[0]}
        if action in {'apply_fixture_notion', 'reconcile_notion'}:
            return self.reconcile_notion(payload['operation_id'])
        return {'state': 'unknown', 'reason': 'Readback/reconciliation required; do not blindly repeat this write'}

    def backup(self, destination):
        raise NotImplementedError('Hosted backup requires coordinated Postgres export plus Storage bytes; local snapshot is not a hosted backup')

    @classmethod
    def restore(cls, source, destination, owner_id=None):
        raise NotImplementedError('Hosted restore requires an isolated authorized database and Storage destination')
