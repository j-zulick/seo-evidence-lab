"""Consume X06 uploads using its actual versioned importer, then export X07 graphs."""
import argparse
import json
from pathlib import Path
from .architecture_analysis import CONTRACT, analyze, compare, export, identity
from seo_qa.crawl_uploads import import_upload, comparison_eligibility


def graph_from_observations(observations, root=None):
    if observations.get('contract') != 'crawl-observations/v1' or not observations.get('valid'):
        raise ValueError('Valid crawl-observations/v1 import required; invalid rows cannot form a complete graph')
    meta = observations['metadata']
    # First declared scope root is deterministic, never replaced with a discovered page.
    # Caller may explicitly choose an inventory root; analyze refuses an absent root.
    root = identity(root or meta['scope'][0])
    pages = []
    for row in observations['rows']:
        available = row['availability']
        pages.append({'url': row['identity'],
                      'page_type': row['page_type'] if available['page_type'] == 'observed' else 'unknown',
                      'links': row['links'] if available['links'] == 'observed' else None})
    graph_meta = dict(meta)
    graph_meta.update(inventory_complete=meta['complete'],
                      edges_complete=all(r['availability']['links'] == 'observed' for r in observations['rows']),
                      upstream_contract=observations['contract'], upload_source_hash=observations['source_hash'],
                      edge_completeness_basis='Explicit per-page links for every imported row; exhaustive extraction remains uploader-asserted')
    graph = analyze({'schema': CONTRACT, 'root': root, 'metadata': graph_meta, 'pages': pages})
    graph['upload_source_hash'] = observations['source_hash']
    graph['upload_format'] = observations['upload_format']
    graph['limitations'].append('X06 validates supplied observations, not actual capture execution or exhaustive link extraction.')
    return graph


def run_uploads(specs, output, max_capture_gap_days=7):
    if len(specs) not in (1, 2):
        raise ValueError('Supply one or two upload specifications')
    observations = []
    sites = []
    for spec in specs:
        path = Path(spec['path'])
        # Read with a bound before loading bytes, including files that grow after stat.
        with path.open('rb') as f:
            payload = f.read(2_000_001)
        data = import_upload(payload, spec.get('format', path.suffix.lstrip('.')), spec.get('metadata'))
        sites.append(graph_from_observations(data, spec.get('root')))
        observations.append(data)
    eligibility = comparison_eligibility(*observations, required_fields=('status', 'links', 'page_type'),
                                         max_capture_gap_days=max_capture_gap_days) if len(sites) == 2 else None
    result = {'sites': sites, 'comparison': compare(*sites, upstream_eligibility=eligibility) if eligibility else None,
              'upload_contract': 'crawl-observations/v1', 'upload_source_hashes': [o['source_hash'] for o in observations]}
    export(result, output)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', help='JSON object with uploads array; paths relative to manifest')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    path = Path(args.manifest)
    manifest = json.loads(path.read_text())
    specs = []
    for original in manifest['uploads']:
        spec = dict(original)
        spec['path'] = path.parent / spec['path']
        if 'metadata_path' in spec:
            spec['metadata'] = json.loads((path.parent / spec.pop('metadata_path')).read_text())
        specs.append(spec)
    run_uploads(specs, args.output, manifest.get('max_capture_gap_days', 7))

if __name__ == '__main__':
    main()
