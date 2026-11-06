"""Offline normalized hosted recovery prototype; no live export/import authority.

An authorized exporter must supply one consistent, quiesced owner/project snapshot.
No Auth/session table or reusable lease is accepted. Restores are read-only local
sandboxes, not replacement live Supabase projects. Hashes detect damage only.
"""
from __future__ import annotations
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import UUID

from .release_recovery import _source, _safe, _staging
from .hosted_store import packed, digest

FORMAT = 'seo-hosted-recovery-local-prototype/v1'
KNOWLEDGE_FORMAT = 'seo-hosted-recovery-knowledge/v2'
PIN_TABLE = 'seo_knowledge_audit_pins_v1'
TABLES = ('seo_qa_tool','seo_requirements','seo_requirement_versions','seo_runs',
          'seo_observations','seo_reviews','seo_operations','seo_events',
          'seo_imports','seo_fixture_destinations')
OPTIONAL_TABLE = 'seo_scoped_checkpoints_v1'
SCOPED_TABLES = ('seo_scoped_requirements_v1','seo_scoped_versions_v1','seo_scoped_runs_v1','seo_scoped_reviews_v1')

def valid_tables(names):
    return set(names) in (set(TABLES),set(TABLES)|{OPTIONAL_TABLE},set(TABLES)|{OPTIONAL_TABLE}|set(SCOPED_TABLES),set(TABLES)|{OPTIONAL_TABLE}|set(SCOPED_TABLES)|{PIN_TABLE})
FORBIDDEN = {'password','access_token','refresh_token','user_jwt','authorization',
             'signing_key','lease_token','lease_expires','session_digest'}
MAX_RECORD_BYTES = 32_000_000
MAX_OBJECT_BYTES = 2_000_000


def sha(data):
    return hashlib.sha256(data).hexdigest()


def uuid(value):
    result = str(UUID(str(value)))
    if result != value:
        raise ValueError('Canonical UUID required')
    return result


def clean_snapshot(rows):
    """Drop lease capabilities and mark in-flight work interrupted, never replay."""
    rows = deepcopy(rows)
    for row in rows.get('seo_runs', []) + rows.get('seo_scoped_runs_v1', []):
        row.pop('lease_token', None)
        row.pop('lease_expires', None)
        if row.get('state') in {'running','queued'}:
            row.update(state='interrupted', error='Recovery requires a fresh explicitly requested run')
    for row in rows.get(OPTIONAL_TABLE, []):
        body=row.get('body',{})
        if body.get('state') in {'queued','running'}:
            from .scope_criteria import canonical
            body.update(state='interrupted',error=None)
            row['canonical_body']=canonical(body).decode()
            row['sha256']=sha(row['canonical_body'].encode())
    return rows


