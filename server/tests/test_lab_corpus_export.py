"""Export joins the saved graph and durable corpus producer receipt.

Synthetic receipts test this boundary, not numerical corpus evaluation.
"""
import copy
import gzip
import hashlib
import json
import uuid

import pytest

import jobs
import lab_artifacts as ledger
import lab_corpus_executor as corpus
import lab_run_store as runs
import lab_tool_manifests
from lab_canonical import graph_digest


@pytest.fixture
def exported_run(monkeypatch, tmp_path):
    for name in ('LEAF_LAB_STORE', 'LEAF_LAB_RUN_STORE'):
        monkeypatch.setenv(name, 'sqlite')
    monkeypatch.setenv('LEAF_LAB_DB', str(tmp_path / 'export.db'))
    monkeypatch.setenv('LEAF_RUNTIME_ENV', 'test')
    monkeypatch.setenv('LEAF_PLATFORM_POSTGRES_REQUIRED', '0')
    monkeypatch.setenv('LEAF_LAB_TOOL_NODES', '1')
    tenant, project, actor = (str(uuid.uuid4()) for _ in range(3))
    raw_suite = json.dumps({
        'schema': 'leafhft.block-suite.v1', 'symbol': 'SYNTHETIC',
        'parent_policy': {'min_k': 2}, 'child_min_k': [1],
        'blocks': {'null.identity': [{}]}, 'min_select_tickets': 100,
        'draws': 10, 'seed': 7}).encode()
    descriptor = {'material_id': str(uuid.uuid4()),
                  'sha256': hashlib.sha256(raw_suite).hexdigest(),
                  'byte_size': len(raw_suite)}
    rows = b'{"synthetic":true}\n'
    raw_rows = gzip.compress(rows)
    corpus_material = {'material_id': str(uuid.uuid4()),
                       'sha256': hashlib.sha256(raw_rows).hexdigest(),
                       'byte_size': len(raw_rows)}
    manifest, _ = lab_tool_manifests.corpus_capability()
    monkeypatch.setattr(lab_tool_manifests, 'registry_for',
                        lambda tenant: {'corpus-evaluator@1.0.0': manifest})
    settings = {'project_id': project, 'corpus': corpus_material,
                'suite': dict(descriptor), 'parameters': {'min_select_tickets': 0}}
    graph = {'schema': 'leaf.lab-graph.v1', 'blocks': [{
        'id': 'evaluation', 'registry': {'id': manifest['id'],
            'version': manifest['version']['semver'],
            'code_sha256': manifest['version']['code_sha256']},
        'settings': settings}], 'pipes': [], 'outputs': {}}
    artifact = ledger.save(tenant, 'export-policy', graph, None, 'member')
    authority = {'tenant_id': tenant, 'project_id': project, 'doc_id': 'export-policy',
                 'graph_revision': 1, 'graph_sha256': artifact['sha256']}
    policy = corpus.compile_policy(authority, 'evaluation', settings)
    context = corpus.validate_context({
        'schema': corpus.SCHEMA, 'tenant_id': tenant, 'org_id': tenant,
        'project_id': project, 'actor_id': actor,
        'corpus': corpus_material, 'suite': descriptor,
        'capability_sha256': corpus.ARCHIVE_SHA256, 'policy': policy})
    def sources(selected):
        assert selected['tenant_id'] == tenant
        assert selected['corpus'] == corpus_material
        assert selected['suite'] == descriptor
        return {'suite': raw_suite, 'corpus': raw_rows}
    monkeypatch.setattr(corpus, 'read_sources', sources)

    def persist(*, compiled=True, corrupt=None):
        current = copy.deepcopy(context)
        if not compiled:
            current.pop('policy')
        effective = (hashlib.sha256(corpus.compiled_suite(raw_suite, policy)).hexdigest()
                     if compiled else descriptor['sha256'])
        report = {'schema': 'leafhft.block-suite-result.v1', 'suite_sha256': effective,
                  'rows_sha256': hashlib.sha256(rows).hexdigest(), 'candidates': [], 'chosen': None,
                  'confirm_report': {}, 'verdict': 'insufficient',
                  'reasons': ['Synthetic export fixture, no evaluator executed.']}
        if compiled:
            report['policy_digest'] = policy['digest']
        if corrupt:
            # A valid hash of stale/wrong producer identities must not authorize export.
            if corrupt == 'parent_suite':
                report['suite_sha256'] = descriptor['sha256']
            elif corrupt == 'policy_digest':
                report['policy_digest'] = 'f' * 64
        value = {'schema': corpus.RESULT_SCHEMA, 'context': current,
                 'job_id': 'synthetic-job', 'worker_id': 'synthetic-worker', 'attempt': 1,
                 'row_count': 1, 'rows_sha256': report['rows_sha256'],
                 'evaluated_suite_sha256': effective, 'result': report,
                 'result_sha256': hashlib.sha256(corpus._json_bytes(report) + b'\n').hexdigest()}
        run = runs.create(tenant, 'export-policy', 'synthetic-run', 1, artifact['sha256'],
                          'ops:1', 'b' * 64, 'export-adapter', 'export-build')
        lease = runs.claim(tenant, run['run_id'], 'export-worker', 60,
                           build_revision='export-build', color='active')
        assert lease
        summary = {key: value[key] for key in ('job_id', 'worker_id', 'attempt',
                   'row_count', 'rows_sha256', 'result_sha256')}
        summary.update({key: run[key] for key in
                        ('run_id', 'doc_id', 'graph_revision', 'graph_sha256')})
        summary['block_id'] = 'evaluation'
        assert runs.append_events(tenant, run['run_id'], 'export-worker', lease['fence'], [{
            'kind': 'value', 'block_id': 'evaluation',
            'payload': {'result': {'data': json.dumps(summary)}}}])
        assert runs.finish(tenant, run['run_id'], 'export-worker', lease['fence'])
        job = {'tenant_id': tenant, 'org_id': tenant, 'project_id': project,
               'idempotency_key': 'lab:synthetic-run:evaluation:corpus:v1',
               'tool': corpus.OPERATION, 'status': 'complete', 'result': {'result': value}}
        monkeypatch.setattr(jobs, 'get_job', lambda job_id: job if job_id == 'synthetic-job' else None)
        return value

    return tenant, artifact, policy, persist


