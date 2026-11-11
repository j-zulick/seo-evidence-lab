"""Synthetic local operations rehearsal; no authentication or hosting claim."""
import argparse
import json
from pathlib import Path

from .client_operations import ClientOperations
from .release_recovery import export_state, restore_state


def run(destination):
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError('Use a new rehearsal directory')
    destination.mkdir(parents=True, mode=0o700)
    operations = ClientOperations(destination / 'clientops.sqlite')
    operations.create_workspace('fixture-one', authenticated_actor='fixture-owner-one', budget_units=3)
    operations.create_workspace('fixture-two', authenticated_actor='fixture-owner-two')
    operations.set_member('fixture-one', authenticated_actor='fixture-owner-one', member='fixture-viewer', role='viewer')
    job = operations.admit('fixture-one', authenticated_actor='fixture-owner-one', request_key='manual-fixture',
                           kind='local_audit', input_hash='a' * 64, reserve_units=3)
    denied = []
    for name, call in [
        ('cross_workspace_read', lambda: operations.get_job('fixture-one', job['id'], authenticated_actor='fixture-owner-two')),
        ('viewer_start', lambda: operations.start('fixture-one', job['id'], authenticated_actor='fixture-viewer')),
        ('active_budget_lowering', lambda: operations.set_budget('fixture-one', authenticated_actor='fixture-owner-one', budget_units=2)),
    ]:
        try:
            call()
        except (PermissionError, ValueError):
            denied.append(name)
        else:
            raise AssertionError('Expected denial: ' + name)
    operations.start('fixture-one', job['id'], authenticated_actor='fixture-owner-one')
    operations.finish('fixture-one', job['id'], authenticated_actor='fixture-owner-one', actual_units=2)
    expected = operations.export('fixture-one', authenticated_actor='fixture-owner-one')
    export_state({'clientops': operations.path}, {}, destination / 'bundle', writers_stopped=True)
    restore_state(destination / 'bundle', destination / 'restored')
    restored = ClientOperations(destination / 'restored/databases/clientops.sqlite')
    assert restored.export('fixture-one', authenticated_actor='fixture-owner-one') == expected
    receipt = {'schema': 'client-operations-rehearsal/v1', 'synthetic': True,
               'authenticated_real_accounts': False, 'network_calls': 0,
               'denials_verified': denied, 'local_units_spent': 2,
               'actual_spending_usd': 0, 'scoped_export_restore_equal': True,
               'hosting_changed': False, 'real_client_acceptance': 'unmeasured'}
    (destination / 'RESULT.json').write_text(json.dumps(receipt, indent=2) + '\n')
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    print(json.dumps(run(args.output), sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
