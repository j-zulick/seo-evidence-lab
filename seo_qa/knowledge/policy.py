"""Cross-record checks at the publication and audit boundaries."""
from collections.abc import Callable, Iterable

from .contracts import (
    AuditImpact, ChangeEvent, GuidanceVersion, KnowledgeClaim, KnowledgeRelease,
    ReviewDecision, RuleBinding, Source, SourceSnapshot, canonical_bytes,
    record_bytes, sha256,
)

ReleaseRecord = Source | SourceSnapshot | KnowledgeClaim | GuidanceVersion | RuleBinding | ReviewDecision
PRIMARY = {'official_primary', 'standards_primary'}
TRANSITIONS = {
    Source: {None: {'discovered'}, 'discovered': {'retracted'}},
    KnowledgeClaim: {
        None: {'extracted', 'needs_review'},
        'extracted': {'needs_review', 'corroborated', 'retracted'},
        'needs_review': {'corroborated', 'retracted'},
        'corroborated': {'superseded', 'retracted'},
    },
    GuidanceVersion: {
        None: {'needs_review'},
        'needs_review': {'approved_guidance', 'retracted'},
        'approved_guidance': {'superseded', 'retracted'},
    },
    RuleBinding: {
        None: {'candidate_rule_change'},
        'candidate_rule_change': {'active_rule', 'retracted'},
        'active_rule': {'superseded', 'retracted'},
    },
}


def validate_transition(event: ChangeEvent, subject: ReleaseRecord, current_status):
    if (event.subject_id != subject.id or event.from_status != current_status
            or event.to_status != getattr(subject, 'status', None)
            or event.to_status not in TRANSITIONS.get(type(subject), {}).get(current_status, ())):
        raise ValueError('Invalid lifecycle transition')


def validate_release(
    release: KnowledgeRelease,
    records: Iterable[ReleaseRecord],
    read_blob: Callable[[str], bytes],
) -> None:
    """Validate the exact closed record set and its evidence bytes.

    Callers must authenticate author, reviewer and publisher identities before
    supplying records and must resolve authority from the curated registry.
    """
    by_id = {}
    for record in records:
        if not isinstance(record, ReleaseRecord):
            raise ValueError('Unsupported release record')
        if record.id in by_id:
            raise ValueError('Duplicate release record')
        by_id[record.id] = record
    expected = {pin.id: pin.sha256 for pin in release.members}
    if expected != {key: sha256(record_bytes(record)) for key, record in by_id.items()}:
        raise ValueError('Release member mismatch')

    def get(key, kind):
        record = by_id.get(key)
        if not isinstance(record, kind):
            raise ValueError(f'Missing or invalid {kind.__name__} reference')
        return record

    def read_checked(digest):
        try:
            data = read_blob(digest)
        except (KeyError, OSError) as exc:
            raise ValueError('Evidence blob unavailable') from exc
        if sha256(data) != digest:
            raise ValueError('Evidence blob hash mismatch')
        return data

    def approval(record, decision):
        decisions = [item for item in by_id.values()
                     if isinstance(item, ReviewDecision) and item.subject_id == record.id]
        if not decisions:
            raise ValueError('Publication requires review')
        decisions.sort(key=lambda item: item.decided_at)
        latest = decisions[-1]
        if (len(decisions) > 1 and decisions[-2].decided_at == latest.decided_at
                or latest.decision != decision or latest.subject_sha256 != expected[record.id]):
            raise ValueError('Publication requires an unambiguous approval of the exact record')

    def evidence_sources(claim):
        for span in claim.evidence:
            snapshot = get(span.snapshot_id, SourceSnapshot)
            if snapshot.availability != 'captured':
                raise ValueError('Evidence must reference a captured snapshot')
            source = get(snapshot.source_id, Source)
            data = read_checked(snapshot.text_sha256)
            if data[span.start_byte:span.end_byte] != span.quote.encode('utf-8'):
                raise ValueError('Evidence quote does not match snapshot bytes')
            yield source

    for record in by_id.values():
        if isinstance(record, SourceSnapshot):
            get(record.source_id, Source)
            if record.captured_at > release.published_at:
                raise ValueError('Release cannot include a future capture')
            if record.availability == 'captured':
                read_checked(record.raw_sha256)
                read_checked(record.text_sha256).decode('utf-8')
        elif isinstance(record, KnowledgeClaim):
            if record.status not in {'corroborated', 'superseded', 'retracted'}:
                raise ValueError('Draft claims cannot enter a release')
            for conflict_id in record.conflicts_with:
                get(conflict_id, KnowledgeClaim)
            if record.supersedes and get(record.supersedes, KnowledgeClaim).status != 'superseded':
                raise ValueError('Superseded claim must be marked in release')
            sources = tuple(evidence_sources(record))
            if record.status == 'corroborated':
                eligible = [s for s in sources if s.status != 'retracted']
                independent = {s.publisher for s in eligible if s.authority == 'independent_research'}
                if (not any(s.authority in PRIMARY for s in eligible) and len(independent) < 2
                        or any(c.status == 'corroborated' and record.id in c.conflicts_with
                               for c in by_id.values() if isinstance(c, KnowledgeClaim))):
                    raise ValueError('Corroboration requires authoritative evidence and resolved conflicts')
        elif isinstance(record, GuidanceVersion):
            if record.status == 'needs_review':
                raise ValueError('Draft guidance cannot enter a release')
            for claim_id in record.claim_ids:
                claim = get(claim_id, KnowledgeClaim)
                if record.status == 'approved_guidance':
                    if claim.status != 'corroborated' or record.scope != claim.scope:
                        raise ValueError('Approved guidance requires corroborated claims with identical scope')
            if record.status == 'approved_guidance':
                approval(record, 'approve_guidance')
        elif isinstance(record, RuleBinding):
            if record.status == 'candidate_rule_change':
                raise ValueError('Candidate rules cannot enter a release')
            guidance = get(record.guidance_id, GuidanceVersion)
            if record.status == 'active_rule':
                if guidance.status != 'approved_guidance':
                    raise ValueError('Active rule requires approved guidance')
                for claim_id in guidance.claim_ids:
                    if not any(source.authority in PRIMARY and source.status != 'retracted'
                               for source in evidence_sources(get(claim_id, KnowledgeClaim))):
                        raise ValueError('Executable rule requires primary evidence for every claim')
                approval(record, 'approve_rule')
        elif isinstance(record, ReviewDecision):
            if record.decided_at > release.published_at or record.subject_id not in by_id:
                raise ValueError('Review must reference an included subject and predate publication')


def validate_audit_pin(
    impact: AuditImpact, release: KnowledgeRelease, bindings: Iterable[RuleBinding],
) -> None:
    """Verify supplied executable pins against the manifest at audit admission/replay."""
    if impact.release_id != release.id:
        raise ValueError('Audit release mismatch')
    selected = sorted(bindings, key=lambda binding: binding.id)
    if tuple(binding.id for binding in selected) != impact.binding_ids:
        raise ValueError('Audit binding selection mismatch')
    if impact.mode != 'bound':
        return
    pins = {pin.id: pin.sha256 for pin in release.members}
    for binding in selected:
        if binding.status != 'active_rule' or pins.get(binding.id) != sha256(record_bytes(binding)):
            raise ValueError('Audit binding is not active in the pinned release')
    bundle = [[binding.id, binding.implementation_revision, binding.implementation_sha256]
              for binding in selected]
    if sha256(canonical_bytes(bundle)) != impact.rule_bundle_sha256:
        raise ValueError('Audit rule bundle mismatch')
