# Five minute demonstration

Use this walkthrough to show an employer or prospective client how an audit change can be inspected. The examples use synthetic captures and search observations; the selected publication-review article is supplied locally.

## Prepare

Use Python 3.12 and SQLite FTS5. From the repository root, run `./walkthrough.sh`, then `./run-local.command`. Open the address printed by the launcher and select Evidence lab at `/qa/lab`. The script may download pinned dependencies; it makes no model or Ahrefs request. The protected hosted preview supports inspection of saved examples, not these interactive local workflows.

## Demonstrate

1. **Overview, about 30 seconds.** State the problem: a finding changed, but a score alone does not tell the reviewer what changed or why. Explain that the app retains the source inputs.
2. **Change analysis, about 90 seconds.** Open the original and edited passages. The edited synthetic article acquires a question heading. Show the before/after finding and the article, guidance, scoring, and evidence pins. Inspect the factor trials: replacing the article alone is sufficient for this heading example; changing guidance alone preserves the original finding.
3. **Search context, about 30 seconds.** Show that both the edited and comparison page gain synthetic visibility. Say that the app has explained the deterministic finding, while the edit's search benefit remains uncertain.
4. **Evaluation, about 60 seconds.** Show all six cases and the intentionally incorrect saved answers. Baseline 4/6 and revised canned 6/6 exercise the harness. Show the retrieval paraphrase miss and explain why the benchmark is not a real-world accuracy claim.
5. **News leads, about 30 seconds.** Follow a synthetic community report to its linked source and corroboration state. Explain that connecting the custom news agent is planned; this is the local review journey.
6. **Article walkthrough, about 60 seconds.** Open the content-review-before-publication article for a small-site owner. Show the supplied content and blank review template. In the full local app, demonstrate a supplied-content assessment, saved revision, or report export. Label any demonstration review as synthetic; do not submit it as an independent human vote.

## Close

For an employer: discuss versioned data, deterministic replay, input validation, failure handling, and the visible evaluation limitations. For a prospective client: discuss an initial review of supplied content/exports and an evidence report that makes recommended work explainable.

The next milestone is one real custom-agent export entering the review inbox and producing a source-bound finding comparison. It has not been accepted end to end. [Current scope](../CAPABILITIES.md) · [Recorded walkthrough](../examples/walkthrough.webm).
