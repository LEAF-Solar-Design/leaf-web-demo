"""T1 overlay routes: propose a preview, decide it, read the live document.

EVERY route is TENANT-SCOPED, and that is a security boundary rather than a
convenience. Propose binds the named session to the caller; decide and revoke
pass the caller's tenant INTO the store, which includes it in the locked
lookup. Without that, knowing a proposal id was enough to decide it, and the
mutation landed on the owning tenant's document because the code read the
tenant off the proposal row instead of off the caller (sol-critic PR #439,
rounds 1 and 2). A foreign id answers exactly like a missing one.

These three are what make the T1 spine reachable. Before them the whole lane
was unreachable code: a registry with no callers, a decision path with no
proposals, an operator card wired to nothing.

THE SPLIT THAT MATTERS. Proposing is cheap and open; deciding is the gate.

  POST /api/overlay/proposals   the requester's own session opens a preview.
                                No confirmation: it writes one session-scoped
                                row, changes nothing another user sees, and
                                expires by lease.
  POST /api/overlay/decisions   the OPERATOR applies or refuses it. This is the
                                irreversible half, so it carries the CAS
                                witness the card was rendered against and an
                                idempotency key, and it writes an audit row.
  GET  /api/overlay             what this session should be rendering right
                                now: the tenant document plus its own pending
                                preview, already resolved.

WHY THE DECIDE ROUTE TAKES BOTH `approve` AND `document_version`. An operator
tap that omitted the version could land against a tenant state they were never
shown, which is exactly the defect a review found in `deny()`. The route does
not default it: a caller that cannot say which version it saw has no business
deciding.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

import deps
import overlay_propose
import overlay_registry
import overlay_stream
import platform_link
import session_store
from annotation_source_authority import SOURCE_AUTHORITY, source_request
from envelopes import ErrorCode, error_obj

router = APIRouter()


def _fail(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        # The overlay reason rides in the message because BAD_PARAMS is the
        # envelope code the client classifies on. retryable=False because every
        # refusal here needs a changed request, not a retry.
        content={"error": error_obj(ErrorCode.BAD_PARAMS, f"{code}: {message}",
                                    False),
                 "degraded_mode": False},
    )


def _store():
    """The platform overlay store, through platform_link's package alias.

    `from platform import overlay_store` loses to the STDLIB platform module
    whenever site-packages precedes the repo root on sys.path — exactly the
    container layout, where it 500'd every request (found by a live staging
    chat turn, #440). Loading by FILE LOCATION is the fix, and platform_link
    already does it under `leaf_platform`; going through it keeps ONE alias in
    the process. Registering a second one here loaded the package twice — two
    `db` modules and two connection pools (sol-critic PR #439 round 6).
    """
    import platform_link  # noqa: PLC0415
    return platform_link.overlay_store()


def _require_project_session(session_id: str, tenant: Any, *, write: bool):
    return platform_link.require_project_session_access(
        session_store.get_session(session_id), tenant, write=write,
    )


def _recheck_proposal_session(session_id: str, tenant: Any):
    """Recheck live project authority while preserving durable legacy decisions."""
    session = session_store.get_session(session_id)
    if session is None:
        # Existing overlay decisions survive deletion of their requester
        # transcript.  There is no live project session left to authorize.
        return True
    return platform_link.require_project_session_access(
        session, tenant, write=True,
    )


class ProposeBody(BaseModel):
    tokens: Dict[str, str] = Field(..., description="token id -> requested value")
    request_text: str = ""
    session_id: str


class DecideBody(BaseModel):
    proposal_id: str
    approve: bool
    decision_key: str = Field(..., min_length=8)
    document_version: int


@router.post("/api/overlay/proposals")
def propose_overlay(body: ProposeBody, tenant=Depends(deps.require_tenant)) -> Any:
    """Open a pending preview for one session."""
    # Validate the session BEFORE any write. append_event raises KeyError for
    # an unknown session, and by the time publish() runs the proposal row is
    # already committed — the caller would get an error for a proposal that
    # exists, then retry into pending_proposal_exists and be stuck. Refusing
    # up front means either everything happens or nothing does.
    #
    # The session must ALSO belong to the calling tenant. Existence alone let a
    # caller name ANOTHER tenant's session: the proposal row is written under
    # the caller's tenant but keyed to the foreign session, and the
    # overlay_proposed announce lands in that session's transcript — a
    # cross-tenant write and a foreign card, repeatable across guessed session
    # ids. get_session's own docstring states the contract this now honours:
    # callers compare tenant_id and answer 404-not-403, so a prober cannot
    # distinguish "does not exist" from "is not yours".
    try:
        session = _require_project_session(body.session_id, tenant, write=True)
    except platform_link.ProjectSessionForbidden:
        return _fail("project_forbidden", "current project role cannot propose an overlay", 403)
    if session is None:
        return _fail("session_not_found",
                     f"no such session {body.session_id!r}", 404)

    store = _store()
    try:
        out = overlay_propose.propose(
            tenant_id=str(tenant),
            session_id=body.session_id,
            requested_tokens=body.tokens,
            request_text=body.request_text,
            store=store,
            current_document=store.document(str(tenant)),
            defaults=overlay_registry.defaults(),
            # THE "broadcaster" IS the durable store. This app has no
            # in-memory fan-out: GET /api/sessions/{id}/stream polls
            # events_after on the transcript, so appending here is what makes
            # the event arrive over SSE within one poll tick — and the same
            # append is the replay source for a client that reconnects.
            # `broadcast` stays None because there is nothing else to push to;
            # publish() still rewrites the envelope with the durable seq.
            append_event=session_store.append_event,
        )
    except overlay_propose.OverlayProposeError as exc:
        return _fail(exc.code, exc.detail or exc.code, exc.status_code)
    except Exception as exc:  # noqa: BLE001 - store raises its own error type
        code = getattr(exc, "code", "overlay_unavailable")
        return _fail(code, str(getattr(exc, "detail", exc))[:200],
                     int(getattr(exc, "status_code", 409)))

    proposal = out["proposal"]
    return {
        "proposal_id": proposal["proposal_id"],
        "tokens": out["tokens"],
        "expires_at": str(proposal.get("lease_expires_at") or ""),
        # The witness the operator card must send back. Handed over here so the
        # card never has to guess which version it is deciding against.
        "document_version": int(store.document(str(tenant)).get("version", 0) or 0),
        "error": None,
        "degraded_mode": False,
    }


@router.post("/api/overlay/decisions")
def decide_overlay(body: DecideBody,
                   x_actor: Optional[str] = Header(default=None, alias="X-Actor"),
                   tenant=Depends(deps.require_tenant)) -> Any:
    """Apply or refuse a pending preview. The operator's single tap."""
    actor = (x_actor or "").strip()
    if not actor:
        # Every runtime mutation must be attributable. The decision path
        # refuses a blank actor too; failing here gives the caller a usable
        # message instead of a 500 from deeper down.
        return _fail("actor_required", "X-Actor is required to decide", 400)

    store = _store()
    proposal = store.latest_proposal(body.proposal_id, str(tenant))
    if proposal is not None:
        try:
            session = _recheck_proposal_session(
                str(proposal.get("session_id") or ""), tenant,
            )
        except platform_link.ProjectSessionForbidden:
            return _fail("project_forbidden", "current project role cannot decide an overlay", 403)
        if session is None:
            return _fail("proposal_not_found", body.proposal_id, 404)
    try:
        if body.approve:
            proposal, document = store.approve(
                proposal_id=body.proposal_id, tenant_id=str(tenant),
                actor=actor, decision_key=body.decision_key,
                expected_version=body.document_version)
        else:
            proposal = store.deny(
                proposal_id=body.proposal_id, tenant_id=str(tenant),
                actor=actor, decision_key=body.decision_key,
                expected_version=body.document_version)
            document = store.document(str(tenant))
    except Exception as exc:  # noqa: BLE001 - OverlayStoreError carries the code
        code = getattr(exc, "code", "decision_failed")
        return _fail(code, str(getattr(exc, "detail", exc))[:200],
                     int(getattr(exc, "status_code", 409)))

    # Announce AFTER the decision committed, into the requester's transcript —
    # this is what clears their card within one SSE poll tick instead of on
    # their next full read. Announce failure must not fail the decision: the
    # state is already durable in overlay_proposals, and the client's read
    # path (GET /api/overlay) reaches the same truth without the event. The
    # narrow except is deliberate: KeyError = the requester's session is gone
    # (nobody is left to notify), StreamContractError = a store misreporting
    # its seq; anything else is a real bug and should surface.
    try:
        overlay_stream.publish(
            overlay_stream.decided_event(
                session_id=str(proposal.get("session_id") or ""),
                seq=0,  # placeholder; publish() rewrites with the durable seq
                proposal_id=proposal["proposal_id"],
                state=proposal["state"],
                document_version=int(document.get("version", 0) or 0),
            ),
            append_event=session_store.append_event,
        )
    except (KeyError, overlay_stream.StreamContractError):
        pass

    return {
        "proposal_id": proposal["proposal_id"],
        "state": proposal["state"],
        "document_version": int(document.get("version", 0) or 0),
        "error": None,
        "degraded_mode": False,
    }


class RevokeBody(BaseModel):
    proposal_id: str
    decision_key: str = Field(..., min_length=8)
    document_version: int


class _ClosedBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AnnotationPreviewBody(_ClosedBody):
    session_id: str
    batch_id: str
    base_version: int = Field(..., ge=0)
    base_commit: str
    base_tree: str
    preview_commit: str
    preview_tree: str
    payload_digest: str
    payload_count: int = Field(..., ge=1)
    request_key: str = Field(..., min_length=8)
    lease_s: int = Field(default=900, ge=1, le=3600)


class AnnotationDecisionBody(_ClosedBody):
    decision_key: str = Field(..., min_length=8)


class AnnotationRetryBody(_ClosedBody):
    request_key: str = Field(..., min_length=8)
    lease_s: int = Field(default=900, ge=1, le=3600)


class AnnotationUndoBody(AnnotationRetryBody):
    pass


def _annotation_store():
    platform_link._ensure_platform_package()
    from leaf_platform import annotation_store  # noqa: PLC0415
    return annotation_store


def _annotation_not_found() -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "annotation unavailable"})


