"""Real PostgreSQL recovery proofs for the campaign execution authority."""
import hashlib
import json
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import psycopg
import pytest

from leaf_platform import campaigns, campaign_execution as execution, db, store


def _seed(make_org):
    org = make_org()
    project = store.create_project(org.org_id, 'Execution project')
    binding = store.create_identity_binding(
        org.org_id, 'auth0', f'auth0|execution-{uuid.uuid4()}', role='owner')
    with db.connection() as conn:
        conn.execute('INSERT INTO project_member_bindings '
                     '(membership_id, org_id, project_id, binding_id, role, invited_by_binding_id) '
                     "VALUES (%s, %s, %s, %s, 'owner', %s)",
                     (uuid.uuid4(), org.org_id, project.project_id, binding.binding_id, binding.binding_id))
    campaign = campaigns.submit_campaign(
        org.org_id, project.project_id, str(org.org_id), binding.binding_id,
        title='ReciPDF', prompt='Organize recipes', idempotency_key='execution')
    scope = (org.org_id, project.project_id, campaign['campaign_id'])
    question = campaigns.ask_question(*scope, question_key='format', prompt='Which format?')
    return scope, binding, question


def _submit(scope, key='task', **changes):
    payload = dict(task_key=key, title='Recipe task', spec='Prepare recipe files',
                   capability='codex.edit', stages=['implementation'], owned_paths=['recipes.md'],
                   source_sha='a' * 40, verify_command='python check_recipes.py',
                   declared_artifacts=['recipe-diff'], depends_on=[], idempotency_key=key)
    payload.update(changes)
    return execution.submit_task(*scope, **payload)


def _claim(scope):
    return execution.claim_task(*scope, worker_id='recipe-worker', lease_seconds=30)


def _settle(scope, attempt, **changes):
    payload = dict(attempt_token=attempt['attempt_token'], fence=attempt['fence'],
                   outcome='succeeded', result={}, artifact_ref='diff:recipe-change',
                   outward_operation_key=attempt['outward_operation_key'])
    payload.update(changes)
    return execution.settle_attempt(*scope, attempt['attempt_id'], **payload)


def _expire(attempt):
    with db.connection() as conn:
        conn.execute("UPDATE campaign_task_attempts SET deadline_at=NOW()-interval '1 second' "
                     'WHERE attempt_id=%s', (uuid.UUID(attempt['attempt_id']),))


def _code(code, function, *args, **kwargs):
    with pytest.raises(campaigns.CampaignError) as exc:
        function(*args, **kwargs)
    assert exc.value.code == code


def test_duplicate_submission_and_task_key_alias(make_org):
    scope, _, _ = _seed(make_org)
    first = _submit(scope)
    for key in ('task', 'another-delivery'):
        replay = _submit(scope, idempotency_key=key)
        assert replay['task_id'] == first['task_id'] and replay['replayed'] is True
        _code('task_conflict', _submit, scope, idempotency_key=key, spec='Changed spec')
    assert len(execution.read_execution(*scope)['tasks']) == 1
    _code('invalid_request', _submit, scope, 'unknown-dependency', depends_on=['absent'])
    capability = _submit(scope, 'capability', kind='capability', parent_task_id=first['task_id'],
                         depends_on=['task'])
    assert capability['parent_task_id'] == first['task_id']
    attempt = _claim(scope)
    assert attempt['task_id'] == first['task_id']
    assert _claim(scope) is None
    _settle(scope, attempt)
    assert _claim(scope)['task_id'] == capability['task_id']


def test_concurrent_claim_has_one_active_attempt(make_org):
    scope, _, _ = _seed(make_org)
    task = _submit(scope)
    barrier = Barrier(2)

    def claim():
        barrier.wait(timeout=10)
        return _claim(scope)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim) for _ in range(2)]
        results = [future.result(timeout=30) for future in futures]
    assert sum(result is not None for result in results) == 1
    with db.connection() as conn:
        rows = conn.execute("SELECT * FROM campaign_task_attempts WHERE task_id=%s AND status='active'",
                            (uuid.UUID(task['task_id']),)).fetchall()
    assert len(rows) == 1
    claim = next(result for result in results if result)
    assert rows[0]['attempt_token_hash'] == hashlib.sha256(claim['attempt_token'].encode()).hexdigest()
    assert 'attempt_token' not in rows[0]
    read = json.dumps(execution.read_execution(*scope))
    assert claim['attempt_token'] not in read
    assert rows[0]['attempt_token_hash'] not in read
    assert 'attempt_token' not in read


