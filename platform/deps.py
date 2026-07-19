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


def _verified_org(authorization: Optional[str]) -> uuid.UUID:
    """Derive the org UUID from a VERIFIED Auth0 session.

    401 on absent/invalid token (server/auth.py). A verified token whose
    namespaced ``org_id`` claim is absent or is not a UUID -> 403 (authenticated
    but not provisioned for a Leaf org) — mirrors the server's verified-but-no-
    tenant-claim posture.
    """
    auth = _server_auth()
    payload = auth.verify_platform_token(authorization)   # -> 401 on bad/absent token
    org_claim = payload.get(auth.claim_ns() + "org_id")
    if not org_claim:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="token verified but carries no org claim; not provisioned for a Leaf org",
        )
    try:
        return uuid.UUID(str(org_claim))
    except (ValueError, AttributeError):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="verified org claim is not a valid org id",
        )


def get_org_id(
    x_org_id: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> uuid.UUID:
    """Resolve the caller's org.

    OFF (auth off): the ``X-Org-Id`` header (dev seam). 400 if absent/malformed.
    ON  (auth live): the VERIFIED session's org claim. The header is ignored.
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
