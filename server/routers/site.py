"""GET /api/site/* — the PUBLIC, unauthenticated site-facing namespace.

Consumed by the leaf_website Next.js app's cache-through routes
(app/api/site/{capabilities,demo-solve}/route.ts), which serve the marketing
stage and landing surfaces to anonymous visitors. Everything here must stay
safe to expose with no session: default-tenant catalog only, internal/QA
tools always filtered, and no per-tenant state.

BUILTIN-ONLY (merge-gate finding #6): site_capabilities() must NEVER expose
an authored/runtime tool — a tenant's own harness-authored tool (folded in
per-tenant by deps.load_tenant_repo_tools()) or anything in the
process-global deps._AUTHORED overlay (both populated at RUNTIME by real
harness author sessions, per deps.all_tools()'s own docstring). deps.py's
/health endpoint (app.py) already draws exactly this line -- `n_tools`
(deps.all_tools(), the full union) vs `n_authored` (len(deps._AUTHORED), the
authored overlay alone) -- this module reuses that same split: the catalog
here is built from ONLY the two static/tracked loaders, deps.load_engine_
registry_tools() (engine/registry.json, falling back to the built-in
DEFAULT_TOOLS) and deps.load_seed_write_tools() (the tracked write_tools.json
seed), deliberately excluding load_tenant_repo_tools() and _AUTHORED.

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


def _builtin_tools() -> list:
    """The BASE/builtin tool set only — see the module docstring's BUILTIN-ONLY
    note. Same de-dupe-by-name posture as deps.all_tools() (last loader wins),
    just restricted to the two static/tracked loaders."""
    by_name: Dict[str, Dict[str, Any]] = {}
    for t in deps.load_engine_registry_tools():
        by_name[t["name"]] = t
    for t in deps.load_seed_write_tools():
        by_name[t["name"]] = t
    return list(by_name.values())


@router.get("/api/site/capabilities")
def site_capabilities() -> Dict[str, Any]:
    """Default-tenant capability catalog, internal tools always excluded,
    BUILTIN tools only — no authored/runtime tool ever reaches this public,
    unauthenticated surface (see module docstring)."""
    families = catalog.build_catalog(_builtin_tools(), include_internal=False)
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
