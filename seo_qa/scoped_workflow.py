"""Explicit scoped owner workflow. No schedules, service key or external actions.

The database owns requirements, versions, lease tokens, transitions and immutable
checkpoint evidence. The application recomputes deterministic meaning on every
read and write. Owner-provided evidence is advisory, never executable behavior.
"""
from copy import deepcopy
import hashlib
import json
import time
from uuid import UUID,uuid4

from .models import RequirementInput,Observation
from .scope_criteria import canonical,validate_observation
from .scoped_storage import PROFILE,validate_record
from .supabase_adapter import SupabaseError,UncertainWrite

CONTRACT='seo-scoped-workflow/v1'

def digest(value):return hashlib.sha256(canonical(value)).hexdigest()

def flag(values):
    value=values.get('SEO_SCOPED_WORKFLOW_ENABLED','false')
    if value not in ('true','false'):
        raise ValueError('SEO_SCOPED_WORKFLOW_ENABLED must be true or false')
    return value=='true'

class ScopedStore:
    scoped_workflow_enabled=True
    def __init__(self,api,project_id):
        self.api=api;self.owner=api.owner_id;self.project_id=str(UUID(project_id))
        probe=self._rpc('contract')
        if probe!={'contract':CONTRACT,'criteria_version':'seo-qa-general/1.0.0','validation':'owner-advisory-no-executable-actions'}:
            raise SupabaseError('Scoped workflow migration contract mismatch')

    def _rpc(self,action,**payload):
        self.api._owner()
        body={'project_id':self.project_id,**payload}
        read=action in {'contract','lookup','get_requirement','list_requirements','get_run','list_runs','list_queue_states','latest_run'}
        op=None if read else {'kind':'scoped_workflow_rpc','action':action,'payload':body}
        response=self.api._request('POST','/rest/v1/rpc/seo_scoped_workflow_v1',operation=op,json={'action':action,'payload':body})
        try:value=self.api._json(response)
        except SupabaseError as exc:
            if op:raise UncertainWrite(op) from exc
            raise
        if not isinstance(value,(dict,list)):
            if op:raise UncertainWrite(op)
            raise SupabaseError('Invalid scoped workflow response')
        return value

    def contains(self,kind,key):
        value=self._rpc('lookup',kind=kind,id=str(UUID(key)))
        if type(value.get('found')) is not bool:raise SupabaseError('Invalid scoped lookup')
        return value['found']

    def _requirement(self,value):
        try:
            if str(UUID(value['id']))!=value['id']:raise ValueError()
            body=RequirementInput(title=value['title'],urls=value['urls'],expected=value['expected']).model_dump()
            if set(body['expected'])!={'seo_qa'} or digest(body)!=value['content_hash']:raise ValueError()
            if type(value['version']) is not int or type(value['current_version']) is not int or not 1<=value['version']<=value['current_version']:raise ValueError()
        except (KeyError,TypeError,ValueError) as exc:raise SupabaseError('Scoped requirement integrity failure') from exc
        return deepcopy(value)

    def _ack_requirement(self,value,body,rid,version):
        value=self._requirement(value)
        if (value['id']!=rid or value['version']!=version or
                value['current_version']!=version or
                {k:value[k] for k in ('title','urls','expected')}!=body):
            raise SupabaseError('Requirement acknowledgement mismatch; reconcile before retry')
        return value

    def create_requirement(self,title,urls,expected):
        body=RequirementInput(title=title,urls=urls,expected=expected).model_dump()
        if set(expected)!={'seo_qa'}:raise ValueError('Scoped requirement required')
        rid=str(uuid4())
        value=self._rpc('create_requirement',id=rid,canonical_body=canonical(body).decode(),sha256=digest(body))
        return self._ack_requirement(value,body,rid,1)

    def get_requirement(self,rid):
        value=self._requirement(self._rpc('get_requirement',id=str(UUID(rid))))
        if value['id']!=rid:raise SupabaseError('Requirement identity mismatch')
        return value

    def list_requirements(self):return [self._requirement(v) for v in self._rpc('list_requirements')]

    def revise_requirement(self,rid,expected):
        old=self.get_requirement(rid)
        body=RequirementInput(title=old['title'],urls=old['urls'],expected=expected).model_dump()
        value=self._rpc('revise_requirement',id=rid,expected_version=old['current_version'],canonical_body=canonical(body).decode(),sha256=digest(body))
        return self._ack_requirement(value,body,rid,old['current_version']+1)

    def _checkpoint_payload(self,record,revision):
        validate_record(record)
        if record['owner']!=self.owner or record['project_id']!=self.project_id:raise SupabaseError('Scoped checkpoint owner mismatch')
        return {'run_id':record['id'],'canonical_body':canonical(record).decode(),'sha256':digest(record),'expected_revision':revision}

    def _load(self,run_id):
        wire=self._rpc('get_run',run_id=str(UUID(run_id)))
        return self._validate_wire(wire,run_id)

    def _validate_wire(self,wire,run_id=None):
        try:
            cp=wire['checkpoint'];record=validate_record(json.loads(cp['canonical_body']))
            if canonical(record).decode()!=cp['canonical_body'] or digest(record)!=cp['sha256']:raise ValueError()
            req=self._requirement(wire['requirement'])
            if (record['owner']!=self.owner or record['project_id']!=self.project_id or
                run_id is not None and record['id']!=run_id or
                record['requirement']!={k:req[k] for k in ('title','urls','expected')} or
                type(cp['revision']) is not int or cp['revision']<1):raise ValueError()
            if record['state']=='running' and (not wire['token'] or wire['lease'] is None):raise ValueError()
        except (KeyError,ValueError,TypeError) as exc:raise SupabaseError('Scoped run integrity failure') from exc
        return wire,record,req

    def _run(self,wire,record,req):
        return {'id':record['id'],'owner':self.owner,'requirement_id':req['id'],'version':req['version'],
                'requirement':req,'state':record['state'],'error':record['error'],
                'lease':wire['lease'],'created':wire['created'],'generation':wire['generation'],
                'data_mode':'owner-advisory/scoped-hosted-v1','stale':req['version']!=req['current_version'],
                'observations':deepcopy(record['observations']),
                'findings':[f for o in record['observations'] for f in o['findings']],
                'reviews':wire['reviews'],'validation':'deterministic-app-verified-owner-advisory',
                'executable_actions_enabled':False}

    def create_run(self,requirement_id):
        req=self.get_requirement(requirement_id);rid=str(uuid4())
        record={'profile':PROFILE,'id':rid,'owner':self.owner,'project_id':self.project_id,'state':'queued',
                'requirement':{k:req[k] for k in ('title','urls','expected')},'observations':[],'error':None}
        wire=self._rpc('create_run',requirement_id=req['id'],requirement_version=req['current_version'],**self._checkpoint_payload(record,0))
        return self._run(*self._validate_wire(wire,rid))

    def get_run(self,run_id):return self._run(*self._load(run_id))

    def list_runs(self):return [self._run(*self._validate_wire(w)) for w in self._rpc('list_runs')]

    def list_run_summaries(self):
        return [dict(id=r['id'],requirement_id=r['requirement_id'],requirement=r['requirement'],version=r['version'],state=r['state'],stale=r['stale'],evidence_status='verified',created=r['created']) for r in self.list_runs()]

    def list_queue_states(self):
        rows=self._rpc('list_queue_states')
        for row in rows:
            if not isinstance(row,dict) or set(row)!={'id','state'} or row['state'] not in {'queued','running','completed','error','interrupted'}:raise SupabaseError('Invalid scoped queue')
            str(UUID(row['id']))
        return rows

    def execute_run(self,run_id,inspector_callable):
        wire,record,req=self._load(run_id)
        if record['state']=='completed':return self._run(wire,record,req)
        if record['state']!='queued':raise ValueError('Run is not queued; reconcile before new work')
        token=str(uuid4());record['state']='running'
        claimed=self._rpc('claim',token=token,**self._checkpoint_payload(record,wire['checkpoint']['revision']))
        wire,record,req=self._validate_wire(claimed,run_id)
        if wire['token']!=token:raise SupabaseError('Scoped lease claim mismatch')
        try:
            for url in req['urls']:
                # Revalidate ownership before each capture, including after sleep
                # or a slow prior checkpoint. Never refresh or extend the lease.
                current,body,_=self._load(run_id)
                if (body['state']!='running' or current['token']!=token or
                    current['checkpoint']['revision']!=wire['checkpoint']['revision'] or
                    current['lease']<=time.time()):
                    raise ValueError('Worker ownership expired or changed before capture')
                raw=Observation.model_validate(inspector_callable(url,req['expected'])).model_dump()
                if raw['url']!=url:raise ValueError('Observation target mismatch')
                validate_observation(req['expected']['seo_qa'],url,raw)
                record['observations'].append(raw)
                next_wire=self._rpc('append',token=token,**self._checkpoint_payload(record,wire['checkpoint']['revision']))
                wire,record,req=self._validate_wire(next_wire,run_id)
            record['state']='completed'
            result=self._rpc('complete',token=token,**self._checkpoint_payload(record,wire['checkpoint']['revision']))
            return self._run(*self._validate_wire(result,run_id))
        except Exception as error:
            try:
                current,body,_=self._load(run_id)
                if body['state']=='running' and current['token']==token:
                    body['state']='error';body['error']=type(error).__name__[:200]
                    self._rpc('fail',token=token,**self._checkpoint_payload(body,current['checkpoint']['revision']))
            except Exception:pass
            raise

    def recover_expired(self):
        count=0
        for w in self._rpc('list_runs'):
            wire,record,_=self._validate_wire(w)
            if record['state']=='running':
                # DB clock is authoritative; interrupt probe rejects unexpired or
                # concurrently renewed leases. No inspection is automatically replayed.
                body=deepcopy(record);body['state']='interrupted'
                result=self._rpc('interrupt_if_expired',**self._checkpoint_payload(body,wire['checkpoint']['revision']))
                if result.get('interrupted') is True:count+=1
        return count

    def _binding(self,run,allow_unresolved=False):
        outcomes=[f['outcome'] for f in run['findings'] if f['outcome']!='not_applicable']
        if run['state']!='completed' or run['stale'] or not outcomes or (not allow_unresolved and set(outcomes)&{'blocked','inconclusive'}):raise ValueError('Current completed resolved run required')
        if self._rpc('latest_run',requirement_id=run['requirement_id'])['id']!=run['id']:raise ValueError('Run is superseded')
        return digest({'run_id':run['id'],'requirement_id':run['requirement_id'],'version':run['version'],'observations':run['observations']})

    def review(self,run_id,decision,reason='',expected_version=None):
        run=self.get_run(run_id)
        if decision not in {'accepted','accepted_with_waiver','rejected'}:raise ValueError('Invalid decision')
        if expected_version is not None and expected_version!=run['version']:raise ValueError('Review version mismatch')
        if decision!='accepted' and not reason.strip():raise ValueError('Review reason required')
        if decision=='accepted' and any(f['outcome']=='fail' for f in run['findings']):raise ValueError('Failed findings need waiver or rejection')
        binding=self._binding(run,decision=='rejected')
        return self._rpc('review',run_id=run_id,id=str(uuid4()),decision=decision,reason=reason,binding=binding)

    def export_report(self,run_id):
        run=self.get_run(run_id);latest=self._rpc('latest_run',requirement_id=run['requirement_id'])['id']
        run['latest_run_id']=latest;run['superseded']=latest!=run_id;run['acceptance_current']=False
        try:
            binding=self._binding(run)
            run['acceptance_current']=bool(run['reviews'] and run['reviews'][-1]['decision'] in {'accepted','accepted_with_waiver'} and run['reviews'][-1]['binding']==binding)
        except ValueError:pass
        run['coverage']={'expected':len(run['requirement']['urls']),'observed':len(run['observations']),'sampled':False}
        run['limits']=['Owner-supplied advisory evidence; deterministic meaning verified by this application, not a ranking guarantee.','No executable actions, automatic publishing, paid inference or hosted knowledge activation.']
        from .scoring import with_scoring
        return with_scoring(run)

