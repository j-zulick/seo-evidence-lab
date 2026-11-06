"""Offline capture history and reproducible, fixture-scoped finding comparisons."""
import argparse
import difflib
import hashlib
import itertools
import json
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
FACTORS = ('article', 'guidance', 'scoring', 'evidence')


def digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest()


def url_key(value):
    if not isinstance(value,str) or len(value)>2048 or any(c.isspace() for c in value):
        raise ValueError("Invalid page URL")
    parts = urlsplit(value)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('Expected an HTTP(S) page URL')
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or '/', parts.query, ''))


def ingest(conn, item):
    """Commit one attempt and its optional content version in one transaction."""
    if not isinstance(item,dict) or len(json.dumps(item).encode())>500_000:
        raise ValueError('Capture exceeds local input limit')
    request = url_key(item['requested_url'])
    captured = datetime.fromisoformat(item['captured_at'])
    if captured.tzinfo is None:
        raise ValueError('Capture time requires an offset')
    response = item['response']
    if not isinstance(response,dict) or not isinstance(response.get('success'),bool):
        raise ValueError('Expected Firecrawl success flag')
    data = response.get('data') or {}
    if not isinstance(data,dict):
        raise ValueError('Invalid Firecrawl data')
    meta = data.get('metadata') or {}
    if not isinstance(meta,dict):
        raise ValueError('Expected capture metadata object')
    final = url_key(meta.get('sourceURL') or request)
    body = data.get('markdown')
    status = meta.get('statusCode')
    if status is not None and (type(status) is not int or not 100<=status<=599):
        raise ValueError('Invalid HTTP status')
    usable = response.get('success') is True and isinstance(body, str) and body.strip() and (status is None or 200 <= status < 300)
    # Preserve publisher wording and line structure. Ignore whitespace-only churn.
    normalized = '\n'.join(' '.join(line.split()) for line in body.splitlines() if line.strip()) if usable else None
    version = digest([final, normalized]) if usable else None
    attempt = digest(item)
    with conn:
        conn.execute('CREATE TABLE IF NOT EXISTS versions(id TEXT PRIMARY KEY,url TEXT,body TEXT)')
        conn.execute('CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY,requested TEXT,final TEXT,checked TEXT,version TEXT,record TEXT)')
        if version:
            conn.execute('INSERT OR IGNORE INTO versions VALUES(?,?,?)', (version, final, normalized))
        conn.execute('INSERT OR IGNORE INTO attempts VALUES(?,?,?,?,?,?)', (attempt, request, final, captured.isoformat(), version, json.dumps(item)))
    return version


def finding(inputs):
    if not isinstance(inputs,dict) or set(inputs)!=set(FACTORS) or not isinstance(inputs['article'],str) or len(inputs['article'])>200_000 or not isinstance(inputs['guidance'],dict) or type(inputs['evidence']) is not bool:
        raise ValueError('Expected bounded article, guidance, scoring and boolean evidence')
    if inputs['scoring'] not in ('heading/v1','heading/v2'):
        return 'unresolved'
    if not inputs['evidence']:
        return 'unmeasured'
    rule = inputs['guidance'].get('rule')
    if rule not in ('heading_present', 'question_heading'):
        return 'unresolved'
    heads = re.findall(r'^#{1,6}\s+(.+)$',inputs['article'],re.M)
    if inputs['scoring'] == 'heading/v2':
        heads = [h for h in heads if len(h.split()) >= 3]
    ok = bool(heads) if rule == 'heading_present' else any(h.endswith('?') for h in heads)
    return 'pass' if ok else 'miss'


def audit(inputs):
    return {'id':digest(inputs),'inputs':inputs,'pins':{k:digest(v) for k,v in inputs.items()},'finding':finding(inputs)}


def compare(old,new):
    changed = [k for k in FACTORS if old[k]!=new[k]]
    trials = []
    for size in range(1,len(changed)+1):
        for subset in itertools.combinations(changed,size):
            trial = dict(old)
            trial.update({k:new[k] for k in subset})
            trials.append({'changed':subset,'finding':finding(trial)})
    resolved = all(row['finding']!='unresolved' for row in trials) and finding(old)!='unresolved' and finding(new)!='unresolved'
    minimal=[]
    if resolved and finding(old)!=finding(new):
        for row in trials:
            changed_set=set(row['changed'])
            if row['finding']==finding(new) and not any(set(prev)<=changed_set for prev in minimal):
                minimal.append(row['changed'])
    return {'before':audit(old),'after':audit(new),'counterfactuals':trials,'minimal_sufficient_changes':minimal,'status':'explained' if minimal else 'unchanged' if resolved else 'unresolved'}


