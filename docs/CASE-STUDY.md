# Explaining a changing SEO finding

An audit score can change when content is edited, guidance changes, scoring is revised, or evidence becomes available. Reviewing only the newest score hides that distinction. SEO Evidence Lab puts the passage, finding, pinned inputs, and replay trials together.

## The implemented example

A synthetic article gains a question heading. Replaying the edited article against the earlier other inputs is sufficient to change the heading finding to pass. Replaying only the guidance change preserves the earlier finding. Both the edited page and a comparison page gain synthetic search visibility, so the demonstration leaves search impact uncertain.

This is a bounded deterministic explanation. It does not infer the cause of an actual ranking change or generalize a heading rule to all content quality.

## Engineering decisions

- **Retain attempts separately from content versions.** Failed capture attempts remain visible without replacing the last successful version with an empty body.
- **Pin the inputs.** Article, guidance, scoring, and evidence availability have inspectable identities, allowing a reviewer to reproduce the finding.
- **Make evaluation failures visible.** Deliberately wrong saved judgments and a missed paraphrase remain in the scorecard.
- **Validate comparison scope.** Saved measurement request windows and response scope must agree; missing evidence remains unknown.
- **Preserve review provenance.** Software checks, owner decisions, and independent human votes remain separate records.
- **Keep the demo runnable locally.** FastAPI, Jinja2, SQLite FTS5, fixture MCP, and local tracing avoid a requirement for paid provider calls.

## Evidence of delivery

The baseline passed 264 local tests and 24 subtests, plus fresh-install, browser, export, MCP, and restart checks. The Git history records actual baseline import, feature, correction, UI, and documentation commits. [Evaluation scope](EVALUATION.md) · [Architecture](ARCHITECTURE.md) · [Demo](DEMO-WALKTHROUGH.md).

## Client and employment relevance

For a small site owner or agency, the local workflow supports reviewing supplied content and observations with an evidence report. Client time savings, ranking gains, and paid-service suitability have not been measured.

For engineering review, the implementation provides concrete discussion of data lineage, transactions, input contracts, replay, read-only tooling, evaluation design, and recovery. The work is domain-specific and inspectable without claiming an end-to-end production service.

The next milestone is the actual custom news-agent bridge: preserve a discovered report, review primary guidance, publish a versioned guidance decision, and show which findings change. [Roadmap](ROADMAP.md).
