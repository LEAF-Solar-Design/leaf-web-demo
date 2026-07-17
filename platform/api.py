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
from .deps import get_org_id
from .models import JOB_KINDS
from .offboard import OrgNotFound, PurgeHook, offboard_org

router = APIRouter(prefix="/api", tags=["platform"])


# --------------------------------------------------------------------------- #
# request bodies
# --------------------------------------------------------------------------- #
class CreateProjectBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class CreateJobBody(BaseModel):
    kind: str
    tool: Optional[str] = None
    params: Optional[Dict[str, Any]] = None


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
