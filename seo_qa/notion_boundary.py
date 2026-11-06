"""Offline Notion boundary prototype. Fake protocol deliberately has no network implementation."""
import copy
import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager


def packed(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(packed(value).encode()).hexdigest()


class Conflict(ValueError):
    pass


class ReplayRefused(ValueError):
    pass


class FakeNotionTransport:
    """v1: atomic CAS and patch/marker write; NOT a real Notion API guarantee."""
    protocol = 'FakeNotionTransport/v1'

    def __init__(self, destination, properties):
        self.destination = copy.deepcopy(destination)
        self.properties = copy.deepcopy(properties)
        self.revision = 1
        self.marker = None
        self.calls = 0
        self.writes = 0
        self.mode = 'ok'
        self.before_patch = None
        self.lock = threading.RLock()

    def read(self, destination):
        with self.lock:
            if destination != self.destination:
                raise ValueError('Destination mismatch')
            return copy.deepcopy(dict(destination=self.destination, revision=self.revision,
                                      properties=self.properties, marker=self.marker))

    def edit(self, changes):
        with self.lock:
            self.properties.update(copy.deepcopy(changes))
            self.revision += 1

    def patch(self, destination, revision, changes, marker):
        with self.lock:
            self.calls += 1
            if self.before_patch:
                self.before_patch()
            if destination != self.destination or revision != self.revision:
                raise Conflict('Concurrent destination edit')
            if self.mode == 'drop_before':
                raise TimeoutError('No response before effect')
            self.properties.update(copy.deepcopy(changes))
            self.marker = copy.deepcopy(marker)
            self.revision += 1
            self.writes += 1
            if self.mode == 'drop_after':
                raise TimeoutError('Reply lost after effect')
            return self.read(destination)


class Connector:
    def __init__(self, path, owner, transport, report_reader):
        if type(transport) is not FakeNotionTransport:
            raise ValueError('Only explicit fake transport v1 supported')
        self.path, self.owner, self.transport, self.report_reader = str(path), owner, transport, report_reader
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS ops (id TEXT PRIMARY KEY, owner TEXT NOT NULL, envelope TEXT NOT NULL, approval TEXT, reviewer TEXT, state TEXT NOT NULL, receipt TEXT)')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=3)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _op(self, db, operation_id):
        row = db.execute('SELECT * FROM ops WHERE id=? AND owner=?', (operation_id, self.owner)).fetchone()
        if row is None:
            raise ValueError('Operation unavailable')
        return dict(row)

    def _unresolved_predecessor(self, db, destination, operation_id):
        for row in db.execute("SELECT id,envelope FROM ops WHERE owner=? AND state='unknown' AND id!=?",
                              (self.owner, operation_id)):
            if json.loads(row['envelope'])['destination'] == destination:
                raise ReplayRefused('Unresolved predecessor at destination; reconcile before new work')

    def get(self, operation_id):
        with self.db() as db:
            return self._op(db, operation_id)

    def _source(self, run_id):
        report = self.report_reader(run_id)
        if (report.get('id') != run_id or not report.get('acceptance_current') or not report.get('reviews')
                or report['reviews'][-1]['decision'] not in ('accepted', 'accepted_with_waiver')):
            raise ValueError('Current source acceptance required')
        return digest(report)

    def propose(self, run_id, destination, mapping, changes):
        if (set(destination) != {'workspace', 'database', 'page'} or
                any(not isinstance(v, str) or not v.strip() for v in destination.values())):
            raise ValueError('Exact destination required')
        if (not changes or set(mapping) != set(changes) or
                any(not isinstance(v, str) or not v.strip() or v.startswith('_') for v in mapping.values()) or
                len(set(mapping.values())) != len(mapping)):
            raise ValueError('Explicit unique property mapping required')
        if any(not isinstance(v, str) for v in changes.values()):
            raise ValueError('v1 supports exact string property values only')
        snapshot = self.transport.read(destination)
        patch = {mapping[k]: v for k, v in changes.items()}
        if not set(patch) <= set(snapshot['properties']):
            raise ValueError('Property missing from destination schema')
        envelope = dict(schema='S09-envelope/v1', owner=self.owner, destination=destination,
                        mapping=mapping, changes=patch, revision=snapshot['revision'],
                        run_id=run_id, source_hash=self._source(run_id))
        content = packed(envelope)
        operation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'seo-qa-notion/v1:' + content))
        with self.db() as db:
            self._unresolved_predecessor(db, destination, operation_id)
            db.execute('INSERT OR IGNORE INTO ops VALUES(?,?,?,NULL,NULL,?,NULL)',
                       (operation_id, self.owner, content, 'proposed'))
        return self.get(operation_id)

    def approve(self, operation_id, exact_hash, reviewer):
        if not isinstance(reviewer, str) or not reviewer.strip():
            raise ValueError('Explicit reviewer required')
        with self.db() as db:
            op = self._op(db, operation_id)
            if op['state'] != 'proposed' or digest(json.loads(op['envelope'])) != exact_hash:
                raise ValueError('Approval content/state mismatch')
            db.execute('UPDATE ops SET approval=?,reviewer=? WHERE id=?', (exact_hash, reviewer, operation_id))

    def dispatch(self, operation_id):
        with self.db() as db:
            op = self._op(db, operation_id)
            if op['state'] != 'proposed':
                raise ReplayRefused('Reconcile existing operation; replay refused')
            env = json.loads(op['envelope'])
            self._unresolved_predecessor(db, env['destination'], operation_id)
            expected_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'seo-qa-notion/v1:' + packed(env)))
            if (expected_id != operation_id or env['owner'] != self.owner or not op['reviewer']
                    or op['approval'] != digest(env)):
                raise ValueError('Exact approval required')
            if self._source(env['run_id']) != env['source_hash']:
                raise ValueError('Source review/content stale')
            # Durable admission BEFORE effect; even process death here forbids replay.
            db.execute('UPDATE ops SET state=? WHERE id=?', ('unknown', operation_id))
        marker = dict(operation_id=operation_id, content_hash=op['approval'])
        try:
            self.transport.patch(env['destination'], env['revision'], env['changes'], marker)
        except Conflict:
            with self.db() as db:
                db.execute('UPDATE ops SET state=? WHERE id=?', ('conflict', operation_id))
            return self.get(operation_id)
        except Exception:
            return self.get(operation_id)
        return self.reconcile(operation_id)

    def reconcile(self, operation_id):
        op = self.get(operation_id)
        if op['state'] != 'unknown':
            return op
        env = json.loads(op['envelope'])
        try:
            snapshot = self.transport.read(env['destination'])
        except Exception:
            return op
        expected = dict(operation_id=operation_id, content_hash=op['approval'])
        if (snapshot['destination'] == env['destination'] and snapshot['marker'] == expected
                and all(snapshot['properties'].get(k) == v for k, v in env['changes'].items())):
            with self.db() as db:
                db.execute('UPDATE ops SET state=?,receipt=? WHERE id=? AND state=?',
                           ('applied', packed(snapshot), operation_id, 'unknown'))
        return self.get(operation_id)
