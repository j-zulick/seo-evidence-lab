"""Local-only saved-export import and triage, separate from fixture source decisions."""
from pathlib import Path
import hmac
from fastapi import Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from .botworthiness.local_web import local_available
from .request_context import request_csrf
from .news_bridge import import_saved, report, decide


def install(app, env):
    def database():
        if not local_available(app):
            raise HTTPException(404)
        return Path(str(app.state.store.path) + '.news-inbox.sqlite')

    def csrf(value):
        if not hmac.compare_digest(value, request_csrf(app.state.csrf_token)):
            raise HTTPException(403, 'Invalid form token')

    def page(error=None, status=200):
        return HTMLResponse(env.get_template('news_imports.html').render(
            data=report(database()), error=error, active='news', csrf=request_csrf(app.state.csrf_token)), status_code=status)

    @app.get('/qa/news-imports')
    def inbox():
        return page()

    @app.post('/qa/news-imports')
    def ingest(content: str = Form(...), format: str = Form('digest-email'),
               synthetic: str = Form('false'), csrf_token: str = Form(...)):
        path = database()
        csrf(csrf_token)
        if synthetic not in {'true', 'false'}:
            return page('Choose a declared export origin.', 400)
        try:
            import_saved(path, content.encode(), format, synthetic == 'true')
        except ValueError as exc:
            return page(str(exc), 400)
        return RedirectResponse('/qa/news-imports', status_code=303)

    @app.post('/qa/news-imports/decision')
    def decision(version_id: str = Form(...), action: str = Form(...), actor: str = Form(...),
                 note: str = Form(...), csrf_token: str = Form(...)):
        path = database()
        csrf(csrf_token)
        try:
            decide(path, version_id, action, actor, note)
        except ValueError as exc:
            return page(str(exc), 400)
        return RedirectResponse('/qa/news-imports', status_code=303)

    @app.get('/qa/news-imports/report.json')
    def download():
        return JSONResponse(report(database()), headers={'Cache-Control': 'no-store',
            'Content-Disposition': 'attachment; filename="saved-news-inbox.json"'})
