"""Tenant-bound deployment of published read-only graphs."""
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

import deps
import entitlements
import lab_activation as store
from lab_artifacts import NotFound
from lab_doc_validators import InvalidGraph, TooLarge
from lab_evaluate import GraphRefused
from routers.lab_artifacts import _actor, _admit, _body
from routers.lab_runs import _project_scope

router = APIRouter(prefix="/api/lab/activations", tags=["lab_activation"])


def _call(function, *args):
    try:
        return function(*args)
    except NotFound:
        raise HTTPException(404, detail="lab_activation_not_found") from None
    except store.Refused as error:
        detail = error.code if error.block is None else {"code": error.code, "block": error.block}
        raise HTTPException(error.status, detail=detail) from None
    except TooLarge:
        raise HTTPException(409, detail="lab_activation_limit") from None
    except GraphRefused:
        raise HTTPException(403, detail="lab_activation_project_refused") from None
    except InvalidGraph:
        raise HTTPException(422, detail="invalid_lab_activation_input") from None
    except Exception:
        raise HTTPException(503, detail="lab_store_unavailable") from None


def _require_schedule_entitlement(tenant):
    try:
        allowed = entitlements.entitlements_for(entitlements.resolve_tier(tenant)).get("deploy", False)
    except entitlements.EntitlementsError:
        raise HTTPException(503, detail="lab_policy_unavailable") from None
    if not allowed:
        raise HTTPException(403, detail="lab_activation_entitlement_required")


@router.post("", status_code=202)
async def create(request: Request, tenant=Depends(deps.require_tenant)):
    _require_schedule_entitlement(tenant)
    body = await _body(request, ("doc_id", "interval_s"), optional=("expected_revision",))
    principal = getattr(tenant, "subject", None) or "tenant:" + str(tenant)
    return await run_in_threadpool(_call, store.create, str(tenant), body["doc_id"],
                                   body["interval_s"], principal, _actor(tenant), body.get("expected_revision"))


@router.get("")
def list_activations(response: Response, preview_doc_id: str | None = None,
                     revision: int | None = None, interval_s: int | None = None,
                     tenant=Depends(deps.require_tenant)):
    if preview_doc_id is not None:
        _require_schedule_entitlement(tenant)
        if revision is None or revision < 1 or interval_s is None:
            raise HTTPException(422, detail="invalid_lab_activation_preview")
        pinned_revision, artifact = _call(store.published_artifact, str(tenant), preview_doc_id, revision)
        _call(_project_scope, tenant, artifact["graph"])
        response.headers["Cache-Control"] = "private, no-store"
        return _call(store.execution_plan, str(tenant), preview_doc_id, pinned_revision, artifact, interval_s)
    _admit(tenant, "deploy")
    return {"activations": _call(store.list, str(tenant))}


@router.post("/{activation_id}/pause")
def pause(activation_id: str, tenant=Depends(deps.require_tenant)):
    _admit(tenant, "deploy")
    return _call(store.pause, str(tenant), activation_id, _actor(tenant))


@router.post("/{activation_id}/resume")
def resume(activation_id: str, tenant=Depends(deps.require_tenant)):
    _admit(tenant, "deploy")
    return _call(store.resume, str(tenant), activation_id, _actor(tenant))


@router.post("/{activation_id}/renew")
def renew(activation_id: str, tenant=Depends(deps.require_tenant)):
    _admit(tenant, "deploy")
    return _call(store.renew, str(tenant), activation_id, _actor(tenant))


@router.post("/{activation_id}/off")
def off(activation_id: str, tenant=Depends(deps.require_tenant)):
    _admit(tenant, "deploy")
    return _call(store.off, str(tenant), activation_id, _actor(tenant))
