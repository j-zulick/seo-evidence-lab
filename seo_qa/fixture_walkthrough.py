"""One offline fixture walkthrough; only the supplied output folder is written."""
import argparse
import json
import sqlite3
from pathlib import Path
from opentelemetry.trace import Status,StatusCode
from .competitor import demo, ingest, compare
from .fixture_trace import LocalTrace
from .measurement import fixture_view
from .timeline_eval import run
from .evidence_search import benchmark


def walkthrough(out):
    out.mkdir(parents=True,exist_ok=True)
    trace=LocalTrace()
    with trace.stage('comparison'):
        data=demo()
    with sqlite3.connect(':memory:') as conn:
        with trace.stage('import'):
            original=ingest(conn,data['attempts'][0])
        with trace.stage('import') as span:
            failed=ingest(conn,data['attempts'][-1])
            assert failed is None
            span.set_status(Status(StatusCode.ERROR))
            assert conn.execute('SELECT id FROM versions').fetchone()[0]==original
        with trace.stage('retry'):
            recovered=ingest(conn,data['attempts'][2])
            assert recovered and recovered!=original
        with trace.stage('diff'):
            import difflib
            old,new=[conn.execute('SELECT body FROM versions WHERE id=?',(k,)).fetchone()[0] for k in (original,recovered)]
            assert list(difflib.unified_diff(old.splitlines(),new.splitlines()))
        with trace.stage('replay'):
            assert compare(*[data['comparison'][k]['inputs'] for k in ('before','after')])==data['comparison']
    reports={'timeline':data,'measurement':fixture_view(),'retrieval':benchmark(),'traces':trace.finish()}
    for key,value in reports.items():
        (out/(key+'.json')).write_text(json.dumps(value,indent=2)+'\n')
    run(out/'evaluation')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path('demo-output'))
    walkthrough(parser.parse_args().output)
