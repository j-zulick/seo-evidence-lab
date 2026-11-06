"""Explicitly local PostgreSQL/private-object snapshot adapters.

No live connector, credentials, schema deployment or auth-session copy exists.
A SqlPort supplies query(sql, params)->list[dict], ref, environment='local', and
maintenance_held=True. Storage supplies revision/read/create/delete_created/paths
and the same ref/environment/maintenance declarations. Attestations are supplied
by the trusted local harness; they are not proof of a hosted project's identity.
"""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import re
from pathlib import Path
from uuid import UUID

from .hosted_recovery import TABLES, OPTIONAL_TABLE, clean_snapshot, validate, export_snapshot, verify, PIN_TABLE
from .hosted_store import packed

SCOPED_TABLES=('seo_scoped_requirements_v1','seo_scoped_versions_v1','seo_scoped_runs_v1','seo_scoped_reviews_v1')
KNOWN_TABLES=TABLES+(OPTIONAL_TABLE,)+SCOPED_TABLES+(PIN_TABLE,)
PROTECTED_REF='mqzslbwdszcbsyppdlfz'


def identity(port, ref):
    if not isinstance(ref,str) or not re.fullmatch('[a-z0-9-]{3,64}',ref):raise ValueError('Explicit bounded reference required')
    if ref==PROTECTED_REF or port.ref!=ref or port.environment!='local' or port.maintenance_held is not True:
        raise ValueError('Only explicitly identified quiesced LOCAL ports accepted')


@contextmanager
def transaction(db, *, read=False):
    db.query('BEGIN ISOLATION LEVEL REPEATABLE READ' if read else 'BEGIN ISOLATION LEVEL SERIALIZABLE',[])
    try:
        db.query("SET LOCAL lock_timeout='2000ms'",[])
        yield
        db.query('COMMIT',[])
    except BaseException:
        try:db.query('ROLLBACK',[])
        except Exception:pass
        raise


def tables(db):
    result=[]
    for table in KNOWN_TABLES:
        found=db.query('SELECT to_regclass($1) AS name',['public.'+table])[0]['name']
        if found is not None:result.append(table)
    if not set(TABLES)<=set(result):raise ValueError('Required application schema unavailable')
    if any(t in result for t in SCOPED_TABLES) and not set(SCOPED_TABLES+(OPTIONAL_TABLE,))<=set(result):
        raise ValueError('Partial scoped workflow schema refused')
    if 'seo_scoped_runs_v1' in result:
        columns=db.query("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=$1",['seo_scoped_runs_v1'])
        marker=any(r['column_name']=='knowledge_pin_required' for r in columns)
        if marker != (PIN_TABLE in result):raise ValueError('Partial knowledge schema refused')
    return result


def _rows(db, selected_tables, owner, project):
    rows={t:[r['body'] for r in db.query('SELECT to_jsonb(t) AS body FROM public.'+t+' t WHERE owner_id=$1::uuid',[owner])] for t in selected_tables}
    rows['seo_qa_tool']=[r for r in rows['seo_qa_tool'] if r['id']==project]
    rows['seo_requirements']=[r for r in rows['seo_requirements'] if r['project_id']==project]
    reqs={r['id'] for r in rows['seo_requirements']}
    rows['seo_requirement_versions']=[r for r in rows['seo_requirement_versions'] if r['requirement_id'] in reqs]
    rows['seo_runs']=[r for r in rows['seo_runs'] if r['requirement_id'] in reqs]
    runs={r['id'] for r in rows['seo_runs']}
    for table in ('seo_observations','seo_reviews','seo_operations'):
        rows[table]=[r for r in rows[table] if r['run_id'] in runs]
    ops={r['id'] for r in rows['seo_operations']}
    rows['seo_fixture_destinations']=[r for r in rows['seo_fixture_destinations'] if r['id'] in ops]
    rows['seo_imports']=[r for r in rows['seo_imports'] if r['project_id']==project]
    rows['seo_events']=[r for r in rows['seo_events'] if isinstance(r.get('body'),dict) and any(r['body'].get(k) in reqs|runs for k in ('id','run','run_id','requirement_id'))]
    if OPTIONAL_TABLE in rows:rows[OPTIONAL_TABLE]=[r for r in rows[OPTIONAL_TABLE] if r['project_id']==project]
    if set(SCOPED_TABLES)<=set(rows):
        rows['seo_scoped_requirements_v1']=[r for r in rows['seo_scoped_requirements_v1'] if r['project_id']==project]
        scoped_reqs={r['id'] for r in rows['seo_scoped_requirements_v1']}
        rows['seo_scoped_versions_v1']=[r for r in rows['seo_scoped_versions_v1'] if r['requirement_id'] in scoped_reqs]
        rows['seo_scoped_runs_v1']=[r for r in rows['seo_scoped_runs_v1'] if r['project_id']==project]
        scoped_runs={r['id'] for r in rows['seo_scoped_runs_v1']}
        rows['seo_scoped_reviews_v1']=[r for r in rows['seo_scoped_reviews_v1'] if r['run_id'] in scoped_runs]
    if PIN_TABLE in rows:rows[PIN_TABLE]=[r for r in rows[PIN_TABLE] if r['project_id']==project]
    # Stable ordering gives reproducible identities; PostgreSQL physical row order
    # is not a contract, and mixed JSON key ordering is normalized by packed().
    return {t:sorted(values,key=lambda r:(str(r.get('id',r.get('run_id',r.get('requirement_id','')))),r.get('version',0))) for t,values in rows.items()}


