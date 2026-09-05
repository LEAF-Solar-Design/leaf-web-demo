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
