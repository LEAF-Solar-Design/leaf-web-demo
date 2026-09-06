"""Closed plan validation and real PostgreSQL first-task authority proofs."""
import hashlib
import importlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
import uuid

import pytest

from leaf_platform import campaign_plan as plan, campaigns, campaign_enrollment as enrollment
from leaf_platform import campaign_execution as execution, db, store


CAMPAIGN = str(uuid.uuid4())
DIGEST = hashlib.sha256(b'Organize recipes').hexdigest()
SOURCE = 'b' * 40


def task(key='build', path='src/app.py', depends_on=None):
    return dict(task_key=key, title='Build recipes', spec='Implement recipe organization',
                capability='recipes.build', stages=['implementation', 'build_test'],
                owned_paths=[path], depends_on=depends_on or [],
                verify_argv=['python', '-m', 'pytest'], artifacts=['implementation'],
                questions=[], capabilities_required=['recipes.storage'])


def document():
    return dict(contract=plan.PLAN_CONTRACT, campaign_id=CAMPAIGN, prompt_digest=DIGEST,
                source_sha=SOURCE, summary='Organize recipes', tasks=[task()], open_questions=[])


def validate(value):
    return plan.validate_plan(value, campaign_id=CAMPAIGN, prompt_digest=DIGEST, source_sha=SOURCE)


def test_validator_accepts_all_input_forms_and_normalizes_copy():
    value = document()
    for source in (value, json.dumps(value), json.dumps(value).encode()):
        result = validate(source)
        assert result == value and result is not value


@pytest.mark.parametrize('field,value', [
    ('stages', ['publication']), ('stages', ['build_test', 'implementation']),
    ('stages', ['implementation', 'implementation']), ('verify_argv', ['bash', '-c', 'true']),
    ('task_key', 'campaign-plan'), ('task_key', 'host-enrollment-machine'),
    ('capabilities_required', ['recipes.storage', 'recipes.storage']),
    ('capabilities_required', ['Provider']), ('questions', [{'question_key': 'q', 'prompt': 'Q', 'budget': 1}]),
])
def test_validator_rejects_task_boundary_violations(field, value):
    doc = document()
    doc['tasks'][0][field] = value
    with pytest.raises(campaigns.CampaignError) as error:
        validate(doc)
    assert error.value.code == 'invalid_plan'


@pytest.mark.parametrize('path', ['../app', 'src/../app', '/app', 'C:/app', 'src\\app',
                                 './app', 'src//app', 'src/', '.leaf/plan.json', '.leaf',
                                 'src/./app', 'src/\x00app', 'src/\napp', 'src/\x7fapp'])
def test_validator_rejects_path_escapes(path):
    doc = document()
    doc['tasks'][0]['owned_paths'] = [path]
    with pytest.raises(campaigns.CampaignError):
        validate(doc)


def test_validator_closed_json_identity_and_size():
    for target in ('root', 'task'):
        doc = document()
        (doc if target == 'root' else doc['tasks'][0])['unknown'] = True
        with pytest.raises(campaigns.CampaignError):
            validate(doc)
    for field, value in [('campaign_id', str(uuid.uuid4())), ('prompt_digest', 'a' * 64),
                         ('source_sha', 'c' * 40), ('summary', float('nan'))]:
        doc = document()
        doc[field] = value
        with pytest.raises(campaigns.CampaignError):
            validate(doc)
    raw = json.dumps(document())
    for invalid in (raw.replace('"summary":', '"summary":"duplicate","summary":'),
                    raw.replace('"title":', '"title":"duplicate","title":'),
                    raw.replace('"Organize recipes"', 'NaN'), b'\xff', ' ' * 262145 + raw):
        with pytest.raises(campaigns.CampaignError):
            validate(invalid)
    doc = document()
    doc['tasks'] = [task('task-' + str(i), 'src/' + str(i)) for i in range(13)]
    with pytest.raises(campaigns.CampaignError):
        validate(doc)


def test_validator_dependency_ordered_overlap_and_cycle():
    doc = document()
    doc['tasks'] = [task('base', 'src'), task('middle', 'other', ['base']),
                    task('finish', 'src/app.py', ['middle'])]
    assert validate(doc) == doc
    doc['tasks'][2]['depends_on'] = []
    with pytest.raises(campaigns.CampaignError, match='overlap'):
        validate(doc)
    doc['tasks'][2]['depends_on'] = ['middle']
    doc['tasks'][0]['depends_on'] = ['finish']
    with pytest.raises(campaigns.CampaignError, match='cycle'):
        validate(doc)
    doc['tasks'][0]['depends_on'] = ['missing']
    with pytest.raises(campaigns.CampaignError):
        validate(doc)


