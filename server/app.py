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
from fastapi.responses import JSONResponse

import dependency_health
import deps
import jobs as job_store
import write_loop
from customization_flags import RolloutMode, mode as customization_mode
from customization_service import CustomizationService
from envelopes import install_error_handlers, with_envelope_fields
from routers import (
    agent,
    author,
    capabilities,
    drawings,
    jobs as jobs_router,
    ops,
    ops_metrics,
    prompt,
    session,
    sessions,
    site,
    tenant,
    tools,
    uploads,
    usage,
)

def _cors_origins() -> list[str]:
    """CORS allow-list, ENV-driven and default-deny in live-auth mode (F17).

    ``LEAF_CORS_ORIGINS`` — a comma-separated allow-list (e.g.
    ``https://app.leafdesign.ai,https://staging.leafdesign.ai``) — is
    authoritative in EVERY mode when set; blanks are trimmed out.

    When it is unset the fallback depends on the auth mode (``deps.auth_live()``,
    read at call time):
      * auth OFF (``LEAF_AUTH_LIVE=0`` — the local single-operator demo) → ``["*"]``
        so localhost dev keeps working (``allow_credentials`` stays ``False``, so a
        wildcard carries no cookie/credential risk).
      * auth ON  (``LEAF_AUTH_LIVE=1`` — live / public) → ``[]`` (DEFAULT-DENY): no
        bare ``*`` ever ships in live mode. The deploy config supplies the prod
        origin VALUE via ``LEAF_CORS_ORIGINS``.
    """
    raw = os.environ.get("LEAF_CORS_ORIGINS", "").strip()
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    # env unset: bare "*" ONLY when auth is off (local demo); default-deny when live.
    return ["*"] if not deps.auth_live() else []


app = FastAPI(title="Leaf Web Demo — Lane D backend", version="1.0.0")


@app.on_event("startup")
def initialize_customization_store() -> None:
    """SQLite coordination is intentionally a single-process deployment."""
    if (
        customization_mode("LEAF_CUSTOMIZATION_R5_MODE") is RolloutMode.OFF
        and customization_mode("LEAF_CUSTOMIZATION_R6_MODE") is RolloutMode.OFF
    ):
        return
    if not deps.auth_live():
        raise RuntimeError("customization requires live authentication")
    CustomizationService.configured()

# §19: byte-counting wall on the upload route — bounds multipart pre-parse
# disk use in-process (chunked bodies included); see UploadBodyLimitMiddleware.
# Registered BEFORE CORSMiddleware so CORS wraps it (last-added = outermost):
# the limiter's own 413 then carries CORS headers for permitted origins.
import guest_uploads as _guest_uploads_mw  # noqa: E402

app.add_middleware(_guest_uploads_mw.UploadBodyLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),  # F17: env-driven allow-list, default-deny in live-auth
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
install_error_handlers(app)

app.include_router(session.router)
app.include_router(sessions.router)  # sessions wire spec (S2): POST /api/sessions, .../messages, .../stream, .../transcript
app.include_router(agent.router)  # S4: POST /api/agent/approvals/{confirmation_id} (record-only)
app.include_router(tools.router)
app.include_router(jobs_router.router)
app.include_router(capabilities.router)
app.include_router(author.router)
app.include_router(drawings.router)  # M2 write loop: versioned drawing endpoints
app.include_router(prompt.router)  # M3: NL prompt router (MATRIX gap #2 — one prompt box -> lanes)
app.include_router(usage.router)  # UI wave 1: per-tenant spend/quota meter (GET /api/usage)
app.include_router(ops.router)  # UI wave 2: ops surface (role-gated tenant spend + kill-switch proxy)
app.include_router(ops_metrics.router)  # APS observability read-API: fleet metrics + in-flight tail + ledger<->job drill-down (X-Ops-Secret)
app.include_router(tenant.router)  # wave 4: per-tenant Claude grant linking (proxy to harness store)
app.include_router(site.router)  # public site-facing namespace for the leaf_website Next app (/api/site/*)
app.include_router(uploads.router)  # §19 guest/account drawing uploads (+ /api/site/guest-upload-policy in site.router)

# §19 retention promise-keeper: the purge daemon deletes expired guest drawings
# at their STAMPED expiry. Started UNCONDITIONALLY (review round 1, MAJOR):
# LEAF_GUEST_UPLOADS_ENABLED=0 stops NEW uploads, but retention promises
# already stamped on existing guest drawings must still be kept — disabling
# the feature never strands data past its promised deletion. A sweep failure
# logs and retries; it never kills the app.
import guest_uploads  # noqa: E402

# Validate an explicit shared upload authority before serving traffic. The
# database connection remains lazy, but an invalid or unsafe selector cannot
# degrade to per-process state.
guest_uploads.upload_store_mode()
write_loop.blob_store_mode()
guest_uploads.start_purge_daemon()


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
        import platform_link

        platform_link.validate_postgres_startup()
        job_store.validate_store_startup()
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
        if platform_link.postgres_startup_required():
            raise RuntimeError("required platform PostgreSQL startup check failed") from exc
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
        "source_sha": os.environ.get("LEAF_SOURCE_SHA", "unknown"),
    })


@app.get("/api/ready")
def ready() -> JSONResponse:
    report = dependency_health.readiness_report()
    return JSONResponse(
        status_code=200 if report["ready"] else 503,
        content=with_envelope_fields(report, degraded_mode=report["status"] != "ready"),
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("APP_PORT", "8130")))