def export_postgres(db, storage, destination, *, source_ref, owner, project):
    identity(db,source_ref);identity(storage,source_ref)
    UUID(owner);UUID(project)
    revision=storage.revision()
    with transaction(db,read=True):
        selected=tables(db)
        db.query('LOCK TABLE '+','.join('public.'+t for t in selected)+' IN SHARE MODE',[])
        rows=_rows(db,selected,owner,project)
        # Collect actual private bytes while locks and maintenance are held. Only
        # publish bundle after the source transaction and consistency check pass.
        cleaned=clean_snapshot(rows);expected=validate(cleaned,owner,project)
        objects={p:storage.read(p) for p in expected}
        for path,data in expected.items():
            if objects[path]!=data:raise ValueError('Storage bytes differ from committed observation')
        if storage.revision()!=revision:raise ValueError('Storage changed during snapshot')
    return export_snapshot(rows,objects.__getitem__,destination,owner=owner,project=project,writers_stopped=True,provenance={'source_ref':source_ref,'source_kind':'local-postgresql','isolation':'repeatable-read-plus-share-locks','storage_revision':revision})


def owner_mapping_plan(source_owner,destination_owner):
    UUID(source_owner);UUID(destination_owner)
    return {'source_owner':source_owner,'destination_owner':destination_owner,
            'executable':source_owner==destination_owner,
            'session_copy':False,'auth_user_creation':False,
            'required':[] if source_owner==destination_owner else [
                'review every owner foreign key, checkpoint/version canonical body and private object path',
                'recompute hashes after explicit mapping; preserve external operation meaning',
                'verify destination Auth user and policies before admission'],
            'mode':'preserve-existing-owner' if source_owner==destination_owner else 'review-required'}


def _receipt_setup(db):
    db.query('CREATE SCHEMA IF NOT EXISTS seo_recovery',[])
    db.query('REVOKE ALL ON SCHEMA seo_recovery FROM PUBLIC',[])
    db.query('CREATE TABLE IF NOT EXISTS seo_recovery.receipts (operation_id text primary key, fingerprint text not null, owner_id uuid not null, project_id uuid not null)',[])
    db.query('REVOKE ALL ON seo_recovery.receipts FROM PUBLIC',[])


def _fingerprint(rows,manifest):
    return hashlib.sha256(packed({'rows':rows,'files':manifest['files'],'provenance':manifest.get('provenance')}).encode()).hexdigest()


def _insert(db,table,row):
    columns={r['column_name'] for r in db.query("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=$1",[table])}
    if set(row)-columns:raise ValueError('Snapshot columns absent from selected destination schema')
    names=','.join('"'+k+'"' for k in sorted(row))
    db.query('INSERT INTO public.'+table+' ('+names+') OVERRIDING SYSTEM VALUE SELECT '+names+' FROM jsonb_populate_record(NULL::public.'+table+',$1::jsonb)',[packed(row)])


def _sequence_repair(db,selected):
    for t in selected:
        for r in db.query("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=$1 AND (is_identity='YES' OR column_default LIKE 'nextval(%')",[t]):
            col=r['column_name'];seq=db.query('SELECT pg_get_serial_sequence($1,$2) AS seq',['public.'+t,col])[0]['seq']
            if seq:
                value=db.query('SELECT max("'+col+'") AS value FROM public.'+t,[])[0]['value']
                db.query('SELECT setval($1::regclass,$2::bigint,$3::boolean)',[seq,value or 1,value is not None])


