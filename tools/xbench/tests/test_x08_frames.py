"""Fake-clock checks for scheduling and honest X08 collection diagnostics."""

import pytest
import json
from uuid import UUID

from benches.x08_frames import collect_frame_sequence


class Canvas:
    box = {'x': 10, 'y': 20, 'width': 800, 'height': 600}

    def __init__(self):
        self.disposed = False

    def bounding_box(self):
        return self.box

    def element_handle(self):
        return self

    def evaluate(self, expression):
        assert 'getComputedStyle' in expression
        return True

    def dispose(self):
        self.disposed = True


class Mouse:
    def __init__(self, page):
        self.page = page
        self.commands = []
        self.drag_error = False
        self.wheel_delay = 0

    def move(self, x, y, steps=None):
        self.commands.append(('move', self.page.clock, x, y, steps))
        if steps and self.drag_error:
            raise RuntimeError('drag failed')

    def down(self, *, button):
        self.commands.append(('down', self.page.clock, button))

    def up(self, *, button):
        self.commands.append(('up', self.page.clock, button))

    def wheel(self, x, y):
        self.commands.append(('wheel', self.page.clock, x, y))
        self.page.clock += self.wheel_delay


class Page:
    def __init__(self):
        self.clock = 123
        self.mouse = Mouse(self)
        self.frames = [16.2, 40.7, 8.1]
        self.events = {'wheel': {'trusted': 20, 'untrusted': 0}}
        self.waits = []
        self.cleaned = False
        self.visible = True
        self.viewport_size = dict(width=1920, height=1080)
        self.recorder = None
        self.owned_recorder = None

    def evaluate(self, script, arg=None):
        if arg is not None and 'canvas' in arg:
            if self.recorder is not None:
                raise RuntimeError('private recorder occupied')
            assert arg['canvas'] is not None
            assert arg['duration_ms'] == 10000
            assert UUID(arg['token']).version == 4
            self.start = self.clock
            self.recorder = dict(token=arg['token'], installed=self.start)
            self.owned_recorder = self.recorder
            return self.start
        if script == '() => performance.now()':
            return self.clock
        assert 'recorder.stop()' in script
        if (arg['installed'] is not True or self.recorder is None or
                self.recorder is not self.owned_recorder or self.recorder['token'] != arg['token']):
            return None
        self.recorder = None
        self.cleaned = True
        # Model the browser's independent deadline, even during blocking I/O.
        finish = min(self.clock, self.start + 10000)
        return {'frame_deltas_ms': self.frames, 'duration_ms': finish - self.start,
                'start_ms': self.start, 'finish_ms': finish, 'visible': self.visible,
                'visibility': ['visible' if self.visible else 'hidden'],
                'raw_events': self.events}

    def wait_for_timeout(self, delay):
        assert delay >= 0
        self.waits.append(delay)
        self.clock += delay


def test_frozen_sequence_schedule_geometry_and_raw_evidence():
    page, canvas = Page(), Canvas()
    callbacks = []

    def select():
        callbacks.append(page.clock - page.start)
        return {'frame_deltas_ms': [999] * 600, 'selected_handles': ['ignored']}

    result = collect_frame_sequence(page, canvas, select)
    assert result['complete'] is True
    assert result['sequence'] == {'pan_drags': 4, 'zoom_in_steps': 10,
                                  'zoom_out_steps': 10, 'duration_ms': 10000}
    assert callbacks == [8500]
    assert [a['start_ms'] for a in result['actions']] == (
        [0, 800, 1600, 2400] + list(range(3200, 8200, 250)) + [8500])
    commands = page.mouse.commands
    assert [c[2] for c in commands if c[0] == 'down'] == ['right'] * 4
    assert [c[2] for c in commands if c[0] == 'up'] == ['right'] * 4
    assert [c[3] for c in commands if c[0] == 'wheel'] == [-120] * 10 + [120] * 10
    moves = [c for c in commands if c[0] == 'move']
    assert len(moves) == 28
    assert all(10 < c[2] < 810 and 20 < c[3] < 620 for c in moves)
    assert all(c[4] == 8 for c in moves if c[4] is not None)
    assert result['frame_deltas_ms'] is page.frames
    assert result['raw_events'] is page.events
    assert result['duration_ms'] == 10000
    assert page.clock == page.start + 10000
    assert page.cleaned and canvas.disposed


def test_overrun_does_not_count_late_wheel_or_run_later_actions():
    page, canvas = Page(), Canvas()
    page.mouse.wheel_delay = 7000
    result = collect_frame_sequence(page, canvas, lambda: pytest.fail('late callback'))
    assert result['complete'] is False
    assert result['sequence']['zoom_in_steps'] == 0
    assert result['sequence']['zoom_out_steps'] == 0
    assert len(result['actions']) == 5
    assert result['actions'][-1]['finish_ms'] == 10200
    assert result['actions'][-1]['within_recording'] is False
    assert len([c for c in page.mouse.commands if c[0] == 'wheel']) == 1
    assert result['frame_deltas_ms'] == [16.2, 40.7, 8.1]
    assert all(delay > 0 for delay in page.waits)
    assert page.cleaned and canvas.disposed