def validate(rows, owner, project):
    uuid(owner); uuid(project)
    if not valid_tables(rows) or any(not isinstance(v,list) for v in rows.values()):
        raise ValueError('Exact declared normalized tables required; no Auth/session tables')
    for table, values in rows.items():
        seen=set()
        for r in values:
            if not isinstance(r,dict) or r.get('owner_id') != owner or FORBIDDEN & r.keys():
                raise ValueError('Owner mismatch or credential/lease metadata')
            key=(r.get('requirement_id'),r.get('version')) if table in {'seo_requirement_versions','seo_scoped_versions_v1'} else r.get('run_id') if table in {OPTIONAL_TABLE,PIN_TABLE} else r.get('id')
            if key in seen or key is None: raise ValueError('Duplicate/missing row identity')
            seen.add(key)
            if table!='seo_events':
                uuid(r.get('requirement_id') if table in {'seo_requirement_versions','seo_scoped_versions_v1'} else r.get('run_id') if table in {OPTIONAL_TABLE,PIN_TABLE} else r.get('id'))
    roots=rows['seo_qa_tool']
    if len(roots)!=1 or roots[0]['id']!=project or roots[0].get('mode')!='dummy':
        raise ValueError('Exact existing dummy-profile root required')
    reqs={r['id']:r for r in rows['seo_requirements']}
    versions={(r['requirement_id'],r['version']):r for r in rows['seo_requirement_versions']}
    for r in reqs.values():
        if r.get('project_id')!=project or (r['id'],r.get('current_version')) not in versions:
            raise ValueError('Requirement/root/version binding failure')
    for r in versions.values():
        body={k:r[k] for k in ('title','urls','expected')}
        if r['requirement_id'] not in reqs or digest(body)!=r.get('content_hash'):
            raise ValueError('Requirement hash/link failure')
    runs={r['id']:r for r in rows['seo_runs']}
    for r in runs.values():
        if (r.get('requirement_id'),r.get('requirement_version')) not in versions or r.get('state') not in {'completed','error','interrupted'}:
            raise ValueError('Run binding/state failure')
    object_paths={}; coverage={r:set() for r in runs}
    for r in rows['seo_observations']:
        if r.get('run_id') not in runs: raise ValueError('Observation/run binding failure')
        run=runs[r['run_id']]; version=versions[run['requirement_id'],run['requirement_version']]
        body=r.get('body'); h=digest(body)
        path=owner+'/'+project+'/'+run['id']+'/'+h+'.json'
        if (not isinstance(body,dict) or body.get('url')!=r.get('url') or r['url'] not in version['urls'] or
            r['url'] in coverage[run['id']] or r.get('evidence_path')!=path or r.get('evidence_hash')!=h or
            r.get('findings')!=body.get('findings')):
            raise ValueError('Observation evidence/hash/path/URL failure')
        _safe(path);object_paths[path]=packed(body).encode();coverage[run['id']].add(r['url'])
    for r in runs.values():
        if r['state']=='completed' and coverage[r['id']]!=set(versions[r['requirement_id'],r['requirement_version']]['urls']):
            raise ValueError('Completed run lacks complete evidence')
    operations={r['id'] for r in rows['seo_operations']}
    for table in ('seo_reviews','seo_operations'):
        if any(r.get('run_id') not in runs for r in rows[table]):raise ValueError('Review/operation run binding failure')
    if any(r['id'] not in operations for r in rows['seo_fixture_destinations']):raise ValueError('Destination operation binding failure')
    for r in rows['seo_events']:
        body=r.get('body',{})
        # Owner-only events have no project FK. Require an explicit selected
        # requirement/run reference, never silently import unrelated owner data.
        refs={body[k] for k in ('id','run','run_id','requirement_id') if isinstance(body.get(k),str)} if isinstance(body,dict) else set()
        if not refs or not refs <= set(reqs) | set(runs):
            raise ValueError('Event cannot be bound to selected project')
    if any(r.get('project_id')!=project for r in rows['seo_imports']):raise ValueError('Import project binding failure')
    for r in rows['seo_imports']:
        if digest({'kind':r['kind'],'raw':r['raw'],'result':r['body']})!=r.get('content_hash'):
            raise ValueError('Import content hash failure')
    for r in rows.get(OPTIONAL_TABLE, []):
        from .scoped_storage import validate_record
        body=r.get('body'); raw=r.get('canonical_body')
        if (not isinstance(raw,str) or len(raw.encode())>MAX_RECORD_BYTES or
            sha(raw.encode())!=r.get('sha256') or json.loads(raw)!=body or
            type(r.get('revision')) is not int or r['revision']<1 or r.get('project_id')!=project or
            body.get('id')!=r['run_id'] or body.get('owner')!=owner or body.get('project_id')!=project):
            raise ValueError('Scoped checkpoint bytes/hash/identity/revision failure')
        validate_record(body)
    if set(SCOPED_TABLES)<=set(rows):
        from .scope_criteria import canonical
        from .models import RequirementInput
        scoped_reqs={r['id']:r for r in rows['seo_scoped_requirements_v1']}
        scoped_versions={(r['requirement_id'],r['version']):r for r in rows['seo_scoped_versions_v1']}
        checkpoints={r['run_id']:r for r in rows[OPTIONAL_TABLE]}
        scoped_runs={r['id']:r for r in rows['seo_scoped_runs_v1']}
        for r in scoped_reqs.values():
            if r.get('project_id')!=project or (r['id'],r.get('current_version')) not in scoped_versions:
                raise ValueError('Scoped requirement version/root mismatch')
        for r in scoped_versions.values():
            body=r.get('body');raw=r.get('canonical_body')
            if (r['requirement_id'] not in scoped_reqs or not isinstance(raw,str) or canonical(body).decode()!=raw or
                sha(raw.encode())!=r.get('sha256') or RequirementInput.model_validate(body).model_dump()!=body):
                raise ValueError('Scoped immutable requirement bytes/hash mismatch')
        for r in scoped_runs.values():
            version=scoped_versions.get((r.get('requirement_id'),r.get('requirement_version')))
            checkpoint=checkpoints.get(r['id'])
            if (r.get('project_id')!=project or version is None or checkpoint is None or
                checkpoint['body']['requirement']!=version['body']):
                raise ValueError('Scoped run/checkpoint/version binding failure')
        if set(checkpoints)!=set(scoped_runs):raise ValueError('Scoped checkpoint/run coverage mismatch')
        if any(r.get('run_id') not in scoped_runs for r in rows['seo_scoped_reviews_v1']):
            raise ValueError('Scoped review/run binding failure')
    scoped = rows.get('seo_scoped_runs_v1', [])
    if PIN_TABLE not in rows:
        if any(r.get('knowledge_pin_required') is not None for r in scoped):
            raise ValueError('Knowledge schema marker requires explicit sidecar inventory')
    else:
        from .knowledge.hosted import validate_pin
        pins={r['run_id']:r for r in rows[PIN_TABLE]}
        runs_by_id={r['id']:r for r in scoped}
        if set(pins)-set(runs_by_id):raise ValueError('Orphan knowledge pin')
        versions={(r['requirement_id'],r['version']):r for r in rows['seo_scoped_versions_v1']}
        for run in scoped:
            required=run.get('knowledge_pin_required');pin=pins.get(run['id'])
            if type(required) is not bool or required != (pin is not None):
                raise ValueError('Knowledge required marker/pin mismatch')
            if pin is not None:
                if pin.get('project_id')!=project:raise ValueError('Knowledge project mismatch')
                validate_pin(pin,run['id'])
                body=json.loads(pin['canonical_body'])
                version=versions[run['requirement_id'],run['requirement_version']]
                expected=dict(owner_id=owner,project_id=project,requirement_id=run['requirement_id'],requirement_version=run['requirement_version'],requirement_sha256=version['sha256'])
                if body.get('admission_binding')!=expected:raise ValueError('Knowledge requirement binding mismatch')
    return object_paths


