"""
The platform's OWN client for the fleet gateway (standardization slice 11a).

GET /api/builds reads the fleet lane through this module with the platform's
credential, never the browser's: the browser holds a tenant session, and a
tenant session must not become a fleet credential by way of a proxy. The
credential is read from the environment INSIDE ``_token()`` at call time and
put on one header; it is never logged, never returned, never placed in a URL.

Configuration (both env names, no values, ever, in a log or a body):
  LEAF_FLEET_GATEWAY_URL     the gateway's base URL; unset = the lane is
                             unconfigured and reads as []
  LEAF_FLEET_GATEWAY_TOKEN   the bearer the gateway issued to this platform

Wire shape this client expects from ``GET {url}/tasks?tenant=<id>&limit=<n>``:
  { "tasks": [ { task_id, title, owner, state, state_since, last_evidence_at,
                 detail, terminal_state, created_at, requested_by?, receipts? } ] }
which is the collector's ``task_state`` row joined to ``tasks`` (the shape
slice 11c teaches the collector to serve). Rows are handed to
``build_queue.from_fleet_task`` untouched; this module only bounds them.

HARDENING CONTRACT. One request, one bounded TOTAL deadline (FLEET_TIMEOUT_S,
wall clock across connect AND read, not just each socket operation: the
socket timeout alone bounds a single recv(), so a gateway trickling a few
bytes per read could otherwise hold the calling thread far past the nominal
timeout), one bounded body (MAX_BODY_BYTES, read in chunks against that same
deadline), a closed set of accepted shapes. Every failure (unset URL, DNS,
refused, timeout, non-200, oversized, non-JSON, wrong shape, a malformed HTTP
response) raises FleetGatewayUnavailable with a SHORT reason the route turns
into one warning string; the reason never carries the token or the response
body.
"""
from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

URL_ENV = "LEAF_FLEET_GATEWAY_URL"
TOKEN_ENV = "LEAF_FLEET_GATEWAY_TOKEN"
FLEET_TIMEOUT_S = 3.0
MAX_BODY_BYTES = 256 * 1024
MAX_ROWS = 200
_READ_CHUNK_BYTES = 64 * 1024


class FleetGatewayUnavailable(Exception):
    """The fleet lane could not be read. ``str(exc)`` is safe to surface."""


def configured() -> bool:
    return bool(os.environ.get(URL_ENV, "").strip())


def _base_url() -> str:
    raw = os.environ.get(URL_ENV, "").strip().rstrip("/")
    if not raw:
        raise FleetGatewayUnavailable("gateway not configured")
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise FleetGatewayUnavailable("gateway url is not http(s)")
    return raw


def _token() -> Optional[str]:
    token = os.environ.get(TOKEN_ENV, "")
    token = token.strip()
    return token or None


def _set_remaining_socket_timeout(stream: Any, remaining: float) -> None:
    """Tighten urllib's socket timeout to the call's remaining budget.

    Test streams and some alternate handlers do not expose a socket. The
    deadline checks in ``_read_bounded`` still apply to those streams.
    """
    fp = getattr(stream, "fp", None)
    raw = getattr(fp, "raw", None)
    sock = getattr(raw, "_sock", None)
    if sock is None or not hasattr(sock, "settimeout"):
        return
    try:
        sock.settimeout(max(0.001, remaining))
    except (OSError, ValueError):
        # A closed or handler-owned socket will fail on read and the caller
        # converts that failure into FleetGatewayUnavailable.
        pass


def _read_bounded(stream: Any, cap: int, deadline: float) -> bytes:
    """Read no more than ``cap`` bytes inside one monotonic deadline."""
    chunks: List[bytes] = []
    remaining_bytes = cap
    while remaining_bytes > 0:
        remaining_time = deadline - time.monotonic()
        if remaining_time <= 0:
            raise TimeoutError("gateway read deadline exceeded")
        _set_remaining_socket_timeout(stream, remaining_time)
        chunk = stream.read(min(_READ_CHUNK_BYTES, remaining_bytes))
        if not chunk:
            break
        if not isinstance(chunk, (bytes, bytearray)):
            raise ValueError("gateway body is not bytes")
        payload = bytes(chunk)
        chunks.append(payload)
        remaining_bytes -= len(payload)
    return b"".join(chunks)


def list_tasks(tenant_id: str, limit: int, *,
               opener: Optional[Callable[..., Any]] = None) -> List[Dict[str, Any]]:
    """The tenant's fleet rows, bounded. Raises FleetGatewayUnavailable."""
    base = _base_url()
    bounded = max(1, min(int(limit), MAX_ROWS))
    query = urllib.parse.urlencode({"tenant": str(tenant_id), "limit": bounded})
    request = urllib.request.Request(f"{base}/tasks?{query}", method="GET")
    request.add_header("Accept", "application/json")
    token = _token()
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    open_fn = opener or urllib.request.urlopen
    # A TOTAL deadline, not just the per-socket-operation timeout urlopen sets:
    # a gateway that trickles the response could otherwise keep each recv()
    # inside FLEET_TIMEOUT_S while the call as a whole runs unbounded.
    deadline = time.monotonic() + FLEET_TIMEOUT_S
    try:
        with open_fn(request, timeout=FLEET_TIMEOUT_S) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                raise FleetGatewayUnavailable(f"gateway answered {int(status)}")
            raw = _read_bounded(response, MAX_BODY_BYTES + 1, deadline)
    except FleetGatewayUnavailable:
        raise
    except urllib.error.HTTPError as exc:
        raise FleetGatewayUnavailable(f"gateway answered {int(exc.code)}") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, http.client.HTTPException) as exc:
        # http.client.HTTPException (IncompleteRead / BadStatusLine /
        # LineTooLong, raised inside response.read()/getresponse()) is NOT an
        # OSError, so it must be listed explicitly: without it, a truncated
        # or malformed HTTP response escapes this function uncaught and 500s
        # the whole /api/builds endpoint, discarding the broker and fold
        # records already computed alongside it.
        raise FleetGatewayUnavailable(f"gateway unreachable ({type(exc).__name__})") from None
    if len(raw) > MAX_BODY_BYTES:
        raise FleetGatewayUnavailable("gateway body over bound")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise FleetGatewayUnavailable("gateway body is not JSON") from None
    if not isinstance(body, dict) or not isinstance(body.get("tasks"), list):
        raise FleetGatewayUnavailable("gateway body has no tasks list")
    rows = [row for row in body["tasks"] if isinstance(row, dict)]
    return rows[:bounded]
