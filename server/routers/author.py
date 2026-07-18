"""POST /api/author — generate a tool, PERSIST its file, register it.

The generated `code` (the actual tool body) is written to a real file under
server/authored/ and `tool['entry']` is pointed at it, so the dynamic loader
runs THAT FILE instead of throwing the code away and re-dispatching to a
pre-coded primitive. An advisory static scan (SPEC §10.2) is attached to the
provenance + preview.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import deps
from deps import fb
from envelopes import error_obj, with_envelope_fields
from tool_validate import static_scan

router = APIRouter()

SERVER_DIR = Path(__file__).resolve().parent.parent
AUTHORED_DIR = SERVER_DIR / "authored"

# App-proxy error name for "the tenant has not linked a Claude grant" (Concern-2).
# DELIBERATELY NOT added to the frozen §10 `error.error_code` enum — it is surfaced
# additively (top-level `grant_required: true` + `reason`) so the frontend can key on
# it while the §10 `error` object still carries a valid enum code (BAD_PARAMS). See
# CONTRACT-ADDENDUM §16.
GRANT_REQUIRED = "GRANT_REQUIRED"


class AuthorRequest(BaseModel):
    description: str


def _grant_required_response(tenant_id: str, harness_message: str | None) -> JSONResponse:
    """§16 grant-required envelope (HTTP 401). The token is NEVER involved here — this
    only signals that the tenant must 'sign in with Claude' before authoring."""
    msg = harness_message or (
        f"tenant {tenant_id!r} has no linked Claude grant — sign in with Claude to author tools."
    )
    body = with_envelope_fields({
        "tool": None,
        "code": None,
        "preview": "Sign in with Claude to author tools — this workspace has no linked Claude grant yet.",
        "source": "grant_required",
        "grant_required": True,
        "reason": GRANT_REQUIRED,
        "static_scan": [],
        # §10-valid error object (frozen enum) so strict §10 consumers see an error;
        # the frontend keys on the additive `grant_required` / `reason` fields.
        "error": error_obj("BAD_PARAMS", msg, retryable=False),
    })
    return JSONResponse(status_code=401, content=body)


@router.post("/api/author")
def author(req: AuthorRequest, tenant=Depends(deps.require_tenant)) -> Dict[str, Any]:
    """Generate a tool package from a description, PERSIST its code to a real
    file, register it so it appears in /api/tools, and return
    {tool, code, preview} (contract section 4)."""
    use_llm = os.environ.get("LEAF_AUTHOR_LLM", "0") == "1"

    # Env-gated seam to the Agent SDK author harness (harness/ - HARNESS-CONTRACT.md).
    # When LEAF_AUTHOR_HARNESS_URL is set, authoring is delegated to the harness, which
    # registers the tool into the TENANT repo (registry.json) itself. Any harness
    # failure falls back to the local templater (noted in the preview).
    tool = None
    source = "template"
    harness_url = os.environ.get("LEAF_AUTHOR_HARNESS_URL", "").rstrip("/")
    if harness_url:
        # Contract 6: forward the RESOLVED tenant so the harness resolves THAT tenant's
        # grant + repo (per-tenant author loop). The token is never sent by the app —
        # the harness owns the per-tenant grant store.
        resp = None
        try:
            import requests
            resp = requests.post(f"{harness_url}/author",
                                 json={"description": req.description,
                                       "tenant_id": str(tenant)}, timeout=120)
        except Exception as exc:
            print(f"[author] harness unreachable, templated fallback: {exc}")
        if resp is not None:
            try:
                jb = resp.json()
            except ValueError:
                jb = None
            # Contract 3 / §16: a missing per-tenant grant is a DISTINCT outcome — the
            # user must sign in with Claude. Short-circuit to the GRANT_REQUIRED shape;
            # do NOT fall back to the templater (that would hide the real reason).
            if resp.status_code in (401, 403) and isinstance(jb, dict) and jb.get("grant_required"):
                harness_msg = (jb.get("error") or {}).get("message")
                return _grant_required_response(str(tenant), harness_msg)
            try:
                resp.raise_for_status()
                body = jb if jb is not None else resp.json()
                tool, code, preview = body["tool"], body["code"], body["preview"]
                source = "harness"
            except Exception as exc:
                tool = None
                print(f"[author] harness error, templated fallback: {exc}")
    if tool is None:
        tool, code, preview = fb.author_tool(req.description)
        source = "template"
        if harness_url:
            preview = "[harness unreachable; templated fallback] " + preview
    if use_llm:
        # Real LLM authoring is optional and gated. No key wired here; we fall
        # back to templating and note it in the preview rather than fail.
        preview = "[LLM gate on, but no provider wired — templated] " + preview

    # advisory static scan (SPEC §10.2) — runs consistently for BOTH sources over the
    # tool's `code`; surfaced in provenance AND at the top level (Contract 3) + preview.
    findings = static_scan(code)
    tool.setdefault("provenance", {})["static_scan"] = findings
    if findings:
        preview = f"{preview}  [static-scan flags: {', '.join(findings)}]"

    if source == "harness":
        # The harness already registered this tool into the TENANT repo (its build
        # route commits registry.json + tools/<name>/). It surfaces in /api/tools via
        # deps.all_tools()'s tenant-repo fold (Contract 2) — NOT the local authored
        # store. So we do NOT persist to server/authored/ or touch _AUTHORED here; the
        # returned tool keeps its tenant-repo-relative entry.
        pass
    else:
        # TEMPLATE path (unchanged): PERSIST the authored file + point the registry
        # entry at it (the FILE is the tool). entry is stored relative to server/ so
        # the broker resolves it regardless of cwd. Register into our lane's store.
        AUTHORED_DIR.mkdir(parents=True, exist_ok=True)
        fname = f"{tool['name']}.py"
        (AUTHORED_DIR / fname).write_text(code, encoding="utf-8")
        tool["entry"] = f"authored/{fname}"
        deps._AUTHORED[:] = [t for t in deps._AUTHORED if t["name"] != tool["name"]]
        deps._AUTHORED.append(tool)
        deps.save_authored_tools(deps._AUTHORED)

    return deps.tenant_echo(
        with_envelope_fields({"tool": tool, "code": code, "preview": preview,
                              "source": source, "static_scan": findings}), tenant
    )
