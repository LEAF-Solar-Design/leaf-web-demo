"""Callable Native router to shared CAS flow to actual author HTTP source proofs.

Only project/session persistence and protected harness HTTP are inert fixtures.
No SDK, PostgreSQL, real Git producer, staged package or runtime proof is claimed.
"""
import copy
import hashlib
import json
import threading
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import requests
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

import deps
import browser_project_authoring as authoring
import browser_project_flow as flow
import native_project_authoring as native
from routers import native_project as routes
from test_browser_project_authoring import lane, ORG, PROJECT, ACTOR, EDIT, BASE, HEAD, TREE, RAW

SID = '88888888-8888-4888-8888-888888888888'
TID = '99999999-9999-4999-8999-999999999999'
PREFIX = '/api/projects/' + PROJECT + '/native/source/requests'
CURRENT = (ORG, PROJECT, ACTOR)


@pytest.fixture
def connected(lane, monkeypatch):
    lane.tenant.authority_resolved = True
    fixture_role = authoring.platform_link.require_project_access
    monkeypatch.setattr(authoring.platform_link, 'require_project_access',
        lambda tenant, project, *, write, binding: fixture_role(tenant, project, write=True, binding=binding))
    lane.events, lane.candidates, lane.confirmations = [], {}, []
    lane.active, lane.allowed = TID, True
    lane.started = {}
    lane.body = dict(source_request_id=EDIT, requirements=lane.request['requirements'],
        source_operation='create', material_refs=copy.deepcopy(lane.request['material_refs']))
    lock = threading.Lock()
    def append(sid, tid, kind, data, *, expected_seq=None):
        assert sid == SID
        with lock:
            if expected_seq is not None and expected_seq != len(lane.events)+1:
                raise RuntimeError('CAS conflict')
            lane.events.append(dict(seq=len(lane.events)+1, turn_id=tid, type=kind, data=copy.deepcopy(data)))
    def begin(action='create', body=None):
        tid = TID if not lane.events else str(uuid.uuid4())
        lane.active = tid
        body = lane.body if body is None else body
        started = dict(stage_authority=True, text=body.get('requirements', ''),
            classifier_hint=flow._hint(action, EDIT, body, source_profile=native.SOURCE_PROFILE))
        lane.started[tid] = copy.deepcopy(started)
        append(SID, tid, 'turn_started', started)
        return {'X-Authority-Session-Id': SID, 'X-Authority-Turn-Id': tid}
    def held(tenant, sid, _sid, tid):
        if not lane.allowed or lane.revoked or sid != SID or tid != lane.active:
            raise flow.inspection.InspectionError(403)
        return (*CURRENT, 'subject')
    monkeypatch.setattr(flow.inspection, 'authority', held)
    monkeypatch.setattr(flow.session_store, 'turn_started_data', lambda sid, tid, org: copy.deepcopy(lane.started.get(tid)) if sid == SID and org == ORG else None)
    monkeypatch.setattr(flow.session_store, 'get_session', lambda sid: dict(last_seq=len(lane.events), active_turn_id=lane.active) if sid == SID else None)
    monkeypatch.setattr(flow.session_store, 'events_after', lambda sid, seq, limit=500: copy.deepcopy(lane.events[seq:seq+limit]))
    monkeypatch.setattr(flow.session_store, 'recent_events', lambda sid, limit: copy.deepcopy(lane.events[-limit:]))
    monkeypatch.setattr(flow.session_store, 'append_event', append)
    monkeypatch.setattr(flow.turn_runner, 'finish_stage_authority', lambda org, sid, tid, subject, **kw: setattr(lane, 'active', None) if lane.active == tid else None)
    def await_confirmation(edit, **kw):
        row = lane.candidates[edit]
        assert row['version'] == kw['expected_version']
        row.update(state='awaiting_confirmation', version=row['version']+1)
    def reject(*args, **kw):
        row = lane.candidates[args[-1]]
        assert row['receipt_digest'] == kw['receipt_digest'] and row['version'] == kw['expected_version']
        assert kw['transition_key'].startswith('native-reject:')
        row.update(state='rejected', version=row['version']+1)
        return copy.deepcopy(row)
    def put(confirmation, **kw):
        assert all(c.confirmation_id != confirmation.confirmation_id for c in lane.confirmations)
        lane.confirmations.append(confirmation)
    store = SimpleNamespace(get_edit=lambda *a: copy.deepcopy(lane.candidates.get(a[-1])),
        list_edits=lambda *a, **kw: copy.deepcopy(list(lane.candidates.values())),
        await_confirmation=await_confirmation, reject_edit=reject, put_confirmation=put)
    monkeypatch.setattr(flow, '_store', lambda: store)
    actual_post = authoring.requests.post
    def post(url, **kwargs):
        if url.endswith('/author'):
            response = actual_post(url, **kwargs)
            result = response.json()
            now = datetime.now(timezone.utc)
            raw = result['receipt']
            lane.candidates[EDIT] = dict(raw, state='staged', version=1,
                receipt_json=raw, receipt_digest=result['receiptDigest'], confirmation_id=None,
                observed_main_commit=None, observed_main_tree=None, observed_private_ref_commit=None,
                created_at=now, updated_at=now)
            if getattr(lane, 'lose_author_response', False):
                raise requests.Timeout()
            return response
        assert url.endswith(('/publish', '/recover'))
        dispatch = json.loads(kwargs['data'])
        assert dispatch['transition_key'].startswith('native-source:')
        row = lane.candidates[EDIT]
        if url.endswith('/publish'):
            row['confirmation_id'] = dispatch['confirmation_id']
        row.update(state='published', version=row['version']+2,
            observed_main_commit=HEAD, observed_main_tree=TREE, observed_private_ref_commit=HEAD)
        raise requests.Timeout()  # Real flow reconciles exact durable settlement.
    monkeypatch.setattr(authoring.requests, 'post', post)
    app = FastAPI()
    app.middleware('http')(routes.no_store_responses)
    app.include_router(routes.router)
    app.dependency_overrides[deps.require_active_tenant] = lambda: lane.tenant
    lane.begin, lane.headers, lane.app, lane.append = begin, begin(), app, append
    return lane


