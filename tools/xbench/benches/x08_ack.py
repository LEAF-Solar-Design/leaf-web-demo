"""Bench-owned evidence for final-confirmation to actual queued-job paint.

Uses a caller-owned synchronous Playwright page and final confirmation locator.
Unavailable evidence is not a product failure, and this helper never grades it.
"""

from math import isfinite
import time
from urllib.parse import urlsplit
from uuid import UUID, uuid4


class AckCaptureError(ValueError):
    """The actual HTTP202/queued-paint evidence is unavailable or inconsistent."""


def _positive(value):
    try:
        return type(value) in (int, float) and isfinite(value) and value > 0
    except OverflowError:
        return False


def page_origin(url):
    """Return only the HTTP origin, never credentials, paths or query strings."""
    try:
        if not isinstance(url, str):
            raise ValueError
        parts = urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.hostname:
            raise ValueError
        port = parts.port
        return (parts.scheme, parts.hostname, port if port is not None else
                (443 if parts.scheme == 'https' else 80))
    except ValueError:
        raise AckCaptureError('Page origin unavailable') from None


def run_request_matches(request, url, origin):
    try:
        return (request.method == 'POST' and page_origin(url) == origin
                and urlsplit(url).path == '/api/run')
    except (ValueError, AttributeError):
        return False


def validate_ack(raw, *, response_status, response_body):
    """Validate synthetic or captured evidence and return only sanitized fields."""
    if type(response_status) is not int or response_status != 202:
        raise AckCaptureError('Actual HTTP202 required; synchronous responses are unavailable')
    if not isinstance(response_body, dict) or response_body.get('status') != 'submitted':
        raise AckCaptureError('A submitted job response is required')
    job_id = response_body.get('job_id')
    try:
        if not isinstance(job_id, str):
            raise ValueError
        parsed = UUID(job_id)
        if str(parsed) != job_id.lower() or parsed.int == 0:
            raise ValueError
    except (ValueError, AttributeError):
        raise AckCaptureError('A valid job UUID is required') from None
    if not isinstance(raw, dict):
        raise AckCaptureError('Missing capture evidence')
    queued_job_id = raw.get('queued_job_id')
    try:
        if (not isinstance(queued_job_id, str)
                or str(UUID(queued_job_id)) != queued_job_id
                or UUID(queued_job_id).int == 0
                or queued_job_id != job_id):
            raise ValueError
    except (ValueError, AttributeError):
        raise AckCaptureError('Queued strip UUID must match the actual submitted job') from None
    times = [raw.get(key) for key in ('pointer_ms', 'response_end_ms', 'paint_ms')]
    if not all(_positive(value) for value in times):
        raise AckCaptureError('Finite positive pointer, resource and paint timestamps required')
    pointer, response_end, paint = times
    if not pointer <= response_end <= paint:
        raise AckCaptureError('Queued paint must follow the fresh network response')
    if type(raw.get('resource_matches')) is not int or raw['resource_matches'] != 1:
        raise AckCaptureError('Exactly one fresh resource timing entry is required')
    if raw.get('queued_painted') is not True:
        raise AckCaptureError('A queued strip paint is required; running is optimistic')
    durations = raw.get('event_durations_ms', [])
    if not isinstance(durations, list) or not all(_positive(d) for d in durations):
        raise AckCaptureError('Invalid Event Timing durations')
    entries = raw.get('event_entries', [])
    if not isinstance(entries, list):
        raise AckCaptureError('Invalid Event Timing entries')
    sanitized_entries = []
    for entry in entries:
        if (not isinstance(entry, dict)
                or entry.get('name') not in ('pointerdown', 'pointerup', 'click')
                or not _positive(entry.get('startTime'))
                or not _positive(entry.get('duration'))
                or not pointer <= entry['startTime'] <= paint
                or type(entry.get('interactionId')) is not int
                or entry['interactionId'] < 0):
            raise AckCaptureError('Invalid or unrelated Event Timing entry')
        sanitized_entries.append({key: entry[key] for key in
                                  ('name', 'startTime', 'duration', 'interactionId')})
    latency = max([paint - pointer] + durations +
                  [entry['duration'] for entry in sanitized_entries])
    if not _positive(latency):
        raise AckCaptureError('A positive latency is required')
    return {'latency_ms': latency, 'http_status': 202, 'rendered': True,
            'job_id': str(parsed), 'queued_job_id': queued_job_id,
            'pointer_ms': pointer, 'paint_ms': paint,
            'response_end_ms': response_end, 'resource_matches': 1,
            'queued_painted': True, 'event_durations_ms': list(durations),
            'event_entries': sanitized_entries}


