"""Session checkpoint routes.

Creating records the current drawing head and transcript sequence. Restoring
copies the recorded drawing version forward and records a durable audit
boundary. Prior-context truncation at that boundary is not yet enforced.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import checkpoints
import deps
import session_store
import write_loop
from routers import drawings
import turn_runner
from envelopes import ErrorCode, error_response, with_envelope_fields

router = APIRouter()
MAX_LABEL_LENGTH = 200


class CreateCheckpointRequest(BaseModel):
    label: Optional[str] = None


def _session_not_found(session_id: str) -> JSONResponse:
    return error_response(
        ErrorCode.SESSION_NOT_FOUND,
        f"unknown session_id {session_id!r}",
        retryable=False,
    )


def _require_owned_session(session_id: str, tenant: Any):
    session = session_store.get_session(session_id)
    if session is None or str(session.get("tenant_id")) != str(tenant):
        return None
    return session


def store_authority_mode() -> str:
    """The mutable-drawing authority (da/store.py authority_mode). Imported
    lazily for the same reason _drawing_version does: the store package is not
    importable at module scope here."""
    import store
    return store.authority_mode()


def _drawing_version(tenant_id: str, drawing_id: str) -> int:
    """Read the drawing store's current head version without mutating it."""
    import store

    backend = write_loop.backend_for_tenant(tenant_id, aps_live=False, da=None)
    manifest = store.load_manifest(backend, tenant_id, drawing_id)
    return int(manifest["head"])


@router.post("/api/sessions/{session_id}/checkpoints")
def create_checkpoint(session_id: str, req: CreateCheckpointRequest,
                      tenant=Depends(deps.require_active_tenant)):
    session = _require_owned_session(session_id, tenant)
    if session is None:
        return _session_not_found(session_id)
    if req.label is not None and len(req.label) > MAX_LABEL_LENGTH:
        return error_response(
            ErrorCode.BAD_PARAMS,
            f"checkpoint label must be at most {MAX_LABEL_LENGTH} characters",
            retryable=False,
            status_code=400,
        )

    try:
        drawing_version = _drawing_version(str(tenant), session["drawing_id"])
    except (KeyError, ValueError):
        return error_response(
            ErrorCode.BAD_PARAMS,
            f"drawing {session['drawing_id']!r} is unavailable",
            retryable=False,
            status_code=404,
        )

    checkpoint = checkpoints.create_checkpoint(
        session_id,
        str(tenant),
        session["drawing_id"],
        drawing_version,
        int(session["last_seq"]),
        req.label,
    )
    if checkpoint is None:
        return error_response(
            ErrorCode.BAD_PARAMS,
            "a session may have at most 50 checkpoints",
            retryable=False,
            status_code=409,
        )
    return JSONResponse(
        status_code=201,
        content=deps.tenant_echo(with_envelope_fields(checkpoint), tenant),
    )


@router.get("/api/sessions/{session_id}/checkpoints")
def list_checkpoints(session_id: str, tenant=Depends(deps.require_active_tenant)):
    if _require_owned_session(session_id, tenant) is None:
        return _session_not_found(session_id)
    body = {"checkpoints": checkpoints.list_checkpoints(session_id, str(tenant))}
    return deps.tenant_echo(with_envelope_fields(body), tenant)


