"""Portable offline bridge; dependencies explicitly injected, no live transport."""
import json
from contextlib import contextmanager
from pathlib import Path
import sqlite3


class DiskFake:
    """DiskFake/v1: persist actual FakeNotionTransport/v1 CAS, no HTTP.

    The exported transport remains an actual FakeNotionTransport instance to
    respect S09's strict type boundary. Each disk transaction invokes the native
    fake's patch on a rehydrated snapshot; effect + markers + counters commit
    together. Every attempt increments calls even if the same ID is repeated.
    """
    def __init__(self, path, fake_class, destination=None, properties=None, mode='ok', after_effect=lambda: None, before_patch=lambda: None):
        self.path, self.fake_class = str(path), fake_class
        self.mode, self.after_effect, self.before_patch = mode, after_effect, before_patch
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS page(id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)')
            row = db.execute('SELECT body FROM page WHERE id=1').fetchone()
            if not row:
                if destination is None or properties is None:
                    raise ValueError('Destination not initialized')
                native = fake_class(destination, properties)
                self.save(db, native)
            else:
                native = self.load(db)
                if destination is not None and destination != native.destination:
                    raise ValueError('Destination differs from saved disk fake')
        self.transport = fake_class(native.destination, native.properties)
        self.transport.read = self.read
        self.transport.patch = self.patch
        self.transport.edit = self.edit

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.execute('PRAGMA synchronous=FULL')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def load(self, db):
        body = json.loads(db.execute('SELECT body FROM page WHERE id=1').fetchone()[0])
        native = self.fake_class(body['destination'], body['properties'])
        for key in ('revision', 'marker', 'calls', 'writes'):
            setattr(native, key, body[key])
        return native

    @staticmethod
    def save(db, native):
        body = {key: getattr(native, key) for key in ('destination', 'properties', 'revision', 'marker', 'calls', 'writes')}
        db.execute('INSERT OR REPLACE INTO page VALUES(1,?)', (json.dumps(body, sort_keys=True),))

    def snapshot(self):
        with self.db() as db:
            return json.loads(db.execute('SELECT body FROM page WHERE id=1').fetchone()[0])

    def read(self, destination):
        with self.db() as db:
            return self.load(db).read(destination)

    def edit(self, changes):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            native = self.load(db)
            native.edit(changes)
            self.save(db, native)

    def patch(self, destination, revision, changes, marker):
        self.before_patch()
        error = None
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            native = self.load(db)
            native.mode = self.mode
            try:
                result = native.patch(destination, revision, changes, marker)
            except Exception as caught:
                error = caught
                result = None
            self.save(db, native)
        # Deliberately outside transaction: process death here retains the effect.
        if error is None or self.mode == 'drop_after':
            self.after_effect()
        if error is not None:
            raise error
        return result


