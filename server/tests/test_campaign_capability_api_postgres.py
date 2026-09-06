"""ReciPDF API admission and receipt reconciliation on real PostgreSQL authority."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib
import json
import os
import subprocess
import threading
import time
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import build_receipts
import campaign_capability_api as api
import customization_service
from customization_models import ChangeState
import deps
import jobs
from routers import campaigns as router


class RecordingExecutor:
    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()

    def submit(self, *args, **kwargs):
        with self.lock:
            self.calls.append((args, kwargs))


class VerifiedTenant(str):
    pass


def _git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.PIPE).decode().strip()


def _publish_catalog(tenant, tmp_path):
    """Use the real Git candidate and customization publication store producer."""
    work = tmp_path / 'author'
    work.mkdir()
    _git(work, 'init', '-b', 'main')
    _git(work, 'config', 'user.email', 'capability-test@example.invalid')
    _git(work, 'config', 'user.name', 'Capability fixture')
    registry = work / 'registry.json'
    registry.write_text('{"tools":[]}', encoding='utf-8')
    _git(work, 'add', '.')
    _git(work, 'commit', '-m', 'Base catalog')
    base = _git(work, 'rev-parse', 'HEAD')
    tool = {'name': 'campaign-host-enrollment', 'entry': 'host.py',
            'description': 'Validate enrolled host readback', 'capabilities': ['campaign.host-enrollment'],
            'params': {}}
    (work / 'host.py').write_text(
        'def run(intake, params):\n'
        '    return {"verified": True, "operation_id": intake["operation_id"],\n'
        '            "input_sha256": intake["input_sha256"],\n'
        '            "readback_sha256": intake["host_readback"]["readback_sha256"]}\n', encoding='utf-8')
    registry.write_text(json.dumps({'tools': [tool]}), encoding='utf-8')
    digest = hashlib.sha256(registry.read_bytes()).hexdigest()
    _git(work, 'add', '.')
    _git(work, 'commit', '-m', 'Published host validator')
    commit = _git(work, 'rev-parse', 'HEAD')
    bare = tmp_path / 'bare'
    bare.mkdir()
    _git(work, 'clone', '--bare', str(work), str(bare / (tenant + '.git')))
    service = customization_service.CustomizationService.configured()
    store = service.store
    change = store.create_change_set(tenant_id=tenant, idempotency_key='create', base_commit=base,
        desired_platform_release='platform@sha256:abc', workspace_contract_digest='d' * 64,
        author_subject='auth0|fixture-author')
    change = store.transition(tenant_id=tenant, change_set_id=change.change_set_id,
        next_state=ChangeState.STAGING, expected_version=change.version, idempotency_key='staging')
    change = store.record_staged(tenant_id=tenant, change_set_id=change.change_set_id,
        expected_version=change.version, idempotency_key='staged', staged_commit=commit,
        catalog_digest=digest, platform_release='platform@sha256:abc', workspace_contract_digest='d' * 64)
    confirmation = str(uuid.uuid4())
    store.put_confirmation(confirmation_id=confirmation,
        payload={'tenant_id': tenant, 'change_set_id': change.change_set_id}, signature='fixture-signature')
    store.get_or_create_publication_request(tenant_id=tenant, change_set_id=change.change_set_id)
    store.bind_publication_confirmation(tenant_id=tenant, change_set_id=change.change_set_id,
                                       confirmation_id=confirmation)
    for state in (ChangeState.AWAITING_APPROVAL, ChangeState.APPROVED, ChangeState.PUBLISHING):
        change = store.transition(tenant_id=tenant, change_set_id=change.change_set_id,
            next_state=state, expected_version=change.version, idempotency_key=state.value,
            **({'approver_subject': 'auth0|fixture-approver'} if state == ChangeState.APPROVED else {}))
    return store.publish(tenant_id=tenant, change_set_id=change.change_set_id,
                         expected_version=change.version, idempotency_key='published')


@pytest.fixture
def postgres_authority(monkeypatch, tmp_path):
    # The verifier supplies the dedicated explicit database. Never discover another one.
    assert os.environ.get('DATABASE_URL'), 'Explicit DATABASE_URL is required'
    campaigns, capabilities, enrollment, db = api._platform()
    from leaf_platform import store
    db.apply_migration()
    monkeypatch.setenv('LEAF_JOBS_STORE', 'postgres')
    monkeypatch.setenv('LEAF_CUSTOMIZATION_STORE', 'postgres')
    monkeypatch.setenv('LEAF_SOURCE_SHA', 'a' * 40)
    monkeypatch.setenv('LEAF_BUILD_RECEIPTS_DIR', str(tmp_path / 'receipts'))
    monkeypatch.setenv('LEAF_TENANT_GIT_DIR', str(tmp_path / 'bare'))
    monkeypatch.setenv('LEAF_EFFECTIVE_TENANTS_DIR', str(tmp_path / 'effective'))
    machine = 'api-host-' + uuid.uuid4().hex
    monkeypatch.setenv('LEAF_CAMPAIGN_ALLOWED_MACHINES', machine)
    monkeypatch.setenv('LEAF_CAMPAIGN_HOST_MACHINE_ID', machine)
    monkeypatch.setenv('LEAF_CAMPAIGN_WORKER_SUBJECT', 'fixture-worker')
    monkeypatch.delenv('LEAF_CAMPAIGN_BRIDGE', raising=False)
    customization_service.reset_configured_services()
    org = store.create_org('ReciPDF API fixture')
    project = store.create_project(org.org_id, 'ReciPDF API project')
    subject = 'auth0|' + uuid.uuid4().hex
    principal = store.create_identity_binding(org.org_id, 'auth0', subject, role='owner')
    with db.connection() as conn:
        conn.execute('INSERT INTO project_member_bindings '
            '(membership_id,org_id,project_id,binding_id,role,invited_by_binding_id) '
            "VALUES (%s,%s,%s,%s,'owner',%s)",
            (uuid.uuid4(), org.org_id, project.project_id, principal.binding_id, principal.binding_id))
    tenant = VerifiedTenant(str(org.org_id))
    tenant.subject = subject
    campaign = campaigns.submit_campaign(org.org_id, project.project_id, str(tenant), principal.binding_id,
        title='ReciPDF', prompt='Build recipe PDF workspace', idempotency_key='campaign')
    scope = (str(org.org_id), str(project.project_id), campaign['campaign_id'])
    repository = store.ensure_project_repository_authority(str(tenant), scope[0], scope[1])
    row = enrollment.request_enrollment(*scope, principal.binding_id, machine_id=machine)
    eid = row['enrollment_id']
    enrollment.enable_enrollment(*scope, eid, principal.binding_id)
    pin = _publish_catalog(str(tenant), tmp_path)
    recorder = RecordingExecutor()
    monkeypatch.setattr(jobs, '_executors', {jobs.LANE_FAST: recorder, jobs.LANE_SLOW: recorder})
    monkeypatch.setattr(jobs, '_reaper_started', True)
    app = FastAPI()
    app.include_router(router.router)
    app.dependency_overrides[deps.require_tenant] = lambda: tenant
    app.dependency_overrides[deps.require_campaign_worker] = lambda: 'fixture-worker'
    router.set_store(None)
    router.set_enrollment_store(None)
    with TestClient(app) as client:
        base = f'/api/campaigns/{scope[2]}/enrollments/{eid}'
        response = client.post(base + '/publication', json={
            'project_id': scope[1], 'change_set_id': pin.change_set_id})
        assert response.status_code == 200, response.text
        yield dict(client=client, db=db, capabilities=capabilities, enrollment=enrollment,
                   scope=scope, eid=eid, principal=principal.binding_id, pin=pin,
                   tenant=tenant, base=base, recorder=recorder, repository=repository)
    customization_service.reset_configured_services()


def _invoke(f, key, digest=None):
    return f['client'].post(f['base'] + '/invoke', json={
        'project_id': f['scope'][1], 'effective_catalog_digest': digest or f['pin'].catalog_digest},
        headers={'Idempotency-Key': key})


def _snapshot(f):
    response = f['client'].get(f'/api/campaigns/{f["scope"][2]}/enrollments',
                             params={'project_id': f['scope'][1]})
    assert response.status_code == 200, response.text
    row, = response.json()['enrollment']['enrollments']
    return row


def _finish(f, job_id, *, receipt=True, failed=False):
    context = f['capabilities'].invocation_context(*f['scope'], f['eid'], f['principal'])
    f['capabilities'].ensure_operation(job_id, context)
    response = f['client'].post('/internal/campaigns/bridge/host_op', json={})
    assert response.status_code == 200, response.text
    op = response.json()['operation']
    assert op['job_id'] == job_id
    resumed = f['client'].post('/internal/campaigns/bridge/host_op', json={
        'operation_id': op['operation_id'], 'claim': op['claim']})
    assert resumed.status_code == 200
    grant_response = f['client'].post('/internal/campaigns/bridge/host_grant', json={
        'operation_id': op['operation_id'], 'claim': op['claim']})
    assert grant_response.status_code == 200, grant_response.text
    assert grant_response.json() == {'ok': True, 'kind': 'grant', 'grant': {
        'operation_id': op['operation_id'], 'enrollment_id': f['eid'], 'link_id': op['link_id'],
        'machine_id': op['machine_id'], 'campaign_id': f['scope'][2], 'leaf_project_id': f['scope'][1],
        'repository_key': f['repository']['repo_key'], 'input_sha256': op['input_sha256']}}
    for stage in ('apply', 'activate', 'readback'):
        body = {key: op[key] for key in ('operation_id', 'attempt', 'fence', 'claim', 'input_sha256')}
        body.update(stage=stage, outcome='failed' if failed else 'succeeded', evidence={
            'config_identity_before': '1' * 64, 'config_identity_after': '2' * 64,
            'readback_sha256': '3' * 64, 'reason': 'validation_failed' if failed else 'verified'})
        settled = f['client'].post('/internal/campaigns/bridge/host_settle', json=body)
        assert settled.status_code == 200, settled.text
        assert 'claim' not in settled.text
        if failed:
            break
    # The recording executor leaves execution under fixture control. Terminal state
    # is real durable state; the actual producer below derives its proof from it.
    with f['db'].connection() as conn:
        conn.execute('UPDATE async_jobs SET status=%s, progress=%s, finished_at=%s, elapsed_ms=10 '
                     'WHERE job_id=%s', ('failed' if failed else 'complete', 'done', time.time(), job_id))
    if receipt:
        assert build_receipts.write_terminal_receipt(jobs.get_job(job_id)) is not None
        actual = build_receipts.read_terminal_receipt(job_id)
        assert actual is not None and actual['capability_provenance'] == context
    return context


def test_real_postgres_capability_api_two_uses_and_unknown_outcome(postgres_authority, monkeypatch):
    f = postgres_authority
    assert _snapshot(f)['completed_uses'] == 0
    key = str(uuid.uuid4())
    submit = jobs.submit_job
    def lose_response(*args, **kwargs):
        submit(*args, **kwargs)
        raise RuntimeError('lost response, never expose this text')
    monkeypatch.setattr(jobs, 'submit_job', lose_response)
    response = _invoke(f, key)
    assert response.status_code == 200, response.text
    first = response.json()['invocation']['job_id']
    monkeypatch.setattr(jobs, 'submit_job', submit)
    assert len(f['recorder'].calls) == 1
    queued = _snapshot(f)
    assert queued['completed_uses'] == 0
    assert queued['invocations'][0]['job_id'] == first
    assert queued['invocations'][0]['status'] == 'submitted'
    with f['db'].cursor() as cur:
        cur.execute('SELECT count(*) AS n FROM campaign_capability_invocations WHERE job_id=%s', (first,))
        assert cur.fetchone()['n'] == 0
    _finish(f, first)
    assert _snapshot(f)['completed_uses'] == 1
    assert _invoke(f, key).json()['invocation']['job_id'] == first
    assert _snapshot(f)['completed_uses'] == 1
    f['db'].reset_pool()
    importlib.reload(api)
    assert _invoke(f, key).json()['invocation']['job_id'] == first
    second_response = _invoke(f, str(uuid.uuid4()))
    assert second_response.status_code == 200, second_response.text
    second = second_response.json()['invocation']['job_id']
    assert second != first
    _finish(f, second)
    completed = _snapshot(f)
    assert completed['completed_uses'] == 2
    assert completed['capability_link']['state'] == 'completed'
    assert completed['capability_link']['first_invocation_receipt_id'] == build_receipts.read_terminal_receipt(first)['digest']
    assert completed['capability_link']['second_invocation_receipt_id'] == build_receipts.read_terminal_receipt(second)['digest']
    assert all(row['counted'] for row in completed['invocations'])
    # Admitted-key recovery is independent of a later catalog generation and revoke.
    with f['db'].connection() as conn:
        conn.execute('UPDATE effective_catalogs SET catalog_digest=%s WHERE tenant_id=%s', ('f' * 64, str(f['tenant'])))
    assert _invoke(f, key).json()['invocation']['job_id'] == first
    conflict = _invoke(f, key, 'e' * 64)
    assert conflict.status_code == 409 and conflict.json()['error']['error_code'] == 'idempotency_conflict'
    f['enrollment'].revoke_enrollment(*f['scope'], f['eid'], f['principal'])
    assert _invoke(f, key).json()['invocation']['job_id'] == first
    assert _snapshot(f)['completed_uses'] == 2
    assert 'claim' not in json.dumps(_snapshot(f))


def test_fresh_stale_key_has_no_admission_and_busy_retains_key(postgres_authority):
    f = postgres_authority
    key = str(uuid.uuid4())
    response = _invoke(f, key, 'e' * 64)
    assert response.status_code == 409 and response.json()['error']['error_code'] == 'catalog_drift'
    stored_key = api._key(f['scope'][2], f['eid'], key)
    assert api._lookup(str(f['tenant']), f['scope'][1], stored_key) is None
    assert not f['recorder'].calls
    with api._admission_lock(str(f['tenant']), f['scope'][0], f['scope'][1], stored_key):
        busy = _invoke(f, key)
        assert busy.status_code == 409 and busy.json()['error']['error_code'] == 'invocation_pending'
        assert not f['recorder'].calls
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: _invoke(f, key), range(2)))
    assert all(r.status_code in (200, 409) for r in responses)
    for response in responses:
        if response.status_code == 409:
            assert response.json()['error']['error_code'] == 'invocation_pending'
    recovered = _invoke(f, key)
    assert recovered.status_code == 200, recovered.text
    assert len(f['recorder'].calls) == 1
    assert {r.json()['invocation']['job_id'] for r in responses if r.status_code == 200} <= {
        recovered.json()['invocation']['job_id']}


def test_missing_failed_foreign_receipts_and_scoped_key_conflict(postgres_authority):
    f = postgres_authority
    first_response = _invoke(f, str(uuid.uuid4()))
    assert first_response.status_code == 200, first_response.text
    first = first_response.json()['invocation']['job_id']
    _finish(f, first, receipt=False)
    assert _snapshot(f)['completed_uses'] == 0
    assert _snapshot(f)['invocations'][0]['reason'] == 'Receipt unavailable'
    failed_response = _invoke(f, str(uuid.uuid4()))
    assert failed_response.status_code == 200, failed_response.text
    failed = failed_response.json()['invocation']['job_id']
    _finish(f, failed, failed=True)
    assert _snapshot(f)['completed_uses'] == 0
    # An actual digest-valid receipt for another job is still foreign proof.
    target = build_receipts.receipts_dir() / first / 'receipt.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes((build_receipts.receipts_dir() / failed / 'receipt.json').read_bytes())
    assert _snapshot(f)['completed_uses'] == 0
    target.write_text('{corrupt', encoding='utf-8')
    assert _snapshot(f)['completed_uses'] == 0
    key = str(uuid.uuid4())
    response = _invoke(f, key)
    assert response.status_code == 200, response.text
    foreign = response.json()['invocation']['job_id']
    with f['db'].connection() as conn:
        conn.execute("UPDATE async_jobs SET execution_json=jsonb_set(execution_json, "
                     "'{capability_provenance,link_id}', to_jsonb(%s::text)) WHERE job_id=%s",
                     (str(uuid.uuid4()), foreign))
    conflict = _invoke(f, key)
    assert conflict.status_code == 409 and conflict.json()['error']['error_code'] == 'idempotency_conflict'
    assert foreign not in conflict.text
