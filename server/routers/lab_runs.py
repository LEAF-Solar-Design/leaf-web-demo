"""Tenant-bound admission and control of pinned Lab runs."""
from __future__ import annotations

import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

import deps
import lab_artifacts as ledger
import lab_registry
import lab_tool_manifests
import lab_tool_nodes
import platform_link
import lab_run_store as store
from dependency_health import _source_revision
from lab_canonical import graph_digest
from lab_doc_validators import InvalidGraph, TooLarge
from lab_evaluate import GraphRefused, admit_graph
from lab_worker import OPS_REVISION
from routers.lab_artifacts import _actor, _admit, _body

router = APIRouter(prefix="/api/lab/runs", tags=["lab_runs"])


def _corpus_actor(tenant):
    """Only the verified live subject may resolve the active platform binding."""
    if (not getattr(tenant, 'authority_resolved', False) or not getattr(tenant, 'subject', None)
            or str(getattr(tenant, 'org_id', '')) != str(tenant)):
        raise GraphRefused('lab_corpus_authority_required')
    binding = platform_link.platform_store().resolve_active_identity_binding('auth0', tenant.subject)
    if binding is None or str(binding.platform_tenant_id) != str(tenant):
        raise GraphRefused('lab_corpus_authority_required')
    return str(binding.binding_id)


def _corpus_context(tenant, actor, settings):
    import lab_corpus_executor as corpus
    try:
        return corpus.validate_context({'schema': corpus.SCHEMA, 'tenant_id': str(tenant),
            'org_id': str(tenant), 'actor_id': actor, 'project_id': settings['project_id'],
            'corpus': settings['corpus'], 'suite': settings['suite'],
            'capability_sha256': corpus.ARCHIVE_SHA256})
    except ValueError:
        raise GraphRefused('lab_corpus_scope_refused') from None


def _authorize_corpus_run(tenant, row, *, write=False):
    authority = store.corpus_authority(row)
    if authority is None:
        return
    actor = _corpus_actor(tenant)
    from leaf_platform import project_lifecycle
    try:
        project_lifecycle.require_project_role(str(tenant), authority['project_id'], actor, write=write)
    except (project_lifecycle.LifecycleForbidden, project_lifecycle.LifecycleUnavailable):
        raise GraphRefused('lab_corpus_project_refused') from None


@router.get('/corpus-options')
def corpus_options(project_id: uuid.UUID | None = None, tenant=Depends(deps.require_tenant)):
    _admit(tenant, 'build')
    def operation():
        if not lab_tool_nodes.enabled():
            raise GraphRefused('lab_corpus_unavailable')
        import lab_corpus_executor as corpus
        actor = _corpus_actor(tenant)
        from leaf_platform import project_lifecycle, project_reference_materials
        corpus.require_executor()
        db = platform_link.platform_db()
        with db.cursor() as cur:
            cur.execute('SELECT project_id,name FROM projects WHERE org_id=%s '
                        'AND deleted_at IS NULL ORDER BY name,project_id LIMIT 201', (str(tenant),))
            candidates = cur.fetchall()
        projects = []
        for row in candidates[:200]:
            try:
                project_lifecycle.require_project_role(str(tenant), row['project_id'], actor, write=False)
            except (project_lifecycle.LifecycleForbidden, project_lifecycle.LifecycleUnavailable):
                continue
            projects.append({'project_id': str(row['project_id']), 'name': row['name']})
        materials = []
        if project_id is not None:
            materials = project_reference_materials.list_materials(str(tenant), project_id, actor)['materials']
        return {'projects': projects, 'materials': materials,
                'manifest': lab_tool_manifests.corpus_capability()[0],
                'limited': len(candidates) > 200}
    return _call(operation)


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except ledger.NotFound:
        raise HTTPException(404, detail="lab_run_not_found") from None
    except store.SubmissionConflict as error:
        raise HTTPException(409, detail=error.code) from None
    except TooLarge:
        raise HTTPException(409, detail="lab_run_queue_full") from None
    except (GraphRefused, InvalidGraph) as error:
        raise HTTPException(422, detail=error.code) from None
    except Exception:
        raise HTTPException(503, detail="lab_store_unavailable") from None


def _create(tenant, body):
    if "submission_id" in body and body["submission_id"] is None:
        raise InvalidGraph("invalid_lab_submission_id")
    if (not isinstance(body["doc_id"], str) or not body["doc_id"]
            or type(body["graph_revision"]) is not int or body["graph_revision"] < 1):
        raise InvalidGraph("invalid_lab_run_fields")
    artifact = ledger.get(str(tenant), body["doc_id"], body["graph_revision"])
    manifests = admit_graph(artifact["graph"], lab_tool_manifests.registry_for(str(tenant)))
    corpus_authority = None
    for block in artifact["graph"]["blocks"]:
        manifest = manifests[block["id"]]
        ref = block["registry"]
        trusted = lab_tool_manifests.trusted_for(str(tenant), ref["id"], ref["version"])
        if manifest['id'] == 'corpus-evaluator' and lab_tool_nodes.enabled():
            if store.store_mode() != 'postgres':
                raise GraphRefused('lab_corpus_postgres_required')
            import lab_corpus_executor as corpus
            actor = _corpus_actor(tenant)
            context = _corpus_context(tenant, actor, block['settings'])
            corpus.require_executor()
            from leaf_platform import project_lifecycle
            try:
                corpus.read_sources(context)
            except (ValueError, project_lifecycle.LifecycleForbidden, project_lifecycle.LifecycleUnavailable):
                raise GraphRefused('lab_corpus_material_refused') from None
            authority = {'schema': 'leaf.lab-corpus-authority.v1', 'tenant_id': str(tenant),
                'project_id': context['project_id'], 'actor_id': actor,
                'doc_id': body['doc_id'], 'graph_revision': body['graph_revision'],
                'graph_sha256': artifact['sha256']}
            if corpus_authority is not None and corpus_authority != authority:
                raise GraphRefused('lab_corpus_project_conflict')
            corpus_authority = authority
            continue
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
                       graph_digest(corpus_authority or {}), "lab-worker.v1", _source_revision(os.environ) or "unknown",
                       registry_digest=store.registry_digest(),
                       submission_id=body.get("submission_id"), authority=_actor(tenant),
                       reconcile_only=body.get("reconcile_only", False), corpus_authority=corpus_authority)
    return {"run_id": row["run_id"], "state": row["state"]}


@router.post("", status_code=202)
async def create(request: Request, tenant=Depends(deps.require_tenant)):
    _admit(tenant, "build")
    body = await _body(request, ("doc_id", "graph_revision"), optional=("submission_id", "reconcile_only"))
    return await run_in_threadpool(_call, _create, tenant, body)


@router.get("/{run_id}")
def get(run_id: str, tenant=Depends(deps.require_tenant)):
    _admit(tenant, "build")
    row = _call(store.get, str(tenant), run_id)
    _call(_authorize_corpus_run, tenant, row)
    return {key: value for key, value in row.items() if key not in {"lease_owner", "lease_fence", 'corpus_authority_json'}}


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
    _call(_authorize_corpus_run, tenant, _call(store.get, str(tenant), run_id))
    rows = _call(store.list_events, str(tenant), run_id, after_seq=after_seq, limit=limit)
    return {"events": [_public_event(event) for event in rows]}


@router.post("/{run_id}/cancel")
def cancel(run_id: str, tenant=Depends(deps.require_tenant)):
    _admit(tenant, "build")
    _call(_authorize_corpus_run, tenant, _call(store.get, str(tenant), run_id), write=True)
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
