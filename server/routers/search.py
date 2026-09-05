"""GET /api/search — slice 10c: the bar's find-scope search index.

Tenant-scoped, bounded, fail-closed over the indexes that exist in this repo
today:
  - tools:    deps.all_tools(tenant) — the SAME per-tenant fold /api/tools
              already serves (a tenant-repo tool stays invisible cross-tenant,
              wave 4).
  - versions: the named drawing's own version chain (?drawing_id=), read
              through the SAME per-tenant store backend routers/drawings.py's
              GET .../versions uses, so a cross-tenant drawing_id can never
              read another tenant's version history.
  - sessions: the caller's OWN operator sessions (operator_session_store,
              subject-scoped). A non-operator caller (most tenants) sees zero
              rows, not an error — the same fold web/src/api.js's
              listOperatorSessions already applies to a 404 from
              /api/operator/sessions itself.

Named gaps, NOT faked (std/palette-search-s10bc, slice 10c judgment — build-
doctrine: an unbacked row is a lie the user tests in four seconds):
  - drawings-by-name: no GET /api/drawings list endpoint exists in this repo,
    only a per-id route. A name index has nothing to read.
  - sheet anchors: no sheets manifest is exposed by slice 5b in this repo.
  - receipts: DEFERRED to slice 11, where receipts became first-class
    (merged after this branch was cut) — not built here, not stubbed.

Bounds (build-doctrine: every hot path carries a number):
  - QUERY_MAX_LEN = 200: bounds every downstream substring scan against a
    caller-controlled string; longer is 400, never silently truncated.
  - ROWS_PER_INDEX_CAP = 8: matches web/src/lib/palette.js's own
    MAX_ARTIFACT_ROWS_PER_KIND, so no index can crowd out the others.
  - INDEX_TIME_BUDGET_S = 0.75: a wall-clock cutoff per index (tenant-repo
    catalog size and version-chain length are both caller/tenant history
    dependent, not bounded by this process). An index over budget stops
    scanning and returns what it already found; it never blocks the other
    indexes or the response.
  - RESPONSE_ROW_CAP = 24: the total row cap across all indexes, matching
    web/src/lib/palette.js's own MAX_ACTION_ROWS, so the response can never
    grow past one screenful regardless of query breadth.
  - Each index is isolated in its own try/except: a dead index (a store
    unavailable, a malformed tenant registry) folds to zero rows for THAT
    index rather than 500ing the whole search.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, Query

import deps
import operator_deps
from envelopes import ErrorCode, error_response, with_envelope_fields

router = APIRouter()

QUERY_MAX_LEN = 200
ROWS_PER_INDEX_CAP = 8
INDEX_TIME_BUDGET_S = 0.75
RESPONSE_ROW_CAP = 24


def _match(needle: str, *fields: Any) -> bool:
    if not needle:
        return True
    return any(needle in str(f or "").lower() for f in fields)


def _tool_rows(tenant: Any, needle: str) -> List[Dict[str, Any]]:
    deadline = time.monotonic() + INDEX_TIME_BUDGET_S
    try:
        catalog_tools = deps.all_tools(str(tenant))
    except Exception:  # noqa: BLE001 - a dead catalog authority is an empty index, not a 500
        return []
    rows: List[Dict[str, Any]] = []
    for t in catalog_tools:
        if time.monotonic() > deadline:
            break
        name = t.get("name") or ""
        if not name:
            continue
        desc = t.get("description") or ""
        if not _match(needle, name, desc):
            continue
        rows.append({"kind": "tool", "id": f"tool:{name}", "label": name, "description": desc})
        if len(rows) >= ROWS_PER_INDEX_CAP:
            break
    return rows


def _version_rows(tenant: Any, drawing_id: Optional[str], needle: str) -> List[Dict[str, Any]]:
    if not drawing_id:
        return []
    try:
        import write_loop  # imported first: its own top-level code puts da/ (store.py's home) on sys.path
        import store

        backend = write_loop.backend_for_tenant(str(tenant), aps_live=False, da=None)
        write_loop.ensure_demo_drawing(backend, str(tenant), drawing_id)
        manifest = store.load_manifest(backend, str(tenant), drawing_id)
    except Exception:  # noqa: BLE001 - an absent/malformed drawing is an empty index, not a 500
        return []
    deadline = time.monotonic() + INDEX_TIME_BUDGET_S
    rows: List[Dict[str, Any]] = []
    for v in manifest.get("versions", []):
        if time.monotonic() > deadline:
            break
        note = v.get("note") or ""
        tool = v.get("tool") or ""
        label = f"v{v.get('v')}"
        if not _match(needle, label, note, tool):
            continue
        rows.append({
            "kind": "version",
            "id": f"version:{v.get('v')}",
            "label": label,
            "description": " · ".join(x for x in (tool, note) if x),
        })
        if len(rows) >= ROWS_PER_INDEX_CAP:
            break
    return rows


def _session_rows(tenant: Any, x_operator_subject: Optional[str], needle: str) -> List[Dict[str, Any]]:
    """Best-effort, subject-scoped, never surfaced as a search error: a
    non-operator caller (most tenants) sees zero rows, exactly the fold
    web/src/api.js's listOperatorSessions already applies to a 404 from
    /api/operator/sessions itself. Reuses operator_deps' own subject
    resolution rather than re-deriving it (this train's own five-times-paid
    lesson: call the neighbour, never mirror it)."""
    try:
        subject = operator_deps._resolve_subject(tenant, x_operator_subject)
        if not subject:
            return []
        import operator_principals
        principal = operator_principals.resolve_principal(subject)
        if principal is None or not principal.active:
            return []
        import operator_session_store
        sessions = operator_session_store.list_sessions(subject)
    except Exception:  # noqa: BLE001 - store/principal lookup down = empty index, not a 500
        return []
    deadline = time.monotonic() + INDEX_TIME_BUDGET_S
    rows: List[Dict[str, Any]] = []
    for s in sessions:
        if time.monotonic() > deadline:
            break
        label = s.get("session_id") or ""
        if not label:
            continue
        if not _match(needle, label, s.get("profile"), s.get("environment"), s.get("status")):
            continue
        rows.append({
            "kind": "session",
            "id": f"session:{label}",
            "label": label,
            "description": " · ".join(x for x in (s.get("profile"), s.get("environment"), s.get("status")) if x),
        })
        if len(rows) >= ROWS_PER_INDEX_CAP:
            break
    return rows


@router.get("/api/search")
def search(
    q: str = Query(default=""),
    drawing_id: Optional[str] = Query(default=None),
    tenant: Any = Depends(deps.require_tenant),
    x_operator_subject: Optional[str] = Header(default=None),
) -> Any:
    if len(q) > QUERY_MAX_LEN:
        return error_response(
            ErrorCode.BAD_PARAMS, f"q exceeds {QUERY_MAX_LEN} characters",
            retryable=False, status_code=400,
        )
    needle = q.strip().lower()
    results: List[Dict[str, Any]] = []
    results.extend(_tool_rows(tenant, needle))
    results.extend(_version_rows(tenant, drawing_id, needle))
    results.extend(_session_rows(tenant, x_operator_subject, needle))
    return with_envelope_fields({"query": q, "results": results[:RESPONSE_ROW_CAP]})
