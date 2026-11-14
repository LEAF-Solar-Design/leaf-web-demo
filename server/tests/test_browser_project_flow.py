"""Focused source transaction proofs; no model, Git or user database."""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import browser_project_flow as flow
from project_repository_edit_contract import STAGED_RECEIPT_CONTRACT, staged_receipt_digest, parse_staged_receipt

ORG = '11111111-1111-4111-8111-111111111111'
PROJECT = '22222222-2222-4222-8222-222222222222'
ACTOR = '33333333-3333-4333-8333-333333333333'
EDIT = '44444444-4444-4444-8444-444444444444'
REPO = '55555555-5555-4555-8555-555555555555'
LEASE = '66666666-6666-4666-8666-666666666666'
CONFIRMATION = '77777777-7777-4777-8777-777777777777'
BOUND = dict(tenant_id=ORG, organization_id=ORG, project_id=PROJECT, repo_key=REPO)
CURRENT = (ORG, PROJECT, ACTOR)
BODY = dict(source_request_id=EDIT, requirements='Create my source', source_operation='create', material_refs=[])


def candidate(body=BODY, state='awaiting_confirmation', version=2):
    raw = dict(BOUND, contract=STAGED_RECEIPT_CONTRACT, edit_id=EDIT, state='staged', operation='edit',
        source_edit_id=None, actor_binding_id=ACTOR, writer_lease_id=LEASE, writer_lease_generation=7,
        base_commit='1' * 40, staged_head_commit='2' * 40, staged_tree='3' * 40,
        changed_paths=['app/page.tsx'], diff_digest='4' * 64,
        instruction_digest=hashlib.sha256(body['requirements'].encode()).hexdigest(),
        idempotency_key='browser-source:' + hashlib.sha256(flow.canonical(body)).hexdigest())
    receipt = parse_staged_receipt(raw)
    now = datetime.now(timezone.utc)
    return dict(raw, state=state, version=version, receipt_json=raw,
        receipt_digest=staged_receipt_digest(receipt), confirmation_id=None,
        observed_main_commit=None, observed_main_tree=None, observed_private_ref_commit=None,
        created_at=now, updated_at=now)


@pytest.fixture
def lane(monkeypatch):
    state = SimpleNamespace(rows={}, events=[], author_calls=0, finished=[], confirmations=[],
                            active='turn', allowed=True, hint=flow._hint('create', EDIT, BODY))
    lock = threading.Lock()
    def check(*args, **kwargs):
        if not state.allowed:
            raise flow.FlowError(403)
        return CURRENT
    def held(*args, **kwargs):
        check()
        if state.active != args[3]:
            raise flow.inspection.InspectionError(404)
        return (*CURRENT, 'subject')
    monkeypatch.setattr(flow, 'scope', check)
    monkeypatch.setattr(flow, 'repository', lambda current: copy.deepcopy(BOUND))
    monkeypatch.setattr(flow.inspection, 'authority', held)
    monkeypatch.setattr(flow.session_store, 'turn_started_data', lambda *a: {
        'stage_authority': True, 'text': BODY['requirements'], 'classifier_hint': copy.deepcopy(state.hint)})
    monkeypatch.setattr(flow.session_store, 'get_session', lambda *a: {
        'last_seq': len(state.events), 'active_turn_id': state.active})
    monkeypatch.setattr(flow.session_store, 'recent_events', lambda sid, limit: copy.deepcopy(state.events[-limit:]))
    monkeypatch.setattr(flow.session_store, 'events_after', lambda sid, seq, limit=500: copy.deepcopy(state.events[seq:seq + limit]))
    def append(sid, turn, kind, data, *, expected_seq=None):
        with lock:
            if expected_seq is not None and expected_seq != len(state.events) + 1:
                raise RuntimeError('sequence mismatch')
            state.events.append(dict(seq=len(state.events) + 1, turn_id=turn, type=kind, data=copy.deepcopy(data)))
            return len(state.events)
    monkeypatch.setattr(flow.session_store, 'append_event', append)
    def finish(org, sid, turn, subject, **kwargs):
        if state.active == turn:
            state.finished.append(turn)
            state.active = None
    monkeypatch.setattr(flow.turn_runner, 'finish_stage_authority', finish)
    def await_confirmation(edit, **kwargs):
        row = state.rows[edit]
        assert row['state'] == 'staged' and row['version'] == kwargs['expected_version']
        row.update(state='awaiting_confirmation', version=row['version'] + 1)
        return copy.deepcopy(row)
    def put(confirmation, **kwargs):
        state.confirmations.append(confirmation)
    store = SimpleNamespace(get_edit=lambda *a: copy.deepcopy(state.rows.get(a[-1])),
        list_edits=lambda *a, **kw: copy.deepcopy(list(state.rows.values())),
        await_confirmation=await_confirmation, put_confirmation=put)
    monkeypatch.setattr(flow, '_store', lambda: store)
    def author(tenant, project, body, **kwargs):
        state.author_calls += 1
        assert state.events[0]['type'] == 'turn_started'
        assert any(e['type'] == 'browser_source_dispatch' for e in state.events)
        row = candidate(state='staged', version=1)
        state.rows[EDIT] = row
        return dict(receipt=row['receipt_json'], receiptDigest=row['receipt_digest'], version=1)
    monkeypatch.setattr(flow.authoring, 'author', author)
    state.append, state.store, state.author = append, store, author
    append('session', 'turn', 'turn_started', {'text': BODY['requirements'], 'classifier_hint': state.hint, 'stage_authority': True})
    return state


