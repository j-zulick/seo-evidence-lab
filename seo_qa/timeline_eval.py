"""Run a versioned synthetic evaluation locally and export Langfuse dataset items."""
import argparse
import json
import sqlite3
from pathlib import Path
from .competitor import ROOT, demo, digest, ingest, visibility

DATA = ROOT/'fixtures/eval'


def run(output):
    output.mkdir(parents=True,exist_ok=True)
    dataset = json.loads((DATA/'cases.json').read_text())
    shadow = json.loads((DATA/'shadow.json').read_text())
    results = []
    requests = []
    fixture = demo()
    for case in dataset['cases']:
        cid, kind, state = case['id'], case['type'], case['input']
        if kind in ('claim','opening'):
            prompt = ('Classify the opening: direct, vague, or insufficient evidence.' if kind=='opening'
                      else 'Classify causal support: supported, unsupported, or insufficient evidence.')
            requests.append({'case_id':cid,'arguments':{'data_class':'synthetic','state':state,
                             'questions':{'judgment':{'type':'choice','instructions':prompt}}}})
            outputs = [shadow[run_id][cid] for run_id in ('baseline','revised')]
            grader = 'simulated Jev response; no model invoked'
        else:
            if kind=='versions':
                capture = fixture['attempts'][0]
                with sqlite3.connect(':memory:') as conn:
                    for body in state['bodies']:
                        ingest(conn,capture | {'response':{'success':True,'data':{'markdown':body}}})
                    value=conn.execute('SELECT count(*) FROM versions').fetchone()[0]
            elif kind=='history':
                row=fixture['series'][0] | state
                value=visibility(row,row['request']['target'],'us','2026-10-01','2026-10-15')['verdict']
            elif kind=='guidance':
                value='review' if len(set(state['claims']))>1 else 'consistent'
            else:
                raise ValueError('Unknown evaluator')
            outputs=[value,value]
            grader='deterministic'
        results.append({'id':cid,'grader':grader,'expected':case['expected'],
                        'baseline':outputs[0],'revised':outputs[1],
                        'baseline_pass':outputs[0]==case['expected'],'revised_pass':outputs[1]==case['expected']})
    items=[{'input':c['input'],'expectedOutput':c['expected'],'metadata':{'case_id':c['id'],'dataset_version':dataset['version'],'synthetic':True}} for c in dataset['cases']]
    report={'dataset_hash':digest(dataset),'dataset_version':dataset['version'],'rubric':'v1','cases':results,
            'human_labels':None,'live_calls':0,'units':0,'mode':'offline synthetic judge fixtures'}
    for name,obj in [('jev-requests',requests),('langfuse-dataset-items',items),('experiment-comparison',report)]:
        (output/(name+'.json')).write_text(json.dumps(obj,indent=2)+'\n')
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',required=True,type=Path)
    run(p.parse_args().output)
