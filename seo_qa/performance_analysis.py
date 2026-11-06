"""Offline uploaded observation analyzer. No collection or network access."""
import json, math, sys
METRICS = {'lcp_ms': (2500,4000), 'inp_ms': (200,500), 'cls': (.1,.25)}
FIELDS = ('content','links','canonical','robots','structured_data')
def analyze(document):
    if document.get('schema') != 'x08-observations/v1':
        raise ValueError('Unsupported observation contract')
    results=[]
    for row in document.get('observations',[]):
        for key in ('url','source','captured_at','device','kind'):
            if not isinstance(row.get(key),str) or not row[key]:
                raise ValueError('Missing provenance: '+key)
        if row['kind'] not in ('field','lab'): raise ValueError('Invalid measurement kind')
        metrics={}
        for name,(good,poor) in METRICS.items():
            value=row.get('metrics',{}).get(name)
            if value is None: verdict='missing'
            elif isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0:
                raise ValueError('Invalid metric: '+name)
            else: verdict='good' if value<=good else 'needs_improvement' if value<=poor else 'poor'
            metrics[name]={'value':value,'verdict':verdict}
        raw,rendered=row.get('raw'),row.get('rendered')
        paired=isinstance(raw,dict) and isinstance(rendered,dict)
        comparison={name: ('missing' if not paired or name not in raw or name not in rendered else 'same' if raw[name]==rendered[name] else 'changed') for name in FIELDS}
        results.append({'provenance':{k:row[k] for k in ('url','source','captured_at','device','kind')},'synthetic':row.get('synthetic',False),'metrics':metrics,'rendering':comparison,'paired':paired,'field_cwv_verdict':'not_assessed: requires representative p75 field window and coverage'})
    return {'schema':'x08-analysis/v1','results':results,'live_collection':'unsupported','limitations':['Uploaded observations only; no current-site or indexing guarantee.','Metric bands are supplied policy, not verified current external guidance.','Raw/rendered fields must come from the same uploaded observation; missing fields are not differences.']}
if __name__=='__main__':
    print(json.dumps(analyze(json.load(open(sys.argv[1]))),indent=2,allow_nan=False))
