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
