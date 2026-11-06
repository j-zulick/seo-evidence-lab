"""Real browser verification on a closed registry of dummy fixtures only.

This is not a live-site JavaScript crawler. Network is denied for every browser
request, service workers are disabled, and only static, reviewed fixture markup
can execute. No input page text or model output is evaluated as browser code.
"""
from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone

from .inspector import inspect_html

FIXTURE_URL = 'https://fixture.invalid/catalog?sku=dummy'
EXPECTED = {'canonical': FIXTURE_URL, 'indexable': True}
_FIXTURES = {
    'canonical-change': '''<!doctype html><html><head><title>Dummy render comparison</title>
<link rel="canonical" href="https://fixture.invalid/catalog?sku=dummy">
<script>document.addEventListener('DOMContentLoaded', () => {
 document.querySelector('link[rel=canonical]').href='https://fixture.invalid/catalog?sku=wrong';
 document.documentElement.dataset.ready='yes';
});</script></head><body><h1>Dummy product</h1></body></html>''',
    'blocked-resource': '''<!doctype html><html><head><title>Dummy blocked resource</title>
<link rel="canonical" href="https://fixture.invalid/catalog?sku=dummy"></head>
<body><img src="https://example.invalid/never-download.png">
<script>fetch('https://example.invalid/never-fetch').catch(() => {});
setTimeout(() => {document.documentElement.dataset.ready='yes';}, 80);</script>
<h1>Dummy product</h1></body></html>''',
    'never-ready': '''<!doctype html><html><head><title>Dummy incomplete render</title>
<link rel="canonical" href="https://fixture.invalid/catalog?sku=dummy"></head>
<body><p>Readiness deliberately never arrives.</p></body></html>''',
}


def fixture_names():
    return tuple(_FIXTURES)


def _incomplete(url, reason):
    return {
        'url': url, 'final_url': url, 'status': 0,
        'observed_at': datetime.now(timezone.utc).isoformat(),
        'evidence': {'render_error': reason},
        'findings': [{'check': 'render_capture', 'outcome': 'inconclusive',
                      'expected': 'complete fixture render', 'observed': None,
                      'reason': reason}],
        'limitations': ['No complete browser render; cannot count as passing evidence.'],
    }


async def async_render_fixture(name, timeout_ms=2000):
    if name not in _FIXTURES:
        raise ValueError('Only registered controlled dummy fixtures may be rendered')
    if type(timeout_ms) is not int or not 50 <= timeout_ms <= 10000:
        raise ValueError('Fixture timeout must be between 50 and 10000 milliseconds')
    html = _FIXTURES[name]
    result = {
        'fixture': name, 'data_mode': 'controlled-dummy-browser',
        'url': FIXTURE_URL, 'raw': inspect_html(html, FIXTURE_URL, expected=EXPECTED),
        'rendered': None, 'complete': False, 'discrepancy': None,
        'blocked_requests': [], 'external_requests_allowed': 0,
        'limitations': ['Controlled static fixture only; not live website rendering or Googlebot behavior.'],
    }
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = None
            try:
                context = await browser.new_context(service_workers='block')
                async def deny(route):
                    result['blocked_requests'].append({'url': route.request.url,
                                                       'method': route.request.method,
                                                       'resource_type': route.request.resource_type})
                    await route.abort('blockedbyclient')
                await context.route('**/*', deny)
                if hasattr(context, 'route_web_socket'):
                    await context.route_web_socket('**/*', lambda ws: ws.close())
                page = await context.new_page()
                # No page.goto: not even a dummy origin is requested over the network.
                await page.set_content(html, wait_until='domcontentloaded', timeout=timeout_ms)
                await page.wait_for_function("document.documentElement.dataset.ready === 'yes'", timeout=timeout_ms)
                captured = await page.content()
                observation = inspect_html(captured, FIXTURE_URL, expected=EXPECTED)
                observation['limitations'] = [
                    'Rendered controlled fixture observation; external resources denied.',
                    'No live-site or search-engine indexing acceptance established.',
                ]
                observation['evidence']['capture_method'] = 'playwright-fixture-set-content'
                observation['evidence']['source_html_sha256'] = hashlib.sha256(html.encode()).hexdigest()
                result['rendered'] = observation
                result['complete'] = True
                raw_findings = [(x['check'], x['outcome'], x['observed']) for x in result['raw']['findings']]
                rendered_findings = [(x['check'], x['outcome'], x['observed']) for x in observation['findings']]
                result['discrepancy'] = raw_findings != rendered_findings
            finally:
                if context:
                    await context.close()
                await browser.close()
    except Exception as exc:
        result['rendered'] = _incomplete(FIXTURE_URL, type(exc).__name__ + ': ' + str(exc)[:300])
    return result


def render_fixture(name, timeout_ms=2000):
    """Synchronous CLI/test wrapper. Async app callers use async_render_fixture."""
    return asyncio.run(async_render_fixture(name, timeout_ms))
