"""Synthetic evidence validation; no browser, network, or product fixtures."""

from types import SimpleNamespace

import pytest

from benches.x08_ack import AckCaptureError, capture_job_ack, validate_ack


JOB = 'e4198b40-89c4-4cb2-bcc7-3278dcf6ffeb'


def evidence(**changes):
    raw = {'pointer_ms': 100, 'response_end_ms': 160, 'paint_ms': 180,
           'resource_matches': 1, 'queued_painted': True}
    raw.update(changes)
    return raw


def validate(raw, status=202, body=None):
    return validate_ack(raw, response_status=status,
                        response_body={'job_id': JOB, 'status': 'submitted'}
                        if body is None else body)


def test_valid_marker_timeline_and_conservative_event_max():
    result = validate(evidence(event_durations_ms=[96, 32], ignored='private'))
    assert result['latency_ms'] == 96
    assert result['job_id'] == JOB
    assert result['http_status'] == 202
    assert result['rendered'] is True
    assert result['pointer_ms'] == 100
    assert result['response_end_ms'] == 160
    assert result['paint_ms'] == 180
    assert 'ignored' not in result
    assert 'PASS' not in result.values()
    assert validate(evidence())['latency_ms'] == 80


@pytest.mark.parametrize('changes', [
    {'queued_painted': False},  # An optimistic running strip cannot acknowledge.
    {'queued_painted': 1}, {'pointer_ms': None}, {'response_end_ms': None},
    {'resource_matches': 0}, {'resource_matches': 2}, {'resource_matches': True},
    {'paint_ms': 150}, {'pointer_ms': 170}, {'event_durations_ms': [-1]},
    {'event_durations_ms': [True]}, {'event_durations_ms': [float('nan')]},
    {'event_durations_ms': None}, {'paint_ms': 100, 'response_end_ms': 100},
])
def test_missing_or_mismatched_evidence_refuses(changes):
    with pytest.raises(AckCaptureError):
        validate(evidence(**changes))


@pytest.mark.parametrize('field', ['pointer_ms', 'response_end_ms', 'paint_ms'])
@pytest.mark.parametrize('value', [True, False, 0, -1, float('nan'),
                                  float('inf'), -float('inf'), '100'])
def test_invalid_timeline_values_refuse(field, value):
    with pytest.raises(AckCaptureError):
        validate(evidence(**{field: value}))


@pytest.mark.parametrize('status', [200, True, 202.0, None])
def test_actual_202_is_required(status):
    with pytest.raises(AckCaptureError):
        validate(evidence(), status=status)


@pytest.mark.parametrize('body', [
    {}, {'job_id': JOB}, {'job_id': JOB, 'status': 'running'},
    {'job_id': 'not-a-uuid', 'status': 'submitted'},
    {'job_id': 123, 'status': 'submitted'},
    {'job_id': '00000000-0000-0000-0000-000000000000', 'status': 'submitted'},
])
def test_submitted_uuid_is_required(body):
    with pytest.raises(AckCaptureError):
        validate(evidence(), body=body)


def test_event_entries_are_sanitized_and_contribute_to_latency():
    entry = {'name': 'click', 'startTime': 105, 'duration': 112,
             'interactionId': 7, 'target': 'excluded'}
    result = validate(evidence(event_entries=[entry]))
    assert result['latency_ms'] == 112
    assert 'target' not in result['event_entries'][0]
    entry['startTime'] = 99
    with pytest.raises(AckCaptureError):
        validate(evidence(event_entries=[entry]))


class PendingResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class Page:
    def __init__(self, cleanup_error=False):
        self.url = 'https://studio.example/project'
        self.cleaned = []
        self.cleanup_error = cleanup_error

    def expect_response(self, predicate, *, timeout):
        assert 0 < timeout <= 15000
        return PendingResponse()

    def evaluate(self, script, token):
        self.cleaned.append(token)
        if self.cleanup_error:
            raise RuntimeError('cleanup failed')


