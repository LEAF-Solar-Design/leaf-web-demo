"""Dormant composition for trusted callers; no installed lifecycle or identity."""
from pathlib import Path
import signal
import threading

import native_preview_service as service
import native_preview_session as session

ERROR_CODES = frozenset({'main-thread-required', 'invalid-input', 'setup-failed',
                         'serve-failed', 'invalid-result', 'signal-restore-failed'})


class HostError(RuntimeError):
    def __init__(self, code):
        super().__init__(code if type(code) is str and code in ERROR_CODES else 'setup-failed')


def run_host(scope, credential_resolver, audience, output_root, *, poll_seconds=5.0,
             error_seconds=30.0):
    """Serve serial renewable sessions, letting active work finish on signals."""
    if threading.current_thread() is not threading.main_thread():
        raise HostError('main-thread-required')
    prior = []
    code = 'invalid-input'
    result = None
    restore_failed = False
    try:
        scope = service.worker.binding(scope)
        if not callable(credential_resolver):
            raise HostError('invalid-input')
        if type(audience) is not str or audience not in session.AUDIENCES:
            raise HostError('invalid-input')
        output_root = Path(output_root)
        poll_seconds = service._wait_setting(poll_seconds)
        error_seconds = service._wait_setting(error_seconds)
        code = 'setup-failed'
        factory = session.make_session_factory(credential_resolver, audience)
        stop = threading.Event()

        def stop_on_signal(_signum, _frame):
            stop.set()

        for signum in (signal.SIGINT, signal.SIGTERM):
            prior.append((signum, signal.getsignal(signum)))
            signal.signal(signum, stop_on_signal)
        code = 'serve-failed'
        value = service.serve(scope, factory, output_root, stop,
                              poll_seconds=poll_seconds, error_seconds=error_seconds)
        code = 'invalid-result'
        if (type(value) is not dict or set(value) != {'state', 'completed'}
                or type(value['state']) is not str
                or value['state'] not in ('stopped', 'busy', 'reconciliation-required')
                or type(value['completed']) is not int or value['completed'] < 0):
            raise HostError('invalid-result')
        result = {'state': value['state'], 'completed': value['completed']}
    except BaseException:
        pass
    finally:
        for signum, handler in reversed(prior):
            try:
                signal.signal(signum, handler)
            except BaseException:
                restore_failed = True
    # Raise outside exception handlers: no arbitrary payload survives as context.
    if restore_failed:
        raise HostError('signal-restore-failed')
    if result is None:
        raise HostError(code)
    return result
