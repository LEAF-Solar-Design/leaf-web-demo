"""Acquisition recovery actions survive the completion runtime stage receipt."""
from copy import deepcopy
import importlib
from types import SimpleNamespace
import uuid

import pytest

import campaign_acquisition_service as acquisition
import campaign_release_service as runtime


@pytest.fixture
def canonical():
    import job_pg_store
    job_pg_store._db()  # Register the existing platform package, no DB connection.
    return importlib.import_module('leaf_platform.campaign_release')


@pytest.mark.parametrize('workflow', [
    '  Design feeder routing, parts and cost from this structured input.\n'
    + 'Preserve every constraint and report unresolved requirements.\n' * 30 + 'Final constraint: café.  ',
    'Deliver feeder_request.json unchanged.\nVerify the downloaded file.',
])
def test_compile_preserves_exact_request_without_claiming_existing_file_fulfills_it(monkeypatch, workflow, canonical):
    from routers import campaigns

    source = '{"feeders":[{"name":"F1","load":100}]}'
    state = {'files': [{'path': 'feeder_request.json', 'content': source}]}
    before = deepcopy(state)
    monkeypatch.setattr(runtime, 'authority', lambda *a: ('org', 'project', 'actor'))
    monkeypatch.setattr(campaigns, '_store', lambda: SimpleNamespace(
        get_campaign=lambda *a: {'prompt': 'Original campaign goal'}))
    monkeypatch.setattr(runtime, '_lifecycle', lambda: SimpleNamespace(project_snapshot=lambda *a: state))
    finish = {'delivery_profile': 'cad_file', 'intended_user': 'Project owner',
              'workflow': workflow, 'artifact_refs': ['feeder_request.json']}
    contract = runtime.compile_finish('tenant', 'project', 'campaign', finish)

    assert contract['requested_workflow'] == workflow
    assert finish['workflow'] == workflow
    assert contract['workflow'] != workflow
    assert contract['scope_classification'] == {
        'version': 1, 'capability': 'existing_artifact_delivery',
        'requested_workflow_status': 'unverified'}
    assert contract['selected_artifact'] == runtime.delivery.validate_bytes('feeder_request.json', source.encode())
    assert len(contract['required_checks']) == 5
    assert not any(key in contract for key in ('web_recipe', 'transform_recipe', 'cad_recipe'))
    assert state == before
    stored = canonical._contract(deepcopy(contract), 'cad_file')
    assert stored == contract
    assert stored['requested_workflow'] == workflow
    assert stored['original_goal'] == 'Original campaign goal'

    legacy = deepcopy(contract)
    del legacy['requested_workflow']
    del legacy['scope_classification']
    assert canonical._contract(deepcopy(legacy), 'cad_file') == legacy

    bounded = deepcopy(contract)
    bounded['requested_workflow'] = ' ' + 'x' * 16382 + ' '
    assert canonical._contract(deepcopy(bounded), 'cad_file') == bounded

    for mutate in [
        lambda c: c.update(extra='unsupported'),
        *[lambda c, text=text: c.update(requested_workflow=text)
          for text in (None, True, 1, [], {}, '', ' \n\t', 'x' * 16385, 'text\x00')],
        *[lambda c, classification=classification: c.update(scope_classification=classification)
          for classification in (None, True, 1, 'unverified', [])],
        lambda c: c['scope_classification'].update(extra='unsupported'),
        *[lambda c, version=version: c['scope_classification'].update(version=version)
          for version in (True, 1.0, '1', 0, 2, None)],
        *[lambda c, capability=capability: c['scope_classification'].update(capability=capability)
          for capability in ('requested_workflow', None, 1, [], {})],
        *[lambda c, status=status: c['scope_classification'].update(requested_workflow_status=status)
          for status in ('verified', None, True, [], {})],
        *[lambda c, field=field: c['scope_classification'].pop(field)
          for field in ('version', 'capability', 'requested_workflow_status')],
        lambda c: c.pop('requested_workflow'),
        lambda c: c.pop('selected_artifact'),
        lambda c: c.update(selected_artifact=None),
        lambda c: c['selected_artifact'].update(bytes_verified=False),
        lambda c: c['selected_artifact'].update(extra='unsupported'),
        *[lambda c, field=field: c.update({field: None})
          for field in ('web_recipe', 'transform_recipe', 'cad_recipe')],
        lambda c: c['scope_classification'].update(command='execute'),
        lambda c: c['scope_classification'].update(token='credential'),
    ]:
        invalid = deepcopy(contract)
        mutate(invalid)
        with pytest.raises(canonical.CampaignError):
            canonical._contract(invalid, 'cad_file')


