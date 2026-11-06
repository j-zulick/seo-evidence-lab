"""Private, local repository for reviewed K00 publication graphs."""
import base64
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from pydantic import TypeAdapter

from .archive import ReleaseArchive
from .contracts import KnowledgeRelease, ReviewDecision, SourceSnapshot, canonical_bytes, record_bytes, sha256
from .policy import ReleaseRecord, validate_release

_RECORD = TypeAdapter(ReleaseRecord)
_VERSION = 1
_SCHEMA = (
    """CREATE TABLE objects (
        kind TEXT NOT NULL, id TEXT NOT NULL, content BLOB NOT NULL, PRIMARY KEY (kind, id)
    )""",
    """CREATE TABLE releases (
        id TEXT PRIMARY KEY NOT NULL, previous TEXT REFERENCES releases(id),
        kind TEXT NOT NULL DEFAULT 'release' CHECK (kind = 'release'),
        FOREIGN KEY (kind, id) REFERENCES objects(kind, id) DEFERRABLE INITIALLY DEFERRED
    )""",
    """CREATE TABLE members (
        release_id TEXT NOT NULL REFERENCES releases(id), id TEXT NOT NULL, digest TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'record' CHECK (kind = 'record'),
        PRIMARY KEY (release_id, id), FOREIGN KEY (kind, digest) REFERENCES objects(kind, id)
    )""",
    """CREATE TABLE evidence (
        release_id TEXT NOT NULL REFERENCES releases(id), digest TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'blob' CHECK (kind = 'blob'),
        PRIMARY KEY (release_id, digest), FOREIGN KEY (kind, digest) REFERENCES objects(kind, id)
    )""",
    """CREATE TABLE review_ids (
        id TEXT PRIMARY KEY NOT NULL, digest TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'record' CHECK (kind = 'record'),
        FOREIGN KEY (kind, digest) REFERENCES objects(kind, id)
    )""",
    """CREATE TABLE selections (
        seq INTEGER PRIMARY KEY, release_id TEXT NOT NULL REFERENCES releases(id)
    )""",
)


