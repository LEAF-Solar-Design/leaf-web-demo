"""Request-local X02 staging fault mode, copied into durable job identity.

No provider state, shared transport patch or credential material is retained.
"""
from contextlib import contextmanager
from contextvars import ContextVar

import requests
import deployment_identity
import runtime_posture
from solar_design_graph import GraphValidationError

HEADER = "X-XBench-Solver-Fault"
MODES = frozenset({"upstream-400", "timeout"})
_mode = ContextVar("x02_solver_fault", default=None)


def validate(mode):
    if mode is None:
        return None
    if type(mode) is not str or mode not in MODES:
        raise GraphValidationError("XBENCH_SOLVER_FAULT_INVALID")
    try:
        staging = (runtime_posture._deployed_runtime()
                   and not runtime_posture._production_runtime()
                   and deployment_identity.deployment_identity()["environment"] == "staging")
    except (ValueError, KeyError, TypeError):
        staging = False
    if not staging:
        raise GraphValidationError("XBENCH_SOLVER_FAULT_UNAVAILABLE")
    return mode


def current_mode():
    return validate(_mode.get())


@contextmanager
def request_mode(mode):
    token = _mode.set(validate(mode))
    try:
        yield
    finally:
        _mode.reset(token)


class Transport:
    """A per-call transport stub, reached only after durable begin_send.

    Timeout fires the transport deadline deterministically without sleeping or
    opening a socket. The normal cloud adapter classifies both stub outcomes.
    """
    def __init__(self, mode):
        self.mode = validate(mode)
        if self.mode is None:
            raise GraphValidationError("XBENCH_SOLVER_FAULT_INVALID")

    def post(self, *args, **kwargs):
        validate(self.mode)  # Recheck the worker posture at the send boundary.
        if self.mode == "timeout":
            raise requests.Timeout("X02 staging solver deadline expired")
        response = requests.Response()
        response.status_code = 400
        response._content = b""
        response._content_consumed = True
        return response
