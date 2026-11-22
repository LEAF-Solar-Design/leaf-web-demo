"""Synthetic consumer tests only: no browser, qualifying grade, or readiness."""

from copy import deepcopy
import json
import shutil
import subprocess

import pytest

from benches import x08_selection as selection
from benches.x08_selection import (
    SelectionCaptureError, collect_selection, geometry_evidence, validate_selection,
)


def evidence(handles=None, kind='click'):
    handles = ['A'] if handles is None else handles
    return {
        'baseline_drawing_key': 'fixture-drawing', 'drawing_key': 'fixture-drawing',
        'baseline_serial': 2,
        'record': {'serial': 3, 'kind': kind, 'additive': False, 'trusted': True,
                   'start_ms': 100, 'event_timestamp': 99.5, 'pointerId': 7,
                   'handler_finished_ms': 110, 'highlight_committed_ms': 105,
                   'render_ms': 120, 'after_frame_ms': 130,
                   'requested_handles': list(handles), 'selected_handles': list(handles),
                   'highlight_geometry_count': 4},
        'pointer': {'trusted': True, 'start_ms': 100, 'event_timestamp': 99.5, 'pointerId': 7},
        'visible_history': ['visible', 'visible'], 'event_timing_supported': True,
        'paintability': {'baseline': True, 'down': True, 'after': True},
        'event_entries': [{'name': 'pointerdown', 'startTime': 99.5,
                           'duration': 40, 'interactionId': 0},
                          {'name': 'click', 'startTime': 115, 'duration': 24,
                           'interactionId': 8}],
        'cleanup': {'recorder_removed': True, 'observer_disconnected': True,
                    'element_disposed': True},
    }


def snapshot():
    return {'drawingKey': 'fixture-drawing', 'handles': [
        {'handle': 'A', 'geometryTriangles': 2, 'geometrySegments': 0, 'markerCount': 1},
        {'handle': 'B', 'geometryTriangles': 0, 'geometrySegments': 3, 'markerCount': 0},
        {'handle': 'C', 'geometryTriangles': 0, 'geometrySegments': 0, 'markerCount': 2},
        {'handle': 'other', 'geometryTriangles': 50, 'geometrySegments': 2, 'markerCount': 6}],
        'omitted': {'blocks': 0, 'activeIntake': 0}, 'selections': []}


def test_geometry_actual_schema_attribution_and_no_mutation():
    raw = snapshot()
    before = deepcopy(raw)
    expected = ['D', 'C', 'B', 'A']
    result = geometry_evidence(raw, expected)
    assert result == {'geometry': {'rendered_module_count': 2, 'marker_module_count': 2,
                                    'omitted_module_count': 2},
                      'expected_handles': ['A', 'B', 'C', 'D'],
                      'rendered_handles': ['A', 'B'], 'missing_handles': ['C', 'D'],
                      'marker_handles': ['A', 'C']}
    assert raw == before
    assert expected == ['D', 'C', 'B', 'A']


@pytest.mark.parametrize('key', ['blocks', 'activeIntake'])
@pytest.mark.parametrize('value', [1, -1, True, 0.0, None])
def test_global_omissions_refuse(key, value):
    raw = snapshot()
    raw['omitted'][key] = value
    with pytest.raises(SelectionCaptureError):
        geometry_evidence(raw, ['A'])


@pytest.mark.parametrize('key', ['geometryTriangles', 'geometrySegments', 'markerCount'])
@pytest.mark.parametrize('value', [True, -1, 1.0, None, '1'])
def test_invalid_counters_even_on_extra_rows(key, value):
    raw = snapshot()
    raw['handles'][-1][key] = value
    with pytest.raises(SelectionCaptureError):
        geometry_evidence(raw, ['A'])


@pytest.mark.parametrize('expected', [[], ['A', 'A'], [''], [True], 'A'])
def test_invalid_expected_handles(expected):
    with pytest.raises(SelectionCaptureError):
        geometry_evidence(snapshot(), expected)


