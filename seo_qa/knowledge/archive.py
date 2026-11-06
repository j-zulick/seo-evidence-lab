"""Local append-only SQLite archive for published manifests, records and evidence."""
from contextlib import closing
import sqlite3
from pathlib import Path
from typing import Iterable

from .contracts import KnowledgeRelease, record_bytes, sha256
from .policy import ReleaseRecord, validate_release


class ReleaseArchive:
    """Use a private coordinator-owned file, with one shared archive per environment."""

    def __init__(self, path: Path):
        self.path = path
        with closing(self._connect()) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS objects (
                kind TEXT NOT NULL, id TEXT NOT NULL, content BLOB NOT NULL,
                PRIMARY KEY (kind, id)
            )''')
            for operation in ('UPDATE', 'DELETE'):
                db.execute(f'''CREATE TRIGGER IF NOT EXISTS forbid_{operation.lower()}
                    BEFORE {operation} ON objects BEGIN
                    SELECT RAISE(ABORT, 'Published archive is append-only'); END''')

    def _connect(self):
        db = sqlite3.connect(self.path)
        db.execute('PRAGMA recursive_triggers = ON')
        return db

    @staticmethod
    def _read(db, kind, key):
        row = db.execute('SELECT content FROM objects WHERE kind=? AND id=?',
                         (kind, key)).fetchone()
        if row is None:
            raise KeyError(key)
        return row[0]

    def _insert(self, db, kind, key, content):
        db.execute('INSERT INTO objects VALUES (?, ?, ?) ON CONFLICT DO NOTHING',
                   (kind, key, content))
        if self._read(db, kind, key) != content:
            raise ValueError('Immutable archive collision')

    def publish(self, release: KnowledgeRelease, records: Iterable[ReleaseRecord],
                read_blob) -> None:
        records = tuple(records)
        blobs = {}

        def capture(digest):
            if digest not in blobs:
                blobs[digest] = bytes(read_blob(digest))
            return blobs[digest]

        validate_release(release, records, capture)
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if release.previous_release_id:
                previous = KnowledgeRelease.model_validate_json(
                    self._read(db, 'release', release.previous_release_id))
                if previous.published_at >= release.published_at:
                    raise ValueError('Predecessor must predate publication')
            for digest, content in blobs.items():
                self._insert(db, 'blob', digest, content)
            for record in records:
                content = record_bytes(record)
                self._insert(db, 'record', sha256(content), content)
            self._insert(db, 'release', release.id, record_bytes(release))

    def get_release(self, release_id: str) -> KnowledgeRelease:
        with closing(self._connect()) as db:
            release = KnowledgeRelease.model_validate_json(self._read(db, 'release', release_id))
        if release.id != release_id:
            raise ValueError('Archived release ID mismatch')
        return release

    def read_record(self, digest: str) -> bytes:
        return self._read_checked('record', digest)

    def read_blob(self, digest: str) -> bytes:
        return self._read_checked('blob', digest)

    def _read_checked(self, kind: str, digest: str) -> bytes:
        with closing(self._connect()) as db:
            content = self._read(db, kind, digest)
        if sha256(content) != digest:
            raise ValueError('Archived content hash mismatch')
        return content