def execute(body=BODY, action='request'):
    return flow.mutate('tenant', PROJECT, 'session', 'turn', action, EDIT, copy.deepcopy(body))


@pytest.mark.parametrize('operation', ['create', 'edit'])
def test_native_edit_consumer_web_stage_replay_and_membership_parity(lane, monkeypatch, operation):
    body = dict(BODY, source_operation=operation)
    if operation == 'edit':
        body['expected_base_commit'] = '1' * 40
    lane.hint = flow._hint(operation, EDIT, body)
    def author(tenant, project, request, **kwargs):
        lane.author_calls += 1
        assert request == dict(flow.body_to_author(body), idempotency_key=flow._request_key(body, flow.WEB_PROFILE))
        row = candidate(body, state='staged', version=1)
        lane.rows[EDIT] = row
        return dict(receipt=row['receipt_json'], receiptDigest=row['receipt_digest'], version=1)
    monkeypatch.setattr(flow.authoring, 'author', author)
    result = execute(body)
    assert result['schema'] == 'leaf.browser-project-source.v1'
    assert result['state'] == 'awaiting_confirmation'
    assert flow.read('tenant', PROJECT, EDIT) == result
    marker = next(e['data'] for e in lane.events if e['type'] == 'browser_source_dispatch')
    assert marker == dict(action=operation, source_request_id=EDIT, payload_digest=hashlib.sha256(
        flow.canonical(dict(action=operation, source_request_id=EDIT, payload=body))).hexdigest())
    lane.active = 'turn'
    assert execute(body) == result
    assert lane.author_calls == 1 and lane.finished == ['turn']


@pytest.mark.parametrize('raw', [
    b'{"receipt_digest":"a","receipt_digest":"b","expected_version":1}',
    b'{"receipt_digest":"a","expected_version":NaN}',
    b'{"receipt_digest":"a","expected_version":true}', b'\xff',
    b'{"x":{"a":1,"\\u0061":2}}',
])
def test_closed_strict_json(raw):
    with pytest.raises(flow.FlowError) as caught:
        flow.parse_request(raw, 'reject')
    assert caught.value.status == 422


def test_stages_exact_receipt_for_review_without_build_claims(lane):
    result = execute()
    assert lane.author_calls == 1 and lane.finished == ['turn']
    assert result['state'] == 'awaiting_confirmation' and result['version'] == 2
    assert set(result) == {'schema', 'project_id', 'source_request_id', 'state', 'version',
        'receipt_digest', 'base_commit', 'source_commit', 'source_tree', 'changed_paths', 'created_at', 'updated_at'}
    assert result['source_commit'] == '2' * 40
    assert lane.rows[EDIT]['observed_main_commit'] is None


