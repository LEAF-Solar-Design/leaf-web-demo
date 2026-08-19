"""Project-bound durable conversation records (0046).

A conversation is a browser project's persistent chat thread. Gated by the
``conv_durable`` flag: while off every route below returns 404 and writes no
row, checked FIRST in every handler, before any store call that could INSERT.

Every route derives its actor's org from the verified tenant through
``platform_link.require_project_access`` -- never from the request body --
and that same call runs on every route including the single-row GET and the
recovery route, not only the list route, so a cross-tenant conversation id
reads exactly like a missing one. The storage layer repeats the
(org_id, project_id) predicate on every query as a second, independent
boundary: a future caller reaching these functions directly, skipping the
router, still reads nothing foreign.

Routing lives in this module rather than under ``server/routers/`` (the
repo's usual home for route handlers) because this card's file budget has no
room for a fifth file.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

import deps
import platform_link
from envelopes import ErrorCode, error_response, with_envelope_fields

SERVER_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = SERVER_DIR.parent

FLAG_CONV_DURABLE = "LEAF_CONV_DURABLE_ENABLED"
MAX_TITLE_LENGTH = 200
TABLE = "conversations"
MESSAGES_TABLE = "conversation_messages"
MESSAGES_IDEMPOTENCY_CONSTRAINT = "conversation_messages_idempotency_unique"
MAX_IDEMPOTENCY_KEY_LENGTH = 200
MAX_MESSAGE_CONTENT_BYTES = 32768
MAX_MESSAGE_METADATA_BYTES = 8192

# The structured-payload extension: a client MAY carry a richer, JSON-encoded
# block inside metadata[_STRUCTURED_METADATA_KEY]. It is intentionally NOT
# parsed before the row is stored -- content and metadata are stored as
# opaque, syntactically-capped values so a durable write never depends on a
# nested payload's semantic validity. Parsing (and therefore poisoning) only
# happens after the row is committed; see ``_post_store_poison_reason``.
_STRUCTURED_METADATA_KEY = "structured"
_QUARANTINE_KIND = "quarantine_receipt"
_QUARANTINE_PLACEHOLDER_CONTENT = (
    "[quarantined: this message's structured payload failed post-store"
    " deserialization; see the receipt's poison_message_id and reason]"
)

router = APIRouter()


def conv_durable_enabled() -> bool:
    """The ``conv_durable`` flag. Off by default; checked before any write."""
    return os.environ.get(FLAG_CONV_DURABLE, "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def platform_db():
    """Load the local platform database package without shadowing stdlib platform.

    Duplicated from session_annex.platform_db() on purpose (its own docstring
    explains why): both copies populate the same sys.modules["leaf_platform"]
    entry, so they share one loaded package at runtime.
    """
    loaded = sys.modules.get("leaf_platform")
    if loaded is None:
        pkg_dir = _PROJECT_ROOT / "platform"
        spec = importlib.util.spec_from_file_location(
            "leaf_platform", pkg_dir / "__init__.py",
            submodule_search_locations=[str(pkg_dir)],
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load the Leaf platform database package")
        loaded = importlib.util.module_from_spec(spec)
        sys.modules["leaf_platform"] = loaded
        spec.loader.exec_module(loaded)
    from leaf_platform import db
    return db


def platform_store():
    """Load leaf_platform.store through the same collision-safe alias."""
    platform_db()  # ensures the leaf_platform package alias is registered first
    from leaf_platform import store
    return store


# --- storage layer --------------------------------------------------------- #

def _row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "conversation_id": str(row["conversation_id"]),
        "org_id": str(row["org_id"]),
        "project_id": str(row["project_id"]),
        "title": row["title"],
        "created_by_binding_id": str(row["created_by_binding_id"]),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


def create_conversation(org_id: Any, project_id: Any, actor_binding_id: Any,
                        title: Optional[str] = None) -> Dict[str, Any]:
    """Insert one project-bound conversation row.

    Callers have already resolved ``org_id``/``project_id`` server-side from a
    verified project membership -- never from a request body.
    """
    db = platform_db()
    conversation_id = uuid.uuid4()
    with db.cursor() as cur:
        cur.execute(
            f"INSERT INTO {TABLE}"
            " (conversation_id, org_id, project_id, title, created_by_binding_id)"
            " VALUES (%s, %s, %s, %s, %s)"
            " RETURNING *",
            (str(conversation_id), str(org_id), str(project_id), title,
             str(actor_binding_id)),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("conversation insert did not return a row")
    return _row(row)


def list_conversations(org_id: Any, project_id: Any) -> List[Dict[str, Any]]:
    """Project AND org scoped -- the storage boundary holds on its own."""
    db = platform_db()
    with db.cursor() as cur:
        cur.execute(
            f"SELECT * FROM {TABLE} WHERE org_id = %s AND project_id = %s"
            " ORDER BY created_at ASC, conversation_id ASC",
            (str(org_id), str(project_id)),
        )
        rows = cur.fetchall()
    return [_row(row) for row in rows]


def get_conversation(org_id: Any, project_id: Any,
                     conversation_id: Any) -> Optional[Dict[str, Any]]:
    """Return one conversation only when org, project, AND id all match."""
    db = platform_db()
    with db.cursor() as cur:
        cur.execute(
            f"SELECT * FROM {TABLE}"
            " WHERE conversation_id = %s AND org_id = %s AND project_id = %s",
            (str(conversation_id), str(org_id), str(project_id)),
        )
        row = cur.fetchone()
    return _row(row) if row is not None else None


def recover_or_create_conversation(org_id: Any, project_id: Any,
                                   actor_binding_id: Any) -> Dict[str, Any]:
    """Return the project's most recent conversation, creating one if none
    exists yet.

    Callers must check ``conv_durable_enabled()`` first: this function
    performs no flag check of its own and will INSERT unconditionally when the
    project has no conversation yet.
    """
    existing = list_conversations(org_id, project_id)
    if existing:
        return existing[-1]
    return create_conversation(org_id, project_id, actor_binding_id)


def rename_conversation(org_id: Any, project_id: Any, conversation_id: Any,
                        title: str) -> Optional[Dict[str, Any]]:
    """Rename an existing conversation.

    An ordinary UPDATE -- no immutability trigger is attached to this table,
    unlike the append-only ``project_lifecycle_receipts`` ledger.
    """
    db = platform_db()
    with db.cursor() as cur:
        cur.execute(
            f"UPDATE {TABLE} SET title = %s, updated_at = NOW()"
            " WHERE conversation_id = %s AND org_id = %s AND project_id = %s"
            " RETURNING *",
            (title, str(conversation_id), str(org_id), str(project_id)),
        )
        row = cur.fetchone()
    return _row(row) if row is not None else None


# --- durable messages: idempotent write + poison quarantine + recovery ----- #
#
# The write path below is the exactly-once boundary for this card: DB-enforced
# (ON CONFLICT against 0048's UNIQUE (org_id, project_id, conversation_id,
# idempotency_key)), never check-then-insert, so two concurrent retries of the
# same key can never both apply -- one wins the INSERT, the other observes the
# conflict and replays the winner's stored row. The whole write runs inside
# ``platform_db().run_transaction``, which wraps ``operation`` in
# ``conn.transaction()``: any exception raised before this function returns,
# including a fault injected mid-write by a caller's retry harness, rolls the
# partial INSERT back completely. A "poison" payload (structured metadata that
# fails to deserialize) is therefore only ever detected AFTER that transaction
# has already committed -- quarantining it is a separate, later write against
# an already-durable row, never a rollback candidate for the original insert.


def _validate_idempotency_key(value: str) -> str:
    value = (value or "").strip()
    if not value or len(value) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise ValueError(
            f"idempotency key must contain 1 to {MAX_IDEMPOTENCY_KEY_LENGTH} characters")
    return value


def _validate_message_content(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("message content must be a string")
    byte_length = len(value.encode("utf-8"))
    if byte_length == 0 or byte_length > MAX_MESSAGE_CONTENT_BYTES:
        raise ValueError(
            f"message content must contain 1 to {MAX_MESSAGE_CONTENT_BYTES} bytes")
    return value


def _validate_message_metadata(value: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("message metadata must be a JSON object")
    try:
        encoded = json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("message metadata must be JSON-serializable") from exc
    if len(encoded.encode("utf-8")) > MAX_MESSAGE_METADATA_BYTES:
        raise ValueError(
            f"message metadata must not exceed {MAX_MESSAGE_METADATA_BYTES} bytes")
    return value


def _row_message(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "message_id": str(row["message_id"]),
        "org_id": str(row["org_id"]),
        "project_id": str(row["project_id"]),
        "conversation_id": str(row["conversation_id"]),
        "content": row["content"],
        "metadata": dict(row["metadata"]) if row["metadata"] else {},
        "created_by_binding_id": str(row["created_by_binding_id"]),
        "idempotency_key": row["idempotency_key"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


def _post_store_poison_reason(metadata: Dict[str, Any]) -> Optional[str]:
    """Attempt the deferred, post-commit deserialization step.

    Returns ``None`` when the row is clean, or a short machine reason code
    (never the raw payload -- callers must reference it, not embed it) when it
    is poison.
    """
    if _STRUCTURED_METADATA_KEY not in metadata:
        return None
    raw = metadata[_STRUCTURED_METADATA_KEY]
    if not isinstance(raw, str):
        return "structured_payload_not_a_string"
    try:
        json.loads(raw)
    except (ValueError, TypeError):
        return "structured_payload_invalid_json"
    return None


def _insert_message_row(
    conn: Any, *, message_id: uuid.UUID, org_id: Any, project_id: Any,
    conversation_id: Any, content: str, metadata: Dict[str, Any],
    actor_binding_id: Any, idempotency_key: str,
) -> Dict[str, Any]:
    """INSERT ... ON CONFLICT DO NOTHING, replaying the stored row on a hit.

    Runs inside the caller's transaction. The unique constraint is the sole
    arbiter of "first" under concurrency: a losing conflicting writer never
    inserts a second row, it only ever reads back the winner's.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {MESSAGES_TABLE}"
            " (message_id, org_id, project_id, conversation_id, content,"
            "  metadata, created_by_binding_id, idempotency_key)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
            f" ON CONFLICT ON CONSTRAINT {MESSAGES_IDEMPOTENCY_CONSTRAINT}"
            " DO NOTHING"
            " RETURNING *",
            (str(message_id), str(org_id), str(project_id), str(conversation_id),
             content, Jsonb(metadata), str(actor_binding_id), idempotency_key),
        )
        row = cur.fetchone()
        replayed = row is None
        if replayed:
            cur.execute(
                f"SELECT * FROM {MESSAGES_TABLE}"
                " WHERE org_id = %s AND project_id = %s AND conversation_id = %s"
                " AND idempotency_key = %s",
                (str(org_id), str(project_id), str(conversation_id), idempotency_key),
            )
            row = cur.fetchone()
    if row is None:
        raise RuntimeError("conversation message write did not converge")
    result = _row_message(row)
    result["replayed"] = replayed
    return result


