"""Drop-folder intake: sidecars, reproducibility, rejection and comparisons."""
import copy
import json

from seo_qa import provider_intake
from seo_qa.competitor import ROOT

EXAMPLE = json.loads((ROOT / 'examples/provider-export-synthetic.json').read_text())


def gsc_payload(start, end, clicks):
    payload = copy.deepcopy(EXAMPLE['payload'])
    payload['request'].update(startDate=start, endDate=end)
    payload['response']['rows'][0]['clicks'] = clicks
    return payload


def drop_raw(drop, name, payload, meta):
    folder = drop / 'inbox' / 'gsc'
    (folder / (name + '.json')).write_text(json.dumps(payload))
    if meta is not None:
        (folder / (name + '.meta.json')).write_text(json.dumps(meta))


def test_init_is_idempotent_and_creates_private_layout(tmp_path):
    provider_intake.init(tmp_path)
    (tmp_path / 'README.txt').write_text('mine')
    provider_intake.init(tmp_path)
    assert (tmp_path / 'README.txt').read_text() == 'mine'
    assert all((tmp_path / 'inbox' / p).is_dir() for p in provider_intake.PROVIDERS)


def test_run_normalizes_compares_and_never_touches_inbox(tmp_path):
    provider_intake.init(tmp_path)
    meta = {'exported_at': '2026-11-14T09:00:00-05:00', 'synthetic': True}
    drop_raw(tmp_path, 'oct', gsc_payload('2026-10-01', '2026-10-28', 14), meta)
    drop_raw(tmp_path, 'nov', gsc_payload('2026-10-29', '2026-11-25', 20),
             {'exported_at': '2026-11-26T09:00:00+00:00', 'synthetic': True})
    (tmp_path / 'inbox' / 'gsc' / 'envelope.json').write_text(json.dumps(EXAMPLE))
    before = {p.name: p.read_bytes() for p in (tmp_path / 'inbox' / 'gsc').iterdir()}
    report = provider_intake.run(tmp_path)
    assert {a['file'] for a in report['accepted']} == {'gsc/oct.json', 'gsc/nov.json', 'gsc/envelope.json'}
    assert report['rejected'] == [] and report['real_exports'] == 0
    clicks = [c for c in report['comparisons'] if c['metric'] == 'clicks'
              and c['before_period'] == ['2026-10-01', '2026-10-28'] and c['after_period'][0] == '2026-10-29']
    assert clicks and clicks[0]['delta'] == 6 and 'causal' in clicks[0]['interpretation']
    assert before == {p.name: p.read_bytes() for p in (tmp_path / 'inbox' / 'gsc').iterdir()}
    assert len(list((tmp_path / 'normalized').glob('*.json'))) == 3  # one per distinct export
    again = provider_intake.run(tmp_path)
    assert again['accepted'] == report['accepted']
    assert json.loads((tmp_path / 'INTAKE-REPORT.json').read_text())['accepted'] == report['accepted']


def test_missing_or_ambiguous_provenance_is_rejected(tmp_path):
    provider_intake.init(tmp_path)
    payload = gsc_payload('2026-10-01', '2026-10-28', 14)
    drop_raw(tmp_path, 'no-meta', payload, None)
    drop_raw(tmp_path, 'undeclared', payload, {'exported_at': '2026-11-14T09:00:00+00:00'})
    drop_raw(tmp_path, 'naive-time', payload, {'exported_at': '2026-11-14T09:00:00', 'synthetic': False})
    drop_raw(tmp_path, 'extra-field', payload, {'exported_at': '2026-11-14T09:00:00+00:00',
                                                'synthetic': False, 'token': 'x'})
    (tmp_path / 'inbox' / 'gsc' / 'broken.json').write_text('{not json')
    wrong = dict(EXAMPLE, provider='crux')
    (tmp_path / 'inbox' / 'gsc' / 'wrong-folder.json').write_text(json.dumps(wrong))
    report = provider_intake.run(tmp_path)
    assert report['accepted'] == []
    reasons = {r['file']: r['reason'] for r in report['rejected']}
    assert set(reasons) == {'gsc/no-meta.json', 'gsc/undeclared.json', 'gsc/naive-time.json',
                            'gsc/extra-field.json', 'gsc/broken.json', 'gsc/wrong-folder.json'}
    assert 'sidecar' in reasons['gsc/no-meta.json'].lower()
    assert 'timezone' in reasons['gsc/naive-time.json']
    report_text = (tmp_path / 'INTAKE-REPORT.json').read_text()
    assert 'reset guide' not in report_text and 'x' not in json.loads(report_text)['rejected'][0]


def test_real_and_synthetic_exports_are_never_compared(tmp_path):
    provider_intake.init(tmp_path)
    drop_raw(tmp_path, 'a', gsc_payload('2026-10-01', '2026-10-28', 14),
             {'exported_at': '2026-11-14T09:00:00+00:00', 'synthetic': True})
    drop_raw(tmp_path, 'b', gsc_payload('2026-10-29', '2026-11-25', 20),
             {'exported_at': '2026-11-26T09:00:00+00:00', 'synthetic': False})
    report = provider_intake.run(tmp_path)
    assert report['real_exports'] == 1 and report['comparisons'] == []


def test_symlinks_are_refused(tmp_path):
    provider_intake.init(tmp_path)
    outside = tmp_path / 'outside.json'
    outside.write_text(json.dumps(EXAMPLE))
    (tmp_path / 'inbox' / 'gsc' / 'link.json').symlink_to(outside)
    report = provider_intake.run(tmp_path)
    assert report['accepted'] == [] and report['rejected'][0]['reason'] == 'Not a regular file'
