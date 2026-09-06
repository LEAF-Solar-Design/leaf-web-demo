"""PostgreSQL authority and restart recovery for campaign host enrollment."""
import uuid

import pytest

from leaf_platform import campaigns, campaign_enrollment as enrollment, campaign_execution as execution, db, store


@pytest.fixture
def seeded(make_org, monkeypatch):
    monkeypatch.setenv('LEAF_CAMPAIGN_ALLOWED_MACHINES', 'VM-C,VM-D')
    monkeypatch.setenv('LEAF_CAMPAIGN_WORKER_SUBJECT', 'worker-service')
    monkeypatch.setenv('LEAF_SOURCE_SHA', 'a' * 40)
    org = make_org()
    project = store.create_project(org.org_id, 'Enrollment project')
    principal = store.create_identity_binding(org.org_id, 'auth0', f'auth0|{uuid.uuid4()}', role='owner')
    with db.connection() as conn:
        conn.execute('INSERT INTO project_member_bindings '
                     '(membership_id, org_id, project_id, binding_id, role, invited_by_binding_id) '
                     "VALUES (%s,%s,%s,%s,'owner',%s)",
                     (uuid.uuid4(), org.org_id, project.project_id, principal.binding_id, principal.binding_id))
    campaign = campaigns.submit_campaign(org.org_id, project.project_id, str(org.org_id),
        principal.binding_id, title='ReciPDF', prompt='Organize recipes', idempotency_key='enrollment')
    return (org.org_id, project.project_id, campaign['campaign_id']), principal.binding_id


def connect(seeded, machine='VM-C'):
    scope, principal = seeded
    return enrollment.request_enrollment(*scope, principal, machine_id=machine)


def test_replay_retains_original_task_source_and_pending_link(seeded, monkeypatch):
    scope, _ = seeded
    first = connect(seeded)
    monkeypatch.setenv('LEAF_SOURCE_SHA', 'b' * 40)
    second = connect(seeded)
    assert second['replayed'] and second['enrollment_id'] == first['enrollment_id']
    assert second['capability_link'] == first['capability_link']
    assert second['capability_link']['state'] == 'pending_link'
    monkeypatch.delenv('LEAF_SOURCE_SHA')
    assert connect(seeded)['enrollment_id'] == first['enrollment_id']
    with pytest.raises(campaigns.CampaignUnavailable, match='source'):
        connect(seeded, 'VM-D')
    snapshot = execution.read_execution(*scope)
    assert len(snapshot['tasks']) == 1
    assert snapshot['tasks'][0]['source_sha'] == 'a' * 40
    assert snapshot['tasks'][0]['kind'] == 'capability'
    assert [event['event_type'] for event in snapshot['events']].count('enrollment_requested') == 1
    assert [event['event_type'] for event in snapshot['events']].count('capability_link_recorded') == 1
    assert 'service_subject' not in first


