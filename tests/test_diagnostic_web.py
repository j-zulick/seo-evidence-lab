import re
from fastapi.testclient import TestClient
from seo_qa.web import create_app
from seo_qa.store import Store


def test_manual_diagnostics_boundary_and_escape(tmp_path):
    c = TestClient(create_app(Store(tmp_path/'web.sqlite')))
    page = c.get('/qa/saved-diagnostics')
    assert page.status_code == 200
    assert page.headers['cache-control'] == 'no-store'
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    bad = dict(content='{}', engine='<script>alert(1)</script>', csrf_token=token)
    result = c.post('/qa/saved-diagnostics', data=bad)
    assert result.status_code == 400
    assert '<script>alert(1)</script>' not in result.text
    bad['csrf_token'] = 'wrong'
    assert c.post('/qa/saved-diagnostics', data=bad).status_code == 403
    c.app.state.browser_auth = True
    assert c.get('/qa/saved-diagnostics').status_code == 404
    assert c.post('/qa/saved-diagnostics', data=bad).status_code == 404


def test_saved_provider_example_has_functional_web_path(tmp_path):
    from pathlib import Path
    c = TestClient(create_app(Store(tmp_path/'provider.sqlite')))
    token = re.search(r'name="csrf_token" value="([^"]+)"', c.get('/qa/saved-diagnostics').text).group(1)
    content = (Path(__file__).resolve().parents[1]/'examples/provider-export-synthetic.json').read_text()
    response = c.post('/qa/saved-diagnostics', data=dict(engine='provider', content=content, csrf_token=token))
    assert response.status_code == 200
    assert 'Normalized diagnostic candidate' in response.text
    assert 'provider-normalizer/v1' in response.text
    assert not list(tmp_path.glob('*.json'))