@pytest.mark.parametrize('change', [
    {'requirements': 'Different'}, {'material_refs': [{'material_id': LEASE, 'sha256': 'a' * 64}]},
    {'expected_base_commit': '9' * 40}, {'source_operation': 'edit', 'expected_base_commit': '1' * 40},
])
def test_full_intent_witness_cannot_retarget_turn(lane, change):
    with pytest.raises(flow.FlowError):
        execute(dict(BODY, **change))
    assert lane.author_calls == 0 and not lane.finished


def test_concurrent_duplicate_does_not_dispatch_or_finish_owner(lane, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = lane.author
    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(flow.authoring, 'author', delayed)
    outcomes = []
    def run():
        try:
            outcomes.append(execute())
        except Exception as exc:
            outcomes.append(exc)
    owner = threading.Thread(target=run)
    owner.start()
    try:
        assert entered.wait(5)
        with pytest.raises(flow.FlowError):
            execute()
        assert lane.finished == []
    finally:
        release.set()
        owner.join(5)
    assert not owner.is_alive() and lane.author_calls == 1
    assert lane.finished == ['turn'] and len(outcomes) == 1


def test_changed_canonical_request_conflicts_even_under_new_turn(lane):
    lane.rows[EDIT] = candidate()
    changed = dict(BODY, material_refs=[{'material_id': LEASE, 'sha256': 'a' * 64}])
    lane.hint = flow._hint('create', EDIT, changed)
    with pytest.raises(flow.FlowError) as caught:
        execute(changed)
    assert caught.value.status == 409 and lane.author_calls == 0


@pytest.mark.parametrize('state,version', [('staged', 1), ('awaiting_confirmation', 2)])
def test_matching_durable_candidate_replays_without_model(lane, state, version):
    lane.rows[EDIT] = candidate(state=state, version=version)
    assert execute()['state'] == 'awaiting_confirmation'
    assert lane.author_calls == 0 and lane.finished == ['turn']


def test_duplicate_returns_existing_candidate_without_finishing_owner(lane):
    lane.rows[EDIT] = candidate()
    digest = hashlib.sha256(flow.canonical(dict(action='create', source_request_id=EDIT, payload=BODY))).hexdigest()
    lane.append('session', 'turn', 'browser_source_dispatch', dict(
        action='create', source_request_id=EDIT, payload_digest=digest))
    assert execute()['state'] == 'awaiting_confirmation'
    assert lane.author_calls == 0 and lane.finished == []


def test_revoked_authority_denies_before_claim(lane):
    lane.allowed = False
    with pytest.raises(flow.FlowError) as caught:
        execute()
    assert caught.value.status == 403
    assert lane.author_calls == 0 and len(lane.events) == 1 and lane.finished == []


def test_row_survives_result_event_failure(lane, monkeypatch):
    def append(*args, **kwargs):
        if args[2] == 'browser_source_result':
            raise RuntimeError('unavailable')
        return lane.append(*args, **kwargs)
    monkeypatch.setattr(flow.session_store, 'append_event', append)
    with pytest.raises(flow.FlowError):
        execute()
    assert lane.rows[EDIT]['state'] == 'awaiting_confirmation'
    assert lane.author_calls == 1


def test_replacement_turn_is_never_finished(lane, monkeypatch):
    def replaced(*args, **kwargs):
        result = lane.author(*args, **kwargs)
        lane.active = 'replacement'
        return result
    monkeypatch.setattr(flow.authoring, 'author', replaced)
    with pytest.raises(flow.FlowError):
        execute()
    assert lane.finished == [] and lane.active == 'replacement'
    assert lane.rows[EDIT]['state'] == 'staged'


def test_incomplete_tail_or_dual_write_never_dispatches(lane, monkeypatch):
    monkeypatch.setattr(flow.session_store, 'recent_events', lambda *a: [])
    with pytest.raises(flow.FlowError):
        execute()
    assert lane.author_calls == 0 and lane.finished == []
    monkeypatch.setattr(flow.session_store, 'recent_events', lambda *a: copy.deepcopy(lane.events))
    monkeypatch.setattr(flow.session_store, 'append_event', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('dual write')))
    with pytest.raises(flow.FlowError):
        execute()
    assert lane.author_calls == 0 and lane.finished == []


