"""Private, offline recovery of explicitly selected paired local state.

Stop application/worker writers before export. This is not a hosted backup API.
Session material is deliberately outside this format; restored operators sign in
again normally. Hashes detect damage, not malicious replacement of a manifest.
"""
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import tempfile

DATABASE_ROLES = frozenset({'audit', 'knowledge', 'review', 'capture', 'operations', 'rules'})
EVIDENCE_ROLES = frozenset({'audit_evidence', 'capture_evidence'})
FORMAT = 'seo-qa-local-recovery-v1'


def _safe(name):
    if not isinstance(name, str):
        raise ValueError('Unsafe artifact path')
    value = PurePosixPath(name)
    if (not isinstance(name, str) or not name or value.is_absolute() or
            '..' in value.parts or '\\' in name or str(value) != name):
        raise ValueError('Unsafe artifact path')
    for part in value.parts:
        lowered = part.lower()
        if (lowered.startswith('.') or any(word in lowered for word in
                ('session', 'credential', 'secret', 'token', 'password')) or
                lowered in {'auth', 'env'} or lowered.endswith(('.pem', '.key'))):
            raise ValueError('Session/configuration material is excluded')
    return value


def _source(path, *, check_name=True):
    path = Path(path).absolute()
    if check_name:
        _safe(path.name)
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError('Symlink source is forbidden')
    return path


def _hash(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


@contextmanager
def _staging(destination):
    destination = Path(destination).absolute()
    if any(p.is_symlink() for p in (destination, *destination.parents)):
        raise ValueError('Symlink destination is forbidden')
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Serializes these tooling invocations. The private parent is a trust boundary.
    lock = destination.parent / '.seo-recovery.lock'
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        if destination.exists():
            raise FileExistsError('Destination must not exist')
        temporary = Path(tempfile.mkdtemp(prefix='.seo-recovery-', dir=destination.parent))
        try:
            yield temporary
            if destination.exists():
                raise FileExistsError('Destination appeared during recovery')
            os.rename(temporary, destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)


def _validate_audit(stage):
    """Check referenced private objects, not just files present in an inventory."""
    audit = stage / 'databases/audit.sqlite'
    if not audit.exists():
        return
    with closing(sqlite3.connect(audit.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        if db.execute("SELECT count(*) FROM runs WHERE token IS NOT NULL OR lease IS NOT NULL OR state IN ('queued','running')").fetchone()[0]:
            raise ValueError('Recovery must exclude active worker capabilities')
        for body, expected in db.execute('SELECT body,hash FROM observations'):
            from .store import digest
            path = stage / 'evidence/audit_evidence' / (expected + '.json')
            if (len(expected) != 64 or any(c not in '0123456789abcdef' for c in expected)
                    or digest(json.loads(body)) != expected or not path.is_file()
                    or _hash(path) != expected):
                raise ValueError('Referenced audit evidence missing or changed')


def export_state(databases, evidence, destination, *, writers_stopped=False):
    """Export named DBs/evidence to a new 0700 directory; caller stops writers.

    Takes SQLite writer reservations on every selected database before snapshots.
    Maintenance is still required for external evidence and cross-store business
    operations. Missing optional components must be explicitly omitted by caller.
    """
    if writers_stopped is not True:
        raise ValueError('Stop writers and acknowledge maintenance before backup')
    if not databases or set(databases) - DATABASE_ROLES or set(evidence) - EVIDENCE_ROLES:
        raise ValueError('Unsupported recovery component')
    databases = {role: _source(path) for role, path in databases.items()}
    evidence = {role: _source(path) for role, path in evidence.items()}
    for path in databases.values():
        if not path.is_file():
            raise ValueError('Database must be an existing regular file')
    for path in evidence.values():
        if not path.is_dir():
            raise ValueError('Evidence must be an existing directory')
        if Path(destination).absolute().is_relative_to(path):
            raise ValueError('Backup must be outside evidence source')
    with _staging(destination) as stage, ExitStack() as locks:
        for path in sorted(set(databases.values())):
            connection = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=1)
            locks.callback(connection.close)
            connection.execute('BEGIN IMMEDIATE')
        for role, path in databases.items():
            target = stage / 'databases' / (role + '.sqlite')
            target.parent.mkdir(exist_ok=True)
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as original:
                with closing(sqlite3.connect(target)) as snapshot:
                    original.backup(snapshot)
                    snapshot.execute('PRAGMA journal_mode=DELETE')
                    if role == 'audit':
                        # Recovery archives cannot carry reusable worker capabilities.
                        snapshot.execute("UPDATE runs SET state='interrupted',error='Recovery requires deliberate new admission' WHERE state IN ('queued','running')")
                        snapshot.execute('UPDATE runs SET token=NULL,lease=NULL')
                        snapshot.commit()
                    if snapshot.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                        raise ValueError('Database integrity check failed')
                    if snapshot.execute('PRAGMA foreign_key_check').fetchall():
                        raise ValueError('Database foreign-key check failed')
        for role, source in evidence.items():
            target = stage / 'evidence' / role
            target.mkdir(parents=True)
            for path in sorted(source.rglob('*')):
                if path.is_symlink():
                    raise ValueError('Symlink evidence is forbidden')
                relative = str(path.relative_to(source))
                _safe(relative)
                if path.is_dir():
                    (target / relative).mkdir(parents=True, exist_ok=True)
                elif path.is_file():
                    (target / relative).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, target / relative)
                else:
                    raise ValueError('Special evidence file is forbidden')
        _validate_audit(stage)
        files = {}
        for path in stage.rglob('*'):
            if path.is_file():
                path.chmod(0o600)
                files[str(path.relative_to(stage))] = {'size': path.stat().st_size, 'sha256': _hash(path)}
        manifest = {'format': FORMAT, 'created_at': datetime.now(timezone.utc).isoformat(),
                    'maintenance_acknowledged': True,
                    'job_policy': 'inflight-interrupted-leases-excluded', 'databases': sorted(databases),
                    'evidence': sorted(evidence), 'files': files}
        manifest_path = stage / 'manifest.json'
        manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n')
        manifest_path.chmod(0o600)
    return manifest


def verify_bundle(source):
    source = _source(source, check_name=False)
    manifest_path = source / 'manifest.json'
    if manifest_path.is_symlink() or manifest_path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('Invalid manifest')
    manifest = json.loads(manifest_path.read_text())
    if (manifest.get('format') != FORMAT or manifest.get('maintenance_acknowledged') is not True or
            not manifest.get('databases') or set(manifest['databases']) - DATABASE_ROLES or
            set(manifest.get('evidence', [])) - EVIDENCE_ROLES):
        raise ValueError('Unsupported manifest')
    expected = set(manifest['files'])
    actual = set()
    for path in source.rglob('*'):
        if path.is_symlink():
            raise ValueError('Symlink bundle is forbidden')
        if path.is_file() and path != manifest_path:
            actual.add(str(path.relative_to(source)))
        elif not path.is_file() and not path.is_dir():
            raise ValueError('Special bundle file is forbidden')
    if actual != expected:
        raise ValueError('Backup inventory mismatch')
    for name, entry in manifest['files'].items():
        relative = _safe(name)
        parts = relative.parts
        if not ((len(parts) == 2 and parts[0] == 'databases' and parts[1] in
                 {role + '.sqlite' for role in manifest['databases']}) or
                (len(parts) >= 3 and parts[0] == 'evidence' and parts[1] in manifest['evidence'])):
            raise ValueError('Artifact is outside declared components')
        path = source / name
        if path.stat().st_size != entry['size'] or _hash(path) != entry['sha256']:
            raise ValueError('Backup integrity failure')
    for role in manifest['databases']:
        path = source / 'databases' / (role + '.sqlite')
        if str(path.relative_to(source)) not in expected:
            raise ValueError('Declared database missing')
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
            if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)] or db.execute('PRAGMA foreign_key_check').fetchall():
                raise ValueError('Restored database integrity failure')
    _validate_audit(source)
    return manifest


