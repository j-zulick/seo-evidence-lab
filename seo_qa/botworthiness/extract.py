"""Bounded local HTML section extraction; offsets refer to normalized block text."""
from hashlib import sha256
from html import escape
from html.parser import HTMLParser
import re

MAX_BYTES = 200_000
WORDS = re.compile(r"[^\W_]+(?:['’\-][^\W_]+)*", re.UNICODE)
BLOCKS = {'p', 'ul', 'ol', 'table', 'blockquote', 'pre', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}
EXCLUDE = {'nav', 'header', 'footer', 'aside', 'script', 'style', 'template', 'noscript'}
VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}


def words(text):
    return WORDS.findall(text)


def sentence(text):
    # Preserve short answers, and avoid cutting common initials/abbreviations.
    for m in re.finditer(r'[.!?](?:[\"”’\']?)(?=\s|$)', text):
        prefix = text[:m.end()]
        if re.search(r'\b(?:Dr|Mr|Ms|Mrs|Prof|e\.g|i\.e)\.$', prefix, re.I):
            continue
        if re.search(r'\b[A-Z]\.$', prefix):
            continue
        return prefix
    return text


class Extractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.blocks, self.current = [], [], None
        self.hidden_depth = 0
        self.title, self.in_title = '', False
        self.toc = False

    def flush(self):
        if self.current:
            self.current['text'] = re.sub(r'\s+', ' ', ''.join(self.current.pop('parts'))).strip()
            if self.current['text']:
                self.blocks.append(self.current)
        self.current = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        excluded = (self.hidden_depth > 0 or tag in EXCLUDE or 'hidden' in a
                    or a.get('aria-hidden', '').lower() == 'true'
                    or a.get('role') in {'doc-footnote', 'doc-endnotes'}
                    or any(x in a.get('class', '').lower().split() for x in ('footnote', 'footnotes')))
        if ('toc' in a.get('class', '').lower().split() or a.get('id') in {'toc', 'table-of-contents'}
                or a.get('aria-label', '').lower() == 'table of contents'):
            self.toc = True
        if tag not in VOID:
            self.stack.append((tag, excluded))
            if excluded:
                self.hidden_depth += 1
        if excluded:
            return
        if tag == 'title':
            self.in_title = True
            return
        if (tag in BLOCKS or (tag in {'div', 'section'} and (
                a.get('id', '').lower() in {'summary', 'key-points', 'tldr'}
                or 'summary' in a.get('class', '').lower().split()))) and not (self.current and self.current['tag'] in {'ul', 'ol', 'table'}):
            inherited_summary = bool(self.current and self.current.get('summary'))
            self.flush()
            self.current = {'tag': tag, 'parts': [], 'summary': (
                a.get('id', '').lower() in {'summary', 'key-points', 'tldr'}
                or 'summary' in a.get('class', '').lower().split() or inherited_summary)}
        elif tag in {'td', 'th', 'tr', 'li', 'br'} and self.current:
            self.current['parts'].append(' ')

    def handle_endtag(self, tag):
        indices = [i for i, (t, _) in enumerate(self.stack) if t == tag]
        if not indices:
            return
        index = indices[-1]
        if self.hidden_depth == 0 and self.current and tag == self.current['tag']:
            self.flush()
        if tag == 'title':
            self.in_title = False
        for _, excluded in self.stack[index:]:
            self.hidden_depth -= int(excluded)
        self.stack = self.stack[:index]

    def handle_data(self, data):
        if self.hidden_depth:
            return
        if self.in_title:
            self.title += data
        elif self.current:
            self.current['parts'].append(data)
        elif data.strip():
            self.current = {'tag': 'p', 'parts': [data], 'summary': False}


def extract(content, content_format='html'):
    if not isinstance(content, str) or not content.strip():
        raise ValueError('Content must be nonempty text')
    if len(content.encode()) > MAX_BYTES:
        raise ValueError('Content exceeds 200 KB')
    if content_format not in {'html', 'text'}:
        raise ValueError('Content format must be html or text')
    html = content
    if content_format == 'text':
        chunks = []
        for block in re.split(r'\n\s*\n', content):
            m = re.match(r'^(#{1,6})\s+([^\n]+)(?:\n(.*))?$', block, re.S)
            if m:
                level = len(m[1])
                chunks.append(f'<h{level}>{escape(m[2])}</h{level}>')
                if m[3]:
                    chunks.append('<p>' + escape(m[3]) + '</p>')
            else:
                chunks.append('<p>' + escape(block) + '</p>')
        html = ''.join(chunks)
    p = Extractor()
    p.feed(html)
    p.close()
    p.flush()
    sections, current = [], None
    summary, summary_heading = False, False
    for index, block in enumerate(p.blocks):
        tag, text = block['tag'], block['text']
        block['locator'] = f'block-{index + 1}'
        if (tag in {'h2', 'h3'} and current is None
                and re.fullmatch(r'(?:summary|key points|tl;?dr)', text, re.I)):
            summary_heading = True
            continue
        if tag in {'h2', 'h3'}:
            current = {'id': f'section-{len(sections)+1}', 'heading': text,
                       'level': int(tag[1]), 'heading_locator': block['locator'],
                       'opening': None, 'opening_kind': None, 'blocks': []}
            sections.append(current)
        elif current is None:
            if block['summary'] and tag not in {'h1', 'h4', 'h5', 'h6'}:
                summary = True
            if summary_heading and tag in {'p', 'ul', 'ol'}:
                summary = True
            if tag == 'h1' and re.fullmatch(r'(?:summary|key points|tl;?dr)', text, re.I):
                summary_heading = True
        if current and tag not in {'h2', 'h3'}:
            current['blocks'].append(block['locator'])
            if current['opening'] is None and not re.fullmatch(r'h[1-6]', tag):
                current.update(opening=sentence(text) if tag not in {'table', 'ul', 'ol'} else text,
                               opening_kind='table' if tag == 'table' else 'list' if tag in {'ul', 'ol'} else 'text',
                               opening_locator=block['locator'])
    visible = ' '.join(b['text'] for b in p.blocks)
    for s in sections:
        s['opening_sha256'] = sha256((s['opening'] or '').encode()).hexdigest()
        s['opening_words'] = len(words(s['opening'] or ''))
    return {'content_sha256': sha256(content.encode()).hexdigest(), 'content_format': content_format,
            'title': p.title.strip() or next((b['text'] for b in p.blocks if b['tag'] == 'h1'), ''),
            'blocks': p.blocks, 'sections': sections, 'word_count': len(words(visible)),
            'list_blocks': sum(b['tag'] in {'ul', 'ol'} for b in p.blocks),
            'table_blocks': sum(b['tag'] == 'table' for b in p.blocks),
            'summary': summary, 'watch_metrics': {'table_of_contents': p.toc,
                                                  'h2_count': sum(b['tag'] == 'h2' for b in p.blocks)}}
