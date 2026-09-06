"""Real PostgreSQL proofs for the campaign's semantic adoption transaction."""
import base64
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
import uuid

import pytest

from leaf_platform import campaign_plan_adoption as adoption
from leaf_platform import campaign_execution as execution, campaigns, db
from leaf_platform import campaign_enrollment as enrollment
from test_campaign_bridge import mounted, call, bind_body, admit_body, SOURCE


def document(scope):
    return dict(contract='leaf.campaign-plan.v1', campaign_id=str(scope[2]),
        prompt_digest=hashlib.sha256('Organize crêpe recipes\nKeep the full prompt.'.encode()).hexdigest(),
        source_sha=SOURCE, summary='Recipe campaign', open_questions=[], tasks=[
            dict(task_key='recipes', title='Organize recipes', spec='Build recipe storage',
                 capability='recipe.storage', stages=['implementation', 'build_test'],
                 owned_paths=['recipes.py'], depends_on=[], verify_argv=['python', 'recipes.py'],
                 artifacts=['recipes'], questions=[], capabilities_required=[]),
        ])


def prepare(mounted, plan=None, *, raw=None, size=None, product_changes=None, result_changes=None):
    plan = plan or document(mounted.scope)
    raw = raw if raw is not None else json.dumps(plan, ensure_ascii=False).encode()
    if size is not None:
        assert len(raw) <= size
        raw += b' ' * (size - len(raw))
    first = call(mounted, 'next')
    binding = call(mounted, 'bind', **bind_body(first['attempt']))['binding']
    call(mounted, 'admit', **admit_body(binding))
    product = dict(path='.leaf/campaign-plan.json',
                   key='leaf-results/' + binding['run_id'] + '/' + binding['leaf_id'] +
                       '/7/.leaf/campaign-plan.json',
                   sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw), verified=True)
    product.update(product_changes or {})
    result = dict(product=product, result_binding='bound', result_fingerprint='e' * 64,
                  requested_source_sha=SOURCE)
    result.update(result_changes or {})
    receipt = call(mounted, 'settle', attempt_id=binding['attempt_id'], fence=binding['fence'],
                   verdict=dict(run_id=binding['run_id'], leaf_id=binding['leaf_id'], fencing_token=7),
                   outcome='succeeded', result=result, artifact_ref=product['key'])['receipt']
    request = dict(task_id=receipt['task_id'], attempt_id=binding['attempt_id'], fence=binding['fence'],
                   result_fingerprint='e' * 64, plan_sha256=hashlib.sha256(raw).hexdigest(),
                   plan_size_bytes=len(raw), plan_b64=base64.b64encode(raw).decode())
    return request, receipt


def snapshot(mounted):
    return execution.read_execution(*mounted.scope)


def children(mounted):
    return [row for row in snapshot(mounted)['tasks'] if row['parent_task_id'] is not None]


