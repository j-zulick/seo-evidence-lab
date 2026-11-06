"""Local adaptation of public Botworthy v3 weights, not its official scorer."""
from hashlib import sha256
import json

VERSION = 'seo-qa-quotability/1.0.0'
EXTRACTOR = 'section-html/1.0.0'
RULES = [
    ('answer_first', 'answerability', 24),
    ('question_headings', 'answerability', 10),
    ('top_summary', 'answerability', 6),
    ('standalone_openings', 'extractability', 14),
    ('short_openers', 'extractability', 4),
    ('lists_tables', 'extractability', 10),
    ('statistics_specifics', 'evidence', 10),
    ('named_sources', 'evidence', 8),
    ('named_expertise', 'evidence', 4),
    ('substantive_update', 'freshness', 10),
]


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value):
    return sha256(canonical(value).encode()).hexdigest()


def profile():
    value = {'version': VERSION, 'extractor': EXTRACTOR,
             'label': 'AI content quotability — local adaptation',
             'reference': 'https://botworthyindex.com/scoreboard',
             'reference_version': 'public v3.0.4, October 2026',
             'rules': [dict(id=i, category=c, weight=w) for i, c, w in RULES],
             'method': {
                 'answer_first': 'clamp((share-0.30)/0.50)',
                 'question_headings': 'clamp(share/0.60)',
                 'top_summary': 'explicit summary/key-points block or introductory Summary heading plus body before substantive H2/H3; binary',
                 'standalone_openings': 'clamp((share-0.70)/0.25)',
                 'short_openers': 'clamp((30-mean_words)/10)',
                 'lists_tables': '0.6*table_presence+0.4*clamp(list_blocks_per_1000_words/5)',
                 'statistics_specifics': 'clamp(reviewed_relevant_figures/5)',
                 'named_sources': 'clamp(reviewed_distinct_sources/2)',
                 'named_expertise': 'expert_quote=1, byline=0.5, none=0',
                 'substantive_update': 'clamp((24-age_months)/12); old title/URL year halves credit',
                 'age_months': 'elapsed days / (365.2425/12)',
                 'rounding': 'two decimal places; round only displayed totals',
                 'word_tokenizer': 'Unicode words with internal apostrophes/hyphens',
                 'semantic_credit': 'two distinct declared evaluator identities agree, or explicit third-party resolution',
                 'missing': 'unknown weight contributes 0 to lower bound and full weight to upper bound; no renormalization',
                 'watch_metrics': 'TOC presence and H2 count, zero weight'},
             'limits': ['Not the official Botworthy Score or a citation probability.',
                        'No model calls, network requests, crawlability verdict or citation measurement.',
                        'Semantic/identity/freshness evidence is supplied by reviewers, not independently verified.',
                        'Existing criterion-attainment scoring and acceptance are unchanged.']}
    return dict(value, sha256=digest(value))
