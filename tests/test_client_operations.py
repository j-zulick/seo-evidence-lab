from concurrent.futures import ThreadPoolExecutor
import pytest
from seo_qa.client_operations import ClientOperations
from seo_qa.release_recovery import export_state, restore_state

HASH = 'a' * 64

@pytest.fixture
def ops(tmp_path):
    result = ClientOperations(tmp_path / 'clientops.sqlite')
    result.create_workspace('one', authenticated_actor='alice', budget_units=10, max_active=2)
    result.create_workspace('two', authenticated_actor='bob', budget_units=3)
    result.set_member('one', authenticated_actor='alice', member='operator', role='operator')
    result.set_member('one', authenticated_actor='alice', member='reader', role='viewer')
    return result

def admit(ops, request='request', **changes):
    kwargs = dict(authenticated_actor='operator', request_key=request, kind='saved_import', input_hash=HASH, reserve_units=4)
    kwargs.update(changes)
    return ops.admit('one', **kwargs)

def test_membership_and_foreign_identifiers_are_scoped(ops):
    job = admit(ops)
    assert ops.get_job('one', job['id'], authenticated_actor='reader') == job
    with pytest.raises(PermissionError):
        ops.get_job('one', job['id'], authenticated_actor='bob')
    with pytest.raises(KeyError):
        ops.get_job('two', job['id'], authenticated_actor='bob')
    assert ops.export('two', authenticated_actor='bob')['jobs'] == []
    with pytest.raises(PermissionError):
        ops.export('one', authenticated_actor='unknown')

def test_viewer_cannot_mutate_members_budget_jobs(ops):
    for call in [lambda: admit(ops, authenticated_actor='reader'),
                 lambda: ops.set_budget('one', authenticated_actor='reader', budget_units=20),
                 lambda: ops.set_member('one', authenticated_actor='reader', member='bob', role='operator')]:
        with pytest.raises(PermissionError): call()
    job = admit(ops)
    with pytest.raises(PermissionError):
        ops.start('one', job['id'], authenticated_actor='reader')
    with pytest.raises(PermissionError):
        ops.finish('one', job['id'], authenticated_actor='reader', state='cancelled')

def test_manual_start_and_settlement_idempotence(ops):
    job = admit(ops)
    with pytest.raises(ValueError, match='manual start'):
        ops.finish('one', job['id'], authenticated_actor='operator', actual_units=2)
    ops.start('one', job['id'], authenticated_actor='operator')
    done = ops.finish('one', job['id'], authenticated_actor='operator', actual_units=2)
    assert ops.finish('one', job['id'], authenticated_actor='operator', actual_units=2) == done
    with pytest.raises(ValueError):
        ops.start('one', job['id'], authenticated_actor='operator')
    snapshot = ops.export('one', authenticated_actor='alice')
    assert snapshot['workspace']['spent_units'] == 2
    assert snapshot['workspace']['reserved_units'] == 0
    assert snapshot['manual_jobs_only'] is True

def test_budget_and_concurrency_bounds_never_overcommit(ops):
    admit(ops)
    with pytest.raises(ValueError, match='budget'):
        admit(ops, 'too-costly', reserve_units=7)
    admit(ops, 'second')
    with pytest.raises(ValueError, match='active job'):
        admit(ops, 'third', reserve_units=0)
    with pytest.raises(ValueError):
        ops.set_budget('one', authenticated_actor='alice', budget_units=7)
    assert ops.export('one', authenticated_actor='alice')['workspace']['reserved_units'] == 8

def test_zero_default_budget_and_no_scheduled_or_provider_kinds(tmp_path):
    ops = ClientOperations(tmp_path / 'clientops.sqlite')
    ops.create_workspace('zero', authenticated_actor='alice')
    with pytest.raises(ValueError, match='budget'):
        ops.admit('zero', authenticated_actor='alice', request_key='r', kind='saved_import', input_hash=HASH, reserve_units=1)
    with pytest.raises(ValueError, match='offline'):
        ops.admit('zero', authenticated_actor='alice', request_key='r', kind='live_provider', input_hash=HASH)
    assert ops.admit('zero', authenticated_actor='alice', request_key='r', kind='local_audit', input_hash=HASH)['state'] == 'admitted'

def test_request_id_is_bound_and_does_not_reserve_twice(ops):
    first = admit(ops)
    assert admit(ops) == first
    with pytest.raises(ValueError, match='bound'): admit(ops, input_hash='b' * 64)
    assert ops.export('one', authenticated_actor='alice')['workspace']['reserved_units'] == 4

def test_revocation_is_checked_again_on_each_operation(ops):
    job = admit(ops)
    ops.set_member('one', authenticated_actor='alice', member='operator', role=None)
    with pytest.raises(PermissionError):
        ops.start('one', job['id'], authenticated_actor='operator')
    ops.finish('one', job['id'], authenticated_actor='alice', state='cancelled')
    with pytest.raises(ValueError):
        ops.set_member('one', authenticated_actor='alice', member='alice', role='viewer')

