"""Frozen v1 wire contracts for Best Practices Intelligence."""
from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

Key = Annotated[str, Field(pattern=r'^[a-z][a-z0-9]*(?:[-_.][a-z0-9]+)*$', max_length=120)]
Digest = Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
Text = Annotated[str, Field(min_length=1)]
Offset = Annotated[int, Field(strict=True, ge=0)]
SourceID = Annotated[str, Field(pattern=r'^src:[a-f0-9]{64}$')]
SnapshotID = Annotated[str, Field(pattern=r'^snap:[a-f0-9]{64}$')]
ClaimID = Annotated[str, Field(pattern=r'^claim:[a-z][a-z0-9]*(?:[-_.][a-z0-9]+)*:[1-9][0-9]*$')]
GuidanceID = Annotated[str, Field(pattern=r'^guide:[a-z][a-z0-9]*(?:[-_.][a-z0-9]+)*:[1-9][0-9]*$')]
BindingID = Annotated[str, Field(pattern=r'^bind:[a-z][a-z0-9]*(?:[-_.][a-z0-9]+)*:[1-9][0-9]*$')]
ReleaseID = Annotated[str, Field(pattern=r'^rel:[a-f0-9]{64}$')]
Authority = Literal['official_primary', 'standards_primary', 'independent_research',
                    'secondary_reporting', 'untrusted']
Status = Literal['discovered', 'extracted', 'corroborated', 'needs_review',
                 'approved_guidance', 'candidate_rule_change', 'active_rule',
                 'superseded', 'retracted']
Check = Literal['canonical', 'indexable', 'crawlable', 'redirect_final',
                'max_redirects', 'allowed_statuses', 'required_text']


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_bytes(data) -> bytes:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


class Wire(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, revalidate_instances='always')


class Entity(Wire):
    schema_version: Literal[1] = 1


class Applicability(Wire):
    provider: Key | None = None
    search_engine: Key | None = None
    jurisdiction: Annotated[str, Field(pattern=r'^[A-Z]{2}(?:-[A-Z0-9]{1,3})?$')] | None = None
    platform_version: Text | None = None
    effective_from: date | None = None
    effective_until: date | None = None
    required_conditions: tuple[Key, ...] = ()

    @model_validator(mode='after')
    def valid_window(self):
        if self.effective_from and self.effective_until and self.effective_until <= self.effective_from:
            raise ValueError('Effective end must follow start')
        if self.required_conditions != tuple(sorted(set(self.required_conditions))):
            raise ValueError('Conditions must be sorted and unique')
        return self


class Source(Entity):
    id: SourceID
    canonical_url: Text
    publisher: Key
    authority: Authority
    status: Literal['discovered', 'retracted'] = 'discovered'

    @field_validator('canonical_url')
    @classmethod
    def canonical_https(cls, url):
        if any(ord(char) < 33 or ord(char) >= 127 for char in url) or '\\' in url or '#' in url:
            raise ValueError('Use canonical ASCII HTTPS URLs')
        parts = urlsplit(url)
        if (parts.scheme != 'https' or not parts.hostname or parts.netloc != parts.hostname
                or not parts.path.startswith('/') or url != parts.geturl()):
            raise ValueError('Use HTTPS with lowercase host, explicit path and no credentials or port')
        return url

    @model_validator(mode='after')
    def identity(self):
        if self.id != 'src:' + sha256(self.canonical_url.encode('ascii')):
            raise ValueError('Source ID does not match URL')
        return self


class SourceSnapshot(Entity):
    id: SnapshotID
    source_id: SourceID
    captured_at: AwareDatetime
    availability: Literal['captured', 'missing', 'blocked']
    raw_sha256: Digest | None = None
    text_sha256: Digest | None = None
    extractor: Key | None = None

    @model_validator(mode='after')
    def identity(self):
        fields = (self.raw_sha256, self.text_sha256, self.extractor)
        if self.availability == 'captured':
            if any(value is None for value in fields):
                raise ValueError('Capture requires raw hash, text hash and extractor revision')
        elif any(value is not None for value in fields):
            raise ValueError('Unavailable captures cannot supply hashes')
        payload = self.model_dump(mode='json', exclude={'id'})
        if self.id != 'snap:' + sha256(canonical_bytes(payload)):
            raise ValueError('Snapshot ID does not match metadata')
        return self


class EvidenceSpan(Wire):
    snapshot_id: SnapshotID
    start_byte: Offset
    end_byte: Offset
    quote: Text

    @model_validator(mode='after')
    def valid_length(self):
        if self.end_byte - self.start_byte != len(self.quote.encode('utf-8')):
            raise ValueError('Evidence span length must match UTF-8 quote')
        return self


