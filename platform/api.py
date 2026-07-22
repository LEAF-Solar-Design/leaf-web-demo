"""HTTP API for the canonical Project/Job entity (FastAPI APIRouter).

Self-contained router — NOT mounted into server/app.py here (a sibling owns that
file). Integration is one line, documented in platform/README.md:

    from platform.api import router as platform_router   # (or the leaf_platform alias)
    app.include_router(platform_router)

Org is resolved by deps.get_org_id (dev: X-Org-Id header). A resource not owned by
the caller's org yields HTTP 404, never 403 (a 403 would leak existence).
"""
from __future__ import annotations

import hmac
import os
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from . import entitlements, store
from .db import cursor
from .deps import (get_org_id, get_review_binding_id, get_write_binding_id, get_write_org_id,
                   require_auth_when_live)
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


class CanonicalRecordBody(BaseModel):
    payload: Dict[str, Any]
    operation_type: Optional[str] = Field(default=None, min_length=1, max_length=100)
    parent_operation_ids: list[uuid.UUID] = Field(default_factory=list)
    branch_name: Optional[str] = Field(default=None, min_length=1, max_length=200)


class ShareGrantBody(BaseModel):
    role: str = "reviewer"
    ttl_seconds: int = 86400


class ShareTokenBody(BaseModel):
    token: str


class SnapshotDiffBody(BaseModel):
    left_snapshot_id: uuid.UUID
    right_snapshot_id: uuid.UUID


class ComplianceRunBody(BaseModel):
    inputs: Dict[str, Any]


class WaiverBody(BaseModel):
    reason: str = Field(min_length=1, max_length=4000)


class WaiverTransitionBody(BaseModel):
    state: str
    note: str = Field(default="", max_length=4000)


class ReviewSignatureBody(BaseModel):
    credential_id: uuid.UUID


# --------------------------------------------------------------------------- #
# orgs — the tenant anchor every project/job route requires
# --------------------------------------------------------------------------- #
# DEV POSTURE (open endpoint) vs LIVE AUTH (F6): POST /api/orgs mints an org.
# With auth OFF (demo) it is UNGATED so the demo can bootstrap the first org_id
# (chicken/egg: you cannot present an org you do not yet have). With
# LEAF_AUTH_LIVE=1 it is GATED behind require_auth_when_live — org creation
# becomes a side effect of a real, verified login and an UNAUTHENTICATED call is
# rejected (401); a client-supplied identity is never trusted. Documented in
# platform/README.md. GET /api/orgs/{org_id} keeps the same 404-not-403
# isolation posture as the project/job reads: a caller may only read THEIR OWN
# org, and a cross-org (or unknown) org yields 404. In live-auth mode the caller
# org is the VERIFIED session's org (deps.get_org_id), never the X-Org-Id header.
@router.post("/orgs")
def create_org(body: CreateOrgBody, _auth: Any = Depends(require_auth_when_live)):
    if body.tier is not None and body.tier not in TIERS:
        raise HTTPException(status_code=422, detail=f"tier must be one of {list(TIERS)}")
    if _auth is not None:
        subject = _auth.get("sub")
        if not subject:
            raise HTTPException(status_code=403, detail="verified token has no external subject")
        if store.resolve_active_identity_binding("auth0", str(subject)) is not None:
            raise HTTPException(status_code=409, detail="verified subject already has a platform identity binding")
    if _auth is not None:
        try:
            org = store.create_org_with_identity(
                body.name, "auth0", str(subject),
                tier=body.tier or "hosted_starter",
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
    else:
        org = (store.create_org(body.name, tier=body.tier) if body.tier is not None
               else store.create_org(body.name))
    return {"org": org.to_dict()}


@router.get("/orgs/{org_id}")
def get_org(org_id: uuid.UUID, caller_org: uuid.UUID = Depends(get_org_id)):
    # 404-not-403: a caller may only read its own org; a mismatch leaks nothing.
    if org_id != caller_org:
        raise HTTPException(status_code=404, detail="org not found")
    # Org-scoped by construction: org_id == the caller's verified org.
    org = store.get_org(org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="org not found")
    return {"org": org.to_dict()}


# --------------------------------------------------------------------------- #
# projects
# --------------------------------------------------------------------------- #
@router.post("/projects")
def create_project(body: CreateProjectBody, org_id: uuid.UUID = Depends(get_write_org_id)):
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
               org_id: uuid.UUID = Depends(get_write_org_id)):
    if body.kind not in JOB_KINDS:
        raise HTTPException(status_code=422, detail=f"kind must be one of {list(JOB_KINDS)}")
    # ownership check first: 404 (not 403) if the project is not the caller's
    # — kept BEFORE entitlement so a cross-org probe learns nothing new.
    if store.get_project(org_id, project_id) is None:
        raise HTTPException(status_code=404, detail="project not found")
    # Tier-branching entitlement enforcement (P1 floor): the caller org's tier
    # must grant the capability this job kind consumes; fail closed.
    denial = entitlements.job_entitlement_denial(store.get_org(org_id), body.kind)
    if denial is not None:
        return denial
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


