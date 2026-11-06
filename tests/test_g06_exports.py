"""Persisted cross-format contract checks; offline synthetic inputs only."""
import copy
import csv
import html
import io
import json
import pytest
from fastapi.testclient import TestClient
from seo_qa.csv_export import canonical, export_report_csv, read_report_csv, ref, safe_cell, scrub
from seo_qa.inspector import inspect_html
from seo_qa.markdown_export import export_report_markdown
from seo_qa.scope_criteria import RULES, SCOPES, configuration, observe
from seo_qa.store import Store
from seo_qa.web import create_app

ALL = [s[0] for s in SCOPES]
CONTENT = '<title>Private article</title><h1>Review</h1><p>PRIVATE_SOURCE_SENTINEL</p>'
URLS = ['https://example.test/one', 'https://example.test/two']

def persisted(tmp_path, scopes=ALL, kind='supplied', fmt='html', status=200, partial=False):
    path = tmp_path/'audit.sqlite'
    store = Store(path)
    config = configuration(scopes, kind, '' if kind=='url' else CONTENT, fmt)
    req = store.create_requirement('Synthetic audit', URLS if partial else URLS[:1], {'seo_qa':config})
    run = store.create_run(req['id'])
    def capture(url, _):
        if status == 0:
            return {'status':0, 'evidence':{}, 'limitations':['Capture denied']}
        return dict(inspect_html(CONTENT,url), status=status)
    def inspector(url, expected):
        if partial and url==URLS[1]:
            raise ValueError('Synthetic capture failure')
        return observe(url, expected, capture=capture)
    if partial:
        with pytest.raises(ValueError, match='Synthetic'):
            store.execute_run(run['id'], inspector)
    else:
        store.execute_run(run['id'], inspector)
    return Store(path), run['id']

def compare(report):
    original = copy.deepcopy(report)
    rows = read_report_csv(export_report_csv(report))
    md = html.unescape(export_report_markdown(report).decode())
    assert report==original
    assert {r['report_ref'] for r in rows}=={ref(canonical(report))}
    for row in rows:
        assert json.dumps(row['payload'],ensure_ascii=False,sort_keys=True) in md
    for key in ('id','requirement_id','version','state','stale','superseded','latest_run_id','coverage','acceptance_current'):
        assert rows[0]['payload'][key]==report.get(key)
    from seo_qa.scoring import score_report
    scoring = score_report(report)
    assert report['scoring'] == scoring
    score_rows = [r['payload'] for r in rows if r['record_type']=='scoring']
    assert score_rows == [scrub({k:v for k,v in scoring.items() if k!='missed_items'})]
    assert [r['payload'] for r in rows if r['record_type']=='missed_item'] == scrub(scoring['missed_items'])
    findings = [r['payload'] for r in rows if r['record_type']=='finding']
    assert findings==[dict(scrub({k:f.get(k) for k in ('check','expected','observed','reason')}),outcome=f['outcome']) for f in report['findings']]
    assert 'PRIVATE_SOURCE_SENTINEL' not in export_report_csv(report).decode('utf-8-sig')
    assert 'PRIVATE_SOURCE_SENTINEL' not in md
    assert any(r['record_type']=='projection' for r in rows)
    return rows

@pytest.mark.parametrize('scopes', [[s] for s in ALL]+[ALL])
@pytest.mark.parametrize('kind,fmt,status,provenance', [
    ('supplied','html',200,'supplied'),('supplied','text',200,'supplied'),
    ('sample','html',200,'synthetic_sample'),('url','html',200,'fetched'),
    ('url','html',403,'fetched'),('url','html',0,'url_attempt')])
def test_scopes_sources_blocked(tmp_path,scopes,kind,fmt,status,provenance):
    store,rid=persisted(tmp_path,scopes,kind,fmt,status)
    report=store.export_report(rid)
    rows=compare(report)
    obs=next(r['payload'] for r in rows if r['record_type']=='observation')
    assert obs['provenance']['kind']==provenance
    assert obs['criteria']=={k:report['observations'][0]['evidence']['criteria'][k] for k in ('version','sha256')}
    assert len(report['findings'])==sum(len(RULES[s]) for s in scopes)
    if status in (403,0):
        assert not any(f['outcome']=='pass' for f in report['findings'])
        assert report['acceptance_current'] is False