def _annotation_unavailable() -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": "annotation unavailable"})


def _annotation_scope(session_id: str, tenant: Any) -> Dict[str, str]:
    session = _require_project_session(session_id, tenant, write=True)
    if session is None:
        raise LookupError("annotation unavailable")
    org_id = str(session.get("org_id") or "")
    project_id = str(session.get("project_id") or "")
    drawing_id = str(session.get("drawing_id") or "")
    subject = getattr(tenant, "subject", None)
    if org_id != str(tenant) or not project_id or not drawing_id or not subject:
        raise LookupError("annotation unavailable")
    store = platform_link.platform_store()
    binding = store.resolve_active_identity_binding("auth0", subject)
    if binding is None or str(binding.platform_tenant_id) != str(tenant):
        raise LookupError("annotation unavailable")
    mapping = store.resolve_project_repository_authority(
        str(tenant), org_id, project_id,
    )
    repository_id = str((mapping or {}).get("repo_key") or "")
    if not repository_id:
        raise LookupError("annotation unavailable")
    return {
        "tenant_id": str(tenant), "org_id": org_id, "project_id": project_id,
        "drawing_id": drawing_id, "session_id": session_id,
        "actor_binding_id": str(binding.binding_id),
        "repository_id": repository_id,
    }


def _batch_scope(batch_id: str, tenant: Any) -> tuple[Dict[str, Any], Dict[str, str]]:
    batch = _annotation_store().latest_batch(batch_id, str(tenant))
    if batch is None:
        raise LookupError("annotation unavailable")
    scope = _annotation_scope(str(batch.get("session_id") or ""), tenant)
    for field in ("tenant_id", "org_id", "project_id", "drawing_id"):
        if str(batch.get(field)) != scope[field]:
            raise LookupError("annotation unavailable")
    if str(batch.get("repository_id")) != scope["repository_id"]:
        raise LookupError("annotation unavailable")
    return batch, scope