@pytest.mark.parametrize('size', [64001, 262144])
def test_real_postgres_raw_adoption_restart_replay(mounted, size):
    request, implementation = prepare(mounted, size=size)
    before = snapshot(mounted)
    db.reset_pool()
    waiting = call(mounted, 'next')
    source = waiting['plan_source']
    assert waiting['kind'] == 'awaiting_plan_validation'
    assert source['product']['size_bytes'] == size and 'verified' not in source['product']
    assert source['result_fingerprint'] == 'e' * 64
    assert source['result_fingerprint'] != implementation['result_fingerprint']
    assert source['remote_fencing_token'] == 7 and source['fence'] == request['fence']
    response = call(mounted, 'plan', **request)
    assert response['adopted'] == dict(tasks=1, capability_tasks=0, questions=0)
    assert response['receipt']['stage'] == 'build_test'
    db.reset_pool()
    replay = call(mounted, 'plan', **request)
    assert replay == dict(response, receipt=dict(response['receipt'], replayed=True))
    after = snapshot(mounted)
    original = next(row for row in after['receipts'] if row['receipt_id'] == implementation['receipt_id'])
    assert original == next(row for row in before['receipts'] if row['receipt_id'] == original['receipt_id'])
    planning = next(row for row in after['tasks'] if row['task_key'] == 'campaign-plan')
    assert planning['status'] == 'succeeded' and planning['fence'] == request['fence'] + 1
    semantic = next(row for row in after['receipts'] if row['stage'] == 'build_test')
    assert semantic['result']['acceptance'] == adoption.ACCEPTANCE
    assert 'exit_code' not in semantic['result'] and 'remote_fencing_token' not in semantic['result']
    with db.connection() as conn:
        attempt = conn.execute('SELECT * FROM campaign_task_attempts WHERE attempt_id=%s',
            (uuid.UUID(semantic['attempt_id']),)).fetchone()
    assert attempt['worker_id'] == 'semantic-adoption' and attempt['status'] == 'settled'
    assert attempt['fence'] == request['fence'] + 1
    # The enrollment scaffold is pre-existing and unrelated to plan children.
    with execution._cursor() as cur:
        scope = enrollment.resolve_worker_scope(cur, mounted.eid, 'worker-service')
        claimed = execution._claim_task_cursor(cur, scope, worker_id='recipe-worker',
                                              lease=900, task_key='recipes')
    assert claimed and claimed['task_key'] == 'recipes'
    for delta in ({'result_fingerprint': 'f' * 64},
                  {'plan_b64': base64.b64encode(b'{}').decode(), 'plan_size_bytes': 2,
                   'plan_sha256': hashlib.sha256(b'{}').hexdigest()}):
        with pytest.raises(mounted.bridge.BridgeError) as error:
            call(mounted, 'plan', **dict(request, **delta))
        assert error.value.status == 409


@pytest.mark.parametrize('failure', ['duplicate', 'campaign', 'prompt', 'source', 'hash', 'size',
                                      'base64', 'oversized', 'revoked', 'other-enrollment'])
def test_real_postgres_invalid_plan_has_no_durable_effect(mounted, failure):
    plan = document(mounted.scope)
    if failure in ('campaign', 'prompt', 'source'):
        field = {'campaign': 'campaign_id', 'prompt': 'prompt_digest', 'source': 'source_sha'}[failure]
        plan[field] = str(uuid.uuid4()) if failure == 'campaign' else '0' * (40 if failure == 'source' else 64)
    raw = json.dumps(plan).encode()
    if failure == 'duplicate':
        raw = raw.replace(b'"summary":', b'"summary":"duplicate","summary":')
    request, _ = prepare(mounted, raw=raw)
    if failure == 'hash':
        request['plan_sha256'] = 'f' * 64
    elif failure == 'size':
        request['plan_size_bytes'] += 1
    elif failure == 'base64':
        request['plan_b64'] += '\n'
    elif failure == 'oversized':
        raw += b' ' * (262145 - len(raw))
        request.update(plan_b64=base64.b64encode(raw).decode(), plan_size_bytes=len(raw),
                       plan_sha256=hashlib.sha256(raw).hexdigest())
    elif failure == 'revoked':
        enrollment.revoke_enrollment(*mounted.scope, mounted.eid, mounted.principal)
    elif failure == 'other-enrollment':
        other = enrollment.request_enrollment(*mounted.scope, mounted.principal, machine_id='VM-D')
        enrollment.enable_enrollment(*mounted.scope, other['enrollment_id'], mounted.principal)
        with db.connection() as conn:
            conn.execute("UPDATE campaign_host_enrollments SET machine_id='VM-retired' WHERE enrollment_id=%s",
                         (uuid.UUID(mounted.eid),))
            conn.execute("UPDATE campaign_host_enrollments SET machine_id='VM-C' WHERE enrollment_id=%s",
                         (uuid.UUID(other['enrollment_id']),))
        mounted.eid = other['enrollment_id']
    before = snapshot(mounted)
    with pytest.raises(mounted.bridge.BridgeError) as error:
        call(mounted, 'plan', **request)
    expected = (422 if failure in ('duplicate', 'campaign', 'prompt', 'source') else
                413 if failure == 'oversized' else 403 if failure == 'revoked' else 409)
    assert error.value.status == expected
    assert snapshot(mounted) == before and not children(mounted)
    if failure != 'revoked':
        assert call(mounted, 'next')['kind'] == 'awaiting_plan_validation'


