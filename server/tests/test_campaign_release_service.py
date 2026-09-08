"""Acquisition recovery actions survive the completion runtime stage receipt."""
from copy import deepcopy
from types import SimpleNamespace
import uuid

import pytest

import campaign_acquisition_service as acquisition
import campaign_release_service as runtime


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