def test_expired_local_attempt_is_fenced(make_org):
    scope, _, _ = _seed(make_org)
    _submit(scope)
    first = _claim(scope)
    _expire(first)
    second = _claim(scope)
    assert second['fence'] == first['fence'] + 1
    _code('stale_attempt', _settle, scope, first)
    _settle(scope, second)
    assert execution.read_execution(*scope)['tasks'][0]['status'] == 'succeeded'


def test_concurrent_expiry_cannot_reset_new_claim(make_org):
    scope, _, _ = _seed(make_org)
    _submit(scope)
    expired = _claim(scope)
    _expire(expired)
    barrier = Barrier(2)

    def recover():
        barrier.wait(timeout=10)
        return _claim(scope)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(recover) for _ in range(2)]
        results = [future.result(timeout=30) for future in futures]
    claimed = [result for result in results if result is not None]
    assert len(claimed) == 1
    read = execution.read_execution(*scope)
    assert read['tasks'][0]['status'] == 'claimed'
    assert read['tasks'][0]['fence'] == expired['fence'] + 1
    assert read['tasks'][0]['active_attempt']['attempt_id'] == claimed[0]['attempt_id']
    _settle(scope, claimed[0])


def test_settlement_replays_after_pool_reset_and_later_claim(make_org):
    scope, _, _ = _seed(make_org)
    _submit(scope, stages=['implementation', 'build_test'])
    first = _claim(scope)
    receipt = _settle(scope, first)
    later = _claim(scope)
    assert later['fence'] > first['fence']
    db.reset_pool()
    replay = _settle(scope, first)
    assert replay['receipt_id'] == receipt['receipt_id'] and replay['replayed'] is True
    _code('settlement_conflict', _settle, scope, first, result={'changed': True})
    _code('stale_attempt', _settle, scope, first, attempt_token='0' * 64)
    assert len(execution.read_execution(*scope)['receipts']) == 1


def test_later_failure_preserves_immutable_success(make_org):
    scope, _, _ = _seed(make_org)
    _submit(scope, stages=['implementation', 'build_test'])
    receipt = _settle(scope, _claim(scope))
    _settle(scope, _claim(scope), outcome='failed', result={'exit_code': 1})
    read = execution.read_execution(*scope)
    assert read['tasks'][0]['status'] == 'failed'
    assert read['tasks'][0]['current_stage'] == 'build_test'
    assert any(r['receipt_id'] == receipt['receipt_id'] and r['outcome'] == 'succeeded'
               for r in read['receipts'])
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
        with db.connection() as conn:
            conn.execute("UPDATE campaign_stage_receipts SET outcome='failed' WHERE receipt_id=%s",
                         (uuid.UUID(receipt['receipt_id']),))
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
        with db.connection() as conn:
            conn.execute('DELETE FROM campaign_events WHERE event_id=%s',
                         (uuid.UUID(read['events'][0]['event_id']),))
    execution.retry_task(*scope, read['tasks'][0]['task_id'])
    assert _claim(scope)['stage'] == 'build_test'


def test_unknown_outward_reconciliation_replays_after_stage_advance(make_org):
    scope, _, _ = _seed(make_org)
    task = _submit(scope, stages=['deployment', 'verification'])
    attempt = _claim(scope)
    key = attempt['outward_operation_key']
    assert key
    _code('reconcile_identity_mismatch', _settle, scope, attempt,
          outcome='unknown', outward_operation_key='invented')
    unknown = _settle(scope, attempt, outcome='unknown')
    assert _claim(scope) is None
    _code('reconcile_required', execution.retry_task, *scope, task['task_id'])
    kwargs = dict(outward_operation_key=key, outcome='succeeded', result={'observed': 'live'},
                  resource_identity='deployment:recipe', rollback_identity='deployment:previous')
    _code('reconcile_identity_mismatch', execution.reconcile_outward, *scope, task['task_id'],
          **{**kwargs, 'outward_operation_key': 'different'})
    receipt = execution.reconcile_outward(*scope, task['task_id'], **kwargs)
    assert receipt['reconciles_receipt_id'] == unknown['receipt_id']
    assert _claim(scope)['stage'] == 'verification'
    db.reset_pool()
    replay = execution.reconcile_outward(*scope, task['task_id'], **kwargs)
    assert replay['receipt_id'] == receipt['receipt_id'] and replay['replayed'] is True
    _code('settlement_conflict', execution.reconcile_outward, *scope, task['task_id'],
          **{**kwargs, 'result': {'observed': 'different'}})
    assert len(execution.read_execution(*scope)['receipts']) == 2


