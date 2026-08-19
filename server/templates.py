"""Immutable versioned Solar CAD template store (0047).

A template version is a permanent fact, never a mutable record: "org X
published template key Y, version N, with this exact content, from this
exact provenance." There is no UPDATE path anywhere in this module -- no
function here issues an UPDATE statement, and the database itself enforces
the same rule with a BEFORE UPDATE OR DELETE trigger on
``solar_template_versions`` (0047's migration). Publishing a new version is
always an INSERT of a new row with a higher ``version``; it can never mutate
or remove a prior one.

Gated by the ``solar_template_beta`` flag: while off, ``publish_version``
raises ``TemplateBetaDisabled`` before any INSERT, and the router below
returns 404 for every route without touching the store.

Routing lives in this module rather than under ``server/routers/`` (the
repo's usual home for route handlers), matching ``conversations.py``'s
precedent: this card's file budget has no room for a separate router file.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import deps
from envelopes import ErrorCode, error_response, with_envelope_fields

SERVER_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = SERVER_DIR.parent

FLAG_SOLAR_TEMPLATE_BETA = "LEAF_SOLAR_TEMPLATE_BETA_ENABLED"
MAX_TEMPLATE_KEY_LENGTH = 200
VALID_SOURCES = ("author", "import", "system")
TABLE = "solar_template_versions"

router = APIRouter()


class TemplateBetaDisabled(RuntimeError):
    """Raised by ``publish_version`` when the ``solar_template_beta`` flag is
    off. Callers must check ``solar_template_beta_enabled()`` themselves to
    avoid this exception on the hot path; it exists as a hard backstop so a
    caller that skips the check still cannot write a row."""


def solar_template_beta_enabled() -> bool:
    """The ``solar_template_beta`` flag. Off by default; checked before any
    write, in both the store and the router."""
    return os.environ.get(FLAG_SOLAR_TEMPLATE_BETA, "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def platform_db():
    """Load the local platform database package without shadowing stdlib
    platform.

    Duplicated from conversations.platform_db() / session_annex.platform_db()
    on purpose (their docstrings explain why): every copy populates the same
    sys.modules["leaf_platform"] entry, so they share one loaded package at
    runtime.
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


def content_digest(content: Dict[str, Any]) -> str:
    """A deterministic sha256 over ``content``: same keys/values in any
    input order always produce the same digest, so a version's provenance
    can be verified by recomputing this from the stored ``content`` column."""
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- storage layer (INSERT and SELECT only -- no UPDATE, ever) ------------- #

def _row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "template_version_id": str(row["template_version_id"]),
        "org_id": str(row["org_id"]),
        "template_key": row["template_key"],
        "version": row["version"],
        "content": row["content"],
        "content_sha256": row["content_sha256"],
        "source": row["source"],
        "provenance_note": row["provenance_note"],
        "published_by_binding_id": str(row["published_by_binding_id"]),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


