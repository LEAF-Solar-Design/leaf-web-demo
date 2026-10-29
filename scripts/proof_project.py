"""Managed project proof, records A through E: lifecycle and sanitized receipts.

Importing this module performs no host work. Production project execution
remains fail-closed until the PP-3B browser implementation is available.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from contextlib import redirect_stdout
from contextvars import ContextVar
from datetime import datetime, timezone
from enum import Enum
from functools import wraps
import errno
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol


REPO = Path(__file__).resolve().parent.parent
_PLATFORM = sys.platform
PROFILE = "project"
SCHEMA = "leaf.project-proof.v1"
WORKER_TOOL = "solar-settings"
TIER = "hosted_pro"
CLAIM_NAMESPACE = "https://leafdesign.ai/"
REQUIREMENTS = ("server/requirements.txt", "platform/requirements.txt",
                "server/requirements-auth.txt")
REQUIRED_MODULES = ("fastapi", "uvicorn", "psycopg", "psycopg_pool", "jwt",
                    "cryptography", "httpx", "requests")
REQUIRED_TEST_IDS = ("project_browser_create_import", "project_browser_bearer_required",
                     "project_settings_worker_readback")
ENGINE_OUTPUTS = ("engine.js", "engine_bg.wasm")
TOOLS = ("node", "cargo", "rustc", "wasm-pack")
BUILD_TIMEOUT_S = 600.0  # Provisional bounded policy, not a measured build budget.
VERSION_TIMEOUT_S = 15.0
MAX_OUTPUT_BYTES = 4096
MIGRATION_TIMEOUT_S = 300.0
_IMPORT_LOCK = threading.RLock()


class Stage(str, Enum):
    PREFLIGHT = "preflight"
    RUNTIME = "runtime_preparation"
    ALLOCATION = "postgresql_allocation"
    TRANSPORT = "transport_readiness"
    MIGRATION = "migration_barrier"
    IDENTITY = "identity"
    BINDING = "database_binding"
    STACK = "stack_and_worker"
    READINESS = "readiness"
    BROWSER = "browser"
    VERIFICATION = "verification"
    TEARDOWN = "teardown"
    DISPATCH = "dispatch"


class FailureCode(str, Enum):
    PREFLIGHT = "PREFLIGHT"
    ENVIRONMENT = "ENVIRONMENT"
    SOURCE = "SOURCE"
    RUNTIME = "RUNTIME"
    BROWSER_UNAVAILABLE = "BROWSER_UNAVAILABLE"
    ALLOCATION = "ALLOCATION"
    TRANSPORT = "TRANSPORT"
    MIGRATION_ENTRY = "MIGRATION_ENTRY"
    MIGRATION_CORPUS = "MIGRATION_CORPUS"
    MIGRATION_TIMEOUT = "MIGRATION_TIMEOUT"
    MIGRATION_EXIT = "MIGRATION_EXIT"
    MIGRATION_RESULT = "MIGRATION_RESULT"
    SCHEMA = "SCHEMA"
    IDENTITY = "IDENTITY"
    BINDING = "BINDING"
    LAUNCH = "LAUNCH"
    OWNERSHIP = "OWNERSHIP"
    READINESS = "READINESS"
    HEARTBEAT = "HEARTBEAT"
    AUTH = "AUTH"
    BROWSER_EXIT = "BROWSER_EXIT"
    BROWSER_EVIDENCE = "BROWSER_EVIDENCE"
    CANONICAL = "CANONICAL"
    CANCELLED = "CANCELLED"
    GUARDIAN = "GUARDIAN"
    CLEANUP_PROCESS = "CLEANUP_PROCESS"
    CLEANUP_IDENTITY = "CLEANUP_IDENTITY"
    CLEANUP_POOL = "CLEANUP_POOL"
    CLEANUP_AUTHORITY = "CLEANUP_AUTHORITY"
    CLEANUP_PRIVATE = "CLEANUP_PRIVATE"
    RECEIPT = "RECEIPT"
    INTERNAL = "INTERNAL"
    USAGE = "USAGE"


@dataclass(frozen=True)
class Failure:
    stage: Stage
    fixed_code: FailureCode

    def __post_init__(self):
        if not isinstance(self.stage, Stage) or not isinstance(self.fixed_code, FailureCode):
            raise TypeError("failure vocabulary must be closed")


class ProofRefused(RuntimeError):
    def __init__(self, failure: Failure):
        self.failure = failure
        super().__init__(failure.stage.value + ":" + failure.fixed_code.value)


def _refuse(code=FailureCode.PREFLIGHT, stage=Stage.PREFLIGHT):
    raise ProofRefused(Failure(stage, code)) from None


@dataclass(frozen=True)
class Ports:
    web: int
    app: int
    broker: int
    harness: int

    def values(self):
        return (self.web, self.app, self.broker, self.harness)


@dataclass(frozen=True)
class Config:
    run_id: str
    run_root: Path
    artifact_root: Path
    ports: Ports
    keep_run_root: bool = False


@dataclass(frozen=True)
class Outcome:
    exit_code: int
    receipt_path: Path | None
    failure: Failure | None
    cleanup_failures: tuple[Failure, ...] = ()


@dataclass(frozen=True)
class FileProof:
    path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class SourceProof:
    commit: str
    dirty_tree_sha256: str
    tested_source_manifest_sha256: str
    files: tuple[FileProof, ...]
    declared_untracked: tuple[str, ...] = ()
    expected_migrations: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkerDescriptor:
    tool: str
    runtime: str
    source_revision: str
    source_sha256: str
    source_manifest: tuple[str, ...]


@dataclass(frozen=True, repr=False)
class CanonicalRequest:
    purpose: str = "descriptor"
    tool_name: str = WORKER_TOOL
    environment: Mapping[str, str] = field(default_factory=dict, repr=False)
    evidence: CanonicalEvidence | None = None
    deadline: float = 0.0
    worker_id: str = ""

    def __post_init__(self):
        object.__setattr__(self, "environment", _RedactedEnvironment(self.environment))


@dataclass(frozen=True, repr=False)
class _RedactedEnvironment(Mapping[str, str]):
    _values: Mapping[str, str] = field(repr=False)

    def __post_init__(self):
        object.__setattr__(self, "_values", MappingProxyType(dict(self._values)))

    def __getitem__(self, key):
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __repr__(self):
        return "{" + ", ".join(repr(key) + ": <redacted>" for key in self) + "}"

    __str__ = __repr__


@dataclass(frozen=True, repr=False)
class HostFacts:
    posture: str
    legacy_posture: str
    synthetic: bool
    run_root_exists: bool
    paths_safe: bool
    output_owned: bool
    receipt_exists: bool
    occupied_ports: tuple[int, ...]
    missing_modules: tuple[str, ...]
    harness_present: bool
    browser_dependency_present: bool
    unapproved_dotenv: tuple[str, ...]
    vendor_candidates: tuple[Path, ...]
    requirements: tuple[FileProof, ...]
    python: str
    tools: tuple[tuple[str, str], ...]
    environment: Mapping[str, str] = field(repr=False)
    engine_outputs: tuple[FileProof, ...] = ()
    toolchain: tuple[tuple[str, str], ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "environment", _RedactedEnvironment(self.environment))


@dataclass(frozen=True, repr=False)
class Reservation:
    run_root: Path
    artifact_root: Path
    port_handles: tuple[Any, ...] = field(default=(), repr=False)
    root_identity: tuple[int, int] | None = field(default=None, repr=False)
    database_port: int = 0
    database_handle: Any = field(default=None, repr=False)

    def release_ports(self):
        for handle in self.port_handles:
            handle.close()
        self.release_database_port()

    def release_database_port(self):
        if self.database_handle is not None:
            self.database_handle.close()


@dataclass(frozen=True)
class OwnershipJournal:
    run_id: str
    run_root: Path
    artifact_root: Path
    created_at: datetime
    authority: AuthorityOwnership | None = None


@dataclass(frozen=True)
class Plan:
    config: Config
    source: SourceProof
    migrations: tuple[FileProof, ...]
    descriptor: WorkerDescriptor
    host: HostFacts = field(repr=False)
    required_test_ids: tuple[str, ...] = REQUIRED_TEST_IDS
    reservation: Reservation | None = field(default=None, repr=False)


@dataclass(frozen=True)
class RuntimeProof:
    requirements: tuple[FileProof, ...]
    python: str
    toolchain: tuple[tuple[str, str], ...]
    vendor_source: tuple[FileProof, ...]
    engine_outputs: tuple[FileProof, ...]
    engine_root: Path
    cargo_target_root: Path


class ResourceKind(str, Enum):
    RUNTIME_PROCESS = "runtime_process"
    AUTHORITY = "authority"
    MIGRATION_PROCESS = "migration_process"
    SEED_PROCESS = "seed_process"
    IDENTITY = "identity"
    STACK_PROCESS = "stack_process"
    BROWSER_PROCESS = "browser_process"
    ROOT = "root"
    POOL = "pool"
    GUARDIAN = "guardian"


# Within a dependency group, the newest acquisition is closed first.
_CLEANUP_ORDER = (
    ResourceKind.BROWSER_PROCESS, ResourceKind.STACK_PROCESS,
    ResourceKind.SEED_PROCESS, ResourceKind.MIGRATION_PROCESS, ResourceKind.RUNTIME_PROCESS,
    ResourceKind.IDENTITY, ResourceKind.POOL, ResourceKind.AUTHORITY,
    ResourceKind.ROOT, ResourceKind.GUARDIAN)
_CLEANUP_CODES = {
    **{kind: FailureCode.CLEANUP_PROCESS for kind in _CLEANUP_ORDER[:5]},
    ResourceKind.IDENTITY: FailureCode.CLEANUP_IDENTITY,
    ResourceKind.POOL: FailureCode.CLEANUP_POOL,
    ResourceKind.AUTHORITY: FailureCode.CLEANUP_AUTHORITY,
    ResourceKind.ROOT: FailureCode.CLEANUP_PRIVATE,
    ResourceKind.GUARDIAN: FailureCode.GUARDIAN}
CLEANUP_TIMEOUT_S = 5.0  # Policy, not a host measurement.


@dataclass(frozen=True)
class CleanupProof:
    processes_stopped: bool
    identity_delivery_closed: bool
    pools_closed: bool
    authority_removed: bool
    private_material_removed: bool
    guardian_disarmed: bool
    failures: tuple[Failure, ...]
    teardown_seconds: float
    observed_rows: tuple[str, ...]

    @property
    def ok(self):
        return (self.processes_stopped and self.identity_delivery_closed and self.pools_closed
                and self.authority_removed and self.private_material_removed
                and self.guardian_disarmed and not self.failures)


def _remember_refusal(method):
    """Remember every escaping exception without changing what the caller sees."""
    @wraps(method)
    def recorded(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except ProofRefused as exc:
            self.remember(exc.failure)
            raise
        except BaseException:
            stage = Stage.TEARDOWN
            if method.__name__ in {"checkpoint", "perform"}:
                stage = args[0] if args else kwargs.get("stage", stage)
                if not isinstance(stage, Stage):
                    stage = Stage.TEARDOWN
            self.remember(Failure(stage, FailureCode.GUARDIAN))
            raise
    return recorded


class RunScope:
    """D's lifetime owner. E supplies the stage driver and final atomic writer.

    All callbacks, including a potentially blocked allocator, run through the
    injected begin_operation seam. A timed-out callback is retained, never
    described as stopped. Destructive cleanup refuses while such work is live.
    """
    def __init__(self, *, effects: Effects, cancelled: Callable[[], bool] = lambda: False):
        self.effects = effects
        self.cancelled = cancelled
        self.failure = None
        self._actions = []
        self._pending = []
        self._closed = None
        self._closing = False
        self._draining = False
        self._lock = threading.RLock()
        self._commit_lock = threading.RLock()
        self._cancelled = False
        self._authority_run_id = None

    def __repr__(self):
        return "<project run scope>"

    @_remember_refusal
    def acquire(self, name: ResourceKind, cleanup: Callable[[], None]) -> None:
        if not isinstance(name, ResourceKind) or not callable(cleanup):
            raise TypeError("invalid cleanup registration")
        with self._lock:
            if (self._closing and not self._draining) or self._closed is not None:
                _refuse(FailureCode.GUARDIAN, Stage.TEARDOWN)
            self._actions.append((name, cleanup))

    @_remember_refusal
    def remember(self, failure: Failure):
        with self._lock:
            if self.failure is None:
                self.failure = failure

    @_remember_refusal
    def cancel(self):
        # E's final atomic rename/link must run inside commit_guard. This lock
        # defines ordering with an in-process cancellation request.
        with self._commit_lock:
            self._cancelled = True

    @_remember_refusal
    def checkpoint(self, stage: Stage):
        with self._lock:
            if self._closing or self._closed is not None:
                _refuse(FailureCode.GUARDIAN, stage)
            self._check_cancelled(stage)

    def _check_cancelled(self, stage):
        if self._cancelled or self.cancelled():
            _refuse(FailureCode.CANCELLED, stage)

    @_remember_refusal
    def perform(self, stage: Stage, operation: Callable[[], Any], *, deadline: float):
        """Run a stage through a pollable injected operation, including allocation.

        A cancelled/overdue operation stays owned by this scope until close.
        Production threads are not killable: unresolved work forbids success
        and guardian disarm. Process-backed adapters retain their own handles.
        """
        # Admission and pending registration are atomic with close's drain start.
        with self._lock:
            self.checkpoint(stage)
            if not math.isfinite(deadline) or deadline <= self.effects.clock.monotonic():
                _refuse(FailureCode.GUARDIAN, stage)
            task = self.effects.host.begin_operation(operation)
            self._pending.append(task)
        while not task.done():
            self.checkpoint(stage)
            remaining = deadline - self.effects.clock.monotonic()
            if remaining <= 0:
                _refuse(FailureCode.GUARDIAN, stage)
            self.effects.clock.wait(min(0.05, remaining))
        self.checkpoint(stage)
        remaining = deadline - self.effects.clock.monotonic()
        if remaining <= 0:
            _refuse(FailureCode.GUARDIAN, stage)
        return task.result()

    @_remember_refusal
    def bind_effects(self):
        scope, host = self, self.effects.host
        class ScopedHost:
            def __getattr__(self, name):
                return getattr(host, name)

            def reserve(self, plan):
                if isinstance(host, _Host):
                    return host.reserve(plan, scope=scope)
                reservation = host.reserve(plan)
                scope.own_reservation(reservation, keep_safe=plan.config.keep_run_root)
                return reservation
        return replace(self.effects, host=ScopedHost())

    @_remember_refusal
    def own_reservation(self, reservation: Reservation, *, keep_safe=False):
        release_ports = reservation.release_ports
        def remove():
            release_ports()
            if not self._settled():
                return False
            return self.effects.host.scrub(reservation, keep_safe=keep_safe)
        self.acquire(ResourceKind.ROOT, remove)

    @_remember_refusal
    def own_guardian(self, guardian):
        # Register before arming so partial arming is recoverable too.
        self.acquire(ResourceKind.GUARDIAN, guardian.disarm)
        guardian.arm()

    @_remember_refusal
    def own_pool(self, pool):
        self.acquire(ResourceKind.POOL, pool.close)

    @_remember_refusal
    def own_browser(self, browser):
        stop = browser.stop
        self.acquire(ResourceKind.BROWSER_PROCESS,
                     lambda: stop(self.effects.clock.monotonic() + CLEANUP_TIMEOUT_S))

    def _settled(self):
        return all(task.done() for task in self._pending)

    def _cleanup_call(self, callback):
        task = self.effects.host.begin_operation(callback)
        deadline = self.effects.clock.monotonic() + CLEANUP_TIMEOUT_S
        while not task.done():
            remaining = deadline - self.effects.clock.monotonic()
            if remaining <= 0:
                self._pending.append(task)
                return False
            self.effects.clock.wait(min(0.05, remaining))
        return task.result() is not False

    @_remember_refusal
    def close(self) -> CleanupProof:
        with self._lock:
            if self._closed is not None:
                return self._closed
            if self._closing:
                _refuse(FailureCode.GUARDIAN, Stage.TEARDOWN)
            self._closing = True
            self._draining = True
        started = self.effects.clock.monotonic()
        failures = []
        failed = set()
        # Settle pending stages before teardown. No unbounded joins, and no
        # success claim if a producer could still allocate or write material.
        deadline = started + CLEANUP_TIMEOUT_S
        while not self._settled() and self.effects.clock.monotonic() < deadline:
            self.effects.clock.wait(min(0.05, deadline - self.effects.clock.monotonic()))
        with self._lock:
            self._draining = False
            actions = tuple(self._actions)
        if not self._settled():
            failed.add(ResourceKind.RUNTIME_PROCESS)
            failures.append(Failure(Stage.TEARDOWN, FailureCode.CLEANUP_PROCESS))
            self.remember(failures[0])
        for kind in _CLEANUP_ORDER:
            for name, callback in reversed(actions):
                if name != kind:
                    continue
                try:
                    # A guardian must stay armed if any teardown was unverified.
                    if kind == ResourceKind.GUARDIAN and (failures or not self._settled()):
                        raise RuntimeError("cleanup unverified")
                    if not self._cleanup_call(callback):
                        raise RuntimeError("cleanup unverified")
                except BaseException:
                    failed.add(kind)
                    failure = Failure(Stage.TEARDOWN, _CLEANUP_CODES[kind])
                    failures.append(failure)
                    self.remember(failure)
            if kind == ResourceKind.AUTHORITY and self._authority_run_id is not None:
                # Observe only after every authority callback, including failed
                # allocations that never returned a cleanup capability.
                try:
                    if not self._settled() or not self._cleanup_call(lambda: _validate_ownership(
                            self.effects.host.authority_inventory(self._authority_run_id),
                            self._authority_run_id, allocated=False)):
                        raise RuntimeError("authority absence unverified")
                except BaseException:
                    failed.add(ResourceKind.AUTHORITY)
                    failure = Failure(Stage.TEARDOWN, FailureCode.CLEANUP_AUTHORITY)
                    failures.append(failure)
                    self.remember(failure)
        if failures:
            self.remember(failures[0])
        processes = not failed.intersection(_CLEANUP_ORDER[:5])
        proof = CleanupProof(processes, ResourceKind.IDENTITY not in failed,
            ResourceKind.POOL not in failed, ResourceKind.AUTHORITY not in failed,
            ResourceKind.ROOT not in failed, ResourceKind.GUARDIAN not in failed,
            tuple(failures), self.effects.clock.monotonic() - started,
            () if failures else ("P10",))
        self._closed = proof
        self._actions.clear()  # Drop leases/callback closures after teardown.
        self._pending = [task for task in self._pending if not task.done()]
        return proof

    @_remember_refusal
    def commit_guard(self, atomic_commit: Callable[[], Any]):
        """E calls this around only its final atomic receipt filesystem action."""
        with self._commit_lock:
            self._check_cancelled(Stage.VERIFICATION)
            if self._closed is None or not self._closed.ok or self.failure is not None:
                _refuse(FailureCode.CLEANUP_PRIVATE, Stage.TEARDOWN)
            return atomic_commit()


@dataclass(frozen=True, repr=False)
class GuardianRequest:
    run_id: str
    reservation: Reservation
    ownership: AuthorityOwnership
    channel: Any = field(repr=False)
    processes: tuple[OwnedProcess, ...] = field(default=(), repr=False)
    deadline: float = 0.0


def guardian_main(request: GuardianRequest, *, effects: Effects) -> int:
    """Bounded liveness channel; EOF triggers cleanup, never a normal disarm.

    The caller transfers retained owned handles, never bare PIDs. A public
    journal alone is insufficient to authorize this request.
    """
    try:
        # This is a capability transferred over the owned parent channel, not
        # an inventory discovered after parent loss. Unknown/new IDs are refused.
        _validate_ownership(_empty_ownership(request.ownership),
                            request.run_id, allocated=False)
        ids = request.ownership.containers + request.ownership.networks
        if (len(set(ids)) != len(ids)
                or any(not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{12,64}", value) is None
                       for value in ids)):
            return 1
        if (request.reservation.artifact_root != request.reservation.run_root / "artifacts"
                or not math.isfinite(request.deadline)):
            return 1
        disarm_refused = False
        while effects.clock.monotonic() < request.deadline:
            remaining = request.deadline - effects.clock.monotonic()
            try:
                event = request.channel.receive(min(0.1, remaining))
            except EOFError:
                break
            if event == "disarm":
                # Independently observe absence; a sender's claim is not proof.
                stopped = True
                for process in request.processes:
                    try:
                        if process.stop(effects.clock.monotonic() + CLEANUP_TIMEOUT_S) is not True:
                            stopped = False
                    except BaseException:
                        stopped = False
                try:
                    owned = effects.host.authority_inventory(request.run_id)
                    _validate_ownership(owned, request.run_id, allocated=False)
                    absent = effects.host.private_absent(request.reservation)
                    if request.ownership.kind == "native":
                        stopped = stopped and _pg_module().native_process_gone(_native_capture(request.ownership))
                except BaseException:
                    absent = False
                if absent and stopped:
                    request.channel.acknowledge()
                    return 0
                disarm_refused = True
                break
            if event not in (None, "alive"):
                return 1
            effects.clock.wait(min(0.05, max(0.0, request.deadline - effects.clock.monotonic())))
        scope = RunScope(effects=effects)
        scope.own_reservation(request.reservation)
        def remove_authority():
            observed = effects.host.authority_inventory(request.run_id)
            if observed.kind == "native" or request.ownership.kind == "native":
                if not _native_subset(observed, request.ownership):
                    return False
            elif (observed.run_id != request.ownership.run_id
                    or observed.names != request.ownership.names
                    or observed.labels != request.ownership.labels
                    or not set(observed.containers).issubset(request.ownership.containers)
                    or not set(observed.networks).issubset(request.ownership.networks)):
                return False
            return _cleanup_authority(None, request.run_id, effects=effects, verify=True,
                                      ownership=request.ownership)
        scope.acquire(ResourceKind.AUTHORITY, remove_authority)
        for process in request.processes:
            scope.acquire(ResourceKind.STACK_PROCESS,
                lambda process=process: process.stop(effects.clock.monotonic() + CLEANUP_TIMEOUT_S))
        proof = scope.close()
        return 0 if proof.ok and not disarm_refused else 1
    except BaseException:
        return 1


def _cleanup_authority(authority, run_id, *, effects, verify=False, ownership=None):
    failed = False
    try:
        if authority is not None:
            authority.stop()
    except BaseException:
        failed = True
    finally:
        # Stop transport even if the ownership read failed. Only a validated
        # snapshot authorizes deletion; the run ID alone never does.
        if ownership is not None:
            effects.host.cleanup_owned_authority(run_id, ownership)
    if verify:
        _validate_ownership(effects.host.authority_inventory(run_id), run_id, allocated=False)
    if failed:
        raise RuntimeError("authority stop failed") from None


@dataclass(frozen=True)
class ProcessIdentity:
    role: str
    pid: int
    start_id: str
    parent_pid: int


@dataclass(frozen=True, repr=False)
class ProcessSpec:
    role: str
    argv: tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str] = field(repr=False)
    deadline: float
    output_path: Path | None = None
    max_output_bytes: int = MAX_OUTPUT_BYTES

    def __post_init__(self):
        secret_flags = {"--token", "--password", "--secret", "--dsn",
                        "--database-url", "--signing-key"}
        if any(arg.partition("=")[0].lower() in secret_flags for arg in self.argv):
            _refuse(FailureCode.RUNTIME, Stage.RUNTIME)
        object.__setattr__(self, "environment", _RedactedEnvironment(self.environment))


class OwnedProcess(Protocol):
    identity: ProcessIdentity
    def poll(self) -> int | None: ...
    def children(self, pids: Mapping[str, int]) -> tuple[ProcessIdentity, ...]: ...
    def stop(self, deadline: float) -> bool: ...


class Clock(Protocol):
    def monotonic(self) -> float: ...
    def utc_now(self) -> datetime: ...
    def wait(self, seconds: float) -> None: ...


class Host(Protocol):
    def inspect(self, config: Config) -> HostFacts: ...
    def reserve(self, plan: Plan) -> Reservation: ...
    def source_snapshot(self) -> SourceProof: ...
    def authority_inventory(self, run_id: str) -> Any: ...
    def scrub(self, reservation: Reservation, *, keep_safe: bool) -> bool: ...
    def private_absent(self, reservation: Reservation) -> bool: ...
    def begin_operation(self, callback: Callable[[], Any]) -> Any: ...
    # E's command/lifecycle adapters; all remain replaceable host effects.
    def cancellation(self, scope: RunScope) -> Any: ...
    def guardian(self, plan: Plan) -> Any: ...
    def receipt_binding(self, run_id: str, path: Path, writer: ReceiptWriter) -> ReceiptBinding: ...
    def recovery(self, run_id: str, root: Path) -> tuple[Reservation, AuthorityOwnership]: ...
    def recovery_processes_absent(self, run_id: str, root: Path) -> bool: ...
    # B's host operations keep imports, file reads and teardown behind a seam.
    def migration_entry_present(self) -> bool: ...
    def migration_corpus(self) -> tuple[tuple[FileProof, ...], tuple[str, ...]]: ...
    def database_module(self, environment: Mapping[str, str]) -> Any: ...
    def store_module(self) -> Any: ...
    def cleanup_authority(self, run_id: str) -> Any: ...
    def cleanup_owned_authority(self, run_id: str, ownership: AuthorityOwnership) -> Any: ...
    def close_identity(self, lease: IdentityLease) -> None: ...
    def read_child_result(self, path: Path) -> Any: ...
    def read_stack_ready(self, path: Path) -> Any: ...
    def app_readiness(self, request: ReadyRequest) -> Any: ...
    def worker_heartbeat(self, request: ReadyRequest) -> Any: ...
    def organization_read(self, request: ReadyRequest, *, authenticated: bool) -> Any: ...


class ReceiptWriter(Protocol):
    def ownership(self, path: Path, value: OwnershipJournal | PublicationIntent) -> None: ...
    def publish(self, path: Path, value: Any) -> None: ...
    def read(self, path: Path) -> Any: ...


@dataclass(frozen=True, repr=False)
class Effects:
    # D/E add lifetime management and receipts without replacing these seams.
    authority_factory: Callable[[str], Any]
    issuer_factory: Callable[[str], Any]
    migration_runner: Callable[..., Any]
    seed_operation: Callable[..., Any]
    process_launcher: Callable[..., OwnedProcess]
    readiness_probe: Callable[[ReadyRequest], ReadyProof]
    browser_runner: Callable[..., OwnedBrowser] | None
    canonical_reader: Callable[[CanonicalRequest], WorkerDescriptor | CanonicalProof]
    clock: Clock
    host: Host
    receipt_writer: ReceiptWriter


def _hash_ok(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _files_ok(files):
    return bool(files) and len({f.path for f in files}) == len(files) and all(
        _hash_ok(f.sha256) and f.size > 0 for f in files)


def preflight(config: Config, *, effects: Effects) -> Plan:
    """Refuse before acquiring authority; reserve only after all admission checks."""
    try:
        if not isinstance(config.run_id, str) or re.fullmatch(r"[a-z0-9][a-z0-9-]{7,63}", config.run_id) is None:
            _refuse()
        ports = config.ports.values()
        if any(type(p) is not int or not 1 <= p <= 65535 for p in ports) or len(set(ports)) != 4:
            _refuse()
        facts = effects.host.inspect(config)
        _authority_kind(facts.environment)
        if facts.posture not in {"local", "test"} or facts.legacy_posture not in {"", "local", "test"} or facts.synthetic:
            _refuse(FailureCode.ENVIRONMENT)
        if facts.run_root_exists:
            _refuse()
        if not facts.paths_safe:
            _refuse()
        if not facts.output_owned:
            _refuse()
        if facts.receipt_exists:
            _refuse()
        if facts.occupied_ports:
            _refuse()
        if facts.missing_modules:
            _refuse()
        if not facts.harness_present:
            _refuse()
        if not facts.browser_dependency_present:
            _refuse(FailureCode.BROWSER_UNAVAILABLE)
        if facts.unapproved_dotenv:
            _refuse(FailureCode.ENVIRONMENT)
        try:
            source = effects.host.source_snapshot()
        except Exception:
            _refuse(FailureCode.SOURCE)
        if (not _hash_ok(source.dirty_tree_sha256) or not _hash_ok(source.tested_source_manifest_sha256)
                or re.fullmatch(r"[0-9a-f]{40}", source.commit) is None or not _files_ok(source.files)):
            _refuse(FailureCode.SOURCE)
        if not set(source.declared_untracked).issubset({f.path for f in source.files}):
            _refuse(FailureCode.SOURCE)
        migrations = tuple(f for f in source.files if f.path in source.expected_migrations)
        if not migrations or {f.path for f in migrations} != set(source.expected_migrations):
            _refuse(FailureCode.SOURCE)
        try:
            descriptor = effects.canonical_reader(CanonicalRequest())
        except Exception:
            _refuse(FailureCode.SOURCE)
        if (descriptor.tool != WORKER_TOOL or not _hash_ok(descriptor.source_sha256)
                or descriptor.source_revision != "sha256:" + descriptor.source_sha256
                or not descriptor.source_manifest or not descriptor.runtime):
            _refuse(FailureCode.SOURCE)
        plan = Plan(config, source, migrations, descriptor, facts)
        reservation = effects.host.reserve(plan)
        plan = replace(plan, reservation=reservation)
        try:
            effects.receipt_writer.ownership(config.run_root / "ownership.json", OwnershipJournal(
                config.run_id, config.run_root, config.artifact_root, effects.clock.utc_now()))
        except Exception:
            reservation.release_ports()
            # Leave the uniquely created root for record D's ownership recovery.
            _refuse()
        return plan
    except ProofRefused:
        raise
    except Exception:
        _refuse()


_OS_KEYS = frozenset({"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
                      "USERPROFILE", "LOCALAPPDATA", "APPDATA", "HOME", "LANG", "LC_ALL"})


class _PrivateEnvironment(dict):
    def __repr__(self):
        return "<private project environment>"

    __str__ = __repr__

    def copy(self):
        return _RedactedEnvironment(self)


def _essential_environment(environ):
    return {key.upper(): value for key, value in environ.items() if key.upper() in _OS_KEYS}


def _load_file(name, path, *, package=False):
    spec = importlib.util.spec_from_file_location(name, path,
        submodule_search_locations=[str(path.parent)] if package else None)
    if spec is None or spec.loader is None:
        raise RuntimeError("module unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Load on first environment construction, never while importing this module.
_libpq_cleaner = None


def _clean_libpq(environ):
    global _libpq_cleaner
    with _IMPORT_LOCK:
        if _libpq_cleaner is None:
            _libpq_cleaner = _load_file(
                "_leaf_project_pg_environment", REPO / "scripts/proof_project_pg.py"
            ).clean_libpq_environment
    return _libpq_cleaner(environ)


def project_environment(plan: Plan, *, os_env: Mapping[str, str], dsn: str,
                        profile_env: Mapping[str, str], organization_id: str) -> dict[str, str]:
    """Project consumers receive one authority and no ambient policy or credentials."""
    try:
        root, ports = plan.config.run_root, plan.config.ports
        if any(key.upper() in {"DATABASE_URL", "LEAF_HARNESS_DATABASE_URL"}
               or (key.upper() == "LEAF_AUTH_LIVE" and value != "1")
               for key, value in os_env.items()):
            _refuse(FailureCode.ENVIRONMENT)
        expected = {"LEAF_PROOF_PROFILE", "LEAF_PROOF_PROJECT_RUN_ID",
                    "LEAF_PROOF_PROJECT_KEY_SHA256", "LEAF_PROOF_PROJECT_JWKS_FILE"}
        if (set(profile_env) != expected or profile_env["LEAF_PROOF_PROFILE"] != PROFILE
                or profile_env["LEAF_PROOF_PROJECT_RUN_ID"] != plan.config.run_id
                or not profile_env["LEAF_PROOF_PROJECT_KEY_SHA256"]
                or Path(profile_env["LEAF_PROOF_PROJECT_JWKS_FILE"]) != root / "public-jwks.json"
                or not isinstance(dsn, str) or not dsn.strip() or not organization_id):
            _refuse(FailureCode.ENVIRONMENT)
        env = _PrivateEnvironment(_clean_libpq(_essential_environment(os_env)))
        env.update(profile_env)
        env.update({
            "LEAF_RUNTIME_ENV": "local", "LEAF_ENV": "local", "LEAF_AUTH_LIVE": "1",
            "LEAF_TENANT_CLAIM_NS": CLAIM_NAMESPACE,
            "LEAF_SOLAR_PROJECT_RUN_ENABLED": "1", "LEAF_UPLOAD_IMPORT_MUTATIONS_ENABLED": "1",
            "LEAF_AGENT_MOCK": "1", "LEAF_GUEST_DXF_EXTRACT": "local",
            "LEAF_AUTHORED_EXECUTION": "0", "LEAF_HARNESS_AUTHORING_MODE": "disabled",
            "LEAF_AGENT_STORE": "postgres", "LEAF_HARNESS_SESSION_STORE": "postgres",
            "VITE_CAD_EDIT": "1", "VITE_SOLAR_FLOW_RAIL": "1", "VITE_SOLAR_SETTINGS_FORM": "1",
            "VITE_TENANT_ID": organization_id, "VITE_MOCK": "0",
            "LEAF_CANONICAL_WORKER_TOOL": WORKER_TOOL, "LEAF_CANONICAL_WORKER_REQUIRED": "1",
            "LEAF_EXPECTED_WORKER_REVISION": plan.descriptor.source_revision,
            "LEAF_CANONICAL_WORKER_ID": "project:" + plan.config.run_id,
            "LEAF_SOURCE_SHA": plan.source.commit, "LEAF_SOURCE_COMMIT": plan.source.commit,
            "LEAF_BUILD_REVISION": plan.source.commit,
            "LEAF_CAD_ENGINE_PKG_DIR": str(root / "cad-engine"),
            "LEAF_E2E_MANAGED": "1", "PYTHONDONTWRITEBYTECODE": "1",
            "LEAF_CUSTOMIZATION_R5_MODE": "off", "LEAF_CUSTOMIZATION_R6_MODE": "off",
        })
        env["DATABASE_URL"] = dsn
        env["LEAF_HARNESS_DATABASE_URL"] = dsn
        web, app = f"http://127.0.0.1:{ports.web}", f"http://127.0.0.1:{ports.app}"
        env.update({"LEAF_E2E_BASE_URL": web, "LEAF_E2E_API_BASE": app,
                    "VITE_API_BASE": app, "LEAF_APP_URL": app, "LEAF_CORS_ORIGINS": web,
                    "BROKER_URL": f"http://127.0.0.1:{ports.broker}",
                    "WEB_PORT": str(ports.web), "APP_PORT": str(ports.app),
                    "BROKER_PORT": str(ports.broker), "HARNESS_PORT": str(ports.harness)})
        stores = {"LEAF_STORE_DIR": "drawings", "LEAF_GUEST_STORE_DIR": "guest-drawings",
                  "LEAF_UPLOADS_DIR": "uploads", "JOBS_DB": "jobs.db", "SESSIONS_DB": "sessions.db",
                  "LEAF_AGENT_LEDGER": "agent-ledger.jsonl", "BROKER_LEDGER": "broker-ledger.jsonl",
                  "BROKER_TENANTS": "broker-tenants.json", "LEAF_GRANTS_DIR": "grants",
                  "LEAF_GRANT_FILE": "no-legacy-grant.token", "LEAF_TENANTS_DIR": "tenants",
                  "LEAF_TENANT_GIT_DIR": "tenant-git", "LEAF_TENANT_MCP_DIR": "tenant-mcp",
                  "LEAF_MARATHON_RUNS_DIR": "marathon-runs"}
        env.update({key: str(root / relative) for key, relative in stores.items()})
        return env
    except ProofRefused:
        raise
    except Exception:
        _refuse(FailureCode.ENVIRONMENT)


def _await_runtime(spec, effects, scope):
    process = effects.process_launcher(spec, scope=scope)
    while (code := process.poll()) is None:
        if effects.clock.monotonic() >= spec.deadline:
            process.stop(effects.clock.monotonic() + 5.0)
            _refuse(FailureCode.RUNTIME, Stage.RUNTIME)
        effects.clock.wait(0.05)
    if code != 0:
        _refuse(FailureCode.RUNTIME, Stage.RUNTIME)
    if not process.stop(effects.clock.monotonic() + 5.0):
        _refuse(FailureCode.RUNTIME, Stage.RUNTIME)


def prepare_runtime(plan: Plan, *, effects: Effects, scope: RunScope) -> RuntimeProof:
    """Build into reserved private directories and bind inputs and outputs."""
    try:
        facts = plan.host
        if plan.reservation is None:
            _refuse(FailureCode.RUNTIME, Stage.RUNTIME)
        if len(facts.vendor_candidates) != 1:
            _refuse(FailureCode.RUNTIME, Stage.RUNTIME)
        vendor = facts.vendor_candidates[0]
        relative = vendor.relative_to(REPO).as_posix() + "/"
        vendor_source = tuple(f for f in plan.source.files if f.path.startswith(relative))
        if (not _files_ok(facts.requirements) or {f.path for f in facts.requirements} != set(REQUIREMENTS)
                or not _files_ok(vendor_source) or not facts.python):
            _refuse(FailureCode.RUNTIME, Stage.RUNTIME)
        if any(requirement not in plan.source.files for requirement in facts.requirements):
            _refuse(FailureCode.SOURCE, Stage.RUNTIME)
        tools = dict(facts.tools)
        if set(tools) != set(TOOLS) or not all(tools.values()):
            _refuse(FailureCode.RUNTIME, Stage.RUNTIME)
        root = plan.config.run_root
        env = _clean_libpq(_essential_environment(facts.environment))
        env.update({"CARGO_TARGET_DIR": str(root / "cargo-target"),
                    "RUSTFLAGS": '--cfg getrandom_backend="wasm_js"',
                    "PYTHONDONTWRITEBYTECODE": "1"})
        for name in TOOLS:
            _await_runtime(ProcessSpec("runtime-version", (tools[name], "--version"), REPO, env,
                effects.clock.monotonic() + VERSION_TIMEOUT_S, root / (name + "-version.txt")), effects, scope)
        _await_runtime(ProcessSpec("runtime-build", (tools["wasm-pack"], "build", "--release",
            "--target", "web", ".", "--out-dir", str(root / "cad-engine"), "--out-name", "engine"),
            root / "vendor-source", env, effects.clock.monotonic() + BUILD_TIMEOUT_S), effects, scope)
        built = effects.host.inspect(plan.config)
        if {f.path for f in built.engine_outputs} != set(ENGINE_OUTPUTS):
            _refuse(FailureCode.RUNTIME, Stage.RUNTIME)
        if (not _files_ok(built.engine_outputs) or built.requirements != facts.requirements
                or set(dict(built.toolchain)) != set(TOOLS) or not all(dict(built.toolchain).values())):
            _refuse(FailureCode.RUNTIME, Stage.RUNTIME)
        if effects.host.source_snapshot() != plan.source:
            _refuse(FailureCode.SOURCE, Stage.RUNTIME)
        return RuntimeProof(facts.requirements, facts.python, built.toolchain, vendor_source,
                            built.engine_outputs, root / "cad-engine", root / "cargo-target")
    except ProofRefused:
        raise
    except Exception:
        _refuse(FailureCode.RUNTIME, Stage.RUNTIME)


@dataclass(frozen=True)
class AuthorityOwnership:
    run_id: str
    names: tuple[str, ...]
    labels: tuple[tuple[str, str], ...]
    containers: tuple[str, ...] = ()
    networks: tuple[str, ...] = ()
    kind: str = "docker"
    data_dir: str = ""
    port: int = 0
    present: bool = False
    pid: int = 0
    start_id: str = ""
    started_at: int = 0


@dataclass(frozen=True, repr=False)
class MigrationRequest:
    run_id: str
    migration_root: Path
    expected_manifest: tuple[FileProof, ...]
    environment: Mapping[str, str] = field(repr=False)
    deadline: float
    result_path: Path

    def __post_init__(self):
        object.__setattr__(self, "environment", _RedactedEnvironment(self.environment))


@dataclass(frozen=True)
class MigrationProof:
    run_id: str
    manifest_sha256: str
    ok: bool
    database: str
    schema: str
    migration_count: int
    applied_migration_count: int


@dataclass(frozen=True, repr=False)
class SeedRequest:
    run_id: str
    subject: str
    environment: Mapping[str, str] = field(repr=False)
    deadline: float
    result_path: Path
    authority: str = "auth0"
    tier: str = TIER

    def __post_init__(self):
        object.__setattr__(self, "environment", _RedactedEnvironment(self.environment))


@dataclass(frozen=True)
class BindingProof:
    organization_id: str
    binding_id: str
    subject: str
    tier: str
    authority: str
    role: str
    status: str


@dataclass(frozen=True)
class TransportProof:
    ok: bool
    server_version: str
    round_trip_bytes: int


@dataclass(frozen=True)
class DatabaseProof:
    authority_run_id: str
    ownership: AuthorityOwnership
    transport: str
    readiness: TransportProof
    migration_manifest: tuple[FileProof, ...]
    migration_manifest_sha256: str
    schema_status: MigrationProof
    allocation_seconds: float
    relay_seconds: float
    migration_seconds: float


@dataclass(frozen=True)
class IdentityProof:
    public_key_thumbprint: str
    issuer: str
    audience: str
    organization_id: str
    binding_id: str
    subject: str
    tier: str


class _SecretToken:
    """A minted token; repr, str and format never show its value."""
    __slots__ = ("_value",)

    def __init__(self, value):
        if type(value) is not str or not value:
            raise ValueError("token")
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self):
        return "<redacted token>"

    __str__ = __repr__


@dataclass(frozen=True, repr=False)
class IdentityLease:
    """Only C decides when to mint; claims remain bound to B's verified row."""
    issuer: Any = field(repr=False)
    run_id: str
    jwks_path: Path
    profile: Mapping[str, str] = field(repr=False)
    binding: BindingProof | None = None

    def __post_init__(self):
        object.__setattr__(self, "profile", _RedactedEnvironment(self.profile))

    def mint(self, *, now: int, lifetime_s: int = 900):
        if self.issuer is None or self.binding is None:
            _refuse(FailureCode.IDENTITY, Stage.IDENTITY)
        binding = self.binding
        if binding.subject != "leaf-proof-project|" + self.run_id:
            _refuse(FailureCode.BINDING, Stage.BINDING)
        try:
            return _SecretToken(self.issuer.mint(self.run_id, tenant_id=binding.organization_id,
                org_id=binding.organization_id, tier=binding.tier, claim_ns=CLAIM_NAMESPACE,
                lifetime_s=lifetime_s, now=now))
        except Exception:
            _refuse(FailureCode.IDENTITY, Stage.IDENTITY)