def _write_quarantine_receipt(
    org_id: Any, project_id: Any, conversation_id: Any, actor_binding_id: Any,
    poison_message_id: str, reason: str,
) -> Dict[str, Any]:
    """Insert one companion receipt row for a poisoned message.

    A fresh, independent INSERT -- never an UPDATE of the poisoned row itself
    (nothing here is a mutating ledger, so nothing depends on an immutability
    trigger). The receipt's own idempotency key is derived from the poison
    message id, so a duplicate quarantine attempt for the same poisoned
    message converges through the exact same ON CONFLICT arbiter as an
    ordinary message write: at most one receipt row per poisoned message,
    ever, even under concurrent recovery attempts.
    """
    db = platform_db()
    receipt_key = f"quarantine:{poison_message_id}"

    def operation(conn: Any) -> Dict[str, Any]:
        return _insert_message_row(
            conn,
            message_id=uuid.uuid4(),
            org_id=org_id, project_id=project_id, conversation_id=conversation_id,
            content=_QUARANTINE_PLACEHOLDER_CONTENT,
            metadata={
                "kind": _QUARANTINE_KIND,
                "poison_message_id": str(poison_message_id),
                "reason": reason,
            },
            actor_binding_id=actor_binding_id, idempotency_key=receipt_key,
        )

    return db.run_transaction(operation)


