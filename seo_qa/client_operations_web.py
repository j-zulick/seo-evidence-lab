"""Private offline operations HTTP adapter using the existing BrowserAuth contract.

No identity headers, new auth provider, hosted writes or scheduled workers.
An injected existing browser service verifies the signed cookie and session row.
Current BrowserAuth is single-owner: a shared multi-account sign-in service still
requires a separately reviewed contract rather than relaxing owner verification.
"""
import hmac
import html
import json
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse
from .browser_auth import BrowserAuthError, BrowserBackendUnavailable
from .client_workspace import WorkspaceExecutor


def create_app(operations, data_root, *, browser_service, origin):
    parsed = urlsplit(origin)
    if (parsed.scheme not in {'https', 'http'} or not parsed.hostname or parsed.path or
            parsed.query or parsed.fragment or parsed.username or parsed.password or
            (parsed.scheme == 'http' and parsed.hostname not in {'127.0.0.1', 'localhost', 'testserver'})):
        raise ValueError('Expected exact private origin')
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    executor = WorkspaceExecutor(operations, data_root)

    @app.middleware('http')
    async def boundary(request, call_next):
        if str(request.base_url).rstrip('/') != origin:
            return JSONResponse({'detail': 'Invalid origin'}, status_code=400)
        try:
            principal = browser_service.authenticate_request(request)
        except BrowserBackendUnavailable:
            return JSONResponse({'detail': 'Sign-in service unavailable'}, status_code=503)
        except BrowserAuthError:
            return JSONResponse({'detail': 'Sign in required'}, status_code=401)
        if request.method != 'GET':
            origins = request.headers.getlist('origin')
            tokens = request.headers.getlist('x-csrf-token')
            if (origins != [origin] or len(tokens) != 1 or
                    not hmac.compare_digest(tokens[0], principal.csrf_token)):
                return JSONResponse({'detail': 'Request verification required'}, status_code=403)
        request.state.principal = principal
        try:
            response = await call_next(request)
        except Exception:
            return JSONResponse({'detail': 'Operation unavailable'}, status_code=503,
                                headers={'Cache-Control': 'no-store'})
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    def denied(error):
        if isinstance(error, PermissionError):
            return JSONResponse({'detail': 'Workspace operation denied'}, status_code=403)
        return JSONResponse({'detail': 'Operation unavailable or invalid'}, status_code=400)

    @app.get('/clientops/{workspace}')
    def home(workspace: str, request: Request):
        try:
            status = operations.status(workspace, authenticated_actor=request.state.principal.owner_id)
        except (ValueError, PermissionError) as error:
            return denied(error)
        # This small form runs only an explicitly submitted local operation.
        token = html.escape(request.state.principal.csrf_token, quote=True)
        return HTMLResponse('<h1>Workspace operations</h1><pre>' + html.escape(json.dumps(status, indent=2)) + '</pre>'
            '<label>Action <select id="action"><option value="import">Import saved queries</option><option value="audit">Audit saved HTML</option><option value="report">Export report</option></select></label>'
            '<label>Request name <input id="key" maxlength="100"></label>'
            '<label>Saved input <textarea id="payload" rows="10" cols="70">{}</textarea></label>'
            '<button id="run">Run manually</button><pre id="result"></pre>'
            '<script>document.getElementById("run").onclick=async()=>{try{const action=document.getElementById("action").value;'
            'const payload=JSON.parse(document.getElementById("payload").value);'
            'const r=await fetch("/api/clientops/"+' + json.dumps(workspace) + '+"/"+action,{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":"' + token + '"},body:JSON.stringify({request_key:document.getElementById("key").value,payload})});'
            'document.getElementById("result").textContent=JSON.stringify(await r.json(),null,2);'
            '}catch(e){document.getElementById("result").textContent="Input could not be processed";}};</script>')

    @app.get('/api/clientops/{workspace}')
    def status(workspace: str, request: Request):
        try:
            return operations.status(workspace, authenticated_actor=request.state.principal.owner_id)
        except (ValueError, PermissionError) as error:
            return denied(error)

    @app.post('/api/clientops/{workspace}/{action}')
    async def execute(workspace: str, action: str, request: Request):
        try:
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 1_000_000:
                    raise ValueError('Oversized operation')
            payload = json.loads(body)
            if not isinstance(payload, dict) or set(payload) != {'request_key', 'payload'}:
                raise ValueError('Invalid operation envelope')
            return executor.execute(workspace, request.state.principal.owner_id,
                                    payload['request_key'], action, payload['payload'])
        except (ValueError, PermissionError, KeyError, TypeError) as error:
            return denied(error)

    return app
