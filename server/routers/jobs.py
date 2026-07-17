"""Async job spine endpoints (this session). Empty at STEP 0; the async
POST /api/run override, GET /api/jobs/*, and SSE stream land here next."""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()
