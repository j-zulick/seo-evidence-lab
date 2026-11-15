"""Independent retrieval labeling harness for v0.3 (offline, no model calls).

Agents may build the sheet and score it; only people fill in relevance.
Workflow:
  1. `sheet`  writes a CSV: one row per (query, passage) with an empty
              `relevant` column, plus blank rows for the labeler's own queries.
  2. A person fills `relevant` with 1/0 and may add queries of their own.
  3. `score`  validates each filled sheet, reports baseline vs lexical
              expansion on those labels, and agreement when two labelers exist.

A result is called "independent" only when every sheet declares
`labeler_kind=human-independent`. Synthetic or agent-filled sheets are scored
but always reported as non-independent.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys

from .competitor import digest
from .evidence_search import index, load, search
from .release_retrieval import VERSION, expanded_search

FIELDS = ['query_id', 'query', 'passage_id', 'passage_text', 'relevant', 'labeler', 'labeler_kind', 'notes']
LABELER_KINDS = {'human-independent', 'human-author', 'synthetic'}
BLANK_QUERY_ROWS = 10
MAX_ROWS = 5000

# Queries proposed for labeling. They are prompts for a person, not labels.
# The labeler is encouraged to add their own phrasing in the blank rows.
PROPOSED_QUERIES = [
    'how do I keep a page out of search results',
    'which version of a page should search engines prefer',
    'how quickly does the page respond when someone clicks',
    'pages that say the same thing at different web addresses',
    'is the archive allowed to be indexed',
    'interaction to next paint',
    'stop crawlers indexing a page',
    'duplicate content signal',
    'button tap delay',
    'recipe for sourdough bread',
]


class LabelError(ValueError):
    """Validation errors name the row, never the labeler's free text."""


def make_sheet(corpus=None, queries=None):
    corpus = load() if corpus is None else corpus
    queries = PROPOSED_QUERIES if queries is None else queries
    out = io.StringIO()
    writer = csv.DictWriter(out, FIELDS, lineterminator='\n')
    writer.writeheader()
    passages = corpus['passages']
    for number, query in enumerate(queries, 1):
        for passage in passages:
            writer.writerow({'query_id': f'q{number:02d}', 'query': query, 'passage_id': passage['id'],
                             'passage_text': passage['text'], 'relevant': '', 'labeler': '',
                             'labeler_kind': '', 'notes': ''})
    for extra in range(BLANK_QUERY_ROWS):
        for passage in passages:
            writer.writerow({'query_id': f'own{extra + 1:02d}', 'query': '', 'passage_id': passage['id'],
                             'passage_text': passage['text'], 'relevant': '', 'labeler': '',
                             'labeler_kind': '', 'notes': ''})
    return out.getvalue()


def parse_sheet(text, corpus=None):
    """Return {'labeler', 'labeler_kind', 'queries': {qid: {'query', 'relevant': [ids]}}}."""
    corpus = load() if corpus is None else corpus
    passage_ids = {p['id'] for p in corpus['passages']}
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or [f.strip() for f in reader.fieldnames] != FIELDS:
        raise LabelError('Sheet header does not match the labeling template')
    labelers, kinds, queries = set(), set(), {}
    for line, row in enumerate(reader, 2):
        if line > MAX_ROWS:
            raise LabelError('Sheet has too many rows')
        relevant = (row['relevant'] or '').strip()
        query = (row['query'] or '').strip()
        if not query and not relevant:
            continue  # unused blank row
        if not query:
            raise LabelError(f'Row {line}: relevance given without a query')
        if relevant == '':
            continue  # query present but this passage left unlabeled; treated as unjudged
        if relevant not in ('0', '1'):
            raise LabelError(f'Row {line}: relevant must be 1 or 0')
        if row['passage_id'] not in passage_ids:
            raise LabelError(f'Row {line}: unknown passage')
        if len(query) > 512 or not re.fullmatch(r'[a-z]{1,5}\d{2}', row['query_id'] or ''):
            raise LabelError(f'Row {line}: invalid query')
        labelers.add((row['labeler'] or '').strip())
        kinds.add((row['labeler_kind'] or '').strip())
        entry = queries.setdefault(row['query_id'], {'query': query, 'judged': {}})
        if entry['query'] != query:
            raise LabelError(f'Row {line}: query text changed within {row["query_id"]}')
        if row['passage_id'] in entry['judged']:
            raise LabelError(f'Row {line}: duplicate judgment')
        entry['judged'][row['passage_id']] = relevant == '1'
    if not queries:
        raise LabelError('Sheet contains no judgments')
    if len(labelers) != 1 or '' in labelers:
        raise LabelError('Each sheet needs exactly one labeler name')
    if len(kinds) != 1 or not kinds <= LABELER_KINDS:
        raise LabelError('labeler_kind must be one of: ' + ', '.join(sorted(LABELER_KINDS)))
    for qid, entry in queries.items():
        if set(entry['judged']) != passage_ids:
            raise LabelError(f'{qid}: judge every passage (1 or 0) or leave the query empty')
        entry['relevant'] = sorted(pid for pid, value in entry['judged'].items() if value)
    return {'labeler': labelers.pop(), 'labeler_kind': kinds.pop(), 'queries': queries}