def _source_receipt(scope: Dict[str, str], *, relation: str,
                    commit_sha: str, tree_sha: str,
                    base_commit: str, base_tree: str,
                    reverses_commit: Optional[str] = None,
                    reverses_tree: Optional[str] = None):
    return SOURCE_AUTHORITY.verify(source_request(
        tenant_id=scope["tenant_id"], org_id=scope["org_id"],
        project_id=scope["project_id"], drawing_id=scope["drawing_id"],
        repository_id=scope["repository_id"], relation=relation,
        commit_sha=commit_sha, tree_sha=tree_sha,
        base_commit=base_commit, base_tree=base_tree,
        reverses_commit=reverses_commit, reverses_tree=reverses_tree,
    ))


def _append_annotation_event(session_id: str, event_type: str,
                             batch: Dict[str, Any]) -> None:
    session_store.append_event(session_id, None, event_type, {
        "batch_id": str(batch["batch_id"]),
        "revision": int(batch["revision"]),
        "state": str(batch["state"]),
        "source_receipt_digest": str(batch["source_receipt_digest"]),
        "payload_digest": str(batch["payload_digest"]),
        "preview_commit": str(batch["preview_commit"]),
        "preview_tree": str(batch["preview_tree"]),
    })


def _annotation_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, (LookupError, ValueError, platform_link.ProjectSessionForbidden)):
        return _annotation_not_found()
    status = int(getattr(exc, "status_code", 503))
    if status in {400, 403, 404, 409, 410, 422}:
        return _annotation_not_found()
    return _annotation_unavailable()


