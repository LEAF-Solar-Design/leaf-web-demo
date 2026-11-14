"""Request-local, closed metadata for source author failures. Never grants authority."""
from contextvars import ContextVar

_INACTIVE = object()
_failure = ContextVar('project_author_failure', default=_INACTIVE)
_SUBPHASES = frozenset({
    'initialize_binding', 'initialize_authority', 'initialize_http',
    'initialize_transport', 'initialize_response',
    'initialization', 'missing_binding', 'author_http', 'author_response', 'transport',
})


def begin():
    return _failure.set(None)


def end(token):
    _failure.reset(token)


def record_failure(subphase, *, http_status=None):
    # No mutable shared value: copied async contexts and nested callers cannot
    # mutate the parent's payload. Only an explicitly scoped first failure wins.
    if _failure.get() is not None or type(subphase) is not str or subphase not in _SUBPHASES:
        return
    status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
    _failure.set((subphase, status))


def snapshot():
    value = _failure.get()
    if value is _INACTIVE or value is None:
        return {}
    subphase, status = value
    result = {'author_subphase': subphase}
    if status is not None:
        result['author_http_status'] = status
    return result
