"""Operator egress boundary (contract/OPERATOR.md section 7, obligations 2-3).

The obligation is that NO generic operator handler can CALL a production deploy
route. A source scan cannot prove this: a handler can call a neutral helper in
any other file that reaches production (an HTTP POST to the Vercel deploy API, a
boto3 ECS update_service, a `vercel promote` subprocess), naming nothing. So the
boundary is enforced at RUNTIME, at the one chokepoint a neutral helper cannot
route around: the process audit hook over outbound sockets and process spawns.

Mechanism: a single `sys.addaudithook` (audit hooks cannot be removed, so no
handler can uninstall it) that, WHILE an operator handler is executing, denies
  - any outbound socket connection / name resolution to a host that is not on a
    tiny egress allowlist (loopback + the platform database + a deployment-
    declared extra), which is DENY-BY-DEFAULT, so an aliased, string-composed, or
    environment-provided production target is denied like any other; and
  - any subprocess / exec / os.system spawn (the `vercel` / `aws` CLI paths),
    which an operator request handler never legitimately needs.

Scope: the operator plane is mounted into the tenant FastAPI process (app.py),
so the guard must NOT be a process-wide deny (that would break tenant egress).
It is armed per operator request via a contextvar (operator_execution / the
require_operator dependency). Outside operator execution the hook is a no-op, so
the tenant surface is byte-identical.

This is the runtime half of production-unreachability; the other halves are no
production deploy credential in the process (O1) and the sealed non-production
action/route surface (O2 manifest). Egress denial holds even if a credential
somehow leaked: with no route, a handler cannot reach the deploy control plane.
"""
from __future__ import annotations

import contextlib
import contextvars
import os
import sys
import urllib.parse
from typing import Iterable, Iterator

# Armed only while an operator handler runs. Contextvars are the correct scope
# for the async request model (each request/task has its own context; a sync
# handler run in the threadpool inherits it via anyio's context copy), so tenant
# tasks interleaving in the same event-loop thread are NOT armed.
_operator_armed: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "leaf_operator_egress_armed", default=False)


class OperatorEgressDenied(RuntimeError):
    """Raised inside the audit hook when an operator handler attempts egress to a
    non-allowlisted host or tries to spawn a process. Propagates out of the
    audited operation, so the connection / spawn never happens."""

    def __init__(self, target: str, kind: str):
        super().__init__(
            f"operator_egress_denied: {kind} to {target!r} is not permitted "
            f"from the operator plane (production is unreachable)")
        self.target = target
        self.kind = kind


# Loopback is always allowed: it cannot reach a remote production deploy route.
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost", "0.0.0.0", "::"})

# Watched audit events. Everything else returns immediately (cheap).
_NET_EVENTS = frozenset({"socket.connect", "socket.getaddrinfo"})
_SPAWN_EVENTS = frozenset({
    "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn",
})
_WATCHED = _NET_EVENTS | _SPAWN_EVENTS

_installed = False


def _allowlist() -> tuple[frozenset[str], tuple[str, ...]]:
    """(exact hosts, suffixes) permitted as operator egress. Deny-by-default:
    only loopback, the platform database host, and deployment-declared extras.
    Read fresh so a test / deployment can set the env before arming."""
    hosts = set(_LOOPBACK)
    suffixes: list[str] = []
    # The platform database is the operator plane's one legitimate egress.
    for var in ("DATABASE_URL", "LEAF_PLATFORM_DATABASE_URL", "PLATFORM_DATABASE_URL"):
        url = os.environ.get(var, "")
        if url:
            host = urllib.parse.urlsplit(url).hostname
            if host:
                hosts.add(host.lower())
    pghost = os.environ.get("PGHOST", "").strip().lower()
    if pghost:
        hosts.add(pghost)
    # Deployment-declared extras: exact host, or a ".suffix" match.
    for extra in filter(None, (e.strip().lower()
                               for e in os.environ.get(
                                   "LEAF_OPERATOR_EGRESS_ALLOW", "").split(","))):
        if extra.startswith("."):
            suffixes.append(extra)
        else:
            hosts.add(extra)
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
    # AF_INET (host, port) / AF_INET6 (host, port, flow, scope): host is [0].
    if isinstance(address, tuple) and address:
        h = address[0]
        return h.decode() if isinstance(h, bytes) else str(h)
    # AF_UNIX (a path, str/bytes): a local IPC socket cannot reach a remote
    # production route, so it is permitted.
    return None


def _audit_hook(event: str, args: tuple) -> None:
    # Fast path: not a watched event, or no operator handler is running.
    if event not in _WATCHED:
        return
    if not _operator_armed.get():
        return
    if event in _SPAWN_EVENTS:
        # An operator request handler never spawns a process; the deploy CLIs
        # (`vercel`, `aws ecs ...`) are exactly this path.
        target = ""
        if args:
            first = args[0]
            target = first.decode() if isinstance(first, bytes) else str(first)
        raise OperatorEgressDenied(target or event, "process-spawn")
    # Network events.
    if event == "socket.getaddrinfo":
        host = args[0] if args else None
        host = host.decode() if isinstance(host, bytes) else (host or "")
        if not _host_allowed(str(host)):
            raise OperatorEgressDenied(str(host), "name-resolution")
        return
    if event == "socket.connect":
        # args = (socket, address)
        address = args[1] if len(args) > 1 else None
        host = _connect_host(address)
        if host is None:  # AF_UNIX / unrecognised local socket: permitted.
            return
        if not _host_allowed(host):
            raise OperatorEgressDenied(host, "socket-connect")
        return


def install_operator_egress_guard() -> None:
    """Install the process audit hook exactly once. Idempotent; audit hooks are
    permanent, so this cannot be undone by any handler."""
    global _installed
    if _installed:
        return
    sys.addaudithook(_audit_hook)
    _installed = True


# Installed at import so it is present before any operator handler can run.
install_operator_egress_guard()


@contextlib.contextmanager
def operator_execution() -> Iterator[None]:
    """Arm the egress boundary for the duration of an operator handler. Every
    operator request runs inside this (see require_operator). Reentrant."""
    token = _operator_armed.set(True)
    try:
        yield
    finally:
        _operator_armed.reset(token)


def is_armed() -> bool:
    return _operator_armed.get()


def egress_allowlist_hosts() -> Iterable[str]:
    """The exact-host allowlist (for diagnostics / tests)."""
    exact, _ = _allowlist()
    return sorted(exact)