@dataclass(frozen=True, repr=False)
class PreparedAuthority:
    authority: Any = field(repr=False)
    dsn: str = field(repr=False)
    identity_lease: IdentityLease = field(repr=False)
    database: DatabaseProof
    identity: IdentityProof
    binding: BindingProof
    plan: Plan | None = field(default=None, repr=False)


def _manifest_hash(files):
    return hashlib.sha256(json.dumps([(f.path, f.sha256, f.size) for f in files],
                                    separators=(",", ":")).encode()).hexdigest()


def _check_corpus(expected, *, effects):
    try:
        actual, tracked = effects.host.migration_corpus()
        if (not _files_ok(expected) or tuple(sorted(expected, key=lambda f: f.path)) != expected
                or any(re.fullmatch(r"platform/migrations/[0-9]{4}_[^/]+\.sql", f.path) is None for f in expected)
                or tuple(f.path for f in expected) != tuple(tracked)
                or actual != expected):
            _refuse(FailureCode.MIGRATION_CORPUS, Stage.MIGRATION)
    except ProofRefused:
        raise
    except Exception:
        _refuse(FailureCode.MIGRATION_CORPUS, Stage.MIGRATION)


def _check_entry(effects):
    try:
        present = effects.host.migration_entry_present()
    except Exception:
        present = False
    if not present:
        _refuse(FailureCode.MIGRATION_ENTRY, Stage.MIGRATION)


def _nonempty_dsn(environment):
    dsn = environment.get("DATABASE_URL")
    if not isinstance(dsn, str) or not dsn.strip():
        _refuse(FailureCode.ENVIRONMENT, Stage.MIGRATION)
    return dsn


def _check_deadline(request, effects):
    if (type(request.deadline) not in {int, float} or not math.isfinite(request.deadline)
            or effects.clock.monotonic() >= request.deadline):
        _refuse(FailureCode.MIGRATION_TIMEOUT, Stage.MIGRATION)


def _validate_migration(proof, request):
    if (type(proof) is not MigrationProof or proof.run_id != request.run_id
            or proof.manifest_sha256 != _manifest_hash(request.expected_manifest)
            or proof.ok is not True or proof.database != "leaf" or proof.schema != "public"
            or type(proof.migration_count) is not int or type(proof.applied_migration_count) is not int
            or proof.migration_count != len(request.expected_manifest)
            or proof.applied_migration_count != len(request.expected_manifest)):
        _refuse(FailureCode.MIGRATION_RESULT, Stage.MIGRATION)
    return proof


def migration_child(request: MigrationRequest, *, effects: Effects) -> MigrationProof:
    """Run only inside a fresh child in production; tests inject every DB effect."""
    _nonempty_dsn(request.environment)  # Must precede even the database import seam.
    _check_deadline(request, effects)
    if request.migration_root != REPO / "platform/migrations":
        _refuse(FailureCode.MIGRATION_CORPUS, Stage.MIGRATION)
    _check_entry(effects)
    _check_corpus(request.expected_manifest, effects=effects)
    db = None
    try:
        try:
            db = effects.host.database_module(request.environment)
        except Exception:
            _refuse(FailureCode.MIGRATION_ENTRY, Stage.MIGRATION)
        if not all(callable(getattr(db, name, None)) for name in
                   ("apply_migration", "assert_schema_current", "reset_pool")):
            _refuse(FailureCode.MIGRATION_ENTRY, Stage.MIGRATION)
        db.apply_migration()
        try:
            status = db.assert_schema_current()
        except Exception:
            _refuse(FailureCode.SCHEMA, Stage.MIGRATION)
        empty_fields = {"missing": dict, "missing_migrations": list, "migration_hash_drift": list,
                        "invalid_constraints": list, "invalid_indexes": list, "invalid_triggers": list}
        if (type(status) is not dict or status.get("ok") is not True
                or any(type(status.get(key)) is not kind or status[key] for key, kind in empty_fields.items())):
            _refuse(FailureCode.SCHEMA, Stage.MIGRATION)
        proof = MigrationProof(request.run_id, _manifest_hash(request.expected_manifest), status["ok"],
            status["database"], status["schema"], status["migration_count"], status["applied_migration_count"])
        _check_deadline(request, effects)
        result = _validate_migration(proof, request)
    except ProofRefused:
        raise
    except Exception:
        _refuse(FailureCode.MIGRATION_EXIT, Stage.MIGRATION)
    finally:
        if db is not None:
            failing = sys.exc_info()[0] is not None
            try:
                db.reset_pool()
            except Exception:
                # Preserve an existing refusal; never turn a failed close into success.
                if not failing:
                    _refuse(FailureCode.MIGRATION_EXIT, Stage.MIGRATION)
    # A cleanup that outlives the deadline is a timeout, not a success.
    _check_deadline(request, effects)
    return result


