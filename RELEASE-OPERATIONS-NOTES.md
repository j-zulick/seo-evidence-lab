# Community and operations offline candidate

Adds persisted community review leads with monotonic removal tombstones, fixed retention expiry, atomic conflict refusal, metadata minimization and a counts-only manual CLI. Adds community SQLite to private local recovery and verifies tombstones remain effective after restore.

The saved-input API is an isolated local preparation capability. Live collection, unattended expiry, genuine content review and hosted client operations remain unverified. See docs/COMMUNITY-OPERATIONS-PREP.md for runnable import/recovery steps and specific launch gates.