def test_bad_geometry_rows_and_drawing():
    for change in ('duplicate', 'empty', 'shape', 'drawing'):
        raw = snapshot()
        if change == 'duplicate':
            raw['handles'].append(deepcopy(raw['handles'][0]))
        elif change == 'empty':
            raw['handles'][0]['handle'] = ' '
        elif change == 'shape':
            raw['handles'] = None
        else:
            raw['drawingKey'] = ''
        with pytest.raises(SelectionCaptureError):
            geometry_evidence(raw, ['A'])


def test_synchronous_commit_and_overlapping_event_durations():
    raw = evidence()
    before = deepcopy(raw)
    result = validate_selection(raw, expected_handles=['A'], kind='click')
    assert result['latency_ms'] == 40
    assert result['oracle_independent'] is False
    result['event_entries'][0]['duration'] = 999
    result['selected_handles'].append('X')
    assert raw == before


def test_marquee_independent_set_and_kind():
    handles = [f'fixture-{i}' for i in range(500)]
    raw = evidence(handles, 'marquee')
    raw['record']['selected_handles'].reverse()
    result = validate_selection(raw, expected_handles=handles, kind='marquee')
    assert result['oracle_independent'] is True
    assert set(result['expected_handles']) == set(handles)
    raw['record']['kind'] = 'click'
    with pytest.raises(SelectionCaptureError):
        validate_selection(raw, expected_handles=handles, kind='marquee')


@pytest.mark.parametrize('section,key,value', [
    ('record', 'serial', 2), ('record', 'serial', True),
    ('record', 'trusted', False), ('pointer', 'trusted', False),
    ('record', 'highlight_geometry_count', 0), ('record', 'highlight_geometry_count', True),
    ('record', 'kind', 'marquee'), ('record', 'additive', True),
    ('record', 'start_ms', 102), ('record', 'event_timestamp', 102),
    ('record', 'pointerId', 8), ('record', 'pointerId', True),
    ('record', 'handler_finished_ms', 121), ('record', 'highlight_committed_ms', 121),
    ('record', 'highlight_committed_ms', 99), ('record', 'after_frame_ms', 119),
    ('record', 'render_ms', float('inf')), ('record', 'start_ms', float('nan')),
    ('record', 'after_frame_ms', -1), ('record', 'render_ms', True),
    ('record', 'selected_handles', ['X']), ('record', 'requested_handles', ['X']),
    ('record', 'selected_handles', ['A', 'A']), ('cleanup', 'element_disposed', False),
    ('cleanup', 'observer_disconnected', False), ('cleanup', 'recorder_removed', False),
])
def test_bad_selection_fields(section, key, value):
    raw = evidence()
    raw[section][key] = value
    with pytest.raises(SelectionCaptureError):
        validate_selection(raw, expected_handles=['A'], kind='click')


@pytest.mark.parametrize('key,value', [
    ('drawing_key', 'different'), ('baseline_drawing_key', ''), ('baseline_serial', -1),
    ('visible_history', []), ('visible_history', ['visible', 'hidden', 'visible']),
    ('event_timing_supported', False), ('event_entries', []), ('event_entries', None),
    ('paintability', None), ('paintability', {}),
])
def test_missing_or_stale_evidence_refuses(key, value):
    raw = evidence()
    raw[key] = value
    with pytest.raises(SelectionCaptureError):
        validate_selection(raw, expected_handles=['A'], kind='click')


@pytest.mark.parametrize('count', [1, 489, 511])
def test_marquee_requires_frozen_size_range(count):
    handles = [f'fixture-{i}' for i in range(count)]
    with pytest.raises(SelectionCaptureError):
        validate_selection(evidence(handles, 'marquee'), expected_handles=handles, kind='marquee')


def test_zero_start_and_tolerance_are_allowed_but_zero_interval_is_not():
    raw = evidence()
    for key in ('start_ms', 'event_timestamp'):
        raw['pointer'][key] = raw['record'][key] = 0
    raw['event_entries'][0]['startTime'] = 0
    assert validate_selection(raw, expected_handles=['A'], kind='click')['latency_ms'] == 130
    raw = evidence()
    raw['pointer']['start_ms'] = 101
    assert validate_selection(raw, expected_handles=['A'], kind='click')['latency_ms'] == 40
    for key in ('handler_finished_ms', 'highlight_committed_ms', 'render_ms', 'after_frame_ms'):
        raw['record'][key] = 100
    with pytest.raises(SelectionCaptureError):
        validate_selection(raw, expected_handles=['A'], kind='click')


