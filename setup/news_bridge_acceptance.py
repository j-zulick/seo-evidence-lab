"""Exercise saved-news import, triage, download, mobile layout and restart locally."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]


def check(out):
    out.mkdir(parents=True, exist_ok=True)
    result = {'checks': [], 'external_requests': [], 'page_errors': [], 'human_review': 'synthetic declaration only'}
    with tempfile.TemporaryDirectory(prefix='seo-news-acceptance-') as temp:
        state = str(Path(temp)/'audit.sqlite')

        def start():
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            log = (out/'server.log').open('a')
            proc = subprocess.Popen([sys.executable, '-m', 'seo_qa', 'qa', '--state', state, '--port', str(port)],
                cwd=ROOT, env=os.environ | {'SEO_SUPABASE_ADAPTER_ENABLED': 'false', 'SEO_KNOWLEDGE_ENABLED': 'false'}, stdout=log, stderr=log)
            base = f'http://127.0.0.1:{port}'
            for _ in range(100):
                if proc.poll() is not None:
                    raise RuntimeError('Local app failed to start')
                try:
                    with urlopen(base+'/qa/news-imports', timeout=.3):
                        return proc, base, log
                except OSError:
                    time.sleep(.1)
            proc.terminate()
            raise RuntimeError('Local app readiness timeout')

        proc, base, log = start()
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(channel='chrome')
                context = browser.new_context(viewport={'width':1440, 'height':1000}, accept_downloads=True)
                def guard(route):
                    if route.request.url.startswith(base+'/'):
                        route.continue_()
                    else:
                        result['external_requests'].append(route.request.url)
                        route.abort()
                context.route('**/*', guard)
                page = context.new_page()
                page.on('pageerror', lambda error: result['page_errors'].append(str(error)))
                page.goto(base+'/qa/news-imports')
                expect(page.get_by_role('heading', name='No saved observations yet')).to_be_visible()
                page.get_by_label('Declared origin').select_option('true')
                page.get_by_label('Paste export JSON (maximum 2 MB)').fill((ROOT/'fixtures/news-bridge/saved-email.json').read_text())
                page.get_by_role('button', name='Import into local inbox').click()
                assert len(context.request.get(base+'/qa/news-imports/report.json').json()['items']) == 2
                result['checks'].append('Actual browser form imports saved synthetic export')
                card = page.locator('article').first
                card.locator('summary').click()
                card.get_by_label('Declared reviewer').fill('Synthetic browser actor')
                card.get_by_label('Action', exact=True).select_option('defer')
                card.get_by_label('Evidence note').fill('Synthetic test declaration; no human acceptance.')
                card.get_by_role('button', name='Record local triage').click()
                with page.expect_download() as pending:
                    page.get_by_role('link', name='Download saved observations JSON').click()
                pending.value.save_as(out/'downloaded-synthetic-news.json')
                data = json.loads((out/'downloaded-synthetic-news.json').read_text())
                assert data['items'][0]['decision']['action'] == 'defer'
                assert all(row['guidance_approved'] is False for row in data['items'])
                result['checks'].append('Version-bound synthetic triage and real JSON download')
                for width in (1440, 390, 320):
                    page.set_viewport_size({'width':width, 'height':1000})
                    page.goto(base+'/qa/news-imports')
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'), width
                    page.keyboard.press('Tab')
                    assert page.evaluate('document.activeElement.tagName') != 'BODY'
                    page.locator('body').click(position={'x':1,'y':1})
                    page.evaluate('scrollTo(0,0)')
                    page.screenshot(path=str(out/f'news-{width}.png'), full_page=True)
                    result['checks'].append(f'{width}px layout fits and keyboard focus available')
                proc.terminate(); proc.wait(timeout=10); log.close()
                proc, base, log = start()
                page.goto(base+'/qa/news-imports')
                reopened = context.request.get(base+'/qa/news-imports/report.json').json()
                assert reopened == data
                result['checks'].append('Actual process stop/restart preserves inbox and triage')
                context.close(); browser.close()
            assert not result['external_requests'] and not result['page_errors']
            result['status'] = 'PASS'
        except Exception as exc:
            result['status'] = 'FAIL'; result['error'] = repr(exc)
            raise
        finally:
            if proc.poll() is None:
                proc.terminate(); proc.wait(timeout=10)
            log.close()
            (out/'RESULTS.json').write_text(json.dumps(result, indent=2)+'\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    check(parser.parse_args().output)