def test_snapshot_precedes_marker_read_and_lost_cas_rechecks(lane, monkeypatch):
    order = []
    snapshot = flow.session_store.get_session
    recent = flow.session_store.recent_events
    monkeypatch.setattr(flow.session_store, 'get_session', lambda *a: (order.append('snapshot'), snapshot(*a))[1])
    monkeypatch.setattr(flow.session_store, 'recent_events', lambda *a: (order.append('events'), recent(*a))[1])
    assert execute()['state'] == 'awaiting_confirmation'
    assert order.index('snapshot') < order.index('events')


def test_event_cas_race_has_exactly_one_cross_process_claim(lane, monkeypatch):
    barrier = threading.Barrier(2)
    local = threading.local()
    original = flow.session_store.get_session
    def snapshot(*args):
        value = original(*args)
        if not getattr(local, 'pinned', False):
            local.pinned = True
            barrier.wait(timeout=5)
        return value
    monkeypatch.setattr(flow.session_store, 'get_session', snapshot)
    claims = []
    def claim():
        try:
            claims.append(flow._claim('session', 'turn', CURRENT, 'create', EDIT, 'a' * 64, lambda: None))
        except Exception as exc:
            claims.append(exc)
    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert all(not thread.is_alive() for thread in threads)
    assert sorted(claims) == [False, True]
    assert len([event for event in lane.events if event['type'] == 'browser_source_dispatch']) == 1


def test_unknown_previous_dispatch_never_runs_second_editor(lane):
    lane.events.insert(0, dict(seq=1, turn_id='old-turn', type='browser_source_dispatch', data={
        'action': 'create', 'source_request_id': EDIT, 'payload_digest': 'a' * 64}))
    lane.events[1]['seq'] = 2
    with pytest.raises(flow.FlowError):
        execute()
    assert lane.author_calls == 0


def prepare_action(lane, action, row=None):
    row = row or candidate()
    lane.rows[EDIT] = row
    body = dict(receipt_digest=row['receipt_digest'], expected_version=row['version'])
    if action == 'confirm':
        body['confirmation_id'] = CONFIRMATION
    lane.hint = flow._hint(action, EDIT, body)
    return body


def harness(monkeypatch, callback):
    monkeypatch.setenv('LEAF_AUTHOR_HARNESS_URL', 'http://internal-harness')
    monkeypatch.setattr(flow.broker_client, 'harness_headers', lambda: {'X-Harness-Secret': 'server-owned'})
    monkeypatch.setattr(flow.requests, 'post', callback)


def published(row):
    row.update(state='published', version=row['version'] + 2, confirmation_id=CONFIRMATION,
        observed_main_commit=row['staged_head_commit'], observed_main_tree=row['staged_tree'],
        observed_private_ref_commit=row['staged_head_commit'])


def test_confirmation_is_explicit_and_lost_publish_response_reconciles(lane, monkeypatch):
    body = prepare_action(lane, 'confirm')
    def post(url, **kwargs):
        assert url.endswith('/publish')
        dispatch = flow.json.loads(kwargs['data'])
        assert dispatch['actor_binding_id'] == ACTOR and dispatch['confirmation_id'] == CONFIRMATION
        assert 'requirements' not in dispatch and kwargs['allow_redirects'] is False
        confirmation = lane.confirmations[0]
        assert confirmation.approver_binding_id == ACTOR
        assert confirmation.writer_witness.writer_lease_id == LEASE
        published(lane.rows[EDIT])
        raise requests.Timeout()
    harness(monkeypatch, post)
    assert execute(body, 'confirm')['state'] == 'published'
    assert len(lane.confirmations) == 1 and lane.author_calls == 0


