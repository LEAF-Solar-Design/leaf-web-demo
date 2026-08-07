"""require_operator — per-request operator principal resolution.

contract/OPERATOR.md sections 1-2. The browser cannot assert operator mode:
the JWT (live auth) supplies only the subject; the server-owned
``operator_principals`` row is the grant. Off-live (dev), the
``X-Operator-Subject`` header names WHICH subject to look up — the grant is
still the PG row, so a dev header without a granted principal resolves
nothing. Denials are 404 (no existence oracle); database unavailability is
503 (fail closed), never an open fallback.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, Header, HTTPException

import deps as tenant_deps
import operator_principals


@dataclass(frozen=True)
class OperatorContext:
    subject: str
    role: str
    role_revision: int
    profiles: tuple
    environment: str
    profile: str


def _resolve_subject(tenant, x_operator_subject: Optional[str]) -> Optional[str]:
    subject = getattr(tenant, "subject", None)
    if subject:
        return subject
    if not tenant_deps.auth_live():
        # Dev convenience only: names the row to look up, grants nothing.
        return x_operator_subject
    return None


def require_operator(
    tenant=Depends(tenant_deps.require_tenant),
    x_operator_subject: Optional[str] = Header(default=None),
    x_operator_profile: Optional[str] = Header(default=None),
) -> OperatorContext:
    subject = _resolve_subject(tenant, x_operator_subject)
    if not subject:
        raise HTTPException(status_code=404, detail="operator_not_found")
    try:
        principal = operator_principals.resolve_principal(subject)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - DB down = fail closed
        raise HTTPException(
            status_code=503, detail="operator_store_unavailable") from exc
    if principal is None or not principal.active:
        # Revoked, suspended, and never-granted are indistinguishable.
        raise HTTPException(status_code=404, detail="operator_not_found")
    profile = (x_operator_profile or "default").strip() or "default"
    if profile not in principal.profiles:
        raise HTTPException(status_code=404, detail="operator_not_found")
    return OperatorContext(
        subject=principal.subject, role=principal.role,
        role_revision=principal.role_revision, profiles=principal.profiles,
        environment=principal.environment, profile=profile)
