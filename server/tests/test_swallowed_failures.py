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
# the three things that must still escape it -- first sighting of each fault
# class, and recovery -- plus the control-flow guarantee that survives both.
# --------------------------------------------------------------------------- #
def _warnings(caplog) -> list:
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


def _always_boom() -> int:
    raise RuntimeError(_SWEEP_BOOM)


def test_the_default_quiet_window_is_the_documented_five_minutes(monkeypatch):
    """Pin the SHIPPED default, and that a bad value cannot silently disable it.

    Other tests assert "no second line appeared inside the window", which would
    pass for any default longer than the test itself. This one names the number,
    so lowering it to something that still floods CloudWatch fails here.

    The malformed cases matter because the caller swallows exceptions to protect
    the daemon: if this raised, the typo would turn the reminders off and look
    exactly like a healthy quiet window.
    """
    monkeypatch.delenv("REAPER_LOG_THROTTLE_S", raising=False)
    assert jobs.reaper_log_throttle_s() == 300.0
    assert jobs.REAPER_LOG_THROTTLE_DEFAULT_S == 300.0

    monkeypatch.setenv("REAPER_LOG_THROTTLE_S", "45.5")
    assert jobs.reaper_log_throttle_s() == 45.5

    monkeypatch.setenv("REAPER_LOG_THROTTLE_S", "0")
    assert jobs.reaper_log_throttle_s() == 0.0

    monkeypatch.setenv("REAPER_LOG_THROTTLE_S", "-10")
    assert jobs.reaper_log_throttle_s() == 300.0, (
        "a negative window is a typo, not a request to log everything; clamping it "
        "to 0 would turn one bad character into the flood this change prevents")

    # Each of these breaks the bound in its own direction if it gets through:
    # a raise is absorbed by the caller's swallow into "no reminder logged";
    # NaN makes every comparison False, restoring full-volume logging; inf
    # suppresses every reminder forever.
    for junk in ("not-a-number", "", "   ", "nan", "NaN", "inf", "-inf", "Infinity"):
        monkeypatch.setenv("REAPER_LOG_THROTTLE_S", junk)
        assert jobs.reaper_log_throttle_s() == 300.0, (
            f"{junk!r} must fall back to the default, not raise into the swallow "
            "and not silently break the bound")


def test_a_bad_throttle_value_says_so_exactly_once(caplog, monkeypatch):
    """Falling back is right; falling back SILENTLY hides an operator error.

    The env var would read as configured while having no effect. Once, not per
    sweep -- a warning on every failure would be its own flood.
    """
    monkeypatch.setenv("REAPER_LOG_THROTTLE_S", "not-a-number")
    monkeypatch.setattr(jobs, "_reaper_throttle_bad_raw", None)
    monkeypatch.setattr(jobs, "_reaper_throttle_warned", False)
    monkeypatch.setattr(jobs, "_reap_orphans_once", _always_boom)

    with caplog.at_level(logging.WARNING, logger=jobs.logger.name):
        for _ in range(30):
            jobs._reaper_sweep_once()

    complaints = [r for r in _warnings(caplog)
                  if "REAPER_LOG_THROTTLE_S" in r.getMessage()]
    assert len(complaints) == 1, (
        f"expected exactly one complaint about the bad value, got {len(complaints)}")
    assert "not-a-number" in complaints[0].getMessage()


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


def test_two_alternating_fault_classes_do_not_bypass_the_throttle(caplog, monkeypatch):
    """A, B, A, B must not log a full traceback every single interval.

    Keying the escape on "the class CHANGED" rather than "this class is NEW to
    the streak" reopens the entire bug: two faults that alternate would make
    every sweep look like new information and restore the 360-tracebacks-an-hour
    flood the throttle exists to stop. Each class earns exactly one full sighting
    per streak; coming back around is a repeat.
    """
    monkeypatch.delenv("REAPER_LOG_THROTTLE_S", raising=False)
    tick = {"n": 0}

    def _alternating() -> int:
        tick["n"] += 1
        raise (RuntimeError if tick["n"] % 2 else ValueError)("alternating-fault")

    monkeypatch.setattr(jobs, "_reap_orphans_once", _alternating)

    with caplog.at_level(logging.ERROR, logger=jobs.logger.name):
        for _ in range(20):
            jobs._reaper_sweep_once()

    assert tick["n"] == 20, "every interval still ran its sweep"
    records = _warnings(caplog)
    assert len(records) == 2, (
        f"20 sweeps alternating between two fault classes emitted {len(records)} "
        "lines; each class must log once, then be throttled like any repeat")


