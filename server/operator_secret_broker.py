"""Operator secret broker (contract/OPERATOR.md Wave 3 / W0.2 decision 4).

The model and the operator surface receive a HANDLE and METADATA, never a
secret value. The broker resolves a short-lived credential on demand through a
deployment-provided MINTER and injects it into EXACTLY ONE adapter call; the
value never crosses back to the caller. Fail-closed by construction:

- Handles are declared in a registry (server/operator_secrets.json, env
  override LEAF_OPERATOR_SECRETS_FILE) that holds ONLY metadata: scope,
  environment, kind, TTL. It NEVER holds a secret value. A missing/unreadable
  file yields an empty registry (no handle resolves).
- Only NON-PRODUCTION scopes may be injected. A handle whose environment is
  production, or an injection requested against a production environment, is
  refused.
- No minter configured => nothing can be injected (dark by default). The real
  minter (cloud IAM / a scoped-token service) is a deployment concern and is
  registered out of band; until then the broker only serves metadata.
- Every injection writes an operator_security_audit row (handle, scope,
  decision) with NO secret value.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from operator_principals import _db

_NON_PRODUCTION = {"staging", "development"}
_ALLOWED_META = {"scope", "environment", "kind", "ttl_s"}

# Deployment-registered minter: (handle_meta) -> short-lived credential string.
# Left None so the broker is dark until a real minter is registered.
_MINTER: Optional[Callable[[Dict[str, Any]], str]] = None


class SecretBrokerError(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def register_minter(minter: Optional[Callable[[Dict[str, Any]], str]]) -> None:
    """Register (or clear) the deployment credential minter. Out-of-band."""
    global _MINTER
    _MINTER = minter


def _registry_path() -> Path:
    return Path(os.environ.get(
        "LEAF_OPERATOR_SECRETS_FILE",
        str(Path(__file__).resolve().parent / "operator_secrets.json")))


def _load_registry() -> Dict[str, Dict[str, Any]]:
    try:
        raw = _registry_path().read_text(encoding="utf-8")
    except OSError:
        return {}
    data = json.loads(raw)
    handles = data.get("handles", {})
    if not isinstance(handles, dict):
        raise SecretBrokerError("secrets registry: handles must be a mapping")
    for handle, meta in handles.items():
        if not isinstance(meta, dict):
            raise SecretBrokerError(f"secrets registry: {handle} must be a mapping")
        if set(meta) != _ALLOWED_META:
            # Exact field set: no unknown fields, and none of scope /
            # environment / kind / ttl_s may be omitted.
            raise SecretBrokerError(
                f"secrets registry: {handle} fields must be exactly "
                f"{sorted(_ALLOWED_META)}, got {sorted(meta)}")
        # Every metadata field must be a plain string / bounded int. This
        # blocks a nested value from hiding inside e.g. scope: {"token": ...}.
        for key in ("scope", "environment", "kind"):
            if not isinstance(meta[key], str) or not meta[key]:
                raise SecretBrokerError(
                    f"secrets registry: {handle}.{key} must be a non-empty "
                    "string (no nested objects, which could smuggle a value)")
        if (not isinstance(meta["ttl_s"], int)
                or isinstance(meta["ttl_s"], bool)
                or not 1 <= meta["ttl_s"] <= 86400):
            raise SecretBrokerError(
                f"secrets registry: {handle}.ttl_s must be an int in [1, 86400]")
        if meta.get("environment") not in _NON_PRODUCTION:
            raise SecretBrokerError(
                f"secrets registry: {handle} environment must be non-production")
    return handles


def _safe_scrub(obj: Any, secret: str) -> Any:
    """Return a credential-free copy of an adapter result, or raise
    SecretBrokerError('adapter_result_not_serializable') if the result is not
    a plain JSON structure. Forcing a JSON round-trip is deliberately strict:
    it scrubs the credential from EVERY string (dict keys AND values) and
    REFUSES any type that could carry the credential past a type-by-type scrub
    (sets, bytes, generators, custom objects). Both the raw credential and its
    JSON-escaped form are redacted."""
    try:
        serialized = json.dumps(obj)
    except (TypeError, ValueError):
        raise SecretBrokerError("adapter_result_not_serializable")
    escaped = json.dumps(secret)[1:-1]  # the credential as it appears in JSON
    for token in {secret, escaped}:
        if token:
            serialized = serialized.replace(token, "***REDACTED***")
    return json.loads(serialized)


def describe(handle: str) -> Optional[Dict[str, Any]]:
    """Handle metadata (scope, environment, kind, ttl_s) — NEVER a value.
    None if the handle is unknown."""
    meta = _load_registry().get(handle)
    if meta is None:
        return None
    return {"handle": handle, "scope": meta.get("scope"),
            "environment": meta.get("environment"), "kind": meta.get("kind"),
            "ttl_s": meta.get("ttl_s")}


def list_handles() -> Dict[str, Dict[str, Any]]:
    """All handle metadata (no values)."""
    return {h: describe(h) for h in _load_registry()}


def _audit_inject(subject: Optional[str], handle: str, scope: Optional[str],
                  decision: str, reason: str) -> None:
    try:
        db = _db()
        with db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO operator_security_audit (subject, action,"
                " decision, reason, environment)"
                " VALUES (%s, %s, %s, %s, %s)",
                (subject or "operator", "operator.secret_inject", decision,
                 f"{reason}:{handle}:{scope or '?'}", "n/a"))
    except Exception:  # noqa: BLE001 - audit is best-effort
        pass


def with_injected(handle: str, environment: str,
                  use: Callable[[str], Any], *,
                  subject: Optional[str] = None) -> Any:
    """Resolve a short-lived credential for `handle` and pass it to `use` for
    EXACTLY ONE call. The credential is never returned to the caller and never
    logged. Fail-closed: unknown handle, production scope/environment, or no
    minter -> SecretBrokerError (audited as a denial)."""
    meta = _load_registry().get(handle)
    if meta is None:
        _audit_inject(subject, handle, None, "deny", "unknown_handle")
        raise SecretBrokerError("unknown_handle")
    scope = meta.get("scope")
    if meta.get("environment") not in _NON_PRODUCTION or environment not in _NON_PRODUCTION:
        _audit_inject(subject, handle, scope, "deny", "production_scope_refused")
        raise SecretBrokerError("production_scope_refused")
    if meta.get("environment") != environment:
        _audit_inject(subject, handle, scope, "deny", "environment_mismatch")
        raise SecretBrokerError("environment_mismatch")
    if _MINTER is None:
        _audit_inject(subject, handle, scope, "deny", "no_minter")
        raise SecretBrokerError("no_minter")

    # Mint. On any minter error, fail with a fixed value-free reason. The
    # raise happens OUTSIDE the except block so the credential-bearing original
    # exception is never retained in __context__.
    credential = None
    minter_failed = False
    try:
        credential = _MINTER(dict(meta, handle=handle))
    except Exception:  # noqa: BLE001
        minter_failed = True
    if minter_failed:
        _audit_inject(subject, handle, scope, "deny", "minter_failed")
        raise SecretBrokerError("minter_failed")

    # Run the adapter for EXACTLY ONE call. Any exception (including a
    # SecretBrokerError raised by the adapter with the credential in it) is
    # treated uniformly as adapter_failed with no detail; raised outside the
    # except so nothing chains the original.
    raw = None
    adapter_failed = False
    try:
        raw = use(credential)
    except Exception:  # noqa: BLE001
        adapter_failed = True
    if adapter_failed:
        credential = None
        _audit_inject(subject, handle, scope, "deny", "adapter_failed")
        raise SecretBrokerError("adapter_failed")

    # Redact the credential from the (JSON-only) result before it leaves.
    result_error: Optional[str] = None
    result: Any = None
    try:
        result = _safe_scrub(raw, credential)
    except SecretBrokerError as exc:
        result_error = exc.reason
    credential = None  # drop the reference
    if result_error is not None:
        _audit_inject(subject, handle, scope, "deny", result_error)
        raise SecretBrokerError(result_error)
    _audit_inject(subject, handle, scope, "inject", "credential_injected")
    return result