def _validate_binding(proof, request):
    if (type(proof) is not BindingProof or proof.subject != request.subject
            or proof.tier != TIER or proof.authority != "auth0"
            or proof.role != "owner" or proof.status != "active"
            or any(not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9-]{1,64}", value) is None
                   for value in (proof.organization_id, proof.binding_id))):
        _refuse(FailureCode.BINDING, Stage.BINDING)
    return proof


def _empty_ownership(owned):
    return replace(owned, containers=(), networks=(), present=False, pid=0, start_id="", started_at=0)


def _native_public_valid(owned, run_id):
    return (type(owned) is dict and set(owned) == {"run_id", "kind", "data_dir", "port"}
        and owned["run_id"] == run_id and owned["kind"] == "native"
        and owned["data_dir"] == "postgres/data" and type(owned["port"]) is int
        and 1 <= owned["port"] <= 65535)


def _native_subset(observed, captured):
    return (observed.kind == captured.kind == "native"
        and _owned_public(observed) == _owned_public(captured)
        and (observed == captured or observed == _empty_ownership(captured)))


def _validate_ownership(owned, run_id, *, allocated):
    if type(owned) is AuthorityOwnership and owned.kind == "native":
        if (not _native_public_valid(_owned_public(owned), run_id)
                or owned.names != () or owned.labels != () or owned.containers != () or owned.networks != ()
                or owned.present is not allocated or type(owned.pid) is not int
                or type(owned.started_at) is not int
                or (allocated and (owned.pid <= 0 or owned.started_at <= 0
                    or type(owned.start_id) is not str or re.fullmatch(r"ps:[0-9]{1,12}", owned.start_id) is None))
                or (not allocated and (owned.pid != 0 or owned.start_id != "" or owned.started_at != 0))):
            _refuse(FailureCode.ALLOCATION, Stage.ALLOCATION)
        return owned
    name = "leafproof-" + run_id
    if (type(owned) is not AuthorityOwnership or owned.run_id != run_id
            or owned.kind != "docker" or owned.data_dir != "" or owned.port != 0
            or owned.present is not False or owned.pid != 0 or owned.start_id != "" or owned.started_at != 0
            or owned.names != (name, name + "-pg", name + "-relay")
            or owned.labels != (("leaf.proof-project.run", run_id),
                                ("leaf.proof-project.owner", "proof_project_pg"))
            or type(owned.containers) is not tuple or type(owned.networks) is not tuple
            or len(owned.containers) != (2 if allocated else 0)
            or len(owned.networks) != (1 if allocated else 0)
            or len(set(owned.containers + owned.networks)) != len(owned.containers + owned.networks)
            or any(not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{12,64}", value) is None
                   for value in owned.containers + owned.networks)):
        _refuse(FailureCode.ALLOCATION, Stage.ALLOCATION)
    return owned


def _seed_child(request, *, effects):
    _nonempty_dsn(request.environment)
    _check_deadline(request, effects)
    db = None
    try:
        if (request.authority != "auth0" or request.tier != TIER
                or request.subject != "leaf-proof-project|" + request.run_id):
            _refuse(FailureCode.BINDING, Stage.BINDING)
        db = effects.host.database_module(request.environment)
        store = effects.host.store_module()
        org = store.create_org_with_identity("Project proof " + request.run_id, "auth0", request.subject, tier=TIER)
        binding = store.resolve_active_identity_binding("auth0", request.subject)
        if (org.status != "active" or org.tier != TIER or binding is None
                or str(binding.platform_tenant_id) != str(org.org_id)
                or binding.external_authority != "auth0" or binding.external_subject != request.subject):
            _refuse(FailureCode.BINDING, Stage.BINDING)
        role = store.active_identity_role(org.org_id, binding.binding_id)
        proof = BindingProof(str(org.org_id), str(binding.binding_id), binding.external_subject,
                             org.tier, binding.external_authority, role, binding.status)
        _check_deadline(request, effects)
        return _validate_binding(proof, request)
    except ProofRefused:
        raise
    except Exception:
        _refuse(FailureCode.BINDING, Stage.BINDING)
    finally:
        if db is not None:
            failing = sys.exc_info()[0] is not None
            try:
                db.reset_pool()
            except Exception:
                if not failing:
                    _refuse(FailureCode.BINDING, Stage.BINDING)


def prepare_authority(plan: Plan, *, effects: Effects, scope: RunScope) -> PreparedAuthority:
    """Journal allocation, prove migration, then bootstrap one active owner."""
    stage, code = Stage.MIGRATION, FailureCode.MIGRATION_CORPUS
    try:
        if isinstance(scope, RunScope):
            scope._authority_run_id = plan.config.run_id
        if plan.reservation is None:
            _refuse(FailureCode.ALLOCATION, Stage.ALLOCATION)
        _check_entry(effects)
        _check_corpus(plan.migrations, effects=effects)
        run_id, root = plan.config.run_id, plan.config.run_root
        stage, code = Stage.ALLOCATION, FailureCode.ALLOCATION
        ownership = _validate_ownership(effects.host.authority_inventory(run_id), run_id, allocated=False)
        effects.receipt_writer.ownership(root / "authority-planned.json", OwnershipJournal(
            run_id, root, plan.config.artifact_root, effects.clock.utc_now(), ownership))
        start = effects.clock.monotonic()
        try:
            authority = effects.authority_factory(run_id)
        except AllocationCollision:
            _refuse(FailureCode.ALLOCATION, Stage.ALLOCATION)
        # Successful factory return establishes ownership after its collision check.
        # Before return, PP-2 cleans partial startup using only its _created receipt.
        captured = []
        scope.acquire(ResourceKind.AUTHORITY, lambda: _cleanup_authority(
            authority, run_id, effects=effects, ownership=captured[0] if captured else None)
            if not isinstance(scope, RunScope) or scope._settled() else False)
        ownership = _validate_ownership(effects.host.authority_inventory(run_id), run_id, allocated=True)
        captured.append(ownership)
        effects.receipt_writer.ownership(root / "authority-observed.json", OwnershipJournal(
            run_id, root, plan.config.artifact_root, effects.clock.utc_now(), ownership))
        allocated = effects.clock.monotonic()
        stage, code = Stage.TRANSPORT, FailureCode.TRANSPORT
        ready = authority.readiness()
        if (type(ready) is not dict or set(ready) != {"ok", "server_version", "round_trip_bytes"}
                or ready["ok"] is not True or type(ready["round_trip_bytes"]) is not int
                or ready["round_trip_bytes"] != 256 or not isinstance(ready["server_version"], str)
                or re.fullmatch(r"[0-9][A-Za-z0-9 .()+_-]{0,127}", ready["server_version"]) is None):
            _refuse(code, stage)
        transport = TransportProof(ready["ok"], ready["server_version"], ready["round_trip_bytes"])
        relayed = effects.clock.monotonic()
        dsn = authority.dsn()
        env = _PrivateEnvironment(_clean_libpq(_essential_environment(plan.host.environment)))
        env.update(DATABASE_URL=dsn, PYTHONDONTWRITEBYTECODE="1")
        _nonempty_dsn(env)
        stage, code = Stage.MIGRATION, FailureCode.MIGRATION_EXIT
        request = MigrationRequest(run_id, REPO / "platform/migrations", plan.migrations,
                                   env, relayed + MIGRATION_TIMEOUT_S, root / "migration-result.json")
        _check_corpus(plan.migrations, effects=effects)
        migration = effects.migration_runner(request, scope=scope)
        _check_deadline(request, effects)
        migration = _validate_migration(migration, request)
        migrated = effects.clock.monotonic()
        stage, code = Stage.IDENTITY, FailureCode.IDENTITY
        issuer = effects.issuer_factory(run_id)
        jwks_path = root / "public-jwks.json"
        lease = IdentityLease(issuer, run_id, jwks_path, {})
        scope.acquire(ResourceKind.IDENTITY, lambda: effects.host.close_identity(lease))
        if (issuer.run_id != run_id or issuer.issuer != "https://proof-project.leaf.invalid/" + run_id + "/"
                or issuer.audience != "leaf-proof-project:" + run_id
                or not isinstance(issuer.thumbprint, str)
                or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", issuer.thumbprint) is None):
            _refuse(code, stage)
        issuer.write_public_jwks(jwks_path)
        profile_env = issuer.profile_env(jwks_path)
        expected_profile = {"LEAF_PROOF_PROFILE": PROFILE, "LEAF_PROOF_PROJECT_RUN_ID": run_id,
            "LEAF_PROOF_PROJECT_KEY_SHA256": issuer.thumbprint, "LEAF_PROOF_PROJECT_JWKS_FILE": str(jwks_path)}
        if profile_env != expected_profile:
            _refuse(code, stage)
        object.__setattr__(lease, "profile", _RedactedEnvironment(profile_env))
        stage, code = Stage.BINDING, FailureCode.BINDING
        seed = SeedRequest(run_id, "leaf-proof-project|" + run_id, env,
                           effects.clock.monotonic() + MIGRATION_TIMEOUT_S, root / "binding-result.json")
        binding = _validate_binding(effects.seed_operation(seed, scope=scope), seed)
        _check_deadline(seed, effects)
        object.__setattr__(lease, "binding", binding)
        database = DatabaseProof(run_id, ownership, "native-loopback" if ownership.kind == "native" else "host-relay", transport, plan.migrations,
            _manifest_hash(plan.migrations), migration, allocated - start, relayed - allocated, migrated - relayed)
        identity = IdentityProof(issuer.thumbprint, issuer.issuer, issuer.audience, binding.organization_id,
                                 binding.binding_id, binding.subject, binding.tier)
        return PreparedAuthority(authority, dsn, lease, database, identity, binding, plan)
    except ProofRefused:
        raise
    except Exception:
        _refuse(code, stage)


STACK_TIMEOUT_S = 120.0
BROWSER_TIMEOUT_S = 300.0
HEARTBEAT_MAX_AGE_S = 30.0
STACK_ROLES = ("broker", "worker", "harness", "app", "web")
APP_DEPENDENCIES = ("broker", "harness", "database", "worker", "durable_stores", "build")
CHECKPOINTS = ("ui_project_created", "ui_dxf_imported", "bearer_present",
    "actor_header_absent", "bearerless_read_refused", "bearerless_mutation_refused",
    "bearerless_no_mutation", "ui_checkout_acquired", "settings_submitted",
    "worker_published", "settings_readback", "settings_reopened")


@dataclass(frozen=True, repr=False)
class ReadyRequest:
    plan: Plan
    launcher: OwnedProcess = field(repr=False)
    environment: Mapping[str, str] = field(repr=False)
    organization_id: str
    binding_id: str
    public_key_thumbprint: str
    bearer: _SecretToken = field(repr=False)
    deadline: float
    expected_identities: tuple[ProcessIdentity, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "environment", _RedactedEnvironment(self.environment))

    @property
    def worker_id(self):
        return "project:" + self.plan.config.run_id


@dataclass(frozen=True)
class HeartbeatProof:
    worker_id: str
    tool_name: str
    runtime: str
    source_revision: str
    source_sha256: str
    observed_at: datetime


@dataclass(frozen=True)
class AppReadiness:
    source_revision: str
    dependencies: tuple[tuple[str, str, bool], ...]


@dataclass(frozen=True)
class ReadyProof:
    owned_process_identities: tuple[ProcessIdentity, ...]
    source_revision: str
    app_readiness: AppReadiness
    heartbeat: HeartbeatProof
    authenticated_org: bool
    bearerless_refused: bool


@dataclass(frozen=True)
class CanonicalEvidence:
    organization_id: str
    project_id: str
    drawing_id: str
    input_version_id: str
    output_version_id: str
    job_id: str
    binding_id: str
    worker_id: str
    checkout_fence: int
    before_settings_sha256: str
    settings_sha256: str
    reopened_settings_sha256: str


@dataclass(frozen=True)
class CanonicalProof:
    evidence: CanonicalEvidence
    stored_settings_sha256: str
    reopened_settings_sha256: str


@dataclass(frozen=True, repr=False)
class BrowserRequest:
    run_id: str
    source_binding: SourceProof
    public_key_thumbprint: str
    organization_id: str
    binding_id: str
    worker_id: str
    web_origin: str
    app_origin: str
    required_test_ids: tuple[str, ...]
    environment: Mapping[str, str] = field(repr=False)
    output_root: Path
    deadline: float

    def __post_init__(self):
        object.__setattr__(self, "environment", _RedactedEnvironment(self.environment))


@dataclass(frozen=True)
class TestOutcome:
    test_id: str
    status: str
    retry: int


@dataclass(frozen=True, repr=False)
class BrowserResult:
    run_id: str
    source_binding: SourceProof
    public_key_thumbprint: str
    exit_code: int
    selected_test_ids: tuple[str, ...]
    started_test_ids: tuple[str, ...]
    completed_test_ids: tuple[str, ...]
    outcomes: tuple[TestOutcome, ...]
    annotation_types: tuple[str, ...]
    checkpoints: Mapping[str, bool]
    canonical: CanonicalEvidence

    def __post_init__(self):
        object.__setattr__(self, "checkpoints", MappingProxyType(dict(self.checkpoints)))


class OwnedBrowser(Protocol):
    process: OwnedProcess
    def poll(self) -> BrowserResult | None: ...
    def stop(self, deadline: float) -> bool: ...


@dataclass(frozen=True)
class ExecutionProof:
    stack: ReadyProof
    worker_identity: str
    initial_heartbeat: HeartbeatProof
    final_heartbeat: HeartbeatProof
    playwright: BrowserResult
    canonical: CanonicalProof
    stack_seconds: float
    browser_seconds: float
    observed_rows: tuple[str, ...] = ("P01", "P04", "P08", "P09", "P11")


def _remaining(deadline, effects, code, stage):
    remaining = deadline - effects.clock.monotonic()
    if not math.isfinite(remaining) or remaining <= 0:
        _refuse(code, stage)
    return remaining


def _owned_stack(request, effects):
    launcher = request.launcher
    if launcher.poll() is not None:
        _refuse(FailureCode.LAUNCH, Stage.STACK)
    raw = effects.host.read_stack_ready(request.plan.config.run_root / "stack-ready.json")
    if raw is None:
        return None
    if (type(raw) is not dict or set(raw) != {"launcher_pid", "pids", "ports"}
            or type(raw["pids"]) is not dict or set(raw["pids"]) != set(STACK_ROLES)
            or raw["ports"] != dict(zip(("web", "app", "broker", "harness"), request.plan.config.ports.values()))):
        _refuse(FailureCode.LAUNCH, Stage.STACK)
    if (type(raw["launcher_pid"]) is not int or raw["launcher_pid"] != launcher.identity.pid
            or launcher.identity.role != "stack-launcher"):
        _refuse(FailureCode.OWNERSHIP, Stage.STACK)
    if (any(type(pid) is not int or pid <= 0 for pid in raw["pids"].values())
            or len(set(raw["pids"].values())) != len(STACK_ROLES)):
        _refuse(FailureCode.OWNERSHIP, Stage.STACK)
    try:
        children = launcher.children(raw["pids"])
    except Exception:
        _refuse(FailureCode.OWNERSHIP, Stage.STACK)
    identities = (launcher.identity, *sorted(children, key=lambda item: item.role))
    if (len(children) != len(STACK_ROLES) or {c.role for c in children} != set(STACK_ROLES)
            or any(c.pid != raw["pids"][c.role] or not c.start_id
                   or c.parent_pid != launcher.identity.pid for c in children)
            or (request.expected_identities and identities != request.expected_identities)):
        _refuse(FailureCode.OWNERSHIP, Stage.STACK)
    return identities


def _heartbeat(request, effects):
    try:
        raw = effects.host.worker_heartbeat(request)
        if raw is None:
            _refuse(FailureCode.HEARTBEAT, Stage.READINESS)
        if type(raw) is not HeartbeatProof:
            _refuse(FailureCode.HEARTBEAT, Stage.READINESS)
        if raw.worker_id != request.worker_id:
            _refuse(FailureCode.HEARTBEAT, Stage.READINESS)
        descriptor = request.plan.descriptor
        if (raw.tool_name, raw.runtime, raw.source_revision, raw.source_sha256) != (
                descriptor.tool, descriptor.runtime, descriptor.source_revision, descriptor.source_sha256):
            _refuse(FailureCode.HEARTBEAT, Stage.READINESS)
        age = (effects.clock.utc_now() - raw.observed_at).total_seconds()
        if not math.isfinite(age) or not 0 <= age <= HEARTBEAT_MAX_AGE_S:
            _refuse(FailureCode.HEARTBEAT, Stage.READINESS)
        return raw
    except ProofRefused:
        _refuse(FailureCode.HEARTBEAT, Stage.READINESS)
    except Exception:
        _refuse(FailureCode.HEARTBEAT, Stage.READINESS)


def check_readiness(request: ReadyRequest, *, effects: Effects) -> ReadyProof:
    """Validate OS custody, exact authority heartbeat and both auth controls."""
    code, stage = FailureCode.LAUNCH, Stage.STACK
    try:
        while True:
            _remaining(request.deadline, effects, code, stage)
            identities = _owned_stack(request, effects)
            if identities is not None:
                break
            effects.clock.wait(min(0.1, _remaining(request.deadline, effects, code, stage)))
        code, stage = FailureCode.READINESS, Stage.READINESS
        app = effects.host.app_readiness(request)
        if (type(app) is not AppReadiness or len(app.dependencies) != len(APP_DEPENDENCIES)
                or {item[0] for item in app.dependencies} != set(APP_DEPENDENCIES)
                or any(state != "ready" or type(required) is not bool for _, state, required in app.dependencies)):
            _refuse(FailureCode.READINESS, Stage.READINESS)
        if app.source_revision != request.plan.source.commit:
            _refuse(FailureCode.READINESS, Stage.READINESS)
        heartbeat = _heartbeat(request, effects)
        code = FailureCode.AUTH
        auth = effects.host.organization_read(request, authenticated=True)
        if auth != (200, request.organization_id, TIER, "active"):
            _refuse(FailureCode.AUTH, Stage.READINESS)
        denied = effects.host.organization_read(request, authenticated=False)
        if type(denied) is not int or denied not in (401, 403):
            _refuse(FailureCode.AUTH, Stage.READINESS)
        _remaining(request.deadline, effects, FailureCode.READINESS, Stage.READINESS)
        if _owned_stack(request, effects) != identities:
            _refuse(FailureCode.OWNERSHIP, Stage.STACK)
        # The two organization reads take time; the returned heartbeat must still be fresh.
        code = FailureCode.HEARTBEAT
        heartbeat = _heartbeat(request, effects)
        return ReadyProof(identities, app.source_revision, app, heartbeat, True, True)
    except ProofRefused:
        raise
    except Exception:
        _refuse(code, stage)


def _validate_browser(result, request):
    if type(result) is not BrowserResult:
        _refuse(FailureCode.BROWSER_EVIDENCE, Stage.BROWSER)
    if type(result.exit_code) is not int or result.exit_code != 0:
        _refuse(FailureCode.BROWSER_EXIT, Stage.BROWSER)
    ids = REQUIRED_TEST_IDS
    if (request.required_test_ids != ids or result.run_id != request.run_id
            or result.source_binding != request.source_binding
            or result.public_key_thumbprint != request.public_key_thumbprint
            or any(type(values) is not tuple or len(values) != len(ids) or set(values) != set(ids)
                   for values in (result.selected_test_ids, result.started_test_ids, result.completed_test_ids))
            or len(result.outcomes) != len(ids) or {o.test_id for o in result.outcomes} != set(ids)
            or any(type(o) is not TestOutcome or type(o.status) is not str or o.status != "passed"
                   or type(o.retry) is not int
                   or o.retry != 0 for o in result.outcomes) or result.annotation_types):
        _refuse(FailureCode.BROWSER_EVIDENCE, Stage.BROWSER)
    if set(result.checkpoints) != set(CHECKPOINTS) or any(v is not True for v in result.checkpoints.values()):
        _refuse(FailureCode.BROWSER_EVIDENCE, Stage.BROWSER)
    evidence = result.canonical
    if (type(evidence) is not CanonicalEvidence
            or evidence.organization_id != request.organization_id or evidence.binding_id != request.binding_id
            or evidence.worker_id != request.worker_id or type(evidence.checkout_fence) is not int
            or not 1 <= evidence.checkout_fence <= 9223372036854775807
            or evidence.input_version_id == evidence.output_version_id
            or any(type(value) is not str or re.fullmatch(r"[A-Za-z0-9:|_-]{1,128}", value) is None
                   for value in (evidence.organization_id, evidence.project_id, evidence.drawing_id,
                       evidence.input_version_id, evidence.output_version_id, evidence.job_id,
                       evidence.binding_id, evidence.worker_id))
            or any(not _hash_ok(value) for value in (evidence.before_settings_sha256,
                evidence.settings_sha256, evidence.reopened_settings_sha256))
            or evidence.before_settings_sha256 == evidence.settings_sha256
            or evidence.settings_sha256 != evidence.reopened_settings_sha256):
        _refuse(FailureCode.BROWSER_EVIDENCE, Stage.BROWSER)
    return result


def run_project_pipeline(prepared: PreparedAuthority, *, effects: Effects, scope: RunScope) -> ExecutionProof:
    """Return execution evidence only; D/E still own teardown and success publication."""
    code, stage = FailureCode.LAUNCH, Stage.STACK
    try:
        plan = prepared.plan
        if plan is None or plan.reservation is None or plan.required_test_ids != REQUIRED_TEST_IDS:
            _refuse(FailureCode.LAUNCH, Stage.STACK)
        if effects.browser_runner is None or effects.browser_runner is _browser_unavailable:
            _refuse(FailureCode.BROWSER_UNAVAILABLE, Stage.BROWSER)
        env = project_environment(plan, os_env=plan.host.environment, dsn=prepared.dsn,
            profile_env=prepared.identity_lease.profile, organization_id=prepared.binding.organization_id)
        start = effects.clock.monotonic()
        deadline = start + STACK_TIMEOUT_S
        root, ports = plan.config.run_root, plan.config.ports
        argv = (sys.executable, "-B", str(REPO / "scripts/start-leaf.py"), "--strict-ports",
            "--with-harness", "--canonical-worker-tool", WORKER_TOOL,
            "--canonical-worker-id", "project:" + plan.config.run_id,
            "--ready-file", str(root / "stack-ready.json"),
            "--web-port", str(ports.web), "--app-port", str(ports.app),
            "--broker-port", str(ports.broker), "--harness-port", str(ports.harness))
        plan.reservation.release_ports()
        launcher = effects.process_launcher(ProcessSpec("stack-launcher", argv, REPO, env, deadline), scope=scope)
        bearer = prepared.identity_lease.mint(now=int(effects.clock.utc_now().timestamp()), lifetime_s=180)
        request = ReadyRequest(plan, launcher, env, prepared.binding.organization_id,
            prepared.binding.binding_id, prepared.identity.public_key_thumbprint, bearer, deadline)
        ready = effects.readiness_probe(request)
        if type(ready) is not ReadyProof or not ready.authenticated_org or not ready.bearerless_refused:
            _refuse(FailureCode.READINESS, Stage.READINESS)
        _remaining(deadline, effects, FailureCode.READINESS, Stage.READINESS)
        stack_ready = effects.clock.monotonic()
        code, stage = FailureCode.BROWSER_EXIT, Stage.BROWSER
        browser_deadline = stack_ready + BROWSER_TIMEOUT_S
        # This distinct late mint is never added to the shared stack environment.
        token = prepared.identity_lease.mint(now=int(effects.clock.utc_now().timestamp()), lifetime_s=360)
        browser_env = _PrivateEnvironment(_essential_environment(plan.host.environment))
        browser_env.update({"LEAF_E2E_TOKEN": token.reveal(), "LEAF_E2E_MANAGED": "1",
            "LEAF_PROOF_PROJECT_RUN_ID": plan.config.run_id})
        browser_request = BrowserRequest(plan.config.run_id, plan.source, prepared.identity.public_key_thumbprint,
            prepared.binding.organization_id, prepared.binding.binding_id, request.worker_id,
            f"http://127.0.0.1:{ports.web}", f"http://127.0.0.1:{ports.app}", REQUIRED_TEST_IDS,
            browser_env, plan.config.artifact_root, browser_deadline)
        request = replace(request, deadline=browser_deadline, expected_identities=ready.owned_process_identities)
        browser = effects.browser_runner(browser_request, scope=scope)
        while True:
            _remaining(browser_deadline, effects, FailureCode.BROWSER_EXIT, Stage.BROWSER)
            if _owned_stack(request, effects) != ready.owned_process_identities:
                _refuse(FailureCode.OWNERSHIP, Stage.STACK)
            final_heartbeat = _heartbeat(request, effects)
            result = browser.poll()
            exit_code = browser.process.poll()
            if exit_code is not None and (type(exit_code) is not int or exit_code != 0):
                _refuse(FailureCode.BROWSER_EXIT, Stage.BROWSER)
            if result is not None:
                if exit_code != 0:
                    _refuse(FailureCode.BROWSER_EVIDENCE, Stage.BROWSER)
                code = FailureCode.BROWSER_EVIDENCE
                result = _validate_browser(result, browser_request)
                break
            if exit_code is not None:
                _refuse(FailureCode.BROWSER_EVIDENCE, Stage.BROWSER)
            effects.clock.wait(min(0.1, _remaining(browser_deadline, effects, code, stage)))
        _remaining(browser_deadline, effects, code, stage)
        code, stage = FailureCode.CANONICAL, Stage.VERIFICATION
        canonical = effects.canonical_reader(CanonicalRequest("execution", WORKER_TOOL, env,
            result.canonical, browser_deadline))
        if type(canonical) is not CanonicalProof or canonical.evidence != result.canonical:
            _refuse(FailureCode.CANONICAL, Stage.VERIFICATION)
        if (canonical.stored_settings_sha256 != result.canonical.settings_sha256
                or canonical.reopened_settings_sha256 != result.canonical.reopened_settings_sha256):
            _refuse(FailureCode.CANONICAL, Stage.VERIFICATION)
        _remaining(browser_deadline, effects, code, stage)
        if _owned_stack(request, effects) != ready.owned_process_identities:
            _refuse(FailureCode.OWNERSHIP, Stage.STACK)
        final_heartbeat = _heartbeat(request, effects)
        return ExecutionProof(ready, request.worker_id, ready.heartbeat, final_heartbeat,
            result, canonical, stack_ready - start, effects.clock.monotonic() - stack_ready)
    except ProofRefused:
        raise
    except Exception:
        _refuse(code, stage)


_PROC_STAT_FIELDS = 50  # fields after "(comm)" on Linux 3.5 and later (52 in all)


def _proc_decimal(token):
    """Read one /proc decimal field strictly: ASCII digits, no sign, no leading zero."""
    if type(token) is not str or re.fullmatch(r"0|[1-9][0-9]*", token) is None:
        raise RuntimeError("child custody unavailable")
    return int(token)


def _posix_child_identity(stat_text, owner_pid, role, pid):
    """Parse one /proc/<pid>/stat line; fail closed unless the launcher owns a live child.

    start-leaf.py starts each child with start_new_session=True, so a valid child's
    session id is its own pid; the launcher's session is also accepted. The row must
    name the requested pid, carry every field a 3.5+ kernel writes, and spell each
    numeric field it reads as a plain decimal.
    """
    if type(stat_text) is not str:
        raise RuntimeError("child custody unavailable")
    head, opened, _ = stat_text.partition(" (")
    _, closed, tail = stat_text.rpartition(")")
    if not opened or not closed or _proc_decimal(head) != pid:
        raise RuntimeError("child custody unavailable")
    parts = tail.split()
    if len(parts) < _PROC_STAT_FIELDS or parts[0] in {"Z", "X"}:
        raise RuntimeError("child custody unavailable")
    parent, session = _proc_decimal(parts[1]), _proc_decimal(parts[3])
    _proc_decimal(parts[19])
    if parent != owner_pid or session not in {owner_pid, pid}:
        raise RuntimeError("child custody unavailable")
    return ProcessIdentity(role, pid, parts[19], parent)


@dataclass(frozen=True)
class _PosixProcessRow:
    pid: int
    ppid: int
    pgid: int
    start: str


def _read_ps(pid, timeout):
    """Read at most 4 KiB, plus an unretained overflow probe, within two seconds."""
    deadline = time.monotonic() + min(2.0, timeout)
    process = subprocess.Popen(
        ["/bin/ps", "-o", "pid=,ppid=,pgid=,stat=,lstart=", "-p", str(pid)],
        env={"LC_ALL": "C", "TZ": "UTC"}, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
    try:
        raw = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
                raise RuntimeError("process observation timed out")
            chunk = process.stdout.read(min(1024, MAX_OUTPUT_BYTES - len(raw)) or 1)
            if len(raw) + len(chunk) > MAX_OUTPUT_BYTES:
                raise RuntimeError("process observation exceeded bound")
            if not chunk:
                break
            raw.extend(chunk)
        return process.wait(timeout=max(0.0, deadline - time.monotonic())), bytes(raw)
    finally:
        try:
            if process.poll() is None:
                process.kill()
                try:
                    process.wait(timeout=max(0.0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    pass
        finally:
            process.stdout.close()


def _darwin_process_row(pid, *, timeout=2.0):
    """Strict, locale-independent ps identity; only exit 1 with no output is gone."""
    try:
        if type(pid) is not int or pid <= 0 or not 0 < timeout <= 2.0:
            raise ValueError
        code, raw = _read_ps(pid, timeout)
        if code == 1 and raw == b"":
            return None
        if code != 0 or len(raw) > MAX_OUTPUT_BYTES:
            raise ValueError
        text = raw.decode("ascii")
        # macOS ps(1): I/R/S/T/U/Z followed only by documented state flags.
        match = re.fullmatch(
            r"[ \t]*([0-9]+)[ \t]+([0-9]+)[ \t]+([0-9]+)[ \t]+"
            r"([IRSTUZ][+<>AELNSsVWX]*)[ \t]+"
            r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun)[ \t]+"
            r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[ \t]+"
            r"([1-9]|[12][0-9]|3[01])[ \t]+([0-9]{2}):([0-9]{2}):([0-9]{2})"
            r"[ \t]+([0-9]{4})[ \t]*\n?", text)
        if match is None:
            raise ValueError
        actual, parent, group, state, weekday, month, day, hour, minute, second, year = match.groups()
        if _proc_decimal(actual) != pid or state[0] == "Z" or "E" in state:
            raise ValueError
        parent, group = _proc_decimal(parent), _proc_decimal(group)
        months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
        started = datetime(int(year), months.index(month) + 1, int(day),
                           int(hour), int(minute), int(second), tzinfo=timezone.utc)
        if ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[started.weekday()] != weekday:
            raise ValueError
        start = f"{weekday} {month} {day} {hour}:{minute}:{second} {year}"
        return _PosixProcessRow(pid, parent, group, start)
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, OverflowError):
        raise RuntimeError("process identity unavailable") from None


def _posix_process_row(pid, *, timeout=2.0):
    """Guardian observation shares macOS identities with the runtime owner."""
    if _PLATFORM == "darwin":
        return _darwin_process_row(pid, timeout=timeout)
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return None
    head, opened, _ = text.partition(" (")
    _, closed, tail = text.rpartition(")")
    parts = tail.split()
    if (not opened or not closed or _proc_decimal(head) != pid
            or len(parts) < _PROC_STAT_FIELDS or parts[0] in {"Z", "X"}):
        raise RuntimeError("process identity unavailable")
    _proc_decimal(parts[19])
    return _PosixProcessRow(pid, _proc_decimal(parts[1]), _proc_decimal(parts[2]), parts[19])


def _process_child(owner, role, pid):
    """Observe live OS identities; a JSON PID alone never establishes custody."""
    if _PLATFORM == "darwin":
        row = _darwin_process_row(pid)
        if row is None or row.ppid != owner.identity.pid or row.pgid not in {owner.identity.pid, pid}:
            raise RuntimeError("child custody unavailable")
        return ProcessIdentity(role, pid, row.start, row.ppid)
    if os.name != "nt":
        return _posix_child_identity(Path(f"/proc/{pid}/stat").read_text(), owner.identity.pid, role, pid)
    import ctypes
    from ctypes import wintypes
    kernel = owner.job.kernel
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    handle = kernel.OpenProcess(0x1000 | 0x0400, False, pid)
    if not handle:
        raise RuntimeError("child identity unavailable")
    try:
        class Basic(ctypes.Structure):
            _fields_ = [("reserved1", ctypes.c_void_p), ("peb", ctypes.c_void_p),
                ("reserved2", ctypes.c_void_p * 2), ("pid", ctypes.c_size_t), ("parent", ctypes.c_size_t)]
        query = ctypes.WinDLL("ntdll").NtQueryInformationProcess
        query.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG, ctypes.c_void_p]
        query.restype = ctypes.c_long
        basic, member, status = Basic(), wintypes.BOOL(), wintypes.DWORD()
        times = [ctypes.c_uint64() for _ in range(4)]
        if (query(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic), None) != 0
                or basic.pid != pid or basic.parent != owner.identity.pid
                or not kernel.IsProcessInJob(handle, owner.job.handle, ctypes.byref(member)) or not member.value
                or not kernel.GetExitCodeProcess(handle, ctypes.byref(status)) or status.value != 259
                or not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times))):
            raise RuntimeError("child custody unavailable")
        return ProcessIdentity(role, pid, str(times[0].value), basic.parent)
    finally:
        kernel.CloseHandle(handle)


