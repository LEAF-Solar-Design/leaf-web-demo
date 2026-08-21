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

import base64
import importlib.util
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

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
# Router-facing rail (B-C2): allowlisted metadata keys with per-value caps,
# stricter than the size-only backstop above; the route rejects before any
# DB call, the module-level validators stay as the fail-closed backstop.
ALLOWED_MESSAGE_METADATA_KEYS = frozenset({"role", "client_message_id", "source"})
MAX_MESSAGE_METADATA_VALUE_BYTES = 500
MESSAGE_STATEMENT_TIMEOUT_MS = 2000

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
    """Return one conversation only when org, project, AND id all match.

    Statement-timeout bounded: this is one of the three DB round trips the
    B-C2 message write route makes (authority read, this lookup, the INSERT),
    and it is also on the read routes' path, so the same
    MESSAGE_STATEMENT_TIMEOUT_MS budget the write itself uses applies here
    too -- SET LOCAL, so it never leaks past this one statement to the next
    pooled borrower.
    """
    db = platform_db()
    with db.transaction() as conn:
        conn.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (str(MESSAGE_STATEMENT_TIMEOUT_MS),),
        )
        row = conn.execute(
            f"SELECT * FROM {TABLE}"
            " WHERE conversation_id = %s AND org_id = %s AND project_id = %s",
            (str(conversation_id), str(org_id), str(project_id)),
        ).fetchone()
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


# --- router-facing validation rail (B-C2): typed 4xx before any DB call --- #


class MessageRejected(ValueError):
    """A message write request fails validation before any DB call.

    Carries the HTTP status the router answers with. Raising this always
    means nothing was written -- every check it guards runs before the one
    INSERT in ``create_message``.
    """

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(message)


def validate_message_content(content: Any) -> str:
    """Enforce the cap on the UTF-8 ENCODED size, never ``len(str)``.

    ``len()`` on a Python str counts code points: 10k 4-byte emoji is 10k
    chars (well under a naive char-count cap) but 40KB once encoded. Every
    comparison below happens on the encoded bytes so a multibyte payload that
    straddles the cap is judged by what actually lands on the wire and in the
    row, not by an undercounted character length.
    """
    if type(content) is not str:
        raise MessageRejected(422, "content must be a string")
    encoded = content.encode("utf-8")
    if len(encoded) < 1:
        raise MessageRejected(422, "content must not be empty")
    if len(encoded) > MAX_MESSAGE_CONTENT_BYTES:
        raise MessageRejected(
            413, f"content exceeds {MAX_MESSAGE_CONTENT_BYTES} bytes")
    return content


def validate_message_metadata(metadata: Any) -> Dict[str, str]:
    """Allowlist, not denylist: an unknown key is rejected outright.

    The oracle forbids silently stripping unknown keys, so a caller that
    over-supplies metadata gets a typed 4xx instead of a quiet downgrade that
    would let it believe a key was stored when it was dropped.
    """
    if metadata is None:
        return {}
    if type(metadata) is not dict:
        raise MessageRejected(422, "metadata must be an object")
    unknown = sorted(set(metadata.keys()) - ALLOWED_MESSAGE_METADATA_KEYS)
    if unknown:
        raise MessageRejected(422, f"unknown metadata keys: {unknown}")
    clean: Dict[str, str] = {}
    for key, value in metadata.items():
        if type(value) is not str:
            raise MessageRejected(422, f"metadata[{key}] must be a string")
        if len(value.encode("utf-8")) > MAX_MESSAGE_METADATA_VALUE_BYTES:
            raise MessageRejected(
                422,
                f"metadata[{key}] exceeds {MAX_MESSAGE_METADATA_VALUE_BYTES} bytes",
            )
        clean[key] = value
    return clean


def validate_idempotency_key(key: Any) -> str:
    if type(key) is not str or not (1 <= len(key) <= MAX_IDEMPOTENCY_KEY_LENGTH):
        raise MessageRejected(
            400,
            f"Idempotency-Key header must be 1-{MAX_IDEMPOTENCY_KEY_LENGTH} characters",
        )
    return key



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