@pytest.mark.parametrize('invalid', [False, True])
def test_real_publish_response_shape_matches_durable_receipt(lane, monkeypatch, invalid):
    body = prepare_action(lane, 'confirm')
    def post(url, **kwargs):
        row = lane.rows[EDIT]
        published(row)
        matrix = dict(contract='leaf.project-repository-edit-coordination.v1', action='authorize_publish',
            edit_id=EDIT, state='publishing', version=3, receipt_digest=row['receipt_digest'],
            expected_main_commit='1' * 40, staged_head_commit='2' * 40, staged_tree='3' * 40,
            private_ref='refs/leaf/changes/' + EDIT, publish_lease_id=CONFIRMATION, publish_lease_generation=8)
        observation = dict(private_ref='refs/leaf/changes/' + EDIT, private_ref_commit='2' * 40,
            before_main_commit='1' * 40, after_main_commit='2' * 40, after_main_tree='3' * 40, compare_and_swap=True)
        settlement = dict(contract=matrix['contract'], action='settle_publish', edit_id=EDIT, state='published', version=4)
        if invalid:
            matrix['staged_tree'] = '9' * 40
        return SimpleNamespace(status_code=200, content=flow.canonical(dict(
            matrix=matrix, observation=observation, settlement=settlement)), close=lambda: None)
    harness(monkeypatch, post)
    if invalid:
        with pytest.raises(flow.FlowError):
            execute(body, 'confirm')
        # Never undo an accepted source because response validation failed.
        assert flow.read('tenant', PROJECT, EDIT)['state'] == 'published'
    else:
        assert execute(body, 'confirm')['state'] == 'published'
    assert len(lane.confirmations) == 1 and lane.author_calls == 0


def test_consumed_confirmation_recovers_without_reapproval(lane, monkeypatch):
    row = candidate(state='publishing', version=3)
    row['confirmation_id'] = CONFIRMATION
    body = prepare_action(lane, 'recover', row)
    def post(url, **kwargs):
        assert url.endswith('/recover')
        dispatch = flow.json.loads(kwargs['data'])
        assert 'confirmation_id' not in dispatch
        assert dispatch['expected_main_commit'] == row['base_commit']
        published(lane.rows[EDIT])
        raise requests.Timeout()
    harness(monkeypatch, post)
    assert execute(body, 'recover')['state'] == 'published'
    assert lane.confirmations == [] and lane.author_calls == 0


@pytest.mark.parametrize('field,value', [('receipt_digest', 'a' * 64), ('expected_version', 99)])
def test_confirmation_tamper_never_approves(lane, field, value):
    body = prepare_action(lane, 'confirm')
    body[field] = value
    lane.hint = flow._hint('confirm', EDIT, body)
    with pytest.raises(flow.FlowError):
        execute(body, 'confirm')
    assert lane.confirmations == []


def test_stored_actor_tamper_is_detected(lane):
    row = candidate()
    row['actor_binding_id'] = CONFIRMATION
    body = prepare_action(lane, 'confirm', row)
    with pytest.raises(flow.FlowError):
        execute(body, 'confirm')
    assert lane.confirmations == []


def test_reject_uses_scoped_version_without_editor_or_confirmation(lane):
    body = prepare_action(lane, 'reject')
    prior = candidate(state='published')
    def reject(*args, **kwargs):
        assert args == (*BOUND.values(), EDIT)
        assert kwargs['expected_version'] == 2
        lane.rows[EDIT].update(state='rejected', version=3)
        return copy.deepcopy(lane.rows[EDIT])
    lane.store.reject_edit = reject
    assert execute(body, 'reject')['state'] == 'rejected'
    assert prior['state'] == 'published' and lane.confirmations == [] and lane.author_calls == 0


def test_read_list_has_closed_receipts_and_rechecks_access(lane, monkeypatch):
    lane.rows[EDIT] = candidate()
    assert flow.read('tenant', PROJECT)[0]['source_request_id'] == EDIT
    assert flow.read('tenant', PROJECT, EDIT)['receipt_digest'] == lane.rows[EDIT]['receipt_digest']
    monkeypatch.setattr(flow, 'repository', lambda *a: None)
    assert flow.read('tenant', PROJECT) == []
    with pytest.raises(flow.FlowError) as caught:
        flow.read('tenant', PROJECT, EDIT)
    assert caught.value.status == 404


