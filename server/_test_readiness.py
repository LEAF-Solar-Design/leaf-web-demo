"""Test-only readiness polling for harnesses that boot real server subprocesses.

Eight suites (``test_dynamic_loader.py`` and seven files under ``tests/``) each
boot a broker and/or app uvicorn child and then poll it until it serves. They
carried byte-identical private copies of this helper, so a fix to the budget or
the failure message had to be applied eight times to take effect. One module,
imported the same bare top-level way as ``_test_run_confirmation``.
"""
from __future__ import annotations

import subprocess
import time
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


# 90s, not 30s: these children are two uvicorn boots racing whatever else the gate
# runner (or another agent) is doing on the box, and a cold import of the engine
# registry is CPU-bound. 30s was tight enough to ERROR all four tests of
# test_dynamic_loader.py at setup under sustained parallel load while an immediate
# re-run passed unchanged, and PR #160 measured the same failure class in
# tests/test_backbone.py and tests/test_wave3.py. The budget only bounds a HANG — a
# child that dies still fails fast on the poll() branch below, so raising it costs
# nothing on the crash path and buys headroom on the slow path.
def wait_ready(url: str, proc: subprocess.Popen, timeout_s: float = 90.0,
               log_path: Path | None = None) -> None:
    deadline = time.time() + timeout_s
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
    raise TimeoutError(f"server at {url} not ready in {timeout_s}s{log_tail(log_path)}")