def publish_version(org_id: Any, template_key: str, content: Dict[str, Any],
                    published_by_binding_id: Any, source: str = "author",
                    provenance_note: Optional[str] = None) -> Dict[str, Any]:
    """Insert one new, immutable template version row.

    Never mutates a prior version: the new row's ``version`` is
    ``1 + MAX(version)`` over the same (org_id, template_key) scope, computed
    fresh from this INSERT's own subquery inside one round trip, so two
    concurrent publishers racing for the same next version collide on the
    ``solar_template_versions_scope_version_unique`` constraint rather than
    silently overwriting each other's row.

    Callers have already resolved ``org_id`` server-side from a verified
    tenant -- never from a request body.
    """
    if not solar_template_beta_enabled():
        raise TemplateBetaDisabled(
            "solar_template_beta is not enabled; no version was published")
    if source not in VALID_SOURCES:
        raise ValueError(f"source must be one of {VALID_SOURCES}, got {source!r}")
    db = platform_db()
    template_version_id = uuid.uuid4()
    digest = content_digest(content)
    with db.cursor() as cur:
        cur.execute(
            f"INSERT INTO {TABLE}"
            " (template_version_id, org_id, template_key, version, content,"
            "  content_sha256, source, provenance_note, published_by_binding_id)"
            " VALUES (%s, %s, %s,"
            "  COALESCE((SELECT MAX(version) FROM solar_template_versions"
            "            WHERE org_id = %s AND template_key = %s), 0) + 1,"
            "  %s, %s, %s, %s, %s)"
            " RETURNING *",
            (str(template_version_id), str(org_id), template_key,
             str(org_id), template_key,
             json.dumps(content), digest, source, provenance_note,
             str(published_by_binding_id)),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("template version insert did not return a row")
    return _row(row)


def list_versions(org_id: Any, template_key: str) -> List[Dict[str, Any]]:
    """Every published version of one template, oldest first. Org AND
    template_key scoped -- the storage boundary holds on its own."""
    db = platform_db()
    with db.cursor() as cur:
        cur.execute(
            f"SELECT * FROM {TABLE} WHERE org_id = %s AND template_key = %s"
            " ORDER BY version ASC",
            (str(org_id), template_key),
        )
        rows = cur.fetchall()
    return [_row(row) for row in rows]


def get_version(org_id: Any, template_key: str,
                version: int) -> Optional[Dict[str, Any]]:
    """Return one specific version, or None when org, template_key, AND
    version do not all match."""
    db = platform_db()
    with db.cursor() as cur:
        cur.execute(
            f"SELECT * FROM {TABLE}"
            " WHERE org_id = %s AND template_key = %s AND version = %s",
            (str(org_id), template_key, version),
        )
        row = cur.fetchone()
    return _row(row) if row is not None else None


def get_latest_version(org_id: Any, template_key: str) -> Optional[Dict[str, Any]]:
    """The highest-numbered version, computed at read time -- never a stored,
    mutated pointer."""
    db = platform_db()
    with db.cursor() as cur:
        cur.execute(
            f"SELECT * FROM {TABLE} WHERE org_id = %s AND template_key = %s"
            " ORDER BY version DESC LIMIT 1",
            (str(org_id), template_key),
        )
        row = cur.fetchone()
    return _row(row) if row is not None else None


# --- router ------------------------------------------------------------------ #

class PublishVersionRequest(BaseModel):
    template_key: str = Field(min_length=1, max_length=MAX_TEMPLATE_KEY_LENGTH)
    content: Dict[str, Any]
    source: str = Field(default="author")
    provenance_note: Optional[str] = Field(default=None, max_length=2000)


def _flag_off() -> JSONResponse:
    return error_response(
        ErrorCode.BAD_PARAMS, "solar template store is not enabled",
        retryable=False, status_code=404,
    )


def _not_found() -> JSONResponse:
    return error_response(
        ErrorCode.BAD_PARAMS, "unknown template version", retryable=False,
        status_code=404,
    )


def _bad_source() -> JSONResponse:
    return error_response(
        ErrorCode.BAD_PARAMS, f"source must be one of {VALID_SOURCES}",
        retryable=False, status_code=400,
    )


def _actor_binding_id(tenant: Any) -> Optional[str]:
    """Resolve the calling identity's binding_id server-side, from the
    verified tenant's own subject -- never from anything the client
    supplied. Returns None when no verified identity is available (guest /
    legacy tenant), which the caller must treat as a 403."""
    subject = getattr(tenant, "subject", None)
    if not isinstance(subject, str) or not subject:
        return None
    store = platform_store()
    binding = store.resolve_active_identity_binding("auth0", subject)
    if binding is None:
        return None
    return str(binding.binding_id)


@router.post("/api/templates")
def api_publish_version(req: PublishVersionRequest,
                        tenant=Depends(deps.require_active_tenant)):
    """Publish a new template version under the caller's own org. ``org_id``
    is always read from the resolved tenant, never from the request body, so
    a caller can only ever publish into its own org."""
    if not solar_template_beta_enabled():
        return _flag_off()
    org_id = getattr(tenant, "org_id", None)
    if not org_id:
        return _not_found()
    if req.source not in VALID_SOURCES:
        return _bad_source()
    actor_binding_id = _actor_binding_id(tenant)
    if actor_binding_id is None:
        return _not_found()
    version = publish_version(
        org_id, req.template_key, req.content, actor_binding_id,
        req.source, req.provenance_note,
    )
    return JSONResponse(
        status_code=201,
        content=deps.tenant_echo(with_envelope_fields(version), tenant),
    )


@router.get("/api/templates/{template_key}/versions")
def api_list_versions(template_key: str,
                      tenant=Depends(deps.require_active_tenant)):
    if not solar_template_beta_enabled():
        return _flag_off()
    org_id = getattr(tenant, "org_id", None)
    if not org_id:
        return _not_found()
    body = {"versions": list_versions(org_id, template_key)}
    return deps.tenant_echo(with_envelope_fields(body), tenant)


@router.get("/api/templates/{template_key}/versions/{version}")
def api_get_version(template_key: str, version: int,
                    tenant=Depends(deps.require_active_tenant)):
    if not solar_template_beta_enabled():
        return _flag_off()
    org_id = getattr(tenant, "org_id", None)
    if not org_id:
        return _not_found()
    row = get_version(org_id, template_key, version)
    if row is None:
        return _not_found()
    return deps.tenant_echo(with_envelope_fields(row), tenant)
