# Iteration 2 offline client operations candidate

## Existing capabilities and the gap

The baseline Store already scopes audit requirements, runs, imports and operations by owner. Production has a fail-closed single-owner boundary with per-request browser identity and durable revocation. Site workspace audience roles describe report presentation and are not authorization roles. Existing recovery preserves paired local state and excludes worker/session capabilities.

This candidate adds a SQLite control plane for multiple local workspace memberships, job admission and local unit budgets, plus a private HTTP adapter using the existing signed-cookie BrowserAuth contract and a workspace-to-Store executor for saved imports, supplied-HTML audits and report exports. It does not replace existing authentication or silently convert the current app to shared client hosting.

## Runnable verification

Use the locked Python 3.12 setup from LOCAL-SETUP.txt, then:

```
.venv/bin/python -m seo_qa.client_operations_walkthrough --output /private/operator/new-clientops-rehearsal
.venv/bin/python -m pytest -q tests/test_client_operations.py tests/test_release_recovery.py
```

The walkthrough provisions two synthetic workspaces, exercises denied cross-workspace reads and viewer writes, reserves and settles a manual job, exports and restores the database into a distinct directory, and compares normal scoped exports. It creates fixture state only, makes no network call, and never authenticates real accounts. Use an absent destination; no existing state is overwritten.

## Embedding contract

ClientOperations receives `authenticated_actor` from a trusted caller. The string is an identity assertion, not a password or token. A web adapter must derive it from a verified request principal and must not accept it directly from untrusted form/query/JSON input. Workspace creation is operator provisioning; its embedding caller must enforce creation authority. Direct SQLite/filesystem access is an operator trust boundary.

The owner provisions operator/viewer membership and adjusts the cumulative local budget. Owner membership cannot be removed or transferred through the member-update method. Owner and operator may admit/start/settle jobs; viewers may read only. Every operation checks current membership, so revocation takes effect for subsequent operations. All reads and exports constrain the workspace; a job identifier alone never grants access.

Admission accepts only manual `saved_import`, `local_audit` or `report_export` work. The caller supplies a SHA256 input identity and a stable request key. Reusing the same binding returns the same job without a second reservation; changing a bound input/kind/reservation fails. Requests never enqueue a worker or call a provider. A restored or terminal request is not automatically replayed. An explicit manual start precedes completion/failure; cancellation of an unstarted job cannot consume units.

Budgets are nonnegative integer local units, default zero. They are not currency, provider quotas, actual spend or an authorization for spending. Each workspace reserves requested units atomically with admission, enforces a configured active-job count, and settles actual local units at completion/failure/cancellation. Actual units cannot exceed reservation. Unused reserved units are released. Failed work may consume measured local units and must be accounted honestly. A budget cannot be lowered below spent plus reserved units. Budget is cumulative until explicitly adjusted, without an automatic reset. No scheduled or live provider job kind is admitted.

`status()` exposes scoped state counts and budget/spent/reserved/available local units. It does not claim provider availability, telemetry delivery or measured service cost. `export()` includes scoped job identifiers and hashes but no payload, credentials, membership table or session capabilities. Keep databases, exports and input hashes private.

## Recovery

Stop writers and settle every admitted/running client job deliberately. For interrupted work, determine actual local usage before marking failed/cancelled; do not replay or assume zero consumed units. Existing release_recovery now accepts a `clientops` database role, validates terminal job states and budget ledger consistency, and refuses export if any job is unsettled. This protects against stale reservations or implicit replay on restore.

Private plan example:

```
{"databases":{"clientops":"/private/operator/clientops.sqlite"},"evidence":{}}
```

Use the existing `seo_qa.release_recovery export --writers-stopped`, `verify`, and `restore` commands with absent destinations. This control-plane-only plan excludes workspace audit databases/evidence. For a complete selected-workspace backup use `WorkspaceExecutor.backup(workspace, verified_actor, absent_destination, writers_stopped=True)`. This owner-only operation filters the control plane to that workspace and packages its Store plus evidence. `WorkspaceExecutor.restore(source, absent_destination)` verifies both bundles, checks workspace identity/inventory and installs a new private `clientops.sqlite` plus `workspaces/{workspace}/audit.sqlite` and evidence. Recreate the executor on those new paths and compare normal readers before acceptance. Other workspace memberships and data are excluded, and active jobs still block backup. Result locators are not usable without their matching Store. Never treat a control-plane-only rehearsal as full workspace recovery. Restore preserves memberships, settled spend and terminal request bindings. Test normal scoped readers before accepting recovery. This rehearsal does not prove managed hosted recovery or live account/session isolation.

