"""POST /api/author — moved verbatim from app.py."""
from __future__ import annotations

import os
from typing import Any, Dict

from fastapi import APIRouter
from pydantic import BaseModel

import deps
from deps import fb
from envelopes import with_envelope_fields

router = APIRouter()


class AuthorRequest(BaseModel):
    description: str


@router.post("/api/author")
def author(req: AuthorRequest) -> Dict[str, Any]:
    """Generate a tool package from a description, register it so it appears in
    /api/tools, and return {tool, code, preview} (contract section 4)."""
    use_llm = os.environ.get("LEAF_AUTHOR_LLM", "0") == "1"
    tool, code, preview = fb.author_tool(req.description)
    if use_llm:
        # Real LLM authoring is optional and gated. No key wired here; we fall
        # back to templating and note it in the preview rather than fail.
        preview = "[LLM gate on, but no provider wired — templated] " + preview

    # register (in-memory + persisted to our lane's store; additive)
    deps._AUTHORED[:] = [t for t in deps._AUTHORED if t["name"] != tool["name"]]
    deps._AUTHORED.append(tool)
    deps.save_authored_tools(deps._AUTHORED)

    return with_envelope_fields({"tool": tool, "code": code, "preview": preview})
