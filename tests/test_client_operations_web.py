import json
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from seo_qa.browser_auth import BrowserAuth, BrowserSettings, SESSION_COOKIE
from seo_qa.client_operations import ClientOperations
from seo_qa.client_operations_web import create_app
from seo_qa.fixtures import GOOD_HTML, FIXTURE_URL

OWNER = '11111111-1111-4111-8111-111111111111'
OTHER = '22222222-2222-4222-8222-222222222222'
ORIGIN = 'https://owner.example'

class OfflineSessions:
    def __init__(self, owner): self.owner, self.rows = owner, {}
    def sign_in(self, email, password):
        return {'owner_id': self.owner, 'access_token': 'fixture-private-access', 'expires_at': 2000}
    def session_create(self, token, digest, expires): self.rows[digest] = token
    def session_valid(self, token, digest): return self.rows.get(digest) == token
    def session_delete(self, token, digest): self.rows.pop(digest, None)

@pytest.fixture
def composed(tmp_path):
    ops = ClientOperations(tmp_path / 'control/clientops.sqlite')
    ops.create_workspace('one', authenticated_actor=OWNER, budget_units=20)
    ops.create_workspace('two', authenticated_actor=OTHER, budget_units=20)
    sessions = OfflineSessions(OWNER)
    auth = BrowserAuth(BrowserSettings('synthetic-browser-key-' + 'x' * 43, OWNER), sessions, clock=lambda:1000)
    cookie, _ = auth.sign_in('synthetic@example.test', 'synthetic-password')
    principal = auth.authenticate(cookie)
    app = create_app(ops, tmp_path / 'workspaces', browser_service=auth, origin=ORIGIN)
    client = TestClient(app, base_url=ORIGIN)
    client.cookies.set(SESSION_COOKIE, cookie)
    client.headers.update({'Origin':ORIGIN, 'X-CSRF-Token': principal.csrf_token})
    return ops, auth, sessions, principal, client, tmp_path


def post(client, workspace, action, key, payload):
    return client.post(f'/api/clientops/{workspace}/{action}', json={'request_key':key,'payload':payload})


def test_real_signed_cookie_and_session_revocation_fail_closed(composed):
    ops, auth, sessions, principal, client, root = composed
    assert client.get('/api/clientops/one').status_code == 200
    assert client.get('/api/clientops/two').status_code == 403
    assert client.get('/clientops/one').status_code == 200
    auth.logout(principal)
    assert client.get('/api/clientops/one').status_code == 401
    assert not (root / 'workspaces').exists()


def test_mutation_origin_csrf_identity_override_and_viewer_denials(composed):
    ops, auth, sessions, principal, client, root = composed
    body={'request_key':'first', 'payload':{'csv':'Query,Clicks\nhello,2\n'}}
    assert client.post('/api/clientops/one/import',json=body,headers={'X-CSRF-Token':'bad'}).status_code==403
    assert client.post('/api/clientops/one/import',json=body,headers={'Origin':'https://evil.example'}).status_code==403
    assert client.post('/api/clientops/one/import',json=body|{'authenticated_actor':OTHER}).status_code==400
    assert post(client,'two','import','first',body['payload']).status_code==403
    assert not (root/'workspaces').exists()


def test_actual_saved_audit_import_export_are_budgeted_scoped_and_secret_free(composed):
    ops, auth, sessions, principal, client, root = composed
    body={'title':'Saved local audit','url':FIXTURE_URL,'html':GOOD_HTML,'expected':{'canonical':FIXTURE_URL}}
    audited = post(client,'one','audit','audit-one',body)
    assert audited.status_code==200, audited.text
    run=audited.json()['result']['id']
    assert 'fixture-private-access' not in audited.text
    assert principal.csrf_token not in audited.text
    assert principal.session_hash not in audited.text
    assert '"token"' not in audited.text
    assert post(client,'one','audit','audit-one',body).json()==audited.json()
    imported=post(client,'one','import','import-one',{'csv':'Query,Clicks\nhello,2\n'})
    assert imported.status_code==200, imported.text
    exported=post(client,'one','report','report-one',{'run_id':run})
    assert exported.status_code==200, exported.text
    assert exported.json()['result']['id']==run
    assert ops.status('one',authenticated_actor=OWNER)['spent_units']==3
    assert ops.status('two',authenticated_actor=OTHER)['spent_units']==0
    assert not (root/'workspaces/two').exists()
    # A different valid signed-owner app still cannot read a foreign run ID.
    other_sessions=OfflineSessions(OTHER)
    other_auth=BrowserAuth(BrowserSettings('other-synthetic-key-'+'x'*43,OTHER),other_sessions,clock=lambda:1000)
    cookie,_=other_auth.sign_in('synthetic@example.test','synthetic-password')
    other=TestClient(create_app(ops,root/'workspaces',browser_service=other_auth,origin=ORIGIN),base_url=ORIGIN)
    other.cookies.set(SESSION_COOKIE,cookie)
    other.headers.update({'Origin':ORIGIN,'X-CSRF-Token':other_auth.authenticate(cookie).csrf_token})
    assert post(other,'two','report','foreign-run',{'run_id':run}).status_code==400
    assert other.get('/api/clientops/one').status_code==403


