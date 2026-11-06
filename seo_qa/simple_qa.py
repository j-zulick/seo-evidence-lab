"""Local or explicitly enabled hosted scope intake; hosted jobs stay queued."""
from __future__ import annotations

from collections import Counter
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from .scope_criteria import SCOPES, RULES, configuration, bundle
from .store import Store
from .botworthiness.local_web import local_available
from .request_context import request_csrf
from .supabase_adapter import SupabaseError

SAMPLE = '''<!doctype html><html lang="en"><head>
<title>A practical guide to caring for houseplants</title>
<meta name="description" content="A simple guide to light, watering and checking your houseplants.">
<meta name="author" content="Sample editor"><link rel="canonical" href="https://sample.invalid/houseplants">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Article","headline":"Caring for houseplants"}</script>
</head><body><article><h1>Caring for houseplants</h1><p>A synthetic example for exploring this tool.</p>
<h2>Start with light</h2><p>Match your plant to the light in your space.</p>
<h2>Check before watering</h2><p>Check the soil rather than relying on a universal timetable.</p>
<a href="/plant-care">Read the care notes</a></article></body></html>'''


def install_routes(app, env, csrf_check, inspect):
    def available():
        return (isinstance(app.state.store, Store) and not hasattr(app.state, 'production_settings')
                or getattr(app.state.store, 'scoped_workflow_enabled', False) is True)

    def render(page, status_code=200, **context):
        return HTMLResponse(env.get_template('qa.html').render(
            page=page, csrf=request_csrf(app.state.csrf_token), scopes=SCOPES, rules=RULES,
            local_botworthiness=local_available(app),
            criteria=bundle(), available=available(),
            deferred=bool(getattr(app.state, 'defer_runs', False)),
            browser_auth=bool(getattr(app.state, 'browser_auth', False)),
            hosted=bool(getattr(app.state.store, 'scoped_workflow_enabled', False)),
            **context), status_code=status_code,
            headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                     'Referrer-Policy': 'same-origin', 'X-Frame-Options': 'DENY'})

    def history():
        rows = []
        summaries = getattr(app.state.store, 'list_run_summaries', None)
        records = summaries() if summaries else app.state.store.list_runs()
        for run in records:
            if 'seo_qa' in run.get('requirement', {}).get('expected', {}):
                rows.append(run)
        return rows

    @app.get('/qa', response_class=HTMLResponse)
    def workspace():
        try:
            runs, warning = history(), None
        except (ValueError, OSError):
            runs, warning = [], 'History could not be verified. Existing evidence has been preserved.'
        return render('start', runs=runs, warning=warning, error=None, values={})

    def create(title, source_kind, content_format, content, url, selected):
        if not available():
            raise HTTPException(503, 'The general scope workspace is not connected to hosted storage yet.')
        if source_kind == 'url':
            try:
                parsed = urlsplit(url.strip())
                valid = (parsed.scheme in {'http', 'https'} and parsed.hostname and
                         not parsed.username and not parsed.password and not parsed.fragment)
                _ = parsed.port
            except ValueError:
                valid = False
            if not valid:
                raise ValueError('Enter an HTTP or HTTPS URL without credentials or a fragment.')
            target = url.strip()
            # All network, DNS, address and redirect checks belong to the existing
            # bounded inspector. No fixture/private-network bypass is introduced.
            config = configuration(selected, 'url', content_format='html')
        else:
            target = 'https://supplied.invalid/' + str(uuid4())
            config = configuration(selected, source_kind, content, content_format)
        label = title.strip() or ('Website audit' if source_kind == 'url' else
                                  'Sample audit' if source_kind == 'sample' else 'Content audit')
        if len(label) > 200:
            raise ValueError('Use an audit title of 200 characters or fewer.')
        requirement = app.state.store.create_requirement(label, [target], {'seo_qa': config})
        run = app.state.store.create_run(requirement['id'])
        if not getattr(app.state, 'defer_runs', False):
            try:
                app.state.store.execute_run(run['id'], inspect)
            except Exception:
                # Durable Store state remains error/partial; never fabricate a result.
                pass
        return RedirectResponse('/qa/audits/' + run['id'], status_code=303)

    @app.post('/qa/audits')
    def audit(title: str = Form(''), source_kind: str = Form('url'),
              content_format: str = Form('text'), content: str = Form(''),
              url: str = Form(''), scopes: list[str] = Form([]), csrf_token: str = Form(...)):
        csrf_check(csrf_token)
        try:
            if source_kind not in {'url', 'supplied'}:
                raise ValueError('Choose a website URL or pasted content.')
            return create(title, source_kind, content_format, content, url, scopes)
        except SupabaseError:
            # Transport diagnostics and ambiguous writes use the application's
            # redacted storage handler; they are not ordinary form validation.
            raise
        except ValueError as error:
            return render('start', status_code=400, runs=history(), warning=None,
                          error=str(error), values={'title': title, 'source_kind': source_kind,
                              'content_format': content_format, 'content': content[:200_000],
                              'url': url, 'scopes': scopes})

    @app.post('/qa/sample')
    def sample(csrf_token: str = Form(...)):
        csrf_check(csrf_token)
        return create('Houseplant guide · sample', 'sample', 'html', SAMPLE, '', [s[0] for s in SCOPES])

    @app.get('/qa/audits/{run_id}', response_class=HTMLResponse)
    def results(run_id: str):
        try:
            report = app.state.store.export_report(run_id)
        except ValueError:
            return render('unavailable', status_code=409, run_id=run_id)
        config = report['requirement']['expected'].get('seo_qa')
        if config is None:
            raise HTTPException(404, 'This run belongs to the technical review desk.')
        rows = [r for observation in report['observations']
                for r in observation['evidence'].get('scope_review', [])]
        counts = Counter(r['outcome'] for r in rows)
        provenance = [o['evidence'].get('provenance', {}) for o in report['observations']]
        return render('results', run=report, config=config, rows=rows, counts=counts,
                      provenance=provenance,
                      selected_scopes=[s for s in SCOPES if s[0] in config['scopes']])