def create(lane):
    with TestClient(lane.app) as client:
        return client.post(PREFIX, json=lane.body, headers=lane.headers)


def test_connected_native_create_full_bytes_and_private_reads(connected):
    result = create(connected)
    assert result.status_code == 200, result.text
    assert result.json()['schema'] == 'leaf.native-project-source.v1'
    assert result.json()['state'] == 'awaiting_confirmation'
    assert result.headers['cache-control'] == 'private, no-store'
    assert len(connected.posts) == 1
    assert connected.posts[0]['source_profile'] == native.SOURCE_PROFILE
    assert connected.posts[0]['material'][0]['product_b64'] == authoring.base64.b64encode(RAW).decode()
    row = connected.candidates[EDIT]
    assert row['idempotency_key'].startswith('native-source:'+SID+':'+TID+':')
    assert 'source_profile' not in row['receipt_json']
    with TestClient(connected.app) as client:
        assert client.get(PREFIX).json()[0]['source_request_id'] == EDIT
        assert client.get(PREFIX+'/'+EDIT).json() == result.json()
    assert flow.read(connected.tenant, PROJECT) == []
    with pytest.raises(flow.FlowError):
        flow.read(connected.tenant, PROJECT, EDIT)


@pytest.mark.parametrize('action', ['confirm', 'reject', 'recover'])
def test_connected_native_lifecycle_reuses_author_row(connected, action):
    assert create(connected).status_code == 200
    row = connected.candidates[EDIT]
    if action == 'recover':
        row.update(state='publishing', version=3, confirmation_id=str(uuid.uuid4()))
    body = dict(receipt_digest=row['receipt_digest'], expected_version=row['version'])
    if action == 'confirm':
        body['confirmation_id'] = str(uuid.uuid4())
    headers = connected.begin(action, body)
    with TestClient(connected.app) as client:
        result = client.post(PREFIX+'/'+EDIT+'/'+action, json=body, headers=headers)
    assert result.status_code == 200, result.text
    assert result.json()['state'] == ('rejected' if action == 'reject' else 'published')
    assert len(connected.posts) == 1
    assert len(connected.confirmations) == (1 if action == 'confirm' else 0)


def test_lost_author_response_reconciles_without_redispatch(connected):
    connected.lose_author_response = True
    assert create(connected).status_code == 503
    assert len(connected.posts) == 1
    connected.headers = connected.begin()
    result = create(connected)
    assert result.status_code == 200, result.text
    assert len(connected.posts) == 1


@pytest.mark.parametrize('change', ['refs', 'actor', 'profile', 'missing', 'digest', 'base', 'tree'])
def test_missing_or_mismatched_witness_fails_closed(connected, change):
    assert create(connected).status_code == 200
    marker = next(e['data'] for e in connected.events if e['type'] == 'browser_source_dispatch')
    if change == 'refs':
        marker['request']['material_refs'][0]['sha256'] = 'f'*64
    elif change == 'actor':
        marker['authority'][2] = str(uuid.uuid4())
    elif change == 'profile':
        marker['source_profile'] = authoring.WEB_PROFILE
    elif change == 'missing':
        marker.pop('request')
    else:
        connected.candidates[EDIT][{'digest':'receipt_digest','base':'base_commit','tree':'staged_tree'}[change]] = 'f'*(64 if change == 'digest' else 40)
    with TestClient(connected.app) as client:
        assert client.get(PREFIX+'/'+EDIT).status_code == 409


@pytest.mark.parametrize('change', [dict(source_operation='edit', expected_base_commit=BASE),
    dict(material_refs=[]), dict(source_profile=authoring.WEB_PROFILE), dict(requirements=''),
    dict(expected_base_commit='bad')])
def test_closed_public_native_request_refuses_without_effect(connected, change):
    connected.body.update(change)
    result = create(connected)
    assert result.status_code == 422
    assert not connected.posts and not connected.initializations
    assert not any(e['type'] == 'browser_source_dispatch' for e in connected.events)


