"""Read-only local fixture tools. Start with python -m seo_qa.timeline_mcp."""
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from .competitor import compare, demo
from .evidence_search import faceted_search, index, load

server = FastMCP('SEO QA fixture timeline')
readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)


def evidence(audit_id):
    if len(audit_id) != 64:
        raise ValueError('Unknown fixture audit ID')
    result = demo()['comparison']
    for key in ('before', 'after'):
        if result[key]['id'] == audit_id:
            return result[key]
    raise ValueError('Unknown fixture audit ID')


@server.tool(annotations=readonly)
def get_finding_evidence(audit_id: str) -> dict:
    """Return the immutable inputs and finding of a known fixture audit."""
    return evidence(audit_id)


@server.tool(annotations=readonly)
def compare_audit_versions(before_id: str, after_id: str) -> dict:
    """Replay two known fixture audit IDs and return their change attribution."""
    return compare(evidence(before_id)['inputs'], evidence(after_id)['inputs'])


@server.tool(annotations=readonly)
def get_competitor_timeline() -> dict:
    """Return the synthetic competitor timeline, including audit IDs for drill-down."""
    snap = demo()
    return {k:snap[k] for k in ('label','first_observed_between','diff','visibility','interpretation')} | {
        'pages':[r['request']['target'] for r in snap['series']],
        'audit_ids':[snap['comparison'][k]['id'] for k in ('before','after')]}



def evidence_facets(query='', sources=None, versions=None, limit=3, offset=0):
    db = index(load())
    try:
        return faceted_search(db, query, sources=sources or None, versions=versions or None,
                              limit=limit, offset=offset)
    finally:
        db.close()


@server.tool(annotations=readonly)
def search_evidence(query: str = '', sources: list[str] | None = None, versions: list[str] | None = None,
                    limit: int = 3, offset: int = 0) -> dict:
    """Search the frozen synthetic passage corpus with source/version facets and provenance hashes.
    Lexical matches do not establish that a claim is true."""
    return evidence_facets(query, sources, versions, limit, offset)


if __name__ == '__main__':
    server.run(transport='stdio')
