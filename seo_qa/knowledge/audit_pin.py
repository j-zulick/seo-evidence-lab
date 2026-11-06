"""Opt-in local Store adapter with atomic, immutable advisory audit pins."""
from dataclasses import asdict
from contextlib import nullcontext
from datetime import datetime, timezone
import json
import base64
import time

from ..store import Store, identifier, packed
from .contracts import AuditImpact, sha256, canonical_bytes
from .retrieval import Context, search

# Observed inspector.py vocabulary: expectation key -> emitted finding check.
CHECK_REGISTRY = dict(canonical='canonical', indexable='indexing_directives',
                      crawlable='robots_access', redirect_final='redirect_destination',
                      max_redirects='redirect_count', allowed_statuses='http_status',
                      required_text='required_text')


class KnowledgeStore(Store):
    supports_hosted_switch = False

    def __init__(self, path, *, repository, context, freshness=None, freshness_inputs=None, **kwargs):
        self.freshness = freshness
        self.freshness_inputs = freshness_inputs or {}
        self.repository = repository
        self.knowledge_context = context
        super().__init__(path, **kwargs)
        with self.connection(True) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS knowledge_audit_pins(
                    run_id TEXT PRIMARY KEY REFERENCES runs(id), body TEXT NOT NULL,
                    hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS knowledge_admission_markers(
                    run_id TEXT PRIMARY KEY REFERENCES runs(id), hash TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES knowledge_audit_pins(run_id));
                INSERT OR IGNORE INTO knowledge_admission_markers SELECT run_id,hash FROM knowledge_audit_pins;
                CREATE TRIGGER IF NOT EXISTS knowledge_markers_no_update
                    BEFORE UPDATE ON knowledge_admission_markers BEGIN
                    SELECT RAISE(ABORT,'Immutable knowledge marker'); END;
                CREATE TRIGGER IF NOT EXISTS knowledge_markers_no_delete
                    BEFORE DELETE ON knowledge_admission_markers BEGIN
                    SELECT RAISE(ABORT,'Immutable knowledge marker'); END;
                CREATE TRIGGER IF NOT EXISTS knowledge_pins_no_update
                    BEFORE UPDATE ON knowledge_audit_pins BEGIN
                    SELECT RAISE(ABORT,'Immutable knowledge pin'); END;
                CREATE TRIGGER IF NOT EXISTS knowledge_pins_no_delete
                    BEFORE DELETE ON knowledge_audit_pins BEGIN
                    SELECT RAISE(ABORT,'Immutable knowledge pin'); END;
            ''')

    def create_run(self, requirement_id):
        run_id = identifier()
        body = prepare_admission(self.repository, self.knowledge_context, run_id,
                                 freshness=self.freshness, freshness_inputs=self.freshness_inputs)
        with self.connection(True) as db:
            req = self._requirement(db, requirement_id)
            db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)',
                       (run_id, self.owner, requirement_id, req['version'], 'queued',
                        None, None, time.time(), None))
            db.execute('INSERT INTO knowledge_audit_pins VALUES(?,?,?)',
                       (run_id, packed(body), sha256(canonical_bytes(body))))
            db.execute('INSERT INTO knowledge_admission_markers VALUES(?,?)',
                       (run_id, sha256(canonical_bytes(body))))
        return self.get_run(run_id)

    def _knowledge(self, run_id):
        with self.connection() as db:
            self._run(db, run_id)  # owner check before pin access
            row = db.execute('SELECT body,hash FROM knowledge_audit_pins WHERE run_id=?',
                             (run_id,)).fetchone()
            marker = db.execute('SELECT hash FROM knowledge_admission_markers WHERE run_id=?',
                                (run_id,)).fetchone()
        if marker is not None and (row is None or marker['hash'] != row['hash']):
            raise ValueError('Required knowledge pin unavailable or changed')
        if row is None:
            return dict(mode='legacy', limitations=['Historical audit has no knowledge pin'])
        body = json.loads(row['body'])
        if sha256(canonical_bytes(body)) != row['hash']:
            raise ValueError('Knowledge pin integrity failure')
        impact = AuditImpact.model_validate(body['impact'])
        if impact.run_id != run_id or impact.mode != 'advisory':
            raise ValueError('Invalid advisory pin')
        from datetime import date
        fields = body['context']
        context = Context(**{**fields, 'evaluated_on': date.fromisoformat(fields['evaluated_on']),
                             'true_conditions': frozenset(fields['true_conditions'])})
        if ('annotation' in body and body['annotation'].get('freshness_revision')
                in {'unknown', 'source-freshness/v1'}):
            from .hosted import validate_pin
            return validate_pin(dict(canonical_body=canonical_bytes(body).decode(),
                                     sha256=row['hash']), run_id)
        if 'annotation' in body:
            from pydantic import TypeAdapter
            from .contracts import KnowledgeRelease, record_bytes
            from .policy import ReleaseRecord, validate_release
            annotation = body['annotation']
            manifest = KnowledgeRelease.model_validate(annotation['manifest'])
            records = [TypeAdapter(ReleaseRecord).validate_python(item['value'])
                       for item in annotation['records']]
            for item, record in zip(annotation['records'], records):
                if item['sha256'] != sha256(record_bytes(record)):
                    raise ValueError('Pinned knowledge record hash mismatch')
            blobs = {key: base64.b64decode(value, validate=True)
                     for key, value in annotation['blobs'].items()}
            validate_release(manifest, records, blobs.__getitem__)
            if manifest.id != impact.release_id:
                raise ValueError('Pinned knowledge release mismatch')
            return dict(**body['impact'], admitted_at=body['admitted_at'], **body['annotation'])
        return dict(**body['impact'], **self._annotation(self.repository,
                    self.repository.get_release(impact.release_id),
                    self.repository.graph(impact.release_id)[1], context=context))

    def _annotation(self, repository, release, records, context=None, evaluated_at=None):
        return annotate(repository, release, records, context or self.knowledge_context,
                        freshness=self.freshness, freshness_inputs=self.freshness_inputs,
                        evaluated_at=evaluated_at)

    def execute_run(self, run_id, inspector_callable):
        self._knowledge(run_id)
        return super().execute_run(run_id, inspector_callable)

    def export_report(self, run_id):
        report = super().export_report(run_id)
        report['knowledge'] = self._knowledge(run_id)
        return report

def annotate(repository, release, records, context, *, freshness=None, freshness_inputs=None, evaluated_at=None):
    result = search(repository, context, release_id=release.id)
    # No audit URL, finding, requirement, HTML or query enters public storage.
    explanations = []
    for hit in result.hits:
        scope = hit.guidance.scope
        explanations.append(dict(
            guidance_id=hit.guidance.id, text=hit.guidance.recommendation,
            uncertainty={key: 'unspecified' if getattr(scope, key) is None else 'explicit'
                         for key in ('provider', 'search_engine', 'jurisdiction',
                                     'platform_version', 'effective_from', 'effective_until')},
            citations=[dict(source_url=c.source.canonical_url,
                            snapshot_id=c.snapshot.id, quote=c.span.quote,
                            start_byte=c.span.start_byte, end_byte=c.span.end_byte)
                       for c in hit.citations]))

    from .contracts import RuleBinding
    from .freshness import encode_inputs
    freshness_revision = ('source-freshness/v1' if freshness and
                          freshness.__module__ == 'seo_qa.knowledge.freshness' else
                          'unknown' if freshness is None else 'external')
    freshness_evidence = encode_inputs(freshness_inputs or {}) if freshness_revision == 'source-freshness/v1' else {}
    freshness = (freshness(repository, context, evaluated_at=evaluated_at or datetime.now(timezone.utc),
                                **(freshness_inputs or {})) if freshness else
                 dict(status='unknown', limitations=['Source freshness has not been verified']))
    from .contracts import SourceSnapshot, record_bytes
    blobs = {digest: base64.b64encode(repository.read_blob(digest)).decode('ascii')
             for record in records if isinstance(record, SourceSnapshot) and record.availability == 'captured'
             for digest in (record.raw_sha256, record.text_sha256)}
    return dict(manifest=release.model_dump(mode='json'), blobs=blobs,
                records=[dict(kind=type(record).__name__, value=record.model_dump(mode='json'),
                              sha256=sha256(record_bytes(record))) for record in records],
                snapshots=[record.model_dump(mode='json') for record in records if isinstance(record, SourceSnapshot)],
                freshness=freshness, freshness_revision=freshness_revision,
                freshness_evidence=freshness_evidence, retrieval=result.result.model_dump(mode='json'),
                explanations=explanations,
                rule_registry=[dict(binding_id=r.id, check=r.check, guidance_id=r.guidance_id,
                                    finding_check=CHECK_REGISTRY[r.check],
                                    implementation_revision=r.implementation_revision,
                                    implementation_sha256=r.implementation_sha256, activation='disabled')
                               for r in records if isinstance(r, RuleBinding)],
                limitations=['Advisory guidance; executable checks use unchanged requirement expectations'])


def prepare_admission(repository, context, run_id, *, freshness=None, freshness_inputs=None):
    if not isinstance(context, Context):
        raise ValueError('Explicit applicability context required')
    admitted_at = datetime.now(timezone.utc)
    opener = getattr(repository, 'open_verified', None)
    with opener() if opener else nullcontext(repository) as verified:
        selected = getattr(getattr(repository, 'config', None), 'release_id', None)
        release, records = verified.graph(selected)
        class PinnedView:
            def graph(self, release_id=None):
                return verified.graph(release_id or release.id)

            def read_blob(self, digest):
                return verified.read_blob(digest)

        annotation = annotate(PinnedView(), release, records, context, freshness=freshness,
                              freshness_inputs=freshness_inputs, evaluated_at=admitted_at)
    impact = AuditImpact(run_id=run_id, evaluated_on=context.evaluated_on,
                         release_id=release.id, mode='advisory')
    body = dict(impact=impact.model_dump(mode='json'), context=asdict(context),
                admitted_at=admitted_at.isoformat(), annotation=annotation)
    body['context']['evaluated_on'] = context.evaluated_on.isoformat()
    body['context']['true_conditions'] = sorted(context.true_conditions)
    if len(canonical_bytes(body)) > 4_000_000:
        raise ValueError('Knowledge audit pin exceeds limit')
    return body
