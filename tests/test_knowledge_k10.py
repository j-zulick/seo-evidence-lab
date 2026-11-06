from datetime import date, datetime, timezone
import sqlite3
import pytest
from seo_qa.fixtures import fixture_inspector, seed
from seo_qa.workflow import build_workflow
from seo_qa.knowledge.audit_pin import KnowledgeStore
from seo_qa.knowledge.capture import ApprovedSource, Fixtures, Ingestor
from seo_qa.knowledge.change_detection import SnapshotInput, compare_snapshots
from seo_qa.knowledge.change_impact import compare_releases
from seo_qa.knowledge.claims import extract_claims
from seo_qa.knowledge.contracts import Source, RuleBinding, ReviewDecision, SourceSnapshot, record_bytes, sha256, canonical_bytes
from seo_qa.knowledge.ingestion.http import Response, RetrievalError
from seo_qa.knowledge.publish import publish_reconciliation
from seo_qa.knowledge.reconciliation import Meaning, Observation, ReviewJournal, Decision, reconcile, released_guidance
from seo_qa.knowledge.repository import KnowledgeRepository
from seo_qa.knowledge.retrieval import Context
from seo_qa.knowledge.sources import SourceRegistry
from test_knowledge_k00 import fixture, published, manifest


def moment(day):
    return datetime(2026, 11, day, tzinfo=timezone.utc)


@pytest.fixture
def integrated(fixture, tmp_path):
    _, records, blobs = published(fixture)
    ids = {r.id for r in records if isinstance(r, RuleBinding)}
    records = [r for r in records if r.id not in ids and not
               (isinstance(r, ReviewDecision) and r.subject_id in ids)]
    root = manifest(records)
    repo = KnowledgeRepository(tmp_path / 'public.db', authorize=lambda *_: True)
    repo.publish(root, records, blobs.__getitem__)
    return repo, root, records, blobs, next(r for r in records if isinstance(r, Source))


def test_update_review_audit_rollback_privacy(integrated, tmp_path):
    repo, root, records, blobs, source = integrated
    old, new = b'Clients must send a header.', b'Clients must send a public header.'
    replies = [Response(200, (), old), Response(200, (), new), Response(200, (), new)]
    ingestor = Ingestor(tmp_path / 'capture.db', [ApprovedSource(source, 'text', min_interval=0)],
                        fixtures=Fixtures({source.canonical_url: replies}), observed_clock=lambda: moment(4))
    before, _, text, _ = ingestor.capture(source.id)
    after, _, text2, _ = ingestor.capture(source.id)
    assert ingestor.capture(source.id)[0] == after
    event, = compare_snapshots([SnapshotInput(source, before, text)], [SnapshotInput(source, after, text2)]).events
    candidate, = extract_claims([SnapshotInput(source, after, text2)], as_of=date(2026,11,4), change_event=event).candidates
    assert candidate.claim.status == 'needs_review'
    baseline = released_guidance(repo, root.id, {'guide:canonical:1': Meaning(topic='canonical', value='canonical')})
    plan = reconcile([Observation(claim=candidate.claim, meaning=Meaning(topic='header', value='public-header'), observed_at=moment(4))],
                     baseline, [source], [after], ingestor.store.read_blob, baseline_release_id=root.id)
    journal = ReviewJournal(tmp_path / 'journal.db', plan, lambda actor: actor == 'reviewer')
    store = KnowledgeStore(tmp_path / 'audit.db', repository=repo, context=Context(date(2026,11,6)))
    req = seed(store)[0]
    workflow = build_workflow(store, fixture_inspector)
    initial = workflow.invoke({'requirement_id':req['id']})
    historical = initial['report']['knowledge']
    def publish():
        return publish_reconciliation(repo, journal, predecessor_id=root.id, through_decision_id='approval',
                                      published_at=moment(5), read_blob=ingestor.store.read_blob)
    with pytest.raises(ValueError, match='review cutoff'):
        publish()
    assert repo.get_release() == root
    journal.record(Decision(id='approval', proposal_id=plan.proposals[0].id, reviewer='reviewer',
                            decided_at=moment(4), action='approve', rationale='Synthetic public-content review'))
    successor = publish()
    current = workflow.invoke({'requirement_id':req['id']})
    explanation, = current['report']['knowledge']['explanations']
    assert explanation['text'] == candidate.claim.statement
    assert explanation['citations'][0]['quote'] == new.decode()
    assert explanation['uncertainty']['provider'] == 'unspecified'
    assert initial['report']['findings'] == current['report']['findings']
    assert initial['report']['requirement']['expected'] == current['report']['requirement']['expected']
    assert compare_releases(repo, root.id, successor.id)['changes']
    assert store.export_report(initial['run_id'])['knowledge'] == historical
    repo.rollback(root.id, expected_current=successor.id)
    assert store.export_report(current['run_id'])['knowledge']['release_id'] == successor.id
    assert store.export_report(initial['run_id'])['knowledge'] == historical
    for private in (req['id'], initial['run_id'], req['urls'][0]):
        assert private.encode() not in repo.export()
    with sqlite3.connect(store.path) as db, pytest.raises(sqlite3.IntegrityError):
        db.execute('DELETE FROM knowledge_audit_pins')
    other = KnowledgeStore(store.path, repository=repo, context=Context(date(2026,11,6)), owner_id='other')
    with pytest.raises(ValueError, match='unavailable'):
        other.export_report(initial['run_id'])


