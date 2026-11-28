"""Record foreground Solve submissions and raw frames on the X08 browser clock."""

from contextlib import contextmanager
from uuid import uuid4

from benches.x08_frames import _PAINTABLE


_INSTALL = """
({mount, token, drawing_key, duration_ms}) => {
    if (window.__xbenchX11Frames !== undefined) throw Error('RecorderOccupied');
    const paintable = __X08_PAINTABLE__;
    const start_ms = performance.now();
    const deadline = start_ms + duration_ms;
    const raf_ticks_ms = [];
    const frames = [];
    let api, canvas, unsubscribe, queue = [];
    let observerGeneration = 0;
    let lastSerial = 0;
    let raf, timer, finish_ms, error;

    const release = () => {
        unsubscribe?.();
        unsubscribe = undefined;
        queue = [];
    };
    const visibilityChanged = () => {
        if (document.visibilityState !== 'visible') stop('Foreground lost');
    };
    function stop(reason) {
        if (finish_ms === undefined) {
            finish_ms = performance.now();
            error = reason;
            cancelAnimationFrame(raf);
            clearTimeout(timer);
            document.removeEventListener('visibilitychange', visibilityChanged);
            release();
        }
        return {start_ms, finish_ms, raf_ticks_ms, frames, error: error ?? null};
    }

    const validMembership = row => {
        if (!row || !Array.isArray(row.panelRefs) || !row.panelRefs.length ||
            !row.panelRefs.every(panel => typeof panel === 'string' && panel.length > 0)) return false;
        if (row.phase === 'saved') return typeof row.stringId === 'string' && !!row.stringId;
        if (typeof row.frameId !== 'string' || !row.frameId) return false;
        return row.phase === 'preview' || (row.phase === 'terminal' &&
            typeof row.jobId === 'string' &&
            /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(row.jobId));
    };
    const submit = draw => {
        if (draw.drawingKey !== drawing_key ||
            !Number.isSafeInteger(draw.graphRev) || draw.graphRev < 0 ||
            !/^[a-f0-9]{64}$/.test(draw.graphSha256) ||
            !Number.isSafeInteger(draw.serial) || draw.serial <= lastSerial ||
            !Array.isArray(draw.memberships) || !draw.memberships.every(validMembership) ||
            !Number.isFinite(draw.render_ms) || draw.render_ms < start_ms ||
            draw.render_ms > performance.now()) {
            stop('Invalid draw identity or clock');
            return;
        }
        if (draw.render_ms >= deadline) return;
        lastSerial = draw.serial;
        // Viewer and recorder can run in either order within an RAF. Keep
        // the preceding submission even when this RAF has already rendered.
        if (queue.length === 2) queue.shift();
        queue.push(draw);
    };

    const bind = () => {
        const next = mount.__leafViewerObservation;
        const nextCanvas = mount.querySelector('canvas');
        if (next !== api || nextCanvas !== canvas) {
            release();
            api = next;
            canvas = nextCanvas;
        }
        if (!unsubscribe && typeof api?.subscribeSolarDraws === 'function' && canvas) {
            observerGeneration++;
            lastSerial = 0;
            unsubscribe = api.subscribeSolarDraws(submit);
        }
    };
    const isPaintable = () => {
        if (!canvas?.width || !canvas.height || !paintable(canvas)) return false;
        const box = canvas.getBoundingClientRect();
        return box.left < innerWidth && box.top < innerHeight && box.right > 0 && box.bottom > 0;
    };
    const sameMembership = (left, right) =>
        left.length === right.length && left.every((row, i) => {
            const other = right[i];
            return row.phase === other.phase && row.stringId === other.stringId &&
                row.frameId === other.frameId && row.jobId === other.jobId &&
                row.panelRefs.length === other.panelRefs.length &&
                row.panelRefs.every((panel, j) => panel === other.panelRefs[j]);
        });

    const frame = at_ms => {
        if (finish_ms !== undefined) return;
        try {
            if (!mount.isConnected || document.visibilityState !== 'visible') {
                stop('Foreground mount lost');
                return;
            }
            if (performance.now() >= deadline || at_ms >= deadline) {
                stop();
                return;
            }
            bind();
            if (at_ms >= start_ms) {
                if (at_ms <= (raf_ticks_ms.at(-1) ?? -1) || raf_ticks_ms.length >= 60_000) {
                    stop('Invalid or exhausted frame clock');
                    return;
                }
                raf_ticks_ms.push(at_ms);
                const draw = queue.findLast(item => item.render_ms < at_ms);
                queue = queue.filter(item => item.render_ms >= at_ms);
                if (draw) {
                    if (!isPaintable()) {
                        stop('Submitted canvas is not paintable');
                        return;
                    }
                    const last = frames.at(-1);
                    if (!last || last.observer_generation !== observerGeneration ||
                        last.graph_sha256 !== draw.graphSha256 ||
                        last.graph_rev !== draw.graphRev ||
                        !sameMembership(last.memberships, draw.memberships)) {
                        if (frames.length >= 256) {
                            stop('Membership capture limit reached');
                            return;
                        }
                        frames.push({
                            at_ms, render_ms: draw.render_ms, serial: draw.serial,
                            observer_generation: observerGeneration,
                            drawing_key, graph_rev: draw.graphRev, graph_sha256: draw.graphSha256,
                            visible: true, memberships: draw.memberships,
                        });
                    }
                }
            }
            raf = requestAnimationFrame(frame);
        } catch {
            stop('Frame capture failed');
        }
    };
    const atDeadline = () => {
        const remaining = deadline - performance.now();
        if (remaining > 0) timer = setTimeout(atDeadline, remaining);
        else stop();
    };

    try {
        bind();
        if (!unsubscribe || !isPaintable()) throw Error('ForegroundObserverUnavailable');
        window.__xbenchX11Frames = {token, stop};
        document.addEventListener('visibilitychange', visibilityChanged);
        timer = setTimeout(atDeadline, duration_ms);
        raf = requestAnimationFrame(frame);
        return start_ms;
    } catch (error) {
        try { stop('Recorder installation failed'); }
        finally { delete window.__xbenchX11Frames; }
        throw error;
    }
}
""".replace("__X08_PAINTABLE__", _PAINTABLE)

