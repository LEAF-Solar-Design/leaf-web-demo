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
import json
import re
import sys
import threading
import urllib.request
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import dependency_health
import deps
import conversations as conversation_crud
import ios_ship_provider as ios_ship_provider_client
import ios_ship_callback_listener
import ios_surface_bridge
import glug_routes
import jobs as job_store
import write_loop
from customization_flags import RolloutMode, mode as customization_mode
from customization_service import CustomizationService
import customization_stage_worker
from envelopes import install_error_handlers, with_envelope_fields
from routers import (
    agent,
    author,
    builds as builds_router,
    cad_upload,
    capabilities,
    change_to_live as change_to_live_router,
    checkpoints,
    conversations as conversations_router,
    deployment_identity,
    demand,
    drawings,
    ios_ship,
    ios_ship_provider as ios_ship_provider_router,
    ios_surface,
    jobs as jobs_router,
    mcp_gateway,
    mcp_status,
    ops,
    ops_metrics,
    platform_customize as platform_customize_router,
    prompt,
    session,
    sessions,
    skills,
    site,
    telemetry,
    templates as templates_router,
    tenant,
    tools,
    uploads,
    usage, overlay)

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

_customization_worker_stop: threading.Event | None = None
_customization_worker_thread: threading.Thread | None = None
_ios_callback_tls_server: ios_ship_callback_listener.CallbackTlsServer | None = None


@app.on_event("startup")
def initialize_glug_mushy_control() -> None:
    """Fail startup closed when the durable Glug control rail is enabled."""
    if os.environ.get("GLUG_MUSHY_ENABLED", "").strip().lower() == "true":
        glug_routes.initialize_services()


@app.on_event("shutdown")
def stop_glug_mushy_control() -> None:
    glug_routes.shutdown_services()


@app.on_event("startup")
def initialize_ios_ship_provider() -> None:
    """Mount one HTTP dispatch and its callback verifier, or fail closed."""
    config = ios_ship_provider_client.ProviderConfig.from_environment()
    adapter = (ios_ship_provider_client.HttpProviderDispatch(config)
               if config is not None else None)
    ios_ship.set_dispatch(adapter.dispatch if adapter is not None else None)
    ios_ship.set_provider_readiness(adapter.readiness if adapter is not None else None)
    ios_ship_provider_router.set_config(config)


@app.on_event("startup")
def start_ios_ship_callback_tls() -> None:
    """Serve only provider callbacks on the private TLS listener when configured."""
    config = ios_ship_callback_listener.CallbackTlsConfig.from_environment()
    if config is None:
        return
    callback_app = FastAPI(title="Leaf iOS ship private callbacks")
    callback_app.include_router(ios_ship_provider_router.router)
    global _ios_callback_tls_server
    _ios_callback_tls_server = ios_ship_callback_listener.CallbackTlsServer(callback_app, config)
    _ios_callback_tls_server.start()


@app.on_event("shutdown")
def stop_ios_ship_callback_tls() -> None:
    global _ios_callback_tls_server
    if _ios_callback_tls_server is not None:
        _ios_callback_tls_server.stop()
    _ios_callback_tls_server = None


@app.on_event("startup")
def recover_session_request_journal() -> None:
    """Settle ambiguous execution and resume durable P5a queue rows."""
    import request_journal
    import turn_runner

    request_journal.validate_startup()
    turn_runner.recover_queued_turns()


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
    if os.environ.get("LEAF_CUSTOMIZATION_STAGE_WORKER_DISABLED", "0") == "1":
        return
    global _customization_worker_stop, _customization_worker_thread
    if _customization_worker_thread is not None and _customization_worker_thread.is_alive():
        return
    _customization_worker_stop = threading.Event()
    _customization_worker_thread = threading.Thread(
        target=customization_stage_worker.serve,
        kwargs={"stop_event": _customization_worker_stop},
        name="customization-stage-worker",
        daemon=True,
    )
    _customization_worker_thread.start()


@app.on_event("shutdown")
def stop_customization_stage_worker() -> None:
    global _customization_worker_stop, _customization_worker_thread
    if _customization_worker_stop is not None:
        _customization_worker_stop.set()
    if _customization_worker_thread is not None:
        _customization_worker_thread.join(timeout=2.0)
    _customization_worker_stop = None
    _customization_worker_thread = None