@pytest.mark.parametrize('compiled', [False, True], ids=['original-suite', 'compiled-policy'])
def test_export_preserves_the_producer_suite_and_policy(exported_run, compiled):
    tenant, artifact, policy, persist = exported_run
    value = persist(compiled=compiled)
    bundle = ledger.export_bundle(tenant, 'export-policy', 1, authorize=lambda graph, run: None)
    parts = bundle['parts']
    assert parts['graph'] == artifact['graph']
    assert parts['identity']['sha256'] == artifact['sha256']
    exported = parts['evaluations'][0]['complete_results']['synthetic-job']
    assert exported == value
    if compiled:
        assert exported['context']['policy'] == policy
        assert exported['result']['suite_sha256'] != exported['context']['suite']['sha256']
    for name, part in parts.items():
        assert bundle['manifest']['parts'][name] == graph_digest(part)


@pytest.mark.parametrize('corrupt', ['parent_suite', 'policy_digest'])
def test_export_refuses_stale_or_wrong_compiled_identities(exported_run, corrupt):
    tenant, _, _, persist = exported_run
    persist(corrupt=corrupt)
    with pytest.raises(ledger.StoreUnavailable, match='lab_export_lineage_mismatch'):
        ledger.export_bundle(tenant, 'export-policy', 1, authorize=lambda graph, run: None)


def test_export_authorizes_before_resolving_a_private_job(exported_run, monkeypatch):
    tenant, _, _, persist = exported_run
    persist()
    monkeypatch.setattr(jobs, 'get_job', lambda job_id: pytest.fail('private job read before authorization'))
    def refused(graph, run):
        raise ledger.ExportForbidden()
    with pytest.raises(ledger.ExportForbidden):
        ledger.export_bundle(tenant, 'export-policy', 1, authorize=refused)
