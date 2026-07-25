"""Two classes of SILENTLY-SWALLOWED backend failure are now observable.

A swallowed error with no visible symptom is the worst kind of bug: the system
is broken and nothing anywhere says so. Both holes closed here were exactly
that shape.

  1. server/jobs.py ``_reaper_loop`` caught broad ``Exception`` and did a bare
     ``pass``. Every sweep failure was discarded with ZERO logging, so the
     orphan reaper could be failing on every single interval indefinitely and
     no log line, metric, or alert would ever report it.

  2. server/envelopes.py ``install_error_handlers`` registered handlers for
     ``RequestValidationError`` and ``HTTPException`` but had NO generic
     ``Exception`` handler. An unhandled exception in any route not
     individually guarded escaped as a RAW framework 500: no section-10
     ``{ok, error, degraded_mode}`` body for the client to parse, and no
     consistent server-side log.

Both fixes ADD OBSERVABILITY WITHOUT CHANGING CONTROL FLOW. The reaper still
swallows and retries next interval (the daemon must survive); routes still
fail. The difference is that the failure is now visible.

Two follow-ups from the sol-critic review of PR #130 extend that same seam --
both about making the signal USABLE, not just present:

  1. The reaper's failure logging is RATE-LIMITED. At REAPER_INTERVAL_S=10 a
     permanently failing sweep emitted a full traceback every 10s: 360/hour per
     process into CloudWatch, all one fault. Now the first failure of a streak
     logs in full, repeats collapse into a terse counted reminder per quiet
     window, a NEW exception type always logs in full, and recovery is
     announced once. Control flow is untouched.

  2. The unhandled-exception envelope carries a CORRELATION ID. The body
     deliberately withholds the failure detail, so an operator handed
     "internal server error" by a user previously had no way to find the
     matching traceback. The same opaque random ID now appears in both.

Run:  cd server && python -m pytest tests/test_swallowed_failures.py -q
"""
from __future__ import annotations

# Cache the STDLIB `platform` module BEFORE anything puts PROJECT_ROOT on sys.path
# (the local `platform/` package otherwise shadows it; mirrors tests/test_job_lanes.py).
import platform as _stdlib_platform  # noqa: E402
_stdlib_platform.python_implementation()

import json  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

SERVER_DIR = Path(__file__).resolve().parent.parent

# route the jobs SQLite DB to a throwaway dir BEFORE `jobs` is imported anywhere
# (jobs.py reads JOBS_DB at import time; mirrors tests/test_job_dwg_version_persist.py).
_DB_PATH = Path(tempfile.mkdtemp(prefix="swallowed-jobs-")) / "jobs.db"
os.environ.setdefault("JOBS_DB", str(_DB_PATH))

if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import jobs  # noqa: E402
from envelopes import ErrorCode, err_envelope, install_error_handlers  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_reaper_log_state():
    """Every test starts from fresh-process throttle bookkeeping.

    The throttle is deliberately module-level state (a real process must
    remember a streak ACROSS sweeps), so without this reset a failing test
    would leak its streak into the next one -- e.g. leaving `consecutive` > 0
    makes the very next successful sweep emit a recovery line and break
    test_reaper_sweep_is_quiet_on_the_success_path.
    """
    jobs._reset_reaper_failure_state()
    yield
    jobs._reset_reaper_failure_state()


# --------------------------------------------------------------------------- #
# 1. the orphan reaper logs its sweep failures instead of discarding them
# --------------------------------------------------------------------------- #
_SWEEP_BOOM = "reaper-sweep-exploded-marker"


