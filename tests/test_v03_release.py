import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from seo_qa.competitor import audit, compare, digest, finding
from seo_qa.evidence_search import load
from seo_qa.release_benchmark import run
from seo_qa.selected_dataset import SelectedDataset


def inputs(article, rule='required_phrase'):
    return {'article': article, 'guidance': {'rule': rule, 'phrase': 'Evidence trail'}, 'scoring': 'content/v1', 'evidence': True}


def dataset():
    return {'schema': 'seo-evidence-dataset/v1', 'label': 'Synthetic release checks',
            'audits': [audit(inputs('Original text')), audit(inputs('Retain the evidence trail.'))],
            'passages': load()['passages']}


def test_new_replay_criteria_and_counterfactuals():
    old, new = inputs('Original text'), inputs('Retain the evidence trail.')
    result = compare(old, new)
    assert result['before']['finding'] == 'miss'
    assert result['after']['finding'] == 'pass'
    assert result['minimal_sufficient_changes'] == [('article',)]
    assert audit(new) == audit(deepcopy(new))
    assert finding(new | {'evidence': False}) == 'unmeasured'
    assert finding(new | {'scoring': 'content/v99'}) == 'unresolved'
    assert finding(new | {'guidance': {'rule': 'required_phrase', 'phrase': ''}}) == 'unresolved'
    assert finding(new | {'guidance': {'rule': 'question_heading'}}) == 'unresolved'
    assert finding(inputs('[Detailed evidence](https://example.test/doc)', 'descriptive_link')) == 'pass'
    for article in ['[Click here](https://example.test/doc)', '[ ](https://example.test/doc)', '[Evidence](http://)', '[Evidence](https://user:pass@example.test)', '![Figure](https://example.test/img)', '[Evidence](javascript:alert(1))', 'No links']:
        assert finding(inputs(article, 'descriptive_link')) == 'miss'


def test_selected_dataset_hash_replay_and_no_aliasing(tmp_path):
    payload = dataset()
    selected = SelectedDataset(payload, digest(payload))
    before, after = [a['id'] for a in payload['audits']]
    payload['audits'][0]['finding'] = 'pass'
    assert selected.evidence(before)['audit']['finding'] == 'miss'
    read = selected.evidence(before)
    read['audit']['finding'] = 'pass'
    assert selected.evidence(before)['audit']['finding'] == 'miss'
    assert selected.comparison(before, after)['comparison']['status'] == 'explained'
    assert selected.search('reactions')['results'][0]['id'] == 'speed'
    for bad in ['../private', 'unknown', '']:
        with pytest.raises(ValueError):
            selected.evidence(bad)
    with pytest.raises(ValueError):
        SelectedDataset(dataset(), '0' * 64)
    tampered = dataset()
    tampered['audits'][0]['finding'] = 'pass'
    with pytest.raises(ValueError):
        SelectedDataset(tampered, digest(tampered))
    duplicate = dataset()
    duplicate['passages'].append(duplicate['passages'][0])
    with pytest.raises(ValueError):
        SelectedDataset(duplicate, digest(duplicate))
    missing = tmp_path / 'absent.json'
    with pytest.raises(ValueError):
        SelectedDataset.from_file(missing, '0' * 64)
    assert not missing.exists()
    with pytest.raises(ValueError):
        selected.search('x' * 513)


def test_expanded_benchmark_original_miss_and_heldout_no_regressions():
    report = run()
    original = report['groups']['original_six']
    assert original['baseline_passed'] == 5
    assert original['expanded_passed'] == 6
    for group in report['groups'].values():
        for case in group['cases']:
            assert not case['baseline']['passed'] or case['expanded']['passed']


def test_selected_dataset_stdio_roundtrip(tmp_path):
    payload = dataset()
    path = tmp_path / 'selected.json'
    path.write_text(json.dumps(payload))
    original_bytes = path.read_bytes()

    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'seo_qa.selected_dataset_mcp'],
                cwd=str(Path(__file__).resolve().parents[1]),
                env={**os.environ, 'SEO_QA_DATASET': str(path), 'SEO_QA_DATASET_SHA256': digest(payload)})
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                assert len(tools) == 4
                assert all(t.annotations.readOnlyHint and not t.annotations.destructiveHint for t in tools)

                async def call(name, arguments):
                    response = await session.call_tool(name, arguments)
                    assert not response.isError
                    return json.loads(response.content[0].text)

                manifest = await call('get_dataset_manifest', {})
                assert manifest['dataset_sha256'] == digest(payload)
                before, after = manifest['audit_ids']
                assert (await call('get_selected_finding', {'audit_id': before}))['audit']['finding'] == 'miss'
                assert (await call('compare_selected_audits', {'before_id': before, 'after_id': after}))['comparison']['status'] == 'explained'
                hits = await call('search_selected_passages', {'query': 'reactions'})
                assert hits['results'][0]['source'] == 'fixture:publisher-c'
                assert (await session.call_tool('get_selected_finding', {'audit_id': '../../private'})).isError

    asyncio.run(exercise())
    assert path.read_bytes() == original_bytes


def test_selected_snapshot_is_detached_from_exposed_payload():
    payload = dataset()
    selected = SelectedDataset(payload, digest(payload))
    original_hash = selected.sha256
    original_results = selected.search('reactions')
    selected.payload['passages'][0]['text'] = 'replacement content'
    payload['passages'][0]['text'] = 'replacement input'
    assert selected.search('reactions') == original_results
    assert digest(selected.payload) == original_hash
    assert not selected.search('replacement')['results']
    with pytest.raises(AttributeError):
        selected.sha256 = '0' * 64
