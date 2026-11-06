"""S07 v1: opt-in local controlled-server rendering; public execution disabled."""
from __future__ import annotations
import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import http.client
import ipaddress
import json
import socket
import time
from urllib.parse import urlsplit, urlunsplit, urljoin
from seo_qa.inspector import inspect_html

VERSION = 's07-controlled-render-v1'

def digest(data):
    return hashlib.sha256(data).hexdigest()

@dataclass(frozen=True)
class Limits:
    requests: int = 20
    redirects: int = 4
    response_bytes: int = 65536
    total_bytes: int = 262144
    dom_bytes: int = 131072
    wall_ms: int = 4000

    def __post_init__(self):
        caps = dict(requests=100, redirects=10, response_bytes=1048576,
                    total_bytes=4194304, dom_bytes=1048576, wall_ms=10000)
        for name, value in asdict(self).items():
            if type(value) is not int or value < (100 if name == 'wall_ms' else 1) or value > caps[name]:
                raise ValueError('Invalid bounded limit: ' + name)

def validate_destination(url, origins, *, controlled=False, resolver=socket.getaddrinfo):
    """Validate all answers on every request. Returned IP must be used to connect."""
    if any(ord(c) < 33 or ord(c) == 127 for c in url) or '\\' in url:
        raise ValueError('Invalid URL characters')
    p = urlsplit(url)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise ValueError('Credential-free HTTP(S) required')
    port = p.port or (443 if p.scheme == 'https' else 80)
    origin = f'{p.scheme}://{p.hostname}:{port}'
    if origin not in origins:
        raise ValueError('Origin outside explicit allowlist')
    if controlled:
        if p.scheme != 'http' or p.hostname != '127.0.0.1' or not 1024 <= port <= 65535:
            raise ValueError('Only explicit ephemeral loopback origin allowed')
    elif port not in (80, 443):
        raise ValueError('Public nonstandard port denied')
    addresses = sorted({row[4][0] for row in resolver(p.hostname, port, type=socket.SOCK_STREAM)})
    if not addresses or any(not (ipaddress.ip_address(a).is_loopback if controlled else ipaddress.ip_address(a).is_global) for a in addresses):
        raise ValueError('DNS destination is not permitted')
    return p, addresses[0], port

class PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, address, port, timeout):
        super().__init__(host, port, timeout=timeout)
        self.address = address
    def connect(self):
        self.sock = socket.create_connection((self.address, self.port), self.timeout)

