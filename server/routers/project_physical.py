"""Read-only canonical project terrain route for the Ground Physical workspace."""
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse

import deps
import entitlements
import project_drawing_editor as editor
import solar_physical_state as state
import solar_project_admission as policy
import solar_project_context as project
import solar_project_physical as physical
from envelopes import with_envelope_fields
from routers import project_drawings, solar_terrain
from solar_design_graph import GraphValidationError

router = APIRouter()
PREFIX = "/api/projects/{project_id}/drawings/{drawing_id}/versions/{version_id}/physical"
NO_STORE = {"Cache-Control": "no-store"}


def _terrain(tenant, project_id, drawing_id, version_id, org_header, project_header):
    if not policy.project_runs_enabled():
        raise GraphValidationError("project_execution_disabled")
    project_drawings._headers(tenant, project_id, org_header, project_header)
    verified = project.resolve_context(tenant, project_id, version_id, drawing_id=drawing_id, write=False)
    tier = entitlements.resolve_tier(tenant)
    try:
        roles, elevated = entitlements.resolve_roles(tenant)
        allowed = entitlements.entitlements_for(tier, roles, elevated).get("run_read", False)
    except entitlements.EntitlementsError:
        return entitlements.policy_unavailable_response("run_read", tier)
    if not allowed:
        return entitlements.entitlement_denied_response("run_read", tier)
    return physical.terrain_view(verified)


@router.get(PREFIX)
def get_physical(project_id: UUID, drawing_id: UUID, version_id: UUID,
                 tenant=Depends(deps.require_active_tenant),
                 x_org_id: Optional[UUID] = Header(default=None),
                 x_project_id: Optional[UUID] = Header(default=None)):
    try:
        result = _terrain(tenant, project_id, drawing_id, version_id, x_org_id, x_project_id)
        if isinstance(result, JSONResponse):
            result.headers.update(NO_STORE)
            return result
        return JSONResponse(content=with_envelope_fields(result), headers=NO_STORE)
    except project.ProjectContextError as exc:
        def refused():
            raise exc
        return project_drawings._dispatch(refused)
    except GraphValidationError as exc:
        return project_drawings._output_failure(exc.code)
    except editor.EditorError as exc:
        def refused():
            raise exc
        return project_drawings._editor_dispatch(refused)
    except state.PhysicalStateError as exc:
        response = solar_terrain._terrain_refused(exc.code)
        response.headers.update(NO_STORE)
        return response
    except Exception:
        return project_drawings._dispatch(lambda: project.refuse("INTERNAL"))
