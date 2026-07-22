"""FastAPI dependencies — org resolution (the tenant boundary at the edge).

TWO behaviors, selected by ``LEAF_AUTH_LIVE`` (read at call time, default off) —
the SAME env toggle the server uses (server/deps.py ``auth_live``):

  OFF (default) -> DEV SEAM: the caller's org is the client-supplied ``X-Org-Id``
      header (documented demo behavior). Byte-identical to the pre-hardening stub.

  ON  (=1)      -> the caller's org is derived from the VERIFIED Auth0 RS256
      session (server/auth.py's RS256-pinned verifier), NOT the header. A
      client-supplied ``X-Org-Id`` is IGNORED, so it can never name another
      tenant's org (F6). No/invalid token -> 401; a verified token that carries
      no org claim -> 403.

Isolating that swap here keeps store.py and api.py org-scoped by construction.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import Header, HTTPException, status

# --------------------------------------------------------------------------- #
# live-auth wiring — reuse server/auth.py's RS256-pinned Auth0 verifier
# --------------------------------------------------------------------------- #
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SERVER_AUTH_FILE = _PROJECT_ROOT / "server" / "auth.py"


def auth_live() -> bool:
    """``LEAF_AUTH_LIVE`` gate (the SAME env the server reads). Read at call time
    so a single process can be toggled in tests and subprocess env overrides
    apply."""
    return os.environ.get("LEAF_AUTH_LIVE", "0") == "1"


def _server_auth():
    """Load server/auth.py (the held-solid RS256 verifier) by explicit file path.

    Loaded lazily via importlib so (a) PyJWT is only required when auth is
    actually live — matching server/deps.py's import discipline — and (b) we
    neither pollute sys.path nor risk re-shadowing the stdlib ``platform`` module
    that platform/tests/conftest.py works hard to defend. Cached in sys.modules.
    """
    mod = sys.modules.get("leaf_server_auth")
    if mod is not None:
        return mod
    spec = importlib.util.spec_from_file_location("leaf_server_auth", _SERVER_AUTH_FILE)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise HTTPException(status_code=500, detail="auth module unavailable")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.modules["leaf_server_auth"] = mod
    return mod


def _verified_identity(authorization: Optional[str]) -> Any:
    """Derive the canonical org UUID from a verified subject binding.

    401 on absent/invalid token (server/auth.py). A verified token whose
    namespaced ``org_id`` claim is absent or is not a UUID -> 403 (authenticated
    but not provisioned for a Leaf org) — mirrors the server's verified-but-no-
    tenant-claim posture.
    """
    auth = _server_auth()
    payload = auth.verify_platform_token(authorization)   # -> 401 on bad/absent token
    subject = payload.get("sub")
    if not subject:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="token verified but carries no external subject",
        )
    from .store import resolve_active_identity_binding
    binding = resolve_active_identity_binding("auth0", str(subject))
    if binding is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="verified subject has no active platform identity binding",
        )
    return binding


def _verified_org(authorization: Optional[str]) -> uuid.UUID:
    return _verified_identity(authorization).platform_tenant_id


def get_org_id(
    x_org_id: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> uuid.UUID:
    """Resolve the caller's org.

    OFF (auth off): the ``X-Org-Id`` header (dev seam). 400 if absent/malformed.
    ON  (auth live): a verified session's active identity binding. Header ignored.
    """
    if not auth_live():
        # DEV SEAM — client-supplied X-Org-Id (documented demo behavior).
        if not x_org_id:
            raise HTTPException(status_code=400, detail="missing X-Org-Id")
        try:
            return uuid.UUID(x_org_id)
        except (ValueError, AttributeError):
            raise HTTPException(status_code=400, detail="invalid X-Org-Id")
    # LIVE AUTH — verified session wins; a client-supplied X-Org-Id is NOT trusted.
    return _verified_org(authorization)


def get_write_org_id(
    x_org_id: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> uuid.UUID:
    """Resolve the org for a mutation and enforce the server-owned live role.

    The auth-off demo seam deliberately remains header-scoped and permissive.
    In live mode, the role is read from the verified subject's tenant-scoped
    binding; no client claim, header, or request body can select it.
    """
    if not auth_live():
        return get_org_id(x_org_id=x_org_id, authorization=authorization)
    binding = _verified_identity(authorization)
    from .store import active_identity_role
    role = active_identity_role(binding.platform_tenant_id, binding.binding_id)
    if role not in {"owner", "editor"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="platform role does not permit mutation")
    return binding.platform_tenant_id


def get_write_binding_id(
    x_actor_binding_id: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> uuid.UUID:
    """Resolve the audited actor binding for a project mutation.

    Live mode ignores the client header and derives the binding from the
    verified subject. The auth-off development seam requires an explicit UUID,
    which the store still validates against the target organization and role.
    """
    if not auth_live():
        if not x_actor_binding_id:
            raise HTTPException(status_code=400, detail="missing X-Actor-Binding-Id")
        try:
            return uuid.UUID(x_actor_binding_id)
        except (ValueError, AttributeError):
            raise HTTPException(status_code=400, detail="invalid X-Actor-Binding-Id")
    binding = _verified_identity(authorization)
    from .store import active_identity_role
    role = active_identity_role(binding.platform_tenant_id, binding.binding_id)
    if role not in {"owner", "editor"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="platform role does not permit mutation")
    return binding.binding_id


def get_review_binding_id(
    x_actor_binding_id: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> uuid.UUID:
    """Resolve a professional-review actor; live mode permits owner/reviewer only."""
    if not auth_live():
        if not x_actor_binding_id:
            raise HTTPException(status_code=400, detail="missing X-Actor-Binding-Id")
        try:
            return uuid.UUID(x_actor_binding_id)
        except (ValueError, AttributeError):
            raise HTTPException(status_code=400, detail="invalid X-Actor-Binding-Id")
    binding = _verified_identity(authorization)
    from .store import active_identity_role
    role = active_identity_role(binding.platform_tenant_id, binding.binding_id)
    if role not in {"owner", "reviewer"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="platform role does not permit professional review")
    return binding.binding_id


def require_auth_when_live(
    authorization: str | None = Header(default=None),
) -> Optional[Any]:
    """Gate for the otherwise-open bootstrap endpoint ``POST /api/orgs`` (F6).

    OFF: no-op — the demo keeps its open org-bootstrap endpoint (byte-identical),
         which is required for the chicken/egg first-org mint.
    ON:  the caller MUST present a VERIFIED Auth0 session (401 otherwise); org
         creation becomes a side effect of a real authenticated identity and a
         client-supplied identity is never trusted. The org claim need NOT yet
         exist (provisioning the first org is exactly this call), so only the
         signature/audience/issuer/expiry are required here.
    """
    if not auth_live():
        return None
    return _server_auth().verify_platform_token(authorization)  # -> 401 on bad/absent token
