"""Conservative, versioned general SEO checks; no model or outbound requests.

This is an opt-in local requirement extension. Evidence identifies supplied
content separately from fetched pages; structural signals never imply rankings.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from html import escape
from html.parser import HTMLParser
import ipaddress
import json
import re
from urllib.parse import urljoin, urlsplit

VERSION = 'seo-qa-general/1.0.0'
MAX_BYTES = 200_000
SCOPES = (
    ('technical', 'Technical', 'Response, canonical and indexing signals'),
    ('content', 'On-page & content', 'Titles, descriptions, headings and readable text'),
    ('links', 'Links', 'Link targets, useful labels and verification limits'),
    ('schema', 'Structured data', 'JSON-LD syntax and declared types'),
    ('readiness', 'AI & search readiness', 'Organization, attribution and claims to review'),
)
RULES = {
    'technical': [('http_response', 'Page response'), ('canonical', 'Canonical declaration'),
                  ('indexing', 'Indexing directives'), ('robots', 'Crawl permissions')],
    'content': [('title', 'Page title'), ('description', 'Meta description'),
                ('headings', 'Heading structure'), ('body', 'Readable content')],
    'links': [('targets', 'Link target syntax'), ('labels', 'Link labels'),
              ('destinations', 'Destination availability')],
    'schema': [('syntax', 'JSON-LD syntax'), ('types', 'Structured-data types')],
    'readiness': [('organization', 'Content organization'), ('attribution', 'Author attribution'),
                  ('claims', 'Facts, freshness & FAQ support')],
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def bundle():
    data = {'version': VERSION, 'engine': 'deterministic-structure/v1',
            'rules': deepcopy(RULES),
            'limits': ['No ranking, indexing or AI-citation guarantee.',
                       'No destination crawling, live offer verification or semantic fact checking.',
                       'Title length, word count and missing FAQs are not universal failures.']}
    # JSON roundtrip normalizes tuples for persistence and exact comparison.
    data = json.loads(canonical(data))
    return dict(data, sha256=sha256(canonical(data)).hexdigest())


def validate_config(value):
    if not isinstance(value, dict) or set(value) != {
            'schema', 'scopes', 'source_kind', 'content_format', 'content', 'criteria'}:
        raise ValueError('Unsupported SEO QA input contract')
    if value['schema'] != 'seo-qa-input/v1' or value['criteria'] != bundle():
        raise ValueError('Unsupported or changed criteria bundle; preserve the original version')
    scopes = value['scopes']
    if (not isinstance(scopes, list) or not scopes or any(type(x) is not str for x in scopes)
            or len(scopes) != len(set(scopes)) or set(scopes) - {x[0] for x in SCOPES}):
        raise ValueError('Choose one or more supported scopes')
    if type(value['source_kind']) is not str or value['source_kind'] not in {'url', 'supplied', 'sample'}:
        raise ValueError('Unsupported content source')
    if type(value['content_format']) is not str or value['content_format'] not in {'html', 'text'} or not isinstance(value['content'], str):
        raise ValueError('Choose HTML or plain text')
    if len(value['content'].encode()) > MAX_BYTES:
        raise ValueError('Content must be at most 200 KB')
    if value['source_kind'] == 'url':
        if value['content'] or value['content_format'] != 'html':
            raise ValueError('URL audits capture their own content')
    elif not value['content'].strip():
        raise ValueError('Paste some content to review')
    return deepcopy(value)


def configuration(scopes, source_kind, content='', content_format='html'):
    return validate_config({'schema': 'seo-qa-input/v1', 'scopes': list(scopes),
                            'source_kind': source_kind, 'content': content,
                            'content_format': content_format, 'criteria': bundle()})


class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.text, self.headings, self.links, self.jsonld = [], [], [], [], []
        self.description, self.canonicals, self.robots, self.authors = [], [], [], []
        self.hidden = []
        self.heading = self.link = None
        self.in_title = False
        self.script = None
        self.base = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if self.hidden:
            if tag in {'script', 'style', 'template', 'noscript'}:
                self.hidden.append(tag)
            return
        if tag in {'script', 'style', 'template', 'noscript'}:
            self.hidden.append(tag)
            if tag == 'script' and a.get('type', '').lower().split(';')[0].strip() == 'application/ld+json':
                self.script = []
            return
        if tag == 'title':
            self.in_title = True
        if tag == 'base' and self.base is None:
            self.base = a.get('href')
        if re.fullmatch(r'h[1-6]', tag):
            self.heading = {'level': int(tag[1]), 'text': ''}
            self.headings.append(self.heading)
        if tag == 'a' and 'href' in a:
            self.link = {'href': a['href'], 'text': a.get('aria-label', '')}
            self.links.append(self.link)
            if 'author' in a.get('rel', '').lower().split():
                self.authors.append('author link')
        if tag == 'img' and self.link:
            self.link['text'] += ' ' + a.get('alt', '')
        if tag == 'link' and 'canonical' in a.get('rel', '').lower().split():
            self.canonicals.append(a.get('href', '').strip())
        if tag == 'meta':
            name = a.get('name', '').lower()
            content = a.get('content', '').strip()
            if name == 'description':
                self.description.append(content)
            if name in {'robots', 'googlebot'}:
                self.robots.append(content)
            if name == 'author' and content:
                self.authors.append(content)

    def handle_endtag(self, tag):
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
                if tag == 'script' and self.script is not None:
                    self.jsonld.append(''.join(self.script))
                    self.script = None
            return
        if tag == 'title':
            self.in_title = False
        if re.fullmatch(r'h[1-6]', tag):
            self.heading = None
        if tag == 'a':
            self.link = None

    def handle_data(self, data):
        if self.hidden:
            if self.hidden[-1] == 'script' and self.script is not None:
                self.script.append(data)
            return
        if self.in_title:
            self.title.append(data)
        else:
            self.text.append(data)
        if self.heading is not None:
            self.heading['text'] += data
        if self.link is not None:
            self.link['text'] += data


def review(content, *, scopes, source_kind, content_format, url, status=None, headers=None,
           robots_txt=None, captured=True):
    page = Page()
    page.feed(content if content_format == 'html' else '<p>' + escape(content) + '</p>')
    html = content_format == 'html'
    rows = []
    failed_capture = not captured or (source_kind == 'url' and
                                      (status is None or not 200 <= status < 300))

    def add(scope, rule, outcome, observed, reason, recommendation, severity='info'):
        title = dict(RULES[scope])[rule]
        # A challenge/error page cannot establish passing content properties.
        if failed_capture and not (scope == 'technical' and rule == 'http_response'):
            outcome, observed = ('blocked' if not captured else 'inconclusive'), None
            reason = 'No complete accessible page was captured for this check.'
            recommendation = 'Resolve the capture problem, then run a fresh audit.'
        rows.append({'scope': scope, 'id': f'{scope}.{rule}', 'title': title,
                     'outcome': outcome, 'observed': observed, 'reason': reason,
                     'recommendation': recommendation, 'severity': severity,
                     'evidence': {'content_sha256': sha256(content.encode()).hexdigest(),
                                  'source_kind': source_kind}})

    if 'technical' in scopes:
        add('technical', 'http_response', 'pass' if captured and status is not None and 200 <= status < 300 else
            'fail' if status is not None and status >= 400 else 'inconclusive', status,
            'No complete accessible page was captured; response status alone cannot pass.' if source_kind == 'url' and not captured else
            'Observed response status; this does not establish indexing.' if status is not None else
            'Supplied content has no observed HTTP response.', 'Verify the live page response.', 'error')
        canonical_values = list(page.canonicals)
        for key, header in (headers or {}).items():
            if key.lower() == 'link':
                canonical_values.extend(re.findall(r'<([^>]+)>\s*;\s*rel\s*=\s*[\"\']?canonical[\"\']?', str(header), re.I))
        valid_canonical = False
        if len(canonical_values) == 1 and canonical_values[0]:
            try:
                reference = canonical_values[0]
                reference_parts = urlsplit(reference)
                resolved = urlsplit(urljoin(url, reference))
                valid_canonical = (resolved.scheme in {'http', 'https'} and bool(resolved.hostname)
                                   and (not reference_parts.scheme or bool(reference_parts.hostname))
                                   and not resolved.username and not any(c.isspace() for c in reference)
                                   and (source_kind == 'url' or bool(urlsplit(reference).scheme)))
                _ = resolved.port
            except ValueError:
                valid_canonical = False
        add('technical', 'canonical', 'pass' if html and valid_canonical else
            'inconclusive', canonical_values if html else None,
            'Declaration count and web-reference syntax only; destination and intent are not verified.',
            'Check that the declared canonical reflects the intended page; absence is not a universal error.')
        meta = page.robots + [str(v) for k, v in (headers or {}).items() if k.lower() == 'x-robots-tag']
        add('technical', 'indexing', 'inconclusive', meta if html else None,
            'Observed directives require an indexing-intent decision; actual indexing is unknown.',
            'Review noindex and crawler-specific directives against the page’s intended audience.')
        add('technical', 'robots', 'inconclusive', robots_txt,
            'Crawl access depends on the crawler and exact URL; it is separate from indexing.',
            'Verify the site’s robots policy for the intended crawler and URL.')
    if 'content' in scopes:
        title = ''.join(page.title).strip()
        add('content', 'title', 'pass' if title else 'inconclusive', title if html else None,
            'A title was observed.' if title else 'No title was observed in this input.',
            'Use a descriptive title that matches the page; no fixed character limit is enforced.')
        add('content', 'description', 'pass' if html and any(page.description) else 'inconclusive',
            page.description if html else None, 'Description presence only, not quality or search display.',
            'Consider an accurate description if this is a complete page.')
        levels = [h['level'] for h in page.headings]
        jump = any(b > a + 1 for a, b in zip(levels, levels[1:]))
        add('content', 'headings', 'pass' if html and levels and not jump else 'inconclusive',
            page.headings if html else None,
            'Heading levels observed; multiple H1s are not automatically failures.',
            'Use headings to organize the page; review skipped levels in context.')
        text = ' '.join(page.text).strip()
        add('content', 'body', 'pass' if text else 'fail', {'characters': len(text)},
            'Visible text in this supplied/captured input; no minimum word count is enforced.',
            'Check extraction if expected text is missing.', 'warning')
    if 'links' in scopes:
        unsafe, unresolved = [], []
        for link in page.links:
            href = link['href'].strip()
            try:
                parsed = urlsplit(href)
                if parsed.scheme.lower() in {'javascript', 'data', 'vbscript', 'file'}:
                    unsafe.append(href[:200])
                elif not href or any(c.isspace() for c in href) or parsed.scheme.lower() not in {'', 'http', 'https', 'mailto', 'tel'}:
                    unresolved.append(href[:200])
                elif parsed.scheme.lower() in {'http', 'https'} and (not parsed.hostname or parsed.username):
                    unresolved.append(href[:200])
                else:
                    _ = parsed.port
            except ValueError:
                unresolved.append(href[:200])
        outcome = 'fail' if unsafe else 'inconclusive' if unresolved else 'pass' if page.links else 'not_applicable'
        add('links', 'targets', outcome, {'count': len(page.links), 'unsafe': unsafe, 'review': unresolved},
            'Only captured link syntax is checked; relative links and fragments are allowed.',
            'Replace unsafe destinations and review malformed targets.', 'error')
        blank = sum(not l['text'].strip() for l in page.links)
        add('links', 'labels', 'inconclusive' if blank else 'pass' if page.links else 'not_applicable',
            {'unlabeled': blank, 'total': len(page.links)}, 'Visible/accessible labels observed, not topical relevance.',
            'Use useful link text or an accessible label instead of an empty anchor.')
        add('links', 'destinations', 'inconclusive' if page.links else 'not_applicable', len(page.links),
            'Link destinations were not requested. A syntactically valid link can still be broken.',
            'Verify important destinations with a separately bounded link check.')
    if 'schema' in scopes:
        errors, types = 0, []
        def walk(value):
            if isinstance(value, dict):
                t = value.get('@type', [])
                types.extend([t] if isinstance(t, str) else [x for x in t if isinstance(x, str)] if isinstance(t, list) else [])
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        for raw in page.jsonld:
            try:
                value = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                if not isinstance(value, (dict, list)):
                    raise ValueError()
                walk(value)
            except (ValueError, RecursionError):
                errors += 1
        add('schema', 'syntax', 'fail' if errors else 'pass' if page.jsonld else 'not_applicable',
            {'blocks': len(page.jsonld), 'invalid': errors}, 'JSON object/array syntax only; absence is not automatically an error.',
            'Repair invalid JSON-LD syntax before checking schema-specific requirements.', 'error')
        add('schema', 'types', 'inconclusive' if page.jsonld else 'not_applicable', sorted(set(types)),
            'Declared types are inventory only; required fields and rich-result eligibility are unverified.',
            'Validate relevant schema properties against the page’s actual content.')
    if 'readiness' in scopes:
        add('readiness', 'organization', 'pass' if page.headings else 'inconclusive',
            {'headings': len(page.headings)}, 'Organization signal only; answer quality is unverified.',
            'Use descriptive sections and clear answers where they help the reader.')
        add('readiness', 'attribution', 'inconclusive', {'author_signals': len(page.authors)},
            'Author signals do not verify identity, expertise or first-hand experience.',
            'Identify accountable authors and cite evidence where the subject requires it.')
        add('readiness', 'claims', 'inconclusive', None,
            'Facts, dates, offers and FAQ answers have not been independently verified.',
            'Review time-sensitive claims and answer support. No FAQ section or fixed word count is required.')
    return rows


def capture_complete(content, status, evidence):
    return (bool(content.strip()) and status not in (None, 0)
            and not evidence.get('error')
            and not any(f.get('outcome') != 'pass' for f in evidence.get('capture_findings', [])))


def observe(url, expected, *, capture=None):
    config = validate_config(expected['seo_qa'])
    if config['source_kind'] == 'url':
        if capture is None:
            from .inspector import inspect_url
            def capture(target, expectations):
                host = urlsplit(target).hostname or ''
                try:
                    forbidden = not ipaddress.ip_address(host).is_global
                except ValueError:
                    forbidden = host == 'localhost' or host.endswith('.localhost')
                if forbidden:
                    return {'status': 0, 'evidence': {},
                            'limitations': ['Private or special-use targets cannot be inspected.']}
                return inspect_url(target, expected=expectations,
                                   allowed_hosts=[urlsplit(target).hostname], fixture_mode=False)
        result = capture(url, {})
        original = deepcopy(result.get('evidence', {}))
        content = original.get('html', '')
        if not isinstance(content, str):
            raise ValueError('Capture did not retain text evidence')
        if len(content.encode()) > MAX_BYTES:
            raise ValueError('Captured page exceeds the 200 KB review limit')
        status = result.get('status')
        original['capture_findings'] = [deepcopy(f) for f in result.get('findings', [])
                                        if f.get('check') == 'capture']
        complete = capture_complete(content, status, original)
        provenance = {'kind': 'fetched' if status is not None and status > 0 else 'url_attempt',
                      'url': url, 'capture_limitations': result.get('limitations', [])}
    else:
        content, original, status, complete = config['content'], {}, None, True
        provenance = {'kind': 'synthetic_sample' if config['source_kind'] == 'sample' else 'supplied',
                      'url': None, 'capture_limitations': ['No website was fetched.']}
    rows = review(content, scopes=config['scopes'], source_kind=config['source_kind'],
                  content_format=config['content_format'], url=url, status=status,
                  headers=original.get('headers'), robots_txt=original.get('robots_txt'), captured=complete)
    original.update({'input': content, 'content_sha256': sha256(content.encode()).hexdigest(),
                     'provenance': provenance, 'scope_review': rows,
                     'criteria': config['criteria']})
    original.setdefault('sha256', sha256(content.encode()).hexdigest())
    return {'url': url, 'status': status, 'observed_at': datetime.now(timezone.utc).isoformat(),
            'evidence': original,
            'findings': [{'check': row['id'], 'outcome': row['outcome'],
                          'expected': row['title'], 'observed': row['observed'],
                          'reason': row['reason']} for row in rows],
            'limitations': config['criteria']['limits'] + provenance['capture_limitations']}


def validate_observation(config, url, value):
    """Require exact scoped evidence before a Store may commit a scoped result.

    Legacy inspectors cannot accidentally complete a general-scope job using an
    unrelated set of technical findings. No network is used for this validation.
    """
    config = validate_config(config)
    evidence = value.get('evidence', {})
    content = evidence.get('input')
    if (not isinstance(content, str) or len(content.encode()) > MAX_BYTES
            or evidence.get('criteria') != config['criteria']
            or evidence.get('content_sha256') != sha256(content.encode()).hexdigest()):
        raise ValueError('Missing or changed general-scope evidence')
    kind = ({'url': 'fetched' if value.get('status') is not None and value['status'] > 0 else 'url_attempt',
             'supplied': 'supplied', 'sample': 'synthetic_sample'}[config['source_kind']])
    if evidence.get('provenance', {}).get('kind') != kind:
        raise ValueError('General-scope source identity mismatch')
    if config['source_kind'] != 'url' and (content != config['content'] or value.get('status') is not None):
        raise ValueError('Supplied content is not a live page capture')
    if config['source_kind'] == 'url' and evidence.get('html', '') != content:
        raise ValueError('Captured content identity mismatch')
    status = value.get('status')
    rows = review(content, scopes=config['scopes'], source_kind=config['source_kind'],
                  content_format=config['content_format'], url=url, status=status,
                  headers=evidence.get('headers'), robots_txt=evidence.get('robots_txt'),
                  captured=(config['source_kind'] != 'url' or capture_complete(content, status, evidence)))
    findings = [{'check': r['id'], 'outcome': r['outcome'], 'expected': r['title'],
                 'observed': r['observed'], 'reason': r['reason']} for r in rows]
    if evidence.get('scope_review') != rows or value.get('findings') != findings:
        raise ValueError('General-scope findings do not match the retained criteria and evidence')
