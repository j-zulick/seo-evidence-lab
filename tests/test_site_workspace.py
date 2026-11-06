import json,re
from pathlib import Path
from fastapi.testclient import TestClient
from seo_qa.store import Store
from seo_qa.web import create_app
from seo_qa.scope_criteria import configuration,observe
from seo_qa.simple_qa import SAMPLE

def setup(tmp_path):
 s=Store(tmp_path/'state.sqlite');return s,TestClient(create_app(s))
def token(c):return re.search(r'name="csrf_token" value="([^"]+)',c.get('/qa/sites').text)[1]
def fields(c,**extra):
 return dict(url='https://example.test/',acknowledge='true',csrf_token=token(c),**extra)
def test_generic_sites_owner_and_validation(tmp_path):
 s,c=setup(tmp_path)
 for url in ('https://garden.example/products/','https://different.example/notes'):
  assert c.post('/qa/sites',data={'url':url,'name':'Generic site','csrf_token':token(c),'acknowledge':'true'}).status_code==200
 for url in ('javascript:alert(1)','https://user:pass@example.test/','https://example.test/#x'):
  assert c.post('/qa/sites',data={'url':url,'csrf_token':token(c),'acknowledge':'true'}).status_code==400
 assert c.post('/qa/sites',data={'url':'https://example.test','csrf_token':'wrong'}).status_code==403
 assert c.post('/qa/sites',data={'url':'https://example.test','csrf_token':token(c)}).status_code==400
 with s.connection() as db: rid=db.execute('SELECT id FROM site_workspace_records LIMIT 1').fetchone()['id']
 assert c.get('/qa/sites/'+rid+'/report.json').headers['cache-control']=='no-store'
 other=TestClient(create_app(Store(s.path,owner_id='other')))
 assert other.get('/qa/sites/'+rid).status_code==404
def test_crawl_upload_and_comparison(tmp_path):
 _,c=setup(tmp_path)
 meta=dict(schema='crawl-upload/v1',captured_at='2026-11-04T00:00:00Z',tool_version='synthetic',configuration_hash='same',
           scope=['https://example.test/'],rendering_mode='raw',complete=True,exclusions=[],source_type='synthetic',source_ref='synthetic-fixture')
 body=dict(schema='crawl-upload/v1',metadata=meta,pages=[{'url':'https://example.test/','status':200}])
 r=c.post('/qa/sites',data=fields(c,kind='crawl'),files={'payload':('crawl.json',json.dumps(body),'application/json')})
 assert r.status_code==200 and 'crawl-observations/v1' in r.text
 assert c.post('/qa/sites',data=fields(c,kind='crawl',content='{bad')).status_code==400
def test_architecture_performance_and_modes(tmp_path):
 s,c=setup(tmp_path)
 base=Path(__file__).resolve().parents[1]
 a=json.loads((base/'fixtures/site-workspace/site-a.json').read_text());b=json.loads((base/'fixtures/site-workspace/site-b.json').read_text())
 r=c.post('/qa/sites',data={'url':a['root'],'comparison_url':b['root'],'kind':'architecture','csrf_token':token(c),'acknowledge':'true'},files={'payload':('a.json',json.dumps(a)),'comparison':('b.json',json.dumps(b))})
 assert r.status_code==200 and 'x07-comparison/v1' in r.text
 perf=(base/'fixtures/site-workspace/performance.json').read_text()
 r=c.post('/qa/sites',data={'url':'https://fixture.invalid/','kind':'performance','content':perf,'csrf_token':token(c),'acknowledge':'true'})
 assert r.status_code==200 and 'not_assessed' in r.text and 'missing' in r.text
 q=s.create_requirement('Modes',['https://supplied.invalid/'],{'seo_qa':configuration(['content'],'supplied',SAMPLE)})
 run=s.create_run(q['id']);s.execute_run(run['id'],observe);before=s.export_report(run['id'])
 for role in ('owner','editor','developer'):
  for level in ('beginner','practitioner','engineer'):
   r=c.get('/qa/audits/'+run['id']+'/explanations',params={'role':role,'level':level})
   assert r.status_code==200 and 'Presentation only' in r.text
 assert s.export_report(run['id'])==before

def test_explanation_json_preserves_full_report_and_coverage(tmp_path):
 s,c=setup(tmp_path)
 q=s.create_requirement('Explain',['https://supplied.invalid/'],{'seo_qa':configuration(['content'],'supplied','Short plain content','text')})
 run=s.create_run(q['id']);s.execute_run(run['id'],observe)
 canonical=s.export_report(run['id'])
 response=c.get('/qa/audits/'+run['id']+'/explanations',params={'format':'json','level':'engineer'})
 assert response.status_code==200 and response.headers['cache-control']=='no-store'
 assert response.json()['canonical']==canonical
 assert response.json()['coverage'].startswith('missed criteria only')
 assert s.export_report(run['id'])==canonical

def test_published_upload_consumer_paths(tmp_path):
 _,c=setup(tmp_path)
 base=Path(__file__).resolve().parents[1]
 document=json.loads((base/'fixtures/site-workspace/garden-upload.json').read_text())
 root=document['metadata']['scope'][0]
 for kind in ('architecture_upload','performance_upload'):
  response=c.post('/qa/sites',data={'url':root,'kind':kind,'acknowledge':'true','csrf_token':token(c)},files={'payload':('garden.json',json.dumps(document),'application/json')})
  assert response.status_code==200,response.text[:400]
  assert 'no website fetched' in response.text
