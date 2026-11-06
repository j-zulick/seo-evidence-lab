import json
import sqlite3
from pathlib import Path

import pytest

from seo_qa.knowledge import contracts as c
from seo_qa.knowledge.archive import ReleaseArchive
from seo_qa.knowledge.policy import validate_audit_pin, validate_release, validate_transition


ENTITY_TYPES = (
    c.Source, c.SourceSnapshot, c.KnowledgeClaim, c.GuidanceVersion,
    c.KnowledgeRelease, c.RuleBinding, c.ChangeEvent, c.ReviewDecision,
    c.RetrievalResult, c.AuditImpact,
)


@pytest.fixture
def fixture():
    path = Path(__file__).resolve().parents[1] / 'fixtures/knowledge/v1.json'
    data = json.loads(path.read_text())
    types = {kind.__name__: kind for kind in ENTITY_TYPES}
    records = [types[item['entity']].model_validate(item['value']) for item in data['records']]
    return records, {digest: text.encode() for digest, text in data['blobs'].items()}


def manifest(records, **changes):
    fields = dict(schema_version=1, published_at='2026-11-03T12:00:00Z',
                  previous_release_id=None,
                  members=[dict(id=r.id, sha256=c.sha256(c.record_bytes(r)))
                           for r in sorted(records, key=lambda r: r.id)])
    fields.update(changes)
    return c.KnowledgeRelease(id='rel:' + c.sha256(c.canonical_bytes(fields)), **fields)


def published(fixture):
    records, blobs = fixture
    release = next(r for r in records if isinstance(r, c.KnowledgeRelease))
    ids = {pin.id for pin in release.members}
    return release, [r for r in records if getattr(r, 'id', None) in ids], blobs


def test_all_contracts_round_trip_and_reject_unknown_fields(fixture):
    records, _ = fixture
    assert {type(r) for r in records} == set(ENTITY_TYPES)
    for record in records:
        kind = type(record)
        assert kind.model_validate_json(c.record_bytes(record)) == record
        assert kind.model_json_schema()['additionalProperties'] is False
        fields = record.model_dump(mode='json')
        with pytest.raises(ValueError):
            kind.model_validate({**fields, 'unexpected': True})
        with pytest.raises(ValueError):
            kind.model_validate({**fields, 'schema_version': 2})


def test_publication_and_historical_replay(fixture, tmp_path):
    release, records, blobs = published(fixture)
    archive = ReleaseArchive(tmp_path / 'archive.sqlite')
    archive.publish(release, records, blobs.__getitem__)
    archive.publish(release, records, blobs.__getitem__)
    successor = manifest(records, previous_release_id=release.id, published_at='2026-11-04T12:00:00Z')
    archive.publish(successor, records, archive.read_blob)
    reopened = ReleaseArchive(archive.path)
    assert reopened.get_release(release.id) == release
    assert reopened.get_release(successor.id) == successor
    for pin in release.members:
        assert c.sha256(reopened.read_record(pin.sha256)) == pin.sha256
    for digest, data in blobs.items():
        if digest in {r.raw_sha256 for r in records if isinstance(r, c.SourceSnapshot)}:
            assert reopened.read_blob(digest) == data
    with sqlite3.connect(archive.path) as db:
        for statement in ('DELETE FROM objects', "UPDATE objects SET content=x'01'"):
            with pytest.raises(sqlite3.IntegrityError, match='append-only'):
                db.execute(statement)


def test_missing_predecessor_rolls_back(fixture, tmp_path):
    _, records, blobs = published(fixture)
    archive = ReleaseArchive(tmp_path / 'archive.sqlite')
    orphan = manifest(records, previous_release_id='rel:' + '0' * 64)
    with pytest.raises(KeyError):
        archive.publish(orphan, records, blobs.__getitem__)
    with pytest.raises(KeyError):
        archive.read_record(orphan.members[0].sha256)