def test_validator_exact_overlap_requires_dependency():
    doc = document()
    doc['tasks'].append(task('integrate', 'src/app.py'))
    with pytest.raises(campaigns.CampaignError, match='overlap'):
        validate(doc)
    doc['tasks'][1]['depends_on'] = ['build']
    assert validate(doc) == doc


def test_validator_dict_byte_limit_and_nested_duplicates():
    doc = document()
    doc['tasks'][0]['verify_argv'] = ['python', 'x' * 262144]
    with pytest.raises(campaigns.CampaignError, match='byte limit'):
        validate(doc)
    doc = document()
    doc['open_questions'] = [{'question_key': 'q', 'prompt': 'Choose'}]
    raw = json.dumps(doc).replace('"question_key":', '"question_key":"duplicate","question_key":')
    with pytest.raises(campaigns.CampaignError, match='duplicate'):
        validate(raw)


@pytest.fixture
def enrolled(make_org, monkeypatch):
    monkeypatch.setenv('LEAF_CAMPAIGN_ALLOWED_MACHINES', 'VM-C,VM-D')
    monkeypatch.setenv('LEAF_CAMPAIGN_WORKER_SUBJECT', 'worker-service')
    monkeypatch.setenv('LEAF_SOURCE_SHA', 'a' * 40)
    monkeypatch.setenv('LEAF_CAMPAIGN_FIRST_TASK_PRODUCER', 'on')
    org = make_org()
    project = store.create_project(org.org_id, 'Planning project')
    principal = store.create_identity_binding(org.org_id, 'auth0', f'auth0|{uuid.uuid4()}', role='owner')
    with db.connection() as conn:
        conn.execute('INSERT INTO project_member_bindings '
                     '(membership_id, org_id, project_id, binding_id, role, invited_by_binding_id) '
                     "VALUES (%s,%s,%s,%s,'owner',%s)",
                     (uuid.uuid4(), org.org_id, project.project_id, principal.binding_id, principal.binding_id))
    campaign = campaigns.submit_campaign(org.org_id, project.project_id, str(org.org_id),
        principal.binding_id, title='ReciPDF', prompt='Organize recipes', idempotency_key='planning')
    scope = (org.org_id, project.project_id, campaign['campaign_id'])
    row = enrollment.request_enrollment(*scope, principal.binding_id, machine_id='VM-C')
    enrollment.enable_enrollment(*scope, row['enrollment_id'], principal.binding_id)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'server'))
    service = importlib.import_module('campaign_worker_service')
    calls = []

    def source(tenant, org_id, project_id, prompt):
        assert (tenant, org_id, project_id) == (str(org.org_id), str(org.org_id), str(project.project_id))
        calls.append(prompt)
        return dict(source_commit=SOURCE, source_tree='c' * 40,
                    seed_digest=hashlib.sha256(prompt.encode('utf-8')).hexdigest(), replayed=True)

    monkeypatch.setattr(service.project_repository_source, 'initialize_project_source', source)
    return scope, principal.binding_id, row['enrollment_id'], service, calls


def next_call(enrolled):
    return enrolled[3].next_work(enrolled[2], 'worker-service')


def snapshot(enrolled):
    return execution.read_execution(*enrolled[0])


def expire(attempt):
    with db.connection() as conn:
        conn.execute("UPDATE campaign_task_attempts SET deadline_at=clock_timestamp()-interval '1 second' "
                     'WHERE attempt_id=%s', (uuid.UUID(attempt['attempt_id']),))


