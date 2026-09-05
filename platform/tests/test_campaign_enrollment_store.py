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