def visibility(record, url, country, start, end):
    missing = {'verdict': 'insufficient coverage'}
    try:
        request = record['request']
        rows = record['keywords']
        if not isinstance(rows, list) or len(rows) > 2000:
            return missing
        if (request['mode'], url_key(request['target']), request['country'], request['history_grouping']) != ('exact', url_key(url), country, 'daily'):
            return missing
        first, last, before, after = map(date.fromisoformat, (request['date_from'], request['date_to'], start, end))
        if not first <= before < after <= last:
            return missing
        counts = {}
        for row in rows:
            observed = date.fromisoformat(row['date'])
            if not first <= observed <= last or observed in counts:
                return missing
            buckets = (row['top3'], row['top4_10'])
            if any(type(count) is not int or count < 0 for count in buckets):
                return missing
            counts[observed] = sum(buckets)
        earlier, later = counts[before], counts[after]
    except (TypeError, KeyError, ValueError):
        return missing
    delta = later - earlier
    return {'before': earlier, 'after': later, 'delta': delta,
            'verdict': 'rose' if delta > 0 else 'fell' if delta < 0 else 'flat'}


def demo():
    fixture=json.loads((ROOT/'fixtures/competitor/demo.json').read_text())
    with sqlite3.connect(':memory:') as conn:
        ids=[ingest(conn,i) for i in fixture['captures']]
        bodies=[conn.execute('SELECT body FROM versions WHERE id=?',(ids[i],)).fetchone()[0] for i in (0,2)]
        counts={table:conn.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ('attempts','versions')}
    old,new=[dict(article=body,guidance=fixture['guidance'][i],scoring='heading/v1',evidence=True) for i,body in enumerate(bodies)]
    visibility_rows=[visibility(row,row['request']['target'],'us','2026-10-01','2026-10-15') for row in fixture['series']]
    return dict(label='Synthetic fixture · local comparison criterion',counts=counts,attempts=fixture['captures'],first_observed_between=[fixture['captures'][1]['captured_at'],fixture['captures'][2]['captured_at']],diff=list(difflib.unified_diff(*[b.splitlines() for b in bodies],lineterm='')),comparison=compare(old,new),series=fixture['series'],visibility=visibility_rows,interpretation='Both pages gained top-10 keywords. The edited page’s search benefit is uncertain.')


def read_json(path):
    if path.stat().st_size>2_000_000:
        raise ValueError('File exceeds 2 MB input limit')
    return json.loads(path.read_text())


def main():
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest='command',required=True)
    imp=sub.add_parser('import');imp.add_argument('input',type=Path);imp.add_argument('--db',required=True,type=Path)
    replay=sub.add_parser('replay');replay.add_argument('manifest',type=Path)
    comparison=sub.add_parser('compare');comparison.add_argument('before',type=Path);comparison.add_argument('after',type=Path)
    timeline=sub.add_parser('visibility');timeline.add_argument('export',type=Path);timeline.add_argument('--url',required=True);timeline.add_argument('--country',required=True);timeline.add_argument('--start',required=True);timeline.add_argument('--end',required=True)
    sub.add_parser('demo')
    args=parser.parse_args()
    if args.command=='import':
        with sqlite3.connect(args.db) as conn:
            result=[ingest(conn,i) for i in read_json(args.input)]
    elif args.command=='replay':
        record=read_json(args.manifest); result=audit(record['inputs'])
        if record!=result: raise ValueError('Replay manifest mismatch')
    elif args.command=='compare':
        old,new=[read_json(p) for p in (args.before,args.after)]
        for record in (old,new):
            if 'inputs' in record and audit(record['inputs'])!=record:
                raise ValueError('Replay manifest mismatch')
        result=compare(old.get('inputs',old),new.get('inputs',new))
    elif args.command=='visibility':
        result=visibility(read_json(args.export),args.url,args.country,args.start,args.end)
    else: result=demo()
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    main()
