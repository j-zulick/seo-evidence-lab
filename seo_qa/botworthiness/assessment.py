"""Evidence-bound local content assessment. No I/O, model or acceptance writes."""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import re

from .extract import extract
from .profile import RULES, digest, profile

MAX_PAGES = 40
PATTERNS = [
    ('conditional_deflection', r'^it depends\b', 'State the usual answer and its conditions together.'),
    ('generic_warmup', r'^(?:we all know|in today.s|when it comes to|as we all|it is important to note)\b', 'Remove the warm-up only if the next sentence gives the supported answer.'),
    ('list_announcement', r'^(?:here are|the following are|let.s (?:look|explore))\b', 'Summarize the useful takeaway before introducing the list.'),
    ('rationale_before_instruction', r'^(?:because|to understand why|the reason)\b', 'For an instruction heading, consider giving the action before its rationale.'),
]


def instant(value):
    if not isinstance(value, str):
        raise ValueError('An explicit timezone-aware ISO timestamp is required')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as e:
        raise ValueError('Invalid ISO timestamp') from e
    if parsed.tzinfo is None:
        raise ValueError('Timestamp must include timezone')
    return parsed.astimezone(timezone.utc)


def clamp(x):
    return max(0.0, min(1.0, x))


def evidence_span(item, extracted):
    if not isinstance(item, dict):
        raise ValueError('Evidence span must be an object')
    block = next((b for b in extracted['blocks'] if b['locator'] == item.get('block')), None)
    if not block or not isinstance(item.get('quote'), str) or not item['quote'].strip() or item['quote'] not in block['text']:
        raise ValueError('Evidence quote must occur in the identified retained block')


def reviewer(item):
    for field in ('evaluator_id', 'evaluator_version', 'pass_id'):
        if not isinstance(item.get(field), str) or not item[field].strip():
            raise ValueError('Review requires evaluator_id, evaluator_version and pass_id')


def review_template(page, *, evaluated_at):
    report = assess_page(page, evaluated_at=evaluated_at)
    return {'schema': 'quotability-review/1', 'source_id': page['id'],
            'content_sha256': report['source']['content_sha256'],
            'profile_sha256': report['profile']['sha256'],
            'evidence_kind': page.get('evidence_kind', 'supplied'),
            'instructions': 'Fill two independent passes; null remains unknown. Claims need exact block/quote evidence. Synthetic reviewers are fixtures, not human review.',
            'evidence_inventory': {'blocks': report['blocks'], 'sections': report['sections']},
            'passes': [{'evaluator_id': '', 'evaluator_version': '', 'pass_id': '',
                        'sections': {s['id']: {'opening_sha256': s['opening_sha256'],
                                             'answer_first': None, 'standalone_openings': None}
                                     for s in report['sections']}} for _ in range(2)],
            'resolutions': [], 'claims': None}