# Canonical records require a server-owned postgres_canonical cutover.  Clients
# cannot select authority mode in a header or body.
@router.post("/projects/{project_id}/history")
def append_history(project_id: uuid.UUID, body: CanonicalRecordBody,
                   idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                   org_id: uuid.UUID = Depends(get_write_org_id)):
    if store.get_project(org_id, project_id) is None:
        raise HTTPException(status_code=404, detail="project not found")
    try:
        operation = store.append_history_operation(
            org_id, project_id, body.operation_type or "drawing.mutation", body.payload,
            idempotency_key or "", parent_operation_ids=body.parent_operation_ids,
            branch_name=body.branch_name,
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"operation": {"operation_id": str(operation.operation_id),
                           "content_hash": operation.content_hash.to_dict(),
                           "idempotency_key": operation.idempotency_key}}


@router.post("/projects/{project_id}/solves")
def append_solve(project_id: uuid.UUID, body: CanonicalRecordBody,
                 idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                 org_id: uuid.UUID = Depends(get_write_org_id)):
    raise HTTPException(
        status_code=405,
        detail="canonical solve records are worker-owned; submit a durable solve job",
    )


@router.post("/projects/{project_id}/share-grants", status_code=201)
def create_share_grant(
    project_id: uuid.UUID,
    body: ShareGrantBody,
    org_id: uuid.UUID = Depends(get_write_org_id),
    binding_id: uuid.UUID = Depends(get_write_binding_id),
):
    from . import access
    try:
        grant = access.create_project_share_grant(
            org_id, project_id, binding_id,
            role=body.role, ttl_seconds=body.ttl_seconds)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"grant": grant}


@router.delete("/projects/{project_id}/share-grants/{grant_id}")
def revoke_share_grant(
    project_id: uuid.UUID,
    grant_id: uuid.UUID,
    org_id: uuid.UUID = Depends(get_write_org_id),
    binding_id: uuid.UUID = Depends(get_write_binding_id),
):
    from . import access
    try:
        outcome = access.revoke_project_share_grant(
            org_id, project_id, grant_id, binding_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    if outcome == "missing":
        raise HTTPException(status_code=404, detail="share grant not found")
    return {"grant_id": str(grant_id), "status": "revoked"}


@router.post("/share-grants/resolve")
def resolve_share_grant(body: ShareTokenBody):
    from . import access
    try:
        grant = access.resolve_project_share_token(body.token)
    except ValueError:
        grant = None
    if grant is None:
        # Avoid an oracle across malformed, unknown, expired, and revoked tokens.
        raise HTTPException(status_code=404, detail="share grant not found")
    return {"grant": grant}


@router.get("/snapshots/{snapshot_id}")
def read_snapshot(snapshot_id: uuid.UUID, _org_id: uuid.UUID = Depends(get_org_id)):
    from . import snapshots
    item = snapshots.get_snapshot(snapshot_id)
    if item is None:
        raise HTTPException(status_code=404, detail="snapshot not found")
    return {"snapshot": item}


@router.post("/snapshots/diff")
def diff_snapshots(body: SnapshotDiffBody, _org_id: uuid.UUID = Depends(get_org_id)):
    from . import snapshots
    left = snapshots.get_snapshot(body.left_snapshot_id)
    right = snapshots.get_snapshot(body.right_snapshot_id)
    if left is None or right is None or left["snapshot_kind"] != right["snapshot_kind"]:
        raise HTTPException(status_code=404, detail="comparable snapshots not found")
    return {"kind": left["snapshot_kind"], "changes": snapshots.diff(left["content"], right["content"])}


@router.post("/projects/{project_id}/solves/{solve_id}/compliance", status_code=201)
def run_compliance(project_id: uuid.UUID, solve_id: uuid.UUID, body: ComplianceRunBody,
                   org_id: uuid.UUID = Depends(get_write_org_id)):
    from . import compliance_store
    try:
        return compliance_store.record_pinned_run(org_id, project_id, solve_id, body.inputs)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/projects/{project_id}/solves/{solve_id}/compliance-findings")
def compliance_findings(project_id: uuid.UUID, solve_id: uuid.UUID,
                        org_id: uuid.UUID = Depends(get_org_id)):
    from . import compliance_store
    return {"findings": compliance_store.list_findings(org_id, project_id, solve_id)}


@router.post("/projects/{project_id}/compliance-findings/{finding_id}/waivers", status_code=201)
def propose_compliance_waiver(project_id: uuid.UUID, finding_id: uuid.UUID, body: WaiverBody,
                              org_id: uuid.UUID = Depends(get_write_org_id),
                              binding_id: uuid.UUID = Depends(get_write_binding_id)):
    from . import compliance_store
    try:
        return compliance_store.propose_waiver(
            org_id, project_id, finding_id, binding_id, body.reason)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/projects/{project_id}/compliance-waivers/{waiver_id}/transitions")
def transition_compliance_waiver(project_id: uuid.UUID, waiver_id: uuid.UUID,
                                 body: WaiverTransitionBody,
                                 org_id: uuid.UUID = Depends(get_write_org_id),
                                 binding_id: uuid.UUID = Depends(get_write_binding_id)):
    from . import compliance_store
    try:
        return compliance_store.transition_waiver(
            org_id, project_id, waiver_id, binding_id, body.state, body.note)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/projects/{project_id}/solves/{solve_id}/evidence-bundles", status_code=201)
def create_evidence_bundle(project_id: uuid.UUID, solve_id: uuid.UUID,
                           idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                           org_id: uuid.UUID = Depends(get_write_org_id)):
    from . import evidence_store
    try:
        return evidence_store.create_bundle(
            org_id, project_id, solve_id, idempotency_key or "")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/projects/{project_id}/evidence-bundles/{bundle_id}")
def export_evidence_bundle(project_id: uuid.UUID, bundle_id: uuid.UUID,
                           org_id: uuid.UUID = Depends(get_org_id)):
    from . import evidence_store
    bundle = evidence_store.export_bundle(org_id, project_id, bundle_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail="evidence bundle not found")
    return bundle


@router.get("/projects/{project_id}/evidence-bundles")
def list_evidence_bundles(
    project_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=100),
    org_id: uuid.UUID = Depends(get_org_id),
):
    from . import evidence_store
    return {"bundles": evidence_store.list_bundles(org_id, project_id, limit=limit)}


