"""Deterministic bounded SEO observations; no ranking/indexing guarantees.

The network adapter pins each socket to a DNS-validated address, including
redirects. Local fixture mode is explicit and accepts loopback targets only.
"""
from __future__ import annotations

from datetime import datetime, timezone
from html.parser import HTMLParser
import hashlib
import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

MAX_BYTES = 1024 * 1024
MAX_REDIRECTS = 5
TIMEOUT = 8
_rate_lock = threading.Lock()
_last_request: dict[str, float] = {}


def _now():
    return datetime.now(timezone.utc).isoformat()


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.canonicals = []
        self.directives = []
        self.base = None
        self.title = []
        self.text = []
        self.in_title = False
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('script', 'style', 'noscript'):
            self.hidden += 1
        if tag == 'title':
            self.in_title = True
        if tag == 'base' and self.base is None:
            self.base = attrs.get('href')
        if tag == 'link' and 'canonical' in attrs.get('rel', '').lower().split():
            self.canonicals.append(attrs.get('href', ''))
        if tag == 'meta':
            self.directives.append((attrs.get('name', '').lower(), attrs.get('content', '')))

    def handle_endtag(self, tag):
        if tag == 'title':
            self.in_title = False
        if tag in ('script', 'style', 'noscript'):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if self.in_title:
            self.title.append(data)
        if not self.hidden:
            self.text.append(data)


def _url_key(url):
    p = urlsplit(url)
    # Preserve query order/values and path case: these can distinguish products.
    host = p.hostname or ''
    if ':' in host:
        host = '[' + host + ']'
    port = p.port
    if port and not (p.scheme.lower() == 'https' and port == 443 or p.scheme.lower() == 'http' and port == 80):
        host += ':' + str(port)
    return urlunsplit((p.scheme.lower(), host.lower(), p.path or '/', p.query, ''))


def _finding(check, outcome, expected, observed, reason):
    return dict(check=check, outcome=outcome, expected=expected, observed=observed, reason=reason)


