"""Bench-owned, synchronous collection of the frozen X08 canvas sequence."""

from math import isfinite
import sys
import time
from uuid import uuid4


_INSTALL = """({canvas, duration_ms, token}) => {
    if (window.__xbenchX08Frames !== undefined) throw Error('RecorderOccupied');
    const start = performance.now();
    const deadline = start + duration_ms;
    if (!Number.isFinite(start) || !Number.isFinite(deadline)) throw Error('InvalidClock');
    const deltas = [];
    const raw_events = {};
    const visibility = [document.visibilityState];
    let visible = document.visibilityState === 'visible';
    let previous = null, raf = null, timer = null, finish = null;
    const types = ['pointerdown', 'pointermove', 'pointerup', 'wheel'];
    for (const type of types) raw_events[type] = {trusted: 0, untrusted: 0};
    const event = e => {
        if (e.target !== canvas || finish !== null || performance.now() >= deadline) return;
        raw_events[e.type][e.isTrusted ? 'trusted' : 'untrusted']++;
    };
    const visibilityChanged = () => {
        if (finish !== null || performance.now() >= deadline) return;
        visibility.push(document.visibilityState);
        visible = visible && document.visibilityState === 'visible';
    };
    const stop = () => {
        if (finish === null) {
            finish = performance.now();
            cancelAnimationFrame(raf);
            clearTimeout(timer);
            for (const type of types) canvas.removeEventListener(type, event, true);
            document.removeEventListener('visibilitychange', visibilityChanged);
        }
        return {frame_deltas_ms: deltas, start_ms: start, finish_ms: finish,
                duration_ms: finish - start, visible, visibility, raw_events};
    };
    const frame = timestamp => {
        if (performance.now() >= deadline || timestamp >= deadline) {
            stop();
            return;
        }
        if (timestamp >= start) {
            if (previous !== null) deltas.push(timestamp - previous);
            previous = timestamp;
        }
        raf = requestAnimationFrame(frame);
    };
    const recorder = {stop, token, installed: start,
        owns: candidate => candidate === recorder};
    window.__xbenchX08Frames = recorder;
    for (const type of types) canvas.addEventListener(type, event, true);
    document.addEventListener('visibilitychange', visibilityChanged);
    const atDeadline = () => {
        const remaining = deadline - performance.now();
        if (remaining > 0) timer = setTimeout(atDeadline, remaining);
        else stop();
    };
    timer = setTimeout(atDeadline, Math.max(0, deadline - performance.now()));
    raf = requestAnimationFrame(frame);
    return start;
}"""

_CLEANUP = """({token, installed}) => {
    const recorder = window.__xbenchX08Frames;
    if (!installed || !recorder || recorder.token !== token ||
        typeof recorder.owns !== 'function' || !recorder.owns(recorder)) return null;
    try { return recorder.stop(); }
    finally {
        if (window.__xbenchX08Frames === recorder) delete window.__xbenchX08Frames;
    }
}"""


_PAINTABLE = """canvas => {
    const style = getComputedStyle(canvas);
    return canvas.isConnected && document.visibilityState === 'visible' &&
        canvas.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}) &&
        style.display !== 'none' && style.visibility === 'visible' &&
        Number(style.opacity) > 0;
}"""


