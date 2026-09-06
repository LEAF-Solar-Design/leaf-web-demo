"""Real PostgreSQL publication and recovery with the actual P8 store producer.

Git and mounted HTTP coverage belongs to the harness adapter suite. These tests
use its response shapes and real source, confirmation, receipt and task rows.
"""
import copy
import hashlib
from pathlib import Path
import sys
import uuid

import pytest

from leaf_platform import campaign_execution as execution, db, repository_edit_store
from test_campaign_bridge import mounted, call, bind_body, admit_body, expire, SOURCE, TREE
from test_campaign_plan_adoption import document, prepare, snapshot

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'server' / 'tests'))
from test_campaign_product_execution import native_output
import campaign_product_execution as product
from project_repository_edit_contract import parse_staged_receipt, staged_receipt_digest, STAGED_RECEIPT_CONTRACT


@pytest.fixture
def products(mounted, monkeypatch):
    plan = document(mounted.scope)
    second = copy.deepcopy(plan['tasks'][0])
    second.update(task_key='second', owned_paths=['second.py'], depends_on=['recipes'])
    plan['tasks'].append(second)
    request, _ = prepare(mounted, plan)
    call(mounted, 'plan', **request)
    head = dict(commit=SOURCE, tree=TREE)
    calls = []
    crash = dict(stage=None)

    def initialize(tenant, org, project, prompt):
        return dict(source_commit=head['commit'], source_tree=head['tree'], replayed=True,
                    seed_digest=hashlib.sha256(prompt.encode()).hexdigest())

    def export(tenant, org, project, commit, tree, *, max_bytes):
        assert (commit, tree) == (head['commit'], head['tree']) and max_bytes == product.SOURCE_LIMIT
        raw = ('bundle at ' + commit).encode()
        return dict(bundle=raw, source_commit=commit, source_tree=tree,
                    bundle_sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw))

    def post(op, body):
        calls.append(op)
        edit = body['edit_id']
        if op == 'stage':
            receipt = parse_staged_receipt(dict(
                {k: body[k] for k in ('tenant_id', 'organization_id', 'project_id', 'repo_key',
                                      'edit_id', 'actor_binding_id', 'instruction_digest', 'idempotency_key')},
                contract=STAGED_RECEIPT_CONTRACT, state='staged', operation='edit', source_edit_id=None,
                writer_lease_id=str(uuid.uuid4()), writer_lease_generation=1,
                base_commit=body['expected_base_commit'], staged_head_commit='9' * 40,
                staged_tree='8' * 40, changed_paths=[f['path'] for f in body['files']], diff_digest='7' * 64))
            digest = staged_receipt_digest(receipt)
            row = repository_edit_store.record_staged(receipt, digest, expected_version=0,
                                                       transition_key=body['idempotency_key'])
            if crash['stage'] == 'stage':
                crash['stage'] = None
                raise RuntimeError('process lost after stage record')
            return dict(witness={}, receipt=receipt.to_mapping(), receiptDigest=digest, version=row['version'])
        row = product._edit_row(edit)
        if op == 'publish':
            row = repository_edit_store.consume_for_publish(edit, body['confirmation_id'],
                expected_version=body['expected_version'], transition_key=body['transition_key'],
                receipt_digest=body['receipt_digest'])
            if crash['stage'] == 'before_cas':
                crash['stage'] = None
                raise RuntimeError('process lost before CAS')
        head.update(commit=row['staged_head_commit'], tree=row['staged_tree'])
        if crash['stage'] == 'cas':
            crash['stage'] = None
            raise RuntimeError('process lost after CAS')
        method = repository_edit_store.recover_publish if op == 'recover' else repository_edit_store.settle_publish
        kwargs = dict(reason_code='campaign_publication_retry') if op == 'recover' else {}
        settled = method(edit, private_ref_commit=row['staged_head_commit'], main_commit=head['commit'],
            main_tree=head['tree'], expected_version=row['version'], transition_key=body['transition_key'] + ':settle', **kwargs)
        if crash['stage'] == 'settlement':
            crash['stage'] = None
            raise RuntimeError('process lost after P8 settlement')
        return dict(observation={}, settlement=settled)

    monkeypatch.setattr(product.source_service, 'initialize_project_source', initialize)
    monkeypatch.setattr(product.source_service, 'export_project_source_bundle', export)
    monkeypatch.setattr(product, '_post', post)
    return mounted, head, calls, crash


def implementation(products, *, late=False):
    mounted, _, _, _ = products
    first = call(mounted, 'next')
    assert first['kind'] == 'claimed' and first['task']['task_key'] == 'recipes'
    attempt = first['attempt']
    exported = call(mounted, 'export', attempt_id=attempt['attempt_id'], fence=attempt['fence'])
    request = bind_body(attempt)
    request['source_ref'] = exported['source']['source_commit']
    binding = call(mounted, 'bind', **request)['binding']
    call(mounted, 'admit', **admit_body(binding))
    if late:
        expire(attempt)
    call(mounted, 'settle', attempt_id=attempt['attempt_id'], fence=attempt['fence'],
        verdict=dict(run_id=binding['run_id'], leaf_id=binding['leaf_id'], fencing_token=7),
        outcome='succeeded', artifact_ref='native-manifest', result=dict(result_binding='bound',
            requested_source_sha=request['source_ref'], result_fingerprint='f' * 64))
    waiting = call(mounted, 'next')
    assert waiting['kind'] == 'awaiting_product_publication'
    saved = waiting['product_source']
    return dict(**{k: saved[k] for k in ('task_id', 'attempt_id', 'fence', 'result_fingerprint')},
                output=native_output(saved)), saved, exported