def export_snapshot(rows, read_object, destination, *, owner, project, writers_stopped=False, provenance=None):
    if writers_stopped is not True:raise ValueError('Quiesced consistent export must be acknowledged')
    rows=clean_snapshot(rows)
    objects=validate(rows,owner,project)
    raw=packed(rows).encode()
    if len(raw)>MAX_RECORD_BYTES:raise ValueError('Snapshot bound exceeded')
    with _staging(destination) as stage:
        files={'records.json':raw}
        for name, expected in objects.items():
            data=read_object(name)
            if not isinstance(data,bytes) or len(data)>MAX_OBJECT_BYTES or data!=expected:
                raise ValueError('Missing/corrupt/noncanonical private Storage bytes')
            files['objects/'+name]=data
        for name,data in files.items():
            path=stage/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data);path.chmod(0o600)
        manifest={'format':KNOWLEDGE_FORMAT if PIN_TABLE in rows else FORMAT,'owner_id':owner,'project_id':project,'writers_stopped':True,
                  'read_only_restore':True,'tables':list(rows),
                  'files':{name:{'size':len(data),'sha256':sha(data)} for name,data in files.items()}}
        if provenance is not None:
            manifest['provenance']=deepcopy(provenance)
        p=stage/'manifest.json';p.write_text(json.dumps(manifest,indent=2)+'\n');p.chmod(0o600)
    return manifest