def test_real_postgres_first_task_token_once_full_prompt_and_expiry(enrolled):
    first = next_call(enrolled)
    assert first['kind'] == 'claimed' and first['attempt']['attempt_token']
    assert first['plan_task']['task_key'] == 'campaign-plan'
    assert first['plan_task']['source_sha'] == SOURCE
    assert first['plan_task']['owned_paths'] == ['.leaf/campaign-plan.json']
    assert first['plan_task']['verify_command'] == plan.VERIFY_COMMAND
    assert first['plan_task']['spec'].endswith('Organize recipes')
    assert DIGEST in first['plan_task']['spec']
    assert first['source']['source_tree'] == 'c' * 40
    second = next_call(enrolled)
    assert second['kind'] == 'active' and 'attempt_token' not in second['attempt']
    assert len(enrolled[4]) == 1
    tasks = snapshot(enrolled)['tasks']
    assert len(tasks) == 2
    assert next(t for t in tasks if t['kind'] == 'capability')['status'] == 'pending'
    expire(first['attempt'])
    recovered = next_call(enrolled)
    assert recovered['kind'] == 'claimed'
    assert recovered['attempt']['fence'] == first['attempt']['fence'] + 1
    assert recovered['attempt']['attempt_token'] != first['attempt']['attempt_token']
    assert len(snapshot(enrolled)['tasks']) == 2


def test_real_postgres_concurrent_next_converges(enrolled, monkeypatch):
    service = enrolled[3]
    original = service.project_repository_source.initialize_project_source
    barrier = Barrier(2)

    def source(*args):
        barrier.wait(timeout=15)
        return original(*args)

    monkeypatch.setattr(service.project_repository_source, 'initialize_project_source', source)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: next_call(enrolled), range(2)))
    assert sorted(row['kind'] for row in results) == ['active', 'claimed']
    assert sum('attempt_token' in row.get('attempt', {}) for row in results) == 1
    assert len(snapshot(enrolled)['tasks']) == 2


def test_real_postgres_revocation_during_source_prevents_task(enrolled, monkeypatch):
    scope, principal, eid, service, _ = enrolled
    original = service.project_repository_source.initialize_project_source

    def source(*args):
        enrollment.revoke_enrollment(*scope, eid, principal)
        return original(*args)

    monkeypatch.setattr(service.project_repository_source, 'initialize_project_source', source)
    with pytest.raises(campaigns.CampaignError) as error:
        next_call(enrolled)
    assert error.value.code == 'worker_forbidden'
    assert len(snapshot(enrolled)['tasks']) == 1
    assert not any(e['event_type'] == 'attempt_claimed' for e in snapshot(enrolled)['events'])


def test_real_postgres_revocation_before_claim_prevents_attempt(enrolled, monkeypatch):
    scope, principal, eid, service, _ = enrolled
    original = enrollment.resolve_worker_scope
    calls = []

    def resolve(cur, enrollment_id, subject):
        calls.append(enrollment_id)
        if len(calls) == 3:
            enrollment.revoke_enrollment(*scope, eid, principal)
        return original(cur, enrollment_id, subject)

    monkeypatch.setattr(enrollment, 'resolve_worker_scope', resolve)
    with pytest.raises(campaigns.CampaignError) as error:
        next_call(enrolled)
    assert error.value.code == 'worker_forbidden'
    assert not any(e['event_type'] == 'attempt_claimed' for e in snapshot(enrolled)['events'])


@pytest.mark.parametrize('denial', ['subject', 'revoked', 'inactive', 'cross-project'])
def test_real_postgres_scope_denial_has_no_producer_or_task(enrolled, denial):
    scope, principal, eid, service, calls = enrolled
    if denial == 'revoked':
        enrollment.revoke_enrollment(*scope, eid, principal)
    elif denial == 'inactive':
        with db.connection() as conn:
            conn.execute('UPDATE projects SET deleted_at=NOW() WHERE project_id=%s', (scope[1],))
    elif denial == 'cross-project':
        other = store.create_project(scope[0], 'Other project')
        with db.connection() as conn:
            conn.execute('UPDATE campaign_capability_links SET project_id=%s WHERE enrollment_id=%s',
                         (other.project_id, uuid.UUID(eid)))
    with pytest.raises(campaigns.CampaignError) as error:
        service.next_work(eid, 'wrong-service' if denial == 'subject' else 'worker-service')
    assert error.value.code in ('worker_forbidden', 'project_unavailable')
    assert calls == [] and len(snapshot(enrolled)['tasks']) == 1


