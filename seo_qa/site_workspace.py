"""Bounded offline uploaded-site workspace. No fetch, crawl, provider or hosted SQL."""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from uuid import uuid4
from urllib.parse import urlsplit, urlunsplit
from fastapi import Form, File, UploadFile, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from .store import Store
from . import workspace_exports
from .request_context import request_csrf
from . import crawl_uploads, architecture_analysis, architecture_upload, performance_analysis, performance_upload, explanation_presenter

LIMIT=2_000_000
ROLES=('owner','editor','developer')
LEVELS=explanation_presenter.MODES
def site_url(raw):
    if not isinstance(raw,str):raise ValueError('Site URL must be text.')
    p=urlsplit(raw.strip())
    if p.scheme not in ('http','https') or not p.hostname or p.username or p.password or p.fragment:
        raise ValueError('Use an HTTP(S) site URL without credentials or fragments.')
    _=p.port
    return urlunsplit((p.scheme,p.netloc.lower(),p.path or '/',p.query,''))
def document(payload):
    def pairs(rows):
        result={}
        for k,v in rows:
            if k in result:raise ValueError('Duplicate JSON key')
            result[k]=v
        return result
    if len(payload)>LIMIT:raise ValueError('Maximum upload size is 2 MB.')
    result=json.loads(payload.decode('utf-8-sig'),object_pairs_hook=pairs,
                      parse_constant=lambda _:(_ for _ in ()).throw(ValueError('Nonfinite JSON')))
    def bounded(v,depth=0):
        if depth>40:raise ValueError('JSON nesting limit exceeded.')
        if isinstance(v,dict):
            for x in v.values():bounded(x,depth+1)
        if isinstance(v,list):
            if len(v)>10000:raise ValueError('Maximum 10,000 items.')
            for x in v:bounded(x,depth+1)
    bounded(result)
    if not isinstance(result,dict):raise ValueError('JSON must contain an object.')
    return result