def test_a_stream_of_unique_fault_classes_is_still_bounded(caplog, monkeypatch):
    """The bound is on LOG LINES, not on fault classes.

    Two earlier versions bounded the wrong thing. Keying the escape on "the class
    changed" let A, B, A, B log every interval; keying it on "this class is new to
    the streak" let a fault throwing a FRESH class every sweep log every interval.
    Same hole, different shape. So this drives 200 sweeps that never repeat a
    class -- the worst case for any class-keyed rule -- and requires the output to
    stay inside the per-window budget anyway.
    """
    monkeypatch.delenv("REAPER_LOG_THROTTLE_S", raising=False)
    tick = {"n": 0}

    def _ever_new() -> int:
        tick["n"] += 1
        raise type(f"Fault{tick['n']}", (RuntimeError,),
                   {"__module__": f"mod_{tick['n']}"})("unique-every-time")

    monkeypatch.setattr(jobs, "_reap_orphans_once", _ever_new)

    with caplog.at_level(logging.ERROR, logger=jobs.logger.name):
        for _ in range(200):
            jobs._reaper_sweep_once()

    assert tick["n"] == 200, "every interval still ran its sweep"

    records = _warnings(caplog)
    ceiling = jobs._MAX_VERBOSE_PER_WINDOW + 1  # + the one terse reminder
    assert len(records) <= ceiling, (
        f"200 sweeps with 200 distinct fault classes emitted {len(records)} lines; "
        f"the per-window budget is {ceiling} regardless of class cardinality")


def test_the_fault_class_bookkeeping_cannot_grow_without_bound(monkeypatch):
    """A long outage must not leak memory through the throttle's own state.

    Unbounded `seen_types` plus a fault that throws a fresh class every
    REAPER_INTERVAL_S is 8,640 retained strings a day, in a process that runs for
    weeks. The set is capped; the overflow is remembered so the counts an operator
    reads stay honest rather than silently wrong.
    """
    monkeypatch.delenv("REAPER_LOG_THROTTLE_S", raising=False)
    tick = {"n": 0}

    def _ever_new() -> int:
        tick["n"] += 1
        raise type(f"Fault{tick['n']}", (RuntimeError,),
                   {"__module__": f"mod_{tick['n']}"})("unique-every-time")

    monkeypatch.setattr(jobs, "_reap_orphans_once", _ever_new)
    for _ in range(200):
        jobs._reaper_sweep_once()

    state = jobs._reaper_failure_state
    assert len(state["seen_types"]) <= jobs._MAX_TRACKED_FAULT_CLASSES
    assert state["class_overflow"] is True, "the cap was hit but not recorded"
    assert state["consecutive"] == 200, "the streak count is NOT what gets capped"


def test_suppressed_failures_are_counted_in_the_next_line_and_on_recovery(
        caplog, monkeypatch):
    """A throttled line must say what it swallowed.

    A log line that silently stops is worse than one that floods: the operator
    cannot tell "healthy" from "throttled into invisibility". Every emitted line
    carries the suppressed count, and recovery reports what never got logged.
    """
    monkeypatch.delenv("REAPER_LOG_THROTTLE_S", raising=False)
    monkeypatch.setattr(jobs, "_reap_orphans_once", _always_boom)

    with caplog.at_level(logging.WARNING, logger=jobs.logger.name):
        for _ in range(9):
            jobs._reaper_sweep_once()
        monkeypatch.setattr(jobs, "_reap_orphans_once", lambda: 0)
        jobs._reaper_sweep_once()

    recovery = _warnings(caplog)[-1].getMessage()
    assert "9 consecutive" in recovery, recovery
    assert "8 never logged" in recovery, (
        f"recovery did not account for the suppressed failures: {recovery!r}")