# §19: byte-counting wall on the upload route — bounds multipart pre-parse
# disk use in-process (chunked bodies included); see UploadBodyLimitMiddleware.
# Registered BEFORE CORSMiddleware so CORS wraps it (last-added = outermost):
# the limiter's own 413 then carries CORS headers for permitted origins.
import guest_uploads as _guest_uploads_mw  # noqa: E402

app.add_middleware(_guest_uploads_mw.UploadBodyLimitMiddleware)
# Same idea for converse messages, which carry inline image bytes: bound the
# stream before FastAPI reads it, and let FastAPI still do the parsing.
from routers import sessions as _sessions_mw  # noqa: E402

app.add_middleware(_sessions_mw.MessageBodyLimitMiddleware)
app.add_middleware(mcp_gateway.McpAuthorityBodyLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),  # F17: env-driven allow-list, default-deny in live-auth
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    # W4g-2 (engine reach): the DXF route's answer carries the version, the
    # head and the leg it took as headers, plus an ETag for the 304 path. A
    # cross-origin page (a split web/API deployment, the local proof stack)
    # only sees them when they are exposed; the client falls back to the head
    # it already holds, but the ETag round trip needs the real header.
    expose_headers=["ETag", "X-Leaf-Version", "X-Leaf-Head", "X-Leaf-Dxf-Source"],
)
install_error_handlers(app)

app.include_router(session.router)
app.include_router(sessions.router)  # sessions wire spec (S2): POST /api/sessions, .../messages, .../stream, .../transcript
app.include_router(checkpoints.router)  # session checkpoint metadata
app.include_router(conversation_crud.router)  # project conversation CRUD
app.include_router(conversations_router.router)  # B-C2: conversation message write path (POST .../messages)
app.include_router(skills.router)
app.include_router(agent.router)  # S4: POST /api/agent/approvals/{confirmation_id} (record-only)
app.include_router(tools.router)
app.include_router(jobs_router.router)
app.include_router(builds_router.router)  # slice 11a: GET /api/builds, one record shape over the broker, fleet and fold lanes
app.include_router(capabilities.router)
app.include_router(mcp_status.router)
app.include_router(mcp_gateway.router)
app.include_router(deployment_identity.router)
app.include_router(demand.router)  # public waitlist capture, no auth required
app.include_router(author.router)
app.include_router(platform_customize_router.router)  # W14 admin self-edit lane (R7): admin-tier + internal-mode gated, branch-only
app.include_router(drawings.router)  # M2 write loop: versioned drawing endpoints
app.include_router(prompt.router)  # M3: NL prompt router (MATRIX gap #2 — one prompt box -> lanes)
app.include_router(usage.router)  # UI wave 1: per-tenant spend/quota meter (GET /api/usage)
app.include_router(ops.router)  # UI wave 2: ops surface (role-gated tenant spend + kill-switch proxy)
app.include_router(ops_metrics.router)  # APS observability read-API: fleet metrics + in-flight tail + ledger<->job drill-down (X-Ops-Secret)
app.include_router(tenant.router)  # wave 4: per-tenant Claude grant linking (proxy to harness store)
app.include_router(site.router)  # public site-facing namespace for the leaf_website Next app (/api/site/*)
app.include_router(uploads.router)  # §19 guest/account drawing uploads (+ /api/site/guest-upload-policy in site.router)
# Dedicated CAD admissibility endpoint (POST /api/cad/upload), fail-closed behind
# LEAF_CAD_UPLOAD_ENABLED: mounted-but-disabled answers 503, so a 404 here now
# means UNMOUNTED, never "flag off". Distinct from the live DrawingUploadControl
# path above (POST /api/drawings/upload -> uploads.router + drawings.router),
# which owns extraction and tenancy; this one only proves a file is admissible
# and durably receipted.
app.include_router(cad_upload.router)
app.include_router(telemetry.router)  # P2 product-event ingest (always 202; identity server-stamped; docs/PLATFORM_TELEMETRY.md)
app.include_router(templates_router.router)  # Wave C solar template beta, fail-closed behind LEAF_SOLAR_TEMPLATE_BETA_ENABLED
app.include_router(overlay.router)  # T1 runtime overlay: propose a preview, decide it, read the resolved tokens
app.include_router(ios_ship.router)  # Wave D one-shot iOS: readiness, one reviewed idempotent launch, status, receipt
app.include_router(ios_ship_provider_router.router)  # internal provider callbacks, bearer-file auth only
app.include_router(ios_surface.router)  # Wave D consume-only iOS readiness surface, fail-closed behind LEAF_IOS_SURFACE_ENABLED (GET /api/ios-surface/status refuses 404 while off)
# slice 12a: GET /api/change-class (repo + shape -> delivery ladder, a pure
# function of the caller's own strings) + GET /api/receipts (the receipts that
# exist). Mounted unconditionally because the ADMISSION is per scope inside the
# router, not per mount: /api/receipts?scope=job: is tenant data bound to the
# caller, while scope=pr:/tree:/train read this repository's private CI state
# with the platform's own credential and require the platform_customize
# entitlement, the same gate GET /api/platform/source runs.
app.include_router(change_to_live_router.router)
app.include_router(glug_routes.router)
# SOURCE SEAM (ios_surface): fn({tenant_id, project_id, revision}) -> a sanitized
# leaf.ios-ship-surface.v1 dict. Wired to the INTERIM in-repo projection of the
# live ship-lane store (ios_surface_bridge), which reads readiness, the newest
# execution for the revision, and its TestFlight receipt. The bridge derives
# readiness.launchable from a TERMINAL BUILD EXISTING, never from ios_ship's
# launch eligibility -- the two spellings of "launchable" mean opposite things
# and copying it through would render "iOS app Ready" before any build had run.
# An external Apple pipeline can supersede this by re-calling
# set_contract_source(fn) with its own reader; nothing else has to change. No
# Apple credential ever reaches this consumer -- the bridge returns only the
# three published categories, and validate_contract fails the whole contract
# closed on any secret-shaped key or value.
ios_surface.set_contract_source(ios_surface_bridge.contract_source)