def test_reaper_sweep_logs_the_exception_and_never_propagates(caplog, monkeypatch):
    """A failing sweep is LOGGED at error level and swallowed.

    Both halves matter. Logged: the previous bare ``pass`` is what made an
    every-interval reaper failure invisible. Swallowed: the daemon thread has
    no supervisor, so a propagating exception would kill the reaper outright
    and orphaned jobs would stop being reclaimed for the life of the process.
    """
    def _boom() -> int:
        raise RuntimeError(_SWEEP_BOOM)

    monkeypatch.setattr(jobs, "_reap_orphans_once", _boom)

    with caplog.at_level(logging.ERROR, logger=jobs.logger.name):
        jobs._reaper_sweep_once()          # must NOT raise

    records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert records, "a failing reaper sweep logged nothing at warning or above"

    record = records[-1]
    assert record.levelno >= logging.ERROR
    # the message carries the exception detail, not just a generic "sweep failed"
    assert _SWEEP_BOOM in record.getMessage()
    # logger.exception() attaches the traceback, which is what an operator needs
    assert record.exc_info is not None


def test_reaper_sweep_is_quiet_on_the_success_path(caplog, monkeypatch):
    """No log noise when the sweep works, because otherwise the signal is worthless."""
    monkeypatch.setattr(jobs, "_reap_orphans_once", lambda: 0)

    with caplog.at_level(logging.WARNING, logger=jobs.logger.name):
        jobs._reaper_sweep_once()

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_reaper_loop_runs_the_sweep_and_lets_baseexception_escape(monkeypatch):
    """One loop iteration runs one sweep, and a BaseException still escapes.

    The guard is `except Exception`, NOT a bare `except:`, so KeyboardInterrupt
    and SystemExit still tear the thread down instead of being swallowed into an
    infinite retry loop on shutdown.
    """
    calls: list[int] = []
    monkeypatch.setattr(jobs.time, "sleep", lambda _s: None)

    # run exactly one iteration, then break out the way a daemon never would
    def _stop_after_one() -> int:
        calls.append(1)
        raise KeyboardInterrupt

    monkeypatch.setattr(jobs, "_reap_orphans_once", _stop_after_one)
    with pytest.raises(KeyboardInterrupt):
        jobs._reaper_loop()

    assert calls == [1], "the loop ran the sweep exactly once per iteration"


# --------------------------------------------------------------------------- #
# 1b. that failure logging is RATE-LIMITED (sol-critic PR #130, follow-up 1)
#
# The fix in (1) traded a silent failure for a loud one: at REAPER_INTERVAL_S=10
# a permanently failing sweep shipped 6 full tracebacks a minute, 8,640 a day per
# process, to CloudWatch. These tests pin the throttle that bounds the VOLUME and
# the three things that must still escape it -- first failure, new exception
# type, recovery -- plus the control-flow guarantee that survives both.
# --------------------------------------------------------------------------- #
def _warnings(caplog) -> list:
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


def _always_boom() -> int:
    raise RuntimeError(_SWEEP_BOOM)


def test_a_permanently_failing_sweep_logs_once_not_once_per_interval(caplog, monkeypatch):
    """The headline: 60 consecutive failures produce ONE log line, not 60.

    Asserted against the SHIPPED default window (env unset), because the default
    is what a deployed process actually runs.
    """
    monkeypatch.delenv("REAPER_LOG_THROTTLE_S", raising=False)
    monkeypatch.setattr(jobs, "_reap_orphans_once", _always_boom)

    with caplog.at_level(logging.ERROR, logger=jobs.logger.name):
        for _ in range(60):
            jobs._reaper_sweep_once()          # none of these may raise

    records = _warnings(caplog)
    assert len(records) == 1, (
        f"60 identical failures inside one quiet window emitted {len(records)} log "
        "lines; the throttle is not holding")
    assert records[0].exc_info is not None, "the one surviving line is the full traceback"
    assert _SWEEP_BOOM in records[0].getMessage()


def test_throttled_failures_are_still_swallowed_and_still_retried(monkeypatch):
    """Control flow is NOT throttled: every interval still runs its sweep.

    The one thing this follow-up must not do is turn "log less" into "reap less".
    A sweep whose failure was suppressed still ran, still swallowed, and the next
    interval still tries again.
    """
    monkeypatch.delenv("REAPER_LOG_THROTTLE_S", raising=False)
    calls: list[int] = []

    def _boom() -> int:
        calls.append(1)
        raise RuntimeError(_SWEEP_BOOM)

    monkeypatch.setattr(jobs, "_reap_orphans_once", _boom)
    for _ in range(25):
        jobs._reaper_sweep_once()              # must NOT raise, ever

    assert len(calls) == 25, "a suppressed log line must not suppress the sweep itself"


