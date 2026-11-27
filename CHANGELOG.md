# v0.2.3

### Added
- **Facet filters on the evidence-search page:** `/qa/evidence-search` shows source and version checkboxes with match counts, using `faceted_search`. A filter with an empty query browses the corpus. Unfiltered search is unchanged, and so is the six-query benchmark.

### Changed
- CI and release workflows pin `runs-on: ubuntu-24.04` ahead of the `ubuntu-latest` move to Ubuntu 26.
- Release notes now link CHANGELOG and docs at the release tag rather than `main`.

### Verification
- Local: full suite and fixture CI pass; the search page was checked at 1280px and 390px with no horizontal overflow.

### Boundaries
- A lexical match is not evidence that a claim is true. The page covers the frozen synthetic corpus only.

# v0.2.2

### Added
- **Faceted evidence retrieval (offline):** `seo_qa.evidence_search.faceted_search` filters passages by source and content version (OR within a facet, AND across facets). It reports facet counts over every match before pagination, with stable bounded paging and unchanged source/version/passage hashes. An empty query browses the indexed corpus. The existing `search()` and its six-query benchmark (5/6) are unchanged. See docs/OFFLINE-FACETS.md. Originally PR #1, replayed onto current main.

### Fixed
- CI and release workflows move to `actions/checkout@v5` and `actions/setup-python@v6`, clearing the Node.js 20 deprecation warning.

### Verification
- Local: 564 tests + 38 subtests passed (Linux x86_64, Python 3.12).

### Boundaries
- Counts describe indexed passages; a lexical match does not prove a claim. No UI or hosted integration for facets yet.

# v0.2.1

Groundwork release: the tooling to finish v0.2 acceptance and to isolate accounts. All of it runs offline from saved files.

### Added
- **Approved guidance releases:** a reviewed news proposal can be approved into an immutable, hash-chained guidance version. `affected` replays supplied audits under it and reports which findings would change, without altering stored audits. Approval requires the reviewer's own `accept_for_review` decision on that source, and the reviewer must differ from the proposal author.
- **Multi-account isolation (Iteration 2 groundwork):** explicit `SEO_MEMBER_ACCOUNT_IDS` allowlist (unset = single owner, unchanged), per-account sessions and storage roots, explicit member root provisioning, and a production route inventory that fails CI on any unclassified route. Includes second-account isolation tests.
- **Independent labeling harness (v0.3 groundwork):** `python -m seo_qa.labeling sheet|score`, with baseline vs expansion per query and Cohen's kappa. Results are only marked independent when every sheet declares it.
- **Provider drop-folder intake (v0.4 groundwork):** `python -m seo_qa.provider_intake init|run` for real saved GSC/CrUX/Ahrefs exports. The inbox is never modified, and real and synthetic exports are never compared.

### Verification
- Local: 555 tests + 24 subtests passed (Linux x86_64, Python 3.12).

### Boundaries
- No real news discovery has been reviewed and approved yet; the owner's review is pending.
- No independent retrieval labels and no real provider exports have been processed.
- Multi-account isolation is tested offline only. The hosted project is paused; no real second account has been checked. Not for shared hosting.
- Reviewer names are declarations, not authenticated identities.

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
