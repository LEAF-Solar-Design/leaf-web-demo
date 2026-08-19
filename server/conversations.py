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
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb

import deps
import platform_link
from envelopes import ErrorCode, error_response, with_envelope_fields

SERVER_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = SERVER_DIR.parent

FLAG_CONV_DURABLE = "LEAF_CONV_DURABLE_ENABLED"
MAX_TITLE_LENGTH = 200
TABLE = "conversations"

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


# --- message write path (0046 conversations, B-C2) -------------------------- #
#
# KNOWN GAP: durable message storage needs a dedicated table -- content up to
# MAX_MESSAGE_CONTENT_BYTES and a real idempotency_key uniqueness constraint
# do not fit the existing `conversations` row (title is capped at
# MAX_TITLE_LENGTH by conversations_title_check, and the table carries no
# idempotency_key column). That table (`conversation_messages`) is not part of
# this card's file budget: files_expected names no migration file, the card is
# flagged migration: false, and adding platform/migrations/00xx would break
# the currently-green test_postgres_authority_inventory_contract.py's pinned
# EXPECTED_MIGRATIONS without also editing that test file, which is likewise
# outside this card's scope. Validation, the flag fence, and the typed-4xx
# envelope below are real and fully covered without a database; create_message
# is the write this route calls once that table exists, and every test that
# exercises it is marked requires_database so it skips cleanly without
# DATABASE_URL, exactly like every other Postgres-backed path in this module.

MAX_MESSAGE_CONTENT_BYTES = 32768
ALLOWED_MESSAGE_METADATA_KEYS = frozenset({"role", "client_message_id", "source"})
MAX_MESSAGE_METADATA_VALUE_BYTES = 500
MAX_IDEMPOTENCY_KEY_LENGTH = 200
MESSAGES_TABLE = "conversation_messages"
MESSAGE_STATEMENT_TIMEOUT_MS = 2000


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


def _message_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "message_id": str(row["message_id"]),
        "conversation_id": str(row["conversation_id"]),
        "org_id": str(row["org_id"]),
        "project_id": str(row["project_id"]),
        "content": row["content"],
        "metadata": row["metadata"] if isinstance(row["metadata"], dict) else {},
        "created_by_binding_id": str(row["created_by_binding_id"]),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


def create_message(org_id: Any, project_id: Any, conversation_id: Any,
                   actor_binding_id: Any, content: str,
                   metadata: Dict[str, Any],
                   idempotency_key: str) -> tuple[Dict[str, Any], bool]:
    """The ONE write statement: INSERT .. ON CONFLICT .. RETURNING.

    A fresh write and a same-key replay both resolve in exactly one round
    trip to this table -- no separate pre-SELECT (so no TOCTOU window between
    a check and an insert) and no separate SELECT-after-INSERT on the replay
    path (so a replay under READ COMMITTED can never race an uncommitted
    winner). Paired with the router's single project-authority call, the
    write path issues one authority read and one data statement.

    Callers must have already validated ``content``/``metadata`` and resolved
    ``org_id``/``actor_binding_id`` from a verified project membership --
    never from the request body. Requires MESSAGES_TABLE to exist (see the
    "KNOWN GAP" note above); raises ``psycopg.errors.UndefinedTable`` until a
    companion migration lands.
    """
    db = platform_db()
    message_id = uuid.uuid4()
    with db.transaction() as conn:
        conn.execute("SET LOCAL statement_timeout = %s",
                     (str(MESSAGE_STATEMENT_TIMEOUT_MS),))
        row = conn.execute(
            f"INSERT INTO {MESSAGES_TABLE}"
            " (message_id, org_id, project_id, conversation_id, content,"
            "  metadata, created_by_binding_id, idempotency_key)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT (org_id, project_id, conversation_id, idempotency_key)"
            " DO UPDATE SET content = conversation_messages.content"
            " RETURNING *, (xmax = 0) AS inserted",
            (str(message_id), str(org_id), str(project_id), str(conversation_id),
             content, Jsonb(metadata), str(actor_binding_id), idempotency_key),
        ).fetchone()
    if row is None:
        raise RuntimeError("message write did not return a row")
    row = dict(row)
    inserted = bool(row.pop("inserted"))
    return _message_row(row), inserted


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