def test_release_rejects_tampering_and_missing_blobs(fixture):
    release, records, blobs = published(fixture)
    with pytest.raises(ValueError, match='member mismatch'):
        validate_release(release, records[:-1], blobs.__getitem__)
    with pytest.raises(ValueError, match='hash mismatch'):
        validate_release(release, records, lambda _: b'tampered')
    with pytest.raises(ValueError, match='unavailable'):
        validate_release(release, records, {}.__getitem__)
    with pytest.raises(ValueError, match='identity'):
        c.KnowledgeRelease.model_validate({**release.model_dump(), 'id': 'rel:' + '0' * 64})


@pytest.mark.parametrize('family', ['conflict', 'missing', 'adversarial'])
def test_draft_never_enters_release(fixture, family):
    _, records, blobs = published(fixture)
    draft = next(r for r in fixture[0] if getattr(r, 'id', '') == f'claim:{family}:1')
    records.append(draft)
    with pytest.raises(ValueError, match='Draft claims'):
        validate_release(manifest(records), records, blobs.__getitem__)


def test_authority_is_required_for_corroboration(fixture):
    _, records, blobs = published(fixture)
    records = [c.Source.model_validate({**r.model_dump(), 'authority': 'secondary_reporting'})
               if isinstance(r, c.Source) else r for r in records]
    with pytest.raises(ValueError, match='authoritative evidence'):
        validate_release(manifest(records), records, blobs.__getitem__)


def test_approval_cannot_be_reused_after_edit(fixture):
    _, records, blobs = published(fixture)
    records = [c.GuidanceVersion.model_validate({**r.model_dump(), 'recommendation': 'Changed.'})
               if isinstance(r, c.GuidanceVersion) else r for r in records]
    with pytest.raises(ValueError, match='exact record'):
        validate_release(manifest(records), records, blobs.__getitem__)


def test_evidence_quote_must_match_bytes(fixture):
    _, records, blobs = published(fixture)
    old = next(r for r in records if getattr(r, 'id', '') == 'claim:canonical:1')
    span = old.evidence[0]
    altered = c.EvidenceSpan.model_validate({**span.model_dump(),
                                          'quote': 'X' * (span.end_byte - span.start_byte)})
    records = [c.KnowledgeClaim.model_validate({**r.model_dump(), 'evidence': (altered,)})
               if r.id == old.id else r for r in records]
    with pytest.raises(ValueError, match='quote does not match'):
        validate_release(manifest(records), records, blobs.__getitem__)


def test_lifecycle_rejects_activation_jump(fixture):
    records, _ = fixture
    binding = next(r for r in records if isinstance(r, c.RuleBinding))
    event = next(r for r in records if isinstance(r, c.ChangeEvent))
    activation = c.ChangeEvent.model_validate({**event.model_dump(), 'subject_id': binding.id,
                                               'to_status': 'active_rule'})
    with pytest.raises(ValueError, match='transition'):
        validate_transition(activation, binding, None)
    event = c.ChangeEvent.model_validate({**activation.model_dump(),
                                        'from_status': 'candidate_rule_change'})
    validate_transition(event, binding, 'candidate_rule_change')


def test_audit_pin_has_no_latest_fallback(fixture):
    release, records, _ = published(fixture)
    binding = next(r for r in records if isinstance(r, c.RuleBinding))
    audit = next(r for r in fixture[0] if isinstance(r, c.AuditImpact))
    validate_audit_pin(audit, release, [binding])
    with pytest.raises(ValueError, match='release mismatch'):
        validate_audit_pin(audit, manifest(records, published_at='2026-11-04T12:00:00Z'), [binding])
    with pytest.raises(ValueError, match='selection mismatch'):
        validate_audit_pin(audit, release, [])
    with pytest.raises(ValueError, match='bundle mismatch'):
        altered = c.AuditImpact.model_validate({**audit.model_dump(), 'rule_bundle_sha256': '0' * 64})
        validate_audit_pin(altered, release, [binding])
    c.AuditImpact(run_id='legacy-run', mode='legacy', evaluated_on='2026-11-03')
