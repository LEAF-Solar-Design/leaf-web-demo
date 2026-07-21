"""GET /api/site/* — the PUBLIC, unauthenticated site-facing namespace.

Consumed by the leaf_website Next.js app's cache-through routes
(app/api/site/{capabilities,demo-solve}/route.ts), which serve the marketing
stage and landing surfaces to anonymous visitors. Everything here must stay
safe to expose with no session: default-tenant catalog only, internal/QA
tools always filtered, and no per-tenant state.

demo-solve serves the canned July demo solve artifact (site_demo_solve.json,
sha-stamped inside the payload). The Solve lane does not execute yet
(CONTRACT-ADDENDUM: declared, honestly unimplemented) — when it does, this
endpoint should compute a fresh solve from the bundled sample intake instead
of replaying the artifact. Until then the artifact IS the contract: the
stage's intakeCache treats a 200 here as the non-degraded path.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException

import catalog
import deps
from envelopes import with_envelope_fields

router = APIRouter()

_DEMO_SOLVE_PATH = Path(__file__).resolve().parent.parent / "site_demo_solve.json"


@router.get("/api/site/capabilities")
def site_capabilities() -> Dict[str, Any]:
    """Default-tenant capability catalog, internal tools always excluded."""
    families = catalog.build_catalog(deps.all_tools(deps.DEFAULT_TENANT), include_internal=False)
    return with_envelope_fields({"families": families})


@router.get("/api/site/demo-solve")
def site_demo_solve() -> Dict[str, Any]:
    """The canned demo solve artifact the marketing stage renders."""
    try:
        return json.loads(_DEMO_SOLVE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise HTTPException(status_code=503, detail="site_demo_solve.json missing on this deployment")
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="site_demo_solve.json is not valid JSON")