def kappa(a, b):
    """Cohen's kappa over shared (query text, passage) judgments."""
    pairs = []
    index_b = {(e['query'].casefold(), pid): v for e in b['queries'].values() for pid, v in e['judged'].items()}
    for entry in a['queries'].values():
        for pid, value in entry['judged'].items():
            key = (entry['query'].casefold(), pid)
            if key in index_b:
                pairs.append((value, index_b[key]))
    if not pairs:
        return {'shared_judgments': 0, 'kappa': None}
    n = len(pairs)
    observed = sum(x == y for x, y in pairs) / n
    pa, pb = sum(x for x, _ in pairs) / n, sum(y for _, y in pairs) / n
    expected = pa * pb + (1 - pa) * (1 - pb)
    value = None if expected == 1 else round((observed - expected) / (1 - expected), 4)
    return {'shared_judgments': n, 'observed_agreement': round(observed, 4), 'kappa': value}


def score(sheets, corpus=None):
    corpus = load() if corpus is None else corpus
    db = index(corpus)
    try:
        per_labeler = []
        for sheet in sheets:
            rows = []
            for qid, entry in sorted(sheet['queries'].items()):
                row = {'query_id': qid, 'query': entry['query'], 'relevant': entry['relevant']}
                for mode, engine in (('baseline', search), ('expanded', expanded_search)):
                    ids = [hit['id'] for hit in engine(db, entry['query'])]
                    expected = entry['relevant']
                    row[mode] = {'top3': ids,
                                 'passed': all(i in ids for i in expected) if expected else not ids}
                row['outcome'] = ('fixed' if row['expanded']['passed'] and not row['baseline']['passed'] else
                                  'regressed' if row['baseline']['passed'] and not row['expanded']['passed'] else
                                  'both_pass' if row['baseline']['passed'] else 'both_miss')
                rows.append(row)
            per_labeler.append({
                'labeler': sheet['labeler'], 'labeler_kind': sheet['labeler_kind'],
                'queries': len(rows),
                'baseline_passed': sum(r['baseline']['passed'] for r in rows),
                'expanded_passed': sum(r['expanded']['passed'] for r in rows),
                'outcomes': {k: sum(r['outcome'] == k for r in rows)
                             for k in ('fixed', 'regressed', 'both_pass', 'both_miss')},
                'cases': rows})
    finally:
        db.close()
    independent = bool(sheets) and all(s['labeler_kind'] == 'human-independent' for s in sheets)
    result = {'retrieval_version': VERSION, 'corpus_sha256': digest(corpus),
              'labels_sha256': digest([{k: s[k] for k in ('labeler_kind', 'queries')} for s in sheets]),
              'independent': independent, 'labelers': per_labeler,
              'limitations': ('Five-passage synthetic corpus; top-3 lexical retrieval. '
                              + ('Labels declared independent by their labelers.' if independent else
                                 'NOT an independent evaluation: at least one sheet is synthetic or author-labeled.'))}
    if len(sheets) == 2:
        result['agreement'] = kappa(*sheets)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m seo_qa.labeling')
    sub = parser.add_subparsers(dest='command', required=True)
    s = sub.add_parser('sheet', help='write a blank labeling sheet')
    s.add_argument('--out', required=True)
    c = sub.add_parser('score', help='score one or two filled sheets')
    c.add_argument('sheets', nargs='+')
    args = parser.parse_args(argv)
    if args.command == 'sheet':
        with open(args.out, 'x', encoding='utf-8', newline='') as handle:
            handle.write(make_sheet())
        print(json.dumps({'written': args.out, 'queries': len(PROPOSED_QUERIES),
                          'blank_query_slots': BLANK_QUERY_ROWS}))
        return 0
    if len(args.sheets) > 2:
        parser.error('score accepts one or two sheets')
    parsed = []
    for path in args.sheets:
        with open(path, encoding='utf-8') as handle:
            parsed.append(parse_sheet(handle.read()))
    print(json.dumps(score(parsed), indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
