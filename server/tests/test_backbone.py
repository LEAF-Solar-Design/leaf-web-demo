"""
Binary acceptance tests for the backend backbone
(async job spine + APS broker v1 + capability catalog + extended envelopes).

All tests run at APS_LIVE=0 — no live APS calls. Real subprocesses (uvicorn)
are booted so process-boundary properties (broker credential isolation,
SQLite restart durability) are tested for real, not simulated.

Run:  cd server && python -m pytest tests/test_backbone.py -q
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

SERVER_DIR = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((SERVER_DIR / "envelope_schema.json").read_text(encoding="utf-8"))

sys.path.insert(0, str(SERVER_DIR))
from envelopes import ErrorCode  # noqa: E402
from _test_readiness import (  # noqa: E402
    BOOT_TIMEOUT_ENV, calibrate_boot_timeout_s, wait_ready)
from _test_run_confirmation import confirmed_requests_payload  # noqa: E402

import jsonschema  # noqa: E402


# --------------------------------------------------------------------------- #
# process harness
# --------------------------------------------------------------------------- #
def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_uvicorn(module_app: str, port: int, env_overrides: dict, log_path: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env.update({k: str(v) for k, v in env_overrides.items()})
    log = open(log_path, "ab")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", module_app, "--port", str(port),
         "--host", "127.0.0.1"],
        cwd=str(SERVER_DIR), env=env, stdout=log, stderr=log,
    )
    return proc


def stop(
    proc: subprocess.Popen,
    log_path: Path,
    graceful_timeout_s: float = 10.0,
) -> dict | None:
    if proc.poll() is None:
        started = time.monotonic()
        proc.terminate()
        try:
            proc.wait(timeout=graceful_timeout_s)
        except subprocess.TimeoutExpired:
            kill_error = None
            try:
                proc.kill()
            except Exception as exc:
                kill_error = f"kill raised {type(exc).__name__}"
            if kill_error is None:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    kill_error = "process did not exit within 10 seconds after kill"
            elapsed_ms = round((time.monotonic() - started) * 1000)
            receipt = {
                "schema": "leaf.test-shutdown-failure.v1",
                "pid": proc.pid,
                "forced_kill": True,
                "graceful_timeout_ms": round(graceful_timeout_s * 1000),
                "elapsed_ms": elapsed_ms,
                "returncode": proc.poll(),
                "log_path": str(log_path),
                "log_size_bytes": log_path.stat().st_size if log_path.exists() else None,
                "cleanup_error": kill_error,
            }
            receipt_path = log_path.with_suffix(log_path.suffix + ".shutdown-failure.json")
            receipt_path.write_text(
                json.dumps(receipt, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            receipt["receipt_path"] = str(receipt_path)
            return receipt
    return None


def assert_stopped(proc: subprocess.Popen, log_path: Path) -> None:
    failure = stop(proc, log_path)
    if failure is not None:
        pytest.fail(
            "forced-kill fallback was required during process cleanup: "
            + json.dumps(failure, sort_keys=True),
            pytrace=False,
        )


# --------------------------------------------------------------------------- #
# fixtures: one broker + one main app for the whole module
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("backbone")
    broker_port, app_port = free_port(), free_port()
    ledger = tmp / "broker_ledger.jsonl"
    tenants = tmp / "broker_tenants.json"
    jobs_db = tmp / "jobs.db"

    broker = start_uvicorn("broker:app", broker_port,
                           # LEAF_BROKER_STORE is pinned, not left to default:
                           # scripts/run-all-gates.py does not scrub it, so an
                           # ambient `postgres` would send the attribution ledger
                           # to the shared authority and leave BROKER_LEDGER
                           # empty. test_6 would then read zero lines for runs
                           # that really happened.
                           {"BROKER_LEDGER": ledger, "BROKER_TENANTS": tenants,
                            "LEAF_BROKER_STORE": "legacy"},
                           tmp / "broker.log")
    app_env = {
        "APS_LIVE": "0",
        "APS_CRED": "/nonexistent",  # acceptance 4: app process never needs the APS secret
        "BROKER_URL": f"http://127.0.0.1:{broker_port}",
        "JOBS_DB": jobs_db,
    }
    app = start_uvicorn("app:app", app_port, app_env, tmp / "app.log")
    try:
        wait_ready(f"http://127.0.0.1:{broker_port}/broker/health", broker,
                   log_path=tmp / "broker.log")
        wait_ready(f"http://127.0.0.1:{app_port}/api/health", app,
                   log_path=tmp / "app.log")
        yield {
            "app": f"http://127.0.0.1:{app_port}",
            "broker": f"http://127.0.0.1:{broker_port}",
            "ledger": ledger,
            "tmp": tmp,
            "app_env": app_env,
            "app_port": app_port,
            "procs": (broker, app),
        }
    finally:
        failures = []
        for proc, log_path in (
            (app, tmp / "app.log"),
            (broker, tmp / "broker.log"),
        ):
            failure = stop(proc, log_path)
            if failure is not None:
                failures.append(failure)
        if failures:
            pytest.fail(
                "forced-kill fallback was required during stack cleanup: "
                + json.dumps(failures, sort_keys=True),
                pytrace=False,
            )


def test_stop_forced_kill_records_receipt_and_reports_failure(tmp_path):
    class HungProcess:
        pid = 4242
        returncode = None

        def __init__(self):
            self.terminated = False
            self.killed = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

        def wait(self, timeout):
            if not self.killed:
                raise subprocess.TimeoutExpired("hung-child", timeout)
            self.returncode = -9
            return self.returncode

    log_path = tmp_path / "hung.log"
    log_path.write_text("child did not stop\n", encoding="utf-8")
    proc = HungProcess()

    failure = stop(proc, log_path, graceful_timeout_s=0.001)

    assert failure is not None
    assert proc.terminated is True
    assert proc.killed is True
    assert failure["forced_kill"] is True
    assert failure["returncode"] == -9
    assert failure["elapsed_ms"] >= 0
    assert failure["log_path"] == str(log_path)
    receipt = json.loads(Path(failure["receipt_path"]).read_text(encoding="utf-8"))
    assert receipt["schema"] == "leaf.test-shutdown-failure.v1"
    assert receipt["log_size_bytes"] == log_path.stat().st_size


def test_boot_timeout_is_calibrated_to_host_speed_not_a_fixed_wall_clock():
    """The boot budget must track how slow the host is, and stay bounded.

    Sits next to the `stop()` test above: both pin harness helpers whose failure
    mode is a MISLEADING gate rather than a wrong product behaviour. This one is
    pure, because `calibrate_boot_timeout_s` takes the measured spawn latency as
    an argument, so it exercises the arithmetic without spawning anything.

    Without it the calibration could silently collapse to a constant (the clamps
    swallowing the scaling, or a bad env parse winning) and nothing would fail;
    the flake it exists to prevent would just come back, still wearing the
    costume of a broken backbone.
    """
    # The slowest boot/spawn ratio actually measured on a saturated EVANS-DEKSTOP
    # (73.8s broker boot at 1.610s/spawn). The budget must COVER this ratio, which
    # is the real property: "a host this slow still gets enough time". Asserting
    # that instead of `loaded > idle` is what makes a weakened multiple or a
    # lowered ceiling fail here rather than pass while quietly losing the headroom.
    worst_ratio = 45.9

    def budget(spawn_s, env=None):
        return calibrate_boot_timeout_s(lambda: spawn_s, env or {})[0]

    for spawn_s in (2.4, 4.336, 1.610):
        got = budget(spawn_s)
        assert got >= spawn_s * worst_ratio, (
            f"a {spawn_s}s/spawn host got {got:.0f}s, under the {worst_ratio}x "
            f"ratio already measured on this box ({spawn_s * worst_ratio:.0f}s): "
            f"the budget no longer covers a boot we have actually seen")
    # ...and still bounded, so a genuine hang cannot park the gate indefinitely.
    assert budget(4.336) <= 300.0, f"budget {budget(4.336):.0f}s exceeds the 300s ceiling"
    # The floor holds the idle case at no less than the fixed budget main shipped.
    assert budget(0.15) == 90.0, f"idle host got {budget(0.15):.0f}s, below the 90s floor"

    # An operator-supplied budget is taken exactly, floor and ceiling included:
    # CI and a loaded dev box are allowed to disagree.
    exact, how = calibrate_boot_timeout_s(lambda: 0.15, {BOOT_TIMEOUT_ENV: "600"})
    assert exact == 600.0 and BOOT_TIMEOUT_ENV in how
    assert budget(0.15, {BOOT_TIMEOUT_ENV: "5"}) == 5.0

    # An override that settles the budget must not pay for a probe it cannot use.
    calls = []

    def counted_probe():
        calls.append(1)
        return 0.15

    calibrate_boot_timeout_s(counted_probe, {BOOT_TIMEOUT_ENV: "600"})
    assert calls == [], "a valid override still spawned a spawn-latency probe"
    calibrate_boot_timeout_s(counted_probe, {})
    assert calls == [1], "the probe was not used when there was no override"

    # Garbage and non-finite values are IGNORED, not obeyed. A 0s budget would
    # error every stack-dependent test at setup and read as a total collapse; an
    # INFINITE one is the mirror hazard, making the deadline infinite so the wait
    # loop never exits and the gate hangs on a boot that will never come. Note
    # `float()` yields inf for "inf" AND for any literal that overflows (1e309).
    for bad in ("", "   ", "abc", "0", "-30", "nan", "inf", "-inf", "1e309"):
        assert budget(0.15, {BOOT_TIMEOUT_ENV: bad}) == 90.0, (
            f"{bad!r} was obeyed instead of ignored")

    # A probe that could not measure must not raise, and must fall back UPWARD.
    # An unmeasurable probe means the host could not start a process at all, so
    # it is the worst reading available, not a missing one. Pinning the floor
    # here would hand the smallest budget to the most degraded host, which is how
    # a real gate run failed two tests at "not ready in 90s" while saturated.
    assert budget(None) == 300.0, (
        "an unmeasurable probe fell back to the floor; on a host too loaded to "
        "start a process that is the smallest budget at the worst moment")


def test_wait_ready_re_measures_once_when_the_host_degrades_mid_wait(monkeypatch):
    """A budget sized at launch must not condemn a host that got slower since.

    Observed on 2026-07-25: the budget was calibrated at 1.316s/spawn and the
    deadline arrived when the box had degraded to 16.781s/spawn, 12x worse. The
    server was fine; the machine changed underneath it. Uses a fake process and a
    dead port so no server is booted, and shrinks the constants so it is fast.
    """
    import _test_readiness as rd

    class NeverExits:
        returncode = None

        def poll(self):
            return None

    # The ceiling must comfortably exceed ONE poll iteration (a requests timeout
    # of 2s plus the sleep), or the re-measure can never buy time it has not
    # already spent and the extension correctly declines. That is real behaviour,
    # not a quirk: an extension is only worth taking if it beats the clock.
    monkeypatch.setattr(rd, "_BOOT_TIMEOUT_FLOOR_S", 0.5)
    monkeypatch.setattr(rd, "_BOOT_TIMEOUT_CEILING_S", 8.0)
    monkeypatch.setattr(rd, "_BOOT_COST_IN_SPAWNS", 10)
    monkeypatch.setattr(rd, "_calibrated", None)  # defeat the per-process cache

    probes = []

    def degrading_probe():
        probes.append(1)
        return 0.02 if len(probes) == 1 else 5.0

    monkeypatch.setattr(rd, "spawn_latency_s", degrading_probe)

    # Record when each poll happened. This is the assertion that has teeth: the
    # message and the probe count would BOTH still appear if the deadline update
    # were deleted, so only "polling actually continued past the original budget"
    # proves the extension bought real time. Polls are made instant so the gap
    # between 0.5s and 8s is unmistakable rather than a narrow timing window.
    polls = []
    started = time.monotonic()

    def refused(*_args, **_kwargs):
        polls.append(time.monotonic() - started)
        raise requests.RequestException("nothing is listening")

    monkeypatch.setattr(rd.requests, "get", refused)

    dead_url = f"http://127.0.0.1:{free_port()}/api/health"
    with pytest.raises(TimeoutError) as exc:
        rd.wait_ready(dead_url, NeverExits())
    message = str(exc.value)

    assert "re-measured mid-wait" in message, message
    # Polling ran well past the 0.5s the original budget allowed. Without the
    # deadline update the last poll lands at ~0.5s and this fails.
    assert max(polls) > 2.0, (
        f"last poll was at {max(polls):.2f}s; polling stopped at the original "
        f"0.5s budget, so the re-measure never granted any extra wait")
    # Exactly three probes pins "once": initial calibration, one regrade at
    # expiry, one final diagnostic. A second regrade would make this four.
    assert len(probes) == 3, f"expected 3 probes (initial, regrade, diagnostic), got {len(probes)}"
    # Still bounded by the ceiling: a true hang must not park the gate.
    assert "not ready in 8s" in message, message


def submit(stack, tool="count-by-layer", params=None, tenant=None, wait=False):
    headers = {"X-Tenant-Id": tenant} if tenant else {}
    url = f"{stack['app']}/api/run" + ("?wait=1" if wait else "")
    payload = confirmed_requests_payload(
        stack["app"], tool, params, "rooftop_demo", headers=headers)
    return requests.post(url, json=payload, headers=headers, timeout=120)


def poll_until_terminal(stack, job_id, timeout_s=30.0):
    seen = []
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = requests.get(f"{stack['app']}/api/jobs/{job_id}", timeout=10)
        assert r.status_code == 200
        rec = r.json()
        if not seen or seen[-1] != rec["status"]:
            seen.append(rec["status"])
        if rec["status"] in ("complete", "failed"):
            return rec, seen
        time.sleep(0.1)
    raise TimeoutError(f"job {job_id} not terminal in {timeout_s}s (saw {seen})")


SECTION3_KEYS = {"ok", "tool", "version", "result", "overlay", "timing_ms", "cost", "error"}


# --------------------------------------------------------------------------- #
# 1. POST /api/run -> 202 {job_id, status:"submitted"}, non-blocking
# --------------------------------------------------------------------------- #
def test_1_run_returns_202_fast(stack):
    """A long job execution must NOT block the 202.

    Proved by IMPLICATION and by COMPARISON on the same runner in the same
    moment, not by a bare wall-clock budget. The old form timed `submit()`
    (which also does a GET /api/tools) against a hard `< 200ms` and flaked on
    loaded shared runners: CI hit 510ms, and a local baseline of this file
    reproduced it 8 times in 89 runs, ranging 210ms to 1720ms. That is
    scheduling noise, not a blocking response.

    So the signal is raised instead of the tolerance. The job sleeps `sleep_s`,
    and the bounds stay tied to `sleep_s` rather than to a fixed millisecond
    budget:

      * the job must still be NON-TERMINAL when the 202 arrives. This is a
        logical implication, not a timing budget: a response that waited for
        execution could only return once the job was terminal. Load can still
        flake it, but only by a pause of a full `sleep_s` somewhere between the
        server ACCEPTING the job and the status GET a few statements later --
        which includes the response's trip back to the client, not just the
        window after it arrives. That is ~5.8x the worst noise measured above.
        The same acceptance-to-response leg sits inside `elapsed` too, so the
        next bound shares part of this exposure rather than being independent of
        it; what none of them share is a dependency on execution TIME.
      * `elapsed < sleep_s`: submitting cannot cost as much as the execution it
        must not wait on.
      * `elapsed - control < sleep_s / 2`, where the control is the same POST
        with a 0s job taken moments earlier. Submit latency must be INDEPENDENT
        of execution time; load slows both measurements, so common-mode noise
        cancels and the difference stays small.

    Against the 1.72s worst noise measured above, `elapsed < sleep_s` sits 5.8x
    clear and the delta bound 2.9x. Only the first two bounds DECIDE: a blocking
    202 costs ~`sleep_s`, which leaves the job terminal and pushes `elapsed` past
    its threshold, and either one fails on its own. The delta only corroborates,
    because the control is subtracted one-sidedly -- a control that was itself
    slow shrinks `delta` and lets it pass on the same blocking implementation.

    The test DRAINS its own jobs in a `finally` before returning. A `sleep_s` job
    left executing is exactly the leak this file was flaking on: its ledger line
    and its worker lane outlive the test that made them, and tests 2 through 6
    then run against state test 1 owns. `finally` and not a trailing statement,
    because a FAILED assertion is when the leak does the most damage -- pytest
    carries on into the next test either way. The guard opens before the first
    POST for the same reason: acceptance happens server-side, so a request that
    dies on the way back may already have started a job. Waiting costs `sleep_s`
    of gate time and is what keeps this test from becoming the next test_3.
    """
    sleep_s = 10.0
    url = f"{stack['app']}/api/run"
    # Build both payloads OUTSIDE the timed windows: the catalog GET is not part
    # of the property under test, and it was previously inflating the measurement.
    control_payload = confirmed_requests_payload(
        stack["app"], "count-by-layer", {}, "rooftop_demo")
    subject_payload = confirmed_requests_payload(
        stack["app"], "count-by-layer", {"_qa_sleep_s": sleep_s}, "rooftop_demo")

    # The `try` opens BEFORE the first POST so that a failure between the two
    # POSTs still drains the first job. It does NOT close the case where a POST
    # itself raises after the server accepted the work: the id existed only in
    # the response that never arrived. Recovering it would mean diffing the job
    # list around the call, which is more machinery in the one place that has to
    # stay boring. So that case is REPORTED, not repaired.
    control = r = None
    subject_sent = False
    try:
        t0 = time.perf_counter()
        control = requests.post(url, json=control_payload, timeout=120)
        control_elapsed = time.perf_counter() - t0

        t1 = time.perf_counter()
        subject_sent = True  # set before the call, so a raise inside it counts
        r = requests.post(url, json=subject_payload, timeout=120)
        elapsed = time.perf_counter() - t1

        assert control.status_code == 202
        assert r.status_code == 202
        body = r.json()
        assert body["status"] == "submitted"
        assert body["job_id"]

        # The 202 came back while the job was still executing. A response that
        # blocked on execution could only return once the job was terminal.
        rec = requests.get(f"{stack['app']}/api/jobs/{body['job_id']}", timeout=10).json()
        assert rec["status"] not in ("complete", "failed"), (
            f"a {sleep_s:g}s job was already terminal when the 202 arrived "
            f"(status={rec['status']!r}): the response blocked on execution")

        # Absolute floor: submitting cannot cost as much as the job it must not wait on.
        assert elapsed < sleep_s, (
            f"submit took {elapsed*1000:.0f}ms, not less than the {sleep_s:g}s job "
            f"execution it must not block on")
        # Independence: the extra execution time must not show up in the response.
        delta = elapsed - control_elapsed
        assert delta < sleep_s / 2, (
            f"submitting a {sleep_s:g}s job cost {delta*1000:.0f}ms more than the 0s "
            f"control ({control_elapsed*1000:.0f}ms -> {elapsed*1000:.0f}ms), so "
            f"execution time is leaking into the 202")
    finally:
        # Nothing this test started may still be executing when it returns. Every
        # step here is guarded per response: reading the id and draining the job
        # both, because a failure on either one must not skip the OTHER job. That
        # is the whole point of the guard, and it is the second job -- the
        # `sleep_s` one -- that does the damage if it is skipped.
        drain_errors = []
        for label, resp in (("control", control), ("subject", r)):
            if resp is None:
                # No response. Only report the subject if its call was reached
                # at all: if the control POST raised, the subject never ran, and
                # blaming it here would bury the real exception.
                if label == "subject" and subject_sent:
                    drain_errors.append(
                        "subject: the POST did not return, so a job may have been "
                        "accepted with an id this test never saw and cannot drain")
                continue
            try:
                body_json = resp.json()
                job_id = body_json.get("job_id") if isinstance(body_json, dict) else None
            except ValueError:
                job_id = None
            if not job_id:
                # An accepted job whose id we cannot read is UNDRAINABLE, not
                # absent. Report it rather than skipping quietly: something is
                # still running and no later test can be trusted to be isolated.
                if resp.status_code == 202:
                    drain_errors.append(
                        f"{label}: accepted 202 but no readable job_id, so an "
                        f"undrainable job may still be running: {resp.text[:120]!r}")
                continue
            try:
                poll_until_terminal(stack, job_id, timeout_s=sleep_s + 30.0)
            except Exception as exc:  # noqa: BLE001 - drain every job, then report
                drain_errors.append(f"{label} {job_id}: {type(exc).__name__}: {exc}")
        assert not drain_errors, (
            "this test could not confirm every job it started had finished: "
            + "; ".join(drain_errors))


# --------------------------------------------------------------------------- #
# 2. submitted->running->complete; result is a section-3 envelope; SQLite
#    record survives a FULL app-process kill + restart
# --------------------------------------------------------------------------- #
def test_2_progression_envelope_and_restart_durability(stack, tmp_path):
    port = free_port()
    env = dict(stack["app_env"])  # same broker, own db; NO APS_LIVE -> proves the default
    env.pop("APS_LIVE")
    env["JOBS_DB"] = tmp_path / "restart_jobs.db"
    app = start_uvicorn("app:app", port, env, tmp_path / "restart_app.log")
    base = f"http://127.0.0.1:{port}"
    try:
        wait_ready(f"{base}/api/health", app, log_path=tmp_path / "restart_app.log")
        assert requests.get(f"{base}/api/health", timeout=5).json()["aps_live"] is False  # APS_LIVE=0 default

        payload = confirmed_requests_payload(
            base, "count-by-layer", {"_qa_sleep_s": 1.5}, "rooftop_demo")
        r = requests.post(f"{base}/api/run", json=payload, timeout=10)
        assert r.status_code == 202
        job_id = r.json()["job_id"]

        seen = []
        deadline = time.time() + 30
        rec = None
        while time.time() < deadline:
            rec = requests.get(f"{base}/api/jobs/{job_id}", timeout=10).json()
            if not seen or seen[-1] != rec["status"]:
                seen.append(rec["status"])
            if rec["status"] in ("complete", "failed"):
                break
            time.sleep(0.05)
        assert rec is not None and rec["status"] == "complete", f"saw {seen}"
        # progression: statuses observed in order, running observed (1.5s job)
        assert seen == [s for s in ("submitted", "running", "complete") if s in seen]
        assert "running" in seen and seen[-1] == "complete"
        # section-3 envelope in job.result
        env3 = rec["result"]
        assert SECTION3_KEYS.issubset(env3.keys())
        assert env3["ok"] is True and env3["error"] is None
        assert env3["result"]["counts"]["Panels"] == 2345

        # kill the app process entirely, restart, record must survive (SQLite)
        assert_stopped(app, tmp_path / "restart_app.log")
        app = start_uvicorn("app:app", port, env, tmp_path / "restart_app.log")
        wait_ready(f"{base}/api/health", app, log_path=tmp_path / "restart_app.log")
        r2 = requests.get(f"{base}/api/jobs/{job_id}", timeout=10)
        assert r2.status_code == 200
        rec2 = r2.json()
        assert rec2["job_id"] == job_id and rec2["status"] == "complete"
        assert rec2["result"]["result"]["counts"]["Panels"] == 2345
    finally:
        assert_stopped(app, tmp_path / "restart_app.log")


# --------------------------------------------------------------------------- #
# 3. JOB_MAX_S=2 + sleep>2s tool -> failed with error_code == TIMEOUT
# --------------------------------------------------------------------------- #
def test_3_job_timeout(stack, tmp_path):
    port = free_port()
    env = dict(stack["app_env"])
    env["JOBS_DB"] = tmp_path / "timeout_jobs.db"
    env["JOB_MAX_S"] = "2"
    app = start_uvicorn("app:app", port, env, tmp_path / "timeout_app.log")
    base = f"http://127.0.0.1:{port}"
    try:
        wait_ready(f"{base}/api/health", app, log_path=tmp_path / "timeout_app.log")
        payload = confirmed_requests_payload(
            base, "count-by-layer", {"_qa_sleep_s": 6}, "rooftop_demo")
        r = requests.post(f"{base}/api/run", json=payload, timeout=10)
        assert r.status_code == 202
        job_id = r.json()["job_id"]
        deadline = time.time() + 20
        rec = None
        while time.time() < deadline:
            rec = requests.get(f"{base}/api/jobs/{job_id}", timeout=10).json()
            if rec["status"] in ("complete", "failed"):
                break
            time.sleep(0.2)
        assert rec is not None and rec["status"] == "failed", f"got {rec and rec['status']}"
        assert rec["error"]["error_code"] == "TIMEOUT"
    finally:
        assert_stopped(app, tmp_path / "timeout_app.log")


# --------------------------------------------------------------------------- #
# 4. no `import da` in app.py/jobs.py; run completes with APS_CRED=/nonexistent
# --------------------------------------------------------------------------- #
def test_4_broker_holds_the_secret(stack):
    for fname in ("app.py", "jobs.py"):
        src = (SERVER_DIR / fname).read_text(encoding="utf-8")
        for i, line in enumerate(src.splitlines(), 1):
            assert not re.match(r"\s*(import|from)\s+da\b", line), f"{fname}:{i}: {line!r}"
        assert "da.client" not in src, f"{fname} references da.client"
    # the main-stack app runs with APS_CRED=/nonexistent — a run still completes
    r = submit(stack, wait=True)
    assert r.status_code == 200
    env3 = r.json()
    assert env3["ok"] is True and env3["result"]["total"] == 2345


# --------------------------------------------------------------------------- #
# 5. kill-switch: disabled tenant -> TENANT_DISABLED; other tenant unaffected
# --------------------------------------------------------------------------- #
def test_5_tenant_kill_switch(stack):
    r = requests.post(f"{stack['broker']}/broker/tenants/t1/disable", timeout=10)
    assert r.status_code == 200 and r.json()["disabled"] is True
    try:
        r1 = submit(stack, tenant="t1", wait=True)
        assert r1.status_code == 403
        assert r1.json()["error"]["error_code"] == "TENANT_DISABLED"
        assert r1.json()["error"]["retryable"] is False
        r2 = submit(stack, tenant="t2", wait=True)
        assert r2.status_code == 200 and r2.json()["ok"] is True
    finally:
        requests.post(f"{stack['broker']}/broker/tenants/t1/enable", timeout=10)


# --------------------------------------------------------------------------- #
# 6. exactly one ledger JSONL line per /broker/run, with attribution fields
# --------------------------------------------------------------------------- #
def _ledger_records(stack) -> list[str]:
    """The COMPLETE ledger records, in file order. An in-flight tail is dropped.

    Completeness is decided by the trailing newline, which is what makes the
    split between transient and persistent exact and needs no timing window.
    broker.py `_ledger_append` writes `line + "\\n"` in ONE call under
    `_ledger_lock`, and appends land at EOF, so an append caught mid-write is
    always the unterminated LAST fragment and never anything before it. That
    holds because ONE broker process owns this fixture's ledger: `_ledger_lock`
    is a thread lock, not an interprocess one, so two brokers sharing a ledger
    file could interleave. The `stack` fixture starts a single broker on a fresh
    temporary path, so there is exactly one writer.

    Everything earlier is finished bytes. If a broker died mid-write and a later
    append ran, the survivor is newline-terminated and will NOT parse -- so it is
    reported rather than skipped. Only the final unterminated fragment is
    ambiguous, and it is the one thing this drops.
    """
    if not stack["ledger"].exists():
        return []
    raw = stack["ledger"].read_text(encoding="utf-8")
    parts = raw.split("\n")
    parts.pop()  # after the last "\n": "" when terminated, else the in-flight tail
    return [p for p in parts if p.strip()]


def _ledger_entries_for(stack, tenants) -> list[dict]:
    """This test's ledger entries, in file order. A broken record FAILS here.

    Three earlier attempts tried to decide whether a broken line was OURS: a
    substring search (claimed `ledger-t10`'s torn line as `ledger-t1`'s), a
    tenant_id regex (picked the wrong occurrence, mangled escaped quotes), and a
    timed settle (made this test fail on any writer in the suite, the exact
    cross-test coupling this change exists to remove). All three asked the wrong
    question. A broken record cannot be attributed, because the field that says
    who it belongs to is the field that may be broken.

    The question that IS answerable is whether a record is finished, and
    `_ledger_records` answers it from the trailing newline rather than from a
    timer. So no attribution and no window: every COMPLETE record must be valid
    JSON, whoever wrote it, and only the unterminated tail is excused.

    That is deliberately not limited to this test's tenants, and it fires BEFORE
    the count: a complete broken record raises here, so the count never sees it.
    Missing and swapped records are the assertions' job, not this one's. What
    nothing here catches is corruption that still parses, a flipped byte in a
    field nothing asserts. That is a real limit, not a claim.
    """
    wanted = set(tenants)
    out = []
    for line in _ledger_records(stack):
        try:
            entry = json.loads(line)
        except ValueError as exc:
            raise AssertionError(
                f"a COMPLETE ledger record is not valid JSON, so it is durable "
                f"corruption and not an append caught mid-write ({exc}): {line!r}"
            ) from exc
        if entry.get("tenant_id") in wanted:
            out.append(entry)
    return out


def test_6_ledger_attribution(stack):
    """Exactly one ledger line per /broker/run, carrying the caller's attribution.

    Scoped to the tenants THIS test owns rather than to a global before/after
    line count. Earlier tests leave real /broker/run calls IN FLIGHT: test_3 runs
    a 6s job under JOB_MAX_S=2, and the app abandons each timed-out attempt while
    the broker keeps executing it (jobs.py `_run_job`: the inner thread is a
    daemon and the retry path re-dispatches, so one test_3 job makes up to
    JOB_MAX_ATTEMPTS distinct broker runs -- 3 by default, and the env can raise
    it -- each appending its own line ~6s later). Those are correct writes, one
    per run, but they land AFTER test_3
    returned. A global count therefore reads them as extra lines and fails
    nondeterministically: CI showed `assert 10 == (6 + 3)`, and a local serial
    baseline of this file reproduced the same shape 3 times in 25 runs.

    Filtering by tenant keeps the invariant EXACT while making it immune to
    unrelated concurrent work. What is asserted is the full expected SEQUENCE,
    not a per-tenant tally: the three calls are serial and `wait=1`, and the
    broker appends inside /broker/run's own `finally`, so each 200 means that
    call's line is already on disk -- nothing here waits or polls for it. That
    holds in LEGACY store mode only -- under LEAF_BROKER_STORE=postgres the run completes
    into the shared authority and this file is never written -- which is why the
    `stack` fixture pins the mode rather than inheriting it. Their relative
    order is therefore fixed, and checking it is what catches two runs whose
    attribution was SWAPPED -- a tally still sees one line per tenant and passes.
    Only the old GLOBAL contiguity assumption was an accident of scheduling;
    order WITHIN the filtered lines is real coverage and is kept.
    """
    n_runs = 3
    tenants = [f"ledger-t{i}" for i in range(n_runs)]
    assert not _ledger_entries_for(stack, tenants), \
        "ledger already carries lines for this test's tenants before it ran"

    for tenant in tenants:
        assert submit(stack, tenant=tenant, wait=True).status_code == 200

    mine = _ledger_entries_for(stack, tenants)
    assert len(mine) == n_runs, (
        f"exactly one line per /broker/run: {n_runs} runs produced {len(mine)} "
        f"line(s) for {tenants}")
    # One equality covers all four ways attribution can be wrong: an extra line,
    # a missing line, a misattributed line, and two lines whose tenants were
    # swapped. A per-tenant tally would pass the swap.
    assert [e["tenant_id"] for e in mine] == tenants, (
        f"ledger attribution does not match the order the runs were made: "
        f"expected {tenants}, got {[e['tenant_id'] for e in mine]}")
    for entry in mine:
        assert entry["tool"] == "count-by-layer"
        assert entry["engine_op"] == "count_by_layer"


# --------------------------------------------------------------------------- #
# 7. capability catalog: families + params_schema; internal/qa filtered
# --------------------------------------------------------------------------- #
def _family_tool_names(body):
    return {c["name"] for f in body["families"] for c in f["capabilities"]}


def test_7_capability_catalog(stack):
    r = requests.get(f"{stack['app']}/api/capabilities", timeout=10)
    assert r.status_code == 200
    body = r.json()
    assert len(body["families"]) >= 1
    for fam in body["families"]:
        assert fam["family_id"] and fam["label"]
        assert len(fam["capabilities"]) >= 1
        for cap in fam["capabilities"]:
            assert cap["name"]
            assert isinstance(cap["params_schema"], dict)
    assert "qa-sleep-count" not in _family_tool_names(body), "internal tool leaked to default view"

    r_qa = requests.get(f"{stack['app']}/api/capabilities",
                        headers={"X-Internal-Role": "qa"}, timeout=10)
    assert "qa-sleep-count" in _family_tool_names(r_qa.json())


# --------------------------------------------------------------------------- #
# 8. every endpoint body validates against envelope_schema.json
# --------------------------------------------------------------------------- #
def test_8_envelope_schema_everywhere(stack):
    a, b = stack["app"], stack["broker"]
    submit_r = submit(stack)
    job_id = submit_r.json()["job_id"]
    rec, _ = poll_until_terminal(stack, job_id)

    bodies = {
        "health": requests.get(f"{a}/api/health", timeout=10).json(),
        "session": requests.get(f"{a}/api/session?dwg=rooftop_demo", timeout=30).json(),
        "tools": requests.get(f"{a}/api/tools", timeout=10).json(),
        "capabilities": requests.get(f"{a}/api/capabilities", timeout=10).json(),
        "author": requests.post(f"{a}/api/author",
                                json={"description": "count panels on layer Panels"},
                                timeout=10).json(),
        "run_202": submit_r.json(),
        "job_record": rec,
        "jobs_list": requests.get(f"{a}/api/jobs?limit=5", timeout=10).json(),
        "run_wait": submit(stack, wait=True).json(),
        "unknown_tool": requests.post(f"{a}/api/run", json={"tool": "nope-nope"},
                                      timeout=10).json(),
        "unknown_job": requests.get(f"{a}/api/jobs/does-not-exist", timeout=10).json(),
        "bad_request_422": requests.post(f"{a}/api/run", json={"nope": 1}, timeout=10).json(),
        "broker_health": requests.get(f"{b}/broker/health", timeout=10).json(),
    }
    for name, body in bodies.items():
        jsonschema.validate(body, SCHEMA)
        if body.get("error") is not None:
            assert body["error"]["error_code"] in ErrorCode.ALL, name
        else:
            assert isinstance(body["degraded_mode"], bool), name
    # failure bodies actually carry structured codes
    assert bodies["unknown_tool"]["error"]["error_code"] == "UNKNOWN_TOOL"
    assert bodies["unknown_job"]["error"]["error_code"] == "BAD_PARAMS"
    assert bodies["bad_request_422"]["error"]["error_code"] == "BAD_PARAMS"


# --------------------------------------------------------------------------- #
# 9. regression: legacy shapes intact; ?wait=1 returns a final section-3 envelope
# --------------------------------------------------------------------------- #
def test_9_regression_legacy_shapes(stack):
    a = stack["app"]
    session = requests.get(f"{a}/api/session?dwg=rooftop_demo", timeout=30).json()
    assert isinstance(session["intake"], dict) and "polylines" in session["intake"]

    tools = requests.get(f"{a}/api/tools", timeout=10).json()
    names = {t["name"] for t in tools["tools"]}
    assert {"count-by-layer", "measure-panel-area", "highlight-panels-near-edge"} <= names
    for t in tools["tools"]:
        assert {"name", "version", "engine_op", "params"} <= set(t.keys())

    author = requests.post(f"{a}/api/author",
                           json={"description": "measure area on layer Panels"},
                           timeout=10).json()
    assert {"tool", "code", "preview"} <= set(author.keys())
    tools2 = requests.get(f"{a}/api/tools", timeout=10).json()
    assert author["tool"]["name"] in {t["name"] for t in tools2["tools"]}

    env3 = submit(stack, wait=True).json()
    assert SECTION3_KEYS <= set(env3.keys())
    assert env3["ok"] is True and env3["result"]["counts"]["Panels"] == 2345


# --------------------------------------------------------------------------- #
# 10. both processes booted via `uvicorn app:app` / `uvicorn broker:app`
#     (the module fixture IS that boot); SSE stream works and terminates
# --------------------------------------------------------------------------- #
def test_10_boot_and_sse(stack):
    broker_proc, app_proc = stack["procs"]
    assert broker_proc.poll() is None and app_proc.poll() is None, "a server died mid-suite"
    assert requests.get(f"{stack['app']}/api/health", timeout=10).json()["ok"] is True
    assert requests.get(f"{stack['broker']}/broker/health", timeout=10).json()["ok"] is True

    job_id = submit(stack, params={"_qa_sleep_s": 1}).json()["job_id"]
    events = []
    with requests.get(f"{stack['app']}/api/jobs/{job_id}/stream", stream=True,
                      timeout=60) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines(decode_unicode=True):
            if line and line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))
    assert events, "no SSE events received"
    assert events[-1]["status"] in ("complete", "failed")
    assert events[-1]["status"] == "complete"
