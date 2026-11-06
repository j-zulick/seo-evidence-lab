"""Finite synthetic multi-domain consumer; never selects live transport."""
import argparse
import json
from pathlib import Path
from .capture import ApprovedSource, Fixtures, Response
from .contracts import Source, sha256
from .operations import UpdateOperations

DOMAINS = {
    "search": "https://developers.google.com/search/docs",
    "web_development": "https://developer.mozilla.org/en-US/docs/Web",
    "marketing": "https://developers.google.com/analytics/devguides/collection/ga4",
    "llm_observability": "https://opentelemetry.io/docs/specs/semconv/gen-ai/",
}

def run(directory):
    # URLs are proposed official-source locators, not fetched/current knowledge.
    sources = []
    replies = {}
    for domain, locator in DOMAINS.items():
        url = "https://" + domain.replace("_", "-") + ".example/fixture"
        sources.append(ApprovedSource(Source(id="src:" + sha256(url.encode()),
            canonical_url=url, publisher=domain, authority="official_primary"), "text", min_interval=0))
        replies[url] = [Response(200, (("content-type", "text/plain"), ("etag", '"fixture-v1"')),
            (domain + ": Pages must have titles. Synthetic evidence.").encode()), Response(304)]
    transport = Fixtures(replies)
    ops = UpdateOperations(Path(directory), sources, fixtures=transport)
    first = ops.run("demo-v1")
    ops.run("demo-v1")
    final = ops.run("demo-conditional-v1")
    return {"synthetic": True, "proposed_locators": DOMAINS, "requests": len(transport.requests),
            "first": first, "final": final, "tick": ops.tick()}

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.directory), indent=2, sort_keys=True))
