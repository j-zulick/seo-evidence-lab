"""Atomic local persistence for immutable K00 captures and retrieval observations."""
from contextlib import contextmanager
import json
import sqlite3
import time

from ..contracts import SourceSnapshot, canonical_bytes, record_bytes, sha256
from .http import remaining

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, extractor TEXT NOT NULL, mode TEXT NOT NULL,
    raw_hash TEXT NOT NULL, text_hash TEXT NOT NULL, record BLOB NOT NULL,
    raw BLOB NOT NULL, text BLOB NOT NULL, documents BLOB NOT NULL,
    UNIQUE (source, extractor, mode, raw_hash, text_hash)
);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY, source TEXT NOT NULL, method TEXT NOT NULL, mode TEXT NOT NULL,
    snapshot TEXT REFERENCES snapshots(id), details BLOB NOT NULL, rejected_body BLOB
);
CREATE INDEX IF NOT EXISTS by_source ON observations(source, method, mode, id);
CREATE INDEX IF NOT EXISTS by_raw_hash ON snapshots(raw_hash);
CREATE INDEX IF NOT EXISTS by_text_hash ON snapshots(text_hash);
CREATE TRIGGER IF NOT EXISTS immutable_snapshots_update BEFORE UPDATE ON snapshots
BEGIN SELECT RAISE(ABORT, 'Snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_snapshots_delete BEFORE DELETE ON snapshots
BEGIN SELECT RAISE(ABORT, 'Snapshots are immutable'); END;
CREATE TABLE IF NOT EXISTS validators (
    source TEXT NOT NULL, method TEXT NOT NULL, mode TEXT NOT NULL,
    observation INTEGER NOT NULL REFERENCES observations(id), details BLOB NOT NULL,
    PRIMARY KEY (source, method, mode)
);
CREATE TABLE IF NOT EXISTS rate_limit (
    source TEXT NOT NULL, mode TEXT NOT NULL, next_at REAL NOT NULL,
    PRIMARY KEY (source, mode)
);
"""


class SnapshotStore:
    def __init__(self, path):
        self.path = path
        with self.connect() as db:
            db.executescript(_SCHEMA)

    @contextmanager
    def connect(self, timeout=5):
        db = sqlite3.connect(self.path, timeout=timeout)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA recursive_triggers=ON")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def previous(self, source, method, mode):
        with self.connect() as db:
            row = db.execute("SELECT details FROM validators WHERE source=? AND method=? AND mode=?",
                             (source, method, mode)).fetchone()
            return json.loads(row[0]) if row else None

    def take_slot(self, source, mode, interval, deadline):
        while True:
            with self.connect(timeout=remaining(deadline)) as db:
                db.execute("BEGIN IMMEDIATE")
                remaining(deadline)
                now = time.time()
                row = db.execute("SELECT next_at FROM rate_limit WHERE source=? AND mode=?",
                                 (source, mode)).fetchone()
                delay = row[0] - now if row else 0
                if delay <= 0:
                    self._defer(db, source, mode, now + interval)
                    return
            if delay >= remaining(deadline):
                raise TimeoutError()
            time.sleep(delay)

    def defer(self, source, mode, seconds):
        with self.connect() as db:
            self._defer(db, source, mode, time.time() + seconds)

    @staticmethod
    def _defer(db, source, mode, until):
        db.execute("""INSERT INTO rate_limit VALUES (?,?,?) ON CONFLICT(source,mode)
                      DO UPDATE SET next_at=max(next_at, excluded.next_at)""", (source, mode, until))

    @staticmethod
    def _snapshot(source, observed, availability, raw_hash=None, text_hash=None, extractor=None):
        fields = dict(schema_version=1, source_id=source, availability=availability,
                      raw_sha256=raw_hash, text_sha256=text_hash, extractor=extractor,
                      captured_at=observed.isoformat().replace("+00:00", "Z"))
        return SourceSnapshot(id="snap:" + sha256(canonical_bytes(fields)), **fields)

    def save(self, source, method, mode, observed, reason, attempts, capture, reuse, rejected_body):
        key = (source, method, mode)
        snapshot_id = None
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if capture is not None:
                raw, text, documents = capture
                extractor = method + "-v1"
                hashes = (source, extractor, mode, sha256(raw), sha256(text))
                row = db.execute("""SELECT id, raw, text, documents, record FROM snapshots
                    WHERE source=? AND extractor=? AND mode=? AND raw_hash=? AND text_hash=?""", hashes).fetchone()
                document_bytes = canonical_bytes(documents)
                if row:
                    snapshot_id, stored_raw, stored_text, stored_documents, wire = row
                    if (stored_raw, stored_text, stored_documents) != (raw, text, document_bytes):
                        raise sqlite3.IntegrityError("Conflicting immutable snapshot payload")
                    reason = "unchanged"
                else:
                    record = self._snapshot(source, observed, "captured", hashes[3], hashes[4], extractor)
                    snapshot_id = record.id
                    wire = record_bytes(record)
                    db.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (snapshot_id, *hashes, wire, raw, text, document_bytes))
            elif reuse is not None:
                snapshot_id = reuse["snapshot_id"]
                wire, raw, text = db.execute("SELECT record, raw, text FROM snapshots WHERE id=?",
                                             (snapshot_id,)).fetchone()
                self._checked(snapshot_id, wire, raw, text)
            else:
                missing = reason in {"http_404", "http_410", "missing_body", "missing_text"}
                wire = record_bytes(self._snapshot(source, observed, "missing" if missing else "blocked"))
            details = dict(source_id=source, method=method, mode=mode, observed_at=observed.isoformat(),
                           outcome=reason, snapshot_id=snapshot_id, snapshot=json.loads(wire), attempts=attempts,
                           rejected_body_sha256=sha256(rejected_body) if rejected_body is not None else None)
            cursor = db.execute("INSERT INTO observations(source,method,mode,snapshot,details,rejected_body) VALUES (?,?,?,?,?,?)",
                                (*key, snapshot_id, canonical_bytes(details), rejected_body))
            if snapshot_id:
                last = attempts[-1]
                validators = dict(reuse["validators"]) if reuse else {}
                for name in ("etag", "last-modified"):
                    values = last["headers"].get(name, [])
                    if len(values) == 1:
                        validators[name] = values[0]
                    elif values:
                        validators.pop(name, None)
                cache = dict(snapshot_id=snapshot_id, url=last["url"], validators=validators,
                             observation_id=cursor.lastrowid)
                if reuse is None:
                    db.execute("""INSERT INTO validators VALUES (?,?,?,?,?)
                        ON CONFLICT(source,method,mode) DO UPDATE SET
                        observation=excluded.observation, details=excluded.details""",
                               (*key, cursor.lastrowid, canonical_bytes(cache)))
                else:
                    db.execute("""UPDATE validators SET observation=?, details=?
                        WHERE source=? AND method=? AND mode=? AND observation=?""",
                               (cursor.lastrowid, canonical_bytes(cache), *key, reuse["observation_id"]))
            return dict(details, id=cursor.lastrowid)

    def health(self, source, method, mode):
        with self.connect() as db:
            rows = db.execute("""SELECT id, details FROM observations WHERE source=? AND method=?
                AND mode=? ORDER BY id DESC LIMIT 1""", (source, method, mode)).fetchone()
            success = db.execute("""SELECT id, details FROM observations WHERE source=? AND method=?
                AND mode=? AND snapshot IS NOT NULL ORDER BY id DESC LIMIT 1""",
                                 (source, method, mode)).fetchone()
        return dict(latest=dict(json.loads(rows[1]), id=rows[0]) if rows else None,
                    last_success=dict(json.loads(success[1]), id=success[0]) if success else None)

    def read(self, snapshot_id):
        """Return the K00 snapshot and its exact raw and canonical text bytes."""
        with self.connect() as db:
            row = db.execute("SELECT record, raw, text FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
        if row is None:
            raise KeyError(snapshot_id)
        return self._checked(snapshot_id, *row), row[1], row[2]

    @staticmethod
    def _checked(snapshot_id, wire, raw, text):
        record = SourceSnapshot.model_validate_json(wire)
        if record.id != snapshot_id or (sha256(raw), sha256(text)) != (record.raw_sha256, record.text_sha256):
            raise sqlite3.IntegrityError("Snapshot integrity check failed")
        return record

    def read_blob(self, digest):
        """Read captured evidence by exact K00 content digest."""
        with self.connect() as db:
            row = db.execute("SELECT raw FROM snapshots WHERE raw_hash=? LIMIT 1", (digest,)).fetchone()
            if row is None:
                row = db.execute("SELECT text FROM snapshots WHERE text_hash=? LIMIT 1", (digest,)).fetchone()
        if row is None:
            raise KeyError(digest)
        if sha256(row[0]) != digest:
            raise sqlite3.IntegrityError("Evidence digest mismatch")
        return row[0]

    def documents(self, observation_id):
        """Read extracted records as seen at a particular successful observation."""
        with self.connect() as db:
            row = db.execute("""SELECT o.details, s.documents FROM observations o
                JOIN snapshots s ON s.id=o.snapshot WHERE o.id=?""", (observation_id,)).fetchone()
        if row is None:
            raise KeyError(observation_id)
        details = json.loads(row[0])
        records = json.loads(row[1])
        if details["method"] != "feed":
            url = details["attempts"][-1]["url"]
            records[0].update(id=url, url=url)
        return [dict(record, observed_at=details["observed_at"]) for record in records]

    def observation(self, observation_id):
        """Read persisted diagnostics and any complete rejected body."""
        with self.connect() as db:
            row = db.execute("SELECT details, rejected_body FROM observations WHERE id=?",
                             (observation_id,)).fetchone()
        if row is None:
            raise KeyError(observation_id)
        return dict(json.loads(row[0]), id=observation_id), row[1]

