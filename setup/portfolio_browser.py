"""Capture the local walkthrough and an intentionally failed browser assertion."""
import argparse, os, shutil, socket, subprocess, sys, tempfile, time
from pathlib import Path
from urllib.request import urlopen
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[1]


def capture(out, record=False):
    out.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0))
            port=sock.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        proc=subprocess.Popen([sys.executable,'-m','seo_qa','qa','--state',str(Path(temp)/'audit.sqlite'),'--port',str(port)],
                              cwd=ROOT,env=os.environ|{'SEO_SUPABASE_ADAPTER_ENABLED':'false','SEO_KNOWLEDGE_ENABLED':'false'})
        try:
            for _ in range(80):
                try:
                    with urlopen(base+'/qa',timeout=1): break
                except OSError: time.sleep(.1)
            else: raise RuntimeError('Local server failed to start')
            with sync_playwright() as pw:
                browser=pw.chromium.launch(channel='chrome')
                ctx=browser.new_context(viewport={'width':1100,'height':850},record_video_dir=out if record else None)
                ctx.tracing.start(screenshots=True,snapshots=True)
                page=ctx.new_page()
                page.goto(base+'/qa/competitors')
                expect(page.get_by_text('Owned-site measurements')).to_be_visible()
                page.screenshot(path=out/'timeline.png',full_page=True)
                if record:
                    page.wait_for_timeout(1200)
                    page.get_by_text('Observed search visibility').scroll_into_view_if_needed()
                    page.wait_for_timeout(1200)
                try:
                    expect(page.get_by_text('The edit definitely caused the ranking gain.',exact=True)).to_be_visible(timeout=250)
                except AssertionError:
                    ctx.tracing.stop(path=out/'injected-failure.zip')
                else: raise AssertionError('Unexpected causal claim displayed')
                page.get_by_role('link',name='Review the captured edit').click()
                expect(page.get_by_text('Pending review.')).to_be_visible()
                page.screenshot(path=out/'inbox.png')
                if record: page.wait_for_timeout(1200)
                ctx.close()
                if record: page.video.path().replace(out/'walkthrough.webm')
                browser.close()
        finally:
            proc.terminate()
            proc.wait(timeout=10)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--record',action='store_true')
    args=parser.parse_args()
    capture(args.output,args.record)
