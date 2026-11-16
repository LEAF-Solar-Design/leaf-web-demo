"""Scoped X08 collection. Report steps are serialized Step dictionaries.

Injected orchestration is diagnostic evidence only. Deadlines bound cooperative
waits and are checked around callbacks; Python callbacks cannot be preempted.
"""

from copy import deepcopy
from dataclasses import asdict
from math import isfinite
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
from uuid import uuid4

from xbench_core import Step, write_json_atomic
from benches.x08_selection import collect_selection, geometry_evidence, validate_selection
from benches.x08_ack import capture_job_ack, validate_ack
from benches.x08_frames import collect_frame_sequence
from benches.x08_frame_evidence import validate_frame_evidence
from benches.x08_metrics import score


LIMITS = ('supported live runtime/native binding not yet installed; injected or '
          'saved samples cannot qualify live evidence. Cooperative deadlines cannot '
          'preempt arbitrary Python callbacks.')


class RuntimeUnavailable(RuntimeError):
    """The supported live adapter is deliberately not bound."""


class DeadlineExceeded(RuntimeError):
    """A cooperative callback exceeded its collection budget."""


class CleanupUnavailable(RuntimeUnavailable):
    """Run qualification cannot survive unverifiable scoped cleanup."""


def _require(condition):
    if not condition:
        raise ValueError('Evidence unavailable or inconsistent')


def _positive(value):
    return type(value) in (int, float) and isfinite(value) and value > 0


def _report(sha):
    return dict(schema='leaf.xbench.x08.collection.v1', qualified_live_grade=False,
                run_id=str(uuid4()), expected_source_sha=sha, source_before=None,
                source_after=None, cold={}, warm={}, errors=[], cleanup={},
                samples={}, cold_run_discarded=False)


class _AckObserver:
    """Remove only this capture's listeners, even when installation fails."""

    def __init__(self, page):
        self.page = page
        self.requests, self.responses, self.installed = [], [], []

    @staticmethod
    def matches(request):
        return (request.method == 'POST' and
                urlsplit(request.url).path == '/api/run')

    def request(self, request):
        if self.matches(request):
            self.requests.append(request)

    def response(self, response):
        if self.matches(response.request):
            self.responses.append(response)

    def __enter__(self):
        _require(callable(getattr(self.page, 'on', None)) and
                 callable(getattr(self.page, 'remove_listener', None)))
        try:
            for event, callback in [('request', self.request), ('response', self.response)]:
                self.page.on(event, callback)
                self.installed.append((event, callback))
        except BaseException as exc:
            self.__exit__(type(exc), exc, exc.__traceback__)
            raise
        return self

    def __exit__(self, exc_type, exc, traceback):
        failure = None
        for event, callback in self.installed:
            try:
                self.page.remove_listener(event, callback)
            except Exception as cleanup_exc:
                failure = cleanup_exc
        if failure is not None:
            if exc is not None and not isinstance(exc, Exception):
                # Listener cleanup must never replace a cancellation signal.
                exc.x08_observer_cleanup_failed = True
                return False
            raise CleanupUnavailable('Scoped observer cleanup unavailable') from failure


