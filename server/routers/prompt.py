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

from fastapi import APIRouter
from pydantic import BaseModel

import deps
from envelopes import ErrorCode, error_response, with_envelope_fields
from nl_router import classify

router = APIRouter()


class PromptRequest(BaseModel):
    text: str


@router.post("/api/nl-prompt")
def nl_prompt(req: PromptRequest) -> Dict[str, Any]:
    text = (req.text or "").strip()
    if not text:
        return error_response(
            ErrorCode.BAD_PARAMS, "text must be a non-empty string", retryable=False
        )
    # Match against the live catalog at REQUEST time (dynamic — authored/write
    # tools included, internal/QA excluded inside classify()).
    result = classify(text, deps.all_tools())
    return with_envelope_fields(result)