def test_cancellation_and_failed_work_preserve_measured_local_units(ops):
    job = admit(ops)
    ops.start('one', job['id'], authenticated_actor='operator')
    with pytest.raises(ValueError, match='exceed'):
        ops.finish('one', job['id'], authenticated_actor='operator', state='failed', actual_units=5)
    ops.finish('one', job['id'], authenticated_actor='operator', state='failed', actual_units=3)
    second = admit(ops, 'second')
    ops.finish('one', second['id'], authenticated_actor='alice', state='cancelled')
    assert ops.export('one', authenticated_actor='alice')['workspace']['spent_units'] == 3
    assert ops.export('one', authenticated_actor='alice')['workspace']['reserved_units'] == 0

def test_parallel_admission_serializes_against_limits(ops):
    def attempt(number):
        try: return admit(ops, f'r-{number}')['id']
        except ValueError: return None
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(attempt, range(6)))
    assert sum(x is not None for x in results) == 2
    assert ops.export('one', authenticated_actor='alice')['workspace']['reserved_units'] == 8

def test_recovery_refuses_unsettled_work_then_preserves_scoped_exports(ops, tmp_path):
    job = admit(ops)
    with pytest.raises(ValueError, match='Settle'):
        export_state({'clientops': ops.path}, {}, tmp_path / 'refused', writers_stopped=True)
    assert not (tmp_path / 'refused').exists()
    ops.finish('one', job['id'], authenticated_actor='alice', state='cancelled')
    expected = ops.export('one', authenticated_actor='alice')
    export_state({'clientops': ops.path}, {}, tmp_path / 'bundle', writers_stopped=True)
    restore_state(tmp_path / 'bundle', tmp_path / 'restored')
    loaded = ClientOperations(tmp_path / 'restored/databases/clientops.sqlite')
    assert loaded.export('one', authenticated_actor='alice') == expected
    with pytest.raises(PermissionError): loaded.export('one', authenticated_actor='bob')

@pytest.mark.parametrize('value', [True, -1, 1.2, 1_000_001])
def test_invalid_units_never_admit(ops, value):
    with pytest.raises(ValueError): admit(ops, reserve_units=value)
    assert ops.export('one', authenticated_actor='alice')['jobs'] == []


def test_unstarted_cancel_cannot_charge_units(ops):
    job = admit(ops)
    with pytest.raises(ValueError, match='Unstarted'):
        ops.finish('one', job['id'], authenticated_actor='alice', state='cancelled', actual_units=1)
    assert ops.export('one', authenticated_actor='alice')['workspace']['spent_units'] == 0


def test_corrupt_ledger_refuses_recovery(ops, tmp_path):
    import sqlite3
    with sqlite3.connect(ops.path) as db:
        db.execute("UPDATE workspaces SET spent_units=3 WHERE id='one'")
    with pytest.raises(ValueError, match='ledger'):
        export_state({'clientops': ops.path}, {}, tmp_path / 'denied', writers_stopped=True)
    assert not (tmp_path / 'denied').exists()


def test_rehearsal_is_offline_and_existing_destination_is_preserved(tmp_path):
    from seo_qa.client_operations_walkthrough import run
    result = run(tmp_path / 'demo')
    assert result['scoped_export_restore_equal'] is True
    assert result['authenticated_real_accounts'] is False
    assert result['network_calls'] == 0
    with pytest.raises(FileExistsError): run(tmp_path / 'demo')


def test_status_is_scoped_and_reservations_are_visible(ops):
    admit(ops)
    status = ops.status('one', authenticated_actor='reader')
    assert status['reserved_units'] == 4
    assert status['available_units'] == 6
    assert status['jobs_by_state']['admitted'] == 1
    assert ops.status('two', authenticated_actor='bob')['reserved_units'] == 0
    with pytest.raises(PermissionError): ops.status('one', authenticated_actor='bob')


def test_owner_event_history_is_scoped_idempotent_and_recovered(ops, tmp_path):
    job = admit(ops)
    expected_count = len(ops.events('one', authenticated_actor='alice'))
    assert admit(ops) == job
    assert len(ops.events('one', authenticated_actor='alice')) == expected_count
    with pytest.raises(PermissionError): ops.events('one', authenticated_actor='reader')
    with pytest.raises(PermissionError): ops.events('one', authenticated_actor='bob')
    ops.start('one', job['id'], authenticated_actor='operator')
    ops.finish('one', job['id'], authenticated_actor='operator', actual_units=2)
    before = ops.events('one', authenticated_actor='alice')
    assert before[-1]['action'] == 'job_complete'
    assert before[-1]['units'] == 2
    assert all(event['workspace'] == 'one' for event in before)
    export_state({'clientops': ops.path}, {}, tmp_path / 'events-bundle', writers_stopped=True)
    restore_state(tmp_path / 'events-bundle', tmp_path / 'events-restored')
    loaded = ClientOperations(tmp_path / 'events-restored/databases/clientops.sqlite')
    assert loaded.events('one', authenticated_actor='alice') == before
