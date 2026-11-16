"""Synthetic offline orchestration doubles; these never establish live proof."""

from copy import deepcopy
import importlib.util
import inspect
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import UUID

import pytest

from benches import x08
from benches.x08_ack import validate_ack
from benches.x08_selection import validate_selection
from xbench_core import BenchContext, Step


SHA = 'a' * 40
HANDLES = ['module-' + str(index) for index in range(500)]


class Page:
    def __init__(self):
        self.listeners = {'request': [], 'response': []}
        self.unrelated = lambda event: None
        self.listeners['response'].append(self.unrelated)
        self.timeouts = []

    def on(self, event, callback):
        self.listeners[event].append(callback)

    def remove_listener(self, event, callback):
        self.listeners[event].remove(callback)

    def emit(self, event, value):
        for callback in list(self.listeners[event]):
            callback(value)

    def set_default_timeout(self, value):
        self.timeouts.append(value)


class Runtime:
    def __init__(self):
        self.page, self.canvas = Page(), object()
        self.now, self.serial, self.ack_serial = 0, 0, 0
        self.closed, self.health_calls = 0, 0
        self.actions = []
        self.outputs = []
        self.unrelated_resource = {'closed': False}
        self.before, self.after = SHA, SHA
        self.fail = None
        self.selection_mutation = lambda raw: None
        self.frame_mutation = lambda raw: None
        self.ack_mode = None
        self.cleanup = dict(recorder_removed=True, element_disposed=True, mouse_released=True)

    def clock(self):
        return self.now

    def read_health(self):
        self.health_calls += 1
        value = self.before if self.health_calls == 1 else self.after
        if isinstance(value, BaseException):
            raise value
        return value

    def geometry_snapshot(self):
        self.trigger('geometry')
        return dict(drawingKey='synthetic-drawing', omitted=dict(blocks=0, activeIntake=0),
                    handles=[dict(handle=h, geometryTriangles=2, geometrySegments=0,
                                  markerCount=0) for h in HANDLES])

    def reference(self):
        self.trigger('reference')
        return dict(hostname='EVANS-DEKSTOP', os='Windows 11', cpu='synthetic-cpu',
                    gpu=['synthetic-gpu'], free_ram_bytes=1024, chromium_version='synthetic',
                    headed=True, viewport=dict(width=1920, height=1080), device_scale_factor=1)

    def trigger(self, name):
        if self.fail == name:
            raise RuntimeError('secret-bearing text must never escape')

    def single_action(self, index):
        return lambda: self.actions.append(('single', index))

    def window_action(self, index):
        return lambda: self.actions.append(('window', index))

    def ack_control(self, index):
        return index

    def frame_cleanup(self):
        return self.cleanup

    def close(self):
        self.closed += 1
        self.trigger('close')

    def selection(self, page, canvas, action, *, expected_handles, kind, timeout_ms):
        assert page is self.page and canvas is self.canvas and 0 < timeout_ms <= 30000
        self.trigger('selection')
        action()
        self.serial += 1
        start = self.serial * 100
        raw = dict(
            baseline_drawing_key='synthetic-drawing', drawing_key='synthetic-drawing',
            baseline_serial=self.serial - 1,
            pointer=dict(trusted=True, start_ms=start, event_timestamp=start, pointerId=7),
            record=dict(serial=self.serial, kind=kind, additive=False, trusted=True,
                        start_ms=start, event_timestamp=start, pointerId=7,
                        handler_finished_ms=start + 5, highlight_committed_ms=start + 6,
                        render_ms=start + 10, after_frame_ms=start + 20,
                        selected_handles=list(expected_handles),
                        requested_handles=list(expected_handles), highlight_geometry_count=2),
            visible_history=['visible'], event_timing_supported=True,
            paintability=dict(baseline=True, down=True, after=True),
            event_entries=[dict(name='click', startTime=start + 1, duration=20, interactionId=1)],
            cleanup=dict(recorder_removed=True, observer_disconnected=True, element_disposed=True))
        row = validate_selection(raw, expected_handles=expected_handles, kind=kind)
        self.selection_mutation(raw)
        row['raw_evidence'] = raw
        self.outputs.append((row, deepcopy(row)))
        return row

    def ack(self, page, control, *, timeout_ms):
        assert page is self.page and 0 < timeout_ms <= 30000
        self.trigger('ack')
        self.ack_serial += 1
        job = str(UUID(int=self.ack_serial))
        start = self.ack_serial * 100
        if self.ack_mode == 'old':
            start = 100
        if self.ack_mode == 'job-reuse':
            job = str(UUID(int=1))
        body = dict(status='submitted', job_id=job)
        request = SimpleNamespace(method='POST', url='https://synthetic.invalid/api/run')
        response = SimpleNamespace(request=request, status=202, json=lambda: body)
        if self.ack_mode != 'missing':
            page.emit('request', request)
        if self.ack_mode == 'unrelated':
            response.request = SimpleNamespace(method='POST', url=request.url)
        page.emit('response', response)
        if self.ack_mode == 'ambiguous':
            page.emit('response', response)
        raw = dict(pointer_ms=start, response_end_ms=start + 5, paint_ms=start + 20,
                   resource_matches=1, queued_painted=True, event_durations_ms=[], event_entries=[])
        row = validate_ack(raw, response_status=202, response_body=body)
        self.outputs.append((row, deepcopy(row)))
        return row

    def frames(self, page, canvas, window, *, duration_ms):
        assert duration_ms == 10000
        self.trigger('frames')
        window()
        kinds = ['pan'] * 4 + ['zoom_in'] * 10 + ['zoom_out'] * 10 + ['window_select']
        starts = [0, 800, 1600, 2400] + list(range(3200, 8200, 250)) + [8500]
        raw = dict(complete=True, visible=True, visibility=['visible'], error=None,
                   start_ms=123, finish_ms=10123, duration_ms=10000,
                   frame_deltas_ms=[16] * 600,
                   sequence=dict(pan_drags=4, zoom_in_steps=10, zoom_out_steps=10, duration_ms=10000),
                   actions=[dict(kind=k, start_ms=s, finish_ms=s + 10, success=True,
                                 within_recording=True) for k, s in zip(kinds, starts)],
                   raw_events={k: dict(trusted=n, untrusted=0) for k, n in
                               [('pointerdown', 5), ('pointerup', 5), ('pointermove', 5), ('wheel', 20)]})
        self.frame_mutation(raw)
        self.outputs.append((raw, deepcopy(raw)))
        return raw