class CompositeStore:
    """Route scoped UUIDs explicitly; transport errors never become fallback."""
    scoped_workflow_enabled=True
    def __init__(self,legacy,scoped):
        self.legacy=legacy;self.scoped=scoped;self.api=legacy.api;self.owner=legacy.owner;self.project_id=legacy.project_id
    def __getattr__(self,name):return getattr(self.legacy,name)
    def _store(self,kind,key):return self.scoped if self.scoped.contains(kind,key) else self.legacy
    def create_requirement(self,title,urls,expected):return (self.scoped if 'seo_qa' in expected else self.legacy).create_requirement(title,urls,expected)
    def get_requirement(self,rid):return self._store('requirement',rid).get_requirement(rid)
    def revise_requirement(self,rid,expected):return self._store('requirement',rid).revise_requirement(rid,expected)
    def create_run(self,rid):return self._store('requirement',rid).create_run(rid)
    def get_run(self,rid):return self._store('run',rid).get_run(rid)
    def execute_run(self,rid,inspect):return self._store('run',rid).execute_run(rid,inspect)
    def export_report(self,rid):return self._store('run',rid).export_report(rid)
    def review(self,rid,*args,**kw):return self._store('run',rid).review(rid,*args,**kw)
    def list_requirements(self):return self.scoped.list_requirements()+self.legacy.list_requirements()
    def list_runs(self):return self.scoped.list_runs()+self.legacy.list_runs()
    def list_run_summaries(self):return self.scoped.list_run_summaries()+self.legacy.list_run_summaries()
    def list_queue_states(self):return self.scoped.list_queue_states()+self.legacy.list_queue_states()
    def recover_expired(self):return self.scoped.recover_expired()+self.legacy.recover_expired()
    def propose_notion(self,rid):
        if self.scoped.contains('run',rid):raise ValueError('Scoped evidence cannot authorize executable actions')
        return self.legacy.propose_notion(rid)

def compose(legacy,*,enabled=False):
    return CompositeStore(legacy,ScopedStore(legacy.api,legacy.project_id)) if enabled else legacy