@pytest.mark.parametrize('late', [False, True])
def test_real_postgres_publication_then_advanced_dependent_source(products, late):
    mounted, head, calls, _ = products
    request, saved, exported = implementation(products, late=late)
    before = {t['task_id']: t for t in snapshot(mounted)['tasks']}
    response = call(mounted, 'product', **request)
    assert response['receipt']['stage'] == 'publication'
    assert response['source'] == dict(source_commit=head['commit'], source_tree=head['tree'])
    replay = call(mounted, 'product', **request)
    assert replay['receipt']['receipt_id'] == response['receipt']['receipt_id'] and replay['receipt']['replayed']
    assert calls == ['stage', 'publish']
    rows = execution.read_attempt_sources(*mounted.scope, saved['attempt_id'])
    assert rows['input_source']['commit_sha'] == SOURCE
    assert rows['result_source']['commit_sha'] == head['commit']
    assert rows['result_source']['attempt_id'] == saved['attempt_id']
    next_task = call(mounted, 'next')
    assert next_task['task']['task_key'] == 'second'
    advanced = call(mounted, 'export', attempt_id=next_task['attempt']['attempt_id'], fence=next_task['attempt']['fence'])
    assert advanced['source']['source_commit'] == head['commit'] != SOURCE
    assert advanced['task']['source_sha'] == SOURCE
    for task in snapshot(mounted)['tasks']:
        assert task['source_sha'] == before[task['task_id']]['source_sha']
        assert task['payload_fingerprint'] == before[task['task_id']]['payload_fingerprint']
    receipts = [r for r in snapshot(mounted)['receipts'] if r['task_id'] == saved['task_id'] and r['outcome'] == 'succeeded']
    assert [r['stage'] for r in receipts].count('build_test') == 1
    assert [r['stage'] for r in receipts].count('publication') == 1


@pytest.mark.parametrize('point', ['stage', 'before_cas', 'cas', 'settlement', 'l2'])
def test_real_postgres_restart_preserves_product_and_recovers_only_publication(products, monkeypatch, point):
    mounted, _, calls, crash = products
    request, saved, _ = implementation(products, late=True)
    crash['stage'] = point
    if point == 'l2':
        from leaf_platform import campaign_product_publication as publication
        original = publication.settle_publication
        def fail(*args, **kwargs):
            raise RuntimeError('process lost after L2 result source')
        monkeypatch.setattr(publication, 'settle_publication', fail)
    with pytest.raises(mounted.bridge.BridgeError):
        call(mounted, 'product', **request)
    if point == 'l2':
        monkeypatch.setattr(publication, 'settle_publication', original)
    snapshot_before = snapshot(mounted)
    task = next(t for t in snapshot_before['tasks'] if t['task_id'] == saved['task_id'])
    assert task['current_stage'] == 'publication' and task['status'] == 'pending'
    db.reset_pool()
    assert call(mounted, 'next')['kind'] == 'awaiting_product_publication'
    result = call(mounted, 'product', **request)
    assert result['receipt']['stage'] == 'publication'
    assert calls.count('stage') == 1 and calls.count('publish') == 1
    assert calls.count('recover') == (1 if point in ('before_cas', 'cas') else 0)
    assert all(r in snapshot(mounted)['receipts'] for r in snapshot_before['receipts'])


@pytest.mark.parametrize('wrong', ['principal', 'enrollment', 'attempt', 'task', 'fingerprint', 'hash', 'tenant'])
def test_real_postgres_invalid_product_has_no_effect(products, wrong):
    mounted, _, calls, _ = products
    request, _, _ = implementation(products)
    if wrong in ('principal', 'enrollment', 'tenant'):
        with db.connection() as conn:
            if wrong == 'principal':
                conn.execute('DELETE FROM project_member_bindings WHERE binding_id=%s',
                             (mounted.principal,))
            elif wrong == 'enrollment':
                conn.execute("UPDATE campaign_host_enrollments SET state='revoked',revoked_at=NOW() WHERE enrollment_id=%s",
                             (uuid.UUID(mounted.eid),))
            else:
                conn.execute('UPDATE campaigns SET tenant_id=%s WHERE campaign_id=%s',
                             (str(uuid.uuid4()), uuid.UUID(str(mounted.scope[2]))))
    elif wrong == 'attempt':
        request['attempt_id'] = str(uuid.uuid4())
    elif wrong == 'task':
        request['task_id'] = str(uuid.uuid4())
    elif wrong == 'fingerprint':
        request['result_fingerprint'] = '0' * 64
    else:
        request['output']['files'][0]['sha256'] = '0' * 64
    before = snapshot(mounted)
    with pytest.raises(mounted.bridge.BridgeError):
        call(mounted, 'product', **request)
    assert calls == [] and snapshot(mounted) == before


def test_real_postgres_recorded_export_conflict_preserves_attempt(products):
    mounted, head, _, _ = products
    first = call(mounted, 'next')['attempt']
    request = dict(attempt_id=first['attempt_id'], fence=first['fence'])
    initial = call(mounted, 'export', **request)
    assert call(mounted, 'export', **request) == initial
    recorded = execution.read_attempt_sources(*mounted.scope, first['attempt_id'])
    head.update(commit='9' * 40, tree='8' * 40)
    with pytest.raises(mounted.bridge.BridgeError) as error:
        call(mounted, 'export', **request)
    assert error.value.status == 409
    assert execution.read_attempt_sources(*mounted.scope, first['attempt_id']) == recorded