_CLEANUP = """
token => {
    const recorder = window.__xbenchX11Frames;
    if (recorder?.token !== token) return null;
    try { return recorder.stop(); }
    finally { delete window.__xbenchX11Frames; }
}
"""


@contextmanager
def record_solve_frames(page, mount, *, drawing_key, duration_ms):
    """Yield raw evidence filled on exit. The caller drives the real Solve UI.

    Start while the exact drawing's canvas is visible. Use the same browser
    performance clock for the commit and terminal events. Any recorder error
    or missing capture invalidates the entire interval.
    """
    if not isinstance(drawing_key, str) or not drawing_key.strip():
        raise ValueError("X11 requires an exact drawing key")
    if type(duration_ms) is not int or not 1 <= duration_ms <= 120_000:
        raise ValueError("X11 requires a finite capture duration of at most 120 seconds")

    token = str(uuid4())
    evidence = {}
    installed = False
    try:
        start = mount.evaluate(
            "(mount, options) => (" + _INSTALL + ")({...options, mount})",
            {"token": token, "drawing_key": drawing_key, "duration_ms": duration_ms},
        )
        installed = True
        evidence["start_ms"] = start
        yield evidence
    except BaseException:
        # Preserve the original operation error through browser cleanup.
        try:
            page.evaluate(_CLEANUP, token)
        except Exception:
            pass
        raise
    else:
        capture = page.evaluate(_CLEANUP, token)
        if not installed or not capture or capture["start_ms"] != start:
            raise RuntimeError("X11 recorder ownership or browser clock was lost")
        if capture["error"] is not None or not capture["frames"] or not capture["raf_ticks_ms"]:
            raise RuntimeError(capture["error"] or "X11 foreground evidence is missing")
        evidence.update(capture)
