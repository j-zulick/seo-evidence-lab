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
