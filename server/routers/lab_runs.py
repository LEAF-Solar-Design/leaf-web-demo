"""Tenant-bound admission and control of pinned Lab runs."""
from __future__ import annotations

import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

import deps
import lab_artifacts as ledger
import lab_registry
import lab_tool_nodes
import lab_run_store as store
from dependency_health import _source_revision
from lab_canonical import graph_digest
from lab_doc_validators import InvalidGraph, TooLarge
from lab_evaluate import GraphRefused, admit_graph, trusted_registry
from lab_worker import OPS_REVISION
from routers.lab_artifacts import _actor, _admit, _body

router = APIRouter(prefix="/api/lab/runs", tags=["lab_runs"])


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except ledger.NotFound:
        raise HTTPException(404, detail="lab_run_not_found") from None
    except TooLarge:
        raise HTTPException(409, detail="lab_run_queue_full") from None
    except (GraphRefused, InvalidGraph) as error:
        raise HTTPException(422, detail=error.code) from None
    except Exception:
        raise HTTPException(503, detail="lab_store_unavailable") from None


def _create(tenant, body):
    if (not isinstance(body["doc_id"], str) or not body["doc_id"]
            or type(body["graph_revision"]) is not int or body["graph_revision"] < 1):
        raise InvalidGraph("invalid_lab_run_fields")
    artifact = ledger.get(str(tenant), body["doc_id"], body["graph_revision"])
    manifests = admit_graph(artifact["graph"], trusted_registry())
    for block in artifact["graph"]["blocks"]:
        manifest = manifests[block["id"]]
        ref = block["registry"]
        trusted = lab_registry.lookup(ref["id"], ref["version"])
        if (manifest["residency"] == "host"
                or manifest["id"] in ("studio-jobs", "studio-capabilities", "host-source")
                or (trusted is not None and trusted.residency == "host")):
            raise GraphRefused("lab_graph_host_refused")
        if lab_tool_nodes.enabled() and lab_tool_nodes.tool_block_admissible(trusted):
            if not lab_tool_nodes.drawing_valid(block["settings"].get("drawing")):
                raise GraphRefused("lab_tool_drawing_required")
            continue
        if manifest["effect"] != "pure" or (trusted is not None and trusted.effect != "pure"):
            raise GraphRefused("lab_graph_effect_refused")
    row = store.create(str(tenant), body["doc_id"], uuid.uuid4().hex,
                       body["graph_revision"], artifact["sha256"], OPS_REVISION,
                       graph_digest({}), "lab-worker.v1", _source_revision(os.environ) or "unknown",
                       registry_digest=store.registry_digest())
    return {"run_id": row["run_id"], "state": row["state"]}


@router.post("", status_code=202)
async def create(request: Request, tenant=Depends(deps.require_tenant)):
    _admit(tenant, "build")
    body = await _body(request, ("doc_id", "graph_revision"))
    return await run_in_threadpool(_call, _create, tenant, body)


@router.get("/{run_id}")
def get(run_id: str, tenant=Depends(deps.require_tenant)):
    _admit(tenant, "build")
    row = _call(store.get, str(tenant), run_id)
    return {key: value for key, value in row.items() if key not in {"lease_owner", "lease_fence"}}


def _public_event(event):
    payload_fields = {
        "failed": ("reason",),
        "suspended": ("reason",),
        "cancelled": (),
        "finished": (),
        "coalesced": ("removed",),
        "lease_takeover": (),
    }
    payload = event["payload"]
    # Value payload keys are output port names, rather than control metadata.
    public_payload = (dict(payload) if event["kind"] == "value" else
                      {key: payload[key] for key in payload_fields.get(event["kind"], ())
                       if key in payload})
    return {"seq": event["seq"], "kind": event["kind"], "block_id": event["block_id"],
            "at": event["created_at"], "payload": public_payload}


@router.get("/{run_id}/events")
def events(run_id: str, after_seq: int = Query(-1, ge=-1),
           limit: int = Query(100, ge=1, le=500), tenant=Depends(deps.require_tenant)):
    _admit(tenant, "build")
    rows = _call(store.list_events, str(tenant), run_id, after_seq=after_seq, limit=limit)
    return {"events": [_public_event(event) for event in rows]}


@router.post("/{run_id}/cancel")
def cancel(run_id: str, tenant=Depends(deps.require_tenant)):
    _admit(tenant, "build")
    return {"cancel_requested": _call(store.request_cancel, str(tenant), run_id, _actor(tenant))}


@router.post("/worker/pause")
async def pause(request: Request, tenant=Depends(deps.require_tenant)):
    _admit(tenant, "build")
    _admit(tenant, "deploy")
    body = await _body(request, ("reason",))
    if not isinstance(body["reason"], str):
        raise HTTPException(422, detail="invalid_lab_worker_pause")
    await run_in_threadpool(_call, store.set_paused, True, body["reason"], _actor(tenant))
    return {"paused": True}


@router.post("/worker/resume")
def resume(tenant=Depends(deps.require_tenant)):
    _admit(tenant, "build")
    _admit(tenant, "deploy")
    _call(store.set_paused, False, "", _actor(tenant))
    return {"paused": False}
