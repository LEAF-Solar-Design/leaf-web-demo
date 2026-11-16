"""Consume actual Viewer observations; no grading or readiness is inferred here."""

from copy import deepcopy
from math import isfinite
from uuid import uuid4


class SelectionCaptureError(ValueError):
    """Selection evidence is unavailable (UNREADABLE), stale, or inconsistent."""


def _require(condition, message):
    if not condition:
        raise SelectionCaptureError(message)


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _integer(value):
    return type(value) is int and value >= 0


def _number(value):
    try:
        return type(value) in (int, float) and isfinite(value) and value >= 0
    except OverflowError:
        return False


def _handles(value):
    _require(isinstance(value, (list, tuple, set, frozenset)) and bool(value),
             'Nonempty independently bound handles required')
    _require(all(_text(h) for h in value) and len(set(value)) == len(value),
             'Handles must be unique nonempty strings')
    return sorted(value)


def geometry_evidence(snapshot, expected_handles):
    """Attribute visible primitives only to the caller's fixture-bound modules."""
    expected = _handles(expected_handles)
    _require(isinstance(snapshot, dict) and _text(snapshot.get('drawingKey')),
             'Actual drawing snapshot required')
    rows, omitted = snapshot.get('handles'), snapshot.get('omitted')
    _require(isinstance(rows, list) and isinstance(omitted, dict), 'Invalid snapshot shape')
    _require(all(_integer(omitted.get(k)) for k in ('blocks', 'activeIntake')),
             'Invalid global omission counters')
    _require(omitted['blocks'] == omitted['activeIntake'] == 0,
             'Global truncation cannot be attributed to modules')
    by_handle = {}
    for row in rows:
        _require(isinstance(row, dict) and _text(row.get('handle')), 'Invalid handle row')
        handle = row['handle']
        _require(handle not in by_handle, 'Duplicate snapshot handle')
        _require(all(_integer(row.get(k)) for k in
                     ('geometryTriangles', 'geometrySegments', 'markerCount')),
                 'Invalid primitive counter')
        by_handle[handle] = row
    rendered = [h for h in expected if h in by_handle and
                by_handle[h]['geometryTriangles'] + by_handle[h]['geometrySegments'] > 0]
    markers = [h for h in expected if h in by_handle and by_handle[h]['markerCount'] > 0]
    missing = sorted(set(expected) - set(rendered))
    return {'geometry': {'rendered_module_count': len(rendered),
                         'marker_module_count': len(markers),
                         'omitted_module_count': len(missing)},
            'expected_handles': expected, 'rendered_handles': rendered,
            'missing_handles': missing, 'marker_handles': markers}