def import_postgres(source, db, storage, *, source_ref, destination_ref, source_owner,
                    destination_owner, project, operation_id, isolated_destination=False):
    """Create-only privileged LOCAL import, with transaction receipt reconciliation.

    ObjectPort.create is immutable and tags only newly created paths with this
    operation. delete_created must never remove a pre-existing or foreign object.
    DB receipt is authority after an uncertain COMMIT; unknown readback retains
    objects for later reconciliation. Never claim cross-service atomicity.
    """
    identity(db,destination_ref);identity(storage,destination_ref)
    if source_ref==destination_ref or destination_ref==PROTECTED_REF or isolated_destination is not True:
        raise ValueError('Explicit distinct isolated destination required')
    if not isinstance(operation_id,str) or not re.fullmatch('[a-zA-Z0-9-]{8,100}',operation_id):raise ValueError('Stable explicit restore operation identity required')
    plan=owner_mapping_plan(source_owner,destination_owner)
    if not plan['executable']:raise ValueError('Owner remapping requires reviewed mapping; preserve UUID for local execution')
    manifest,rows=verify(source)
    if manifest.get('provenance',{}).get('source_ref')!=source_ref or manifest.get('provenance',{}).get('source_kind')!='local-postgresql':
        raise ValueError('Source reference/provenance does not match selected snapshot')
    if manifest['owner_id']!=source_owner or manifest['project_id']!=project:raise ValueError('Snapshot selection mismatch')
    fingerprint=_fingerprint(rows,manifest)
    selected=list(rows);expected=validate(rows,source_owner,project)
    receipt=None;started=False;dispatched=False
    try:
        with transaction(db):
            selected_schema=tables(db)
            if not set(selected)<=set(selected_schema):raise ValueError('Destination lacks complete snapshot schema')
            db.query('LOCK TABLE '+','.join('public.'+t for t in selected_schema)+' IN EXCLUSIVE MODE',[])
            _receipt_setup(db)
            prior=db.query('SELECT * FROM seo_recovery.receipts WHERE operation_id=$1',[operation_id])
            if prior:
                if prior[0]['fingerprint']!=fingerprint or prior[0]['owner_id']!=destination_owner or prior[0]['project_id']!=project:
                    raise ValueError('Restore operation identity reused for different snapshot')
                if clean_snapshot(_rows(db,selected,destination_owner,project))!=rows:raise ValueError('Previously restored records changed; do not overwrite')
                for path,data in expected.items():
                    if storage.read(path)!=data:raise ValueError('Previously restored object changed; do not overwrite')
                receipt={'state':'reconciled','operation_id':operation_id,'fingerprint':fingerprint}
            else:
                # Isolated application schema is entirely empty. Never merge or
                # replace an existing project, even if it belongs to same owner.
                if any(db.query('SELECT 1 AS found FROM public.'+t+' LIMIT 1',[]) for t in selected_schema):raise ValueError('Destination application tables must be empty')
                if storage.paths():raise ValueError('Destination private bucket must be empty')
                auth=db.query('SELECT id FROM auth.users WHERE id=$1::uuid',[destination_owner])
                if len(auth)!=1:raise ValueError('Verified pre-existing destination Auth owner required; no user/session import')
                started=True
                for path,data in expected.items():
                    storage.create(path,data,operation_id)
                    if storage.read(path)!=data:raise ValueError('Destination Storage readback mismatch')
                # FK-safe order; checkpoint references root, new scoped run links
                # scoped versions, scoped reviews follow runs.
                ordered=list(TABLES)
                ordered.remove('seo_fixture_destinations');ordered.append('seo_fixture_destinations')
                ordered += [t for t in ('seo_scoped_requirements_v1','seo_scoped_versions_v1',OPTIONAL_TABLE,'seo_scoped_runs_v1','seo_scoped_reviews_v1',PIN_TABLE) if t in rows]
                for table in ordered:
                    for row in rows[table]:_insert(db,table,row)
                if clean_snapshot(_rows(db,selected,destination_owner,project))!=rows:raise ValueError('Destination normalized readback differs')
                db.query('INSERT INTO seo_recovery.receipts VALUES($1,$2,$3::uuid,$4::uuid)',[operation_id,fingerprint,destination_owner,project])
                receipt={'state':'restored','operation_id':operation_id,'fingerprint':fingerprint}
            dispatched=True
    except BaseException:
        # The DB transaction helper already attempted rollback. If a COMMIT reply
        # was lost, receipt readback prevents deleting committed evidence or
        # replaying writes. An unreachable DB leaves objects untouched/unknown.
        if started:
            try:prior=db.query('SELECT * FROM seo_recovery.receipts WHERE operation_id=$1',[operation_id])
            except Exception:
                if dispatched:raise RuntimeError('Restore outcome unknown; retain objects and reconcile stable operation identity')
                prior=[]
            if prior and prior[0]['fingerprint']==fingerprint:
                return {'state':'reconciled','operation_id':operation_id,'fingerprint':fingerprint,'sequence_repair_pending':True}
            storage.delete_created(operation_id)
        raise
    # PostgreSQL sequence counters are not transactional. Repair after committed
    # row/receipt readback; safe idempotent repair also runs on reconciliation.
    _sequence_repair(db,selected)
    return receipt
