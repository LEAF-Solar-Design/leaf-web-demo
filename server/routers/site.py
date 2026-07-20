"""Public marketing-site endpoints (this lane) — NO auth dependency.

Like /api/health, these routes deliberately omit ``Depends(deps.require_tenant)``
(the codebase gates per-route), so they answer 200 with no bearer token even
when LEAF_AUTH_LIVE=1. They expose only the pre-baked demo solve and a
params-schema-free projection of the public capability catalog — no tenant
data, no provenance, no writable surface.

  GET /api/site/demo-solve    -> cached "string-panels" solve of the sample roof
  GET /api/site/capabilities  -> public projection of the capability catalog

Both endpoints send ``ETag`` + ``Cache-Control: public, max-age=300,
stale-while-revalidate=3600`` and honor ``If-None-Match`` with a 304.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

import broker_client
import catalog
import deps
import site_demo
from envelopes import ErrorCode, error_response, with_envelope_fields

router = APIRouter()

CACHE_CONTROL = "public, max-age=300, stale-while-revalidate=3600"


def _cache_headers(etag: str) -> Dict[str, str]:
    return {"ETag": etag, "Cache-Control": CACHE_CONTROL}


def _if_none_match_hit(request: Request, etag: str) -> bool:
    inm = request.headers.get("if-none-match")
    if not inm:
        return False
    candidates = [t.strip().lstrip("W/").strip() for t in inm.split(",")]
    return etag in candidates or "*" in candidates


@router.get("/api/site/demo-solve")
def demo_solve(request: Request):
    """The cached public demo solve (site_demo.get_demo_solve). A broker that is
    down or a failed solve surfaces as the repo's standard §10 error envelope
    with HTTP 502 — never a bare 500."""
    try:
        solve = site_demo.get_demo_solve()
    except broker_client.BrokerUnreachable as exc:
        return error_response(ErrorCode.BROKER_UNREACHABLE,
                              f"demo solve unavailable: {exc}",
                              retryable=True, status_code=502)
    except site_demo.SiteSolveError as exc:
        err = (exc.envelope.get("error") or {})
        code = err.get("error_code")
        code = code if code in ErrorCode.ALL else ErrorCode.WORKITEM_FAILED
        return error_response(code, f"demo solve failed: {exc}",
                              retryable=bool(err.get("retryable", True)),
                              status_code=502)
    etag = site_demo.solve_etag(solve)
    if _if_none_match_hit(request, etag):
        return Response(status_code=304, headers=_cache_headers(etag))
    return JSONResponse(
        status_code=200,
        content=with_envelope_fields({"ok": True, "solve": solve}),
        headers=_cache_headers(etag),
    )


def _public_families() -> List[Dict[str, Any]]:
    """Public projection of the SAME catalog builder /api/capabilities uses
    (catalog.build_catalog over deps.all_tools(), internal tools filtered) —
    stripped to name/version/description/capabilities: NO params_schema, NO
    provenance."""
    families = catalog.build_catalog(deps.all_tools(), include_internal=False)
    out: List[Dict[str, Any]] = []
    for fam in families:
        out.append({
            "family_id": fam.get("family_id"),
            "label": fam.get("label", ""),
            "description": fam.get("description", ""),
            "tools": [
                {
                    "name": t.get("name"),
                    "version": t.get("version", "1.0.0"),
                    "description": t.get("description", ""),
                    "capabilities": t.get("capabilities", []),
                }
                for t in fam.get("capabilities", [])
            ],
        })
    return out


@router.get("/api/site/capabilities")
def site_capabilities(request: Request):
    families = _public_families()
    digest = hashlib.sha256(
        json.dumps(families, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    etag = f'"{digest[:16]}-1"'
    if _if_none_match_hit(request, etag):
        return Response(status_code=304, headers=_cache_headers(etag))
    return JSONResponse(
        status_code=200,
        content=with_envelope_fields({"ok": True, "families": families}),
        headers=_cache_headers(etag),
    )
