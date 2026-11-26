import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import browser_static_build as build
import browser_static_build_recheck as recheck


def uid(value):
    return str(uuid.UUID(int=value))


@pytest.fixture
def app(monkeypatch):
    authority = dict(tenant_id=uid(1), organization_id=uid(1), project_id=uid(2), repo_key=uid(3))
    candidate = dict(source_request_id=uid(4), version=3, receipt_digest='a' * 64,
                     source_commit='b' * 40, source_tree='c' * 40)
    record = dict(authority, **candidate, job_id=uid(5), actor_binding_id=uid(6))
    lookup = {k: record[k] for k in recheck._LOOKUP}
    body = dict(authority, **candidate, job_id=record['job_id'], operation='write')
    state = SimpleNamespace(
        record=record, role='write', role_calls=[], publication=dict(candidate, project_id=uid(2), state='published'),
    )

    def find(tenant_id, project_id, job_id):
        if state.record and (tenant_id, project_id, job_id) == (uid(1), uid(2), uid(5)):
            return dict(state.record)
        return None

    def role(current, *, write):
        state.role_calls.append((current, write))
        assert current == (uid(1), uid(2), uid(6))
        if state.role is None or (write and state.role != 'write'):
            raise recheck.flow.FlowError(403)

    monkeypatch.setenv('LEAF_STATIC_NEXT_EXECUTOR', 'enabled')
    monkeypatch.setattr(recheck.deps, '_dispatch_secret_ok', lambda value: value == 'fixture-dispatch')
    monkeypatch.setattr(recheck, '_store', lambda: SimpleNamespace(find=find))
    monkeypatch.setattr(recheck, '_require_role', role)
    monkeypatch.setattr(recheck.flow, 'repository', lambda current: dict(authority))
    monkeypatch.setattr(recheck.flow, '_read', lambda scope, source_id: state.publication)
    monkeypatch.setattr(recheck.flow, 'representation', lambda row, scope: row)
    api = FastAPI()
    api.include_router(recheck.router)
    client = TestClient(api)
    headers = {'X-Tenant-Id': authority['tenant_id'], 'X-Dispatch-Secret': 'fixture-dispatch'}

    def post(value=body, **kwargs):
        return client.post(recheck.PATH, json=value, headers=headers, **kwargs)

    return SimpleNamespace(**locals())


def test_restart_uses_the_persisted_full_candidate_without_returning_actor(app):
    result = app.post(dict(app.lookup, operation='read'))
    assert result.status_code == 200
    assert result.json() == dict(
        app.authority, **app.candidate, job_id=app.record['job_id'],
        schema='leaf.browser-static-candidate-recheck.v1',
        execution_id=build._execution_digest(app.authority, app.record['job_id'], app.candidate),
    )
    assert app.state.role_calls == [((uid(1), uid(2), uid(6)), False)] * 2


@pytest.mark.parametrize('field,value,status', [
    ('tenant_id', uid(9), 403),
    ('organization_id', uid(9), 403),
    ('project_id', uid(9), 404),
    ('repo_key', uid(9), 404),
    ('job_id', uid(9), 404),
    ('source_request_id', uid(9), 404),
    ('version', 4, 404),
    ('receipt_digest', 'd' * 64, 404),
    ('source_commit', 'd' * 40, 404),
    ('source_tree', 'd' * 40, 404),
    ('actor_binding_id', uid(6), 422),
])
def test_foreign_and_caller_supplied_authority_is_refused(app, field, value, status):
    assert app.post(dict(app.body, **{field: value})).status_code == status
    assert app.state.role_calls == []


@pytest.mark.parametrize('role,write_status,read_status', [
    ('write', 200, 200),
    ('read', 403, 200),
    (None, 403, 403),
])
def test_rechecks_current_operation_specific_project_permission(app, role, write_status, read_status):
    app.state.role = role
    assert app.post().status_code == write_status
    assert app.post(dict(app.lookup, operation='read')).status_code == read_status


def test_revocation_during_publication_check_is_refused(app, monkeypatch):
    def revoke(row, scope):
        app.state.role = None
        return row

    monkeypatch.setattr(recheck.flow, 'representation', revoke)
    assert app.post().status_code == 403
    assert len(app.state.role_calls) == 2


def test_missing_and_stale_admission_fail_closed(app):
    app.state.publication['version'] += 1
    assert app.post().status_code == 409
    app.state.publication['version'] -= 1
    app.state.record = None
    assert app.post(dict(app.lookup, operation='read')).status_code == 404


def test_partial_write_uncredentialed_method_and_default_off_are_refused(app, monkeypatch):
    assert app.post(dict(app.lookup, operation='write')).status_code == 200
    assert app.post(dict(app.lookup, operation='write', source_request_id=uid(4))).status_code == 422
    assert app.client.get(recheck.PATH, headers=app.headers).status_code == 405
    assert app.client.post(recheck.PATH, json=app.body).status_code == 403
    assert app.client.post(recheck.PATH + '/', headers=app.headers, follow_redirects=False).status_code == 307
    monkeypatch.delenv('LEAF_STATIC_NEXT_EXECUTOR')
    assert app.post().status_code == 404
