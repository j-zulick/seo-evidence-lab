# Evaluation and verification

These results describe the local synthetic release, not measured client outcomes or live model accuracy. The tested application baseline is `ec88b48ae5d0fc74d51bdf1227ad267c52c93fe6`.

| Measurement | Recorded result | Interpretation |
| --- | --- | --- |
| Fixture evaluation | Baseline 4/6; revised canned answers 6/6 | Three deterministic cases and three simulated Jev-style judgments test the harness, including deliberately wrong answers. |
| Retrieval benchmark | Five of six expected passages found within the benchmark's top-three criterion | The fixed paraphrase “slow user reactions” misses the intended passage. No population accuracy estimate is implied. |
| Portable fixture suite | 264 tests and 24 subtests passed locally | Regression coverage of the selected local release; not remote CI or a production security audit. |
| UI browser checks | Desktop/mobile journey, keyboard scrolling, JSON download and provenance checked | Verified in the local acceptance environment. |
| Core browser checks | Audit/review/revision/export and actual restart persistence checked | Browser fixtures do not supply independent human editorial votes. |
| MCP | Actual stdio initialization, tool listing and all three tool calls exercised | Known local fixture data, not arbitrary user datasets. |
| Packaging | Fresh extraction exercised; source files independently checked against frozen Git blobs | Release assets must be regenerated after a source or documentation change. |

## Inspect and reproduce

- [Saved experiment comparison](../examples/experiment-comparison.json)
- [Retrieval benchmark report](../examples/retrieval.json)
- [Evaluation screenshot](../examples/evaluation.png)
- [Current capability limits](../CAPABILITIES.md)

Run `./walkthrough.sh` to prepare the environment and reports. Run `./setup/fixture_ci.sh` for the selected suite. Browser acceptance uses installed Chrome and the commands in [README](../README.md). Timing changes on a new run; inspect the actual report rather than presenting a previous duration as a new measurement.

Evaluation identities include cases, saved judgments, and executable implementation hashes. Altering a canned answer changes the identity, even if a headline score remains the same. The visible failure cases are part of the demonstration.

## What remains unmeasured

Real Jev judgments, connected Langfuse service behavior, independent labels, real search improvement, client time savings, and production reliability have not been established. Owner local-MVP signoff and advisory source approvals are recorded; independent editorial judgments and empirical walkthrough observations remain separate.

The prepared GitHub workflow is manual. Its first actual remote result must be recorded after authorized publication. Reuse these historical local results with their baseline identity; do not label them as a new CI run.