def collect(runtime, **kwargs):
    options = dict(expected_source_sha=SHA, expected_module_handles=HANDLES,
                   expected_window_handles=HANDLES, clock=runtime.clock,
                   selection_collector=runtime.selection, ack_collector=runtime.ack,
                   frame_collector=runtime.frames)
    options.update(kwargs)
    return x08.orchestrate(runtime, **options)


def verdict(report, step):
    return report['steps'][step - 1]['verdict']


def assert_released(runtime):
    assert runtime.closed == 1
    assert runtime.health_calls == 2
    assert runtime.page.listeners == {'request': [], 'response': [runtime.page.unrelated]}
    assert runtime.unrelated_resource == {'closed': False}


def test_full_synthetic_cold_warm_collection_and_no_mutation():
    runtime = Runtime()
    report = collect(runtime)
    assert report['schema'] == 'leaf.xbench.x08.collection.v1'
    assert UUID(report['run_id'])
    assert report['qualified_live_grade'] is False
    assert report['errors'] == []
    assert [row['verdict'] for row in report['steps']] == ['PASS'] * 6
    assert report['cold_run_discarded'] is True
    for phase in ('cold', 'warm'):
        assert [len(report[phase][key]) for key in ('single', 'window', 'ack')] == [20, 5, 10]
        assert len(report[phase]['frame']['frame_deltas_ms']) == 600
        assert len(report[phase]['frame_window']) == 1
    assert len(report['samples']['single_select_ms']) == 20
    assert len(report['samples']['window_select']) == 5
    assert len(report['samples']['job_ack']) == 10
    assert len(report['samples']['frame_deltas_ms']) == 600
    assert set(r['job_id'] for r in report['cold']['ack']).isdisjoint(
        r['job_id'] for r in report['samples']['job_ack'])
    assert all(original == saved for original, saved in runtime.outputs)
    assert_released(runtime)