def verify(source):
    source=_source(source,check_name=False)
    p=source/'manifest.json'
    if p.is_symlink() or p.stat().st_size>2_000_000:raise ValueError('Invalid manifest')
    manifest=json.loads(p.read_text())
    if (manifest.get('format') not in {FORMAT,KNOWLEDGE_FORMAT} or not valid_tables(manifest.get('tables',[])) or
        manifest.get('writers_stopped') is not True or manifest.get('read_only_restore') is not True):
        raise ValueError('Invalid snapshot declaration')
    names=set(manifest['files']);actual=set()
    for path in source.rglob('*'):
        if path.is_symlink():raise ValueError('Symlink bundle refused')
        if path.is_file() and path!=p:actual.add(str(path.relative_to(source)))
        elif not path.is_file() and not path.is_dir():raise ValueError('Special file refused')
    if actual!=names or 'records.json' not in names:raise ValueError('Bundle inventory mismatch')
    for name,item in manifest['files'].items():
        _safe(name)
        limit=MAX_RECORD_BYTES if name=='records.json' else MAX_OBJECT_BYTES
        path=source/name
        if path.stat().st_size>limit or path.stat().st_size!=item['size'] or sha(path.read_bytes())!=item['sha256']:
            raise ValueError('Bundle integrity failure')
    rows=json.loads((source/'records.json').read_text())
    if (manifest['format']==KNOWLEDGE_FORMAT)!=(PIN_TABLE in rows):raise ValueError('Knowledge inventory/format mismatch')
    objects=validate(rows,manifest['owner_id'],manifest['project_id'])
    if names!={'records.json',*('objects/'+name for name in objects)}:raise ValueError('Object inventory differs from records')
    for name,data in objects.items():
        if (source/'objects'/name).read_bytes()!=data:raise ValueError('Evidence differs from normalized observation')
    return manifest,rows


def restore(source, destination):
    """Atomic new local target only. No live transport or Auth import exists."""
    manifest,rows=verify(source)
    with _staging(destination) as stage:
        with sqlite3.connect(stage/'recovery.sqlite') as db:
            db.execute('create table recovery_rows (table_name text not null, ordinal integer not null, body text not null, primary key(table_name,ordinal))')
            db.executemany('insert into recovery_rows values (?,?,?)',[(t,i,packed(r)) for t in rows for i,r in enumerate(rows[t])])
        (stage/'recovery.sqlite').chmod(0o600)
        for name in manifest['files']:
            if name.startswith('objects/'):
                p=stage/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes((Path(source)/name).read_bytes());p.chmod(0o600)
        (stage/'target.json').write_text(packed({'owner_id':manifest['owner_id'],'project_id':manifest['project_id'],'read_only':True,'tables':list(rows)}))
        (stage/'target.json').chmod(0o600)
    return manifest