@pytest.mark.parametrize('failure', ['callback', 'pan'])
def test_action_failure_returns_incomplete_and_cleans_up(failure):
    page, canvas = Page(), Canvas()
    page.mouse.drag_error = failure == 'pan'

    def select():
        raise RuntimeError('selection failed')

    result = collect_frame_sequence(page, canvas, select)
    assert result['complete'] is False
    assert result['error'] == 'RuntimeError'
    assert result['actions'][-1]['success'] is False
    assert result['actions'][-1]['within_recording'] is False
    assert result['sequence']['pan_drags'] == (0 if failure == 'pan' else 4)
    downs = [c for c in page.mouse.commands if c[0] == 'down']
    ups = [c for c in page.mouse.commands if c[0] == 'up']
    assert len(downs) == len(ups)
    assert page.clock == page.start + 10000
    assert page.cleaned and canvas.disposed


def test_callback_overrun_and_hidden_page_remain_explicit():
    page, canvas = Page(), Canvas()
    page.visible = False

    def select():
        page.clock += 2000

    result = collect_frame_sequence(page, canvas, select)
    assert result['complete'] is False
    assert result['visible'] is False
    assert result['actions'][-1]['finish_ms'] == 10500
    assert result['actions'][-1]['within_recording'] is False
    assert result['duration_ms'] == 10000
    assert page.cleaned and canvas.disposed


@pytest.mark.parametrize('duration', [9999, 10001, 0, True, '10000', None])
def test_invalid_duration_rejected_before_io(duration):
    with pytest.raises(ValueError, match='10000'):
        collect_frame_sequence(None, None, None, duration_ms=duration)


@pytest.mark.parametrize('box', [None, {}, {'x': 0, 'y': 0, 'width': 0, 'height': 1},
                                {'x': 0, 'y': 0, 'width': 1, 'height': -1},
                                {'x': float('nan'), 'y': 0, 'width': 1, 'height': 1}])
def test_invalid_canvas_rejected(box):
    canvas = Canvas()
    canvas.box = box
    with pytest.raises(ValueError, match='bounding box'):
        collect_frame_sequence(None, canvas, lambda: None)


def test_private_action_error_is_type_only_in_serialized_snapshot():
    page, canvas = Page(), Canvas()

    def select():
        raise RuntimeError('SECRET-SENTINEL private path and credentials')

    result = collect_frame_sequence(page, canvas, select)
    assert result['error'] == 'RuntimeError'
    assert 'SECRET-SENTINEL' not in json.dumps(result)
    assert result['frame_deltas_ms'] == page.frames
    assert page.cleaned and canvas.disposed


def test_moving_resized_canvas_is_retargeted_for_every_pan_and_wheel():
    page, canvas = Page(), Canvas()
    boxes = []

    def bounds():
        index = len(boxes)
        box = dict(x=10 + index, y=20 + index, width=800 - index, height=600 - index)
        boxes.append(box)
        return box

    canvas.bounding_box = bounds
    result = collect_frame_sequence(page, canvas, lambda: None)
    assert result['complete']
    assert len(boxes) == 25  # Initial validation plus each of the 24 native actions.
    centers = [c for c in page.mouse.commands if c[0] == 'move' and c[4] is None]
    assert [(c[2], c[3]) for c in centers] == [
        (b['x'] + b['width'] / 2, b['y'] + b['height'] / 2) for b in boxes[1:]]


@pytest.mark.parametrize('fault', ['clipped', 'viewport', 'hidden', 'missing-evidence'])
def test_late_invalid_native_target_never_dispatches_or_counts(fault):
    page, canvas = Page(), Canvas()
    original_wait = page.wait_for_timeout

    def wait(delay):
        original_wait(delay)
        if page.clock - page.start >= 3450:
            if fault == 'clipped':
                canvas.box = dict(x=1900, y=20, width=800, height=600)
            elif fault == 'viewport':
                page.viewport_size = dict(width=float('nan'), height=1080)
            elif fault == 'hidden':
                canvas.evaluate = lambda expression: False
            else:
                canvas.evaluate = None

    page.wait_for_timeout = wait
    result = collect_frame_sequence(page, canvas, lambda: pytest.fail('late selection'))
    assert not result['complete']
    assert result['sequence']['zoom_in_steps'] == 1
    assert not result['actions'][-1]['success']
    assert len([c for c in page.mouse.commands if c[0] == 'wheel']) == 1
    assert page.cleaned and canvas.disposed


