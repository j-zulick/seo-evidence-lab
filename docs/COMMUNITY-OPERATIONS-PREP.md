# Offline community and client operations preparation

This candidate adds a manual saved-input retention store and integrates its database into existing local recovery. It does not implement live collection, multi-tenant client hosting, authentication changes, a scheduler, or provider access. No such service is activated.

## Run the saved-input path

Use the locked Python 3.12 setup in LOCAL-SETUP.txt. Keep the database and saved input outside the public source tree in a private operator directory. Inputs follow fixtures/community-news.json; each post, including removals, requires an observed_at timestamp. An explicit synthetic boolean identifies source origin, without attesting to provider authorization.

```
.venv/bin/python -m seo_qa.community_store --database /private/operator/community.sqlite --input /private/operator/saved-community.json --as-of 2026-11-11T12:00:00Z --retention-days 30
```

The CLI prints counts only. It never fetches content. The database stores normalized lead fields and synthetic origin, excluding usernames, raw post bodies and unrelated fields. Guidance remains pending genuine review. Default retention is 30 days, with an allowed range of 1–90 days; this is a conservative local implementation default, not a provider-policy determination. Use the caller's actual observation and receipt times.

Run without --input to expire records and inspect counts at the supplied actual time. Retention is enforced on reads and writes; there is no background expiry while the application is stopped. Retained content is cleared at expiry. Tombstones retain only identity, state and ordering timestamps to prevent stale exports from resurrecting content. New observations do not extend the first admitted expiration. Equal-time conflicts fail atomically. Removal wins over ordering and is sticky.

SQLite secure_delete is enabled and rollback journals are used. This clears database cells; it is not a guarantee of forensic erasure on the filesystem, snapshots or external backups. Saved input files and existing backup copies need their own access, deletion and retention policy. No usernames or copied discussion archive should enter public release artifacts.

## Rehearse backup and restore

Stop all local writers. Include community alongside every applicable existing component in a private explicit recovery plan:

```
{"databases":{"community":"/private/operator/community.sqlite"},"evidence":{}}
```

```
.venv/bin/python -m seo_qa.release_recovery export --plan /private/operator/plan.json --destination /private/operator/backup-new --writers-stopped
.venv/bin/python -m seo_qa.release_recovery verify --source /private/operator/backup-new
.venv/bin/python -m seo_qa.release_recovery restore --source /private/operator/backup-new --destination /private/operator/restore-new
.venv/bin/python -m seo_qa.community_store --database /private/operator/restore-new/databases/community.sqlite --as-of 2026-11-11T12:00:00Z
```

Destinations must be absent. Compare normal application output before/after, including removal tombstones. Existing recovery validates inventory, hashes, SQLite integrity, private permissions and excludes session/lease capabilities. Never restore into an active directory or replay interrupted jobs automatically. Remove disposable rehearsal state after verification. A community-only rehearsal proves that component, not a whole application recovery. Tests also exercise paired audit/knowledge/review/capture/operations readback.

## Later launch gates

Community collection: permitted provider access and current retention/deletion obligations, bounded subreddit/time/comment scope, authenticated access where required, real input review, removal ingestion and backup retention procedure. A real observation-to-corroborated-guidance example remains unmeasured. Do not infer approval from synthetic flags or source links.

Iteration 2 client operations: real client/workspace scope, actual second-account isolation and session tests, selected hosting capacity, scoped ingestion/jobs/budgets, monitoring, managed recovery and operator ownership, real pilot results. Existing local permissions/recovery fixtures do not establish hosted tenancy or client acceptance. Keep hosting paused and provider/model calls disabled until those gates and applicable launch authority are met.

## Publication sequence

Use the candidate manifest and checksums to select exact bytes. Reconcile other release candidates before composition. Run the portable suite plus the new retention suite on the composed commit; run the manual GitHub workflow on that exact commit only when source publication is selected. Create a tag after remote verification. This candidate has no remote CI or deployment evidence of its own. Its source release can be reviewed independently of the live collection and client-service gates.
