"""POST /api/nl-prompt — natural-language prompt router (CONTRACT-ADDENDUM §12).

MATRIX frontend gap #2: the product's one prompt box dispatches across the
Run / Solve / Build lanes. This endpoint is the backend classifier.

    POST /api/nl-prompt  { "text": "count panels per layer" }
    -> §10-enveloped
       { "lane": "run", "tool": "count-by-layer", "params": {}, "confidence": 0.94,
         "rationale": "...", "error": null, "degraded_mode": false }

v1 is ZERO-LLM: `nl_router.classify` is a pure deterministic matcher over the
LIVE catalog (`deps.all_tools()`, internal/QA tools filtered as in
/api/capabilities). No model sits in the request hot path. See nl_router.py for
the documented optional LLM-classifier seam.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

import deps
from envelopes import ErrorCode, error_response, with_envelope_fields
from nl_router import classify

router = APIRouter()

# Bound the free-text prompt so an unbounded body can't exhaust the classifier /
# request pipeline (security-audit F15). 8_000 chars is far above any real prompt;
# an over-cap body is rejected at the validation layer before the endpoint runs.
MAX_PROMPT_TEXT = 8_000


class PromptRequest(BaseModel):
    text: str = Field(..., max_length=MAX_PROMPT_TEXT)


@router.post("/api/nl-prompt")
def nl_prompt(req: PromptRequest, tenant=Depends(deps.require_tenant)) -> Dict[str, Any]:
    text = (req.text or "").strip()
    if not text:
        return error_response(
            ErrorCode.BAD_PARAMS, "text must be a non-empty string", retryable=False
        )
    # Match against the live catalog at REQUEST time (dynamic — authored/write
    # tools included, internal/QA excluded inside classify()). SCOPED to the
    # requesting tenant: all_tools() with no argument defaults to demo-tenant and
    # would classify against, and reveal the existence of, that tenant's authored
    # tools to every caller. Pass the tenant so each caller sees only the shared
    # globals plus their own authored/repo tools.
    result = classify(text, deps.all_tools(deps.customization_tenant(tenant)))
    return with_envelope_fields(result)
