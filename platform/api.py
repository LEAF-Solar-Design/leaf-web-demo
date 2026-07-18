"""HTTP API for the canonical Project/Job entity (FastAPI APIRouter).

Self-contained router — NOT mounted into server/app.py here (a sibling owns that
file). Integration is one line, documented in platform/README.md:

    from platform.api import router as platform_router   # (or the leaf_platform alias)
    app.include_router(platform_router)

Org is resolved by deps.get_org_id (dev: X-Org-Id header). A resource not owned by
the caller's org yields HTTP 404, never 403 (a 403 would leak existence).
"""
from __future__ import annotations

import os
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from . import store
from .db import cursor
from .deps import get_org_id
from .models import JOB_KINDS, TIERS, Org
from .offboard import OrgNotFound, PurgeHook, offboard_org

router = APIRouter(prefix="/api", tags=["platform"])


# --------------------------------------------------------------------------- #
# request bodies
# --------------------------------------------------------------------------- #
class CreateOrgBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    tier: Optional[str] = None


class CreateProjectBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class CreateJobBody(BaseModel):
    kind: str
    tool: Optional[str] = None
    params: Optional[Dict[str, Any]] = None


# --------------------------------------------------------------------------- #
# orgs — the tenant anchor every project/job route requires
# --------------------------------------------------------------------------- #
# DEV POSTURE (open endpoint): POST /api/orgs mints an org with NO auth gate so
# the demo can bootstrap the first org_id (chicken/egg: you cannot present an
# org you do not yet have). In PRODUCTION this MUST be gated behind the
# auth/identity layer — org creation becomes a side effect of first login /
# provisioning, and a client-supplied identity is never trusted here. Documented
# in platform/README.md. GET /api/orgs/{org_id} keeps the same 404-not-403
# isolation posture as the project/job reads: a caller may only read THEIR OWN
# org (X-Org-Id must match the path), and a cross-org (or unknown) org yields 404.
@router.post("/orgs")
def create_org(body: CreateOrgBody):
    if body.tier is not None and body.tier not in TIERS:
        raise HTTPException(status_code=422, detail=f"tier must be one of {list(TIERS)}")
    org = (store.create_org(body.name, tier=body.tier) if body.tier is not None
           else store.create_org(body.name))
    return {"org": org.to_dict()}


@router.get("/orgs/{org_id}")
def get_org(org_id: uuid.UUID, caller_org: uuid.UUID = Depends(get_org_id)):
    # 404-not-403: a caller may only read its own org; a mismatch leaks nothing.
    if org_id != caller_org:
        raise HTTPException(status_code=404, detail="org not found")
    # store.py has no get_org (and is not this lane's to edit); this read is
    # org-scoped by construction (WHERE org_id = the caller's verified org).
    with cursor() as cur:
        cur.execute(
            "SELECT org_id, name, tier, status, created_at, offboarded_at "
            "FROM orgs WHERE org_id = %(org_id)s",
            {"org_id": org_id},
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="org not found")
    return {"org": Org.from_row(row).to_dict()}


# --------------------------------------------------------------------------- #
# projects
# --------------------------------------------------------------------------- #
@router.post("/projects")
def create_project(body: CreateProjectBody, org_id: uuid.UUID = Depends(get_org_id)):
    project = store.create_project(org_id, body.name)
    return {"project": project.to_dict()}


@router.get("/projects")
def list_projects(org_id: uuid.UUID = Depends(get_org_id)):
    return {"projects": [p.to_dict() for p in store.list_projects(org_id)]}


@router.get("/projects/{project_id}")
def open_project(project_id: uuid.UUID, org_id: uuid.UUID = Depends(get_org_id)):
    """Workspace hydration payload: project + drawing_versions[] + jobs[] + built_tools[]."""
    payload = store.hydrate_project(org_id, project_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="project not found")
    return payload


# --------------------------------------------------------------------------- #
# jobs
# --------------------------------------------------------------------------- #
@router.post("/projects/{project_id}/jobs")
def create_job(project_id: uuid.UUID, body: CreateJobBody,
               org_id: uuid.UUID = Depends(get_org_id)):
    if body.kind not in JOB_KINDS:
        raise HTTPException(status_code=422, detail=f"kind must be one of {list(JOB_KINDS)}")
    # ownership check first: 404 (not 403) if the project is not the caller's
    if store.get_project(org_id, project_id) is None:
        raise HTTPException(status_code=404, detail="project not found")
    job = store.create_job(
        org_id, project_id, body.kind, tool_name=body.tool, params=body.params
    )
    return {"job": job.to_dict()}


@router.get("/projects/{project_id}/jobs")
def list_jobs(project_id: uuid.UUID, org_id: uuid.UUID = Depends(get_org_id)):
    if store.get_project(org_id, project_id) is None:
        raise HTTPException(status_code=404, detail="project not found")
    return {"jobs": [j.to_dict() for j in store.list_jobs(org_id, project_id)]}


@router.get("/jobs/{job_id}")
def get_job(job_id: uuid.UUID, org_id: uuid.UUID = Depends(get_org_id)):
    job = store.get_job(org_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return {"job": job.to_dict()}


# --------------------------------------------------------------------------- #
# offboarding (admin/operator-gated — the only hard-delete path)
# --------------------------------------------------------------------------- #
# Integration seam: the credential-broker / OSS siblings inject the real purge hooks
# by overriding these module attributes. Defaults are no-ops so a standalone deploy
# does not silently believe secrets/blobs were purged.
key_purge_hook: PurgeHook = lambda ref: None
blob_purge_hook: PurgeHook = lambda ref: None


@router.delete("/orgs/{org_id}")
def offboard(org_id: uuid.UUID, x_admin_token: str | None = Header(default=None)):
    admin_token = os.environ.get("PLATFORM_ADMIN_TOKEN")
    if not admin_token:
        raise HTTPException(status_code=503, detail="offboarding not configured (no admin token)")
    if x_admin_token != admin_token:
        raise HTTPException(status_code=403, detail="admin token required")
    try:
        result = offboard_org(
            org_id, key_purge_hook=key_purge_hook, blob_purge_hook=blob_purge_hook
        )
    except OrgNotFound:
        raise HTTPException(status_code=404, detail="org not found")
    return {
        "org_id": str(result.org_id), "status": result.status,
        "deleted_projects": result.deleted_projects,
        "purged_secret_refs": len(result.secret_refs),
        "purged_blob_refs": len(result.blob_refs),
    }