def restore_state(source, destination):
    """Verify first, copy to private staging, verify again, install new destination.

    Does not remap owners, log in, change published releases or replay jobs.
    Call normal application readers on restored paths before accepting recovery.
    """
    source = _source(source)
    manifest = verify_bundle(source)
    with _staging(destination) as stage:
        shutil.copytree(source, stage, dirs_exist_ok=True)
        for path in stage.rglob('*'):
            path.chmod(0o700 if path.is_dir() else 0o600)
        verify_bundle(stage)
    return manifest


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    export = commands.add_parser('export')
    export.add_argument('--plan', required=True, help='Private JSON databases/evidence path map')
    export.add_argument('--destination', required=True)
    export.add_argument('--writers-stopped', action='store_true')
    verify = commands.add_parser('verify')
    verify.add_argument('--source', required=True)
    restore = commands.add_parser('restore')
    restore.add_argument('--source', required=True)
    restore.add_argument('--destination', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'export':
            plan = json.loads(Path(args.plan).read_text())
            if not isinstance(plan, dict) or set(plan) != {'databases', 'evidence'}:
                raise ValueError('Expected explicit databases/evidence path maps')
            receipt = export_state(plan['databases'], plan['evidence'], args.destination,
                                   writers_stopped=args.writers_stopped)
        elif args.command == 'verify':
            receipt = verify_bundle(args.source)
        else:
            receipt = restore_state(args.source, args.destination)
    except (ValueError, OSError, sqlite3.DatabaseError, KeyError, TypeError):
        print(json.dumps({'status': 'refused', 'operation': args.command}))
        return 1
    print(json.dumps({'status': 'complete', 'operation': args.command,
                      'format': receipt['format'], 'files': len(receipt['files']),
                      'scope': 'local-private-maintenance-only'}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
