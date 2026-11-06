"""Offline upload/observation contract. No fetching, inference, or storage side effects."""
from __future__ import annotations
import csv
import hashlib
import io
import json
import math
import re
from datetime import datetime
from urllib.parse import urlsplit
from .imports import import_crawl, url_identity, CRAWL_ALIASES, _key, MAX_BYTES, MAX_ROWS, MAX_CELL

SCHEMA = 'crawl-upload/v1'
CONTRACT = 'crawl-observations/v1'
EXTRA = {'links', 'page_type', 'performance', 'raw', 'rendered'}
FIELDS = sorted(set(CRAWL_ALIASES.values()) | EXTRA)


def _strict_url(value):
    identity = url_identity(value)
    if any(c.isspace() for c in value.strip()) or re.search(r'%(?![0-9A-Fa-f]{2})', value):
        raise ValueError('URL whitespace or malformed percent escape')
    if urlsplit(value.strip()).port == 0:
        raise ValueError('URL port zero forbidden')
    return identity


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError('Timestamp must be text')
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('Timestamp requires timezone')
    return stamp


def _json(text):
    def pairs(items):
        out = {}
        for k, v in items:
            if k in out:
                raise ValueError('Duplicate JSON key: ' + k)
            out[k] = v
        return out
    return json.loads(text, object_pairs_hook=pairs,
                      parse_constant=lambda x: (_ for _ in ()).throw(ValueError('Nonfinite JSON number')))


def _bounded(value, depth=0):
    if depth > 12:
        raise ValueError('Nesting exceeds 12 levels')
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError('Nonfinite number forbidden')
    if isinstance(value, str) and len(value) > MAX_CELL:
        raise ValueError('Cell exceeds length limit')
    if isinstance(value, dict):
        for k, v in value.items():
            _bounded(k, depth + 1)
            _bounded(v, depth + 1)
    elif isinstance(value, list):
        if len(value) > MAX_ROWS:
            raise ValueError('Array exceeds row limit')
        for v in value:
            _bounded(v, depth + 1)


def _within(identity, scopes):
    p = urlsplit(identity)
    for scope in scopes:
        s = urlsplit(_strict_url(scope))
        root = s.path.rstrip('/')
        if (p.scheme, p.netloc) == (s.scheme, s.netloc) and (not root or p.path == root or p.path.startswith(root + '/')):
            return True
    return False


def _extras(row):
    if 'links' in row:
        if not isinstance(row['links'], list) or any(not isinstance(v, str) for v in row['links']):
            raise ValueError('links must be an explicit list of absolute URLs')
        for link in row['links']:
            _strict_url(link)
    if 'page_type' in row and (not isinstance(row['page_type'], str) or not row['page_type'].strip()):
        raise ValueError('page_type must be nonempty text')
    if 'performance' in row:
        if not isinstance(row['performance'], list):
            raise ValueError('performance must be a list')
        for obs in row['performance']:
            if not isinstance(obs, dict) or obs.get('kind') not in {'field', 'lab'} or obs.get('device') not in {'mobile', 'desktop'}:
                raise ValueError('Performance kind/device required')
            if not isinstance(obs.get('source'), str) or not obs['source'].strip():
                raise ValueError('Performance source required')
            _stamp(obs.get('captured_at'))
            metrics = obs.get('metrics')
            if not isinstance(metrics, dict) or not metrics:
                raise ValueError('Explicit performance metrics required')
            for key, val in metrics.items():
                if key not in {'lcp_ms', 'inp_ms', 'cls', 'ttfb_ms'} or type(val) not in {int, float} or val < 0:
                    raise ValueError('Invalid metric name/value')
    for mode in ('raw', 'rendered'):
        if mode in row:
            obs = row[mode]
            if not isinstance(obs, dict) or not isinstance(obs.get('source'), str) or not obs['source'].strip() or not isinstance(obs.get('capture_id'), str) or not obs['capture_id']:
                raise ValueError('Rendering source and capture_id required')
            _stamp(obs.get('captured_at'))
            for key, val in obs.items():
                if key in {'links', 'structured_data'} and not isinstance(val, list):
                    raise ValueError('Rendering lists required')
                if key in {'content', 'canonical', 'robots'} and not isinstance(val, str):
                    raise ValueError('Rendering text required')
            for link in obs.get('links', []):
                _strict_url(link)


