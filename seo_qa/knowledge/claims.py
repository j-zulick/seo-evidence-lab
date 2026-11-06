"""Deterministic, extractive K04 candidates for human review."""
from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from datetime import date
from typing import Literal

from pydantic import ValidationError, model_validator

from .change_detection import MODAL, ChangeEvent, SnapshotInput
from .contracts import (
    Applicability, Authority, EvidenceSpan, KnowledgeClaim, SnapshotID,
    Source, SourceID, SourceSnapshot, Wire, canonical_bytes, sha256,
)
from .policy import PRIMARY

Kind = Literal['requirement', 'recommendation', 'deprecation', 'measurement',
               'limitation', 'announcement']
Origin = Literal['primary_assertion', 'research_observation', 'research_hypothesis',
                 'other_source_assertion']
Timing = Literal['prospective', 'current_as_stated', 'unknown']
SIGNALS = (
    ('deprecation', r'\b(?:deprecat\w*|sunset\w*|retir(?:e|ed|ing)|discontinu\w*)\b'),
    ('requirement', r'\b(?:must|shall|required|mandatory|prohibited)\b'),
    ('recommendation', r'\b(?:recommend\w*|should)\b|^(?:use|prefer|avoid)\s'),
    ('limitation', r'\b(?:maximum|minimum|threshold|at (?:least|most)|no more than|'
                   r'unsupported|does not support|limited to|limits?)\b'),
    ('measurement', r'\b(?:measured|observed|median|mean|average)\b|\d\s*%'),
    ('announcement', r'\b(?:announc\w*|will|plans to|scheduled to|coming soon|effective|starting|'
                      r'supports?|now available|takes effect)\b'),
)
INSTRUCTION = re.compile(
    r'\b(?:ignore (?:all |the )?(?:previous|prior|above) instructions|system prompt|'
    r'output (?:only|json)|mark .{0,80} (?:approved|active)|approve .{0,80}claim)\b', re.I,
)
VAGUE = re.compile(r'\b(?:may|might|could|hypothesi[sz]\w*|suggests?)\b', re.I)
HEADING = re.compile(r'^#{1,6}\s+\S')
FIELDS = re.compile(
    r'(?:^|;\s*)(Provider|Product|Version|Region|Conditions?|Exceptions?)\s*:\s*([^;\n]+)',
    re.I,
)
QUALIFIER = re.compile(
    r'\b(only if|only when|if|unless|except(?: for)?)\s+([^;,]+)', re.I,
)
DATE = r'\d{4}-\d{2}-\d{2}'
EFFECTIVE_DATE = re.compile(
    rf'\b(effective(?: from| until)?|starting|takes effect(?: on)?)\s+'
    rf'({DATE}|[A-Z][a-z]+ \d{{1,2}}, \d{{4}})\b', re.I,
)
MONTHS = {name.casefold(): number for number, name in enumerate(
    ('January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
     'September', 'October', 'November', 'December'), 1,
)}


class ExplicitField(Wire):
    name: Literal['provider', 'product', 'version', 'region', 'condition', 'exception']
    value: str


class ClaimCandidate(Wire):
    claim: KnowledgeClaim
    source_id: SourceID
    authority: Authority
    kind: Kind
    origin: Origin
    timing: Timing
    fields: tuple[ExplicitField, ...] = ()
    heading: EvidenceSpan | None = None
    change_event_id: str | None = None

    @model_validator(mode='after')
    def extractive_draft(self):
        if (self.claim.status != 'needs_review' or len(self.claim.evidence) != 1
                or self.claim.statement != self.claim.evidence[0].quote):
            raise ValueError('Candidate must be a verbatim claim pending review')
        if self.heading and self.heading.snapshot_id != self.claim.evidence[0].snapshot_id:
            raise ValueError('Heading must belong to the claim snapshot')
        return self


class ExtractionResult(Wire):
    candidates: tuple[ClaimCandidate, ...] = ()
    limitations: tuple[str, ...] = ()


def verify_evidence(
    span: EvidenceSpan, source: Source, snapshot: SourceSnapshot, text: bytes,
) -> None:
    """Verify the K00 identity chain and exact UTF-8 evidence independently."""
    source = Source.model_validate(source)
    snapshot = SourceSnapshot.model_validate(snapshot)
    span = EvidenceSpan.model_validate(span)
    if (snapshot.source_id != source.id or span.snapshot_id != snapshot.id
            or snapshot.availability != 'captured'):
        raise ValueError('Evidence source or snapshot mismatch')
    if sha256(text) != snapshot.text_sha256:
        raise ValueError('Evidence text hash mismatch')
    _verify_span(span, snapshot.id, text)


