"""Minimized Markdown projection of an authorized integrity-checked report.

Reuses the CSV field allowlist/redaction. This is a download, not an import or
an authorization decision; the caller must obtain the report from its Store.
"""
from __future__ import annotations

import html
import json

from .csv_export import project_rows


def _text(value):
    # Encode Markdown punctuation as entities so imported text cannot become
    # active links/images, raw HTML, headings or code fences in a renderer.
    escaped = html.escape(str(value), quote=True)
    return ''.join(f'&#{ord(c)};' if c in '\\`*_{}[]()#+-.!|~' else c
                   for c in escaped).replace('\r', '&#13;').replace('\n', '&#10;')


def export_report_markdown(report):
    rows = project_rows(report)
    lines = ['# SEO QA report', '',
             'Minimized export. URLs use hashed references; sensitive fields are redacted.',
             'Technical findings do not establish indexing, traffic or business outcomes.',
             'JSON is authoritative. Raw inputs, full evidence, complete criteria and scope guidance, '
             'and full knowledge record bodies and evidence blobs are omitted. Free text may remain private.', '']
    for row in rows:
        lines.extend(['## ' + _text(row['record_type']), '',
                      'Reference: ' + _text(row['record_ref']), '',
                      _text(json.dumps(json.loads(row['payload_json']), ensure_ascii=False,
                                       sort_keys=True)), ''])
    if report.get('knowledge') is not None:
        lines.extend(['Use the authorized JSON report for full historical citations and source URLs.', ''])
    return '\n'.join(lines).encode('utf-8')
