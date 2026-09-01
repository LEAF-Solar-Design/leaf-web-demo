"""Operator egress boundary (contract/OPERATOR.md section 7, obligations 2-3).

The obligation: no generic operator handler can CALL a production deploy route.
A source scan cannot prove it (a handler can call a neutral helper in any file
that reaches production), so the boundary is enforced at RUNTIME, at the process
audit hook over outbound sockets and process spawns.

TWO LAYERS, because a single context-scoped check is escapable:

  Layer 1 - UNCONDITIONAL (no context): the known production deploy CONTROL
  PLANE (the production surface, the Vercel deploy API, AWS ECS, cloud metadata)
  and the deploy CLIs (`vercel`, `aws`, ...) are denied for the WHOLE process,
  always. This is what an earlier context-only guard got wrong: a handler could
  run a neutral helper through contextvars.Context().run(), a raw threading.
  Thread, or loop.run_in_executor, all of which start with a FRESH context where
  a context-scoped flag reads its default. Layer 1 has no context to escape, so
  those escapes reaching a real deploy route are denied anyway. It is a denylist
  of KNOWN, FINITE production endpoints (not a credential-name denylist over an
  unbounded space), and it is safe for the tenant, which never reaches them (the
  tenant's egress is developer.api.autodesk.com + S3, per broker.py).

  Layer 2 - operator context (DENY-BY-DEFAULT): while an operator handler runs
  (armed by require_operator), ONLY loopback + the platform DB + a deployment-
  declared extra are permitted, and no subprocess may spawn. This closes an
  ALIASED / string-composed / environment-provided target on the innocent
  (same-context) path, which Layer 1's denylist would miss.

IRREDUCIBLE in-process residual (bounded, named, not waived): a handler that
DELIBERATELY runs a helper in a fresh context (contextvars.Context().run) AND
targets a host that is neither a known deploy route (Layer 1) nor caught while
armed (Layer 2 escaped) - i.e. a pre-arranged alias reached by deliberate
evasion - and any egress a spawned CHILD process makes, are outside an in-process
hook. The complete boundary is the deployment's NETWORK EGRESS POLICY (the
operator plane's task/container denies routes to the production deploy control
plane); this guard is the in-process defense-in-depth for it. See
docs: contract/OPERATOR.md section 7.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import os
import re
import sys
import urllib.parse
from typing import Iterable, Iterator

_operator_armed: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "leaf_operator_egress_armed", default=False)


class OperatorEgressDenied(RuntimeError):
    """Raised inside the audit hook when egress to a production deploy route
    (any context) or non-allowlisted host / a spawn (operator context) is
    attempted. Propagates out of the audited operation, so it never happens."""

    def __init__(self, target: str, kind: str):
        super().__init__(
            f"operator_egress_denied: {kind} to {target!r} is not permitted "
            f"(production is unreachable from the operator plane)")
        self.target = target
        self.kind = kind


# --- Observability (TEL-1): a firing denial is a firing security control, so it
#     must be visible three ways -- a structured log line, a product event, and
#     an EMF metric -- BEFORE the exception propagates. All three are best-effort
#     (mirrors emf_metrics.py / telemetry_sink.py's own never-raise contract):
#     telemetry failing must never mask, delay, or weaken the deny itself. The
#     imports are LAZY and individually guarded, not module-level, because this
#     guard installs itself at IMPORT TIME (see install_operator_egress_guard()
#     below) -- a broken telemetry_sink or emf_metrics import must never be able
#     to stop the security hook from installing. -------------------------------
_HOST_CLASS_CLOUD_METADATA = "cloud_metadata"
_HOST_CLASS_PRODUCTION_CONTROL_PLANE = "production_control_plane"
_HOST_CLASS_OPERATOR_DISALLOWED = "operator_disallowed_host"
_HOST_CLASS_DEPLOY_CLI = "deploy_cli"
# Bounded, ALLOWLISTED classes only -- never the raw host, which can carry a
# caller-composed or env-provided value (and therefore secrets). Kept in sync
# by hand with the classification below; a class outside this set is a bug in
# _host_class, never a value read from the network.
_HOST_CLASS_ALLOW = frozenset({
    _HOST_CLASS_CLOUD_METADATA, _HOST_CLASS_PRODUCTION_CONTROL_PLANE,
    _HOST_CLASS_OPERATOR_DISALLOWED, _HOST_CLASS_DEPLOY_CLI,
})
_CLOUD_METADATA_HOSTS = frozenset({"169.254.169.254", "metadata.google.internal"})


def _host_class(target: str, kind: str) -> str:
    """Classify `target` into a bounded CLASS, never the raw value. A spawn
    event's target is a program name (kind is one of the _SPAWN_EVENTS
    kinds); a network event's target is a host, classified against the same
    Layer 1 / metadata sets the guard itself enforces against."""
    if kind in ("deploy-cli-spawn", "process-spawn"):
        return _HOST_CLASS_DEPLOY_CLI
    host = (target or "").strip().lower().rstrip(".")
    if host in _CLOUD_METADATA_HOSTS:
        return _HOST_CLASS_CLOUD_METADATA
    if _is_deploy_control_plane(host):
        return _HOST_CLASS_PRODUCTION_CONTROL_PLANE
    return _HOST_CLASS_OPERATOR_DISALLOWED


def _log_egress_denied(kind: str, host_class: str, caller_surface: str) -> None:
    """One structured JSON line to stderr -- the SAME wire discipline
    emf_metrics._write / telemetry_sink use (the awslogs driver ships stderr
    to CloudWatch Logs), so a firing denial is greppable/alertable even if the
    metric or the event never lands. No raw target: see _host_class."""
    try:
        doc = {
            "event": "operator_egress_denied",
            "kind": kind,
            "host_class": host_class,
            "caller_surface": caller_surface,
        }
        sys.stderr.write(json.dumps(doc, separators=(",", ":"), allow_nan=False) + "\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 - logging must never block a deny
        pass


def _emit_egress_denied_event(kind: str, host_class: str, caller_surface: str) -> None:
    """Product event security.egress_denied (docs/PLATFORM_TELEMETRY.md
    domain.action convention). The audit hook fires inside the process's own
    network/spawn layer, not a request, so there is no tenant/session to
    stamp; "system" identifies a process-level security emission the same way
    other emitters stamp "guest"/"account"/"anon" for theirs."""
    try:
        import telemetry_sink

        telemetry_sink.emit(
            "security.egress_denied",
            tenant_id="system",
            tenant_kind="system",
            session_id="system",
            labels={"kind": kind, "host_class": host_class, "caller_surface": caller_surface},
        )
    except Exception:  # noqa: BLE001 - a security denial must never be undone by telemetry
        pass


def _emit_egress_denied_metric(kind: str) -> None:
    """EMF metric EgressDenied (Count) in Leaf/Platform/APS, dimensioned by
    `kind` ONLY (LOW-CARDINALITY, matching emf_metrics.py's own dimension
    discipline). Reuses emf_metrics's own EMF writer so the wire format (the
    `_aws` CloudWatchMetrics envelope, one JSON line to stderr) stays
    byte-identical to every other Leaf/Platform/APS metric."""
    try:
        import emf_metrics

        if emf_metrics._DISABLED:  # honor APS_EMF_DISABLED like every emit_* there
            return
        directives = [{
            "Namespace": emf_metrics.NAMESPACE,
            "Dimensions": [["kind"]],
            "Metrics": [{"Name": "EgressDenied", "Unit": "Count"}],
        }]
        emf_metrics._emit(directives, {"kind": kind, "EgressDenied": 1})
    except Exception:  # noqa: BLE001 - a security denial must never be undone by telemetry
        pass


def _deny(target: str, kind: str, *, armed: bool) -> OperatorEgressDenied:
    """Single choke point for every OperatorEgressDenied: emit (log, product
    event, EMF metric) for the firing denial, THEN return the exception for
    the caller to raise. Emission runs BEFORE the raise and is entirely
    best-effort, so a telemetry fault can never suppress, delay, or soften the
    deny -- the raise always happens even if every emit above failed."""
    host_class = _host_class(target, kind)
    caller_surface = "operator_handler" if armed else "process"
    _log_egress_denied(kind, host_class, caller_surface)
    _emit_egress_denied_event(kind, host_class, caller_surface)
    _emit_egress_denied_metric(kind)
    return OperatorEgressDenied(target, kind)


# --- Layer 1: the KNOWN production deploy control plane, denied for the whole
#     process, unconditionally (un-escapable by any context/thread/executor). ---
# EXACT production hosts only. NB: this must NOT catch STAGING, which is
# legitimate operator egress (the plane stages releases). `api.leafdesign.ai` is
# production; `staging-api.leafdesign.ai` is staging and is deliberately absent,
# so an exact/suffix match here never denies a staging host.
_DEPLOY_HOST_EXACT = frozenset({
    "api.leafdesign.ai", "leafdesign.ai", "www.leafdesign.ai",  # production surface
    "api.vercel.com", "vercel.com",   # Vercel deploy API
    "169.254.169.254",                # cloud metadata (IMDS)
    "metadata.google.internal",       # GCE metadata
    # 169.254.170.2 (the ECS task metadata/credentials endpoint) is deliberately
    # NOT here. On ECS it is the task's OWN link-local endpoint — the only way
    # the process reads its container metadata (health's task_definition_arn
    # field, server/app.py:_ecs_task_definition_arn) and its task-role
    # credentials. It is not a production deploy control plane: it hands out
    # the STAGING task's identity, and reaching production still requires a
    # route Layer 1 denies (ecs.*.amazonaws.com, api.leafdesign.ai, ...).
    # Denying it process-wide made every /api/health raise OperatorEgressDenied
    # on ECS, so no task at or after cb653b01 could pass ELB health checks and
    # every staging app forward deploy timed out at promote (runs 32173719414,
    # 32174277786 on 2026-08-18). Layer 2 still denies it while an operator
    # handler is armed, which is the contract's actual obligation.
})
_DEPLOY_HOST_PATTERNS = (
    re.compile(r"(^|\.)vercel\.(com|app)$", re.I),
    # AWS ECS / Elastic Beanstalk across FIPS (ecs-fips.*), dual-stack
    # (ecs.<region>.api.aws), and partitions (.amazonaws.com / .amazonaws.com.cn).
    re.compile(r"^ecs(-fips)?\.[a-z0-9-]+\.amazonaws\.com(\.cn)?$", re.I),
    re.compile(r"^ecs(-fips)?\.[a-z0-9-]+\.api\.aws$", re.I),
    re.compile(r"^elasticbeanstalk(-fips)?\.[a-z0-9-]+\.amazonaws\.com(\.cn)?$", re.I),
)
# Deploy CLIs: a spawn of one is a production deploy attempt, denied process-wide
# (the tenant never spawns these; its toolchain is unrelated). Matched by
# basename with the executable extension stripped (_cli_basename), so aws.exe /
# aws.cmd match "aws". A wrapper (npx/pnpm/...) is inspected one token deeper.
_DEPLOY_CLIS = frozenset({
    "vercel", "aws", "gcloud", "kubectl", "eb", "flyctl", "netlify", "wrangler",
    "sam", "cdk", "serverless", "sls", "terraform", "tofu", "pulumi", "helm",
    "sst", "copilot", "eksctl", "kustomize", "skaffold",
})
_WRAPPERS = frozenset({
    "npx", "pnpm", "yarn", "npm", "pipx", "uvx", "dotnet", "bunx", "bun",
})


def _is_deploy_control_plane(host: str) -> bool:
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False
    if host in _DEPLOY_HOST_EXACT:
        return True
    return any(p.search(host) for p in _DEPLOY_HOST_PATTERNS)


# --- Layer 2: operator-context deny-by-default allowlist ---------------------
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost", "0.0.0.0", "::"})

_NET_EVENTS = frozenset({"socket.connect", "socket.getaddrinfo"})
_SPAWN_EVENTS = frozenset({
    "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn",
})
_WATCHED = _NET_EVENTS | _SPAWN_EVENTS

_installed = False


def _allowlist() -> tuple[frozenset[str], tuple[str, ...]]:
    hosts = set(_LOOPBACK)
    suffixes: list[str] = []
    for var in (
        "DATABASE_URL",
        "LEAF_PLATFORM_DATABASE_URL",
        "PLATFORM_DATABASE_URL",
        "BROKER_URL",
    ):
        url = os.environ.get(var, "")
        if url:
            host = urllib.parse.urlsplit(url).hostname
            if host:
                hosts.add(host.lower())
    pghost = os.environ.get("PGHOST", "").strip().lower()
    if pghost:
        hosts.add(pghost)
    for extra in filter(None, (e.strip().lower()
                               for e in os.environ.get(
                                   "LEAF_OPERATOR_EGRESS_ALLOW", "").split(","))):
        (suffixes.append(extra) if extra.startswith(".") else hosts.add(extra))
    return frozenset(hosts), tuple(suffixes)


def _host_allowed(host: str) -> bool:
    host = (host or "").strip().lower()
    if not host:
        return False
    exact, suffixes = _allowlist()
    if host in exact:
        return True
    return any(host.endswith(sfx) for sfx in suffixes)


def _connect_host(address: object) -> str | None:
    if isinstance(address, tuple) and address:
        h = address[0]
        return h.decode() if isinstance(h, bytes) else str(h)
    return None  # AF_UNIX / local socket: cannot reach a remote deploy route.


_EXE_EXT = (".exe", ".cmd", ".bat", ".ps1")


def _basename_noext(tok: str) -> str:
    b = os.path.basename((tok or "").strip().strip('"').lower())
    for ext in _EXE_EXT:
        if b.endswith(ext):
            return b[: -len(ext)]
    return b


def _spawn_tokens(args: tuple) -> list[str]:
    # The audit args differ per event: subprocess.Popen is
    # (executable, argv, cwd, env) where argv is a list (POSIX) or a joined
    # command string (Windows); os.system is (command,); os.exec is
    # (path, argv, env). Prefer the argv sequence; fall back to splitting a
    # command string. Return the argv tokens.
    argv_list = None
    cmd_str = None
    for a in args:
        if isinstance(a, bytes):
            a = a.decode(errors="ignore")
        if isinstance(a, (list, tuple)) and a and argv_list is None:
            argv_list = [t.decode(errors="ignore") if isinstance(t, bytes) else str(t)
                         for t in a]
        elif isinstance(a, str) and a and cmd_str is None:
            cmd_str = a
    if argv_list:
        return argv_list
    return cmd_str.split() if cmd_str else []


def _is_deploy_cli_spawn(tokens: list[str]) -> bool:
    if not tokens:
        return False
    names = [_basename_noext(t) for t in tokens]
    if names[0] in _DEPLOY_CLIS:
        return True
    # A wrapper (npx cdk deploy, pnpm exec vercel ...) is inspected one or two
    # tokens deeper. Not the whole argv, to avoid denying a benign argument.
    if names[0] in _WRAPPERS:
        return any(n in _DEPLOY_CLIS for n in names[1:3])
    return False


def _audit_hook(event: str, args: tuple) -> None:
    if event not in _WATCHED:
        return
    armed = _operator_armed.get()

    if event in _SPAWN_EVENTS:
        tokens = _spawn_tokens(args)
        target = tokens[0] if tokens else event
        # Layer 1: a deploy-CLI spawn is denied for the whole process, always
        # (incl. wrapper forms like `npx cdk deploy`).
        if _is_deploy_cli_spawn(tokens):
            raise _deny(target, "deploy-cli-spawn", armed=armed)
        # Layer 2: an operator handler spawns NO process at all.
        if armed:
            raise _deny(target, "process-spawn", armed=armed)
        return

    # Network events: resolve the host once.
    if event == "socket.getaddrinfo":
        host = args[0] if args else None
        host = host.decode() if isinstance(host, bytes) else (host or "")
        host = str(host)
    else:  # socket.connect
        host = _connect_host(args[1] if len(args) > 1 else None)
        if host is None:  # AF_UNIX / local: never a remote deploy route.
            return
    kind = "name-resolution" if event == "socket.getaddrinfo" else "socket-connect"

    # Layer 1: the known production deploy control plane is denied unconditionally
    # (no context to escape via a fresh context / raw thread / executor).
    if _is_deploy_control_plane(host):
        raise _deny(host, f"deploy-route/{kind}", armed=armed)
    # Layer 2: while an operator handler runs, deny-by-default (closes aliased /
    # env-provided targets on the innocent same-context path).
    if armed and not _host_allowed(host):
        raise _deny(host, kind, armed=armed)


def install_operator_egress_guard() -> None:
    """Install the process audit hook exactly once. Idempotent; audit hooks are
    permanent, so no handler can undo it."""
    global _installed
    if _installed:
        return
    sys.addaudithook(_audit_hook)
    _installed = True


install_operator_egress_guard()


@contextlib.contextmanager
def operator_execution() -> Iterator[None]:
    """Arm Layer 2 for the duration of an operator handler (see require_operator).
    Reentrant. Layer 1 is always active and needs no arming."""
    token = _operator_armed.set(True)
    try:
        yield
    finally:
        _operator_armed.reset(token)


def is_armed() -> bool:
    return _operator_armed.get()


def is_deploy_control_plane(host: str) -> bool:
    """Public: True if the host is a known production deploy route (Layer 1)."""
    return _is_deploy_control_plane(host)


def egress_allowlist_hosts() -> Iterable[str]:
    exact, _ = _allowlist()
    return sorted(exact)
