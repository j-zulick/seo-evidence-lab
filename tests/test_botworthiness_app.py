"""Main local interface + HTTP/storage boundaries; synthetic inputs only."""
import copy
import csv
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from fastapi.testclient import TestClient
from seo_qa.store import Store
from seo_qa.web import create_app
from seo_qa.botworthiness.local_web import LocalRecords, PREFIX
from seo_qa.fixtures import fixture_inspector, FIXTURE_URL

HTML = '<h1>Guide</h1><p id="summary">Use the documented check interval.</p><h2>What should I do?</h2><p>Use the interval in the manual.</p>'


class LocalAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'local.sqlite'
        self.store = Store(self.path)
        self.inspector = Mock(side_effect=fixture_inspector)
        self.app = create_app(self.store, inspector_module=self.inspector)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.csrf = re.search(r'name="csrf_token" value="([^"]+)"', self.client.get(PREFIX).text)[1]

    def post(self, path, **data):
        return self.client.post(path, data=dict(csrf_token=self.csrf, **data), follow_redirects=False)

    def create(self, **changes):
        data = dict(title='Article', page_id='article', content=HTML, content_format='html')
        data.update(changes)
        response = self.post(PREFIX+'/assess', **data)
        self.assertEqual(response.status_code, 303, response.text)
        return response.headers['location']

    def report(self, location):
        response = self.client.get(location+'/report.json')
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_main_navigation_and_intake_without_new_audit(self):
        for path in ['/', '/qa', PREFIX]:
            self.assertIn('href="/qa/botworthiness"', self.client.get(path).text)
        location = self.create()
        detail = self.client.get(location)
        self.assertEqual(detail.status_code, 200)
        self.assertIn('Bot-worthiness findings and library heatmap', detail.text)
        self.assertEqual(self.store.list_runs(), [])
        report = self.report(location)
        self.assertEqual(report['pages'][0]['source']['evidence_kind'], 'supplied')
        self.assertIsNone(report['pages'][0]['score'])

    def test_export_formats_match_one_record(self):
        location = self.create()
        report = self.report(location)
        rows = list(csv.DictReader(io.StringIO(self.client.get(location+'/report.csv').text)))
        reconstructed = json.loads(rows[0]['payload_json'])
        reconstructed['pages'] = [json.loads(r['payload_json']) for r in rows[1:]]
        self.assertEqual(reconstructed, report)
        self.assertIn(json.dumps(report, indent=2, ensure_ascii=False), self.client.get(location+'/report.md').text)
        for fmt in ['html', 'json', 'csv', 'md']:
            response = self.client.get(location+'/report.'+fmt)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertIn('attachment;', response.headers['content-disposition'])
        self.assertEqual(self.client.get(location+'/report.xml').status_code, 404)

    def test_manual_review_keeps_prior_record_and_disagreements(self):
        location = self.create()
        original = self.report(location)
        result = self.post(location+'/review', review_mode='manual', reviewer_0='Fixture A', reviewer_1='Fixture B',
            rubric_0='fixture/1', rubric_1='fixture/1', vote_0_0_0_answer_first='yes', vote_1_0_0_answer_first='no',
            vote_0_0_0_standalone_openings='yes', vote_1_0_0_standalone_openings='yes')
        self.assertEqual(result.status_code, 303, result.text)
        newer = self.report(result.headers['location'])
        self.assertEqual(newer['pages'][0]['semantic_decisions']['answer_first'][0]['state'], 'disagreement')
        rows = {r['id']: r for r in newer['pages'][0]['criteria']}
        self.assertEqual(rows['standalone_openings']['points'], 14)
        self.assertIsNone(rows['answer_first']['points'])
        self.assertEqual(self.report(location), original)
        self.assertEqual(len(LocalRecords(self.store).history()[0]), 2)

    def test_same_reviewer_refused(self):
        location = self.create()
        response = self.post(location+'/review', reviewer_0='same', reviewer_1='same', rubric_0='1', rubric_1='1')
        self.assertEqual(response.status_code, 400)
        self.assertIn('distinct', response.text)
        self.assertEqual(len(LocalRecords(self.store).history()[0]), 1)

    def test_review_template_import_and_source_mismatch(self):
        location = self.create()
        template = self.client.get(location+'/reviews.json').json()
        for i, item in enumerate(template['article']['passes']):
            item.update(evaluator_id=f'fixture-{i}', evaluator_version='fixture/1', pass_id=f'pass-{i}')
            for row in item['sections'].values():
                row.update(answer_first=True, standalone_openings=True)
        response = self.post(location+'/review', review_mode='json', reviews=json.dumps(template))
        self.assertEqual(response.status_code, 303, response.text)
        report = self.report(response.headers['location'])
        self.assertEqual(report['pages'][0]['criteria'][0]['points'], 24)
        template['article']['content_sha256'] = 'a'*64
        response = self.post(location+'/review', review_mode='json', reviews=json.dumps(template))
        self.assertEqual(response.status_code, 400)
        self.assertIn('mismatch', response.text)

    def test_distinct_article_reviewers_are_not_silently_merged(self):
        manifest={'pages':[{'id':str(i),'content':HTML} for i in range(2)]}
        response=self.post(PREFIX+'/assess',mode='batch',manifest=json.dumps(manifest))
        location=response.headers['location']
        reviews=self.client.get(location+'/reviews.json').json()
        for key,review in reviews.items():
            for i,item in enumerate(review['passes']):
                item.update(evaluator_id=f'fixture-{key}-{i}',evaluator_version='fixture/1',pass_id=f'pass-{i}')
        result=self.post(location+'/review',review_mode='json',reviews=json.dumps(reviews))
        self.assertEqual(result.status_code,303,result.text)
        reviewed=result.headers['location']
        self.assertNotIn('id="editorial-form"',self.client.get(reviewed).text)
        self.assertEqual(self.post(reviewed+'/review',review_mode='manual').status_code,400)

    def test_persistence_across_app_restart_and_database_separation(self):
        location = self.create()
        with TestClient(create_app(Store(self.path))) as client:
            self.assertEqual(client.get(location+'/report.json').json(), self.report(location))
        with TestClient(create_app(Store(Path(self.temp.name)/'different.sqlite'))) as client:
            self.assertEqual(client.get(location).status_code, 404)

    def test_corrupt_sidecar_blocks_read_review_and_exports(self):
        location = self.create()
        path = LocalRecords(self.store).root/(location.rsplit('/', 1)[1]+'.json')
        value = json.loads(path.read_text())
        value['report']['pages'][0]['score'] = 100
        path.write_text(json.dumps(value))
        for suffix in ['', '/visual', '/reviews.json', '/report.json']:
            self.assertEqual(self.client.get(location+suffix).status_code, 409)
        self.assertEqual(self.post(location+'/review').status_code, 409)
        self.assertIn('could not be verified', self.client.get(PREFIX).text)

    def test_compare_and_revise_without_changing_original(self):
        first = self.create()
        original = self.report(first)
        edit = self.client.get(first+'/edit')
        self.assertEqual(edit.status_code, 200)
        self.assertIn('name="parent_id" value="'+first.rsplit('/', 1)[1]+'"', edit.text)
        second = self.create(content=HTML.replace('Use the interval', 'It depends on the interval'), parent_id=first.rsplit('/', 1)[1])
        response = self.post(PREFIX+'/compare', before=first.rsplit('/', 1)[1], after=second.rsplit('/', 1)[1])
        self.assertEqual(response.status_code, 200)
        self.assertIn('Not comparable', response.text)
        self.assertIn('Content changed', response.text)
        self.assertEqual(self.report(first), original)
        self.assertEqual(self.post(PREFIX+'/compare', before='x', after='x').status_code, 400)

    def test_batch_limits_and_file_path_rejection(self):
        for manifest in [{'pages':[{'id':'x','content_file':'/etc/passwd'}]}, {'pages':[]}, {'pages':[{'id':str(i),'content':HTML} for i in range(41)]}]:
            self.assertEqual(self.post(PREFIX+'/assess', mode='batch', manifest=json.dumps(manifest)).status_code, 400)
        response = self.post(PREFIX+'/assess', mode='batch', manifest=json.dumps({'sampled':True,'expected_ids':['a','b'],'pages':[{'id':'a','content':HTML}]}))
        self.assertEqual(response.status_code, 303)
        report = self.report(response.headers['location'])
        self.assertEqual(report['coverage']['missing_ids'], ['b'])
        self.assertTrue(report['coverage']['sampled'])

    def test_no_content_refetch_when_reusing_audit_and_legacy_scores_unchanged(self):
        req = self.store.create_requirement('Original', [FIXTURE_URL], {'indexable':True})
        run = self.store.create_run(req['id'])
        self.store.execute_run(run['id'], self.inspector)
        original = self.store.export_report(run['id'])
        self.assertEqual(original['scoring']['score'], 10)
        self.assertIn('Assess saved content', self.client.get('/runs/'+run['id']).text)
        response = self.post(PREFIX+'/from-run/'+run['id'])
        self.assertEqual(response.status_code, 303, response.text)
        report = self.report(response.headers['location'])
        self.assertEqual(self.inspector.call_count, 1)
        self.assertEqual(report['pages'][0]['source']['evidence_kind'], 'synthetic')
        self.assertEqual(self.store.export_report(run['id']), original)
        for fmt in ['json', 'csv', 'md']:
            self.assertEqual(self.client.get(f'/runs/{run["id"]}/report.{fmt}').status_code, 200)
        self.assertEqual(self.client.get(f'/runs/{run["id"]}/report.json').json()['scoring'], original['scoring'])

    def test_scoped_audit_reuse_keeps_source_format_and_scoring(self):
        response = self.post('/qa/audits', title='Pasted', source_kind='supplied', content_format='html', content=HTML, scopes=['content','readiness'])
        self.assertEqual(response.status_code, 303)
        run_id = response.headers['location'].rsplit('/',1)[1]
        original = self.store.export_report(run_id)
        self.assertIn('Assess saved content', self.client.get(response.headers['location']).text)
        result = self.post(PREFIX+'/from-run/'+run_id)
        self.assertEqual(result.status_code, 303, result.text)
        report = self.report(result.headers['location'])
        self.assertEqual(report['pages'][0]['source']['content_format'], 'html')
        self.assertEqual(report['pages'][0]['source']['evidence_kind'], 'supplied')
        self.assertEqual(self.store.export_report(run_id), original)

    def test_original_evidence_tamper_blocks_derived_report(self):
        req=self.store.create_requirement('Original',[FIXTURE_URL],{'indexable':True})
        run=self.store.create_run(req['id']); self.store.execute_run(run['id'],self.inspector)
        response=self.post(PREFIX+'/from-run/'+run['id'])
        self.assertEqual(response.status_code,303)
        location=response.headers['location']
        for path in self.store.evidence_dir.glob('*.json'):
            path.write_text('changed')
        self.assertEqual(self.client.get(location+'/report.json').status_code,409)
        self.assertEqual(self.post(location+'/review').status_code,409)

    def test_queued_run_cannot_be_assessed(self):
        req=self.store.create_requirement('Queued',[FIXTURE_URL],{'indexable':True})
        run=self.store.create_run(req['id'])
        self.assertEqual(self.post(PREFIX+'/from-run/'+run['id']).status_code,400)
        self.assertFalse(LocalRecords(self.store).root.exists())

    def test_csrf_on_every_mutation_and_untrusted_hosts(self):
        location=self.create()
        for path in [PREFIX+'/assess', PREFIX+'/sample', PREFIX+'/from-run/a', PREFIX+'/compare',location+'/review']:
            response=self.client.post(path,data={'csrf_token':'wrong'})
            self.assertEqual(response.status_code,403,(path,response.text))
        self.assertEqual(self.client.get(PREFIX,headers={'host':'evil.example'}).status_code,400)

    def test_local_only_guards_rechecked_for_reads_and_writes(self):
        location=self.create()
        root=LocalRecords(self.store).root
        count=len(list(root.glob('*.json')))
        for key,value in [('production_settings',SimpleNamespace()),('browser_auth',True)]:
            setattr(self.app.state,key,value)
            for path in [PREFIX, location, location+'/report.json', location+'/visual']:
                self.assertEqual(self.client.get(path).status_code,404)
            self.assertEqual(self.post(PREFIX+'/sample').status_code,404)
            self.assertNotIn('href="'+PREFIX+'"',self.client.get('/qa').text)
            delattr(self.app.state,key)
        self.assertEqual(len(list(root.glob('*.json'))),count)

    def test_switched_hosted_store_cannot_read_local_sidecar(self):
        location=self.create()
        class HostedProxy:
            def __getattr__(self,key):return getattr(self.original,key)
        proxy=HostedProxy();proxy.original=self.store
        self.app.state.store=proxy
        self.assertEqual(self.client.get(location).status_code,404)
        self.assertEqual(self.post(PREFIX+'/sample').status_code,404)

    def test_limits_and_html_escaping(self):
        response=self.post(PREFIX+'/assess',content='x'*200001)
        self.assertEqual(response.status_code,400)
        self.assertIn('200 KB',response.text)
        response=self.client.post(PREFIX+'/assess',content='x'*12000001,headers={'content-type':'application/x-www-form-urlencoded'})
        self.assertEqual(response.status_code,413)
        self.assertEqual(self.client.post(PREFIX+'/assess',json={}).status_code,415)
        location=self.create(title='</h1><script>window.BAD=1</script>')
        self.assertNotIn('<script>window.BAD',self.client.get(location).text)
        self.assertNotIn('</script><script>window.BAD',self.client.get(location+'/visual').text)

    def test_main_cli_can_start_local_workspace_without_optional_graph_dependency(self):
        run=subprocess.run([sys.executable,'-m','seo_qa','--help'],text=True,capture_output=True)
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertIn('qa',run.stdout)


if __name__=='__main__':unittest.main()
