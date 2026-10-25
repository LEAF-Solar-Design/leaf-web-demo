"""Tenant-bound deployment of published read-only graphs."""
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

import deps
import lab_activation as store
from lab_artifacts import NotFound
from lab_doc_validators import InvalidGraph, TooLarge
from routers.lab_artifacts import _actor, _admit, _body

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
    except InvalidGraph:
        raise HTTPException(422, detail="invalid_lab_activation_input") from None
    except Exception:
        raise HTTPException(503, detail="lab_store_unavailable") from None


@router.post("", status_code=202)
async def create(request: Request, tenant=Depends(deps.require_tenant)):
    _admit(tenant, "deploy")
    body = await _body(request, ("doc_id", "interval_s"))
    principal = getattr(tenant, "subject", None) or "tenant:" + str(tenant)
    return await run_in_threadpool(_call, store.create, str(tenant), body["doc_id"],
                                   body["interval_s"], principal, _actor(tenant))


@router.get("")
def list_activations(tenant=Depends(deps.require_tenant)):
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