_INSTALL = """(control, token) => {
    if (window.__xbenchX08Ack) return false;
    const s = {token, pointer_ms: null, paint_ms: null, queued_painted: false,
               queued_job_id: null,
               events: [], raf: null, task: null, stopped: false};
    const queued = () => Array.from(document.querySelectorAll('.strip-running .verb'))
        .filter(el => /^queued:\\s*\\S/.test(el.textContent.trim()));
    const jobId = el => el.closest('.strip-running')?.dataset.jobId ?? null;
    const paintable = el => {
        if (document.visibilityState !== 'visible' || !el.isConnected) return false;
        const intersects = Array.from(el.getClientRects()).some(r =>
            r.width > 0 && r.height > 0 && r.right > 0 && r.bottom > 0 &&
            r.left < window.innerWidth && r.top < window.innerHeight);
        if (!intersects) return false;
        for (let node = el; node; node = node.parentElement) {
            const style = getComputedStyle(node);
            if (style.display === 'none' || style.visibility !== 'visible' ||
                Number(style.opacity) <= 0) return false;
        }
        return true;
    };
    const baseline = new Map();
    const pointer = e => {
        if (!e.isTrusted) return;
        if (s.pointer_ms !== null) { s.multiple_pointers = true; return; }
        for (const el of queued()) baseline.set(el, {text: el.textContent.trim(), id: jobId(el)});
        s.pointer_ms = e.timeStamp;
    };
    const events = entries => {
        for (const e of entries) {
            if (s.pointer_ms === null || e.startTime < s.pointer_ms ||
                !['pointerdown', 'pointerup', 'click'].includes(e.name) ||
                !e.target || !(e.target === control || control.contains(e.target))) continue;
            s.events.push({name: e.name, startTime: e.startTime,
                           duration: e.duration, interactionId: e.interactionId});
        }
    };
    s.stop = () => {
        s.stopped = true;
        if (s.mo) s.mo.disconnect();
        if (s.po) s.po.disconnect();
        control.removeEventListener('pointerdown', pointer, true);
        cancelAnimationFrame(s.raf);
        clearTimeout(s.task);
    };
    window.__xbenchX08Ack = s;
    control.addEventListener('pointerdown', pointer, true);
    const unchanged = (el, text, id) => el.isConnected &&
        el.textContent.trim() === text && jobId(el) === id && queued().includes(el);
    const schedule = (el, text, id) => {
        s.raf = requestAnimationFrame(() => {
            s.raf = null;
            if (s.stopped) return;
            if (!unchanged(el, text, id)) { scan(); return; }
            // Animation progress need not mutate the DOM. Keep checking frames
            // until the queued strip (including its ancestors) can paint.
            if (!paintable(el)) { schedule(el, text, id); return; }
            s.task = setTimeout(() => {
                s.task = null;
                if (s.stopped) return;
                if (!unchanged(el, text, id)) { scan(); return; }
                if (!paintable(el)) { schedule(el, text, id); return; }
                s.paint_ms = performance.now();
                s.queued_job_id = id;
                s.queued_painted = true;
            }, 0);
        });
    };
    const scan = () => {
        if (s.stopped || s.pointer_ms === null || s.paint_ms !== null ||
            s.raf !== null || s.task !== null) return;
        const current = queued();
        for (const [el, prior] of baseline) {
            if (!current.includes(el) || el.textContent.trim() !== prior.text ||
                jobId(el) !== prior.id) baseline.delete(el);
        }
        const el = current.find(el => !baseline.has(el));
        if (!el) return;
        schedule(el, el.textContent.trim(), jobId(el));
    };
    s.mo = new MutationObserver(scan);
    s.mo.observe(document.body, {subtree: true, childList: true, characterData: true,
                                 attributes: true, attributeFilter: ['class', 'data-job-id']});
    if (typeof PerformanceObserver !== 'undefined') {
        try {
            s.po = new PerformanceObserver(list => events(list.getEntries()));
            s.po.observe({type: 'event', durationThreshold: 16});
            s.drain = () => events(s.po.takeRecords());
        } catch (_) { if (s.po) s.po.disconnect(); }
    }
    return true;
}"""

