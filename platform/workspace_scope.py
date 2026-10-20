"""Live canonical identity and current project membership for workspace profiles."""
from __future__ import annotations

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
) -> IdentityBinding:
    # This endpoint has no dev, demo, or guest identity fallback. Authorization
    # always goes through the same canonical verifier and durable binding lookup.
    if not deps.auth_live():
        raise HTTPException(status_code=503, detail="workspace scope unavailable")
    try:
        return deps._verified_identity(authorization)
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
    binding: IdentityBinding = Depends(_live_identity),
) -> WorkspaceScope:
    try:
        role = project_lifecycle.require_project_role(
            binding.platform_tenant_id, project_id, binding.binding_id, write=False,
        )
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
