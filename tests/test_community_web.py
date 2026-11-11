"""Local access, escaping and persistent removal/expiry through real web routes."""
from datetime import datetime, timedelta, timezone
import json
import re
from fastapi.testclient import TestClient
import pytest
from seo_qa.web import create_app
from seo_qa.store import Store
from seo_qa.community_store import CommunityStore


def setup(tmp_path):
    app = create_app(Store(tmp_path/'state.sqlite'))
    return app, TestClient(app), tmp_path/'state.sqlite.community.sqlite'


def token(client):
    page = client.get('/qa/community-retained')
    assert page.status_code == 200
    return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)


def payload(days_ago=1, **fields):
    now = datetime.now(timezone.utc) - timedelta(days=days_ago)
    post = dict(id='private-1', title='<script>private title</script>', subreddit='fixture',
                observed_at=now.isoformat(), created_at=(now-timedelta(hours=1)).isoformat(),
                primary_sources=['https://example.test/source'], username='excluded-user', body='excluded-raw-post')
    post.update(fields)
    return dict(synthetic=True, posts=[post])


def submit(client, raw=None, **extra):
    values = dict(csrf_token=token(client), saved_json=json.dumps(raw or payload()),retention_days='30')
    values.update(extra)
    return client.post('/qa/community-retained/import',data=values,follow_redirects=False)


def test_private_roundtrip_restart_removal_and_late_import(tmp_path):
    app, client, db = setup(tmp_path)
    raw = payload()
    assert submit(client, raw).status_code == 303
    page = client.get('/qa/community-retained')
    assert page.headers['cache-control'] == 'no-store'
    assert '&lt;script&gt;private title&lt;/script&gt;' in page.text
    assert '<script>private title' not in page.text and 'Synthetic' in page.text
    assert 'guidance approval pending' in page.text
    assert b'excluded-user' not in db.read_bytes() and b'excluded-raw-post' not in db.read_bytes()
    # A separate app instance uses the same private store.
    reopened = TestClient(create_app(Store(tmp_path/'state.sqlite')))
    assert 'private title' in reopened.get('/qa/community-retained').text
    response = reopened.post('/qa/community-retained/remove',data=dict(csrf_token=token(reopened),lead_id='private-1'),follow_redirects=False)
    assert response.status_code == 303
    assert 'private title' not in reopened.get('/qa/community-retained').text
    assert b'private title' not in db.read_bytes()
    assert submit(reopened, raw).status_code == 303
    assert 'private title' not in reopened.get('/qa/community-retained').text
    # Existing fixture presentation is preserved.
    assert client.get('/qa/community-news').status_code == 200


@pytest.mark.parametrize('mode', ['production','browser','nonlocal'])
def test_unavailable_modes_never_create_community_store(tmp_path, mode):
    app, client, db = setup(tmp_path)
    if mode == 'production': app.state.production_settings = object()
    elif mode == 'browser': app.state.browser_auth = True
    else: app.state.store = object()
    assert client.get('/qa/community-retained').status_code == 404
    for suffix in ('import','remove'):
        assert client.post('/qa/community-retained/'+suffix,data={'csrf_token':'x'}).status_code == 404
    assert not db.exists()


def test_csrf_and_bad_host_do_not_create_store(tmp_path):
    app, client, db = setup(tmp_path)
    assert client.post('/qa/community-retained/import',data=dict(csrf_token='wrong',saved_json=json.dumps(payload()))).status_code == 403
    assert client.get('/qa/community-retained',headers={'host':'attacker.example'}).status_code == 400
    assert not db.exists()


def test_expired_text_is_scrubbed_on_import_and_bad_retention_rejected(tmp_path):
    app, client, db = setup(tmp_path)
    assert submit(client,payload(days_ago=4),retention_days='2').status_code == 303
    assert 'private title' not in client.get('/qa/community-retained').text
    assert 'expired' in client.get('/qa/community-retained').text
    assert b'private title' not in db.read_bytes()
    for days in ('0','91','bad'):
        assert submit(client,retention_days=days).status_code == 400


def test_malformed_and_oversized_inputs_are_generic_and_do_not_echo(tmp_path):
    app, client, db = setup(tmp_path)
    csrf = token(client)
    for value in ['secret-malformed-json', json.dumps(payload(synthetic='invalid'))]:
        # The second input keeps a valid envelope but a harmless extra field;
        # explicitly break its title to ensure refusal rather than guess.
        if value.startswith('{'):
            p=json.loads(value);p['posts'][0]['title']=None;value=json.dumps(p)
        response = client.post('/qa/community-retained/import',data=dict(csrf_token=csrf,saved_json=value))
        assert response.status_code == 400 and 'secret-malformed-json' not in response.text
    assert client.post('/qa/community-retained/import',data=dict(csrf_token=csrf,saved_json='x'*500001)).status_code == 413
    assert client.post('/qa/community-retained/import',content=b'x'*3100001,headers={'content-type':'application/x-www-form-urlencoded'}).status_code == 413
    assert client.post('/qa/community-retained/import',json=payload()).status_code == 415
    assert client.post('/qa/community-retained/remove',data=dict(csrf_token=csrf,lead_id='unknown')).status_code == 404
    assert CommunityStore(db).snapshot(as_of=datetime.now(timezone.utc).isoformat())['leads'] == []
