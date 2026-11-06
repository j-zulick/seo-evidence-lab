"""Offline comparison of K00 snapshots and their original UTF-8 evidence."""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
from difflib import SequenceMatcher
from typing import Iterable, Literal

from .contracts import (
    Digest, Entity, EvidenceSpan, Key, Source, SourceID, SourceSnapshot, Wire,
    canonical_bytes, sha256,
)

REVISION = 'k03-v1'
Outcome = Literal['added', 'edited', 'removed', 'unchanged', 'incomplete']
Category = Literal[
    'requirement', 'threshold', 'supported_feature', 'deprecation',
    'effective_date', 'clarification', 'body',
]
CATEGORIES = (
    ('requirement', r'\b(?:must|shall|required|prohibited|mandatory)\b'),
    ('threshold', r'\b(?:threshold|limit|at least|at most|maximum|minimum|no more than)\b|\d\s*%'),
    ('supported_feature', r'\b(?:supports?|supported|unsupported|features?|compatible)\b'),
    ('deprecation', r'\b(?:deprecat\w*|sunset\w*|retir\w*|discontinu\w*)\b'),
    ('effective_date', r'\b(?:effective|takes effect|starting|as of)\b'),
    ('clarification', r'\b(?:clarif\w*|exception|applies only|does not apply|for example)\b'),
)
MODAL = re.compile(
    r'^(?P<subject>.+?)\s+(?:must|shall)\s+(?P<negative>not\s+)?(?P<action>.+?)[.!]?$', re.I,
)


@dataclass(frozen=True)
class SnapshotInput:
    source: Source
    snapshot: SourceSnapshot
    text: bytes | None = None
    template_lines: frozenset[int] = frozenset()
    announcement: bool = False
    conflict_scope: str | None = None
    observed_at: datetime | None = None


class DocumentRevision(Wire):
    source: Source
    snapshot: SourceSnapshot
    content_sha256: Digest | None = None


class PassageChange(Wire):
    before: tuple[EvidenceSpan, ...]
    after: tuple[EvidenceSpan, ...]
    categories: tuple[Category, ...]


class DocumentComparison(Wire):
    source_id: SourceID
    outcome: Outcome
    before: DocumentRevision | None
    after: DocumentRevision | None
    changes: tuple[PassageChange, ...] = ()
    limitations: tuple[str, ...] = ()


class ChangeEvent(Entity):
    """K03 observation. The K00 namesake represents a status transition."""

    id: Key
    detector_revision: Key = REVISION
    outcome: Literal['added', 'edited', 'removed', 'incomplete']
    documents: tuple[DocumentComparison, ...]


class Syndication(Wire):
    content_sha256: Digest
    revisions: tuple[DocumentRevision, ...]


class PossibleContradiction(Wire):
    scope: str
    affirmative: EvidenceSpan
    negative: EvidenceSpan


class ComparisonResult(Entity):
    detector_revision: Key = REVISION
    documents: tuple[DocumentComparison, ...]
    events: tuple[ChangeEvent, ...]
    syndication: tuple[Syndication, ...]
    possible_contradictions: tuple[PossibleContradiction, ...]


@dataclass(frozen=True)
class _Prepared:
    revision: DocumentRevision
    lines: tuple[str, ...] = ()
    spans: tuple[EvidenceSpan, ...] = ()
    limitation: str | None = None
    announcement: bool = False
    conflict_scope: str | None = None
    observed_at: datetime | None = None


def _prepare(item: SnapshotInput) -> _Prepared:
    if item.snapshot.source_id != item.source.id:
        raise ValueError('Snapshot source ID does not match source')
    revision = DocumentRevision(source=item.source, snapshot=item.snapshot)
    if item.snapshot.availability != 'captured':
        if item.text is not None or item.template_lines:
            raise ValueError('Unavailable snapshots cannot supply text or template lines')
        return _Prepared(revision, limitation=item.snapshot.availability)
    if item.text is None:
        return _Prepared(revision, limitation='captured_text_missing')
    if sha256(item.text) != item.snapshot.text_sha256:
        return _Prepared(revision, limitation='text_hash_mismatch')

    try:
        text = item.text.decode('utf-8')
    except UnicodeDecodeError:
        return _Prepared(revision, limitation='invalid_utf8')

    raw_lines = text.splitlines(keepends=True)
    if any(line < 1 or line > len(raw_lines) for line in item.template_lines):
        raise ValueError('Template line numbers must refer to the original text')
    lines, spans = [], []
    offset = 0
    for number, raw_line in enumerate(raw_lines, 1):
        quote = raw_line.rstrip('\r\n')
        normalized = unicodedata.normalize('NFC', quote)
        if number not in item.template_lines and normalized.strip():
            lines.append(normalized)
            spans.append(EvidenceSpan(
                snapshot_id=item.snapshot.id, start_byte=offset,
                end_byte=offset + len(quote.encode('utf-8')), quote=quote,
            ))
        offset += len(raw_line.encode('utf-8'))
    if not lines:
        return _Prepared(revision, limitation='no_material_text')

    revision = DocumentRevision(
        source=item.source, snapshot=item.snapshot,
        content_sha256=sha256(canonical_bytes(lines)),
    )
    return _Prepared(revision, tuple(lines), tuple(spans), announcement=item.announcement,
                     conflict_scope=item.conflict_scope)


