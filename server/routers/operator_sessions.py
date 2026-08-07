"""Operator session surface (contract/OPERATOR.md section 2).

Additive namespace: /api/operator/*. Every route resolves the principal
fresh via require_operator (server-owned grant; 404 no-oracle; 503 when the
store is unavailable). Turn dispatch into the harness OperatorLoop rides
LEAF_OPERATOR_HARNESS_URL; unset, the messages surface is 503 — the write
surface is unavailable rather than silently degraded.
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import operator_deps
from operator_deps import OperatorContext

router = APIRouter()


def _store():
    import operator_session_store
    return operator_session_store


def _wrap_store_errors(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - PG down = 503, never open
        raise HTTPException(
            status_code=503, detail="operator_store_unavailable") from exc


class CreateSessionBody(BaseModel):
    model_config = {"extra": "forbid"}


class MessageBody(BaseModel):
    text: str
    model_config = {"extra": "forbid"}


@router.post("/api/operator/sessions")
def create_session(body: CreateSessionBody = None,
                   op: OperatorContext = Depends(operator_deps.require_operator)) -> Dict[str, Any]:
    return _wrap_store_errors(
        _store().create_or_get_session, op.subject, op.profile,
        op.environment)


@router.get("/api/operator/sessions")
def list_sessions(op: OperatorContext = Depends(operator_deps.require_operator)) -> Dict[str, Any]:
    return {"sessions": _wrap_store_errors(_store().list_sessions, op.subject)}


@router.get("/api/operator/sessions/{session_id}")
def get_session(session_id: str,
                op: OperatorContext = Depends(operator_deps.require_operator)) -> Dict[str, Any]:
    row = _wrap_store_errors(_store().get_session, session_id, op.subject)
    if row is None:
        raise HTTPException(status_code=404,
                            detail="operator_session_not_found")
    return row


@router.get("/api/operator/sessions/{session_id}/events")
def read_events(session_id: str, after_seq: int = 0, limit: int = 200,
                op: OperatorContext = Depends(operator_deps.require_operator)) -> Dict[str, Any]:
    row = _wrap_store_errors(_store().get_session, session_id, op.subject)
    if row is None:
        raise HTTPException(status_code=404,
                            detail="operator_session_not_found")
    events = _wrap_store_errors(
        _store().read_events, session_id, op.subject, after_seq, limit)
    return {"events": events}


def _harness_turn(session_id: str, turn_id: str, op: OperatorContext,
                  text: str) -> Optional[Dict[str, Any]]:
    """Forward the turn to the harness OperatorLoop. Server-attested: the
    operator context rides the body the APP resolved; the harness secret
    gates the hop. Returns None when no harness is configured."""
    base = os.environ.get("LEAF_OPERATOR_HARNESS_URL", "").rstrip("/")
    if not base:
        return None
    payload = json.dumps({
        "sessionId": session_id, "turnId": turn_id, "text": text,
        "operator": {"subject": op.subject, "roleRevision": op.role_revision,
                     "profile": op.profile, "environment": op.environment},
    }).encode("utf-8")
    req = urllib.request.Request(
        base + "/operator/turn", data=payload, method="POST",
        headers={"Content-Type": "application/json",
                 "X-Harness-Secret": os.environ.get("LEAF_HARNESS_SECRET", "")})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


@router.post("/api/operator/sessions/{session_id}/messages")
def post_message(session_id: str, body: MessageBody,
                 op: OperatorContext = Depends(operator_deps.require_operator)) -> Dict[str, Any]:
    store = _store()
    row = _wrap_store_errors(store.get_session, session_id, op.subject)
    if row is None:
        raise HTTPException(status_code=404,
                            detail="operator_session_not_found")
    if not os.environ.get("LEAF_OPERATOR_HARNESS_URL"):
        raise HTTPException(status_code=503,
                            detail="operator_harness_unconfigured")
    turn_id = _wrap_store_errors(
        store.begin_turn, session_id, op.subject, op.role_revision,
        op.profiles, op.environment)
    if turn_id is None:
        raise HTTPException(status_code=409, detail="turn_in_progress")
    _wrap_store_errors(store.append_event, session_id, turn_id,
                       "operator_turn_started", {"subject": op.subject})
    try:
        result = _harness_turn(session_id, turn_id, op, body.text)
        _wrap_store_errors(store.append_event, session_id, turn_id,
                           "operator_turn_complete",
                           {"stop_reason": (result or {}).get(
                               "stopReason", "end_turn")})
        return {"turn_id": turn_id, "status": "complete",
                "result": result}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - harness failure ends the turn
        _wrap_store_errors(store.append_event, session_id, turn_id,
                           "operator_error", {"error": str(exc)[:500]})
        raise HTTPException(status_code=502,
                            detail="operator_harness_error") from exc
    finally:
        _wrap_store_errors(store.end_turn, session_id, turn_id)


@router.get("/api/operator/audit")
def read_audit(limit: int = 100,
               op: OperatorContext = Depends(operator_deps.require_operator)) -> Dict[str, Any]:
    """Own-subject security audit view (Lane B writes the rows)."""
    from operator_principals import _db

    def _read():
        db = _db()
        with db.cursor() as cur:
            cur.execute(
                "SELECT ts, session_id, turn_id, action, decision, reason,"
                " authority_id FROM operator_security_audit"
                " WHERE subject = %s ORDER BY ts DESC LIMIT %s",
                (op.subject, limit))
            return [{"ts": str(r["ts"]), "session_id": r["session_id"],
                     "turn_id": r["turn_id"], "action": r["action"],
                     "decision": r["decision"], "reason": r["reason"],
                     "authority_id": r["authority_id"]}
                    for r in cur.fetchall()]

    return {"audit": _wrap_store_errors(_read)}