def _project_http(request, path, *, bearer=None):
    import http.client
    remaining = request.deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("readiness deadline")
    connection = http.client.HTTPConnection("127.0.0.1", request.plan.config.ports.app,
                                           timeout=min(2.0, remaining))
    transport, timer = None, None
    def interrupt_read():
        try:
            if transport is not None:
                transport.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    # A peer that trickles headers/body must not extend the absolute deadline.
    try:
        connection.connect()
        transport = connection.sock
        remaining = request.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("readiness deadline")
        transport.settimeout(min(2.0, remaining))
        timer = threading.Timer(min(2.0, remaining), interrupt_read)
        timer.daemon = True
        timer.start()
        headers = {} if bearer is None else {"Authorization": "Bearer " + bearer.reveal()}
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        raw = response.read(65537)
        if len(raw) > 65536 or time.monotonic() >= request.deadline:
            raise ValueError("readiness response bound")
        return response.status, json.loads(raw)
    finally:
        if timer is not None:
            timer.cancel()
        connection.close()


def _project_connection(environment, deadline):
    # Explicit connection authority; no pool and no mutation of parent environment.
    dsn = _nonempty_dsn(environment)
    if _clean_libpq(os.environ) != dict(os.environ):
        raise RuntimeError("database child environment mismatch")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("database deadline")
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(dsn, row_factory=dict_row, connect_timeout=max(1, math.ceil(min(2, remaining))),
        options=f"-c statement_timeout={max(1, int(min(2, remaining) * 1000))} -c default_transaction_read_only=on")


def _settings_hash(context, *, digest):
    settings = context.graph["settings"] if context.graph is not None else None
    return digest(settings)


def _canonical_database_proof(request, *, graph, connection):
    """Verify stored job and historical publication via the canonical verifier."""
    from uuid import UUID
    evidence = request.evidence
    with connection(request.environment, request.deadline) as conn:
        job = conn.execute("SELECT * FROM jobs WHERE job_id=%s AND deleted_at IS NULL",
                           (evidence.job_id,)).fetchone()
        if (job is None or job["status"] != "succeeded" or job["tool_name"] != WORKER_TOOL
                or str(job["org_id"]) != evidence.organization_id
                or str(job["project_id"]) != evidence.project_id
                or job["request_tenant_id"] != evidence.organization_id
                or str(job["input_version_id"]) != evidence.input_version_id
                or str(job["output_version_id"]) != evidence.output_version_id
                or (job.get("provenance") or {}).get("worker_id") != evidence.worker_id):
            _refuse(FailureCode.CANONICAL, Stage.VERIFICATION)
        commit = job["result"]["graph_commit"]
        expected = {key: commit[key] for key in ("schema", "attempt", "tool", "tool_manifest_sha256",
            "parameters", "parent_intake_sha256", "before_graph_sha256", "graph_project_id", "initialized")}
        expected.update(organization_id=evidence.organization_id, project_id=evidence.project_id,
            drawing_id=evidence.drawing_id, parent_version_id=evidence.input_version_id,
            actor_binding_id=evidence.binding_id, checkout_fence=str(evidence.checkout_fence), job_id=evidence.job_id)
        proof = graph.project_graph_job_provenance(UUID(evidence.job_id), commit, expected=expected, conn=conn)
        if proof["output_version_id"] != evidence.output_version_id:
            _refuse(FailureCode.CANONICAL, Stage.VERIFICATION)
        before, _ = graph._stored_context(expected, UUID(evidence.input_version_id), conn)
        stored, _ = graph._stored_context(expected, UUID(evidence.output_version_id), conn)
        if _settings_hash(before, digest=graph.local.digest) != evidence.before_settings_sha256:
            _refuse(FailureCode.CANONICAL, Stage.VERIFICATION)
        stored_hash = _settings_hash(stored, digest=graph.local.digest)
    # Reopen on an independent connection after releasing the first read transaction.
    with connection(request.environment, request.deadline) as conn:
        reopened, _ = graph._stored_context(expected, UUID(evidence.output_version_id), conn)
        reopened_hash = _settings_hash(reopened, digest=graph.local.digest)
    return CanonicalProof(evidence, stored_hash, reopened_hash)


def _canonical_child_entry():
    # Fresh interpreter: server imports may read the scrubbed, run-local environment.
    db = None
    try:
        environment = _PrivateEnvironment(_clean_libpq(os.environ))
        _nonempty_dsn(environment)
        raw = json.loads(environment.pop("LEAF_PROJECT_CANONICAL_REQUEST"))
        evidence = None if raw["evidence"] is None else CanonicalEvidence(**raw["evidence"])
        request = CanonicalRequest(raw["purpose"], WORKER_TOOL, environment, evidence, raw["deadline"], raw["worker_id"])
        with open(os.devnull, "w") as sink, redirect_stdout(sink):
            if request.purpose == "heartbeat":
                with _project_connection(environment, request.deadline) as conn:
                    row = conn.execute("SELECT worker_id, tool_name, runtime, source_revision, source_sha256, observed_at "
                        "FROM canonical_worker_heartbeats WHERE worker_id = %s", (request.worker_id,)).fetchone()
                    public = None if row is None else {"worker_id": row["worker_id"], "tool_name": row["tool_name"],
                        "runtime": row["runtime"], "source_revision": row["source_revision"],
                        "source_sha256": row["source_sha256"], "observed_at": row["observed_at"].isoformat()}
            elif request.purpose == "execution":
                db = _Host.database_module(environment)
                sys.path.insert(0, str(REPO / "server"))
                try:
                    graph = importlib.import_module("solar_project_graph")
                    proof = _canonical_database_proof(request, graph=graph, connection=_project_connection)
                    public = {"stored_settings_sha256": proof.stored_settings_sha256,
                              "reopened_settings_sha256": proof.reopened_settings_sha256}
                finally:
                    sys.path.remove(str(REPO / "server"))
                    db.reset_pool()
                    db = None
            else:
                return 1
        print(json.dumps(public))
        return 0
    except Exception:
        return 1
    finally:
        if db is not None:
            with open(os.devnull, "w") as sink, redirect_stdout(sink):
                db.reset_pool()


def _canonical_execution(request):
    _nonempty_dsn(request.environment)
    if (request.purpose not in {"execution", "heartbeat"}
            or (request.purpose == "execution" and type(request.evidence) is not CanonicalEvidence)):
        _refuse(FailureCode.CANONICAL, Stage.VERIFICATION)
    environment = _PrivateEnvironment(request.environment)
    evidence = request.evidence
    # Explicit public fields only; never serialize runtime state or a capability.
    public = None if evidence is None else {name: getattr(evidence, name) for name in ("organization_id", "project_id", "drawing_id",
        "input_version_id", "output_version_id", "job_id", "binding_id", "worker_id", "checkout_fence",
        "before_settings_sha256", "settings_sha256", "reopened_settings_sha256")}
    environment["LEAF_PROJECT_CANONICAL_REQUEST"] = json.dumps({"evidence": public, "deadline": request.deadline,
        "purpose": request.purpose, "worker_id": request.worker_id})
    output = Path(environment["LEAF_STORE_DIR"]).parent / (request.purpose + "-readback.json")
    code = ("import runpy,sys; m=runpy.run_path(sys.argv[1]); "
            "sys.exit(m['_canonical_child_entry']())")
    process = _RuntimeProcess(ProcessSpec("canonical-child", (sys.executable, "-B", "-c", code,
        str(REPO / "scripts/proof_project.py")), REPO.parent, environment, request.deadline, output))
    try:
        process.start()
        while process.poll() is None:
            remaining = request.deadline - time.monotonic()
            if remaining <= 0:
                _refuse(FailureCode.CANONICAL, Stage.VERIFICATION)
            time.sleep(min(0.05, remaining))
        if process.poll() != 0 or time.monotonic() >= request.deadline:
            _refuse(FailureCode.CANONICAL, Stage.VERIFICATION)
        raw = _Host.read_child_result(output)
        if request.purpose == "heartbeat":
            if raw is None:
                return None
            heartbeat = HeartbeatProof(**dict(raw, observed_at=datetime.fromisoformat(raw["observed_at"])))
            return heartbeat
        if (type(raw) is not dict or set(raw) != {"stored_settings_sha256", "reopened_settings_sha256"}
                or any(not _hash_ok(value) for value in raw.values())):
            _refuse(FailureCode.CANONICAL, Stage.VERIFICATION)
        return CanonicalProof(evidence, raw["stored_settings_sha256"], raw["reopened_settings_sha256"])
    finally:
        stopped = process.stop(request.deadline)
        if stopped:
            output.unlink(missing_ok=True)
        if not stopped or time.monotonic() >= request.deadline:
            _refuse(FailureCode.CANONICAL, Stage.VERIFICATION)


def _later(record):
    def unavailable(*args, **kwargs):
        raise RuntimeError("record " + record + " adapter is not implemented")
    return unavailable


def _browser_unavailable(request, *, scope):
    """PP-3B has not landed; production browser execution remains unavailable."""
    _refuse(FailureCode.BROWSER_UNAVAILABLE, Stage.BROWSER)


class _Clock:
    monotonic = staticmethod(time.monotonic)
    wait = staticmethod(time.sleep)

    @staticmethod
    def utc_now():
        return datetime.now(timezone.utc)


def _canonical_reader(request):
    if request.purpose != "descriptor":
        return _canonical_execution(request)
    # No sys.path change; exact dependency bindings are restored even on error.
    names = ("platform_link", "solar_project_tools", "solver_adapters",
             "solver_adapters.arlo_design", "solver_adapters.autofill", "_leaf_project_worker")
    with _IMPORT_LOCK:
        saved = {name: sys.modules.get(name) for name in names}
        try:
            server = REPO / "server"
            for name in names[:2]:
                _load_file(name, server / (name + ".py"))
            package = _load_file("solver_adapters", server / "solver_adapters/__init__.py", package=True)
            for name in ("arlo_design", "autofill"):
                setattr(package, name, _load_file("solver_adapters." + name, server / "solver_adapters" / (name + ".py")))
            module = _load_file(names[-1], server / "canonical_worker.py")
            descriptor = module.graph_worker_descriptor(request.tool_name, server_dir=server)
            manifest = tuple(sorted((*module.GRAPH_WORKER_MANIFEST,
                                     "solar_tools/" + request.tool_name.replace("-", "_") + ".json")))
            return WorkerDescriptor(descriptor["tool_name"], descriptor["runtime"],
                                    descriptor["source_revision"], descriptor["source_sha256"], manifest)
        finally:
            for name, previous in saved.items():
                if previous is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = previous


def _file_proof(path, name):
    raw = path.read_bytes()
    return FileProof(name, hashlib.sha256(raw).hexdigest(), len(raw))


def _path_safe(path):
    if not path.is_absolute() or ".." in path.parts:
        return False
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            return False
    return True


def _git(*args):
    result = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, timeout=30,
                            env=_essential_environment(os.environ))
    if result.returncode:
        raise RuntimeError("source snapshot unavailable")
    return result.stdout


def _private_directory(path):
    if os.name != "nt":
        path.mkdir(mode=0o700, exist_ok=False)
        return
    import ctypes
    from ctypes import wintypes
    class Attributes(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("descriptor", ctypes.c_void_p),
                    ("inherit", wintypes.BOOL)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    descriptor = ctypes.c_void_p()
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            "D:P(A;OICI;FA;;;OW)", 1, ctypes.byref(descriptor), None):
        raise RuntimeError("private directory unavailable")
    try:
        attributes = Attributes(ctypes.sizeof(Attributes), descriptor, False)
        if not kernel.CreateDirectoryW(str(path), ctypes.byref(attributes)):
            raise RuntimeError("exclusive private directory unavailable")
    finally:
        kernel.LocalFree(descriptor)


def _native_tombstone_present(root):
    """True when a native removal tombstone may exist under root; an unreadable root counts as present (fails closed)."""
    try:
        with os.scandir(root) as entries:
            return any(entry.name.startswith("postgres.removing-") for entry in entries)
    except FileNotFoundError:
        return False
    except OSError:
        return True