def test_registry_authority_private_bytes(integrated, tmp_path):
    repo, root, records, blobs, source = integrated
    registry = SourceRegistry([r for r in records if isinstance(r, Source)])
    assert registry.resolve(source.canonical_url) == source
    with pytest.raises(ValueError): SourceRegistry([source, source])
    with pytest.raises(ValueError): SourceRegistry([source], aliases={source.canonical_url:source.id})
    with pytest.raises(KeyError): registry.resolve('https://private.example/account')
    authorize = registry.publication_authorizer([sha256(record_bytes(r)) for r in records], blobs)
    restricted = KnowledgeRepository(tmp_path / 'restricted.db', authorize=authorize)
    restricted.publish(root, records, blobs.__getitem__)
    altered = [Source.model_validate({**r.model_dump(), 'authority':'untrusted'}) if isinstance(r, Source) else r for r in records]
    with pytest.raises(PermissionError): restricted.publish(manifest(altered), altered, blobs.__getitem__)
    assert not registry.publication_authorizer([sha256(record_bytes(r)) for r in records], [])('publish', root, records)
    private = b'private audit cookie=SECRET'
    snap = next(r for r in records if isinstance(r, SourceSnapshot))
    fields = snap.model_dump(mode='json', exclude={'id'})
    fields.update(raw_sha256=sha256(private), text_sha256=sha256(private))
    forged = SourceSnapshot(id='snap:'+sha256(canonical_bytes(fields)), **fields)
    assert not authorize('publish', root, [forged])


def test_retry_and_interrupted_admission(integrated, tmp_path):
    repo, root, records, blobs, source = integrated
    scripted = Fixtures({source.canonical_url:[RetrievalError('timeout'), Response(200, (), b'Clients must send a header.')]})
    ingestor = Ingestor(tmp_path/'capture.db', [ApprovedSource(source, 'text', min_interval=0)],
                        fixtures=scripted, retries=1, observed_clock=lambda:moment(4))
    assert ingestor.capture(source.id)[0].availability == 'captured'
    assert len(scripted.requests) == 2
    assert ingestor.capture(source.id)[0].availability != 'captured'
    store = KnowledgeStore(tmp_path/'audit.db', repository=repo, context=Context(date(2026,11,6)))
    req = seed(store)[0]
    with store.connection(True) as db:
        db.execute("CREATE TRIGGER inject_failure BEFORE INSERT ON knowledge_audit_pins BEGIN SELECT RAISE(ABORT,'interrupted'); END")
    with pytest.raises(sqlite3.IntegrityError, match='interrupted'): store.create_run(req['id'])
    assert store.list_runs() == []
    with store.connection(True) as db: db.execute('DROP TRIGGER inject_failure')
    assert store.create_run(req['id'])['state'] == 'queued'


def test_existing_web_report_shows_pinned_guidance(integrated, tmp_path):
    from fastapi.testclient import TestClient
    from seo_qa.web import create_app
    repo, root, records, blobs, source = integrated
    context = Context(date(2026,11,4), provider='fixture-provider', search_engine='fixture-engine',
                      jurisdiction='US', platform_version='v2', true_conditions=frozenset({'html-response'}))
    store = KnowledgeStore(tmp_path/'audit.db', repository=repo, context=context)
    req = seed(store)[0]
    state = build_workflow(store, fixture_inspector).invoke({'requirement_id':req['id']})
    with TestClient(create_app(store, inspector_module=fixture_inspector), base_url='http://127.0.0.1') as client:
        response = client.get('/runs/'+state['run_id']+'/report')
        assert response.status_code == 200
        assert root.id in response.text
        assert 'Guidance for this audit' in response.text
        assert 'Snapshot snap:' in response.text
        data = client.get('/runs/'+state['run_id']+'/report.json').json()
        assert data['knowledge']['release_id'] == root.id


def test_knowledge_mode_refuses_unpinned_hosted_switch(integrated, tmp_path):
    from seo_qa.connection_runtime import ConnectionRuntime
    repo, *_ = integrated
    store = KnowledgeStore(tmp_path/'audit.db', repository=repo, context=Context(date(2026,11,4)))
    runtime = ConnectionRuntime(store)
    with pytest.raises(ValueError, match='hosted knowledge admission is unavailable'):
        runtime.connect()
    assert runtime.backend is store