def _verify_span(span: EvidenceSpan, snapshot_id: SnapshotID, text: bytes) -> None:
    if (span.snapshot_id != snapshot_id
            or text[span.start_byte:span.end_byte] != span.quote.encode('utf-8')):
        raise ValueError('Evidence span does not match snapshot bytes')


def _explicit_fields(quote: str) -> tuple[ExplicitField, ...]:
    statement, _, metadata = quote.partition(';')
    labeled = tuple(
        ExplicitField(name=name.lower().removesuffix('s'), value=value.rstrip('.!? '))
        for name, value in FIELDS.findall(';' + metadata) if value.rstrip('.!? ')
    )
    qualifiers = tuple(
        ExplicitField(
            name='exception' if marker.lower().startswith(('unless', 'except')) else 'condition',
            value=f'{marker} {value.rstrip(".!? ")}',
        )
        for marker, value in QUALIFIER.findall(statement)
    )
    return labeled + qualifiers


def _scope(quote: str, fields: tuple[ExplicitField, ...]) -> tuple[Applicability, bool]:
    dates = defaultdict(set)
    invalid = False
    for label, value in EFFECTIVE_DATE.findall(quote):
        try:
            if '-' in value:
                parsed = date.fromisoformat(value)
            else:
                month, day, year = value.replace(',', '').split()
                parsed = date(int(year), MONTHS[month.casefold()], int(day))
        except (ValueError, KeyError):
            invalid = True
            continue
        key = 'effective_until' if label.lower().endswith('until') else 'effective_from'
        dates[key].add(parsed)
    values = {key: next(iter(items)) for key, items in dates.items() if len(items) == 1}
    invalid |= any(len(items) > 1 for items in dates.values())
    for name, key, pattern in (
        ('provider', 'provider', r'[a-z][a-z0-9]*(?:[-_.][a-z0-9]+)*'),
        ('region', 'jurisdiction', r'[A-Z]{2}(?:-[A-Z0-9]{1,3})?'),
        ('version', 'platform_version', r'[\w.-]+'),
    ):
        matches = {field.value for field in fields if field.name == name}
        if len(matches) == 1:
            value = next(iter(matches))
            if re.fullmatch(pattern, value):
                values[key] = value
    try:
        return Applicability(**values), invalid
    except ValidationError:
        for key in ('effective_from', 'effective_until'):
            values.pop(key, None)
        try:
            return Applicability(**values), True
        except ValidationError:
            return Applicability(), True


def _event_spans(event, source, snapshot, text):
    documents = [doc for doc in event.documents if doc.source_id == source.id]
    if len(documents) != 1 or documents[0].after is None:
        raise ValueError('Change event does not identify a unique current revision')
    document = documents[0]
    if document.outcome not in {'added', 'edited'}:
        raise ValueError('Change event does not identify a changed current revision')
    if document.after.snapshot != snapshot or document.after.source != source:
        raise ValueError('Change event current revision mismatch')
    spans = tuple(span for change in document.changes for span in change.after)
    for span in spans:
        _verify_span(span, snapshot.id, text)
    return spans


