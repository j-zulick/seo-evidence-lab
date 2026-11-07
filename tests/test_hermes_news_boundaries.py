"""Saved community lead contracts: provenance, malformed input and custody."""
from copy import deepcopy
import pytest

from seo_qa.community_news import review


def saved_input():
    return {'synthetic': True, 'posts': [{
        'id': 'saved-a', 'subreddit': 'fixture-seo', 'title': 'Unverified indexing report',
        'created_at': '2026-11-01T09:00:00Z',
        'observed_at': '2026-11-01T10:00:00Z',
        'primary_sources': ['https://publisher.example/announcement'],
    }]}


@pytest.mark.parametrize('flag', ['true', 1, 0, None, [], {}])
def test_synthetic_origin_requires_explicit_boolean(flag):
    data = saved_input()
    data['synthetic'] = flag
    with pytest.raises(ValueError):
        review(data)


def test_missing_origin_cannot_silently_become_real():
    data = saved_input()
    del data['synthetic']
    with pytest.raises(ValueError):
        review(data)


@pytest.mark.parametrize('flag', [True, False])
def test_boolean_origin_is_retained_without_approval(flag):
    data = saved_input()
    data['synthetic'] = flag
    result = review(data)
    assert result['synthetic'] is flag
    assert result['publication'] == 'human review pending'
    assert result['leads'][0]['guidance_approved'] is False
    assert result['network_calls'] == 0


@pytest.mark.parametrize('post', [None, [], 'bad', {}, {'id': 'a'}])
def test_malformed_post_has_controlled_validation_failure(post):
    with pytest.raises(ValueError):
        review({'synthetic': True, 'posts': [post]})


@pytest.mark.parametrize('field,value', [
    ('created_at', 1), ('observed_at', None),
    ('subreddit', {'private': 'not a community name'}), ('subreddit', ''),
    ('title', '   '), ('primary_sources', [123]),
])
def test_malformed_fields_have_controlled_validation_failure(field, value):
    data = saved_input()
    data['posts'][0][field] = value
    with pytest.raises(ValueError):
        review(data)


def test_result_owns_its_locator_list():
    data = saved_input()
    original = deepcopy(data)
    result = review(data)
    data['posts'][0]['primary_sources'].append('https://different.example/changed')
    assert result['leads'][0]['primary_sources'] == original['posts'][0]['primary_sources']
    result['leads'][0]['primary_sources'].clear()
    assert len(data['posts'][0]['primary_sources']) == 2


def test_non_json_and_nonfinite_extra_data_are_refused():
    for extra in [object(), float('nan')]:
        data = saved_input()
        data['unexpected'] = extra
        with pytest.raises(ValueError):
            review(data)


def test_removed_content_is_still_minimized():
    data = {'synthetic': True, 'posts': [{'id': 'removed-a', 'removed': True,
             'title': 'private removed text', 'primary_sources': ['https://private.example/old']}]}
    assert review(data)['leads'] == [{'id': 'removed-a', 'status': 'removed', 'text_retained': False}]
