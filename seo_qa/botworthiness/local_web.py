"""Local-app integration: additive, immutable sidecar records; no hosted fallback."""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from urllib.parse import parse_qs

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from ..store import Store
from ..request_context import request_csrf
from .assessment import assess_library, compare, review_template
from .profile import canonical, digest
from .reporting import to_json, to_csv, to_markdown, to_html

PREFIX = '/qa/botworthiness'
HEADERS = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
           'Referrer-Policy': 'same-origin', 'X-Frame-Options': 'SAMEORIGIN'}
MAX_FORM = 12_000_000


def local_available(app):
    return (isinstance(app.state.store, Store)
            and not hasattr(app.state, 'production_settings')
            and not getattr(app.state, 'browser_auth', False))


class LocalRecords:
    def __init__(self, store):
        self.root = store.path.resolve().parent/(store.path.name+'.quotability')

    def get(self, identity):
        if not re.fullmatch(r'[a-f0-9]{64}', identity):
            raise HTTPException(404, 'Assessment not found')
        path = self.root/(identity+'.json')
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            raise HTTPException(404, 'Assessment not found') from None
        except (ValueError, OSError):
            raise HTTPException(409, 'Assessment evidence cannot be verified') from None
        if digest(record) != identity:
            raise HTTPException(409, 'Assessment evidence has changed')
        return record

    def save(self, record):
        identity = digest(record)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root/(identity+'.json')
        try:
            with path.open('x', encoding='utf-8') as stream:
                stream.write(canonical(record))
        except FileExistsError:
            if self.get(identity) != record:
                raise ValueError('Existing assessment identity mismatch')
        return identity

    def history(self):
        rows, unavailable = [], 0
        if self.root.exists():
            for path in self.root.glob('*.json'):
                try:
                    r = self.get(path.stem)
                    rows.append({'id': path.stem, 'name': r['name'], 'at': r['report']['evaluated_at'],
                                 'status': r['report']['status'], 'pages': len(r['report']['pages'])})
                except (HTTPException, KeyError, TypeError):
                    unavailable += 1
        return sorted(rows, key=lambda r: r['at'], reverse=True), unavailable


def from_run(store, run_id):
    # export_report verifies the original retained evidence; never re-fetch a URL.
    report = store.export_report(run_id)
    if report['state'] != 'completed':
        raise ValueError('Finish the audit before assessing its retained content.')
    config = report['requirement']['expected'].get('seo_qa', {})
    pages = []
    for index, observation in enumerate(report['observations']):
        e = observation.get('evidence', {})
        content = e.get('input', e.get('html'))
        if not isinstance(content, str) or not content.strip():
            raise ValueError('This audit has no retained page text. Paste the content instead.')
        content_hash = sha256(content.encode()).hexdigest()
        if content_hash not in {e.get('content_sha256'), e.get('sha256')}:
            raise ValueError('Retained content does not match its evidence identity.')
        kind = ('synthetic' if config.get('source_kind') == 'sample' or e.get('data_mode') == 'dummy' or observation.get('data_mode') == 'dummy'
                else 'supplied' if config.get('source_kind') == 'supplied' else 'captured')
        status = observation.get('status')
        captured = (not e.get('error') and status is not None and 200 <= status < 300
                    and all(f.get('outcome') == 'pass' for f in e.get('capture_findings', [])))
        pages.append({'id': f'{report["requirement"]["id"]}:{index}',
                      'title': report['requirement']['title'], 'url': observation.get('url'),
                      'content': content, 'content_format': config.get('content_format', 'html'),
                      'evidence_kind': kind, 'captured_at': observation.get('observed_at'),
                      'capture_complete': not bool(e.get('error')) if config.get('source_kind') in {'supplied', 'sample'} else bool(captured)})
    if not pages:
        raise ValueError('This audit has no retained page content.')
    return {'pages': pages, 'sampled': bool(report.get('coverage', {}).get('sampled', False))}


async def read_form(request):
    if request.headers.get('content-type', '').split(';')[0] != 'application/x-www-form-urlencoded':
        raise HTTPException(415, 'Use the local form to submit content.')
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_FORM:
            raise HTTPException(413, 'This submission is too large. Use up to 40 pages of 200 KB each.')
        chunks.append(chunk)
    try:
        rows = parse_qs(b''.join(chunks).decode('utf-8'), keep_blank_values=True, max_num_fields=3000)
    except (ValueError, UnicodeError):
        raise ValueError('The submitted form could not be read.') from None
    if any(len(values) != 1 for values in rows.values()):
        raise ValueError('Repeated form fields are not allowed.')
    return {key: values[0] for key, values in rows.items()}