def test_the_throttled_reminder_is_terse_and_carries_the_running_count(caplog, monkeypatch):
    """Once the quiet window lapses, the reminder says how bad it is.

    A count is what makes a terse line actionable: "still failing, 4 consecutive"
    distinguishes a permanent fault from a one-off blip without a traceback.
    Window 0 = every failure is immediately due, which is how this reaches the
    reminder branch deterministically without touching the clock.
    """
    monkeypatch.setenv("REAPER_LOG_THROTTLE_S", "0")
    monkeypatch.setattr(jobs, "_reap_orphans_once", _always_boom)

    with caplog.at_level(logging.ERROR, logger=jobs.logger.name):
        for _ in range(4):
            jobs._reaper_sweep_once()

    records = _warnings(caplog)
    assert len(records) == 4

    # first is the full traceback, the rest are terse reminders
    assert records[0].exc_info is not None
    for record in records[1:]:
        assert record.exc_info is None, "a throttled reminder must not re-ship the traceback"

    counted = [r.getMessage() for r in records[1:]]
    assert "2 consecutive" in counted[0]
    assert "3 consecutive" in counted[1]
    assert "4 consecutive" in counted[2]
    # the fault class stays legible even without the traceback
    assert all("RuntimeError" in m for m in counted)


def test_a_new_exception_type_always_logs_in_full(caplog, monkeypatch):
    """A CHANGED fault class escapes the quiet window.

    Otherwise a streak of RuntimeErrors would swallow the first ValueError --
    hiding a genuinely new failure behind an already-known one, which is the
    exact silent-failure class this whole module exists to prevent.
    """
    monkeypatch.delenv("REAPER_LOG_THROTTLE_S", raising=False)

    with caplog.at_level(logging.ERROR, logger=jobs.logger.name):
        monkeypatch.setattr(jobs, "_reap_orphans_once", _always_boom)
        for _ in range(5):
            jobs._reaper_sweep_once()

        def _different_boom() -> int:
            raise ValueError("a-different-fault-class")

        monkeypatch.setattr(jobs, "_reap_orphans_once", _different_boom)
        jobs._reaper_sweep_once()

    records = _warnings(caplog)
    assert len(records) == 2, "the new exception type did not escape the quiet window"
    assert all(r.exc_info is not None for r in records), "both are full tracebacks"
    assert "a-different-fault-class" in records[1].getMessage()


def test_recovery_is_announced_once_then_the_next_failure_logs_in_full_again(
        caplog, monkeypatch):
    """Recovery closes the loop and re-arms the full-traceback path.

    Without an explicit recovery line, a throttled streak that stops logging is
    indistinguishable from a fault that quietly fixed itself.
    """
    monkeypatch.delenv("REAPER_LOG_THROTTLE_S", raising=False)

    with caplog.at_level(logging.WARNING, logger=jobs.logger.name):
        monkeypatch.setattr(jobs, "_reap_orphans_once", _always_boom)
        for _ in range(3):
            jobs._reaper_sweep_once()

        monkeypatch.setattr(jobs, "_reap_orphans_once", lambda: 0)
        jobs._reaper_sweep_once()              # recovers
        jobs._reaper_sweep_once()              # and stays quiet afterwards

        monkeypatch.setattr(jobs, "_reap_orphans_once", _always_boom)
        jobs._reaper_sweep_once()              # a fresh streak starts over

    records = _warnings(caplog)
    assert len(records) == 3, [r.getMessage() for r in records]

    assert records[0].exc_info is not None                      # first failure, in full
    assert "recovered after 3 consecutive" in records[1].getMessage()
    assert records[1].exc_info is None                          # nothing to trace on success
    assert records[2].exc_info is not None, (
        "after recovery the next failure must log in full again, not resume the "
        "previous streak's quiet window")