@pytest.mark.parametrize('key,value', [
    ('name', 'keydown'), ('startTime', 98), ('startTime', 131),
    ('startTime', -1), ('duration', 0), ('duration', float('nan')),
    ('interactionId', True), ('interactionId', -1),
])
def test_unrelated_or_malformed_event_is_not_filtered(key, value):
    raw = evidence()
    raw['event_entries'][1][key] = value
    with pytest.raises(SelectionCaptureError):
        validate_selection(raw, expected_handles=['A'], kind='click')


@pytest.mark.parametrize('phase', ['baseline', 'down', 'after'])
@pytest.mark.parametrize('value', [False, None, 1, 'true'])
def test_paintability_requires_actual_true_at_every_phase(phase, value):
    raw = evidence()
    raw['paintability'][phase] = value
    with pytest.raises(SelectionCaptureError, match='paintable'):
        validate_selection(raw, expected_handles=['A'], kind='click')


class FakeCanvas:
    def __init__(self, setup_error=False, dispose_error=False):
        self.setup_error = setup_error
        self.dispose_error = dispose_error
        self.disposed = False
        self.installed = False

    def element_handle(self):
        return self

    def evaluate(self, script, token):
        assert script == selection._INSTALL
        self.installed = True
        if self.setup_error:
            raise RuntimeError('setup unavailable')
        return True

    def dispose(self):
        self.disposed = True
        if self.dispose_error:
            raise RuntimeError('dispose unavailable')


class FakePage:
    def __init__(self, timeout=False, cleanup_error=False):
        self.timeout = timeout
        self.cleanup_error = cleanup_error
        self.cleaned = False
        self.raw = evidence()

    def wait_for_function(self, script, *, arg, timeout):
        assert script == selection._READY
        if self.timeout:
            raise RuntimeError('timeout')

    def evaluate(self, script, token):
        if script == selection._CLEANUP:
            self.cleaned = True
            if self.cleanup_error:
                raise RuntimeError('cleanup unavailable')
            return {'recorder_removed': True, 'observer_disconnected': True}
        assert script == selection._SNAPSHOT
        return self.raw


def test_collect_calls_action_once_and_preserves_evidence():
    page, canvas, calls = FakePage(), FakeCanvas(), []
    result = collect_selection(page, canvas, lambda: calls.append('UI action'),
                               expected_handles=['A'], kind='click')
    assert calls == ['UI action']
    assert page.cleaned and canvas.disposed
    assert result['raw_evidence']['cleanup']['element_disposed'] is True
    assert result['latency_ms'] == 40
    assert result['raw_evidence'] is not page.raw


def test_collector_binds_expected_before_action():
    page, canvas, expected = FakePage(), FakeCanvas(), ['A']
    result = collect_selection(page, canvas, lambda: expected.append('changed'),
                               expected_handles=expected, kind='click')
    assert result['expected_handles'] == ['A']


def test_missing_cleanup_result_never_returns_evidence():
    class MissingCleanup(FakePage):
        def evaluate(self, script, token):
            if script == selection._CLEANUP:
                return None
            return super().evaluate(script, token)

    canvas = FakeCanvas()
    with pytest.raises(SelectionCaptureError, match='cleanup'):
        collect_selection(MissingCleanup(), canvas, lambda: None,
                          expected_handles=['A'], kind='click')
    assert canvas.disposed


def test_direct_element_handle_is_rejected_without_disposal_or_action():
    class DirectHandle:
        disposed = False

        def dispose(self):
            self.disposed = True

    handle, calls, page = DirectHandle(), [], FakePage()
    with pytest.raises(SelectionCaptureError, match='Locator'):
        collect_selection(page, handle, lambda: calls.append(1),
                          expected_handles=['A'], kind='click')
    assert not handle.disposed and not calls and not page.cleaned


