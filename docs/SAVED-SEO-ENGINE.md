# Saved SEO engine diagnostics adapter

`seo_qa.saved_seo_adapter` consumes retained JSON bytes from the reviewed
`seo-skills/seo-audit-skill` commit
`fac4b75f2059b6d74bb05a865fe1b0ca5de3ba66`, package 7.0.0. It supports
`AuditResult` schema 2 and scoring version 4 as defined in upstream
[`src/types.ts`](https://github.com/seo-skills/seo-audit-skill/blob/fac4b75f2059b6d74bb05a865fe1b0ca5de3ba66/src/types.ts)
and [`src/scoring.ts`](https://github.com/seo-skills/seo-audit-skill/blob/fac4b75f2059b6d74bb05a865fe1b0ca5de3ba66/src/scoring.ts).

This is an additive local Python intake seam, with no engine subprocess,
browser fetch, automatic guidance release or new UI. The upstream dashboard
and Electron application are not embedded in this tool.

## Intake

Call `admit(raw_bytes, envelope, page_text)`. Keep original raw bytes and the
exact retained page text. The envelope contains:

- `adapter_schema: saved-seo/v1` and exact `engine` identity (the module's
  `ENGINE` constant).
- `operation` with nonempty `id`, terminal `status: completed` and
  timezone-aware `captured_at`.
- `input` with requested/final URLs, `scope: single_page`, `rendering: raw` or
  `rendered`, retained `page_version` and SHA-256 of UTF-8 page text.
- `provenance.raw_sha256` of the original JSON bytes and `evidence_kind`
  explicitly `synthetic` or `saved_external_diagnostic`.

The adapter requires explicit upstream run measurement flags, scoring identity,
single-page per-page coverage, finite scores, rule IDs, outcomes and consistent
counts. Empty, cancelled, aggregated, detached or older-schema runs are rejected;
they do not become clean audits. Unknown legacy run modes need a future adapter
version. Not-measured checks remain unmeasured and outside the denominator.

Normalized findings retain a locator composed of raw hash, rule ID and page
version. Original upstream results and messages remain present. Staging cap
waivers remain visible, and imported diagnostics never constitute review,
activation, content quality certification or a measured search outcome.

## Comparison

`compare(before, after)` accepts adapter outputs and requires exact engine,
target, rendering, run configuration, coverage and evidence-class compatibility.
Each rule is compared separately: a repaired title cannot cancel a newly missing
description. Changed rule sets remain explicit additions/removals. The adapter
does not infer severity where the upstream result does not supply it.

Controlled regression fixtures in `tests/test_saved_seo_adapter.py` are authored
engine-shaped JSON, not reports produced by executing the upstream engine. Their
HTML loses a description while gaining a title, producing equal aggregate scores
and two distinct rule changes. Actual engine execution remains separately
evidenced in the workspace build receipt.

## Verification and later publication

Run `.venv/bin/python -m pytest -q tests/test_saved_seo_adapter.py` and the complete
fixture suite on the exact composed source before source publication. Real
provider data and human review stay distinct from synthetic regression evidence.
Keep live audits, model calls, schedules and hosting disabled under the current
operating scope. The isolated upstream build and dependency cache are not part of
the application source ZIP or public adapter patch.
