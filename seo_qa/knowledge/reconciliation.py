"""Offline K05 reconciliation and reviewer-controlled working history."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from contextlib import closing
from datetime import date, datetime
from itertools import combinations
from pathlib import Path
import sqlite3
from typing import Literal

from pydantic import AwareDatetime, model_validator

from .archive import ReleaseArchive
from .contracts import (
    Applicability, Digest, EvidenceSpan, GuidanceID, GuidanceVersion, Key,
    KnowledgeClaim, ReleaseID, Source, SourceSnapshot, Text, Wire,
    canonical_bytes, sha256,
)
from .policy import PRIMARY

Change = Literal['assert', 'clarify', 'deprecate', 'supersede', 'retract', 'correct']


class Meaning(Wire):
    """A curated semantic key and canonical assertion value, including units."""

    topic: Key
    value: Text
    page_type: Key | None = None
    scope_known: bool = True


class Observation(Wire):
    claim: KnowledgeClaim
    meaning: Meaning
    observed_at: AwareDatetime
    change: Change = 'assert'
    target: GuidanceID | None = None

    @model_validator(mode='after')
    def explicit_target(self):
        if (self.change == 'assert') != (self.target is None):
            raise ValueError('Lifecycle observations require an explicit guidance target')
        if self.claim.status in {'retracted', 'superseded'}:
            raise ValueError('Inactive claims cannot be submitted as new observations')
        return self


class CurrentGuidance(Wire):
    guidance: GuidanceVersion
    meaning: Meaning


class Citation(Wire):
    source: Source
    snapshot: SourceSnapshot
    span: EvidenceSpan


class Proposal(Wire):
    id: Digest
    observations: tuple[Observation, ...]
    citations: tuple[Citation, ...]
    corroborated: bool
    supports: tuple[GuidanceID, ...] = ()

    @property
    def observation(self):
        return self.observations[0]


class Conflict(Wire):
    left: Digest
    right: Digest | GuidanceID
    reason: Literal['contradiction', 'ambiguous_applicability', 'competing_changes']


class Plan(Wire):
    baseline_release_id: ReleaseID | None = None
    baseline: tuple[CurrentGuidance, ...]
    proposals: tuple[Proposal, ...]
    conflicts: tuple[Conflict, ...]


def released_guidance(
    archive: ReleaseArchive, release_id: str, meanings: dict[str, Meaning],
) -> tuple[CurrentGuidance, ...]:
    """Load approved baseline versions by exact release and record digest."""
    result = []
    for pin in archive.get_release(release_id).members:
        if pin.id.startswith('guide:'):
            guidance = GuidanceVersion.model_validate_json(archive.read_record(pin.sha256))
            if guidance.id != pin.id:
                raise ValueError('Archived guidance ID mismatch')
            if guidance.status == 'approved_guidance':
                result.append(CurrentGuidance(guidance=guidance, meaning=meanings[pin.id]))
    return tuple(result)


def _bytes(model):
    return canonical_bytes(model.model_dump(mode='json'))


def _dimensions(scope, meaning):
    return (scope.provider, scope.search_engine, scope.jurisdiction,
            scope.platform_version, meaning.page_type, scope.required_conditions)


def _overlaps(first, second, a: Applicability, b: Applicability, *, ignore_page_type=False):
    if ((a.effective_until and b.effective_from and a.effective_until <= b.effective_from)
            or (b.effective_until and a.effective_from and b.effective_until <= a.effective_from)):
        return False
    for index, (left, right) in enumerate(zip(first[:-1], second[:-1])):
        if (index == 4 and ignore_page_type) or left is None or right is None or left == right:
            continue
        if index == 2 and left.split('-')[0] == right.split('-')[0] and (
                '-' not in left or '-' not in right):
            continue
        return False
    # Required conditions are positive predicates and can hold together.
    return True


def _unique(records):
    by_id = {}
    for record in records:
        if by_id.setdefault(record.id, record) != record:
            raise ValueError(f'Conflicting inputs for {record.id}')
    return by_id


def _citations(observations, sources, snapshots, read_blob):
    citations = set()
    for observation in observations:
        for span in observation.claim.evidence:
            snapshot = snapshots.get(span.snapshot_id)
            if (snapshot is None or snapshot.availability != 'captured'
                    or snapshot.captured_at > observation.observed_at
                    or snapshot.source_id not in sources):
                raise ValueError('Evidence requires a known, timely captured source')
            data = read_blob(snapshot.text_sha256)
            if (sha256(data) != snapshot.text_sha256
                    or data[span.start_byte:span.end_byte] != span.quote.encode('utf-8')):
                raise ValueError('Evidence does not match captured bytes')
            citations.add(Citation(source=sources[snapshot.source_id], snapshot=snapshot, span=span))
    return tuple(sorted(citations, key=_bytes))


def _corroborated(citations):
    eligible = [item for item in citations if item.source.status != 'retracted']
    if any(item.source.authority in PRIMARY for item in eligible):
        return True
    research = [item for item in eligible if item.source.authority == 'independent_research']
    return any(a.source.publisher != b.source.publisher
               and a.snapshot.text_sha256 != b.snapshot.text_sha256
               for a, b in combinations(research, 2))


def reconcile(
    observations: Iterable[Observation],
    current: Iterable[CurrentGuidance],
    sources: Iterable[Source],
    snapshots: Iterable[SourceSnapshot],
    read_blob: Callable[[str], bytes],
    *,
    baseline_release_id: ReleaseID | None = None,
) -> Plan:
    """Create deterministic proposals from curated semantics and verified evidence."""
    baseline = {}
    for entry in current:
        guide = entry.guidance
        if guide.status != 'approved_guidance' or baseline.setdefault(guide.id, entry) != entry:
            raise ValueError('Baseline must contain distinct approved guidance')
    sources, snapshots = _unique(sources), _unique(snapshots)
    claims = {}
    groups = defaultdict(set)
    for item in observations:
        if claims.setdefault(item.claim.id, item) != item:
            raise ValueError('Claim ID reused with different content')
        if item.target:
            target = baseline.get(item.target)
            if (target is None or target.meaning.topic != item.meaning.topic
                    or not target.meaning.scope_known
                    or _dimensions(target.guidance.scope, target.meaning)
                    != _dimensions(item.claim.scope, item.meaning)):
                raise ValueError('Lifecycle target must match topic and applicability')
            if item.change == 'clarify' and target.meaning.value != item.meaning.value:
                raise ValueError('Clarification must preserve the assertion value')
            if item.claim.supersedes and item.claim.supersedes not in target.guidance.claim_ids:
                raise ValueError('Claim supersession must agree with the guidance target')
            if not item.claim.scope.effective_from:
                raise ValueError('Lifecycle change requires an explicit effective start')

    known_claims = set(claims)
    for entry in baseline.values():
        known_claims.update(entry.guidance.claim_ids)
    for item in claims.values():
        if set(item.claim.conflicts_with) - known_claims:
            raise ValueError('Explicit claim conflicts require known counterparts')
        if item.claim.supersedes and not item.target:
            raise ValueError('Claim supersession requires an explicit guidance target')

    disputed = {key for item in claims.values() for key in item.claim.conflicts_with}
    disputed.update(item.claim.id for item in claims.values() if item.claim.conflicts_with)
    for item in claims.values():
        separate = item.claim.id if item.claim.id in disputed else None
        groups[item.meaning, item.claim.scope, item.change, item.target, separate].add(item)

    proposals = []
    for group in groups.values():
        items = tuple(sorted(group, key=_bytes))
        citations = _citations(items, sources, snapshots, read_blob)
        item = items[0]
        supports = tuple(sorted(key for key, entry in baseline.items()
                                if item.change == 'assert' and item.meaning == entry.meaning
                                and item.claim.scope == entry.guidance.scope))
        payload = dict(observations=items, citations=citations,
                       corroborated=_corroborated(citations), supports=supports)
        body = dict(observations=[x.model_dump(mode='json') for x in items],
                    citations=[x.model_dump(mode='json') for x in citations],
                    corroborated=payload['corroborated'], supports=supports)
        proposals.append(Proposal(id=sha256(canonical_bytes(body)), **payload))
    proposals.sort(key=lambda item: item.id)

    conflicts = []
    for index, proposal in enumerate(proposals):
        item = proposal.observation
        claim_ids = {entry.claim.id for entry in proposal.observations}
        declared = {key for entry in proposal.observations for key in entry.claim.conflicts_with}
        others = [(key, entry.meaning, entry.guidance.scope, None,
                   set(entry.guidance.claim_ids), set())
                  for key, entry in sorted(baseline.items()) if key != item.target]
        others.extend((other.id, other.observation.meaning, other.observation.claim.scope,
                       other.observation.target,
                       {entry.claim.id for entry in other.observations},
                       {key for entry in other.observations for key in entry.claim.conflicts_with})
                      for other in proposals[index + 1:])
        for key, meaning, scope, target, other_ids, other_declared in others:
            if declared & other_ids or other_declared & claim_ids:
                conflicts.append(Conflict(left=proposal.id, right=key, reason='contradiction'))
                continue
            if meaning.topic != item.meaning.topic:
                continue
            if not _overlaps(_dimensions(item.claim.scope, item.meaning),
                             _dimensions(scope, meaning), item.claim.scope, scope):
                continue
            if item.target and item.target == target:
                reason = 'competing_changes'
            elif not item.meaning.scope_known or not meaning.scope_known:
                reason = 'ambiguous_applicability'
            elif item.meaning.value != meaning.value:
                reason = 'contradiction'
            else:
                continue
            conflicts.append(Conflict(left=proposal.id, right=key, reason=reason))
    return Plan(baseline_release_id=baseline_release_id,
                baseline=tuple(baseline[key] for key in sorted(baseline)),
                proposals=tuple(proposals), conflicts=tuple(conflicts))


class Decision(Wire):
    id: Key
    proposal_id: Digest
    reviewer: Key
    decided_at: AwareDatetime
    action: Literal['approve', 'reject', 'request_changes', 'reverse']
    rationale: Text


class WorkingView(Wire):
    guidance: tuple[CurrentGuidance, ...]
    additions: tuple[Proposal, ...]
    supporting_evidence: tuple[Proposal, ...]


def _effective(scope, day):
    return ((scope.effective_from is None or scope.effective_from <= day)
            and (scope.effective_until is None or day < scope.effective_until))


class ReviewJournal:
    """Durable, append-only decisions for a single immutable reconciliation plan."""

    def __init__(self, path: Path, plan: Plan, can_review: Callable[[str], bool]):
        self.path = path
        self.plan = plan
        self.can_review = can_review
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS plan (slot INTEGER PRIMARY KEY, body BLOB NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS decisions (id TEXT PRIMARY KEY, body BLOB NOT NULL)')
            for table in ('plan', 'decisions'):
                for operation in ('UPDATE', 'DELETE'):
                    db.execute(f"""CREATE TRIGGER IF NOT EXISTS protect_{table}_{operation}
                        BEFORE {operation} ON {table} BEGIN
                        SELECT RAISE(ABORT, 'Reconciliation history is append-only'); END""")
            db.execute('INSERT OR IGNORE INTO plan VALUES (1, ?)', (_bytes(plan),))
            if db.execute('SELECT body FROM plan WHERE slot=1').fetchone()[0] != _bytes(plan):
                raise ValueError('Journal already belongs to a different plan')

    def _connect(self):
        db = sqlite3.connect(self.path)
        db.execute('PRAGMA recursive_triggers = ON')
        return db

    @staticmethod
    def _history(db):
        return tuple(Decision.model_validate_json(row[0]) for row in
                     db.execute('SELECT body FROM decisions ORDER BY rowid'))

    def history(self) -> tuple[Decision, ...]:
        with closing(self._connect()) as db, db:
            db.execute('BEGIN')
            if db.execute('SELECT body FROM plan WHERE slot=1').fetchone()[0] != _bytes(self.plan):
                raise ValueError('Journal already belongs to a different plan')
            return self._history(db)

    def record(self, decision: Decision) -> None:
        if not self.can_review(decision.reviewer):
            raise ValueError('Reviewer is not authorized')
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            history = self._history(db)
            for previous in history:
                if previous.id == decision.id:
                    if previous != decision:
                        raise ValueError('Decision ID collision')
                    return
            if history and decision.decided_at < history[-1].decided_at:
                raise ValueError('Decision cannot backdate journal history')
            proposals = {item.id: item for item in self.plan.proposals}
            proposal = proposals.get(decision.proposal_id)
            if proposal is None:
                raise ValueError('Unknown proposal')
            if any(item.observed_at > decision.decided_at for item in proposal.observations):
                raise ValueError('Review predates its evidence')
            latest = {item.proposal_id: item.action for item in history}
            active = latest.get(proposal.id) == 'approve'
            if active != (decision.action == 'reverse'):
                raise ValueError('Only reversal can end an active approval')
            if decision.action == 'reverse' and proposal.observation.change != 'supersede':
                raise ValueError('Reversal requires an approved supersession')
            if decision.action == 'approve':
                if not proposal.corroborated or not proposal.observation.meaning.scope_known:
                    raise ValueError('Approval requires corroboration and known applicability')
                for conflict in self.plan.conflicts:
                    if proposal.id in (conflict.left, conflict.right):
                        other = conflict.right if conflict.left == proposal.id else conflict.left
                        if latest.get(other) != 'reject':
                            raise ValueError('Resolve competing evidence before approval')
            db.execute('INSERT INTO decisions VALUES (?, ?)', (decision.id, _bytes(decision)))

    def view(self, effective_on: date, *, known_at: datetime | None = None) -> WorkingView:
        """Evaluate business dates independently of the reviewer knowledge cutoff."""
        if known_at is not None and known_at.utcoffset() is None:
            raise ValueError('Knowledge cutoff must include a timezone')
        latest = {item.proposal_id: item.action for item in self.history()
                  if known_at is None or item.decided_at <= known_at}
        approved = tuple(proposal for proposal in self.plan.proposals
                         if latest.get(proposal.id) == 'approve'
                         and _effective(proposal.observation.claim.scope, effective_on))
        displaced = {proposal.observation.target for proposal in approved}
        return WorkingView(
            guidance=tuple(entry for entry in self.plan.baseline
                           if entry.guidance.id not in displaced
                           and _effective(entry.guidance.scope, effective_on)),
            additions=tuple(proposal for proposal in approved
                            if not proposal.supports
                            and proposal.observation.change not in {'deprecate', 'retract'}),
            supporting_evidence=tuple(proposal for proposal in approved
                                      if set(proposal.supports) - displaced),
        )
