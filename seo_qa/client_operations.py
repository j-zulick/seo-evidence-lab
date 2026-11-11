"""Offline scoped workspace operations for trusted authenticated callers.

Actor identifiers are assertions from the embedding authentication boundary, NOT
credentials. This module has no authentication endpoint, worker, live provider
or billing integration. Reserved integer units are an operator-defined local
admission budget, never a currency charge or provider quota measurement.
"""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

KINDS = frozenset({'saved_import', 'local_audit', 'report_export'})
ROLES = frozenset({'owner', 'operator', 'viewer'})


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}', value):
        raise ValueError('Invalid identifier')
    return value


def _units(value):
    if type(value) is not int or not 0 <= value <= 1_000_000:
        raise ValueError('Expected bounded nonnegative integer units')
    return value


class ClientOperations:
    def __init__(self, path):
        self.path = Path(path)
        if self.path.is_symlink():
            raise ValueError('Symlink database is forbidden')
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS workspaces (
                    id TEXT PRIMARY KEY, budget_units INTEGER NOT NULL,
                    spent_units INTEGER NOT NULL DEFAULT 0,
                    reserved_units INTEGER NOT NULL DEFAULT 0,
                    max_active INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS memberships (
                    workspace TEXT NOT NULL REFERENCES workspaces(id),
                    actor TEXT NOT NULL, role TEXT NOT NULL,
                    PRIMARY KEY(workspace, actor));
                CREATE TABLE IF NOT EXISTS client_events (
                    id TEXT PRIMARY KEY, workspace TEXT NOT NULL REFERENCES workspaces(id),
                    actor TEXT NOT NULL, action TEXT NOT NULL, subject TEXT,
                    units INTEGER NOT NULL, occurred_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS client_job_guards (
                    job TEXT PRIMARY KEY, guard_hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS client_jobs (
                    id TEXT PRIMARY KEY, workspace TEXT NOT NULL REFERENCES workspaces(id),
                    request_key TEXT NOT NULL, kind TEXT NOT NULL,
                    input_hash TEXT NOT NULL, reserved_units INTEGER NOT NULL,
                    state TEXT NOT NULL, actual_units INTEGER,
                    UNIQUE(workspace, request_key));
            ''')
        self.path.chmod(0o600)

    @contextmanager
    def _transaction(self):
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN IMMEDIATE')
            with db:
                yield db

    @staticmethod
    def _authorize(db, workspace, actor, roles=ROLES):
        _identifier(workspace)
        _identifier(actor)
        row = db.execute('SELECT role FROM memberships WHERE workspace=? AND actor=?', (workspace, actor)).fetchone()
        if row is None or row['role'] not in roles:
            raise PermissionError('Workspace operation denied')
        return row['role']

    @staticmethod
    def _event(db, workspace, actor, action, subject=None, units=0):
        db.execute('INSERT INTO client_events VALUES (?,?,?,?,?,?,?)',
                   (str(uuid4()), workspace, actor, action, subject, units,
                    datetime.now(timezone.utc).isoformat()))

    def events(self, workspace, *, authenticated_actor):
        with self._transaction() as db:
            self._authorize(db, workspace, authenticated_actor, {'owner'})
            return [dict(row) for row in db.execute('SELECT * FROM client_events WHERE workspace=? ORDER BY rowid', (workspace,))]

    def create_workspace(self, workspace, *, authenticated_actor, budget_units=0, max_active=1):
        """Operator provisioning; caller must enforce workspace creation authority."""
        _identifier(workspace)
        _identifier(authenticated_actor)
        _units(budget_units)
        if type(max_active) is not int or not 1 <= max_active <= 10:
            raise ValueError('Active job limit must be between 1 and 10')
        with self._transaction() as db:
            db.execute('INSERT INTO workspaces(id,budget_units,max_active) VALUES (?,?,?)', (workspace, budget_units, max_active))
            db.execute('INSERT INTO memberships VALUES (?,?,?)', (workspace, authenticated_actor, 'owner'))
            self._event(db, workspace, authenticated_actor, 'workspace_created', units=budget_units)

    def set_member(self, workspace, *, authenticated_actor, member, role):
        _identifier(member)
        if role is not None and role not in {'operator', 'viewer'}:
            raise ValueError('Only operator/viewer membership may be changed')
        with self._transaction() as db:
            self._authorize(db, workspace, authenticated_actor, {'owner'})
            old = db.execute('SELECT role FROM memberships WHERE workspace=? AND actor=?', (workspace, member)).fetchone()
            if old and old['role'] == 'owner':
                raise ValueError('Owner membership cannot be changed by this operation')
            if role is None:
                db.execute('DELETE FROM memberships WHERE workspace=? AND actor=?', (workspace, member))
            else:
                db.execute('INSERT OR REPLACE INTO memberships VALUES (?,?,?)', (workspace, member, role))
            self._event(db, workspace, authenticated_actor, 'member_' + (role or 'revoked'), subject=member)

    def set_budget(self, workspace, *, authenticated_actor, budget_units):
        _units(budget_units)
        with self._transaction() as db:
            self._authorize(db, workspace, authenticated_actor, {'owner'})
            row = db.execute('SELECT * FROM workspaces WHERE id=?', (workspace,)).fetchone()
            if budget_units < row['spent_units'] + row['reserved_units']:
                raise ValueError('Budget cannot undercut committed units')
            db.execute('UPDATE workspaces SET budget_units=? WHERE id=?', (budget_units, workspace))
            self._event(db, workspace, authenticated_actor, 'budget_set', units=budget_units)

    def admit(self, workspace, *, authenticated_actor, request_key, kind, input_hash, reserve_units=0):
        _identifier(request_key)
        _units(reserve_units)
        if kind not in KINDS:
            raise ValueError('Only manual offline jobs are supported')
        if not isinstance(input_hash, str) or not re.fullmatch('[0-9a-f]{64}', input_hash):
            raise ValueError('Expected exact SHA256 input identity')
        with self._transaction() as db:
            self._authorize(db, workspace, authenticated_actor, {'owner', 'operator'})
            old = db.execute('SELECT * FROM client_jobs WHERE workspace=? AND request_key=?', (workspace, request_key)).fetchone()
            if old:
                if (old['kind'], old['input_hash'], old['reserved_units']) != (kind, input_hash, reserve_units):
                    raise ValueError('Request key is bound to another operation')
                return dict(old)
            limits = db.execute('SELECT * FROM workspaces WHERE id=?', (workspace,)).fetchone()
            active = db.execute("SELECT count(*) FROM client_jobs WHERE workspace=? AND state IN ('admitted','running')", (workspace,)).fetchone()[0]
            if active >= limits['max_active']:
                raise ValueError('Workspace active job limit reached')
            if limits['spent_units'] + limits['reserved_units'] + reserve_units > limits['budget_units']:
                raise ValueError('Workspace admission budget exceeded')
            identifier = str(uuid4())
            db.execute('INSERT INTO client_jobs VALUES (?,?,?,?,?,?,?,NULL)', (identifier, workspace, request_key, kind, input_hash, reserve_units, 'admitted'))
            db.execute('UPDATE workspaces SET reserved_units=reserved_units+? WHERE id=?', (reserve_units, workspace))
            self._event(db, workspace, authenticated_actor, 'job_admitted', subject=identifier, units=reserve_units)
            return dict(db.execute('SELECT * FROM client_jobs WHERE id=?', (identifier,)).fetchone())

    def get_job(self, workspace, identifier, *, authenticated_actor):
        with self._transaction() as db:
            self._authorize(db, workspace, authenticated_actor)
            row = db.execute('SELECT * FROM client_jobs WHERE id=? AND workspace=?', (identifier, workspace)).fetchone()
            if row is None:
                raise KeyError('Job unavailable')
            return dict(row)

    def start(self, workspace, identifier, *, authenticated_actor, execution_key=None):
        with self._transaction() as db:
            self._authorize(db, workspace, authenticated_actor, {'owner', 'operator'})
            row = db.execute('SELECT * FROM client_jobs WHERE id=? AND workspace=?', (identifier, workspace)).fetchone()
            if row is None:
                raise KeyError('Job unavailable')
            if row['state'] != 'admitted':
                raise ValueError('Only explicitly admitted jobs may start')
            db.execute("UPDATE client_jobs SET state='running' WHERE id=?", (identifier,))
            if execution_key is not None:
                if not isinstance(execution_key, str) or not 32 <= len(execution_key) <= 128:
                    raise ValueError('Expected private execution key')
                db.execute('INSERT INTO client_job_guards VALUES (?,?)', (identifier, hashlib.sha256(execution_key.encode()).hexdigest()))
            self._event(db, workspace, authenticated_actor, 'job_started', subject=identifier)
        return self.get_job(workspace, identifier, authenticated_actor=authenticated_actor)

    def finish(self, workspace, identifier, *, authenticated_actor, actual_units=0, state='complete', execution_key=None):
        _units(actual_units)
        if state not in {'complete', 'failed', 'cancelled'}:
            raise ValueError('Unsupported completion state')
        with self._transaction() as db:
            self._authorize(db, workspace, authenticated_actor, {'owner', 'operator'})
            row = db.execute('SELECT * FROM client_jobs WHERE id=? AND workspace=?', (identifier, workspace)).fetchone()
            if row is None:
                raise KeyError('Job unavailable')
            guard = db.execute('SELECT guard_hash FROM client_job_guards WHERE job=?', (identifier,)).fetchone()
            supplied = hashlib.sha256(execution_key.encode()).hexdigest() if isinstance(execution_key, str) else ''
            if guard and not hmac.compare_digest(guard[0], supplied):
                raise PermissionError('Worker settlement ownership required')
            if row['state'] == state and row['actual_units'] == actual_units:
                return dict(row)
            if row['state'] not in {'admitted', 'running'}:
                raise ValueError('Job is already terminal')
            if state != 'cancelled' and row['state'] != 'running':
                raise ValueError('Explicit manual start required')
            if row['state'] == 'admitted' and actual_units:
                raise ValueError('Unstarted jobs cannot consume units')
            if actual_units > row['reserved_units']:
                raise ValueError('Actual units exceed admitted reservation')
            db.execute('UPDATE workspaces SET reserved_units=reserved_units-?,spent_units=spent_units+? WHERE id=?', (row['reserved_units'], actual_units, workspace))
            db.execute('UPDATE client_jobs SET state=?,actual_units=? WHERE id=?', (state, actual_units, identifier))
            db.execute('DELETE FROM client_job_guards WHERE job=?', (identifier,))
            self._event(db, workspace, authenticated_actor, 'job_' + state, subject=identifier, units=actual_units)
        return self.get_job(workspace, identifier, authenticated_actor=authenticated_actor)

    def remove_execution_guard_after_stop(self, workspace, identifier, *, authenticated_actor, writers_stopped=False):
        """Owner maintenance only; stop executor before releasing settlement guard.

        Keeps reservation and running state until explicit measured settlement.
        It cannot detect a lying operator acknowledgment of process termination.
        """
        if writers_stopped is not True:
            raise ValueError('Stop executor before maintenance reconciliation')
        with self._transaction() as db:
            self._authorize(db, workspace, authenticated_actor, {'owner'})
            row = db.execute('SELECT state FROM client_jobs WHERE id=? AND workspace=?', (identifier, workspace)).fetchone()
            if row is None or row['state'] != 'running':
                raise ValueError('Only running work requires reconciliation')
            db.execute('DELETE FROM client_job_guards WHERE job=?', (identifier,))
            self._event(db, workspace, authenticated_actor, 'guard_removed_after_stop', subject=identifier)

    def status(self, workspace, *, authenticated_actor):
        """Workspace-local admission metrics; never provider cost or availability."""
        snapshot = self.export(workspace, authenticated_actor=authenticated_actor)
        limits = snapshot['workspace']
        return {'schema': 'client-operations-status/v1', 'workspace': workspace,
                'jobs_by_state': {state: sum(job['state'] == state for job in snapshot['jobs'])
                                  for state in ('admitted', 'running', 'complete', 'failed', 'cancelled')},
                'budget_units': limits['budget_units'], 'spent_units': limits['spent_units'],
                'reserved_units': limits['reserved_units'],
                'available_units': limits['budget_units'] - limits['spent_units'] - limits['reserved_units'],
                'max_active': limits['max_active'], 'network_calls': 0,
                'provider_quota_measured': False}

    def export(self, workspace, *, authenticated_actor):
        with self._transaction() as db:
            self._authorize(db, workspace, authenticated_actor)
            limits = dict(db.execute('SELECT * FROM workspaces WHERE id=?', (workspace,)).fetchone())
            jobs = [dict(row) for row in db.execute('SELECT * FROM client_jobs WHERE workspace=? ORDER BY rowid', (workspace,))]
            return {'schema': 'client-operations/v1', 'workspace': limits, 'jobs': jobs,
                    'network_calls': 0, 'manual_jobs_only': True, 'provider_quota_measured': False}
