"""Mounted planning bridge: real ledger authority, restart recovery and replay."""
import base64
import hashlib
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from leaf_platform import campaigns, campaign_enrollment as enrollment
from leaf_platform import campaign_execution as execution, db, store


SOURCE = 'b' * 40
TREE = 'c' * 40
BUNDLE = b'Git bundle producer fixture'


@pytest.fixture
def mounted(make_org, monkeypatch):
    for key, value in {
        'LEAF_CAMPAIGN_BRIDGE': 'on', 'LEAF_CAMPAIGN_FIRST_TASK_PRODUCER': 'on',
        'LEAF_CAMPAIGN_ALLOWED_MACHINES': 'VM-C,VM-D',
        'LEAF_CAMPAIGN_WORKER_SUBJECT': 'worker-service', 'LEAF_SOURCE_SHA': 'a' * 40,
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'server'))
    bridge = importlib.import_module('campaign_bridge')
    org = make_org()
    principal = store.create_identity_binding(org.org_id, 'auth0',
        'auth0|' + str(uuid.uuid4()), role='owner')

    def enroll(project=None):
        project = project or store.create_project(org.org_id, f'Mounted planning {uuid.uuid4()}')
        with db.connection() as conn:
            conn.execute('INSERT INTO project_member_bindings '
                '(membership_id, org_id, project_id, binding_id, role, invited_by_binding_id) '
                "VALUES (%s,%s,%s,%s,'owner',%s) ON CONFLICT DO NOTHING",
                (uuid.uuid4(), org.org_id, project.project_id, principal.binding_id, principal.binding_id))
        campaign = campaigns.submit_campaign(org.org_id, project.project_id, str(org.org_id),
            principal.binding_id, title='ReciPDF', prompt='Organize crêpe recipes\nKeep the full prompt.',
            idempotency_key=str(uuid.uuid4()))
        scope = (org.org_id, project.project_id, campaign['campaign_id'])
        row = enrollment.request_enrollment(*scope, principal.binding_id, machine_id='VM-C')
        enrollment.enable_enrollment(*scope, row['enrollment_id'], principal.binding_id)
        return scope, row['enrollment_id']

    scope, eid = enroll()
    calls = []

    def initialize(tenant, org_id, project_id, prompt):
        calls.append(prompt)
        store.ensure_project_repository_authority(tenant, org_id, project_id)
        return dict(source_commit=SOURCE, source_tree=TREE,
                    seed_digest=hashlib.sha256(prompt.encode('utf-8')).hexdigest(), replayed=True)

    def export(tenant, org_id, project_id, commit, tree):
        assert store.resolve_project_repository_authority(tenant, org_id, project_id)
        assert (commit, tree) == (SOURCE, TREE)
        return dict(bundle=BUNDLE, source_commit=commit, source_tree=tree,
                    bundle_sha256=hashlib.sha256(BUNDLE).hexdigest(), size_bytes=len(BUNDLE))

    monkeypatch.setattr(bridge.source_service, 'initialize_project_source', initialize)
    monkeypatch.setattr(bridge.source_service, 'export_project_source_bundle', export)
    return SimpleNamespace(bridge=bridge, scope=scope, eid=eid, principal=principal.binding_id,
                           enroll=enroll, calls=calls)


def call(mounted, op, **body):
    return mounted.bridge.handle(op, dict(enrollment_id=mounted.eid, **body), 'worker-service')


def bind_body(attempt):
    return dict(attempt_id=attempt['attempt_id'], fence=attempt['fence'],
        run_id='run-' + str(uuid.uuid4()), registration_id='reg-' + str(uuid.uuid4()),
        root_request_id='root-' + str(uuid.uuid4()), gateway_project_id='project-' + str(uuid.uuid4()),
        source_ref=SOURCE, packet_digest='d' * 64, budget_class='explicit', reservation_micro_usd=1000000)


def admit_body(binding):
    return {key: binding[key] for key in ('attempt_id', 'leaf_id', 'run_id', 'submission_digest')}


def settle_body(binding):
    return dict(attempt_id=binding['attempt_id'], fence=binding['fence'],
        verdict=dict(run_id=binding['run_id'], leaf_id=binding['leaf_id'], fencing_token=1),
        outcome='succeeded', result={'planning_only': True}, artifact_ref='artifact:campaign-plan')


def expire(attempt):
    with db.connection() as conn:
        conn.execute("UPDATE campaign_task_attempts SET deadline_at=clock_timestamp()-interval '1 second' "
                     'WHERE attempt_id=%s', (uuid.UUID(attempt['attempt_id']),))