def test_current_single_owner_service_rejects_forged_identity_header(composed):
    *_,client,root = composed
    assert client.get('/api/clientops/two',headers={'X-Actor-ID':OTHER}).status_code==403
    client.cookies.clear()
    assert client.get('/api/clientops/one',headers={'X-Actor-ID':OWNER}).status_code==401


def test_paired_workspace_recovery_uses_filtered_control_and_normal_readback(composed):
    from seo_qa.client_workspace import WorkspaceExecutor
    ops, auth, sessions, principal, client, root = composed
    body={'title':'Saved local audit','url':FIXTURE_URL,'html':GOOD_HTML,'expected':{'canonical':FIXTURE_URL}}
    result=post(client,'one','audit','backup-audit',body)
    assert result.status_code==200
    run=result.json()['result']['id']
    executor=WorkspaceExecutor(ops,root/'workspaces')
    executor.backup('one',OWNER,root/'paired-backup',writers_stopped=True)
    assert OTHER.encode() not in (root/'paired-backup/control/databases/clientops.sqlite').read_bytes()
    WorkspaceExecutor.restore(root/'paired-backup',root/'paired-restored')
    restored_ops=ClientOperations(root/'paired-restored/clientops.sqlite')
    restored=WorkspaceExecutor(restored_ops,root/'paired-restored/workspaces')
    assert restored.execute('one',OWNER,'backup-audit','audit',body)==result.json()
    assert restored.store('one',OWNER).export_report(run)['id']==run
    with pytest.raises(PermissionError): restored_ops.status('two',authenticated_actor=OTHER)
    assert 'fixture-private-access' not in json.dumps(restored_ops.export('one',authenticated_actor=OWNER))
    with pytest.raises(PermissionError): executor.backup('one',OTHER,root/'denied',writers_stopped=True)
    assert not (root/'denied').exists()