def test_the_throttle_bookkeeping_never_kills_the_daemon(caplog, monkeypatch):
    """A fault in the throttle itself is swallowed like any other sweep failure.

    The bookkeeping runs on the daemon's failure path, so if it could raise it
    would kill the exact thread the swallow exists to protect -- reintroducing
    the original bug through the fix for it.

    Degrading QUIETLY is the accepted cost: a broken throttle knob suppresses
    the reminders, but the first failure of the streak has already logged in
    full, so the fault itself never becomes invisible.
    """
    monkeypatch.setattr(jobs, "_reap_orphans_once", _always_boom)

    def _broken_throttle() -> float:
        raise RuntimeError("throttle knob is broken")

    monkeypatch.setattr(jobs, "reaper_log_throttle_s", _broken_throttle)

    with caplog.at_level(logging.ERROR, logger=jobs.logger.name):
        jobs._reaper_sweep_once()              # first failure: does not consult the window
        jobs._reaper_sweep_once()              # second: hits the broken knob, must not raise
        jobs._reaper_sweep_once()

    # not raising is most of the point, but assert the rest of it explicitly so
    # this cannot quietly become a test that checks nothing.
    records = _warnings(caplog)
    assert len(records) == 1, (
        "the first failure must still log in full even though every later call "
        "hits the broken knob and is swallowed")
    assert records[0].exc_info is not None
    assert _SWEEP_BOOM in records[0].getMessage()


# --------------------------------------------------------------------------- #
# 2. an unhandled route exception answers in the section-10 envelope
# --------------------------------------------------------------------------- #
_ROUTE_BOOM = "unhandled-route-secret-marker"


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/boom")
    def _boom():                                    # noqa: ANN202
        raise RuntimeError(_ROUTE_BOOM)

    @app.get("/missing")
    def _missing():                                 # noqa: ANN202
        raise HTTPException(status_code=404, detail="no such thing")

    install_error_handlers(app)
    return app


def _client() -> TestClient:
    # raise_server_exceptions=False is REQUIRED: starlette's ServerErrorMiddleware
    # always re-raises after sending the response (so real servers still log the
    # traceback), and the default TestClient re-raises it into the test instead of
    # surfacing the response we want to assert on.
    return TestClient(_app(), raise_server_exceptions=False)


def test_unhandled_route_exception_returns_the_standard_error_envelope():
    """The headline: a raw framework 500 becomes a parseable section-10 body."""
    response = _client().get("/boom")

    assert response.status_code == 500
    body = response.json()

    # exact section-10 shape, the same keys err_envelope() produces everywhere else,
    # so the frontend's existing error path handles this with no special case
    assert set(body) == set(err_envelope(ErrorCode.INTERNAL, "x", False))
    assert body["ok"] is False
    assert body["result"] is None
    assert body["degraded_mode"] is False
    assert body["error"]["error_code"] == ErrorCode.INTERNAL
    assert body["error"]["retryable"] is False


def test_unhandled_route_exception_does_not_echo_the_exception_text():
    """The catch-all covers EVERY route, so an arbitrary in-flight exception can
    carry a path, a SQL fragment, or credential material. It is logged, never
    echoed. That is the same posture the validation handler takes on rejected input."""
    response = _client().get("/boom")

    assert _ROUTE_BOOM not in response.text


def test_unhandled_route_exception_is_logged_with_its_traceback(caplog):
    """Suppressing the body is only safe because the operator still gets the
    detail. Without this the fix would trade one silent failure for another."""
    with caplog.at_level(logging.ERROR, logger="envelopes"):
        _client().get("/boom")

    records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert records, "an unhandled route exception logged nothing at error level"
    assert any(r.exc_info is not None for r in records), "traceback was not logged"
    assert any(_ROUTE_BOOM in r.getMessage() or (
        r.exc_info and _ROUTE_BOOM in str(r.exc_info[1])) for r in records)