class LocalReadbackAPI:
    """Read-only HostedStore port over restored normalized SQLite rows.

    Exercises application DTO/evidence validation; does not simulate SQL policies.
    """
    def __init__(self,target):
        self.target=_source(target,check_name=False)
        config=json.loads((self.target/'target.json').read_text());self.owner_id=config['owner_id'];self.project=config['project_id']
        with sqlite3.connect((self.target/'recovery.sqlite').as_uri()+'?mode=ro',uri=True) as db:
            self.rows={t:[json.loads(r[0]) for r in db.execute('select body from recovery_rows where table_name=? order by ordinal',(t,))] for t in config['tables']}
        validate(self.rows,self.owner_id,self.project)
    def scoped_checkpoint(self,run_id):
        row=next(r for r in self.rows.get(OPTIONAL_TABLE,[]) if r['run_id']==run_id)
        return deepcopy(row)
    def _owner(self):return None
    def read_root(self,pid):return self.rows['seo_qa_tool'][0] if pid==self.project else None
    def _requirement(self,rid,version=None):
        req=next(r for r in self.rows['seo_requirements'] if r['id']==rid)
        v=next(r for r in self.rows['seo_requirement_versions'] if r['requirement_id']==rid and r['version']==(version or req['current_version']))
        return {**{k:v[k] for k in ('title','urls','expected','content_hash','version')},'id':rid,'current_version':req['current_version']}
    def _run(self,row):
        reviews=[dict(id=r['id'],run_id=r['run_id'],decision=r['decision'],reason=r.get('reason',''),binding=r['binding_hash'],created=r.get('created_at')) for r in self.rows['seo_reviews'] if r['run_id']==row['id']]
        return dict(id=row['id'],owner=self.owner_id,requirement_id=row['requirement_id'],version=row['requirement_version'],state=row['state'],requirement=self._requirement(row['requirement_id'],row['requirement_version']),observations=[r['body'] for r in self.rows['seo_observations'] if r['run_id']==row['id']],reviews=reviews,data_mode='dummy/supabase-hosted',acceptance_current=False)
    def _request(self,method,path,**kwargs):
        body=kwargs.get('json',{});action=body.get('action');payload=body.get('payload',{})
        if method!='POST' or path!='/rest/v1/rpc/seo_workflow' or payload.get('project_id')!=self.project:raise ValueError('Only selected read RPCs allowed')
        if action=='list_runs':return [self._run(r) for r in sorted(self.rows['seo_runs'],key=lambda r:r.get('generation',0),reverse=True)]
        if action in {'get_run','export_report'}:return self._run(next(r for r in self.rows['seo_runs'] if r['id']==payload['run_id']))
        if action=='get_requirement':return self._requirement(payload['id'])
        if action=='list_requirements':return [self._requirement(r['id']) for r in self.rows['seo_requirements']]
        if action=='list_imports':return [dict(id=r['id'],kind=r['kind'],raw=r['raw'],result=r['body'],hash=r['content_hash']) for r in self.rows['seo_imports']]
        raise ValueError('Restored sandbox rejects every mutation')
    def _json(self,value):return deepcopy(value)
    def reconcile_evidence(self,operation):
        name=operation['path'];_safe(name)
        if not name.startswith(self.owner_id+'/'+self.project+'/'):raise ValueError('Object outside selected owner/project')
        if sha((self.target/'objects'/name).read_bytes())!=operation['sha256']:raise ValueError('Restored private evidence corrupted')
        return {'state':'confirmed'}


def main():
    parser=argparse.ArgumentParser(description=__doc__);commands=parser.add_subparsers(dest='command',required=True)
    p=commands.add_parser('export');p.add_argument('fixture');p.add_argument('destination');p.add_argument('--writers-stopped',action='store_true')
    p=commands.add_parser('verify');p.add_argument('source')
    p=commands.add_parser('restore');p.add_argument('source');p.add_argument('destination')
    args=parser.parse_args()
    if args.command=='export':
        fixture=_source(args.fixture,check_name=False);config=json.loads((fixture/'fixture.json').read_text());rows=json.loads((fixture/'records.json').read_text())
        def read(name):
            _safe(name);return _source(fixture/'objects'/name,check_name=False).read_bytes()
        result=export_snapshot(rows,read,args.destination,owner=config['owner_id'],project=config['project_id'],writers_stopped=args.writers_stopped)
    elif args.command=='verify':result=verify(args.source)[0]
    else:result=restore(args.source,args.destination)
    print(json.dumps({'format':result['format'],'verified':True,'live_effects':[],'spend_usd':0}))

if __name__=='__main__':main()
