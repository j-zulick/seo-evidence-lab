"""Saved-export boundaries; all test inputs are synthetic and offline."""
from copy import deepcopy
import json
import re
import sqlite3
import subprocess
import sys
from fastapi.testclient import TestClient
import pytest
from seo_qa.news_bridge import import_saved, normalize, report, decide, source_url
from seo_qa.store import Store
from seo_qa.web import create_app


def email(title='A saved SEO report', text='Inspect evidence before changing guidance.'):
    return {'to': 'synthetic-recipient@example.invalid', 'subject': 'Synthetic digest',
            'payload': {'mime_type': 'text/html', 'body': {'content':
                '<p><a href="https://docs.google.com/document/d/PRIVATE-DEMO-ID/edit">Running document</a></p>'
                '<h2>SEO</h2><p><a href="https://publisher.example/guide#section">'+title+'</a></p>'
                '<p>Publisher | Published: 2026-11-02; time not stated</p><p>'+text+'</p>'}}}


def raw(value):
    return json.dumps(value).encode()


def test_saved_shape_minimization_unknown_time_and_immutability(tmp_path):
    source = email(text='Digest footer: synthetic-recipient@example.invalid'); original = deepcopy(source); db = tmp_path/'news.sqlite'
    result = import_saved(db, raw(source), synthetic=True)
    row = report(db)['items'][0]
    assert result['network_calls'] == 0 and not result['guidance_published']
    assert row['published_at'] == '2026-11-02' and row['observed_at'] is None
    assert row['url'] == 'https://publisher.example/guide'
    assert row['source_kind'] == 'saved_digest_excerpt' and row['synthetic'] is True
    assert row['decision'] is None and row['guidance_approved'] is False
    assert source == original
    for private in [b'synthetic-recipient', b'PRIVATE-DEMO-ID', b'Running document', b'"to"']:
        assert private not in db.read_bytes()


def test_retry_versions_and_late_arrival_do_not_delete(tmp_path):
    db = tmp_path/'news.sqlite'
    a = import_saved(db, raw(email()), synthetic=True)
    b = import_saved(db, raw(email()), synthetic=True)
    assert a['import_id'] == b['import_id'] and b['duplicate_import']
    assert report(db)['attempts'] == 2 and report(db)['imports'] == 1
    assert len(report(db)['items']) == 1
    changed = email(text='Earlier publication, later import: retain both.')
    changed['payload']['body']['content'] = changed['payload']['body']['content'].replace('2026-11-02', '2026-10-20')
    import_saved(db, raw(changed), synthetic=True)
    rows = report(db)['items']
    assert len(rows) == 2 and rows[0]['published_at'] == '2026-10-20'
    assert len({r['story_id'] for r in rows}) == 1 and len({r['version_id'] for r in rows}) == 2
    assert 'not publisher chronology' in report(db)['order']


@pytest.mark.parametrize('url', ['http://publisher.example/', 'https://user:password@example.org/',
    'https://127.0.0.1/a', 'https://[::1]/a', 'https://localhost/a', 'https://host.internal/a',
    'https://docs.google.com/document/d/private', 'https://example.org:bad/', 'https://example.org/has space'])
def test_unsafe_locators_refused(url):
    with pytest.raises(ValueError): source_url(url)


@pytest.mark.parametrize('payload', [b'{bad', b'{"a":1,"a":2}', b'{"a":NaN}', b'[]', b'null'])
def test_rejections_preserve_last_good_data_and_record_failure(tmp_path, payload):
    db = tmp_path/'news.sqlite'; import_saved(db, raw(email()), synthetic=True)
    with pytest.raises(ValueError): import_saved(db, payload, synthetic=True)
    result = report(db)
    assert len(result['items']) == 1 and result['imports'] == 1 and result['attempts'] == 2
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT status FROM news_attempts ORDER BY id DESC LIMIT 1').fetchone()[0] == 'rejected'


def normalized():
    return {'schema': 'seo-news-export/v1', 'synthetic': True, 'items': [{
        'url': 'https://publisher.example/guide', 'title': 'Saved guide', 'summary': 'Review needed.',
        'topics': ['SEO'], 'published_at': None, 'observed_at': None}]}


def test_limits_origin_temporal_precision_and_atomic_validation(tmp_path):
    db = tmp_path/'news.sqlite'
    with pytest.raises(ValueError): import_saved(db, b'x'*2_000_001)
    with pytest.raises(ValueError): import_saved(db, raw({'payload': {'mime_type': 'text/html'}}))
    value = normalized()
    assert normalize(value, 'normalized', True)[0]['published_at'] is None
    with pytest.raises(ValueError): normalize(value, 'normalized', False)
    value['items'].append(value['items'][0] | {'observed_at': '2026-11-02T10:00:00'})
    with pytest.raises(ValueError): import_saved(db, raw(value), 'normalized', True)
    assert report(db)['items'] == []
    value = normalized()
    value['items'][0] |= {'published_at': '2026-11-02T12:00:00+00:00', 'observed_at': '2026-11-02T11:00:00+00:00'}
    with pytest.raises(ValueError): normalize(value, 'normalized', True)