def inspect_html(html: str, url: str, headers: dict = None, status: int = 200,
                 final_url: str = None, redirects: list = None,
                 robots_txt: str = None, expected: dict = None) -> dict:
    expected = expected or {}
    headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    final_url = final_url or url
    redirects = redirects or []
    crawler = str(expected.get('crawler', 'Googlebot')).lower()
    page = _Page()
    page.feed(html)
    base = urljoin(final_url, page.base) if page.base else final_url
    canonical = [urljoin(base, c) for c in page.canonicals if c.strip()]
    for item in re.findall(r'<([^>]+)>\s*;\s*rel\s*=\s*["\']?canonical["\']?', headers.get('link', ''), flags=re.I):
        canonical.append(urljoin(final_url, item))
    directives = []
    for name, content in page.directives:
        if name in ('robots', crawler):
            directives.extend(x.strip().lower() for x in content.split(','))
    # X-Robots-Tag can scope a sequence to a named crawler.
    for header_line in headers.get('x-robots-tag', '').splitlines():
        scope = None
        for item in header_line.split(','):
            item = item.strip().lower()
            if ':' in item and item.split(':', 1)[0].strip() not in ('unavailable_after', 'max-snippet', 'max-image-preview', 'max-video-preview'):
                scope, item = (x.strip() for x in item.split(':', 1))
            if scope is None or scope == crawler:
                directives.append(item)
    noindex = bool({'noindex', 'none'} & set(directives))
    indexable = 200 <= status < 300 and not noindex
    crawlable = None
    if robots_txt is not None:
        robot = RobotFileParser()
        robot.parse(robots_txt.splitlines())
        crawlable = robot.can_fetch(crawler, final_url)
    findings = []
    target = expected.get('canonical', expected.get('canonical_url'))
    if target is not None:
        try:
            matched = len(canonical) == 1 and _url_key(canonical[0]) == _url_key(str(target))
        except ValueError:
            matched = False
        findings.append(_finding('canonical', 'pass' if matched else 'fail', target, canonical,
                                  'Exactly one canonical matches the requirement.' if matched else 'Missing, conflicting or unexpected canonical.'))
    else:
        findings.append(_finding('canonical', 'not_applicable', None, canonical, 'No canonical requirement supplied.'))
    if 'indexable' in expected:
        blocked = status in (401, 403, 429) or status >= 500 or status == 0
        findings.append(_finding('indexing_directives', 'blocked' if blocked else 'pass' if indexable == expected['indexable'] else 'fail',
                                  expected['indexable'], indexable,
                                  'Capture blocked; indexing eligibility is unverified.' if blocked else 'Technical HTTP/directive comparison only; actual search indexing is not established.'))
    if 'crawlable' in expected:
        findings.append(_finding('robots_access', 'inconclusive' if crawlable is None else 'pass' if crawlable == expected['crawlable'] else 'fail',
                                  expected['crawlable'], crawlable, 'No robots.txt observation available.' if crawlable is None else 'Robots access is separate from noindex and actual indexing.'))
    target_final = expected.get('redirect_final', expected.get('final_url'))
    if target_final is not None:
        try:
            same = _url_key(final_url) == _url_key(str(target_final))
        except ValueError:
            same = False
        findings.append(_finding('redirect_destination', 'pass' if same else 'fail', target_final, final_url, 'Final destination compared with approved requirement.'))
    if 'max_redirects' in expected:
        findings.append(_finding('redirect_count', 'pass' if len(redirects) <= expected['max_redirects'] else 'fail', expected['max_redirects'], len(redirects), 'Observed redirect chain length.'))
    if 'allowed_statuses' in expected:
        findings.append(_finding('http_status', 'pass' if status in expected['allowed_statuses'] else 'fail', expected['allowed_statuses'], status, 'Final HTTP status compared.'))
    text = ' '.join(page.text)
    for phrase in expected.get('required_text', []):
        findings.append(_finding('required_text', 'pass' if phrase in text else 'fail', phrase, phrase if phrase in text else None, 'Exact visible-text preservation check.'))
    if status in (401, 403, 429) or status >= 500 or status == 0:
        # A canonical accidentally present in a challenge/error page cannot pass.
        for row in findings:
            if row['outcome'] == 'pass':
                row['outcome'] = 'inconclusive'
                row['reason'] = 'Error/challenge capture cannot establish a passing page observation.'
        findings.append(_finding('capture', 'blocked', 'accessible page', status, 'HTTP error or challenge response.'))
    raw = html.encode('utf-8')
    return dict(url=url, final_url=final_url, status=status, observed_at=_now(),
                evidence=dict(html=html, sha256=hashlib.sha256(raw).hexdigest(), headers=headers,
                              canonical=canonical, directives=directives, robots_txt=robots_txt,
                              crawlable=crawlable, redirects=redirects, title=''.join(page.title).strip()),
                findings=findings,
                limitations=['Raw HTML observation; JavaScript rendering and actual search indexing are not verified.'])


def _validate_url(url, allowed_hosts, fixture_mode):
    p = urlsplit(url)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise ValueError('Only credential-free HTTP(S) URLs are allowed')
    hostname = p.hostname.rstrip('.').lower()
    if hostname not in allowed_hosts:
        raise ValueError('Destination hostname is outside the approved scope')
    port = p.port or (443 if p.scheme == 'https' else 80)
    if not fixture_mode and port not in (80, 443):
        raise ValueError('Nonstandard port is not allowed')
    answers = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    addresses = list(dict.fromkeys(row[4][0] for row in answers))
    if not addresses:
        raise ValueError('No resolved destination')
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if fixture_mode:
            if not ip.is_loopback:
                raise ValueError('Fixture mode only permits loopback targets')
        elif not ip.is_global:
            raise ValueError('Non-public destination is prohibited')
    return p, addresses[0], port


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, address, port):
        super().__init__(host, port=port, timeout=TIMEOUT)
        self.address = address

    def connect(self):
        self.sock = socket.create_connection((self.address, self.port), self.timeout)


