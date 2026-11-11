# v0.3 local release candidate

This candidate adds an opt-in local backend and MCP path. The existing browser
walkthrough retains its v0.1 heading example and six-query baseline. No live
collection, provider calls, automatic guidance approvals or hosting is enabled.

## Reproducible replay

`seo_qa.competitor.audit` and `compare` accept the existing four-factor input
contract. `scoring: content/v1` adds two bounded rules:

- `guidance: {"rule": "required_phrase", "phrase": "Evidence trail"}` checks
  a literal, case-insensitive substring in supplied Markdown. Empty, missing,
  oversized phrases and unsupported rule versions remain unresolved.
- `guidance: {"rule": "descriptive_link"}` requires one HTTP(S) Markdown text
  link with a label other than `here`, `click here`, `read more`, `more`, or
  `link`. Images do not qualify. This is an explicit heuristic; it does not
  inspect the destination, understand link meaning or assess accessibility.

Both preserve old article/guidance/scoring/evidence pins. Missing evidence stays
unmeasured. CLI replay and compare work with exported audit JSON as before.
These criteria explain findings, with no ranking or citation causality claim.

## Retrieval benchmark

Run `.venv/bin/python -m seo_qa.release_benchmark`. The opt-in retrieval engine
adds a small fixed domain alias map while preserving the original FTS5 baseline.
The original six queries improve from 5/6 to 6/6; 12 additional authored queries
improve from 10/12 to 12/12. Labels and examples are synthetic and agent-authored;
the vocabulary overlaps the alias design. This is regression evidence, not an
independent generalization, semantic truth or measured client-quality result.

Every returned passage includes its unchanged source, version and content hash.
Contradictory passages are retained, and missing queries remain empty.

## Select a local dataset

Use an explicitly chosen JSON file with exactly these top-level keys:

```json
{"schema":"seo-evidence-dataset/v1","label":"My selected local evidence","audits":[],"passages":[]}
```

Each audit must be the unmodified output of `competitor.audit(inputs)`. Each
passage has exactly `id`, `source`, `version`, `text`, all nonempty strings.
Source pointers are retained as data and never fetched. Only select files the
connected MCP client may read: source passages and complete selected audits are
returned to it. There is no automatic selection from private application data.

Compute the expected content identity using `competitor.digest(payload)`; this
is a canonical JSON content hash, not a hash of the file's whitespace bytes.
Set `SEO_QA_DATASET` to the selected file and `SEO_QA_DATASET_SHA256` to that hash,
then launch `.venv/bin/python -m seo_qa.selected_dataset_mcp` over stdio.

An included synthetic file and its canonical content hash provide a ready
startup example:

```sh
export SEO_QA_DATASET="$PWD/examples/v03-selected-dataset.json"
export SEO_QA_DATASET_SHA256="$(cat examples/v03-selected-dataset.sha256)"
.venv/bin/python -m seo_qa.selected_dataset_mcp
```

The new server exposes four read-only tools: manifest, exact finding, audit
comparison and passage search. It loads the file once at startup, rejects
modified hashes or audit replays, caps the file at 2 MB, and accepts no tool
argument for filesystem paths or network URLs. Restart explicitly to select a
new version. The existing three-tool fixture server remains compatible.

## Later publication

1. Compose this isolated candidate with the v0.2 bridge once its actual contract
   is available. Its current four-factor replay contract is backward compatible;
   this candidate does not pretend to include unimplemented upstream features.
2. Run `./setup/fixture_ci.sh`, `.venv/bin/python -m pytest -q`, and the benchmark
   on the exact composed commit. Retain the selected dataset identity.
3. Use the existing release owner to publish the tested source/tag and retain
   actual remote CI evidence. No remote v0.3 tag is created by this preparation.
4. Keep application hosting and live providers paused until their actual rollout
   scope and resource capacity are available. Independent source/content review
   and human pilot acceptance remain unmeasured.

The release source ZIP excludes generated state, local environments, credentials,
and this workspace's private historical receipts.