class KnowledgeRepository(ReleaseArchive):
    """Keep the database under coordinator-owned private application state.

    The trusted caller supplies authorize(operation, release, records). It must
    enforce the K00 publisher, provenance and exact review attestations for
    publication/import, and publisher authorization for rollback.
    """

    def __init__(self, path, *, authorize=None, read_only=False):
        self.path = Path(path).resolve()
        self._authorize = authorize
        self._read_only = read_only
        if not read_only:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
        with self._transaction(write=not read_only) as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            expected = {'objects', 'releases', 'members', 'review_ids', 'selections', 'evidence'}
            if version == _VERSION and tables == expected:
                return
            if read_only or version != 0 or tables not in (set(), {'objects'}):
                raise ValueError('Unsupported knowledge repository schema')
            if tables:
                columns = [(row[1], row[5]) for row in db.execute('PRAGMA table_info(objects)')]
                if columns != [('kind', 1), ('id', 2), ('content', 0)]:
                    raise ValueError('Unrecognized K00 archive schema')
            for statement in _SCHEMA[bool(tables):]:
                db.execute(statement)
            for table in expected:
                for operation in ('UPDATE', 'DELETE'):
                    db.execute(f'''CREATE TRIGGER k06_{table}_{operation}
                        BEFORE {operation} ON {table} BEGIN
                        SELECT RAISE(ABORT, 'Knowledge history is append-only'); END''')
            if tables:
                # K00 has no current selection. Preserve that on migration.
                releases = [self._decode_release(key, wire) for key, wire in db.execute(
                    "SELECT id, content FROM objects WHERE kind='release'")]
                for release in sorted(releases, key=lambda item: item.published_at):
                    records = self._records(db, release)
                    self._store(db, release, records, lambda digest: self._blob(db, digest))
            db.execute('''CREATE TRIGGER k06_catalog_release BEFORE INSERT ON objects
                WHEN NEW.kind = 'release' AND NOT EXISTS (SELECT 1 FROM releases WHERE id=NEW.id)
                BEGIN SELECT RAISE(ABORT, 'Use the K06 publisher'); END''')
            db.execute(f'PRAGMA user_version={_VERSION}')

    def _connect(self):
        if self._read_only:
            db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)
        else:
            db = sqlite3.connect(self.path)
        try:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA recursive_triggers=ON')
            db.execute('PRAGMA synchronous=FULL')
        except BaseException:
            db.close()
            raise
        return db

    @contextmanager
    def _transaction(self, *, write=False):
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield db

    @staticmethod
    def _decode_release(key, wire):
        release = KnowledgeRelease.model_validate_json(wire)
        if release.id != key or record_bytes(release) != wire:
            raise ValueError('Archived manifest mismatch')
        return release

    def _release(self, db, key):
        if key is None:
            row = db.execute('SELECT release_id FROM selections ORDER BY seq DESC LIMIT 1').fetchone()
            if row is None:
                raise KeyError('No current knowledge release')
            key = row[0]
        if db.execute('SELECT 1 FROM releases WHERE id=?', (key,)).fetchone() is None:
            raise KeyError(key)
        return self._decode_release(key, self._read(db, 'release', key))

    def _records(self, db, release):
        records = []
        for pin in release.members:
            wire = self._read(db, 'record', pin.sha256)
            record = _RECORD.validate_json(wire)
            if record.id != pin.id or sha256(wire) != pin.sha256 or record_bytes(record) != wire:
                raise ValueError('Archived record mismatch')
            records.append(record)
        return tuple(records)

    def _blob(self, db, digest):
        data = self._read(db, 'blob', digest)
        if sha256(data) != digest:
            raise ValueError('Archived evidence mismatch')
        return data

    def _allowed(self, operation, release, records):
        if self._read_only or self._authorize is None or self._authorize(operation, release, records) is not True:
            raise PermissionError(f'Knowledge {operation} requires publisher authorization')

    def _store(self, db, release, records, read_blob):
        blobs = {}

        def capture(digest):
            if digest not in blobs:
                data = read_blob(digest)
                if not isinstance(data, bytes):
                    raise ValueError('Evidence must be bytes')
                blobs[digest] = data
            return blobs[digest]

        validate_release(release, records, capture)
        if release.previous_release_id:
            previous = self._release(db, release.previous_release_id)
            if previous.published_at >= release.published_at:
                raise ValueError('Predecessor must predate publication')
        if db.execute('SELECT 1 FROM releases WHERE id=?', (release.id,)).fetchone():
            self._graph(db, release.id)
            return False
        for digest, data in blobs.items():
            self._insert(db, 'blob', digest, data)
        for record in records:
            wire = record_bytes(record)
            digest = sha256(wire)
            self._insert(db, 'record', digest, wire)
            if isinstance(record, ReviewDecision):
                db.execute('INSERT INTO review_ids(id,digest) VALUES (?,?) ON CONFLICT DO NOTHING',
                           (record.id, digest))
                if db.execute('SELECT digest FROM review_ids WHERE id=?', (record.id,)).fetchone()[0] != digest:
                    raise ValueError('Review ID already identifies a different decision')
        db.execute('INSERT INTO releases(id,previous) VALUES (?,?)',
                   (release.id, release.previous_release_id))
        self._insert(db, 'release', release.id, record_bytes(release))
        db.executemany('INSERT INTO evidence(release_id,digest) VALUES (?,?)',
                       ((release.id, digest) for digest in blobs))
        db.executemany('INSERT INTO members(release_id,id,digest) VALUES (?,?,?)',
                       ((release.id, pin.id, pin.sha256) for pin in release.members))
        return True

    def publish(self, release, records, read_blob):
        release = KnowledgeRelease.model_validate(release)
        records = tuple(_RECORD.validate_python(record) for record in records)
        self._allowed('publish', release, records)
        with self._transaction(write=True) as db:
            # Once a current release exists, new publications extend it.
            if not db.execute('SELECT 1 FROM releases WHERE id=?', (release.id,)).fetchone():
                current = db.execute('SELECT release_id FROM selections ORDER BY seq DESC LIMIT 1').fetchone()
                if current and current[0] != release.previous_release_id:
                    raise ValueError('Publication predecessor is no longer current')
            if self._store(db, release, records, read_blob):
                db.execute('INSERT INTO selections(release_id) VALUES (?)', (release.id,))

    def get_release(self, release_id=None):
        with self._transaction() as db:
            return self._release(db, release_id)

    def _graph(self, db, release_id):
        release = self._release(db, release_id)
        records = self._records(db, release)
        pins = tuple(db.execute('SELECT id,digest FROM members WHERE release_id=? ORDER BY id',
                                (release.id,)))
        if pins != tuple((pin.id, pin.sha256) for pin in release.members):
            raise ValueError('Release member index mismatch')
        expected_blobs = {digest for record in records
                          if isinstance(record, SourceSnapshot) and record.availability == 'captured'
                          for digest in (record.raw_sha256, record.text_sha256)}
        indexed_blobs = {row[0] for row in db.execute(
            'SELECT digest FROM evidence WHERE release_id=?', (release.id,))}
        if expected_blobs != indexed_blobs:
            raise ValueError('Release evidence index mismatch')
        validate_release(release, records, lambda digest: self._blob(db, digest))
        return release, records

    def graph(self, release_id=None):
        """Resolve one verified release and its closed evidence graph."""
        with self._transaction() as db:
            return self._graph(db, release_id)

    def lookup(self, record_id, *, release_id=None):
        _, records = self.graph(release_id)
        for record in records:
            if record.id == record_id:
                return record
        raise KeyError(record_id)

    def rollback(self, release_id, *, expected_current):
        with self._transaction(write=True) as db:
            current = self._release(db, None)
            if current.id != expected_current:
                raise ValueError('Current release changed')
            release, records = self._graph(db, release_id)
            self._allowed('rollback', release, records)
            if release.published_at >= current.published_at:
                raise ValueError('Rollback requires an earlier release')
            db.execute('INSERT INTO selections(release_id) VALUES (?)', (release.id,))

    def export(self):
        """Return portable v1 bytes for all verified releases and selections."""
        with self._transaction() as db:
            releases = []
            objects = {}
            for (key,) in db.execute('SELECT id FROM releases ORDER BY id'):
                release, records = self._graph(db, key)
                releases.append(release.model_dump(mode='json'))
                for record in records:
                    if isinstance(record, SourceSnapshot) and record.availability == 'captured':
                        for digest in (record.raw_sha256, record.text_sha256):
                            objects[digest] = self._blob(db, digest)
            records = {digest: json.loads(self._read(db, 'record', digest))
                       for (digest,) in db.execute('SELECT DISTINCT digest FROM members')}
            selections = [row[0] for row in db.execute('SELECT release_id FROM selections ORDER BY seq')]
            return canonical_bytes(dict(version=1, releases=releases, records=records,
                                        blobs={k: base64.b64encode(v).decode('ascii') for k, v in objects.items()},
                                        selections=selections))

    def import_bundle(self, data):
        """Verify and restore an export into an empty repository atomically."""
        if not isinstance(data, bytes):
            raise ValueError('Knowledge export must be bytes')
        bundle = json.loads(data)
        if (not isinstance(bundle, dict)
                or set(bundle) != {'version', 'releases', 'records', 'blobs', 'selections'}
                or type(bundle['version']) is not int or bundle['version'] != 1
                or any(not isinstance(bundle[k], list) for k in ('releases', 'selections'))
                or any(not isinstance(bundle[k], dict) for k in ('records', 'blobs'))
                or canonical_bytes(bundle) != data):
            raise ValueError('Invalid canonical knowledge export')
        releases = [KnowledgeRelease.model_validate(item) for item in bundle['releases']]
        records = {key: _RECORD.validate_python(item) for key, item in bundle['records'].items()}
        blobs = {key: base64.b64decode(item, validate=True) for key, item in bundle['blobs'].items()
                 if isinstance(item, str)}
        expected_records = {pin.sha256 for release in releases for pin in release.members}
        expected_blobs = {digest for record in records.values()
                          if isinstance(record, SourceSnapshot) and record.availability == 'captured'
                          for digest in (record.raw_sha256, record.text_sha256)}
        ids = {release.id for release in releases}
        if (len(ids) != len(releases) or set(records) != expected_records
                or set(blobs) != expected_blobs or set(blobs) != set(bundle['blobs'])
                or any(canonical_bytes(wire) != record_bytes(release)
                       for wire, release in zip(bundle['releases'], releases))
                or any(canonical_bytes(bundle['records'][key]) != record_bytes(record)
                       for key, record in records.items())
                or any(base64.b64encode(blob).decode('ascii') != bundle['blobs'][key]
                       for key, blob in blobs.items())
                or any(not isinstance(key, str) or key not in ids for key in bundle['selections'])):
            raise ValueError('Invalid export graph')
        with self._transaction(write=True) as db:
            if db.execute('SELECT 1 FROM objects LIMIT 1').fetchone():
                raise ValueError('Restore target must be empty')
            for release in sorted(releases, key=lambda item: item.published_at):
                members = tuple(records[pin.sha256] for pin in release.members)
                self._allowed('import', release, members)
                self._store(db, release, members, blobs.__getitem__)
            db.executemany('INSERT INTO selections(release_id) VALUES (?)',
                           ((key,) for key in bundle['selections']))

    def backup(self, destination):
        """Write a consistent SQLite backup to a new private file."""
        destination = Path(destination)
        fd, name = tempfile.mkstemp(prefix='.knowledge-backup-', dir=destination.parent)
        os.close(fd)
        staged = Path(name)
        try:
            with closing(self._connect()) as source, closing(sqlite3.connect(staged)) as target:
                source.backup(target)
                if (target.execute('PRAGMA integrity_check').fetchall() != [('ok',)]
                        or target.execute('PRAGMA foreign_key_check').fetchone()):
                    raise ValueError('Backup integrity check failed')
            os.link(staged, destination)
        finally:
            staged.unlink()
