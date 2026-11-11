"""Manual local-only saved diagnostic validation; never fetch or activate rules."""
import hmac
import json
from fastapi import Form, HTTPException
from fastapi.responses import HTMLResponse
from .botworthiness.local_web import local_available
from .request_context import request_csrf
from .external_diagnostics import import_saved
from .provider_contract import normalize


def install(app, env):
    def page(result=None, error=None, status=200):
        if not local_available(app):
            raise HTTPException(404)
        return HTMLResponse(env.get_template('saved_diagnostics.html').render(
            result=json.dumps(result, indent=2, allow_nan=False) if result else None,
            error=error, active='overview', csrf=request_csrf(app.state.csrf_token)),
            status_code=status, headers={'Cache-Control': 'no-store'})

    @app.get('/qa/saved-diagnostics')
    def view():
        return page()

    @app.post('/qa/saved-diagnostics')
    def validate(content: str = Form(...), engine: str = Form(...), bindings: str = Form('{}'), csrf_token: str = Form(...)):
        if not local_available(app):
            raise HTTPException(404)
        if not hmac.compare_digest(csrf_token, request_csrf(app.state.csrf_token)):
            raise HTTPException(403, 'Invalid form token')
        if len(content.encode()) > 2_000_000 or len(bindings.encode()) > 16_000:
            return page(error='Saved input exceeds limit.', status=400)
        try:
            if engine == 'geo':
                result = import_saved(content.encode(), **json.loads(bindings))
                # Raw upstream output may contain private local paths/content.
                result.pop('raw_output')
            elif engine == 'provider':
                result = normalize(json.loads(content))
            else:
                raise ValueError('Unknown saved diagnostic engine')
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
            return page(error='Input refused: ' + str(exc), status=400)
        return page(result=result)