def test_running_executor_reservation_cannot_be_released_by_competing_settlement(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from seo_qa.client_workspace import WorkspaceExecutor
    from seo_qa.store import Store
    ops=ClientOperations(tmp_path/'control.sqlite')
    ops.create_workspace('one',authenticated_actor=OWNER,budget_units=1,max_active=1)
    executor=WorkspaceExecutor(ops,tmp_path/'workspaces')
    entered, resume=Event(),Event()
    original=Store.save_import
    def paused(*args,**kwargs):
        entered.set()
        assert resume.wait(5)
        return original(*args,**kwargs)
    monkeypatch.setattr(Store,'save_import',paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(executor.execute,'one',OWNER,'single','import',{'csv':'Query,Clicks\nhello,2\n'})
        assert entered.wait(5)
        job=ops.export('one',authenticated_actor=OWNER)['jobs'][0]
        for state in ('cancelled','failed','complete'):
            with pytest.raises(PermissionError,match='ownership'):
                ops.finish('one',job['id'],authenticated_actor=OWNER,state=state,actual_units=0)
        with pytest.raises(ValueError,match='active job'):
            ops.admit('one',authenticated_actor=OWNER,request_key='second',kind='saved_import',input_hash='b'*64,reserve_units=1)
        assert ops.status('one',authenticated_actor=OWNER)['reserved_units']==1
        resume.set()
        assert future.result()['job']['state']=='complete'
    assert len(executor.store('one',OWNER).list_imports())==1
    assert ops.status('one',authenticated_actor=OWNER)['spent_units']==1
    assert ops.status('one',authenticated_actor=OWNER)['reserved_units']==0


def test_crashed_executor_guard_requires_explicit_stopped_owner_maintenance(tmp_path):
    ops=ClientOperations(tmp_path/'control.sqlite')
    ops.create_workspace('one',authenticated_actor=OWNER,budget_units=1)
    job=ops.admit('one',authenticated_actor=OWNER,request_key='single',kind='saved_import',input_hash='b'*64,reserve_units=1)
    ops.start('one',job['id'],authenticated_actor=OWNER,execution_key='synthetic-private-key-'+'x'*32)
    with pytest.raises(ValueError,match='Stop executor'):
        ops.remove_execution_guard_after_stop('one',job['id'],authenticated_actor=OWNER)
    ops.remove_execution_guard_after_stop('one',job['id'],authenticated_actor=OWNER,writers_stopped=True)
    assert ops.status('one',authenticated_actor=OWNER)['reserved_units']==1
    ops.finish('one',job['id'],authenticated_actor=OWNER,state='failed',actual_units=1)
    assert ops.status('one',authenticated_actor=OWNER)['spent_units']==1


def test_hash_valid_swapped_workspace_bundle_refused_before_install(tmp_path):
    import shutil
    from seo_qa.client_workspace import WorkspaceExecutor
    ops=ClientOperations(tmp_path/'control.sqlite')
    ops.create_workspace('one',authenticated_actor=OWNER,budget_units=2)
    ops.create_workspace('two',authenticated_actor=OTHER,budget_units=2)
    executor=WorkspaceExecutor(ops,tmp_path/'workspaces')
    body={'title':'Saved local audit','url':FIXTURE_URL,'html':GOOD_HTML,'expected':{'canonical':FIXTURE_URL}}
    executor.execute('one',OWNER,'audit-one','audit',body)
    executor.execute('two',OTHER,'audit-two','audit',body)
    executor.backup('one',OWNER,tmp_path/'backup-one',writers_stopped=True)
    executor.backup('two',OTHER,tmp_path/'backup-two',writers_stopped=True)
    shutil.rmtree(tmp_path/'backup-one/workspace')
    shutil.copytree(tmp_path/'backup-two/workspace',tmp_path/'backup-one/workspace')
    with pytest.raises(ValueError,match='owner mismatch'):
        WorkspaceExecutor.restore(tmp_path/'backup-one',tmp_path/'must-not-install')
    assert not (tmp_path/'must-not-install').exists()


def test_hash_valid_missing_result_locator_refused_before_install(tmp_path):
    import sqlite3
    from seo_qa.client_workspace import WorkspaceExecutor
    from seo_qa.release_recovery import export_state
    ops=ClientOperations(tmp_path/'control.sqlite')
    ops.create_workspace('one',authenticated_actor=OWNER,budget_units=2)
    executor=WorkspaceExecutor(ops,tmp_path/'workspaces')
    body={'title':'Saved local audit','url':FIXTURE_URL,'html':GOOD_HTML,'expected':{'canonical':FIXTURE_URL}}
    executor.execute('one',OWNER,'audit-one','audit',body)
    executor.backup('one',OWNER,tmp_path/'backup',writers_stopped=True)
    # Build a new internally hash-valid control bundle with a dangling result.
    import shutil
    edited=tmp_path/'edited.sqlite'
    shutil.copyfile(tmp_path/'backup/control/databases/clientops.sqlite',edited)
    with sqlite3.connect(edited) as db:
        db.execute('UPDATE client_results SET locator=?',(json.dumps({'run_id':'missing-run'}),))
    shutil.rmtree(tmp_path/'backup/control')
    export_state({'clientops':edited},{},tmp_path/'backup/control',writers_stopped=True)
    with pytest.raises(ValueError,match='Run unavailable'):
        WorkspaceExecutor.restore(tmp_path/'backup',tmp_path/'must-not-install')
    assert not (tmp_path/'must-not-install').exists()


def test_viewer_cannot_obtain_mutating_store_handle(tmp_path):
    from seo_qa.client_workspace import WorkspaceExecutor
    ops=ClientOperations(tmp_path/'control.sqlite')
    ops.create_workspace('one',authenticated_actor=OWNER)
    ops.set_member('one',authenticated_actor=OWNER,member='viewer',role='viewer')
    executor=WorkspaceExecutor(ops,tmp_path/'workspaces')
    with pytest.raises(PermissionError): executor.store('one','viewer')
    assert not (tmp_path/'workspaces').exists()
