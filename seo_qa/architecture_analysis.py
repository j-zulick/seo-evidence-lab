"""Offline directed crawl graph analysis; proposed X06 boundary, no network."""
import argparse
from collections import Counter, deque
import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

CONTRACT = 'x07-graph-input/v1'

def identity(value):
    if not isinstance(value, str) or any(ord(c) < 33 for c in value) or '\\' in value:
        raise ValueError('Invalid URL')
    p = urlsplit(value)
    if p.scheme.lower() not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise ValueError('Absolute HTTP(S) URL without credentials required')
    host = p.hostname.lower()
    if ':' in host:
        host = '[' + host + ']'
    port = p.port
    if port and (p.scheme.lower(), port) not in [('http', 80), ('https', 443)]:
        host += ':' + str(port)
    return urlunsplit((p.scheme.lower(), host, p.path or '/', p.query, ''))

def analyze(data):
    if data.get('schema') != CONTRACT:
        raise ValueError('Unsupported graph contract')
    meta = data['metadata']
    stamp = datetime.fromisoformat(meta['captured_at'].replace('Z', '+00:00'))
    if stamp.tzinfo is None or not meta.get('source_ref'):
        raise ValueError('Timezone and provenance required')
    for key in ('inventory_complete', 'edges_complete'):
        if type(meta.get(key)) is not bool:
            raise ValueError('Explicit completeness booleans required')
    if not isinstance(meta.get('exclusions'), list):
        raise ValueError('Explicit exclusion inventory required')
    rows = data['pages']
    if not isinstance(rows, list) or not 1 <= len(rows) <= 10000:
        raise ValueError('Expected 1..10000 pages')
    root = identity(data['root'])
    origin = urlsplit(root).netloc
    pages = {}
    for row in rows:
        url = identity(row['url'])
        if url in pages or urlsplit(url).netloc != origin:
            raise ValueError('Duplicate identity or cross-host inventory')
        links = row.get('links')
        if links is not None and (not isinstance(links, list) or len(links) > 10000):
            raise ValueError('links must be null or bounded list')
        if not isinstance(row.get('page_type', 'unknown'), str):
            raise ValueError('page_type must be text')
        pages[url] = row
    if root not in pages:
        raise ValueError('Root absent from inventory')
    edges, external = set(), set()
    missing_observations = sorted(u for u, r in pages.items() if r.get('links') is None)
    for source, row in pages.items():
        for raw in row.get('links') or []:
            if not isinstance(raw, str) or not raw:
                raise ValueError('Invalid link observation')
            target = identity(urljoin(source, raw))
            if urlsplit(target).netloc == origin:
                edges.add((source, target))
            else:
                external.add((source, target))
    outgoing = {u: set() for u in pages}
    incoming = Counter()
    dangling = []
    for source, target in sorted(edges):
        if target in pages:
            outgoing[source].add(target)
            incoming[target] += 1
        else:
            dangling.append({'source': source, 'target': target})
    depths = {root: 0}
    queue = deque([root])
    while queue:
        source = queue.popleft()
        for target in sorted(outgoing[source]):
            if target not in depths:
                depths[target] = depths[source] + 1
                queue.append(target)
    sufficient = meta['inventory_complete'] and meta['edges_complete'] and not missing_observations and not dangling and not meta.get('exclusions')
    nodes = [{'url': u, 'page_type': pages[u].get('page_type', 'unknown'),
              'depth': depths.get(u), 'reachable': u in depths,
              'observed_in_degree': incoming[u], 'observed_out_degree': len(outgoing[u]),
              'orphan_candidate': (incoming[u] == 0 and u != root) if sufficient else None}
             for u in sorted(pages)]
    templates = Counter((pages[s].get('page_type', 'unknown'), pages[t].get('page_type', 'unknown'))
                        for s, t in edges if t in pages)
    suggestions = []
    for edge in dangling:
        suggestions.append({'priority': 1, 'kind': 'unobserved_destination', 'evidence': edge,
                            'action': 'Check crawl coverage and destination; absence does not prove a broken link.'})
    for node in nodes:
        if node['orphan_candidate']:
            suggestions.append({'priority': 2, 'kind': 'orphan_candidate', 'evidence': node['url'],
                                'action': 'Review intended discovery path and add a relevant link if appropriate.'})
    limitations = ['Observed graph only; no ranking guarantee. Depth is shortest observed directed path, not crawler-reported depth.',
                   'Declared completeness is supplied provenance, not independently verified; link counts are unique document edges.']
    if not sufficient:
        limitations.append('Sampling bias: orphan classification withheld; incomplete inventory/edges, excluded scope, unknown links or unobserved destinations.')
    return {'schema': 'x07-architecture/v1', 'input_sha256': hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest(),
            'root': root, 'metadata': meta, 'nodes': nodes, 'directed_edges': [list(e) for e in sorted(edges)],
            'unobserved_destinations': dangling, 'missing_link_observations': missing_observations,
            'external_edge_count': len(external), 'orphan_eligible': sufficient,
            'summary': {'pages': len(pages), 'internal_edges': len(edges), 'reachable_pages': len(depths)},
            'hubs': sorted(nodes, key=lambda n: (-n['observed_in_degree'], n['url']))[:5],
            'page_types': dict(sorted(Counter(n['page_type'] for n in nodes).items())),
            'templates': [{'source_type': s, 'target_type': t, 'edges': count} for (s, t), count in sorted(templates.items())],
            'suggestions': suggestions, 'limitations': limitations}

