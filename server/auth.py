"""
server/auth.py — Concern 1: Leaf PLATFORM identity (Auth0 JWT verification).

This module answers ONE question: *who is this tenant*. It verifies an Auth0
RS256 access token (signature / audience / issuer / expiry) and extracts the
namespaced tenant/org/tier claims minted by the Post-Login Action
(auth0-actions/post-login-add-tenant-claim.js).

Near-direct port of aws-ai-manager/app/api/deps.py (the fleet's proven Auth0
verify path), adapted to be:
  - env-configurable (issuer / audience / JWKS / claim namespace),
  - offline-testable (LEAF_AUTH0_JWKS_FILE points the verifier at a locally
    generated RS256 public JWKS — no live Auth0 for the automated gate),
  - precise about status codes: unauthenticated -> 401; authenticated-but-no-
    tenant -> 403 (so callers distinguish "bad token" from "no workspace").

INVARIANT (Concern 1 vs Concern 2): the tenant claim NEVER carries a Claude /
Anthropic credential. The user's "sign in with Claude" Agent-SDK OAuth is a
SEPARATE concern owned by sibling `hosted-oauth-spike`. See contract/AUTH.md.

IMPORT DISCIPLINE: PyJWT is imported at module load, so this module must only
be imported when LEAF_AUTH_LIVE=1 (deps.require_tenant imports it lazily). With
the toggle off the demo never touches PyJWT — backward-compat stays a pure
no-op and does not require `pip install -r requirements-auth.txt`.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

import jwt
from fastapi import HTTPException, status
from jwt import PyJWKClient, PyJWKSet

# --------------------------------------------------------------------------- #
# config — env with ROOT-ASSUMED DEFAULTS (see contract/AUTH.md operator section)
# Read at call time (not import time) so a single process can be toggled in tests
# and so subprocess env overrides apply.
# --------------------------------------------------------------------------- #
DEFAULT_ISSUER = "https://leafautomation.us.auth0.com/"
DEFAULT_AUDIENCE = "https://api.leafdesign.ai"
DEFAULT_JWKS_URL = "https://leafautomation.us.auth0.com/.well-known/jwks.json"
DEFAULT_CLAIM_NS = "https://leafdesign.ai/"


def issuer() -> str:
    return os.environ.get("LEAF_AUTH0_ISSUER", DEFAULT_ISSUER)


def audience() -> str:
    return os.environ.get("LEAF_AUTH0_AUDIENCE", DEFAULT_AUDIENCE)


def jwks_url() -> str:
    return os.environ.get("LEAF_AUTH0_JWKS_URL", DEFAULT_JWKS_URL)


def claim_ns() -> str:
    """Namespaced-claim prefix, always trailing-slash normalized."""
    ns = os.environ.get("LEAF_TENANT_CLAIM_NS", DEFAULT_CLAIM_NS)
    return ns if ns.endswith("/") else ns + "/"


def _jwks_file() -> Optional[str]:
    """Offline/test override: a local JWKS json file (no network fetch)."""
    return os.environ.get("LEAF_AUTH0_JWKS_FILE") or None


# --------------------------------------------------------------------------- #
# signing-key resolution
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=8)
def _cached_jwks_client(url: str) -> PyJWKClient:
    """Cached remote JWKS client (mirrors aws-ai-manager's lru_cached client)."""
    return PyJWKClient(url)


def _signing_key(token: str):
    """Resolve the RS256 signing key for a token.

    When LEAF_AUTH0_JWKS_FILE is set, load the key from a local JWKS json (the
    offline test path — no network). Otherwise use the cached remote JWKS client.
    """
    local = _jwks_file()
    if local:
        data = json.loads(Path(local).read_text(encoding="utf-8"))
        jwks = PyJWKSet.from_dict(data)
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.PyJWTError:
            kid = None
        if kid is not None:
            try:
                return jwks[kid].key
            except Exception:  # noqa: BLE001 - fall through to scan / single-key
                pass
        for k in jwks.keys:
            if kid is None or getattr(k, "key_id", None) == kid:
                return k.key
        if len(jwks.keys) == 1:  # single-key set, kid mismatch tolerated for local tests
            return jwks.keys[0].key
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="no matching signing key for token 'kid'")
    return _cached_jwks_client(jwks_url()).get_signing_key_from_jwt(token).key


def _bearer(authorization: Optional[str]) -> str:
    """Extract the raw token from an 'Authorization: Bearer <token>' header.
    Missing/malformed -> 401 (unauthenticated)."""
    if not authorization:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="missing bearer token (Authorization header)")
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="malformed Authorization header; expected 'Bearer <token>'")
    return parts[1].strip()


def verify_platform_token(authorization: Optional[str]) -> Dict[str, Any]:
    """Verify an Auth0 RS256 access token from a raw Authorization header value.

    Returns the decoded JWT payload. Raises HTTPException(401) on any
    signature / audience / issuer / expiry / malformed-header failure.
    """
    token = _bearer(authorization)
    try:
        key = _signing_key(token)
        return jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=audience(),
            issuer=issuer(),
            options={"require": ["exp", "iat"]},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="token has expired")
    except jwt.InvalidAudienceError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token audience")
    except jwt.InvalidIssuerError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token issuer")
    except jwt.InvalidSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token signature")
    except HTTPException:
        raise
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"invalid token: {exc}")


def extract_tenant_claims(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Read the namespaced tenant/org/tier/roles claims from a *verified* payload.

    A verified token that lacks the tenant claim is AUTHENTICATED BUT HAS NO
    WORKSPACE -> HTTP 403 (distinct from the 401 of an unauthenticated request).

    `roles` is returned RAW (whatever the Action minted, or None when absent):
    normalization/validation is roles.normalize_role_names' job at the deps
    seam — an unreadable roles claim degrades to no roles, never to a 4xx,
    because roles only ever ADD capability (contract/AUTH.md §11.5).
    """
    ns = claim_ns()
    tenant_id = payload.get(ns + "tenant_id")
    if not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(f"token verified but missing tenant claim '{ns}tenant_id'; "
                    "this identity is not provisioned for a Leaf workspace"),
        )
    return {
        "tenant_id": tenant_id,
        "org_id": payload.get(ns + "org_id"),
        "tier": payload.get(ns + "tier"),
        "roles": payload.get(ns + "roles"),
    }