@pytest.mark.parametrize('changes', [
    {'product_changes': {'verified': False}},
    {'product_changes': {'path': 'elsewhere.json'}},
    {'product_changes': {'key': '../unsafe'}},
    {'product_changes': {'sha256': 'F' * 64}},
    {'product_changes': {'size_bytes': 262145}},
    {'result_changes': {'result_binding': 'unbound'}},
    {'result_changes': {'requested_source_sha': 'a' * 40}},
    {'result_changes': {'result_fingerprint': 'invalid'}},
])
def test_real_postgres_saved_context_requires_verified_product(mounted, changes):
    request, _ = prepare(mounted, **changes)
    assert call(mounted, 'next')['plan_source'] is None
    before = snapshot(mounted)
    with pytest.raises(mounted.bridge.BridgeError) as error:
        call(mounted, 'plan', **request)
    assert error.value.status == 409 and snapshot(mounted) == before


def test_real_postgres_capabilities_questions_and_dependencies(mounted):
    plan = document(mounted.scope)
    first = plan['tasks'][0]
    first['capabilities_required'] = ['recipe.device']
    first['questions'] = [dict(question_key='format', prompt='Which recipe format?', options=['text', 'json'])]
    second = copy.deepcopy(first)
    second.update(task_key='index', title='Index recipes', owned_paths=['index.py'],
                  depends_on=['recipes'], questions=[])
    independent = copy.deepcopy(first)
    independent.update(task_key='independent', owned_paths=['independent.py'],
                       capabilities_required=[], questions=[])
    plan['tasks'] = [second, independent, first]
    request, _ = prepare(mounted, plan)
    result = call(mounted, 'plan', **request)
    assert result['adopted'] == dict(tasks=3, capability_tasks=1, questions=1)
    tasks = {row['task_key']: row for row in children(mounted)}
    key = 'CAP-' + hashlib.sha256(b'recipe.device').hexdigest()[:24]
    assert set(tasks['recipes']['depends_on']) == {'campaign-plan', key}
    assert set(tasks['index']['depends_on']) == {'campaign-plan', key, 'recipes'}
    assert tasks['independent']['depends_on'] == ['campaign-plan']
    capability = tasks[key]
    assert capability['stages'] == ['implementation', 'build_test', 'publication', 'verification']
    question = snapshot(mounted)['pending_questions'][0]
    assert question['task_ids'] == [tasks['recipes']['task_id']]
    assert len(question['question_key']) <= 128
    campaigns.answer_question(*mounted.scope, question['question_id'], mounted.principal, answer='json')
    with db.connection() as conn:
        link = conn.execute('SELECT * FROM campaign_capability_links WHERE task_id=%s',
                            (uuid.UUID(capability['task_id']),)).fetchone()
    assert str(link['enrollment_id']) == mounted.eid and link['state'] == 'pending_link'
    with execution._cursor() as cur:
        scope = enrollment.resolve_worker_scope(cur, mounted.eid, 'worker-service')
        assert execution._claim_task_cursor(cur, scope, worker_id='worker', lease=900, task_key='recipes') is None
        assert execution._claim_task_cursor(cur, scope, worker_id='worker', lease=900,
                                            task_key='independent') is not None
    # Final ordinary verification cannot settle a pending linked capability.
    with db.connection() as conn:
        conn.execute("UPDATE campaign_tasks SET current_stage='verification' WHERE task_id=%s",
                     (uuid.UUID(capability['task_id']),))
    with execution._cursor() as cur:
        scope = enrollment.resolve_worker_scope(cur, mounted.eid, 'worker-service')
        claimed = execution._claim_task_cursor(cur, scope, worker_id='worker', lease=900, task_key=key)
    with pytest.raises(execution.CampaignError) as error:
        execution.settle_attempt(*mounted.scope, claimed['attempt_id'], attempt_token=claimed['attempt_token'],
            fence=claimed['fence'], outcome='succeeded', result={'observed': True}, verified=True)
    assert error.value.code == 'insufficient_evidence'
    assert next(row for row in children(mounted) if row['task_key'] == key)['status'] == 'claimed'