# --- retention GC: bounded, tenant-scoped, in-flight-fenced ---------------- #
#
# One call is one tenant's (``org_id``'s) pass -- never spans orgs, so a
# scheduler invoking this once per org keeps GC itself tenant-isolated the
# same way every read/write path above already is. Bounded two ways so a
# single run can never become an unbounded background job: a row cap (never
# deletes more than ``gc_row_cap()`` rows total across every batch in one
# call) and a wall-clock cap (``gc_wall_clock_s()``, checked BETWEEN batches
# only -- an in-flight batch is never torn mid-delete). Deletes only rows
# whose ``created_at`` is older than the retention window
# (``retention_days()``). Returns a sanitized receipt: ids and counts only,
# never message content or metadata, matching the quarantine receipt's own
# "never embed the payload" rule above.
#
# The in-flight guard (oracle clause 2) is folded into the DELETE's own
# candidate selection -- a ``NOT EXISTS`` against ``app_session_requests``'s
# non-terminal rows for that exact (org_id, project_id) -- inside the SAME
# statement and transaction snapshot that performs the delete, rather than a
# separate pre-check SELECT. A request that has already committed
# 'admitted'/'executing'/'queued' before this transaction's snapshot is
# always visible here and blocks that project's rows this pass; the only
# unguarded window is a request whose OWN commit lands inside this
# transaction's snapshot, which is unreachable by any caller that awaits its
# admission response before sending conversation traffic. Closing that
# residual window fully would require a lock shared with
# ``request_journal.py``, outside this card's two-file budget.
#
# An unrecognized future request state is treated as NOT terminal (fails
# closed, blocking GC for that project) rather than assuming it is safe to
# ignore -- this module names the terminal set explicitly instead of
# importing ``request_journal``'s private ``_TERMINAL``, so the two stay
# independent artifacts a reviewer can diff against each other.
_JOURNAL_TERMINAL_STATES = ("completed", "failed", "abandoned")

GC_DEFAULT_RETENTION_DAYS = 90.0
GC_DEFAULT_ROW_CAP = 500
GC_DEFAULT_WALL_CLOCK_S = 5.0
GC_DEFAULT_BATCH_SIZE = 200


def retention_days() -> float:
    """The GC retention window in days. Non-positive or unparseable falls
    back to the default rather than disabling retention entirely."""
    raw = os.environ.get("LEAF_CONV_RETENTION_DAYS", "").strip()
    if not raw:
        return GC_DEFAULT_RETENTION_DAYS
    try:
        value = float(raw)
    except ValueError:
        return GC_DEFAULT_RETENTION_DAYS
    return value if value > 0 else GC_DEFAULT_RETENTION_DAYS


def gc_row_cap() -> int:
    """Maximum rows one GC call may delete in total across all its batches."""
    raw = os.environ.get("LEAF_CONV_GC_ROW_CAP", "").strip()
    if not raw:
        return GC_DEFAULT_ROW_CAP
    try:
        value = int(raw)
    except ValueError:
        return GC_DEFAULT_ROW_CAP
    return value if value > 0 else GC_DEFAULT_ROW_CAP


def gc_wall_clock_s() -> float:
    """Maximum wall-clock seconds one GC call may run, checked between
    batches only -- never aborts a batch already in flight."""
    raw = os.environ.get("LEAF_CONV_GC_WALL_CLOCK_S", "").strip()
    if not raw:
        return GC_DEFAULT_WALL_CLOCK_S
    try:
        value = float(raw)
    except ValueError:
        return GC_DEFAULT_WALL_CLOCK_S
    return value if value > 0 else GC_DEFAULT_WALL_CLOCK_S


def _gc_delete_batch(
    conn: Any, *, org_id: str, cutoff: datetime, limit: int,
) -> List[Dict[str, Any]]:
    """One bounded batch: candidate selection and delete share one statement
    and one transaction snapshot, so the in-flight guard below can never be
    stale relative to the rows it is about to delete.

    ``SKIP LOCKED`` lets a concurrent GC pass over the same org make forward
    progress on disjoint candidates instead of blocking, the same pattern
    ``request_journal.claim_next_queued_and_turn`` already uses for queue
    claims.
    """
    cur = conn.execute(
        "WITH candidates AS ("
        "  SELECT message_id, project_id, conversation_id"
        "  FROM conversation_messages"
        "  WHERE org_id = %s AND created_at < %s"
        "  AND NOT EXISTS ("
        "    SELECT 1 FROM app_session_requests AS r"
        "    WHERE r.org_id = conversation_messages.org_id"
        "    AND r.project_id = conversation_messages.project_id"
        "    AND NOT (r.state = ANY(%s))"
        "  )"
        "  ORDER BY created_at ASC, message_id ASC"
        "  LIMIT %s"
        "  FOR UPDATE OF conversation_messages SKIP LOCKED"
        ")"
        " DELETE FROM conversation_messages"
        " WHERE message_id IN (SELECT message_id FROM candidates)"
        " RETURNING message_id, project_id, conversation_id",
        (org_id, cutoff, list(_JOURNAL_TERMINAL_STATES), limit),
    )
    return list(cur.fetchall())


