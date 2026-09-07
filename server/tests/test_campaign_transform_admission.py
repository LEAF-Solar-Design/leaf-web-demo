"""Actual-output release integration and authority boundaries."""
from contextlib import contextmanager
from types import SimpleNamespace
import importlib.util

import pytest

import broker
import campaign_acquisition_service as acquisition
import campaign_release_service as runtime
import campaign_transform_job as transform
import campaign_web_tool_static as recipe
from routers import campaigns
from test_campaign_delivery_service import runtime as delivery_runtime


SOURCE = b'[{"name":"One","count":2},{"name":"=formula","count":3}]'


@pytest.fixture
def release(delivery_runtime, monkeypatch):
    store, lifecycle = delivery_runtime('source.json', SOURCE)
    source = store.release['contract']['selected_artifact']
    store.release['contract']['transform_recipe'] = {
        'recipe_id': recipe.RECIPE_ID, 'recipe_version': 1, 'source_artifact': source}
    store.release['contract']['selected_artifact'] = runtime.delivery.validate_bytes('records.csv', recipe.expected_output(SOURCE))
    store.progress = []
    store.questions = []
    def progress(*args, **kw):
        store.progress.append(kw)
        store.release['status'] = kw['state']
    store.set_progress = progress
    monkeypatch.setattr(campaigns, '_STORE', SimpleNamespace(
        ask_question=lambda *a, **kw: store.questions.append(kw)))
    return store, lifecycle


def complete(*args, **kwargs):
    return {'state': 'complete', 'output_bytes': recipe.expected_output(args[5]),
            'job_id': 'actual-job', 'publication': {'catalog_commit': 'a' * 40}}


def test_actual_transform_output_reaches_all_stages(release, monkeypatch):
    store, lifecycle = release
    calls = []
    def producer(*args, **kw):
        calls.append((args[5], kw))
        return complete(*args, **kw)
    monkeypatch.setattr(acquisition, 'advance', producer)
    rid = store.release['release_id']
    result = runtime.advance('tenant', 'project', 'campaign', rid, 'session', 'turn')
    assert result['release']['status'] == 'finished'
    assert calls == [(SOURCE, {'authority_session_id': 'session', 'authority_turn_id': 'turn'})]
    assert len(store.stages) == 5 and all(s['status'] == 'passed' for s in store.stages)
    assert store.stages[0]['evidence']['job_id'] == 'actual-job'
    assert lifecycle.writes == 2
    raw, meta = runtime.read_artifact('tenant', 'project', 'campaign', rid, 'records.csv')
    assert raw == recipe.expected_output(SOURCE) and meta['retrieved']
    assert 'campaign-records-to-csv' in store.stages[-1]['evidence']['replay_recipe']


@pytest.mark.parametrize('state,refs,kind', [
    ('working', {'job_id': 'job'}, 'job'), ('working', {'change_set_id': 'change'}, 'authoring'),
    ('awaiting_user', {}, 'authority')])
def test_pending_work_records_no_failed_stage(release, monkeypatch, state, refs, kind):
    store, lifecycle = release
    monkeypatch.setattr(acquisition, 'advance', lambda *a, **kw: dict(refs, state=state,
        reason='Existing work needs attention', recommended_action='Continue this release'))
    runtime.advance('tenant', 'project', 'campaign', store.release['release_id'])
    assert store.stages == []
    assert store.progress[-1]['next_action']['wait_kind'] == kind
    assert lifecycle.writes == 1
    assert len(store.questions) == (1 if state == 'awaiting_user' else 0)


def test_pending_then_resume_retains_frozen_input(release, monkeypatch):
    store, lifecycle = release
    monkeypatch.setattr(acquisition, 'advance', lambda *a, **kw: {
        'state': 'working', 'job_id': 'job', 'reason': 'running', 'recommended_action': 'Wait'})
    runtime.advance('tenant', 'project', 'campaign', store.release['release_id'])
    lifecycle.files[0]['content'] = '[{"name":"edited"}]'
    monkeypatch.setattr(acquisition, 'advance', complete)
    result = runtime.advance('tenant', 'project', 'campaign', store.release['release_id'])
    assert result['release']['status'] == 'finished'
    assert lifecycle.writes == 2
    assert lifecycle.files[-1]['content'].encode() == recipe.expected_output(SOURCE)


