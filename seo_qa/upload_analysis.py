"""Offline integration of explicit uploaded observations; never collects pages."""
from .crawl_uploads import import_upload, comparison_eligibility
from .architecture_analysis import analyze as graph, compare as compare_graphs
from .performance_analysis import analyze as performance, FIELDS
from .architecture_upload import graph_from_observations

def analyze_dataset(dataset):
    if dataset.get('contract') != 'crawl-observations/v1' or not dataset.get('valid'):
        return {'status': 'invalid', 'errors': dataset.get('errors', []),
                'graph': None, 'performance': None, 'rendering': []}
    meta, rows = dataset['metadata'], dataset['rows']
    pages = [{'url': row['identity'],
              'links': row.get('links') if row['availability']['links'] == 'observed' else None,
              'page_type': row.get('page_type', 'unknown')} for row in rows]
    graph_result = None
    if pages and meta['scope'][0] in {page['url'] for page in pages}:
        graph_result = graph_from_observations(dataset)
    observations, renderings = [], []
    for row in rows:
        for item in row.get('performance', []):
            observations.append({**item, 'url': row['identity'],
                                 'synthetic': meta['source_type'] == 'synthetic'})
        captures = row.get('observations', {})
        raw, rendered = captures.get('raw'), captures.get('rendered')
        paired = bool(raw and rendered and all(raw.get(key) == rendered.get(key)
                      for key in ('source', 'capture_id', 'captured_at', 'device')))
        renderings.append({'url': row['identity'], 'paired': paired,
            'capture_provenance': {'raw': raw, 'rendered': rendered},
            'fields': {name: ('missing' if not paired or name not in raw or name not in rendered
                       else 'same' if raw[name] == rendered[name] else 'changed') for name in FIELDS}})
    return {'status': 'analyzed', 'input_sha256': dataset['source_hash'], 'graph': graph_result,
            'performance': performance({'schema': 'x08-observations/v1', 'observations': observations}),
            'rendering': renderings,
            'limitations': ['Upload completeness, capture dates and pairing are uploader assertions.',
                'No live collection, p75 field-window inference, rankings or missing-edge inference.',
                'Rendering pairs are separate from device/kind performance observations.']}

def analyze_upload(payload, format, metadata=None):
    dataset = import_upload(payload, format, metadata)
    return {'dataset': dataset, 'analysis': analyze_dataset(dataset)}

def compare_uploads(left, right):
    eligibility = comparison_eligibility(left, right, required_fields=['status', 'links', 'page_type'])
    a, b = analyze_dataset(left), analyze_dataset(right)
    comparison = None
    if eligibility['eligible'] and a['graph'] and b['graph']:
        comparison = compare_graphs(a['graph'], b['graph'], upstream_eligibility=eligibility)
    return {'schema': 'uploaded-site-comparison/v1', 'eligibility': eligibility,
            'left': a, 'right': b, 'graph_comparison': comparison,
            'performance_comparison': 'not_assessed: device/kind/representative windows require explicit comparison policy'}