def _empty_gc_receipt(org_id: Any, *, flag_enabled: bool, cutoff: Optional[datetime] = None,
                       row_cap: Optional[int] = None,
                       wall_clock_cap: Optional[float] = None) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "org_id": str(org_id),
        "flag_enabled": flag_enabled,
        "cutoff": cutoff.isoformat() if cutoff is not None else None,
        "retention_days": retention_days(),
        "row_cap": row_cap if row_cap is not None else gc_row_cap(),
        "wall_clock_cap_s": wall_clock_cap if wall_clock_cap is not None else gc_wall_clock_s(),
        "deleted_message_count": 0,
        "projects_touched": [],
        "conversations_touched_count": 0,
        "batches": 0,
        "truncated_by_row_cap": False,
        "truncated_by_wall_clock": False,
        "started_at": now.isoformat(),
        "finished_at": now.isoformat(),
    }


def run_retention_gc(org_id: Any, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Run one bounded, tenant-scoped retention GC pass for ``org_id``.

    Gated by ``conv_durable_enabled()`` FIRST, exactly like every route above
    -- when the flag is off this makes no store call and deletes nothing,
    matching the "checked before any store call" rule the rest of this
    module already follows (the module docstring's "checked FIRST in every
    handler" is not route-only: a scheduled background pass is held to the
    same rule so a negative control that only probes the POST route cannot
    miss a GC path that deletes durable data while the feature is disabled).

    Deletes durable message rows older than the retention window in capped
    batches, skipping any project with a non-terminal ``app_session_requests``
    row (oracle clause 2). Stops after ``gc_row_cap()`` total deletions or
    ``gc_wall_clock_s()`` elapsed wall-clock time, whichever comes first, and
    returns a sanitized receipt describing what happened -- ids and counts
    only.
    """
    if not conv_durable_enabled():
        return _empty_gc_receipt(org_id, flag_enabled=False)

    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=retention_days())
    row_cap = gc_row_cap()
    wall_clock_cap = gc_wall_clock_s()
    batch_size = min(GC_DEFAULT_BATCH_SIZE, row_cap)

    db = platform_db()
    started = time.monotonic()
    deleted_total = 0
    batches = 0
    projects_touched: Set[str] = set()
    conversations_touched: Set[str] = set()
    truncated_by_row_cap = False
    truncated_by_wall_clock = False

    while True:
        if deleted_total >= row_cap:
            truncated_by_row_cap = True
            break
        if time.monotonic() - started >= wall_clock_cap:
            truncated_by_wall_clock = True
            break
        limit = min(batch_size, row_cap - deleted_total)

        def operation(conn: Any, _limit: int = limit) -> List[Dict[str, Any]]:
            return _gc_delete_batch(
                conn, org_id=str(org_id), cutoff=cutoff, limit=_limit)

        rows = db.run_transaction(operation)
        batches += 1
        if not rows:
            break
        deleted_total += len(rows)
        for row in rows:
            projects_touched.add(str(row["project_id"]))
            conversations_touched.add(str(row["conversation_id"]))
        if len(rows) < limit:
            break  # fewer candidates than requested: this org is drained

    finished = datetime.now(timezone.utc)
    return {
        "org_id": str(org_id),
        "flag_enabled": True,
        "cutoff": cutoff.isoformat(),
        "retention_days": retention_days(),
        "row_cap": row_cap,
        "wall_clock_cap_s": wall_clock_cap,
        "deleted_message_count": deleted_total,
        "projects_touched": sorted(projects_touched),
        "conversations_touched_count": len(conversations_touched),
        "batches": batches,
        "truncated_by_row_cap": truncated_by_row_cap,
        "truncated_by_wall_clock": truncated_by_wall_clock,
        "started_at": now.isoformat(),
        "finished_at": finished.isoformat(),
    }

# --- message tail (0048) / recovery-on-reconnect (card B-C3) --------------- #

RECOVERY_DEFAULT_LIMIT = 50
RECOVERY_MAX_LIMIT = 200
# Bounds the recovery query independently of the per-app request timeout, so
# a seq-scan (a missing/unused index) fails fast and loud in staging instead
# of quietly eating the whole request budget. idx_conversation_messages_
# scope_order (0048) already covers (org_id, project_id, conversation_id,
# created_at) -- exactly this query's WHERE + ORDER BY -- so this ceiling
# should never actually bind on a healthy index; it exists as a hard fence
# for card B-C4's "cannot wedge recovery" case, not as the primary defense.
_RECOVERY_STATEMENT_TIMEOUT_MS = 3000


class InvalidRecoveryCursor(ValueError):
    """The ``cursor`` query param does not decode to a (created_at, message_id)
    pair. Raised by ``decode_recovery_cursor``; the router maps it to a typed
    400, never a 500 -- a caller replaying a stale/edited cursor is a normal
    client mistake, not a server fault."""


def _configure_recovery_statement_timeout(cur: Any) -> None:
    """Mirrors dependency_health._configure_statement_timeout's exact
    ``set_config(..., true)`` shape (SET LOCAL, scoped to this transaction
    only) rather than inventing a second convention for the same thing."""
    cur.execute(
        "SELECT set_config('statement_timeout', %s, true)",
        (f"{_RECOVERY_STATEMENT_TIMEOUT_MS}ms",),
    )


def encode_recovery_cursor(created_at: datetime, message_id: Any) -> str:
    """Opaque cursor over exactly (created_at, message_id) -- never a project
    or conversation scope, so a caller editing/tampering the cursor can only
    ever shift the pagination boundary, never widen the query's WHERE-clause
    scope (that always comes from the caller-verified org_id/project_id/
    conversation_id, resolved server-side, on every call)."""
    raw = f"{created_at.isoformat()}|{message_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_recovery_cursor(cursor: str) -> Tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        created_at_str, message_id_str = raw.split("|", 1)
        created_at = datetime.fromisoformat(created_at_str)
        message_id = uuid.UUID(message_id_str)
    except Exception as exc:
        raise InvalidRecoveryCursor("recovery cursor is malformed") from exc
    return created_at, message_id


def _latest_conversation_id(org_id: Any, project_id: Any) -> Optional[str]:
    """The project's most-recently-created conversation, matching
    ``recover_or_create_conversation``'s own "most recent" semantics -- this
    function only ever reads (``list_conversations``), it never creates one,
    so an empty/new project (no conversation row yet) resolves to None
    rather than lazily inserting anything."""
    existing = list_conversations(org_id, project_id)
    if not existing:
        return None
    return existing[-1]["conversation_id"]


def _fence_page(
    page_rows: List[Dict[str, Any]], org_id: Any, project_id: Any,
    conversation_id: Any,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Apply B-C4's poison fence to ONE raw keyset page.

    Quarantine receipt rows never appear in the item stream. A poisoned
    message is skipped and surfaced as a gap entry (reason code + receipt
    reference, never the raw payload), matching recover_conversation_messages'
    whole-tail semantics. Because a receipt row can land on a later page than
    the message it fences, receipts are resolved with one additional bounded
    lookup keyed by the page's message ids (at most one statement per page,
    statement-timeout configured) instead of trusting page locality.
    """
    candidates = [
        row for row in page_rows
        if (row["metadata"] or {}).get("kind") != _QUARANTINE_KIND
    ]
    if not candidates:
        return [], []
    receipt_keys = [f"quarantine:{row['message_id']}" for row in candidates]
    db = platform_db()
    with db.cursor() as cur:
        _configure_recovery_statement_timeout(cur)
        cur.execute(
            f"SELECT * FROM {MESSAGES_TABLE}"
            " WHERE org_id = %s AND project_id = %s AND conversation_id = %s"
            " AND idempotency_key = ANY(%s)",
            (str(org_id), str(project_id), str(conversation_id), receipt_keys),
        )
        receipts = {
            str((r["metadata"] or {}).get("poison_message_id")): r
            for r in cur.fetchall()
        }
    items: List[Dict[str, Any]] = []
    gaps: List[Dict[str, Any]] = []
    for row in candidates:
        message_id = str(row["message_id"])
        receipt = receipts.get(message_id)
        if receipt is not None:
            gaps.append({
                "message_id": message_id,
                "reason": (receipt["metadata"] or {}).get("reason"),
                "receipt_id": str(receipt["message_id"]),
                "quarantined_at": receipt["created_at"].isoformat()
                    if receipt["created_at"] else None,
            })
            continue
        items.append(_row_message(row))
    return items, gaps


def list_conversation_messages_page(
    org_id: Any, project_id: Any, conversation_id: Any, *,
    after: Optional[Tuple[datetime, Any]] = None,
    limit: int = RECOVERY_DEFAULT_LIMIT,
) -> Tuple[List[Dict[str, Any]], Optional[str], bool]:
    """One page of a conversation's message tail, in insertion order.

    Ordered ``(created_at, message_id) ASC`` -- the id tiebreak makes a
    same-transaction timestamp burst a total order, so a page boundary
    landing inside a tie can never dup or drop a row. ``after`` is an
    EXCLUSIVE boundary (``> (cursor_created_at, cursor_message_id)``): the
    caller already has that row, so it is never replayed on the next page,
    unlike an inclusive ``>=`` cursor which would double-deliver it on every
    reconnect. Fetches ``limit + 1`` rows to compute ``has_more`` without a
    second COUNT query; performs zero writes.
    """
    db = platform_db()
    with db.cursor() as cur:
        _configure_recovery_statement_timeout(cur)
        if after is None:
            cur.execute(
                f"SELECT * FROM {MESSAGES_TABLE}"
                " WHERE org_id = %s AND project_id = %s AND conversation_id = %s"
                " ORDER BY created_at ASC, message_id ASC LIMIT %s",
                (str(org_id), str(project_id), str(conversation_id), limit + 1),
            )
        else:
            after_created_at, after_message_id = after
            cur.execute(
                f"SELECT * FROM {MESSAGES_TABLE}"
                " WHERE org_id = %s AND project_id = %s AND conversation_id = %s"
                " AND (created_at, message_id) > (%s, %s)"
                " ORDER BY created_at ASC, message_id ASC LIMIT %s",
                (str(org_id), str(project_id), str(conversation_id),
                 after_created_at, str(after_message_id), limit + 1),
            )
        rows = cur.fetchall()
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    items, gaps = _fence_page(page_rows, org_id, project_id, conversation_id)
    next_cursor = None
    if has_more and page_rows:
        last = page_rows[-1]
        next_cursor = encode_recovery_cursor(last["created_at"], last["message_id"])
    return items, gaps, next_cursor, has_more


def recover_conversation_tail(
    org_id: Any, project_id: Any, *, cursor: Optional[str] = None,
    limit: int = RECOVERY_DEFAULT_LIMIT,
) -> Dict[str, Any]:
    """The project's most recent conversation, tail-paginated from ``cursor``.

    Pure read -- never creates a conversation, never touches a message row.
    An empty/new project (no conversation yet) returns the full empty-page
    envelope shape (``items: [] , next_cursor: null, has_more: false``), NOT
    a 404 -- the caller must already be authorized for this project before
    this function is ever reached (the router's job), so "no conversation
    yet" and "not authorized" are never conflated here.

    Raises ``InvalidRecoveryCursor`` for a malformed ``cursor`` -- the router
    maps that to a typed 400. Decoded BEFORE any DB call: a bad cursor is
    cheap-to-reject caller input, not a reason to spend a query.
    """
    after = decode_recovery_cursor(cursor) if cursor else None
    conversation_id = _latest_conversation_id(org_id, project_id)
    if conversation_id is None:
        return {"items": [], "gaps": [], "next_cursor": None, "has_more": False}
    items, gaps, next_cursor, has_more = list_conversation_messages_page(
        org_id, project_id, conversation_id, after=after, limit=limit,
    )
    return {"items": items, "gaps": gaps, "next_cursor": next_cursor,
            "has_more": has_more}



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