class _Host:
    @staticmethod
    def read_stack_ready(path):
        try:
            return _Host.read_child_result(path)
        except FileNotFoundError:
            return None

    @staticmethod
    def app_readiness(request):
        status, payload = _project_http(request, "/api/ready")
        if status != 200 or payload.get("ready") is not True or payload.get("status") != "ready":
            _refuse(FailureCode.READINESS, Stage.READINESS)
        dependencies = payload["dependencies"]
        return AppReadiness(payload["source_revision"], tuple(sorted(
            (name, value["state"], value["required"]) for name, value in dependencies.items())))

    @staticmethod
    def organization_read(request, *, authenticated):
        status, payload = _project_http(request, "/api/orgs/" + request.organization_id,
                                       bearer=request.bearer if authenticated else None)
        if not authenticated:
            return status
        org = payload.get("org", {})
        return status, org.get("org_id"), org.get("tier"), org.get("status")

    @staticmethod
    def worker_heartbeat(request):
        # Exact owner selection: the same-tool newest-worker helper is insufficient.
        return _canonical_execution(CanonicalRequest("heartbeat", WORKER_TOOL, request.environment,
            deadline=min(request.deadline, time.monotonic() + 5.0), worker_id=request.worker_id))

    @staticmethod
    def begin_operation(callback):
        return _PendingCall(callback)

    @staticmethod
    def private_absent(reservation):
        # lexists catches dangling links too. An unlink/rmtree return is not proof.
        return _path_absent(reservation.run_root)

    @staticmethod
    def scrub(reservation, *, keep_safe):
        # Native PG must pass its own marker/identity cleanup first. Generic
        # root scrubbing must never erase a failed stop or a foreign marker.
        native = reservation.run_root / "postgres"
        if (native.exists() or native.is_symlink()
                or _native_tombstone_present(reservation.run_root)):
            return False
        root = reservation.run_root
        if (not root.is_absolute() or root == Path(root.anchor)
                or root.is_relative_to(REPO) or REPO.is_relative_to(root)
                or reservation.artifact_root != root / "artifacts" or not _path_safe(root)):
            return False
        # No retained-file allowlist exists before E. Remove the owned private
        # root even for keep_safe; E may later retain approved public evidence.
        if os.path.lexists(root):
            info = root.stat()
            if reservation.root_identity != (info.st_dev, info.st_ino):
                return False
            shutil.rmtree(root)
        return _Host.private_absent(reservation)

    @staticmethod
    def migration_entry_present():
        return all((REPO / name).is_file() and _path_safe(REPO / name)
                   for name in ("platform/__init__.py", "platform/db.py"))

    @staticmethod
    def migration_corpus():
        # HEAD plus index preserves deletions as missing and admits tracked additions.
        tracked = set(_git("ls-tree", "-r", "--name-only", "HEAD", "platform/migrations").decode().splitlines())
        tracked.update(_git("ls-files", "--cached", "platform/migrations").decode().splitlines())
        expected = tuple(sorted(name for name in tracked
            if re.fullmatch(r"platform/migrations/[0-9]{4}_[^/]+\.sql", name)))
        paths = sorted((REPO / "platform/migrations").glob("[0-9][0-9][0-9][0-9]_*.sql"))
        if any(not _path_safe(path) for path in paths):
            raise RuntimeError("unsafe migration corpus")
        return tuple(_file_proof(path, path.relative_to(REPO).as_posix()) for path in paths), expected

    @staticmethod
    def database_module(environment):
        # This adapter runs in the dedicated interpreter, with the already scrubbed
        # environment supplied at process creation. Never set a parent's DB policy.
        dsn = _nonempty_dsn(environment)
        if (os.environ.get("DATABASE_URL") != dsn
                or _clean_libpq(os.environ) != dict(os.environ)):
            raise RuntimeError("database child environment mismatch")
        with _IMPORT_LOCK:
            package = _load_file("leaf_platform", REPO / "platform/__init__.py", package=True)
            db = _load_file("leaf_platform.db", REPO / "platform/db.py")
            package.db = db
            return db

    @staticmethod
    def store_module():
        # Relative imports reuse exactly the child-local leaf_platform.db pool.
        return importlib.import_module("leaf_platform.store")

    def authority_inventory(self, run_id):
        pg = _pg_module()
        pg._validate(run_id)
        native = getattr(self, "_native_context", None)
        if native is not None:
            expected_run, root, port = native
            if run_id != expected_run:
                raise RuntimeError("native authority run mismatch")
            selected = pg.native_inventory(run_id, root, port)
            return _native_ownership(run_id, port, selected)
        selected = pg._inventory(run_id, pg._docker)
        if (set(selected) != {"containers", "networks"}
                or any(not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{12,64}", value) is None
                       for values in selected.values() for value in values)):
            raise RuntimeError("authority inventory unavailable")
        name = "leafproof-" + run_id
        return AuthorityOwnership(run_id, (name, name + "-pg", name + "-relay"),
            ((pg.RUN_LABEL, run_id), (pg.OWNER_LABEL, pg.OWNER)),
            tuple(selected["containers"]), tuple(selected["networks"]))

    @staticmethod
    def cleanup_authority(run_id):
        return _pg_module().cleanup(run_id)

    def cleanup_owned_authority(self, run_id, ownership):
        pg = _pg_module()
        _validate_ownership(_empty_ownership(ownership), run_id, allocated=False)
        if ownership.kind == "native":
            native = getattr(self, "_native_context", None)
            if native is None or native[0] != run_id or native[2] != ownership.port:
                raise RuntimeError("native authority context missing")
            captured = _native_capture(ownership) if ownership.present else None
            if not pg.cleanup_native(run_id, native[1], native[2], captured,
                    environ=getattr(self, "_authority_environment", None)):
                raise RuntimeError("native authority removal unverified")
            return True
        allowed = set(ownership.containers + ownership.networks)
        def docker(args):
            # PP-2 re-inventories immediately before deletion. Guard the actual
            # destructive call as well, so replacement IDs cannot slip through.
            if args[:2] == ["rm", "-f"] or args[:2] == ["network", "rm"]:
                if len(args) != 3 or args[2] not in allowed:
                    raise RuntimeError("unowned authority resource")
            return pg._docker(args)
        return pg.cleanup(run_id, docker=docker)

    @staticmethod
    def close_identity(lease):
        object.__setattr__(lease, "issuer", None)
        lease.jwks_path.unlink(missing_ok=True)
        if not _path_absent(lease.jwks_path):
            raise RuntimeError("identity removal unverified")

    @staticmethod
    def read_child_result(path):
        if not _path_safe(path):
            raise RuntimeError("unsafe child result")
        with path.open("rb") as stream:
            raw = stream.read(MAX_OUTPUT_BYTES + 1)
        if len(raw) > MAX_OUTPUT_BYTES:
            raise RuntimeError("child result exceeded bound")
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate child result field")
                result[key] = value
            return result
        return json.loads(raw, object_pairs_hook=unique)

    def inspect(self, config):
        root = config.run_root
        safe = (_path_safe(root) and _path_safe(config.artifact_root)
                and not root.is_relative_to(REPO))
        with _IMPORT_LOCK:
            launcher = _load_file("_leaf_project_launcher", REPO / "scripts/start-leaf.py")
        tools = launcher.project_worker_tools()
        if tools is None or WORKER_TOOL not in tools:
            raise RuntimeError("project worker tool unavailable")
        occupied = tuple(port for port in config.ports.values()
                         if not launcher.is_port_free(port, exclusive=True))
        vendor = REPO / "vendor"
        candidates = tuple(sorted(p for p in vendor.iterdir() if p.is_dir()
                           and (p / "Cargo.toml").is_file() and (p / "worker-browser.mjs").is_file()))
        requirements = tuple(_file_proof(REPO / name, name) for name in REQUIREMENTS)
        outputs = []
        for name in ENGINE_OUTPUTS:
            path = root / "cad-engine" / name
            if safe and path.is_file():
                if not _path_safe(path):
                    raise RuntimeError("unsafe runtime output")
                outputs.append(_file_proof(path, name))
        versions = []
        for name in TOOLS:
            path = root / (name + "-version.txt")
            if safe and path.is_file():
                if not _path_safe(path) or path.stat().st_size > MAX_OUTPUT_BYTES:
                    raise RuntimeError("runtime output exceeded bound")
                value = path.read_text(encoding="utf-8").strip()
                if not re.fullmatch(r"[A-Za-z0-9 .+()_:/-]{1,256}", value):
                    raise RuntimeError("runtime version unavailable")
                versions.append((name, value))
        return HostFacts(
            os.environ.get("LEAF_RUNTIME_ENV", "local").strip().lower(),
            os.environ.get("LEAF_ENV", "").strip().lower(),
            any(os.environ.get(k, "0") != "0" for k in ("LEAF_SYNTHETIC_STRING_SIZING", "LEAF_SYNTHETIC_ROOFTOP")),
            root.exists(), safe, config.artifact_root == root / "artifacts",
            (config.artifact_root / "receipt.json").exists(), tuple(occupied),
            tuple(name for name in REQUIRED_MODULES if importlib.util.find_spec(name) is None),
            (REPO / "harness/dist/scripts/serve.js").is_file(),
            (REPO / "web/node_modules/@playwright/test/package.json").is_file(),
            tuple(name for name in (".env", ".env.local", ".env.development", ".env.development.local")
                  if (REPO / "web" / name).exists()), candidates, requirements,
            sys.version.split()[0], tuple((name, shutil.which(name) or "") for name in TOOLS),
            _essential_environment(os.environ), tuple(outputs), tuple(versions))

    def source_snapshot(self):
        commit = _git("rev-parse", "HEAD").decode().strip()
        head = set(_git("ls-tree", "-r", "--name-only", "HEAD").decode().splitlines())
        cached = set(_git("ls-files", "--cached").decode().splitlines())
        untracked = tuple(sorted(name for name in _git("ls-files", "--others", "--exclude-standard").decode().splitlines()
            if name.split("/")[0] in {"scripts", "server", "platform", "web", "harness", "vendor"}))
        files = tuple(_file_proof(REPO / name, name) for name in sorted(head | cached | set(untracked)))
        manifest = json.dumps([(f.path, f.sha256, f.size) for f in files], separators=(",", ":")).encode()
        dirty = _git("diff", "--binary", "HEAD") + b"\0" + json.dumps(
            [(f.path, f.sha256, f.size) for f in files if f.path in untracked], separators=(",", ":")).encode()
        migrations = tuple(sorted(name for name in head | cached if re.fullmatch(r"platform/migrations/[0-9]{4}_[^/]+\.sql", name)))
        return SourceProof(commit, hashlib.sha256(dirty).hexdigest(), hashlib.sha256(manifest).hexdigest(),
                           files, untracked, migrations)

    def reserve(self, plan, *, scope=None):
        config = plan.config
        kind = _authority_kind(plan.host.environment)
        if not _path_safe(config.run_root) or not _path_safe(config.artifact_root):
            raise RuntimeError("unsafe reservation")
        _private_directory(config.run_root)
        root_stat = config.run_root.stat()
        reservation = Reservation(config.run_root, config.artifact_root, (),
                                  (root_stat.st_dev, root_stat.st_ino))
        if scope is not None:
            scope.own_reservation(reservation, keep_safe=config.keep_run_root)
        handles = []
        try:
            _private_directory(config.artifact_root)
            # Cargo may create a lockfile alongside Cargo.toml. Stage the exact
            # measured vendor inputs so even that output stays off the checkout.
            if len(plan.host.vendor_candidates) == 1:
                vendor = plan.host.vendor_candidates[0]
                prefix = vendor.relative_to(REPO).as_posix() + "/"
                destination = config.run_root / "vendor-source"
                _private_directory(destination)
                for proof in plan.source.files:
                    if not proof.path.startswith(prefix):
                        continue
                    source = REPO / proof.path
                    relative = Path(proof.path[len(prefix):])
                    if relative.is_absolute() or ".." in relative.parts or not _path_safe(source):
                        raise RuntimeError("unsafe vendor source")
                    raw = source.read_bytes()
                    if hashlib.sha256(raw).hexdigest() != proof.sha256 or len(raw) != proof.size:
                        raise RuntimeError("vendor source changed")
                    target = destination / relative
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    with target.open("xb") as stream:
                        stream.write(raw)
            for port in config.ports.values():
                handle = socket.socket()
                handles.append(handle)
                object.__setattr__(reservation, "port_handles", tuple(handles))
                if os.name == "nt":
                    handle.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                handle.bind(("127.0.0.1", port))
            if kind == "native":
                handle = socket.socket()
                object.__setattr__(reservation, "database_handle", handle)
                if os.name == "nt":
                    handle.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                handle.bind(("127.0.0.1", 0))
                object.__setattr__(reservation, "database_port", handle.getsockname()[1])
                self._native_context = (config.run_id, config.run_root, reservation.database_port)
            self._reservation = reservation
            self._authority_environment = plan.host.environment
            return reservation
        except Exception:
            reservation.release_database_port()
            for handle in handles:
                handle.close()
            # Remove only empty directories created here; D handles a partially
            # staged root. Never recursively delete an unjournaled tree here.
            try:
                config.artifact_root.rmdir()
                config.run_root.rmdir()
            except OSError:
                pass
            raise RuntimeError("exclusive reservation failed") from None


class _ReceiptWriter:
    def __init__(self):
        self._intents = {}

    def publish(self, path, value):
        return _publish_receipt(self, path, value)

    read = staticmethod(lambda path: _read_receipt(path))

    def ownership(self, path, value):
        if isinstance(value, PublicationIntent):
            return _record_publication(self, path, value)
        public = {"run_id": value.run_id, "run_root": str(value.run_root),
                  "artifact_root": str(value.artifact_root), "created_at": value.created_at.isoformat()}
        if value.authority is not None:
            owned = value.authority
            public["authority"] = _owned_public(owned)
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(public, stream, sort_keys=True)
            stream.write("\n")


class _WindowsJob:
    """Build-only custody: assign the suspended child before it can run."""
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("min_working", ctypes.c_size_t),
                        ("max_working", ctypes.c_size_t), ("active", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("read_ops", "write_ops", "other_ops", "read", "write", "other")]
        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t),
                        ("peak_job", ctypes.c_size_t)]
        self.ctypes = ctypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.c_void_p] * 4
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise RuntimeError("runtime custody unavailable")
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise RuntimeError("runtime custody unavailable")

    def attach_and_resume(self, process):
        ctypes = self.ctypes
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise RuntimeError("runtime custody unavailable")
        times = [ctypes.c_uint64() for _ in range(4)]
        if not self.kernel.GetProcessTimes(int(process._handle), *(ctypes.byref(t) for t in times)):
            raise RuntimeError("runtime identity unavailable")
        resume = ctypes.WinDLL("ntdll").NtResumeProcess
        resume.argtypes = [ctypes.c_void_p]
        resume.restype = ctypes.c_long
        if resume(int(process._handle)) != 0:
            raise RuntimeError("runtime resume failed")
        return str(times[0].value)

    def stop(self, deadline):
        # Query the owned job until every descendant is gone; closing a handle
        # or observing only the launcher exit is not an absence proof.
        from ctypes import wintypes
        ctypes = self.ctypes
        class Accounting(ctypes.Structure):
            _fields_ = [(name, ctypes.c_int64) for name in
                ("user", "kernel", "period_user", "period_kernel")] + [
                (name, wintypes.DWORD) for name in
                ("faults", "total", "active", "terminated")]
        self.kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
            ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
        if not self.handle:
            return False  # A discarded handle cannot prove an empty job.
        if not self.kernel.TerminateJobObject(self.handle, 1):
            return False
        while True:
            counts = Accounting()
            if not self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(counts),
                                                         ctypes.sizeof(counts), None):
                return False
            if counts.active == 0:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.05, remaining))

    def close(self):
        if self.handle:
            if not self.kernel.CloseHandle(self.handle):
                raise RuntimeError("owned job close failed")
            self.handle = None


def _signal_group(pgid, sig):
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass  # An absent owned group needs no further signal.


def _group_empty(pgid, deadline):
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))


