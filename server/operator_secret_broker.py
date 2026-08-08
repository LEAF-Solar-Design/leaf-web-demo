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

TRUST BOUNDARY. The registry is TRUSTED server configuration (a checked-in
file, or a deployment-set LEAF_OPERATOR_SECRETS_FILE). It is not reachable by
the model, by tenants, or by any HTTP request, and by contract it holds ONLY
metadata, never a secret value. Handle / scope / kind / environment are PUBLIC
metadata BY DESIGN: describe() and the /api/operator/secrets route exist to
surface them. So the broker's runtime guarantee is specifically about the
value the MINTER returns: that minted credential must never appear in a return
value, an exception, or an audit. The broker never surfaces minter output as
metadata, and its receipt is a pure constant, so no minted credential can ride
out on the metadata surface. The identifier charset on registry fields is
defense-in-depth (it refuses obviously value-shaped strings); the registry-is-
trusted contract, not the charset, is what keeps secrets out of metadata.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from operator_principals import _db

_NON_PRODUCTION = {"staging", "development"}
_ALLOWED_META = {"scope", "environment", "kind", "ttl_s"}
# scope / kind are echoed into audits (and were once echoed to callers), so
# they must be bounded IDENTIFIERS, never free text. This makes it structurally
# impossible to smuggle a credential value (mixed case, high entropy, length,
# or special characters) into a metadata field the broker surfaces.
_IDENT_RE = re.compile(r"^[a-z0-9_.:-]{1,64}$")

# Deployment-registered minter: (handle_meta) -> short-lived credential string.
# Left None so the broker is dark until a real minter is registered.
_MINTER: Optional[Callable[[Dict[str, Any]], str]] = None

# Deployment-registered rotator: (handle_meta) -> None. It performs the
# external rotation (invalidate the currently-active credential for the scope
# and provision a fresh one in the backing store) as a side effect. It returns
# nothing the broker surfaces, and the new secret NEVER crosses back. Left None
# so rotation is dark until a real rotator is registered.
_ROTATOR: Optional[Callable[[Dict[str, Any]], None]] = None