class Bridge:
    def __init__(self, store, directory, *, connector_class, fake_class, checkpoint_class,
                 digest, atomic_json, mode='ok', after_effect=lambda: None, before_patch=lambda: None):
        self.store, self.directory = store, Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.checkpoint_class, self.digest, self.atomic_json = checkpoint_class, digest, atomic_json
        self.connector_class, self.fake_class = connector_class, fake_class
        self.mode, self.after_effect, self.before_patch = mode, after_effect, before_patch

    def disk(self, **kwargs):
        return DiskFake(self.directory / 'destination.sqlite', self.fake_class,
                        mode=self.mode, after_effect=self.after_effect, before_patch=self.before_patch, **kwargs)

    def connector(self, disk):
        return self.connector_class(self.directory / 'notion.sqlite', self.store.owner,
                                    disk.transport, self.store.export_report)

    def plan(self):
        return json.loads((self.directory / 'plan.json').read_text())

    def prepare(self, batch_id, run_id, destination, properties, mapping, changes):
        if (self.directory / 'plan.json').exists():
            raise ValueError('Existing plan preserved; use saved proposal/resume')
        disk = self.disk(destination=destination, properties=properties)
        connector = self.connector(disk)
        op = connector.propose(run_id, destination, mapping, changes)
        env = json.loads(op['envelope'])
        plan = {'schema': 'S03-S09-local-bridge/v1', 'batch_id': batch_id,
                'source_owner': self.store.owner, 'source_run_id': run_id,
                'notion_operation_id': op['id'], 'envelope_hash': self.digest(env),
                'source_hash': env['source_hash'], 'destination': destination}
        self.atomic_json(self.directory / 'plan.json', plan)
        return op

    def validate_plan(self, connector):
        plan = self.plan()
        if plan['schema'] != 'S03-S09-local-bridge/v1' or plan['source_owner'] != self.store.owner:
            raise ValueError('Bridge owner/schema mismatch')
        op = connector.get(plan['notion_operation_id'])
        env = json.loads(op['envelope'])
        if (self.digest(env) != plan['envelope_hash'] or env['run_id'] != plan['source_run_id']
                or env['source_hash'] != plan['source_hash'] or env['destination'] != plan['destination']):
            raise ValueError('Saved exact proposal binding mismatch')
        return plan, op, env

    def approve_fixture(self, exact_hash, reviewer):
        connector = self.connector(self.disk())
        plan, op, env = self.validate_plan(connector)
        if exact_hash != plan['envelope_hash']:
            raise ValueError('Fixture approval differs from held exact envelope')
        connector.approve(op['id'], exact_hash, reviewer)
        return connector.get(op['id'])

    def execute(self, *, resume=False, hook=lambda phase: None):
        disk = self.disk()
        connector = self.connector(disk)
        plan, op, env = self.validate_plan(connector)
        if op['approval'] != plan['envelope_hash'] or not op['reviewer']:
            raise ValueError('Separate exact fixture approval required')
        # Source acceptance/evidence freshness checked even on explicit resume;
        # resolving a past effect is distinct from accepting the now-stale source.
        if connector._source(env['run_id']) != env['source_hash']:
            raise ValueError('Current accepted Store report differs from held proposal')
        manifest = [dict(kind='S09-fixture-approved', notion_operation_id=op['id'],
                         envelope_hash=plan['envelope_hash'], source_run_id=env['run_id'],
                         source_hash=env['source_hash'])]
        target = 'offline-bridge:' + self.digest(plan)
        with self.checkpoint_class(self.directory / 'checkpoint') as journal:
            journal.initialize(plan['batch_id'], target, manifest, resume=resume)
            row = dict(journal.db.execute('SELECT * FROM operations').fetchone())
            transport = JournalTransport(target, connector, disk, plan, row, self.digest)
            # Completed S03 journals skip transport callbacks: still validate the
            # actual destination now, so post-effect human edits cannot be hidden.
            if row['state'] == 'complete':
                transport.confirmed(row['id'])
            return journal.run(transport, hook=hook)


class JournalTransport:
    def __init__(self, target, connector, disk, plan, row, digest):
        self.target, self.connector, self.disk, self.plan = target, connector, disk, plan
        self.row, self.digest = row, digest

    def confirmed(self, operation_id):
        if operation_id != self.row['id']:
            raise ValueError('S03 operation ID mismatch')
        op = self.connector.reconcile(self.plan['notion_operation_id'])
        env = json.loads(op['envelope'])
        snapshot = self.disk.read(env['destination'])
        marker = {'operation_id': op['id'], 'content_hash': self.plan['envelope_hash']}
        if (op['state'] != 'applied' or snapshot['marker'] != marker
                or any(snapshot['properties'].get(k) != v for k, v in env['changes'].items())):
            raise ValueError('Ambiguous/conflicting destination; resume refuses dispatch')
        return {'status': 'applied', 'operation_id': operation_id,
                'payload_hash': self.row['payload_hash'],
                'result': {'notion_operation_id': op['id'], 'envelope_hash': self.plan['envelope_hash'],
                           'destination': snapshot}}

    def dispatch(self, operation_id, payload, payload_hash):
        if (operation_id != self.row['id'] or payload != json.loads(self.row['payload'])
                or payload_hash != self.row['payload_hash']):
            raise ValueError('S03 stable payload differs from durable intent')
        self.connector.dispatch(self.plan['notion_operation_id'])
        # A lost reply remains unknown in this invocation. Only explicit resume
        # may read back/reconcile it; no hidden second dispatch.
        if self.connector.get(self.plan['notion_operation_id'])['state'] != 'applied':
            raise ValueError('Destination outcome unresolved; use explicit resume')
        return self.confirmed(operation_id)

    def readback(self, operation_id):
        return self.confirmed(operation_id)
