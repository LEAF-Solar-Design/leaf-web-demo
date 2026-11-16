"""Fake-clock checks for scheduling and honest X08 collection diagnostics."""

import pytest

from benches.x08_frames import collect_frame_sequence


class Canvas:
    box = {'x': 10, 'y': 20, 'width': 800, 'height': 600}

    def __init__(self):
        self.disposed = False

    def bounding_box(self):
        return self.box

    def element_handle(self):
        return self

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

    def evaluate(self, script, arg=None):
        if arg is not None:
            assert arg['canvas'] is not None
            assert arg['duration_ms'] == 10000
            self.start = self.clock
            return self.start
        if script == '() => performance.now()':
            return self.clock
        assert 'recorder.stop()' in script
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
    assert len(moves) == 9
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
    assert page.waits[-1] == 0
    assert page.cleaned and canvas.disposed


@pytest.mark.parametrize('failure', ['callback', 'pan'])
def test_action_failure_returns_incomplete_and_cleans_up(failure):
    page, canvas = Page(), Canvas()
    page.mouse.drag_error = failure == 'pan'

    def select():
        raise RuntimeError('selection failed')

    result = collect_frame_sequence(page, canvas, select)
    assert result['complete'] is False
    assert result['error'].startswith('RuntimeError:')
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