class Control:
    def evaluate(self, script, token):
        self.token = token
        return True

    def click(self, *, timeout):
        raise RuntimeError('click failed')


@pytest.mark.parametrize('cleanup_error', [False, True])
def test_click_failure_always_attempts_scoped_cleanup_and_keeps_original(cleanup_error):
    page, control = Page(cleanup_error), Control()
    with pytest.raises(RuntimeError, match='click failed'):
        capture_job_ack(page, control)
    assert page.cleaned == [control.token]


@pytest.mark.parametrize('cleanup_error', [False, True])
def test_unpaintable_queued_timeout_cleans_up_and_preserves_capture_error(cleanup_error):
    class WaitingPage(Page):
        def expect_response(self, predicate, *, timeout):
            pending = PendingResponse()
            pending.value = SimpleNamespace(
                url='https://studio.example/api/run', status=202,
                request=SimpleNamespace(method='POST'),
                json=lambda: {'job_id': JOB, 'status': 'submitted'})
            return pending

        def wait_for_function(self, script, *, arg, timeout):
            # A queued node that never becomes paintable cannot yield evidence.
            raise TimeoutError('queued strip stayed transparent')

        def evaluate(self, script, arg):
            assert isinstance(arg, str), 'Timed-out evidence must not be sampled'
            return super().evaluate(script, arg)

    class ClickControl(Control):
        def click(self, *, timeout):
            pass

    page, control = WaitingPage(cleanup_error), ClickControl()
    with pytest.raises(AckCaptureError, match='Queued paint/resource evidence unavailable') as exc:
        capture_job_ack(page, control)
    assert isinstance(exc.value.__cause__, TimeoutError)
    assert page.cleaned == [control.token]


def test_capture_uses_actual_post_response_body_before_timing_and_cleans_up():
    calls = []

    def body():
        calls.append('body')
        return {'job_id': JOB, 'status': 'submitted', 'private': 'excluded'}

    response = SimpleNamespace(url='https://studio.example/api/run?project=1',
                               request=SimpleNamespace(method='POST'),
                               status=202, json=body)

    class CapturePage(Page):
        def expect_response(self, predicate, *, timeout):
            assert predicate(response)
            assert not predicate(SimpleNamespace(url=response.url,
                                                request=SimpleNamespace(method='GET')))
            assert not predicate(SimpleNamespace(url=response.url.replace('/api/run', '/api/run/extra'),
                                                request=response.request))
            pending = PendingResponse()
            pending.value = response
            return pending

        def wait_for_function(self, script, *, arg, timeout):
            assert calls == ['click', 'body']
            assert arg == {'token': control.token, 'url': response.url}
            calls.append('wait')

        def evaluate(self, script, arg):
            if isinstance(arg, dict):
                assert calls[-1] == 'wait'
                return evidence()
            return super().evaluate(script, arg)

    class ClickControl(Control):
        def click(self, *, timeout):
            calls.append('click')

    page, control = CapturePage(), ClickControl()
    result = capture_job_ack(page, control)
    assert result['latency_ms'] == 80
    assert 'private' not in result
    assert page.cleaned == [control.token]


@pytest.mark.parametrize('url', [
    'https://foreign.example/api/run', 'https://studio.example.evil/api/run',
    'http://studio.example/api/run', 'https://studio.example:444/api/run',
    'https://studio.example/api/run/extra',
])
def test_capture_rejects_foreign_and_lookalike_response_origins(url):
    class PredicatePage(Page):
        def expect_response(self, predicate, *, timeout):
            request = SimpleNamespace(method='POST')
            assert predicate(SimpleNamespace(request=request,
                                             url='https://studio.example:443/api/run'))
            assert not predicate(SimpleNamespace(request=request, url=url))
            raise TimeoutError()

    page, control = PredicatePage(), Control()
    with pytest.raises(AckCaptureError, match='Actual run response unavailable'):
        capture_job_ack(page, control, clock=lambda: 0)
    assert page.cleaned == [control.token]