def test_the_streak_count_spans_every_fault_class(caplog, monkeypatch):
    """`consecutive` is failures since the last SUCCESS, not since the last class.

    Resetting the count on a class change understates the outage: a four-sweep
    failure streak that ended on a different class would report "1 consecutive"
    on recovery, telling an operator the opposite of what happened.
    """
    monkeypatch.delenv("REAPER_LOG_THROTTLE_S", raising=False)

    def _runtime() -> int:
        raise RuntimeError("class-a")

    def _value() -> int:
        raise ValueError("class-b")

    with caplog.at_level(logging.WARNING, logger=jobs.logger.name):
        for probe in (_runtime, _value, _runtime, _value):
            monkeypatch.setattr(jobs, "_reap_orphans_once", probe)
            jobs._reaper_sweep_once()

        monkeypatch.setattr(jobs, "_reap_orphans_once", lambda: 0)
        jobs._reaper_sweep_once()

    recovery = _warnings(caplog)[-1].getMessage()
    assert "4 consecutive" in recovery, (
        f"recovery reported the wrong streak length: {recovery!r}")
    assert "2 fault classes" in recovery, (
        "a mixed outage must say so rather than name only its last class")


def test_same_qualname_in_different_modules_are_distinct_fault_classes(
        caplog, monkeypatch):
    """`__qualname__` alone collides, and a collision SUPPRESSES a real signal.

    `package_a.Error` and `package_b.Error` both render as `Error`. Keyed on that
    alone, a genuine change of fault class looks like a repeat and gets throttled
    away -- silently, which is the failure mode this module exists to prevent.
    """
    monkeypatch.delenv("REAPER_LOG_THROTTLE_S", raising=False)

    first = type("Error", (RuntimeError,), {"__module__": "package_a"})
    second = type("Error", (RuntimeError,), {"__module__": "package_b"})
    assert first.__qualname__ == second.__qualname__, "the collision under test"

    with caplog.at_level(logging.ERROR, logger=jobs.logger.name):
        for cls in (first, second):
            def _boom(cls=cls) -> int:
                raise cls("colliding-qualname")
            monkeypatch.setattr(jobs, "_reap_orphans_once", _boom)
            jobs._reaper_sweep_once()

    records = _warnings(caplog)
    assert len(records) == 2, (
        "the second fault class was folded into the first because they share a "
        "__qualname__; the key must be module-qualified")
    assert all(r.exc_info is not None for r in records)


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
    """Per-EXCEPTION, not per-process and not per-app.

    Both requests go through ONE client deliberately. Building a fresh app per
    request would make this pass even if the ID were generated once per app and
    reused for every 500 it served -- which is precisely the bug the test is
    supposed to exclude. Same app, same route, same exception: still different.
    """
    client = _client()
    ids = [_error_id_from(client.get("/boom").json()) for _ in range(5)]

    assert len(set(ids)) == 5, f"ids repeated within one app: {ids}"


def test_the_correlation_id_is_opaque_and_cannot_carry_content():
    """It IDENTIFIES the failure, it does not DESCRIBE it.

    Generated fresh rather than derived from the request or the exception, and
    constrained to hex, so there is no channel through which exception text, a
    filesystem path, or credential material could ride along inside it.

    16 hex chars = 64 bits. 32 bits collides at ~1% within 10k ids, and
    CloudWatch pools logs across every task and lifetime, so a short token can
    point an operator at the wrong traceback.
    """
    response = _client().get("/boom")
    error_id = _error_id_from(response.json())

    assert re.fullmatch(r"[0-9a-f]{16}", error_id), f"unexpected id shape: {error_id!r}"
    # the id is the ONLY thing added: the exception text is still withheld
    assert _ROUTE_BOOM not in response.text


def test_the_id_bearing_envelope_is_still_contract_legal():
    """Mechanical proof the ID needed no additive field.

    contract/CONTRACT.md §10 REQUIRES {error_code, message, retryable} and freezes
    the error_code ENUM; `message` is declared only as `{"type": "string"}`, so
    carrying the id inside it leaves the key set byte-identical.

    The schema check alone is necessary but NOT sufficient: envelope_schema.json
    has no `additionalProperties: false`, and §10 explicitly permits additive
    extension, so validation would accept extra keys. The key-by-key assertions
    below are what actually pin the surface, so a future widening of what the
    client sees fails here rather than passing validation quietly.
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