@pytest.mark.parametrize('stage', ['publication', 'deployment', 'cleanup'])
def test_interrupted_outward_lease_requires_reconciliation(make_org, stage):
    scope, _, _ = _seed(make_org)
    task = _submit(scope, stages=[stage])
    attempt = _claim(scope)
    _expire(attempt)
    assert _claim(scope) is None
    read = execution.read_execution(*scope)
    assert read['tasks'][0]['status'] == 'reconcile_required'
    assert read['receipts'][0]['outcome'] == 'unknown'
    assert read['receipts'][0]['outward_operation_key'] == attempt['outward_operation_key']
    _code('stale_attempt', _settle, scope, attempt)
    _code('reconcile_required', execution.retry_task, *scope, task['task_id'])
    kwargs = dict(outward_operation_key=attempt['outward_operation_key'],
                  outcome='failed', result={'observed': 'no effect'})
    receipt = execution.reconcile_outward(*scope, task['task_id'], **kwargs)
    db.reset_pool()
    assert execution.reconcile_outward(*scope, task['task_id'], **kwargs)['receipt_id'] == receipt['receipt_id']
    execution.retry_task(*scope, task['task_id'])
    retried = _claim(scope)
    assert retried['outward_operation_key'] == attempt['outward_operation_key']
    assert retried['fence'] > attempt['fence']


def test_question_blocks_only_linked_task(make_org):
    scope, binding, question = _seed(make_org)
    task = _submit(scope, 'a')
    independent = _submit(scope, 'b')
    execution.link_question(*scope, task['task_id'], question['question_id'])
    replay = execution.link_question(*scope, task['task_id'], question['question_id'])
    assert replay['replayed'] is True
    read = execution.read_execution(*scope)
    assert read['pending_questions'][0]['task_ids'] == [task['task_id']]
    assert read['tasks'][0]['blocked_by_questions'] == [question['question_id']]
    assert _claim(scope)['task_id'] == independent['task_id']
    assert _claim(scope) is None
    campaigns.answer_question(*scope, question['question_id'], binding.binding_id, answer='use tags')
    assert _claim(scope)['task_id'] == task['task_id']


def test_foreign_scope_denied_without_writes(make_org):
    scope, _, question = _seed(make_org)
    task = _submit(scope)
    attempt = _claim(scope)
    other_project = store.create_project(scope[0], 'Other project')
    other_org = make_org()
    foreign_project = store.create_project(other_org.org_id, 'Foreign project')
    for foreign in ((scope[0], other_project.project_id, scope[2]),
                    (other_org.org_id, foreign_project.project_id, scope[2])):
        for function, args, kwargs in (
            (_submit, (foreign,), {}), (_claim, (foreign,), {}),
            (execution.read_execution, foreign, {}), (_settle, (foreign, attempt), {}),
            (execution.link_question, (*foreign, task['task_id'], question['question_id']), {}),
            (execution.retry_task, (*foreign, task['task_id']), {}),
            (execution.reconcile_outward, (*foreign, task['task_id']),
             dict(outward_operation_key='key', outcome='failed', result={})),
            (_bind, (foreign, attempt), {}),
            (execution.pending_remote_bindings, foreign, {}),
            (execution.record_remote_admission, (*foreign, attempt['attempt_id']),
             dict(leaf_id='vmc-' + 'a' * 48, run_id='run', submission_digest='b' * 64)),
            (execution.settle_remote_attempt, (*foreign, attempt['attempt_id']),
             dict(fence=attempt['fence'], verdict={'run_id': 'run', 'leaf_id': 'vmc-' + 'a' * 48,
                                                'fencing_token': 1}, outcome='failed', result={})),
        ):
            _code('project_unavailable', function, *args, **kwargs)
    read = execution.read_execution(*scope)
    assert len(read['tasks']) == 1 and len(read['events']) == 2 and read['receipts'] == []


