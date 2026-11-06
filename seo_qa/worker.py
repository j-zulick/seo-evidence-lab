"""One bounded local queue pass; never a daemon or an automatic job retry.

The Store owns claims and leases. Interrupted/error jobs remain evidence and
require a deliberate new run. Hosting this CLI is a separate deployment step.
"""
from __future__ import annotations

import argparse
import getpass
import json

from .store import Store
from .supabase_adapter import SupabaseError, UncertainWrite


def work_once(store, inspect, *, limit=1):
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError('Worker pass limit must be between one and ten')
    recovered = store.recover_expired()
    # Metadata listing avoids downloading every historical evidence object just
    # to select queued work. Each execution still verifies its own input/evidence.
    listing = getattr(store, 'list_queue_states', store.list_runs)
    queued = [run for run in listing() if run['state'] == 'queued'][:limit]
    results = []
    stopped = False
    for run in queued:
        try:
            store.execute_run(run['id'], inspect)
            state = store.get_run(run['id'])['state']
        except (Exception, KeyboardInterrupt) as error:
            # Reconcile by reading the stable run ID, never replay a mutation.
            # Failed readback is unknown, not evidence that the write failed.
            try:
                state = store.get_run(run['id'])['state']
            except Exception:
                state = 'unknown'
            results.append({'id': run['id'], 'state': state,
                            'error_type': type(error).__name__,
                            'next_action': 'read_run_before_new_work'})
            # Auth loss or transport uncertainty must not dispatch the remainder
            # of a pass. Manual sign-in/readback is required before continuation.
            if isinstance(error, KeyboardInterrupt):
                results[-1]['invocation_interrupted'] = True
            if isinstance(error, (SupabaseError, UncertainWrite, KeyboardInterrupt)) or state == 'unknown':
                stopped = True
                break
        else:
            results.append({'id': run['id'], 'state': state})
    return {'recovered_expired': recovered, 'attempted': len(results),
            'results': results, 'automatic_retry': False, 'stopped_early': stopped}