def validate_selection(raw, *, expected_handles, kind):
    """Validate actual evidence without scoring it.

    Callers must independently bind expected_handles to the fixture/oracle, never
    derive them from selection output. Chromium can omit fast Event Timing entries
    below its reporting threshold: absence is UNREADABLE, never a product FAIL.
    """
    expected = _handles(expected_handles)
    _require(kind in ('click', 'marquee'), 'Invalid selection kind')
    _require(len(expected) == 1 if kind == 'click' else 490 <= len(expected) <= 510,
             'Invalid independently expected selection size')
    _require(isinstance(raw, dict), 'Missing selection evidence')
    cleanup = raw.get('cleanup')
    _require(isinstance(cleanup, dict) and all(cleanup.get(k) is True for k in
             ('recorder_removed', 'observer_disconnected', 'element_disposed')),
             'Capture cleanup unavailable')
    _require(_text(raw.get('baseline_drawing_key')) and
             raw.get('drawing_key') == raw['baseline_drawing_key'], 'Stale drawing')
    baseline, record, pointer = raw.get('baseline_serial'), raw.get('record'), raw.get('pointer')
    _require(_integer(baseline) and isinstance(record, dict) and isinstance(pointer, dict),
             'Missing baseline, record or pointer')
    _require(_integer(record.get('serial')) and record['serial'] > baseline,
             'Selection must be newer than baseline')
    _require(record.get('kind') == kind and record.get('additive') is False,
             'Wrong kind or additive selection')
    _require(pointer.get('trusted') is True and record.get('trusted') is True,
             'Trusted pointer required')
    for key in ('start_ms', 'event_timestamp'):
        _require(_number(pointer.get(key)) and _number(record.get(key)) and
                 abs(pointer[key] - record[key]) <= 1, 'Pointer timestamps differ')
    _require(_integer(pointer.get('pointerId')) and
             type(record.get('pointerId')) is int and
             record['pointerId'] == pointer['pointerId'], 'Pointer identity differs')
    keys = ('start_ms', 'handler_finished_ms', 'highlight_committed_ms',
            'render_ms', 'after_frame_ms')
    _require(all(_number(record.get(k)) for k in keys), 'Invalid selection timestamps')
    start, handler, commit, render, after = [record[k] for k in keys]
    _require(start <= handler <= render <= after and start <= commit <= render and after > start,
             'Invalid post-render ordering')
    selected, requested = _handles(record.get('selected_handles')), _handles(record.get('requested_handles'))
    _require(selected == requested == expected, 'Selection differs from independent handles')
    _require(_integer(record.get('highlight_geometry_count')) and
             record['highlight_geometry_count'] > 0, 'Missing highlight geometry')
    history = raw.get('visible_history')
    _require(isinstance(history, list) and bool(history) and
             all(value == 'visible' for value in history), 'Capture was not continuously visible')
    paintability = raw.get('paintability')
    _require(isinstance(paintability, dict) and all(paintability.get(k) is True for k in
             ('baseline', 'down', 'after')), 'Canvas was not paintable throughout capture')
    _require(raw.get('event_timing_supported') is True, 'Event Timing unavailable')
    entries = raw.get('event_entries')
    _require(isinstance(entries, list) and bool(entries), 'Event Timing entries unavailable (UNREADABLE)')
    sanitized = []
    for entry in entries:
        _require(isinstance(entry, dict) and entry.get('name') in ('pointerdown', 'pointerup', 'click')
                 and _number(entry.get('startTime')) and _number(entry.get('duration'))
                 and entry['duration'] > 0 and _integer(entry.get('interactionId'))
                 and start - 1 <= entry['startTime'] <= after,
                 'Malformed or unrelated Event Timing entry')
        sanitized.append({k: entry[k] for k in ('name', 'startTime', 'duration', 'interactionId')})
    return {**{k: record[k] for k in keys}, 'event_timestamp': record['event_timestamp'],
            'pointerId': record['pointerId'], 'serial': record['serial'], 'kind': kind,
            'drawing_key': raw['drawing_key'], 'selected_handles': selected,
            'requested_handles': requested, 'expected_handles': expected,
            'highlight_geometry_count': record['highlight_geometry_count'],
            'oracle_independent': kind == 'marquee', 'event_entries': sanitized,
            'latency_ms': max([after - start] + [e['duration'] for e in sanitized])}


