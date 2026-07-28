"""App-side session preparation for the warm instant executor pool.

The app performs this work when a Claude session opens. It selects only a
catalog-declared, trusted read-only artifact, reads that artifact from an
allowlisted local registry root, and asks the separate control plane to assign
and load a warm executor. The returned assignment is cached only for the
authenticated app-to-harness back-edge. It is never returned to the browser.

This module is not in the invocation hot path. The harness calls the assigned
executor directly. The existing POST /api/run path remains the batch path.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

import requests

import deps
import customization_service
from instant_artifact_registry import (
    ArtifactResolutionError,
    FilesystemTrustedPlatformArtifactRegistry,
    InstantArtifact,
)


CONTRACT = "leaf.instant-execution/v1"
SERVER_DIR = Path(__file__).resolve().parent
_DIGEST = "sha256:"
ARTIFACT_REGISTRY = FilesystemTrustedPlatformArtifactRegistry(
    SERVER_DIR / "instant_tools",
)
_lock = threading.Lock()
_assignments: "OrderedDict[Tuple[str, str], Dict[str, Any]]" = OrderedDict()
_session_locks: "OrderedDict[Tuple[str, str], threading.RLock]" = OrderedDict()
_DRAWING_FIELDS = (
    "dwg", "layers", "polylines", "inserts", "faces3d", "blockdefs",
    "geodata", "images", "imageNames",
)
_MAX_CONTEXT_BYTES = 8 * 1024 * 1024
_MAX_ASSIGNMENTS = 256
_MAX_SESSION_LOCKS = 512
_now = time.time


def _control_url() -> str:
    return os.environ.get("LEAF_INSTANT_CONTROL_URL", "").strip().rstrip("/")


def _control_timeout_s() -> float:
    try:
        return max(0.1, min(float(os.environ.get("LEAF_INSTANT_CONTROL_TIMEOUT_S", "10")), 60.0))
    except (TypeError, ValueError):
        return 10.0


def _control_headers() -> Dict[str, str]:
    headers = {"content-type": "application/json"}
    secret = os.environ.get("LEAF_INSTANT_CONTROL_SECRET", "").strip()
    filename = os.environ.get("LEAF_INSTANT_CONTROL_SECRET_FILE", "").strip()
    if secret and filename:
        return headers
    if filename:
        try:
            path = Path(filename)
            secret = path.read_text(encoding="utf-8").strip() if path.is_absolute() else ""
        except OSError:
            secret = ""
    if secret:
        headers["x-instant-control-secret"] = secret
    return headers


def _control_transport(url: str) -> Dict[str, Any]:
    """Return fail-closed requests mTLS options for the private control API."""
    parsed = urlparse(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
        raise ValueError("instant control URL is invalid")
    if parsed.scheme == "http":
        try:
            loopback = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise ValueError("plain HTTP instant control URL must be loopback")
        return {}
    if parsed.scheme != "https":
        raise ValueError("instant control URL must use HTTPS")
    names = (
        "LEAF_INSTANT_CONTROL_CA_FILE",
        "LEAF_INSTANT_CONTROL_CLIENT_CERT_FILE",
        "LEAF_INSTANT_CONTROL_CLIENT_KEY_FILE",
    )
    files = [os.environ.get(name, "").strip() for name in names]
    if not all(files) or not all(Path(filename).is_file() for filename in files):
        raise ValueError("instant control HTTPS requires CA, client certificate, and client key files")
    return {"verify": files[0], "cert": (files[1], files[2])}


def _sha256(value: bytes) -> str:
    return _DIGEST + hashlib.sha256(value).hexdigest()


def _sanitized_drawing_context(drawing_id: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return a bounded demo intake and its immutable invocation reference.

    The general versioned-drawing adapter lands after the local trusted-tool
    proof. Until then, any non-demo drawing fails closed for instant execution
    and remains available through the existing batch path.
    """
    if drawing_id not in ("rooftop_demo", "rooftop-demo"):
        raise ValueError("instant drawing context is not available for this drawing")
    source = deps.load_cached_intake()
    sanitized = {key: source[key] for key in _DRAWING_FIELDS if key in source}
    encoded = json.dumps(
        sanitized, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > _MAX_CONTEXT_BYTES:
        raise ValueError("instant drawing context exceeds the assignment limit")
    digest = _sha256(encoded)
    reference = {
        "drawing_id": drawing_id,
        "version_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"leaf:{drawing_id}:{digest}")),
        "content_digest": digest,
        "geometry_ref": f"drawing-context:{digest.removeprefix(_DIGEST)}",
    }
    return sanitized, reference


def _eligible_tool() -> Optional[Dict[str, Any]]:
    """Return only a checked-in trusted-platform catalog entry.

    Tenant repositories and authored tools can appear in ``deps.all_tools``.
    The instant tier does not consult that merge because it has no hostile-code
    isolation boundary. Those tools remain available only through batch.
    """
    for tool in deps.load_seed_catalog_tools():
        if tool.get("execution_class") != "instant":
            continue
        if tool.get("capabilities") != ["drawing.read"]:
            continue
        if tool.get("runtime") != "python-3.12":
            continue
        return tool
    return None


def _expires_at(value: Dict[str, Any]) -> Optional[float]:
    raw = value.get("expires_at")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _assignment_valid(
    value: Any, tenant_id: str, session_id: str, expected: Dict[str, Any],
) -> bool:
    if not isinstance(value, dict):
        return False
    required_strings = (
        "assignment_id", "executor_id", "executor_endpoint", "lease_id",
        "lease_token", "effective_catalog_digest", "code_digest",
        "artifact_digest", "issued_at", "expires_at",
    )
    return (
        value.get("contract") == CONTRACT
        and value.get("tenant_id") == tenant_id
        and value.get("session_id") == session_id
        and value.get("execution_class") == "instant"
        and isinstance(value.get("binding_epoch"), int)
        and value["binding_epoch"] > 0
        and all(isinstance(value.get(key), str) and value[key] for key in required_strings)
        and value.get("effective_catalog_digest") == expected.get("effective_catalog_digest")
        and value.get("code_digest") == expected.get("artifact", {}).get("code_digest")
        and value.get("artifact_digest") == expected.get("artifact", {}).get("artifact_digest")
        and value.get("drawing_context") == expected.get("drawing_context", {}).get("reference")
        and (_expires_at(value) or 0) > _now()
    )


def _cache_limit() -> int:
    """Return a conservative, bounded process-cache limit.

    The cache is only an app-to-harness convenience. Durable control-plane
    state remains authoritative, so an evicted value never grants a new lease.
    """
    try:
        return max(1, min(int(os.environ.get("LEAF_INSTANT_CACHE_MAX", _MAX_ASSIGNMENTS)), 4096))
    except (TypeError, ValueError):
        return _MAX_ASSIGNMENTS


def _remember_assignment(key: Tuple[str, str], assignment: Dict[str, Any]) -> None:
    with _lock:
        _assignments[key] = dict(assignment)
        _assignments.move_to_end(key)
        while len(_assignments) > _cache_limit():
            _assignments.popitem(last=False)


def _session_lock(key: Tuple[str, str]) -> threading.RLock:
    with _lock:
        lock = _session_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _session_locks[key] = lock
        _session_locks.move_to_end(key)
        # Do not evict a lock held by another caller. A temporary overflow is
        # safer than splitting one session into two concurrent prepare calls.
        while len(_session_locks) > _MAX_SESSION_LOCKS:
            oldest_key, oldest_lock = next(iter(_session_locks.items()))
            if oldest_lock.acquire(blocking=False):
                oldest_lock.release()
                _session_locks.pop(oldest_key)
            else:
                break
        return lock


def _request_body(
    tenant_id: str, session_id: str, drawing_id: str, artifact: InstantArtifact,
    effective_catalog_digest: str,
) -> Dict[str, Any]:
    drawing_data, drawing_reference = _sanitized_drawing_context(drawing_id)
    return {
        "contract": CONTRACT,
        "tenant_id": tenant_id,
        "session_id": session_id,
        "effective_catalog_digest": effective_catalog_digest,
        "artifact": {
            "tool_id": artifact.tool_id,
            "tool_version": artifact.tool_version,
            "capability_id": artifact.capability_id,
            "runtime": artifact.runtime,
            "entrypoint": artifact.entrypoint,
            "limits": artifact.limits_for_transport(),
            "params_schema_digest": artifact.params_schema_digest,
            "catalog_commit": artifact.catalog_commit,
            "code_digest": artifact.code_digest,
            "artifact_digest": artifact.artifact_digest,
            # Trusted registry bytes are sent to the separate control plane in
            # this staging tier. This is transport, never caller input.
            "source": artifact.source.decode("utf-8"),
        },
        "drawing_context": {
            "reference": drawing_reference,
            "data": drawing_data,
        },
    }


def prepare_session(tenant_id: str, session_id: str, drawing_id: str) -> Dict[str, Any]:
    """Single-flight wrapper so one logical session claims at most one slot."""
    key = (str(tenant_id), str(session_id))
    session_lock = _session_lock(key)
    with session_lock:
        if assignment_for_session(*key) is not None:
            return {"ready": True, "reason": None}
        return _prepare_session_uncached(str(tenant_id), str(session_id), drawing_id)


def _prepare_session_uncached(tenant_id: str, session_id: str, drawing_id: str) -> Dict[str, Any]:
    """Assign and preload one trusted instant artifact before the first call.

    Failure is fail closed for instant execution but does not break the existing
    batch-only session. The returned status is safe for a browser response. It
    never includes an endpoint, lease, source, or control-plane detail.
    """
    key = (str(tenant_id), str(session_id))

    url = _control_url()
    if not url:
        return {"ready": False, "reason": "instant_pool_disabled"}
    if "x-instant-control-secret" not in _control_headers():
        return {"ready": False, "reason": "instant_control_auth_missing"}
    tool = _eligible_tool()
    if tool is None:
        return {"ready": False, "reason": "no_eligible_instant_tool"}

    try:
        pin = (
            customization_service.effective_catalog_pin(tenant_id)
            or deps.base_catalog_pin(deps.all_tools(tenant_id))
        )
        catalog_commit = pin["catalog_commit"]
        artifact = ARTIFACT_REGISTRY.resolve(tool, catalog_commit=catalog_commit)
        body = _request_body(
            str(tenant_id), str(session_id), drawing_id, artifact,
            _DIGEST + pin["effective_catalog_digest"],
        )
        response = requests.post(
            f"{url}/v1/sessions", json=body, headers=_control_headers(),
            timeout=(2.0, _control_timeout_s()),
            **_control_transport(url),
        )
        response.raise_for_status()
        assignment = response.json()
    except (ArtifactResolutionError, OSError, UnicodeError, ValueError, requests.RequestException):
        return {"ready": False, "reason": "instant_control_unavailable"}

    if not _assignment_valid(assignment, str(tenant_id), str(session_id), body):
        return {"ready": False, "reason": "invalid_instant_assignment"}
    _remember_assignment(key, assignment)
    return {"ready": True, "reason": None}


def assignment_for_session(tenant_id: str, session_id: str) -> Optional[Dict[str, Any]]:
    """Return a copy for the authenticated app-to-harness request only."""
    key = (str(tenant_id), str(session_id))
    # This also single-flights renewal. A second harness turn must never race a
    # first one into presenting an old lease after a failed renewal.
    with _session_lock(key):
        with _lock:
            cached = _assignments.get(key)
            if cached is None:
                return None
            value = dict(cached)
            _assignments.move_to_end(key)
        expires = _expires_at(value) or 0
        now = _now()
        if expires <= now:
            with _lock:
                _assignments.pop(key, None)
            return None
        issued = _timestamp(value.get("issued_at")) or now
        if expires - now <= max(10.0, (expires - issued) / 2):
            renewed = _renew(value)
            if renewed is not None:
                candidate = dict(value)
                candidate.update(renewed)
                candidate["issued_at"] = datetime.fromtimestamp(
                    _now(), timezone.utc,
                ).isoformat().replace("+00:00", "Z")
                # Never replace an expired value with a renewal response that
                # is already invalid by the time it is checked.
                if (_expires_at(candidate) or 0) <= _now():
                    with _lock:
                        _assignments.pop(key, None)
                    return None
                value = candidate
                _remember_assignment(key, value)
            elif expires <= _now():
                with _lock:
                    _assignments.pop(key, None)
                return None
        return value


def _timestamp(raw: Any) -> Optional[float]:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()
    except ValueError:
        return None


def _renew(assignment: Dict[str, Any]) -> Optional[Dict[str, str]]:
    url = _control_url()
    if not url or "x-instant-control-secret" not in _control_headers():
        return None
    try:
        response = requests.post(
            f"{url}/v1/sessions/{assignment['session_id']}/renew",
            json={"binding_epoch": assignment["binding_epoch"]},
            headers=_control_headers(), timeout=(2.0, _control_timeout_s()),
            **_control_transport(url),
        )
        response.raise_for_status()
        value = response.json()
    except (KeyError, ValueError, requests.RequestException):
        return None
    if not isinstance(value, dict):
        return None
    required = ("lease_id", "lease_token", "expires_at")
    if not all(isinstance(value.get(key), str) and value[key] for key in required):
        return None
    if (_timestamp(value["expires_at"]) or 0) <= _now():
        return None
    return {key: value[key] for key in required}


def clear_assignment(tenant_id: str, session_id: str) -> None:
    with _lock:
        _assignments.pop((str(tenant_id), str(session_id)), None)


def _reset_for_tests() -> None:
    with _lock:
        _assignments.clear()
        _session_locks.clear()