class _RuntimeProcess:
    def __init__(self, spec):
        self.spec = spec
        self.process = None
        self.job = None
        self.output = None
        self.identity = None
        self._stopped = False
        self._starting = False
        self._start_attempted = False
        self._stop_requested = False
        self._lock = threading.RLock()
        self._stop_lock = threading.Lock()

    def __repr__(self):
        return "<owned runtime process>"

    def start(self):
        spec = self.spec
        if spec.role not in {"runtime-version", "runtime-build", "migration-child", "seed-child", "stack-launcher", "canonical-child"}:
            raise RuntimeError("unknown process role")
        with self._lock:
            if self._start_attempted or self._stop_requested:
                raise RuntimeError("runtime process start refused")
            self._start_attempted = True
            self._starting = True
        try:
            if spec.output_path is not None:
                self.output = spec.output_path.open("xb")
            if os.name == "nt":
                self.job = _WindowsJob()
            process = subprocess.Popen(spec.argv, cwd=spec.cwd, env=dict(spec.environment),
                stdin=subprocess.DEVNULL, stdout=self.output or subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=(0x08000000 | 0x4) if os.name == "nt" else 0,
                start_new_session=os.name != "nt")
            with self._lock:
                self.process = process
            if self.job is not None:
                start_id = self.job.attach_and_resume(self.process)
            elif _PLATFORM == "darwin":
                row = _darwin_process_row(self.process.pid)
                if row is None:
                    raise RuntimeError("child identity unavailable")
                start_id = row.start
            else:
                # Linux's OS start tick; other POSIX hosts still fail closed.
                start_id = Path(f"/proc/{self.process.pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
            with self._lock:
                self.identity = ProcessIdentity(spec.role, self.process.pid, start_id, os.getpid())
                if self._stop_requested:
                    raise RuntimeError("runtime process start cancelled")
        except BaseException:
            # Even if stop ran before Popen returned, this acquired child must
            # be stopped through its retained handle, never a cached verdict.
            self.stop(time.monotonic() + 5.0)
            raise RuntimeError("runtime process start failed") from None
        finally:
            with self._lock:
                self._starting = False

    def poll(self):
        if self.output is not None and self.spec.output_path.stat().st_size > self.spec.max_output_bytes:
            self.stop(time.monotonic() + 5.0)
            return 1
        return self.process.poll()

    def children(self, pids):
        return tuple(_process_child(self, role, pid) for role, pid in pids.items())

    def stop(self, deadline):
        with self._lock:
            self._stop_requested = True
        # Serialize stop attempts without waiting beyond the caller's budget.
        if not self._stop_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            return False
        try:
            with self._lock:
                if self._stopped and not self._starting:
                    return True
            stopped = self._stop(deadline)
            with self._lock:
                self._stopped = stopped and not self._starting
                return self._stopped
        finally:
            self._stop_lock.release()

    def _stop(self, deadline):
        try:
            if self.process is None:
                if self.job is not None:
                    self.job.close()
                # Closing the job may overlap Popen returning an owned child.
                # Recheck publication and startup together; a child falls through
                # to retained-handle termination even if attachment failed.
                with self._lock:
                    if self.process is None:
                        return self._start_attempted and not self._starting
            def observe():
                if self.process.poll() is not None:
                    return None
                if os.name == "nt":
                    # Popen's retained kernel handle cannot target a reused PID.
                    return self.identity
                if _PLATFORM == "darwin":
                    row = _darwin_process_row(self.process.pid)
                    return None if row is None else ProcessIdentity(
                        self.spec.role, self.process.pid, row.start, row.ppid)
                try:
                    parts = Path(f"/proc/{self.process.pid}/stat").read_text().rsplit(")", 1)[1].split()
                except FileNotFoundError:
                    return None
                return ProcessIdentity(self.spec.role, self.process.pid, parts[19], int(parts[1]))
            def terminate():
                self.process.terminate()  # Retained Popen owner, never a PID lookup.
            def kill():
                if self.job is not None:
                    self.job.stop(deadline)
                elif os.name == "nt":
                    self.process.kill()
                else:
                    _signal_group(self.process.pid, signal.SIGKILL)
            def wait(seconds):
                try:
                    self.process.wait(timeout=seconds)
                    return True
                except subprocess.TimeoutExpired:
                    return False
            if self.identity is None:
                # A suspended child whose identity/assignment failed never ran.
                if os.name == "nt":
                    self.process.kill()
                else:
                    _signal_group(self.process.pid, signal.SIGKILL)
                stopped = wait(max(0.0, deadline - time.monotonic()))
                if self.job is not None:
                    stopped = self.job.stop(deadline) and stopped
                    self.job.close()
                elif os.name != "nt":
                    return stopped and _group_empty(self.process.pid, deadline)
                return stopped
            stopped = stop_owned(StopRequest(self.identity, observe, terminate, kill, wait),
                                 effects=production_effects(), deadline=deadline)
            # The launcher can exit while its descendants remain in our job.
            if self.job is not None:
                empty = self.job.stop(deadline)
                self.job.close()
                return stopped and empty
            if os.name == "nt":
                return False
            # Residual pgid-reuse risk exists only after our owned group is empty;
            # killpg on an empty group raises ProcessLookupError, absorbed above.
            _signal_group(self.process.pid, signal.SIGKILL)
            return stopped and _group_empty(self.process.pid, deadline)
        except BaseException:
            return False
        finally:
            if self.output is not None:
                self.output.close()
                self.output = None


def _launch_runtime(spec, *, scope):
    process = _RuntimeProcess(spec)
    # Registration is synchronous and precedes the first possible child.
    kind = {"migration-child": ResourceKind.MIGRATION_PROCESS,
            "seed-child": ResourceKind.SEED_PROCESS,
            "stack-launcher": ResourceKind.STACK_PROCESS}.get(spec.role, ResourceKind.RUNTIME_PROCESS)
    scope.acquire(kind, lambda: process.stop(time.monotonic() + 5.0))
    process.start()
    return process


def _pg_module():
    with _IMPORT_LOCK:
        name = "_leaf_project_pg_environment"
        return sys.modules.get(name) or _load_file(name, REPO / "scripts/proof_project_pg.py")


class AllocationCollision(RuntimeError):
    """PP-2 rejected existing resources before provisioning anything."""


class _ProjectAuthority:
    """Project cleanup separates transport shutdown from captured-ID removal."""
    def __init__(self, authority):
        self._authority = authority

    def __repr__(self):
        return "<project database authority>"

    def readiness(self):
        return self._authority.readiness()

    def dsn(self):
        return self._authority.dsn()

    def stop(self):
        return self._authority._stop_transport()


def _authority_kind(environ=None):
    source = os.environ if environ is None else environ
    kind = source.get("LEAF_PROOF_PROJECT_AUTHORITY", "docker")
    if kind not in ("docker", "native"):
        _refuse(FailureCode.ENVIRONMENT, Stage.PREFLIGHT)
    return kind


def _native_ownership(run_id, port, selected):
    return AuthorityOwnership(run_id, (), (), kind="native", data_dir="postgres/data", port=port,
        present=selected is not None, pid=selected["pid"] if selected else 0,
        start_id=selected["start_id"] if selected else "", started_at=selected["started_at"] if selected else 0)


def _native_capture(owned):
    return dict(_owned_public(owned), pid=owned.pid, start_id=owned.start_id, started_at=owned.started_at)


def _authority_factory(run_id, *, reservation=None, environ=None):
    kind = _authority_kind(environ)
    try:
        if kind == "native":
            if reservation is None or not reservation.database_port:
                _refuse(FailureCode.ALLOCATION, Stage.ALLOCATION)
            reservation.release_database_port()
            return _ProjectAuthority(_pg_module().NativePgAuthority.start(run_id,
                run_root=reservation.run_root, port=reservation.database_port, environ=environ))
        return _ProjectAuthority(_pg_module().PgAuthority.start(run_id))
    except RuntimeError as error:
        # PP-2's closed refusal is emitted under its start lock, before provision.
        # Do not classify arbitrary startup failures as collisions.
        if str(error) == "run already exists":
            raise AllocationCollision("allocation collision") from None
        raise


def _issuer_factory(run_id):
    with _IMPORT_LOCK:
        name = "_leaf_project_fixture_identity"
        module = sys.modules.get(name) or _load_file(name, REPO / "scripts/proof_project_identity.py")
    return module.FixtureIssuer.create(run_id)


# Fixed child entry, public path only in argv. Credentials travel only in env.
_DATABASE_CHILD = """import importlib.util, sys
spec = importlib.util.spec_from_file_location('_leaf_project_child', sys.argv[1])
p = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = p
spec.loader.exec_module(p)
raise SystemExit(p._database_child_entry())
"""


def _request_public(request):
    public = {"run_id": request.run_id, "deadline": request.deadline,
              "result_path": str(request.result_path)}
    if isinstance(request, MigrationRequest):
        public.update(kind="migration", migration_root=str(request.migration_root),
            expected_manifest=[{"path": f.path, "sha256": f.sha256, "size": f.size} for f in request.expected_manifest])
    else:
        public.update(kind="seed", subject=request.subject, authority=request.authority, tier=request.tier)
    return public


def _proof_public(proof):
    if isinstance(proof, MigrationProof):
        return {"run_id": proof.run_id, "manifest_sha256": proof.manifest_sha256, "ok": proof.ok,
            "database": proof.database, "schema": proof.schema, "migration_count": proof.migration_count,
            "applied_migration_count": proof.applied_migration_count}
    return {"organization_id": proof.organization_id, "binding_id": proof.binding_id,
        "subject": proof.subject, "tier": proof.tier, "authority": proof.authority,
        "role": proof.role, "status": proof.status}


def _database_child_entry():
    """Production transport adapter, never a general supervisor CLI."""
    try:
        public = json.loads(os.environ["LEAF_PROJECT_CHILD_REQUEST"])
        kind = public.pop("kind")
        public["result_path"] = Path(public["result_path"])
        dsn = _nonempty_dsn(os.environ)  # Must precede the libpq cleaner's import.
        env = _PrivateEnvironment(_clean_libpq(_essential_environment(os.environ)))
        env["DATABASE_URL"] = dsn
        public["environment"] = env
        if kind == "migration":
            public["migration_root"] = Path(public["migration_root"])
            public["expected_manifest"] = tuple(FileProof(**item) for item in public["expected_manifest"])
            request = MigrationRequest(**public)
            operation = migration_child
        elif kind == "seed":
            request, operation = SeedRequest(**public), _seed_child
        else:
            return 1
        # Database libraries must not write diagnostics into the result channel.
        with open(os.devnull, "w") as sink, redirect_stdout(sink):
            proof = operation(request, effects=production_effects())
        print(json.dumps(_proof_public(proof), sort_keys=True))
        return 0
    except ProofRefused as exc:
        print(json.dumps({"failure": exc.failure.fixed_code.value}))
        return 1
    except BaseException:
        return 1


def _run_database_child(request, *, effects, scope):
    _nonempty_dsn(request.environment)
    _check_deadline(request, effects)
    migration = isinstance(request, MigrationRequest)
    code = FailureCode.MIGRATION_EXIT if migration else FailureCode.BINDING
    stage = Stage.MIGRATION if migration else Stage.BINDING
    env = _PrivateEnvironment(_clean_libpq(_essential_environment(request.environment)))
    env.update(DATABASE_URL=request.environment["DATABASE_URL"], PYTHONDONTWRITEBYTECODE="1",
               LEAF_PROJECT_CHILD_REQUEST=json.dumps(_request_public(request)))
    spec = ProcessSpec("migration-child" if migration else "seed-child",
        (sys.executable, "-B", "-c", _DATABASE_CHILD, str(REPO / "scripts/proof_project.py")),
        REPO.parent, env, request.deadline, request.result_path)
    process = None
    try:
        process = effects.process_launcher(spec, scope=scope)
        while True:
            _check_deadline(request, effects)
            exit_code = process.poll()
            if exit_code is not None:
                break
            effects.clock.wait(0.05)
        _check_deadline(request, effects)
        if exit_code != 0:
            # The child reports only closed failure codes, never exception text.
            try:
                failure = effects.host.read_child_result(request.result_path)
                allowed = {item.value: item for item in (FailureCode.MIGRATION_ENTRY, FailureCode.MIGRATION_CORPUS,
                    FailureCode.MIGRATION_TIMEOUT, FailureCode.MIGRATION_EXIT, FailureCode.MIGRATION_RESULT,
                    FailureCode.SCHEMA, FailureCode.BINDING, FailureCode.ENVIRONMENT)}
                if type(failure) is dict and set(failure) == {"failure"} and failure["failure"] in allowed:
                    code = allowed[failure["failure"]]
            except Exception:
                pass
            _refuse(code, stage)
        try:
            raw = effects.host.read_child_result(request.result_path)
            _check_deadline(request, effects)
            if type(raw) is not dict:
                raise ValueError
            if migration:
                result = _validate_migration(MigrationProof(**raw), request)
            else:
                result = _validate_binding(BindingProof(**raw), request)
        except ProofRefused:
            raise
        except Exception:
            _refuse(FailureCode.MIGRATION_RESULT if migration else FailureCode.BINDING, stage)
    except ProofRefused:
        raise
    except Exception:
        _refuse(code, stage)
    finally:
        failing = sys.exc_info()[0] is not None
        if process is not None:
            try:
                stopped = process.stop(effects.clock.monotonic() + 5.0)
            except Exception:
                stopped = False
            if not stopped and not failing:
                _refuse(code, stage)
    # Stopping the child must also finish inside the deadline.
    _check_deadline(request, effects)
    return result


def production_effects() -> Effects:
    """Adapters are lazy: construction imports no worker and acquires nothing."""
    host = _SupervisorHost()
    effects = Effects(lambda run_id: _authority_factory(run_id,
        reservation=getattr(host, "_reservation", None), environ=getattr(host, "_authority_environment", None)),
                   _issuer_factory, None, None,
                   _launch_supervised, None, _browser_unavailable, _canonical_reader,
                   _Clock(), host, _ReceiptWriter())
    return replace(effects,
        readiness_probe=lambda request: check_readiness(request, effects=effects),
        migration_runner=lambda request, *, scope: _run_database_child(request, effects=effects, scope=scope),
        seed_operation=lambda request, *, scope: _run_database_child(request, effects=effects, scope=scope))


class _PendingCall:
    """Bounded caller-facing completion for existing synchronous adapters.

    A thread timeout is not termination. RunScope retains the handle, refuses
    destructive cleanup while work is live, and keeps its guardian armed.
    """
    def __init__(self, callback):
        self._done = threading.Event()
        self._value = None
        self._error = None
        def run():
            try:
                self._value = callback()
            except BaseException as error:
                self._error = error
            finally:
                self._done.set()
        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def __repr__(self):
        return "<owned pending operation>"

    def done(self):
        return self._done.is_set()

    def result(self):
        if not self.done():
            raise RuntimeError("operation pending")
        if self._error is not None:
            raise self._error
        return self._value


@dataclass(frozen=True, repr=False)
class StopRequest:
    identity: ProcessIdentity
    observe: Callable[[], ProcessIdentity | None] = field(repr=False)
    terminate: Callable[[], None] = field(repr=False)
    kill: Callable[[], None] = field(repr=False)
    wait: Callable[[float], bool] = field(repr=False)


def stop_owned(request: StopRequest, *, effects: Effects, deadline: float) -> bool:
    """Escalate only the retained owner, rechecking identity before each signal."""
    try:
        remaining = deadline - effects.clock.monotonic()
        if not math.isfinite(remaining) or remaining <= 0:
            return False
        if request.observe() != request.identity:
            return True  # Original owner is gone; the replacement is unrelated.
        request.terminate()
        if request.wait(min(1.0, remaining)):
            return True
        if request.observe() != request.identity:
            return True
        remaining = deadline - effects.clock.monotonic()
        if remaining <= 0:
            return False
        request.kill()
        remaining = max(0.0, deadline - effects.clock.monotonic())
        return request.wait(remaining) is True
    except BaseException:
        return False


def _path_absent(path):
    """Only ENOENT proves absence; denied inspection and dangling links do not."""
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


# Record E. A-D's stage implementations remain the producers of evidence.
MIGRATION_SCHEMA = "leaf.project-migration.v1"
RECEIPT_NAME = "project-proof-receipt.json"
MIGRATION_RECEIPT_NAME = "project-migration-receipt.json"
RECEIPT_LIMIT = 256 * 1024
REQUIRED_ROWS = ("P01", "P04", "P08", "P09", "P10", "P11")
_CLEANUP_FIELDS = ("processes_stopped", "identity_delivery_closed", "pools_closed",
    "authority_removed", "private_material_removed", "guardian_disarmed")
_RECEIPT_FIELDS = frozenset(("schema", "run_id", "profile", "source", "runtime", "identity",
    "database", "stack", "worker", "playwright", "rows", "canonical", "timings", "cleanup", "failure", "ok"))
_SOURCE_FIELDS = ("commit", "dirty_tree_sha256", "tested_source_manifest_sha256")
_IDENTITY_FIELDS = ("public_key_thumbprint", "issuer", "audience", "organization_id", "binding_id", "subject", "tier")
_CANONICAL_FIELDS = ("project_id", "drawing_id", "input_version_id", "output_version_id",
    "job_id", "checkout_fence", "settings_readback_sha256")
_TIMING_FIELDS = ("allocation", "relay_ready", "migration", "stack_ready", "browser", "teardown")


@dataclass(frozen=True, repr=False)
class ReceiptBinding:
    run_id: str
    source: SourceProof
    public_key_thumbprint: str
    profile: str = PROFILE
    schema: str = SCHEMA


@dataclass(frozen=True)
class Verification:
    ok: bool
    failure: Failure | None


@dataclass(frozen=True, repr=False)
class PublicationIntent:
    journal: OwnershipJournal
    reservation: Reservation
    binding: ReceiptBinding
    filename: str


@dataclass(frozen=True, repr=False)
class _Publication:
    document: Mapping[str, Any]
    commit: Callable[[Callable[[], Any]], Any] = field(repr=False)
    check_cancelled: Callable[[], None] = field(default=lambda: None, repr=False)
    private_values: Any = field(default=None, repr=False)


def _receipt_refused():
    _refuse(FailureCode.RECEIPT, Stage.VERIFICATION)


_RUN_PRIVATE = ContextVar("project_run_private", default=None)


class _PrivateValues:
    """Run-local denylist, retained through cleanup; never render private values."""
    def __init__(self):
        self._values = set()
        self._lock = threading.RLock()
        self.rejected = False

    def __repr__(self):
        return "<private run values>"

    def add(self, value):
        from urllib.parse import quote, quote_plus, unquote
        if isinstance(value, _SecretToken):
            value = value.reveal()
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="ignore")
        if type(value) is str and value:
            with self._lock:
                for raw in (value, unquote(value)):
                    if raw:
                        self._values.update((raw, quote(raw, safe=""), quote_plus(raw, safe="")))

    def dsn(self, value):
        from urllib.parse import urlsplit, unquote
        self.add(value)
        try:
            password = urlsplit(value).password
        except ValueError:
            _receipt_refused()
        if password:
            self.add(unquote(password))

    def environment(self, environment):
        for name, value in environment.items():
            name = name.upper()
            if name.endswith("DATABASE_URL") or name in {"DSN", "DATABASE_DSN"}:
                self.dsn(value)
            elif (re.search(r"PASSWORD|SECRET|TOKEN|BEARER|CAPABILITY|CHECKOUT", name)
                    or name in {"KEY", "PRIVATE_KEY", "SIGNING_KEY"}):
                self.add(value)

    def issuer(self, issuer):
        for name in ("private_key", "_private_key"):
            key = getattr(issuer, name, None)
            if key is None:
                continue
            if isinstance(key, (str, bytes)):
                self.add(key)
            else:
                from cryptography.hazmat.primitives import serialization
                for form in (serialization.PrivateFormat.PKCS8, serialization.PrivateFormat.TraditionalOpenSSL):
                    pem = key.private_bytes(serialization.Encoding.PEM, form, serialization.NoEncryption())
                    self.add(pem)
                    self.add(b"".join(pem.splitlines()[1:-1]))

    def capabilities(self, owner):
        for name in ("checkout_capability", "checkout_token", "capability"):
            self.add(getattr(owner, name, None))

    def check(self, value):
        from urllib.parse import unquote, unquote_plus
        if self.rejected:
            _receipt_refused()
        if type(value) is str:
            with self._lock:
                secrets = tuple(self._values)
            if any(secret in candidate for candidate in (value, unquote(value), unquote_plus(value))
                   for secret in secrets):
                self.rejected = True
                _receipt_refused()
        elif isinstance(value, Mapping):
            for key, item in value.items():
                self.check(key)
                self.check(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                self.check(item)


def _private_effects(effects, private):
    class Authority:
        def __init__(self, authority):
            self.authority = authority
        def __getattr__(self, name):
            return getattr(self.authority, name)
        def dsn(self):
            value = self.authority.dsn()
            private.dsn(value)
            return value
    class Issuer:
        def __init__(self, issuer):
            self.issuer_object = issuer
            private.issuer(issuer)
        def __getattr__(self, name):
            return getattr(self.issuer_object, name)
        def mint(self, *args, **kwargs):
            value = self.issuer_object.mint(*args, **kwargs)
            private.add(value)
            return value
    def browser(request, *, scope):
        private.environment(request.environment)
        handle = effects.browser_runner(request, scope=scope)
        private.capabilities(handle)
        class Browser:
            def __getattr__(self, name):
                return getattr(handle, name)
            def poll(self):
                result = handle.poll()
                private.capabilities(handle)
                return result
        return Browser()
    return replace(effects,
        authority_factory=lambda run_id: Authority(effects.authority_factory(run_id)),
        issuer_factory=lambda run_id: Issuer(effects.issuer_factory(run_id)),
        browser_runner=browser if effects.browser_runner not in (None, _browser_unavailable) else effects.browser_runner)


def _exact(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        _receipt_refused()
    return value


def _public_id(value):
    return type(value) is str and re.fullmatch(r"[A-Za-z0-9:|_-]{1,128}", value) is not None


def _source_public(source):
    return {name: getattr(source, name) for name in _SOURCE_FIELDS}


def _file_rows(files):
    return [{"path": f.path, "sha256": f.sha256, "size": f.size} for f in files]


def _owned_public(owned):
    if owned.kind == "native":
        return {"run_id": owned.run_id, "kind": "native", "data_dir": owned.data_dir, "port": owned.port}
    if owned.kind != "docker":
        _receipt_refused()
    return {"run_id": owned.run_id, "names": list(owned.names), "labels": dict(owned.labels),
        "containers": list(owned.containers), "networks": list(owned.networks)}


def _read_owned_public(value, run_id, *, native_identity=None):
    if type(value) is dict and value.get("kind") == "native":
        if not _native_public_valid(value, run_id):
            _receipt_refused()
        return _native_ownership(run_id, value["port"], native_identity)
    row = _exact(value, ("run_id", "names", "labels", "containers", "networks"))
    if any(type(row[key]) is not list for key in ("names", "containers", "networks")):
        _receipt_refused()
    _exact(row["labels"], ("leaf.proof-project.run", "leaf.proof-project.owner"))
    return AuthorityOwnership(row["run_id"], tuple(row["names"]),
        tuple((key, row["labels"][key]) for key in ("leaf.proof-project.run", "leaf.proof-project.owner")),
        tuple(row["containers"]), tuple(row["networks"]))


def _heartbeat_public(heartbeat):
    return {"worker_id": heartbeat.worker_id, "tool_name": heartbeat.tool_name,
        "runtime": heartbeat.runtime, "source_revision": heartbeat.source_revision,
        "source_sha256": heartbeat.source_sha256, "observed_at": heartbeat.observed_at.isoformat()}


def _failure_public(failure):
    return None if failure is None else {"stage": failure.stage.value, "fixed_code": failure.fixed_code.value}


def _receipt_document(plan, runtime, prepared, execution, cleanup_proof, failure, *, migration=False):
    """Only named public evidence enters the wire format; never copy __dict__."""
    database = prepared if type(prepared) is DatabaseProof else prepared.database if prepared is not None else None
    identity = prepared.identity if type(prepared) is PreparedAuthority else None
    document = dict(schema=MIGRATION_SCHEMA if migration else SCHEMA, run_id=plan.config.run_id,
        profile=PROFILE, source=_source_public(plan.source), runtime=None, identity=None,
        database=None, stack=None, worker=None, playwright=None, rows=dict.fromkeys(REQUIRED_ROWS, False),
        canonical=None, timings=dict.fromkeys(_TIMING_FIELDS, 0.0),
        cleanup={name: getattr(cleanup_proof, name) for name in _CLEANUP_FIELDS},
        failure=_failure_public(failure), ok=False)
    document["cleanup"]["failures"] = [_failure_public(item) for item in cleanup_proof.failures]
    document["timings"]["teardown"] = cleanup_proof.teardown_seconds
    document["rows"]["P10"] = cleanup_proof.ok and "P10" in cleanup_proof.observed_rows
    if runtime is not None:
        document["runtime"] = {"requirements": _file_rows(runtime.requirements), "python": runtime.python,
            "toolchain": dict(runtime.toolchain), "vendor_source": _file_rows(runtime.vendor_source),
            "engine_outputs": _file_rows(runtime.engine_outputs)}
    if identity is not None:
        document["identity"] = {name: getattr(identity, name) for name in _IDENTITY_FIELDS}
    if database is not None:
        document["database"] = {"authority_run_id": database.authority_run_id,
            "ownership": _owned_public(database.ownership),
            "transport": "native-loopback" if database.ownership.kind == "native" else "docker-exec-relay",
            "readiness": {"ok": database.readiness.ok, "server_version": database.readiness.server_version,
                "round_trip_bytes": database.readiness.round_trip_bytes},
            "migration_manifest": _file_rows(database.migration_manifest),
            "migration_manifest_sha256": database.migration_manifest_sha256,
            "schema_status": _proof_public(database.schema_status)}
        document["timings"].update(allocation=database.allocation_seconds, relay_ready=database.relay_seconds,
            migration=database.migration_seconds)
        document["rows"]["P04"] = database.schema_status.ok is True
    if execution is not None:
        ready, browser, canonical = execution.stack, execution.playwright, execution.canonical
        document["stack"] = {"owned_process_identities": [
            {"role": item.role, "pid": item.pid, "start_id": item.start_id, "parent_pid": item.parent_pid}
            for item in ready.owned_process_identities], "source_revision": ready.source_revision,
            "app_readiness": {"source_revision": ready.app_readiness.source_revision,
                "dependencies": {name: {"state": state, "required": required}
                                 for name, state, required in ready.app_readiness.dependencies}}}
        document["worker"] = {"identity": execution.worker_identity, "tool": plan.descriptor.tool,
            "source_revision": plan.descriptor.source_revision, "source_sha256": plan.descriptor.source_sha256,
            "source_manifest": list(plan.descriptor.source_manifest),
            "initial_heartbeat": _heartbeat_public(execution.initial_heartbeat),
            "final_heartbeat": _heartbeat_public(execution.final_heartbeat)}
        document["playwright"] = {"selected_test_ids": list(browser.selected_test_ids),
            "started_test_ids": list(browser.started_test_ids), "completed_test_ids": list(browser.completed_test_ids),
            "outcomes": [{"test_id": item.test_id, "status": item.status, "retry": item.retry} for item in browser.outcomes],
            "annotation_types": list(browser.annotation_types)}
        document["canonical"] = {name: getattr(canonical.evidence, name) for name in _CANONICAL_FIELDS[:-1]}
        document["canonical"]["settings_readback_sha256"] = canonical.stored_settings_sha256
        document["timings"].update(stack_ready=execution.stack_seconds, browser=execution.browser_seconds)
        for row in execution.observed_rows:
            if row in REQUIRED_ROWS:
                document["rows"][row] = True
    complete = database is not None if migration else execution is not None and runtime is not None and identity is not None
    document["ok"] = failure is None and cleanup_proof.ok and complete
    return document


def _validate_files(rows, *, names=None):
    if type(rows) is not list or not rows:
        _receipt_refused()
    paths = []
    for item in rows:
        _exact(item, ("path", "sha256", "size"))
        path = item["path"]
        if (type(path) is not str or re.fullmatch(r"[A-Za-z0-9_./-]{1,240}", path) is None
                or path.startswith("/") or ".." in path.split("/") or not _hash_ok(item["sha256"])
                or type(item["size"]) is not int or item["size"] <= 0):
            _receipt_refused()
        paths.append(path)
    if len(set(paths)) != len(paths) or (names is not None and set(paths) != set(names)):
        _receipt_refused()


def _validate_receipt_structure(document, *, project=True):
    _exact(document, _RECEIPT_FIELDS)
    if (document["schema"] != (SCHEMA if project else MIGRATION_SCHEMA) or document["profile"] != PROFILE
            or type(document["run_id"]) is not str
            or re.fullmatch(r"[a-z0-9][a-z0-9-]{7,63}", document["run_id"]) is None
            or type(document["ok"]) is not bool):
        _receipt_refused()
    source = _exact(document["source"], _SOURCE_FIELDS)
    if (type(source["commit"]) is not str or re.fullmatch(r"[a-f0-9]{40}", source["commit"]) is None
            or not all(_hash_ok(source[key]) for key in _SOURCE_FIELDS[1:])):
        _receipt_refused()
    rows = _exact(document["rows"], REQUIRED_ROWS)
    if any(type(value) is not bool for value in rows.values()):
        _receipt_refused()
    cleanup_row = _exact(document["cleanup"], (*_CLEANUP_FIELDS, "failures"))
    if any(type(cleanup_row[key]) is not bool for key in _CLEANUP_FIELDS) or type(cleanup_row["failures"]) is not list:
        _receipt_refused()
    for failure in [document["failure"], *cleanup_row["failures"]]:
        if failure is not None:
            _exact(failure, ("stage", "fixed_code"))
            if failure["stage"] not in {s.value for s in Stage} or failure["fixed_code"] not in {c.value for c in FailureCode}:
                _receipt_refused()
    timings = _exact(document["timings"], _TIMING_FIELDS)
    if any(type(value) not in (float, int) or not math.isfinite(value) or value < 0 for value in timings.values()):
        _receipt_refused()
    runtime = document["runtime"]
    if runtime is not None:
        _exact(runtime, ("requirements", "python", "toolchain", "vendor_source", "engine_outputs"))
        _validate_files(runtime["requirements"], names=REQUIREMENTS)
        _validate_files(runtime["vendor_source"])
        _validate_files(runtime["engine_outputs"], names=ENGINE_OUTPUTS)
        _exact(runtime["toolchain"], TOOLS)
        if any(type(value) is not str or re.fullmatch(r"[A-Za-z0-9 .+()_:/-]{1,256}", value) is None
               for value in [runtime["python"], *runtime["toolchain"].values()]):
            _receipt_refused()
    identity = document["identity"]
    if identity is not None:
        _exact(identity, _IDENTITY_FIELDS)
        if (not all(_public_id(identity[key]) for key in ("public_key_thumbprint", "organization_id", "binding_id"))
                or identity["issuer"] != "https://proof-project.leaf.invalid/" + document["run_id"] + "/"
                or identity["audience"] != "leaf-proof-project:" + document["run_id"]
                or identity["subject"] != "leaf-proof-project|" + document["run_id"] or identity["tier"] != TIER):
            _receipt_refused()
    database = document["database"]
    if database is not None:
        _exact(database, ("authority_run_id", "ownership", "transport", "readiness", "migration_manifest",
            "migration_manifest_sha256", "schema_status"))
        if (database["authority_run_id"] != document["run_id"]
                or database["transport"] not in ("docker-exec-relay", "native-loopback")):
            _receipt_refused()
        owned = database["ownership"]
        if database["transport"] == "native-loopback":
            if not _native_public_valid(owned, document["run_id"]):
                _receipt_refused()
        else:
            _exact(owned, ("run_id", "names", "labels", "containers", "networks"))
            _validate_ownership(_read_owned_public(owned, document["run_id"]), document["run_id"], allocated=True)
        ready = _exact(database["readiness"], ("ok", "server_version", "round_trip_bytes"))
        if (ready["ok"] is not True or type(ready["round_trip_bytes"]) is not int or ready["round_trip_bytes"] != 256
                or type(ready["server_version"]) is not str
                or re.fullmatch(r"[0-9][A-Za-z0-9 .()+_-]{0,127}", ready["server_version"]) is None):
            _receipt_refused()
        _validate_files(database["migration_manifest"])
        manifest = tuple(FileProof(**item) for item in database["migration_manifest"])
        if database["migration_manifest_sha256"] != _manifest_hash(manifest):
            _receipt_refused()
        status = _exact(database["schema_status"], ("run_id", "manifest_sha256", "ok", "database", "schema",
            "migration_count", "applied_migration_count"))
        _validate_migration(MigrationProof(**status), MigrationRequest(document["run_id"], REPO / "platform/migrations",
            manifest, {}, 0, Path("unused")))
    stack = document["stack"]
    if stack is not None:
        _exact(stack, ("owned_process_identities", "source_revision", "app_readiness"))
        if stack["source_revision"] != source["commit"] or type(stack["owned_process_identities"]) is not list:
            _receipt_refused()
        roles, pids = [], []
        for item in stack["owned_process_identities"]:
            _exact(item, ("role", "pid", "start_id", "parent_pid"))
            if (type(item["pid"]) is not int or item["pid"] <= 0 or type(item["parent_pid"]) is not int
                    or item["parent_pid"] <= 0 or not _public_id(item["start_id"])):
                _receipt_refused()
            roles.append(item["role"])
            pids.append(item["pid"])
        if len(set(pids)) != 6 or len(roles) != 6 or set(roles) != {"stack-launcher", *STACK_ROLES}:
            _receipt_refused()
        launcher = next(item for item in stack["owned_process_identities"] if item["role"] == "stack-launcher")
        if any(item["parent_pid"] != launcher["pid"] for item in stack["owned_process_identities"]
               if item["role"] != "stack-launcher"):
            _receipt_refused()
        app = _exact(stack["app_readiness"], ("source_revision", "dependencies"))
        if app["source_revision"] != source["commit"]:
            _receipt_refused()
        _exact(app["dependencies"], APP_DEPENDENCIES)
        for item in app["dependencies"].values():
            _exact(item, ("state", "required"))
            if item["state"] != "ready" or item["required"] is not True:
                _receipt_refused()
    worker = document["worker"]
    if worker is not None:
        _exact(worker, ("identity", "tool", "source_revision", "source_sha256", "source_manifest",
            "initial_heartbeat", "final_heartbeat"))
        if (worker["identity"] != "project:" + document["run_id"] or worker["tool"] != WORKER_TOOL
                or not _hash_ok(worker["source_sha256"]) or worker["source_revision"] != "sha256:" + worker["source_sha256"]
                or type(worker["source_manifest"]) is not list or not worker["source_manifest"]
                or len(set(worker["source_manifest"])) != len(worker["source_manifest"])
                or any(type(item) is not str or re.fullmatch(r"[A-Za-z0-9_./-]{1,240}", item) is None
                       or ".." in item.split("/") for item in worker["source_manifest"])):
            _receipt_refused()
        stamps = []
        for key in ("initial_heartbeat", "final_heartbeat"):
            beat = _exact(worker[key], ("worker_id", "tool_name", "runtime", "source_revision", "source_sha256", "observed_at"))
            if (beat["worker_id"] != worker["identity"] or beat["tool_name"] != WORKER_TOOL
                    or beat["source_revision"] != worker["source_revision"] or beat["source_sha256"] != worker["source_sha256"]
                    or type(beat["runtime"]) is not str or re.fullmatch(r"[A-Za-z0-9 .+()_-]{1,128}", beat["runtime"]) is None):
                _receipt_refused()
            stamp = datetime.fromisoformat(beat["observed_at"])
            if stamp.tzinfo is None:
                _receipt_refused()
            stamps.append(stamp)
        if stamps[1] < stamps[0] or worker["initial_heartbeat"]["runtime"] != worker["final_heartbeat"]["runtime"]:
            _receipt_refused()
    playwright = document["playwright"]
    if playwright is not None:
        _exact(playwright, ("selected_test_ids", "started_test_ids", "completed_test_ids", "outcomes", "annotation_types"))
        for key in ("selected_test_ids", "started_test_ids", "completed_test_ids"):
            ids = playwright[key]
            if type(ids) is not list or len(ids) != 3 or set(ids) != set(REQUIRED_TEST_IDS):
                _receipt_refused()
        if playwright["annotation_types"] != [] or type(playwright["outcomes"]) is not list or len(playwright["outcomes"]) != 3:
            _receipt_refused()
        ids = []
        for item in playwright["outcomes"]:
            _exact(item, ("test_id", "status", "retry"))
            if item["status"] != "passed" or type(item["retry"]) is not int or item["retry"] != 0:
                _receipt_refused()
            ids.append(item["test_id"])
        if set(ids) != set(REQUIRED_TEST_IDS):
            _receipt_refused()
    canonical = document["canonical"]
    if canonical is not None:
        _exact(canonical, _CANONICAL_FIELDS)
        if (any(not _public_id(canonical[key]) for key in _CANONICAL_FIELDS[:5])
                or canonical["input_version_id"] == canonical["output_version_id"]
                or type(canonical["checkout_fence"]) is not int or not 1 <= canonical["checkout_fence"] <= 9223372036854775807
                or not _hash_ok(canonical["settings_readback_sha256"])):
            _receipt_refused()
    if not project and any(document[key] is not None for key in ("identity", "stack", "worker", "playwright", "canonical")):
        _receipt_refused()


def _verify_document(document, expected):
    _validate_receipt_structure(document)
    if (type(expected) is not ReceiptBinding or expected.schema != SCHEMA or document["schema"] != expected.schema
            or document["run_id"] != expected.run_id or document["profile"] != expected.profile
            or document["source"] != _source_public(expected.source) or document["identity"] is None
            or document["identity"]["public_key_thumbprint"] != expected.public_key_thumbprint):
        _receipt_refused()
    if (document["ok"] is not True or document["failure"] is not None
            or any(document[key] is None for key in ("runtime", "identity", "database", "stack", "worker", "playwright", "canonical"))
            or any(document["rows"][key] is not True for key in REQUIRED_ROWS)
            or any(document["cleanup"][key] is not True for key in _CLEANUP_FIELDS) or document["cleanup"]["failures"]):
        _receipt_refused()


def verify_receipt(path: Path, *, expected: ReceiptBinding, effects: Effects | None = None) -> Verification:
    try:
        effects = effects if effects is not None else production_effects()
        document = effects.receipt_writer.read(path)
        _verify_document(document, expected)
        return Verification(True, None)
    except BaseException:
        return Verification(False, Failure(Stage.VERIFICATION, FailureCode.RECEIPT))


def _read_receipt(path):
    if not _path_safe(path):
        _receipt_refused()
    with path.open("rb") as stream:
        raw = stream.read(RECEIPT_LIMIT + 1)
    if len(raw) > RECEIPT_LIMIT:
        _receipt_refused()
    return _decode_public(raw)


def _decode_public(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                _receipt_refused()
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=unique, parse_constant=lambda value: _receipt_refused())


def _intent_public(intent):
    journal, binding = intent.journal, intent.binding
    return {"run_id": journal.run_id, "run_root": str(journal.run_root),
        "artifact_root": str(journal.artifact_root), "created_at": journal.created_at.isoformat(),
        "publication": intent.filename, "source": _source_public(binding.source),
        "profile": binding.profile, "schema": binding.schema,
        "public_key_thumbprint": binding.public_key_thumbprint,
        "authority": None if journal.authority is None else _owned_public(journal.authority),
        "root_identity": list(intent.reservation.root_identity or ())}


def _write_synced(path, payload):
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _record_publication(writer, path, intent):
    journal, reservation = intent.journal, intent.reservation
    if (path != journal.run_root / "publication-intent.json" or reservation.run_root != journal.run_root
            or reservation.artifact_root != journal.artifact_root or journal.artifact_root != journal.run_root / "artifacts"
            or intent.filename not in (RECEIPT_NAME, MIGRATION_RECEIPT_NAME)
            or intent.binding.run_id != journal.run_id or not _path_safe(path)
            or not _path_safe(journal.artifact_root) or reservation.root_identity is None):
        _receipt_refused()
    current = journal.run_root.stat()
    if reservation.root_identity != (current.st_dev, current.st_ino):
        _receipt_refused()
    public = _intent_public(intent)
    raw = (json.dumps(public, sort_keys=True, separators=(",", ":")) + "\n").encode()
    _write_synced(path, raw)
    writer._intents[journal.artifact_root / intent.filename] = (intent, raw)


def _renameat2_noreplace(source, target):
    import ctypes
    library = ctypes.CDLL(None, use_errno=True)
    rename = getattr(library, "renameat2", None)
    if rename is None:
        return errno.ENOSYS
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1) != 0:
        return ctypes.get_errno()
    return True


def _rename_noreplace(source, target):
    # Windows rename is atomically no-replace. POSIX os.rename overwrites, so
    # use the kernel's no-replace rename or an atomic no-replace hard link.
    if os.name == "nt":
        os.rename(source, target)
        return
    result = _renameat2_noreplace(source, target)
    if result is True:
        return
    if result not in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP, errno.ENOTSUP, errno.EPERM):
        _receipt_refused()
    try:
        os.link(source, target)
    except OSError:
        _receipt_refused()
    try:
        os.unlink(source)
    except OSError:
        pass  # The receipt is already published; unlink failure must not undo or refuse it.


