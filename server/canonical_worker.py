"""PostgreSQL-backed G1A worker for existing Leaf solver adapters.

Run as a separate process from the API.  It owns no APS credentials and claims
jobs using the canonical Platform lease transaction.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import threading
from time import monotonic
import uuid
from typing import Any, Dict

import platform_link
from solar_project_tools import SUPPORTED_TOOLS
from solver_adapters import arlo_design, autofill

ADAPTERS = {autofill.TOOL_NAME: autofill.run, arlo_design.TOOL_NAME: arlo_design.run}
DEFAULT_LEASE_SECONDS = 30.0
PROJECT_POSTURE_RUNTIMES = frozenset({"local", "test"})


def project_tools_from_env(environ=None) -> tuple[str, ...]:
    """Return validated, distinct project tools in configured polling order."""
    env = os.environ if environ is None else environ
    tools = []
    for item in env.get("LEAF_CANONICAL_PROJECT_TOOLS", "").split(","):
        name = item.strip()
        if not name:
            continue
        if name not in SUPPORTED_TOOLS:
            raise ValueError(f"unknown project tool: {name}")
        if name not in tools:
            tools.append(name)
    return tuple(tools)


def project_posture_ok(environ=None) -> bool:
    """Allow enabled local/test runs or explicitly allowed staging, never production.

    Mirrors solar_project_admission.project_runs_enabled() (exactly "1") without importing
    that module, which pulls in the whole server.
    """
    env = os.environ if environ is None else environ
    runtime = env.get("LEAF_RUNTIME_ENV", "").strip().lower()
    allowed = {item.strip().lower() for item in
               env.get("LEAF_SOLAR_PROJECT_RUN_ALLOWED_RUNTIMES", "").split(",")}
    return ((runtime in PROJECT_POSTURE_RUNTIMES
             or (runtime == "staging" and runtime in allowed))
            and env.get("LEAF_SOLAR_PROJECT_RUN_ENABLED", "0") == "1")

# Frozen server-relative source scope. The selected declaration is added below.
# Include the settings builtin and its direct helpers, not just the dispatcher.
GRAPH_WORKER_MANIFEST = (
    "builtins/solar_commit_solve.py",
    "builtins/solar_settings.py",
    "canonical_worker.py",
    "checkout_capability.py",
    "demo_actor.py",
    "deps.py",
    "leaf_cloud_client.py",
    "platform_link.py",
    "solar_design_graph.py",
    "solar_graph_context.py",
    "solar_graph_seed.py",
    "solar_local_graph.py",
    "solar_preset_sync.py",
    "solar_project.py",
    "solar_project_context.py",
    "solar_project_graph.py",
    "solar_project_jobs.py",
    "solar_project_sizing.py",
    "solar_project_solve.py",
    "solar_project_tools.py",
    "solar_proposal_candidate.py",
    "solar_settings_invalidation.py",
    "solar_sizing_client.py",
    "solar_sizing_power.py",
    "solar_solve_results.py",
    "solar_tools/__init__.py",
    "tenant_id_validator.py",
    "tool_validate.py",
    "write_loop.py",
)


def graph_worker_descriptor(tool_name: str, *, server_dir=None) -> Dict[str, str]:
    """Fingerprint raw graph sources without importing a database driver or opening a DB."""
    if tool_name not in SUPPORTED_TOOLS:
        raise RuntimeError(f"unknown graph worker tool: {tool_name}")
    root = Path(server_dir) if server_dir is not None else Path(__file__).resolve().parent
    declaration = f"solar_tools/{tool_name.replace('-', '_')}.json"
    digest = hashlib.sha256()
    for relative in sorted((*GRAPH_WORKER_MANIFEST, declaration)):
        path = root / relative
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise RuntimeError(f"cannot read graph worker source: {path}") from exc
        digest.update(relative.encode("utf-8") + b"\0" + str(len(raw)).encode("ascii") + b"\0")
        digest.update(raw)
    source_sha256 = digest.hexdigest()
    return {"tool_name": tool_name,
            "runtime": f"python-{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "source_revision": "sha256:" + source_sha256, "source_sha256": source_sha256}


def _worker_id_argument(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value):
        raise argparse.ArgumentTypeError("worker id must be 1-128 ASCII letters, digits, or ._:-")
    return value


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


class ProjectHeartbeat:
    """Keep project descriptors live independently of synchronous claims."""

    def __init__(self, owner: str, descriptors: tuple[Dict[str, str], ...], *,
                 interval: float = 5.0, stop_event: threading.Event | None = None) -> None:
        self._owner = owner
        self._descriptors = descriptors
        self._interval = interval
        self._stop = stop_event if stop_event is not None else threading.Event()
        self._started = threading.Event()
        self._lock = threading.Lock()
        self._in_flight: str | None = None
        self._reported: set[tuple[str, type[Exception]]] = set()
        self.still_running_after_close = False
        self._thread = threading.Thread(
            target=self._beat, name="canonical-project-heartbeat", daemon=True)

    def start(self, *, stop_event: threading.Event | None = None) -> None:
        deadline = monotonic() + self._interval + 1.0
        self._thread.start()
        while not self._started.is_set() and not self._stop.is_set():
            if stop_event is not None and stop_event.is_set():
                return
            remaining = deadline - monotonic()
            if remaining <= 0:
                return
            self._started.wait(timeout=min(remaining, 0.05))

    def close(self) -> None:
        # After stop is set, no heartbeat is admitted; at most the admitted call completes, so worker_health can look live for at most 15 s.
        with self._lock:
            self._stop.set()
            in_flight = self._in_flight
        self._thread.join(timeout=self._interval + 1.0)
        self.still_running_after_close = self._thread.is_alive()
        if self.still_running_after_close:
            print("canonical worker: project heartbeat thread still finishing one in-flight call at shutdown "
                  f"(tool={in_flight})",
                  file=sys.stderr)

    def _beat(self) -> None:
        while not self._stop.is_set():
            for descriptor in self._descriptors:
                tool = descriptor["tool_name"]
                try:
                    canonical_jobs = platform_link._canonical_jobs_module()
                    with self._lock:
                        if self._stop.is_set():
                            return
                        self._in_flight = tool
                    try:
                        canonical_jobs.record_worker_heartbeat(
                            self._owner, tool, descriptor["runtime"],
                            descriptor["source_revision"], descriptor["source_sha256"])
                    finally:
                        with self._lock:
                            self._in_flight = None
                except Exception as exc:  # noqa: BLE001 - isolate optional project tools
                    key = (tool, type(exc))
                    if key not in self._reported:
                        self._reported.add(key)
                        print(f"canonical worker: project tool {tool}: {type(exc).__name__}",
                              file=sys.stderr)
            self._started.set()
            if self._stop.wait(self._interval):
                return


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}"


def run_once(owner: str, *, lease_seconds: float = DEFAULT_LEASE_SECONDS,
             tool_name: str = autofill.TOOL_NAME) -> bool:
    import solar_project_graph
    import solar_project_jobs

    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    if tool_name in solar_project_graph.SUPPORTED_TOOLS:
        return solar_project_jobs.run_once(owner, tool_name=tool_name, lease_seconds=lease_seconds)
    canonical_jobs = platform_link._canonical_jobs_module()
    if tool_name == autofill.TOOL_NAME:
        descriptor = autofill.descriptor()
    elif tool_name == arlo_design.TOOL_NAME:
        descriptor = arlo_design.descriptor()
    else:
        raise ValueError(f"no canonical solver adapter for {tool_name}")
    canonical_jobs.record_worker_heartbeat(
        owner, descriptor["tool_name"], descriptor["runtime"], descriptor["source_revision"],
        descriptor["source_sha256"])
    job = canonical_jobs.claim_next(
        owner, lease_seconds=lease_seconds, tool_name=descriptor["tool_name"])
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
        params = dict(job.get("params") or {})
        if tool_name == arlo_design.TOOL_NAME:
            result = adapter(params, job_context=job, cancelled=lambda: guard.lost)
        else:
            result = adapter(params)
        provenance = {**base_provenance,
                      "worker_id": owner,
                      "solver_revision": result["solver_revision"],
                      "source_sha256": result["source_sha256"],
                      "runtime": result["runtime"]}
        guard.close()
        if guard.lost:
            # Do not attempt a terminal write after durable custody was lost.
            # The current owner or the expiry/retry path remains authoritative.
            return True
    except Exception as exc:  # noqa: BLE001
        guard.close()
        if guard.lost:
            return True
        retryable = isinstance(
            exc, (subprocess.TimeoutExpired, TimeoutError, ConnectionError))
        error = {"error_code": "SOLVER_FAILED", "message": f"{type(exc).__name__}: {exc}",
                 "retryable": retryable}
        canonical_jobs.fail_or_retry(job["job_id"], owner, error,
                                     {**base_provenance, "failure": error})
        return True
    guard.close()
    if guard.lost:
        return True
    try:
        # A persistence failure is not evidence that the deterministic solver
        # failed. Leave the running lease to expire so another attempt can
        # safely retry instead of writing a false terminal result.
        canonical_jobs.complete_solve(job["job_id"], owner, result, provenance)
    except Exception:  # noqa: BLE001 - lease expiry is the durable retry path
        return True
    finally:
        guard.close()
    return True


def serve(*, poll_seconds: float = 1.0, lease_seconds: float = DEFAULT_LEASE_SECONDS,
          once: bool = False, stop_event: threading.Event | None = None,
          tool_name: str = autofill.TOOL_NAME, worker_id: str | None = None,
          project_readiness: bool = False, project_tools: tuple[str, ...] = ()) -> None:
    owner = worker_id if worker_id is not None else globals()["worker_id"]()
    descriptor = graph_worker_descriptor(tool_name) if project_readiness else None
    ready_printed = False
    stop = stop_event or threading.Event()
    if project_tools and not project_posture_ok():
        print("canonical worker: project tool polling disabled: project posture not met "
              f"(runtime={os.environ.get('LEAF_RUNTIME_ENV', '')})", file=sys.stderr)
        project_tools = ()
    project_descriptors = tuple(graph_worker_descriptor(tool) for tool in project_tools)
    heartbeat = ProjectHeartbeat(owner, project_descriptors) if project_tools else None
    # Project tools stay live while a synchronous claim runs, because jobs queue durably and are claimed when the loop returns.
    # With N project tools, an idle cycle makes N+1 claim calls and at most N/5 heartbeats per second.
    try:
        if heartbeat is not None:
            heartbeat.start(stop_event=stop)
        while not stop.is_set():
            if descriptor is not None:
                platform_link._canonical_jobs_module().record_worker_heartbeat(
                    owner, descriptor["tool_name"], descriptor["runtime"],
                    descriptor["source_revision"], descriptor["source_sha256"])
            claimed = run_once(owner, lease_seconds=lease_seconds, tool_name=tool_name)
            # Ready only once one full cycle (heartbeat and claim) has completed.
            if descriptor is not None and not ready_printed:
                print("LEAF_CANONICAL_WORKER_READY " + json.dumps({
                    "worker_id": owner, "tool_name": descriptor["tool_name"],
                    "source_revision": descriptor["source_revision"]}, separators=(",", ":")),
                    flush=True)
                ready_printed = True
            for tool in project_tools:
                try:
                    project_claimed = run_once(owner, lease_seconds=lease_seconds, tool_name=tool)
                    claimed = project_claimed or claimed
                except Exception as exc:  # noqa: BLE001 - isolate optional project tools
                    print(f"canonical worker: project tool {tool}: {type(exc).__name__}", file=sys.stderr)
            if once:
                return
            if not claimed:
                stop.wait(poll_seconds)
    finally:
        if heartbeat is not None:
            heartbeat.close()


def main() -> None:
    import solar_project_graph

    parser = argparse.ArgumentParser(description="Leaf canonical solve worker")
    parser.add_argument("--once", action="store_true")
    # No prefix of this option is a prefix of a base option, so every abbreviation the base
    # parser accepted (--p for --poll-seconds) still resolves to the same option.
    parser.add_argument("--readiness", dest="project_readiness", action="store_true")
    parser.add_argument("--worker-id", type=_worker_id_argument)
    parser.add_argument("--tool", choices=sorted(set(ADAPTERS) | solar_project_graph.SUPPORTED_TOOLS), default=autofill.TOOL_NAME)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--lease-seconds", type=float, default=float(
        os.environ.get("LEAF_CANONICAL_LEASE_SECONDS", DEFAULT_LEASE_SECONDS)))
    args = parser.parse_args()
    try:
        project_tools = project_tools_from_env()
    except ValueError as exc:
        parser.error(str(exc))
    if args.project_readiness and args.tool not in solar_project_graph.SUPPORTED_TOOLS:
        parser.error("--readiness requires a graph tool")
    if args.project_readiness and not project_posture_ok():
        parser.error("--readiness requires a local or test runtime with project Solar runs enabled")
    # A process that can only heartbeat against part of the expected schema is
    # not ready. Refuse to claim work until every required relation is present.
    platform_link._load_platform()[1].assert_schema_current()
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda _signum, _frame: stop.set())
    serve(poll_seconds=max(args.poll_seconds, 0.05),
          lease_seconds=max(args.lease_seconds, 1.0), once=args.once, stop_event=stop,
          tool_name=args.tool, worker_id=args.worker_id, project_readiness=args.project_readiness,
          project_tools=project_tools)


if __name__ == "__main__":
    main()