def install_routes(app, env, csrf_check):
    def records():
        if not local_available(app):
            raise HTTPException(404, 'Bot-worthiness is available only in the local app.')
        return LocalRecords(app.state.store)

    def render(page, status=200, **context):
        return HTMLResponse(env.get_template('botworthiness.html').render(
            page=page, csrf=request_csrf(app.state.csrf_token), prefix=PREFIX, **context),
            status_code=status, headers=HEADERS)

    def load(identity):
        record = records().get(identity)
        if record.get('origin_run_id'):
            try:
                original = from_run(app.state.store, record['origin_run_id'])
            except ValueError:
                raise HTTPException(409, 'Original audit evidence cannot be verified.') from None
            if original != record['manifest']:
                raise HTTPException(409, 'Original audit content has changed.')
        return record

    def manual_values(record):
        values = {}
        for pi, p in enumerate(record['report']['pages']):
            review = record['reviews'].get(p['source']['id'], {})
            for i, item in enumerate(review.get('passes', [])):
                values[f'reviewer_{i}'] = item['evaluator_id']
                values[f'rubric_{i}'] = item['evaluator_version']
                for si, section in enumerate(p['sections']):
                    row = item.get('sections', {}).get(section['id'], {})
                    for key in ('answer_first', 'standalone_openings'):
                        value = row.get(key)
                        values[f'vote_{i}_{pi}_{si}_{key}'] = '' if value is None else 'yes' if value else 'no'
        return values

    def simple_review_available(record):
        reviewers = {tuple((p['evaluator_id'], p['evaluator_version']) for p in r.get('passes', []))
                     for r in record['reviews'].values() if r.get('passes')}
        return sum(len(p['sections']) for p in record['report']['pages']) <= 200 and len(reviewers) <= 1

    def save(name, manifest, reviews=None, origin=None, parent=None):
        if not isinstance(manifest, dict) or 'pages' not in manifest:
            raise ValueError('The library must contain a pages list.')
        at = datetime.now(timezone.utc).isoformat()
        report = assess_library(manifest['pages'], evaluated_at=at, reviews=reviews,
                                expected_ids=manifest.get('expected_ids'), sampled=manifest.get('sampled', False))
        record = {'schema': 'quotability-local-app/1', 'name': name[:200] or 'Content assessment',
                  'manifest': manifest, 'reviews': reviews or {}, 'report': report,
                  'origin_run_id': origin, 'parent_id': parent}
        return records().save(record)

    @app.get(PREFIX, response_class=HTMLResponse)
    def workspace():
        history, unavailable = records().history()
        return render('start', history=history, unavailable=unavailable, values={}, error=None)

    @app.post(PREFIX+'/assess')
    async def assess(request: Request):
        records()
        fields = await read_form(request)
        csrf_check(fields.get('csrf_token'))
        try:
            parent = fields.get('parent_id') or None
            if parent:
                load(parent)
            if fields.get('mode') == 'batch':
                manifest = json.loads(fields.get('manifest', ''))
                if not isinstance(manifest, dict) or not isinstance(manifest.get('pages'), list):
                    raise ValueError('The library must contain a pages list.')
                if any(not isinstance(p, dict) or 'content_file' in p or p.get('evidence_kind', 'supplied') not in {'supplied', 'synthetic'} for p in manifest['pages']):
                    raise ValueError('Paste content inside each page. File paths and captured-page claims are not accepted here.')
            else:
                manifest = {'pages': [{'id': fields.get('page_id', '').strip() or 'article',
                            'title': fields.get('title', '').strip() or 'Content assessment',
                            'content': fields.get('content', ''),
                            'content_format': fields.get('content_format', 'html'), 'evidence_kind': 'supplied'}]}
            identity = save(fields.get('title', '').strip(), manifest, parent=parent)
            return RedirectResponse(PREFIX+'/'+identity, 303)
        except (ValueError, TypeError, KeyError) as error:
            history, unavailable = records().history()
            return render('start', status=400, history=history, unavailable=unavailable,
                          values=fields, error=str(error))

    @app.post(PREFIX+'/sample')
    async def sample(request: Request):
        records()
        fields = await read_form(request)
        csrf_check(fields.get('csrf_token'))
        from ..simple_qa import SAMPLE
        identity = save('Houseplant guide · synthetic example', {'pages': [
            {'id': 'houseplant-sample', 'content': SAMPLE, 'evidence_kind': 'synthetic'}], 'sampled': True})
        return RedirectResponse(PREFIX+'/'+identity, 303)

    @app.post(PREFIX+'/from-run/{run_id}')
    async def reuse_run(run_id: str, request: Request):
        records()
        fields = await read_form(request)
        csrf_check(fields.get('csrf_token'))
        manifest = from_run(app.state.store, run_id)
        identity = save('Saved audit · '+app.state.store.export_report(run_id)['requirement']['title'], manifest, origin=run_id)
        return RedirectResponse(PREFIX+'/'+identity, 303)

    @app.post(PREFIX+'/compare', response_class=HTMLResponse)
    async def comparison(request: Request):
        records()
        fields = await read_form(request)
        csrf_check(fields.get('csrf_token'))
        a, b = fields.get('before', ''), fields.get('after', '')
        if a == b:
            raise ValueError('Choose two different saved assessments.')
        before, after = load(a), load(b)
        result = compare(before['report'], after['report'])
        return render('comparison', comparison=result, before=before, after=after, after_id=b)

    @app.get(PREFIX+'/{identity}', response_class=HTMLResponse)
    def detail(identity: str):
        record = load(identity)
        history, _ = records().history()
        return render('detail', record=record, identity=identity, error=None, review_text='',
                      simple_review=simple_review_available(record), history=history, manual=manual_values(record))

    @app.get(PREFIX+'/{identity}/edit', response_class=HTMLResponse)
    def edit(identity: str):
        record = load(identity)
        history, unavailable = records().history()
        manifest = deepcopy(record['manifest'])
        for p in manifest['pages']:
            p['evidence_kind'] = 'supplied'
            p.pop('captured_at', None)
            p.pop('capture_complete', None)
        return render('start', history=history, unavailable=unavailable, error=None,
                      values={'title': record['name'], 'mode': 'batch', 'manifest': to_json(manifest), 'parent_id': identity})

    @app.get(PREFIX+'/{identity}/visual', response_class=HTMLResponse)
    def visual(identity: str):
        return HTMLResponse(to_html(load(identity)['report']), headers=HEADERS)

    @app.get(PREFIX+'/{identity}/reviews.json')
    def template(identity: str):
        record = load(identity)
        result = record['reviews'] or {p['id']: review_template(p, evaluated_at=record['report']['evaluated_at']) for p in record['manifest']['pages']}
        return JSONResponse(result, headers=dict(HEADERS, **{'Content-Disposition': 'attachment; filename="quotability-reviews.json"'}))

    @app.get(PREFIX+'/{identity}/report.{format}')
    def export(identity: str, format: str):
        report = load(identity)['report']
        exporters = {'json': (to_json, 'application/json'), 'csv': (to_csv, 'text/csv'),
                     'md': (to_markdown, 'text/markdown'), 'html': (to_html, 'text/html')}
        if format not in exporters:
            raise HTTPException(404, 'Unknown report format')
        function, media = exporters[format]
        return Response(function(report), media_type=media, headers=dict(HEADERS, **{
            'Content-Disposition': f'attachment; filename="bot-worthiness.{format}"'}))

    @app.post(PREFIX+'/{identity}/review')
    async def review(identity: str, request: Request):
        record = load(identity)
        fields = await read_form(request)
        csrf_check(fields.get('csrf_token'))
        try:
            if fields.get('review_mode') == 'json':
                reviews = json.loads(fields.get('reviews', ''))
                if not isinstance(reviews, dict):
                    raise ValueError('Review evidence must be an object keyed by article ID.')
            else:
                if not simple_review_available(record):
                    raise ValueError('Use review-file import for large libraries or differing reviewer identities across articles.')
                reviews = deepcopy(record['reviews']) or {p['id']: review_template(p, evaluated_at=record['report']['evaluated_at']) for p in record['manifest']['pages']}
                for pi, p in enumerate(record['report']['pages']):
                    r = reviews[p['source']['id']]
                    passes = []
                    for i in range(2):
                        name = fields.get(f'reviewer_{i}', '').strip()
                        if not name:
                            raise ValueError('Name both independent reviewers, or use review-file import to retain a partial review.')
                        item = {'evaluator_id': name, 'evaluator_version': fields.get(f'rubric_{i}', '').strip(),
                                'pass_id': f'editorial-pass-{i+1}', 'sections': {}}
                        for si, s in enumerate(p['sections']):
                            values = {}
                            for key in ('answer_first', 'standalone_openings'):
                                value = fields.get(f'vote_{i}_{pi}_{si}_{key}', '')
                                if value not in {'', 'yes', 'no'}:
                                    raise ValueError('Choose yes, no or unmeasured for each judgment.')
                                values[key] = {'': None, 'yes': True, 'no': False}[value]
                            item['sections'][s['id']] = dict(values, opening_sha256=s['opening_sha256'])
                        passes.append(item)
                    r['passes'] = passes
                    # A previous adjudication cannot be carried across changed votes.
                    r['resolutions'] = []
            new_id = save(record['name'], record['manifest'], reviews=reviews,
                          origin=record['origin_run_id'], parent=identity)
            return RedirectResponse(PREFIX+'/'+new_id, 303)
        except (ValueError, TypeError, KeyError) as error:
            history, _ = records().history()
            return render('detail', status=400, record=record, identity=identity, error=str(error),
                          review_text=fields.get('reviews', ''), simple_review=simple_review_available(record),
                          history=history, manual=fields)