def _index(items: Iterable[SnapshotInput]) -> dict[str, _Prepared]:
    indexed = {}
    for item in items:
        if item.source.id in indexed:
            raise ValueError('Each inventory must contain at most one snapshot per source')
        prepared = _prepare(item)
        if item.observed_at is not None:
            if item.observed_at.utcoffset() is None or item.observed_at < item.snapshot.captured_at:
                raise ValueError('Observation must be aware and cannot predate capture')
            prepared = replace(prepared, observed_at=item.observed_at)
        indexed[item.source.id] = prepared
    return indexed


def _passage_changes(before: _Prepared | None, after: _Prepared | None):
    old = before.lines if before else ()
    new = after.lines if after else ()
    for tag, i, j, k, l in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag == 'equal':
            continue
        passage = '\n'.join(old[i:j] + new[k:l])
        categories = tuple(name for name, pattern in CATEGORIES if re.search(pattern, passage, re.I))
        yield PassageChange(
            before=before.spans[i:j] if before else (),
            after=after.spans[k:l] if after else (),
            categories=categories or ('body',),
        )


def _compare(
    source_id: str,
    before: _Prepared | None,
    after: _Prepared | None,
    historical_complete: bool,
    removed: bool,
) -> DocumentComparison:
    if before and after and (before.observed_at or before.revision.snapshot.captured_at) > (
            after.observed_at or after.revision.snapshot.captured_at):
        raise ValueError('Current snapshot predates historical snapshot')
    limitations = [
        f'{side}:{item.limitation}'
        for side, item in (('historical', before), ('current', after))
        if item and item.limitation
    ]
    if before is None and not historical_complete:
        limitations.append('historical:snapshot_absent')
    if after is None and not removed:
        limitations.append('current:snapshot_absent')
    if before and after and not limitations:
        if before.revision.snapshot.extractor != after.revision.snapshot.extractor:
            limitations.append('extractor_revision_changed')
    if limitations:
        outcome = 'incomplete'
        changes = ()
    elif before is None:
        outcome = 'added'
        changes = tuple(_passage_changes(before, after))
    elif after is None:
        outcome = 'removed'
        changes = tuple(_passage_changes(before, after))
    else:
        changes = tuple(_passage_changes(before, after))
        outcome = 'edited' if changes else 'unchanged'
    return DocumentComparison(
        source_id=source_id, outcome=outcome,
        before=before.revision if before else None,
        after=after.revision if after else None,
        changes=changes, limitations=tuple(limitations),
    )


def _possible_contradictions(current: dict[str, _Prepared]):
    claims = defaultdict(lambda: ([], []))
    for source_id, item in sorted(current.items()):
        if not item.conflict_scope:
            continue
        for line, span in zip(item.lines, item.spans):
            match = MODAL.fullmatch(line.strip())
            if match:
                key = (item.conflict_scope, match['subject'].casefold(), match['action'].casefold())
                claims[key][bool(match['negative'])].append((source_id, span))
    for (scope, _, _), (affirmative, negative) in sorted(claims.items()):
        for left_id, left in affirmative:
            for right_id, right in negative:
                if left_id != right_id:
                    yield PossibleContradiction(scope=scope, affirmative=left, negative=right)


def compare_snapshots(
    historical: Iterable[SnapshotInput],
    current: Iterable[SnapshotInput],
    *,
    historical_complete: bool = False,
    confirmed_removed: Iterable[str] = (),
) -> ComparisonResult:
    """Compare caller-selected inventories. Confirm removals independently of capture failures."""
    old, new = _index(historical), _index(current)
    removed = frozenset(confirmed_removed)
    if removed - (old.keys() - new.keys()):
        raise ValueError('Confirmed removals must appear only in the historical inventory')

    documents = []
    event_groups = defaultdict(list)
    syndicated = defaultdict(list)
    for source_id in sorted(old.keys() | new.keys()):
        before, after = old.get(source_id), new.get(source_id)
        document = _compare(source_id, before, after, historical_complete, source_id in removed)
        documents.append(document)
        if after and after.announcement and after.revision.content_sha256:
            syndicated[after.revision.content_sha256].append(after.revision)
        if document.outcome == 'unchanged':
            continue
        if document.outcome in ('added', 'edited') and after.announcement:
            key = (document.outcome, document.before.content_sha256 if before else None,
                   document.after.content_sha256)
        else:
            key = (document.outcome, source_id)
        event_groups[key].append(document)

    events = []
    for (outcome, *_), members in event_groups.items():
        payload = {
            'revision': REVISION,
            'documents': [item.model_dump(mode='json') for item in members],
        }
        events.append(ChangeEvent(id='k03-' + sha256(canonical_bytes(payload)),
                                  outcome=outcome, documents=tuple(members)))
    return ComparisonResult(
        documents=tuple(documents), events=tuple(sorted(events, key=lambda event: event.id)),
        syndication=tuple(Syndication(content_sha256=digest, revisions=tuple(revisions))
                          for digest, revisions in sorted(syndicated.items()) if len(revisions) > 1),
        possible_contradictions=tuple(_possible_contradictions(new)),
    )