def compare(left, right, upstream_eligibility=None):
    reasons = []
    if upstream_eligibility is None:
        for field in ('rendering_mode', 'scope_kind', 'link_policy', 'capture_window'):
            a, b = left['metadata'].get(field), right['metadata'].get(field)
            if not a or a != b:
                reasons.append('Missing or incompatible ' + field)
    else:
        if upstream_eligibility.get('contract') != 'crawl-observations/v1':
            raise ValueError('Unsupported upstream eligibility contract')
        reasons.extend(upstream_eligibility['reasons'])
        if not upstream_eligibility['eligible'] and not reasons:
            reasons.append('Upstream comparison withheld')
    if not left['orphan_eligible'] or not right['orphan_eligible']:
        reasons.append('Incomplete graph coverage')
    if 'unknown' in left['page_types'] or 'unknown' in right['page_types']:
        reasons.append('Page type mapping incomplete')
    eligible = not reasons
    return {'schema': 'x07-comparison/v1', 'eligible': eligible, 'upstream_eligibility': upstream_eligibility, 'limitations': reasons + ['Cross-site differences are descriptive; industry, scale and editorial intent can differ.'],
            'left_input_sha256': left['input_sha256'], 'right_input_sha256': right['input_sha256'],
            'observed_summaries': {'left': left['summary'], 'right': right['summary']},
            'deltas_right_minus_left': {k: right['summary'][k] - v for k, v in left['summary'].items()} if eligible else None,
            'page_type_share_differences': {t: right['page_types'].get(t, 0)/right['summary']['pages'] - left['page_types'].get(t, 0)/left['summary']['pages'] for t in sorted(left['page_types'].keys() | right['page_types'].keys())} if eligible else None,
            'template_differences': {'left': left['templates'], 'right': right['templates']} if eligible else None}

def safe_cell(value):
    value = str(value)
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) else value

def export(result, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    (output / 'report.json').write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
    with (output / 'nodes.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(result['sites'][0]['nodes'][0]))
        writer.writeheader()
        for site in result['sites']:
            for node in site['nodes']:
                writer.writerow({k: safe_cell(v) for k, v in node.items()})
    with (output / 'edges.csv').open('w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['site_root', 'source', 'target', 'destination_in_inventory'])
        for site in result['sites']:
            inventory = {n['url'] for n in site['nodes']}
            for source, target in site['directed_edges']:
                writer.writerow([safe_cell(site['root']), safe_cell(source), safe_cell(target), target in inventory])
    lines = ['# Offline architecture analysis', '', 'Synthetic inputs are fixtures when so labeled. No ranking guarantees.', '']
    for site in result['sites']:
        lines += [site['root'], '', json.dumps(site['summary']), '', *site['limitations'], '']
        for suggestion in site['suggestions']:
            lines.append(json.dumps(suggestion))
    lines += ['', 'Comparison:', json.dumps(result.get('comparison'), sort_keys=True)]
    (output / 'report.md').write_text('\n'.join(lines) + '\n')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('inputs', nargs='+')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    if len(args.inputs) not in (1, 2):
        parser.error('Supply one or two datasets')
    sites = []
    for path in args.inputs:
        if Path(path).stat().st_size > 5_000_000:
            raise ValueError('Input exceeds 5 MB')
        sites.append(analyze(json.loads(Path(path).read_text(encoding='utf-8'))))
    result = {'sites': sites, 'comparison': compare(*sites) if len(sites) == 2 else None}
    export(result, args.output)

if __name__ == '__main__':
    main()
