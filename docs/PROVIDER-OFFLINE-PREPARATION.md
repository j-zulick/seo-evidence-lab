# Saved-provider preparation candidate

This milestone adds an offline adapter contract and command. It does not add live authentication, collection, hosted writes, or actual Jev/Langfuse results. Existing UI importers remain compatible; this stricter contract is an explicit local preparation interface.

Run with Python 3.12 in the prepared environment:

```sh
.venv/bin/python -m seo_qa.provider_contract examples/provider-export-synthetic.json
.venv/bin/python -m pytest -q tests/test_provider_contract.py
```

The envelope has `schema: seo-qa/saved-provider/v1`, `provider`, an explicit boolean `synthetic`, a timezone-bearing `exported_at`, and `payload`. Supported adapters are `gsc`, `crux`, and `ahrefs-saved-top10`. GSC and CrUX use the already supported saved shapes. The Ahrefs adapter consumes the project's exact daily top-3/top-4-to-10 saved format; it does not claim arbitrary CSV or current vendor API compatibility.

Each normalized observation binds the envelope hash, payload hash, normalization version, source scope, dimensions, reporting period, metric and unit. GSC comparisons include search type, freshness selection, aggregation and filter context. A date dimension yields a one-day observation. Missing rows and missing percentiles yield unavailable coverage, never fabricated zeroes. Ahrefs observations require exact URL scope, bounded unique dates, country, daily grouping and integer counts. Comparisons require matching identity, provenance class and schema/normalizer versions, equal-length nonoverlapping windows, and retained source hashes. A delta is an observation, not a causal claim.

Keep the original JSON privately beside each result and reproduce normalization before accepting imported data. A hash reference alone is not proof the original export exists or that a provider authenticated it. The `synthetic` field is declared provenance, not independent attestation. Do not place private exports in a public release. No fixtures here establish real provider performance.

## Later acceptance and launch

1. Integrate this candidate's additive module, regression cases and example into the selected successor; run the standard fixture suite.
2. Obtain an already authorized saved export from the intended property/account and retain its exact request and scope privately. Wrap it with `synthetic: false` only when it is genuinely real. No live calls are required for saved-data acceptance.
3. Reproduce normalization twice, inspect scope/window compatibility and link the private source hash to the chosen report. Check that unavailable coverage stays explicit.
4. For genuine Jev evaluation, obtain independently labeled cases and actual permitted judgments; the existing canned evaluation remains simulated. For connected Langfuse acceptance, record actual authorized ingestion/readback and reconciled usage. Neither is measured by this candidate.
5. Release source only after the integrated commit passes verification. Keep application hosting paused until its separate resume conditions are met. No credentials, service accounts or provider budgets are needed to run this offline candidate.

External gates: actual authorized provider exports, independent evaluation labels/judgments and connected tracing evidence. These gates do not block exercising the synthetic local contract. No production or client-readiness acceptance is claimed.

## Drop-folder intake for real saved exports (prepared, exports pending)

```sh
.venv/bin/python -m seo_qa.provider_intake init ~/private/provider-drop
# Save each export under inbox/<gsc|crux|ahrefs-saved-top10>/<name>.json with a
# <name>.meta.json sidecar: {"exported_at": "...+00:00", "synthetic": false}
.venv/bin/python -m seo_qa.provider_intake run ~/private/provider-drop
```

`run` leaves the inbox untouched, normalizes each export twice and requires
identical results, writes `normalized/<source_hash>.json`, and writes
`INTAKE-REPORT.json` with accepted files, rejections with reasons, the count of
real (non-synthetic) exports, and every comparable pair. Real and synthetic
exports are never compared. Symlinks, files over 20 MB, undeclared provenance,
timezone-less export times and extra sidecar fields are refused. Keep the drop
folder private and outside any repository.
