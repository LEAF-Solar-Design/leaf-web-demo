"""PostgreSQL proofs for finish authority within the campaign engine."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
import sys
import json
import time
import uuid

import psycopg
import pytest

from leaf_platform import (campaigns, campaign_release as releases, campaign_execution as execution,
                           db, store, project_lifecycle)

# Insert server/ AFTER collection-time sys.path setup so `import deps` etc. resolve
# to server/deps.py rather than any same-named module reachable from platform/.
_SERVER_DIR = Path(__file__).resolve().parent.parent.parent / 'server'
sys.path.insert(0, str(_SERVER_DIR))

import deps  # noqa: E402
import platform_link  # noqa: E402
import campaign_release_service as runtime  # noqa: E402
from routers import campaigns as campaigns_router, campaign_mcp  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _seed(make_org, org=None):
    org = org or make_org()
    project = store.create_project(org.org_id, 'Finish project ' + str(uuid.uuid4()))
    binding = store.create_identity_binding(org.org_id, 'auth0', 'auth0|finish-' + str(uuid.uuid4()), role='owner')
    with db.connection() as conn:
        conn.execute('INSERT INTO project_member_bindings '
                     '(membership_id,org_id,project_id,binding_id,role,invited_by_binding_id) '
                     "VALUES (%s,%s,%s,%s,'owner',%s)",
                     (uuid.uuid4(), org.org_id, project.project_id, binding.binding_id, binding.binding_id))
    campaign = campaigns.submit_campaign(org.org_id, project.project_id, str(org.org_id), binding.binding_id,
                                         title='Finish recipes', prompt='Make recipes useful', idempotency_key='finish')
    return (org.org_id, project.project_id, campaign['campaign_id']), binding.binding_id, org


def _contract():
    return dict(original_goal='Organize all recipes', intended_user='Home cook',
                workflow='Open and read the recipe file', release_boundary='One readable recipe',
                deferred_items=['Recipe search'], artifact_refs=['recipe-file'],
                required_checks=[dict(check_id=stage + '.proof', stage=stage, description='Observe ' + stage)
                                 for stage in releases.STAGES])


def _create(scope, principal, **changes):
    payload = dict(contract=_contract(), delivery_profile='cad_file', idempotency_key='release')
    payload.update(changes)
    return releases.create_release(*scope, principal, **payload)


def _metadata(name, media_type):
    return dict(path=name, name=name, media_type=media_type, format=name.rsplit('.', 1)[-1],
                sha256='a' * 64, size_bytes=10, content_valid=True, bytes_verified=True)


def test_transform_contract_and_normalized_scheduling_intent(make_org):
    scope, principal, _ = _seed(make_org)
    contract = _contract()
    contract.update(transform_recipe=dict(recipe_id='json-records-to-csv', recipe_version=1,
                                         source_artifact=_metadata('records.json', 'application/json')),
                    selected_artifact=_metadata('records.csv', 'text/csv'),
                    deadline_at='2026-09-07T10:00:00-05:00', priority_score=70)
    row = _create(scope, principal, contract=contract)
    assert row['contract']['deadline_at'] == '2026-09-07T15:00:00Z'
    assert row['contract']['transform_recipe']['source_artifact']['format'] == 'json'
    for change in ({'delivery_profile': 'web_tool'},
                   {'contract': {**contract, 'web_recipe': None}},
                   {'contract': {**contract, 'priority_score': True}},
                   {'contract': {**contract, 'priority_score': 101}},
                   {'contract': {**contract, 'deadline_at': '2026-09-07'}},
                   {'contract': {**contract, 'transform_recipe': {**contract['transform_recipe'], 'shell': 'bad'}}},
                   {'contract': {**contract, 'selected_artifact': _metadata('wrong.html', 'text/html')}}):
        _code('invalid_request', _create, scope, principal, **{**dict(contract=contract), **change})


def test_execution_guard_excludes_other_connection_without_locking_release_rows(make_org):
    scope, principal, _ = _seed(make_org)
    row = _create(scope, principal)
    with releases.execution_guard(*scope, row['release_id']) as acquired:
        assert acquired
        with ThreadPoolExecutor(max_workers=1) as pool:
            def contender():
                with releases.execution_guard(*scope, row['release_id']) as entered:
                    return entered
            assert pool.submit(contender).result(timeout=5) is False
        # Inner runtime transactions can take the row and org locks independently.
        progress = releases.set_progress(*scope, row['release_id'], principal, state='active',
                                        next_action=dict(wait_kind='job', reason='The conversion is running.'))
        assert progress['status'] == 'active'
    with releases.execution_guard(*scope, row['release_id']) as acquired:
        assert acquired


def test_pending_progress_preserves_predecessors_and_never_consumes_corrections(make_org):
    scope, principal, _ = _seed(make_org)
    row = _create(scope, principal)
    _record(scope, row, 'implementation')
    for _ in range(4):
        releases.set_progress(*scope, row['release_id'], principal, state='active',
                              next_action=dict(wait_kind='authoring', reason='Preparing the missing tool.', change_set_id='stage-one'))
    snapshot = releases.get_release(*scope, row['release_id'])
    assert len(snapshot['stages']) == 1 and snapshot['stages'][0]['status'] == 'passed'
    releases.set_progress(*scope, row['release_id'], principal, state='waiting',
                          next_action=dict(wait_kind='authority', reason='Sign in to continue.'))
    assert row['release_id'] not in {item['release_id'] for item in releases.runnable_releases(200)}
    _code('release_not_active', releases.set_progress, *scope, row['release_id'], principal,
          state='active', next_action=dict(wait_kind='job', reason='Still running.'))


@pytest.mark.parametrize('later_state', ['paused', 'authority'])
def test_worker_stale_candidate_cannot_override_human_hold(make_org, monkeypatch, later_state):
    import campaign_release_worker as worker

    scope, principal, _ = _seed(make_org)
    row = _create(scope, principal)
    releases.set_progress(*scope, row['release_id'], principal, state='waiting',
                          next_action=dict(wait_kind='publication', reason='Publication pending.'))
    selected = [item for item in releases.runnable_releases(200) if item['release_id'] == row['release_id']]
    assert len(selected) == 1
    _, tenant = _joint_app(scope, principal, monkeypatch)
    advances = []
    monkeypatch.setattr(runtime, 'advance', lambda *args: advances.append(args))

    def actor_after_user_change(candidate):
        assert candidate['status'] == 'waiting'
        if later_state == 'paused':
            releases.transition_release(*scope, row['release_id'], principal, action='pause')
        else:
            releases.transition_release(*scope, row['release_id'], principal, action='resume')
            releases.set_progress(*scope, row['release_id'], principal, state='waiting',
                                  next_action=dict(wait_kind='authority', reason='Account action required.'))
        return tenant

    service = SimpleNamespace(_store=lambda: SimpleNamespace(runnable_releases=lambda limit: selected),
                              actor_for_release=actor_after_user_change, advance=runtime.advance,
                              resume_pending=runtime.resume_pending)
    assert worker.run_once(service)['failed'] == 0
    current = releases.get_release(*scope, row['release_id'])
    assert current['release']['status'] == ('paused' if later_state == 'paused' else 'waiting')
    if later_state == 'authority':
        assert current['next_action']['wait_kind'] == 'authority'
    assert not advances and not current['stages']
    assert releases.transition_release(*scope, row['release_id'], principal, action='resume')['status'] == 'active'


@pytest.mark.parametrize('kind', ['authoring', 'job', 'capacity', 'publication', 'approval'])
def test_automatic_resume_keeps_eligible_pending_work_moving(make_org, monkeypatch, kind):
    scope, principal, _ = _seed(make_org)
    row = _create(scope, principal)
    releases.set_progress(*scope, row['release_id'], principal, state='waiting',
                          next_action=dict(wait_kind=kind, reason='Producer pending.'))
    _, tenant = _joint_app(scope, principal, monkeypatch)
    advances = []
    monkeypatch.setattr(runtime, 'advance', lambda *args: advances.append(args))
    runtime.resume_pending(tenant, scope[1], scope[2], row['release_id'])
    assert advances == [(tenant, scope[1], scope[2], row['release_id'])]
    assert releases.get_release(*scope, row['release_id'])['release']['status'] == 'active'


def _outstanding_job(scope, principal, row):
    from job_pg_store import PostgresJobStore
    job = str(uuid.uuid4())
    context = dict(schema='leaf.campaign-transform.v1', capability='campaign.records-to-csv',
                   recipe_id='json-records-to-csv', recipe_version=1, tenant_id=str(scope[0]),
                   org_id=str(scope[0]), project_id=str(scope[1]), campaign_id=str(scope[2]),
                   release_id=row['release_id'], contract_version=1, binding_id=str(principal),
                   tool_name='campaign-records-to-csv', change_set_id='published-transform',
                   catalog_commit='a' * 40, effective_catalog_digest='b' * 64,
                   tool_manifest_sha256='sha256:' + 'c' * 64, tool_source_sha256='d' * 64, input_sha256='e' * 64)
    writer = PostgresJobStore()
    writer.submit(dict(job_id=job, tenant_id=str(scope[0]), org_id=str(scope[0]), project_id=str(scope[1]),
                       tool=context['tool_name'], params=json.dumps({'source_json': '[{"x":1}]'}), dwg='',
                       created_at=time.time(), execution=json.dumps({'completion_provenance': context}),
                       authority_mode='legacy_sqlite', idempotency_key=job, submission_fingerprint='a' * 64, dwg_version=None))
    return writer, job


def test_outstanding_async_work_holds_workspace_slot_and_counts_toward_three(make_org):
    scope, principal, org = _seed(make_org)
    other, other_principal, _ = _seed(make_org, org)
    row = _create(scope, principal)
    jobs = [_outstanding_job(scope, principal, row) for _ in range(3)]
    _task(scope, 'after-transform')
    assert _claim(scope) is None
    writer, job = jobs[0]
    assert writer.complete(job, 0, 'complete', {'ok': True}, None, {}, 'finished', None, time.time()) == 'applied'
    attempt = _claim(scope)
    assert attempt is not None
    _settle(scope, attempt)
    releases.set_progress(*scope, row['release_id'], principal, state='waiting',
                          next_action=dict(wait_kind='job', reason='The conversion is running.', job_id=jobs[1][1]))
    queued = _create(other, other_principal)
    assert queued['status'] == 'queued'
    for writer, job in jobs[1:]:
        writer.complete(job, 0, 'complete', {'ok': True}, None, {}, 'finished', None, time.time())
    assert releases.transition_release(*other, queued['release_id'], other_principal, action='resume')['status'] == 'active'


def test_runnable_deadlines_priority_and_failed_stage_exclusion(make_org):
    entries = []
    for deadline, priority in ((None, 99), ('2026-09-08T00:00:00Z', 90), ('2026-09-07T00:00:00Z', 1),
                               ('2026-09-08T00:00:00Z', 100)):
        scope, principal, _ = _seed(make_org)
        contract = {**_contract(), 'priority_score': priority}
        if deadline:
            contract['deadline_at'] = deadline
        entries.append((scope, principal, _create(scope, principal, contract=contract)))
    ids = {row['release_id'] for _, _, row in entries}
    found = [row['release_id'] for row in releases.runnable_releases(200) if row['release_id'] in ids]
    assert found == [entries[i][2]['release_id'] for i in (2, 3, 1, 0)]
    scope, principal, row = entries[2]
    _record(scope, row, 'implementation', status='unavailable', evidence={'contract_version': 1, 'checks': []})
    assert row['release_id'] not in {r['release_id'] for r in releases.runnable_releases(200)}
    releases.retry_stage(*scope, row['release_id'], principal, stage='implementation')
    assert row['release_id'] in {r['release_id'] for r in releases.runnable_releases(200)}
    releases.transition_release(*scope, row['release_id'], principal, action='pause')
    assert row['release_id'] not in {r['release_id'] for r in releases.runnable_releases(200)}


def _evidence(stage, version=1, source='a' * 40):
    evidence = dict(contract_version=version, source_revision=source,
                    checks=[dict(check_id=stage + '.proof', status='passed', evidence={'observed': 'actual result'})])
    if stage == 'deployment':
        evidence.update(observed_revision=source, resource_identity='deployment:recipe', rollback_identity='deployment:prior')
    if stage == 'user_verification':
        evidence.update(workflow=_contract()['workflow'], observations=[{'opened': 'recipe-file', 'readable': True}])
    if stage == 'delivery':
        evidence.update(replay_recipe='Open the delivered file and read the recipe', artifacts=[
            dict(artifact_ref='recipe-file', name='recipe.dxf', sha256='b' * 64, byte_count=100,
                 retrieved=True, valid=True, access_path='/artifacts/recipe-file')])
    return evidence


def _record(scope, row, stage, **changes):
    payload = dict(stage=stage, status='passed', evidence=_evidence(stage, row['contract_version']),
                   producer=releases.PRODUCERS[stage], operation_key=stage + '-first')
    payload.update(changes)
    return releases.record_stage(*scope, row['release_id'], **payload)


def _through(scope, row, stages):
    return [_record(scope, row, stage) for stage in stages]


def _code(code, function, *args, **kwargs):
    with pytest.raises(campaigns.CampaignError) as error:
        function(*args, **kwargs)
    assert error.value.code == code


def _task(scope, key, stages=None):
    return execution.submit_task(*scope, task_key=key, title='Prepare recipe', spec='Prepare recipe',
                                 capability='codex.edit', stages=stages or ['implementation'],
                                 owned_paths=['recipe.txt'], source_sha='a' * 40,
                                 verify_command='python check.py', declared_artifacts=['recipe-file'],
                                 depends_on=[], idempotency_key=key)


def _claim(scope):
    return execution.claim_task(*scope, worker_id='same-worker', lease_seconds=30)


def _settle(scope, attempt):
    return execution.settle_attempt(*scope, attempt['attempt_id'], attempt_token=attempt['attempt_token'],
                                    fence=attempt['fence'], outcome='succeeded', result={},
                                    artifact_ref='diff:recipe-change')


def test_release_idempotency_scope_and_revocation(make_org):
    scope, principal, _ = _seed(make_org)
    row = _create(scope, principal)
    assert _create(scope, principal)['replayed']
    changed = _contract()
    changed['release_boundary'] = 'Two recipes'
    _code('idempotency_conflict', _create, scope, principal, contract=changed)
    foreign, foreign_principal, _ = _seed(make_org)
    other_project = store.create_project(scope[0], 'Other')
    for denied in ((foreign[0], scope[1], scope[2]), (scope[0], other_project.project_id, scope[2]),
                   (scope[0], scope[1], foreign[2])):
        _code('project_unavailable', releases.get_release, *denied, row['release_id'])
        _code('project_unavailable', _create, denied, principal)
        _code('project_unavailable', _record, denied, row, 'implementation')
    _code('project_unavailable', releases.get_release, *foreign, row['release_id'])
    _code('project_unavailable', _create, scope, foreign_principal)
    with db.connection() as conn:
        conn.execute('DELETE FROM project_member_bindings WHERE org_id=%s AND project_id=%s AND binding_id=%s',
                     (scope[0], scope[1], principal))
    _code('project_unavailable', _create, scope, principal)
    _code('project_unavailable', releases.transition_release, *scope, row['release_id'], principal, action='pause')
    _code('project_unavailable', releases.revise_contract, *scope, row['release_id'], principal,
          contract=changed, reason='Narrow scope', idempotency_key='revise')


def test_one_active_queue_pause_wait_and_resume(make_org):
    scope, principal, org = _seed(make_org)
    other, other_principal, _ = _seed(make_org, org)
    first = _create(scope, principal)
    second = _create(other, other_principal)
    assert first['status'] == 'active' and second['status'] == 'queued'
    _task(scope, 'running')
    _task(other, 'queued')
    assert _claim(other) is None
    attempt = _claim(scope)
    releases.transition_release(*scope, first['release_id'], principal, action='pause')
    assert _claim(scope) is None
    assert releases.transition_release(*other, second['release_id'], other_principal, action='resume')['status'] == 'queued'
    _settle(scope, attempt)
    assert releases.transition_release(*other, second['release_id'], other_principal, action='resume')['status'] == 'active'
    assert releases.transition_release(*scope, first['release_id'], principal, action='resume')['status'] == 'queued'
    releases.transition_release(*other, second['release_id'], other_principal, action='wait')
    assert _claim(other) is None
    assert releases.transition_release(*scope, first['release_id'], principal, action='resume')['status'] == 'active'
    releases.transition_release(*scope, first['release_id'], principal, action='cancel')
    assert _claim(scope) is None
    _code('release_terminal', releases.transition_release, *scope, first['release_id'], principal, action='resume')


def test_concurrent_creation_and_same_worker_attempt_cap(make_org):
    scope, principal, _ = _seed(make_org)
    barrier = Barrier(2)
    def create(key):
        barrier.wait(timeout=10)
        return _create(scope, principal, idempotency_key=key)
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(create, ['one', 'two']))
    assert sorted(row['status'] for row in rows) == ['active', 'queued']
    # Use a separate campaign so latest-release selection cannot obscure the
    # cap proof with the second, queued release above.
    scope, principal, _ = _seed(make_org)
    _create(scope, principal)
    for index in range(4):
        _task(scope, 'task-' + str(index))
    barrier = Barrier(4)
    def claim(_):
        barrier.wait(timeout=10)
        return _claim(scope)
    with ThreadPoolExecutor(max_workers=4) as pool:
        attempts = list(pool.map(claim, range(4)))
    assert sum(attempt is not None for attempt in attempts) == 3
    assert {attempt['worker_id'] for attempt in attempts if attempt} == {'same-worker'}
    _settle(scope, next(attempt for attempt in attempts if attempt))
    assert _claim(scope) is not None


@pytest.mark.parametrize('change', [
    {'original_goal': ''}, {'intended_user': ' '}, {'workflow': ''}, {'release_boundary': ''},
    {'required_checks': []}, {'required_checks': _contract()['required_checks'][:-1]},
    {'required_checks': _contract()['required_checks'] * 2},
    {'commands': ['run something']}, {'workflow': 'x' * 65000},
])
def test_contract_defense(make_org, change):
    scope, principal, _ = _seed(make_org)
    _code('invalid_request', _create, scope, principal, contract={**_contract(), **change})
    assert releases.release_snapshot(*scope)['release'] is None


def test_unresolved_cad_requires_actual_delivery(make_org):
    scope, principal, _ = _seed(make_org)
    contract = _contract()
    contract.update(artifact_refs=[], release_boundary='Create and deliver one readable recipe file; output unresolved')
    row = _create(scope, principal, contract=contract)
    snapshot = releases.get_release(*scope, row['release_id'])
    assert snapshot['release']['status'] == 'active'
    assert snapshot['release']['contract']['artifact_refs'] == []
    assert snapshot['release']['scope_summary'] == contract['release_boundary']
    assert len(snapshot['remaining']) == 5
    assert snapshot['deliverables'] == []
    _code('insufficient_evidence', releases.finish_release, *scope, row['release_id'])
    _through(scope, row, releases.STAGES[:-1])
    for artifacts in ([], [{**_evidence('delivery')['artifacts'][0], 'retrieved': False}],
                      [{**_evidence('delivery')['artifacts'][0], 'valid': False}],
                      [{**_evidence('delivery')['artifacts'][0], 'byte_count': 0}]):
        evidence = _evidence('delivery')
        evidence['artifacts'] = artifacts
        _code('insufficient_evidence', _record, scope, row, 'delivery', evidence=evidence)
        _code('insufficient_evidence', releases.finish_release, *scope, row['release_id'])
    snapshot = releases.get_release(*scope, row['release_id'])
    assert snapshot['release']['status'] == 'active'
    assert len(snapshot['remaining']) == 1
    assert snapshot['deliverables'] == []
    receipt = _record(scope, row, 'delivery')
    assert releases.finish_release(*scope, row['release_id'])['status'] == 'finished'
    snapshot = releases.get_release(*scope, row['release_id'])
    assert snapshot['release']['contract']['artifact_refs'] == []
    assert snapshot['deliverables'] == receipt['evidence']['artifacts']
    assert snapshot['remaining'] == []


@pytest.mark.parametrize('committed_stages', [releases.STAGES[:1], releases.STAGES])
def test_revoked_release_principal_cannot_advance_or_finish(make_org, committed_stages):
    scope, principal, _ = _seed(make_org)
    row = _create(scope, principal)
    _through(scope, row, committed_stages)
    before = releases.get_release(*scope, row['release_id'])
    with db.connection() as conn:
        conn.execute('DELETE FROM project_member_bindings WHERE org_id=%s AND project_id=%s AND binding_id=%s',
                     (scope[0], scope[1], principal))
    _code('project_unavailable', _record, scope, row, 'publication', operation_key='after-revocation')
    _code('project_unavailable', releases.finish_release, *scope, row['release_id'])
    assert releases.get_release(*scope, row['release_id']) == before


def test_verified_completion_and_immutable_predecessors(make_org):
    scope, principal, _ = _seed(make_org)
    row = _create(scope, principal)
    _code('insufficient_evidence', releases.finish_release, *scope, row['release_id'])
    receipts = _through(scope, row, releases.STAGES)
    for stage, receipt in zip(releases.STAGES, receipts):
        assert _record(scope, row, stage)['stage_id'] == receipt['stage_id']
        _code('stage_already_passed', _record, scope, row, stage, operation_key='new-operation')
    result = releases.finish_release(*scope, row['release_id'])
    assert result['status'] == 'finished'
    assert releases.finish_release(*scope, row['release_id'])['replayed']
    snapshot = releases.get_release(*scope, row['release_id'])
    assert snapshot['remaining'] == []
    assert all(check['status'] == 'passed' for check in snapshot['coverage'])
    assert snapshot['deliverables'][0]['byte_count'] == 100
    assert campaigns.get_campaign(*scope)['status'] == 'accepted'
    assert _record(scope, row, 'implementation')['replayed']
    with db.connection() as conn:
        before = conn.execute('SELECT count(*) AS n FROM campaign_release_stages WHERE release_id=%s',
                              (uuid.UUID(row['release_id']),)).fetchone()['n']
    assert before == 5
    for table in ('campaign_release_stages', 'campaign_release_contracts'):
        for statement in ('DELETE FROM ' + table + ' WHERE release_id=%s',
                          'UPDATE ' + table + ' SET contract_version=contract_version WHERE release_id=%s'):
            with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
                with db.connection() as conn:
                    conn.execute(statement, (uuid.UUID(row['release_id']),))


def test_wrong_producer_contract_checks_and_source_cannot_finish(make_org):
    scope, principal, _ = _seed(make_org)
    row = _create(scope, principal)
    _code('invalid_producer', _record, scope, row, 'delivery', producer='task_ledger')
    _code('predecessor_required', _record, scope, row, 'publication')
    for version in (True, 2, '1'):
        _code('contract_version_mismatch', _record, scope, row, 'implementation', evidence=_evidence('implementation', version))
    evidence = _evidence('implementation')
    evidence['checks'] = []
    _code('insufficient_evidence', _record, scope, row, 'implementation', evidence=evidence)
    evidence['source_revision'] = ''
    _code('invalid_request', _record, scope, row, 'implementation', evidence=evidence)
    _record(scope, row, 'implementation')
    _code('source_revision_mismatch', _record, scope, row, 'publication', evidence=_evidence('publication', source='b' * 40))
    _record(scope, row, 'publication')
    evidence = _evidence('deployment')
    evidence['observed_revision'] = 'b' * 40
    _code('source_revision_mismatch', _record, scope, row, 'deployment', evidence=evidence)
    _record(scope, row, 'deployment')
    evidence = _evidence('user_verification')
    evidence['workflow'] = 'Different workflow'
    _code('workflow_mismatch', _record, scope, row, 'user_verification', evidence=evidence)
    evidence = _evidence('user_verification')
    evidence.pop('observations')
    evidence['passed'] = True
    _code('insufficient_evidence', _record, scope, row, 'user_verification', evidence=evidence)
    _code('insufficient_evidence', releases.finish_release, *scope, row['release_id'])
    snapshot = releases.get_release(*scope, row['release_id'])
    assert len(snapshot['stages']) == 3
    assert len(snapshot['remaining']) == 2
    assert snapshot['deliverables'] == []


@pytest.mark.parametrize('field,value', [('byte_count', 0), ('byte_count', True), ('byte_count', 1.0),
                                       ('retrieved', False), ('retrieved', 1), ('valid', False),
                                       ('valid', 1), ('sha256', 'not-a-digest')])
def test_file_readback_is_required(make_org, field, value):
    scope, principal, _ = _seed(make_org)
    row = _create(scope, principal)
    _through(scope, row, releases.STAGES[:-1])
    evidence = _evidence('delivery')
    evidence['artifacts'][0][field] = value
    _code('insufficient_evidence', _record, scope, row, 'delivery', evidence=evidence)
    _code('insufficient_evidence', releases.finish_release, *scope, row['release_id'])
    assert releases.get_release(*scope, row['release_id'])['deliverables'] == []


def test_failed_corrections_persist_stop_without_rolling_back_progress(make_org):
    scope, principal, _ = _seed(make_org)
    row = _create(scope, principal)
    first = _record(scope, row, 'implementation')
    evidence = dict(contract_version=1, source_revision='a' * 40, checks=[])
    for number in range(3):
        receipt = _record(scope, row, 'publication', status='unavailable', evidence=evidence,
                          operation_key='attempt-' + str(number))
        assert _record(scope, row, 'publication', status='unavailable', evidence=evidence,
                       operation_key='attempt-' + str(number))['stage_id'] == receipt['stage_id']
        _code('stage_conflict', _record, scope, row, 'publication', status='failed', evidence=evidence,
              operation_key='attempt-' + str(number))
        if number < 2:
            assert releases.retry_stage(*scope, row['release_id'], principal, stage='publication')['status'] == 'active'
    db.reset_pool()
    snapshot = releases.get_release(*scope, row['release_id'])
    assert snapshot['release']['status'] == 'needs_approach'
    assert snapshot['next_action']['action'] == 'revise_contract'
    assert snapshot['stages'][0]['stage_id'] == first['stage_id']
    assert len(snapshot['stages']) == 4
    _code('needs_approach', releases.retry_stage, *scope, row['release_id'], principal, stage='publication')
    _code('needs_approach', releases.transition_release, *scope, row['release_id'], principal, action='resume')
    changed = _contract()
    changed['release_boundary'] = 'Deliver a simpler recipe'
    revised = releases.revise_contract(*scope, row['release_id'], principal, contract=changed,
                                       reason='Use simpler file content', idempotency_key='approach')
    assert revised['contract_version'] == 2 and revised['status'] == 'active'
    assert len(releases.get_release(*scope, row['release_id'])['remaining']) == 5
    _code('insufficient_evidence', releases.finish_release, *scope, row['release_id'])
    _through(scope, revised, releases.STAGES)
    assert releases.finish_release(*scope, row['release_id'])['status'] == 'finished'


def test_revision_decision_replay_and_original_ambition(make_org):
    scope, principal, _ = _seed(make_org)
    row = _create(scope, principal)
    _record(scope, row, 'implementation')
    changed = _contract()
    changed['release_boundary'] = 'One simpler recipe'
    args = dict(contract=changed, reason='Limit the release', idempotency_key='revision')
    revised = releases.revise_contract(*scope, row['release_id'], principal, **args)
    assert releases.revise_contract(*scope, row['release_id'], principal, **args)['replayed']
    assert revised['contract']['original_goal'] == row['contract']['original_goal']
    bad = deepcopy(changed)
    bad['original_goal'] = 'Erase original goal'
    _code('original_goal_immutable', releases.revise_contract, *scope, row['release_id'], principal,
          contract=bad, reason='Change', idempotency_key='different')
    decision = dict(decision_key='dependency', kind='external_dependency', payload={'needed': 'File reader'}, decided_by='adapter')
    accepted = releases.record_decision(*scope, row['release_id'], **decision)
    assert releases.record_decision(*scope, row['release_id'], **decision)['decision_id'] == accepted['decision_id']
    _code('decision_conflict', releases.record_decision, *scope, row['release_id'],
          **{**decision, 'payload': {'needed': 'Other reader'}})
    snapshot = releases.get_release(*scope, row['release_id'])
    assert len(snapshot['decisions']) == 2 and len(snapshot['stages']) == 1
    assert all(check['status'] == 'unavailable' for check in snapshot['coverage'])
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
        with db.connection() as conn:
            conn.execute('DELETE FROM campaign_release_decisions WHERE decision_id=%s', (uuid.UUID(accepted['decision_id']),))


def test_scoped_foreign_keys_reject_cross_campaign_release_rows(make_org):
    scope, principal, _ = _seed(make_org)
    foreign, _, _ = _seed(make_org)
    row = _create(scope, principal)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with db.connection() as conn:
            conn.execute('INSERT INTO campaign_release_decisions '
                         '(decision_id,org_id,project_id,campaign_id,release_id,decision_key,kind,payload,decided_by,payload_fingerprint) '
                         "VALUES (%s,%s,%s,%s,%s,'foreign','scope','{}','adapter','fingerprint')",
                         (uuid.uuid4(), foreign[0], foreign[1], uuid.UUID(foreign[2]), uuid.UUID(row['release_id'])))


def test_legacy_claims_and_uncertain_recovery(make_org):
    scope, principal, _ = _seed(make_org)
    assert releases.release_snapshot(*scope)['release'] is None
    _task(scope, 'legacy')
    _settle(scope, _claim(scope))
    row = _create(scope, principal)
    _task(scope, 'outward', ['publication'])
    attempt = _claim(scope)
    with db.connection() as conn:
        conn.execute("UPDATE campaign_task_attempts SET deadline_at=NOW()-interval '1 second' WHERE attempt_id=%s",
                     (uuid.UUID(attempt['attempt_id']),))
    assert _claim(scope) is None
    _task(scope, 'later')
    assert _claim(scope) is None
    releases.transition_release(*scope, row['release_id'], principal, action='cancel')
    task = next(task for task in execution.read_execution(*scope)['tasks'] if task['task_key'] == 'outward')
    assert task['status'] == 'reconcile_required'
    recovered = execution.reconcile_outward(*scope, task['task_id'],
                                            outward_operation_key=attempt['outward_operation_key'],
                                            outcome='failed', result={'observed': 'publication absent'})
    assert recovered['outcome'] == 'failed'
    assert _claim(scope) is None


def test_missing_artifact_and_secret_material_are_not_deliverables(make_org):
    scope, principal, _ = _seed(make_org)
    row = _create(scope, principal)
    _through(scope, row, releases.STAGES[:-1])
    evidence = _evidence('delivery')
    evidence['artifacts'] = []
    _code('insufficient_evidence', _record, scope, row, 'delivery', evidence=evidence)
    evidence = _evidence('delivery')
    evidence['artifacts'][0]['artifact_ref'] = 'unpromised-file'
    _code('insufficient_evidence', _record, scope, row, 'delivery', evidence=evidence)
    evidence = _evidence('delivery')
    evidence['replay_recipe'] = ''
    _code('invalid_request', _record, scope, row, 'delivery', evidence=evidence)
    with pytest.raises(campaigns.CampaignError):
        releases.record_decision(*scope, row['release_id'], decision_key='unsafe', kind='scope',
                                 payload={'credentials': {'password': 'private-value'}}, decided_by='adapter')
    assert releases.get_release(*scope, row['release_id'])['decisions'] == []
    assert releases.get_release(*scope, row['release_id'])['deliverables'] == []


DXF_DRAWING = b'0\nSECTION\n2\nENTITIES\n0\nCIRCLE\n10\n0\n20\n0\n40\n10\n0\nENDSEC\n0\nEOF\n'


def _joint_app(scope, principal, monkeypatch):
    """Wire a real FastAPI app over the real store/lifecycle with a synthetic tenant boundary."""
    org_id, project_id, campaign_id = scope
    tenant = SimpleNamespace(tenant_id=str(org_id))

    def access(caller, pid, **kwargs):
        if caller is not tenant or str(pid) != str(project_id):
            raise platform_link.ProjectSessionForbidden('wrong workspace or actor')
        return str(org_id)

    monkeypatch.setattr(platform_link, 'require_project_access', access)
    monkeypatch.setattr(platform_link, 'resolve_caller_binding',
                        lambda caller: SimpleNamespace(binding_id=principal) if caller is tenant else None)
    monkeypatch.setattr(campaigns_router, '_STORE', campaigns)
    monkeypatch.setattr(runtime, '_STORE', releases)
    monkeypatch.setattr(runtime, '_LIFECYCLE', project_lifecycle)
    monkeypatch.setattr(runtime.capabilities, 'resolve', lambda *args, **kwargs: {
        'selected': 'project-file-delivery', 'candidates': [], 'missing_capability': None,
        'recommended_action': 'Use existing project file delivery'})
    app = FastAPI()
    app.include_router(campaigns_router.router)
    app.include_router(campaign_mcp.router)
    app.dependency_overrides[deps.require_tenant] = lambda: tenant
    return TestClient(app), tenant


def test_finish_request_recovers_interrupted_write_and_downloads_dxf_bytes(make_org, monkeypatch):
    scope, principal, org = _seed(make_org)
    org_id, project_id, campaign_id = scope
    project_lifecycle.put_project_file(org_id, project_id, principal, path='drawing.dxf',
        media_type='image/vnd.dxf', content=DXF_DRAWING.decode(), idempotency_key='seed-dxf')
    writes = {'n': 0}
    real_put = project_lifecycle.put_project_file

    def flaky_put(*args, **kwargs):
        result = real_put(*args, **kwargs)
        writes['n'] += 1
        if writes['n'] == 1:
            raise RuntimeError('lost response after commit')
        return result

    monkeypatch.setattr(project_lifecycle, 'put_project_file', flaky_put)
    client, tenant = _joint_app(scope, principal, monkeypatch)
    body = {'project_id': str(project_id), 'finish': {
        'delivery_profile': 'cad_file', 'intended_user': 'Project owner',
        'workflow': 'Open the project and retrieve the valid DXF file',
        'artifact_refs': ['drawing.dxf']}}
    with client:
        first = client.post(f'/api/campaigns/{campaign_id}/releases',
                            headers={'Idempotency-Key': 'finish-dxf'}, json=body)
        assert first.status_code == 503, first.text
        assert writes['n'] == 1
        second = client.post(f'/api/campaigns/{campaign_id}/releases',
                             headers={'Idempotency-Key': 'finish-dxf'}, json=body)
        assert second.status_code in (200, 201), second.text
        assert writes['n'] == 1
        completion = second.json()['completion']
        assert completion['release']['status'] == 'finished'
        assert {s['stage'] for s in completion['stages'] if s['status'] == 'passed'} == set(releases.STAGES)
        artifact = completion['deliverables'][0]
        downloaded = client.get(artifact['access_path'])
        assert downloaded.status_code == 200, downloaded.text
        assert downloaded.content == DXF_DRAWING
        assert artifact['valid'] is True and artifact['retrieved'] is True
        assert campaigns.get_campaign(*scope)['status'] != 'succeeded'
        before = project_lifecycle.project_snapshot(org_id, project_id, principal)
        replay = client.post(f'/api/campaigns/{campaign_id}/releases',
                             headers={'Idempotency-Key': 'finish-dxf'}, json=body)
        assert replay.status_code in (200, 201), replay.text
        after = project_lifecycle.project_snapshot(org_id, project_id, principal)
        assert before['files'] == after['files']
        assert writes['n'] == 1


def test_wrong_workspace_and_actor_denied_and_saved_drift_reports_unavailable(make_org, monkeypatch):
    scope, principal, org = _seed(make_org)
    org_id, project_id, campaign_id = scope
    project_lifecycle.put_project_file(org_id, project_id, principal, path='result.json',
        media_type='application/json', content='{"items":["ok"]}\n', idempotency_key='seed-json')
    client, tenant = _joint_app(scope, principal, monkeypatch)
    body = {'project_id': str(project_id), 'finish': {
        'delivery_profile': 'cad_file', 'intended_user': 'Project owner',
        'workflow': 'Open the project and retrieve the valid JSON file',
        'artifact_refs': ['result.json']}}
    with client:
        foreign_project = store.create_project(org_id, 'Other workspace')
        denied_workspace = client.post(f'/api/campaigns/{campaign_id}/releases',
            headers={'Idempotency-Key': 'wrong-workspace'},
            json={**body, 'project_id': str(foreign_project.project_id)})
        assert denied_workspace.status_code == 403, denied_workspace.text
        stranger_app = FastAPI()
        stranger_app.include_router(campaigns_router.router)
        stranger_app.dependency_overrides[deps.require_tenant] = lambda: SimpleNamespace(tenant_id=str(org_id))
        with TestClient(stranger_app) as stranger_client:
            denied_actor = stranger_client.post(f'/api/campaigns/{campaign_id}/releases',
                headers={'Idempotency-Key': 'wrong-actor'}, json=body)
            assert denied_actor.status_code == 403, denied_actor.text
        finished = client.post(f'/api/campaigns/{campaign_id}/releases',
                               headers={'Idempotency-Key': 'finish-json'}, json=body)
        assert finished.status_code in (200, 201), finished.text
        completion = finished.json()['completion']
        release_id = completion['release']['release_id']
        artifact = completion['deliverables'][0]
        downloaded = client.get(artifact['access_path'])
        assert downloaded.status_code == 200 and downloaded.content == b'{"items":["ok"]}\n'
        project_lifecycle.put_project_file(org_id, project_id, principal, path='result.json',
            media_type='application/json', content='{"items":["drifted"]}\n', idempotency_key='drift-write')
        preserved = client.get(artifact['access_path'])
        assert preserved.status_code == 200 and preserved.content == downloaded.content
        project_lifecycle.put_project_file(org_id, project_id, principal,
            path=runtime._artifact(completion['release'])['path'], media_type='application/json',
            content='{"items":["changed release"]}\n', idempotency_key='drift-release-write')
        drifted = client.get(f'/api/campaigns/{campaign_id}/releases/{release_id}',
                             params={'project_id': str(project_id)})
        assert drifted.status_code == 200, drifted.text
        drifted_completion = drifted.json()['completion']
        assert drifted_completion['current_verification']['status'] == 'failed'
        assert drifted_completion['deliverables'] == []
        redownload = client.get(artifact['access_path'])
        assert redownload.status_code == 409, redownload.text
