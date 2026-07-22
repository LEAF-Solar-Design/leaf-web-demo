"""PostgreSQL-backed G1A worker for existing Leaf solver adapters.

Run as a separate process from the API.  It owns no APS credentials and claims
jobs using the canonical Platform lease transaction.
"""
from __future__ import annotations

import argparse
import os
import signal
import socket
import threading
import uuid
from typing import Any, Dict

import platform_link
from solver_adapters import autofill

ADAPTERS = {autofill.TOOL_NAME: autofill.run}
DEFAULT_LEASE_SECONDS = 30.0


class LeaseGuard:
    """Renew one claimed lease while a synchronous solver adapter is running."""

    def __init__(self, canonical_jobs: Any, job_id: Any, owner: str,
                 descriptor: Dict[str, str], lease_seconds: float) -> None:
        self._canonical_jobs = canonical_jobs
        self._job_id = job_id
        self._owner = owner
        self._descriptor = descriptor
        self._lease_seconds = lease_seconds
        self._interval = max(min(lease_seconds / 3.0, 10.0), 0.05)
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread = threading.Thread(
            target=self._renew, name=f"canonical-lease-{job_id}", daemon=True)

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=self._interval + 1.0)

    def _renew(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                renewed = self._canonical_jobs.heartbeat(
                    self._job_id, self._owner, lease_seconds=self._lease_seconds)
                if not renewed:
                    self._lost.set()
                    return
                self._canonical_jobs.record_worker_heartbeat(
                    self._owner, self._descriptor["tool_name"],
                    self._descriptor["runtime"], self._descriptor["source_revision"],
                    self._descriptor["source_sha256"])
            except Exception:  # noqa: BLE001 - losing durable custody must fail closed
                self._lost.set()
                return


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}"


def run_once(owner: str, *, lease_seconds: float = DEFAULT_LEASE_SECONDS) -> bool:
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    canonical_jobs = platform_link._canonical_jobs_module()
    descriptor = autofill.descriptor()
    canonical_jobs.record_worker_heartbeat(
        owner, descriptor["tool_name"], descriptor["runtime"], descriptor["source_revision"],
        descriptor["source_sha256"])
    job = canonical_jobs.claim_next(owner, lease_seconds=lease_seconds)
    if job is None:
        return False
    attempt = int(job["attempt"])
    base_provenance: Dict[str, Any] = {"attempt": attempt, "execution_path": "local"}
    guard = LeaseGuard(canonical_jobs, job["job_id"], owner, descriptor, lease_seconds)
    guard.start()
    try:
        adapter = ADAPTERS.get(str(job.get("tool_name")))
        if adapter is None:
            raise ValueError(f"no canonical solver adapter for {job.get('tool_name')}")
        result = adapter(dict(job.get("params") or {}))
        provenance = {**base_provenance,
                      "solver_revision": result["solver_revision"],
                      "source_sha256": result["source_sha256"],
                      "runtime": result["runtime"]}
        guard.close()
        if guard.lost:
            # Do not attempt a terminal write after durable custody was lost.
            # The current owner or the expiry/retry path remains authoritative.
            return True
        outcome = canonical_jobs.complete_solve(job["job_id"], owner, result, provenance)
        if outcome not in {"applied", "duplicate"}:
            raise RuntimeError(f"canonical completion was not accepted: {outcome}")
    except Exception as exc:  # noqa: BLE001
        guard.close()
        if guard.lost:
            return True
        error = {"error_code": "SOLVER_FAILED", "message": f"{type(exc).__name__}: {exc}",
                 "retryable": False}
        canonical_jobs.fail_or_retry(job["job_id"], owner, error,
                                     {**base_provenance, "failure": error})
    finally:
        guard.close()
    return True


def serve(*, poll_seconds: float = 1.0, lease_seconds: float = DEFAULT_LEASE_SECONDS,
          once: bool = False, stop_event: threading.Event | None = None) -> None:
    owner = worker_id()
    stop = stop_event or threading.Event()
    while not stop.is_set():
        claimed = run_once(owner, lease_seconds=lease_seconds)
        if once:
            return
        if not claimed:
            stop.wait(poll_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Leaf canonical solve worker")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--lease-seconds", type=float, default=float(
        os.environ.get("LEAF_CANONICAL_LEASE_SECONDS", DEFAULT_LEASE_SECONDS)))
    args = parser.parse_args()
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda _signum, _frame: stop.set())
    serve(poll_seconds=max(args.poll_seconds, 0.05),
          lease_seconds=max(args.lease_seconds, 1.0), once=args.once, stop_event=stop)


if __name__ == "__main__":
    main()
