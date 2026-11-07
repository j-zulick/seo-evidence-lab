"""Evaluation results bind canned outcomes and executable source, not cases alone."""
import hashlib
import json
from pathlib import Path

from seo_qa import timeline_eval
from seo_qa.competitor import digest


def test_report_binds_inputs_code_and_exact_export(tmp_path):
    report = timeline_eval.run(tmp_path)
    assert report['dataset_hash'] == digest(json.loads((timeline_eval.DATA/'cases.json').read_bytes()))
    provenance = report['provenance']
    assert provenance['shadow_sha256'] == hashlib.sha256((timeline_eval.DATA/'shadow.json').read_bytes()).hexdigest()
    for relative, expected in provenance['implementation_sha256'].items():
        actual = Path(timeline_eval.__file__).resolve().parents[1]/relative
        assert hashlib.sha256(actual.read_bytes()).hexdigest() == expected
    identity = report['result_hash']
    assert identity == digest({key: value for key, value in report.items() if key != 'result_hash'})
    assert json.loads((tmp_path/'experiment-comparison.json').read_bytes()) == report
    assert report['human_labels'] is None and report['live_calls'] == 0


def test_changed_shadow_changes_result_identity_without_relabeling_dataset(tmp_path, monkeypatch):
    inputs = tmp_path/'inputs'
    inputs.mkdir()
    for name in ('cases.json', 'shadow.json'):
        (inputs/name).write_bytes((timeline_eval.DATA/name).read_bytes())
    monkeypatch.setattr(timeline_eval, 'DATA', inputs)
    before = timeline_eval.run(tmp_path/'before')
    shadow = json.loads((inputs/'shadow.json').read_text())
    case = next(row for row in before['cases'] if row['grader'].startswith('simulated'))
    shadow['revised'][case['id']] = 'changed canned judgment'
    (inputs/'shadow.json').write_text(json.dumps(shadow))
    after = timeline_eval.run(tmp_path/'after')
    assert after['dataset_hash'] == before['dataset_hash']
    assert after['provenance']['cases_sha256'] == before['provenance']['cases_sha256']
    assert after['provenance']['shadow_sha256'] != before['provenance']['shadow_sha256']
    assert after['result_hash'] != before['result_hash']
    assert after['human_labels'] is None and after['live_calls'] == 0


def test_same_frozen_inputs_have_stable_identity(tmp_path):
    assert timeline_eval.run(tmp_path/'a')['result_hash'] == timeline_eval.run(tmp_path/'b')['result_hash']