@pytest.mark.parametrize('fault', ['backwards', 'nan', 'infinity', 'bool', 'stall'])
def test_browser_clock_failure_preserves_raw_snapshot_and_cleanup(fault):
    page, canvas = Page(), Canvas()
    original_wait = page.wait_for_timeout
    original_evaluate = page.evaluate
    bad = {'backwards': 122, 'nan': float('nan'), 'infinity': float('inf'), 'bool': True}
    waited = []

    def wait(delay):
        waited.append(delay)
        if fault != 'stall':
            original_wait(delay)

    def evaluate(script, arg=None):
        if waited and script == '() => performance.now()' and fault != 'stall':
            return bad[fault]
        return original_evaluate(script, arg)

    page.wait_for_timeout, page.evaluate = wait, evaluate
    result = collect_frame_sequence(page, canvas, lambda: None)
    assert not result['complete']
    assert result['error'] == ('TimeoutError' if fault == 'stall' else 'ValueError')
    assert result['frame_deltas_ms'] == page.frames
    assert len(waited) <= 2
    assert page.cleaned and canvas.disposed


@pytest.mark.parametrize('bad', [-1, True, float('nan'), float('inf'), 16, 0])
def test_independent_clock_failure_blocks_late_operation(bad):
    page, canvas = Page(), Canvas()
    native = [0]
    original = page.wait_for_timeout

    def wait(delay):
        original(delay)
        native[0] = bad

    page.wait_for_timeout = wait
    result = collect_frame_sequence(page, canvas, lambda: None, clock=lambda: native[0])
    assert not result['complete']
    assert result['error'] == ('TimeoutError' if bad in (0, 16) else 'ValueError')
    assert result['sequence']['pan_drags'] == 1
    assert result['frame_deltas_ms'] == page.frames
    assert page.cleaned and canvas.disposed


@pytest.mark.parametrize('mode', ['preexisting', 'foreign', 'copied-token', 'missing'])
def test_foreign_recorder_never_stopped_replaced_or_returned(mode):
    page, canvas = Page(), Canvas()
    foreign = dict(token='foreign', installed=123)
    if mode == 'preexisting':
        page.recorder = foreign

    def replace():
        nonlocal foreign
        if mode == 'copied-token':
            foreign = dict(page.recorder)
        elif mode == 'missing':
            foreign = None
        page.recorder = foreign

    result = collect_frame_sequence(page, canvas, replace)
    assert not result['complete'] and result['error'] == 'RuntimeError'
    assert page.recorder is foreign
    assert result['frame_deltas_ms'] == []
    assert not page.cleaned and canvas.disposed


@pytest.mark.parametrize('exception', [KeyboardInterrupt, SystemExit])
def test_cancellation_retains_type_only_partial_snapshot_and_releases(exception):
    page, canvas = Page(), Canvas()

    def cancel():
        raise exception('SECRET-SENTINEL')

    with pytest.raises(exception) as caught:
        collect_frame_sequence(page, canvas, cancel)
    raw = caught.value.x08_frame_snapshot
    assert raw['error'] == exception.__name__
    assert not raw['complete']
    assert raw['frame_deltas_ms'] == page.frames
    assert 'SECRET-SENTINEL' not in json.dumps(raw)
    assert page.cleaned and canvas.disposed


def test_cancellation_survives_cleanup_failure_and_disposal_is_attempted():
    page, canvas = Page(), Canvas()
    original = page.evaluate

    def evaluate(script, arg=None):
        if arg is not None and 'installed' in arg:
            raise RuntimeError('SECRET-SENTINEL cleanup')
        return original(script, arg)

    def cancel():
        raise KeyboardInterrupt('SECRET-SENTINEL action')

    page.evaluate = evaluate
    with pytest.raises(KeyboardInterrupt) as caught:
        collect_frame_sequence(page, canvas, cancel)
    assert caught.value.x08_frame_snapshot['error'] == 'KeyboardInterrupt'
    assert canvas.disposed
    assert 'SECRET-SENTINEL' not in json.dumps(caught.value.x08_frame_snapshot)


def test_failed_install_reply_still_cleans_only_its_owned_recorder():
    page, canvas = Page(), Canvas()
    original = page.evaluate

    def evaluate(script, arg=None):
        result = original(script, arg)
        if arg is not None and 'canvas' in arg:
            raise RuntimeError('SECRET-SENTINEL after installation')
        return result

    page.evaluate = evaluate
    result = collect_frame_sequence(page, canvas, lambda: None)
    assert result['error'] == 'RuntimeError' and not result['complete']
    assert result['frame_deltas_ms'] == page.frames
    assert page.cleaned and page.recorder is None and canvas.disposed
    assert 'SECRET-SENTINEL' not in json.dumps(result)
