"""Durable local development adapter. Not a Supabase or production-auth claim."""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .models import Observation, RequirementInput


def packed(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(packed(value).encode()).hexdigest()


def identifier():
    return str(uuid.uuid4())


class Store:
    def __init__(self, path, evidence_dir=None, owner_id='local-dummy-owner'):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.evidence_dir = Path(evidence_dir or self.path.parent / 'evidence')
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.owner = owner_id
        with self.connection() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS requirements(id TEXT PRIMARY KEY,owner TEXT NOT NULL,version INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS versions(id TEXT NOT NULL,version INTEGER NOT NULL,body TEXT NOT NULL,hash TEXT NOT NULL,PRIMARY KEY(id,version),FOREIGN KEY(id) REFERENCES requirements(id));
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,owner TEXT NOT NULL,requirement_id TEXT NOT NULL,version INTEGER NOT NULL,state TEXT NOT NULL,token TEXT,lease REAL,created REAL NOT NULL,error TEXT,FOREIGN KEY(requirement_id,version) REFERENCES versions(id,version));
            CREATE TABLE IF NOT EXISTS observations(id TEXT PRIMARY KEY,run_id TEXT NOT NULL,url TEXT NOT NULL,body TEXT NOT NULL,hash TEXT NOT NULL,UNIQUE(run_id,url),FOREIGN KEY(run_id) REFERENCES runs(id));
            CREATE TABLE IF NOT EXISTS reviews(id TEXT PRIMARY KEY,run_id TEXT NOT NULL,decision TEXT NOT NULL,reason TEXT NOT NULL,binding TEXT NOT NULL,created REAL NOT NULL,FOREIGN KEY(run_id) REFERENCES runs(id));
            CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,owner TEXT NOT NULL,run_id TEXT NOT NULL,binding TEXT NOT NULL,payload TEXT NOT NULL,state TEXT NOT NULL,receipt TEXT);
            CREATE TABLE IF NOT EXISTS fixture_destinations(id TEXT PRIMARY KEY,body TEXT NOT NULL,version INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,body TEXT NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS import_records(id TEXT PRIMARY KEY,owner TEXT NOT NULL,kind TEXT NOT NULL,raw TEXT NOT NULL,body TEXT NOT NULL,hash TEXT NOT NULL,created REAL NOT NULL);
            CREATE TRIGGER IF NOT EXISTS versions_no_update BEFORE UPDATE ON versions BEGIN SELECT RAISE(ABORT,'Immutable version'); END;
            CREATE TRIGGER IF NOT EXISTS versions_no_delete BEFORE DELETE ON versions BEGIN SELECT RAISE(ABORT,'Immutable version'); END;
            CREATE TRIGGER IF NOT EXISTS observations_no_update BEFORE UPDATE ON observations BEGIN SELECT RAISE(ABORT,'Immutable observation'); END;
            CREATE TRIGGER IF NOT EXISTS observations_no_delete BEFORE DELETE ON observations BEGIN SELECT RAISE(ABORT,'Immutable observation'); END;
            CREATE TRIGGER IF NOT EXISTS reviews_no_update BEFORE UPDATE ON reviews BEGIN SELECT RAISE(ABORT,'Immutable review'); END;
            CREATE TRIGGER IF NOT EXISTS reviews_no_delete BEFORE DELETE ON reviews BEGIN SELECT RAISE(ABORT,'Immutable review'); END;
            ''')

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA synchronous=FULL')
        try:
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def event(self, db, kind, body):
        db.execute('INSERT INTO events(kind,body,created) VALUES(?,?,?)', (kind, packed(body), time.time()))

    def _requirement(self, db, rid, version=None):
        row = db.execute('SELECT * FROM requirements WHERE id=? AND owner=?', (rid,self.owner)).fetchone()
        if row is None:
            raise ValueError('Requirement unavailable')
        v = version if version is not None else row['version']
        rec = db.execute('SELECT * FROM versions WHERE id=? AND version=?',(rid,v)).fetchone()
        if rec is None or digest(json.loads(rec['body'])) != rec['hash']:
            raise ValueError('Requirement integrity failure')
        return dict(json.loads(rec['body']), id=rid, version=v, current_version=row['version'])

    def create_requirement(self, title, urls, expected):
        value = RequirementInput(title=title, urls=urls, expected=expected).model_dump()
        rid = identifier()
        with self.connection(True) as db:
            db.execute('INSERT INTO requirements VALUES(?,?,1)',(rid,self.owner))
            db.execute('INSERT INTO versions VALUES(?,?,?,?)',(rid,1,packed(value),digest(value)))
            self.event(db,'requirement_created',{'id':rid,'version':1})
        return self.get_requirement(rid)

    def get_requirement(self, rid):
        with self.connection() as db:
            return self._requirement(db,rid)

    def list_requirements(self):
        with self.connection() as db:
            return [self._requirement(db,x['id']) for x in db.execute('SELECT id FROM requirements WHERE owner=? ORDER BY rowid DESC',(self.owner,)).fetchall()]

    def revise_requirement(self, rid, expected):
        with self.connection(True) as db:
            old = self._requirement(db,rid)
            value = RequirementInput(title=old['title'],urls=old['urls'],expected=expected).model_dump()
            v = old['version'] + 1
            db.execute('INSERT INTO versions VALUES(?,?,?,?)',(rid,v,packed(value),digest(value)))
            db.execute('UPDATE requirements SET version=? WHERE id=?',(v,rid))
            self.event(db,'requirement_revised',{'id':rid,'version':v})
        return self.get_requirement(rid)

    def create_run(self, requirement_id):
        with self.connection(True) as db:
            req = self._requirement(db,requirement_id)
            run_id = identifier()
            db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)',(run_id,self.owner,requirement_id,req['version'],'queued',None,None,time.time(),None))
        return self.get_run(run_id)

    def _run(self, db, run_id):
        row = db.execute('SELECT * FROM runs WHERE id=? AND owner=?',(run_id,self.owner)).fetchone()
        if row is None:
            raise ValueError('Run unavailable')
        return dict(row)

    def get_run(self, run_id):
        with self.connection() as db:
            run = self._run(db,run_id)
            run['requirement'] = self._requirement(db,run['requirement_id'],run['version'])
            run['observations'] = []
            for row in db.execute('SELECT * FROM observations WHERE run_id=? ORDER BY rowid',(run_id,)):
                body = json.loads(row['body'])
                f = self.evidence_dir / (row['hash']+'.json')
                if digest(body) != row['hash'] or not f.is_file() or hashlib.sha256(f.read_bytes()).hexdigest() != row['hash']:
                    raise ValueError('Missing or changed evidence')
                run['observations'].append(body)
            run['findings'] = [f for obs in run['observations'] for f in obs['findings']]
            run['reviews'] = [dict(x) for x in db.execute('SELECT * FROM reviews WHERE run_id=? ORDER BY rowid',(run_id,))]
            run['stale'] = run['version'] != run['requirement']['current_version']
            run['data_mode'] = 'dummy/local-development'
            return run

    def list_runs(self):
        with self.connection() as db:
            ids = [x['id'] for x in db.execute('SELECT id FROM runs WHERE owner=? ORDER BY rowid DESC',(self.owner,))]
        return [self.get_run(x) for x in ids]

    def list_queue_states(self):
        """Owner metadata only; no evidence reads or lease changes."""
        with self.connection() as db:
            return [dict(row) for row in db.execute(
                'SELECT id,state,created,lease FROM runs WHERE owner=? ORDER BY created DESC',
                (self.owner,))]

    def list_run_summaries(self):
        """Navigation metadata only; never a substitute for verified reports."""
        with self.connection() as db:
            rows = [dict(row) for row in db.execute(
                'SELECT id,requirement_id,version,state FROM runs WHERE owner=? ORDER BY rowid DESC', (self.owner,))]
            for row in rows:
                row['requirement'] = self._requirement(db, row['requirement_id'], row['version'])
                row['stale'] = row['version'] != row['requirement']['current_version']
        for row in rows:
            try:
                self.get_run(row['id'])
                row['evidence_status'] = 'verified'
            except (ValueError, OSError):
                row['evidence_status'] = 'unavailable'
        return rows

    def save_import(self,kind,raw,result):
        if kind not in ('crawl','queries','comparison'):
            raise ValueError('Unknown import type')
        body={'kind':kind,'raw':raw,'result':result}
        h=digest(body)
        with self.connection(True) as db:
            old=db.execute('SELECT id FROM import_records WHERE owner=? AND hash=?',(self.owner,h)).fetchone()
            if old:return old['id']
            iid=identifier()
            db.execute('INSERT INTO import_records VALUES(?,?,?,?,?,?,?)',(iid,self.owner,kind,raw,packed(result),h,time.time()))
            self.event(db,'import_retained',{'id':iid,'hash':h})
        return iid

    def list_imports(self):
        with self.connection() as db:
            rows=db.execute('SELECT * FROM import_records WHERE owner=? ORDER BY rowid DESC',(self.owner,)).fetchall()
            result=[]
            for row in rows:
                body=json.loads(row['body'])
                if digest({'kind':row['kind'],'raw':row['raw'],'result':body})!=row['hash']:
                    raise ValueError('Import integrity failure')
                result.append({'id':row['id'],'kind':row['kind'],'hash':row['hash'],'result':body})
            return result

    def execute_run(self, run_id, inspector_callable):
        token = identifier()
        with self.connection(True) as db:
            run = self._run(db,run_id)
            if run['state'] == 'completed':
                return self.get_run(run_id)
            if run['state'] != 'queued':
                raise ValueError('Run is not queued; reconcile interrupted work')
            req = self._requirement(db,run['requirement_id'],run['version'])
            if req['version'] != req['current_version']:
                raise ValueError('Requirement changed before inspection')
            db.execute('UPDATE runs SET state=?,token=?,lease=? WHERE id=?',('running',token,time.time()+300,run_id))
        try:
            for url in req['urls']:
                with self.connection() as db:
                    current = self._run(db,run_id)
                    if current['state'] != 'running' or current['token'] != token or current['lease'] <= time.time():
                        raise ValueError('Worker ownership expired before capture')
                value = Observation.model_validate(inspector_callable(url,req['expected'])).model_dump()
                if value['url'] != url or not value['findings'] or not value['evidence']:
                    raise ValueError('Incomplete or mismatched observation')
                if 'seo_qa' in req['expected']:
                    from .scope_criteria import validate_observation
                    validate_observation(req['expected']['seo_qa'], url, value)
                checks={'canonical':'canonical','indexable':'indexing_directives','crawlable':'robots_access',
                        'redirect_final':'redirect_destination','max_redirects':'redirect_count','allowed_statuses':'http_status'}
                for key,check in checks.items():
                    if key in req['expected'] and not any(f['check']==check and f['expected']==req['expected'][key] and f['outcome']!='not_applicable' for f in value['findings']):
                        raise ValueError('Missing configured check: '+key)
                for phrase in req['expected'].get('required_text',[]):
                    if not any(f['check']=='required_text' and f['expected']==phrase and f['outcome']!='not_applicable' for f in value['findings']):
                        raise ValueError('Missing required-text observation')
                h = digest(value)
                target = self.evidence_dir/(h+'.json')
                tmp = self.evidence_dir/(identifier()+'.tmp')
                tmp.write_text(packed(value),encoding='utf-8')
                tmp.replace(target)
                with self.connection(True) as db:
                    current = self._run(db,run_id)
                    if current['state'] != 'running' or current['token'] != token or current['lease'] < time.time():
                        raise ValueError('Worker ownership expired')
                    db.execute('INSERT INTO observations VALUES(?,?,?,?,?)',(identifier(),run_id,url,packed(value),h))
            with self.connection(True) as db:
                current=self._run(db,run_id)
                if current['state']!='running' or current['token']!=token or current['lease']<time.time():
                    raise ValueError('Worker ownership expired')
                db.execute('UPDATE runs SET state=?,token=NULL,lease=NULL WHERE id=?',('completed',run_id))
                self.event(db,'run_completed',{'run':run_id})
        except Exception as error:
            with self.connection(True) as db:
                db.execute('UPDATE runs SET state=?,error=?,token=NULL WHERE id=? AND token=?',('error',type(error).__name__+': '+str(error)[:200],run_id,token))
            raise
        return self.get_run(run_id)

    def recover_expired(self):
        with self.connection(True) as db:
            rows=db.execute('SELECT id FROM runs WHERE owner=? AND state=? AND lease<?',(self.owner,'running',time.time())).fetchall()
            for row in rows:
                db.execute('UPDATE runs SET state=?,token=NULL,error=? WHERE id=?',('interrupted','Worker lease expired; create a fresh inspection',row['id']))
            return len(rows)

    def _binding(self, db, run_id, allow_unresolved=False):
        run=self._run(db,run_id)
        req=self._requirement(db,run['requirement_id'])
        latest=db.execute('SELECT id FROM runs WHERE requirement_id=? AND owner=? ORDER BY rowid DESC LIMIT 1',(run['requirement_id'],self.owner)).fetchone()
        if run['state']!='completed' or run['version']!=req['version'] or latest['id']!=run_id:
            raise ValueError('Run incomplete, superseded or stale')
        verified=self.get_run(run_id)
        if len(verified['observations'])!=len(req['urls']):
            raise ValueError('Incomplete URL coverage')
        outcomes=[x['outcome'] for x in verified['findings'] if x['outcome']!='not_applicable']
        if not outcomes or (not allow_unresolved and any(x in ('blocked','inconclusive') for x in outcomes)):
            raise ValueError('Unresolved observations cannot be accepted')
        return digest({'run':run_id,'requirement':req,'observations':verified['observations']}),outcomes

    def review(self, run_id, decision, reason='', expected_version=None):
        if decision not in ('accepted','accepted_with_waiver','rejected'):
            raise ValueError('Unsupported review decision')
        with self.connection(True) as db:
            run=self._run(db,run_id)
            if expected_version is not None and expected_version!=run['version']:
                raise ValueError('Review version mismatch')
            binding,outcomes=self._binding(db,run_id,allow_unresolved=decision=='rejected')
            if decision=='accepted' and 'fail' in outcomes:
                raise ValueError('Failed checks require explicit waiver or rejection')
            if decision in ('accepted_with_waiver','rejected') and not reason.strip():
                raise ValueError('Review reason required')
            result={'id':identifier(),'run_id':run_id,'decision':decision,'reason':reason,'binding':binding,'created':time.time()}
            db.execute('INSERT INTO reviews VALUES(:id,:run_id,:decision,:reason,:binding,:created)',result)
            self.event(db,'review',result)
            return result

    def export_report(self, run_id):
        run=self.get_run(run_id)
        with self.connection() as db:
            latest = db.execute('SELECT id FROM runs WHERE requirement_id=? AND owner=? ORDER BY rowid DESC LIMIT 1',
                                (run['requirement_id'], self.owner)).fetchone()
        run['latest_run_id'] = latest['id']
        run['superseded'] = run['latest_run_id'] != run_id
        run['acceptance_current']=False
        try:
            with self.connection() as db:
                binding,_=self._binding(db,run_id)
                run['acceptance_current']=bool(run['reviews'] and run['reviews'][-1]['decision'] in ('accepted','accepted_with_waiver') and run['reviews'][-1]['binding']==binding)
        except ValueError:
            pass
        run['limits']=['Local development adapter; hosted Supabase/Auth isolation not established.','Technical checks do not prove indexing, citations, visits or conversions.']
        run['coverage']={'expected':len(run['requirement']['urls']),'observed':len(run['observations']),'sampled':False}
        from .scoring import with_scoring
        return with_scoring(run)

    def propose_notion(self,run_id):
        report=self.export_report(run_id)
        if not report['acceptance_current']:
            raise ValueError('Current acceptance required')
        payload={'run_id':run_id,'decision':report['reviews'][-1]['decision'],'coverage':report['coverage'],'data_mode':report['data_mode']}
        binding=digest(payload)
        with self.connection(True) as db:
            old=db.execute('SELECT * FROM operations WHERE owner=? AND binding=?',(self.owner,binding)).fetchone()
            if old:return dict(old)
            oid=identifier()
            db.execute('INSERT INTO operations VALUES(?,?,?,?,?,?,?)',(oid,self.owner,run_id,binding,packed(payload),'proposed',None))
        return {'id':oid,'state':'proposed','payload':payload}

    def apply_fixture_notion(self,operation_id,lose_response=False):
        with self.connection(True) as db:
            op=db.execute('SELECT * FROM operations WHERE id=? AND owner=?',(operation_id,self.owner)).fetchone()
            if not op:raise ValueError('Operation unavailable')
            if op['state'] in ('applied','unknown'):
                return {'id':operation_id,'state':op['state'],'receipt':op['receipt']}
            if not self.export_report(op['run_id'])['acceptance_current']:
                raise ValueError('Acceptance stale')
            db.execute('INSERT INTO fixture_destinations VALUES(?,?,1)',(operation_id,op['payload']))
            receipt=packed({'destination':'fixture-only','operation':operation_id,'hash':digest(json.loads(op['payload']))})
            state='unknown' if lose_response else 'applied'
            db.execute('UPDATE operations SET state=?,receipt=? WHERE id=?',(state,receipt,operation_id))
        return {'id':operation_id,'state':state,'receipt':None if lose_response else receipt}

    def reconcile_notion(self,operation_id):
        with self.connection(True) as db:
            op=db.execute('SELECT * FROM operations WHERE id=? AND owner=?',(operation_id,self.owner)).fetchone()
            if not op:raise ValueError('Operation unavailable')
            dest=db.execute('SELECT * FROM fixture_destinations WHERE id=?',(operation_id,)).fetchone()
            if dest and dest['body']==op['payload']:
                db.execute('UPDATE operations SET state=? WHERE id=?',('applied',operation_id))
                return {'id':operation_id,'state':'applied','destination':'fixture-only'}
            return {'id':operation_id,'state':'unresolved'}

    def backup(self,destination):
        target=Path(destination)
        target.mkdir(parents=True,exist_ok=False)
        with self.connection() as db:
            backup=sqlite3.connect(target/'database.sqlite')
            db.backup(backup)
            backup.close()
        shutil.copytree(self.evidence_dir,target/'evidence')
        manifest={str(p.relative_to(target)):hashlib.sha256(p.read_bytes()).hexdigest() for p in target.rglob('*') if p.is_file()}
        (target/'manifest.json').write_text(packed(manifest))
        return target

    @classmethod
    def restore(cls,source,destination,owner_id='local-dummy-owner'):
        source,destination=Path(source),Path(destination)
        manifest=json.loads((source/'manifest.json').read_text())
        for name,h in manifest.items():
            p=(source/name).resolve()
            if not p.is_relative_to(source.resolve()) or hashlib.sha256(p.read_bytes()).hexdigest()!=h:
                raise ValueError('Backup integrity failure')
        destination.mkdir(parents=True,exist_ok=False)
        shutil.copy2(source/'database.sqlite',destination/'database.sqlite')
        shutil.copytree(source/'evidence',destination/'evidence')
        result=cls(destination/'database.sqlite',destination/'evidence',owner_id)
        result.list_runs()
        return result
