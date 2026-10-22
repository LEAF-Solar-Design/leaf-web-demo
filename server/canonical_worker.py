"""PostgreSQL-backed G1A worker for existing Leaf solver adapters.

Run as a separate process from the API.  It owns no APS credentials and claims
jobs using the canonical Platform lease transaction.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Dict

import platform_link
from solar_project_tools import SUPPORTED_TOOLS
from solver_adapters import arlo_design, autofill

ADAPTERS = {autofill.TOOL_NAME: autofill.run, arlo_design.TOOL_NAME: arlo_design.run}
DEFAULT_LEASE_SECONDS = 30.0
PROJECT_POSTURE_RUNTIMES = frozenset({"local", "test"})


def project_posture_ok(environ=None) -> bool:
    """The managed project proof's posture: a local or test runtime with project Solar runs on.

    Mirrors solar_project_admission.project_runs_enabled() (exactly "1") without importing
    that module, which pulls in the whole server.
    """
    env = os.environ if environ is None else environ
    runtime = env.get("LEAF_RUNTIME_ENV", "")
    return (runtime.strip().lower() in PROJECT_POSTURE_RUNTIMES
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
          project_readiness: bool = False, graph_heartbeat: bool = False) -> None:
    owner = worker_id if worker_id is not None else globals()["worker_id"]()
    descriptor = (graph_worker_descriptor(tool_name)
                  if project_readiness or (graph_heartbeat and tool_name in SUPPORTED_TOOLS)
                  else None)
    ready_printed = False
    stop = stop_event or threading.Event()
    while not stop.is_set():
        if descriptor is not None:
            platform_link._canonical_jobs_module().record_worker_heartbeat(
                owner, descriptor["tool_name"], descriptor["runtime"],
                descriptor["source_revision"], descriptor["source_sha256"])
        claimed = run_once(owner, lease_seconds=lease_seconds, tool_name=tool_name)
        # Ready only once one full cycle (heartbeat and claim) has completed.
        if project_readiness and descriptor is not None and not ready_printed:
            print("LEAF_CANONICAL_WORKER_READY " + json.dumps({
                "worker_id": owner, "tool_name": descriptor["tool_name"],
                "source_revision": descriptor["source_revision"]}, separators=(",", ":")),
                flush=True)
            ready_printed = True
        if once:
            return
        if not claimed:
            stop.wait(poll_seconds)


def _serve_tools(worker_ids: Dict[str, str], *, stop_event: threading.Event,
                 poll_seconds: float, lease_seconds: float) -> None:
    failures = []
    failure_lock = threading.Lock()

    def run(tool_name):
        failure_class = "RuntimeError"  # An unsolicited return also loses this loop.
        try:
            serve(tool_name=tool_name, worker_id=worker_ids[tool_name],
                  stop_event=stop_event, poll_seconds=poll_seconds,
                  lease_seconds=lease_seconds, graph_heartbeat=True)
        except BaseException as exc:  # A thread's SystemExit must also restart the task.
            failure_class = type(exc).__name__
        with failure_lock:
            if not stop_event.is_set():
                failures.append((tool_name, failure_class))
                stop_event.set()

    threads = [threading.Thread(target=run, args=(tool,),
                                name=f"canonical-worker-{tool}", daemon=True)
               for tool in worker_ids]
    try:
        for thread in threads:
            thread.start()
        stop_event.wait()
    finally:
        stop_event.set()
        # One deadline for all loops; blocked adapters cannot hold process shutdown open.
        deadline = time.monotonic() + poll_seconds + lease_seconds
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
    if failures:
        tool, failure_class = failures[0]
        print(f"canonical worker loop failed: {tool} {failure_class}", file=sys.stderr, flush=True)
        raise SystemExit(1)


class _ToolAction(argparse.Action):
    """Replace the default tool on first use, then collect explicit tools in order."""

    def __call__(self, parser, namespace, values, option_string=None):
        tools = getattr(namespace, self.dest)
        if not isinstance(tools, list):
            tools = []
        tools.append(values)
        setattr(namespace, self.dest, tools)


def main() -> None:
    import solar_project_graph

    parser = argparse.ArgumentParser(description="Leaf canonical solve worker")
    parser.add_argument("--once", action="store_true")
    # No prefix of this option is a prefix of a base option, so every abbreviation the base
    # parser accepted (--p for --poll-seconds) still resolves to the same option.
    parser.add_argument("--readiness", dest="project_readiness", action="store_true")
    parser.add_argument("--worker-id", type=_worker_id_argument)
    parser.add_argument("--tool", choices=sorted(set(ADAPTERS) | solar_project_graph.SUPPORTED_TOOLS),
                        action=_ToolAction, default=autofill.TOOL_NAME)
    parser.add_argument("--health", action="store_true")
    parser.add_argument("--max-age-seconds", type=float)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--lease-seconds", type=float, default=float(
        os.environ.get("LEAF_CANONICAL_LEASE_SECONDS", DEFAULT_LEASE_SECONDS)))
    args = parser.parse_args()
    tools = [args.tool] if isinstance(args.tool, str) else args.tool
    multi_tool = len(tools) > 1
    if len(set(tools)) != len(tools):
        parser.error("duplicate --tool values are refused even with LEAF_CANONICAL_MULTI_TOOL=1")
    if multi_tool and os.environ.get("LEAF_CANONICAL_MULTI_TOOL") != "1":
        parser.error("multiple --tool values require LEAF_CANONICAL_MULTI_TOOL=1")
    if multi_tool and (args.project_readiness or args.once):
        parser.error("multiple --tool values cannot be used with --readiness or --once")
    if args.project_readiness and tools[0] not in solar_project_graph.SUPPORTED_TOOLS:
        parser.error("--readiness requires a graph tool")
    if args.project_readiness and not project_posture_ok():
        parser.error("--readiness requires a local or test runtime with project Solar runs enabled")
    if args.health and not multi_tool and args.worker_id is None:
        parser.error("single-tool --health requires --worker-id")
    if multi_tool or args.health:
        if not math.isfinite(args.lease_seconds) or not math.isfinite(args.poll_seconds):
            parser.error("multi-tool and health modes require finite lease and poll seconds")
    if multi_tool:
        base = args.worker_id if args.worker_id is not None else socket.gethostname()
        try:
            worker_ids = {tool: _worker_id_argument(f"{base}:{tool}") for tool in tools}
        except argparse.ArgumentTypeError as exc:
            parser.error(str(exc))
    elif args.health:
        worker_ids = {tools[0]: args.worker_id}
    if args.health:
        max_age = args.max_age_seconds
        if max_age is None:
            max_age = 3 * max(args.lease_seconds, 1.0)
        if not math.isfinite(max_age):
            parser.error("--max-age-seconds must be finite")
        canonical_jobs = platform_link._canonical_jobs_module()
        unhealthy = []
        for tool, owner in worker_ids.items():
            row = canonical_jobs.worker_heartbeat(owner, max_age_seconds=max(15.0, max_age))
            if row is None or row["tool_name"] != tool:
                unhealthy.append(tool)
        if unhealthy:
            print("canonical worker heartbeat stale or missing: " + ", ".join(unhealthy),
                  file=sys.stderr, flush=True)
        raise SystemExit(1 if unhealthy else 0)
    # A process that can only heartbeat against part of the expected schema is
    # not ready. Refuse to claim work until every required relation is present.
    platform_link._load_platform()[1].assert_schema_current()
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda _signum, _frame: stop.set())
    if multi_tool:
        _serve_tools(worker_ids, stop_event=stop, poll_seconds=max(args.poll_seconds, 0.05),
                     lease_seconds=max(args.lease_seconds, 1.0))
        return
    serve(poll_seconds=max(args.poll_seconds, 0.05),
          lease_seconds=max(args.lease_seconds, 1.0), once=args.once, stop_event=stop,
          tool_name=tools[0], worker_id=args.worker_id, project_readiness=args.project_readiness)


if __name__ == "__main__":
    main()