_READY = """({token, url}) => {
    const s = window.__xbenchX08Ack;
    if (!s || s.token !== token) return false;
    const resources = performance.getEntriesByType('resource')
        .filter(e => e.name === url && s.pointer_ms !== null && e.startTime >= s.pointer_ms);
    return s.multiple_pointers || (s.paint_ms !== null && resources.length > 0);
}"""

_SNAPSHOT = """({token, url}) => {
    const s = window.__xbenchX08Ack;
    if (!s || s.token !== token) return null;
    if (s.drain) s.drain();
    const resources = performance.getEntriesByType('resource')
        .filter(e => e.name === url && s.pointer_ms !== null && e.startTime >= s.pointer_ms);
    const r = resources.length === 1 ? resources[0] : null;
    const events = s.events.filter(e => e.startTime <= s.paint_ms);
    return {pointer_ms: s.multiple_pointers ? null : s.pointer_ms,
            paint_ms: s.paint_ms, queued_painted: s.queued_painted,
            queued_job_id: s.queued_job_id,
            resource_matches: resources.length,
            response_end_ms: r && r.responseStart > 0 ? r.responseEnd : null,
            event_entries: events, event_durations_ms: events.map(e => e.duration)};
}"""

_CLEANUP = """token => {
    const s = window.__xbenchX08Ack;
    if (!s || s.token !== token) return;
    try { s.stop(); } finally { delete window.__xbenchX08Ack; }
}"""


def capture_job_ack(page, control, *, timeout_ms=15000, clock=time.monotonic):
    """Click the supplied final control and collect one actual202 paint interval.

    The caller retains ownership of its signed-in browser/context. Action errors
    propagate, while missing or inconsistent measurement evidence refuses capture.
    One cooperative deadline bounds waits; synchronous Python/body callbacks cannot
    be preempted. The first queued paint is recorded before Python validates its
    strip UUID against the actual response body.
    """
    if not _positive(timeout_ms):
        raise AckCaptureError('A positive timeout is required')
    last = None

    def now():
        nonlocal last
        value = clock()
        try:
            valid = (type(value) in (int, float) and isfinite(value)
                     and (last is None or value >= last))
        except OverflowError:
            valid = False
        if not valid:
            raise AckCaptureError('Monotonic finite clock required')
        last = value
        return value

    deadline = now() + timeout_ms / 1000
    if not isfinite(deadline) or not _positive(deadline - last):
        raise AckCaptureError('Finite deadline required')

    def remaining():
        value = (deadline - now()) * 1000
        if not _positive(value):
            raise AckCaptureError('Acknowledgment deadline exceeded')
        return value

    origin = page_origin(getattr(page, 'url', None))
    token = str(uuid4())
    failed = True
    try:
        remaining()
        if not control.evaluate(_INSTALL, token):
            raise AckCaptureError('Another acknowledgment capture is already active')
        remaining()
        click_failed = False
        try:
            with page.expect_response(
                    lambda response: run_request_matches(response.request, response.url, origin),
                    timeout=remaining()) as pending:
                try:
                    control.click(timeout=remaining())
                except Exception:
                    click_failed = True
                    raise
                remaining()
            remaining()
            response = pending.value
            remaining()
        except Exception as exc:
            if click_failed:
                raise
            raise AckCaptureError('Actual run response unavailable') from exc
        if not run_request_matches(response.request, response.url, origin):
            raise AckCaptureError('Actual run response origin unavailable')
        if type(response.status) is not int or response.status != 202:
            raise AckCaptureError('Actual HTTP202 unavailable')
        try:
            remaining()
            body = response.json()  # Waits for the actual response body.
            remaining()
        except Exception as exc:
            raise AckCaptureError('Submitted response body unavailable') from exc
        args = {'token': token, 'url': response.url}
        try:
            page.wait_for_function(_READY, arg=args, timeout=remaining())
            remaining()
        except Exception as exc:
            raise AckCaptureError('Queued paint/resource evidence unavailable') from exc
        remaining()
        raw = page.evaluate(_SNAPSHOT, args)
        remaining()
        result = validate_ack(raw, response_status=response.status, response_body=body)
        remaining()
        failed = False
        return result
    finally:
        try:
            page.evaluate(_CLEANUP, token)
        except Exception:
            if not failed:
                raise AckCaptureError('Capture cleanup unavailable') from None