def collect_frame_sequence(page, canvas, window_select, *, duration_ms=10000,
                           clock=time.monotonic):
    """Collect raw frames; ``window_select()`` drives its own real mouse drag.

    Action errors are returned as incomplete diagnostics, without successful
    counts for failed or late actions. Cooperative deadlines cannot preempt
    arbitrary Python callbacks. No callback return value is geometry evidence.
    """
    if type(duration_ms) not in (int, float) or duration_ms != 10000:
        raise ValueError("X08 requires exactly 10000 ms")
    box = canvas.bounding_box()
    if (not isinstance(box, dict)
            or any(type(box.get(key)) not in (int, float)
                   or not isfinite(box[key]) for key in ('x', 'y', 'width', 'height'))
            or box['width'] <= 0 or box['height'] <= 0):
        raise ValueError("X08 requires a positive canvas bounding box")
    if not callable(window_select):
        raise ValueError("window_select must be callable")

    handle = None
    actions = []
    error = None
    snapshot = None
    cancelled = None
    token, start = str(uuid4()), None
    installed = False
    last_clock = last_browser = deadline = None
    held = False

    def native():
        nonlocal last_clock
        value = clock()
        if (type(value) not in (int, float) or not isfinite(value) or
                (last_clock is not None and value < last_clock)):
            raise ValueError('Invalid monotonic clock')
        last_clock = value
        if deadline is not None and value >= deadline:
            raise TimeoutError('Frame deadline')
        return value

    def checked(callback, *args, **kwargs):
        native()
        value = callback(*args, **kwargs)
        native()
        return value

    def now():
        nonlocal last_browser
        value = checked(page.evaluate, "() => performance.now()")
        if (type(value) not in (int, float) or not isfinite(value) or
                value < start or (last_browser is not None and value < last_browser)):
            raise ValueError('Invalid browser clock')
        last_browser = value
        return value - start

    def wait(delay):
        before = now()
        if delay > 0:
            native_before = native()
            left = (deadline - native_before) * 1000
            if not isfinite(left) or left <= 0:
                raise TimeoutError('Frame deadline')
            checked(page.wait_for_timeout, min(delay, left))
            if native() <= native_before:
                raise TimeoutError('Monotonic clock stalled')
            if now() <= before:
                raise TimeoutError('Browser clock stalled')

    def targets(dx=0, dy=0):
        current = checked(canvas.bounding_box)
        viewport = checked(lambda: page.viewport_size)
        if (not isinstance(current, dict) or not isinstance(viewport, dict) or
                any(type(current.get(k)) not in (int, float) or not isfinite(current[k])
                    for k in ('x', 'y', 'width', 'height')) or
                current['width'] <= 0 or current['height'] <= 0 or
                any(type(viewport.get(k)) not in (int, float) or
                    not isfinite(viewport[k]) or viewport[k] <= 0
                    for k in ('width', 'height'))):
            raise ValueError('Invalid canvas geometry')
        if checked(canvas.evaluate, _PAINTABLE) is not True:
            raise ValueError('Canvas paintability unavailable')
        center = (current['x'] + current['width'] / 2,
                  current['y'] + current['height'] / 2)
        end = (center[0] + dx * current['width'], center[1] + dy * current['height'])
        for x, y in (center, end):
            if not (max(0, current['x']) < x < min(viewport['width'], current['x'] + current['width'])
                    and max(0, current['y']) < y < min(viewport['height'], current['y'] + current['height'])):
                raise ValueError('Native target clipped')
        return center, end

    def operation(callback, *args, **kwargs):
        if now() >= duration_ms:
            raise TimeoutError('Recording interval ended')
        return checked(callback, *args, **kwargs)

    try:
        initial = native()
        deadline = initial + duration_ms / 1000 + 5
        if not isfinite(deadline) or deadline <= initial:
            raise ValueError('Invalid frame deadline')
        native()
        handle = canvas.element_handle()
        native()
        installed = True
        start = page.evaluate(_INSTALL,
                              {'canvas': handle, 'duration_ms': duration_ms, 'token': token})
        native()
        if type(start) not in (int, float) or not isfinite(start):
            raise ValueError('Invalid browser clock')
        last_browser = start

        def run(kind, planned, operation):
            wait(max(0, planned - now()))
            began = now()
            if began >= duration_ms:
                return False
            action = {'kind': kind, 'start_ms': began, 'finish_ms': None,
                      'success': False, 'within_recording': False}
            actions.append(action)
            try:
                operation()
                action['finish_ms'] = now()
                action['success'] = action['finish_ms'] <= duration_ms
            finally:
                if action['finish_ms'] is None:
                    pending_action = sys.exc_info()[1]
                    try:
                        action['finish_ms'] = now()
                    except Exception:
                        if pending_action is None:
                            raise
                action['within_recording'] = (
                    action['success'] and action['finish_ms'] <= duration_ms)
            return action['within_recording']

        def pan(dx, dy):
            nonlocal held
            center, end = targets(dx, dy)
            operation(page.mouse.move, *center)

            def press():
                nonlocal held
                held = True
                page.mouse.down(button='right')

            try:
                operation(press)
                operation(page.mouse.move, *end, steps=8)
            finally:
                pending_pan = sys.exc_info()[1]
                try:
                    if held:
                        # Release even after clock failure, but check both sides
                        # on the normal path just like every other native input.
                        try:
                            native()
                        finally:
                            page.mouse.up(button='right')
                            held = False
                        native()
                except Exception:
                    if pending_pan is None or isinstance(pending_pan, Exception):
                        raise

        try:
            for index, (dx, dy) in enumerate(((.12, .08), (-.12, .08),
                                             (-.12, -.08), (.12, -.08))):
                if not run('pan', index * 800, lambda: pan(dx, dy)):
                    break
            else:
                for index in range(20):
                    def zoom():
                        center, _ = targets()
                        operation(page.mouse.move, *center)
                        operation(page.mouse.wheel, 0, -120 if index < 10 else 120)

                    if not run('zoom_in' if index < 10 else 'zoom_out',
                               3200 + index * 250, zoom):
                        break
                else:
                    run('window_select', 8500, lambda: operation(window_select))
        except Exception as exc:
            error = type(exc).__name__
        remaining = max(0, duration_ms - now())
        wait(remaining)
        # Browser timers can wake slightly early; never close the recorder early.
        remaining = duration_ms - now()
        while remaining > 0:
            wait(remaining)
            remaining = duration_ms - now()
    except Exception as exc:
        error = type(exc).__name__
    except BaseException as exc:
        cancelled = exc
        error = type(exc).__name__
    finally:
        # Cleanup deliberately bypasses failed clocks and always attempts each
        # owned resource. Preserve cancellation even if cleanup also fails.
        cleanup_failure = None
        try:
            if installed:
                snapshot = page.evaluate(_CLEANUP, {'token': token, 'installed': installed})
                if snapshot is None:
                    raise RuntimeError('Owned recorder unavailable')
        except BaseException as exc:
            cleanup_failure = exc
        finally:
            for release in ([lambda: page.mouse.up(button='right')] if held else []) + (
                    [handle.dispose] if handle is not None else []):
                try:
                    release()
                except BaseException as exc:
                    if cleanup_failure is None or not isinstance(exc, Exception):
                        cleanup_failure = exc
        if cleanup_failure is not None:
            if not isinstance(cleanup_failure, Exception):
                cancelled = cancelled or cleanup_failure
            error = error or type(cleanup_failure).__name__

    if snapshot is None:
        snapshot = dict(frame_deltas_ms=[], duration_ms=None, start_ms=None,
                        finish_ms=None, visible=False, visibility=[], raw_events={})

    counts = {kind: sum(a['kind'] == kind and a['within_recording'] for a in actions)
              for kind in ('pan', 'zoom_in', 'zoom_out', 'window_select')}
    result = {
        'frame_deltas_ms': snapshot['frame_deltas_ms'],
        'sequence': {'pan_drags': counts['pan'], 'zoom_in_steps': counts['zoom_in'],
                     'zoom_out_steps': counts['zoom_out'],
                     'duration_ms': snapshot['duration_ms']},
        'complete': error is None and counts == {
            'pan': 4, 'zoom_in': 10, 'zoom_out': 10, 'window_select': 1},
        'visible': snapshot['visible'], 'raw_events': snapshot['raw_events'],
        'actions': actions, 'duration_ms': snapshot['duration_ms'],
        'start_ms': snapshot['start_ms'], 'finish_ms': snapshot['finish_ms'],
        'visibility': snapshot['visibility'], 'error': error,
    }
    if cancelled is not None:
        cancelled.x08_frame_snapshot = result
        raise cancelled
    return result
