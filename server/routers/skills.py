"""Tenant-safe skill catalog routes."""
from __future__ import annotations

import hmac
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header

import deps
import skills_catalog
import telemetry_sink
from routers import ops


router = APIRouter()


def _tenant_identity(tenant: Any) -> tuple[str, str]:
    tenant_id = str(getattr(tenant, "tenant_id", tenant))
    return tenant_id, ("guest" if tenant_id.startswith("guest-") else "account")


def record_checkpoint_created(*, tenant_id: str, tenant_kind: str, session_id: str,
                              checkpoint_id: str, skill: Optional[str] = None) -> None:
    """Emit checkpoint.created.

    Reusable wiring point for any checkpoint-creating choke point (today:
    routers/checkpoints.py `create_checkpoint`, out of this card's file
    boundary; wiring the real caller is the named follow-up). Kept here,
    proven by direct unit test, so the event family and label shape exist
    before the caller does.
    """
    labels: Dict[str, Any] = {"checkpoint_id": checkpoint_id}
    if skill is not None:
        labels["skill"] = skill
    telemetry_sink.emit("checkpoint.created", tenant_id=tenant_id, tenant_kind=tenant_kind,
                        session_id=session_id, labels=labels)


def record_checkpoint_restored(*, tenant_id: str, tenant_kind: str, session_id: str,
                               checkpoint_id: str, skill: Optional[str] = None) -> None:
    """Emit checkpoint.restored. See record_checkpoint_created for the wiring note."""
    labels: Dict[str, Any] = {"checkpoint_id": checkpoint_id}
    if skill is not None:
        labels["skill"] = skill
    telemetry_sink.emit("checkpoint.restored", tenant_id=tenant_id, tenant_kind=tenant_kind,
                        session_id=session_id, labels=labels)


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
    """List the tenant bundle, plus the separate operator bundle when authorized.

    This GET is the one real skill choke point today: each skill the catalog
    actually surfaces to the caller is counted as invoked-for-use.
    """
    result = skills_catalog.catalog(operator=_is_operator(x_ops_secret))
    tenant_id, tenant_kind = _tenant_identity(_tenant)
    for skill in result.get("skills", []):
        name = skill.get("name")
        if isinstance(name, str):
            telemetry_sink.emit("skill.invoked", tenant_id=tenant_id, tenant_kind=tenant_kind,
                                session_id="none", labels={"skill": name})
    return result