@pytest.mark.parametrize('url', [None, '', 'about:blank', 'https://', 'https://host:bad'])
def test_capture_requires_readable_page_origin(url):
    page, control = Page(), Control()
    page.url = url
    with pytest.raises(AckCaptureError, match='origin'):
        capture_job_ack(page, control, clock=lambda: 0)
    assert not page.cleaned
    assert not hasattr(control, 'token')


@pytest.mark.parametrize('origin', ['http://localhost:5173', 'https://staging.example'])
def test_capture_accepts_actual_staging_origin(origin):
    class StagingPage(Page):
        def expect_response(self, predicate, *, timeout):
            response = SimpleNamespace(url=origin + '/api/run?project=synthetic',
                                       request=SimpleNamespace(method='POST'), status=202,
                                       json=lambda: {'job_id': JOB, 'status': 'submitted'})
            assert predicate(response)
            pending = PendingResponse()
            pending.value = response
            return pending

        def wait_for_function(self, script, *, arg, timeout):
            pass

        def evaluate(self, script, arg):
            if isinstance(arg, dict):
                return evidence()
            return super().evaluate(script, arg)

    class ClickControl(Control):
        def click(self, *, timeout):
            pass

    page, control = StagingPage(), ClickControl()
    page.url = origin + '/studio'
    assert capture_job_ack(page, control, clock=lambda: 0)['http_status'] == 202
    assert page.cleaned == [control.token]


@pytest.mark.parametrize('budget', [True, 0, -1, float('nan'), float('inf'), 10 ** 400])
def test_capture_rejects_invalid_timeout_before_install(budget):
    control = Control()
    with pytest.raises(AckCaptureError):
        capture_job_ack(Page(), control, timeout_ms=budget, clock=lambda: 0)
    assert not hasattr(control, 'token')


@pytest.mark.parametrize('overrun', [None, 'install', 'click', 'response', 'body', 'paint', 'snapshot'])
def test_one_ack_budget_and_cooperative_overrun_cleanup(overrun):
    now, waits, sampled = [0.0], [], []

    def advance(stage):
        now[0] += 20 if overrun == stage else 1

    def body():
        advance('body')
        return {'job_id': JOB, 'status': 'submitted'}

    response = SimpleNamespace(url='https://studio.example/api/run',
                               request=SimpleNamespace(method='POST'), status=202, json=body)

    class BudgetPending(PendingResponse):
        @property
        def value(self):
            advance('response')
            return response

    class BudgetPage(Page):
        def expect_response(self, predicate, *, timeout):
            assert predicate(response)
            waits.append(('response', timeout))
            return BudgetPending()

        def wait_for_function(self, script, *, arg, timeout):
            waits.append(('paint', timeout))
            advance('paint')

        def evaluate(self, script, arg):
            if isinstance(arg, dict):
                sampled.append(True)
                advance('snapshot')
                return evidence()
            return super().evaluate(script, arg)

    class BudgetControl(Control):
        def evaluate(self, script, token):
            advance('install')
            return super().evaluate(script, token)

        def click(self, *, timeout):
            waits.append(('click', timeout))
            advance('click')

    page, control = BudgetPage(), BudgetControl()
    if overrun is None:
        assert capture_job_ack(page, control, clock=lambda: now[0])['latency_ms'] == 80
        assert waits == [('response', 14000), ('click', 14000), ('paint', 11000)]
    else:
        with pytest.raises(AckCaptureError):
            capture_job_ack(page, control, clock=lambda: now[0])
        if overrun != 'snapshot':
            assert sampled == []
    assert page.cleaned == [control.token]


def test_ack_cancellation_still_cleans_up():
    class CancelControl(Control):
        def click(self, *, timeout):
            raise KeyboardInterrupt()

    page, control = Page(), CancelControl()
    with pytest.raises(KeyboardInterrupt):
        capture_job_ack(page, control, clock=lambda: 0)
    assert page.cleaned == [control.token]
