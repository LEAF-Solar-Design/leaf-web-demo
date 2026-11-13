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


def test_known_local_pre_generation_refusal_survives_private_middleware_without_generation(connected, monkeypatch):
    def unavailable(*args, **kwargs):
        raise flow.VideoUnavailableBeforeGeneration()
    monkeypatch.setattr(native, '_require_video_prerequisites', unavailable)
    result = create(connected)
    expected = dict(schema='leaf.native-source-pre-generation-refusal.v1', project_id=PROJECT,
        source_request_id=EDIT, source_profile=native.SOURCE_PROFILE, action='create',
        authority_session_id=SID, authority_turn_id=TID, stage='before_generation')
    assert result.status_code == 503
    assert result.json() == dict(error=dict(code='video_unavailable', message=flow.VIDEO_REFUSAL_MESSAGE), refusal=expected)
    assert result.headers['cache-control'] == 'private, no-store'
    assert not connected.posts and not connected.initializations and not connected.candidates
    assert connected.active is None
    assert len([e for e in connected.events if e['type'] == 'browser_source_dispatch']) == 1
    assert [e['data']['refusal'] for e in connected.events if e['type'] == 'browser_source_error'] == [expected]
    # A known refusal records disposition; it does not erase the original claim.
    connected.headers = connected.begin()
    assert create(connected).status_code == 409
    assert not connected.posts and not connected.initializations


@pytest.mark.parametrize('change', ['plain503', 'downstream-code', 'wrong-request', 'extra', 'revoked', 'no-disposition'])
def test_only_current_bound_local_refusal_is_public(connected, monkeypatch, change):
    if change == 'downstream-code':
        def downstream(*args, **kwargs):
            error = authoring.AuthoringError('author_unavailable')
            error.code = 'video_unavailable'
            raise error
        monkeypatch.setattr(authoring, 'author', downstream)
    elif change == 'plain503':
        monkeypatch.setattr(native, 'author', lambda *a, **kw: (_ for _ in ()).throw(flow.FlowError(503)))
    else:
        def foreign(*args, **kwargs):
            witness = dict(schema='leaf.native-source-pre-generation-refusal.v1', project_id=PROJECT,
                source_request_id=EDIT, source_profile=native.SOURCE_PROFILE, action='create',
                authority_session_id=SID, authority_turn_id=TID, stage='before_generation')
            if change == 'wrong-request':
                witness['source_request_id'] = str(uuid.uuid4())
            elif change == 'extra':
                witness['diagnostic'] = 'private provider detail'
            else:
                connected.revoked = True
            raise flow.PreGenerationRefusal(witness, provenance=None if change == 'no-disposition' else flow._RECORDED_REFUSAL)
        monkeypatch.setattr(native, 'author', foreign)
    result = create(connected)
    assert result.status_code in (403, 503)
    assert 'refusal' not in result.json()
    assert 'private provider detail' not in result.text
    assert result.headers['cache-control'] == 'private, no-store'
    assert not connected.posts and not connected.initializations


def test_refusal_is_not_returned_when_membership_changes_after_turn_settlement(connected, monkeypatch):
    monkeypatch.setattr(native, '_require_video_prerequisites', lambda *a, **kw:
        (_ for _ in ()).throw(flow.VideoUnavailableBeforeGeneration()))
    def finish(*args, **kwargs):
        connected.active = None
        connected.revoked = True
    monkeypatch.setattr(flow.turn_runner, 'finish_stage_authority', finish)
    response = create(connected)
    assert response.status_code == 403
    assert 'refusal' not in response.json()
    assert response.headers['cache-control'] == 'private, no-store'
    assert not connected.posts and not connected.initializations


def test_untrusted_error_body_is_scrubbed_even_if_it_looks_like_a_refusal(connected):
    async def downstream_response(request):
        return routes.JSONResponse({'refusal': {'stage': 'before_generation'},
            'error': {'code': 'video_unavailable', 'message': 'private provider detail'}}, status_code=503)
    request = Request({'type': 'http', 'method': 'POST', 'path': PREFIX, 'headers': []})
    import asyncio
    result = asyncio.run(routes.no_store_responses(request, downstream_response))
    assert 'refusal' not in json.loads(result.body)
    assert b'private provider detail' not in result.body
    assert result.headers['cache-control'] == 'private, no-store'


