"""Live canonical identity and current project membership for workspace profiles."""
from __future__ import annotations

import os
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from . import deps, project_lifecycle
from .models import IdentityBinding

router = APIRouter()


class WorkspaceScope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    tenantId: uuid.UUID
    projectId: uuid.UUID
    platformUserId: uuid.UUID
    bindingId: uuid.UUID
    externalAuthority: Literal["auth0"]
    externalSubject: str = Field(min_length=1, max_length=500)
    projectRole: Literal["owner", "editor", "reviewer", "read_only"]


def _live_identity(
    authorization: str | None = Header(default=None),
) -> tuple[IdentityBinding, dict]:
    # This endpoint has no dev, demo, or guest identity fallback. Authorization
    # always goes through the same canonical verifier and durable binding lookup.
    if not deps.auth_live():
        raise HTTPException(status_code=503, detail="workspace scope unavailable")
    try:
        payload = deps._server_auth().verify_platform_token(authorization)
        subject = payload.get("sub")
        if not subject:
            raise HTTPException(
                status_code=403, detail="token verified but carries no external subject",
            )
        from .store import resolve_active_identity_binding
        binding = resolve_active_identity_binding("auth0", str(subject))
        if binding is None:
            raise HTTPException(
                status_code=403,
                detail="verified subject has no active platform identity binding",
            )
        return binding, payload
    except HTTPException as exc:
        if exc.status_code < 500:
            raise
        raise HTTPException(status_code=503, detail="workspace scope unavailable") from None
    except Exception:
        raise HTTPException(status_code=503, detail="workspace scope unavailable") from None


@router.get("/projects/{project_id}/scope", response_model=WorkspaceScope)
def get_workspace_scope(
    project_id: uuid.UUID,
    response: Response,
    identity: tuple[IdentityBinding, dict] = Depends(_live_identity),
) -> WorkspaceScope:
    binding, payload = identity
    try:
        role = project_lifecycle.require_project_role(
            binding.platform_tenant_id, project_id, binding.binding_id, write=False,
        )
        # Mirror the two-factor platform admin pass from #2248 and P10.
        if (role == "editor"
                and payload.get(deps._server_auth().claim_ns() + "tier") == "admin"
                and payload.get("sub") in {
                    subject.strip() for subject in
                    os.environ.get("LEAF_PLATFORM_ADMIN_SUBJECTS", "").split(",")
                    if subject.strip()
                }):
            role = "owner"
        scope = WorkspaceScope(
            tenantId=binding.platform_tenant_id,
            projectId=project_id,
            platformUserId=binding.platform_user_id,
            bindingId=binding.binding_id,
            externalAuthority=binding.external_authority,
            externalSubject=binding.external_subject,
            projectRole=role,
        )
    except project_lifecycle.LifecycleUnavailable:
        raise HTTPException(status_code=404, detail="project resource not found") from None
    except project_lifecycle.LifecycleForbidden:
        raise HTTPException(status_code=403, detail="project access denied") from None
    except Exception:
        raise HTTPException(status_code=503, detail="workspace scope unavailable") from None
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Authorization"
    return scope