@pytest.mark.parametrize('late', [False, True])
def test_mounted_export_restart_replay_and_plan_validation_boundary(mounted, monkeypatch, late):
    first = call(mounted, 'next')
    attempt = first['attempt']
    assert first['kind'] == 'claimed' and 'attempt_token' not in attempt
    exported = call(mounted, 'export', attempt_id=attempt['attempt_id'], fence=attempt['fence'])
    assert base64.b64decode(exported['source']['bundle_b64']) == BUNDLE
    assert exported['source']['source_commit'] == exported['task']['source_sha'] == SOURCE
    authority = store.resolve_project_repository_authority(
        str(mounted.scope[0]), str(mounted.scope[0]), str(mounted.scope[1]))
    assert exported['source']['repository_key'] == authority['repo_key']
    assert exported['task']['spec'].endswith(mounted.calls[0])
    assert exported['source']['seed_digest'] == hashlib.sha256(mounted.calls[0].encode()).hexdigest()
    json.dumps(exported, allow_nan=False)
    request = bind_body(attempt)
    binding = call(mounted, 'bind', **request)['binding']
    db.reset_pool()
    recovered = call(mounted, 'recover')['pending_remote_bindings']
    assert recovered == [binding]
    assert call(mounted, 'next')['pending_remote_bindings'] == recovered
    if late:
        expire(attempt)
    assert call(mounted, 'bind', **request)['binding']['submission'] == binding['submission']
    admitted = call(mounted, 'admit', **admit_body(binding))['binding']
    assert admitted['state'] == 'admitted' and admitted['reservation_id'] is None
    assert call(mounted, 'admit', **admit_body(binding))['binding']['replayed']
    receipt = call(mounted, 'settle', **settle_body(binding))['receipt']
    assert call(mounted, 'settle', **settle_body(binding))['receipt']['receipt_id'] == receipt['receipt_id']
    assert call(mounted, 'bind', **request)['binding']['replayed']
    assert call(mounted, 'admit', **admit_body(binding))['binding']['replayed']
    snapshot = execution.read_execution(*mounted.scope)
    task = next(row for row in snapshot['tasks'] if row['task_key'] == 'campaign-plan')
    assert task['status'] == 'pending' and task['current_stage'] == 'build_test'
    assert len([row for row in snapshot['receipts'] if row['outcome'] == 'succeeded']) == 1
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) AS count FROM campaign_dispatch_bindings WHERE attempt_id=%s',
                            (uuid.UUID(attempt['attempt_id']),)).fetchone()['count'] == 1

    def unexpected(*args, **kwargs):
        pytest.fail('planning validation must not initialize source or claim another attempt')

    monkeypatch.setattr(mounted.bridge.source_service, 'initialize_project_source', unexpected)
    monkeypatch.setattr(execution, '_claim_task_cursor', unexpected)
    waiting = call(mounted, 'next')
    assert waiting['kind'] == 'awaiting_plan_validation' and waiting['task_id'] == task['task_id']
    assert waiting['plan_source'] is None
    assert 'attempt' not in waiting and call(mounted, 'recover')['pending_remote_bindings'] == []
    assert mounted.bridge.campaign_worker_service.next_work(mounted.eid, 'worker-service') == waiting
    assert execution.read_execution(*mounted.scope) == snapshot


@pytest.mark.parametrize('op', ['export', 'bind', 'admit', 'settle'])
@pytest.mark.parametrize('denial', ['subject', 'revoked', 'cross-project', 'same-machine'])
def test_attempt_operations_reject_wrong_authority(mounted, op, denial):
    attempt = call(mounted, 'next')['attempt']
    request = bind_body(attempt)
    binding = call(mounted, 'bind', **request)['binding']
    call(mounted, 'admit', **admit_body(binding))
    bodies = {'export': dict(attempt_id=attempt['attempt_id'], fence=attempt['fence']),
              'bind': request, 'admit': admit_body(binding), 'settle': settle_body(binding)}
    eid = mounted.eid
    subject = 'worker-service'
    if denial == 'subject':
        subject = 'browser-user'
    elif denial == 'revoked':
        enrollment.revoke_enrollment(*mounted.scope, eid, mounted.principal)
    elif denial == 'cross-project':
        _, eid = mounted.enroll()
    else:
        # Reassign the configured host to a new enabled enrollment in the SAME scope.
        # Its machine matches the frozen binding, but its persisted worker does not.
        other = enrollment.request_enrollment(*mounted.scope, mounted.principal, machine_id='VM-D')
        enrollment.enable_enrollment(*mounted.scope, other['enrollment_id'], mounted.principal)
        with db.connection() as conn:
            conn.execute("UPDATE campaign_host_enrollments SET machine_id='VM-retired' WHERE enrollment_id=%s",
                         (uuid.UUID(eid),))
            conn.execute("UPDATE campaign_host_enrollments SET machine_id='VM-C' WHERE enrollment_id=%s",
                         (uuid.UUID(other['enrollment_id']),))
        eid = other['enrollment_id']
        assert mounted.bridge.handle('recover', {'enrollment_id': eid}, subject)['pending_remote_bindings'] == []
        assert mounted.bridge.handle('next', {'enrollment_id': eid}, subject)['kind'] != 'recover'
    before = execution.read_execution(*mounted.scope)
    with pytest.raises(mounted.bridge.BridgeError) as error:
        mounted.bridge.handle(op, dict(enrollment_id=eid, **bodies[op]), subject)
    assert error.value.status == 403
    assert execution.read_execution(*mounted.scope) == before