def test_catch_all_does_not_shadow_the_specific_handlers():
    """Regression guard on the whole point of registering it last: HTTPException
    must still map to its own status and code, not get flattened into 500."""
    response = _client().get("/missing")

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["error_code"] == ErrorCode.BAD_PARAMS
    assert body["error"]["retryable"] is False


# --------------------------------------------------------------------------- #
# 2b. that envelope carries a CORRELATION ID (sol-critic PR #130, follow-up 2)
#
# Withholding str(exc) from the client is right, but it left the caller holding a
# bare "internal server error" with nothing tying it to the logged traceback. An
# operator could not answer "which 500 was yours?". These tests pin the join key,
# its opacity, and that adding it stayed inside the frozen §10 contract.
# --------------------------------------------------------------------------- #
_ERROR_ID_RE = re.compile(r"error_id[=:]\s*([0-9a-f]+)")


def _error_id_from(body: dict) -> str:
    match = _ERROR_ID_RE.search(body["error"]["message"])
    assert match, f"no correlation id in the error message: {body['error']['message']!r}"
    return match.group(1)


def test_the_correlation_id_in_the_body_matches_the_one_in_the_log(caplog):
    """The headline: the token a user can read back IS the token in the log.

    This is the entire point of the follow-up. A user quotes the ID from their
    error message and the operator greps it out of CloudWatch to land on the
    exact traceback, rather than guessing among every 500 on that route.
    """
    with caplog.at_level(logging.ERROR, logger="envelopes"):
        response = _client().get("/boom")

    error_id = _error_id_from(response.json())

    matching = [r for r in caplog.records
                if r.levelno >= logging.ERROR and error_id in r.getMessage()]
    assert matching, (
        f"correlation id {error_id!r} was returned to the caller but appears in no "
        "log line, so it correlates nothing")
    assert matching[0].exc_info is not None, (
        "the log line the id points at must be the one carrying the traceback")
    assert _ROUTE_BOOM in str(matching[0].exc_info[1])


def test_each_unhandled_exception_gets_a_fresh_correlation_id():
    """Per-exception, not per-process: two 500s must be distinguishable."""
    first = _error_id_from(_client().get("/boom").json())
    second = _error_id_from(_client().get("/boom").json())

    assert first != second


def test_the_correlation_id_is_opaque_and_cannot_carry_content():
    """It IDENTIFIES the failure, it does not DESCRIBE it.

    Generated fresh rather than derived from the request or the exception, and
    constrained to hex, so there is no channel through which exception text, a
    filesystem path, or credential material could ride along inside it.
    """
    response = _client().get("/boom")
    error_id = _error_id_from(response.json())

    assert re.fullmatch(r"[0-9a-f]{8}", error_id), f"unexpected id shape: {error_id!r}"
    # the id is the ONLY thing added: the exception text is still withheld
    assert _ROUTE_BOOM not in response.text


def test_the_id_bearing_envelope_is_still_contract_legal():
    """Mechanical proof the ID needed no additive field.

    contract/CONTRACT.md §10 freezes the error object's KEYS and the error_code
    ENUM -- `message` is an unconstrained string, so carrying the id inside it
    leaves the shape byte-identical. Checked against the machine-readable schema
    rather than argued, and asserted key-by-key so a future widening of what the
    client sees fails here.
    """
    import jsonschema  # lazy: the local platform/ package shadows what it imports

    schema = json.loads((SERVER_DIR / "envelope_schema.json").read_text(encoding="utf-8"))
    body = _client().get("/boom").json()

    jsonschema.Draft202012Validator(schema).validate(body)

    assert set(body) == set(err_envelope(ErrorCode.INTERNAL, "x", False))
    assert set(body["error"]) == {"error_code", "message", "retryable"}
    assert body["error"]["error_code"] == ErrorCode.INTERNAL
    assert isinstance(body["error"]["message"], str)
    assert body["error"]["retryable"] is False
    assert body["degraded_mode"] is False


def test_the_error_message_still_reads_as_the_generic_failure():
    """The id is an addition, not a replacement: the human-readable part stands."""
    body = _client().get("/boom").json()

    assert body["error"]["message"].startswith("internal server error")
