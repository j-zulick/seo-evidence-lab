"""Capture and verify the local iteration-one journey in disposable state."""
import argparse, json, os, socket, subprocess, sys, tempfile, time
from pathlib import Path
from urllib.request import urlopen
from playwright.sync_api import sync_playwright, expect
ROOT=Path(__file__).resolve().parents[1]


def capture(out, record=False):
    out.mkdir(parents=True,exist_ok=True)
    receipt={'checks':[],'page_errors':[],'external_requests':[],'human_reviews':'pending'}
    with tempfile.TemporaryDirectory(prefix='seo-ui-') as temp:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        with (out/'server.log').open('w') as log:
            proc=subprocess.Popen([sys.executable,'-m','seo_qa','qa','--state',str(Path(temp)/'ui.sqlite'),'--port',str(port)],
                                  cwd=ROOT,env=os.environ|{'SEO_SUPABASE_ADAPTER_ENABLED':'false','SEO_KNOWLEDGE_ENABLED':'false'},stdout=log,stderr=log)
            try:
                for _ in range(100):
                    if proc.poll() is not None:raise RuntimeError('Server failed')
                    try:
                        with urlopen(base+'/qa/lab',timeout=.3):break
                    except OSError:time.sleep(.1)
                else:raise RuntimeError('Readiness timeout')
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(channel='chrome')
                    ctx=browser.new_context(viewport={'width':1440,'height':950},record_video_dir=out if record else None)
                    def guard(route):
                        if route.request.url.startswith(base+'/'):route.continue_()
                        else:
                            receipt['external_requests'].append(route.request.url);route.abort()
                    ctx.route('**/*',guard)
                    page=ctx.new_page()
                    page.on('pageerror',lambda e:receipt['page_errors'].append(str(e)))
                    ctx.tracing.start(screenshots=True,snapshots=True)
                    routes=[('overview','/qa/lab','See what changed.'),('timeline','/qa/competitors','Why did this finding change?'),
                            ('evaluation','/qa/evaluations','Show the misses, too.'),('news','/qa/community-news','Start with a lead.'),
                            ('inbox','/qa/source-inbox','A source changed.'),('search','/qa/evidence-search?q=noindex','Find the passage.')]
                    for viewport,width in [('desktop',1440),('mobile',390)]:
                        page.set_viewport_size({'width':width,'height':950})
                        for name,path,title in routes:
                            page.goto(base+path)
                            expect(page.get_by_role('heading',level=1)).to_contain_text(title)
                            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'),(viewport,path)
                            page.keyboard.press('Tab')
                            assert page.evaluate('document.activeElement.tagName')!='BODY'
                            page.locator('body').click(position={'x':1,'y':1})
                            page.screenshot(path=out/f'{name}-{viewport}.png',full_page=True)
                            if name=='evaluation' and viewport=='mobile':
                                region=page.get_by_role('region',name='Evaluation results');region.focus()
                                assert region.evaluate('(el)=>el.scrollWidth>el.clientWidth')
                                region.press('ArrowRight');page.wait_for_timeout(300)
                                assert region.evaluate('(el)=>el.scrollLeft>0')
                            receipt['checks'].append(viewport+' '+name)
                    page.goto(base+'/qa/lab')
                    page.get_by_role('link',name='Explore the example').click()
                    expect(page).to_have_url(base+'/qa/competitors')
                    page.get_by_role('link',name='Original manifest').click()
                    assert json.loads(page.inner_text('body'))['finding']=='miss'
                    report=ctx.request.get(base+'/qa/evaluations/report.json').json()
                    assert (report['baseline_passed'],report['revised_passed'],report['retrieval_passed'])==(4,6,5)
                    page.goto(base+'/qa/evaluations')
                    with page.expect_download() as download:
                        page.get_by_role('link',name='Download a fresh run',exact=True).click()
                    saved=out/'evaluation-downloaded.json'
                    download.value.save_as(saved)
                    assert json.loads(saved.read_text())['experiment']['result_hash']==report['experiment']['result_hash']
                    page.goto(base+'/qa/walkthrough')
                    expect(page.get_by_text('Human review is pending',exact=True)).to_be_visible()
                    assert ctx.request.get(base+'/qa/walkthrough/library.json').json()['pages']
                    page.goto(base+'/qa/source-inbox')
                    expect(page.get_by_text('Pending review.',exact=True)).to_be_visible()
                    if record:
                        page.set_viewport_size({'width':1440,'height':950})
                        for name,path,title in routes[:3]:
                            page.goto(base+path);page.wait_for_timeout(1200)
                            page.evaluate('scrollTo(0,document.body.scrollHeight/2)');page.wait_for_timeout(1200)
                    ctx.tracing.stop(path=out/'walkthrough-trace.zip');ctx.close()
                    if record:page.video.path().replace(out/'walkthrough.webm')
                    browser.close()
                assert not receipt['page_errors']
                assert not receipt['external_requests']
                receipt['status']='PASS'
            except Exception as exc:
                receipt['status']='FAIL';receipt['error']=repr(exc);raise
            finally:
                proc.terminate();proc.wait(timeout=10)
                (out/'RESULTS.json').write_text(json.dumps(receipt,indent=2)+'\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--record',action='store_true')
    args=parser.parse_args();capture(args.output,args.record)