@pytest.mark.parametrize('mutate', [
    lambda raw: raw['record'].update(trusted=False),
    lambda raw: raw['record'].update(serial=0),
    lambda raw: raw['record'].update(selected_handles=['wrong']),
    lambda raw: raw.update(drawing_key='other'),
    lambda raw: raw['pointer'].update(event_timestamp=0),
    lambda raw: raw['record'].update(highlight_geometry_count=0),
])
def test_selection_mismatch_stale_untrusted(mutate):
    runtime = Runtime()
    runtime.selection_mutation = mutate
    report = collect(runtime)
    assert verdict(report, 2) == verdict(report, 3) == verdict(report, 5) == 'UNREADABLE'
    assert report['cold']['single']  # rejected raw evidence survives
    assert not report['samples']['single_select_ms']
    assert_released(runtime)


def test_reused_selection_across_cold_and_warm():
    runtime = Runtime()
    original = runtime.selection
    first = []

    def reused(*args, **kwargs):
        row = original(*args, **kwargs)
        if not first:
            first.append(deepcopy(row))
        if runtime.serial == 27:
            return deepcopy(first[0])
        return row

    report = collect(runtime, selection_collector=reused)
    assert report['cold_run_discarded'] is True
    assert verdict(report, 2) == 'UNREADABLE'
    assert len(report['warm']['single']) == 1
    assert_released(runtime)


@pytest.mark.parametrize('mode', ['old', 'job-reuse', 'unrelated', 'missing', 'ambiguous'])
def test_ack_identity_and_observation_fail_closed(mode):
    runtime = Runtime()
    runtime.ack_mode = mode
    report = collect(runtime)
    assert verdict(report, 4) == 'UNREADABLE'
    assert report['cold_run_discarded'] is False
    assert report['cold']['single'] and report['warm']['single']
    assert_released(runtime)


def test_unsupported_page_observation_is_unreadable():
    runtime = Runtime()
    runtime.page.on = None
    report = collect(runtime)
    assert verdict(report, 4) == 'UNREADABLE'
    assert runtime.ack_serial == 0
    assert_released(runtime)


@pytest.mark.parametrize('mutate', [
    lambda raw: raw['actions'][0].update(kind='zoom_in'),
    lambda raw: raw['actions'][-1].update(finish_ms=10001),
    lambda raw: raw['actions'].pop(),
    lambda raw: raw.update(complete=False),
    lambda raw: raw['frame_deltas_ms'].pop(),
])
def test_frame_order_late_incomplete_refusal(mutate):
    runtime = Runtime()
    runtime.frame_mutation = mutate
    report = collect(runtime)
    assert verdict(report, 5) == 'UNREADABLE'
    assert 'frame_deltas_ms' not in report['samples']
    assert report['cold']['frame'] is not None
    assert_released(runtime)


def test_explicit_frame_cleanup_required():
    runtime = Runtime()
    runtime.cleanup = {}
    report = collect(runtime)
    assert verdict(report, 5) == 'UNREADABLE'
    assert report['warm']['frame_cleanup'] == {}
    assert_released(runtime)


@pytest.mark.parametrize('before', ['b' * 40, RuntimeError('secret')])
def test_source_before_blocks_all_collectors(before):
    runtime = Runtime()
    runtime.before = before
    report = collect(runtime)
    assert runtime.outputs == [] and runtime.actions == []
    assert all(row['verdict'] == 'UNREADABLE' for row in report['steps'])
    assert report['source_after'] == SHA
    assert_released(runtime)