def test_manual_triage_is_version_bound_without_guidance_activation(tmp_path):
    db = tmp_path/'news.sqlite'; import_saved(db, raw(email()), synthetic=True)
    version = report(db)['items'][0]['version_id']
    decide(db, version, 'accept_for_review', 'Synthetic actor', 'Synthetic declaration only.')
    import_saved(db, raw(email(text='New excerpt needs new review.')), synthetic=True)
    rows = report(db)['items']
    assert rows[0]['decision'] is None and rows[1]['decision']['action'] == 'accept_for_review'
    assert all(r['guidance_approved'] is False for r in rows)
    with pytest.raises(ValueError): decide(db, version, 'publish_guidance', 'Synthetic actor', 'Not allowed.')
    with pytest.raises(ValueError): decide(db, 'unknown', 'defer', 'Synthetic actor', 'Unknown version.')


def test_local_ui_csrf_escape_download_and_hosted_boundary(tmp_path):
    client = TestClient(create_app(Store(tmp_path/'audit.sqlite')))
    page = client.get('/qa/news-imports')
    assert page.status_code == 200 and 'No saved observations yet' in page.text
    assert page.headers['cache-control'] == 'no-store'
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text)[1]
    assert client.post('/qa/news-imports', data={'content': '{}', 'csrf_token': 'wrong'}).status_code == 403
    redirect = client.post('/qa/news-imports', data={'content': json.dumps(email(title='&lt;img src=x onerror=alert(1)&gt;')), 'synthetic': 'true', 'csrf_token': token}, follow_redirects=False)
    assert redirect.status_code == 303 and redirect.headers['cache-control'] == 'no-store'
    response = client.get(redirect.headers['location'])
    assert response.status_code == 200 and '&lt;img src=x onerror=alert(1)&gt;' in response.text
    assert '<img src=x' not in response.text
    result = client.get('/qa/news-imports/report.json')
    assert result.status_code == 200 and result.headers['cache-control'] == 'no-store'
    assert result.json()['imports'] == 1
    assert '/qa/news-imports' in client.get('/qa/community-news').text
    client.app.state.browser_auth = True
    assert client.get('/qa/news-imports').status_code == 404
    assert client.get('/qa/news-imports/report.json').status_code == 404


def test_cli_reads_back_persisted_export_in_another_process(tmp_path):
    src = tmp_path/'saved.json'; src.write_bytes(raw(email())); db = tmp_path/'news.sqlite'
    first = subprocess.run([sys.executable, '-m', 'seo_qa.news_bridge', 'import', '--input', str(src), '--db', str(db), '--synthetic'], capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    second = subprocess.run([sys.executable, '-m', 'seo_qa.news_bridge', 'report', '--db', str(db)], capture_output=True, text=True)
    assert second.returncode == 0 and len(json.loads(second.stdout)['items']) == 1


def test_news_html_is_not_cacheable(tmp_path):
    from fastapi.testclient import TestClient
    from seo_qa.web import create_app
    from seo_qa.store import Store
    c = TestClient(create_app(Store(tmp_path/'no-cache.sqlite')))
    response = c.get('/qa/news-imports')
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'

def test_proposal_binds_source_exports_and_replay_without_activation(tmp_path):
    from seo_qa.news_bridge import propose
    from seo_qa.competitor import audit
    db = tmp_path/'news.sqlite'
    receipt = import_saved(db, raw(email()), synthetic=True)
    version = report(db)['items'][0]['version_id']
    inputs = {'article':'# Reset settings\nOpen settings.', 'guidance':{'rule':'heading_present'},
              'scoring':'heading/v1', 'evidence':True}
    original = deepcopy(inputs)
    proposal = propose(db, version, inputs, 'question_heading', 'Synthetic software author',
                       'Synthetic source-to-rule mapping for proposal-only replay.')
    assert inputs == original
    assert proposal['audit_id'] == audit(inputs)['id'] and proposal['audit_pins'] == audit(inputs)['pins']
    assert proposal['import_provenance'] == [{'import_id':receipt['import_id'], 'export_hash':receipt['export_hash']}]
    assert proposal['replay']['before']['finding'] == 'pass'
    assert proposal['replay']['after']['finding'] == 'miss'
    assert proposal['human_approval'] is None and not proposal['active'] and not proposal['source_truth_verified']
    again = propose(db, version, inputs, 'question_heading', 'Synthetic software author',
                    'Synthetic source-to-rule mapping for proposal-only replay.')
    assert again == proposal
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT COUNT(*) FROM news_proposals').fetchone()[0] == 1
    assert report(db)['items'][0]['decision'] is None


@pytest.mark.parametrize('invalid', ['unknown_version','unsupported_rule','missing_reason'])
def test_proposals_refuse_unknown_source_or_unexplained_mapping(tmp_path, invalid):
    from seo_qa.news_bridge import propose
    db = tmp_path/'news.sqlite'; import_saved(db, raw(email()), synthetic=True)
    version = report(db)['items'][0]['version_id']
    inputs = {'article':'# Heading', 'guidance':{'rule':'heading_present'}, 'scoring':'heading/v1', 'evidence':True}
    with pytest.raises(ValueError):
        propose(db, 'unknown' if invalid=='unknown_version' else version, inputs,
                'unsupported' if invalid=='unsupported_rule' else 'question_heading',
                'Synthetic author', '' if invalid=='missing_reason' else 'Explicit mapping')
