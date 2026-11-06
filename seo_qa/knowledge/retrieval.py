"""Release-pinned advisory search with byte-precise citations."""
from contextlib import closing
from dataclasses import dataclass
from datetime import date
import sqlite3
from typing import Protocol

from .contracts import (
    EvidenceSpan, GuidanceVersion, KnowledgeClaim, RetrievalResult, Source, SourceSnapshot,
)


@dataclass(frozen=True)
class Context:
    evaluated_on: date
    provider: str | None = None
    search_engine: str | None = None
    jurisdiction: str | None = None
    platform_version: str | None = None
    true_conditions: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Citation:
    claim_id: str
    source: Source
    snapshot: SourceSnapshot
    span: EvidenceSpan


@dataclass(frozen=True)
class GuidanceHit:
    guidance: GuidanceVersion
    citations: tuple[Citation, ...]


@dataclass(frozen=True)
class SearchResult:
    result: RetrievalResult
    hits: tuple[GuidanceHit, ...] = ()


class SemanticAdapter(Protocol):
    def search(self, query: str, documents: dict[str, str]) -> list[str]:
        """Return ranked IDs drawn from the supplied eligible documents."""


def _applies(scope, context):
    return (
        all(getattr(scope, key) in (None, getattr(context, key))
            for key in ('provider', 'search_engine', 'jurisdiction', 'platform_version'))
        and (scope.effective_from is None or scope.effective_from <= context.evaluated_on)
        and (scope.effective_until is None or context.evaluated_on < scope.effective_until)
        and context.true_conditions.issuperset(scope.required_conditions)
    )


def _full_text(query, documents):
    # Build from verified, filtered release content. Keep the index ephemeral.
    with closing(sqlite3.connect(':memory:')) as db:
        db.execute('CREATE VIRTUAL TABLE search USING fts5(id UNINDEXED, body)')
        db.executemany('INSERT INTO search VALUES (?,?)', documents.items())
        # Treat input as a phrase so operators and punctuation cannot alter the query.
        phrase = '"' + query.replace('"', '""') + '"'
        return [row[0] for row in db.execute(
            'SELECT id FROM search WHERE body MATCH ? ORDER BY rank, id', (phrase,))]


def search(repository, context: Context, *, release_id=None, guidance_id=None,
           query=None, authority=None, semantic=False, adapter: SemanticAdapter | None = None,
           telemetry=None, model_identity=None):
    """Return approved, applicable guidance from the selected release.

    Text search matches a tokenized phrase across recommendations, statements
    and evidence quotes. Authority optionally requires a supporting citation
    from that class. A semantic adapter only receives eligible documents.
    """
    release, records = repository.graph(release_id)
    by_id = {record.id: record for record in records}
    hits = {}
    documents = {}
    for record in records:
        if (not isinstance(record, GuidanceVersion) or record.status != 'approved_guidance'
                or guidance_id not in (None, record.id) or not _applies(record.scope, context)):
            continue
        citations = []
        statements = []
        for claim_id in record.claim_ids:
            claim = by_id[claim_id]
            assert isinstance(claim, KnowledgeClaim)
            statements.append(claim.statement)
            for span in claim.evidence:
                snapshot = by_id[span.snapshot_id]
                citations.append(Citation(claim_id, by_id[snapshot.source_id], snapshot, span))
        if authority is not None and not any(item.source.authority == authority for item in citations):
            continue
        hits[record.id] = GuidanceHit(record, tuple(citations))
        documents[record.id] = '\n'.join(
            [record.recommendation, *statements, *(item.span.quote for item in citations)])

    fields = dict(release_id=release.id, evaluated_on=context.evaluated_on)
    if semantic:
        if not isinstance(query, str) or not query.strip():
            raise ValueError('Semantic search requires query text')
        if adapter is None:
            return SearchResult(RetrievalResult(**fields, outcome='unavailable',
                                                limitations=('Semantic adapter unavailable',)))
        try:
            if telemetry is not None:
                if not isinstance(model_identity, str) or not model_identity.strip():
                    raise ValueError('Instrumented semantic search requires model identity')
                ranked = telemetry.call(lambda: list(adapter.search(query, documents.copy())),
                                        model=model_identity, retrieval_id=release.id)
            else:
                ranked = list(adapter.search(query, documents.copy()))
        except (OSError, TimeoutError):
            return SearchResult(RetrievalResult(**fields, outcome='unavailable',
                                                limitations=('Semantic adapter failed',)))
        keys = tuple(dict.fromkeys(key for key in ranked if isinstance(key, str) and key in hits))
    elif query is not None:
        try:
            keys = tuple(_full_text(query, documents))
        except sqlite3.OperationalError as exc:
            if 'no such module: fts5' not in str(exc):
                raise
            return SearchResult(RetrievalResult(**fields, outcome='unavailable',
                                                limitations=('SQLite FTS5 unavailable',)))
    else:
        keys = tuple(hits)

    return SearchResult(RetrievalResult(**fields, outcome='matched' if keys else 'no_match',
                                       guidance_ids=keys), tuple(hits[key] for key in keys))
