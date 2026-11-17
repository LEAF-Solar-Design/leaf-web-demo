"""Native-bound camera preparation, independent of rendered selections."""
from types import SimpleNamespace
import pytest
from benches.x08_oracle import choose_window, contained_handles, guarded_window_rect
from benches.x08_runtime import frame_native_modules


def test_guarded_edges_preserve_independent_set_and_survive_rounding():
    modules = [dict(handle='A', bounds=[0, 0, 1, 1]),
               dict(handle='B', bounds=[2, 0, 3, 1]),
               dict(handle='C', bounds=[3.001, 0, 3.002, 1])]
    rect = guarded_window_rect(modules, [0, 0, 3, 1], ['A', 'B'])
    assert rect[0] < 0 and rect[2] > 3 and rect[3] > 1
    assert contained_handles(modules, rect) == ['A', 'B']
    rounded_geometry = [dict(handle='A', bounds=[-0.000001, 0, 1, 1.000001]),
                        dict(handle='B', bounds=[2, 0, 3.000001, 1])]
    assert contained_handles(rounded_geometry, rect) == ['A', 'B']
    with pytest.raises(ValueError):
        guarded_window_rect(modules, [0, 0, 3, 1], ['A'])


def test_choose_window_remains_independent_and_exact():
    modules = [dict(handle=str(i), bounds=[i * 2, 0, i * 2 + 1, 1]) for i in range(8)]
    result = choose_window(modules, target=3, tolerance=0)
    assert result['expected_handles'] == ['0', '1', '2']
    assert contained_handles(modules, result['rect']) == result['expected_handles']


class CameraDouble:
    def __init__(self, *, offset=0, fail_down=False, stalled=False):
        self.center = [5 + offset, 5]
        self.zoom = 0.001
        self.fail_down, self.stalled = fail_down, stalled
        self.held = False
        self.last = (50, 50)
        self.calls = []
        self.settles = 0
        self._expected_drawing_key = 'invented-drawing'
        self._oracle = {'modules': [dict(handle='A', bounds=[0, 0, 10, 10])]}
        self.canvas = SimpleNamespace(evaluate=self.project, bounding_box=lambda: dict(x=0, y=0, width=100, height=100))
        self.page = SimpleNamespace(mouse=self, evaluate=self.settle, wait_for_function=self.ready)

    def ready(self, expression, **kwargs):
        assert 'snapshot()?.drawingKey' in expression
        assert kwargs == dict(arg='invented-drawing', timeout=10000)

    def project(self, expression, point):
        return dict(x=50 + (point[0] - self.center[0]) * self.zoom,
                    y=50 + (point[1] - self.center[1]) * self.zoom)

    def _point(self, x, y):
        p = self.project(None, [x, y])
        assert 0 < p['x'] < 100 and 0 < p['y'] < 100
        return p['x'], p['y']

    def move(self, x, y, **kwargs):
        if self.held:
            self.center[0] -= (x - self.last[0]) / self.zoom * 0.8
            self.center[1] -= (y - self.last[1]) / self.zoom * 0.8
        self.last = (x, y)
        self.calls.append('move')

    def down(self, *, button):
        assert button == 'right'
        self.calls.append('down')
        if self.fail_down:
            raise RuntimeError('input unavailable')
        self.held = True

    def up(self, *, button):
        assert button == 'right'
        self.held = False
        self.calls.append('up')

    def wheel(self, x, y):
        self.calls.append('wheel')
        if not self.stalled:
            self.zoom *= 1.2 if y < 0 else 1 / 1.2

    def settle(self, expression):
        assert 'requestAnimationFrame' in expression
        self.settles += 1


@pytest.mark.parametrize('offset', [0, 10000])
def test_camera_preparation_uses_inputs_and_keeps_geometry(offset):
    runtime = CameraDouble(offset=offset)
    original = runtime._oracle['modules']
    result = frame_native_modules(runtime)
    assert 0.55 <= result['module_viewport_coverage'] <= 0.8
    assert result['before_cold_samples'] is True
    assert runtime._oracle['modules'] is original
    assert runtime.settles and 'wheel' in runtime.calls
    assert not runtime.held


def test_pan_failure_still_releases_owned_button():
    runtime = CameraDouble(offset=10000, fail_down=True)
    with pytest.raises(RuntimeError):
        frame_native_modules(runtime)
    assert runtime.calls[-1] == 'up'
    assert not runtime.held


def test_camera_preparation_is_bounded_when_zoom_does_not_move():
    runtime = CameraDouble(stalled=True)
    with pytest.raises(ValueError, match='did not converge'):
        frame_native_modules(runtime, max_steps=3)
    assert runtime.calls.count('wheel') == 3
    assert not runtime.held