def test_real_postgres_remote_recovery_precedes_source_and_filters_machine(enrolled):
    scope, principal, eid, service, calls = enrolled
    attempt = execution.claim_task(*scope, worker_id='enrollment-' + eid, lease_seconds=30)
    binding = execution.bind_remote_dispatch(*scope, attempt['attempt_id'], fence=attempt['fence'],
        machine_id='VM-C', run_id='planning-recovery', registration_id='planning-registration',
        root_request_id='planning-root', gateway_project_id='planning-project', source_ref='a' * 40,
        packet_digest='b' * 64, budget_class='explicit', reservation_micro_usd=1000000)
    result = next_call(enrolled)
    assert result['kind'] == 'recover' and calls == []
    assert result['pending_remote_bindings'] == enrollment.resolve_worker_enrollment(eid, 'worker-service')
    assert result['pending_remote_bindings'][0]['attempt_id'] == binding['attempt_id']
    other = enrollment.request_enrollment(*scope, principal, machine_id='VM-D')
    enrollment.enable_enrollment(*scope, other['enrollment_id'], principal)
    assert service.next_work(other['enrollment_id'], 'worker-service')['kind'] == 'claimed'


def test_real_postgres_source_conflict_preserves_task(enrolled, monkeypatch):
    first = next_call(enrolled)
    expire(first['attempt'])
    before = snapshot(enrolled)
    original = enrolled[3].project_repository_source.initialize_project_source
    monkeypatch.setattr(enrolled[3].project_repository_source, 'initialize_project_source',
                        lambda *args: dict(original(*args), source_commit='d' * 40))
    with pytest.raises(campaigns.CampaignConflict) as error:
        next_call(enrolled)
    assert error.value.code == 'plan_source_conflict'
    assert snapshot(enrolled) == before


def test_real_postgres_invalid_tenant_never_initializes_source(enrolled):
    with db.connection() as conn:
        conn.execute('UPDATE campaigns SET tenant_id=%s WHERE campaign_id=%s',
                     ('unmapped-tenant', uuid.UUID(enrolled[0][2])))
    before = snapshot(enrolled)
    with pytest.raises(enrolled[3].project_repository_source.SourceConflict):
        next_call(enrolled)
    assert snapshot(enrolled) == before and enrolled[4] == []


def test_real_postgres_mismatched_seed_creates_no_task(enrolled, monkeypatch):
    source = enrolled[3].project_repository_source
    original = source.initialize_project_source
    monkeypatch.setattr(source, 'initialize_project_source',
                        lambda *args: dict(original(*args), seed_digest='f' * 64))
    before = snapshot(enrolled)
    with pytest.raises(source.SourceConflict):
        next_call(enrolled)
    assert snapshot(enrolled) == before


@pytest.mark.parametrize('prompt', ['x' * 12001, '\u00e9' * 6001])
def test_real_postgres_oversized_prompt_no_task_or_source(enrolled, prompt):
    with db.connection() as conn:
        conn.execute('UPDATE campaigns SET prompt=%s WHERE campaign_id=%s',
                     (prompt, uuid.UUID(enrolled[0][2])))
    before = snapshot(enrolled)
    with pytest.raises(campaigns.CampaignConflict) as error:
        next_call(enrolled)
    assert error.value.code == 'prompt_too_large'
    assert snapshot(enrolled) == before and enrolled[4] == []


def test_real_postgres_unicode_digest_and_syntax_only_build_test_rejected(enrolled):
    prompt = 'Organize cr\u00eape recipes\nPreserve every word.'
    with db.connection() as conn:
        conn.execute('UPDATE campaigns SET prompt=%s WHERE campaign_id=%s',
                     (prompt, uuid.UUID(enrolled[0][2])))
    first = next_call(enrolled)
    assert first['source']['seed_digest'] == hashlib.sha256(prompt.encode('utf-8')).hexdigest()
    assert first['plan_task']['spec'].endswith(prompt)
    attempt = first['attempt']
    execution.settle_attempt(*enrolled[0], attempt['attempt_id'],
        attempt_token=attempt['attempt_token'], fence=attempt['fence'], outcome='succeeded',
        artifact_ref='artifact:campaign-plan', result={})
    attempt = next_call(enrolled)['attempt']
    before = snapshot(enrolled)
    with pytest.raises(campaigns.CampaignError) as error:
        execution.settle_attempt(*enrolled[0], attempt['attempt_id'],
            attempt_token=attempt['attempt_token'], fence=attempt['fence'], outcome='succeeded',
            artifact_ref='artifact:campaign-plan',
            result={'exit_code': 0, 'verify_command': plan.VERIFY_COMMAND})
    assert error.value.code == 'insufficient_evidence'
    assert snapshot(enrolled) == before
    assert len(snapshot(enrolled)['tasks']) == 2
