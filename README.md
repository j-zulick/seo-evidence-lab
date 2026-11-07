# SEO Evidence Lab
Follow an emerging report to its source, inspect the page edit, and replay the finding that changed.

![Evidence lab overview](examples/overview.png)

This local Python application combines an SEO audit workspace with an inspectable synthetic change-analysis demo. The planned connection to a custom SEO/LLM-visibility news agent and research/scraper pipeline makes discovery the start of the evidence trail.

## Try it locally
Use Python 3.12 with SQLite FTS5. Run the one-command walkthrough:

```sh
./walkthrough.sh
```

It installs the pinned local environment and writes reports to `demo-output/`. Choose the interpreter with `SEO_QA_BOOTSTRAP_PYTHON` if `python3.12` has a different name.

Then start the app:

```sh
./run-local.command
```

Open the printed address and choose **Evidence lab**. The main journey starts at `/qa/lab`.

- **Change analysis** puts the original passage, edit, finding and counterfactual trials together.
- **Evaluation** exposes the six-case results and the failed retrieval paraphrase.
- **News leads** separates an emerging report from its primary-source review.
- **Article walkthrough** uses the included publication-review article for a small-site owner. The editorial template remains blank.

[Recorded walkthrough](examples/walkthrough.webm) · [Evaluation screen](examples/evaluation.png) · [Current experiment report](examples/experiment-comparison.json) · [Retrieval benchmark](examples/retrieval.json)

## Inspect a claim
The edited synthetic page acquires a question heading. Replacing only the article makes the heading criterion pass. Updating only the guidance preserves the original finding. Both the edited page and comparison page gain synthetic top-ten keywords, so the edit’s search benefit remains uncertain.

![Passage and finding comparison](examples/timeline.png)

The fixture replay covers a heading criterion. The broader five-scope audit and the separate Bot-worthiness assessment are available through the audit workspace. Old audits and edited-content assessments retain distinct identities.

## Technology and implemented scope
| Area | Local implementation |
| --- | --- |
| App | Python 3.12, FastAPI, Jinja2, responsive server-rendered pages |
| Evidence | SQLite attempt/version storage, content hashes, immutable replay inputs |
| Ingestion | Saved Firecrawl-shaped captures, Search Console and CrUX JSON imports, bounded synthetic subreddit observations |
| Search | SQLite FTS5, passage/source/version hashes, frozen six-query benchmark |
| Tools | Actual MCP stdio server with three read-only fixture tools |
| Evaluation | Three deterministic cases plus three simulated Jev-style judgments, Langfuse dataset-item exports |
| Observability | OpenTelemetry in-memory spans for comparison, import, failure, retry, diff and replay |
| Verification | pytest, Hypothesis, Playwright/Chrome, browser traces, real restart and export checks |
| Product | Five audit scopes, uploaded-site analysis, explanations, history, Bot-worthiness review/import/revision and exports |

The evaluation’s baseline is 4/6; revised canned answers reach 6/6. Those results test the harness. Real model performance and human labels remain pending. Retrieval meets five of six expectations; “slow user reactions” misses its intended passage. Every result is inspectable by case. The evaluation JSON binds the cases, saved judgments and implementation hashes.

## Reproduce verification
```sh
./setup/fixture_ci.sh
.venv/bin/python setup/iteration_one_browser.py --output local-ui-checks
.venv/bin/python setup/local_acceptance.py --output local-core-checks
```

Browser checks require installed Chrome. Optional `--record` on the UI script also requires the Playwright FFmpeg helper. Dependencies and browser artifacts remain outside source control.

The fixture CI workflow is prepared for manual execution after repository publication. Remote CI and hosted provider acceptance are pending. The source bootstrap has been exercised in a fresh extracted folder on macOS with Python 3.12.

## Architecture
```mermaid
flowchart LR
  A[Saved captures and news leads] --> B[Attempts and content versions]
  B --> C[Diff and heading replay]
  C --> D[Evidence lab]
  E[Saved search measurements] --> D
  B --> F[Manual review inbox]
  G[Versioned passages] --> H[FTS5 retrieval]
  C --> I[Read-only MCP]
  C --> J[Local evaluation and traces]
```

## Engineering examples
- Packaging refused a local app whose new imports were absent from the historical hosted allowlist.
- Independent review caught request windows that contradicted the saved observations.
- Community payloads now require explicit synthetic provenance and reject malformed/aliased source lists.
- Evaluation identity includes the simulated answers and executable source, so changing a canned answer changes the result identity.

[News/research integration design](NEWS-INTELLIGENCE.md) · [Capabilities and limits](CAPABILITIES.md) · [Release notes](RELEASE-NOTES.md) · [Contributing](CONTRIBUTING.md)

## Origins and publication
Sweepr’s documentation-drift workflow supplied the originating concept; Botworthy Index later informed the separate local quotability adaptation. [Attribution](ATTRIBUTION.txt).

Owner review, repository publication and license selection remain pending. See [license decision](LICENSE-DECISION.txt). Hosting, recurring collection and live rules remain paused. The demo uses zero live Ahrefs, model or evaluation calls.
