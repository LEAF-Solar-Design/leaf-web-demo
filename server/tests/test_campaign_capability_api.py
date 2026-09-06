"""ReciPDF browser boundaries and publication producer contract."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
from types import SimpleNamespace
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import campaign_capability_api as api
import customization_service as customization
from customization_models import ChangeState, ChangeSetNotFoundError
import deps
import tool_loader
from routers import campaigns


ORG, PROJECT, CAMPAIGN, ENROLLMENT, PRINCIPAL, LINK = [str(uuid.uuid4()) for _ in range(6)]
TOOL = {'name': 'campaign-host-enrollment', 'entry': 'host.py', 'params': {}}


@pytest.fixture
def authority(monkeypatch):
    pin = SimpleNamespace(tenant_id=ORG, change_set_id=str(uuid.uuid4()),
                          catalog_commit='a' * 40, catalog_digest='b' * 64)
    change = SimpleNamespace(**vars(pin), state=ChangeState.PUBLISHED, staged_commit=pin.catalog_commit)
    service = SimpleNamespace(store=SimpleNamespace(
        get_effective_catalog=lambda **k: pin, get_change_set=lambda **k: change),
        _staged_tool=lambda value: deepcopy(TOOL))
    monkeypatch.setattr(customization.CustomizationService, 'configured', lambda: service)
    monkeypatch.setattr(customization, 'effective_catalog_pin', lambda tenant: {
        'catalog_commit': pin.catalog_commit, 'effective_catalog_digest': pin.catalog_digest})
    monkeypatch.setattr(deps, 'effective_tools_with_provenance',
                        lambda tenant: [(deepcopy(TOOL), deps.TOOL_SOURCE_TENANT_REPO)])
    monkeypatch.setattr(tool_loader, 'published_tool_source_sha256', lambda *a: 'c' * 64)
    monkeypatch.setattr(api.platform_link, 'resolve_caller_binding', lambda tenant: SimpleNamespace(
        binding_id=PRINCIPAL, platform_tenant_id=ORG))
    def access(tenant, project, *, write, binding):
        assert write and binding.binding_id == PRINCIPAL
        if project != PROJECT:
            raise api.platform_link.ProjectSessionForbidden('private')
        return ORG
    monkeypatch.setattr(api.platform_link, 'require_project_access', access)
    platform = api._platform()
    monkeypatch.setattr(platform[0], 'get_campaign', lambda *a: {'tenant_id': ORG})
    return pin, change, service


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(campaigns.router)
    app.dependency_overrides[deps.require_tenant] = lambda: ORG
    with TestClient(app) as client:
        yield client


def test_publication_has_three_distinct_digests_and_bounded_list(authority):
    pin, _, _ = authority
    publication, tool = api._publication(ORG)
    assert publication['effective_catalog_digest'] == pin.catalog_digest
    assert publication['tool_manifest_sha256'] == deps.catalog_tool_digest(tool)
    assert publication['tool_source_sha256'] == 'c' * 64
    assert len(set(publication[k] for k in ('effective_catalog_digest', 'tool_manifest_sha256',
                                          'tool_source_sha256'))) == 3
    row, = api.list_capabilities(ORG, PROJECT, CAMPAIGN)
    assert set(row) == {'change_set_id', 'tool_name', 'label', 'catalog_commit', 'effective_catalog_digest'}


@pytest.mark.parametrize('mutation', ['foreign_pin', 'foreign_change', 'unpublished', 'commit',
                                    'digest', 'winner', 'source', 'stale'])
def test_unavailable_authoritative_catalog(authority, monkeypatch, mutation):
    pin, change, _ = authority
    if mutation == 'foreign_pin':
        pin.tenant_id = 'foreign'
    elif mutation == 'foreign_change':
        change.tenant_id = 'foreign'
    elif mutation == 'unpublished':
        change.state = ChangeState.STAGED
    elif mutation == 'commit':
        change.staged_commit = 'd' * 40
    elif mutation == 'digest':
        change.catalog_digest = 'd' * 64
    elif mutation == 'winner':
        monkeypatch.setattr(deps, 'effective_tools_with_provenance', lambda t: [(TOOL, 'authored')])
    elif mutation == 'source':
        monkeypatch.setattr(tool_loader, 'published_tool_source_sha256', lambda *a: None)
    else:
        monkeypatch.setattr(customization, 'effective_catalog_pin', lambda t: None)
    with pytest.raises(api.CapabilityError) as exc:
        api.list_capabilities(ORG, PROJECT, CAMPAIGN)
    assert exc.value.status == 503 and str(exc.value) == 'Campaign capability request failed'


def test_missing_or_unrelated_publication_is_empty(authority):
    _, _, service = authority
    service._staged_tool = lambda change: {'name': 'another-tool'}
    assert api.list_capabilities(ORG, PROJECT, CAMPAIGN) == []
    def missing(**kwargs):
        raise ChangeSetNotFoundError('private')
    service.store.get_effective_catalog = missing
    assert api.list_capabilities(ORG, PROJECT, CAMPAIGN) == []


def test_actual_source_normalization_and_containment(tmp_path, monkeypatch):
    source = tmp_path / 'host.py'
    source.write_bytes(b'def run(a, b):\r\n    return {}\r')
    monkeypatch.setattr(tool_loader, '_tenant_repo_root', lambda tenant: tmp_path)
    assert tool_loader.published_tool_source_sha256(TOOL, ORG) == hashlib.sha256(
        b'def run(a, b):\n    return {}\n').hexdigest()
    assert tool_loader.published_tool_source_sha256({**TOOL, 'entry': '../outside.py'}, ORG) is None
    source.write_bytes(b'\xff')
    assert tool_loader.published_tool_source_sha256(TOOL, ORG) is None


def test_explicit_routes_precede_generic_and_do_not_accept_proof(client, monkeypatch):
    calls = []
    monkeypatch.setattr(api, 'bind_publication', lambda *a: calls.append(a) or {'enrollment_id': ENROLLMENT})
    monkeypatch.setattr(api, 'invoke', lambda *a: calls.append(a) or {
        'job_id': str(uuid.uuid4()), 'status': 'submitted', 'progress': 'Queued'})
    base = f'/api/campaigns/{CAMPAIGN}/enrollments/{ENROLLMENT}'
    for op, body in [('publication', {'project_id': PROJECT, 'change_set_id': 'change'}),
                     ('invoke', {'project_id': PROJECT, 'effective_catalog_digest': 'a' * 64})]:
        assert client.post(base + '/' + op, json=body, headers={'Idempotency-Key': str(uuid.uuid4())}).status_code == 200
        for key in ('tenant_id', 'org_id', 'principal', 'machine_id', 'tool', 'receipt', 'capability_provenance'):
            assert client.post(base + '/' + op, json={**body, key: 'private'},
                               headers={'Idempotency-Key': 'key'}).status_code == 400
        raw = '{"project_id":"' + PROJECT + '","project_id":"' + PROJECT + '"}'
        assert client.post(base + '/' + op, content=raw).status_code == 400
        assert client.post(base + '/' + op, content=' ' * 4097).status_code == 400
    assert len(calls) == 2


@pytest.mark.parametrize('key', ['', 'x' * 129, 'key\tbad', 'key\x7f'])
def test_invalid_key_before_admission(client, monkeypatch, key):
    monkeypatch.setattr(api, 'invoke', lambda *a: pytest.fail('admission entered'))
    response = client.post(f'/api/campaigns/{CAMPAIGN}/enrollments/{ENROLLMENT}/invoke',
        json={'project_id': PROJECT, 'effective_catalog_digest': 'a' * 64}, headers={'Idempotency-Key': key})
    assert response.status_code == 400


def test_writer_denial_is_safe(authority, client):
    for route in ('capabilities', 'enrollments'):
        response = client.get(f'/api/campaigns/{CAMPAIGN}/{route}?project_id={uuid.uuid4()}')
        assert response.status_code == 403
        assert 'private' not in response.text


@pytest.mark.parametrize('op,method,body', [
    ('host_op', 'claim_host_operation', {}),
    ('host_settle', 'settle_host_operation', {}),
    ('host_grant', 'read_host_grant', {'operation_id': str(uuid.uuid4()), 'claim': 'x' * 43}),
])
def test_worker_mount_bootstraps_bridge_off(client, monkeypatch, op, method, body):
    capabilities = api._platform()[1]
    response_body = {'ok': True, 'kind': 'idle'}
    calls = []
    monkeypatch.setattr(capabilities, method,
                        lambda subject, value: calls.append((subject, value)) or response_body, raising=False)
    monkeypatch.delenv('LEAF_CAMPAIGN_BRIDGE', raising=False)
    # Use the actual dependency before providing the authenticated test subject.
    assert client.post('/internal/campaigns/bridge/' + op, json=body).status_code in (401, 403, 503)
    assert not calls
    client.app.dependency_overrides[deps.require_campaign_worker] = lambda: 'worker-service'
    result = client.post('/internal/campaigns/bridge/' + op, json=body)
    assert result.status_code == 200 and result.json() == response_body
    assert calls == [('worker-service', body)]
    assert client.post('/internal/campaigns/bridge/next', json={'enrollment_id': ENROLLMENT}).status_code == 503
    assert client.post('/internal/campaigns/bridge/' + op, content='{"claim":"x","claim":"y"}').status_code == 400


@pytest.mark.parametrize('op,method', [('host_op', 'claim_host_operation'),
                                    ('host_settle', 'settle_host_operation'), ('host_grant', 'read_host_grant')])
@pytest.mark.parametrize('kind,status', [('invalid', 400), ('forbidden', 403), ('conflict', 409), ('unavailable', 503)])
def test_host_errors_never_leak_claim(client, monkeypatch, op, method, kind, status):
    capabilities = api._platform()[1]
    error = {'invalid': capabilities.CampaignError('invalid_request', 'secret-claim'),
             'forbidden': capabilities.CampaignError('worker_forbidden', 'secret-claim'),
             'conflict': capabilities.CampaignConflict('stale', 'secret-claim'),
             'unavailable': capabilities.CampaignUnavailable('missing', 'secret-claim')}[kind]
    def fail(*args):
        raise error
    monkeypatch.setattr(capabilities, method, fail, raising=False)
    client.app.dependency_overrides[deps.require_campaign_worker] = lambda: 'worker-service'
    result = client.post('/internal/campaigns/bridge/' + op, json={})
    assert result.status_code == status and 'secret-claim' not in result.text


@pytest.mark.parametrize('op,body', [
    ('host_op', {'enrollment_id': ENROLLMENT}),
    ('host_op', {'operation_id': ENROLLMENT, 'claim': 'bad'}),
    ('host_grant', {}),
    ('host_grant', {'operation_id': ENROLLMENT, 'claim': 'x' * 43, 'machine_id': 'VM-C'}),
    ('host_grant', {'operation_id': ENROLLMENT, 'claim': 'bad'}),
    ('host_settle', {'operation_id': ENROLLMENT, 'claim': 'x' * 43}),
])
def test_actual_host_closed_wire_before_storage(client, monkeypatch, op, body):
    api._platform()
    monkeypatch.setenv('LEAF_CAMPAIGN_WORKER_SUBJECT', 'worker-service')
    monkeypatch.setenv('LEAF_CAMPAIGN_ALLOWED_MACHINES', 'VM-C')
    monkeypatch.setenv('LEAF_CAMPAIGN_HOST_MACHINE_ID', 'VM-C')
    client.app.dependency_overrides[deps.require_campaign_worker] = lambda: 'worker-service'
    response = client.post('/internal/campaigns/bridge/' + op, json=body)
    assert response.status_code == 400


def test_publication_binding_replays_and_conflicts(authority, monkeypatch):
    pin, _, _ = authority
    capabilities = api._platform()[1]
    stored = []
    def bind(*args, publication):
        assert args == (ORG, PROJECT, CAMPAIGN, ENROLLMENT, PRINCIPAL)
        assert set(publication) == set(capabilities.PUBLICATION)
        if stored and publication != stored[0]:
            raise capabilities.CampaignConflict('capability_conflict', 'private')
        replayed = bool(stored)
        stored.append(dict(publication))
        return {'enrollment_id': ENROLLMENT, 'replayed': replayed}
    monkeypatch.setattr(capabilities, 'bind_publication', bind)
    assert not api.bind_publication(ORG, PROJECT, CAMPAIGN, ENROLLMENT, pin.change_set_id)['replayed']
    assert api.bind_publication(ORG, PROJECT, CAMPAIGN, ENROLLMENT, pin.change_set_id)['replayed']
    monkeypatch.setattr(tool_loader, 'published_tool_source_sha256', lambda *a: 'e' * 64)
    with pytest.raises(api.CapabilityError) as error:
        api.bind_publication(ORG, PROJECT, CAMPAIGN, ENROLLMENT, pin.change_set_id)
    assert error.value.code == 'publication_conflict'


@pytest.fixture
def invocation_edges(authority, monkeypatch):
    capabilities = api._platform()[1]
    publication, tool = api._publication(ORG)
    context = dict(capabilities.CONSTANTS, **publication, tenant_id=ORG, org_id=ORG,
                   project_id=PROJECT, campaign_id=CAMPAIGN, enrollment_id=ENROLLMENT, link_id=LINK)
    monkeypatch.setattr(api, '_stored_context', lambda *a: deepcopy(context))
    monkeypatch.setattr(capabilities, 'invocation_context', lambda *a: deepcopy(context))
    held = []
    @contextmanager
    def lock(*args):
        held.append(True)
        try:
            yield
        finally:
            held.pop()
    monkeypatch.setattr(api, '_admission_lock', lock)
    durable = []
    def lookup(*args):
        assert held
        return durable[0] if durable else None
    monkeypatch.setattr(api, '_lookup', lookup)
    return context, tool, durable, held


def test_admitted_recovery_precedes_catalog_and_changed_digest_conflicts(invocation_edges, monkeypatch):
    import jobs
    context, _, durable, _ = invocation_edges
    job_id = str(uuid.uuid4())
    durable.append(dict(job_id=job_id, tenant_id=ORG, org_id=ORG, project_id=PROJECT,
                        tool=context['tool_name'], execution_json={'capability_provenance': context}))
    monkeypatch.setattr(jobs, 'get_job', lambda jid: dict(job_id=jid, tenant_id=ORG, org_id=ORG,
        project_id=PROJECT, tool=context['tool_name'], capability_provenance=context,
        status='running', progress='secret provider text'))
    monkeypatch.setattr(api, '_publication', lambda *a: pytest.fail('catalog consulted on recovery'))
    result = api.invoke(ORG, PROJECT, CAMPAIGN, ENROLLMENT, context['effective_catalog_digest'], 'key')
    assert result == {'job_id': job_id, 'status': 'running', 'progress': 'Working'}
    with pytest.raises(api.CapabilityError) as error:
        api.invoke(ORG, PROJECT, CAMPAIGN, ENROLLMENT, 'f' * 64, 'key')
    assert error.value.code == 'idempotency_conflict'


def test_submit_uncertainty_never_becomes_catalog_drift(invocation_edges, monkeypatch):
    import jobs
    context, _, _, held = invocation_edges
    def fail(**kwargs):
        assert held and kwargs['capability_provenance'] == context
        assert kwargs['params'] == {} and kwargs['dwg'] == '' and kwargs['aps_live'] is False
        assert not set(kwargs) & {'dwg_version', 'checkout_holder', 'checkout_fence', 'plan'}
        raise RuntimeError('lost response')
    monkeypatch.setattr(jobs, 'submit_job', fail)
    with pytest.raises(api.CapabilityError) as error:
        api.invoke(ORG, PROJECT, CAMPAIGN, ENROLLMENT, context['effective_catalog_digest'], 'key')
    assert error.value.code == 'invocation_unknown' and not held


def test_lock_cleanup_failure_closes_connection_and_masks_drift(monkeypatch):
    calls = []
    class Connection:
        def cursor(self):
            return self
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def execute(self, sql, params=()):
            calls.append(sql)
            if 'pg_advisory_unlock' in sql:
                raise RuntimeError('cleanup failed')
        def fetchone(self):
            return {'acquired': True}
        def commit(self):
            calls.append('commit')
        def rollback(self):
            calls.append('rollback')
        def close(self):
            calls.append('close')
    conn = Connection()
    pool = SimpleNamespace(getconn=lambda **k: conn, putconn=lambda c: calls.append('return'))
    monkeypatch.setattr(api._platform()[3], 'get_pool', lambda: pool)
    with pytest.raises(api.CapabilityError) as error:
        with api._admission_lock(ORG, ORG, PROJECT, 'key'):
            assert calls[-1] == 'commit'
            raise api.CapabilityError(409, 'catalog_drift')
    assert error.value.code == 'invocation_unknown'
    assert calls[-2:] == ['close', 'return']