@router.get("/professional-review/context")
def get_professional_review_context(
    org_id: uuid.UUID = Depends(get_org_id),
    binding_id: uuid.UUID = Depends(get_review_binding_id),
):
    from . import signing
    return signing.review_context(org_id, binding_id)


@router.post("/projects/{project_id}/evidence-bundles/{bundle_id}/signatures", status_code=201)
def countersign_evidence_bundle(
    project_id: uuid.UUID,
    bundle_id: uuid.UUID,
    body: ReviewSignatureBody,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    org_id: uuid.UUID = Depends(get_org_id),
    binding_id: uuid.UUID = Depends(get_review_binding_id),
):
    from . import signing
    try:
        return signing.countersign(
            org_id, project_id, bundle_id, body.credential_id, binding_id,
            idempotency_key or "")
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/projects/{project_id}/review-signatures/{signature_id}/verify")
def verify_review_signature(project_id: uuid.UUID, signature_id: uuid.UUID,
                            org_id: uuid.UUID = Depends(get_org_id)):
    from . import signing
    result = signing.verify_signature(org_id, project_id, signature_id)
    if result.get("errors") == ["signature_not_found"]:
        raise HTTPException(status_code=404, detail="review signature not found")
    return result


@router.post("/shared/evidence-bundles/{bundle_id}/resolve")
def resolve_shared_evidence_bundle(bundle_id: uuid.UUID, body: ShareTokenBody):
    from . import access, evidence_store
    try:
        grant = access.resolve_project_share_token(body.token)
    except ValueError:
        grant = None
    if grant is None:
        raise HTTPException(status_code=404, detail="shared evidence bundle not found")
    bundle = evidence_store.export_bundle(
        uuid.UUID(grant["org_id"]), uuid.UUID(grant["project_id"]), bundle_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail="shared evidence bundle not found")
    return {"role": grant["role"], **bundle}


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
        # fail closed when the env is unset (no token configured => no offboarding)
        raise HTTPException(status_code=503, detail="offboarding not configured (no admin token)")
    # F16: constant-time compare (hmac.compare_digest) so the admin token cannot be
    # recovered byte-by-byte via response-timing. Guard None first (compare_digest
    # rejects None) and compare as bytes so a non-ASCII header can't raise.
    if x_admin_token is None or not hmac.compare_digest(
        x_admin_token.encode("utf-8"), admin_token.encode("utf-8")
    ):
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
        # hard-PURGE audit window (migration 0002; DELETION-OFFBOARDING-DESIGN.md sec 3)
        "purge_requested_at": (
            result.purge_requested_at.isoformat() if result.purge_requested_at else None
        ),
        "purge_completed_at": (
            result.purge_completed_at.isoformat() if result.purge_completed_at else None
        ),
    }