def install_routes(app,env,csrf_check):
    def available():
        return isinstance(app.state.store,Store) and not hasattr(app.state,'production_settings')
    def history():
        if not available():return []
        with app.state.store.connection() as db:
            if not db.execute("SELECT name FROM sqlite_master WHERE name='site_workspace_records'").fetchone():return []
            return [json.loads(x['body']) for x in db.execute('SELECT body FROM site_workspace_records WHERE owner=? ORDER BY rowid DESC LIMIT 30',(app.state.store.owner,))]
    def page(record=None,error=None,status=200):
        return HTMLResponse(env.get_template('site_workspace.html').render(
            csrf=request_csrf(app.state.csrf_token),available=available(),records=history(),
            record=record,error=error,roles=ROLES,levels=LEVELS),status_code=status,
            headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
    def save(record):
        with app.state.store.connection(True) as db:
            db.execute('CREATE TABLE IF NOT EXISTS site_workspace_records(id TEXT PRIMARY KEY,owner TEXT NOT NULL,body TEXT NOT NULL)')
            db.execute('INSERT INTO site_workspace_records VALUES(?,?,?)',(record['id'],app.state.store.owner,json.dumps(record,ensure_ascii=False,allow_nan=False)))
    def get(identifier):
        if not available():raise HTTPException(503,'Local workspace storage is not enabled.')
        with app.state.store.connection() as db:
            if not db.execute("SELECT name FROM sqlite_master WHERE name='site_workspace_records'").fetchone():
                raise HTTPException(404,'Workspace result unavailable for this owner.')
            row=db.execute('SELECT body FROM site_workspace_records WHERE id=? AND owner=?',(identifier,app.state.store.owner)).fetchone()
        if not row:raise HTTPException(404,'Workspace result unavailable for this owner.')
        return json.loads(row['body'])
    @app.get('/qa/sites',response_class=HTMLResponse)
    def start():return page()
    @app.post('/qa/sites')
    async def analyze(name:str=Form(''),url:str=Form(...),role:str=Form('owner'),level:str=Form('beginner'),
                      kind:str=Form('configuration'),format:str=Form('json'),content:str=Form(''),
                      metadata:str=Form('{}'),payload:UploadFile|None=File(None),
                      comparison:UploadFile|None=File(None),comparison_url:str=Form(''),
                      acknowledge:bool=Form(False),csrf_token:str=Form(...)):
        csrf_check(csrf_token)
        if not available():raise HTTPException(503,'Uploaded site analysis requires local development storage; hosted adapter is not enabled.')
        try:
            if not acknowledge:raise ValueError('Acknowledge the uploaded-data review limits before saving.')
            root=site_url(url)
            if format not in ('json','csv') or kind not in ('crawl','architecture_upload','performance_upload') and format!='json':raise ValueError('CSV is supported only for crawl uploads.')
            if len(name)>200 or role not in ROLES or level not in LEVELS:raise ValueError('Unsupported name, audience role or expertise level.')
            if len(content.encode())>LIMIT or len(metadata.encode())>LIMIT:raise ValueError('Maximum input size is 2 MB.')
            raw=await payload.read(LIMIT+1) if payload and payload.filename else content.encode()
            other=await comparison.read(LIMIT+1) if comparison and comparison.filename else None
            if other is not None and kind not in ('crawl','architecture','architecture_upload'):raise ValueError('Comparison uploads require crawl or architecture analysis.')
            if len(raw)>LIMIT or other is not None and len(other)>LIMIT:raise ValueError('Maximum upload size is 2 MB.')
            if kind=='configuration':
                result={'status':'configured locally','network_requests':0}
            elif kind=='crawl':
                result=crawl_uploads.import_upload(raw,format,document(metadata.encode()) if format=='csv' else None)
                if any(urlsplit(row['identity']).hostname!=urlsplit(root).hostname for row in result['rows']):
                    raise ValueError('Uploaded pages must match the configured primary site.')
                if other is not None:
                    second=crawl_uploads.import_upload(other,format,document(metadata.encode()) if format=='csv' else None)
                    result={'primary':result,'secondary':second,'eligibility':crawl_uploads.comparison_eligibility(result,second)}
            elif kind=='architecture_upload':
                primary=crawl_uploads.import_upload(raw,format,document(metadata.encode()) if format=='csv' else None)
                result=architecture_upload.graph_from_observations(primary,root)
                if other is not None:
                    secondary=crawl_uploads.import_upload(other,format,document(metadata.encode()) if format=='csv' else None)
                    if not comparison_url:raise ValueError('Declare the comparison site URL.')
                    right=architecture_upload.graph_from_observations(secondary,site_url(comparison_url))
                    eligibility=crawl_uploads.comparison_eligibility(primary,secondary,required_fields=('status','links','page_type'))
                    result={'primary':result,'secondary':right,'comparison':architecture_analysis.compare(result,right,upstream_eligibility=eligibility)}
            elif kind=='performance_upload':
                result=performance_upload.analyze_upload(raw,crawl_uploads.import_upload,format,document(metadata.encode()) if format=='csv' else None)
                if any(urlsplit(x['url']).hostname!=urlsplit(root).hostname for x in result['pages']):
                    raise ValueError('Uploaded observation URLs must match the configured site.')
            elif kind=='architecture':
                data=document(raw)
                if site_url(data.get('root',''))!=root:raise ValueError('Graph root must match the configured site URL.')
                result=architecture_analysis.analyze(data)
                if other is not None:
                    second=document(other)
                    if not comparison_url or site_url(second.get('root',''))!=site_url(comparison_url):
                        raise ValueError('Declare the comparison site matching its graph root.')
                    second_result=architecture_analysis.analyze(second)
                    result={'primary':result,'secondary':second_result,
                            'comparison':architecture_analysis.compare(result,second_result)}
            elif kind=='performance':
                data=document(raw)
                if any(urlsplit(x.get('url','')).hostname!=urlsplit(root).hostname for x in data.get('observations',[])):
                    raise ValueError('Performance observation URLs must match the configured site.')
                result=performance_analysis.analyze(data)
            else:raise ValueError('Choose a supported analysis.')
            record={'id':str(uuid4()),'schema':'site-workspace/v1','name':name.strip() or root,'url':root,
                    'role':role,'level':level,'kind':kind,'created_at':datetime.now(timezone.utc).isoformat(),
                    'source':{'kind':'configuration' if kind=='configuration' else 'supplied_upload','sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),'website_fetched':False},
                    'comparison_source_sha256':hashlib.sha256(other).hexdigest() if other else None,
                    'acknowledged_limits':True,'result':result,'limitations':['Uploaded observations and site configuration only. No website fetched, facts independently checked or ranking/citation gains established.',
                                                  'Local-only records; hosted workspace persistence and final X06–X08 adapter acceptance remain separate.']}
            save(record)
            return page(record)
        except (ValueError,TypeError,KeyError,AttributeError,RecursionError,UnicodeError) as error:
            return page(error=str(error)[:300] or 'Input could not be interpreted.',status=400)
    @app.get('/qa/sites/{identifier}')
    def result(identifier:str):return page(get(identifier))
    @app.get('/qa/sites/{identifier}/report.json')
    def download(identifier:str):
        return JSONResponse(get(identifier),headers={'Cache-Control':'no-store','Content-Disposition':'attachment; filename="site-analysis.json"'})
    @app.get('/qa/sites/{identifier}/report.{format}')
    def minimized_download(identifier:str,format:str):
        if format not in ('csv','md'):raise HTTPException(400,'Unsupported report format.')
        return workspace_exports.download(get(identifier),format,'site-analysis')
    @app.get('/qa/audits/{run_id}/explanations')
    def explanations(run_id:str,level:str='beginner',role:str='owner',format:str='html'):
        if level not in LEVELS or role not in ROLES:raise HTTPException(400,'Unsupported audience or level.')
        report=app.state.store.export_report(run_id)
        presentation=explanation_presenter.explain(report,level)
        if format=='json':
            return JSONResponse(presentation,headers={'Cache-Control':'no-store','Content-Disposition':'attachment; filename="audit-explanations.json"'})
        if format in ('csv','md'):
            return workspace_exports.download(presentation,format,'audit-explanations')
        if format!='html':raise HTTPException(400,'Unsupported presentation format.')
        return HTMLResponse(env.get_template('explanation_modes.html').render(
            report=report,presentation=presentation,role=role,level=level,roles=ROLES,levels=LEVELS),
            headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