def test_real_scope_rejects_bare_tenant_and_demo():
    with pytest.raises(flow.FlowError):
        flow.scope(ORG, PROJECT)
    demo = flow.deps.TenantContext(ORG, subject='subject', authority_resolved=True, verified_demo=True)
    with pytest.raises(flow.FlowError):
        flow.scope(demo, PROJECT)


def test_default_web_hint_key_and_claim_keep_original_shape(lane):
    assert flow._hint('create', EDIT, BODY) == dict(source='author_panel', purpose='stage_authority',
        browser_action='create', source_request_id=EDIT, material_refs=[])
    expected_key = 'browser-source:' + hashlib.sha256(flow.canonical(BODY)).hexdigest()
    assert flow._request_key(BODY, flow.WEB_PROFILE) == expected_key
    execute()
    marker = next(e['data'] for e in lane.events if e['type'] == 'browser_source_dispatch')
    assert set(marker) == {'action', 'source_request_id', 'payload_digest'}
    assert marker['payload_digest'] == hashlib.sha256(flow.canonical(dict(
        action='create', source_request_id=EDIT, payload=BODY))).hexdigest()


@pytest.mark.parametrize('code', ['material_too_large', 'cancelled', 'author_unavailable'])
def test_forward_diagnostic_preserves_typed_stage_failure(lane, monkeypatch, code):
    def fail(*args, **kwargs):
        error = flow.authoring.AuthoringError(code)
        error.args = ('fixture-private-exception-text',)
        raise error
    monkeypatch.setattr(flow.authoring, 'author', fail)
    with pytest.raises(flow.FlowError) as caught:
        execute()
    event = [item for item in lane.events if item['type'] == 'browser_source_error']
    assert len(event) == 1
    data = event[0]['data']
    assert caught.value.status == flow.authoring._ERRORS[code][0]
    assert data == dict(source_request_id=EDIT, phase='stage', code=code,
        status=caught.value.status, correlation_id=data['correlation_id'],
        text='Source outcome unavailable. Review the saved source state before retrying.')
    assert str(uuid.UUID(data['correlation_id'])) == data['correlation_id']
    assert uuid.UUID(data['correlation_id']).version == 4
    assert 'fixture-private-exception-text' not in json.dumps(event)
    assert BODY['requirements'] not in json.dumps(event)
    assert lane.author_calls == 0 and lane.finished == ['turn']


def test_forward_diagnostic_readback_uses_canonical_flow_error(lane, monkeypatch):
    def fail(*args, **kwargs):
        error = flow.FlowError(409)
        error.code = 'fixture-private-code'
        error.args = ('fixture-private-exception-text',)
        raise error
    monkeypatch.setattr(flow, 'representation', fail)
    with pytest.raises(flow.FlowError) as caught:
        execute()
    data = [item['data'] for item in lane.events if item['type'] == 'browser_source_error'][0]
    assert caught.value.status == 409
    assert (data['phase'], data['code'], data['status']) == ('result_readback', 'source_changed', 409)
    assert uuid.UUID(data['correlation_id']).version == 4
    assert 'fixture-private' not in json.dumps(data)
    assert lane.rows[EDIT]['state'] == 'awaiting_confirmation'
    assert lane.author_calls == 1 and lane.finished == ['turn']


@pytest.mark.parametrize('typed', [False, True])
def test_forward_diagnostic_unknown_failure_is_generic_and_secret_free(lane, monkeypatch, typed):
    def fail(*args, **kwargs):
        error = flow.authoring.AuthoringError('author_unavailable') if typed else RuntimeError()
        if typed:
            error.code = 'fixture-private-code'
        error.args = ('fixture-private-exception-text',)
        raise error
    monkeypatch.setattr(flow.authoring, 'author', fail)
    with pytest.raises(flow.FlowError) as caught:
        execute()
    data = [item['data'] for item in lane.events if item['type'] == 'browser_source_error'][0]
    assert caught.value.status == 503
    assert (data['phase'], data['code'], data['status']) == ('stage', 'service_unavailable', 503)
    assert uuid.UUID(data['correlation_id']).version == 4
    assert 'fixture-private' not in json.dumps(data)
    assert lane.rows == {} and lane.finished == ['turn']


