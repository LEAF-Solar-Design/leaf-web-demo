"""Bench-owned, synchronous collection of the frozen X08 canvas sequence."""

from math import isfinite


_INSTALL = """({canvas, duration_ms}) => {
    const start = performance.now();
    const deadline = start + duration_ms;
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
    window.__xbenchX08Frames = {stop};
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

_CLEANUP = """() => {
    const recorder = window.__xbenchX08Frames;
    if (!recorder) return null;
    try { return recorder.stop(); }
    finally { delete window.__xbenchX08Frames; }
}"""


def collect_frame_sequence(page, canvas, window_select, *, duration_ms=10000):
    """Collect raw frames; ``window_select()`` drives its own real mouse drag.

    Action errors are returned as incomplete diagnostics, without successful
    counts for failed or late actions. Setup errors still propagate. No callback
    return value is used as evidence of selection or geometry.
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

    center = (box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
    handle = canvas.element_handle()
    actions = []
    error = None
    snapshot = None
    try:
        start = page.evaluate(_INSTALL, {'canvas': handle, 'duration_ms': duration_ms})

        def now():
            return page.evaluate("() => performance.now()") - start

        def run(kind, planned, operation):
            page.wait_for_timeout(max(0, planned - now()))
            began = now()
            if began >= duration_ms:
                return False
            action = {'kind': kind, 'start_ms': began, 'finish_ms': None,
                      'success': False, 'within_recording': False}
            actions.append(action)
            try:
                operation()
                action['success'] = True
            finally:
                action['finish_ms'] = now()
                action['within_recording'] = (
                    action['success'] and action['finish_ms'] <= duration_ms)
            return action['within_recording']

        def pan(dx, dy):
            page.mouse.move(*center)
            try:
                page.mouse.down(button='right')
                page.mouse.move(center[0] + dx * box['width'],
                                center[1] + dy * box['height'], steps=8)
            finally:
                page.mouse.up(button='right')

        try:
            for index, (dx, dy) in enumerate(((.12, .08), (-.12, .08),
                                             (-.12, -.08), (.12, -.08))):
                if not run('pan', index * 800, lambda: pan(dx, dy)):
                    break
            else:
                for index in range(20):
                    def zoom():
                        if index == 0:
                            page.mouse.move(*center)
                        page.mouse.wheel(0, -120 if index < 10 else 120)

                    if not run('zoom_in' if index < 10 else 'zoom_out',
                               3200 + index * 250, zoom):
                        break
                else:
                    run('window_select', 8500, window_select)
        except Exception as exc:
            error = f'{type(exc).__name__}: {exc}'
        remaining = max(0, duration_ms - now())
        page.wait_for_timeout(remaining)
        # Browser timers can wake slightly early; never close the recorder early.
        remaining = duration_ms - now()
        while remaining > 0:
            page.wait_for_timeout(remaining)
            remaining = duration_ms - now()
    finally:
        try:
            snapshot = page.evaluate(_CLEANUP)
        finally:
            if handle is not None:
                handle.dispose()

    counts = {kind: sum(a['kind'] == kind and a['within_recording'] for a in actions)
              for kind in ('pan', 'zoom_in', 'zoom_out', 'window_select')}
    return {
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
