"""Offline provenance facet acceptance; runnable without third-party packages."""
import unittest
from seo_qa.competitor import digest
from seo_qa.evidence_search import benchmark, faceted_search, index, load, search


class EvidenceFacetTests(unittest.TestCase):
    def setUp(self):
        self.db = index({'passages': [
            {'id': 'a1', 'source': 'publisher-a', 'version': '1', 'text': 'archive index'},
            {'id': 'a2', 'source': 'publisher-a', 'version': '2', 'text': 'archive canonical'},
            {'id': 'b1', 'source': 'publisher-b', 'version': '1', 'text': 'archive index'},
            {'id': 'b2', 'source': 'publisher-b', 'version': '2', 'text': 'unrelated speed'},
            {'id': 'c1', 'source': 'publisher-c', 'version': '1', 'text': 'archive index'},
        ]})
        self.addCleanup(self.db.close)

    def test_counts_cover_all_matches_before_pagination(self):
        result = faceted_search(self.db, 'archive', limit=1)
        self.assertEqual(result['total'], 4)
        self.assertEqual(len(result['results']), 1)
        self.assertTrue(result['has_more'])
        self.assertEqual(sum(row['count'] for row in result['facets']['sources']), 4)

    def test_disjunctive_counts_keep_other_facet_and_query(self):
        result = faceted_search(self.db, 'archive', sources=['publisher-a'], versions=['1'])
        self.assertEqual([row['id'] for row in result['results']], ['a1'])
        self.assertEqual({r['value']: r['count'] for r in result['facets']['sources']},
                         {'publisher-a': 1, 'publisher-b': 1, 'publisher-c': 1})
        self.assertEqual({r['value']: r['count'] for r in result['facets']['versions']},
                         {'1': 1, '2': 1})
        self.assertTrue(result['facets']['sources'][0]['selected'])

    def test_or_within_facet_and_across_facets(self):
        result = faceted_search(self.db, '', sources=['publisher-a', 'publisher-b'], versions=['1'])
        self.assertEqual({row['id'] for row in result['results']}, {'a1', 'b1'})

    def test_unknown_selected_value_remains_visible_with_zero_count(self):
        result = faceted_search(self.db, 'archive', sources=['missing'])
        self.assertEqual(result['total'], 0)
        self.assertIn({'value': 'missing', 'count': 0, 'selected': True},
                      result['facets']['sources'])

    def test_filters_and_operators_are_data(self):
        self.assertEqual(faceted_search(self.db, 'archive', sources=["x') OR 1=1 --"])['total'], 0)
        self.assertEqual(faceted_search(self.db, 'archive OR speed')['total'], 5)
        self.assertEqual(faceted_search(self.db, '!!!')['total'], 0)

    def test_browse_pagination_is_stable(self):
        first = faceted_search(self.db, limit=2)
        second = faceted_search(self.db, limit=2, offset=2)
        self.assertEqual([r['id'] for r in first['results']], ['a1', 'a2'])
        self.assertEqual([r['id'] for r in second['results']], ['b1', 'b2'])
        self.assertEqual(faceted_search(self.db, offset=5)['total'], 5)
        self.assertFalse(faceted_search(self.db, offset=5)['has_more'])

    def test_provenance_and_limits_are_explicit(self):
        result = faceted_search(self.db, 'canonical')
        row = result['results'][0]
        self.assertEqual(row['sha256'], digest([row['source'], row['version'], row['passage']]))
        self.assertEqual(result['coverage'], 'indexed_corpus_only')
        self.assertTrue(result['limitations'])
        self.assertEqual(result['query_mode'], 'any_term')
        self.assertEqual(faceted_search(self.db)['query_mode'], 'browse')

    def test_bounded_inputs_reject_silent_query_truncation(self):
        invalid = [
            {'query': None}, {'query': 'x' * 513}, {'query': ' '.join(['x'] * 21)},
            {'limit': True}, {'limit': 0}, {'limit': 101}, {'offset': -1},
            {'offset': 100001}, {'offset': False}, {'sources': 'publisher-a'},
            {'versions': [None]}, {'sources': ['']}, {'sources': ['x' * 513]},
            {'versions': ['1'] * 51},
        ]
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                faceted_search(self.db, **kwargs)

    def test_legacy_search_is_unchanged(self):
        self.assertEqual(faceted_search(self.db, 'archive')['results'], search(self.db, 'archive'))
        self.assertEqual(search(self.db, ''), [])
        result = benchmark()
        self.assertEqual(len(result['queries']), 6)
        self.assertEqual(sum(q['all_expected_in_top_three'] for q in result['queries']), 5)


if __name__ == '__main__':
    unittest.main()


def test_evidence_search_page_filters_by_facets(tmp_path):
    from fastapi.testclient import TestClient
    from seo_qa.store import Store
    from seo_qa.web import create_app
    client = TestClient(create_app(Store(tmp_path / 'ui.sqlite')))
    page = client.get('/qa/evidence-search', params={'q': 'noindex'})
    assert page.status_code == 200 and 'Filter by source' in page.text and 'fixture:publisher-a' in page.text
    db = index(load())
    try:
        sources = [f['value'] for f in faceted_search(db, '')['facets']['sources']]
    finally:
        db.close()
    other = next(s for s in sources if s != 'fixture:publisher-a')
    filtered = client.get('/qa/evidence-search', params={'q': 'noindex', 'source': other})
    assert filtered.status_code == 200 and 'matching passage' in filtered.text
    assert 'A noindex directive requests exclusion' not in filtered.text
    browse = client.get('/qa/evidence-search', params={'source': 'fixture:publisher-a'})
    assert 'A noindex directive requests exclusion' in browse.text
    assert client.get('/qa/evidence-search', params={'offset': '-1'}).status_code in (400, 422)


def test_evidence_search_json_endpoint(tmp_path):
    from fastapi.testclient import TestClient
    from seo_qa.store import Store
    from seo_qa.web import create_app
    client = TestClient(create_app(Store(tmp_path / 'json.sqlite')))
    body = client.get('/qa/evidence-search.json', params={'q': 'noindex'}).json()
    assert body['results'][0]['id'] == 'robots' and body['facets']['sources']
    assert client.get('/qa/evidence-search.json', params={'q': 'x' * 600}).status_code == 400
    assert client.get('/qa/evidence-search.json', params={'limit': '1000'}).status_code == 400