_INSTALL = """(canvas, token) => {
    if (window.__xbenchX08Selection) throw Error('Capture already active');
    const s = {token, canvas, entries: [], visible_history: [document.visibilityState],
        downs: 0, moves: 0, ups: 0, pointer: null, observer_disconnected: false};
    window.__xbenchX08Selection = s;
    s.paintable = () => {
        if (!canvas.isConnected || document.visibilityState !== 'visible') return false;
        const r = canvas.getBoundingClientRect();
        if (![r.left, r.top, r.right, r.bottom, r.width, r.height].every(Number.isFinite) ||
            r.width <= 0 || r.height <= 0 || r.right <= 0 || r.bottom <= 0 ||
            r.left >= window.innerWidth || r.top >= window.innerHeight) return false;
        for (let el = canvas; el; el = el.parentElement) {
            const style = getComputedStyle(el);
            if (style.display === 'none' || style.visibility !== 'visible' ||
                !Number.isFinite(Number(style.opacity)) || Number(style.opacity) <= 0) return false;
        }
        return true;
    };
    s.paintability = {baseline: s.paintable(), down: false, after: false};
    s.visibility = () => s.visible_history.push(document.visibilityState);
    s.down = e => {
        s.downs++;
        if (s.downs === 1) {
            s.paintability.down = s.paintable();
            s.pointer = {trusted: e.isTrusted, start_ms: performance.now(),
                event_timestamp: e.timeStamp, pointerId: e.pointerId};
        }
    };
    s.move = () => s.moves++;
    s.up = () => s.ups++;
    s.ingest = entries => {
        for (const e of entries) s.entries.push({name: e.name, startTime: e.startTime,
            duration: e.duration, interactionId: e.interactionId,
            target_matches: !!e.target && (e.target === canvas || canvas.contains(e.target))});
    };
    s.matchingEntries = record => s.entries.filter(e => e.target_matches &&
        ['pointerdown', 'pointerup', 'click'].includes(e.name) &&
        e.startTime >= s.pointer.start_ms - 1 && e.startTime <= record.after_frame_ms)
        .map(({target_matches, ...entry}) => entry);
    document.addEventListener('visibilitychange', s.visibility, true);
    canvas.addEventListener('pointerdown', s.down, true);
    canvas.addEventListener('pointermove', s.move, true);
    canvas.addEventListener('pointerup', s.up, true);
    s.host = canvas.closest('.viewer-canvas');
    const api = s.host && s.host.__leafViewerObservation;
    if (!api || typeof api.snapshot !== 'function') throw Error('Viewer API unavailable');
    s.api = api;
    s.readSnapshot = () => {
        if (canvas.closest('.viewer-canvas') !== s.host ||
            s.host.__leafViewerObservation !== s.api ||
            typeof s.api.snapshot !== 'function') throw Error('Viewer API lost or replaced');
        return s.api.snapshot();
    };
    if (!s.paintability.baseline) throw Error('Canvas baseline not paintable');
    const baseline = s.readSnapshot();
    if (!baseline || typeof baseline.drawingKey !== 'string' || !baseline.drawingKey.trim()
        || !Array.isArray(baseline.selections)) throw Error('No real rendered baseline');
    let serial = 0;
    for (const r of baseline.selections) {
        if (!Number.isInteger(r.serial) || r.serial <= serial) throw Error('Invalid history');
        serial = r.serial;
    }
    s.baseline_drawing_key = baseline.drawingKey;
    s.baseline_serial = serial;
    if (typeof PerformanceObserver === 'undefined' ||
        !PerformanceObserver.supportedEntryTypes.includes('event')) throw Error('Event Timing unavailable');
    s.po = new PerformanceObserver(list => s.ingest(list.getEntries()));
    s.po.observe({type: 'event', buffered: false, durationThreshold: 16});
    s.event_timing_supported = true;
    return true;
}"""

_READY = """token => {
    const s = window.__xbenchX08Selection;
    if (!s || s.token !== token) throw Error('Recorder unavailable');
    if (s.downs > 1) throw Error('Multiple selection pointerdowns');
    const snap = s.readSnapshot();
    if (!snap) return false;
    if (snap.drawingKey !== s.baseline_drawing_key) throw Error('Drawing changed');
    if (!s.pointer || !Array.isArray(snap.selections)) return false;
    const records = snap.selections.filter(r => r.serial > s.baseline_serial &&
        r.pointerId === s.pointer.pointerId &&
        Math.abs(r.event_timestamp - s.pointer.event_timestamp) <= 1);
    const record = records[records.length - 1];
    s.ingest(s.po.takeRecords());
    return !!record && s.matchingEntries(record).length > 0;
}"""