@pytest.mark.parametrize('available', [False, True])
def test_actual_native_prerequisite_check_precedes_author(connected, monkeypatch, available):
    material = connected.body['material_refs'][0]['material_id']
    connected.rows[material]['media_type'] = 'video/quicktime'
    actual_import = native.importlib.import_module
    monkeypatch.setattr(native.importlib, 'import_module', lambda name, *a, **kw:
        SimpleNamespace(_available=lambda: available) if name == 'project_video_materials' else actual_import(name, *a, **kw))
    calls = []
    monkeypatch.setattr(authoring, 'author', lambda *a, **kw: calls.append('author'))
    request = dict(connected.body, edit_id=EDIT, idempotency_key='synthetic')
    request.pop('source_request_id')
    if available:
        native.author(connected.tenant, PROJECT, request)
        assert calls == ['author']
    else:
        with pytest.raises(flow.VideoUnavailableBeforeGeneration):
            native.author(connected.tenant, PROJECT, request)
        assert not calls
    assert not connected.posts and not connected.initializations


def prepare_native_edit_consumer(lane):
    lane.body.update(source_operation='edit', expected_base_commit=BASE, material_refs=[])
    lane.headers = lane.begin('edit')
    def helper(result):
        from project_repository_edit_contract import parse_staged_receipt, staged_receipt_digest
        result['receipt']['changed_paths'] = ['Sources/RecipeModel.swift']
        result['receiptDigest'] = staged_receipt_digest(parse_staged_receipt(result['receipt']))
        return result
    lane.response_mutation = helper


@pytest.mark.parametrize('action', ['confirm', 'reject', 'recover'])
def test_native_edit_consumer_public_stage_reads_and_durable_lifecycle(connected, action):
    prepare_native_edit_consumer(connected)
    result = create(connected)
    assert result.status_code == 200, result.text
    assert result.json()['state'] == 'awaiting_confirmation' and result.json()['version'] == 2
    assert result.json()['changed_paths'] == ['Sources/RecipeModel.swift']
    assert result.json()['base_commit'] == BASE
    assert result.headers['cache-control'] == 'private, no-store'
    assert len(connected.posts) == 1 and not connected.reads
    dispatch = connected.posts[0]
    assert dispatch['source_operation'] == 'edit' and dispatch['source_profile'] == native.SOURCE_PROFILE
    assert dispatch['expected_base_commit'] == BASE and dispatch['material'] == []
    assert dispatch['actor_binding_id'] == ACTOR and dispatch['project_id'] == PROJECT
    assert dispatch['requirements'] == connected.body['requirements']
    assert all(dispatch[key] == value for key, value in connected.authority.items())
    with TestClient(connected.app) as client:
        assert client.get(PREFIX).json() == [result.json()]
        assert client.get(PREFIX+'/'+EDIT).json() == result.json()
    assert flow.read(connected.tenant, PROJECT) == []
    with pytest.raises(flow.FlowError) as caught:
        flow.read(connected.tenant, PROJECT, EDIT)
    assert caught.value.status == 409
    row = connected.candidates[EDIT]
    assert row['receipt_json']['operation'] == 'edit' and 'source_profile' not in row['receipt_json']
    assert row['observed_main_commit'] is None
    if action == 'recover':
        row.update(state='publishing', version=3, confirmation_id=str(uuid.uuid4()))
    body = dict(receipt_digest=row['receipt_digest'], expected_version=row['version'])
    if action == 'confirm':
        body['confirmation_id'] = str(uuid.uuid4())
    with TestClient(connected.app) as client:
        settled = client.post(PREFIX+'/'+EDIT+'/'+action, json=body, headers=connected.begin(action, body))
        assert settled.status_code == 200, settled.text
        assert settled.json()['state'] == ('rejected' if action == 'reject' else 'published')
        assert settled.json()['version'] == (3 if action == 'reject' else (5 if action == 'recover' else 4))
        if action == 'confirm':
            replay = client.post(PREFIX+'/'+EDIT+'/confirm', json=body, headers=connected.begin('confirm', body))
            assert replay.status_code == 200 and replay.json() == settled.json()
        assert client.get(PREFIX+'/'+EDIT).json() == settled.json()
    assert len(connected.posts) == 1
    assert len(connected.confirmations) == (1 if action == 'confirm' else 0)
    assert row['observed_main_commit'] == (None if action == 'reject' else HEAD)