def _commit_receipt(temporary, path, root_identity, artifact_identity, check_cancelled):
    root = path.parent.parent
    if not all(_path_safe(item) for item in (root, path.parent, temporary, path)):
        _receipt_refused()
    root_info, artifact_info = root.stat(), path.parent.stat()
    if ((root_info.st_dev, root_info.st_ino) != root_identity
            or (artifact_info.st_dev, artifact_info.st_ino) != artifact_identity):
        _receipt_refused()
    check_cancelled()
    _rename_noreplace(temporary, path)


def _publish_receipt(writer, path, publication):
    """Prepare outside the commit guard; the guard encloses only the rename."""
    if type(publication) is not _Publication or path not in writer._intents:
        _receipt_refused()
    intent, intent_bytes = writer._intents[path]
    document = publication.document
    if publication.private_values is not None:
        publication.private_values.check(document)
    project = intent.binding.schema == SCHEMA
    _validate_receipt_structure(document, project=project)
    if (document["run_id"] != intent.binding.run_id or document["source"] != _source_public(intent.binding.source)
            or document["schema"] != intent.binding.schema or document["profile"] != intent.binding.profile):
        _receipt_refused()
    if document["ok"] and project:
        _verify_document(document, intent.binding)
    if document["ok"] and not project and (document["database"] is None or document["failure"] is not None
            or not document["rows"]["P04"] or not document["rows"]["P10"]):
        _receipt_refused()
    if any(document["cleanup"][name] is not True for name in _CLEANUP_FIELDS) or document["cleanup"]["failures"]:
        _receipt_refused()
    root = intent.reservation.run_root
    if (path != intent.reservation.artifact_root / intent.filename or not _path_safe(root)
            or not _path_safe(path) or not _path_absent(root)):
        _receipt_refused()
    # D verified complete removal. Exclusively create a fresh evidence-only
    # directory; a replacement root is never adopted, even for keep_run_root.
    _private_directory(root)
    _private_directory(path.parent)
    current = root.stat()
    artifact = path.parent.stat()
    renewed = dict(json.loads(intent_bytes), root_identity=[current.st_dev, current.st_ino])
    _write_synced(root / "publication-intent.json", (json.dumps(renewed, sort_keys=True) + "\n").encode())
    _write_synced(root / "guardian-clean.json", (json.dumps({"run_id": intent.binding.run_id,
        "processes_stopped": True, "guardian_disarmed": True}, sort_keys=True) + "\n").encode())
    payload = (json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    if len(payload) > RECEIPT_LIMIT:
        _receipt_refused()
    temporary = path.with_name(path.name + ".pending")
    try:
        _write_synced(temporary, payload)
        publication.commit(lambda: _commit_receipt(temporary, path,
            (current.st_dev, current.st_ino), (artifact.st_dev, artifact.st_ino), publication.check_cancelled))
    finally:
        temporary.unlink(missing_ok=True)


_STAGE_CODES = {Stage.PREFLIGHT: FailureCode.PREFLIGHT, Stage.RUNTIME: FailureCode.RUNTIME,
    Stage.ALLOCATION: FailureCode.ALLOCATION, Stage.TRANSPORT: FailureCode.TRANSPORT,
    Stage.MIGRATION: FailureCode.MIGRATION_EXIT, Stage.IDENTITY: FailureCode.IDENTITY,
    Stage.BINDING: FailureCode.BINDING, Stage.STACK: FailureCode.LAUNCH,
    Stage.READINESS: FailureCode.READINESS, Stage.BROWSER: FailureCode.BROWSER_EXIT,
    Stage.VERIFICATION: FailureCode.RECEIPT, Stage.TEARDOWN: FailureCode.CLEANUP_PRIVATE,
    Stage.DISPATCH: FailureCode.USAGE}


def _stage_call(scope, stage, operation, seconds):
    def normalize(call, code):
        try:
            return call()
        except ProofRefused:
            raise
        except (KeyboardInterrupt, SystemExit):
            _refuse(FailureCode.CANCELLED, stage)
        except BaseException:
            _refuse(code, stage)
    def invoke():
        token = _RUN_PRIVATE.set(getattr(scope, "private_values", None))
        try:
            return normalize(operation, _STAGE_CODES[stage])
        finally:
            _RUN_PRIVATE.reset(token)
    # Normalize the operation transport before D remembers an escaping error.
    # Its own deadline/cancellation refusals keep their original fixed codes.
    original = scope.effects
    class Host:
        def __getattr__(self, name):
            return getattr(original.host, name)
        def begin_operation(self, callback):
            task = normalize(lambda: original.host.begin_operation(callback), FailureCode.INTERNAL)
            class Task:
                def done(self):
                    return normalize(task.done, FailureCode.INTERNAL)
                def result(self):
                    return normalize(task.result, FailureCode.INTERNAL)
            return Task()
    scope.effects = replace(original, host=Host())
    try:
        return scope.perform(stage, invoke, deadline=scope.effects.clock.monotonic() + seconds)
    finally:
        scope.effects = original


class _MigrationBoundary(BaseException):
    """Internal successful stop at B's first identity effect, before any issuer."""


def _measure_prepared(plan, *, effects, scope):
    # Reuse B's allocation/readiness/migration barriers byte-for-byte. Observe
    # their injected results and stop at the identity boundary; never seed.
    observed = {}
    original_writer = effects.receipt_writer
    class Writer:
        def ownership(self, path, value):
            original_writer.ownership(path, value)
            if value.authority is not None and (value.authority.containers or value.authority.present):
                observed["ownership"] = value.authority
        def __getattr__(self, name):
            return getattr(original_writer, name)
    def allocate(run_id):
        start = effects.clock.monotonic()
        authority = effects.authority_factory(run_id)
        observed["allocation"] = effects.clock.monotonic() - start
        class Authority:
            def __repr__(self):
                return "<migration authority>"
            def readiness(self):
                start = effects.clock.monotonic()
                result = authority.readiness()
                observed["relay"] = effects.clock.monotonic() - start
                observed["readiness"] = result
                return result
            def dsn(self):
                return authority.dsn()
            def stop(self):
                return authority.stop()
        return Authority()
    def migrate(request, *, scope):
        start = effects.clock.monotonic()
        result = effects.migration_runner(request, scope=scope)
        observed["migration"] = effects.clock.monotonic() - start
        observed["proof"] = result
        return result
    def boundary(run_id):
        raise _MigrationBoundary()
    measured = replace(effects, authority_factory=allocate, migration_runner=migrate,
        issuer_factory=boundary, receipt_writer=Writer())
    try:
        prepare_authority(plan, effects=measured, scope=scope)
    except _MigrationBoundary:
        return DatabaseProof(plan.config.run_id, observed["ownership"],
            "native-loopback" if observed["ownership"].kind == "native" else "host-relay",
            TransportProof(**observed["readiness"]), plan.migrations, _manifest_hash(plan.migrations),
            observed["proof"], observed["allocation"], observed["relay"], observed["migration"])
    _refuse(FailureCode.INTERNAL, Stage.MIGRATION)


class _RunWriter:
    def __init__(self, writer, guardian):
        self.writer, self.guardian = writer, guardian
    def ownership(self, path, value):
        self.writer.ownership(path, value)
        if (isinstance(value, OwnershipJournal) and value.authority is not None
                and (value.authority.containers or value.authority.present)):
            self.guardian.observe(value.authority)
    def __getattr__(self, name):
        return getattr(self.writer, name)


def _lifecycle(config, *, effects, migration):
    effects = effects if effects is not None else production_effects()
    scope = RunScope(effects=effects)
    scope.private_values = private = _PrivateValues()
    plan = runtime = prepared = execution = cleanup_proof = receipt_path = None
    cancellation = None
    try:
        cancellation = effects.host.cancellation(scope)
        cancellation.__enter__()
        bound = _private_effects(scope.bind_effects(), private)
        plan = _stage_call(scope, Stage.PREFLIGHT, lambda: preflight(config, effects=bound), 120.0)
        private.environment(plan.host.environment)
        guardian = _stage_call(scope, Stage.ALLOCATION, lambda: effects.host.guardian(plan), CLEANUP_TIMEOUT_S)
        scope.guardian = guardian
        _stage_call(scope, Stage.ALLOCATION, lambda: scope.own_guardian(guardian), CLEANUP_TIMEOUT_S)
        def launch(spec, *, scope):
            private.environment(spec.environment)
            child = effects.process_launcher(spec, scope=scope)
            guardian.own_process(child)
            return child
        bound = replace(bound, process_launcher=launch, receipt_writer=_RunWriter(effects.receipt_writer, guardian))
        if not migration:
            runtime = _stage_call(scope, Stage.RUNTIME,
                lambda: prepare_runtime(plan, effects=bound, scope=scope), BUILD_TIMEOUT_S + 4 * VERSION_TIMEOUT_S + 5)
        prepared = _stage_call(scope, Stage.ALLOCATION,
            lambda: (_measure_prepared(plan, effects=bound, scope=scope) if migration
                     else prepare_authority(plan, effects=bound, scope=scope)), 2 * MIGRATION_TIMEOUT_S + 120)
        if not migration:
            execution = _stage_call(scope, Stage.BROWSER,
                lambda: run_project_pipeline(prepared, effects=bound, scope=scope), STACK_TIMEOUT_S + BROWSER_TIMEOUT_S + 5)
    except ProofRefused as error:
        scope.remember(error.failure)
    except (KeyboardInterrupt, SystemExit):
        scope.remember(Failure(Stage.VERIFICATION, FailureCode.CANCELLED))
    except BaseException:
        scope.remember(Failure(Stage.VERIFICATION, FailureCode.INTERNAL))
    try:
        if plan is not None:
            key = prepared.identity.public_key_thumbprint if type(prepared) is PreparedAuthority else ""
            database = prepared.database if type(prepared) is PreparedAuthority else prepared
            binding = ReceiptBinding(config.run_id, plan.source, key, schema=MIGRATION_SCHEMA if migration else SCHEMA)
            filename = MIGRATION_RECEIPT_NAME if migration else RECEIPT_NAME
            intent = PublicationIntent(OwnershipJournal(config.run_id, config.run_root, config.artifact_root,
                effects.clock.utc_now(), database.ownership if database is not None else None),
                plan.reservation, binding, filename)
            private.check(_intent_public(intent))
            _stage_call(scope, Stage.VERIFICATION,
                lambda: effects.receipt_writer.ownership(config.run_root / "publication-intent.json", intent), CLEANUP_TIMEOUT_S)
    except ProofRefused as error:
        scope.remember(error.failure)
        plan = None  # No publication authorization was durably recorded.
    except BaseException:
        scope.remember(Failure(Stage.VERIFICATION, FailureCode.RECEIPT))
        plan = None
    try:
        cleanup_proof = scope.close()
        if plan is not None and cleanup_proof.ok:
            document = _receipt_document(plan, runtime, prepared, execution, cleanup_proof, scope.failure, migration=migration)
            private.check(document)
            _validate_receipt_structure(document, project=not migration)
            if document["ok"] and not migration:
                _verify_document(document, binding)
            def check_cancelled():
                scope._check_cancelled(Stage.VERIFICATION)
            def commit(action):
                with scope._commit_lock:
                    check_cancelled()
                    scope._receipt_committing = True
                    try:
                        if document["ok"]:
                            return scope.commit_guard(action)
                        return action()
                    finally:
                        scope._receipt_committing = False
            candidate = config.artifact_root / filename
            effects.receipt_writer.publish(candidate, _Publication(document, commit, check_cancelled, private))
            receipt_path = candidate
    except ProofRefused as error:
        scope.remember(error.failure)
    except BaseException:
        scope.remember(Failure(Stage.VERIFICATION, FailureCode.RECEIPT))
    finally:
        if cancellation is not None:
            try:
                cancellation.__exit__(None, None, None)
            except BaseException:
                scope.remember(Failure(Stage.VERIFICATION, FailureCode.INTERNAL))
    return Outcome(0 if scope.failure is None and receipt_path is not None else _exit_code(scope.failure),
        receipt_path, scope.failure, cleanup_proof.failures if cleanup_proof is not None else ())


def supervise(config: Config, *, effects: Effects | None = None) -> Outcome:
    return _lifecycle(config, effects=effects, migration=False)


def measure_migration(config: Config, *, effects: Effects | None = None) -> Outcome:
    return _lifecycle(config, effects=effects, migration=True)


def cleanup(run_id: str, *, run_root: Path, effects: Effects | None = None) -> CleanupProof:
    effects = effects if effects is not None else production_effects()
    scope = RunScope(effects=effects)
    try:
        if type(run_id) is not str or re.fullmatch(r"[a-z0-9][a-z0-9-]{7,63}", run_id) is None:
            _refuse(FailureCode.USAGE, Stage.TEARDOWN)
        # Recovery never obtains process-kill authority from bare journal PIDs.
        reservation, ownership = effects.host.recovery(run_id, run_root)
        if not effects.host.recovery_processes_absent(run_id, run_root):
            _refuse(FailureCode.CLEANUP_PROCESS, Stage.TEARDOWN)
        scope.own_reservation(reservation)
        scope.acquire(ResourceKind.AUTHORITY, lambda: _cleanup_authority(None, run_id,
            effects=effects, verify=True, ownership=ownership))
    except ProofRefused as error:
        scope.remember(error.failure)
    except BaseException:
        scope.remember(Failure(Stage.TEARDOWN, FailureCode.CLEANUP_PRIVATE))
    if scope.failure is not None and not scope._actions:
        return CleanupProof(False, False, False, False, False, False, (scope.failure,), 0.0, ())
    return scope.close()


def _exit_code(failure):
    # Stable nonzero values; only E's USAGE has a dedicated parser exit.
    return 2 if failure is not None and failure.fixed_code == FailureCode.USAGE else 1


def main(argv=None, *, effects: Effects | None = None) -> int:
    import argparse
    class SafeParser(argparse.ArgumentParser):
        def error(self, message):
            self.exit(2, "USAGE: Invalid project proof command.\n")
        def print_help(self, file=None):
            self._print_message("USAGE: Commands are supervise, measure-migrations, cleanup and verify-receipt.\n", file)
    parser = SafeParser(prog="proof_project", allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True, parser_class=SafeParser)
    for name in ("supervise", "measure-migrations", "cleanup", "verify-receipt"):
        command = sub.add_parser(name, allow_abbrev=False)
        command.add_argument("--run-id", required=True)
        command.add_argument("--receipt" if name == "verify-receipt" else "--run-root", required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code)
    try:
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{7,63}", args.run_id) is None:
            _refuse(FailureCode.USAGE, Stage.DISPATCH)
        effects = effects if effects is not None else production_effects()
        if args.command == "verify-receipt":
            path = Path(args.receipt)
            try:
                binding = effects.host.receipt_binding(args.run_id, path, effects.receipt_writer)
            except BaseException:
                _receipt_refused()
            result = verify_receipt(path, expected=binding, effects=effects)
            failure = result.failure
        elif args.command == "cleanup":
            result = cleanup(args.run_id, run_root=Path(args.run_root), effects=effects)
            failure = None if result.ok else result.failures[0] if result.failures else Failure(Stage.TEARDOWN, FailureCode.CLEANUP_PRIVATE)
        else:
            root = Path(args.run_root)
            config = Config(args.run_id, root, root / "artifacts", Ports(5275, 8230, 8240, 8250))
            operation = supervise if args.command == "supervise" else measure_migration
            failure = operation(config, effects=effects).failure
        if failure is None:
            print("OK: Project proof command completed.")
            return 0
    except ProofRefused as error:
        failure = error.failure
    except (KeyboardInterrupt, SystemExit):
        failure = Failure(Stage.DISPATCH, FailureCode.CANCELLED)
    except BaseException:
        failure = Failure(Stage.DISPATCH, FailureCode.INTERNAL)
    print(failure.fixed_code.value + ": Project proof command failed.")
    return _exit_code(failure)


class _RetainedOutput:
    """Bounded private framing; retention is rebuilt from a closed event schema.

    No prefix is emitted while a frame is incomplete. Overflow discards the
    complete frame, including its prefix, and never grows a second buffer.
    """
    def __init__(self, project, limit=MAX_OUTPUT_BYTES):
        self._raw = bytearray()
        self._project = project
        self._private = _RUN_PRIVATE.get()
        self.limit = min(limit, MAX_OUTPUT_BYTES)
        self.overflow = False
        self.failure = None

    def __repr__(self):
        return "<bounded private child output>"

    def feed(self, chunk):
        if not isinstance(chunk, bytes):
            self.overflow = True
        if self.overflow or len(self._raw) + len(chunk) > self.limit:
            self.overflow = True
            self._raw.clear()
            return
        self._raw.extend(chunk)

    def finish(self):
        try:
            if self.overflow:
                return b""
            event = self._project(bytes(self._raw))
            if event is None:
                return b""
            # Encoding a new approved event is the only retention operation.
            if self._private is not None:
                self._private.check(event)
            return (json.dumps(event, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
        except ProofRefused as error:
            self.failure = error.failure
            return b""
        except BaseException:
            return b""
        finally:
            self._raw.clear()


def _approved_event(raw):
    """Optional child diagnostics: two closed enums, no arbitrary message."""
    value = _decode_public(raw)
    if type(value) is not dict or set(value) != {"stage", "fixed_code"}:
        return None
    stage = Stage(value["stage"])
    code = FailureCode(value["fixed_code"])
    return {"stage": stage.value, "fixed_code": code.value}


def _child_public(spec, raw):
    private = _RUN_PRIVATE.get() or _PrivateValues()
    private.environment(spec.environment)
    if spec.role == "runtime-version":
        name = spec.output_path.name.removesuffix("-version.txt")
        value = raw.decode("ascii").strip()
        private.check(value)
        patterns = {"node": r"v([0-9]+\.[0-9]+\.[0-9]+)",
            "cargo": r"cargo ([0-9]+\.[0-9]+\.[0-9]+)(?: \([a-f0-9]+ [0-9-]+\))?",
            "rustc": r"rustc ([0-9]+\.[0-9]+\.[0-9]+)(?: \([a-f0-9]+ [0-9-]+\))?",
            "wasm-pack": r"wasm-pack ([0-9]+\.[0-9]+\.[0-9]+)"}
        match = re.fullmatch(patterns[name], value)
        if match is None:
            return None
        return {"tool": name, "version": match.group(1)}
    value = _decode_public(raw)
    private.check(value)
    if type(value) is dict and set(value) == {"failure"}:
        return {"failure": FailureCode(value["failure"]).value}
    if spec.role in {"migration-child", "seed-child"}:
        request = json.loads(spec.environment["LEAF_PROJECT_CHILD_REQUEST"])
        if spec.role == "migration-child":
            _exact(value, ("run_id", "manifest_sha256", "ok", "database", "schema", "migration_count", "applied_migration_count"))
            expected = MigrationRequest(request["run_id"], Path(request["migration_root"]),
                tuple(FileProof(**item) for item in request["expected_manifest"]), {}, request["deadline"], Path(request["result_path"]))
            proof = _validate_migration(MigrationProof(**value), expected)
        else:
            _exact(value, ("organization_id", "binding_id", "subject", "tier", "authority", "role", "status"))
            expected = SeedRequest(request["run_id"], request["subject"], {}, request["deadline"], Path(request["result_path"]))
            proof = _validate_binding(BindingProof(**value), expected)
        return _proof_public(proof)
    if spec.role == "canonical-child":
        request = json.loads(spec.environment["LEAF_PROJECT_CANONICAL_REQUEST"])
        if request["purpose"] == "heartbeat":
            if value is None:
                return None
            _exact(value, ("worker_id", "tool_name", "runtime", "source_revision", "source_sha256", "observed_at"))
            if (value["worker_id"] != request["worker_id"] or value["tool_name"] != WORKER_TOOL
                    or not _hash_ok(value["source_sha256"]) or value["source_revision"] != "sha256:" + value["source_sha256"]
                    or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value["runtime"]) is None):
                return None
            beat = HeartbeatProof(**dict(value, observed_at=datetime.fromisoformat(value["observed_at"])))
            return _heartbeat_public(beat)
        _exact(value, ("stored_settings_sha256", "reopened_settings_sha256"))
        if not all(_hash_ok(item) for item in value.values()):
            return None
        return {"stored_settings_sha256": value["stored_settings_sha256"],
            "reopened_settings_sha256": value["reopened_settings_sha256"]}
    return _approved_event(raw)


class _OutputPipe:
    def __init__(self, spec):
        self.spec = spec
        private = _RUN_PRIVATE.get()
        def project(raw):
            token = _RUN_PRIVATE.set(private)
            try:
                return _child_public(spec, raw)
            finally:
                _RUN_PRIVATE.reset(token)
        self.capture = _RetainedOutput(project, spec.max_output_bytes)
        self.reader, self.writer = os.pipe()
        self.done = threading.Event()
        self.closed = False
        def drain():
            try:
                while True:
                    chunk = os.read(self.reader, 1024)
                    if not chunk:
                        break
                    self.capture.feed(chunk)
            except BaseException:
                self.capture.overflow = True
            finally:
                os.close(self.reader)
                self.done.set()
        self.thread = threading.Thread(target=drain, daemon=True)
        self.thread.start()

    def __repr__(self):
        return "<private output pipe>"

    def fileno(self):
        return self.writer

    def launched(self):
        if self.writer is not None:
            os.close(self.writer)
            self.writer = None

    def close(self):
        if self.closed:
            return
        self.launched()
        if not self.done.wait(CLEANUP_TIMEOUT_S):
            self.capture.overflow = True
            raise RuntimeError("child output unavailable")
        payload = self.capture.finish()
        if self.capture.failure is not None:
            raise ProofRefused(self.capture.failure)
        if self.spec.role == "runtime-version" and payload:
            event = json.loads(payload)
            payload = (event["tool"] + " " + event["version"] + "\n").encode()
        # Empty/invalid output fails the consumer's existing result validation.
        _write_synced(self.spec.output_path, payload)
        self.closed = True


class _OutputDestination:
    def __init__(self, spec):
        self.spec, self.pipe = spec, None
    def __repr__(self):
        return "<private output destination>"
    def open(self, mode):
        if mode != "xb" or self.pipe is not None:
            raise RuntimeError("child output unavailable")
        self.pipe = _OutputPipe(self.spec)
        return self.pipe
    def stat(self):
        from types import SimpleNamespace
        return SimpleNamespace(st_size=MAX_OUTPUT_BYTES + 1 if self.pipe.capture.overflow else 0)


class _CapturedRuntimeProcess(_RuntimeProcess):
    """Reuse A/D's launch, identity and stop implementation with a pipe target."""
    def __init__(self, spec):
        self.destination = _OutputDestination(spec) if spec.output_path is not None else None
        super().__init__(replace(spec, output_path=self.destination) if self.destination is not None else spec)

    def start(self):
        super().start()
        if self.output is not None:
            self.output.launched()

    def poll(self):
        result = super().poll()
        if result is not None and self.output is not None:
            self.output.close()
        return result


# All existing child launch sites (including canonical readback) now consume
# bounded pipes. A/D's retained ownership and stop methods are inherited.
_RuntimeProcess = _CapturedRuntimeProcess


def _launch_supervised(spec, *, scope):
    child = _launch_runtime(spec, scope=scope)
    guardian = getattr(scope, "guardian", None)
    if guardian is not None:
        guardian.own_process(child)
    return child


class _Cancellation:
    def __init__(self, scope):
        self.scope, self.previous = scope, {}
    def __enter__(self):
        import signal
        if threading.current_thread() is threading.main_thread():
            for name in (signal.SIGINT, signal.SIGTERM):
                self.previous[name] = signal.getsignal(name)
                signal.signal(name, self._signal)
        return self
    def _signal(self, signum, frame):
        self.scope.cancel()
        # Python signal handlers can reenter the commit RLock. Abort even in
        # the bytecode window between the last cancellation check and rename.
        if getattr(self.scope, "_receipt_committing", False):
            _refuse(FailureCode.CANCELLED, Stage.VERIFICATION)
    def __exit__(self, *args):
        import signal
        for name, handler in self.previous.items():
            signal.signal(name, handler)


class _SupervisorHost(_Host):
    def cancellation(self, scope):
        self.scope = scope
        return _Cancellation(scope)

    def guardian(self, plan):
        self.lease = _GuardianLease(plan)
        return self.lease

    def scrub(self, reservation, *, keep_safe):
        # Preserve a public absence observation only when every retained owner
        # has stopped and no stage can still allocate. Recovery can then finish
        # a failed filesystem scrub without inventing process-kill authority.
        scope, lease = getattr(self, "scope", None), getattr(self, "lease", None)
        if scope is not None and lease is not None and scope._settled():
            if all(child.stop(time.monotonic() + CLEANUP_TIMEOUT_S) is True for child in lease.children):
                root = reservation.run_root
                if _path_safe(root) and not _path_absent(root):
                    info = root.stat()
                    if reservation.root_identity == (info.st_dev, info.st_ino):
                        marker = root / "guardian-clean.json"
                        public = {"run_id": lease.plan.config.run_id, "processes_stopped": True, "guardian_disarmed": False}
                        if _path_absent(marker):
                            _write_synced(marker, (json.dumps(public, sort_keys=True) + "\n").encode())
        return _Host.scrub(reservation, keep_safe=keep_safe)

    def receipt_binding(self, run_id, path, writer):
        if path.name != RECEIPT_NAME or path.parent.name != "artifacts" or not _path_safe(path):
            _receipt_refused()
        journal = writer.read(path.parent.parent / "publication-intent.json")
        source = self.source_snapshot()
        if (type(journal) is not dict or journal.get("run_id") != run_id or journal.get("publication") != RECEIPT_NAME
                or journal.get("run_root") != str(path.parent.parent) or journal.get("artifact_root") != str(path.parent)
                or journal.get("source") != _source_public(source) or journal.get("schema") != SCHEMA
                or journal.get("profile") != PROFILE or not _public_id(journal.get("public_key_thumbprint"))):
            _receipt_refused()
        return ReceiptBinding(run_id, source, journal["public_key_thumbprint"])

    def recovery(self, run_id, root):
        if (not root.is_absolute() or root == Path(root.anchor) or root.is_relative_to(REPO)
                or REPO.is_relative_to(root) or not _path_safe(root)):
            _refuse(FailureCode.CLEANUP_PRIVATE, Stage.TEARDOWN)
        journal = _read_receipt(root / "publication-intent.json")
        info = root.stat()
        if (journal.get("run_id") != run_id or journal.get("run_root") != str(root)
                or journal.get("artifact_root") != str(root / "artifacts")
                or journal.get("root_identity") != [info.st_dev, info.st_ino]):
            _refuse(FailureCode.CLEANUP_PRIVATE, Stage.TEARDOWN)
        value = journal["authority"]
        if value is None:
            ownership = self.authority_inventory(run_id)
            _validate_ownership(ownership, run_id, allocated=False)
        else:
            ownership = _read_owned_public(value, run_id)
            if ownership.kind == "native":
                self._native_context = (run_id, root, ownership.port)
                # Public journals do not transfer process-stop capability.
                _validate_ownership(self.authority_inventory(run_id), run_id, allocated=False)
            else:
                _validate_ownership(ownership, run_id, allocated=True)
        return Reservation(root, root / "artifacts", (), (info.st_dev, info.st_ino)), ownership

    @staticmethod
    def recovery_processes_absent(run_id, root):
        # A run interrupted before final cleanup can still have owned children.
        # Without the guardian's transferred handles, refuse to infer absence.
        marker = root / "guardian-clean.json"
        try:
            return _read_receipt(marker) == {"run_id": run_id, "processes_stopped": True, "guardian_disarmed": True}
        except BaseException:
            return False


class _GuardianLease:
    """Private pipes carry Windows job handles or start-bound POSIX groups.

    No listener or network endpoint is used. The host fault experiment remains
    separate from fake-only receipt tests.
    """
    def __init__(self, plan):
        self.plan, self.process = plan, None
        self.sent_jobs = set()
        self.sent_groups = set()
        self.responses = None
        self.children = []

    def __repr__(self):
        return "<project guardian lease>"

    def _send(self, value):
        if self.process is None or self.process.poll() is not None:
            _refuse(FailureCode.GUARDIAN, Stage.ALLOCATION)
        raw = (json.dumps(value, separators=(",", ":")) + "\n").encode()
        if len(raw) > 16384:
            _refuse(FailureCode.GUARDIAN, Stage.ALLOCATION)
        self.process.stdin.write(raw)
        self.process.stdin.flush()

    def _response(self):
        import queue
        try:
            return self.responses.get(timeout=CLEANUP_TIMEOUT_S) == b"OK\n"
        except queue.Empty:
            return False

    def arm(self):
        import queue
        self.responses = queue.Queue(maxsize=4)
        code = "import runpy,sys; m=runpy.run_path(sys.argv[1]); sys.exit(m['_guardian_entry']())"
        environment = _PrivateEnvironment(_clean_libpq(_essential_environment(self.plan.host.environment)))
        if (_authority_kind(self.plan.host.environment) == "native"
                and "LEAF_PG_BIN" in self.plan.host.environment):
            environment["LEAF_PG_BIN"] = self.plan.host.environment["LEAF_PG_BIN"]
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        options = ({"creationflags": 0x08000000} if _PLATFORM == "win32"
                   else {"start_new_session": True})
        self.process = subprocess.Popen((sys.executable, "-B", "-c", code, str(REPO / "scripts/proof_project.py")),
            cwd=REPO.parent, env=dict(environment), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, **options)
        def read():
            try:
                while True:
                    line = self.process.stdout.readline(8)
                    self.responses.put(line if line == b"OK\n" else b"FAILED\n", timeout=CLEANUP_TIMEOUT_S)
                    if not line or line != b"OK\n":
                        break
            except BaseException:
                pass
        threading.Thread(target=read, daemon=True).start()
        root = self.plan.reservation
        self._send({"event": "init", "run_id": self.plan.config.run_id, "run_root": str(root.run_root),
            "root_identity": list(root.root_identity), "deadline": time.monotonic() + 3600.0})
        if not self._response():
            _refuse(FailureCode.GUARDIAN, Stage.ALLOCATION)

    def observe(self, ownership):
        _validate_ownership(ownership, self.plan.config.run_id, allocated=True)
        message = {"event": "authority", "ownership": _owned_public(ownership)}
        if ownership.kind == "native":
            message["identity"] = {"pid": ownership.pid, "start_id": ownership.start_id,
                                   "started_at": ownership.started_at}
        self._send(message)
        if not self._response():
            _refuse(FailureCode.GUARDIAN, Stage.ALLOCATION)

    def own_process(self, child):
        if _PLATFORM != "win32":
            identity = getattr(child, "identity", None)
            if (type(identity) is not ProcessIdentity
                    or not _group_fields(identity.pid, identity.start_id, identity.role)):
                _refuse(FailureCode.GUARDIAN, Stage.STACK)
            key = (identity.pid, identity.start_id)
            if key in self.sent_groups:
                return
            self._send({"event": "group", "pid": identity.pid,
                        "start": identity.start_id, "role": identity.role})
            if not self._response():
                _refuse(FailureCode.GUARDIAN, Stage.STACK)
            self.sent_groups.add(key)
            self.children.append(child)
            return
        job = getattr(child, "job", None)
        if job is None or not job.handle:
            _refuse(FailureCode.GUARDIAN, Stage.STACK)
        if id(job) in self.sent_jobs:
            return
        import ctypes
        from ctypes import wintypes
        kernel = job.kernel
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
            ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        duplicate = wintypes.HANDLE()
        if not kernel.DuplicateHandle(kernel.GetCurrentProcess(), job.handle, int(self.process._handle),
                ctypes.byref(duplicate), 0, False, 2):
            _refuse(FailureCode.GUARDIAN, Stage.STACK)
        self._send({"event": "job", "handle": duplicate.value})
        if not self._response():
            _refuse(FailureCode.GUARDIAN, Stage.STACK)
        self.sent_jobs.add(id(job))
        self.children.append(child)

    def disarm(self):
        if self.process is None:
            return True
        try:
            self._send({"event": "disarm"})
            if not self._response():
                return False
            self.process.stdin.close()
            return self.process.wait(timeout=CLEANUP_TIMEOUT_S) == 0
        except BaseException:
            return False


def _group_fields(pid, start, role):
    return (type(pid) is int and 0 < pid <= 2147483647
            and type(start) is str and re.fullmatch(r"[ -~]{1,128}", start) is not None
            and type(role) is str and re.fullmatch(r"[a-z][a-z0-9-]{0,63}", role) is not None)


class _TransferredGroup:
    def __init__(self, pid, start, role):
        if not _group_fields(pid, start, role):
            raise RuntimeError("group identity unavailable")
        self.pid, self.start, self.role = pid, start, role
        self.stopped = False

    def __repr__(self):
        return "<transferred owned group>"

    def stop(self, deadline):
        if self.stopped:
            return True
        try:
            remaining = deadline - time.monotonic()
            if not math.isfinite(remaining) or remaining <= 0:
                return False
            row = _posix_process_row(self.pid, timeout=min(2.0, remaining))
            if time.monotonic() >= deadline:
                return False
            if row is not None and row.start == self.start and row.pgid == self.pid:
                _signal_group(self.pid, signal.SIGKILL)
            # A gone/replaced leader never authorizes a signal. Group absence,
            # not a stale PID or a successful kill, is the completion evidence.
            self.stopped = _group_empty(self.pid, deadline)
            return self.stopped
        except Exception:
            return False


class _TransferredJob:
    def __init__(self, handle):
        self.job = _WindowsJob()
        self.job.close()
        self.job.handle = handle
        self.stopped = False
    def __repr__(self):
        return "<transferred owned job>"
    def stop(self, deadline):
        if not self.stopped:
            self.stopped = self.job.stop(deadline)
            if self.stopped:
                self.job.close()
        return self.stopped


def _guardian_object(pairs):
    """Refuse ambiguous objects at every nesting level of the private protocol."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate guardian message key")
        result[key] = value
    return result


def _guardian_entry():
    """Fixed private child entry, deliberately absent from the public parser."""
    import queue
    messages = queue.Queue(maxsize=64)
    def read():
        try:
            while True:
                raw = sys.stdin.buffer.readline(16385)
                if not raw:
                    break
                if len(raw) > 16384:
                    break
                try:
                    item = json.loads(raw, object_pairs_hook=_guardian_object)
                except ValueError:
                    # Invalid input is not a clean EOF and must never be acknowledged.
                    messages.put_nowait({})
                    break
                messages.put_nowait(item)
        except BaseException:
            pass
        finally:
            try:
                messages.put_nowait(None)
            except queue.Full:
                pass
    def acknowledge():
        sys.stdout.write("OK\n")
        sys.stdout.flush()
    try:
        threading.Thread(target=read, daemon=True).start()
        initial = messages.get(timeout=CLEANUP_TIMEOUT_S)
        _exact(initial, ("event", "run_id", "run_root", "root_identity", "deadline"))
        run_id, root = initial["run_id"], Path(initial["run_root"])
        if (initial["event"] != "init" or not _path_safe(root) or not root.is_absolute()
                or root.is_relative_to(REPO) or REPO.is_relative_to(root)):
            return 1
        info = root.stat()
        if initial["root_identity"] != [info.st_dev, info.st_ino]:
            return 1
        name = "leafproof-" + run_id
        owned = AuthorityOwnership(run_id, (name, name + "-pg", name + "-relay"),
            (("leaf.proof-project.run", run_id), ("leaf.proof-project.owner", "proof_project_pg")))
        _validate_ownership(owned, run_id, allocated=False)
        effects = production_effects()
        jobs = []
        class Channel:
            def receive(self, timeout):
                try:
                    item = messages.get(timeout=timeout)
                except queue.Empty:
                    return None
                if item is None:
                    raise EOFError
                if item == {"event": "disarm"}:
                    return "disarm"
                if type(item) is not dict:
                    return "invalid"
                if set(item) in ({"event", "ownership"}, {"event", "ownership", "identity"}) and item["event"] == "authority":
                    identity = None
                    if type(item["ownership"]) is dict and item["ownership"].get("kind") == "native":
                        _exact(item, ("event", "ownership", "identity"))
                        identity = _exact(item["identity"], ("pid", "start_id", "started_at"))
                    else:
                        _exact(item, ("event", "ownership"))
                    ownership = _read_owned_public(item["ownership"], run_id, native_identity=identity)
                    _validate_ownership(ownership, run_id, allocated=True)
                    if ownership.kind == "native":
                        effects.host._native_context = (run_id, root, ownership.port)
                    object.__setattr__(request, "ownership", ownership)
                elif (set(item) == {"event", "handle"} and item["event"] == "job"
                        and type(item["handle"]) is int and item["handle"] > 0):
                    jobs.append(_TransferredJob(item["handle"]))
                elif (_PLATFORM != "win32" and set(item) == {"event", "pid", "start", "role"}
                        and item["event"] == "group"
                        and _group_fields(item["pid"], item["start"], item["role"])):
                    jobs.append(_TransferredGroup(item["pid"], item["start"], item["role"]))
                else:
                    return "invalid"
                acknowledge()
                return "alive"
            def acknowledge(self):
                acknowledge()
        request = GuardianRequest(run_id, Reservation(root, root / "artifacts", (), (info.st_dev, info.st_ino)),
            owned, Channel(), jobs, initial["deadline"])
        acknowledge()
        return guardian_main(request, effects=effects)
    except BaseException:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
