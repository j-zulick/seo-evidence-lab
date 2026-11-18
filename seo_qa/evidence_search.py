"""Offline lexical retrieval with immutable passage provenance."""
import json
import re
import sqlite3
import statistics
import time
from .competitor import ROOT, digest


def load():
    return json.loads((ROOT/'fixtures/retrieval/corpus.json').read_text())


def index(corpus):
    db = sqlite3.connect(':memory:')
    db.execute('CREATE VIRTUAL TABLE passages USING fts5(id UNINDEXED, source UNINDEXED, version UNINDEXED, body)')
    db.executemany('INSERT INTO passages VALUES(?,?,?,?)', [(p['id'],p['source'],p['version'],p['text']) for p in corpus['passages']])
    return db


def search(db, query):
    if len(query)>512:
        raise ValueError('Query exceeds 512 characters')
    words = re.findall(r'\w+',query)[:20]
    if not words:
        return []
    expr = ' OR '.join('"'+word+'"' for word in words)
    rows = db.execute('SELECT id,source,version,body FROM passages WHERE passages MATCH ? ORDER BY rank,id LIMIT 3',(expr,))
    return [dict(id=i,source=s,version=v,passage=t,sha256=digest([s,v,t])) for i,s,v,t in rows]


def _selected_facets(values, name):
    if values is None:
        return ()
    if not isinstance(values, (list, tuple)) or len(values) > 50:
        raise ValueError(name + ' must be a list or tuple of at most 50 values')
    if any(not isinstance(value, str) or not value or len(value) > 512 for value in values):
        raise ValueError(name + ' values must be nonempty strings of at most 512 characters')
    return tuple(sorted(set(values)))


def faceted_search(db, query='', *, sources=None, versions=None, limit=3, offset=0):
    """Search the indexed corpus with inspectable source/version facets.

    Values within one facet are ORed; different facets are ANDed. Each facet
    count ignores its own selection and retains the query and other selection.
    Counts cover every match before pagination, never just the displayed hits.
    An empty query browses the corpus. Nonempty queries use any-term lexical
    matching, as search() does, but reject more than 20 terms instead of
    silently omitting part of a faceted query.
    """
    if not isinstance(query, str) or len(query) > 512:
        raise ValueError('Query must be text of at most 512 characters')
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('Limit must be an integer from 1 through 100')
    if type(offset) is not int or not 0 <= offset <= 100_000:
        raise ValueError('Offset must be an integer from 0 through 100000')
    selections = {
        'source': _selected_facets(sources, 'Sources'),
        'version': _selected_facets(versions, 'Versions'),
    }
    words = re.findall(r'\w+', query)
    if len(words) > 20:
        raise ValueError('Faceted query exceeds 20 terms')
    expression = ' OR '.join('"' + word + '"' for word in words)

    def where(exclude=None):
        clauses, parameters = [], []
        if query.strip():
            # Punctuation-only input means no textual matches, not a browse.
            clauses.append('passages MATCH ?' if expression else '0')
            if expression:
                parameters.append(expression)
        for column, values in selections.items():
            if column != exclude and values:
                clauses.append(column + ' IN (' + ','.join('?' for _ in values) + ')')
                parameters.extend(values)
        return (' WHERE ' + ' AND '.join(clauses) if clauses else ''), parameters

    predicate, parameters = where()
    total = db.execute('SELECT COUNT(*) FROM passages' + predicate, parameters).fetchone()[0]
    order = 'rank,id' if expression else 'id'
    rows = db.execute(
        'SELECT id,source,version,body FROM passages' + predicate
        + ' ORDER BY ' + order + ' LIMIT ? OFFSET ?', [*parameters, limit, offset])
    results = [dict(id=i, source=s, version=v, passage=t, sha256=digest([s, v, t]))
               for i, s, v, t in rows]
    facets = {}
    for column, name in (('source', 'sources'), ('version', 'versions')):
        facet_predicate, facet_parameters = where(exclude=column)
        counts = dict(db.execute(
            'SELECT ' + column + ',COUNT(*) FROM passages' + facet_predicate
            + ' GROUP BY ' + column + ' ORDER BY ' + column, facet_parameters))
        # Retain an unknown selected value so a caller can show and clear it.
        for value in selections[column]:
            counts.setdefault(value, 0)
        facets[name] = [
            dict(value=value, count=counts[value], selected=value in selections[column])
            for value in sorted(counts)]
    return {
        'results': results, 'total': total, 'limit': limit, 'offset': offset,
        'has_more': offset + len(results) < total, 'facets': facets,
        'query_mode': 'any_term' if query.strip() else 'browse',
        'coverage': 'indexed_corpus_only',
        'limitations': ['Lexical matches do not establish factual support or exhaustive source coverage.'],
    }


def benchmark():
    corpus=load()
    db=index(corpus)
    rows=[]
    try:
        for item in corpus['queries']:
            timings=[]
            for _ in range(30):
                start=time.perf_counter_ns()
                hits=search(db,item['query'])
                timings.append((time.perf_counter_ns()-start)/1e6)
            ids=[hit['id'] for hit in hits]
            expected=item['expected']
            ok=all(i in ids for i in expected) if expected else not ids
            rows.append(dict(**item,results=hits,all_expected_in_top_three=ok,median_ms=statistics.median(timings)))
    finally:
        db.close()
    return {'corpus_hash':digest(corpus),'queries':rows,'accuracy':sum(r['all_expected_in_top_three'] for r in rows)/len(rows),
            'definition':'All expected passages retrieved in top three; empty expectation requires no matches. Warm in-memory lexical search, 30 repetitions.'}


if __name__=='__main__':
    print(json.dumps(benchmark(),indent=2))