def queue_status(store, *, now=None, verify_evidence=False):
    """Private read-only snapshot. Unknown telemetry is never reported healthy.

    Evidence verification is opt-in because it downloads private objects. Run
    completion is durable job success, not proof of a live worker heartbeat.
    """
    import time
    import math
    from datetime import datetime

    now = time.time() if now is None else now
    def timestamp(value):
        try:
            if isinstance(value, str):
                value = datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
                return None
            return value
        except (ValueError, OverflowError):
            return None

    counts = {state: 0 for state in ('queued', 'running', 'completed', 'error', 'interrupted', 'unknown')}
    runs, ages, uncertain = [], [], []
    try:
        listing = getattr(store, 'list_queue_states', store.list_runs)
        rows = listing()
    except Exception as error:
        return {'status': 'unavailable', 'counts': None, 'runs': [],
                'storage': {'metadata': 'unavailable', 'error_type': type(error).__name__},
                'worker_running': 'not_observed', 'automatic_retry': False,
                'next_action': 'restore_access_then_read_status_before_new_work'}
    for row in rows:
        state = row['state'] if row['state'] in counts else 'unknown'
        counts[state] += 1
        created, lease = timestamp(row.get('created')), timestamp(row.get('lease'))
        age = max(0, now-created) if created is not None and created <= now else None
        if state == 'queued' and age is not None:
            ages.append(age)
        reason = ('expired_lease' if state == 'running' and lease is not None and lease <= now
                  else 'reconciliation_required' if state in {'interrupted', 'unknown'} else None)
        if reason:
            uncertain.append({'id': row['id'], 'reason': reason})
        runs.append({'id': row['id'], 'state': state, 'queue_age_seconds': age if state == 'queued' else None})
    storage = {'metadata': 'available', 'evidence': 'not_checked', 'failures': []}
    if verify_evidence:
        for row in rows:
            try:
                store.get_run(row['id'])
            except Exception as error:
                storage['failures'].append({'id': row['id'], 'error_type': type(error).__name__})
        storage['evidence'] = 'unavailable_or_changed' if storage['failures'] else 'verified'
    return {'status': 'attention' if uncertain or counts['error'] or storage['failures'] else 'observed',
            'counts': counts, 'runs': runs, 'worker_running': 'not_observed',
            'worker_success': {'completed_runs': counts['completed'], 'last_success_at': None,
                               'heartbeat': 'not_instrumented'},
            'queue': {'oldest_known_age_seconds': max(ages) if ages else None,
                      'age_unknown_count': counts['queued']-len(ages)},
            'uncertain_jobs': uncertain, 'storage': storage, 'automatic_retry': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Single explicit SEO audit queue pass')
    backend = parser.add_mutually_exclusive_group(required=True)
    backend.add_argument('--state', help='Explicit persistent local audit SQLite path')
    backend.add_argument('--hosted', action='store_true', help='Existing Supabase Store from explicit environment')
    parser.add_argument('--owner-login', action='store_true', help='Prompt privately for existing owner account; requires --hosted')
    parser.add_argument('--evidence-dir', help='Explicit local evidence directory; requires --state')
    parser.add_argument('--limit', type=int, default=1)
    parser.add_argument('--verify-evidence', action='store_true', help='With --status, read and verify private run evidence')
    parser.add_argument('--status', action='store_true', help='Read queue states without recovering leases or executing work')
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument('--fixture', action='store_true', help='Only built-in dummy URLs; no network')
    scope.add_argument('--allowed-host', action='append', dest='hosts', help='Authorized exact public host')
    args = parser.parse_args(argv)
    if args.evidence_dir and not args.state:
        parser.error('--evidence-dir requires --state')
    if args.verify_evidence and not args.status:
        parser.error('--verify-evidence requires --status')
    if not args.status and not (args.fixture or args.hosts):
        parser.error('Execution requires --fixture or an explicit --allowed-host')
    if args.hosted and args.fixture:
        parser.error('Hosted worker requires explicit authorized hosts, never fixture inspection')
    if args.owner_login and not args.hosted:
        parser.error('Owner sign-in requires the hosted backend')
    if args.status:
        inspect = None
    elif args.fixture:
        from .fixtures import fixture_inspector
        inspect = fixture_inspector
    else:
        from .production import build_inspector
        inspect = build_inspector(tuple(args.hosts))
    if args.hosted:
        if args.owner_login:
            from .browser_storage import BrowserStorage
            auth = BrowserStorage.from_environ()
            email = input('Existing SEO QA owner email: ').strip()
            password = getpass.getpass('Password (not saved): ')
            try:
                session = auth.sign_in(email, password)
                store = auth.create_store(session['access_token'])
            except Exception:
                print(json.dumps({'status': 'refused', 'reason': 'Owner sign-in or workspace verification failed'}))
                return 1
            finally:
                password = None
        else:
            from .production_storage import create_store
            store = create_store()
    else:
        from .knowledge.config import configured_store
        store = configured_store(args.state, evidence_dir=args.evidence_dir)
    try:
        result = queue_status(store, verify_evidence=args.verify_evidence) if args.status else work_once(store, inspect, limit=args.limit)
    finally:
        if args.hosted:
            store.api.close()
    print(json.dumps(result, sort_keys=True))
    if result.get('status') in {'unavailable', 'attention'}:
        return 1
    return 1 if result.get('stopped_early') or any(row['state'] not in {'completed'} for row in result.get('results', [])) else 0


if __name__ == '__main__':
    try:
        code = main()
    except (Exception, KeyboardInterrupt) as error:
        # No traceback/provider text in the interactive operator interface.
        print(json.dumps({'status': 'refused', 'error_type': type(error).__name__,
                          'next_action': 'check_configuration_auth_and_read_queue_status'}))
        code = 1
    raise SystemExit(code)
