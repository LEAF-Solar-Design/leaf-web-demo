"""Measure the T1 tap-to-screen path against the REAL app.

The product claim under test: an operator approval reaches the requester's
screen hotswap-style, budget 3-4 seconds. This measures the app's share of
that budget with the real FastAPI app, the real sqlite transcript, and the
real SSE stream endpoint — the same polling generator production runs.

Two things are deliberately out of frame, and reported as such rather than
guessed at: network RTT (deployment-dependent) and the browser applying a CSS
custom property (a synchronous style write; there is nothing async to wait
on). The Postgres store is faked HERE and measured SEPARATELY against the
real PostgreSQL 16 — see measure_overlay_store_pg.py (run in WSL) — because this Windows
host cannot reach the WSL server over TCP.

What one trial does:

  1. opens the real GET /api/sessions/{id}/stream as a background reader
  2. T0: POST /api/overlay/decisions (the operator's tap)
  3. T1: the overlay_decided SSE frame arrives at the reader
  4. reports T1-T0

Run:  cd server && python measure_overlay_latency.py
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SERVER_DIR))
os.environ.setdefault(
    "SESSIONS_DB",
    str(Path(tempfile.mkdtemp(prefix="overlay-measure-")) / "sessions.db"))
os.environ.setdefault("JOBS_DB",
                      str(Path(tempfile.mkdtemp(prefix="overlay-measure-")) / "jobs.db"))

import session_store  # noqa: E402
from routers import overlay as overlay_router  # noqa: E402

TENANT = "tenant-measure"
TRIALS = 20


class FakeStore:
    """Postgres stands aside; its latency is measured separately in WSL."""

    session_id = ""

    def document(self, tenant_id):
        return {"tenant_id": tenant_id, "version": 3, "tokens": {}}

    def approve(self, **kw):
        return ({"proposal_id": kw["proposal_id"], "state": "approved",
                 "session_id": self.session_id},
                {"version": 4, "tokens": {"color.border": "#123456"}})


def main() -> int:
    import socket
    import urllib.request

    import uvicorn

    from app import app

    fake = FakeStore()
    overlay_router._store = lambda: fake  # noqa: SLF001 - measurement harness

    # A REAL uvicorn server, not TestClient: the stream and the tap must run
    # concurrently, and TestClient serializes through one async portal — the
    # streaming read blocks the POST and the measurement deadlocks. A real
    # server also measures the actual HTTP + event-loop path production runs.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "uvicorn did not start"

    samples = []

    for trial in range(TRIALS):
        session = session_store.get_or_create_session(
            TENANT, f"dwg-measure-{trial}")["session_id"]
        fake.session_id = session

        got = {}
        ready = threading.Event()

        def reader():
            # The REAL SSE endpoint, exactly as the browser consumes it.
            req = urllib.request.Request(
                f"{base}/api/sessions/{session}/stream?after_seq=0",
                headers={"X-Tenant-Id": TENANT})
            with urllib.request.urlopen(req, timeout=10) as res:
                ready.set()
                for raw in res:
                    if raw.startswith(b"event: overlay_decided"):
                        got["at"] = time.perf_counter()
                        return

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        ready.wait(timeout=10)
        time.sleep(0.05)  # let the reader reach its first poll

        t0 = time.perf_counter()
        body = json.dumps({"proposal_id": f"p-{trial}", "approve": True,
                           "decision_key": "k1234567",
                           "document_version": 3}).encode()
        req = urllib.request.Request(
            f"{base}/api/overlay/decisions", data=body, method="POST",
            headers={"X-Tenant-Id": TENANT, "X-Actor": "op@measure",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as res:
            assert res.status == 200
        tap_done = time.perf_counter()

        t.join(timeout=10)
        if "at" not in got:
            print(f"trial {trial}: SSE frame never arrived", file=sys.stderr)
            return 1
        samples.append({
            "tap_ms": (tap_done - t0) * 1000.0,
            "tap_to_sse_ms": (got["at"] - t0) * 1000.0,
        })

    tap = sorted(s["tap_ms"] for s in samples)
    sse = sorted(s["tap_to_sse_ms"] for s in samples)

    def pct(xs, p):
        return xs[min(len(xs) - 1, int(len(xs) * p))]

    print(json.dumps({
        "trials": TRIALS,
        "decide_route_ms": {"p50": round(statistics.median(tap), 1),
                            "p95": round(pct(tap, 0.95), 1),
                            "max": round(tap[-1], 1)},
        "tap_to_sse_frame_ms": {"p50": round(statistics.median(sse), 1),
                                "p95": round(pct(sse, 0.95), 1),
                                "max": round(sse[-1], 1)},
        "notes": [
            "store faked; real PG16 store latency measured separately in WSL",
            "excludes network RTT and the browser's synchronous style write",
            "SSE poll cadence STREAM_POLL_S=0.3s bounds the frame delay",
        ],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
