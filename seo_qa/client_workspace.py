"""Manual offline import/audit/report execution behind workspace admission."""
import hashlib
import json
import secrets
from pathlib import Path

from .store import Store
from .inspector import inspect_html
from .imports import import_queries


def _public_report(value):
    if isinstance(value, dict):
        return {k: _public_report(v) for k, v in value.items() if k not in {'token', 'lease', 'session_hash', 'csrf_token'}}
    if isinstance(value, list):
        return [_public_report(v) for v in value]
    return value


class WorkspaceExecutor:
    def __init__(self, operations, data_root):
        self.operations = operations
        self.root = Path(data_root)
        with operations._transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS client_results (job TEXT PRIMARY KEY REFERENCES client_jobs(id), locator TEXT NOT NULL)')

    def store(self, workspace, actor):
        with self.operations._transaction() as db:
            self.operations._authorize(db, workspace, actor, {'owner', 'operator'})
        directory = self.root / workspace
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        store = Store(directory / 'audit.sqlite', directory / 'evidence', owner_id=workspace)
        store.path.chmod(0o600)
        return store

    def _resolve(self, store, locator):
        if 'run_id' in locator:
            return _public_report(store.export_report(locator['run_id']))
        return locator

    def execute(self, workspace, actor, request_key, action, payload):
        shapes = {'import': {'csv'}, 'audit': {'title', 'url', 'html', 'expected'}, 'report': {'run_id'}}
        kinds = {'import': 'saved_import', 'audit': 'local_audit', 'report': 'report_export'}
        if action not in shapes or not isinstance(payload, dict) or set(payload) != shapes[action]:
            raise ValueError('Unsupported local operation input')
        packed = json.dumps(payload, sort_keys=True, allow_nan=False)
        if len(packed.encode()) > 1_000_000:
            raise ValueError('Local operation input too large')
        binding = hashlib.sha256((action + ':' + packed).encode()).hexdigest()
        job = self.operations.admit(workspace, authenticated_actor=actor, request_key=request_key,
                                    kind=kinds[action], input_hash=binding, reserve_units=1)
        store = self.store(workspace, actor)
        if job['state'] == 'complete':
            with self.operations._transaction() as db:
                locator = db.execute('SELECT locator FROM client_results WHERE job=?', (job['id'],)).fetchone()
            if locator is None:
                raise ValueError('Completed operation requires result reconciliation')
            return {'job': job, 'result': self._resolve(store, json.loads(locator[0]))}
        if job['state'] != 'admitted':
            raise ValueError('Operation needs deliberate reconciliation; no automatic replay')
        execution_key = secrets.token_urlsafe(32)
        self.operations.start(workspace, job['id'], authenticated_actor=actor, execution_key=execution_key)
        try:
            if action == 'import':
                result = import_queries(payload['csv'])
                identifier = store.save_import('queries', payload['csv'], result)
                locator = {'import_id': identifier, 'rows': len(result.get('rows', []))}
            elif action == 'audit':
                if not isinstance(payload['html'], str):
                    raise ValueError('Expected saved HTML')
                requirement = store.create_requirement(payload['title'], [payload['url']], payload['expected'])
                run = store.create_run(requirement['id'])
                store.execute_run(run['id'], lambda url, expected: inspect_html(payload['html'], url, expected=expected))
                locator = {'run_id': run['id']}
            else:
                store.export_report(payload['run_id'])  # Ownership checked before retaining locator.
                locator = {'run_id': payload['run_id']}
            with self.operations._transaction() as db:
                db.execute('INSERT INTO client_results VALUES (?,?)', (job['id'], json.dumps(locator, sort_keys=True)))
            done = self.operations.finish(workspace, job['id'], authenticated_actor=actor, actual_units=1, execution_key=execution_key)
            return {'job': done, 'result': self._resolve(store, locator)}
        except Exception:
            # Work may have partially executed. Preserve the failed job and charge
            # its one deterministic local processing unit; never replay silently.
            current = self.operations.get_job(workspace, job['id'], authenticated_actor=actor)
            if current['state'] == 'running':
                self.operations.finish(workspace, job['id'], authenticated_actor=actor, actual_units=1, state='failed', execution_key=execution_key)
            raise

    def backup(self, workspace, actor, destination, *, writers_stopped=False):
        """Export one authorized workspace plus its filtered control plane."""
        import sqlite3
        from contextlib import closing
        from .release_recovery import _staging, export_state
        self.operations.events(workspace, authenticated_actor=actor)  # Owner only.
        if writers_stopped is not True:
            raise ValueError('Stop writers before workspace backup')
        with _staging(destination) as stage:
            sliced = stage / 'selected.sqlite'
            with closing(sqlite3.connect(self.operations.path)) as source, closing(sqlite3.connect(sliced)) as target:
                source.backup(target)
                target.execute('PRAGMA foreign_keys=ON')
                target.execute('PRAGMA secure_delete=ON')
                target.execute('DELETE FROM client_job_guards WHERE job IN (SELECT id FROM client_jobs WHERE workspace<>?)', (workspace,))
                target.execute('DELETE FROM client_results WHERE job IN (SELECT id FROM client_jobs WHERE workspace<>?)', (workspace,))
                for table in ('client_events', 'client_jobs', 'memberships'):
                    target.execute(f'DELETE FROM {table} WHERE workspace<>?', (workspace,))
                target.execute('DELETE FROM workspaces WHERE id<>?', (workspace,))
                target.commit()
                target.execute('VACUUM')
            export_state({'clientops': sliced}, {}, stage / 'control', writers_stopped=True)
            sliced.unlink()
            directory = self.root / workspace
            has_audit = (directory / 'audit.sqlite').exists()
            if has_audit:
                export_state({'audit': directory / 'audit.sqlite'},
                             {'audit_evidence': directory / 'evidence'},
                             stage / 'workspace', writers_stopped=True)
            manifest = {'schema': 'client-workspace-recovery/v1', 'workspace': workspace, 'has_audit': has_audit}
            (stage / 'workspace.json').write_text(json.dumps(manifest, sort_keys=True) + '\n')
            (stage / 'workspace.json').chmod(0o600)
        return manifest

    @staticmethod
    def restore(source, destination):
        """Private operator restore to a new root; verify all before installation."""
        import shutil
        import sqlite3
        from contextlib import closing
        from .client_operations import _identifier
        from .release_recovery import _staging, verify_bundle, restore_state
        source = Path(source)
        if source.is_symlink() or (source / 'workspace.json').is_symlink():
            raise ValueError('Symlink backup refused')
        manifest = json.loads((source / 'workspace.json').read_text())
        if set(manifest) != {'schema', 'workspace', 'has_audit'} or manifest['schema'] != 'client-workspace-recovery/v1' or type(manifest['has_audit']) is not bool:
            raise ValueError('Invalid workspace recovery manifest')
        workspace = _identifier(manifest['workspace'])
        expected = {'control', 'workspace.json'} | ({'workspace'} if manifest['has_audit'] else set())
        if {p.name for p in source.iterdir() if p.name != '.seo-recovery.lock'} != expected:
            raise ValueError('Unexpected workspace backup inventory')
        verify_bundle(source / 'control')
        with closing(sqlite3.connect(source / 'control/databases/clientops.sqlite')) as db:
            if db.execute('SELECT id FROM workspaces').fetchall() != [(workspace,)]:
                raise ValueError('Workspace manifest identity mismatch')
            count = db.execute('SELECT count(*) FROM client_results').fetchone()[0]
            if count and not manifest['has_audit']:
                raise ValueError('Workspace results require paired Store backup')
        if manifest['has_audit']:
            verify_bundle(source / 'workspace')
            audit_path = (source / 'workspace/databases/audit.sqlite').resolve()
            with closing(sqlite3.connect(audit_path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
                for table in ('requirements', 'runs', 'operations', 'import_records'):
                    if db.execute(f'SELECT count(*) FROM {table} WHERE owner<>?', (workspace,)).fetchone()[0]:
                        raise ValueError('Paired Store workspace owner mismatch')
        with _staging(destination) as stage:
            restore_state(source / 'control', stage / 'control-copy')
            (stage / 'control-copy/databases/clientops.sqlite').rename(stage / 'clientops.sqlite')
            shutil.rmtree(stage / 'control-copy')
            if manifest['has_audit']:
                restore_state(source / 'workspace', stage / 'workspace-copy')
                target = stage / 'workspaces' / workspace
                target.mkdir(parents=True, mode=0o700)
                (stage / 'workspace-copy/databases/audit.sqlite').rename(target / 'audit.sqlite')
                (stage / 'workspace-copy/evidence/audit_evidence').rename(target / 'evidence')
                shutil.rmtree(stage / 'workspace-copy')
                # Validate actual references with normal application readers
                # before the staging directory becomes an installed destination.
                from .client_operations import ClientOperations
                restored_ops = ClientOperations(stage / 'clientops.sqlite')
                loaded = WorkspaceExecutor(restored_ops, stage / 'workspaces')
                with restored_ops._transaction() as db:
                    owner = db.execute("SELECT actor FROM memberships WHERE workspace=? AND role='owner'", (workspace,)).fetchone()
                    locators = [json.loads(row[0]) for row in db.execute('SELECT locator FROM client_results')]
                if owner is None:
                    raise ValueError('Workspace owner missing from recovery')
                restored_store = loaded.store(workspace, owner[0])
                import_ids = {row['id'] for row in restored_store.list_imports()}
                for locator in locators:
                    if set(locator) == {'run_id'}:
                        restored_store.export_report(locator['run_id'])
                    elif set(locator) == {'import_id', 'rows'} and locator['import_id'] in import_ids:
                        pass
                    else:
                        raise ValueError('Paired workspace result reference unavailable')
        return manifest