def test_partial_queued_superseded_historical(tmp_path,monkeypatch):
    import seo_qa.scope_criteria as criteria
    monkeypatch.setattr(criteria,'VERSION','seo-qa-general/historical-test')
    store,rid=persisted(tmp_path,kind='url',partial=True)
    old=store.export_report(rid)
    assert old['state']=='error' and old['coverage']=={'expected':2,'observed':1,'sampled':False}
    monkeypatch.setattr(criteria,'VERSION','seo-qa-general/future-test')
    queued=store.create_run(old['requirement_id'])
    compare(store.export_report(queued['id']))
    store.revise_requirement(old['requirement_id'],{'seo_qa':configuration(ALL,'supplied',CONTENT)})
    report=store.export_report(rid)
    rows=compare(report)
    assert report['stale'] and report['superseded'] and not report['acceptance_current']
    assert report['findings']==old['findings']
    req=next(r['payload'] for r in rows if r['record_type']=='requirement')
    assert req['expected']['seo_qa']['criteria_version']=='seo-qa-general/historical-test'

@pytest.mark.parametrize('value',['=1+1','+SUM(A1)','-2','@cmd','\x00 =1','\t=1','\r=1','\n=1','\ufeff=1','\u00a0=1',"'literal",'é, "quoted"\nline','ordinary'])
def test_spreadsheet_roundtrip(value):
    report={'id':value,'findings':[{'check':value,'outcome':'inconclusive','reason':value}]}
    raw=export_report_csv(report)
    assert raw.startswith(b'\xef\xbb\xbf')
    cells=list(csv.reader(io.StringIO(raw.decode('utf-8-sig'),newline='')))
    assert cells[1][3]==safe_cell(value) and cells[3][5]==safe_cell(value)
    rows=read_report_csv(raw)
    assert rows[0]['record_ref']==value and rows[2]['payload']['check']==value

def test_credentials_valid_json_unknown_outcomes_knowledge_omitted():
    report={'id':'test','knowledge':{'private':'PRIVATE_GRAPH'},
            'requirement':{'expected':{'required_text':['password=synthetic']},'urls':['https://private.test/path']},
            'findings':[{'check':'=formula','outcome':'future_outcome','reason':'token=synthetic'}]}
    raw=export_report_csv(report)
    rows=read_report_csv(raw)
    assert rows[1]['payload']['expected']['required_text']==['[REDACTED]']
    assert 'private.test' not in raw.decode('utf-8-sig')
    assert rows[2]['outcome']=='unknown' and rows[2]['payload']['source_outcome']=='future_outcome'
    md=export_report_markdown(report).decode()
    assert 'PRIVATE_GRAPH' not in md and 'historical citations' in md

@pytest.mark.parametrize('ext',['json','csv','md'])
def test_download_owner_integrity(tmp_path,ext):
    store,rid=persisted(tmp_path)
    path=f'/runs/{rid}/report.{ext}'
    response=TestClient(create_app(store)).get(path)
    assert response.status_code==200 and response.headers['cache-control']=='no-store'
    assert 'attachment;' in response.headers['content-disposition']
    other=Store(tmp_path/'audit.sqlite',owner_id='other-owner')
    assert TestClient(create_app(other)).get(path).status_code==400
    for evidence in store.evidence_dir.glob('*.json'):
        evidence.write_text('corrupt')
    assert TestClient(create_app(store)).get(path).status_code==400


def test_acceptance_waiver_supersession_and_revision_agree(tmp_path):
    from seo_qa.fixtures import FIXTURE_URL, fixture_inspector
    s=Store(tmp_path/'legacy.sqlite')
    req=s.create_requirement('Legacy failure',[FIXTURE_URL],{'indexable':False})
    run=s.create_run(req['id']);s.execute_run(run['id'],fixture_inspector)
    assert any(f['outcome']=='fail' for f in s.get_run(run['id'])['findings'])
    s.review(run['id'],'accepted_with_waiver','Synthetic waiver',expected_version=1)
    report=s.export_report(run['id']);compare(report)
    assert report['acceptance_current'] is True
    s.create_run(req['id'])
    report=s.export_report(run['id']);compare(report)
    assert report['superseded'] and not report['acceptance_current']
    assert report['reviews'][-1]['decision']=='accepted_with_waiver'
    s.revise_requirement(req['id'],{'indexable':False})
    report=s.export_report(run['id']);compare(report)
    assert report['stale'] and report['version']==1