@router.post("/api/sessions/{session_id}/checkpoints/{checkpoint_id}/restore")
def restore_checkpoint(session_id: str, checkpoint_id: str,
                       tenant=Depends(deps.require_active_tenant)):
    """Restore a checkpointed drawing by copy-forward and record an audit event.

    The transcript remains append-only. ``checkpoint_restored`` records the
    boundary as a durable audit event. Prior-context truncation at that
    boundary is not yet enforced, so this route does not change turn context.

    The stores are separate, so this is ordered rather than cross-store atomic:
    validate all state, copy the drawing forward, then append the audit event.
    If the final append fails, the committed drawing restore is still reported
    with ``event_recorded: false``.
    """
    session = _require_owned_session(session_id, tenant)
    if session is None:
        return _session_not_found(session_id)
    checkpoint = checkpoints.get_checkpoint(session_id, str(tenant), checkpoint_id)
    if checkpoint is None:
        return _session_not_found(session_id)
    restore_turn_id = f"restore-{checkpoint_id}"
    # A restore occupies the turn slot so a turn cannot begin between this
    # route's validation and its drawing commit. It is not a turn, so it emits
    # no turn_started or turn_complete events.
    if not session_store.try_begin_turn(session_id, restore_turn_id,
                                        turn_runner.turn_max_s()):
        active = session_store.get_session(session_id)
        active_turn_id = active.get("active_turn_id") if active is not None else None
        return error_response(
            ErrorCode.TURN_IN_PROGRESS,
            f"cannot restore while turn {active_turn_id!r} is in progress; cancel it first",
            retryable=False,
            status_code=409,
        )

    # The PostgreSQL drawing authority requires an ACTIVE CHECKOUT to publish a
    # version and explicitly refuses an anonymous holder (da/store.py: "a writer
    # that names no session may not ..."). A checkpoint restore names no
    # session, so under that authority it can only ever produce a confusing 409
    # ("an active checkout is required") or 403 ("checked out by ...").
    #
    # WHO holds the checkout for a restore is a product decision, not a defect
    # to paper over: taking one would mean seizing a lock a user may hold. So
    # this refuses UP FRONT, honestly, and names the follow-up (PR #310 review
    # round 2, blocker 3). The legacy authority is unaffected.
    if store_authority_mode() == "postgres":
        return error_response(
            ErrorCode.BAD_PARAMS,
            "checkpoint restore is not yet supported under the PostgreSQL "
            "drawing authority: publishing a version there requires an active "
            "checkout, and a restore holds none. Restore the drawing through "
            "the drawings API with a checkout instead.",
            retryable=False, status_code=409,
        )

    try:
        try:
            restored = drawings.restore_drawing_version(
                str(tenant), checkpoint["drawing_id"], checkpoint["drawing_version"],
                actor="checkpoint_restore")
        except drawings.RestoreMutationsDisabled:
            return error_response(
                ErrorCode.INTERNAL,
                "drawing mutations are temporarily disabled for a storage cutover",
                retryable=True,
                status_code=503,
            )
        except drawings.RestoreCheckoutDenied as exc:
            return error_response(ErrorCode.BAD_PARAMS, str(exc), retryable=False,
                                  status_code=403)
        except write_loop.ProofStateUnreadable as exc:
            return error_response(ErrorCode.INTERNAL, str(exc), retryable=True,
                                  status_code=503)
        except (drawings.RestoreSourceUnavailable,
                drawings.RestoreSourceUnreadable,
                drawings.RestoreDrawingUnavailable,
                drawings.RestoreCommitRejected,
                OSError) as exc:
            return error_response(
                ErrorCode.BAD_PARAMS,
                f"checkpoint drawing version is unavailable: {exc}",
                retryable=False,
                status_code=409,
            )

        event_recorded = True
        event_seq = None
        try:
            event_seq = session_store.append_event(
                session_id,
                None,
                "checkpoint_restored",
                {
                    "checkpoint_id": checkpoint["checkpoint_id"],
                    "transcript_seq": checkpoint["transcript_seq"],
                    "drawing_version": checkpoint["drawing_version"],
                    "new_drawing_version": restored.version,
                },
            )
        except Exception:  # committed drawing restore must remain visible
            event_recorded = False

        body = {
            **checkpoint,
            "new_drawing_version": restored.version,
            "event_seq": event_seq,
            "event_recorded": event_recorded,
        }
        return deps.tenant_echo(with_envelope_fields(body), tenant)
    finally:
        session_store.end_turn(session_id, restore_turn_id)
        # Releasing the slot is a terminal for queue purposes: a prompt that
        # was parked behind this reservation has no other event coming, and
        # every real terminal path kicks (PR #310 review round 2, finding 2).
        # _kick_queued never raises.
        turn_runner._kick_queued(session_id)
