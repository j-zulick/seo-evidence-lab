"""Source-specific, digest-bound freshness assessment. No collection or scheduling."""
from dataclasses import dataclass
from datetime import datetime
import math

from .contracts import GuidanceVersion, KnowledgeClaim, Source, SourceSnapshot
from .retrieval import _applies


def aware(value):
    if value is not None and (not isinstance(value, datetime) or value.tzinfo is None
                              or value.utcoffset() is None):
        raise ValueError('Timezone-aware timestamps required')
    return value


@dataclass(frozen=True)
class SourceFreshnessPolicy:
    source_id: str
    max_check_age_seconds: float
    authority_topics: tuple[str, ...] = ()
    max_publication_age_seconds: float | None = None
    required: bool = True

    def __post_init__(self):
        if self.max_check_age_seconds is None or not isinstance(self.source_id, str):
            raise ValueError('Required source age and identity missing')
        for age in (self.max_check_age_seconds, self.max_publication_age_seconds):
            if age is not None and (type(age) not in (int, float) or not math.isfinite(age) or age <= 0):
                raise ValueError('Positive finite source age policy required')
        if type(self.required) is not bool:
            raise ValueError('Explicit source coverage policy required')


@dataclass(frozen=True)
class SourceCheck:
    source_id: str
    latest_outcome: str
    attempted_at: datetime
    fetched_at: datetime | None = None
    last_successful_check_at: datetime | None = None
    snapshot_id: str | None = None
    raw_sha256: str | None = None
    text_sha256: str | None = None

    def __post_init__(self):
        if self.attempted_at is None:
            raise ValueError('Source check attempt time required')
        for value in (self.attempted_at, self.fetched_at, self.last_successful_check_at):
            aware(value)
        if self.latest_outcome not in {'captured', 'unchanged', 'not_modified', 'blocked', 'failed', 'missing'}:
            raise ValueError('Unknown source check outcome')


@dataclass(frozen=True)
class SourcePublication:
    snapshot_id: str
    published_at: datetime | None = None
    modified_at: datetime | None = None
    expires_at: datetime | None = None
    authority_topic: str | None = None

    def __post_init__(self):
        for value in (self.published_at, self.modified_at, self.expires_at):
            aware(value)


def _index(items, key):
    indexed = {}
    for item in items:
        identity = getattr(item, key)
        if identity in indexed:
            raise ValueError('Ambiguous source freshness metadata')
        indexed[identity] = item
    return indexed


