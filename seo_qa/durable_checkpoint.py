"""S03 offline checkpoint prototype. No hosted adapter or network imports.

Transport methods are bounded by the adapter/caller, not retried here. A durable
local journal cannot prove remote absence; uncertain operations fail closed.
"""
from __future__ import annotations
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from uuid import UUID, uuid5

VERSION = 's03-checkpoint-v1'


def packed(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(packed(value).encode()).hexdigest()


class Refusal(ValueError):
    pass


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(packed(value) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


class Checkpoint:
    """One journal per batch. Caller supplies run ID; resume never creates it."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = open(self.directory / 'lock', 'a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise Refusal('Another writer owns this journal') from None
        self.db = sqlite3.connect(self.directory / 'journal.sqlite', timeout=2)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS batch (
                id TEXT PRIMARY KEY, manifest TEXT NOT NULL, target TEXT NOT NULL,
                version TEXT NOT NULL, first_failure TEXT);
            CREATE TABLE IF NOT EXISTS operations (
                ordinal INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL,
                payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
                state TEXT NOT NULL, result TEXT);
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, detail TEXT NOT NULL);
        ''')

    def close(self):
        self.db.close()
        self.lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def event(self, kind, detail):
        self.db.execute('INSERT INTO events(kind,detail) VALUES (?,?)', (kind, packed(detail)))

    def initialize(self, run_id, target, manifest, *, resume=False):
        run_id = str(UUID(run_id))
        if not target or not isinstance(manifest, list) or not manifest:
            raise Refusal('Explicit target and nonempty ordered manifest required')
        encoded = packed(manifest)
        old = self.db.execute('SELECT * FROM batch').fetchone()
        if old:
            if not resume:
                raise Refusal('Existing journal requires explicit resume')
            if (old['id'], old['target'], old['manifest'], old['version']) != (run_id, target, encoded, VERSION):
                raise Refusal('Run, target, manifest or version differs from saved intent')
            return
        if resume:
            raise Refusal('No saved run; resume cannot create a batch')
        with self.db:
            self.db.execute('INSERT INTO batch VALUES (?,?,?,?,NULL)', (run_id, encoded, target, VERSION))
            for ordinal, payload in enumerate(manifest):
                identity = str(uuid5(UUID(run_id), f'{target}:{ordinal}:{digest(payload)}'))
                self.db.execute('INSERT INTO operations VALUES (?,?,?,?,?,NULL)',
                                (ordinal, identity, packed(payload), digest(payload), 'prepared'))
            self.event('prepared', {'run_id': run_id, 'count': len(manifest)})

    def failure(self, error):
        # No transport exception messages or credentials enter public evidence.
        pending = self.db.execute("SELECT id FROM operations WHERE state != 'complete' ORDER BY ordinal LIMIT 1").fetchone()
        value = packed({'type': type(error).__name__, 'reason': 'Reconcile before dispatch',
                        'operation_id': pending['id'] if pending else None})
        with self.db:
            self.db.execute('UPDATE batch SET first_failure=COALESCE(first_failure,?)', (value,))
            self.event('failure', {'type': type(error).__name__})

    @staticmethod
    def validated(row, response):
        if (not isinstance(response, dict) or response.get('status') != 'applied'
                or response.get('operation_id') != row['id']
                or response.get('payload_hash') != row['payload_hash']
                or 'result' not in response):
            raise Refusal('Outcome unresolved or identity/content mismatch; dispatch forbidden')
        packed(response['result'])
        return response['result']

    def run(self, transport, *, hook=lambda phase: None):
        batch = self.db.execute('SELECT * FROM batch').fetchone()
        if not batch:
            raise Refusal('Initialize a saved run first')
        try:
            if getattr(transport, 'target', None) != batch['target']:
                raise Refusal('Transport target differs from durable intent')
            rows = self.db.execute('SELECT * FROM operations ORDER BY ordinal').fetchall()
            for row in rows:
                if row['state'] == 'complete':
                    continue
                if row['state'] == 'prepared':
                    hook('before_dispatch')
                    with self.db:
                        self.db.execute("UPDATE operations SET state='dispatching' WHERE id=?", (row['id'],))
                        self.event('dispatch_intent', {'operation_id': row['id']})
                    hook('after_dispatch_marker')
                    response = transport.dispatch(row['id'], json.loads(row['payload']), row['payload_hash'])
                    hook('after_dispatch')
                elif row['state'] == 'dispatching':
                    response = transport.readback(row['id'])
                    hook('on_resume')
                else:
                    raise Refusal('Invalid journal lifecycle state')
                result = self.validated(row, response)
                with self.db:
                    self.db.execute("UPDATE operations SET state='complete',result=? WHERE id=?", (packed(result), row['id']))
                    self.event('confirmed', {'operation_id': row['id']})
            hook('before_receipt')
            batch = dict(self.db.execute('SELECT * FROM batch').fetchone())
            receipt = {'version': VERSION, 'run_id': batch['id'], 'target': batch['target'],
                       'manifest_hash': digest(json.loads(batch['manifest'])), 'passed': True,
                       'first_failure': json.loads(batch['first_failure']) if batch['first_failure'] else None,
                       'operations': [dict(r) for r in self.db.execute('SELECT * FROM operations ORDER BY ordinal')]}
            atomic_json(self.directory / 'receipt.json', receipt)
            return receipt
        except Exception as error:
            self.failure(error)
            raise