def test_native_edit_consumer_lost_author_response_reuses_original_operation(connected):
    prepare_native_edit_consumer(connected)
    connected.lose_author_response = True
    assert create(connected).status_code == 503
    original_key = connected.candidates[EDIT]['idempotency_key']
    connected.headers = connected.begin('edit')
    result = create(connected)
    assert result.status_code == 200, result.text
    assert result.json()['state'] == 'awaiting_confirmation' and result.json()['version'] == 2
    assert len(connected.posts) == 1 and not connected.confirmations
    assert connected.candidates[EDIT]['idempotency_key'] == original_key


@pytest.mark.parametrize('kind', ['missing-base', 'stale-base', 'actor', 'oversize', 'hash'])
def test_native_edit_consumer_public_refusal_preserves_source_and_claims(connected, kind):
    prepare_native_edit_consumer(connected)
    if kind == 'missing-base':
        connected.body.pop('expected_base_commit')
    elif kind == 'stale-base':
        connected.body['expected_base_commit'] = 'f'*40
    else:
        connected.body['material_refs'] = copy.deepcopy(connected.request['material_refs'])
        material = connected.body['material_refs'][0]['material_id']
        if kind == 'actor':
            connected.rows[material]['actor_binding_id'] = str(uuid.uuid4())
        elif kind == 'oversize':
            connected.rows[material]['byte_size'] = 1048577
        else:
            connected.rows[material]['sha256'] = 'f'*64
    connected.headers = connected.begin('edit')
    result = create(connected)
    assert result.status_code == {'missing-base':422, 'stale-base':409, 'actor':404, 'oversize':413, 'hash':409}[kind]
    assert not connected.posts and not connected.candidates and not connected.confirmations
    if kind != 'stale-base':
        assert not connected.initializations
        assert not any(e['type'] == 'browser_source_dispatch' for e in connected.events)


@pytest.mark.parametrize('kind', ['request-operation', 'hint-operation', 'marker-operation', 'fingerprint',
    'expected-base', 'actor', 'profile', 'receipt-digest', 'receipt-helper'])
def test_native_edit_consumer_persisted_operation_and_receipt_witness_tamper(connected, kind):
    prepare_native_edit_consumer(connected)
    assert create(connected).status_code == 200
    event = next(e for e in connected.events if e['type'] == 'browser_source_dispatch')
    marker = event['data']
    if kind == 'request-operation':
        marker['request']['source_operation'] = 'create'
    elif kind == 'hint-operation':
        connected.started[event['turn_id']]['classifier_hint']['browser_action'] = 'create'
    elif kind == 'marker-operation':
        marker['action'] = 'create'
    elif kind == 'fingerprint':
        marker['payload_digest'] = hashlib.sha256(flow.canonical(dict(source_profile=native.SOURCE_PROFILE,
            action='create', source_request_id=EDIT, payload=marker['request']))).hexdigest()
    elif kind == 'expected-base':
        marker['request']['expected_base_commit'] = 'f'*40
    elif kind == 'actor':
        marker['authority'][2] = str(uuid.uuid4())
    elif kind == 'profile':
        marker['source_profile'] = authoring.WEB_PROFILE
    elif kind == 'receipt-digest':
        connected.candidates[EDIT]['receipt_digest'] = 'f'*64
    else:
        connected.candidates[EDIT]['receipt_json']['changed_paths'] = ['Sources/Tampered.swift']
    with TestClient(connected.app) as client:
        assert client.get(PREFIX+'/'+EDIT).status_code == 409
        assert client.get(PREFIX).status_code == 409
        row = connected.candidates[EDIT]
        body = dict(receipt_digest=row['receipt_digest'], expected_version=row['version'], confirmation_id=str(uuid.uuid4()))
        assert client.post(PREFIX+'/'+EDIT+'/confirm', json=body, headers=connected.begin('confirm', body)).status_code == 409
    assert len(connected.posts) == 1 and not connected.confirmations


