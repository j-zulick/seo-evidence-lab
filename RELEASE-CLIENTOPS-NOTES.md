# Iteration 2 offline operations foundation

Adds scoped owner/operator/viewer memberships, per-operation revocation checks, manual-only job admission and explicit start, atomic local budget reservations and active-job limits, request idempotency, settlement accounting, scoped export/status, and recovery refusal for unsettled or inconsistent ledgers.

Includes a runnable synthetic two-workspace recovery rehearsal and positive/denied/concurrent-operation regression tests. Includes an HTTP adapter using existing signed-cookie/session authentication and a workspace Store executor for real local saved-query imports, supplied-HTML audits and report exports. It is a private local operations foundation; it does not claim production tenancy, real account authentication or actual client acceptance. See docs/CLIENT-OPERATIONS-PREP.md.