@pytest.mark.parametrize('changes', [
    {'artifact_ref': None, 'result': {'exit_code': 0, 'verify_command': 'python check_recipes.py'}},
    {'result': {'exit_code': 1, 'verify_command': 'python check_recipes.py'}},
    {'result': {'exit_code': 0, 'verify_command': 'another command'}},
])
def test_stage_name_does_not_grant_success(make_org, changes):
    scope, _, _ = _seed(make_org)
    _submit(scope, stages=['build_test'])
    attempt = _claim(scope)
    _code('insufficient_evidence', _settle, scope, attempt, **changes)
    read = execution.read_execution(*scope)
    assert read['tasks'][0]['status'] == 'claimed' and read['receipts'] == []
    _settle(scope, attempt, result={'exit_code': 0, 'verify_command': 'python check_recipes.py'})


def test_store_has_no_ddl_or_job_submission():
    source = (Path(__file__).resolve().parents[1] / 'campaign_execution.py').read_text(encoding='utf-8')
    assert not re.search(r'\b(?:CREATE|ALTER|DROP)\s+(?:TABLE|INDEX|TRIGGER|FUNCTION)', source, re.I)
    assert not re.search(r'\bINSERT\s+INTO\s+jobs\b', source, re.I)
    router = (Path(__file__).resolve().parents[2] / 'server' / 'routers' / 'campaigns.py').read_text(encoding='utf-8')
    for method in ('bind_remote_dispatch', 'record_remote_admission',
                   'settle_remote_attempt', 'pending_remote_bindings'):
        assert method not in router


def _bind(scope, attempt, **changes):
    payload = dict(fence=attempt['fence'], machine_id='recipe-machine', run_id='recipe-run',
                   registration_id='recipe-registration', root_request_id='recipe-root',
                   gateway_project_id='recipe-project', source_ref='a' * 40,
                   packet_digest='b' * 64, budget_class='explicit', reservation_micro_usd=1000000)
    payload.update(changes)
    return execution.bind_remote_dispatch(*scope, attempt['attempt_id'], **payload)


def _admit(scope, binding, **changes):
    payload = {key: binding[key] for key in ('leaf_id', 'run_id', 'submission_digest')}
    payload.update(changes)
    return execution.record_remote_admission(*scope, binding['attempt_id'], **payload)


def _remote_settle(scope, binding, **changes):
    payload = dict(fence=binding['fence'], verdict=dict(run_id=binding['run_id'],
                   leaf_id=binding['leaf_id'], fencing_token=3), outcome='succeeded',
                   result={}, artifact_ref='diff:remote')
    payload.update(changes)
    return execution.settle_remote_attempt(*scope, binding['attempt_id'], **payload)


def test_remote_binding_freezes_gateway_submission_before_restart(make_org):
    scope, _, _ = _seed(make_org)
    _submit(scope)
    attempt = _claim(scope)
    binding = _bind(scope, attempt, machine_id='recipe-é', root_request_id='root-é')
    # Fleet producer: core/claudewalk/fleet/gateway/delegation.py,
    # Submission.model_dump(), canonical_digest and remote_leaf_id (v1).
    # Pin its full JSON bytes independently of the store's serializer.
    # Frozen UTF-8 vector generated from that producer's canonical bytes.
    vector = dict(version=1, request_id='cd-' + '0' * 48, root_request_id='root-é',
                  registration_id='recipe-registration', project_id='recipe-project',
                  source_ref='a' * 40, packet_digest='b' * 64, budget_class='explicit',
                  reservation_micro_usd=1000000, machine_id='recipe-é', run_id='recipe-run',
                  leaf_id='vmc-9f66d7a919b9c914e23e3d748243943547cde9556cff3486')
    assert 'vmc-' + execution._canonical_digest(['recipe-é', vector['request_id']])[:48] == vector['leaf_id']
    assert execution._canonical_digest(vector) == 'eafbfad5977d9ad72919176040c901f4cce826ead73469679fe8e8ef7afa5776'
    leaf_bytes = ('["recipe-é","' + binding['request_id'] + '"]').encode('utf-8')
    assert binding['leaf_id'] == 'vmc-' + hashlib.sha256(leaf_bytes).hexdigest()[:48]
    body = binding['submission']
    assert body == dict(version=1, request_id=binding['request_id'], root_request_id='root-é',
                        registration_id='recipe-registration', project_id='recipe-project',
                        source_ref='a' * 40, packet_digest='b' * 64,
                        budget_class='explicit', reservation_micro_usd=1000000)
    producer_bytes = (
        '{"budget_class":"explicit","leaf_id":"' + binding['leaf_id'] +
        '","machine_id":"recipe-é","packet_digest":"' + 'b' * 64 +
        '","project_id":"recipe-project","registration_id":"recipe-registration",'
        '"request_id":"' + binding['request_id'] + '","reservation_micro_usd":1000000,'
        '"root_request_id":"root-é","run_id":"recipe-run","source_ref":"' + 'a' * 40 +
        '","version":1}').encode('utf-8')
    assert binding['submission_digest'] == hashlib.sha256(producer_bytes).hexdigest()
    # Simulate submit outcome unknown: the only local durable state is bound.
    db.reset_pool()
    pending = execution.pending_remote_bindings(*scope)
    assert len(pending) == 1 and pending[0]['submission'] == body
    replay = _bind(scope, attempt, machine_id='recipe-é', root_request_id='root-é')
    assert replay['replayed'] and replay['submission_digest'] == binding['submission_digest']
    assert replay['leaf_id'] == binding['leaf_id']
    assert attempt['attempt_token'] not in json.dumps(pending)
    admitted = _admit(scope, binding)
    assert admitted['state'] == 'admitted' and admitted['reservation_id'] is None
    db.reset_pool()
    assert _admit(scope, binding)['replayed']
    assert len(execution.read_execution(*scope)['events']) == 4
    _remote_settle(scope, binding)