## Exact remaining implementation and launch work

The new `client_operations_web.create_app(operations, data_root, browser_service=existing_browser_auth, origin=exact_origin)` adapter derives actor identity from the existing verified signed cookie and durable session lookup on every request. It rejects identity headers/envelope fields as an authority source, enforces exact origin and session-bound CSRF on mutations, and exposes a small manual form at `/clientops/{workspace}` plus scoped JSON routes. It does not create login credentials or change the existing sign-in route. BrowserAuth currently permits its configured single owner; shared multi-account sign-in still requires a reviewed multiuser authentication/session contract. Tests use two separately configured signed-owner services with offline session backends and do not claim real hosted accounts.

`WorkspaceExecutor` maps the authorized workspace to a private local directory and an owner-scoped Store. Every actual saved-query import, supplied-HTML audit and report export passes admission, explicit start and settlement. Each attempted operation consumes one deterministic local processing unit, including failure; this is not measured provider quota or spending. Input hash plus request key binds replay. Completed operations reuse result locators; running/failed work requires deliberate reconciliation. A crash between Store writes and settlement remains an explicit manual reconciliation case. Report exports recursively exclude session/worker capability fields; authentication material is never passed to the Store. Content itself remains private supplied data.

Manual form inputs are JSON: import `{"csv":"Query,Clicks\nexample,2\n"}`, audit `{"title":"Saved page","url":"https://example.test/","html":"<h1>Saved page</h1>","expected":{"indexable":true}}`, report `{"run_id":"returned-audit-id"}`. Use a new stable request name for new work; do not change an existing binding. Hosting is not started by importing the app factory.

Before shared hosted use, extend equivalent workspace authorization to the legacy app/MCP routes or mount only this reviewed private operations adapter. Review the multi-account identity/session contract and storage policies, rather than widening the current owner check implicitly. The new adapter is functional for its saved-import/audit/export scope; legacy routes are not automatically migrated.

Owner-only durable events record successful provisioning, membership, budget and job transitions with UTC timestamps, scoped identities and local unit changes. Idempotent replay does not duplicate transition events. For hosting, add latency metrics, denied-request telemetry and an approved monitoring delivery path. Interrupted jobs remain visible via status and owner-only events; an authorized operator can inspect the private Store and settle failed/cancelled usage through `finish`, then use a new request binding only after determining whether the prior side effect completed. Add a guided hosted reconciliation experience and approve operator support responsibilities. Establish real client/workspace scope, real second-account/session isolation, backup retention, hosted storage capacity and managed recovery, followed by genuine pilot acceptance. No hosted schema, provider authentication, schedules or paid integration is activated by this candidate.

For source publication, compose the exact reviewed candidates, run their combined portable checks and actual remote CI, then tag the verified commit. Hosting remains paused; live launch needs the applicable later launch authority. This candidate itself has no remote CI or deployment evidence.

## Settlement and restore integrity

Executor jobs hold a private settlement guard until their work is terminal. A competing cancellation/failure/completion cannot release the reservation or concurrency slot while the executor is performing a Store write. The guard key never enters responses or events, and active guards block backup. For a crashed executor, the owner must stop the actual process, inspect results, then call `remove_execution_guard_after_stop(..., writers_stopped=True)` followed by measured failed/cancelled settlement. Removing the guard retains the reservation and running state; it is not a cancellation. The maintenance acknowledgment relies on the trusted operator actually stopping the process.

Paired restore validates the Store's workspace owners and resolves all retained audit/import locators through normal staged application readers before installing the destination. A hash-valid backup from another workspace and a dangling result reference are refused. Filtered SQLite control snapshots use secure deletion plus VACUUM before export to remove excluded workspace records from freed pages; filesystem/snapshot erasure remains an operator concern.
