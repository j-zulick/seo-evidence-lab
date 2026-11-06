"""Local single-owner review UI. Captured HTML is always escaped, never executed."""
from __future__ import annotations
import hmac
import json
import secrets
from pathlib import Path
from html import escape
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware
from jinja2 import Environment, FileSystemLoader, select_autoescape
from .request_context import request_csrf
from .botworthiness.local_web import local_available

ROOT=Path(__file__).resolve().parents[1]

def create_app(store, inspector_module=None, imports_module=None, connection_runtime=None, defer_runs=False,
               qa_capture=None):
    if inspector_module is None:
        from . import inspector as inspector_module
    if imports_module is None:
        from . import imports as imports_module
    app=FastAPI(title='SEO QA Tool', docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1','localhost','testserver'])
    app.state.csrf_token=secrets.token_urlsafe(32)
    app.state.store=store
    app.state.defer_runs=bool(defer_runs)
    if connection_runtime is None:
        from .connection_runtime import ConnectionRuntime
        connection_runtime=ConnectionRuntime(store)
    env=Environment(loader=FileSystemLoader(str(ROOT/'templates')),autoescape=select_autoescape(['html','xml'],default=True))
    env.filters['pretty']=lambda value: json.dumps(value,indent=2,ensure_ascii=False,default=str)
    def render(page,**context):
        return HTMLResponse(env.get_template('app.html').render(page=page,csrf=request_csrf(app.state.csrf_token),local_botworthiness=local_available(app),browser_auth=bool(getattr(app.state,'browser_auth',False)),production_mode=hasattr(app.state,'production_settings'),hosted_store_active=app.state.store is not store,connection_status=connection_runtime.status(),**context))
    def csrf_check(token):
        if not isinstance(token,str) or not hmac.compare_digest(token,request_csrf(app.state.csrf_token)):
            raise HTTPException(403,'Invalid form token; reload the page')
    def inspect(url,expected):
        if 'seo_qa' in expected:
            from .scope_criteria import observe
            return observe(url, expected, capture=qa_capture)
        custom=inspector_module if callable(inspector_module) else getattr(inspector_module,'inspect_fixture',None)
        if custom:
            return custom(url,expected)
        # UI's default is explicitly synthetic, no external network access.
        html='<title>Dummy page</title><link rel="canonical" href="'+escape(url,quote=True)+'"><p>Fixture product information</p>'
        result=inspector_module.inspect_html(html,url,robots_txt='User-agent: *\nAllow: /',expected=expected)
        result.setdefault('limitations',[]).append('Synthetic local HTML; no real website fetched by this run.')
        result.setdefault('evidence',{})['data_mode']='dummy'
        return result
    @app.exception_handler(ValueError)
    async def value_error(request,error):
        response=render('error',error=str(error)); response.status_code=400; return response
    from .supabase_adapter import SupabaseError, UncertainWrite
    @app.exception_handler(SupabaseError)
    async def storage_error(request,error):
        message = ('The saved outcome could not be confirmed. Check audit history before submitting again.'
                   if isinstance(error, UncertainWrite) else
                   'Private storage is temporarily unavailable. Existing evidence has been preserved.')
        response = render('error', error=message)
        response.status_code = 503
        response.headers['Cache-Control'] = 'no-store'
        return response
    @app.get('/health')
    def health():
        try:
            app.state.store.list_requirements()
            app.state.store.list_runs()
        except (ValueError,OSError) as error:
            return JSONResponse({'status':'not_ready','reason':type(error).__name__},status_code=503)
        return {'status':'ready','mode':'supabase-dummy' if app.state.store is not store else 'local-dummy','hosted_supabase_verified':app.state.store is not store}
    @app.get('/',response_class=HTMLResponse)
    def home():
        requirements=app.state.store.list_requirements()
        try:
            summaries = getattr(app.state.store, 'list_run_summaries', None)
            runs = summaries() if summaries else app.state.store.list_runs()
            runs_warning = ('Some run evidence is unavailable or changed. Affected runs cannot be reviewed or exported.'
                            if any(r.get('evidence_status') == 'unavailable' for r in runs) else None)
            runs_count = len(runs)
        except (ValueError,OSError):
            runs=[]
            runs_count=None
            runs_warning='Run evidence is unavailable or changed. Runs cannot be reviewed until evidence is recovered.'
        return render('home',requirements=requirements,runs=runs,runs_warning=runs_warning,runs_count=runs_count)
    @app.get('/requirements/new',response_class=HTMLResponse)
    def new_requirement():return render('new')
    @app.post('/requirements')
    def add_requirement(title:str=Form(...),urls:str=Form(...),expected:str=Form(...),csrf_token:str=Form(...)):
        csrf_check(csrf_token)
        try: rules=json.loads(expected)
        except json.JSONDecodeError:raise ValueError('Expected checks must be valid JSON')
        result=app.state.store.create_requirement(title,[x.strip() for x in urls.splitlines() if x.strip()],rules)
        return RedirectResponse('/requirements/'+result['id'],status_code=303)
    @app.get('/requirements/{rid}',response_class=HTMLResponse)
    def requirement(rid:str):return render('requirement',requirement=app.state.store.get_requirement(rid))
    @app.post('/requirements/{rid}/runs')
    def run_requirement(rid:str,csrf_token:str=Form(...)):
        csrf_check(csrf_token)
        run=app.state.store.create_run(rid)
        if not defer_runs:
            try:app.state.store.execute_run(run['id'],inspect)
            except Exception:
                # Failed state stays visible; never present it as passing.
                pass
        return RedirectResponse('/runs/'+run['id'],status_code=303)
    @app.get('/runs/{run_id}',response_class=HTMLResponse)
    def run_detail(run_id:str):
        run=app.state.store.export_report(run_id)
        return render('run',run=run)
    @app.post('/runs/{run_id}/review')
    def review(run_id:str,decision:str=Form(...),reason:str=Form(''),expected_version:int=Form(...),csrf_token:str=Form(...)):
        csrf_check(csrf_token)
        app.state.store.review(run_id,decision,reason,expected_version=expected_version)
        return RedirectResponse('/runs/'+run_id,status_code=303)
    @app.get('/runs/{run_id}/report',response_class=HTMLResponse)
    def report(run_id:str):
        return render('report',run=app.state.store.export_report(run_id))
    @app.get('/runs/{run_id}/report.csv')
    def report_csv(run_id:str):
        from .csv_export import export_report_csv
        # Store verifies evidence integrity and computes current acceptance.
        data=export_report_csv(app.state.store.export_report(run_id))
        return Response(data,media_type='text/csv; charset=utf-8',headers={
            'Content-Disposition':'attachment; filename="seo-qa-report.csv"',
            'Cache-Control':'no-store',
            'X-Content-Type-Options':'nosniff'})
    @app.get('/runs/{run_id}/report.json')
    def report_json(run_id:str):
        return JSONResponse(app.state.store.export_report(run_id),headers={'Content-Disposition':'attachment; filename="seo-qa-report.json"','Cache-Control':'no-store'})
    @app.get('/runs/{run_id}/report.md')
    def report_markdown(run_id:str):
        from .markdown_export import export_report_markdown
        data=export_report_markdown(app.state.store.export_report(run_id))
        return Response(data,media_type='text/markdown; charset=utf-8',headers={
            'Content-Disposition':'attachment; filename="seo-qa-report.md"',
            'Cache-Control':'no-store', 'X-Content-Type-Options':'nosniff'})
    @app.post('/runs/{run_id}/notion')
    def notion_proposal(run_id:str,csrf_token:str=Form(...)):
        csrf_check(csrf_token)
        return render('operation',operation=app.state.store.propose_notion(run_id))
    @app.post('/operations/{operation_id}/apply')
    def notion_apply(operation_id:str,csrf_token:str=Form(...)):
        csrf_check(csrf_token)
        return render('operation',operation=app.state.store.apply_fixture_notion(operation_id))
    @app.get('/imports',response_class=HTMLResponse)
    def imports_form():return render('imports',result=None,saved_imports=app.state.store.list_imports())
    @app.post('/imports')
    def imports_submit(kind:str=Form(...),csv_text:str=Form(...),metadata:str=Form('{}'),csrf_token:str=Form(...)):
        csrf_check(csrf_token)
        if kind=='queries':result=imports_module.import_queries(csv_text)
        elif kind=='crawl':
            try:meta=json.loads(metadata)
            except json.JSONDecodeError:raise ValueError('Metadata must be valid JSON')
            result=imports_module.import_crawl(csv_text,meta)
        else:raise ValueError('Unknown import kind')
        iid=app.state.store.save_import(kind,csv_text,result)
        return render('imports',result=dict(result,import_id=iid),saved_imports=app.state.store.list_imports())
    @app.post('/imports/compare')
    def compare_imports(before_id:str=Form(...),after_id:str=Form(...),csrf_token:str=Form(...)):
        csrf_check(csrf_token)
        records={x['id']:x for x in app.state.store.list_imports() if x['kind']=='crawl'}
        if before_id not in records or after_id not in records or before_id==after_id:
            raise ValueError('Select two different saved crawl imports')
        result=imports_module.compare_crawls(records[before_id]['result'],records[after_id]['result'])
        iid=app.state.store.save_import('comparison',json.dumps({'before':before_id,'after':after_id}),result)
        return render('imports',result=dict(result,import_id=iid),saved_imports=app.state.store.list_imports())
    from .connection_web import install_connection_routes
    install_connection_routes(app,render,csrf_check,connection_runtime)
    from .simple_qa import install_routes
    install_routes(app, env, csrf_check, inspect)
    from .site_workspace import install_routes as install_site_workspace
    install_site_workspace(app,env,csrf_check)
    from .botworthiness.local_web import install_routes as install_botworthiness
    install_botworthiness(app, env, csrf_check)
    from .competitor_web import install_routes as install_competitor_routes
    install_competitor_routes(app, env)
    from .source_inbox import install as install_inbox
    install_inbox(app,env,csrf_check)
    return app