@pytest.mark.parametrize('kind', ['version', 'digest', 'revoked', 'repo'])
def test_native_edit_consumer_confirmation_keeps_cas_and_current_authority(connected, kind):
    prepare_native_edit_consumer(connected)
    assert create(connected).status_code == 200
    row = connected.candidates[EDIT]
    body = dict(receipt_digest=row['receipt_digest'], expected_version=row['version'], confirmation_id=str(uuid.uuid4()))
    if kind == 'version':
        body['expected_version'] += 1
    elif kind == 'digest':
        body['receipt_digest'] = 'f'*64
    elif kind == 'revoked':
        connected.revoked = True
    else:
        connected.authority['repo_key'] = str(uuid.uuid4())
    with TestClient(connected.app) as client:
        response = client.post(PREFIX+'/'+EDIT+'/confirm', json=body, headers=connected.begin('confirm', body))
    assert response.status_code == (403 if kind == 'revoked' else 409)
    assert row['state'] == 'awaiting_confirmation' and row['version'] == 2
    assert row['observed_main_commit'] is None
    assert len(connected.posts) == 1 and not connected.confirmations


def test_native_edit_consumer_create_full_bytes_and_private_reads(connected):
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
def test_native_edit_consumer_create_lifecycle_reuses_author_row(connected, action):
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


@pytest.mark.parametrize('change', [dict(source_operation='edit'), dict(source_operation='delete'),
    dict(material_refs=[]), dict(source_profile=authoring.WEB_PROFILE), dict(requirements=''),
    dict(expected_base_commit='bad')])
def test_native_edit_consumer_closed_public_request_refuses_without_effect(connected, change):
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


@pytest.fixture
def native_export(connected, monkeypatch):
    assert create(connected).status_code == 200
    row = connected.candidates[EDIT]
    body = dict(receipt_digest=row['receipt_digest'], expected_version=row['version'],
                confirmation_id=str(uuid.uuid4()))
    with TestClient(connected.app) as client:
        published = client.post(PREFIX+'/'+EDIT+'/confirm', json=body,
                                headers=connected.begin('confirm', body))
    assert published.status_code == 200 and published.json()['state'] == 'published'
    connected.export_query = {key: str(published.json()[key]) for key in routes.source_export._WITNESSES}
    connected.bundle = b'# v3 git bundle\n@object-format=sha1\n' + HEAD.encode() + b' refs/heads/main\n\nPACK'
    connected.export_result = dict(bundle=connected.bundle, size_bytes=len(connected.bundle),
        bundle_sha256=hashlib.sha256(connected.bundle).hexdigest(), source_commit=HEAD, source_tree=TREE,
        lease_id=ACTOR, lease_generation='1')
    connected.exports = []
    def producer(*args, **kwargs):
        connected.exports.append((args, kwargs))
        return connected.export_result
    monkeypatch.setattr(routes.source, 'export_project_source_bundle', producer)
    # A customer download does not depend on a live conversation or worker lease.
    connected.allowed, connected.active = False, None
    return connected


def download_native(lane, query=None, project=PROJECT, request_id=EDIT):
    path = '/api/projects/'+project+'/native/source/requests/'+request_id+'/export'
    with TestClient(lane.app) as client:
        return client.get(path, params=lane.export_query if query is None else query)


def test_native_export_exact_private_bytes_and_identity_for_read_only_member(native_export, monkeypatch):
    accesses = []
    def member(tenant, project, *, write, binding):
        assert tenant is native_export.tenant and project == PROJECT and write is False
        accesses.append(project)
        return ORG
    monkeypatch.setattr(authoring.platform_link, 'require_project_access', member)
    response = download_native(native_export)
    assert response.status_code == 200, response.text
    assert response.content == native_export.bundle
    assert accesses
    assert native_export.exports == [((ORG, ORG, PROJECT, HEAD, TREE), {'max_bytes': 67108864})]
    assert response.headers['content-type'] == 'application/octet-stream'
    assert response.headers['content-disposition'] == f'attachment; filename="project-source-{HEAD}.bundle"'
    assert response.headers['content-length'] == str(len(native_export.bundle))
    assert response.headers['cache-control'] == 'private, no-store'
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert response.headers['x-leaf-bundle-sha256'] == hashlib.sha256(response.content).hexdigest()
    for key, value in native_export.export_query.items():
        assert response.headers['x-leaf-source-'+key.removeprefix('source_').replace('_', '-')] == value
    assert not any('lease' in key or 'repo' in key for key in response.headers)


@pytest.mark.parametrize('field', ['source_commit', 'source_tree', 'receipt_digest', 'version'])
def test_native_export_refuses_stale_review_witness_before_production(native_export, field):
    query = dict(native_export.export_query)
    query[field] = str(int(query[field])+1) if field == 'version' else 'f'*len(query[field])
    response = download_native(native_export, query)
    assert response.status_code == 409 and not native_export.exports
    assert response.headers['cache-control'] == 'private, no-store'


