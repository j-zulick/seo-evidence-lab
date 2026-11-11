# Portfolio review and demonstration

## Verdict and intended audience

The tagged v0.1.0 is a published local portfolio release with actual public CI and matching downloadable source/checksums. The v0.2 saved-news bridge is a separately verified development candidate, not a new stable release or hosted client service. The strongest delivery is the runnable source, synthetic demonstration, screenshots, and recording; a new hosted application is unnecessary for this portfolio milestone.

The project addresses a practical review question for small-site owners and agencies: did an audit finding change because the article, guidance, scoring, or available evidence changed? It demonstrates backend/data integration, evidence lineage, deterministic replay, local tooling, failure handling, and transparent evaluation. That is a positioning judgment based on inspectable artifacts, not a prediction of hiring or client conversion.

## Finished positioning statements

- **Project description:** Explain changing SEO findings with versioned evidence, deterministic replay, transparent evaluation, and read-only local MCP tools.
- **Employer-facing statement:** Directed and verified a Codex-assisted Python/FastAPI evidence-review project, integrating versioned source inputs, replay, contract validation, and reproducible tests while preserving explicit boundaries between synthetic results and live acceptance.
- **Client-facing statement:** Review supplied content and saved observations through a report that shows the passage change, supporting inputs, and why a deterministic finding changed; search impact and business outcomes remain separately measured questions.

User contribution evidenced in project records includes choosing the domain problem, workflows, release priorities, source/owner decisions, public destination and license/publication approval. Implementation, tests, packaging and much of the documentation were Codex-assisted, with Hermes Management support providing independently reproduced fixes. Dependencies supply the web framework, parser/runtime, database engine, tracing, test tools and MCP protocol support. Do not present all underlying infrastructure as newly authored or claim sole unaided implementation.

## Small reference comparison

Inspected November 11, 2026. This is selective source/test/license review, not execution or a complete audit of either external repository. Public popularity is not used as evidence of quality.

| Reference and useful pattern | Existing coverage and demonstrated gap | Decision and evidence |
| --- | --- | --- |
| Preserved local SEO QA predecessor: retain the working audit and review workflows | The first public-history import contains 183 exact matches to the reviewed local archive; a new audit framework was not needed | Extend the project-local implementation. [Initial import](https://github.com/j-zulick/seo-evidence-lab/commit/d8d25cc8d247d2e87e7c22f53b054b057a8aa288). Attribution separates Sweepr's originating concept and Botworthy's later methodology. |
| Crawlee: request identity, duplicate handling, and retained handled state | Existing saved captures and local versioning cover this release's needs. The concrete missing piece was importing the actual saved email envelope, not collecting pages | Use a thin saved-export adapter; do not add a crawler or copy upstream source. Crawlee is TypeScript and Apache-2.0, outside the current Python dependency environment. Inspected [queue implementation](https://github.com/apify/crawlee/blob/80ec50162041cac8f85edbcc0031466ae4b68541/packages/core/src/storages/request-queue.ts), [duplicate-request regression](https://github.com/apify/crawlee/blob/80ec50162041cac8f85edbcc0031466ae4b68541/test/core/request-queue/adding-the-same-request-should-not-call-the-api.test.ts), and license at that commit. Reconsider direct upstream use only if separately authorized collection exposes a real unmet need. |
| Official MCP Python SDK v1.30.0: function schemas and tool registration | The installed SDK already supplies protocol/tool infrastructure. The project adds three bounded domain reads and unknown-ID refusal; annotations alone are not access control | Keep the installed dependency instead of implementing a protocol framework. MIT license inspected. [Tool implementation](https://github.com/modelcontextprotocol/python-sdk/blob/8c2fa6eabacde869d75568e33b9d466b58c2f512/src/mcp/server/fastmcp/tools/base.py) and [100-tool listing regression](https://github.com/modelcontextprotocol/python-sdk/blob/8c2fa6eabacde869d75568e33b9d466b58c2f512/tests/issues/test_100_tool_listing.py). Current HEAD has different paths/API generations, so its snippets were not adopted. Repository metadata showed it unarchived; that is not a maintenance guarantee. |

Windmill MCP was supplied as a possible reference, but no unmet workflow-runner requirement justifies adopting or rebuilding it here. No upstream fork/contribution is needed for the demonstrated saved-export mismatch. A small local adapter has lower upkeep and a directly testable domain outcome.

## Concrete engineering case

The saved news producer uses `payload.mime_type` and `payload.body.content`, not a fictional generic feed. Four inspected saved digests supplied usable headline URLs; an older digest with no article URLs was rejected without inventing them. A real footer repeated the recipient address inside prose: the first private compatibility check caught that, the adapter minimized email addresses in excerpts, and a regression plus clean import verified the correction. Real digest content stays outside public source.

The bridge pins source/import/version identities and can replay an explicitly proposed heading-rule change against supplied audit inputs. The resulting artifact remains inactive and unreviewed. This makes the provenance connection inspectable without manufacturing corroboration, human review, or rule activation. [Contract and limits](SAVED-NEWS-BRIDGE.md).

## Two minute demonstration

1. **0:00–0:20:** State the problem and distinguish an audit finding from actual search performance.
2. **0:20–0:50:** Show the original/edit passages, pinned inputs and counterfactual heading replay.
3. **0:50–1:20:** Show incorrect canned judgments and the retrieval paraphrase miss. These evaluate the harness, not a live model's accuracy.
4. **1:20–1:45:** On the v0.2 candidate, import the synthetic saved digest and show version identity, retained revisions and an inactive proposal. On stable v0.1.0, show the packaged news examples and label the bridge as development work.
5. **1:45–2:00:** Explain the next acceptance gate: genuine primary-source corroboration and approved guidance publication. No ranking gain, client savings or production operation is inferred.

## Interview questions the artifacts can answer

- Why keep failed import attempts separately from last-good content versions?
- What exactly is pinned in a replay, and what would invalidate a comparison?
- Why use SQLite and a local manual workflow before adding distributed ingestion?
- What did the recipient-footer regression reveal that synthetic-only testing missed?
- How are MCP tool hints different from actual authorization and input refusal?
- Why is a conditional replay explanation different from causal attribution of search results?
- What remains unknown when an export provides a publication date but no observation timestamp?

## Acceptance and remaining findings

| Classification | Owner and dependency | Observable completion |
| --- | --- | --- |
| Publication blockers for v0.1.0 | Source owner; already resolved approval, MIT, exact artifact and actual CI | Public repo/release/tag and anonymous asset hashes verified; no remaining known blocker in inspected scope |
| v0.2 development checks | Source/integrator; scoped implementation and composition | Saved-export, malformed/retry/privacy cases, proposal pins, browser restart and combined regression pass at an exact candidate |
| Full news-to-approved-guidance acceptance | Genuine source reviewer and integrator; primary evidence and actual decision | A reviewed guidance version is approved, published immutably and linked to affected accepted findings; not inferred from a software proposal |
| Deployment-only work | Designated deployment owner; separate authorization, configuration and cost/data review | Actual multi-user authorization, secure provider setup, recovery and operating acceptance; original production stays paused |
| Follow-on improvements | Existing owners; demonstrated need | Broader retrieval/replay, live provider acceptance and permitted collection each have their own evidence; they do not block the useful local source release |

The executable next step is the saved-export demonstration and candidate composition, not a new framework, account or paid service. [Architecture](ARCHITECTURE.md) · [Evaluation scope](EVALUATION.md) · [Roadmap](ROADMAP.md).
