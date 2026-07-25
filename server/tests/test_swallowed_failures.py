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

Run:  cd server && python -m pytest tests/test_swallowed_failures.py -q
"""
from __future__ import annotations

# Cache the STDLIB `platform` module BEFORE anything puts PROJECT_ROOT on sys.path
# (the local `platform/` package otherwise shadows it; mirrors tests/test_job_lanes.py).
import platform as _stdlib_platform  # noqa: E402
_stdlib_platform.python_implementation()

import logging  # noqa: E402
import os  # noqa: E402
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
