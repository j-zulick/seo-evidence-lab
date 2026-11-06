from copy import deepcopy
import pytest
from fastapi.testclient import TestClient
from seo_qa.community_news import fixture, review
from seo_qa.competitor import ROOT
from seo_qa.store import Store
from seo_qa.web import create_app
import json


def test_leads_require_review_and_removed_content_cleared():
    data=fixture()
    assert [p['status'] for p in data['leads']]==['unverified lead','primary review needed','removed']
    assert all(p.get('guidance_approved') is not True for p in data['leads'])
    sample=json.loads((ROOT/'fixtures/community-news.json').read_text())
    sample['posts'][2]['title']='text removed by author'
    assert 'text removed' not in json.dumps(review(sample))
    assert data['network_calls']==0


def test_input_validation_and_immutability():
    sample=json.loads((ROOT/'fixtures/community-news.json').read_text())
    original=deepcopy(sample)
    review(sample)
    assert sample==original
    with pytest.raises(ValueError):
        review(sample|{'posts':sample['posts']+[sample['posts'][0]]})
    sample['posts'][0]['observed_at']='2026-10-01T10:00:00Z'
    with pytest.raises(ValueError):
        review(sample)


def test_normal_app_entry(tmp_path):
    c=TestClient(create_app(Store(tmp_path/'audit.sqlite')))
    assert '/qa/community-news' in c.get('/qa/competitors').text
    page=c.get('/qa/community-news')
    assert page.status_code==200
    assert 'primary review needed' in page.text
