"""Offline paired-state recovery; no hosted backup or production claim."""
import json
import os
from pathlib import Path
import sqlite3

import pytest

from seo_qa.fixtures import seed, fixture_inspector
from seo_qa.store import Store
from seo_qa.release_recovery import export_state, restore_state, verify_bundle, main
from test_knowledge_k00 import fixture
from test_knowledge_k10 import integrated


@pytest.fixture
def paired(tmp_path):
    original = tmp_path / 'original'
    store = Store(original / 'audit.sqlite', evidence_dir=original / 'evidence')
    requirement = seed(store)[0]
    run = store.execute_run(store.create_run(requirement['id'])['id'], fixture_inspector)
    store.review(run['id'], 'accepted')
    for name in ('knowledge', 'review', 'capture', 'operations'):
        with sqlite3.connect(original / (name + '.sqlite')) as db:
            db.execute('CREATE TABLE marker (value TEXT)')
            db.execute('INSERT INTO marker VALUES (?)', ('paired-' + name,))
    databases = {name: original / (name + '.sqlite') for name in
                 ('audit', 'knowledge', 'review', 'capture', 'operations')}
    return store, run, databases, {'audit_evidence': store.evidence_dir}


def test_paired_restore_normal_application_readback(paired, tmp_path):
    store, run, databases, evidence = paired
    expected = store.export_report(run['id'])
    bundle, restored = tmp_path / 'bundle', tmp_path / 'restored'
    receipt = export_state(databases, evidence, bundle, writers_stopped=True)
    assert receipt['databases'] == sorted(databases)
    restore_state(bundle, restored)
    loaded = Store(restored / 'databases/audit.sqlite', restored / 'evidence/audit_evidence')
    assert loaded.export_report(run['id']) == expected
    for role in ('knowledge', 'review', 'capture', 'operations'):
        with sqlite3.connect(restored / 'databases' / (role + '.sqlite')) as db:
            assert db.execute('SELECT value FROM marker').fetchone() == ('paired-' + role,)
    assert os.stat(bundle).st_mode & 0o777 == 0o700
    assert all(os.stat(path).st_mode & 0o777 == 0o600 for path in restored.rglob('*') if path.is_file())


@pytest.mark.parametrize('damage', ['corrupt', 'missing', 'extra', 'symlink', 'traversal', 'missing-db'])
def test_damage_rejected_without_destination(paired, tmp_path, damage):
    _, _, databases, evidence = paired
    bundle = tmp_path / 'bundle'
    export_state(databases, evidence, bundle, writers_stopped=True)
    target = next((bundle / 'evidence/audit_evidence').glob('*.json'))
    if damage == 'corrupt': target.write_bytes(b'broken')
    elif damage == 'missing': target.unlink()
    elif damage == 'extra': (bundle / 'extra').write_text('unlisted')
    elif damage == 'symlink':
        target.unlink()
        target.symlink_to(databases['audit'])
    else:
        path = bundle / 'manifest.json'
        manifest = json.loads(path.read_text())
        if damage == 'traversal':
            manifest['files']['../escaped'] = manifest['files'].pop('databases/audit.sqlite')
        else:
            del manifest['files']['databases/audit.sqlite']
            (bundle / 'databases/audit.sqlite').unlink()
        path.write_text(json.dumps(manifest))
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError): restore_state(bundle, destination)
    assert not destination.exists()


def test_maintenance_overwrite_and_secret_refusal(paired, tmp_path):
    _, _, databases, evidence = paired
    with pytest.raises(ValueError, match='Stop writers'):
        export_state(databases, evidence, tmp_path / 'denied')
    secret = evidence['audit_evidence'] / 'supabase-user-session.json'
    secret.write_text('never exported')
    with pytest.raises(ValueError, match='Session'):
        export_state(databases, evidence, tmp_path / 'denied', writers_stopped=True)
    assert not (tmp_path / 'denied').exists()
    secret.unlink()
    bundle = tmp_path / 'bundle'
    export_state(databases, evidence, bundle, writers_stopped=True)
    with pytest.raises(FileExistsError): export_state(databases, evidence, bundle, writers_stopped=True)
    with pytest.raises(FileExistsError): restore_state(bundle, bundle)
    assert verify_bundle(bundle)


