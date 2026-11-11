import pytest
from seo_qa.community_store import CommunityStore
from seo_qa.release_recovery import export_state, restore_state


def payload(**changes):
    post = {'id': 'story-1', 'title': 'Private retained title', 'subreddit': 'fixture',
            'created_at': '2026-11-01T00:00:00Z', 'observed_at': '2026-11-02T00:00:00Z',
            'primary_sources': ['https://example.org/guidance'],
            'username': 'never-store-this', 'body': 'raw-body-never-store'}
    post.update(changes)
    return {'synthetic': True, 'posts': [post]}


def test_removal_scrubs_persistent_text_and_late_exports_cannot_restore(tmp_path):
    store = CommunityStore(tmp_path / 'community.sqlite')
    assert store.ingest(payload(), received_at='2026-11-03T00:00:00Z')['leads'][0]['synthetic'] is True
    removed = store.ingest(payload(removed=True), received_at='2026-11-03T00:00:00Z')
    assert removed['leads'] == [{'id': 'story-1', 'status': 'removed', 'text_retained': False}]
    reopened = CommunityStore(store.path)
    assert reopened.ingest(payload(observed_at='2026-11-04T00:00:00Z'), received_at='2026-11-04T00:00:00Z') == removed
    data = store.path.read_bytes()
    assert b'Private retained title' not in data
    assert b'never-store-this' not in data
    assert b'raw-body-never-store' not in data


def test_expiry_is_sticky_and_updates_do_not_extend_initial_retention(tmp_path):
    store = CommunityStore(tmp_path / 'community.sqlite')
    store.ingest(payload(), received_at='2026-11-02T00:00:00Z', retention_days=2)
    store.ingest(payload(title='Updated', observed_at='2026-11-03T00:00:00Z'), received_at='2026-11-03T00:00:00Z', retention_days=30)
    expired = store.snapshot(as_of='2026-11-04T00:00:00Z')
    assert expired['leads'][0]['status'] == 'expired'
    assert b'Updated' not in store.path.read_bytes()
    assert store.ingest(payload(observed_at='2026-11-05T00:00:00Z'), received_at='2026-11-05T00:00:00Z') == expired


def test_equal_time_conflict_rolls_back_entire_batch(tmp_path):
    store = CommunityStore(tmp_path / 'community.sqlite')
    expected = store.ingest(payload(), received_at='2026-11-03T00:00:00Z')
    batch = payload(title='Conflicting title')
    batch['posts'].insert(0, dict(payload()['posts'][0], id='new-story'))
    with pytest.raises(ValueError, match='Conflicting'):
        store.ingest(batch, received_at='2026-11-03T00:00:00Z')
    assert store.snapshot(as_of='2026-11-03T00:00:00Z') == expected


def test_out_of_order_update_ignored_but_old_removal_scrubs(tmp_path):
    store = CommunityStore(tmp_path / 'community.sqlite')
    first = store.ingest(payload(observed_at='2026-11-03T00:00:00Z'), received_at='2026-11-03T00:00:00Z')
    assert store.ingest(payload(title='Stale title'), received_at='2026-11-03T00:00:00Z') == first
    assert store.ingest(payload(removed=True), received_at='2026-11-03T00:00:00Z')['leads'][0]['status'] == 'removed'


@pytest.mark.parametrize('kwargs', [
    {'received_at': '2026-11-01T00:00:00Z'},
    {'received_at': '2026-11-03'},
    {'received_at': '2026-11-03T00:00:00Z', 'retention_days': True},
    {'received_at': '2026-11-03T00:00:00Z', 'retention_days': 91},
])
def test_invalid_policy_or_time_never_writes(tmp_path, kwargs):
    store = CommunityStore(tmp_path / 'community.sqlite')
    with pytest.raises(ValueError):
        store.ingest(payload(), **kwargs)
    assert store.snapshot(as_of='2026-11-03T00:00:00Z')['leads'] == []


def test_tombstones_and_private_normal_readback_survive_recovery(tmp_path):
    store = CommunityStore(tmp_path / 'community.sqlite')
    store.ingest(payload(), received_at='2026-11-03T00:00:00Z')
    expected = store.ingest(payload(removed=True), received_at='2026-11-03T00:00:00Z')
    bundle = tmp_path / 'bundle'
    export_state({'community': store.path}, {}, bundle, writers_stopped=True)
    restore_state(bundle, tmp_path / 'restored')
    restored = CommunityStore(tmp_path / 'restored/databases/community.sqlite')
    assert restored.ingest(payload(), received_at='2026-11-03T00:00:00Z') == expected


def test_cli_never_prints_retained_text_or_private_paths(tmp_path, capsys):
    import json
    from seo_qa.community_store import main
    source = tmp_path / 'private-input.json'
    source.write_text(json.dumps(payload()))
    args = ['--database', str(tmp_path / 'community.sqlite'), '--as-of', '2026-11-03T00:00:00Z']
    assert main(args + ['--input', str(source)]) == 0
    assert main(args) == 0
    assert main(args + ['--retention-days', '0', '--input', str(source)]) == 1
    output = capsys.readouterr().out
    assert 'Private retained title' not in output
    assert str(tmp_path) not in output
    assert '"network_calls": 0' in output


def test_private_permissions_and_symlink_database_refusal(tmp_path):
    store = CommunityStore(tmp_path / 'private/community.sqlite')
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.path.parent.stat().st_mode & 0o777 == 0o700
    alias = tmp_path / 'alias.sqlite'
    alias.symlink_to(store.path)
    with pytest.raises(ValueError, match='Symlink'):
        CommunityStore(alias)