def import_upload(payload: bytes, format: str, metadata=None):
    """CSV uses a metadata sidecar; JSON uses {schema, metadata, pages}."""
    if not isinstance(payload, bytes) or len(payload) > MAX_BYTES:
        raise ValueError('Upload must be bytes and at most 2 MB')
    try:
        text = payload.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise ValueError('Only UTF-8 encoding is accepted') from exc
    if '\x00' in text:
        raise ValueError('NUL bytes forbidden')
    extras = {}
    if format == 'json':
        try:
            body = _json(text)
            _bounded(body)
        except (RecursionError, json.JSONDecodeError) as exc:
            raise ValueError('Malformed JSON') from exc
        if not isinstance(body, dict) or body.get('schema') != SCHEMA or not isinstance(body.get('pages'), list):
            raise ValueError('JSON requires supported schema and pages array')
        if metadata is not None:
            raise ValueError('JSON metadata must be in envelope')
        metadata = body.get('metadata')
        pages = body['pages']
        if len(pages) > MAX_ROWS or any(not isinstance(p, dict) for p in pages):
            raise ValueError('Invalid pages or row limit exceeded')
        headers = sorted(set(k for p in pages for k in p if k not in EXTRA))
        stream = io.StringIO(newline='')
        writer = csv.DictWriter(stream, fieldnames=headers)
        writer.writeheader()
        for n, page in enumerate(pages, 2):
            simple = {k: v for k, v in page.items() if k not in EXTRA}
            if any(type(v) not in {str, int} for v in simple.values()):
                raise ValueError('Page scalar fields require strings or integers; omit missing values')
            writer.writerow(simple)
            extras[n] = {k: page[k] for k in EXTRA if k in page}
        text = stream.getvalue()
    elif format != 'csv':
        raise ValueError('Format must be csv or json')
    if not isinstance(metadata, dict) or metadata.get('schema') != SCHEMA:
        raise ValueError('Metadata requires schema ' + SCHEMA)
    _bounded(metadata)
    if metadata.get('rendering_mode') is not None and not isinstance(metadata['rendering_mode'], str):
        raise ValueError('rendering_mode must be text')
    result = import_crawl(text, metadata)
    errors = result['errors']
    def error(reason, **kw):
        errors.append(dict(reason=reason, severity='error', **kw))
    for field in ('tool_version', 'configuration_hash', 'source_type', 'source_ref'):
        if not isinstance(metadata.get(field), str) or not metadata[field].strip():
            error('Required upload provenance', field=field)
    if metadata.get('source_type') not in ('first_party', 'competitor', 'synthetic'):
        error('Unsupported source_type')
    if not isinstance(metadata.get('exclusions'), list) or any(not isinstance(x, str) for x in metadata.get('exclusions', [])):
        error('exclusions must be an explicit list of strings')
    valid = []
    scopes = metadata.get('scope')
    for row in result['rows']:
        try:
            _strict_url(row['url'])
            if not isinstance(scopes, list) or not scopes or not _within(row['identity'], scopes):
                raise ValueError('Page is outside declared scope')
            extra = extras.get(row['source_row'], {})
            _extras(extra)
            row.update({k: v for k, v in extra.items() if k not in {'raw', 'rendered'}})
            row['observations'] = {k: v for k, v in extra.items() if k in {'raw', 'rendered'}}
            for field in ('crawl_depth', 'inlinks'):
                if field in row and row[field] != '':
                    if not str(row[field]).isdigit():
                        raise ValueError(field + ' must be a nonnegative integer')
                    row[field] = int(row[field])
            for field in ('canonical', 'redirect_url'):
                if row.get(field):
                    _strict_url(row[field])
            row['availability'] = {f: ('observed' if (f in row['observations'] if f in {'raw', 'rendered'} else f in row and row[f] != '') else 'unknown') for f in FIELDS}
            valid.append(row)
        except (ValueError, TypeError) as exc:
            error(str(exc), row=row['source_row'])
    result['rows'] = valid
    if not valid:
        error('No valid pages')
    result.update(contract=CONTRACT, source_hash=hashlib.sha256(payload).hexdigest(), upload_format=format)
    headers = next(csv.reader(io.StringIO(text)), [])
    numeric_status = any(_key(h) in {'status code', 'http status'} for h in headers)
    result['field_mapping'] = {h: (None if numeric_status and _key(h) == 'status' else CRAWL_ALIASES.get(_key(h))) for h in headers}
    result['field_mapping'].update({k: k for x in extras.values() for k in x})
    result['availability'] = {f: {'observed': sum(r['availability'][f] == 'observed' for r in valid), 'total': len(valid)} for f in FIELDS}
    result['valid'] = not any(e['severity'] == 'error' for e in errors)
    return result


