"""Finite synthetic manual update -> review -> release -> remote reader -> audit."""
import argparse
import base64
from datetime import datetime, date, timezone
import json
from pathlib import Path
import time

import httpx

from ..fixtures import seed, fixture_inspector
from ..workflow import build_workflow
from .audit_pin import KnowledgeStore
from .capture import ApprovedSource, Fixtures, Response
from .change_detection import SnapshotInput, compare_snapshots
from .claims import extract_claims
from .contracts import Source, record_bytes, sha256
from .local_demo import run_demo
from .operations import UpdateOperations
from .publish import publish_reconciliation
from .reconciliation import Meaning, Observation, ReviewJournal, Decision, reconcile, released_guidance
from .remote import RemoteConfig, SupabaseKnowledgeRepository
from .repository import KnowledgeRepository
from .retrieval import Context


def run(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Fresh destinations make retry/recovery behavior explicit.
    flow = directory / 'manual-flow'
    flow.mkdir(mode=0o700)
    bootstrap = run_demo(flow / 'bootstrap')
    repo = KnowledgeRepository(bootstrap['repository'], authorize=lambda *_: True)
    baseline, records = repo.graph()
    source = next(record for record in records if isinstance(record, Source))
    old = b'Clients must send a header.'
    new = b'Clients must send a public header.'
    transport = Fixtures({source.canonical_url: [Response(200, (('etag', '"v1"'),), old),
        Response(200, (('etag', '"v2"'),), new), Response(304)]})
    ops = UpdateOperations(flow / 'updates', [ApprovedSource(source, 'text', min_interval=0)],
                           fixtures=transport)
    client = ops.clients[source.id]
    client._clock = lambda: datetime(2026, 11, 4, tzinfo=timezone.utc)
    ops.run('synthetic-first')
    first_key = client.health(source.id)['last_success']['snapshot_id']
    first, _, text1 = client.store.read(first_key)
    ops.run('synthetic-change')
    after_key = client.health(source.id)['last_success']['snapshot_id']
    after, _, text2 = client.store.read(after_key)
    ops.run('synthetic-change')  # Exact retry must consume no additional reply.
    final_health = ops.run('synthetic-conditional')
    if len(transport.requests) != 3:
        raise RuntimeError('Unexpected acquisition replay')
    event, = compare_snapshots([SnapshotInput(source, first, text1)],
                               [SnapshotInput(source, after, text2)]).events
    candidate, = extract_claims([SnapshotInput(source, after, text2)],
                                as_of=date(2026, 11, 4), change_event=event).candidates
    baseline_guidance = released_guidance(repo, baseline.id,
        {'guide:canonical:1': Meaning(topic='canonical', value='canonical')})
    observed = datetime(2026, 11, 4, tzinfo=timezone.utc)
    plan = reconcile([Observation(claim=candidate.claim, meaning=Meaning(topic='header', value='public-header'),
                                  observed_at=observed)],
                     baseline_guidance, [source], [after], client.store.read_blob,
                     baseline_release_id=baseline.id)
    journal = ReviewJournal(flow / 'review.sqlite', plan, lambda actor: actor == 'fixture-reviewer')
    blocked_before_review = False
    try:
        publish_reconciliation(repo, journal, predecessor_id=baseline.id,
            published_at=datetime(2026, 11, 6, tzinfo=timezone.utc),
            through_decision_id='synthetic-approval', read_blob=client.store.read_blob)
    except ValueError:
        blocked_before_review = True
    if not blocked_before_review or repo.get_release().id != baseline.id:
        raise RuntimeError('Unreviewed update entered publication')
    journal.record(Decision(id='synthetic-approval', proposal_id=plan.proposals[0].id,
                            reviewer='fixture-reviewer', decided_at=observed, action='approve',
                            rationale='Synthetic actor attestation only; no human review occurred'))
    release = publish_reconciliation(repo, journal, predecessor_id=baseline.id,
        published_at=datetime(2026, 11, 6, tzinfo=timezone.utc),
        through_decision_id='synthetic-approval', read_blob=client.store.read_blob)
    bundle = repo.export()
    claims = dict(sub='11111111-1111-4111-8111-111111111111', role='authenticated',
                  iss='https://'+'a'*20+'.supabase.co/auth/v1', exp=time.time()+3600)
    token = 'fixture.'+base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')+'.synthetic'
    config = RemoteConfig('a'*20, release.id, 'synthetic_knowledge_bundles', 'sb_publishable_fixture', token)
    calls = []

    def endpoint(request):
        calls.append(str(request.url))
        if request.url.params['release_id'] != 'eq.'+release.id:
            return httpx.Response(404)
        return httpx.Response(200, json=[dict(release_id=release.id, bundle_sha256=sha256(bundle),
                                              bundle_base64=base64.b64encode(bundle).decode())])

    remote = SupabaseKnowledgeRepository(config, transport=httpx.MockTransport(endpoint))
    audit = KnowledgeStore(flow / 'audit.sqlite', repository=remote, context=Context(date(2026,11,6)))
    req = seed(audit)[0]
    result = build_workflow(audit, fixture_inspector).invoke({'requirement_id': req['id']})
    report = result['report']
    annotation = report['knowledge']
    if annotation['explanations'][0]['citations'][0]['quote'] != new.decode():
        raise RuntimeError('Acquired evidence did not reach the audit')
    (flow / 'report.json').write_text(json.dumps(report, indent=2))
    from ..csv_export import export_report_csv
    from ..markdown_export import export_report_markdown
    (flow / 'report.csv').write_bytes(export_report_csv(report))
    (flow / 'report.md').write_bytes(export_report_markdown(report))
    # Neither source outage nor current release rollback changes the admitted report.
    remote.transport = httpx.MockTransport(lambda _: httpx.Response(503))
    repo.rollback(baseline.id, expected_current=release.id)
    if audit.export_report(result['run_id'])['knowledge'] != annotation:
        raise RuntimeError('Historical replay changed')
    receipt = dict(mode='synthetic-local', source_update_requests=len(transport.requests),
                   remote_fixture_requests=len(calls), scheduler=ops.tick(), health=final_health,
                   selected_release_id=release.id, selected_release_sha256=sha256(record_bytes(release)),
                   audit_id=result['run_id'], snapshot_id=after.id, raw_sha256=after.raw_sha256,
                   text_sha256=after.text_sha256, quote=new.decode(),
                   freshness=annotation['freshness'], executable_activation=False,
                   human_review=False, live_repository=False, blocked_before_review=blocked_before_review, output=str(flow.resolve()))
    (flow / 'receipt.json').write_text(json.dumps(receipt, indent=2))
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.directory), indent=2))
