"""Test-only readiness polling for harnesses that boot real server subprocesses.

Eight suites (``test_dynamic_loader.py`` and seven files under ``tests/``) each
boot a broker and/or app uvicorn child and then poll it until it serves. They
carried byte-identical private copies of this helper, so a fix to the budget or
the failure message had to be applied eight times to take effect. One module,
imported the same bare top-level way as ``_test_run_confirmation``.

The budget is calibrated against the host rather than fixed, and a timeout says
how slow the host was when it gave up, so that "the box could not start a
process" stops being reported as "the server is broken". See ``wait_ready``.
"""
from __future__ import annotations

import math
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path

import requests


_TAIL_BYTES = 64 * 1024  # plenty for 25 lines of uvicorn output, bounded on purpose


def log_tail(log_path: Path | None, max_lines: int = 25) -> str:
    """Tail of a child's stdout+stderr log, for readiness failure messages.

    Without it a readiness failure is a bare "not ready in Ns" — indistinguishable
    between a slow boot and a server that is up but wedged before it can serve. The
    child holds this file open for append; the read is best-effort and must never
    replace the real failure with an exception from the diagnostic itself.

    That last sentence is the whole contract, so this reads a BOUNDED tail and
    catches BROADLY. A wedged child can append without limit, and the previous
    whole-file `read_text()` was unbounded in both memory and time: on a large log
    it could raise `MemoryError`, which is not an `OSError`, escape this function,
    and REPLACE the `TimeoutError`/`RuntimeError` the caller is trying to report —
    turning a readable "server not ready, here is why" into an unrelated crash.
    Seeking to the last `_TAIL_BYTES` also keeps the cost flat as the log grows.
    """
    if log_path is None:
        return ""
    try:
        with log_path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - _TAIL_BYTES))
            chunk = fh.read()
        text = chunk.decode("utf-8", errors="replace")
        # A mid-line seek leaves a partial first line; drop it rather than print it.
        if size > _TAIL_BYTES and "\n" in text:
            text = text.split("\n", 1)[1]
        lines = text.splitlines()
    except Exception as exc:  # noqa: BLE001 — diagnostics must never mask the real failure
        return f"\n  (could not read {log_path}: {exc!r})"
    if not lines:
        return f"\n  ({log_path} is empty — the child logged nothing)"
    body = "\n".join(f"  | {ln}" for ln in lines[-max_lines:])
    shown = min(len(lines), max_lines)
    truncated = " (tail)" if size > _TAIL_BYTES else ""
    return f"\n  last {shown} line(s) of {log_path}{truncated}:\n{body}"


BOOT_TIMEOUT_ENV = "LEAF_TEST_BOOT_TIMEOUT_S"

# A boot budget is not a property of the code under test. It is a property of the
# HOST. Every fixed value picked so far has been wrong on some box: 30s ERRORed all
# four tests of test_dynamic_loader.py at setup under parallel load, and the 90s that
# replaced it clears the worst boot actually measured on a saturated EVANS-DEKSTOP
# (73.8s) by 16s, which is a coin-flip rather than headroom. Raising the constant
# again just moves the cliff, so the budget is CALIBRATED against the host instead.
#
# The scaling unit is the cost of starting a bare Python interpreter. It is the
# right unit because it is the same contention the boot pays (process creation,
# then CPU-bound imports), and because it is the signal that actually moved when
# the flake appeared: spawn latency on this host went from ~150ms idle to
# 2697-4336ms saturated, and the suite went from 20s to 143s per iteration.
#
# Measured boot/spawn ratios on a saturated EVANS-DEKSTOP (4 rounds, broker + app):
# 6.1x, 9.9x, 18.0x, 23.6x, 23.6x, 29.1x, 29.6x, 45.9x. The multiple below is ~2x
# the worst of those, so the budget tracks load with real margin rather than
# assuming a speed.
_BOOT_COST_IN_SPAWNS = 90

# The floor keeps a fast host from getting a TIGHTER budget than main already ships
# (an idle 0.15s spawn would otherwise calibrate to 13.5s). The ceiling keeps a
# genuine hang from parking the gate: at the worst spawn latency yet recorded
# (4.336s) the formula asks for 390s, and 300s still covers the worst ratio above
# at that latency (4.336 * 45.9 = 199s), so the clamp bounds the FORMULA without
# cutting into the boot time actually observed.
_BOOT_TIMEOUT_FLOOR_S = 90.0
_BOOT_TIMEOUT_CEILING_S = 300.0

# Diagnosis only, never pass/fail: the idle spawn latency this host shows when it is
# not saturated, used to say how much slower it had become at the moment of failure.
_HEALTHY_SPAWN_S = 0.25
_SPAWN_PROBE_TIMEOUT_S = 30.0

_calibrated: tuple[float, str] | None = None