def create_message(
    org_id: Any, project_id: Any, conversation_id: Any, actor_binding_id: Any,
    idempotency_key: str, content: str, metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Write one durable conversation message, exactly once per idempotency key.

    Callers have already resolved ``org_id``/``project_id`` server-side from a
    verified project membership, exactly like ``create_conversation``.

    Returns ``{"message": ..., "replayed": bool, "quarantine": dict | None}``.
    ``replayed`` is True when this call observed an existing row for the same
    (org, project, conversation, idempotency_key) rather than inserting a new
    one -- the caller should treat the response the same way either time. The
    poison check (and any resulting quarantine write) runs only on the first,
    non-replayed insert: a retry that lands on the replay path must not
    re-fire the quarantine side effect a second time.
    """
    idempotency_key = _validate_idempotency_key(idempotency_key)
    content = _validate_message_content(content)
    metadata = _validate_message_metadata(metadata)
    db = platform_db()

    def operation(conn: Any) -> Dict[str, Any]:
        return _insert_message_row(
            conn,
            message_id=uuid.uuid4(),
            org_id=org_id, project_id=project_id, conversation_id=conversation_id,
            content=content, metadata=metadata,
            actor_binding_id=actor_binding_id, idempotency_key=idempotency_key,
        )

    message = db.run_transaction(operation)
    replayed = message.pop("replayed")

    quarantine = None
    if not replayed:
        reason = _post_store_poison_reason(message["metadata"])
        if reason is not None:
            quarantine = _write_quarantine_receipt(
                org_id, project_id, conversation_id, actor_binding_id,
                message["message_id"], reason,
            )

    return {"message": message, "replayed": replayed, "quarantine": quarantine}


def recover_conversation_messages(
    org_id: Any, project_id: Any, conversation_id: Any, *, limit: int = 200,
) -> Dict[str, Any]:
    """Return this conversation's ordered message tail, poison-fenced.

    Tenant/project/conversation scoped by the same three-column predicate as
    every other storage function in this module -- a foreign or unknown
    conversation id reads as an empty tail, never an error and never another
    tenant's rows.

    Quarantined rows are excluded from ``messages`` and each surfaces instead
    as one entry in ``gaps`` (message id, reason code, quarantine time) -- the
    gap is reported, never silently dropped, and the reason is a short code
    referencing the poisoned row, never the raw payload. This function only
    reads: it never quarantines a row itself, so a recovery scan can never
    mistake a live writer's half-committed row for poison (nothing here does
    the deserialization attempt), and running it twice over the same tail is
    side-effect-free and returns the identical gap list both times.
    """
    if limit <= 0 or limit > 500:
        raise ValueError("recovery page limit must be between 1 and 500")
    db = platform_db()
    with db.cursor() as cur:
        cur.execute(
            f"SELECT * FROM {MESSAGES_TABLE}"
            " WHERE org_id = %s AND project_id = %s AND conversation_id = %s"
            " ORDER BY created_at ASC, message_id ASC LIMIT %s",
            (str(org_id), str(project_id), str(conversation_id), limit),
        )
        rows = list(cur.fetchall())

    receipts_by_poison_id: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        metadata = row["metadata"] or {}
        if metadata.get("kind") == _QUARANTINE_KIND:
            poison_id = metadata.get("poison_message_id")
            if poison_id:
                receipts_by_poison_id[str(poison_id)] = row

    messages: List[Dict[str, Any]] = []
    gaps: List[Dict[str, Any]] = []
    for row in rows:
        metadata = row["metadata"] or {}
        if metadata.get("kind") == _QUARANTINE_KIND:
            continue  # receipts never appear in the message stream itself
        message_id = str(row["message_id"])
        receipt = receipts_by_poison_id.get(message_id)
        if receipt is not None:
            gaps.append({
                "message_id": message_id,
                "reason": (receipt["metadata"] or {}).get("reason"),
                "receipt_id": str(receipt["message_id"]),
                "quarantined_at": receipt["created_at"].isoformat()
                    if receipt["created_at"] else None,
            })
            continue
        messages.append(_row_message(row))

    return {"messages": messages, "gaps": gaps}


# --- router ----------------------------------------------------------------- #

class CreateConversationRequest(BaseModel):
    title: Optional[str] = Field(default=None, max_length=MAX_TITLE_LENGTH)


def _flag_off() -> JSONResponse:
    return error_response(
        ErrorCode.BAD_PARAMS, "conversation persistence is not enabled",
        retryable=False, status_code=404,
    )


def _not_found() -> JSONResponse:
    return error_response(
        ErrorCode.BAD_PARAMS, "unknown conversation", retryable=False,
        status_code=404,
    )


def _forbidden() -> JSONResponse:
    return error_response(
        ErrorCode.FORBIDDEN,
        "current project role does not permit this conversation action",
        retryable=False, status_code=403,
    )


def _require_project(tenant: Any, project_id: str, *, write: bool) -> Optional[str]:
    """Resolve org_id for a verified, same-tenant, role-checked project.

    Returns None (route should 404) for a missing OR cross-tenant project,
    matching ``platform_link.require_project_access``'s own contract, so a
    cross-tenant conversation id reads exactly like a missing one and no
    caller can distinguish "exists in another tenant" from "does not exist".
    """
    try:
        return platform_link.require_project_access(tenant, project_id, write=write)
    except LookupError:
        return None


def _actor_binding_id(tenant: Any) -> str:
    """Resolve the calling identity's binding_id server-side.

    Only reachable after ``_require_project`` has already succeeded for this
    same tenant, which resolved the identical (authority, subject) binding
    internally -- this call re-reads the same row, it does not trust anything
    the client supplied.
    """
    subject = getattr(tenant, "subject", None)
    if not isinstance(subject, str) or not subject:
        raise platform_link.ProjectSessionForbidden(
            "conversation actor requires a verified identity")
    store = platform_store()
    binding = store.resolve_active_identity_binding("auth0", subject)
    if binding is None:
        raise platform_link.ProjectSessionForbidden(
            "conversation actor identity binding is unavailable")
    return str(binding.binding_id)


@router.post("/api/projects/{project_id}/conversations")
def api_create_conversation(project_id: str, req: CreateConversationRequest,
                            tenant=Depends(deps.require_active_tenant)):
    if not conv_durable_enabled():
        return _flag_off()
    try:
        org_id = _require_project(tenant, project_id, write=True)
        if org_id is None:
            return _not_found()
        actor_binding_id = _actor_binding_id(tenant)
    except platform_link.ProjectSessionForbidden:
        return _forbidden()
    conversation = create_conversation(
        org_id, project_id, actor_binding_id, req.title)
    return JSONResponse(
        status_code=201,
        content=deps.tenant_echo(with_envelope_fields(conversation), tenant),
    )


@router.get("/api/projects/{project_id}/conversations")
def api_list_conversations(project_id: str,
                           tenant=Depends(deps.require_active_tenant)):
    if not conv_durable_enabled():
        return _flag_off()
    try:
        org_id = _require_project(tenant, project_id, write=False)
    except platform_link.ProjectSessionForbidden:
        return _forbidden()
    if org_id is None:
        return _not_found()
    body = {"conversations": list_conversations(org_id, project_id)}
    return deps.tenant_echo(with_envelope_fields(body), tenant)


@router.get("/api/projects/{project_id}/conversations/{conversation_id}")
def api_get_conversation(project_id: str, conversation_id: str,
                         tenant=Depends(deps.require_active_tenant)):
    if not conv_durable_enabled():
        return _flag_off()
    try:
        org_id = _require_project(tenant, project_id, write=False)
    except platform_link.ProjectSessionForbidden:
        return _forbidden()
    if org_id is None:
        return _not_found()
    conversation = get_conversation(org_id, project_id, conversation_id)
    if conversation is None:
        return _not_found()
    return deps.tenant_echo(with_envelope_fields(conversation), tenant)


@router.post("/api/projects/{project_id}/conversations/recover")
def api_recover_conversation(project_id: str,
                             tenant=Depends(deps.require_active_tenant)):
    """Resume the project's most recent conversation, or start one.

    The flag gate runs before project resolution and before the store call
    that may INSERT -- ordering matters here specifically because, unlike the
    read-only routes, this one can write on a cache miss.
    """
    if not conv_durable_enabled():
        return _flag_off()
    try:
        org_id = _require_project(tenant, project_id, write=True)
        if org_id is None:
            return _not_found()
        actor_binding_id = _actor_binding_id(tenant)
    except platform_link.ProjectSessionForbidden:
        return _forbidden()
    conversation = recover_or_create_conversation(
        org_id, project_id, actor_binding_id)
    return deps.tenant_echo(with_envelope_fields(conversation), tenant)
