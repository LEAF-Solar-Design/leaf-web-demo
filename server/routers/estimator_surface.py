"""Fail-closed Studio status and project-bound Estimator handoff routes."""
import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

import deps
import estimator_handoff as handoff
import platform_link
from routers.lab_surface import lab_posture

logger = logging.getLogger(__name__)
NO_STORE = {"Cache-Control": "no-store"}


def refused(code=404):
    return HTTPException(code, detail="estimator_unavailable", headers=NO_STORE)


def rail():
    posture = lab_posture()
    if os.environ.get("LEAF_ESTIMATOR_SURFACE_ENABLED") != "true" or posture not in handoff.ISSUERS:
        raise refused()
    rows = handoff.configured_bindings(os.environ.get("LEAF_ESTIMATOR_BINDINGS", "[]"), posture)
    if not rows:
        raise refused()
    return rows


router = APIRouter(tags=["estimator_surface"], dependencies=[Depends(rail)])


def caller(tenant=Depends(deps.require_tenant)):
    if not handoff.human_caller(tenant):
        raise refused(403)
    return tenant


@router.get("/api/estimator/status")
def status(rows=Depends(rail), tenant=Depends(caller)):
    visible = []
    try:
        binding = platform_link.resolve_caller_binding(tenant)
        if binding is None:
            raise refused()
        for row in rows:
            if row.tenant_id != str(tenant):
                continue
            try:
                platform_link.require_project_access(tenant, row.studio_project_id, write=True, binding=binding)
            except (LookupError, platform_link.ProjectSessionForbidden):
                continue
            visible.append({"project_id": row.studio_project_id, "estimator_origin": row.estimator_origin})
    except (LookupError, platform_link.ProjectSessionForbidden):
        raise refused() from None
    if not visible:
        raise refused()
    return JSONResponse({"state": "on", "bindings": visible}, headers=NO_STORE)


async def channel(request: Request):
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise refused(400)
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 1024:
            raise refused(400)
        raw.extend(chunk)
    try:
        body = handoff.strict_json(raw)
        if (type(body) is not dict or set(body) != {"chan"} or type(body["chan"]) is not str
                or not handoff.CHANNEL.fullmatch(body["chan"])):
            raise ValueError()
        return body["chan"]
    except (ValueError, UnicodeError, RecursionError):
        raise refused(400) from None


@router.post("/api/projects/{project_id}/estimator/handoff")
def mint(project_id: str, rows=Depends(rail), tenant=Depends(caller), chan=Depends(channel)):
    if not handoff.canonical_uuid(project_id):
        raise refused(400)
    try:
        platform_link.require_project_access(tenant, project_id, write=True)
    except (LookupError, platform_link.ProjectSessionForbidden):
        raise refused() from None
    row = next((row for row in rows if row.tenant_id == str(tenant) and row.studio_project_id == project_id), None)
    if row is None:
        raise refused()
    retry = handoff.limiter.admit(tenant.subject)
    if retry:
        raise HTTPException(429, detail="estimator_unavailable", headers={**NO_STORE, "Retry-After": str(retry)})
    try:
        claims = handoff.claims_for(row, tenant.subject, chan, lab_posture())
        token = handoff.signer().sign(claims)
    except Exception:
        # No exception text: SDK exceptions may contain request/response material.
        logger.warning("Estimator signing unavailable")
        raise refused(503) from None
    logger.info("Estimator mint jti=%s sub=%s tenant=%s sproj=%s binding=%s",
                claims["jti"], claims["sub"], row.tenant_id, row.studio_project_id, row.deployment_id)
    return JSONResponse({"token": token, "estimator_origin": row.estimator_origin,
                         "subh": claims["sub"], "expires_in": 60}, headers=NO_STORE)
