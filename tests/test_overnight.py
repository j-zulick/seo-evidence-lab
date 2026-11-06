import sqlite3
from hypothesis import given, strategies as st
from seo_qa.competitor import demo, ingest
from seo_qa.evidence_search import load,index,search

@given(st.lists(st.text(alphabet='abcdef',min_size=1,max_size=15),min_size=1,max_size=18))
def test_import_history_is_retry_safe(bodies):
    fixture=demo()['attempts'][0]
    with sqlite3.connect(':memory:') as conn:
        for i,body in enumerate(bodies):
            sample=fixture|{'captured_at':f'2026-10-01T12:00:{i:02d}+00:00','response':{'success':True,'data':{'markdown':body}}}
            before=ingest(conn,sample)
            assert ingest(conn,sample)==before
        assert conn.execute('SELECT count(*) FROM attempts').fetchone()[0]==len(bodies)
        assert {r[0] for r in conn.execute('SELECT body FROM versions')}==set(bodies)


def test_missing_and_conflict():
    db=index(load())
    try:
        assert search(db,'unobtainium')==[]
        assert {p['id'] for p in search(db,'indexing archive')}=={'conflict-a','conflict-b'}
    finally:
        db.close()


def test_inbox_requires_explicit_action(tmp_path):
    import re
    from fastapi.testclient import TestClient
    from seo_qa.store import Store
    from seo_qa.web import create_app
    client=TestClient(create_app(Store(tmp_path/'local.sqlite')))
    page=client.get('/qa/source-inbox')
    assert 'Pending review' in page.text
    fields={name:re.search(fr'name="{name}" value="([^"]+)"',page.text)[1] for name in ('csrf_token','key')}
    assert client.post('/qa/source-inbox',data=fields|{'csrf_token':'bad','action':'accept'}).status_code==403
    page=client.post('/qa/source-inbox',data=fields|{'action':'defer'})
    assert page.status_code==200 and 'UTC · defer' in page.text


def test_malformed_capture_preserves_version():
    import pytest
    sample=demo()['attempts'][0]
    with sqlite3.connect(':memory:') as conn:
        initial=ingest(conn,sample)
        malformed=sample|{'response':{'success':True,'data':{'markdown':'bad','metadata':{'statusCode':'bad'}}}}
        with pytest.raises(ValueError):
            ingest(conn,malformed)
        assert conn.execute('SELECT id FROM versions').fetchall()==[(initial,)]


def test_unknown_revision_is_unresolved_and_does_not_mutate_manifest():
    from copy import deepcopy
    from seo_qa.competitor import compare
    old=demo()['comparison']['before']['inputs']
    preserved=deepcopy(old)
    assert compare(old,old|{'scoring':'unregistered/v9'})['status']=='unresolved'
    assert old==preserved


def test_browser_search_has_provenance_and_empty_state(tmp_path):
    from fastapi.testclient import TestClient
    from seo_qa.store import Store
    from seo_qa.web import create_app
    client=TestClient(create_app(Store(tmp_path/'search.sqlite')))
    assert 'fixture:publisher-a' in client.get('/qa/evidence-search',params={'q':'noindex'}).text
    assert 'No matching evidence.' in client.get('/qa/evidence-search',params={'q':'unobtainium'}).text
    assert client.get('/qa/evidence-search',params={'q':'x'*513}).status_code==400


def test_visibility_requires_consistent_request_and_capture_dates():
    from seo_qa.competitor import visibility
    series = demo()['series'][0]
    def result(request, start='2026-10-01', end='2026-10-15', rows=None):
        return visibility(series | {'request': series['request'] | request,
                                    'keywords': series['keywords'] if rows is None else rows},
                          series['request']['target'], 'us', start, end)
    assert result({}) == {'before':3,'after':6,'delta':3,'verdict':'rose'}
    missing = {'verdict':'insufficient coverage'}
    assert result({'date_from':'2026-11-01','date_to':'2026-11-15'}) == missing
    assert result({'date_from':'2026-10-16'}) == missing
    assert result({'date_to':'invalid'}) == missing
    assert result({'date_from':None}) == missing
    assert result({},start='invalid') == missing
    assert result({},end='2026-09-01') == missing
    assert result({},rows=series['keywords'][:1]) == missing
    assert result({},rows=series['keywords']+[series['keywords'][0]|{'date':'2026-11-03'}]) == missing


def test_measurement_predicate_refuses_bad_values_and_periods():
    from seo_qa.measurement import comparison_allowed
    left=dict(source='CrUX',scope='url',target='guide',metric='LCP',unit='ms',
              period=['2026-10-01','2026-10-07'],value=0)
    right=left|{'period':['2026-10-08','2026-10-14']}
    assert comparison_allowed(left,right)
    for value in (-100,float('nan'),float('inf'),True,'120',None,{}):
        assert not comparison_allowed(left|{'value':value},right)
        assert not comparison_allowed(left,right|{'value':value})
    for period in (None,[],['bad','bad'],['2026-10-01'],['2026-10-07','2026-10-01'],{}):
        assert not comparison_allowed(left|{'period':period},right)
        assert not comparison_allowed(left,right|{'period':period})