class SecretBrokerError(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def register_minter(minter: Optional[Callable[[Dict[str, Any]], str]]) -> None:
    """Register (or clear) the deployment credential minter. Out-of-band."""
    global _MINTER
    _MINTER = minter


def register_rotator(rotator: Optional[Callable[[Dict[str, Any]], None]]) -> None:
    """Register (or clear) the deployment credential rotator. Out-of-band."""
    global _ROTATOR
    _ROTATOR = rotator


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
        # The handle KEY is echoed in the receipt and the audit reason, so it
        # too must be a bounded identifier, never a smuggled value.
        if not isinstance(handle, str) or not _IDENT_RE.match(handle):
            raise SecretBrokerError(
                "secrets registry: handle keys must match "
                "[a-z0-9_.:-]{1,64} so a value cannot be smuggled into a key")
        if not isinstance(meta, dict):
            raise SecretBrokerError(f"secrets registry: {handle} must be a mapping")
        if set(meta) != _ALLOWED_META:
            # Exact field set: no unknown fields, and none of scope /
            # environment / kind / ttl_s may be omitted. Do NOT interpolate the
            # actual field names: an unknown field name is un-validated registry
            # content and this error surfaces (HTTP 409), so echoing it would be
            # a smuggling path. `handle` is already charset-validated above.
            raise SecretBrokerError(
                f"secrets registry: {handle} fields must be exactly "
                f"{sorted(_ALLOWED_META)}")
        # Every metadata field must be a plain string / bounded int. This
        # blocks a nested value from hiding inside e.g. scope: {"token": ...}.
        for key in ("scope", "environment", "kind"):
            if not isinstance(meta[key], str) or not meta[key]:
                raise SecretBrokerError(
                    f"secrets registry: {handle}.{key} must be a non-empty "
                    "string (no nested objects, which could smuggle a value)")
        # scope / kind are surfaced in audits, so they must be bounded
        # identifiers. A credential-shaped string (uppercase, entropy, length,
        # special characters) is rejected at load, not echoed downstream.
        for key in ("scope", "kind"):
            if not _IDENT_RE.match(meta[key]):
                raise SecretBrokerError(
                    f"secrets registry: {handle}.{key} must match "
                    f"[a-z0-9_.:-]{{1,64}} so a credential value cannot be "
                    "smuggled into echoed metadata")
        if (not isinstance(meta["ttl_s"], int)
                or isinstance(meta["ttl_s"], bool)
                or not 1 <= meta["ttl_s"] <= 86400):
            raise SecretBrokerError(
                f"secrets registry: {handle}.ttl_s must be an int in [1, 86400]")
        if meta.get("environment") not in _NON_PRODUCTION:
            raise SecretBrokerError(
                f"secrets registry: {handle} environment must be non-production")
    return handles


# NOTE: there is deliberately NO "scrub the adapter's return value" path.
# Scrubbing arbitrary returns is unwinnable: an adapter can split the
# credential character-by-character (list(cred)), fragment it, or hide it in a
# custom type. Instead, with_injected() returns a FIXED broker-authored
# receipt and IGNORES the adapter's return value entirely, so there is no path
# for the credential to escape through the return. An adapter that needs to
# surface a real result captures it in the caller's own closure (adapters are
# trusted server-side code), never through the broker.


def describe(handle: str) -> Optional[Dict[str, Any]]:
    """Handle metadata (scope, environment, kind, ttl_s), NEVER a value.
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
                  use: Callable[[str], None], *,
                  subject: Optional[str] = None) -> Dict[str, Any]:
    """Resolve a short-lived credential for `handle` and pass it to `use` for
    EXACTLY ONE side-effecting call. `use` returns nothing that the broker
    surfaces: its return value is DISCARDED. The broker returns a FIXED,
    broker-authored receipt (handle, scope, injected=True) that contains no
    adapter-derived data, so the credential has no path to escape through the
    return value (a hostile adapter cannot echo, split, fragment, or encode it
    into the broker's output). If the adapter needs to surface a real result
    (a PR URL, a rotation receipt), it captures that in the CALLER's own
    closure; adapters are trusted server-side code, so keeping the credential
    out of that closure is the adapter's discipline, and the broker's job is
    only to guarantee the credential never leaves through the broker itself.

    Fail-closed: unknown handle, production scope/environment, no minter, a
    minter error, or any adapter exception -> SecretBrokerError with a fixed
    value-free reason (audited as a denial). Every failure raise happens
    OUTSIDE its except block so no credential-bearing exception is retained in
    __context__ or __cause__."""
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
    except BaseException:  # noqa: BLE001 - mask EVERY failure, value-free
        minter_failed = True
    if minter_failed:
        _audit_inject(subject, handle, scope, "deny", "minter_failed")
        raise SecretBrokerError("minter_failed")

    # Run the adapter for EXACTLY ONE call. The return value is IGNORED, so the
    # broker never surfaces adapter output, which closes every scrub-evasion
    # vector at the source. Any exception (including a SecretBrokerError the
    # adapter raises with the credential inside, or a credential-bearing error
    # thrown while serializing a hostile return type) is treated uniformly as
    # adapter_failed with no detail; raised outside the except so nothing
    # chains the original.
    adapter_failed = False
    try:
        use(credential)
    except BaseException:  # noqa: BLE001 - mask EVERY failure, value-free
        adapter_failed = True
    finally:
        credential = None  # drop the reference in all paths
    if adapter_failed:
        _audit_inject(subject, handle, scope, "deny", "adapter_failed")
        raise SecretBrokerError("adapter_failed")

    _audit_inject(subject, handle, scope, "inject", "credential_injected")
    # Receipt is a PURE CONSTANT: it carries nothing derived from the minter,
    # the adapter, or the registry, so it is provably impossible for any minted
    # credential or registry string to ride out on the return value. The caller
    # already knows which handle it called and can describe(handle) for
    # metadata; the receipt only confirms injection happened.
    return {"injected": True}


def rotate(handle: str, environment: str, *,
           subject: Optional[str] = None) -> Dict[str, Any]:
    """Rotate the credential behind `handle`: invalidate the currently-active
    secret for its scope and provision a fresh one in the backing store. The
    new secret is NEVER returned or logged (same isolation as with_injected).

    This is a DB-FREE side-effect primitive: it holds no transaction and writes
    no audit, so the caller (the worker_credential_rotate runbook) can invoke it
    INSIDE its own single transaction and own all auditing. A failure raises a
    fixed value-free SecretBrokerError so the caller's transaction rolls back
    (fail-closed): unknown handle, production scope/environment, no rotator, or
    a rotator error. The raise happens OUTSIDE the except block so no
    credential-bearing exception is retained in __context__/__cause__.

    Dark by default: no rotator registered => `no_rotator`, so nothing rotates
    until a deployment registers one out of band.
    """
    meta = _load_registry().get(handle)
    if meta is None:
        raise SecretBrokerError("unknown_handle")
    if meta.get("environment") not in _NON_PRODUCTION or environment not in _NON_PRODUCTION:
        raise SecretBrokerError("production_scope_refused")
    if meta.get("environment") != environment:
        raise SecretBrokerError("environment_mismatch")
    if _ROTATOR is None:
        raise SecretBrokerError("no_rotator")

    # Perform the external rotation exactly once. The return value is IGNORED
    # (the new secret must not surface), and EVERY failure is masked as a fixed
    # value-free error raised outside the except.
    rotator_failed = False
    try:
        _ROTATOR(dict(meta, handle=handle))
    except BaseException:  # noqa: BLE001 - mask EVERY failure, value-free
        rotator_failed = True
    if rotator_failed:
        raise SecretBrokerError("rotator_failed")

    # Constant receipt: no rotator- or registry-derived data crosses back.
    return {"rotated": True}
