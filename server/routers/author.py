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
from pydantic import BaseModel

import deps
from deps import fb
from envelopes import with_envelope_fields
from tool_validate import static_scan

router = APIRouter()

SERVER_DIR = Path(__file__).resolve().parent.parent
AUTHORED_DIR = SERVER_DIR / "authored"


class AuthorRequest(BaseModel):
    description: str


@router.post("/api/author")
def author(req: AuthorRequest, tenant=Depends(deps.require_tenant)) -> Dict[str, Any]:
    """Generate a tool package from a description, PERSIST its code to a real
    file, register it so it appears in /api/tools, and return
    {tool, code, preview} (contract section 4)."""
    use_llm = os.environ.get("LEAF_AUTHOR_LLM", "0") == "1"

    # Env-gated seam to the Agent SDK author harness (harness/ - HARNESS-CONTRACT.md).
    # When LEAF_AUTHOR_HARNESS_URL is set, authoring is delegated to the harness and
    # the returned package flows through the SAME persistence/registration pipeline
    # below, so /api/tools and the dynamic loader treat both sources identically.
    # Any harness failure falls back to the local templater (noted in the preview).
    tool = None
    harness_url = os.environ.get("LEAF_AUTHOR_HARNESS_URL", "").rstrip("/")
    if harness_url:
        try:
            import requests
            r = requests.post(f"{harness_url}/author",
                              json={"description": req.description}, timeout=120)
            r.raise_for_status()
            body = r.json()
            tool, code, preview = body["tool"], body["code"], body["preview"]
        except Exception as exc:
            tool = None
            print(f"[author] harness unreachable, templated fallback: {exc}")
    if tool is None:
        tool, code, preview = fb.author_tool(req.description)
        if harness_url:
            preview = "[harness unreachable; templated fallback] " + preview
    if use_llm:
        # Real LLM authoring is optional and gated. No key wired here; we fall
        # back to templating and note it in the preview rather than fail.
        preview = "[LLM gate on, but no provider wired — templated] " + preview

    # PERSIST the authored file + point the registry entry at it (the FILE is
    # the tool). entry is stored relative to server/ so the broker resolves it
    # regardless of cwd.
    AUTHORED_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{tool['name']}.py"
    (AUTHORED_DIR / fname).write_text(code, encoding="utf-8")
    tool["entry"] = f"authored/{fname}"

    # advisory static scan (non-blocking v1): surface in provenance + preview
    findings = static_scan(code)
    tool.setdefault("provenance", {})["static_scan"] = findings
    if findings:
        preview = f"{preview}  [static-scan flags: {', '.join(findings)}]"

    # register (in-memory + persisted to our lane's store; additive)
    deps._AUTHORED[:] = [t for t in deps._AUTHORED if t["name"] != tool["name"]]
    deps._AUTHORED.append(tool)
    deps.save_authored_tools(deps._AUTHORED)

    return deps.tenant_echo(
        with_envelope_fields({"tool": tool, "code": code, "preview": preview}), tenant
    )