def test_real_postgres_open_questions_link_every_plan_task(mounted):
    plan = document(mounted.scope)
    plan['open_questions'] = [dict(question_key='audience', prompt='Who reads the recipes?')]
    second = copy.deepcopy(plan['tasks'][0])
    second.update(task_key='second', owned_paths=['second.py'])
    plan['tasks'].append(second)
    request, _ = prepare(mounted, plan)
    assert call(mounted, 'plan', **request)['adopted']['questions'] == 1
    question = snapshot(mounted)['pending_questions'][0]
    assert set(question['task_ids']) == {row['task_id'] for row in children(mounted)}


def test_real_postgres_exception_rolls_back_all_adoption_rows(mounted, monkeypatch):
    plan = document(mounted.scope)
    plan['tasks'][0]['capabilities_required'] = ['recipe.device']
    request, _ = prepare(mounted, plan)
    before = snapshot(mounted)
    submit = execution._submit_task_cursor
    calls = []

    def crash(*args, **kwargs):
        row = submit(*args, **kwargs)
        calls.append(row['task_id'])
        raise RuntimeError('forced failure after first child insert')

    with monkeypatch.context() as patch:
        patch.setattr(execution, '_submit_task_cursor', crash)
        with pytest.raises(mounted.bridge.BridgeError) as error:
            call(mounted, 'plan', **request)
        assert error.value.status == 503
    assert len(calls) == 1 and snapshot(mounted) == before
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) AS count FROM campaign_capability_links '
            'WHERE campaign_id=%s', (uuid.UUID(str(mounted.scope[2])),)).fetchone()['count'] == 1
    assert call(mounted, 'plan', **request)['adopted'] == dict(tasks=1, capability_tasks=1, questions=0)
    assert len(children(mounted)) == 2


def test_real_postgres_concurrent_exact_replay(mounted):
    request, _ = prepare(mounted)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: call(mounted, 'plan', **request), range(2)))
    assert responses[0]['receipt']['receipt_id'] == responses[1]['receipt']['receipt_id']
    assert sorted(row['receipt']['replayed'] for row in responses) == [False, True]
    assert len(children(mounted)) == 1
    assert len([row for row in snapshot(mounted)['receipts'] if row['stage'] == 'build_test']) == 1


@pytest.mark.parametrize('delta,status', [
    ({'budget': 100}, 400), ({'fence': True}, 400),
    ({'plan_size_bytes': True}, 400), ({'plan_sha256': 'A' * 64}, 400),
    ({'task_id': str(uuid.uuid4())}, 409), ({'attempt_id': str(uuid.uuid4())}, 409),
])
def test_real_postgres_plan_request_rejects_overrides_and_wrong_identity(mounted, delta, status):
    request, _ = prepare(mounted)
    before = snapshot(mounted)
    with pytest.raises(mounted.bridge.BridgeError) as error:
        call(mounted, 'plan', **dict(request, **delta))
    assert error.value.status == status and snapshot(mounted) == before


def test_real_postgres_ordinary_build_evidence_cannot_forge_adoption(mounted):
    request, _ = prepare(mounted)
    with execution._cursor() as cur:
        scope = enrollment.resolve_worker_scope(cur, mounted.eid, 'worker-service')
        claimed = execution._claim_task_cursor(cur, scope, worker_id='worker', lease=900,
                                              task_key='campaign-plan')
    before = snapshot(mounted)
    with pytest.raises(execution.CampaignError) as error:
        execution.settle_attempt(*mounted.scope, claimed['attempt_id'], attempt_token=claimed['attempt_token'],
            fence=claimed['fence'], outcome='succeeded', artifact_ref='plan',
            result=dict(acceptance=adoption.ACCEPTANCE, exit_code=0,
                        verify_command='python -m json.tool .leaf/campaign-plan.json'))
    assert error.value.code == 'insufficient_evidence'
    assert snapshot(mounted) == before and not children(mounted)