@pytest.mark.parametrize('subphase,status', [('initialize_http', 503), ('initialize_authority', None)])
def test_subphase_and_correlated_warning_preserve_failure_contract(lane, monkeypatch, caplog, subphase, status):
    import project_author_diagnostics as diagnostics
    def fail(*args, **kwargs):
        diagnostics.record_failure(subphase, http_status=status)
        error = flow.authoring.AuthoringError('author_unavailable')
        error.args = ('fixture-private-author-error',)
        raise error
    monkeypatch.setattr(flow.authoring, 'author', fail)
    with pytest.raises(flow.FlowError) as caught: execute()
    assert caught.value.status == 503
    data = [event['data'] for event in lane.events if event['type'] == 'browser_source_error'][0]
    assert data['author_subphase'] == subphase
    assert data['phase'] == 'stage' and data['code'] == 'author_unavailable' and data['status'] == 503
    assert data.get('author_http_status') == status
    records = [record for record in caplog.records if record.name == 'browser_project_flow']
    assert len(records) == 1
    logged = json.loads(records[0].getMessage())
    assert set(logged) == {'event','project_id','source_request_id','correlation_id','phase','code','status','author_subphase'} | ({'author_http_status'} if status else set())
    assert logged['event'] == 'project_author_failure'
    assert logged['source_request_id'] == EDIT and logged['project_id'] == PROJECT
    assert logged['correlation_id'] == data['correlation_id'] and logged['author_subphase'] == subphase
    assert 'fixture-private' not in json.dumps(data) + records[0].getMessage()
    assert BODY['requirements'] not in records[0].getMessage()
    assert lane.finished == ['turn'] and diagnostics.snapshot() == {}


def test_diagnostic_context_is_closed_and_nested():
    import project_author_diagnostics as diagnostics
    diagnostics.record_failure('author_http', http_status=503)
    assert diagnostics.snapshot() == {}
    outer = diagnostics.begin()
    try:
        diagnostics.record_failure('initialize_http', http_status=502)
        diagnostics.record_failure('author_transport')
        assert diagnostics.snapshot() == dict(author_subphase='initialize_http', author_http_status=502)
        inner = diagnostics.begin()
        try:
            diagnostics.record_failure('fixture-private')
            assert diagnostics.snapshot() == {}
            diagnostics.record_failure('author_http', http_status='fixture-private')
            assert diagnostics.snapshot() == dict(author_subphase='author_http')
        finally:
            diagnostics.end(inner)
        assert diagnostics.snapshot() == dict(author_subphase='initialize_http', author_http_status=502)
    finally:
        diagnostics.end(outer)
    assert diagnostics.snapshot() == {}


def test_diagnostic_context_isolated_between_threads_and_copied_contexts():
    import concurrent.futures
    import contextvars
    import project_author_diagnostics as diagnostics
    barrier = threading.Barrier(2)
    def isolated(subphase):
        token = diagnostics.begin()
        try:
            diagnostics.record_failure(subphase)
            barrier.wait(timeout=5)
            return diagnostics.snapshot()
        finally:
            diagnostics.end(token)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(isolated, name) for name in ('author_http','initialize_http')]
        assert [f.result() for f in futures] == [dict(author_subphase=name) for name in ('author_http','initialize_http')]
    token = diagnostics.begin()
    try:
        child = contextvars.copy_context()
        child.run(diagnostics.record_failure, 'author_http', http_status=500)
        assert child.run(diagnostics.snapshot) == dict(author_subphase='author_http', author_http_status=500)
        assert diagnostics.snapshot() == {}
    finally:
        diagnostics.end(token)
