"""Owner-local retained community leads; manual JSON only, no collection."""
from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from .botworthiness.local_web import local_available
from .community_store import CommunityStore
from .request_context import request_csrf

PREFIX = '/qa/community-retained'
HEADERS = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
           'Referrer-Policy': 'same-origin', 'X-Frame-Options': 'SAMEORIGIN'}
MAX_FORM = 3_100_000


def install(app, env, csrf_check):
    def available():
        if not local_available(app):
            raise HTTPException(404)

    def now():
        return datetime.now(timezone.utc).isoformat()

    def store():
        return CommunityStore(Path(str(app.state.store.path) + '.community.sqlite'))

    async def fields(request):
        available()
        if request.headers.get('content-type', '').split(';')[0] != 'application/x-www-form-urlencoded':
            raise HTTPException(415, 'Use the saved-input form')
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_FORM:
                raise HTTPException(413, 'Saved input exceeds the local limit')
        try:
            values = parse_qs(body.decode('utf-8'), keep_blank_values=True, max_num_fields=8)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, 'Invalid form') from None
        if any(len(v) != 1 for v in values.values()):
            raise HTTPException(400, 'Repeated form fields are forbidden')
        values = {k: v[0] for k, v in values.items()}
        csrf_check(values.get('csrf_token'))
        return values

    @app.get(PREFIX, response_class=HTMLResponse)
    def retained():
        available()
        data = store().snapshot(as_of=now())
        return HTMLResponse(env.get_template('community_retained.html').render(
            data=data, csrf=request_csrf(app.state.csrf_token), active='news'), headers=HEADERS)

    @app.post(PREFIX + '/import')
    async def import_saved(request: Request):
        values = await fields(request)
        text = values.get('saved_json', '')
        if len(text.encode('utf-8')) > 500_000:
            raise HTTPException(413, 'Saved JSON exceeds 500 KB')
        try:
            payload = json.loads(text)
            days = int(values.get('retention_days', '30'))
            store().ingest(payload, received_at=now(), retention_days=days)
        except (ValueError, TypeError, OverflowError, RecursionError):
            # Never echo private submitted JSON or parser details.
            raise HTTPException(400, 'Saved input refused; check its format, dates and retention period') from None
        return RedirectResponse(PREFIX, status_code=303, headers=HEADERS)

    @app.post(PREFIX + '/remove')
    async def remove(request: Request):
        values = await fields(request)
        identity = values.get('lead_id', '')
        if not identity or len(identity) > 100:
            raise HTTPException(400, 'Select a retained lead')
        if not store().remove(identity, as_of=now()):
            raise HTTPException(404, 'Retained lead not found')
        return RedirectResponse(PREFIX, status_code=303, headers=HEADERS)