def test_crash_after_task_commit_converges_without_source(seeded, monkeypatch):
    original = execution.submit_task

    def fail_after_commit(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('process stopped after durable task')

    monkeypatch.setattr(execution, 'submit_task', fail_after_commit)
    with pytest.raises(campaigns.CampaignUnavailable):
        connect(seeded)
    monkeypatch.setattr(execution, 'submit_task', original)
    monkeypatch.delenv('LEAF_SOURCE_SHA')
    row = connect(seeded)
    assert row['state'] == 'pending'
    snapshot = execution.read_execution(*seeded[0])
    assert len(snapshot['tasks']) == 1
    assert len(enrollment.list_enrollments(*seeded[0])) == 1
    assert sorted(event['event_type'] for event in snapshot['events']) == [
        'capability_link_recorded', 'enrollment_requested', 'task_submitted']


def test_enable_revoke_restart_and_worker_subject(seeded):
    scope, principal = seeded
    row = connect(seeded)
    eid = row['enrollment_id']
    with pytest.raises(campaigns.CampaignError):
        enrollment.resolve_worker_enrollment(eid, 'worker-service')
    enabled = enrollment.enable_enrollment(*scope, eid, principal)
    assert enabled['enabled_at'] and enabled['revoked_at'] is None
    assert enrollment.enable_enrollment(*scope, eid, principal)['replayed']
    db.reset_pool()
    assert enrollment.resolve_worker_enrollment(eid, 'worker-service') == []
    with pytest.raises(campaigns.CampaignError):
        enrollment.resolve_worker_enrollment(eid, 'other-service')
    before = execution.read_execution(*scope)
    revoked = enrollment.revoke_enrollment(*scope, eid, principal)
    assert revoked['enabled_at'] == enabled['enabled_at'] and revoked['revoked_at']
    assert enrollment.revoke_enrollment(*scope, eid, principal)['replayed']
    assert connect(seeded)['state'] == 'revoked'
    with pytest.raises(campaigns.CampaignConflict):
        enrollment.enable_enrollment(*scope, eid, principal)
    with pytest.raises(campaigns.CampaignError):
        enrollment.resolve_worker_enrollment(eid, 'worker-service')
    after = execution.read_execution(*scope)
    assert after['receipts'] == before['receipts']
    assert after['tasks'] == before['tasks']


def test_real_postgres_worker_scope_preserves_recovery_list(seeded, monkeypatch):
    scope, principal = seeded
    row = connect(seeded)
    enrollment.enable_enrollment(*scope, row['enrollment_id'], principal)
    with campaigns._cursor() as cur:
        resolved = enrollment.resolve_worker_scope(cur, row['enrollment_id'], 'worker-service')
    assert set(resolved) == {'org', 'project', 'campaign', 'enrollment_id', 'machine_id', 'tenant_id', 'prompt'}
    assert tuple(str(resolved[key]) for key in ('org', 'project', 'campaign')) == tuple(map(str, scope))
    assert resolved['prompt'] == 'Organize recipes' and resolved['tenant_id'] == str(scope[0])
    expected = {'machine_id': 'VM-C', 'attempt_id': 'pending', 'request_body': {'preserved': True}}
    monkeypatch.setattr(execution, 'pending_remote_bindings',
                        lambda *args: [expected, {'machine_id': 'VM-D', 'attempt_id': 'foreign'}])
    recovered = enrollment.resolve_worker_enrollment(row['enrollment_id'], 'worker-service')
    assert recovered == [expected] and isinstance(recovered, list)


def test_scope_and_viewer_authorization_on_postgres(seeded):
    scope, principal = seeded
    row = connect(seeded)
    other = store.create_project(scope[0], 'Other enrollment project')
    wrong = (scope[0], other.project_id, scope[2])
    for operation, args in (
        (enrollment.list_enrollments, wrong),
        (enrollment.enable_enrollment, (*wrong, row['enrollment_id'], principal)),
        (enrollment.revoke_enrollment, (*wrong, row['enrollment_id'], principal)),
    ):
        with pytest.raises(campaigns.CampaignUnavailable):
            operation(*args)
    with db.connection() as conn:
        conn.execute("UPDATE project_member_bindings SET role='read_only' WHERE binding_id=%s", (principal,))
    for operation in (enrollment.enable_enrollment, enrollment.revoke_enrollment):
        with pytest.raises(campaigns.CampaignUnavailable):
            operation(*scope, row['enrollment_id'], principal)
    with pytest.raises(campaigns.CampaignUnavailable):
        connect(seeded)


def test_corrupt_cross_project_link_cannot_authorize_recovery(seeded):
    scope, principal = seeded
    row = connect(seeded)
    enrollment.enable_enrollment(*scope, row['enrollment_id'], principal)
    other = store.create_project(scope[0], 'Other link project')
    with db.connection() as conn:
        conn.execute('UPDATE campaign_capability_links SET project_id=%s WHERE link_id=%s',
                     (other.project_id, uuid.UUID(row['capability_link']['link_id'])))
    with pytest.raises(campaigns.CampaignUnavailable):
        enrollment.list_enrollments(*scope)
    with pytest.raises(campaigns.CampaignUnavailable):
        enrollment.resolve_worker_enrollment(row['enrollment_id'], 'worker-service')


def test_server_configuration_and_no_caller_lifecycle_evidence(seeded, monkeypatch):
    with pytest.raises(campaigns.CampaignError):
        connect(seeded, 'unlisted')
    for source in ('', 'not-a-sha'):
        monkeypatch.setenv('LEAF_SOURCE_SHA', source)
        with pytest.raises(campaigns.CampaignUnavailable):
            connect(seeded)
    monkeypatch.setenv('LEAF_SOURCE_SHA', 'a' * 40)
    scope, principal = seeded
    with pytest.raises(TypeError):
        enrollment.request_enrollment(*scope, principal, machine_id='VM-C', publication_id='fake')
    row = connect(seeded)
    assert row['capability_link']['state'] == 'pending_link'
    assert not hasattr(enrollment, 'record_capability_link')


def test_revoke_preserves_accepted_receipt_and_pending_binding(seeded):
    scope, principal = seeded
    row = connect(seeded)
    enrollment.enable_enrollment(*scope, row['enrollment_id'], principal)
    execution.submit_task(*scope, task_key='ordinary-recovery', idempotency_key='ordinary-recovery',
        title='Ordinary recovery task', spec='Preserve remote recovery on enrollment revocation',
        capability='recipe.organize', stages=['implementation', 'build_test'], owned_paths=['recipes/'],
        source_sha='a' * 40, verify_command='pytest', declared_artifacts=['diff'], depends_on=[])

    def bind(attempt):
        return execution.bind_remote_dispatch(*scope, attempt['attempt_id'],
            fence=attempt['fence'], machine_id='VM-C', run_id='enrollment-run',
            registration_id='enrollment-registration', root_request_id='enrollment-root',
            gateway_project_id='enrollment-project', source_ref='a' * 40,
            packet_digest='b' * 64, budget_class='explicit', reservation_micro_usd=1000000)

    first = execution.claim_task(*scope, worker_id='VM-C', lease_seconds=30)
    binding = bind(first)
    execution.record_remote_admission(*scope, first['attempt_id'], **{
        key: binding[key] for key in ('leaf_id', 'run_id', 'submission_digest')})
    receipt = execution.settle_remote_attempt(*scope, first['attempt_id'], fence=first['fence'],
        verdict={'run_id': binding['run_id'], 'leaf_id': binding['leaf_id'], 'fencing_token': 3},
        outcome='succeeded', result={}, artifact_ref='diff:enrollment')
    second = execution.claim_task(*scope, worker_id='VM-C', lease_seconds=30)
    later = bind(second)
    db.reset_pool()
    recovered = enrollment.resolve_worker_enrollment(row['enrollment_id'], 'worker-service')
    assert [item['attempt_id'] for item in recovered] == [later['attempt_id']]
    with db.connection() as conn:
        before = conn.execute('SELECT * FROM campaign_dispatch_bindings WHERE campaign_id=%s ORDER BY fence',
                              (uuid.UUID(scope[2]),)).fetchall()
    enrollment.revoke_enrollment(*scope, row['enrollment_id'], principal)
    with db.connection() as conn:
        after = conn.execute('SELECT * FROM campaign_dispatch_bindings WHERE campaign_id=%s ORDER BY fence',
                             (uuid.UUID(scope[2]),)).fetchall()
    assert after == before
    assert receipt in execution.read_execution(*scope)['receipts']
    with pytest.raises(campaigns.CampaignError):
        enrollment.resolve_worker_enrollment(row['enrollment_id'], 'worker-service')


def legacy_task(seeded, *, title=None):
    import hashlib

    scope, _ = seeded
    key = 'host-enrollment-' + hashlib.sha256(b'VM-C').hexdigest()
    contract = enrollment._host_contract('VM-C', legacy=True)
    if title is not None:
        contract['title'] = title
    return execution.submit_task(*scope, task_key=key, idempotency_key=key,
        kind='capability', capability='campaign.host-enrollment', source_sha='a' * 40,
        **contract, depends_on=[])


def test_fresh_host_contract_is_system_verification(seeded):
    row = connect(seeded)
    snapshot = execution.read_execution(*seeded[0])
    task = snapshot['tasks'][0]
    assert task['task_id'] == row['capability_link']['task_id']
    assert task['title'] == 'Verify campaign host enrollment'
    assert task['stages'] == ['verification'] and task['current_stage'] == 'verification'
    assert task['owned_paths'] == []
    assert task['verify_command'] == 'campaign_capabilities.count_invocation'
    assert task['declared_artifacts'] == [
        'published-capability-binding', 'two-verified-invocation-receipts']
    assert execution.claim_task(*seeded[0], worker_id='generic', lease_seconds=30) is None
    scope = dict(zip(('org', 'project', 'campaign'), map(uuid.UUID, map(str, seeded[0]))))
    with execution._cursor() as cur:
        assert execution._claim_task_cursor(
            cur, scope, worker_id='generic', lease=30, task_key=task['task_key']) is None


@pytest.mark.parametrize('already_linked', [False, True])
def test_exact_untouched_legacy_repairs_once(seeded, monkeypatch, already_linked):
    before = legacy_task(seeded)
    if already_linked:
        with monkeypatch.context() as prior:
            prior.setattr(enrollment, '_repair_host_task', lambda cur, scope, task, machine: task)
            original_enrollment = connect(seeded)
    monkeypatch.delenv('LEAF_SOURCE_SHA')
    row = connect(seeded)
    if already_linked:
        assert row['enrollment_id'] == original_enrollment['enrollment_id']
        assert row['capability_link']['link_id'] == original_enrollment['capability_link']['link_id']
    after = execution.read_execution(*seeded[0])['tasks'][0]
    assert row['capability_link']['task_id'] == before['task_id']
    for key in ('task_id', 'source_sha', 'idempotency_key', 'created_at', 'task_key', 'parent_task_id'):
        assert after[key] == before[key]
    assert after['stages'] == ['verification'] and after['fence'] == 0
    assert after['payload_fingerprint'] != before['payload_fingerprint']
    assert connect(seeded)['replayed']
    repairs = [event for event in execution.read_execution(*seeded[0])['events']
               if event['payload'].get('old_fingerprint')]
    assert len(repairs) == 1
    assert repairs[0]['event_type'] == 'capability_link_recorded'
    assert repairs[0]['payload']['old_fingerprint'] == before['payload_fingerprint']
    assert repairs[0]['payload']['new_fingerprint'] == after['payload_fingerprint']


@pytest.mark.parametrize('history', ['unexpected', 'attempt', 'receipt', 'binding'])
def test_legacy_history_or_unexpected_contract_is_preserved(seeded, history):
    task = legacy_task(seeded, title='Different task' if history == 'unexpected' else None)
    scope = seeded[0]
    if history != 'unexpected':
        # Reproduce a historical pre-producer attempt, without the new generic claimer.
        with db.connection() as conn:
            attempt = conn.execute(
                'INSERT INTO campaign_task_attempts (attempt_id, task_id, org_id, project_id, '
                'campaign_id, fence, attempt_token_hash, worker_id, stage, deadline_at, status) '
                "VALUES (%s,%s,%s,%s,%s,1,%s,'historical','implementation',"
                "NOW()+interval '1 hour','active') RETURNING *",
                (uuid.uuid4(), uuid.UUID(task['task_id']), *map(uuid.UUID, map(str, scope)),
                 '1' * 64)).fetchone()
        if history == 'receipt':
            params = dict(zip(('org', 'project', 'campaign'), map(uuid.UUID, map(str, scope))))
            with execution._cursor() as cur:
                execution._receipt(cur, params, task, attempt,
                    execution._values('failed', {'reason': 'historical'}, None, None, None, None, False))
        elif history == 'binding':
            with db.connection() as conn:
                conn.execute('UPDATE campaign_tasks SET fence=1 WHERE task_id=%s',
                             (uuid.UUID(task['task_id']),))
            execution.bind_remote_dispatch(*scope, str(attempt['attempt_id']), fence=1,
                machine_id='VM-C', run_id='legacy-run', registration_id='legacy-registration',
                root_request_id='legacy-root', gateway_project_id='legacy-project',
                source_ref='a' * 40, packet_digest='b' * 64,
                budget_class='explicit', reservation_micro_usd=100)
    before = execution.read_execution(*scope)
    with pytest.raises(campaigns.CampaignConflict) as exc:
        connect(seeded)
    assert exc.value.code == 'reconcile_required'
    assert execution.read_execution(*scope) == before
    assert enrollment.list_enrollments(*scope) == []


@pytest.mark.parametrize('remote', [False, True])
def test_generic_success_rejects_historical_host_attempt(seeded, remote):
    import hashlib

    task = legacy_task(seeded)
    token = '1' * 64
    attempt_id = uuid.uuid4()
    with db.connection() as conn:
        conn.execute('UPDATE campaign_tasks SET fence=1, status=\'claimed\' WHERE task_id=%s',
                     (uuid.UUID(task['task_id']),))
        conn.execute(
            'INSERT INTO campaign_task_attempts (attempt_id,task_id,org_id,project_id,campaign_id,'
            'fence,attempt_token_hash,worker_id,stage,deadline_at,status) '
            "VALUES (%s,%s,%s,%s,%s,1,%s,'historical','implementation',"
            "NOW()+interval '1 hour','active')",
            (attempt_id, uuid.UUID(task['task_id']), *map(uuid.UUID, map(str, seeded[0])),
             hashlib.sha256(token.encode()).hexdigest()))
    if remote:
        binding = execution.bind_remote_dispatch(*seeded[0], str(attempt_id), fence=1,
            machine_id='VM-C', run_id='legacy-run', registration_id='legacy-registration',
            root_request_id='legacy-root', gateway_project_id='legacy-project',
            source_ref='a' * 40, packet_digest='b' * 64,
            budget_class='explicit', reservation_micro_usd=100)
        execution.record_remote_admission(*seeded[0], str(attempt_id), **{
            key: binding[key] for key in ('leaf_id', 'run_id', 'submission_digest')})
        with pytest.raises(campaigns.CampaignError, match='internal lifecycle'):
            execution.settle_remote_attempt(*seeded[0], str(attempt_id), fence=1,
                verdict={'run_id': binding['run_id'], 'leaf_id': binding['leaf_id'], 'fencing_token': 1},
                outcome='succeeded', result={}, artifact_ref='diff:fake')
    else:
        with pytest.raises(campaigns.CampaignError, match='internal lifecycle'):
            execution.settle_attempt(*seeded[0], str(attempt_id), attempt_token=token, fence=1,
                                     outcome='succeeded', result={}, artifact_ref='diff:fake')
    assert execution.read_execution(*seeded[0])['receipts'] == []
