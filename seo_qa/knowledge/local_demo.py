"""Offline K06 smoke flow using only the built-in synthetic fixture."""
import json
from datetime import date
from pathlib import Path
from tempfile import mkdtemp

from .contracts import (
    GuidanceVersion, KnowledgeClaim, KnowledgeRelease, ReviewDecision, Source, SourceSnapshot,
    canonical_bytes, record_bytes, sha256,
)
from .repository import KnowledgeRepository
from .retrieval import Context, search

_FIXTURE = Path(__file__).resolve().parents[2] / 'fixtures' / 'knowledge' / 'v1.json'
_TYPES = {kind.__name__: kind for kind in
          (Source, SourceSnapshot, KnowledgeClaim, GuidanceVersion, ReviewDecision)}


def _manifest(records, published_at, previous=None):
    fields = dict(schema_version=1, published_at=published_at, previous_release_id=previous,
                  members=[dict(id=record.id, sha256=sha256(record_bytes(record)))
                           for record in sorted(records, key=lambda item: item.id)])
    return KnowledgeRelease(id='rel:' + sha256(canonical_bytes(fields)), **fields)


def run_demo(state_dir):
    fixture = json.loads(_FIXTURE.read_bytes())
    baseline = next(KnowledgeRelease.model_validate(item['value'])
                    for item in fixture['records'] if item['entity'] == 'KnowledgeRelease')
    ids = {pin.id for pin in baseline.members}
    records = [_TYPES[item['entity']].model_validate(item['value'])
               for item in fixture['records']
               if item['entity'] in _TYPES and item['value']['id'] in ids
               and not item['value'].get('subject_id', '').startswith('bind:')]
    guide = next(record for record in records if isinstance(record, GuidanceVersion))
    approval = next(record for record in records if isinstance(record, ReviewDecision)
                    and record.subject_id == guide.id)
    newer = guide.model_copy(update={'id': guide.id.rsplit(':', 1)[0] + ':2',
                                     'recommendation': 'Inspect canonical references before release.'})
    reviewed = approval.model_copy(update={'id': 'k06-demo-review-v2', 'subject_id': newer.id,
                                           'subject_sha256': sha256(record_bytes(newer))})
    successor_records = [record for record in records if record not in (guide, approval)]
    successor_records.extend((newer, reviewed))
    first = _manifest(records, '2026-11-03T18:00:00Z')
    second = _manifest(successor_records, '2026-11-04T18:00:00Z', first.id)
    graphs = {release.id: {record.id: record for record in members}
              for release, members in ((first, records), (second, successor_records))}

    def authorize(operation, release, members):
        permitted = operation == 'publish' or (operation == 'rollback' and release.id == first.id)
        return permitted and graphs.get(release.id) == {record.id: record for record in members}

    root = Path(state_dir)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = Path(mkdtemp(prefix='knowledge-demo-', dir=root)) / 'knowledge.sqlite'
    writer = KnowledgeRepository(path, authorize=authorize)
    writer.publish(first, records, lambda digest: fixture['blobs'][digest].encode('utf-8'))
    writer.publish(second, successor_records, writer.read_blob)
    reader = KnowledgeRepository(path, read_only=True)
    context = Context(date(2026, 11, 4), provider='fixture-provider', search_engine='fixture-engine',
                      jurisdiction='US', platform_version='v2', true_conditions=frozenset({'html-response'}))
    current = search(reader, context, query='canonical')
    historical = search(reader, context, release_id=first.id, query='canonical')
    writer.rollback(first.id, expected_current=second.id)
    rolled_back = search(reader, context, query='canonical')
    retained = search(reader, context, release_id=second.id, query='canonical')
    if (current.result.guidance_ids != (newer.id,) or historical.result.guidance_ids != (guide.id,)
            or rolled_back != historical or retained != current):
        raise RuntimeError('Synthetic knowledge retrieval or rollback failed')
    citation = current.hits[0].citations[0]
    data = reader.read_blob(citation.snapshot.text_sha256)
    if data[citation.span.start_byte:citation.span.end_byte] != citation.span.quote.encode('utf-8'):
        raise RuntimeError('Synthetic knowledge citation could not be reproduced')
    return dict(mode='synthetic-local', repository=str(path.resolve()),
                published_release_ids=[first.id, second.id],
                current_before_rollback=current.result.model_dump(mode='json'),
                historical_before_rollback=historical.result.model_dump(mode='json'),
                current_after_rollback=rolled_back.result.model_dump(mode='json'),
                retained_after_rollback=retained.result.model_dump(mode='json'),
                citation=dict(source_url=citation.source.canonical_url, snapshot_id=citation.snapshot.id,
                              start_byte=citation.span.start_byte, end_byte=citation.span.end_byte,
                              quote=citation.span.quote))