@pytest.mark.parametrize('adapter', ['web', 'transform', 'cad'])
def test_compile_preserves_requested_workflow_for_specialized_adapters(monkeypatch, adapter):
    from routers import campaigns

    workflow = 'Make CSV output.\nKeep the requested scope.'
    artifact = runtime.delivery.validate_bytes('records.csv', b'name\nExample\n')
    recipe = {'source_artifact': {'path': 'records.json'}}
    state = {'files': [{'path': 'records.json', 'content': '[{"name":"Example"}]'}]}
    monkeypatch.setattr(runtime, 'authority', lambda *a: ('org', 'project', 'actor'))
    monkeypatch.setattr(campaigns, '_store', lambda: SimpleNamespace(
        get_campaign=lambda *a: {'prompt': 'Original campaign goal'}))
    monkeypatch.setattr(runtime, '_lifecycle', lambda: SimpleNamespace(project_snapshot=lambda *a: state))
    monkeypatch.setattr(runtime.dxf_inventory, 'requested', lambda *a: adapter == 'cad')
    monkeypatch.setattr(runtime.dxf_inventory, 'compile_recipe', lambda *a: (artifact, recipe))
    monkeypatch.setattr(runtime.web_release, 'compile_recipe', lambda *a: (artifact, recipe))
    contract = runtime.compile_finish('tenant', 'project', 'campaign', {
        'delivery_profile': 'web_tool' if adapter == 'web' else 'cad_file',
        'intended_user': 'Project owner', 'workflow': workflow, 'artifact_refs': ['records.json']})

    assert contract['requested_workflow'] == workflow
    assert contract[adapter + '_recipe'] == recipe
    assert 'scope_classification' not in contract


@pytest.mark.parametrize('failure', ['budget', 'ordinary', 'invalid_file'])
def test_advance_records_failure_before_waiting_with_recovery_action(monkeypatch, failure):
    org, project, campaign, release_id, actor = [str(uuid.uuid4()) for _ in range(5)]
    source = b'[{"name":"Example"}]'
    artifact = runtime.delivery.validate_bytes('records.csv', b'"name"\r\n"Example"\r\n')
    release = {'release_id': release_id, 'contract_version': 2, 'status': 'active',
        'contract': {'selected_artifact': artifact, 'transform_recipe': {'source_artifact': {}},
                     'required_checks': [{'stage': stage, 'check_id': stage + '-check'}
                                         for stage in runtime.STAGES]}}
    previous = {'stage': 'implementation', 'status': 'passed', 'contract_version': 1,
                'evidence': {'receipt': {'receipt_id': 'previous-proof'}}}
    completion = {'release': release, 'stages': [deepcopy(previous)], 'decisions': []}
    calls = []

    def record_stage(*args, **kwargs):
        assert release['status'] == 'active'
        calls.append(('stage', deepcopy(kwargs)))
        completion['stages'].append(deepcopy(kwargs))

    def set_progress(*args, **kwargs):
        assert [kind for kind, _ in calls] == ['stage']
        calls.append(('progress', deepcopy(kwargs)))
        release['status'] = kwargs['state']

    store = SimpleNamespace(get_release=lambda *a: deepcopy(completion), record_stage=record_stage,
        set_progress=set_progress, finish_release=lambda *a: pytest.fail('failed acquisition finished release'))
    monkeypatch.setattr(runtime, '_store', lambda: store)
    monkeypatch.setattr(runtime, 'authority', lambda *a: (org, project, actor))
    monkeypatch.setattr(runtime, '_transform_input', lambda *a: source)
    monkeypatch.setattr(runtime, '_pending', lambda *a: pytest.fail('failed acquisition skipped stage receipt'))
    acquired = []
    if failure == 'budget':
        reason = 'Workspace execution budget exhausted'
        action = ('Wait for the existing workspace limit reset or ask the workspace administrator '
                  'to review the limit, then explicitly resume this release')
    elif failure == 'ordinary':
        reason = 'The published transform job failed'
        action = 'Inspect the existing job before one bounded correction'
    else:
        reason = 'Source records changed before acquisition'
        action = 'Restore a valid source artifact or provide the missing delivery adapter'

        def invalid_input(*args):
            raise runtime.delivery.DeliveryConflict(reason)

        monkeypatch.setattr(runtime, '_transform_input', invalid_input)

    def acquire(*args, **kwargs):
        acquired.append(True)
        assert args[5] == source
        return {'state': 'failed', 'reason': reason, 'recommended_action': action}

    monkeypatch.setattr(acquisition, 'advance', acquire)
    result = runtime._advance('tenant', project, campaign, release_id)
    assert acquired == ([] if failure == 'invalid_file' else [True])
    assert [kind for kind, _ in calls] == ['stage', 'progress']
    stage = calls[0][1]
    assert stage['stage'] == 'implementation' and stage['status'] == 'unavailable'
    assert stage['producer'] == runtime.PRODUCERS['implementation']
    observations = {'reason': reason, 'recommended_action': action}
    assert stage['evidence'] == {**observations, 'contract_version': 2,
        'source_revision': artifact['sha256'], 'checks': [
            {'check_id': 'implementation-check', 'status': 'unavailable', 'evidence': observations}]}
    assert calls[1][1] == {'state': 'waiting', 'next_action': {
        'wait_kind': 'authority', **observations}}
    assert result['release']['status'] == 'waiting'
    assert result['stages'][0] == previous
    assert len(result['stages']) == 2 and result['decisions'] == []
