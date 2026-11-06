"""Repeatable local-only browser/restart acceptance; synthetic evidence, never human approval.
Run: python setup/local_acceptance.py --output /absolute/evidence/folder
Requires pinned application runtime, Playwright and installed Chrome. No website fetches.
"""
import argparse, csv, hashlib, io, json, os, re, socket, subprocess, sys, tempfile, time
from pathlib import Path
from urllib.request import urlopen
from playwright.sync_api import sync_playwright, expect
ROOT=Path(__file__).resolve().parents[1]
HTML='<h1>Synthetic fixture</h1><h2>What should I do?</h2><p>Follow the documented steps.</p>'
def main():
 a=argparse.ArgumentParser();a.add_argument('--output',required=True);args=a.parse_args();out=Path(args.output).resolve();out.mkdir(parents=True,exist_ok=True)
 receipt={'kind':'local synthetic acceptance','checks':[],'human_acceptance':False,'external_requests':[]}
 def passed(name):receipt['checks'].append(name)
 with tempfile.TemporaryDirectory(prefix='seo-local-qa-') as td:
  with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
  base=f'http://127.0.0.1:{port}';state=Path(td)/'state.sqlite';proc=None;log=(out/'server.log').open('w')
  def start():
   nonlocal proc
   env=dict(os.environ,SEO_SUPABASE_ADAPTER_ENABLED='false',SEO_KNOWLEDGE_ENABLED='false')
   proc=subprocess.Popen([sys.executable,'-m','seo_qa','qa','--state',str(state),'--port',str(port)],cwd=ROOT,env=env,stdout=log,stderr=log)
   for _ in range(100):
    if proc.poll() is not None:raise RuntimeError('Server failed; inspect server.log')
    try:
     with urlopen(base+'/qa',timeout=.3) as r:
      if r.status==200:return
    except Exception:time.sleep(.1)
   raise RuntimeError('Server not ready')
  def stop():
   if proc and proc.poll() is None:proc.terminate();proc.wait(timeout=10)
  try:
   start()
   with sync_playwright() as pw:
    browser=pw.chromium.launch(headless=True,channel='chrome');context=browser.new_context(viewport={'width':1440,'height':1000},accept_downloads=True)
    def route(r):
     if r.request.url.startswith(base):r.continue_()
     else:receipt['external_requests'].append(r.request.url);r.abort()
    context.route('**/*',route);page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    def token():return page.locator('input[name=csrf_token]').first.input_value()
    def post(path,data):
     r=context.request.post(base+path,form=dict(csrf_token=token(),**data));assert r.status==200,(r.status,r.text());return r.url.removeprefix(base)
    page.goto(base+'/qa');page.get_by_role('radio',name='Paste content',exact=True).check();page.get_by_label('Audit name').fill('Synthetic incomplete audit');page.get_by_label('Content format').select_option('html');page.get_by_label('Your content',exact=True).fill(HTML);page.get_by_role('button',name='Run audit',exact=True).click();expect(page.get_by_role('heading',name='Synthetic incomplete audit',exact=True)).to_be_visible()
    audit=page.url.removeprefix(base);run_id=audit.rsplit('/',1)[1];original=context.request.get(base+'/runs/'+run_id+'/report.json').body();passed('Browser pasted audit saved')
    page.get_by_role('button',name='Assess saved content',exact=True).click();expect(page.locator('iframe')).to_be_visible();first=page.url.removeprefix(base);passed('Saved audit reuse in browser')
    blobs={}
    def exports(path):
     result={}
     for fmt in ['json','csv','md','html']:
      r=context.request.get(base+path+'/report.'+fmt);assert r.status==200;assert r.headers['cache-control']=='no-store';assert 'attachment' in r.headers['content-disposition'];result[fmt]=r.body()
     report=json.loads(result['json']);rows=list(csv.DictReader(io.StringIO(result['csv'].decode())));rebuild=json.loads(rows[0]['payload_json']);rebuild['pages']=[json.loads(x['payload_json']) for x in rows[1:]];assert rebuild==report;assert json.dumps(report,indent=2,ensure_ascii=False) in result['md'].decode()
     return result
    blobs[first]=exports(first);passed('Four exports with JSON/CSV/Markdown record correspondence')
    page.locator('#editorial-form summary').click();page.locator('#reviewer-0').fill('Synthetic fixture reviewer A');page.locator('#reviewer-1').fill('Synthetic fixture reviewer B')
    for sel in page.locator('select[id^=vote-]').all():sel.select_option('yes')
    page.get_by_role('button',name='Save editorial review',exact=True).click();manual=page.url.removeprefix(base);assert manual!=first;passed('Synthetic manual review creates new immutable record')
    template=context.request.get(base+manual+'/reviews.json').json()
    for article in template.values():
     for i,item in enumerate(article['passes']):
      item.update(evaluator_id=f'synthetic-review-{i}',evaluator_version='fixture/1',pass_id=f'fixture-{i}')
      for row in item['sections'].values():row.update(answer_first=True,standalone_openings=True)
    page.locator('#review-import summary').click();page.locator('#reviews').fill(json.dumps(template));page.get_by_role('button',name='Import review evidence',exact=True).click();reviewed=page.url.removeprefix(base);assert reviewed!=manual;passed('Browser review JSON import')
    page.get_by_role('link',name='Revise content',exact=True).click()
    if page.locator('#mode').input_value()=='batch':
     manifest=json.loads(page.locator('#manifest').input_value());manifest['pages'][0]['content']=HTML.replace('Follow','Carefully follow');page.locator('#manifest').fill(json.dumps(manifest))
    else:page.locator('#content').fill(HTML.replace('Follow','Carefully follow'))
    page.get_by_role('button',name='Assess bot-worthiness',exact=True).click();revision=page.url.removeprefix(base);page.locator('#before').select_option(reviewed.rsplit('/',1)[1]);page.get_by_role('button',name='Compare assessments',exact=True).click();expect(page.get_by_role('heading',name='Bot-worthiness comparison',exact=True)).to_be_visible();assert 'Not comparable' in page.inner_text('main');passed('Browser revision/comparison preserves uncertainty')
    for name,width in [('desktop',1440),('mobile',390)]:
     page.set_viewport_size({'width':width,'height':1000});page.goto(base+reviewed);frame=page.frame_locator('iframe');expect(frame.get_by_role('heading',name='Library comparison',exact=True)).to_be_visible();assert page.evaluate('document.documentElement.scrollWidth <= innerWidth');page.screenshot(path=str(out/(name+'.png')),full_page=True)
     page.keyboard.press('Tab');assert page.evaluate('document.activeElement.tagName')!='BODY'
     # Inner table is allowed to scroll; ensure containing region accepts keyboard focus.
     regions=frame.locator('[tabindex="0"]');receipt[name+'_focusable_inner_regions']=regions.count()
     region=regions.first;region.focus();assert region.evaluate('(e)=>e===document.activeElement')
     if name=='mobile':
      assert region.evaluate('(e)=>e.scrollWidth>e.clientWidth');region.press('ArrowRight');page.wait_for_timeout(200);assert region.evaluate('(e)=>e.scrollLeft>0');passed('Mobile clipped table is keyboard-scrollable')
     for label in ['JSON','CSV','Markdown','HTML']:
      with page.expect_download() as download:page.get_by_role('link',name='Download '+label,exact=True).click()
      assert download.value.failure() is None
     passed(name+' report visible, page fits viewport, keyboard focus and four browser downloads')
    for level in ['beginner','engineer']:
     path=f'/qa/audits/{run_id}/explanations?level={level}&role=developer';page.goto(base+path);expect(page.get_by_role('heading',name='Finding explanations',exact=True)).to_be_visible();data=context.request.get(base+path+'&format=json').json();assert data['explanations'],'Fixture must include missed/incomplete findings';(out/(level+'-explanations.json')).write_text(json.dumps(data,indent=2));page.screenshot(path=str(out/(level+'-explanations.png')),full_page=True)
    passed('Beginner/engineer presentation on actual missed/incomplete findings')
    page.goto(base+'/qa/sites');expect(page.get_by_role('heading',name='Uploaded site workspace',exact=True)).to_be_visible();passed('Uploaded-site route preserved')
    assert context.request.get(base+'/runs/'+run_id+'/report.json').body()==original
    for path in [first,manual,reviewed,revision]:blobs[path]=exports(path)
    stop();start()
    for path,expected in blobs.items():assert exports(path)==expected
    assert context.request.get(base+'/runs/'+run_id+'/report.json').body()==original;passed('Actual process stop/restart preserves audit and all four exports of each assessment revision')
    assert not errors,errors;assert not receipt['external_requests'];receipt['page_errors']=errors;receipt['export_sha256']={path:{k:hashlib.sha256(v).hexdigest() for k,v in vals.items()} for path,vals in blobs.items()};browser.close()
   receipt['status']='PASS'
  except Exception as e:receipt['status']='FAIL';receipt['error']=repr(e);raise
  finally:stop();log.close();(out/'RESULTS.json').write_text(json.dumps(receipt,indent=2))
if __name__=='__main__':main()