def test_lost_input_write_response_recovers_readback(release, monkeypatch):
    store, lifecycle = release
    monkeypatch.setattr(acquisition, 'advance', complete)
    lifecycle.interrupt = True
    with pytest.raises(RuntimeError):
        runtime.advance('tenant', 'project', 'campaign', store.release['release_id'])
    assert lifecycle.writes == 1 and store.stages == []
    result = runtime.advance('tenant', 'project', 'campaign', store.release['release_id'])
    assert result['release']['status'] == 'finished' and lifecycle.writes == 2


def test_wrong_actual_output_never_publishes(release, monkeypatch):
    store, lifecycle = release
    monkeypatch.setattr(acquisition, 'advance', lambda *a, **kw: {
        'state': 'complete', 'output_bytes': b'"wrong"\r\n"value"\r\n',
        'job_id': 'bad-job', 'publication': {}})
    result = runtime.advance('tenant', 'project', 'campaign', store.release['release_id'])
    assert result['release']['status'] != 'finished'
    assert len(store.stages) == 1 and store.stages[0]['status'] != 'passed'
    assert lifecycle.writes == 1


def test_busy_execution_guard_has_no_effects(release, monkeypatch):
    store, lifecycle = release
    @contextmanager
    def busy(*args):
        yield False
    store.execution_guard = busy
    monkeypatch.setattr(acquisition, 'advance', lambda *a, **kw: pytest.fail('busy release invoked'))
    runtime.advance('tenant', 'project', 'campaign', store.release['release_id'])
    assert lifecycle.writes == 0 and store.stages == []


def test_authored_switch_and_account_disable_admission(monkeypatch):
    monkeypatch.setattr(broker, '_authored_execution_enabled', lambda: False)
    with pytest.raises(acquisition.AcquisitionError, match='disabled'):
        acquisition._run_authority('tenant', {})
    monkeypatch.setattr(broker, '_authored_execution_enabled', lambda: True)
    monkeypatch.setattr(broker, 'tenant_disabled', lambda tenant: True)
    with pytest.raises(acquisition.AcquisitionError, match='disabled'):
        acquisition._run_authority('tenant', {})


def test_authored_switch_rechecked_before_execution_and_terminal(monkeypatch):
    monkeypatch.setattr(broker, '_authored_execution_enabled', lambda: False)
    # The real authority method fails before any execution or PG mutation.
    with pytest.raises(ValueError, match='authored execution'):
        transform.check_authority({'tenant_id': 'tenant'})


def test_csv_request_selects_transform_and_reuses_existing_csv(release, monkeypatch):
    store, lifecycle = release
    monkeypatch.setattr(campaigns, '_STORE', SimpleNamespace(get_campaign=lambda *a: {'prompt': 'Original ambition'}))
    finish = {'delivery_profile': 'cad_file', 'intended_user': 'owner',
              'workflow': 'Convert records to CSV', 'artifact_refs': ['source.json']}
    contract = runtime.compile_finish('tenant', 'project', 'campaign', finish)
    assert contract['transform_recipe']['source_artifact']['path'] == 'source.json'
    assert contract['selected_artifact']['format'] == 'csv' and contract['original_goal'] == 'Original ambition'
    lifecycle.files.append({'path': 'ready.csv', 'content': 'name\r\nvalue\r\n'})
    contract = runtime.compile_finish('tenant', 'project', 'campaign', dict(finish, artifact_refs=['ready.csv', 'source.json']))
    assert 'transform_recipe' not in contract and contract['selected_artifact']['path'] == 'ready.csv'


def test_deadline_validated_but_public_priority_refused():
    finish = {'delivery_profile': 'cad_file', 'intended_user': 'owner', 'workflow': 'CSV', 'artifact_refs': []}
    assert runtime.validate_finish(dict(finish, deadline_at='2026-09-08T18:00:00Z'))['deadline_at'].endswith('Z')
    for extra in ({'deadline_at': 'tomorrow'}, {'deadline_at': '2026-09-08T18:00:00'}, {'priority_score': 100}):
        with pytest.raises(ValueError):
            runtime.validate_finish(dict(finish, **extra))