def comparison_eligibility(left, right, mode='cross_site', required_fields=('status',), max_capture_gap_days=7):
    """Eligibility only, never inferred changes. Caller chooses fields and time window."""
    if mode not in {'cross_site', 'temporal'} or type(max_capture_gap_days) not in {int, float} or not 0 <= max_capture_gap_days <= 365:
        raise ValueError('Invalid comparison policy')
    if any(f not in FIELDS for f in required_fields):
        raise ValueError('Unknown required field')
    reasons = []
    for label, data in [('left', left), ('right', right)]:
        if data.get('contract') != CONTRACT or not data.get('valid'):
            reasons.append(label + ': invalid import or unsupported contract')
        if data.get('metadata', {}).get('complete') is not True:
            reasons.append(label + ': inventory completeness unestablished')
        for f in required_fields:
            counts = data.get('availability', {}).get(f, {})
            if not counts.get('total') or counts.get('observed') != counts.get('total'):
                reasons.append(label + ': missing field ' + f)
    a, b = left.get('metadata', {}), right.get('metadata', {})
    for f in ('profile', 'tool_version', 'configuration_hash', 'rendering_mode', 'exclusions'):
        if a.get(f) is None or a.get(f) != b.get(f):
            reasons.append('Incompatible ' + f)
    if mode == 'temporal' and a.get('scope') != b.get('scope'):
        reasons.append('Temporal comparison requires identical declared scopes')
    try:
        delta = (_stamp(b.get('captured_at')) - _stamp(a.get('captured_at'))).total_seconds()
        if mode == 'temporal' and delta <= 0:
            reasons.append('Temporal comparison requires later capture')
        if abs(delta) > max_capture_gap_days * 86400:
            reasons.append('Capture gap exceeds policy')
    except (ValueError, TypeError):
        reasons.append('Capture chronology unavailable')
    return {'contract': CONTRACT, 'mode': mode, 'eligible': not reasons, 'reasons': reasons,
            'required_fields': list(required_fields), 'max_capture_gap_days': max_capture_gap_days,
            'source_hashes': [left.get('source_hash'), right.get('source_hash')],
            'limitations': ['Completeness and provenance are uploader assertions, not independently verified.',
                            'Scope sampling and different sites can bias comparisons; no ranking guarantees.',
                            'Blank scalar values are unknown. Explicit empty lists are observed empty.']}


def main():
    import argparse
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('upload', type=Path)
    parser.add_argument('--format', choices=['csv', 'json'], required=True)
    parser.add_argument('--metadata', type=Path)
    args = parser.parse_args()
    # Bounded read even when the file changes after a size check.
    with args.upload.open('rb') as f:
        payload = f.read(MAX_BYTES + 1)
    meta = None
    if args.metadata:
        with args.metadata.open('rb') as f:
            raw = f.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            parser.error('Metadata exceeds size limit')
        meta = _json(raw.decode('utf-8-sig'))
    try:
        result = import_upload(payload, args.format, meta)
    except ValueError as exc:
        parser.exit(2, str(exc) + '\n')
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result['valid'] else 1

if __name__ == '__main__':
    raise SystemExit(main())