def test_remote_admission_identity_and_frozen_material(make_org):
    scope, _, _ = _seed(make_org)
    _submit(scope)
    attempt = _claim(scope)
    binding = _bind(scope, attempt)
    for changes in ({'machine_id': 'other'}, {'run_id': 'other'}, {'registration_id': 'other'},
                    {'root_request_id': 'other'}, {'gateway_project_id': 'other'},
                    {'source_ref': 'c' * 40}, {'packet_digest': 'c' * 64},
                    {'budget_class': 'daily'}, {'reservation_micro_usd': 2}):
        _code('dispatch_conflict', _bind, scope, attempt, **changes)
    for changes in ({'leaf_id': 'vmc-' + 'c' * 48}, {'run_id': 'other'},
                    {'submission_digest': 'c' * 64}):
        _code('dispatch_identity_mismatch', _admit, scope, binding, **changes)
    assert len(execution.read_execution(*scope)['events']) == 3
    assert execution.pending_remote_bindings(*scope)[0]['state'] == 'bound'
    _code('dispatch_identity_mismatch', _remote_settle, scope, binding)
    _admit(scope, binding, reservation_id='reservation-1')
    assert _admit(scope, binding)['reservation_id'] == 'reservation-1'
    _code('dispatch_conflict', _admit, scope, binding, reservation_id='reservation-2')
    _code('remote_reconciliation_required', _settle, scope, attempt)
    _remote_settle(scope, binding)
    assert _admit(scope, binding, reservation_id='reservation-1')['replayed']
    _code('dispatch_conflict', _admit, scope, binding, reservation_id='reservation-2')


@pytest.mark.parametrize('stage', execution.STAGES)
@pytest.mark.parametrize('sweep', [False, True])
def test_remote_expiry_in_every_stage_recovers_atomically(make_org, stage, sweep):
    scope, _, _ = _seed(make_org)
    task = _submit(scope, stages=[stage])
    attempt = _claim(scope)
    binding = _bind(scope, attempt)
    _expire(attempt)
    if sweep:
        assert _claim(scope) is None
        assert execution.read_execution(*scope)['tasks'][0]['status'] == 'reconcile_required'
        key = attempt['outward_operation_key'] or binding['request_id']
        _code('remote_reconciliation_required', execution.reconcile_outward, *scope, task['task_id'],
              outward_operation_key=key, outcome='failed', result={})
    db.reset_pool()
    assert _bind(scope, attempt)['replayed']
    assert execution.pending_remote_bindings(*scope)[0]['submission'] == binding['submission']
    _admit(scope, binding)
    receipt = _remote_settle(scope, binding, outcome='failed')
    assert receipt['reconciles_receipt_id'] is not None
    assert receipt['fence'] == attempt['fence'] + 1
    read = execution.read_execution(*scope)
    assert len(read['receipts']) == 2 and read['tasks'][0]['status'] == 'failed'
    unknown = next(row for row in read['receipts'] if row['outcome'] == 'unknown')
    assert unknown['receipt_id'] == receipt['reconciles_receipt_id']
    assert unknown['outward_operation_key'] == (attempt['outward_operation_key'] or binding['request_id'])
    assert execution.pending_remote_bindings(*scope) == []
    db.reset_pool()
    replay = _remote_settle(scope, binding, outcome='failed')
    assert replay['replayed'] and replay['receipt_id'] == receipt['receipt_id']


