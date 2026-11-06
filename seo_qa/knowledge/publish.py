"""Publish reviewed K05 guidance through the authorized knowledge repository."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime

from pydantic import AwareDatetime, TypeAdapter

from .contracts import (
    GuidanceVersion, KnowledgeClaim, KnowledgeRelease, ReviewDecision, RuleBinding,
    canonical_bytes, record_bytes, sha256,
)
from .reconciliation import ReviewJournal, _dimensions, _overlaps
from .repository import KnowledgeRepository

_RECEIPT = 'K05 reconciliation '


def _digest(value):
    return sha256(canonical_bytes(value))


def _wire(model):
    return model.model_dump(mode='json')


def _replace(record, **changes):
    return type(record).model_validate({**record.model_dump(), **changes})


def _scope(scope, page_type):
    if page_type is None:
        return scope
    conditions = {*scope.required_conditions, f'page-type-{page_type}'}
    return _replace(scope, required_conditions=tuple(sorted(conditions)))


def _remaining(scope, changes):
    """Subtract reviewed half-open windows from the baseline window."""
    start, stop = scope.effective_from or date.min, scope.effective_until
    windows = [(start, stop)]
    for change in changes:
        left, right = change.effective_from, change.effective_until
        result = []
        for low, high in windows:
            if (high is not None and high <= left) or (right is not None and right <= low):
                result.append((low, high))
                continue
            if low < left:
                result.append((low, left))
            if right is not None and (high is None or right < high):
                result.append((right, high))
        windows = result
    return tuple(_replace(scope, effective_from=low if low != start else scope.effective_from,
                          effective_until=high) for low, high in windows)


def publish_reconciliation(
    repository: KnowledgeRepository,
    journal: ReviewJournal,
    *,
    predecessor_id: str,
    published_at: datetime,
    through_decision_id: str,
    read_blob: Callable[[str], bytes],
) -> KnowledgeRelease:
    """Project one journal cutoff into a successor and atomically publish it.

    The repository authorizer verifies publisher permission and the generated
    exact-record attestations. The caller supplies missing raw/text blobs.
    """
    if published_at.utcoffset() is None:
        raise ValueError('Publication requires an aware timestamp')
    plan = journal.plan
    if plan.baseline_release_id is None:
        raise ValueError('Publication requires a pinned reconciliation baseline')
    baseline, base_records = repository.graph(plan.baseline_release_id)
    predecessor, prior_records = repository.graph(predecessor_id)
    if predecessor.published_at >= published_at:
        raise ValueError('Predecessor must predate publication')
    actual = {r.id: r for r in base_records
              if isinstance(r, GuidanceVersion) and r.status == 'approved_guidance'}
    if actual != {entry.guidance.id: entry.guidance for entry in plan.baseline}:
        raise ValueError('Plan guidance differs from its pinned baseline')

    plan_digest = _digest(_wire(plan))
    history = journal.history()
    positions = {decision.id: index for index, decision in enumerate(history)}
    if through_decision_id not in positions:
        raise ValueError('Unknown publication review cutoff')
    cutoff = positions[through_decision_id]
    if history[cutoff].decided_at > published_at:
        raise ValueError('Publication cannot predate review')

    prior_cutoff = -1
    if predecessor.id != baseline.id:
        prefix = _RECEIPT + f'{plan_digest} {predecessor.previous_release_id} '
        cursors = {r.rationale[len(prefix):].split(' ', 1)[0] for r in prior_records
                   if isinstance(r, ReviewDecision) and r.rationale.startswith(prefix)}
        if len(cursors) != 1 or next(iter(cursors)) not in positions:
            raise ValueError('Predecessor belongs to a different publication lineage')
        prior_cutoff = positions[cursors.pop()]
    if cutoff <= prior_cutoff:
        raise ValueError('Review cutoff must advance predecessor history')

    proposals = {proposal.id: proposal for proposal in plan.proposals}
    prior = {d.proposal_id: d for d in history[:prior_cutoff + 1]}
    latest = {d.proposal_id: d for d in history[:cutoff + 1]}
    active = {key: d for key, d in latest.items() if d.action == 'approve'}
    prior_active = {key: d for key, d in prior.items() if d.action == 'approve'}
    if active == prior_active:
        raise ValueError('No reviewed guidance changes to publish')

    records = {r.id: r for r in base_records}
    for record in prior_records:
        if isinstance(record, RuleBinding) and record.status != 'active_rule':
            records[record.id] = record

    def add(record):
        if records.setdefault(record.id, record) != record:
            raise ValueError(f'Publication record collision for {record.id}')

    attestations = {}

    def attest(subject, decision, action='approve_guidance'):
        previous = attestations.get(subject.id)
        if previous is None or (decision.decided_at, decision.id) > (
                previous[1].decided_at, previous[1].id):
            attestations[subject.id] = (subject, decision, action)

    def add_review(subject, decision, action):
        digest = sha256(record_bytes(subject))
        rationale = (_RECEIPT + f'{plan_digest} {predecessor.id} '
                     f'{through_decision_id} {decision.proposal_id} {_digest(_wire(decision))} '
                     f'{decision.rationale}')
        review_id = 'k05-' + _digest([digest, rationale, action])
        add(ReviewDecision(id=review_id, subject_id=subject.id, subject_sha256=digest,
                           reviewer_id=decision.reviewer, decided_at=decision.decided_at,
                           decision=action, rationale=rationale))

    def emit(recommendation, scope, claims, decision):
        claim_ids = []
        for statement, evidence in claims:
            # Hash the exact scoped payload so segment and replacement IDs are stable.
            claim_id = 'claim:k05-' + _digest([statement, _wire(scope),
                                              [_wire(span) for span in evidence]]) + ':1'
            add(KnowledgeClaim(id=claim_id, statement=statement, scope=scope,
                               status='corroborated', evidence=evidence))
            claim_ids.append(claim_id)
        claim_ids = tuple(sorted(set(claim_ids)))
        guide_id = 'guide:k05-' + _digest([recommendation, _wire(scope), claim_ids]) + ':1'
        guide = GuidanceVersion(id=guide_id, recommendation=recommendation, scope=scope,
                                claim_ids=claim_ids, status='approved_guidance')
        add(guide)
        attest(guide, decision)

    for key in active:
        item = proposals[key].observation
        for entry in plan.baseline:
            scope = entry.guidance.scope
            if (entry.meaning.topic == item.meaning.topic
                    and entry.meaning.page_type is not None
                    and entry.meaning.page_type != item.meaning.page_type
                    and scope != _scope(scope, entry.meaning.page_type)
                    and _overlaps(_dimensions(scope, entry.meaning),
                                  _dimensions(item.claim.scope, item.meaning),
                                  scope, item.claim.scope,
                                  ignore_page_type=True)):
                raise ValueError('Baseline page-type scope must be encoded before publishing adjacent guidance')

    replacements = defaultdict(list)
    support = defaultdict(list)
    for key, decision in sorted(active.items()):
        proposal = proposals[key]
        item = proposal.observation
        for citation in proposal.citations:
            add(citation.source)
            add(citation.snapshot)
        evidence = tuple(sorted({citation.span for citation in proposal.citations},
                                key=lambda span: canonical_bytes(_wire(span))))
        claim = (item.claim.statement, evidence)
        for target in proposal.supports:
            support[target].append((decision, claim))
        if item.target:
            replacements[item.target].append((decision, item))
        if not proposal.supports and item.change not in {'deprecate', 'retract'}:
            emit(item.claim.statement, _scope(item.claim.scope, item.meaning.page_type),
                 (claim,), decision)

    reversed_targets = defaultdict(list)
    for key in prior_active.keys() - active.keys():
        decision = next((d for d in reversed(history[prior_cutoff + 1:cutoff + 1])
                         if d.proposal_id == key and d.action == 'reverse'), None)
        if decision is None:
            raise ValueError('Removing published approval requires an explicit reversal')
        reversed_targets[proposals[key].observation.target].append(decision)

    for entry in plan.baseline:
        guide = entry.guidance
        changes = replacements[guide.id]
        enrichment = support[guide.id]
        reversals = reversed_targets[guide.id]
        if not (changes or enrichment or reversals):
            continue
        decisions = [d for d, _ in changes + enrichment] + reversals
        decision = max(decisions, key=lambda d: (d.decided_at, d.id))
        if not (changes or enrichment):
            # The reversal restores the exact earlier guidance bytes.
            attest(guide, decision)
            continue
        status = 'retracted' if any(item.change == 'retract' for _, item in changes) else 'superseded'
        retired = _replace(guide, status=status)
        records[guide.id] = retired
        attest(retired, decision, 'retract' if status == 'retracted' else 'approve_guidance')
        claims = [(records[key].statement, records[key].evidence) for key in guide.claim_ids]
        claims.extend(claim for _, claim in enrichment)
        for scope in _remaining(guide.scope, [item.claim.scope for _, item in changes]):
            emit(guide.recommendation, _scope(scope, entry.meaning.page_type), claims, decision)
        for record in tuple(records.values()):
            if isinstance(record, RuleBinding) and record.guidance_id == guide.id and record.status == 'active_rule':
                records[record.id] = _replace(record, status='superseded')

    # Historical attestations remain pinned in the predecessor. Replace subject
    # attestations in this graph to avoid ambiguous timestamp ties.
    records = {key: record for key, record in records.items()
               if not isinstance(record, ReviewDecision) or record.subject_id not in attestations}
    for subject, decision, action in attestations.values():
        add_review(subject, decision, action)

    fields = dict(schema_version=1, published_at=published_at,
                  previous_release_id=predecessor.id,
                  members=[dict(id=key, sha256=sha256(record_bytes(record)))
                           for key, record in sorted(records.items())])
    # Normalize datetime exactly as the frozen manifest model does.
    fields['published_at'] = TypeAdapter(AwareDatetime).dump_python(published_at, mode='json')
    release = KnowledgeRelease(id='rel:' + _digest(fields), **fields)

    def evidence_blob(digest):
        try:
            return repository.read_blob(digest)
        except KeyError:
            return read_blob(digest)

    repository.publish(release, records.values(), evidence_blob)
    return release