def extract_claims(
    snapshots: Iterable[SnapshotInput], *, as_of: date,
    change_event: ChangeEvent | None = None,
    truncated_snapshots: Iterable[SnapshotID] = (),
) -> ExtractionResult:
    """Extract review candidates from verified captures or a K03 change event.

    Use full captures for contradiction checks. Conflict scope and truncation
    markers are trusted caller metadata. Source text is always inert data.
    """
    if change_event is not None:
        change_event = ChangeEvent.model_validate(change_event)
    truncated = set(truncated_snapshots)
    limitations = set()
    candidates = {}
    modals = defaultdict(lambda: (set(), set()))
    for item in snapshots:
        label = item.snapshot.id
        try:
            source = Source.model_validate(item.source)
            snapshot = SourceSnapshot.model_validate(item.snapshot)
            if source.id != snapshot.source_id or source.status == 'retracted':
                raise ValueError('Invalid or retracted source relationship')
            if snapshot.availability != 'captured' or item.text is None:
                limitations.add(f'{label}: text unavailable')
                continue
            text = item.text
            if sha256(text) != snapshot.text_sha256:
                raise ValueError('Capture text hash mismatch')
            decoded = text.decode('utf-8')
            changes = None if change_event is None else _event_spans(
                change_event, source, snapshot, text,
            )
        except ValueError as exc:
            limitations.add(f'{label}: {str(exc).splitlines()[0]}')
            continue

        lines = decoded.splitlines(keepends=True)
        if any(number < 1 or number > len(lines) for number in item.template_lines):
            limitations.add(f'{label}: invalid template line numbers')
            continue
        if label in truncated:
            limitations.add(f'{label}: truncated capture')
        offset = 0
        heading = None
        for number, line in enumerate(lines, 1):
            start = offset
            offset += len(line.encode('utf-8'))
            if number in item.template_lines or (label in truncated and number == len(lines)
                                                  and not line.endswith(('\r', '\n'))):
                continue
            stripped = line.strip()
            if not stripped or INSTRUCTION.search(stripped):
                continue
            if HEADING.match(stripped):
                heading = _span(snapshot.id, start, line, stripped)
                continue
            # Keep semicolon-delimited scope and conditional clauses together.
            for sentence in re.finditer(r'\S.*?(?:[.!?](?=\s+[A-Z]|\s*$)|$)', line):
                quote = sentence.group().strip()
                if not quote.endswith(('.', '!')) or quote.endswith(('...', '…')):
                    continue
                kind = next((name for name, signal in SIGNALS if re.search(signal, quote, re.I)), None)
                if not kind:
                    continue
                span = _span(snapshot.id, start + len(line[:sentence.start()].encode()), sentence.group(), quote)
                if changes is not None and not any(
                    delta.start_byte <= span.start_byte and span.end_byte <= delta.end_byte
                    for delta in changes
                ):
                    continue
                _verify_span(span, snapshot.id, text)
                if heading:
                    _verify_span(heading, snapshot.id, text)
                fields = _explicit_fields(quote)
                scope, invalid_date = _scope(quote, fields)
                if invalid_date:
                    limitations.add(f'{label}: ambiguous or malformed scope')
                origin = 'other_source_assertion'
                if source.authority in PRIMARY:
                    origin = 'primary_assertion'
                elif source.authority == 'independent_research':
                    origin = 'research_hypothesis' if VAGUE.search(quote) else 'research_observation'
                timing = 'unknown'
                if ((scope.effective_from and scope.effective_from > as_of)
                        or re.search(r'\b(?:will|plans to|scheduled to|coming soon)\b', quote, re.I)):
                    timing = 'prospective'
                elif (not invalid_date and not VAGUE.search(quote)
                      and (scope.effective_until is None or as_of < scope.effective_until)
                      and (scope.effective_from or re.search(r'\b(?:now|currently)\b', quote, re.I))):
                    timing = 'current_as_stated'
                if kind == 'announcement' and not (
                    item.announcement or timing != 'unknown'
                    or re.search(r'\bannounc\w*\b', quote, re.I)
                ):
                    continue
                identity = sha256(canonical_bytes(span.model_dump(mode='json')))
                claim = KnowledgeClaim(
                    id=f'claim:k04-{identity}:1', statement=quote, scope=scope,
                    status='needs_review', evidence=(span,),
                )
                candidates[claim.id] = ClaimCandidate(
                    claim=claim, source_id=source.id, authority=source.authority,
                    kind=kind, origin=origin, timing=timing, fields=fields, heading=heading,
                    change_event_id=change_event.id if change_event else None,
                )
                modal = MODAL.fullmatch(quote)
                if modal and item.conflict_scope:
                    key = (item.conflict_scope, modal['subject'].casefold(),
                           modal['action'].casefold(), scope, fields)
                    modals[key][bool(modal['negative'])].add(claim.id)

    conflicts = defaultdict(set)
    for positive, negative in modals.values():
        for claim_id in positive:
            conflicts[claim_id].update(negative)
        for claim_id in negative:
            conflicts[claim_id].update(positive)
    return ExtractionResult(
        candidates=tuple(
            candidate.model_copy(update={'claim': candidate.claim.model_copy(
                update={'conflicts_with': tuple(sorted(conflicts[claim_id]))},
            )})
            for claim_id, candidate in sorted(candidates.items())
        ),
        limitations=tuple(sorted(limitations)),
    )


def _span(snapshot_id: SnapshotID, offset: int, raw: str, quote: str) -> EvidenceSpan:
    start = offset + len(raw[:len(raw) - len(raw.lstrip())].encode('utf-8'))
    return EvidenceSpan(snapshot_id=snapshot_id, start_byte=start,
                        end_byte=start + len(quote.encode('utf-8')), quote=quote)
