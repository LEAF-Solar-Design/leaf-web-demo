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


# --- Layer 1: the KNOWN production deploy control plane, denied for the whole
#     process, unconditionally (un-escapable by any context/thread/executor). ---
# EXACT production hosts only. NB: this must NOT catch STAGING, which is
# legitimate operator egress (the plane stages releases). `api.leafdesign.ai` is
# production; `staging-api.leafdesign.ai` is staging and is deliberately absent,
# so an exact/suffix match here never denies a staging host.
_DEPLOY_HOST_EXACT = frozenset({
    "api.leafdesign.ai", "leafdesign.ai", "www.leafdesign.ai",  # production surface
    "api.vercel.com", "vercel.com",   # Vercel deploy API
})

# Cloud metadata / task-credential endpoints. NOT in the process-wide layer:
# on ECS the app's OWN AWS SDK fetches its task-role credentials from
# 169.254.170.2 (AWS_CONTAINER_CREDENTIALS_RELATIVE_URI), so a process-wide
# denial makes the app undeployable (observed live: every /api/health GET at
# 45f0c24c raised OperatorEgressDenied inside boto3's provider chain and the
# blue/green promote timed out). Denying these only under an ARMED operator
# context loses nothing real: code running in this process already holds the
# task credentials through the SDK's in-memory cache, so the endpoint denial
# only ever stopped operator-directed SSRF-style fetches -- which are exactly
# the armed-context paths. Listed here explicitly so arming denies them even
# when an env allowlist (LEAF_OPERATOR_EGRESS_ALLOW, DATABASE_URL host) names
# them: the operator-context denial is ABSOLUTE, not allowlist-overridable.
_METADATA_HOSTS = frozenset({
    "169.254.169.254",                # cloud metadata (IMDS)
    "metadata.google.internal", "169.254.170.2",  # GCE / ECS task metadata
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
            raise OperatorEgressDenied(target, "deploy-cli-spawn")
        # Layer 2: an operator handler spawns NO process at all.
        if armed:
            raise OperatorEgressDenied(target, "process-spawn")
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
        raise OperatorEgressDenied(host, f"deploy-route/{kind}")
    # Layer 2: while an operator handler runs, deny-by-default (closes aliased /
    # env-provided targets on the innocent same-context path). Metadata hosts
    # are denied here ABSOLUTELY -- before and regardless of the allowlist.
    if armed:
        h = (host or "").strip().lower().rstrip(".")
        if h in _METADATA_HOSTS:
            raise OperatorEgressDenied(host, f"metadata-endpoint/{kind}")
        if not _host_allowed(host):
            raise OperatorEgressDenied(host, kind)


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