@pytest.mark.parametrize('after', ['b' * 40, None, RuntimeError('secret')])
def test_source_after_blocks_every_grade_preserves_evidence(after):
    runtime = Runtime()
    runtime.after = after
    report = collect(runtime)
    assert all(row['verdict'] == 'UNREADABLE' for row in report['steps'])
    assert len(report['samples']['job_ack']) == 10
    assert report['source_before'] == SHA
    assert_released(runtime)


@pytest.mark.parametrize('phase', ['geometry', 'reference', 'selection', 'ack', 'frames', 'close'])
def test_operational_failures_close_once_and_preserve_predecessors(phase):
    runtime = Runtime()
    runtime.fail = phase
    report = collect(runtime)
    assert report['errors']
    assert 'secret' not in json.dumps(report)
    if phase in ('ack', 'frames', 'close'):
        assert len(report['warm']['valid_single']) == 20
    if phase == 'close':
        assert all(row['verdict'] == 'UNREADABLE' for row in report['steps'])
        assert report['cleanup']['closed'] is False
    assert_released(runtime)


@pytest.mark.parametrize('exception', [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize('target', ['geometry_snapshot', 'reference', 'selection', 'ack', 'frames'])
def test_cancellation_partial_report_and_owned_close(exception, target):
    runtime = Runtime()

    def cancel(*args, **kwargs):
        raise exception()

    setattr(runtime, target, cancel)
    with pytest.raises(exception) as caught:
        collect(runtime)
    report = caught.value.x08_partial_report
    assert report['source_after'] == SHA
    assert report['cleanup']['closed'] is True
    if target in ('ack', 'frames'):
        assert len(report['cold']['single']) == 20
    assert_released(runtime)


@pytest.mark.parametrize('budget', [True, 0, -1, float('nan'), float('inf')])
@pytest.mark.parametrize('key', ['total_timeout_s', 'phase_timeout_s'])
def test_invalid_deadlines_fail_closed(budget, key):
    runtime = Runtime()
    report = collect(runtime, **{key: budget})
    assert runtime.outputs == []
    assert all(row['verdict'] == 'UNREADABLE' for row in report['steps'])
    assert runtime.closed == 1


def test_fake_clock_stall_retains_raw_and_bounded_wait():
    runtime = Runtime()
    original = runtime.selection

    def stalled(*args, **kwargs):
        row = original(*args, **kwargs)
        runtime.now += 31
        return row

    report = collect(runtime, selection_collector=stalled)
    assert verdict(report, 2) == 'UNREADABLE'
    assert len(report['cold']['single']) == 1
    assert not report['cold']['valid_single']
    assert runtime.page.timeouts and all(0 < v <= 30000 for v in runtime.page.timeouts)
    assert_released(runtime)


def test_total_deadline_prevents_later_collectors():
    runtime = Runtime()
    original = runtime.reference

    def stalled():
        result = original()
        runtime.now = 241
        return result

    runtime.reference = stalled
    report = collect(runtime)
    assert runtime.outputs == []
    assert report['samples']['reference']['cpu'] == 'synthetic-cpu'
    assert all(row['verdict'] == 'UNREADABLE' for row in report['steps'])
    assert_released(runtime)


def test_actual_collectors_are_public_defaults():
    parameters = inspect.signature(x08.orchestrate).parameters
    assert parameters['selection_collector'].default is x08.collect_selection
    assert parameters['ack_collector'].default is x08.capture_job_ack
    assert parameters['frame_collector'].default is x08.collect_frame_sequence


def test_missing_raw_selection_cannot_be_admitted_by_injection():
    runtime = Runtime()
    original = runtime.selection

    def no_raw(*args, **kwargs):
        row = original(*args, **kwargs)
        return {key: value for key, value in row.items() if key != 'raw_evidence'}

    report = collect(runtime, selection_collector=no_raw)
    assert verdict(report, 2) == 'UNREADABLE'
    assert report['warm']['single']
    assert not report['samples']['single_select_ms']
    assert_released(runtime)


def test_warm_cancellation_preserves_completed_predecessor_samples():
    runtime = Runtime()
    original = runtime.ack

    def cancel(*args, **kwargs):
        if runtime.ack_serial == 10:
            raise KeyboardInterrupt()
        return original(*args, **kwargs)

    with pytest.raises(KeyboardInterrupt) as caught:
        collect(runtime, ack_collector=cancel)
    report = caught.value.x08_partial_report
    assert report['cold_run_discarded'] is True
    assert len(report['samples']['single_select_ms']) == 20
    assert len(report['samples']['window_select']) == 5
    assert report['samples']['job_ack'] == []
    assert_released(runtime)


def test_cleanup_failure_cannot_replace_original_cancellation():
    runtime = Runtime()

    def cancel(*args, **kwargs):
        raise KeyboardInterrupt()

    def failed_cleanup():
        raise RuntimeError('secret')

    runtime.frame_cleanup = failed_cleanup
    with pytest.raises(KeyboardInterrupt) as caught:
        collect(runtime, frame_collector=cancel)
    assert caught.value.x08_partial_report['errors']
    assert_released(runtime)


@pytest.mark.parametrize('callback', ['read_health', 'close'])
def test_final_callbacks_respect_deadlines_and_close_still_occurs(callback):
    runtime = Runtime()
    original = getattr(runtime, callback)

    def stalled():
        result = original()
        if callback == 'close' or runtime.health_calls == 2:
            runtime.now += 31
        return result

    setattr(runtime, callback, stalled)
    report = collect(runtime)
    assert all(row['verdict'] == 'UNREADABLE' for row in report['steps'])
    assert report['cleanup']['closed'] is True
    assert_released(runtime)


def test_default_run_refuses_saved_or_injected_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(x08, 'report_destination', lambda ctx: tmp_path / 'samples.json')
    runtime = Runtime()
    ctx = SimpleNamespace(out_dir=tmp_path, source_sha=SHA,
                          manifest={'samples': 'saved.json', 'runtime': runtime})
    steps = x08.run(ctx)
    assert [row.verdict for row in steps] == ['UNREADABLE'] * 6
    report = json.loads((tmp_path / 'samples.json').read_text())
    assert report['qualified_live_grade'] is False
    assert report['source_sha'] == SHA
    assert 'saved samples cannot qualify' in report['limitation']
    assert report['errors'][0]['phase'] == 'adapter'
    assert list(tmp_path.iterdir()) == [tmp_path / 'samples.json']
    assert runtime.closed == 0 and runtime.health_calls == 0


def test_shared_grade_standalone_loader_keeps_default_run_unreadable(tmp_path, monkeypatch):
    module_path = Path(x08.__file__)
    spec = importlib.util.spec_from_file_location('xbench_bench_x08', module_path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, 'xbench_bench_x08', module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'report_destination', lambda ctx: tmp_path / 'samples.json')

    ctx = BenchContext('X08', {}, 'https://synthetic.invalid', tmp_path, SHA, [])
    steps = module.run(ctx)
    assert all(isinstance(row, Step) for row in steps)
    assert [row.n for row in steps] == list(range(1, 7))
    assert [row.verdict for row in steps] == ['UNREADABLE'] * 6
    report = json.loads((tmp_path / 'samples.json').read_text())
    assert report['schema'] == 'leaf.xbench.x08.collection.v1'
    assert report['source_sha'] == SHA
    assert report['qualified_live_grade'] is False
    assert 'saved samples cannot qualify' in report['limitation']
    assert [row['verdict'] for row in report['steps']] == ['UNREADABLE'] * 6
    assert list(tmp_path.iterdir()) == [tmp_path / 'samples.json']


def test_readable_geometry_failure_skips_timings_keeps_reference():
    runtime = Runtime()
    original = runtime.geometry_snapshot

    def incomplete():
        snapshot = original()
        snapshot['handles'].pop()
        return snapshot

    runtime.geometry_snapshot = incomplete
    report = collect(runtime, stop_on_geometry_failure=True)
    assert [r['verdict'] for r in report['steps']] == [
        'FAIL', 'UNREADABLE', 'UNREADABLE', 'UNREADABLE', 'UNREADABLE', 'PASS']
    assert report['blocker'] == 'X08_MODULE_GEOMETRY_INCOMPLETE'
    assert runtime.actions == [] and runtime.outputs == []
    assert_released(runtime)


def test_geometry_failure_source_drift_overrides_product_failure():
    runtime = Runtime()
    runtime.geometry_snapshot = lambda: dict(
        drawingKey='synthetic', handles=[], omitted=dict(blocks=0, activeIntake=0))
    runtime.after = 'b' * 40
    report = collect(runtime, stop_on_geometry_failure=True)
    assert [r['verdict'] for r in report['steps']] == ['UNREADABLE'] * 6
    assert_released(runtime)


def test_setup_error_six_steps_only_type_and_no_readiness(tmp_path, monkeypatch):
    monkeypatch.setattr(x08, 'report_destination', lambda ctx: tmp_path / 'samples.json')

    def unavailable(ctx):
        raise ValueError('private fixture path and credential text')

    monkeypatch.setattr(x08, 'live_runtime', unavailable)
    ctx = SimpleNamespace(out_dir=tmp_path, source_sha=SHA)
    assert [r.verdict for r in x08.run(ctx)] == ['UNREADABLE'] * 6
    saved = json.loads((tmp_path / 'samples.json').read_text())
    assert saved['errors'] == [dict(phase='adapter', step=list(range(1, 7)), type='ValueError')]
    assert saved['source_sha'] == SHA and 'private fixture' not in json.dumps(saved)
    assert list(tmp_path.iterdir()) == [tmp_path / 'samples.json']


def test_output_escape_never_writes_or_acquires(tmp_path, monkeypatch):
    def refuse(ctx):
        raise ValueError('outside root')

    monkeypatch.setattr(x08, 'report_destination', refuse)
    monkeypatch.setattr(x08, 'live_runtime', lambda ctx: pytest.fail('must not acquire'))
    assert [r.verdict for r in x08.run(SimpleNamespace(
        source_sha=SHA, out_dir=tmp_path))] == ['UNREADABLE'] * 6
    assert list(tmp_path.iterdir()) == []


def test_run_uses_factory_handles_and_saves_cancellation(tmp_path, monkeypatch):
    monkeypatch.setattr(x08, 'report_destination', lambda ctx: tmp_path / 'samples.json')
    runtime = Runtime()
    runtime.expected_module_handles = HANDLES
    runtime.expected_window_handles = HANDLES
    monkeypatch.setattr(x08, 'live_runtime', lambda ctx: runtime)
    original = x08.orchestrate

    def collect_supported(value, **kwargs):
        assert kwargs['expected_module_handles'] is HANDLES
        assert kwargs['expected_window_handles'] is HANDLES
        assert kwargs['stop_on_geometry_failure'] is True
        return original(value, clock=value.clock, selection_collector=value.selection,
                        ack_collector=value.ack, frame_collector=value.frames, **kwargs)

    monkeypatch.setattr(x08, 'orchestrate', collect_supported)
    ctx = SimpleNamespace(out_dir=tmp_path, source_sha=SHA, manifest={})
    assert [r.verdict for r in x08.run(ctx)] == ['PASS'] * 6
    saved = json.loads((tmp_path / 'samples.json').read_text())
    assert saved['acquisition'] == 'supported_live_runtime'
    assert saved['qualified_live_grade'] is False
    assert_released(runtime)

    runtime = Runtime()
    runtime.expected_module_handles = HANDLES
    runtime.expected_window_handles = HANDLES

    def cancel():
        raise KeyboardInterrupt('private text')

    runtime.geometry_snapshot = cancel
    with pytest.raises(KeyboardInterrupt):
        x08.run(ctx)
    saved = json.loads((tmp_path / 'samples.json').read_text())
    assert 'private text' not in json.dumps(saved)
    assert saved['cleanup']['closed'] is True
    assert [r['verdict'] for r in saved['steps']] == ['UNREADABLE'] * 6
    assert_released(runtime)
