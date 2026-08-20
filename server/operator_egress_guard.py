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
import logging
import os
import re
import sys
import time
import urllib.parse
from typing import Iterable, Iterator, NoReturn

_logger = logging.getLogger(__name__)

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


# --- Observability: a firing denial is a SECURITY-RELEVANT event and, until
#     now, produced no log line, no metric, no event -- invisible in
#     production. `_deny` is the single choke point every raise site below
#     goes through, so no future call site can add a silent denial. NEVER
#     the raw target/host: an aliased or env-composed target can carry a
#     secret (contract/OPERATOR.md section 7), so only the bounded
#     kind/host_class/caller_surface classification travels. ---------------

# host_class: an ALLOWLISTED CLASS, never the raw host.
_HOST_CLASS_PROCESS = "process"                    # a spawn: no host at all
_HOST_CLASS_DEPLOY_CONTROL_PLANE = "deploy_control_plane"  # Layer 1 known route
_HOST_CLASS_UNALLOWLISTED = "unallowlisted"        # Layer 2 host not on the allowlist

# caller_surface: which layer/context produced the denial.
_CALLER_SURFACE_PROCESS_WIDE = "process_wide"      # Layer 1, unconditional
_CALLER_SURFACE_OPERATOR_CONTEXT = "operator_context"  # Layer 2, while armed

_EVENT_EGRESS_DENIED = "security.egress_denied"


def _emit_egress_denied_metric(kind: str) -> None:
    """EMF metric emission for EgressDenied (Count, dimension {kind}) in
    Leaf/Platform/APS, via the SAME pattern as emf_metrics.py's own _emit:
    one JSON line to stderr with an `_aws`/`CloudWatchMetrics` envelope; the
    ECS awslogs driver ships it and CloudWatch extracts the metric. Kept
    local (not added to emf_metrics.py) because this card's file boundary is
    frozen to this module and its test. Best-effort, NEVER raises."""
    if os.environ.get("APS_EMF_DISABLED", "") == "1":
        return
    try:
        namespace = "Leaf/Platform/APS"
        try:
            import emf_metrics  # first-party sibling module; NAMESPACE mirror

            namespace = emf_metrics.NAMESPACE
        except Exception:  # noqa: BLE001 - fall back to the literal namespace
            pass
        doc = {
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [{
                    "Namespace": namespace,
                    "Dimensions": [["kind"]],
                    "Metrics": [{"Name": "EgressDenied", "Unit": "Count"}],
                }],
            },
            "kind": kind,
            "EgressDenied": 1,
        }
        sys.stderr.write(json.dumps(doc, separators=(",", ":"), allow_nan=False) + "\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 - metrics must never break the deny
        pass


def _emit_egress_denied_event(kind: str, host_class: str, caller_surface: str) -> None:
    """Product event security.egress_denied via telemetry_sink.emit(), the
    existing product-event door (docs/PLATFORM_TELEMETRY.md). This fires
    outside any tenant/session context (a process-wide security control, not
    a tenant handler), so identity is stamped "anon", the existing
    convention for events with no resolved principal (routers/telemetry.py).
    Best-effort, NEVER raises."""
    try:
        import telemetry_sink

        telemetry_sink.emit(
            _EVENT_EGRESS_DENIED,
            tenant_id="anon",
            tenant_kind="anon",
            session_id="server",
            labels={
                "kind": kind,
                "host_class": host_class,
                "caller_surface": caller_surface,
            },
        )
    except Exception:  # noqa: BLE001 - telemetry must never break the deny
        pass


def _emit_egress_denied_log(kind: str, host_class: str, caller_surface: str) -> None:
    """Structured log line at deny. Best-effort, NEVER raises."""
    try:
        _logger.warning(json.dumps({
            "event": _EVENT_EGRESS_DENIED,
            "kind": kind,
            "host_class": host_class,
            "caller_surface": caller_surface,
        }, separators=(",", ":")))
    except Exception:  # noqa: BLE001 - logging must never break the deny
        pass


def _deny(target: str, kind: str, host_class: str, caller_surface: str) -> NoReturn:
    """The single raise site for OperatorEgressDenied: emit log + event +
    metric, THEN raise. A firing denial must never be silent again."""
    _emit_egress_denied_log(kind, host_class, caller_surface)
    _emit_egress_denied_event(kind, host_class, caller_surface)
    _emit_egress_denied_metric(kind)
    raise OperatorEgressDenied(target, kind)


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
    for var in ("DATABASE_URL", "LEAF_PLATFORM_DATABASE_URL", "PLATFORM_DATABASE_URL"):
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
            _deny(target, "deploy-cli-spawn", _HOST_CLASS_PROCESS,
                  _CALLER_SURFACE_PROCESS_WIDE)
        # Layer 2: an operator handler spawns NO process at all.
        if armed:
            _deny(target, "process-spawn", _HOST_CLASS_PROCESS,
                  _CALLER_SURFACE_OPERATOR_CONTEXT)
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
        _deny(host, f"deploy-route/{kind}", _HOST_CLASS_DEPLOY_CONTROL_PLANE,
              _CALLER_SURFACE_PROCESS_WIDE)
    # Layer 2: while an operator handler runs, deny-by-default (closes aliased /
    # env-provided targets on the innocent same-context path).
    if armed and not _host_allowed(host):
        _deny(host, kind, _HOST_CLASS_UNALLOWLISTED,
              _CALLER_SURFACE_OPERATOR_CONTEXT)


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