@pytest.mark.parametrize('failure', ['expired', 'fence', 'oversized', 'digest', 'size', 'seed',
                                      'commit', 'revoked-during-export', 'expired-during-export'])
def test_export_rejects_stale_authority_and_invalid_source(mounted, monkeypatch, failure):
    attempt = call(mounted, 'next')['attempt']
    fence = attempt['fence']
    source = mounted.bridge.source_service
    if failure == 'expired':
        expire(attempt)
    elif failure == 'fence':
        fence += 1
    elif failure in ('seed', 'commit'):
        original = source.initialize_project_source
        key = 'seed_digest' if failure == 'seed' else 'source_commit'
        monkeypatch.setattr(source, 'initialize_project_source',
            lambda *args: dict(original(*args), **{key: 'f' * (64 if failure == 'seed' else 40)}))
    else:
        original = source.export_project_source_bundle

        def export(*args):
            bundle = original(*args)
            if failure == 'oversized':
                raw = b'x' * 262145
                return dict(bundle, bundle=raw, size_bytes=len(raw), bundle_sha256=hashlib.sha256(raw).hexdigest())
            if failure == 'digest':
                return dict(bundle, bundle_sha256='0' * 64)
            if failure == 'size':
                return dict(bundle, size_bytes=len(BUNDLE) + 1)
            if failure == 'revoked-during-export':
                enrollment.revoke_enrollment(*mounted.scope, mounted.eid, mounted.principal)
            if failure == 'expired-during-export':
                expire(attempt)
            return bundle

        monkeypatch.setattr(source, 'export_project_source_bundle', export)
    with pytest.raises(mounted.bridge.BridgeError) as error:
        call(mounted, 'export', attempt_id=attempt['attempt_id'], fence=fence)
    assert error.value.status == (403 if failure == 'revoked-during-export' else 409)


def test_nonplanning_attempt_and_closed_requests_fail_before_mutation(mounted):
    capability = execution.claim_task(*mounted.scope, worker_id='enrollment-' + mounted.eid, lease_seconds=900)
    with pytest.raises(mounted.bridge.BridgeError) as error:
        call(mounted, 'export', attempt_id=capability['attempt_id'], fence=capability['fence'])
    assert error.value.status == 403
    for op, body in [('next', {'org_id': str(mounted.scope[0])}), ('other', {}),
                     ('export', {'attempt_id': str(uuid.uuid4()), 'fence': True}),
                     ('export', {'attempt_id': 1, 'fence': 1})]:
        with pytest.raises(mounted.bridge.BridgeError) as error:
            call(mounted, op, **body)
        assert error.value.status == 400


def test_next_recovers_binding_created_during_source_io(mounted, monkeypatch):
    service = mounted.bridge.campaign_worker_service
    initialize = mounted.bridge.source_service.initialize_project_source
    bindings = []

    def initialize_and_bind(*args):
        seed = initialize(*args)
        # Another coordinator can claim and bind while next has released its
        # enrollment lock for the source producer.
        enrollment_store, ledger, plan = service._platform()
        with ledger._cursor() as cur:
            scope = enrollment_store.resolve_worker_scope(cur, mounted.eid, 'worker-service')
            plan.ensure_first_task(scope, seed['source_commit'], scope['prompt'])
        with ledger._cursor() as cur:
            scope = enrollment_store.resolve_worker_scope(cur, mounted.eid, 'worker-service')
            ledger._lock(cur, 'campaign-next:' + mounted.eid)
            attempt = ledger._claim_task_cursor(cur, scope,
                worker_id='enrollment-' + mounted.eid, lease=900, task_key='campaign-plan')
        bindings.append(call(mounted, 'bind', **bind_body(attempt))['binding'])
        return seed

    monkeypatch.setattr(mounted.bridge.source_service, 'initialize_project_source', initialize_and_bind)
    result = call(mounted, 'next')
    assert result['kind'] == 'recover'
    assert result['pending_remote_bindings'] == bindings
    assert 'attempt' not in result