def orchestrate(runtime, *, expected_source_sha, expected_module_handles,
                expected_window_handles, clock=time.monotonic, total_timeout_s=240,
                phase_timeout_s=30, selection_collector=collect_selection,
                ack_collector=capture_job_ack, frame_collector=collect_frame_sequence):
    """Collect cold then warm evidence; always release only runtime-owned resources.

    Cancellation is re-raised with ``x08_partial_report`` after final health and
    close. Inputs and all collector snapshots remain caller-owned and unmodified.
    """
    report = _report(expected_source_sha)
    affected = set()
    cancelled = None
    deadline = phase_deadline = None
    modules, windows = [], []
    serials, pointers, responses, jobs = {}, set(), [], set()
    last_selection_end = -1
    last_ack_paint = -1
    drawing_key = None

    def error(phase, step, exc):
        report['errors'].append(dict(phase=phase, step=step, type=type(exc).__name__))
        affected.update(step if isinstance(step, list) else [step])

    def teardown_clock():
        nonlocal cancelled
        try:
            return clock()
        except BaseException as exc:
            error('teardown.clock', list(range(1, 7)), exc)
            if not isinstance(exc, Exception):
                cancelled = cancelled or exc
            return None

    def remaining():
        now = clock()
        _require(type(now) in (int, float) and isfinite(now))
        left = min(deadline, phase_deadline) - now
        if left <= 0:
            raise DeadlineExceeded()
        return left

    def call(callback, *args, retain=None, **kwargs):
        remaining()
        result = callback(*args, **kwargs)
        if retain is not None:
            retain(deepcopy(result))
        remaining()
        return result

    def timeout():
        value = remaining() * 1000
        setter = getattr(runtime.page, 'set_default_timeout', None)
        if callable(setter):
            call(setter, value)
        return remaining() * 1000

    def stage(phase, name, step, operation):
        nonlocal phase_deadline
        phase_deadline = min(deadline, clock() + phase_timeout_s)
        try:
            call(operation)
            return True
        except Exception as exc:
            error(phase + '.' + name,
                  list(range(1, 7)) if isinstance(exc, CleanupUnavailable) else step, exc)
            return False

    def health(key, final=False):
        # Final observation is attempted even after budget exhaustion/cancellation.
        if final:
            report[key] = runtime.read_health()
        else:
            call(runtime.read_health, retain=lambda value: report.update({key: value}))
        _require(isinstance(report[key], str) and
                 re.fullmatch('[0-9a-f]{40}', report[key]) is not None and
                 report[key] == expected_source_sha)

    def selection(phase, name, index, handles, kind, actor):
        nonlocal last_selection_end
        action = call(actor, index)
        _require(callable(action))
        row = call(selection_collector, runtime.page, runtime.canvas,
                   lambda: call(action), expected_handles=handles, kind=kind,
                   timeout_ms=timeout(), retain=report[phase][name].append)
        raw = row.get('raw_evidence')
        checked = validate_selection(raw, expected_handles=handles, kind=kind)
        _require(all(row.get(key) == value for key, value in checked.items()))
        key = checked['drawing_key']
        identity = (key, raw['pointer']['event_timestamp'], raw['pointer']['pointerId'])
        _require(key == drawing_key and identity not in pointers and
                 checked['serial'] > serials.get(key, 0) and
                 checked['start_ms'] > last_selection_end)
        pointers.add(identity)
        serials[key] = checked['serial']
        last_selection_end = checked['after_frame_ms']
        return deepcopy(checked)

    def ack(phase, index):
        nonlocal last_ack_paint
        control = call(runtime.ack_control, index)
        with _AckObserver(runtime.page) as observer:
            row = call(ack_collector, runtime.page, control, timeout_ms=timeout(),
                       retain=report[phase]['ack'].append)
        _require(len(observer.requests) == len(observer.responses) == 1)
        response = observer.responses[0]
        _require(response.request is observer.requests[0] and
                 all(response is not previous for previous in responses))
        body = call(response.json)
        checked = validate_ack(row, response_status=response.status, response_body=body)
        _require(all(row.get(key) == value for key, value in checked.items()) and
                 row['job_id'] not in jobs and row['pointer_ms'] > last_ack_paint)
        jobs.add(row['job_id'])
        responses.append(response)
        last_ack_paint = row['paint_ms']
        return deepcopy(checked)

    try:
        _require(_positive(total_timeout_s) and _positive(phase_timeout_s))
        started = clock()
        _require(type(started) in (int, float) and isfinite(started))
        deadline = started + total_timeout_s
        phase_deadline = min(deadline, started + phase_timeout_s)
        _require(isfinite(deadline) and isinstance(expected_source_sha, str) and
                 re.fullmatch('[0-9a-f]{40}', expected_source_sha) is not None)
        modules, windows = list(expected_module_handles), list(expected_window_handles)
        _require(bool(modules) and all(isinstance(h, str) and h.strip() for h in modules)
                 and len(set(modules)) == len(modules) and 490 <= len(windows) <= 510
                 and all(isinstance(h, str) and h.strip() for h in windows)
                 and len(set(windows)) == len(windows) and set(windows) <= set(modules))
        health('source_before')

        def geometry():
            nonlocal drawing_key
            snapshot = call(runtime.geometry_snapshot,
                            retain=lambda value: report.update(geometry_snapshot=value))
            evidence = geometry_evidence(snapshot, modules)
            drawing_key = snapshot['drawingKey']
            report['samples']['geometry'] = evidence['geometry']
            report['geometry_evidence'] = evidence

        geometry_ok = stage('setup', 'geometry', 1, geometry)
        stage('setup', 'reference', 6, lambda: call(
            runtime.reference, retain=lambda value: report['samples'].update(reference=value)))
        for phase in ('cold', 'warm'):
            raw = report[phase]
            raw.update(single=[], window=[], ack=[], frame=None, frame_cleanup=None,
                       frame_window=[], valid_single=[], valid_window=[], valid_ack=[])

            def singles():
                _require(geometry_ok)
                for index in range(20):
                    raw['valid_single'].append(selection(
                        phase, 'single', index, [modules[index % len(modules)]],
                        'click', runtime.single_action))

            def window_selections():
                _require(geometry_ok)
                for index in range(5):
                    raw['valid_window'].append(selection(
                        phase, 'window', index, windows, 'marquee', runtime.window_action))

            def acknowledgements():
                for index in range(10):
                    raw['valid_ack'].append(ack(phase, index))

            def frames():
                _require(geometry_ok and remaining() > 10)
                timeout()
                validated_window = []

                def window():
                    validated_window.append(selection(
                        phase, 'frame_window', 5, windows, 'marquee', runtime.window_action))

                failure = None
                try:
                    snapshot = call(frame_collector, runtime.page, runtime.canvas, window,
                                    duration_ms=10000,
                                    retain=lambda value: raw.update(frame=value))
                except BaseException as exc:
                    failure = exc
                finally:
                    # Observe cleanup even when the helper failed or ran out of time.
                    try:
                        raw['frame_cleanup'] = deepcopy(runtime.frame_cleanup())
                    except BaseException as exc:
                        error(phase + '.frame_cleanup', list(range(1, 7)), exc)
                        if failure is None or isinstance(failure, Exception):
                            failure = exc
                if failure is not None:
                    raise failure
                remaining()
                cleanup = raw['frame_cleanup']
                if not (isinstance(cleanup, dict) and all(cleanup.get(key) is True for key in
                        ('recorder_removed', 'element_disposed', 'mouse_released'))):
                    raise CleanupUnavailable('Frame cleanup unavailable')
                _require(len(validated_window) == 1)
                raw['valid_frame'] = validate_frame_evidence(snapshot, cleanup=cleanup)

            complete = [stage(phase, name, step, operation) for name, step, operation in
                        [('single', 2, singles), ('window', 3, window_selections),
                         ('ack', 4, acknowledgements), ('frame', 5, frames)]]
            raw['complete'] = all(complete)
            if phase == 'cold':
                report['cold_run_discarded'] = raw['complete']
            else:
                report['samples'].update(
                    single_select_ms=[r['latency_ms'] for r in raw['valid_single']],
                    window_select=deepcopy(raw['valid_window']),
                    job_ack=deepcopy(raw['valid_ack']))
                report['samples'].update(deepcopy(raw.get('valid_frame', {})))
    except Exception as exc:
        error('setup', list(range(1, 7)), exc)
    except BaseException as exc:
        cancelled = exc
        error('cancelled', list(range(1, 7)), exc)
    finally:
        final_started = teardown_clock()
        try:
            health('source_after', final=True)
            finished = clock()
            if deadline is not None and (not isfinite(finished) or
                    finished >= deadline or final_started is None or
                    finished - final_started >= phase_timeout_s):
                raise DeadlineExceeded()
        except Exception as exc:
            error('source_after', list(range(1, 7)), exc)
        except BaseException as exc:
            cancelled = cancelled or exc
            error('source_after', list(range(1, 7)), exc)
        finally:
            close_started = teardown_clock()
            try:
                runtime.close()
                report['cleanup']['closed'] = True
            except BaseException as exc:
                report['cleanup']['closed'] = False
                error('cleanup', list(range(1, 7)), exc)
                if not isinstance(exc, Exception):
                    cancelled = cancelled or exc
            else:
                try:
                    finished = clock()
                    if deadline is not None and (not isfinite(finished) or
                            finished >= deadline or close_started is None or
                            finished - close_started >= phase_timeout_s):
                        raise DeadlineExceeded()
                except Exception as exc:
                    error('cleanup.deadline', list(range(1, 7)), exc)
                except BaseException as exc:
                    cancelled = cancelled or exc
                    error('cleanup.deadline', list(range(1, 7)), exc)
        warm = report['warm']
        # Completed predecessors remain usable diagnostics after cancellation.
        if warm:
            report['samples'].update(
                single_select_ms=[r['latency_ms'] for r in warm['valid_single']],
                window_select=deepcopy(warm['valid_window']),
                job_ack=deepcopy(warm['valid_ack']))
            report['samples'].update(deepcopy(warm.get('valid_frame', {})))
        report['samples']['cold_run_discarded'] = report['cold_run_discarded']
        rows = score(report['samples'], len(modules))
        report['steps'] = [asdict(Step(row.n, 'UNREADABLE', row.evidence,
                                     row.observed, row.expected) if row.n in affected else row)
                           for row in rows]
    if cancelled is not None:
        cancelled.x08_partial_report = report
        raise cancelled
    return report


def live_runtime(ctx):
    """No manifest sample path, callable or injected runtime is admitted here."""
    raise RuntimeUnavailable('supported live runtime/native binding not yet installed')


def run(ctx):
    """Shared entrypoint stays UNREADABLE until a supported adapter is built."""
    report = _report(ctx.source_sha)
    report['source_sha'] = ctx.source_sha
    report['limitation'] = LIMITS
    try:
        live_runtime(ctx)
    except RuntimeUnavailable:
        report['errors'].append(dict(phase='adapter', step=list(range(1, 7)),
                                     type='RuntimeUnavailable'))
    report['steps'] = [asdict(row) for row in score({}, 0)]
    destination = Path(ctx.out_dir) / 'samples.json'
    write_json_atomic(destination, report)
    return [Step(**row) for row in report['steps']]