def test_writer_reservation_refuses_active_transaction(paired, tmp_path):
    _, _, databases, evidence = paired
    with sqlite3.connect(databases['operations']) as busy:
        busy.execute('BEGIN IMMEDIATE')
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            export_state(databases, evidence, tmp_path / 'busy', writers_stopped=True)
    assert not (tmp_path / 'busy').exists()


def test_symlink_source_and_auth_component_refused(paired, tmp_path):
    _, _, databases, evidence = paired
    alias = tmp_path / 'alias.sqlite'
    alias.symlink_to(databases['audit'])
    with pytest.raises(ValueError, match='Symlink'):
        export_state({'audit': alias}, {}, tmp_path / 'denied', writers_stopped=True)
    with pytest.raises(ValueError, match='Unsupported'):
        export_state({'session': databases['audit']}, {}, tmp_path / 'denied', writers_stopped=True)


def test_actual_knowledge_pin_and_update_state_survive_paired_restore(integrated, tmp_path):
    from datetime import date
    from seo_qa.knowledge.audit_pin import KnowledgeStore
    from seo_qa.knowledge.capture import ApprovedSource, Fixtures, Response
    from seo_qa.knowledge.operations import UpdateOperations
    from seo_qa.knowledge.reconciliation import Plan, ReviewJournal
    from seo_qa.knowledge.repository import KnowledgeRepository
    from seo_qa.knowledge.retrieval import Context

    repository, release, _, _, source = integrated
    store = KnowledgeStore(tmp_path / 'audit.sqlite', repository=repository,
                           context=Context(date(2026, 11, 4)))
    requirement = seed(store)[0]
    run = store.execute_run(store.create_run(requirement['id'])['id'], fixture_inspector)
    historical = store.export_report(run['id'])
    plan = Plan(baseline_release_id=release.id, baseline=(), proposals=(), conflicts=())
    journal = ReviewJournal(tmp_path / 'review.sqlite', plan, lambda _: False)
    approved = [ApprovedSource(source, 'text', min_interval=0)]
    operations = UpdateOperations(tmp_path / 'updates', approved,
        fixtures=Fixtures({source.canonical_url: [Response(200, (), b'Pages must have titles.')]}))
    expected = operations.run('paired-recovery')
    assert expected['incomplete_jobs'] == 0
    export_state({'audit': store.path, 'knowledge': repository.path, 'review': journal.path,
                  'capture': operations.root / 'captures.sqlite', 'operations': operations.path},
                 {'audit_evidence': store.evidence_dir}, tmp_path / 'bundle', writers_stopped=True)
    restore_state(tmp_path / 'bundle', tmp_path / 'restored')
    restored = tmp_path / 'restored'
    archive = KnowledgeRepository(restored / 'databases/knowledge.sqlite', read_only=True)
    loaded = KnowledgeStore(restored / 'databases/audit.sqlite', repository=archive,
                           evidence_dir=restored / 'evidence/audit_evidence',
                           context=Context(date(2026, 11, 10)))
    assert loaded.export_report(run['id']) == historical
    assert archive.get_release().id == release.id
    assert ReviewJournal(restored / 'databases/review.sqlite', plan, lambda _: False).history() == ()
    update_directory = restored / 'resumed-updates'
    update_directory.mkdir()
    for role, filename in [('capture', 'captures.sqlite'), ('operations', 'operations.sqlite')]:
        (restored / 'databases' / (role + '.sqlite')).rename(update_directory / filename)
    no_fetch = Fixtures({})
    resumed = UpdateOperations(update_directory, approved, fixtures=no_fetch)
    assert resumed.run('paired-recovery')['review_backlog'] == expected['review_backlog']
    assert not no_fetch.requests


def test_cli_private_summary_and_refusal(paired, tmp_path, capsys):
    _, _, databases, evidence = paired
    plan = tmp_path / 'private-plan.json'
    plan.write_text(json.dumps({'databases': {k:str(v) for k,v in databases.items()},
                                'evidence': {k:str(v) for k,v in evidence.items()}}))
    bundle = tmp_path / 'bundle'
    assert main(['export', '--plan', str(plan), '--destination', str(bundle)]) == 1
    assert main(['export', '--plan', str(plan), '--destination', str(bundle), '--writers-stopped']) == 0
    assert main(['verify', '--source', str(bundle)]) == 0
    assert main(['restore', '--source', str(bundle), '--destination', str(tmp_path / 'restored')]) == 0
    output = capsys.readouterr().out
    assert str(tmp_path) not in output
    assert 'local-private-maintenance-only' in output
