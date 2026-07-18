"""
Leaf web demo — backend composition root (FastAPI).

STABLE FILE: after the STEP 0 router split, no sibling session restructures
this file; add routes to your OWN router (see routers/__init__.py ownership map)
and shared seams to deps.py.

Run (two processes; see README.md):
    cd server && APS_LIVE=0 uvicorn app:app --port 8130
    cd server && uvicorn broker:app --port 8140

Env:
    APS_LIVE=0  (default)  -> cached sample intake + pure-python tool logic
    APS_LIVE=1             -> tool runs route through the broker to APS DA
    APP_PORT=8130          -> port for `python app.py`
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import deps
from envelopes import install_error_handlers, with_envelope_fields
from routers import author, capabilities, drawings, jobs, session, tools

app = FastAPI(title="Leaf Web Demo — Lane D backend", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # permissive for localhost dev
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
install_error_handlers(app)

app.include_router(session.router)
app.include_router(tools.router)
app.include_router(jobs.router)
app.include_router(capabilities.router)
app.include_router(author.router)
app.include_router(drawings.router)  # M2 write loop: versioned drawing endpoints


# --- platform Project/Job router (org-scoped persistence; platform/README.md) --- #
# Loaded under the `leaf_platform` alias because the directory name shadows the
# stdlib `platform` module (mechanism mirrors platform/tests/conftest.py).
# Mounted LAST deliberately: the async spine keeps precedence on
# GET /api/jobs/{job_id}; the platform Job row is served via
# /api/projects/{project_id}/jobs. DB connects lazily (DATABASE_URL env or
# platform/.env.local); import failure only logs — the demo runs without it.
def _mount_platform_router() -> None:
    import importlib.util
    from pathlib import Path

    pkg_dir = Path(__file__).resolve().parent.parent / "platform"
    try:
        if "leaf_platform" not in sys.modules:
            spec = importlib.util.spec_from_file_location(
                "leaf_platform", pkg_dir / "__init__.py",
                submodule_search_locations=[str(pkg_dir)])
            mod = importlib.util.module_from_spec(spec)
            sys.modules["leaf_platform"] = mod
            spec.loader.exec_module(mod)
        from leaf_platform.api import router as platform_router
        app.include_router(platform_router)
        print("[leaf-demo] platform router mounted (/api/projects, /api/orgs)")
    except Exception as exc:  # pragma: no cover - env-dependent
        print(f"[leaf-demo] platform router NOT mounted: {exc}", file=sys.stderr)


_mount_platform_router()


@app.get("/api/health")
def health() -> Dict[str, Any]:
    return with_envelope_fields({
        "ok": True,
        "aps_live": deps.APS_LIVE,
        "data_file_present": deps.DATA_FILE.exists(),
        "engine_registry_present": deps.ENGINE_REGISTRY.exists(),
        "da_client_present": (deps.DA_DIR / "client.py").exists(),
        "n_tools": len(deps.all_tools()),
        "n_authored": len(deps._AUTHORED),
    })


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("APP_PORT", "8130")))