def validate_review(review, page, extracted, evaluated):
    if not review:
        return
    if not isinstance(review, dict):
        raise ValueError('Review must be an object')
    if (review.get('schema') != 'quotability-review/1' or review.get('source_id') != page['id']
            or review.get('content_sha256') != extracted['content_sha256']
            or review.get('profile_sha256') != profile()['sha256']
            or review.get('evidence_kind') != page.get('evidence_kind', 'supplied')):
        raise ValueError('Review source, profile or evidence-kind mismatch; regenerate the template')
    sections = {s['id']: s for s in extracted['sections']}
    passes = review.get('passes', [])
    if not isinstance(passes, list) or not isinstance(review.get('resolutions', []), list):
        raise ValueError('Passes and resolutions must be lists')
    ids, evaluators = set(), set()
    for p in passes:
        if not isinstance(p, dict) or not isinstance(p.get('sections'), dict):
            raise ValueError('Coding pass requires a sections object')
        reviewer(p)
        if p['pass_id'] in ids or p['evaluator_id'] in evaluators:
            raise ValueError('Semantic passes require distinct pass and evaluator identities')
        ids.add(p['pass_id'])
        evaluators.add(p['evaluator_id'])
        for sid, item in p.get('sections', {}).items():
            if not isinstance(item, dict) or sid not in sections or item.get('opening_sha256') != sections[sid]['opening_sha256']:
                raise ValueError('Section review identity mismatch')
            for key in ('answer_first', 'standalone_openings'):
                if item.get(key) is not None and type(item[key]) is not bool:
                    raise ValueError('Section judgments must be true, false or null')
    if len(passes) > 2:
        raise ValueError('Exactly two coding passes are supported')
    resolved = set()
    for item in review.get('resolutions', []):
        if not isinstance(item, dict):
            raise ValueError('Resolution must be an object')
        reviewer(item)
        key = (item.get('section_id'), item.get('criterion'))
        if key in resolved or key[0] not in sections or key[1] not in {'answer_first', 'standalone_openings'}:
            raise ValueError('Invalid or duplicate resolution')
        resolved.add(key)
        votes = [p.get('sections', {}).get(key[0], {}).get(key[1]) for p in passes]
        if (len(votes) != 2 or any(type(v) is not bool for v in votes) or votes[0] == votes[1]
                or type(item.get('value')) is not bool or not item.get('reason')
                or item['evaluator_id'] in evaluators or item['pass_id'] in ids):
            raise ValueError('Resolution requires a real disagreement, third evaluator, decision and rationale')
    claims = review.get('claims')
    if claims is None:
        return
    if not isinstance(claims, dict):
        raise ValueError('Claims must be an object or null')
    reviewer(claims)
    for field in ('statistics', 'sources'):
        rows = claims.get(field)
        if rows is not None:
            if not isinstance(rows, list):
                raise ValueError('Claim inventories must be lists or null')
            seen, seen_spans = set(), set()
            for item in rows:
                evidence_span(item, extracted)
                identity = item.get('identity')
                if not isinstance(identity, str) or not identity.strip() or identity.casefold() in seen:
                    raise ValueError('Claim identities must be nonempty and unique')
                seen.add(identity.casefold())
                span_key = (item['block'], item['quote'])
                if span_key in seen_spans:
                    raise ValueError('Duplicate evidence spans cannot earn repeated claim credit')
                seen_spans.add(span_key)
                if field == 'statistics' and not re.search(r'\d', item['quote']):
                    raise ValueError('A reviewed statistic must contain a figure')
    expertise = claims.get('expertise')
    if expertise is not None:
        if not isinstance(expertise, dict):
            raise ValueError('Expertise must be an object or null')
        if expertise.get('kind') not in {'none', 'byline', 'expert_quote'}:
            raise ValueError('Invalid expertise kind')
        if expertise['kind'] != 'none':
            evidence_span(expertise, extracted)
            if not expertise.get('identity'):
                raise ValueError('Named expertise needs an identity')
    update = claims.get('update')
    if update is not None:
        evidence_span(update, extracted)
        at = instant(update.get('substantive_at'))
        if at > evaluated or not isinstance(update.get('change_evidence'), str) or not update['change_evidence'].strip():
            raise ValueError('Update needs nonfuture substantive date and retained change-evidence reference')


def semantic(criterion, sections, review):
    decisions = []
    for section in sections:
        votes = [p.get('sections', {}).get(section['id'], {}).get(criterion) for p in review.get('passes', [])]
        value, state = None, 'unmeasured'
        if len(votes) == 2 and all(type(v) is bool for v in votes):
            if votes[0] == votes[1]:
                value, state = votes[0], 'agreed'
            else:
                state = 'disagreement'
                resolution = next((r for r in review.get('resolutions', [])
                                   if r['section_id'] == section['id'] and r['criterion'] == criterion), None)
                if resolution:
                    value, state = resolution['value'], 'resolved'
        if section['opening'] is None:
            value, state = None, 'no_opening'
        decisions.append({'section_id': section['id'], 'value': value, 'state': state, 'votes': votes})
    if not decisions or any(d['value'] is None for d in decisions):
        return None, decisions
    return sum(d['value'] for d in decisions) / len(decisions), decisions


