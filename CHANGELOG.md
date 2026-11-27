# v0.2.0

Cumulative source release. It connects saved news and research exports to a versioned review workflow, and broadens replay, retrieval and read-only MCP. Everything runs locally from saved files. Nothing collects, calls providers or deploys.

### Milestone features
- **Saved news bridge (roadmap v0.2):** imports saved news-agent exports with source URL, publication/observation time, content hash and capture provenance; keeps successive versions and failed attempts; private versioned review inbox with triage; source-bound impact proposals that stay **inactive and unapproved** until reviewed.
- **Replay, retrieval and MCP (roadmap v0.3 groundwork):** more deterministic replay criteria beyond headings; bounded lexical retrieval expansion with a larger benchmark; read-only MCP over selected, pinned local datasets with content hashes.

### Previews (saved-import only, not milestone acceptance)
- Strict saved GSC / CrUX / Ahrefs export contracts and compatible comparisons.
- Community lead retention: manual imports, expiry, sticky removal, recovery.
- Saved GEO and single-page SEO diagnostic intake with a local validation screen.
- Local client operations: workspace roles, manual job/budget controls, authenticated local execution, report exports, paired recovery.

### Fixes
- Linux ARM64 (e.g. Docker on Apple Silicon) strict setup now works: added aarch64 wheel hashes.
- Private inbox caching prevented; mobile diagnostic validation improved.

### Verification
- Local: 505 tests + 24 subtests passed on Linux aarch64 and on Linux x86_64 (Python 3.12).

### Boundaries
- Retrieval benchmark is small, synthetic and agent-labeled; held-out independent labels are still pending.
- Real corroboration and owner-review acceptance of a news discovery is pending (planned v0.2.1).
- No live Ahrefs/GSC/CrUX, Reddit, Jev or Langfuse acceptance. Observational data does not show causation.
- Multi-account identity/session and route scoping are not in this release; not for shared hosting. Hosting stays paused.

# Local successor changes
- Added an offline FTS5 passage index and fixed retrieval benchmark.
- Added local OpenTelemetry spans, injected fetch failure/retry, property tests and portable fixture CI.
- Added a local change inbox with explicit accept-for-review and defer operations.
- Added input limits and stricter replay-manifest checks. Retained heading-only replay scope.
- Packaged the local timeline walkthrough, synthetic reports, browser captures and reproducible setup.

# Iteration 1 presentation and review
- Added evidence-lab entry, coherent navigation, responsive passage cards and explicit replay trials.
- Added a six-case evaluation and retrieval scorecard with visible baseline failures and lexical miss.
- Added the selected article walkthrough and blank source-bound review template.
- Integrated Hermes-supplied community, measurement and result-identity regressions.
- Refreshed source-bound screenshots, recording and portable fixture coverage.
