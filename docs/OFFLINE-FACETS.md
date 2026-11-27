# Offline provenance facets — G4

Goal: add source and content-version filters to local passage retrieval while preserving evidence identity and honest coverage limits.

Execution prompt: Read AGENTS.md, README.md and LOCAL-SETUP.txt. Work only in offline fixture retrieval. Preserve the existing search API and frozen six-query benchmark. Add OR-within-facet / AND-across-facets filtering, disjunctive facet counts over every query match before pagination, stable bounded paging and source/version/passage hashes. Treat query operators and filter strings as data. Reject overlong faceted queries rather than silently omit terms. Include explicit lexical-support and indexed-corpus coverage limits. Exercise regression cases before requesting acceptance. Keep spending zero, provider/model calls offline, hosting paused and schedules disabled. On a test failure, repair and rerun affected checks; after two identical environmental failures, record the exact blocker and continue independent work. Do not merge or deploy.

## API

`seo_qa.evidence_search.faceted_search(db, query='', sources=None, versions=None, limit=3, offset=0)` returns results, exact total before pagination, paging metadata, source/version facets and limitations.

An empty query browses the indexed corpus. A nonempty query matches any literal word; punctuation-only queries return no matches. At most 512 query characters, 20 terms, 50 selected values per facet, 100 hits per page and offset 100000 are accepted. Selected values within one facet are ORed; source and version filters are ANDed. Each facet's counts ignore its own selection and preserve the other filter and query. Unknown selected values stay visible with count zero.

Counts describe indexed passages. Lexical retrieval does not prove a claim, and matching opposing passages does not resolve their disagreement. Existing search() behavior and its top-three benchmark are preserved. This adds a Python retrieval API; it does not claim a new UI or hosted integration.

## Verification and final acceptance

1. From this checkout, run `python3 -m unittest discover -s tests -p test_evidence_facets.py -v`. Expect nine passing tests, including the unchanged five-of-six benchmark.
2. With the required Python 3.12 environment bootstrapped using `./setup/bootstrap_local.sh`, run `./setup/fixture_ci.sh`. The facet regression is included in the portable suite. Expect all checks and the offline walkthrough to pass.
3. If a check fails, preserve its output, fix the failing behavior on this branch and rerun that check followed by the portable suite. An absent runtime/dependency is a blocked check rather than a product pass.
4. Review the draft change and its exact head SHA. Merging and deployment remain outside this execution.

The standalone test command uses only the standard library and SQLite FTS5. Repository-wide acceptance still requires the pinned environment.