# §19 retention promise-keeper: the purge daemon deletes expired guest drawings
# at their STAMPED expiry. It starts by default even when
# LEAF_GUEST_UPLOADS_ENABLED=0 stops NEW uploads, because existing retention
# promises must still be kept. LEAF_GUEST_PURGE_DISABLED=1 is reserved for the
# pytest process. A sweep failure logs and retries; it never kills the app.
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

# P8 uses the same non-colliding ``leaf_platform`` package alias established by
# the platform mount. Import its private coordination router only afterwards.
from routers import project_repository_edit as project_repository_edit_router  # noqa: E402

app.include_router(project_repository_edit_router.router)


# --- operator control plane (contract/OPERATOR.md; Wave 1, dark by default) --- #
# Mounted ONLY when LEAF_OPERATOR_ENABLED=1: the release sequence merges the
# foundations behind a disabled flag, and the contract freeze gate
# (tests/test_operator_vocab_freeze.py) proves a default app registers no
# /api/operator route. A mount failure with the flag ON raises — a half-mounted
# operator surface must never boot.
def _mount_operator_router() -> None:
    if os.environ.get("LEAF_OPERATOR_ENABLED", "").strip() != "1":
        return
    from routers import operator_sessions as operator_sessions_router
    from routers import operator_runbooks as operator_runbooks_router
    from routers import operator_overlay as operator_overlay_router
    from routers import operator_secrets as operator_secrets_router
    from routers import operator_credential as operator_credential_router
    from routers import operator_external as operator_external_router
    from routers import operator_release as operator_release_router
    from routers import operator_worker as operator_worker_router

    app.include_router(operator_sessions_router.router)
    # Browser tenant controls share the operator feature gate. Their bearer
    # authority lives in the app, while the always-mounted /api/ops service
    # surface keeps its separate server-side credential contract.
    app.include_router(ops.operator_router)
    # Lane F reversible runbooks. Write actions are always-confirm and gated by
    # a one-use authority; they ship dark (enabled:false in operator_policy.json)
    # so mounting the routes grants nothing until an operator enables an action.
    app.include_router(operator_runbooks_router.router)
    app.include_router(operator_overlay_router.router)
    # Secret broker read surface: handle metadata only (never a value); the
    # broker is fail-closed (no minter registered by default).
    app.include_router(operator_secrets_router.router)
    # Credential-rotation runbook (O4): always-confirm, broker-verified
    # non-production precondition; rotation is DARK (no rotator registered).
    app.include_router(operator_credential_router.router)
    # External-write runbook (O5): always-confirm, allowlisted non-production
    # destination + broker-scoped token; DARK (no adapter ships, no minter).
    app.include_router(operator_external_router.router)
    # Staging release runbook (O6): always-confirm, STAGING ONLY, immutable
    # candidate per source SHA; DARK (no stager registered). Never reaches prod.
    app.include_router(operator_release_router.router)
    # Operator worker dispatch (Lane D, O2/O3): capability-only handler that
    # forwards a BOUNDED job over the secret-gated app->harness hop to the
    # isolated disposable worker. It never executes commands in this process,
    # and the harness side ships dark (501 until an isolating substrate is wired).
    app.include_router(operator_worker_router.router)
    print("[leaf-demo] operator control plane mounted (/api/operator/*)")


