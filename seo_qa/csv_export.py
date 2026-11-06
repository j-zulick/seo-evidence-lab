"""Offline, minimized spreadsheet report projection. No I/O or store access."""
import csv
import hashlib
import io
import ipaddress
import json
import re

SCHEMA = 'seo-qa-report-csv/1'
COLUMNS = ('schema_version', 'report_ref', 'record_type', 'record_ref', 'parent_ref',
           'check', 'outcome', 'payload_json')
OUTCOMES = {'pass', 'fail', 'blocked', 'inconclusive', 'not_applicable'}
URL = re.compile(r'(?i)(?:[a-z][a-z0-9+.-]*://|www\.|(?<!:)//)[^\s<>"\'\]\[{},]+')
IP = re.compile(r'(?<![\w:])(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?(?:/[^\s<>"\']*)?')
IPV6 = re.compile(r'(?<![\w:])[0-9A-Fa-f:]{2,}(?:%[A-Za-z0-9]+)?(?![\w:])')
SECRET_KEY = re.compile(r'(?i)(token|secret|password|authorization|cookie|session|api.?key|credential)')
CREDENTIAL = re.compile(r'(?i)(?:bearer\s+\S+|(?:token|secret|password|api[_-]?key)\s*[:=]\s*[^\s,;]+|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def ref(value):
    return 'sha256:' + hashlib.sha256(str(value).encode('utf-8')).hexdigest()


def scrub(value):
    if isinstance(value, str):
        value = URL.sub(lambda m: ref(m.group()), value)
        value = IP.sub(lambda m: ref(m.group()), value)
        def ipv6_reference(match):
            try:
                ipaddress.IPv6Address(match.group())
            except ValueError:
                return match.group()
            return ref(match.group())
        value = IPV6.sub(ipv6_reference, value)
        return CREDENTIAL.sub('[REDACTED]', value)
    if isinstance(value, dict):
        return {scrub(str(k)): '[REDACTED]' if SECRET_KEY.search(str(k)) else scrub(v)
                for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise ValueError('Report must contain JSON values')


def pick(value, fields):
    return {k: scrub(value.get(k)) for k in fields}


def safe_cell(value):
    value = str(value)
    # Control characters before a formula are ignored by common spreadsheets.
    significant = value.lstrip(''.join(chr(n) for n in range(33)) + '\u00a0\ufeff')
    if value.startswith("'") or value.startswith(('\t', '\r', '\n')) or significant.startswith(('=', '+', '-', '@')):
        return "'" + value
    return value


def project_rows(report):
    """Return schema rows; findings retain their observation parent."""
    report_ref = ref(canonical(report))
    root = str(report['id'])
    rows = []

    def add(kind, key, parent, payload, check='', outcome=''):
        rows.append(dict(zip(COLUMNS, (SCHEMA, report_ref, kind, key, parent,
                                      scrub(check), outcome, canonical(payload)))))

    run = pick(report, ('id', 'requirement_id', 'version', 'state', 'stale', 'superseded', 'latest_run_id', 'created', 'data_mode', 'coverage', 'validation', 'executable_actions_enabled'))
    # No truthiness coercion: absent/invalid acceptance is explicitly unknown.
    run['acceptance_current'] = report.get('acceptance_current') if type(report.get('acceptance_current')) is bool else None
    add('run', root, '', run)
    req = report.get('requirement') or {}
    requirement = pick(req, ('id', 'title', 'version', 'current_version', 'content_hash', 'expected'))
    scope_input = (req.get('expected') or {}).get('seo_qa')
    if isinstance(scope_input, dict):
        # The full input belongs in the authorized JSON/evidence archive, not in
        # a minimized CSV/Markdown requirement cell. Retain identity and scope.
        criteria = scope_input.get('criteria') or {}
        requirement['expected'] = {'seo_qa': scrub({
            'schema': scope_input.get('schema'), 'scopes': scope_input.get('scopes'),
            'source_kind': scope_input.get('source_kind'),
            'content_format': scope_input.get('content_format'),
            'criteria_version': criteria.get('version'), 'criteria_sha256': criteria.get('sha256')})}
    requirement['url_refs'] = [ref(u) for u in req.get('urls', [])]
    add('requirement', root + '/requirement', root, requirement)

    def finding_rows(findings, parent):
        for i, finding in enumerate(findings):
            payload = pick(finding, ('check', 'expected', 'observed', 'reason'))
            outcome = finding.get('outcome')
            payload['outcome'] = outcome if outcome in OUTCOMES else 'unknown'
            if outcome not in OUTCOMES:
                payload['source_outcome'] = scrub(outcome)
            add('finding', parent + '/finding/' + str(i), parent, payload,
                finding.get('check') or '', payload['outcome'])

    observations = report.get('observations') or []
    for i, observation in enumerate(observations):
        parent = root + '/observation/' + str(i)
        payload = pick(observation, ('status', 'observed_at', 'data_mode', 'limitations'))
        payload.update(url_ref=ref(observation['url']) if observation.get('url') else None,
                       final_url_ref=ref(observation['final_url']) if observation.get('final_url') else None,
                       evidence_sha256=(observation.get('evidence') or {}).get('sha256'))
        evidence = observation.get('evidence') or {}
        if isinstance(evidence.get('provenance'), dict):
            payload['provenance'] = pick(evidence['provenance'], ('kind', 'capture_limitations'))
        if isinstance(evidence.get('criteria'), dict):
            payload['criteria'] = pick(evidence['criteria'], ('version', 'sha256'))
        add('observation', parent, root, scrub(payload))
        finding_rows(observation.get('findings') or [], parent)
    if not observations:
        finding_rows(report.get('findings') or [], root)
    for i, review in enumerate(report.get('reviews') or []):
        add('review', root + '/review/' + str(i), root,
            pick(review, ('id', 'run_id', 'decision', 'reason', 'binding', 'created')))
    for i, limit in enumerate(report.get('limits') or []):
        add('limit', root + '/limit/' + str(i), root, {'text': scrub(limit)})
    # Additive score records follow the existing observation/finding sequence;
    # preserve all G06 provenance, minimization and projection records.
    from .scoring import score_report
    scoring = score_report(report)
    add('scoring', root + '/scoring', root, scrub({k: v for k, v in scoring.items() if k != 'missed_items'}))
    for i, item in enumerate(scoring['missed_items']):
        add('missed_item', root + '/missed/' + str(i), root, scrub(item),
            item['criterion_id'], item['outcome'])

    knowledge = report.get('knowledge')
    if knowledge and knowledge.get('release_id'):
        add('knowledge', root + '/knowledge', root, scrub({
            key: knowledge.get(key) for key in (
                'release_id', 'evaluated_on', 'admitted_at', 'mode', 'freshness',
                'manifest', 'snapshots', 'rule_registry', 'explanations', 'limitations')
        }))
        add('knowledge_records', root + '/knowledge-records', root, {
            'records': [{'id': item['value']['id'], 'sha256': item['sha256']}
                        for item in knowledge.get('records', [])]})

    add('projection', root + '/projection', root, {
        'authoritative_format': 'JSON',
        'omissions': ['raw input and capture evidence', 'complete criteria and scope guidance',
                      'full knowledge record bodies and evidence blobs'],
        'privacy': 'URLs are hashed and credential patterns redacted; free text may remain private.'})
    # Structural references also originate in input.
    return [{k: scrub(v) for k, v in row.items()} for row in rows]


def export_report_csv(report):
    """Return UTF-8 BOM bytes for an authorized, integrity-checked report."""
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, quoting=csv.QUOTE_ALL, lineterminator='\r\n')
    writer.writerow(COLUMNS)
    for row in project_rows(report):
        writer.writerow([safe_cell(row[k]) for k in COLUMNS])
    return stream.getvalue().encode('utf-8-sig')


def read_report_csv(data):
    """Decode this interchange projection, not a Store import or review action."""
    reader = csv.reader(io.StringIO(data.decode('utf-8-sig'), newline=''), strict=True)
    if next(reader, None) != list(COLUMNS):
        raise ValueError('Unsupported CSV columns')
    result = []
    for cells in reader:
        if len(cells) != len(COLUMNS):
            raise ValueError('Invalid row width')
        row = dict(zip(COLUMNS, [c[1:] if c.startswith("'") else c for c in cells]))
        if row['schema_version'] != SCHEMA:
            raise ValueError('Unsupported CSV version')
        row['payload'] = json.loads(row.pop('payload_json'))
        result.append(row)
    return result