def test_remote_accepted_settlement_replays_after_later_fence(make_org):
    scope, _, _ = _seed(make_org)
    _submit(scope, stages=['implementation', 'build_test'])
    attempt = _claim(scope)
    binding = _bind(scope, attempt)
    _admit(scope, binding)
    receipt = _remote_settle(scope, binding)
    later = _claim(scope)
    later_binding = _bind(scope, later)
    _expire(later)
    assert _claim(scope) is None
    db.reset_pool()
    replay = _remote_settle(scope, binding)
    assert replay['replayed'] and replay['receipt_id'] == receipt['receipt_id']
    assert _bind(scope, attempt)['replayed']
    assert _admit(scope, binding)['replayed']
    _code('dispatch_conflict', _admit, scope, binding, reservation_id='late-reservation')
    assert [row['attempt_id'] for row in execution.pending_remote_bindings(*scope)] == [later_binding['attempt_id']]
    for verdict in (dict(run_id=binding['run_id'], leaf_id=binding['leaf_id'], fencing_token=4),
                    dict(run_id=binding['run_id'], leaf_id=binding['leaf_id'], fencing_token=3, changed=True)):
        _code('settlement_conflict', _remote_settle, scope, binding, verdict=verdict)
    _code('settlement_conflict', _remote_settle, scope, binding, result={'changed': True})
    assert len(execution.read_execution(*scope)['receipts']) == 2


def test_late_remote_success_advances_and_replays_without_claim_token(make_org):
    scope, _, _ = _seed(make_org)
    _submit(scope, stages=['implementation', 'build_test'])
    attempt = _claim(scope)
    binding = _bind(scope, attempt)
    _expire(attempt)
    db.reset_pool()
    recovered = execution.pending_remote_bindings(*scope)[0]
    _admit(scope, recovered)
    receipt = _remote_settle(scope, recovered)
    read = execution.read_execution(*scope)
    assert read['tasks'][0]['current_stage'] == 'build_test'
    assert read['tasks'][0]['status'] == 'pending'
    assert len(read['receipts']) == 2 and receipt['reconciles_receipt_id'] is not None
    later = _claim(scope)
    assert later['fence'] > receipt['fence']
    db.reset_pool()
    assert _remote_settle(scope, recovered)['receipt_id'] == receipt['receipt_id']
    assert execution.pending_remote_bindings(*scope) == []


def test_remote_source_scope_fence_and_numeric_denials(make_org):
    scope, _, _ = _seed(make_org)
    task = _submit(scope)
    attempt = _claim(scope)
    _code('dispatch_identity_mismatch', _bind, scope, attempt, source_ref='c' * 40)
    _code('stale_attempt', _bind, scope, attempt, fence=attempt['fence'] + 1)
    for value in (True, 1.0, '1', 0, -1, 9223372036854775808):
        _code('invalid_request', _bind, scope, attempt, reservation_micro_usd=value)
    for value in (True, 1.0, '1'):
        _code('invalid_request', _bind, scope, attempt, fence=value)
    assert len(execution.read_execution(*scope)['events']) == 2
    binding = _bind(scope, attempt)
    _admit(scope, binding)
    for value in (True, 3.0, '3', -1, 9223372036854775808):
        _code('dispatch_identity_mismatch', _remote_settle, scope, binding,
              verdict=dict(run_id=binding['run_id'], leaf_id=binding['leaf_id'], fencing_token=value))
    for key in ('run_id', 'leaf_id'):
        verdict = dict(run_id=binding['run_id'], leaf_id=binding['leaf_id'], fencing_token=3)
        verdict[key] = 'other'
        _code('dispatch_identity_mismatch', _remote_settle, scope, binding, verdict=verdict)
    # A moved, unsettled task fence is not a provider acceptance replay.
    with db.connection() as conn:
        conn.execute('UPDATE campaign_tasks SET fence=fence+1 WHERE task_id=%s', (uuid.UUID(task['task_id']),))
    _code('stale_attempt', _remote_settle, scope, binding)
    assert execution.read_execution(*scope)['receipts'] == []


def test_first_remote_binding_requires_live_lease(make_org):
    scope, _, _ = _seed(make_org)
    _submit(scope)
    attempt = _claim(scope)
    _expire(attempt)
    _code('stale_attempt', _bind, scope, attempt)
    assert execution.pending_remote_bindings(*scope) == []