@pytest.mark.parametrize('kind', ['duplicate', 'missing', 'unknown', 'oversize', 'bad-version', 'bad-id'])
def test_native_export_closed_bounded_query_and_request_identity(native_export, kind):
    query = list(native_export.export_query.items())
    request_id = EDIT
    if kind == 'duplicate':
        query.append(query[0])
    elif kind == 'missing':
        query.pop()
    elif kind == 'unknown':
        query.append(('repo_key', ACTOR))
    elif kind == 'oversize':
        query.append(('unknown', 'x'*2049))
    elif kind == 'bad-version':
        query = [(key, '01' if key == 'version' else value) for key, value in query]
    else:
        request_id = 'invalid'
    response = download_native(native_export, query, request_id=request_id)
    assert response.status_code == 422 and not native_export.exports


@pytest.mark.parametrize('kind', ['revoked', 'foreign-project', 'unauthenticated', 'worker-only', 'unresolved'])
def test_native_export_denies_without_customer_membership(native_export, kind):
    project = PROJECT
    if kind == 'revoked':
        native_export.revoked = True
    elif kind == 'foreign-project':
        project = str(uuid.uuid4())
    elif kind == 'unresolved':
        native_export.tenant.authority_resolved = False
    else:
        def deny():
            raise HTTPException(401 if kind == 'unauthenticated' else 403, 'private identity detail')
        native_export.app.dependency_overrides[deps.require_active_tenant] = deny
    # Authorization wins over malformed query data; no bundle is produced.
    response = download_native(native_export, [('unknown', 'bad')], project=project)
    assert response.status_code == 403 and not native_export.exports
    assert response.headers['cache-control'] == 'private, no-store'
    assert 'private identity detail' not in response.text


@pytest.mark.parametrize('kind', ['unpublished', 'missing', 'browser-profile', 'receipt-tamper'])
def test_native_export_requires_durable_native_publication(native_export, kind):
    if kind == 'missing':
        native_export.candidates.clear()
    elif kind == 'browser-profile':
        from test_browser_project_flow import candidate
        native_export.candidates[EDIT] = candidate()
    elif kind == 'receipt-tamper':
        native_export.candidates[EDIT]['receipt_json']['changed_paths'] = ['Sources/Other.swift']
    else:
        native_export.candidates[EDIT]['state'] = 'awaiting_confirmation'
    response = download_native(native_export)
    assert response.status_code == 409 and not native_export.exports


@pytest.mark.parametrize('kind,status', [('revoked', 403), ('mapping', 403), ('principal', 403),
                                        ('candidate', 409), ('missing', 409)])
def test_native_export_rechecks_authorization_and_candidate_after_production(native_export, monkeypatch, kind, status):
    def producer(*args, **kwargs):
        if kind == 'revoked':
            native_export.revoked = True
        elif kind == 'mapping':
            native_export.authority['repo_key'] = str(uuid.uuid4())
        elif kind == 'principal':
            native_export.tenant.subject = 'another-person'
        elif kind == 'missing':
            native_export.candidates.clear()
        else:
            native_export.candidates[EDIT]['version'] += 1
        return native_export.export_result
    monkeypatch.setattr(routes.source, 'export_project_source_bundle', producer)
    response = download_native(native_export)
    assert response.status_code == status
    assert native_export.bundle not in response.content
    assert response.headers['cache-control'] == 'private, no-store'


@pytest.mark.parametrize('field,value', [('bundle', b'not a Git bundle'), ('size_bytes', True),
    ('size_bytes', 1), ('bundle_sha256', '0'*64), ('source_commit', 'e'*40), ('source_tree', 'f'*40)])
def test_native_export_verifies_producer_bytes_and_witnesses(native_export, field, value):
    native_export.export_result[field] = value
    response = download_native(native_export)
    assert response.status_code == 503
    assert response.headers['cache-control'] == 'private, no-store'


@pytest.mark.parametrize('error,status', [(routes.source.SourceConflict, 409), (routes.source.SourceUnavailable, 503)])
def test_native_export_bounds_producer_errors(native_export, monkeypatch, error, status):
    def producer(*args, **kwargs):
        raise error('private producer detail')
    monkeypatch.setattr(routes.source, 'export_project_source_bundle', producer)
    response = download_native(native_export)
    assert response.status_code == status and 'private producer detail' not in response.text