class Capture:
    def __init__(self, origin, limits):
        self.origin, self.limits = origin, limits
        self.deadline = time.monotonic() + limits.wall_ms / 1000
        self.records, self.errors = [], []
        self.bytes, self.redirects = 0, 0

    def fetch(self, url, method='GET'):
        # Called serially by the async route lock; no browser direct network.
        if method != 'GET':
            raise ValueError('Only GET permitted')
        if len(self.records) >= self.limits.requests:
            raise ValueError('Request limit exceeded')
        record = {'url': url, 'method': method}
        self.records.append(record)
        p, address, port = validate_destination(url, {self.origin}, controlled=True)
        record['pinned_ip'] = address
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Capture deadline exceeded')
        conn = PinnedHTTP(p.hostname, address, port, min(remaining, 1.0))
        try:
            conn.request('GET', urlunsplit(('', '', p.path or '/', p.query, '')),
                         headers={'Accept-Encoding': 'identity', 'User-Agent': 'SEO-QA-S07/1'})
            response = conn.getresponse()
            headers = {}
            for key, value in response.getheaders():
                key = key.lower()
                headers[key] = headers[key] + ('\n' if key == 'x-robots-tag' else ', ') + value if key in headers else value
            if headers.get('content-encoding', 'identity') != 'identity':
                raise ValueError('Encoded response denied')
            allowance = min(self.limits.response_bytes, self.limits.total_bytes - self.bytes)
            chunks, size = [], 0
            while size <= allowance:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Capture deadline exceeded while reading')
                if conn.sock:
                    conn.sock.settimeout(min(remaining, 1.0))
                chunk = response.read1(min(16384, allowance + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            body = b''.join(chunks)
            self.bytes += len(body)
            record.update(status=response.status, bytes=len(body), sha256=digest(body))
            if len(body) > allowance:
                raise ValueError('Response or aggregate byte limit exceeded')
            if response.status in (301, 302, 303, 307, 308):
                self.redirects += 1
                record['location'] = headers.get('location')
                if self.redirects > self.limits.redirects:
                    raise ValueError('Redirect limit exceeded')
            if response.status >= 400:
                raise ValueError('HTTP resource failure')
            return response.status, headers, body
        finally:
            conn.close()

async def async_render_controlled(url, controlled_origin, expected=None, limits=None):
    limits = limits or Limits()
    validate_destination(url, {controlled_origin}, controlled=True)
    config = {'version': VERSION, 'origin': controlled_origin, 'limits': asdict(limits),
              'ready': 'document.documentElement.dataset.ready === "yes"', 'expected': expected or {}}
    cap = Capture(controlled_origin, limits)
    result = dict(version=VERSION, url=url, observed_at=datetime.now(timezone.utc).isoformat(),
                  data_mode='controlled-server-offline', complete=False, discrepancy=None,
                  raw=None, rendered=None, requests=cap.records, errors=cap.errors,
                  config=config, config_sha256=digest(json.dumps(config, sort_keys=True).encode()),
                  limitations=['Bounded local capture only; no public site, Googlebot or future script completion claim.'])
    browser = None
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, timeout=limits.wall_ms,
                args=['--disable-background-networking', '--dns-prefetch-disable'])
            try:
                async with asyncio.timeout(limits.wall_ms / 1000):
                    # Never return a 3xx to Chromium: Playwright does not reroute its later hops.
                    current = url
                    while True:
                        first = await asyncio.to_thread(cap.fetch, current)
                        if first[0] not in (301, 302, 303, 307, 308):
                            break
                        if not first[1].get('location'):
                            raise ValueError('Redirect missing location')
                        current = urljoin(current, first[1]['location'])
                    prefetched = {current: first}
                    context = await browser.new_context(service_workers='block', accept_downloads=False)
                    lock = asyncio.Lock()
                    async def route_request(route):
                        async with lock:
                            try:
                                if route.request.is_navigation_request() and route.request.frame.parent_frame:
                                    raise ValueError('Subframes denied')
                                if route.request.is_navigation_request() and result['raw'] is not None:
                                    raise ValueError('Subsequent navigation denied')
                                cached = prefetched.pop(route.request.url, None)
                                status, headers, body = cached if cached is not None else await asyncio.to_thread(cap.fetch, route.request.url, route.request.method)
                                if status in (301, 302, 303, 307, 308):
                                    raise ValueError('Redirected subresource or subsequent navigation denied')
                                if route.request.is_navigation_request() and result['raw'] is None and status not in (301, 302, 303, 307, 308):
                                    html = body.decode('utf-8', errors='replace')
                                    result['source_sha256'] = digest(body)
                                    result['raw'] = inspect_html(html, url, headers, status, route.request.url, redirects=[x for x in cap.records if 'location' in x], expected=expected)
                                # Browser never connects to a network destination itself.
                                headers = {k:v for k,v in headers.items() if k not in ('content-length', 'transfer-encoding', 'connection', 'content-security-policy')}
                                headers['content-security-policy'] = "worker-src 'none'; frame-src 'none'; object-src 'none'"
                                await route.fulfill(status=status, headers=headers, body=body)
                            except Exception as exc:
                                cap.errors.append(str(exc))
                                await route.abort('blockedbyclient')
                    await context.route('**/*', route_request)
                    await context.add_init_script("document.addEventListener('securitypolicyviolation', () => console.error('S07:CSP blocked resource'))")
                    async def deny_ws(ws):
                        cap.errors.append('WebSocket denied')
                        await ws.close()
                    await context.route_web_socket('**/*', deny_ws)
                    page = await context.new_page()
                    page.on('console', lambda message: cap.errors.append(message.text) if message.text.startswith('S07:') else None)
                    page.on('pageerror', lambda error: cap.errors.append('JavaScript error: ' + str(error)))
                    page.on('requestfailed', lambda request: cap.errors.append('Request failed: ' + request.url))
                    page.on('popup', lambda popup: cap.errors.append('Popup denied'))
                    page.on('download', lambda download: cap.errors.append('Download denied'))
                    await page.goto(current, wait_until='domcontentloaded', timeout=limits.wall_ms)
                    await page.wait_for_function(config['ready'], timeout=limits.wall_ms)
                    await page.wait_for_load_state('networkidle', timeout=limits.wall_ms)
                    captured = await page.content()
                    if len(captured.encode()) > limits.dom_bytes:
                        raise ValueError('DOM byte limit exceeded')
                    validate_destination(page.url, {controlled_origin}, controlled=True)
                    if time.monotonic() > cap.deadline:
                        raise TimeoutError('Capture deadline exceeded')
                    if cap.errors or result['raw'] is None:
                        raise ValueError('Incomplete requests or missing raw capture')
                    result['rendered'] = inspect_html(captured, url, headers=result['raw']['evidence']['headers'], status=result['raw']['status'], final_url=page.url, redirects=[x for x in cap.records if 'location' in x], expected=expected)
                    result['rendered']['limitations'] = result['limitations'][:]
                    result['dom_sha256'] = digest(captured.encode())
                    result['browser_version'] = browser.version
                    result['complete'] = True
                    signature = lambda r: [(f['check'], f['outcome'], f['observed']) for f in r['findings']]
                    result['discrepancy'] = signature(result['raw']) != signature(result['rendered'])
            finally:
                await asyncio.wait_for(browser.close(), timeout=2)
    except Exception as exc:
        cap.errors.append(type(exc).__name__ + ': ' + str(exc)[:250])
    if not result['complete']:
        result['rendered'] = dict(url=url, final_url=url, status=0, observed_at=result['observed_at'],
            evidence={'errors': cap.errors}, findings=[dict(check='render_capture', outcome='inconclusive',
            expected='complete bounded render', observed=None, reason='; '.join(cap.errors))],
            limitations=['Incomplete rendering remains unknown and cannot pass.'])
    result['total_bytes'] = cap.bytes
    return result

def render_controlled(url, controlled_origin, expected=None, limits=None):
    return asyncio.run(async_render_controlled(url, controlled_origin, expected, limits))