def test_setup_failure_preserves_original_cause_when_no_observer_created():
    page, canvas = FakePage(), FakeCanvas(setup_error=True)
    with pytest.raises(SelectionCaptureError) as error:
        collect_selection(page, canvas, lambda: None, expected_handles=['A'], kind='click')
    assert str(error.value.__cause__) == 'setup unavailable'
    assert page.cleaned and canvas.disposed


@pytest.mark.parametrize('failure', ['timeout', 'action', 'setup', 'cleanup', 'dispose'])
def test_failure_always_attempts_cleanup_and_disposal(failure):
    page = FakePage(timeout=failure == 'timeout', cleanup_error=failure == 'cleanup')
    canvas = FakeCanvas(setup_error=failure == 'setup', dispose_error=failure == 'dispose')
    calls = []

    def action():
        calls.append(1)
        if failure == 'action':
            raise RuntimeError('UI action failed')

    with pytest.raises(SelectionCaptureError):
        collect_selection(page, canvas, action, expected_handles=['A'], kind='click')
    assert page.cleaned and canvas.disposed
    assert len(calls) == (0 if failure == 'setup' else 1)


def test_js_contract_scoped_observation_and_cleanup():
    # String inspection only; these tests do not establish browser behavior.
    scripts = '\n'.join((selection._INSTALL, selection._READY,
                         selection._SNAPSHOT, selection._CLEANUP))
    for forbidden in ('dispatchEvent', 'fetch(', 'performance.now =', '.close(', 'project('):
        assert forbidden not in scripts
    for required in ("canvas.closest('.viewer-canvas')", 's.api.snapshot()', 'e.isTrusted',
                     "buffered: false, durationThreshold: 16", 'takeRecords()',
                     's.downs > 1', 's.downs !== 1', 's.baseline_serial',
                     's.po.disconnect()', "removeEventListener('visibilitychange'",
                     'delete window.__xbenchX08Selection'):
        assert required in scripts