class KnowledgeClaim(Entity):
    id: ClaimID
    statement: Text
    scope: Applicability
    status: Literal['extracted', 'corroborated', 'needs_review', 'superseded', 'retracted']
    evidence: tuple[EvidenceSpan, ...] = ()
    conflicts_with: tuple[ClaimID, ...] = ()
    supersedes: ClaimID | None = None

    @model_validator(mode='after')
    def relations(self):
        if self.id in self.conflicts_with or self.id == self.supersedes:
            raise ValueError('Claim cannot relate to itself')
        if self.status == 'corroborated' and (not self.evidence or self.conflicts_with):
            raise ValueError('Corroboration requires evidence and resolved conflicts')
        if self.supersedes:
            family, revision = self.id.rsplit(':', 1)
            previous_family, previous_revision = self.supersedes.rsplit(':', 1)
            if family != previous_family or int(previous_revision) >= int(revision):
                raise ValueError('Supersession requires an older revision in the same family')
        return self


class ReviewDecision(Entity):
    id: Key
    subject_id: ClaimID | GuidanceID | BindingID
    subject_sha256: Digest
    reviewer_id: Key
    decided_at: AwareDatetime
    decision: Literal['approve_guidance', 'approve_rule', 'request_changes', 'reject', 'retract']
    rationale: Text

    @model_validator(mode='after')
    def subject_kind(self):
        prefix = {'approve_guidance': 'guide:', 'approve_rule': 'bind:'}.get(self.decision)
        if prefix and not self.subject_id.startswith(prefix):
            raise ValueError('Approval decision has the wrong subject type')
        return self


class GuidanceVersion(Entity):
    id: GuidanceID
    recommendation: Text
    scope: Applicability
    claim_ids: Annotated[tuple[ClaimID, ...], Field(min_length=1)]
    status: Literal['needs_review', 'approved_guidance', 'superseded', 'retracted']


class RuleBinding(Entity):
    id: BindingID
    guidance_id: GuidanceID
    check: Check
    implementation_revision: Key
    implementation_sha256: Digest
    status: Literal['candidate_rule_change', 'active_rule', 'superseded', 'retracted']


class ChangeEvent(Entity):
    id: Key
    subject_id: SourceID | ClaimID | GuidanceID | BindingID
    actor_id: Key
    occurred_at: AwareDatetime
    from_status: Status | None
    to_status: Status
    reason: Text


class MemberPin(Wire):
    id: SourceID | SnapshotID | ClaimID | GuidanceID | BindingID | Key
    sha256: Digest


class KnowledgeRelease(Entity):
    id: ReleaseID
    published_at: AwareDatetime
    previous_release_id: ReleaseID | None = None
    members: Annotated[tuple[MemberPin, ...], Field(min_length=1)]

    @model_validator(mode='after')
    def identity(self):
        ids = tuple(pin.id for pin in self.members)
        if ids != tuple(sorted(set(ids))):
            raise ValueError('Release members must be sorted and unique')
        payload = self.model_dump(mode='json', exclude={'id'})
        if self.id != 'rel:' + sha256(canonical_bytes(payload)) or self.id == self.previous_release_id:
            raise ValueError('Invalid release identity')
        return self


class RetrievalResult(Entity):
    release_id: ReleaseID
    evaluated_on: date
    outcome: Literal['matched', 'no_match', 'unavailable']
    guidance_ids: tuple[GuidanceID, ...] = ()
    limitations: tuple[Text, ...] = ()

    @model_validator(mode='after')
    def result_shape(self):
        if (self.outcome == 'matched') != bool(self.guidance_ids):
            raise ValueError('Only matched results carry guidance')
        if self.outcome == 'unavailable' and not self.limitations:
            raise ValueError('Unavailable retrieval requires a limitation')
        return self


class AuditImpact(Entity):
    run_id: Text
    evaluated_on: date
    release_id: ReleaseID | None = None
    rule_bundle_sha256: Digest | None = None
    binding_ids: tuple[BindingID, ...] = ()
    mode: Literal['legacy', 'advisory', 'bound']

    @model_validator(mode='after')
    def complete_pin(self):
        if self.binding_ids != tuple(sorted(set(self.binding_ids))):
            raise ValueError('Audit bindings must be sorted and unique')
        if (self.mode == 'legacy') != (self.release_id is None):
            raise ValueError('Release is required for nonlegacy audits only')
        if (self.mode == 'bound') != bool(self.rule_bundle_sha256 and self.binding_ids):
            raise ValueError('Bound audits require executable bindings and bundle hash')
        if self.mode != 'bound' and (self.rule_bundle_sha256 or self.binding_ids):
            raise ValueError('Legacy and advisory audits cannot carry executable pins')
        return self


def record_bytes(record: Entity) -> bytes:
    return canonical_bytes(record.model_dump(mode='json'))