def _annotation_decision_copy(batch: Dict[str, Any]) -> str:
    count = int(batch["payload_count"])
    noun = "change" if count == 1 else "changes"
    state = str(batch["state"])
    kind = str(batch["kind"])
    if state == "pending":
        prefix = "Review undo for" if kind == "undo" else "Review"
        return f"{prefix} {count} annotation {noun}."
    if state == "accepted":
        prefix = "Undid" if kind == "undo" else "Applied"
        return f"{prefix} {count} annotation {noun}."
    if state == "rejected":
        return f"Rejected {count} annotation {noun}."
    return "This annotation proposal is no longer current."


def _annotation_projection(batch: Dict[str, Any],
                           target: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Closed browser projection with server-authored copy and Git witnesses."""
    if target is None:
        target_version = batch["target_version"]
        target_commit = batch["target_commit"]
        target_tree = batch["target_tree"]
    else:
        target_version = target["version"]
        target_commit = target["commit_sha"]
        target_tree = target["tree_sha"]
    return {
        "decision_copy": _annotation_decision_copy(batch),
        "batch_id": str(batch["batch_id"]),
        "revision": int(batch["revision"]),
        "state": str(batch["state"]),
        "kind": str(batch["kind"]),
        "payload_digest": str(batch["payload_digest"]),
        "payload_count": int(batch["payload_count"]),
        "base_version": int(batch["base_version"]),
        "base_commit": str(batch["base_commit"]),
        "base_tree": str(batch["base_tree"]),
        "preview_commit": str(batch["preview_commit"]),
        "preview_tree": str(batch["preview_tree"]),
        "retry_of_batch_id": (
            str(batch["retry_of_batch_id"]) if batch.get("retry_of_batch_id") else None
        ),
        "reverses_batch_id": (
            str(batch["reverses_batch_id"]) if batch.get("reverses_batch_id") else None
        ),
        "reverses_commit": (
            str(batch["reverses_commit"]) if batch.get("reverses_commit") else None
        ),
        "reverses_tree": (
            str(batch["reverses_tree"]) if batch.get("reverses_tree") else None
        ),
        "applied_version": (
            int(batch["applied_version"]) if batch.get("applied_version") is not None else None
        ),
        "target_version": int(target_version),
        "target_commit": str(target_commit),
        "target_tree": str(target_tree),
    }


@router.get("/api/overlay/annotations/current")
def read_current_annotation(session_id: str,
                            tenant=Depends(deps.require_active_tenant)) -> Any:
    try:
        scope = _annotation_scope(session_id, tenant)
        current = _annotation_store().current_annotation(
            tenant_id=scope["tenant_id"], org_id=scope["org_id"],
            project_id=scope["project_id"], drawing_id=scope["drawing_id"],
            session_id=scope["session_id"], repository_id=scope["repository_id"],
        )
        if current is None:
            return Response(status_code=204)
        return _annotation_projection(current)
    except Exception as exc:  # noqa: BLE001 - scope failures stay indistinguishable
        return _annotation_error(exc)


@router.post("/api/overlay/annotations/preview")
def preview_annotation(body: AnnotationPreviewBody,
                       tenant=Depends(deps.require_active_tenant)) -> Any:
    """Create one source-verified annotation preview for an owned session."""
    try:
        scope = _annotation_scope(body.session_id, tenant)
        store = _annotation_store()
        target = store.target(
            scope["tenant_id"], scope["org_id"], scope["project_id"],
            scope["drawing_id"],
        )
        if target is None or (
            int(target["version"]) != body.base_version
            or target["commit_sha"] != body.base_commit
            or target["tree_sha"] != body.base_tree
            or str(target["repository_id"]) != scope["repository_id"]
        ):
            raise LookupError("annotation unavailable")
        receipt = _source_receipt(
            scope, relation="preview", commit_sha=body.preview_commit,
            tree_sha=body.preview_tree, base_commit=body.base_commit,
            base_tree=body.base_tree,
        )
        batch = store.create_batch(
            batch_id=body.batch_id, tenant_id=scope["tenant_id"],
            org_id=scope["org_id"], project_id=scope["project_id"],
            drawing_id=scope["drawing_id"], session_id=scope["session_id"],
            kind="apply", base_version=body.base_version,
            base_commit=body.base_commit, base_tree=body.base_tree,
            preview_commit=body.preview_commit, preview_tree=body.preview_tree,
            payload_digest=body.payload_digest, payload_count=body.payload_count,
            request_key=body.request_key,
            created_by_binding_id=scope["actor_binding_id"],
            source_authority=SOURCE_AUTHORITY, source_receipt=receipt,
            lease_s=body.lease_s,
        )
        _append_annotation_event(scope["session_id"], "annotation_previewed", batch)
        return _annotation_projection(batch, target)
    except Exception as exc:  # noqa: BLE001 - all authority failures are closed
        return _annotation_error(exc)


def _decision(batch_id: str, body: AnnotationDecisionBody,
              tenant: Any, *, accept: bool) -> Any:
    try:
        batch, scope = _batch_scope(batch_id, tenant)
        receipt = _source_receipt(
            scope, relation="inverse" if batch["kind"] == "undo" else "preview",
            commit_sha=batch["preview_commit"], tree_sha=batch["preview_tree"],
            base_commit=batch["base_commit"], base_tree=batch["base_tree"],
            reverses_commit=batch.get("reverses_commit"),
            reverses_tree=batch.get("reverses_tree"),
        )
        store = _annotation_store()
        if accept:
            decided, target = store.accept(
                batch_id=batch_id, tenant_id=scope["tenant_id"],
                actor_binding_id=scope["actor_binding_id"],
                decision_key=body.decision_key, source_authority=SOURCE_AUTHORITY,
                source_receipt=receipt,
            )
            event = "annotation_accepted"
        else:
            decided = store.reject(
                batch_id=batch_id, tenant_id=scope["tenant_id"],
                actor_binding_id=scope["actor_binding_id"],
                decision_key=body.decision_key,
            )
            target = store.target(
                scope["tenant_id"], scope["org_id"], scope["project_id"],
                scope["drawing_id"],
            )
            if target is None:
                raise LookupError("annotation unavailable")
            event = "annotation_rejected"
        _append_annotation_event(scope["session_id"], event, decided)
        return _annotation_projection(decided, target)
    except Exception as exc:  # noqa: BLE001
        return _annotation_error(exc)


@router.post("/api/overlay/annotations/{batch_id}/accept")
def accept_annotation(batch_id: str, body: AnnotationDecisionBody,
                      tenant=Depends(deps.require_active_tenant)) -> Any:
    return _decision(batch_id, body, tenant, accept=True)


@router.post("/api/overlay/annotations/{batch_id}/reject")
def reject_annotation(batch_id: str, body: AnnotationDecisionBody,
                      tenant=Depends(deps.require_active_tenant)) -> Any:
    return _decision(batch_id, body, tenant, accept=False)


def _inverse_payload_digest(source: Dict[str, Any]) -> str:
    payload = {
        "kind": "annotation_inverse_v1",
        "source_batch_id": str(source["batch_id"]),
        "source_payload_digest": str(source["payload_digest"]),
        "preview_commit": str(source["base_commit"]),
        "preview_tree": str(source["base_tree"]),
        "reverses_commit": str(source["preview_commit"]),
        "reverses_tree": str(source["preview_tree"]),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _linked_preview(source_batch_id: str, body: AnnotationRetryBody,
                    tenant: Any, *, undo: bool) -> Any:
    try:
        source, scope = _batch_scope(source_batch_id, tenant)
        store = _annotation_store()
        target = store.target(
            scope["tenant_id"], scope["org_id"], scope["project_id"],
            scope["drawing_id"],
        )
        if target is None:
            raise LookupError("annotation unavailable")
        batch_id = str(uuid.uuid4())
        preview_commit = source["base_commit"] if undo else source["preview_commit"]
        preview_tree = source["base_tree"] if undo else source["preview_tree"]
        payload_digest = (
            _inverse_payload_digest(source) if undo else source["payload_digest"]
        )
        relation = "inverse" if undo else "preview"
        receipt = _source_receipt(
            scope, relation=relation, commit_sha=preview_commit,
            tree_sha=preview_tree, base_commit=target["commit_sha"],
            base_tree=target["tree_sha"],
            reverses_commit=source["preview_commit"] if undo else None,
            reverses_tree=source["preview_tree"] if undo else None,
        )
        batch = store.create_batch(
            batch_id=batch_id, tenant_id=scope["tenant_id"],
            org_id=scope["org_id"], project_id=scope["project_id"],
            drawing_id=scope["drawing_id"], session_id=scope["session_id"],
            kind="undo" if undo else "apply",
            base_version=int(target["version"]), base_commit=target["commit_sha"],
            base_tree=target["tree_sha"], preview_commit=preview_commit,
            preview_tree=preview_tree, payload_digest=payload_digest,
            payload_count=int(source["payload_count"]), request_key=body.request_key,
            created_by_binding_id=scope["actor_binding_id"],
            source_authority=SOURCE_AUTHORITY, source_receipt=receipt,
            lease_s=body.lease_s,
            retry_of_batch_id=None if undo else source_batch_id,
            reverses_batch_id=source_batch_id if undo else None,
        )
        _append_annotation_event(
            scope["session_id"], "annotation_undo_previewed" if undo
            else "annotation_retry_previewed", batch,
        )
        return _annotation_projection(batch, target)
    except Exception as exc:  # noqa: BLE001
        return _annotation_error(exc)


@router.post("/api/overlay/annotations/{batch_id}/retry")
def retry_annotation(batch_id: str, body: AnnotationRetryBody,
                     tenant=Depends(deps.require_active_tenant)) -> Any:
    return _linked_preview(batch_id, body, tenant, undo=False)


@router.post("/api/overlay/annotations/{batch_id}/undo")
def undo_annotation(batch_id: str, body: AnnotationUndoBody,
                    tenant=Depends(deps.require_active_tenant)) -> Any:
    return _linked_preview(batch_id, body, tenant, undo=True)


@router.post("/api/overlay/revocations")
def revoke_overlay(body: RevokeBody,
                   x_actor: Optional[str] = Header(default=None, alias="X-Actor"),
                   tenant=Depends(deps.require_tenant)) -> Any:
    """Withdraw an APPROVED overlay. The operator's undo.

    Same discipline as deciding, for the same reasons: an actor (a runtime
    mutation must be attributable), a decision_key (a retry returns the
    original outcome, a replay is refused), and the CAS witness (withdrawing
    against a version the operator never saw is the deny() defect again).

    The announce is the one the whole stream contract was built around:
    overlay_revoked is the event a user must never miss, because missing it
    leaves a withdrawn theme on screen. It carries token IDS and the new
    document version, never values — the client re-reads rather than patching,
    so a missed event costs latency, not correctness.
    """
    actor = (x_actor or "").strip()
    if not actor:
        return _fail("actor_required", "X-Actor is required to revoke", 400)

    store = _store()
    proposal = store.latest_proposal(body.proposal_id, str(tenant))
    if proposal is not None:
        try:
            session = _recheck_proposal_session(
                str(proposal.get("session_id") or ""), tenant,
            )
        except platform_link.ProjectSessionForbidden:
            return _fail("project_forbidden", "current project role cannot revoke an overlay", 403)
        if session is None:
            return _fail("proposal_not_found", body.proposal_id, 404)
    try:
        proposal, document = store.revert(
            proposal_id=body.proposal_id, tenant_id=str(tenant),
            actor=actor, decision_key=body.decision_key,
            expected_version=body.document_version)
    except Exception as exc:  # noqa: BLE001 - OverlayStoreError carries the code
        code = getattr(exc, "code", "revoke_failed")
        return _fail(code, str(getattr(exc, "detail", exc))[:200],
                     int(getattr(exc, "status_code", 409)))

    # Durable state first, announce second — identical to decide, and the same
    # narrow except: a vanished requester session must not fail the operator's
    # revoke, and the read path reaches the same truth without the event.
    try:
        overlay_stream.publish(
            overlay_stream.revoked_event(
                session_id=str(proposal.get("session_id") or ""),
                seq=0,  # placeholder; publish() rewrites with the durable seq
                proposal_id=proposal["proposal_id"],
                tokens=list((proposal.get("tokens") or {}).keys()),
                document_version=int(document.get("version", 0) or 0),
                reason="operator_reverted",
            ),
            append_event=session_store.append_event,
        )
    except (KeyError, overlay_stream.StreamContractError):
        pass

    return {
        "proposal_id": proposal["proposal_id"],
        "state": proposal["state"],
        "document_version": int(document.get("version", 0) or 0),
        "error": None,
        "degraded_mode": False,
    }


@router.get("/api/overlay")
def read_overlay(session_id: Optional[str] = None,
                 tenant=Depends(deps.require_tenant)) -> Any:
    """What this session should render right now.

    Returns the RESOLVED token map (tenant document with the session's own
    pending preview layered on top) rather than the two separately, so a client
    cannot get the precedence wrong. The version is the tenant document's, not
    the preview's: the preview is not a committed state and has no version to
    decide against.
    """
    if session_id:
        try:
            session = _require_project_session(session_id, tenant, write=False)
        except platform_link.ProjectSessionForbidden:
            return _fail("project_forbidden", "current project role cannot read this overlay", 403)
        if session is None:
            return _fail("session_not_found", f"no such session {session_id!r}", 404)
    store = _store()
    document = store.document(str(tenant))
    tokens = store.effective_tokens(str(tenant), session_id)
    pending = (store.pending_for_session(str(tenant), session_id)
               if session_id else None)
    return {
        "tokens": tokens,
        "document_version": int(document.get("version", 0) or 0),
        "pending_proposal_id": (pending or {}).get("proposal_id"),
        "css": overlay_registry.render_css_vars(tokens),
        "error": None,
        "degraded_mode": False,
    }
