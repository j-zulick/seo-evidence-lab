"""Normal-entry checks for presentation, numeric drill-down, and local boundaries."""
import json
from pathlib import Path

from fastapi.testclient import TestClient
from seo_qa.web import create_app
from seo_qa.store import Store
from seo_qa.timeline_eval import evaluate, run
from seo_qa.evidence_search import benchmark


def client(tmp_path):
    return TestClient(create_app(Store(tmp_path/'ui.sqlite')))


def test_navigation_and_empty_search(tmp_path):
    c = client(tmp_path)
    for path in ('/qa/lab','/qa/competitors','/qa/evaluations','/qa/community-news','/qa/evidence-search','/qa/source-inbox'):
        page = c.get(path)
        assert page.status_code == 200, path
        assert 'Skip to content' in page.text
        assert 'aria-label="Evidence lab"' in page.text
        assert '/qa/evaluations' in page.text
    assert '/qa/lab' in c.get('/qa').text
    assert 'No matching evidence.' in c.get('/qa/evidence-search?q=unobtainium').text


def test_scorecard_matches_underlying_results_and_exposes_failure(tmp_path):
    c = client(tmp_path)
    report = c.get('/qa/evaluations/report.json').json()
    assert report['experiment'] == evaluate()['report']
    assert report['baseline_passed'] == 4
    assert report['revised_passed'] == 6
    assert report['retrieval_passed'] == 5
    assert len(report['experiment']['cases']) == 6
    assert len(report['retrieval']['queries']) == 6
    failed = [row['query'] for row in report['retrieval']['queries'] if not row['all_expected_in_top_three']]
    assert failed == ['slow user reactions']
    page = c.get('/qa/evaluations')
    for label in ('Simulated judgments','slow user reactions','unsupported','vague','no matching evidence'):
        assert label.lower() in page.text.lower()
    assert report['experiment']['live_calls'] == 0
    assert report['experiment']['human_labels'] is None


def test_evaluation_cli_reports_remain_compatible(tmp_path):
    result = run(tmp_path)
    assert json.loads((tmp_path/'experiment-comparison.json').read_text()) == result
    assert result == evaluate()['report']
    assert len(json.loads((tmp_path/'langfuse-dataset-items.json').read_text())) == 6


def test_fixture_routes_refuse_hosted_state(tmp_path):
    c = client(tmp_path)
    c.app.state.browser_auth = True
    for path in ('/qa/lab','/qa/evaluations','/qa/evaluations/report.json'):
        assert c.get(path).status_code == 404


def test_walkthrough_retains_content_binding_and_blank_reviews(tmp_path):
    from seo_qa.competitor import ROOT
    c = client(tmp_path)
    library = c.get('/qa/walkthrough/library.json').json()
    template = c.get('/qa/walkthrough/reviews.json').json()
    assert library['pages'][0]['content'] == (ROOT/'fixtures/walkthrough/article.html').read_text()
    assert 'small-site owner' in c.get('/qa/walkthrough').text
    record = template[library['pages'][0]['id']]
    for reviewer in record['passes']:
        assert reviewer['evaluator_id'] == ''
        assert all(s['answer_first'] is None and s['standalone_openings'] is None
                   for s in reviewer['sections'].values())
    assert c.get('/qa/walkthrough/unknown.json').status_code == 404