def spawn_latency_s() -> float | None:
    """Cost of starting a bare Python interpreter on this host, right now.

    Best-effort by contract: this only sizes a timeout and labels a failure, so
    every failure mode returns ``None`` for "could not measure" rather than
    raising. A probe that raised would convert a slow host into an unrelated
    crash, which is the exact confusion this module exists to remove.
    """
    try:
        started = time.perf_counter()
        subprocess.run(
            [sys.executable, "-c", "pass"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=_SPAWN_PROBE_TIMEOUT_S, check=True,
        )
        return time.perf_counter() - started
    except Exception:  # noqa: BLE001 (a probe must never mask or become the failure)
        return None


def calibrate_boot_timeout_s(measure_spawn: Callable[[], float | None],
                             env: Mapping[str, str]) -> tuple[float, str]:
    """Return ``(budget_s, how_it_was_derived)``. Pure given its probe, so testable.

    ``measure_spawn`` is a callable rather than a value so it is only invoked when
    it can change the answer. An operator-pinned budget therefore costs no
    subprocess at all, which matters most in CI, the case most likely to pin one.

    An explicit ``LEAF_TEST_BOOT_TIMEOUT_S`` is taken EXACTLY, with no floor or
    ceiling applied: CI and a loaded dev box need different budgets, and an
    operator who names a number is answering this question themselves. It must
    still be a FINITE positive number. `float()` happily returns `inf` for "inf"
    and for any literal that overflows (`1e309`), and an infinite budget makes
    `deadline` infinite, so the wait loop never exits and the gate hangs forever
    on a boot that will never succeed. That is the precise failure the ceiling
    exists to prevent, so it cannot be reachable through the override. Values
    that are unparseable, non-positive, NaN or infinite are ignored rather than
    obeyed: silently running with a 0s budget would error every stack-dependent
    test and read as a total collapse, which is the confusion this module is for.
    """
    raw = env.get(BOOT_TIMEOUT_ENV, "").strip()
    if raw:
        try:
            override = float(raw)
        except ValueError:
            override = -1.0
        if math.isfinite(override) and override > 0:
            return override, f"{BOOT_TIMEOUT_ENV}={raw}"
    spawn_s = measure_spawn()
    if spawn_s is None:
        return _BOOT_TIMEOUT_FLOOR_S, "spawn probe unavailable, using floor"
    scaled = spawn_s * _BOOT_COST_IN_SPAWNS
    budget = min(max(scaled, _BOOT_TIMEOUT_FLOOR_S), _BOOT_TIMEOUT_CEILING_S)
    how = f"{spawn_s:.3f}s/spawn * {_BOOT_COST_IN_SPAWNS} = {scaled:.0f}s"
    if budget != scaled:
        bound = "floor" if budget == _BOOT_TIMEOUT_FLOOR_S else "ceiling"
        how += f", clamped to the {bound} {budget:.0f}s"
    return budget, how


def boot_timeout_s() -> tuple[float, str]:
    """Calibrated budget for this process, measured once and reused.

    Measured once because the probe costs a real process spawn (up to seconds on
    the very hosts this protects) and a suite calls ``wait_ready`` up to four
    times. The first call happens while the children are already booting, so the
    probe sees the same contention they do, and it is skipped entirely when an
    override already settles the budget.
    """
    global _calibrated
    if _calibrated is None:
        _calibrated = calibrate_boot_timeout_s(spawn_latency_s, os.environ)
    return _calibrated


def _slowness_note() -> str:
    """Re-measure at the moment of failure and say what the evidence supports.

    This is the point of the whole module. A bare "not ready in Ns" is read off
    the scoreboard as a broken server; when eleven tests error together on a
    module-scoped fixture it looks like the backbone collapsed. Naming the host
    speed at the moment of the timeout separates "this box could not start a
    process" from "this server could not start".
    """
    spawn_s = spawn_latency_s()
    if spawn_s is None:
        return ("\n  host speed at timeout: could not measure (the probe itself "
                "failed or exceeded "
                f"{_SPAWN_PROBE_TIMEOUT_S:g}s, which is itself a sign of saturation)")
    ratio = spawn_s / _HEALTHY_SPAWN_S
    verdict = (
        "the host is saturated; a boot failure would not slow down bare "
        "interpreter startup"
        if ratio >= 2.0 else
        "the host is responsive, so this points at the SERVER, not at load")
    return (f"\n  host speed at timeout: {spawn_s:.3f}s to start a bare Python "
            f"interpreter, {ratio:.1f}x the {_HEALTHY_SPAWN_S:g}s idle baseline "
            f"-> {verdict}")


def wait_ready(url: str, proc: subprocess.Popen, timeout_s: float | None = None,
               log_path: Path | None = None) -> None:
    if timeout_s is None:
        timeout_s, how = boot_timeout_s()
    else:
        how = "caller-supplied"
    deadline = time.time() + timeout_s
    started = time.time()
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"server process exited early (rc={proc.returncode})"
                f"{log_tail(log_path)}")
        try:
            if requests.get(url, timeout=2).status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(0.25)
    raise TimeoutError(
        f"server at {url} not ready in {timeout_s:.0f}s "
        f"(waited {time.time() - started:.0f}s; budget: {how})"
        f"{_slowness_note()}{log_tail(log_path)}")
