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
