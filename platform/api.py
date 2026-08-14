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
from typing import Any, Dict, Literal, NamedTuple, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import billing, deps as platform_deps, entitlements, project_lifecycle, store
from .db import cursor
from .deps import (get_org_id, get_review_binding_id, get_write_binding_id, get_write_org_id,
                   require_auth_when_live)
from .models import JOB_KINDS, TIERS, Org
from .mutation_fence import drawing_mutation_commit_guard
from .offboard import OrgNotFound, PurgeHook, offboard_org

router = APIRouter(prefix="/api", tags=["platform"])


def upload_import_mutations_enabled() -> bool:
    """Return true only for an explicit upload/import activation."""
    return os.environ.get("LEAF_UPLOAD_IMPORT_MUTATIONS_ENABLED", "0") == "1"


# --------------------------------------------------------------------------- #
# request bodies
# --------------------------------------------------------------------------- #
class CreateOrgBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    tier: Optional[str] = None
    # DEV SEAM (contract/BILLING.md §6): with auth OFF, a caller may bootstrap
    # the org WITH its identity binding so cross-repo harnesses can exercise the
    # billing org-resolve path. With LEAF_AUTH_LIVE=1 a client-supplied identity
    # is never trusted — the route 422s if these are present.
    external_subject: Optional[str] = Field(default=None, min_length=1, max_length=500)
    external_authority: str = Field(default="auth0", min_length=1, max_length=100)


class CreateProjectBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class CreateBlankProjectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)


class InviteProjectMemberBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_id: uuid.UUID
    role: Literal["owner", "editor", "reviewer", "read_only"]


class PutProjectFileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=512)
    media_type: str = Field(min_length=3, max_length=200)
    content: str = Field(max_length=1_048_576)


class CloneProjectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)


class AccountUploadSourceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["account_upload"]
    drawing_id: str
    version: int = Field(ge=1)

    @field_validator("drawing_id")
    @classmethod
    def validate_drawing_id(cls, value: str) -> str:
        if store.is_account_upload_source_id(value):
            return value
        raise ValueError(
            "drawing_id must be a canonical UUID or legacy u-<10 lowercase hex>")


class ImportDrawingVersionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: AccountUploadSourceBody
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
        # Live auth: identity comes from the VERIFIED token only. A body-supplied
        # subject is refused loudly rather than silently ignored, so a miswired
        # caller learns immediately that client identity hints carry no weight.
        if body.external_subject is not None:
            raise HTTPException(status_code=422,
                                detail="external_subject is a dev-posture seam; "
                                       "live auth binds the verified token subject")
        subject = _auth.get("sub")
        if not subject:
            raise HTTPException(status_code=403, detail="verified token has no external subject")
        try:
            org, created = store.ensure_org_with_identity(
                body.name, "auth0", str(subject),
                tier=body.tier or "hosted_starter",
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
    elif body.external_subject is not None:
        # DEV SEAM: bootstrap org + binding in one call, mirroring the live-auth
        # shape, so the billing org-resolve path is exercisable without Auth0.
        try:
            org = store.create_org_with_identity(
                body.name, body.external_authority, body.external_subject,
                tier=body.tier or "hosted_starter",
            )
            created = True
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
    else:
        org = (store.create_org(body.name, tier=body.tier) if body.tier is not None
               else store.create_org(body.name))
        created = True
    if created:
        _emit_org_event("org.created", str(org.org_id), {"tier": org.tier})
    return {"org": org.to_dict(), "created": created}


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
class _LifecycleActor(NamedTuple):
    org_id: uuid.UUID
    binding_id: uuid.UUID


def _get_lifecycle_actor(
    x_org_id: str | None = Header(default=None),
    x_actor_binding_id: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> _LifecycleActor:
    """Resolve any active actor; project_lifecycle enforces the project role.

    The existing write dependency intentionally permits only tenant-level
    owners/editors.  Lifecycle membership is narrower: an invited project
    editor may write only that project and a reviewer/read-only member may read
    it.  Live identity still comes only from the verified Auth0 subject.
    """
    if platform_deps.auth_live():
        binding = platform_deps._verified_identity(authorization)
        return _LifecycleActor(
            binding.platform_tenant_id, binding.binding_id,
        )
    if not x_org_id:
        raise HTTPException(status_code=400, detail="missing X-Org-Id")
    try:
        org_id = uuid.UUID(x_org_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="invalid X-Org-Id") from None
    if not x_actor_binding_id:
        raise HTTPException(status_code=400, detail="missing X-Actor-Binding-Id")
    try:
        binding_id = uuid.UUID(x_actor_binding_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="invalid X-Actor-Binding-Id") from None
    return _LifecycleActor(org_id, binding_id)


def _lifecycle_response(operation):
    try:
        return operation()
    except project_lifecycle.LifecycleUnavailable:
        raise HTTPException(status_code=404, detail="project resource not found") from None
    except project_lifecycle.LifecycleForbidden:
        raise HTTPException(status_code=403, detail="project access denied") from None
    except project_lifecycle.LifecycleConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.post("/projects")
def create_project(body: CreateProjectBody, org_id: uuid.UUID = Depends(get_write_org_id)):
    # This route is the canonical platform project factory. Authority remains a
    # server policy and cannot be selected by request headers or body fields.
    try:
        project, created = store.get_or_create_project(
            org_id,
            body.name,
            authority_mode="postgres_canonical",
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return {"project": project.to_dict(), "created": created}


@router.post("/projects/blank", status_code=201)
def create_blank_project(
    body: CreateBlankProjectBody,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    actor: _LifecycleActor = Depends(_get_lifecycle_actor),
):
    return _lifecycle_response(lambda: project_lifecycle.create_blank_project(
        actor.org_id, actor.binding_id, name=body.name, idempotency_key=idempotency_key,
    ))


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


@router.get("/projects/{project_id}/lifecycle")
def get_project_lifecycle(
    project_id: uuid.UUID,
    actor: _LifecycleActor = Depends(_get_lifecycle_actor),
):
    return _lifecycle_response(lambda: project_lifecycle.project_snapshot(
        actor.org_id, project_id, actor.binding_id,
    ))


@router.post("/projects/{project_id}/members", status_code=201)
def invite_project_member(
    project_id: uuid.UUID,
    body: InviteProjectMemberBody,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    actor: _LifecycleActor = Depends(_get_lifecycle_actor),
):
    return _lifecycle_response(lambda: project_lifecycle.invite_project_member(
        actor.org_id, project_id, actor.binding_id, member_binding_id=body.binding_id,
        role=body.role, idempotency_key=idempotency_key,
    ))


@router.delete("/projects/{project_id}/members/{membership_id}")
def revoke_project_member(
    project_id: uuid.UUID,
    membership_id: uuid.UUID,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    actor: _LifecycleActor = Depends(_get_lifecycle_actor),
):
    return _lifecycle_response(lambda: project_lifecycle.revoke_project_member(
        actor.org_id, project_id, actor.binding_id, membership_id=membership_id,
        idempotency_key=idempotency_key,
    ))


@router.put("/projects/{project_id}/files")
def put_project_file(
    project_id: uuid.UUID,
    body: PutProjectFileBody,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    actor: _LifecycleActor = Depends(_get_lifecycle_actor),
):
    return _lifecycle_response(lambda: project_lifecycle.put_project_file(
        actor.org_id, project_id, actor.binding_id, path=body.path,
        media_type=body.media_type, content=body.content,
        idempotency_key=idempotency_key,
    ))


@router.delete("/projects/{project_id}/files/{file_id}")
def delete_project_file(
    project_id: uuid.UUID,
    file_id: uuid.UUID,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    actor: _LifecycleActor = Depends(_get_lifecycle_actor),
):
    return _lifecycle_response(lambda: project_lifecycle.delete_project_file(
        actor.org_id, project_id, actor.binding_id, file_id=file_id,
        idempotency_key=idempotency_key,
    ))


@router.post("/projects/{project_id}/clone", status_code=201)
def clone_project(
    project_id: uuid.UUID,
    body: CloneProjectBody,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    actor: _LifecycleActor = Depends(_get_lifecycle_actor),
):
    return _lifecycle_response(lambda: project_lifecycle.clone_project(
        actor.org_id, project_id, actor.binding_id, name=body.name,
        idempotency_key=idempotency_key,
    ))


@router.post("/projects/{project_id}/export")
def export_project(
    project_id: uuid.UUID,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    actor: _LifecycleActor = Depends(_get_lifecycle_actor),
):
    return _lifecycle_response(lambda: project_lifecycle.export_project(
        actor.org_id, project_id, actor.binding_id, idempotency_key=idempotency_key,
    ))


@router.post("/projects/{project_id}/reset")
def reset_project(
    project_id: uuid.UUID,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    actor: _LifecycleActor = Depends(_get_lifecycle_actor),
):
    return _lifecycle_response(lambda: project_lifecycle.reset_project(
        actor.org_id, project_id, actor.binding_id, idempotency_key=idempotency_key,
    ))


@router.delete("/projects/{project_id}")
def delete_project(
    project_id: uuid.UUID,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    actor: _LifecycleActor = Depends(_get_lifecycle_actor),
):
    return _lifecycle_response(lambda: project_lifecycle.delete_project(
        actor.org_id, project_id, actor.binding_id, idempotency_key=idempotency_key,
    ))


@router.post("/projects/{project_id}/drawing-versions/import", status_code=201)
def import_drawing_version(
    project_id: uuid.UUID,
    body: ImportDrawingVersionBody,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    org_id: uuid.UUID = Depends(get_write_org_id),
    actor_binding_id: uuid.UUID = Depends(get_write_binding_id),
):
    """Adopt a ready same-org account upload using server-derived refs and provenance."""
    # Two independent gates, both fail-closed, checked in this order:
    #   1. the lane's own activation flag (default OFF) — a deployment that has
    #      not explicitly enabled upload/import never writes at all;
    #   2. the shared cutover fence, held across the commit, so a drain started
    #      mid-flight cannot be crossed by this already-admitted request.
    if not upload_import_mutations_enabled():
        raise HTTPException(
            status_code=503,
            detail="drawing upload/import mutations are temporarily disabled",
        )
    with drawing_mutation_commit_guard() as commit_enabled:
        if not commit_enabled:
            raise HTTPException(
                status_code=503,
                detail=(
                    "drawing mutations are temporarily disabled for a storage cutover"
                ),
            )
        try:
            version, replayed = store.import_ready_account_upload(
                org_id,
                project_id,
                source_drawing_id=body.source.drawing_id,
                source_version=body.source.version,
                name=body.name,
                idempotency_key=idempotency_key,
                actor_binding_id=actor_binding_id,
            )
        except store.DrawingImportUnavailable as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except store.DrawingImportForbidden as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from None
        except store.DrawingImportConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
    return {"drawing_version": version.to_dict(), "replayed": replayed}


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
    # must grant the capability this job kind consumes; fail closed. The org
    # read happens INSIDE the helper's fail-closed boundary (a DB hiccup on the
    # enforcement read refuses with the structured 503, not a bare 500).
    denial = entitlements.stored_job_entitlement_denial(org_id, body.kind)
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
    _emit_org_event("org.offboarded", str(result.org_id), {"status": result.status})
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


# --------------------------------------------------------------------------- #
# billing tier sync (flag-gated skeleton, DARK by default — contract/BILLING.md §3)
# --------------------------------------------------------------------------- #
class BillingTierSyncBody(BaseModel):
    """Subscription facts as leaf_website's Stripe webhook knows them —
    the SAME fields the Post-Login Action reads from app_metadata.leaf, so
    both legs derive the tier from identical inputs. Never carries a
    Claude/Anthropic credential (contract/AUTH.md §0) and never carries a
    literal tier (single derivation path — the mapping table decides)."""
    plan: Optional[str] = None
    subscription_active: Optional[bool] = None
    subscription_status: Optional[str] = None
    # audit echoes only — recorded in the response for the caller's log line;
    # deliberately NOT persisted (no Stripe identifiers in the platform DB
    # until the spec's deferred audit-table decision is taken).
    stripe_subscription_id: Optional[str] = None
    stripe_event_id: Optional[str] = None


class BillingOrgResolveBody(BaseModel):
    """A verified external identity as leaf_website's Stripe webhook knows it
    (the org OWNER's Auth0 ``sub``). POSTed in the body, never in the URL, so
    the subject stays out of access logs."""
    external_authority: str = Field(default="auth0", min_length=1, max_length=100)
    external_subject: str = Field(min_length=1, max_length=500)


def _require_billing_sync_caller(x_billing_sync_secret: str | None) -> None:
    """The billing feed's shared fail-closed gate: flag off -> 503, secret
    unconfigured -> 503, missing/wrong secret -> 403 (constant-time, F16)."""
    if not billing.sync_enabled():
        raise HTTPException(status_code=503,
                            detail="billing tier sync is not enabled (LEAF_BILLING_SYNC_LIVE)")
    secret = billing.sync_secret()
    if not secret:
        raise HTTPException(status_code=503,
                            detail="billing tier sync not configured (no sync secret)")
    if x_billing_sync_secret is None or not hmac.compare_digest(
        x_billing_sync_secret.encode("utf-8"), secret.encode("utf-8")
    ):
        raise HTTPException(status_code=403, detail="billing sync secret required")


@router.post("/billing/org-resolve")
def billing_org_resolve(body: BillingOrgResolveBody,
                        x_billing_sync_secret: str | None = Header(default=None)):
    """Resolve a verified external identity to its platform org UUID
    (contract/BILLING.md §6 durable linkage).

    leaf_website's Stripe webhook calls this ONCE per org — on the first
    subscription event with no stored linkage — and persists the returned
    UUID in its own DB (Organization.platformOrgId). Same trusted-internal-
    caller model and fail-closed gates as tier-sync; read-only (resolves a
    binding the platform already owns, writes nothing).

    404 in every unresolvable case — unknown identity (an account predating
    platform bootstrap), a non-OWNER binding (the contract keys the linkage
    on the org owner; member/reviewer identities must not enumerate), and a
    non-active org (offboarding marks the org BEFORE revoking its bindings,
    and a caller caching a UUID in that window would durably persist a
    linkage tier-sync forever answers 409 for).
    """
    _require_billing_sync_caller(x_billing_sync_secret)
    binding = store.resolve_active_identity_binding(
        body.external_authority, body.external_subject)
    if binding is None:
        raise HTTPException(status_code=404, detail="no platform org for this identity")
    role = store.active_identity_role(binding.platform_tenant_id, binding.binding_id)
    if role != "owner":
        raise HTTPException(status_code=404, detail="no platform org for this identity")
    org = store.get_org(binding.platform_tenant_id)
    if org is None or org.status != "active":
        raise HTTPException(status_code=404, detail="no platform org for this identity")
    return {"org_id": str(org.org_id)}


@router.post("/orgs/{org_id}/billing/tier-sync")
def billing_tier_sync(org_id: uuid.UUID, body: BillingTierSyncBody,
                      x_billing_sync_secret: str | None = Header(default=None)):
    """Update the org's STORED tier from subscription facts (idempotent).

    Operator-gated and fail-closed at every step: flag off -> 503, secret
    unconfigured -> 503, wrong secret -> 403 (constant-time compare, F16),
    unknown org -> 404, non-active org -> 409 (billing must never resurrect
    an offboarding/deleted org). The derived tier is validated against the
    frozen claim-mintable subset before the write.
    """
    _require_billing_sync_caller(x_billing_sync_secret)

    org = store.get_org(org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="org not found")
    if org.status != "active":
        raise HTTPException(status_code=409,
                            detail=f"organization is not active (status={org.status}); "
                                   "tier not updated")

    bt = billing.server_billing_tiers()
    tier = bt.derive_tier(body.plan, body.subscription_active, body.subscription_status)
    if tier not in bt.CLAIM_TIERS:  # unreachable by construction; belt over the write
        raise HTTPException(status_code=503, detail="derived tier outside frozen vocabulary")

    updated = store.set_org_tier(org_id, tier)
    if updated is None:
        # active-guarded UPDATE matched nothing: the org flipped state between
        # the read above and the write — refuse rather than guess (TOCTOU).
        raise HTTPException(status_code=409,
                            detail="organization state changed during sync; tier not updated")
    if updated.tier != org.tier:
        _emit_org_event("billing.tier_changed", str(org_id), {
            "from_tier": org.tier, "to_tier": updated.tier})
    return {
        "org_id": str(org_id),
        "previous_tier": org.tier,
        "tier": updated.tier,
        "applied": updated.tier != org.tier,
        "stripe_subscription_id": body.stripe_subscription_id,
        "stripe_event_id": body.stripe_event_id,
    }


def _emit_org_event(name: str, org_id: str, labels=None) -> None:
    """Best-effort enterprise/revenue product event (P2): near-zero volume,
    high value (org.created / billing.tier_changed / org.offboarded).
    NEVER raises; a standalone platform process without server/ on sys.path
    simply skips (ImportError caught)."""
    try:
        import telemetry_sink

        telemetry_sink.emit(
            name,
            tenant_id=org_id,
            tenant_kind="account",
            session_id="server",
            labels=labels,
        )
    except Exception:  # noqa: BLE001 - telemetry never touches org lifecycle
        pass
