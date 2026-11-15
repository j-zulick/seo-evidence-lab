"""Guidance release: human approval gate, immutability, chain and impact replay."""
import json
import sqlite3

import pytest

from seo_qa import guidance_release as gr
from seo_qa.competitor import ROOT
from seo_qa.news_bridge import decide, import_saved, propose, report

FIX = ROOT / 'fixtures/news-bridge'
INPUTS = json.loads((FIX / 'audit-inputs.json').read_text())
AUTHOR = 'Synthetic proposer'


@pytest.fixture
def staged(tmp_path):
    db = tmp_path / 'inbox.sqlite'
    import_saved(db, (FIX / 'saved-email.json').read_bytes(), synthetic=True)
    version = next(x['version_id'] for x in report(db)['items'] if x['url'] == 'https://publisher.example/guide')
    proposal = propose(db, version, INPUTS, 'question_heading', AUTHOR, 'Synthetic mapping for tests.')
    return db, version, proposal


def test_full_chain_produces_verified_release_and_impact(staged):
    db, version, proposal = staged
    decide(db, version, 'accept_for_review', 'Reviewer A', 'Mapping justified for test.')
    release = gr.approve(db, proposal['id'], 'Reviewer A', 'Approved for test.')
    assert release['sequence'] == 1 and release['previous_release_id'] is None
    assert release['guidance'] == {'rule': 'question_heading'} and release['audits_changed'] is False
    assert release['identity_authenticated'] is False and release['source_synthetic'] is True
    assert gr.approve(db, proposal['id'], 'Reviewer A', 'Approved for test.') == release  # idempotent
    chain = gr.releases(db)
    assert chain['verified'] and chain['current']['id'] == release['id']
    question = INPUTS | {'article': '# How do I reset settings?\nOpen settings.'}
    impact = gr.affected(db, release['id'], [INPUTS, question])
    assert [(a['before'], a['after'], a['changed']) for a in impact['audits']] == [
        ('pass', 'miss', True), ('pass', 'pass', False)]
    assert impact['changed'] == 1 and 'unchanged' in impact['interpretation']


@pytest.mark.parametrize('action', ['reject', 'defer', None])
def test_release_requires_reviewers_latest_acceptance(staged, action):
    db, version, proposal = staged
    if action:
        decide(db, version, 'accept_for_review', 'Reviewer A', 'first look')
        decide(db, version, action, 'Reviewer A', 'changed my mind')
    with pytest.raises(gr.ReleaseError, match='accept_for_review'):
        gr.approve(db, proposal['id'], 'Reviewer A', 'note')


def test_another_persons_acceptance_does_not_count(staged):
    db, version, proposal = staged
    decide(db, version, 'accept_for_review', 'Reviewer B', 'ok')
    with pytest.raises(gr.ReleaseError, match='accept_for_review'):
        gr.approve(db, proposal['id'], 'Reviewer A', 'note')


def test_author_cannot_approve_own_proposal(staged):
    db, version, proposal = staged
    decide(db, version, 'accept_for_review', AUTHOR, 'self')
    with pytest.raises(gr.ReleaseError, match='differ'):
        gr.approve(db, proposal['id'], AUTHOR.upper(), 'self approve')


def test_releases_are_immutable_and_tampering_is_detected(staged):
    db, version, proposal = staged
    decide(db, version, 'accept_for_review', 'Reviewer A', 'ok')
    release = gr.approve(db, proposal['id'], 'Reviewer A', 'ok')
    conn = sqlite3.connect(db)
    for sql in ("UPDATE guidance_releases SET record='{}'", 'DELETE FROM guidance_releases'):
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            conn.execute(sql)
    conn.execute('DROP TRIGGER guidance_releases_no_update')
    tampered = dict(release, reviewer='Someone else')
    conn.execute('UPDATE guidance_releases SET record=?', (json.dumps(tampered),))
    conn.commit(); conn.close()
    with pytest.raises(gr.ReleaseError, match='verification'):
        gr.releases(db)


def test_second_release_links_to_first(staged, tmp_path):
    db, version, proposal = staged
    decide(db, version, 'accept_for_review', 'Reviewer A', 'ok')
    first = gr.approve(db, proposal['id'], 'Reviewer A', 'ok')
    second_prop = propose(db, version, INPUTS, 'heading_present', AUTHOR, 'Revert mapping for tests.')
    second = gr.approve(db, second_prop['id'], 'Reviewer A', 'ok again')
    assert second['sequence'] == 2 and second['previous_release_id'] == first['id']
    assert [r['id'] for r in gr.releases(db)['releases']] == [first['id'], second['id']]


def test_unknown_and_invalid_inputs_are_refused(staged):
    db, version, proposal = staged
    with pytest.raises(gr.ReleaseError, match='Unknown proposal'):
        gr.approve(db, 'f' * 64, 'Reviewer A', 'note')
    with pytest.raises(gr.ReleaseError, match='reviewer'):
        gr.approve(db, proposal['id'], ' ', 'note')
    decide(db, version, 'accept_for_review', 'Reviewer A', 'ok')
    release = gr.approve(db, proposal['id'], 'Reviewer A', 'ok')
    with pytest.raises(gr.ReleaseError):
        gr.affected(db, release['id'], [])
    with pytest.raises(gr.ReleaseError, match='Unknown guidance release'):
        gr.affected(db, 'e' * 64, [INPUTS])


def test_cli_lists_and_replays(staged, tmp_path, capsys):
    db, version, proposal = staged
    decide(db, version, 'accept_for_review', 'Reviewer A', 'ok')
    assert gr.main(['approve', '--db', str(db), '--proposal-id', proposal['id'],
                    '--reviewer', 'Reviewer A', '--note', 'ok']) == 0
    release_id = json.loads(capsys.readouterr().out)['id']
    audits = tmp_path / 'audits.json'
    audits.write_text(json.dumps([INPUTS]))
    assert gr.main(['affected', '--db', str(db), '--release-id', release_id, '--audits', str(audits)]) == 0
    assert json.loads(capsys.readouterr().out)['changed'] == 1
    assert gr.main(['list', '--db', str(db)]) == 0
    assert json.loads(capsys.readouterr().out)['verified'] is True
