"""Public marketing-site endpoints with no authentication dependency.

The capability route exposes only static builtin tools. The solve route serves
a broker-backed proof over the bundled sample intake. Both routes provide HTTP
validators and a five-minute public cache policy.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List

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
    header = request.headers.get("if-none-match")
    if not header:
        return False
    candidates = [value.strip().removeprefix("W/") for value in header.split(",")]
    return etag in candidates or "*" in candidates


def _builtin_tools() -> list:
    """Return only tracked static tools, never tenant or runtime overlays."""
    by_name: Dict[str, Dict[str, Any]] = {}
    for tool in deps.load_engine_registry_tools():
        by_name[tool["name"]] = tool
    for tool in deps.load_seed_write_tools():
        by_name[tool["name"]] = tool
    return list(by_name.values())


def _public_families() -> List[Dict[str, Any]]:
    """Project the builtin-only catalog without params or provenance."""
    families = catalog.build_catalog(_builtin_tools(), include_internal=False)
    return [
        {
            "family_id": family.get("family_id"),
            "label": family.get("label", ""),
            "description": family.get("description", ""),
            "tools": [
                {
                    "name": tool.get("name"),
                    "version": tool.get("version", "1.0.0"),
                    "description": tool.get("description", ""),
                    "capabilities": tool.get("capabilities", []),
                }
                for tool in family.get("capabilities", [])
            ],
        }
        for family in families
    ]


@router.get("/api/site/capabilities")
def site_capabilities(request: Request):
    """Public, builtin-only capability projection with HTTP validators."""
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


@router.get("/api/site/guest-upload-policy")
def guest_upload_policy() -> Dict[str, Any]:
    """Return the public guest upload policy from its enforcement source."""
    import guest_uploads

    return with_envelope_fields(guest_uploads.policy_view())


@router.get("/api/site/demo-solve")
def site_demo_solve(request: Request):
    """Serve a current broker-backed solve proof or a standard 502 envelope."""
    try:
        solve = site_demo.get_demo_solve()
    except broker_client.BrokerUnreachable as exc:
        return error_response(
            ErrorCode.BROKER_UNREACHABLE,
            f"demo solve unavailable: {exc}",
            retryable=True,
            status_code=502,
        )
    except site_demo.SiteSolveError as exc:
        error = exc.envelope.get("error") or {}
        code = error.get("error_code")
        code = code if code in ErrorCode.ALL else ErrorCode.WORKITEM_FAILED
        return error_response(
            code,
            f"demo solve failed: {exc}",
            retryable=bool(error.get("retryable", True)),
            status_code=502,
        )
    etag = site_demo.solve_etag(solve)
    if _if_none_match_hit(request, etag):
        return Response(status_code=304, headers=_cache_headers(etag))
    return JSONResponse(
        status_code=200,
        content=with_envelope_fields({"ok": True, "solve": solve}),
        headers=_cache_headers(etag),
    )
