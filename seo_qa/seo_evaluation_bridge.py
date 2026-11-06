"""Portable SEO evaluation bridge. Supply actual pilot and E1 dependencies.

No source loaders, stores, hosted configuration, model calls or network here.
Dependencies own technical inspection and lexical mapping respectively.
"""
from copy import deepcopy
from hashlib import sha256


def evaluate(bundle, state_dir, *, review_card, map_query):
    if bundle.get('domain') != 'seo' or bundle['pilot_case'].get('domain') != 'seo':
        raise ValueError('SEO-only bridge; GTM Review owns a separate identity/store')
    case = bundle['pilot_case']
    if case.get('origin') != 'synthetic':
        raise ValueError('Synthetic captures only')
    if not bundle['passages']:
        raise ValueError('Explicit candidate passages required')
    html = case['capture']['html']
    capture_hash = sha256(html.encode()).hexdigest()
    # Raw containment is provenance binding, not a semantic/visibility guarantee.
    for passage in bundle['passages']:
        if passage['url'] != case['url'] or passage['text'] not in html:
            raise ValueError('Passage detached from supplied capture URL/bytes')
    mapping = map_query(deepcopy(bundle['query']), deepcopy(bundle['passages']), deepcopy(bundle['sources']))
    query = bundle['query']
    for key in ('origin', 'evidence_class', 'scope', 'synthetic'):
        if mapping[key] != query[key]:
            raise ValueError('Mapping changed class or scope provenance')
    if mapping['verdict'] == 'supported':
        passage = next(p for p in bundle['passages'] if p['id'] == mapping['passage_id'])
        if (mapping['passage_sha256'] != sha256(passage['text'].encode()).hexdigest()
                or passage['text'][mapping['span_start']:mapping['span_end']] != mapping['quote']):
            raise ValueError('Mapped quote/hash does not resolve to source bytes')
    card = review_card(deepcopy(case), state_dir)
    if card['evidence_sha256'] and card['evidence_sha256'] != capture_hash:
        raise ValueError('Pilot report and mapping capture differ')
    technical_accepted = bool(card['report'] and card['report']['acceptance_current'])
    lexical_supported = mapping['verdict'] == 'supported'
    return {'id': bundle['id'], 'domain': 'seo', 'synthetic': True,
            'pilot': card, 'mapping': mapping, 'capture_sha256': capture_hash,
            'technical_acceptance_current': technical_accepted,
            'bridge_ready': lexical_supported and technical_accepted and card['accept_allowed'],
            'semantic_miss': bundle.get('semantic_support_expected') is True and mapping['verdict'] == 'no_passage',
            'semantic_label_origin': 'authored fixture rubric; independent human judgment unobserved',
            'coverage': {'promises': card['promise_count'], 'supported_promises': card['supported'],
                         'promise_gaps': len(card['gaps']), 'findings': len(card['findings']),
                         'lexical_queries': 1, 'lexically_supported': int(lexical_supported)},
            'review_items': card['review_items'] + 1,
            'human_review_seconds': None, 'human_timing_gate': 'unrun',
            'live_acceptance': False, 'traffic_ai_revenue_claim': None,
            'limits': ['Raw capture containment binds provenance, not semantic entailment or visibility.',
                       'Lexical support cannot authorize Store acceptance or website/CRM actions.',
                       'Technical review is synthetic local evidence only; no ranking/AI/traffic/revenue outcome.',
                       'GTM Review owns separate domain identities/stores; no GTM object created.']}


def summarize(rows):
    return {'bundles': len(rows),
            'lexically_supported': sum(r['mapping']['verdict'] == 'supported' for r in rows),
            'technical_accepted': sum(r['technical_acceptance_current'] for r in rows),
            'bridge_ready': sum(r['bridge_ready'] for r in rows),
            'semantic_misses': sum(r['semantic_miss'] for r in rows),
            **{key: sum(r['coverage'][key] for r in rows) for key in
               ['promises', 'supported_promises', 'promise_gaps', 'findings']},
            'review_items': sum(r['review_items'] for r in rows)}
