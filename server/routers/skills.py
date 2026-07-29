"""Tenant-safe skill catalog routes."""
from __future__ import annotations

import hmac
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header

import deps
import skills_catalog
from routers import ops


router = APIRouter()


def _is_operator(x_ops_secret: Optional[str]) -> bool:
    """Use the existing ops-secret principal proof without making this route ops-only.

    Compared as BYTES: hmac.compare_digest raises TypeError on non-ASCII str
    input, and an ASGI header can legally carry latin-1 — so a stray `é` in the
    header was an unauthenticated 500 (review round 2). Encoding both sides
    keeps the comparison constant-time and total; a non-UTF-8-encodable secret
    cannot occur (it comes from an env var we set), but the header side is
    caller-controlled and must never throw."""
    secret = ops._ops_secret()
    if secret is None:
        return False
    try:
        provided = (x_ops_secret or "").encode("utf-8")
    except UnicodeEncodeError:  # lone surrogates from a hostile raw header
        return False
    return hmac.compare_digest(provided, secret.encode("utf-8"))


@router.get("/api/skills")
def get_skills(
    _tenant: Any = Depends(deps.require_tenant),
    x_ops_secret: Optional[str] = Header(default=None),
) -> dict[str, list[dict[str, str]]]:
    """List the tenant bundle, plus the separate operator bundle when authorized."""
    return skills_catalog.catalog(operator=_is_operator(x_ops_secret))
