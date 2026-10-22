"""Managed project proof, records A and B: runtime and isolated authority.

There is intentionally no CLI or success-receipt path. Later records implement
the lifecycle against these seams. Importing this module performs no host work.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from contextlib import redirect_stdout
from datetime import datetime, timezone
from enum import Enum
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol


REPO = Path(__file__).resolve().parent.parent
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


@dataclass(frozen=True)
class CanonicalRequest:
    purpose: str = "descriptor"
    tool_name: str = WORKER_TOOL


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

    def release_ports(self):
        for handle in self.port_handles:
            handle.close()


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


class RunScope(Protocol):
    """D supplies lifetime management; A/B synchronously register acquisitions."""
    def acquire(self, name: ResourceKind, cleanup: Callable[[], None]) -> None: ...
    def close(self) -> Any: ...


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
    # B's host operations keep imports, file reads and teardown behind a seam.
    def migration_entry_present(self) -> bool: ...
    def migration_corpus(self) -> tuple[tuple[FileProof, ...], tuple[str, ...]]: ...
    def database_module(self, environment: Mapping[str, str]) -> Any: ...
    def store_module(self) -> Any: ...
    def cleanup_authority(self, run_id: str) -> Any: ...
    def close_identity(self, lease: IdentityLease) -> None: ...
    def read_child_result(self, path: Path) -> Any: ...


class ReceiptWriter(Protocol):
    def ownership(self, path: Path, value: OwnershipJournal) -> None: ...
    def publish(self, path: Path, value: Any) -> None: ...
    def read(self, path: Path) -> Any: ...


@dataclass(frozen=True, repr=False)
class Effects:
    # Later request/result types belong to B/C/D/E, without changing these seams.
    authority_factory: Callable[[str], Any]
    issuer_factory: Callable[[str], Any]
    migration_runner: Callable[..., Any]
    seed_operation: Callable[..., Any]
    process_launcher: Callable[..., OwnedProcess]
    readiness_probe: Callable[[Any], Any]
    browser_runner: Callable[..., Any]
    canonical_reader: Callable[[CanonicalRequest], WorkerDescriptor]
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
            return self.issuer.mint(self.run_id, tenant_id=binding.organization_id,
                org_id=binding.organization_id, tier=binding.tier, claim_ns=CLAIM_NAMESPACE,
                lifetime_s=lifetime_s, now=now)
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
        return _validate_migration(proof, request)
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


def _validate_binding(proof, request):
    if (type(proof) is not BindingProof or proof.subject != request.subject
            or proof.tier != TIER or proof.authority != "auth0"
            or proof.role != "owner" or proof.status != "active"
            or any(not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9-]{1,64}", value) is None
                   for value in (proof.organization_id, proof.binding_id))):
        _refuse(FailureCode.BINDING, Stage.BINDING)
    return proof


def _validate_ownership(owned, run_id, *, allocated):
    name = "leafproof-" + run_id
    if (type(owned) is not AuthorityOwnership or owned.run_id != run_id
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
        if plan.reservation is None:
            _refuse(FailureCode.ALLOCATION, Stage.ALLOCATION)
        _check_entry(effects)
        _check_corpus(plan.migrations, effects=effects)
        run_id, root = plan.config.run_id, plan.config.run_root
        stage, code = Stage.ALLOCATION, FailureCode.ALLOCATION
        ownership = _validate_ownership(effects.host.authority_inventory(run_id), run_id, allocated=False)
        authority = None
        scope.acquire(ResourceKind.AUTHORITY, lambda: authority.stop() if authority is not None
                      else effects.host.cleanup_authority(run_id))
        effects.receipt_writer.ownership(root / "authority-planned.json", OwnershipJournal(
            run_id, root, plan.config.artifact_root, effects.clock.utc_now(), ownership))
        start = effects.clock.monotonic()
        authority = effects.authority_factory(run_id)
        ownership = _validate_ownership(effects.host.authority_inventory(run_id), run_id, allocated=True)
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
        database = DatabaseProof(run_id, ownership, "host-relay", transport, plan.migrations,
            _manifest_hash(plan.migrations), migration, allocated - start, relayed - allocated, migrated - relayed)
        identity = IdentityProof(issuer.thumbprint, issuer.issuer, issuer.audience, binding.organization_id,
                                 binding.binding_id, binding.subject, binding.tier)
        return PreparedAuthority(authority, dsn, lease, database, identity, binding)
    except ProofRefused:
        raise
    except Exception:
        _refuse(code, stage)


def _later(record):
    def unavailable(*args, **kwargs):
        raise RuntimeError("record " + record + " adapter is not implemented")
    return unavailable


def _browser_unavailable(request, *, scope):
    _refuse(FailureCode.BROWSER_UNAVAILABLE, Stage.BROWSER)


class _Clock:
    monotonic = staticmethod(time.monotonic)
    wait = staticmethod(time.sleep)

    @staticmethod
    def utc_now():
        return datetime.now(timezone.utc)


def _canonical_reader(request):
    if request.purpose != "descriptor":
        return _later("C")(request)
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


class _Host:
    scrub = staticmethod(_later("D"))

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

    @staticmethod
    def authority_inventory(run_id):
        pg = _pg_module()
        pg._validate(run_id)
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

    @staticmethod
    def close_identity(lease):
        object.__setattr__(lease, "issuer", None)
        lease.jwks_path.unlink(missing_ok=True)

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

    def reserve(self, plan):
        config = plan.config
        if not _path_safe(config.run_root) or not _path_safe(config.artifact_root):
            raise RuntimeError("unsafe reservation")
        _private_directory(config.run_root)
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
                if os.name == "nt":
                    handle.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                handle.bind(("127.0.0.1", port))
            return Reservation(config.run_root, config.artifact_root, tuple(handles))
        except Exception:
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
    publish = staticmethod(_later("E"))
    read = staticmethod(_later("E"))

    @staticmethod
    def ownership(path, value):
        public = {"run_id": value.run_id, "run_root": str(value.run_root),
                  "artifact_root": str(value.artifact_root), "created_at": value.created_at.isoformat()}
        if value.authority is not None:
            owned = value.authority
            public["authority"] = {"run_id": owned.run_id, "names": list(owned.names),
                "labels": dict(owned.labels), "containers": list(owned.containers), "networks": list(owned.networks)}
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

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class _RuntimeProcess:
    def __init__(self, spec):
        self.spec = spec
        self.process = None
        self.job = None
        self.output = None
        self.identity = None

    def __repr__(self):
        return "<owned runtime process>"

    def start(self):
        spec = self.spec
        if spec.role not in {"runtime-version", "runtime-build", "migration-child", "seed-child"}:
            return _later("C")(spec)
        try:
            if spec.output_path is not None:
                self.output = spec.output_path.open("xb")
            if os.name == "nt":
                self.job = _WindowsJob()
            self.process = subprocess.Popen(spec.argv, cwd=spec.cwd, env=dict(spec.environment),
                stdin=subprocess.DEVNULL, stdout=self.output or subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=(0x08000000 | 0x4) if os.name == "nt" else 0,
                start_new_session=os.name != "nt")
            if self.job is not None:
                start_id = self.job.attach_and_resume(self.process)
            else:
                # Linux's OS start tick; fail closed on unsupported POSIX hosts.
                start_id = Path(f"/proc/{self.process.pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
            self.identity = ProcessIdentity(spec.role, self.process.pid, start_id, os.getpid())
        except Exception:
            self.stop(time.monotonic() + 5.0)
            raise RuntimeError("runtime process start failed") from None

    def poll(self):
        if self.output is not None and self.spec.output_path.stat().st_size > self.spec.max_output_bytes:
            self.stop(time.monotonic() + 5.0)
            return 1
        return self.process.poll()

    def children(self, pids):
        return _later("C")(pids)

    def stop(self, deadline):
        try:
            if self.job is not None:
                self.job.close()
            if self.process is not None:
                if os.name != "nt":
                    import signal
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                elif self.process.poll() is None:
                    self.process.kill()
                self.process.wait(timeout=max(0.01, deadline - time.monotonic()))
            return True
        except Exception:
            return False
        finally:
            if self.output is not None:
                self.output.close()
                self.output = None


def _launch_runtime(spec, *, scope):
    process = _RuntimeProcess(spec)
    # Registration is synchronous and precedes the first possible child.
    kind = {"migration-child": ResourceKind.MIGRATION_PROCESS,
            "seed-child": ResourceKind.SEED_PROCESS}.get(spec.role, ResourceKind.RUNTIME_PROCESS)
    scope.acquire(kind, lambda: process.stop(time.monotonic() + 5.0))
    process.start()
    return process


def _pg_module():
    with _IMPORT_LOCK:
        name = "_leaf_project_pg_environment"
        return sys.modules.get(name) or _load_file(name, REPO / "scripts/proof_project_pg.py")


def _authority_factory(run_id):
    return _pg_module().PgAuthority.start(run_id)


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
        env = _PrivateEnvironment(_clean_libpq(_essential_environment(os.environ)))
        env["DATABASE_URL"] = os.environ.get("DATABASE_URL", "")
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
                return _validate_migration(MigrationProof(**raw), request)
            return _validate_binding(BindingProof(**raw), request)
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


def production_effects() -> Effects:
    """Adapters are lazy: construction imports no worker and acquires nothing."""
    effects = Effects(_authority_factory, _issuer_factory, None, None,
                   _launch_runtime, _later("C"), _browser_unavailable, _canonical_reader,
                   _Clock(), _Host(), _ReceiptWriter())
    return replace(effects,
        migration_runner=lambda request, *, scope: _run_database_child(request, effects=effects, scope=scope),
        seed_operation=lambda request, *, scope: _run_database_child(request, effects=effects, scope=scope))