def assess_page(page, *, evaluated_at, review=None):
    evaluated = instant(evaluated_at)
    if not isinstance(page, dict) or not isinstance(page.get('id'), str) or not page['id'].strip():
        raise ValueError('Page requires a stable nonempty string id')
    if page.get('evidence_kind', 'supplied') not in {'supplied', 'synthetic', 'captured'}:
        raise ValueError('Unsupported evidence kind')
    captured_at = page.get('captured_at')
    if captured_at and instant(captured_at) > evaluated:
        raise ValueError('Capture cannot be after evaluation')
    if page.get('evidence_kind') == 'captured' and not captured_at:
        raise ValueError('Captured evidence requires captured_at')
    extracted = extract(page.get('content'), page.get('content_format', 'html'))
    complete = page.get('capture_complete', True)
    if type(complete) is not bool:
        raise ValueError('capture_complete must be boolean')
    if page.get('evidence_kind') == 'captured' and 'capture_complete' not in page:
        raise ValueError('Captured evidence requires explicit capture completeness')
    if review is not None and not isinstance(review, dict):
        raise ValueError('Review must be an object')
    review = {} if review is None else review
    validate_review(review, page, extracted, evaluated)
    n = len(extracted['sections'])
    measures, decisions = {}, {}
    for key, low, spread in [('answer_first', .30, .50), ('standalone_openings', .70, .25)]:
        share, decisions[key] = semantic(key, extracted['sections'], review)
        measures[key] = (None if share is None else clamp((share-low)/spread), {'share': share})
    headings = extracted['sections']
    question_share = sum(s['heading'].rstrip().endswith('?') for s in headings) / n if n else None
    measures['question_headings'] = (None if question_share is None else clamp(question_share/.60), {'share': question_share})
    measures['top_summary'] = (float(extracted['summary']), {'present': extracted['summary']})
    text_openings = [s for s in headings if s['opening_kind'] == 'text']
    # List/table openings are a different unit; never tokenize a whole table as a sentence.
    average = sum(s['opening_words'] for s in text_openings)/len(text_openings) if text_openings and all(s['opening'] is not None for s in headings) else None
    measures['short_openers'] = (None if average is None else clamp((30-average)/10), {'average_words': average, 'text_openers': len(text_openings)})
    wc = extracted['word_count']
    density = 1000*extracted['list_blocks']/wc if wc else None
    measures['lists_tables'] = (None if density is None else .60*bool(extracted['table_blocks'])+.40*clamp(density/5), {'table_blocks': extracted['table_blocks'], 'list_blocks_per_1000_words': density})
    claims = review.get('claims') or {}
    for key, field, full in [('statistics_specifics', 'statistics', 5), ('named_sources', 'sources', 2)]:
        count = len(claims[field]) if claims.get(field) is not None else None
        measures[key] = (None if count is None else clamp(count/full), {'reviewed_count': count})
    expertise = claims.get('expertise')
    measures['named_expertise'] = (None if expertise is None else {'none': 0., 'byline': .5, 'expert_quote': 1.}[expertise['kind']], {'reviewed_kind': expertise['kind'] if expertise else None})
    update = claims.get('update')
    age = (evaluated-instant(update['substantive_at'])).total_seconds()/86400/(365.2425/12) if update else None
    years = [int(y) for y in re.findall(r'(?<!\d)(?:19\d{2}|20\d{2})(?!\d)', extracted['title']+' '+str(page.get('url', '')))]
    stale_year = any(y < evaluated.year for y in years)
    measures['substantive_update'] = (None if age is None else clamp((24-age)/12)*(.5 if stale_year else 1), {'months': age, 'older_year_penalty': stale_year})
    rows = []
    for key, category, weight in RULES:
        fraction, measurement = measures[key]
        if not complete or not wc:
            fraction = None
        rows.append({'id': key, 'category': category, 'weight': weight,
                     'state': 'measured' if fraction is not None else 'unmeasured',
                     'fraction': fraction, 'points': None if fraction is None else round(weight*fraction, 2),
                     'measurement': measurement, 'content_sha256': extracted['content_sha256']})
    unknown = sum(r['weight'] for r in rows if r['fraction'] is None)
    lower = sum(r['weight']*r['fraction'] for r in rows if r['fraction'] is not None)
    categories = {}
    for category in dict.fromkeys(r['category'] for r in rows):
        subset = [r for r in rows if r['category'] == category]
        lo = sum(r['weight']*r['fraction'] for r in subset if r['fraction'] is not None)
        missing = sum(r['weight'] for r in subset if r['fraction'] is None)
        categories[category] = {'maximum': sum(r['weight'] for r in subset), 'lower': round(lo, 2), 'upper': round(lo+missing, 2)}
    feedback = []
    for s in headings:
        for key, pattern, action in PATTERNS:
            if re.search(pattern, s['opening'] or '', re.I):
                feedback.append({'section_id': s['id'], 'pattern': key, 'kind': 'heuristic_review_prompt',
                                 'quote': s['opening'], 'action': action})
        verdict = next(d for d in decisions['answer_first'] if d['section_id'] == s['id'])
        if verdict['value'] is False:
            feedback.append({'section_id': s['id'], 'pattern': 'answer_not_first', 'kind': 'reviewed',
                             'quote': s['opening'], 'action': 'Move the supported answer into the first sentence; preserve its conditions and evidence.'})
    return {'schema': 'quotability-page/1', 'profile': profile(),
            'source': {'id': page['id'], 'url': page.get('url'), 'title': page.get('title') or extracted['title'] or page['id'],
                       'content_sha256': extracted['content_sha256'], 'content_format': extracted['content_format'],
                       'evidence_kind': page.get('evidence_kind', 'supplied'), 'captured_at': captured_at, 'capture_complete': complete},
            'evaluated_at': evaluated_at, 'status': 'complete' if unknown == 0 else 'incomplete',
            'score': round(lower, 2) if unknown == 0 else None,
            'bounds': {'lower': round(lower, 2), 'upper': round(lower+unknown, 2)},
            'evidence_coverage_weight': 100-unknown, 'categories': categories,
            'criteria': rows, 'sections': headings, 'blocks': extracted['blocks'],
            'word_count': wc, 'watch_metrics': extracted['watch_metrics'], 'semantic_decisions': decisions,
            'feedback': feedback, 'review': deepcopy(review), 'review_sha256': digest(review),
            'benchmark': {'status': 'unavailable', 'reason': 'No verified sector dataset supplied.'},
            'citation_observations': {'status': 'unmeasured'}, 'acceptance': 'not_assessed'}