_SNAPSHOT = """token => {
    const s = window.__xbenchX08Selection;
    if (!s || s.token !== token || s.downs !== 1) throw Error('Invalid recorder');
    const snap = s.readSnapshot();
    if (!snap || snap.drawingKey !== s.baseline_drawing_key) throw Error('Drawing changed');
    s.paintability.after = s.paintable();
    const records = snap.selections.filter(r => r.serial > s.baseline_serial &&
        r.pointerId === s.pointer.pointerId &&
        Math.abs(r.event_timestamp - s.pointer.event_timestamp) <= 1);
    const record = records[records.length - 1];
    if (!record) throw Error('Fresh selection unavailable');
    s.ingest(s.po.takeRecords());
    const event_entries = s.matchingEntries(record);
    return {baseline_drawing_key: s.baseline_drawing_key, drawing_key: snap.drawingKey,
        baseline_serial: s.baseline_serial, record, pointer: s.pointer,
        event_timing_supported: s.event_timing_supported, event_entries,
        all_event_entries: s.entries, visible_history: s.visible_history,
        paintability: s.paintability,
        pointer_counts: {down: s.downs, move: s.moves, up: s.ups}};
}"""

_CLEANUP = """({token, installed}) => {
    const s = window.__xbenchX08Selection;
    // A failed install may have found an incumbent. Never touch its recorder.
    // After a successful install, a missing recorder makes cleanup unverifiable.
    if (!s || s.token !== token) return installed ? null :
        {recorder_removed: true, observer_disconnected: true};
    let observer_disconnected = !s.po;
    try {
        if (s.po) { s.po.disconnect(); observer_disconnected = true; }
    } finally {
        try {
            s.canvas.removeEventListener('pointerdown', s.down, true);
            s.canvas.removeEventListener('pointermove', s.move, true);
            s.canvas.removeEventListener('pointerup', s.up, true);
            document.removeEventListener('visibilitychange', s.visibility, true);
        } finally { delete window.__xbenchX08Selection; }
    }
    return {recorder_removed: !window.__xbenchX08Selection, observer_disconnected};
}"""


def collect_selection(page, canvas, action, *, expected_handles, kind, timeout_ms=10000):
    """Run one caller UI action and consume actual post-render evidence.

    Canvas must be a Locator that creates a temporary element handle. Only that
    handle is disposed; the caller owns page/browser and any direct ElementHandle.
    Event Timing threshold omissions refuse capture, including very fast events.
    """
    _require(callable(getattr(canvas, 'element_handle', None)), 'Canvas Locator required')
    token, element, raw, failure = str(uuid4()), None, None, None
    installed = False
    cleanup = None
    try:
        element = canvas.element_handle()
        _require(element is not None, 'Canvas element unavailable')
        expected_handles = _handles(expected_handles)
        _require(kind in ('click', 'marquee') and _number(timeout_ms) and timeout_ms > 0,
                 'Invalid capture arguments')
        _require(len(expected_handles) == 1 if kind == 'click' else
                 490 <= len(expected_handles) <= 510, 'Invalid expected selection size')
        _require(element.evaluate(_INSTALL, token) is True, 'Recorder installation unavailable')
        installed = True
        action()
        page.wait_for_function(_READY, arg=token, timeout=timeout_ms)
        raw = page.evaluate(_SNAPSHOT, token)
    except Exception as exc:
        failure = exc
    finally:
        try:
            cleanup = page.evaluate(_CLEANUP, {'token': token, 'installed': installed})
        except Exception as exc:
            failure = SelectionCaptureError('Recorder cleanup unavailable')
            failure.__cause__ = exc
        finally:
            disposed = False
            if element is not None:
                try:
                    element.dispose()
                    disposed = True
                except Exception as exc:
                    failure = SelectionCaptureError('Element disposal unavailable')
                    failure.__cause__ = exc
            if isinstance(cleanup, dict):
                cleanup = dict(cleanup, element_disposed=disposed)
    _require(isinstance(cleanup, dict) and all(cleanup.get(k) is True for k in
             ('recorder_removed', 'observer_disconnected', 'element_disposed')),
             'Capture cleanup unavailable')
    if failure is not None:
        raise SelectionCaptureError('Selection capture unavailable') from failure
    _require(isinstance(raw, dict), 'Selection snapshot unavailable')
    raw = deepcopy(raw)
    raw['cleanup'] = cleanup
    result = validate_selection(raw, expected_handles=expected_handles, kind=kind)
    result['raw_evidence'] = raw
    return result
