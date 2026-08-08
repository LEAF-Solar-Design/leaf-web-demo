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
    "169.254.169.254",                # cloud metadata (IMDS)
    "metadata.google.internal", "169.254.170.2",  # GCE / ECS task metadata
})
_DEPLOY_HOST_PATTERNS = (
    re.compile(r"(^|\.)vercel\.(com|app)$", re.I),
    re.compile(r"^ecs\.[a-z0-9-]+\.amazonaws\.com$", re.I),          # AWS ECS
    re.compile(r"^elasticbeanstalk\.[a-z0-9-]+\.amazonaws\.com$", re.I),
)
# Deploy CLIs: a spawn of one is a production deploy attempt, denied process-wide
# (the tenant never spawns these; its toolchain is unrelated).
_DEPLOY_CLIS = frozenset({
    "vercel", "vercel.cmd", "aws", "aws.cmd", "gcloud", "kubectl",
    "eb", "flyctl", "netlify", "wrangler",
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


def _cli_basename(target: str) -> str:
    # The audit arg may be a full command string ("vercel promote ...", as
    # Windows joins argv) or a single executable/path. Take the first
    # whitespace-delimited token, then its basename.
    s = (target or "").strip().strip('"')
    tokens = s.split()
    first = tokens[0] if tokens else ""
    return os.path.basename(first.lower())


def _audit_hook(event: str, args: tuple) -> None:
    if event not in _WATCHED:
        return
    armed = _operator_armed.get()

    if event in _SPAWN_EVENTS:
        # The audit args differ per event (subprocess.Popen is
        # (executable, argv, cwd, env); os.system is (command,); os.exec is
        # (path, argv, env)). Collect every candidate executable string: the
        # executable/path AND the first element of any argv sequence, so a
        # `Popen(["vercel", ...])` (executable=None) is still seen.
        candidates: list[str] = []

        def _add(v: object) -> None:
            if isinstance(v, bytes):
                candidates.append(v.decode(errors="ignore"))
            elif isinstance(v, str):
                candidates.append(v)
            elif isinstance(v, (list, tuple)) and v:
                _add(v[0])

        for a in args:
            _add(a)
        target = next((c for c in candidates if c), "") or event
        # Layer 1: a deploy-CLI spawn is denied for the whole process, always.
        if any(_cli_basename(c) in _DEPLOY_CLIS for c in candidates):
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
    # env-provided targets on the innocent same-context path).
    if armed and not _host_allowed(host):
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