class _PinnedHTTPS(_PinnedHTTP):
    def connect(self):
        super().connect()
        self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=self.host)


def _fetch(url, hosts, fixture_mode):
    p, address, port = _validate_url(url, hosts, fixture_mode)
    if not fixture_mode:
        with _rate_lock:
            wait = 1 - (time.monotonic() - _last_request.get(p.hostname, 0))
            if wait > 0:
                time.sleep(wait)
            _last_request[p.hostname] = time.monotonic()
    conn = (_PinnedHTTPS if p.scheme == 'https' else _PinnedHTTP)(p.hostname, address, port)
    try:
        path = urlunsplit(('', '', p.path or '/', p.query, ''))
        conn.request('GET', path, headers={'User-Agent': 'SEO-QA-Tool/0.1 (owner-authorized bounded inspection)', 'Accept-Encoding': 'identity'})
        response = conn.getresponse()
        # Preserve repeatable headers rather than silently dropping a second X-Robots-Tag.
        headers = {}
        for name, value in response.getheaders():
            name = name.lower()
            separator = '\n' if name == 'x-robots-tag' else ', '
            headers[name] = headers[name] + separator + value if name in headers else value
        raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('Response exceeds capture size limit')
        encoding = response.headers.get_content_charset() or 'utf-8'
        return response.status, headers, raw.decode(encoding, errors='replace')
    finally:
        conn.close()


def inspect_url(url: str, expected: dict = None, allowed_hosts: list = None,
                fixture_mode: bool = False) -> dict:
    expected = expected or {}
    hosts = {str(x).rstrip('.').lower() for x in (allowed_hosts or [])}
    if not hosts:
        return _capture_error(url, url, [], 'Explicit approved hosts are required', 'blocked')
    current = url
    redirects = []
    seen = set()
    try:
        for _ in range(MAX_REDIRECTS + 1):
            if current in seen:
                raise ValueError('Redirect loop')
            seen.add(current)
            status, headers, html = _fetch(current, hosts, fixture_mode)
            if status in (301, 302, 303, 307, 308):
                if len(redirects) >= MAX_REDIRECTS:
                    raise ValueError('Redirect hop limit reached')
                location = headers.get('location')
                if not location:
                    raise ValueError('Redirect response has no destination')
                target = urljoin(current, location)
                redirects.append({'url': current, 'status': status, 'destination': target})
                current = target
                continue
            robots = None
            limitations = []
            if 'crawlable' in expected:
                parts = urlsplit(current)
                robots_url = urlunsplit((parts.scheme, parts.netloc, '/robots.txt', '', ''))
                try:
                    robot_status, _, robot_text = _fetch(robots_url, hosts, fixture_mode)
                    if robot_status == 200:
                        robots = robot_text
                    elif robot_status in (404, 410):
                        robots = ''
                    else:
                        limitations.append('robots.txt did not return a usable observation.')
                except Exception:
                    limitations.append('robots.txt capture failed; crawler access remains unknown.')
            result = inspect_html(html, url, headers, status, current, redirects, robots, expected)
            result['limitations'].extend(limitations)
            return result
    except (socket.timeout, TimeoutError):
        return _capture_error(url, current, redirects, 'Capture timed out', 'inconclusive')
    except Exception as exc:
        return _capture_error(url, current, redirects, str(exc)[:250], 'blocked')
    return _capture_error(url, current, redirects, 'Capture incomplete', 'inconclusive')


def _capture_error(url, current, redirects, reason, outcome):
    return dict(url=url, final_url=current, status=0, observed_at=_now(),
                evidence={'redirects': redirects, 'error': reason},
                findings=[_finding('capture', outcome, 'complete authorized observation', None, reason)],
                limitations=['No complete page evidence; no technical acceptance established.'])