def test_javascript_with_synthetic_parent_producer_and_observer_boundaries():
    # Optional local JS runtime only. This models DOM/observer behavior, not a
    # browser render or real Event Timing, and supplies no qualifying evidence.
    node = shutil.which('node')
    if node is None:
        pytest.skip('Local JavaScript runtime unavailable')
    scripts = json.dumps([selection._INSTALL, selection._READY,
                          selection._SNAPSHOT, selection._CLEANUP])
    harness = r"""
const assert = require('node:assert/strict');
const [install, ready, snapshot, cleanupRecorder] = scripts.map(source => eval('(' + source + ')'));
const cleanup = (token, installed = true) => cleanupRecorder({token, installed});
let canvas, host, ancestor, api, scene, observer;
function setup() {
    global.window = {innerWidth: 800, innerHeight: 600};
    global.document = {visibilityState: 'visible', addEventListener() {}, removeEventListener() {}};
    global.performance = {now: () => 100};
    global.getComputedStyle = el => el.style;
    const style = () => ({display: 'block', visibility: 'visible', opacity: '1'});
    ancestor = {style: style(), parentElement: null};
    host = {style: style(), parentElement: ancestor};
    canvas = {style: style(), parentElement: host, isConnected: true,
        closest: selector => { assert.equal(selector, '.viewer-canvas'); return host; },
        getBoundingClientRect: () => ({left: 10, top: 10, right: 110, bottom: 110, width: 100, height: 100}),
        addEventListener() {}, removeEventListener() {}, contains: () => false};
    scene = {drawingKey: 'drawing', selections: []};
    api = {snapshot: () => scene};
    host.__leafViewerObservation = api;
    global.PerformanceObserver = class {
        static supportedEntryTypes = ['event'];
        constructor(callback) { this.callback = callback; this.pending = []; observer = this; }
        observe(options) { assert.deepEqual(options, {type: 'event', buffered: false, durationThreshold: 16}); }
        takeRecords() { const entries = this.pending; this.pending = []; return entries; }
        disconnect() { this.disconnected = true; }
    };
}
function down() {
    window.__xbenchX08Selection.down({isTrusted: true, timeStamp: 100, pointerId: 7});
    scene.selections.push({serial: 1, pointerId: 7, event_timestamp: 100, after_frame_ms: 130});
}
setup();
assert.equal(canvas.__leafViewerObservation, undefined);
assert.equal(install(canvas, 'ours'), true);
down();
assert.equal(ready('ours'), false); // Real selection alone is insufficient.
observer.pending.push({name: 'click', startTime: 110, duration: 24, interactionId: 1, target: canvas});
assert.equal(ready('ours'), true); // Drains actual takeRecords on a later poll.
assert.equal(snapshot('ours').event_entries.length, 1);
assert.deepEqual(snapshot('ours').paintability, {baseline: true, down: true, after: true});
const incumbent = window.__xbenchX08Selection;
assert.throws(() => install(canvas, 'other'), /already active/);
cleanup('other', false);
assert.equal(window.__xbenchX08Selection, incumbent);
assert.notEqual(observer.disconnected, true);
assert.deepEqual(cleanup('ours'), {recorder_removed: true, observer_disconnected: true});
assert.equal(observer.disconnected, true);
assert.equal(window.__xbenchX08Selection, undefined);
assert.equal(cleanup('ours'), null); // Disappeared after installation cannot certify cleanup.

for (const replacement of [null, {snapshot: () => scene}]) {
    setup(); install(canvas, 'ours'); down();
    host.__leafViewerObservation = replacement;
    assert.throws(() => ready('ours'), /lost or replaced/);
    assert.throws(() => snapshot('ours'), /lost or replaced/);
    cleanup('ours');
}
for (const change of [
    () => ancestor.style.opacity = '0',
    () => ancestor.style.display = 'none',
    () => ancestor.style.visibility = 'hidden',
    () => canvas.isConnected = false,
    () => document.visibilityState = 'hidden',
    () => canvas.getBoundingClientRect = () => ({left: 900, top: 10, right: 1000, bottom: 110, width: 100, height: 100}),
    () => canvas.getBoundingClientRect = () => ({left: 10, top: 10, right: 10, bottom: 10, width: 0, height: 0})
]) {
    setup(); change();
    assert.throws(() => install(canvas, 'ours'), /not paintable/);
    assert.deepEqual(cleanup('ours'), {recorder_removed: true, observer_disconnected: true});
    setup(); install(canvas, 'ours'); change(); down();
    const raw = snapshot('ours');
    assert.equal(raw.paintability.down, false);
    assert.equal(raw.paintability.after, false);
    cleanup('ours');
}
setup(); install(canvas, 'ours'); down();
window.__xbenchX08Selection.down({isTrusted: true, timeStamp: 101, pointerId: 7});
assert.throws(() => ready('ours'), /Multiple/);
assert.throws(() => snapshot('ours'), /Invalid recorder/);
cleanup('ours');
setup(); delete host.__leafViewerObservation;
assert.throws(() => install(canvas, 'ours'), /API unavailable/);
assert.deepEqual(cleanup('ours'), {recorder_removed: true, observer_disconnected: true});
setup(); global.PerformanceObserver = undefined;
assert.throws(() => install(canvas, 'ours'), /Event Timing unavailable/);
assert.deepEqual(cleanup('ours'), {recorder_removed: true, observer_disconnected: true});
setup();
PerformanceObserver.prototype.observe = () => { throw Error('observe failed'); };
assert.throws(() => install(canvas, 'ours'), /observe failed/);
assert.deepEqual(cleanup('ours'), {recorder_removed: true, observer_disconnected: true});
assert.equal(observer.disconnected, true);
setup(); install(canvas, 'ours'); down();
scene.drawingKey = 'replacement-drawing';
assert.throws(() => ready('ours'), /Drawing changed/);
assert.throws(() => snapshot('ours'), /Drawing changed/);
cleanup('ours');
"""
    result = subprocess.run([node], input='const scripts = ' + scripts + ';\n' + harness,
                            text=True, capture_output=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