_mount_operator_router()


# One-time identity provisioning rail. Its dedicated flag is intentionally
# independent of LEAF_OPERATOR_ENABLED, so enabling this route does not expose
# the wider operator control plane and enabling that plane does not expose this
# mutation.
def _mount_operator_identity_binding_router() -> None:
    if os.environ.get(
            "LEAF_OPERATOR_IDENTITY_BINDING_ENABLED", "").strip() != "1":
        return
    from routers import operator_identity_bindings

    app.include_router(operator_identity_bindings.router)
    print("[leaf-demo] operator identity binding rail mounted")


_mount_operator_identity_binding_router()


def _drawing_mutation_fence_state() -> str:
    """Report the live shared-fence state without changing liveness."""
    configured = os.environ.get(
        "LEAF_DRAWING_MUTATIONS_FENCE_FILE", ""
    ).strip()
    if not configured:
        return "unconfigured"
    try:
        value = Path(configured).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return "unreadable"
    return {"0": "closed", "1": "open"}.get(value, "invalid")


def _ecs_task_definition_arn() -> str:
    base = os.environ.get("ECS_CONTAINER_METADATA_URI_V4", "")
    if not re.fullmatch(r"http://169\.254\.170\.2/v4/[A-Za-z0-9_-]+", base):
        return "unconfigured"
    try:
        with urllib.request.urlopen(f"{base}/task", timeout=1) as response:
            metadata = json.loads(response.read(65536))
        task = re.fullmatch(
            r"arn:aws:ecs:(?P<region>[a-z0-9-]+):(?P<account>[0-9]{12}):"
            r"task/(?:(?:[A-Za-z0-9_-]+)/)?[0-9a-f-]+",
            str(metadata.get("TaskARN", "")),
        )
        family = str(metadata.get("Family", ""))
        revision = str(metadata.get("Revision", ""))
        if task is None or not re.fullmatch(r"[A-Za-z0-9_-]+", family):
            return "unavailable"
        if not revision.isdigit() or int(revision) < 1:
            return "unavailable"
        return (
            f"arn:aws:ecs:{task.group('region')}:{task.group('account')}:"
            f"task-definition/{family}:{revision}"
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return "unavailable"


@app.get("/api/health")
def health() -> Dict[str, Any]:
    return with_envelope_fields({
        "ok": True,
        "aps_live": deps.APS_LIVE,
        "data_file_present": deps.DATA_FILE.exists(),
        "engine_registry_present": deps.ENGINE_REGISTRY.exists(),
        "da_client_present": (deps.DA_DIR / "client.py").exists(),
        # Liveness is process-scoped. Do not select the default tenant here:
        # authored mode can correctly have no effective catalog for that tenant
        # yet, and that request-scoped state must not make the process unhealthy.
        "n_tools": len(deps.shared_tools()),
        "n_authored": len(deps._AUTHORED),
        "source_sha": os.environ.get("LEAF_SOURCE_SHA", "unknown"),
        "drawing_mutation_fence_state": _drawing_mutation_fence_state(),
        "drawing_store_authority": os.environ.get(
            "LEAF_DRAWING_STORE", "legacy"
        ).strip().lower(),
        "upload_store_authority": os.environ.get(
            "LEAF_UPLOAD_STORE", "legacy"
        ).strip().lower(),
        "task_definition_arn": _ecs_task_definition_arn(),
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
