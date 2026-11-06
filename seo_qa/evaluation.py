"""Deterministic Pydantic Evals cases; no hosted judge/model calls."""
from dataclasses import dataclass
import json
from pathlib import Path
from pydantic_evals import Case,Dataset
from pydantic_evals.evaluators import Evaluator,EvaluatorContext
from .inspector import inspect_html

@dataclass
class ExactVerdict(Evaluator):
    def evaluate(self,ctx:EvaluatorContext)->bool:
        return ctx.output==ctx.expected_output

CASES=[
    ('correct_canonical',{'html':'<link rel="canonical" href="/a">','expected':{'canonical':'https://fixture.example/a'}},'pass'),
    ('wrong_canonical',{'html':'<link rel="canonical" href="/b">','expected':{'canonical':'https://fixture.example/a'}},'fail'),
    ('duplicate_canonical',{'html':'<link rel="canonical" href="/a"><link rel="canonical" href="/a">','expected':{'canonical':'https://fixture.example/a'}},'fail'),
    ('noindex',{'html':'<meta name="robots" content="noindex">','expected':{'indexable':True}},'fail'),
    ('unknown_robots',{'html':'<p>dummy</p>','expected':{'crawlable':True}},'inconclusive'),
    ('denied',{'html':'<p>denied</p>','status':403,'expected':{'indexable':True}},'blocked'),
    ('text_removed',{'html':'<p>new</p>','expected':{'required_text':['compatibility']}},'fail'),
]

def task(value):
    observation=inspect_html(url='https://fixture.example/a',**value)
    return next(x['outcome'] for x in observation['findings'] if x['outcome']!='not_applicable')

def run(destination='artifacts/evaluation.json'):
    dataset=Dataset(name='seo-deterministic-regression-v1',cases=[Case(name=n,inputs=i,expected_output=e) for n,i,e in CASES],evaluators=[ExactVerdict()])
    report=dataset.evaluate_sync(task,progress=False,max_concurrency=1)
    results=[{'name':case.name,'expected':case.expected_output,'observed':case.output,'passed':case.output==case.expected_output} for case in report.cases]
    receipt={'framework':'Pydantic Evals','kind':'deterministic fixture regression; no unseen accuracy claim','model_calls':0,'cases':results,'passed':sum(x['passed'] for x in results),'total':len(CASES),'errors':len(report.failures)}
    path=Path(destination);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(receipt,indent=2))
    if receipt['passed']!=len(CASES) or receipt['errors']:
        raise ValueError('Evaluation failed')
    return receipt

if __name__=='__main__':print(json.dumps(run(),indent=2))