def assess_release(repository, context, *, evaluated_at, policies, checks=(), metadata=()):
    aware(evaluated_at)
    if evaluated_at is None:
        raise ValueError('Explicit assessment time required')
    policy_by_id = _index(policies, 'source_id')
    check_by_id = _index(checks, 'source_id')
    publication_by_id = _index(metadata, 'snapshot_id')
    base = dict(schema='knowledge-freshness/v1', evaluated_at=evaluated_at.isoformat(),
                can_claim_current_best_practices=False, executable_activation_allowed=False)
    try:
        release, records = repository.graph()
    except (OSError, ValueError, KeyError):
        return dict(**base, release_id=None, status='unavailable', source_coverage=[],
                    guidance=[], limitations=['Verified release unavailable'])
    by_id = {record.id: record for record in records}
    sources = {record.id: record for record in records if isinstance(record, Source)}
    assessments = {}
    coverage = []
    for snapshot in (record for record in records if isinstance(record, SourceSnapshot)):
        source = sources[snapshot.source_id]
        policy = policy_by_id.get(source.id)
        check = check_by_id.get(source.id)
        publication = publication_by_id.get(snapshot.id)
        limits = []
        if snapshot.availability != 'captured':
            limits.append('Source capture unavailable')
        if source.status == 'retracted':
            limits.append('Source retracted')
        if policy is None:
            limits.append('Source freshness policy unknown')
        if check is None or check.last_successful_check_at is None:
            limits.append('Successful source check unknown')
        else:
            if (check.snapshot_id, check.raw_sha256, check.text_sha256) != (
                    snapshot.id, snapshot.raw_sha256, snapshot.text_sha256):
                limits.append('Source check does not bind pinned evidence')
            if (check.attempted_at > evaluated_at or check.last_successful_check_at > check.attempted_at
                    or check.last_successful_check_at < snapshot.captured_at
                    or check.fetched_at is not None and check.fetched_at > check.last_successful_check_at):
                limits.append('Inconsistent source check timestamps')
            if check.latest_outcome not in {'captured', 'unchanged', 'not_modified'}:
                limits.append('Latest source check failed or unavailable')
            if policy and (evaluated_at - check.last_successful_check_at).total_seconds() > policy.max_check_age_seconds:
                limits.append('Source check stale')
        if snapshot.captured_at > evaluated_at:
            limits.append('Capture is future dated')
        if publication is None or publication.authority_topic is None:
            limits.append('Reviewed authority topic unknown')
        elif (source.authority not in {'official_primary', 'standards_primary'}
              or policy is None or publication.authority_topic not in policy.authority_topics):
            limits.append('Source is context only for this topic')
        if publication:
            for value in (publication.published_at, publication.modified_at):
                if value and value > evaluated_at:
                    limits.append('Publisher timestamp is future dated')
            if publication.expires_at and publication.expires_at <= evaluated_at:
                limits.append('Source publication expired')
            if policy and policy.max_publication_age_seconds is not None:
                if publication.published_at is None:
                    limits.append('Publisher date unknown')
                elif (evaluated_at - publication.published_at).total_seconds() > policy.max_publication_age_seconds:
                    limits.append('Source publication stale')
        detail = dict(source_id=source.id, snapshot_id=snapshot.id, authority=source.authority,
                      raw_sha256=snapshot.raw_sha256, text_sha256=snapshot.text_sha256,
                      status='current' if not limits else 'limited', limitations=sorted(set(limits)),
                      latest_outcome=check.latest_outcome if check else None,
                      attempted_at=check.attempted_at.isoformat() if check else None,
                      fetched_at=check.fetched_at.isoformat() if check and check.fetched_at else None,
                      last_successful_check_at=check.last_successful_check_at.isoformat()
                      if check and check.last_successful_check_at else None,
                      published_at=publication.published_at.isoformat() if publication and publication.published_at else None,
                      modified_at=publication.modified_at.isoformat() if publication and publication.modified_at else None,
                      expires_at=publication.expires_at.isoformat() if publication and publication.expires_at else None)
        assessments[snapshot.id] = detail
        coverage.append(detail)
    missing = sorted(key for key, policy in policy_by_id.items() if policy.required and key not in sources)
    guidance = []
    for guide in (record for record in records if isinstance(record, GuidanceVersion)):
        limits = []
        if guide.status != 'approved_guidance':
            limits.append('Guidance is ' + guide.status)
        if not _applies(guide.scope, context):
            limits.append('Guidance is outside known applicability or effective window')
        for claim_id in guide.claim_ids:
            claim = by_id[claim_id]
            if not isinstance(claim, KnowledgeClaim) or claim.status != 'corroborated' or claim.conflicts_with:
                limits.append('Claim is unresolved, superseded or withdrawn')
            for span in claim.evidence:
                limits.extend(assessments[span.snapshot_id]['limitations'])
        guidance.append(dict(guidance_id=guide.id, status='current' if not limits else 'limited',
                             effective_from=guide.scope.effective_from.isoformat() if guide.scope.effective_from else None,
                             effective_until=guide.scope.effective_until.isoformat() if guide.scope.effective_until else None,
                             limitations=sorted(set(limits))))
    limitations = []
    if context.evaluated_on > evaluated_at.date():
        limitations.append('Evaluation date is future dated at admission')
    if release.published_at > evaluated_at:
        limitations.append('Release publication is future dated')
    for source_id, policy in policy_by_id.items():
        if policy.required:
            covered = [item for item in coverage if item['source_id'] == source_id]
            if not covered or any(item['status'] != 'current' for item in covered):
                limitations.append('Required source coverage is missing or limited: ' + source_id)
    if missing:
        limitations.append('Required sources missing from release: ' + ', '.join(missing))
    if not guidance or any(item['status'] != 'current' for item in guidance):
        limitations.append('Current best practices are not established for all requested guidance')
    if not policy_by_id:
        limitations.append('Source coverage policy is unconfigured')
    current = not limitations
    base['can_claim_current_best_practices'] = current
    return dict(**base, release_id=release.id, status='current' if current else 'limited',
                source_coverage=coverage, guidance=guidance, limitations=limitations)


def encode_inputs(inputs):
    from dataclasses import asdict
    def wire(value):
        if isinstance(value, datetime):
            return aware(value).isoformat()
        if isinstance(value, (tuple, list)):
            return [wire(item) for item in value]
        if isinstance(value, dict):
            return {key: wire(item) for key, item in value.items()}
        return value
    return {key: [wire(asdict(item)) for item in inputs.get(key, ())]
            for key in ('policies', 'checks', 'metadata')}


def decode_inputs(value):
    if not isinstance(value, dict) or set(value) - {'policies', 'checks', 'metadata'}:
        raise ValueError('Invalid freshness evidence shape')
    types = dict(policies=SourceFreshnessPolicy, checks=SourceCheck, metadata=SourcePublication)
    result = {}
    for key, kind in types.items():
        rows = []
        items = value.get(key, ())
        if not isinstance(items, (list, tuple)) or len(items) > 10000:
            raise ValueError('Bounded freshness evidence list required')
        for raw in items:
            if not isinstance(raw, dict):
                raise ValueError('Freshness record must be an object')
            fields = dict(raw)
            for name, item in fields.items():
                if name.endswith('_at') and item is not None:
                    fields[name] = datetime.fromisoformat(item)
            if key == 'policies':
                fields['authority_topics'] = tuple(fields.get('authority_topics', ()))
            rows.append(kind(**fields))
        result[key] = tuple(rows)
    return result
