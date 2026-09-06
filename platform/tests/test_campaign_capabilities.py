"""Real PostgreSQL lifecycle for ReciPDF's published host capability."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import time
import uuid

import pytest
from psycopg.errors import ForeignKeyViolation, RaiseException
from psycopg.types.json import Jsonb

from leaf_platform import campaigns, campaign_capabilities as capabilities, campaign_enrollment as enrollment, db, store


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


@pytest.fixture
def seeded(make_org, monkeypatch):
    machine = 'test-host-' + str(uuid.uuid4())
    monkeypatch.setenv('LEAF_CAMPAIGN_ALLOWED_MACHINES', machine)
    monkeypatch.setenv('LEAF_CAMPAIGN_HOST_MACHINE_ID', machine)
    monkeypatch.setenv('LEAF_CAMPAIGN_WORKER_SUBJECT', 'worker-service')
    monkeypatch.setenv('LEAF_SOURCE_SHA', 'a' * 40)
    org = make_org()
    project = store.create_project(org.org_id, 'Host capability project')
    principal = store.create_identity_binding(org.org_id, 'auth0', 'auth0|' + str(uuid.uuid4()), role='owner')
    with db.connection() as conn:
        conn.execute('INSERT INTO project_member_bindings '
                     '(membership_id,org_id,project_id,binding_id,role,invited_by_binding_id) '
                     "VALUES (%s,%s,%s,%s,'owner',%s)",
                     (uuid.uuid4(), org.org_id, project.project_id, principal.binding_id, principal.binding_id))
    campaign = campaigns.submit_campaign(org.org_id, project.project_id, 'tenant-' + str(uuid.uuid4()),
        principal.binding_id, title='ReciPDF', prompt='Organize recipes', idempotency_key='host')
    scope = (org.org_id, project.project_id, campaign['campaign_id'])
    row = enrollment.request_enrollment(*scope, principal.binding_id, machine_id=machine)
    return scope, principal.binding_id, row['enrollment_id']


@pytest.fixture
def publication():
    return {'change_set_id': 'change-' + str(uuid.uuid4()), 'catalog_commit': 'b' * 40,
            'effective_catalog_digest': 'c' * 64, 'tool_name': 'campaign-host-enrollment',
            'tool_manifest_sha256': 'sha256:' + 'd' * 64, 'tool_source_sha256': 'e' * 64}


@pytest.fixture
def host_grant_seeded(make_org, monkeypatch):
    machine = 'test-host-grant-' + str(uuid.uuid4())
    monkeypatch.setenv('LEAF_CAMPAIGN_ALLOWED_MACHINES', machine)
    monkeypatch.setenv('LEAF_CAMPAIGN_HOST_MACHINE_ID', machine)
    monkeypatch.setenv('LEAF_CAMPAIGN_WORKER_SUBJECT', 'worker-service')
    monkeypatch.setenv('LEAF_SOURCE_SHA', 'a' * 40)
    org = make_org()
    project = store.create_project(org.org_id, 'Host grant project')
    principal = store.create_identity_binding(org.org_id, 'auth0', 'auth0|' + str(uuid.uuid4()), role='owner')
    with db.connection() as conn:
        conn.execute('INSERT INTO project_member_bindings '
                     '(membership_id,org_id,project_id,binding_id,role,invited_by_binding_id) '
                     "VALUES (%s,%s,%s,%s,'owner',%s)",
                     (uuid.uuid4(), org.org_id, project.project_id, principal.binding_id, principal.binding_id))
    campaign = campaigns.submit_campaign(org.org_id, project.project_id, str(org.org_id),
        principal.binding_id, title='ReciPDF', prompt='Organize recipes', idempotency_key='host-grant')
    scope = (org.org_id, project.project_id, campaign['campaign_id'])
    row = enrollment.request_enrollment(*scope, principal.binding_id, machine_id=machine)
    return scope, principal.binding_id, row['enrollment_id']


@pytest.fixture
def host_grant_claimed(host_grant_seeded, publication):
    context = ready(host_grant_seeded, publication)
    authority = store.register_project_repository_authority(
        context['tenant_id'], context['org_id'], context['project_id'], str(uuid.uuid4()))
    jid = job(context)
    capabilities.ensure_operation(jid, context)
    return context, authority, claim()


def host_grant_request(op):
    return {key: op[key] for key in ('operation_id', 'claim')}


def host_grant_snapshot(op):
    with db.connection() as conn:
        return [conn.execute(sql, (value,)).fetchone() for sql, value in (
            ('SELECT * FROM campaign_host_operations WHERE operation_id=%s', uuid.UUID(op['operation_id'])),
            ('SELECT * FROM campaign_capability_invocations WHERE job_id=%s', uuid.UUID(op['job_id'])),
            ('SELECT * FROM async_jobs WHERE job_id=%s', op['job_id']),
            ('SELECT * FROM campaign_host_enrollments WHERE enrollment_id=%s', uuid.UUID(op['enrollment_id'])),
            ('SELECT * FROM campaign_capability_links WHERE link_id=%s', uuid.UUID(op['link_id'])))]


def test_host_grant_exact_authority_read_preserves_state(host_grant_seeded, host_grant_claimed):
    context, authority, op = host_grant_claimed
    settle(op)
    before = host_grant_snapshot(op)
    execution = lifecycle_snapshot(host_grant_seeded)
    expected = {'ok': True, 'kind': 'grant', 'grant': {
        'operation_id': op['operation_id'], 'enrollment_id': context['enrollment_id'],
        'link_id': context['link_id'], 'machine_id': op['machine_id'],
        'campaign_id': context['campaign_id'], 'leaf_project_id': context['project_id'],
        'repository_key': authority['repo_key'], 'input_sha256': op['input_sha256']}}
    assert capabilities.read_host_grant('worker-service', host_grant_request(op)) == expected
    db.reset_pool()
    assert capabilities.read_host_grant('worker-service', host_grant_request(op)) == expected
    assert host_grant_snapshot(op) == before
    assert lifecycle_snapshot(host_grant_seeded) == execution
    assert op['claim'] not in json.dumps(expected)
    assert hashlib.sha256(op['claim'].encode()).hexdigest() not in json.dumps(expected)


@pytest.mark.parametrize('damage', [
    'wrong_claim', 'expired', 'foreign_worker', 'foreign_machine', 'revoked', 'closed',
    'terminal', 'context', 'input', 'project', 'missing_operation'])
def test_host_grant_rejects_stale_or_foreign_state(
        host_grant_seeded, host_grant_claimed, monkeypatch, damage):
    context, _, op = host_grant_claimed
    request = host_grant_request(op)
    subject = 'worker-service'
    if damage == 'wrong_claim':
        request['claim'] = 'x' * 43
    elif damage == 'foreign_worker':
        subject = 'foreign-worker'
    elif damage == 'foreign_machine':
        monkeypatch.setenv('LEAF_CAMPAIGN_ALLOWED_MACHINES', 'foreign-machine')
        monkeypatch.setenv('LEAF_CAMPAIGN_HOST_MACHINE_ID', 'foreign-machine')
    elif damage == 'revoked':
        scope, principal, eid = host_grant_seeded
        enrollment.revoke_enrollment(*scope, eid, principal)
    elif damage == 'terminal':
        settle(op, outcome='failed')
    elif damage == 'missing_operation':
        request['operation_id'] = str(uuid.uuid4())
    else:
        with db.connection() as conn:
            if damage == 'expired':
                conn.execute("UPDATE campaign_host_operations SET lease_expires_at=NOW()-interval '1 second' "
                             'WHERE operation_id=%s', (uuid.UUID(op['operation_id']),))
            elif damage == 'closed':
                conn.execute("UPDATE async_jobs SET progress='closed' WHERE job_id=%s", (op['job_id'],))
            elif damage == 'context':
                conn.execute('UPDATE async_jobs SET execution_json=%s WHERE job_id=%s',
                             (Jsonb({'capability_provenance': {**context, 'link_id': str(uuid.uuid4())}}),
                              op['job_id']))
            elif damage == 'input':
                conn.execute('UPDATE campaign_host_operations SET input_sha256=%s WHERE operation_id=%s',
                             ('0' * 64, uuid.UUID(op['operation_id'])))
            else:
                conn.execute('UPDATE projects SET deleted_at=NOW() WHERE project_id=%s',
                             (uuid.UUID(context['project_id']),))
    before = host_grant_snapshot(op)
    error = campaigns.CampaignError if damage == 'foreign_worker' else campaigns.CampaignConflict
    with pytest.raises(error) as exc:
        capabilities.read_host_grant(subject, request)
    assert op['claim'] not in str(exc.value)
    assert hashlib.sha256(op['claim'].encode()).hexdigest() not in str(exc.value)
    assert host_grant_snapshot(op) == before


def test_host_grant_missing_mapping_ignores_other_project_authority(host_grant_seeded, publication):
    context = ready(host_grant_seeded, publication)
    other = store.create_project(uuid.UUID(context['org_id']), 'Other repository')
    store.register_project_repository_authority(
        context['tenant_id'], context['org_id'], other.project_id, uuid.uuid4())
    jid = job(context)
    capabilities.ensure_operation(jid, context)
    op = claim()
    before = host_grant_snapshot(op)
    with pytest.raises(capabilities.CampaignUnavailable, match='Host grant is unavailable'):
        capabilities.read_host_grant('worker-service', host_grant_request(op))
    assert host_grant_snapshot(op) == before
    assert store.resolve_project_repository_authority(
        context['tenant_id'], context['org_id'], context['project_id']) is None


def test_host_grant_legacy_tenant_is_sanitized_unavailable(seeded, publication):
    context = ready(seeded, publication)
    jid = job(context)
    capabilities.ensure_operation(jid, context)
    op = claim()
    before = host_grant_snapshot(op)
    with pytest.raises(capabilities.CampaignUnavailable, match='Host grant is unavailable') as exc:
        capabilities.read_host_grant('worker-service', host_grant_request(op))
    assert context['tenant_id'] not in str(exc.value)
    assert host_grant_snapshot(op) == before


@pytest.mark.parametrize('damage', ['extra', 'missing', 'uuid', 'claim', 'empty', 'list'])
def test_host_grant_closed_request(host_grant_claimed, damage):
    _, _, op = host_grant_claimed
    request = host_grant_request(op)
    if damage == 'extra':
        request['repository_key'] = str(uuid.uuid4())
    elif damage == 'missing':
        del request['claim']
    elif damage == 'uuid':
        request['operation_id'] = '{' + op['operation_id'] + '}'
    elif damage == 'claim':
        request['claim'] = 'bad claim'
    elif damage == 'empty':
        request = {}
    else:
        request = []
    before = host_grant_snapshot(op)
    with pytest.raises(campaigns.CampaignError):
        capabilities.read_host_grant('worker-service', request)
    assert host_grant_snapshot(op) == before


def ready(seeded, publication):
    scope, principal, eid = seeded
    capabilities.bind_publication(*scope, eid, principal, publication=publication)
    enrollment.enable_enrollment(*scope, eid, principal)
    return capabilities.invocation_context(*scope, eid, principal)


def job(context):
    job_id = str(uuid.uuid4())
    now = time.time()
    with db.connection() as conn:
        conn.execute('INSERT INTO async_jobs (job_id,tenant_id,tool,params_json,dwg,status,progress,'
                     'created_at,updated_at,execution_json,org_id,project_id,submission_fingerprint,attempt) '
                     "VALUES (%s,%s,%s,%s,'','running','host',%s,%s,%s,%s,%s,%s,1)",
                     (job_id, context['tenant_id'], context['tool_name'], Jsonb({}), now, now,
                      Jsonb({'capability_provenance': context}), context['org_id'], context['project_id'], 'f' * 64))
    return job_id


def claim():
    return capabilities.claim_host_operation('worker-service', {})['operation']


def body(op, stage='apply', outcome='succeeded'):
    return {**{key: op[key] for key in ('operation_id', 'attempt', 'fence', 'claim', 'input_sha256')},
            'stage': stage, 'outcome': outcome,
            'evidence': {'config_identity_before': '1' * 64, 'config_identity_after': '2' * 64,
                         'readback_sha256': '3' * 64,
                         'reason': 'verified' if outcome == 'succeeded' else 'validation_failed'}}


def settle(op, stage='apply', outcome='succeeded'):
    return capabilities.settle_host_operation('worker-service', body(op, stage, outcome))


def finish(context, job_id):
    op = claim()
    assert op['job_id'] == job_id
    for stage in capabilities.STAGES:
        settle(op, stage)
    with db.connection() as conn:
        row = conn.execute("UPDATE async_jobs SET status='complete', progress='done', finished_at=%s, "
                           'elapsed_ms=10 WHERE job_id=%s RETURNING *', (time.time(), job_id)).fetchone()
    # Use the actual producer, retaining all existing names and shapes.
    path = Path(__file__).resolve().parents[2] / 'server' / 'build_receipts.py'
    spec = importlib.util.spec_from_file_location('host_test_build_receipts', path)
    producer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(producer)
    receipt = producer.build_receipt(dict(row), source_sha='a' * 40)
    receipt.update(org_id=context['org_id'], project_id=context['project_id'],
                   capability_provenance=context, host_readback=body(op, 'readback')['evidence'])
    receipt['digest'] = producer._digest(receipt)
    return receipt


def count(seeded, job_id, receipt):
    scope, _, eid = seeded
    return capabilities.count_invocation(*scope, eid, job_id=job_id, receipt=receipt)


def test_publication_replay_conflict_and_enabled_invocation(seeded, publication):
    scope, principal, eid = seeded
    with pytest.raises(campaigns.CampaignError):
        capabilities.invocation_context(*scope, eid, principal)
    first = capabilities.bind_publication(*scope, eid, principal, publication=publication)
    assert first['capability_link']['publication_id'] == publication['change_set_id']
    assert first['capability_link']['effective_catalog_id'] == publication['effective_catalog_digest']
    assert capabilities.bind_publication(*scope, eid, principal, publication=publication)['replayed']
    with pytest.raises(campaigns.CampaignConflict):
        capabilities.bind_publication(*scope, eid, principal, publication={**publication, 'catalog_commit': 'f' * 40})
    with pytest.raises(campaigns.CampaignError):
        capabilities.invocation_context(*scope, eid, principal)
    enrollment.enable_enrollment(*scope, eid, principal)
    context = capabilities.invocation_context(*scope, eid, principal)
    assert context['tenant_id'].startswith('tenant-')
    assert set(context) == capabilities.CONTEXT_KEYS
    with db.connection() as conn:
        conn.execute("UPDATE project_member_bindings SET role='read_only' WHERE binding_id=%s", (principal,))
    for operation, kwargs in ((capabilities.bind_publication, {'publication': publication}),
                              (capabilities.invocation_context, {})):
        with pytest.raises(campaigns.CampaignError):
            operation(*scope, eid, principal, **kwargs)


def test_enabled_without_publication_cannot_invoke(seeded):
    scope, principal, eid = seeded
    enrollment.enable_enrollment(*scope, eid, principal)
    with pytest.raises(campaigns.CampaignError):
        capabilities.invocation_context(*scope, eid, principal)


def test_operation_restart_exact_context_and_scope(seeded, publication):
    context = ready(seeded, publication)
    jid = job(context)
    first = capabilities.ensure_operation(jid, context)
    db.reset_pool()
    replay = capabilities.ensure_operation(jid, context)
    assert replay['operation_id'] == first['operation_id'] and replay['replayed']
    assert first['input_sha256'] == digest({'schema': 'leaf.campaign-host-operation.v1', 'job_id': jid, 'context': context})
    for changed in ({**context, 'tenant_id': 'foreign'}, {**context, 'tool_source_sha256': 'f' * 64},
                    {**context, 'project_id': str(uuid.uuid4())}, {**context, 'extra': True}):
        with pytest.raises(campaigns.CampaignError):
            capabilities.ensure_operation(jid, changed)
    foreign = job({**context, 'project_id': str(uuid.uuid4())})
    with pytest.raises(campaigns.CampaignError):
        capabilities.ensure_operation(foreign, context)
    with pytest.raises(campaigns.CampaignError):
        capabilities.ensure_operation(str(uuid.uuid4()), context)
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) AS n FROM campaign_host_operations WHERE job_id=%s',
                            (uuid.UUID(jid),)).fetchone()['n'] == 1
        assert conn.execute('SELECT count(*) AS n FROM campaign_capability_invocations WHERE job_id=%s',
                            (uuid.UUID(jid),)).fetchone()['n'] == 1


def test_claim_hash_resume_machine_serialization_and_restart(seeded, publication):
    context = ready(seeded, publication)
    ids = [job(context), job(context)]
    for jid in ids:
        capabilities.ensure_operation(jid, context)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: capabilities.claim_host_operation('worker-service', {}), range(2)))
    assert sorted(result['kind'] for result in results) == ['claimed', 'idle']
    op = next(result['operation'] for result in results if result['kind'] == 'claimed')
    assert len(op['claim']) == 43
    with db.connection() as conn:
        stored = conn.execute('SELECT * FROM campaign_host_operations WHERE operation_id=%s',
                              (uuid.UUID(op['operation_id']),)).fetchone()
    assert stored['claim_sha256'] == hashlib.sha256(op['claim'].encode()).hexdigest()
    assert op['claim'] not in str(stored)
    public = capabilities.read_operation(op['job_id'], context)
    assert not {'claim', 'claim_sha256', 'service_subject'} & set(public)
    resumed = capabilities.claim_host_operation('worker-service',
                {'operation_id': op['operation_id'], 'claim': op['claim']})['operation']
    assert resumed['attempt'] == op['attempt'] and resumed['fence'] == op['fence']
    settle(op)
    with db.connection() as conn:
        conn.execute("UPDATE campaign_host_operations SET lease_expires_at=NOW()-interval '1 second' "
                     'WHERE operation_id=%s', (uuid.UUID(op['operation_id']),))
    db.reset_pool()
    recovered = claim()
    assert recovered['operation_id'] == op['operation_id']
    assert recovered['stage'] == 'activate' and recovered['completed_stages'] == ['apply']
    assert recovered['fence'] == op['fence'] + 1 and recovered['claim'] != op['claim']
    with pytest.raises(campaigns.CampaignConflict):
        settle(op, 'activate')
    assert settle(recovered, 'activate')['completed_stages'] == ['apply', 'activate']


def test_monotonic_settlement_and_stale_proofs(seeded, publication):
    context = ready(seeded, publication)
    jid = job(context)
    capabilities.ensure_operation(jid, context)
    op = claim()
    with pytest.raises(campaigns.CampaignConflict):
        settle(op, 'activate')
    for key, value in (('claim', 'x' * 43), ('attempt', 2), ('fence', 2), ('input_sha256', 'f' * 64)):
        with pytest.raises(campaigns.CampaignConflict):
            capabilities.settle_host_operation('worker-service', {**body(op), key: value})
    assert not settle(op)['replayed']
    assert settle(op)['replayed']
    changed = body(op)
    changed['evidence']['readback_sha256'] = '4' * 64
    with pytest.raises(campaigns.CampaignConflict):
        capabilities.settle_host_operation('worker-service', changed)
    settle(op, 'activate')
    assert settle(op, 'readback')['completed_stages'] == list(capabilities.STAGES)
    assert settle(op, 'readback')['replayed']


@pytest.mark.parametrize('outcome', ['held', 'failed'])
def test_non_success_preserves_predecessors(seeded, publication, outcome):
    context = ready(seeded, publication)
    jid = job(context)
    capabilities.ensure_operation(jid, context)
    op = claim()
    settle(op)
    settle(op, 'activate', outcome)
    state = capabilities.read_operation(jid, context)
    assert state['outcome'] == outcome and state['completed_stages'] == ['apply']
    assert state['stage_evidence']['apply'] == {'outcome': 'succeeded', 'evidence': body(op)['evidence']}
    with pytest.raises(campaigns.CampaignConflict):
        settle(op, 'readback')


@pytest.mark.parametrize('cancel', ['revoked', 'closed', 'failed', 'complete', 'project'])
def test_cancellation_blocks_activation(seeded, publication, cancel):
    context = ready(seeded, publication)
    jid = job(context)
    capabilities.ensure_operation(jid, context)
    op = claim()
    settle(op)
    if cancel == 'revoked':
        scope, principal, eid = seeded
        enrollment.revoke_enrollment(*scope, eid, principal)
    else:
        with db.connection() as conn:
            if cancel == 'closed':
                conn.execute("UPDATE async_jobs SET progress='closed' WHERE job_id=%s", (jid,))
            elif cancel == 'project':
                conn.execute('UPDATE projects SET deleted_at=NOW() WHERE project_id=%s',
                             (uuid.UUID(context['project_id']),))
            else:
                conn.execute('UPDATE async_jobs SET status=%s WHERE job_id=%s', (cancel, jid))
    with pytest.raises(campaigns.CampaignConflict):
        settle(op, 'activate')
    assert capabilities.read_operation(jid, context)['completed_stages'] == ['apply']


def test_closed_job_readback_retains_evidence_without_counting(seeded, publication):
    context = ready(seeded, publication)
    jid = job(context)
    capabilities.ensure_operation(jid, context)
    op = claim()
    settle(op)
    settle(op, 'activate')
    with db.connection() as conn:
        conn.execute("UPDATE async_jobs SET progress='closed' WHERE job_id=%s", (jid,))
    assert settle(op, 'readback')['completed_stages'] == list(capabilities.STAGES)
    assert capabilities.read_operation(jid, context)['outcome'] == 'succeeded'
    assert enrollment.list_enrollments(*seeded[0])[0]['capability_link']['counted_job_ids'] == []


def lifecycle_snapshot(seeded):
    return enrollment.execution.read_execution(*seeded[0])


def completed_uses(seeded, publication):
    context = ready(seeded, publication)
    uses = []
    for _ in range(2):
        jid = job(context)
        capabilities.ensure_operation(jid, context)
        uses.append((jid, finish(context, jid)))
    return context, uses


def assert_one_verification(seeded, publication, uses):
    snapshot = lifecycle_snapshot(seeded)
    assert len(snapshot['tasks']) == 1 and snapshot['tasks'][0]['status'] == 'succeeded'
    task = snapshot['tasks'][0]
    assert task['stages'] == ['verification'] and task['fence'] == 1
    assert len(snapshot['receipts']) == 1
    receipt = snapshot['receipts'][0]
    assert receipt['stage'] == 'verification' and receipt['outcome'] == 'succeeded'
    assert receipt['verified'] is True
    assert 'exit_code' not in receipt['result'] and 'verify_command' not in receipt['result']
    observed = receipt['result']['observed']
    assert all(observed['publication_binding'][key] == value for key, value in publication.items())
    assert observed['publication_binding']['publication_id'] == publication['change_set_id']
    assert [(use['job_id'], use['receipt_id'], use['receipt_digest'])
            for use in observed['invocations']] == [
                (jid, proof.get('receipt_id', proof['digest']), proof['digest']) for jid, proof in uses]
    assert all(use['readback_sha256'] == '3' * 64 for use in observed['invocations'])
    assert [event['event_type'] for event in snapshot['events']].count('stage_succeeded') == 1
    with db.connection() as conn:
        attempts = conn.execute('SELECT * FROM campaign_task_attempts WHERE task_id=%s',
                                (uuid.UUID(task['task_id']),)).fetchall()
    assert len(attempts) == 1 and attempts[0]['worker_id'] == 'capability-lifecycle'
    assert attempts[0]['status'] == 'settled' and attempts[0]['settled_at'] is not None
    assert attempts[0]['stage'] == 'verification' and attempts[0]['fence'] == 1
    assert len(attempts[0]['attempt_token_hash']) == 64
    assert 'attempt_token_hash' not in str(snapshot)
    return receipt


def test_second_use_settles_once_under_concurrency_and_reconnect(seeded, publication):
    _, uses = completed_uses(seeded, publication)
    count(seeded, *uses[0])
    first = lifecycle_snapshot(seeded)
    assert first['tasks'][0]['status'] == 'pending'
    assert first['tasks'][0]['fence'] == 0 and first['receipts'] == []
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: count(seeded, *uses[1]), range(2)))
    assert sum(result['replayed'] for result in results) == 1
    receipt = assert_one_verification(seeded, publication, uses)
    db.reset_pool()
    for use in uses:
        assert count(seeded, *use)['replayed']
    assert assert_one_verification(seeded, publication, uses) == receipt


def test_completed_link_recovers_missing_settlement_from_exact_replay(seeded, publication, monkeypatch):
    _, uses = completed_uses(seeded, publication)
    complete = enrollment.execution._complete_host_capability
    # Reproduce the accepted prior producer: it counted actual proofs but had no settler.
    monkeypatch.setattr(enrollment.execution, '_complete_host_capability', lambda *args: None)
    for use in uses:
        count(seeded, *use)
    assert lifecycle_snapshot(seeded)['receipts'] == []
    assert enrollment.list_enrollments(*seeded[0])[0]['capability_link']['state'] == 'completed'
    monkeypatch.setattr(enrollment.execution, '_complete_host_capability', complete)
    db.reset_pool()
    assert count(seeded, *uses[1])['replayed']
    assert_one_verification(seeded, publication, uses)


def test_failed_completion_transaction_leaves_no_partial_second_count(seeded, publication, monkeypatch):
    _, uses = completed_uses(seeded, publication)
    count(seeded, *uses[0])
    original = enrollment.execution._event

    def interrupt(cur, scope, task, event_type, *args, **kwargs):
        if event_type == 'stage_succeeded':
            raise RuntimeError('crash before commit')
        return original(cur, scope, task, event_type, *args, **kwargs)

    monkeypatch.setattr(enrollment.execution, '_event', interrupt)
    with pytest.raises(campaigns.CampaignUnavailable):
        count(seeded, *uses[1])
    assert lifecycle_snapshot(seeded)['receipts'] == []
    assert lifecycle_snapshot(seeded)['tasks'][0]['fence'] == 0
    assert enrollment.list_enrollments(*seeded[0])[0]['capability_link']['state'] == 'invoked_once'
    monkeypatch.setattr(enrollment.execution, '_event', original)
    count(seeded, *uses[1])
    assert_one_verification(seeded, publication, uses)


@pytest.mark.parametrize('damage', [
    'failed', 'cancelled', 'closed', 'running', 'missing_identity', 'readback',
    'foreign_context', 'missing_operation'])
def test_second_count_rechecks_first_stored_proof(seeded, publication, damage):
    context, uses = completed_uses(seeded, publication)
    count(seeded, *uses[0])
    with db.connection() as conn:
        if damage in ('cancelled', 'closed'):
            conn.execute("UPDATE async_jobs SET progress='closed' WHERE job_id=%s", (uses[0][0],))
        elif damage in ('failed', 'running'):
            conn.execute('UPDATE async_jobs SET status=%s WHERE job_id=%s', (damage, uses[0][0]))
        elif damage == 'foreign_context':
            conn.execute('UPDATE async_jobs SET execution_json=%s WHERE job_id=%s',
                         (Jsonb({'capability_provenance': {**context, 'link_id': str(uuid.uuid4())}}),
                          uses[0][0]))
        elif damage == 'missing_operation':
            conn.execute('DELETE FROM campaign_host_operations WHERE job_id=%s',
                         (uuid.UUID(uses[0][0]),))
        elif damage == 'missing_identity':
            conn.execute('UPDATE campaign_capability_links SET first_invocation_receipt_id=%s '
                         'WHERE link_id=%s', ('missing-proof', uuid.UUID(context['link_id'])))
        else:
            conn.execute("UPDATE campaign_host_operations SET stage_evidence=stage_evidence - 'readback' "
                         'WHERE job_id=%s', (uuid.UUID(uses[0][0]),))
    with pytest.raises(campaigns.CampaignConflict):
        count(seeded, *uses[1])
    assert lifecycle_snapshot(seeded)['receipts'] == []
    assert lifecycle_snapshot(seeded)['tasks'][0]['status'] == 'pending'


def test_link_state_alone_cannot_complete_task(seeded, publication):
    context = ready(seeded, publication)
    with db.connection() as conn:
        conn.execute("UPDATE campaign_capability_links SET state='completed', "
                     'first_invocation_receipt_id=%s, second_invocation_receipt_id=%s WHERE link_id=%s',
                     ('absent-first', 'absent-second', uuid.UUID(context['link_id'])))
    scope = dict(zip(('org', 'project', 'campaign'), map(uuid.UUID, map(str, seeded[0]))))
    with pytest.raises(campaigns.CampaignConflict):
        with enrollment.execution._cursor() as cur:
            enrollment.execution._complete_host_capability(cur, scope, seeded[2])
    assert lifecycle_snapshot(seeded)['receipts'] == []



def test_two_terminal_producer_receipts_count_distinct_jobs(seeded, publication):
    context = ready(seeded, publication)
    first = job(context)
    capabilities.ensure_operation(first, context)
    receipt = finish(context, first)
    for key, value in (('status', 'failed'), ('source_sha', 'unknown'),
                       ('host_readback', {}), ('tenant_id', 'foreign'), ('digest', '0' * 64)):
        bad = deepcopy(receipt)
        bad[key] = value
        if key != 'digest':
            bad['digest'] = digest({k: v for k, v in bad.items() if k != 'digest'})
        with pytest.raises(campaigns.CampaignError):
            count(seeded, first, bad)
    assert enrollment.list_enrollments(*seeded[0])[0]['capability_link']['state'] == 'published'
    result = count(seeded, first, receipt)
    assert result['capability_link']['state'] == 'invoked_once'
    assert result['capability_link']['first_invocation_receipt_id'] == receipt['digest']
    assert count(seeded, first, receipt)['replayed']
    second = job(context)
    capabilities.ensure_operation(second, context)
    second_receipt = finish(context, second)
    second_receipt['receipt_id'] = 'producer-' + str(uuid.uuid4())
    second_receipt['digest'] = digest({k: v for k, v in second_receipt.items() if k != 'digest'})
    result = count(seeded, second, second_receipt)
    link = result['capability_link']
    assert link['state'] == 'completed' and set(link['counted_job_ids']) == {first, second}
    assert link['second_invocation_receipt_id'] == second_receipt['receipt_id']
    scope, principal, eid = seeded
    assert capabilities.invocation_context(*scope, eid, principal) == context
    assert count(seeded, second, second_receipt)['replayed']
    for key, value in (('status', 'failed'), ('job_id', str(uuid.uuid4())), ('tool', 'foreign'),
                       ('source_sha', 'unknown'), ('project_id', str(uuid.uuid4())),
                       ('host_readback', {}), ('capability_provenance', {**context, 'catalog_commit': 'f' * 40}),
                       ('written_at', receipt['written_at'] + 1)):
        bad = deepcopy(receipt)
        bad[key] = value
        bad['digest'] = digest({k: v for k, v in bad.items() if k != 'digest'})
        with pytest.raises(campaigns.CampaignError):
            count(seeded, first, bad)
    with db.connection() as conn:
        conn.execute('UPDATE campaign_capability_links SET catalog_commit=%s WHERE link_id=%s',
                     ('f' * 40, uuid.UUID(context['link_id'])))
    with pytest.raises(campaigns.CampaignConflict):
        count(seeded, first, receipt)


def test_host_wire_rejects_selectors_and_foreign_subject(seeded, publication):
    ready(seeded, publication)
    for payload in ({'machine_id': 'other'}, {'enrollment_id': seeded[2]}, {'command': 'run'}):
        with pytest.raises(campaigns.CampaignError):
            capabilities.claim_host_operation('worker-service', payload)
    with pytest.raises(campaigns.CampaignError):
        capabilities.claim_host_operation('foreign', {})


def test_database_scope_and_immutable_invocation(seeded, publication):
    context = ready(seeded, publication)
    jid = job(context)
    capabilities.ensure_operation(jid, context)
    with pytest.raises(RaiseException):
        with db.connection() as conn:
            conn.execute('UPDATE campaign_capability_invocations SET context=%s WHERE job_id=%s',
                         (Jsonb({**context, 'tenant_id': 'foreign'}), uuid.UUID(jid)))
    with pytest.raises(ForeignKeyViolation):
        with db.connection() as conn:
            conn.execute('UPDATE campaign_host_operations SET enrollment_id=%s WHERE job_id=%s',
                         (uuid.uuid4(), uuid.UUID(jid)))
    with pytest.raises(ForeignKeyViolation):
        with db.connection() as conn:
            conn.execute('UPDATE async_jobs SET tenant_id=%s WHERE job_id=%s', ('foreign', jid))


@pytest.mark.parametrize('outcome', ['held', 'failed'])
def test_terminal_job_cannot_count_unsuccessful_host_operation(seeded, publication, outcome):
    context = ready(seeded, publication)
    jid = job(context)
    capabilities.ensure_operation(jid, context)
    op = claim()
    settle(op)
    settle(op, 'activate', outcome)
    with db.connection() as conn:
        row = conn.execute("UPDATE async_jobs SET status='complete', progress='done', finished_at=%s "
                           'WHERE job_id=%s RETURNING *', (time.time(), jid)).fetchone()
    receipt = {'schema': 'leaf.build-receipt.v1', 'job_id': jid, 'tenant_id': context['tenant_id'],
               'org_id': context['org_id'], 'project_id': context['project_id'], 'tool': context['tool_name'],
               'status': 'complete', 'attempt': row['attempt'], 'execution_path': None, 'fallback': False,
               'created_at': row['created_at'], 'finished_at': row['finished_at'], 'elapsed_ms': None,
               'error_code': None, 'source_sha': 'a' * 40, 'written_at': time.time(),
               'capability_provenance': context, 'host_readback': body(op, 'readback')['evidence']}
    receipt['digest'] = digest(receipt)
    with pytest.raises(campaigns.CampaignConflict):
        count(seeded, jid, receipt)
    assert enrollment.list_enrollments(*seeded[0])[0]['capability_link']['counted_job_ids'] == []