def assess_library(pages, *, evaluated_at, reviews=None, expected_ids=None, sampled=False):
    if not isinstance(pages, list) or not 1 <= len(pages) <= MAX_PAGES:
        raise ValueError('A library must contain 1-40 pages')
    if type(sampled) is not bool:
        raise ValueError('sampled must be boolean')
    if any(not isinstance(p, dict) for p in pages):
        raise ValueError('Every page must be an object')
    ids = [p.get('id') for p in pages]
    if any(not isinstance(i, str) or not i.strip() for i in ids) or len(set(ids)) != len(ids):
        raise ValueError('Page identities must be unique nonempty strings')
    reviews = {} if reviews is None else reviews
    if not isinstance(reviews, dict) or set(reviews)-set(ids):
        raise ValueError('Review identities do not match this library')
    expected = ids if expected_ids is None else expected_ids
    if not isinstance(expected, list) or any(not isinstance(i, str) or not i for i in expected) or len(set(expected)) != len(expected):
        raise ValueError('Expected identities must be a unique list')
    reports = [assess_page(p, evaluated_at=evaluated_at, review=reviews.get(p['id'])) for p in pages]
    patterns = {}
    for report in reports:
        for f in report['feedback']:
            bucket = patterns.setdefault(f['pattern'], {'occurrences': 0, 'page_ids': []})
            bucket['occurrences'] += 1
            if report['source']['id'] not in bucket['page_ids']:
                bucket['page_ids'].append(report['source']['id'])
    missing, unexpected = sorted(set(expected)-set(ids)), sorted(set(ids)-set(expected))
    return {'schema': 'quotability-library/1', 'profile': profile(), 'evaluated_at': evaluated_at,
            'coverage': {'sampled': sampled, 'expected_ids': expected, 'observed_ids': ids,
                         'missing_ids': missing, 'unexpected_ids': unexpected,
                         'scope': 'supplied_batch' if expected_ids is None else 'declared_library',
                         'complete_declared_coverage': not sampled and not missing and not unexpected},
            'status': 'complete' if not sampled and not missing and not unexpected and all(p['status'] == 'complete' for p in reports) else 'incomplete',
            'pages': reports, 'patterns': patterns,
            'benchmark': {'status': 'unavailable'}, 'limits': profile()['limits']}


def compare(before, after):
    if before.get('schema') != 'quotability-library/1' or after.get('schema') != 'quotability-library/1':
        raise ValueError('Compare requires library reports')
    same_profile = before['profile'] == after['profile']
    left = {p['source']['id']: p for p in before['pages']}
    right = {p['source']['id']: p for p in after['pages']}
    def evaluators(page):
        review = page.get('review') or {}
        records = review.get('passes', []) + review.get('resolutions', [])
        if review.get('claims'):
            records += [review['claims']]
        return sorted({(p['evaluator_id'], p['evaluator_version']) for p in records})
    rows = []
    for identity in sorted(left.keys() & right.keys()):
        a, b = left[identity], right[identity]
        same_evaluators = evaluators(a) == evaluators(b)
        comparable = same_profile and same_evaluators and a['score'] is not None and b['score'] is not None
        sections_a = {s['id']: s for s in a['sections']}
        sections = [{'section_id': s['id'], 'identity_matched': s['id'] in sections_a and sections_a[s['id']]['heading'] == s['heading'],
                     'opening_changed': sections_a.get(s['id'], {}).get('opening_sha256') != s['opening_sha256']} for s in b['sections']]
        rows.append({'id': identity, 'content_changed': a['source']['content_sha256'] != b['source']['content_sha256'],
                     'evaluator_changed': not same_evaluators, 'comparable': comparable,
                     'score_delta': round(b['score']-a['score'], 2) if comparable else None,
                     'before_bounds': a['bounds'], 'after_bounds': b['bounds'], 'sections': sections})
    return {'schema': 'quotability-comparison/1', 'methodology_changed': not same_profile,
            'added_ids': sorted(right.keys()-left.keys()), 'removed_ids': sorted(left.keys()-right.keys()),
            'pages': rows, 'meaning': 'Content assessment change; no AI-citation effect established.'}


def attach_to_report(report, library):
    """Additive projection: never modify scoring, findings, review or acceptance."""
    result = deepcopy(report)
    result['quotability'] = deepcopy(library)
    return result