def test_oversize_and_hash_mismatch_are_before_dispatch_claim(connected):
    material = connected.body['material_refs'][0]['material_id']
    connected.rows[material]['byte_size'] = 1048577
    assert create(connected).status_code == 413
    assert not connected.posts and not connected.initializations
    assert len(connected.events) == 1


def test_cross_profile_collision_refuses_before_claim(connected):
    from test_browser_project_flow import candidate
    connected.candidates[EDIT] = candidate()  # Deliberately foreign authority and Web namespace.
    assert create(connected).status_code == 409
    assert not connected.posts and len(connected.events) == 1


@pytest.mark.parametrize('kind', ['revoked', 'foreign', 'stale-turn'])
def test_current_authority_denial_precedes_body(connected, monkeypatch, kind):
    if kind == 'revoked':
        connected.revoked = True
    elif kind == 'foreign':
        connected.headers['X-Authority-Session-Id'] = str(uuid.uuid4())
    else:
        connected.active = 'replacement'
    async def never_read(self):
        pytest.fail('unauthorized body was read')
        yield b''
    monkeypatch.setattr(Request, 'stream', never_read)
    assert create(connected).status_code == 403
    assert not connected.posts


@pytest.mark.parametrize('payload', [b'{"x":NaN}', b'{"x":1,"x":2}', b'\xff'])
def test_strict_json_and_private_errors(connected, payload):
    with TestClient(connected.app) as client:
        response = client.post(PREFIX, content=payload, headers=dict(connected.headers, **{'Content-Type':'application/json'}))
    assert response.status_code == 422
    assert response.headers['cache-control'] == 'private, no-store'


def test_dependency_failures_are_private_and_bounded(connected):
    def deny():
        raise HTTPException(401, 'private provider detail')
    connected.app.dependency_overrides[deps.require_active_tenant] = deny
    with TestClient(connected.app) as client:
        result = client.post(PREFIX, content=b'bad')
    assert result.status_code == 403
    assert 'private provider detail' not in result.text
    assert result.headers['cache-control'] == 'private, no-store'


def test_native_cas_claim_race_has_one_winner(connected):
    digest = hashlib.sha256(flow.canonical(dict(source_profile=native.SOURCE_PROFILE,
        action='create', source_request_id=EDIT, payload=connected.body))).hexdigest()
    winners = []
    def claim():
        winners.append(flow._claim(SID, TID, CURRENT, 'create', EDIT, digest, lambda: None,
            source_profile=native.SOURCE_PROFILE, body=connected.body))
    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert sorted(winners) == [False, True]
    marker = connected.events[-1]['data']
    assert marker['request'] == connected.body and marker['source_profile'] == native.SOURCE_PROFILE


def test_concurrent_callable_route_has_one_actual_author_dispatch(connected, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    actual_post = authoring.requests.post
    def delayed(url, **kwargs):
        if url.endswith('/author'):
            entered.set()
            assert release.wait(5)
        return actual_post(url, **kwargs)
    monkeypatch.setattr(authoring.requests, 'post', delayed)
    outcomes = []
    thread = threading.Thread(target=lambda: outcomes.append(create(connected)))
    thread.start()
    try:
        assert entered.wait(5)
        assert create(connected).status_code == 409
        assert connected.active == TID
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert outcomes[0].status_code == 200, outcomes[0].text
    assert len(connected.posts) == 1


def test_unknown_old_turn_without_row_cannot_redispatch(connected, monkeypatch):
    def lost(url, **kwargs):
        raise requests.Timeout()
    monkeypatch.setattr(authoring.requests, 'post', lost)
    assert create(connected).status_code == 503
    assert not connected.candidates
    connected.headers = connected.begin()
    monkeypatch.setattr(authoring.requests, 'post', lambda *a, **kw: pytest.fail('unknown outcome redispatched'))
    assert create(connected).status_code == 409


def test_hash_mismatch_has_no_claim_or_source_effect(connected):
    material = connected.body['material_refs'][0]['material_id']
    connected.rows[material]['sha256'] = 'f'*64
    assert create(connected).status_code == 409
    assert not connected.posts and not connected.initializations
    assert len(connected.events) == 1


def test_changed_full_request_cannot_reuse_candidate(connected):
    assert create(connected).status_code == 200
    connected.body['material_refs'][0]['sha256'] = 'f'*64
    connected.headers = connected.begin()
    assert create(connected).status_code == 409
    assert len(connected.posts) == 1


def test_native_confirmation_is_not_issued_twice(connected):
    assert create(connected).status_code == 200
    row = connected.candidates[EDIT]
    body = dict(receipt_digest=row['receipt_digest'], expected_version=row['version'], confirmation_id=str(uuid.uuid4()))
    with TestClient(connected.app) as client:
        assert client.post(PREFIX+'/'+EDIT+'/confirm', json=body, headers=connected.begin('confirm', body)).status_code == 200
        # A fresh held intent observes durable publication and cannot mint a second approval.
        assert client.post(PREFIX+'/'+EDIT+'/confirm', json=body, headers=connected.begin('confirm', body)).status_code == 200
    assert len(connected.confirmations) == 1 and len(connected.posts) == 1
