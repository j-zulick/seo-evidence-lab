import sqlite3
from fastapi.testclient import TestClient
from seo_qa.competitor import demo, compare, ingest, visibility
from seo_qa.store import Store
from seo_qa.web import create_app


def test_capture_retry_timeout_and_redirect():
    data=demo()
    assert data['counts']=={'attempts':4,'versions':2}
    with sqlite3.connect(':memory:') as db:
        first=data['attempts'][0]
        assert ingest(db,first)==ingest(db,first)
        assert db.execute('SELECT count(*) FROM attempts').fetchone()==(1,)
        redirected=dict(first,requested_url='https://edited.example/old')
        assert ingest(db,redirected)==ingest(db,first)
        assert ingest(db,data['attempts'][-1]) is None
        assert db.execute('SELECT count(*) FROM versions').fetchone()==(1,)


def test_attribution_and_unknown():
    old=demo()['comparison']['before']['inputs']
    for key,before,after in [('article','Title','## Good?'),('evidence',False,True),('scoring','heading/v1','heading/v2'),('guidance',{'rule':'heading_present'},{'rule':'question_heading'})]:
        baseline=dict(old,article='## Title', **{})
        left=baseline|{key:before};right=baseline|{key:after}
        assert compare(left,right)['minimal_sufficient_changes']==[(key,)]
    assert compare(old,old|{'scoring':'unknown'})['status']=='unresolved'
    left=old|{'evidence':False};right=old|{'article':'## A question?', 'evidence':True}
    assert compare(left,right)['minimal_sufficient_changes']==[('article','evidence')]


def test_coverage():
    series=demo()['series'][0]; url=series['request']['target']
    for record in [series|{'keywords':[]},series|{'request':series['request']|{'history_grouping':'monthly'}}]:
        assert visibility(record,url,'us','2026-10-01','2026-10-15')['verdict']=='insufficient coverage'


def test_normal_entry(tmp_path):
    client=TestClient(create_app(Store(tmp_path/'audit.sqlite')))
    assert '/qa/competitors' in client.get('/qa').text
    assert 'Both pages gained' in client.get('/qa/competitors').text
    record=client.get('/qa/competitors/comparison.json').json()['comparison']['before']
    assert client.get('/qa/competitors/evidence/'+record['id']).json()==record
    assert client.get('/qa/competitors/evidence/unknown').status_code==404
