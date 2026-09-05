"""Platform job authority adapter.

This is the piece that makes the Projects workspace non-hollow: when a run is
submitted WITH project context (the new optional ``X-Org-Id`` + ``X-Project-Id``
headers on ``POST /api/run``), the async spine records a canonical
``leaf_platform`` **Job** row (``kind="run"``, ``tool_name``, ``params``,
``spine_ref=<spine job_id>``) whose ``status`` tracks the run
(``queued → running → succeeded|failed``), with ``result`` = the §3 envelope on
success and ``cost_usd`` = the envelope's ``cost.usd_est``.

STRICTLY BEST-EFFORT + ENV-GATED. Every entry point is a no-op unless BOTH:

  * the caller supplied project context (both headers present), AND
  * a platform ``DATABASE_URL`` resolves (env or ``platform/.env.local``).

Any DB / import error logs **exactly one line** and NEVER propagates — a linkage
failure must never affect the run. So the DB-less demo (no headers OR no DB) is
BYTE-IDENTICAL to before (``server/tests/test_backbone.py`` depends on this): the
spine INSERT, execution, and HTTP bodies are untouched.

Correlation is DURABLE (wave 3, Contract 5a): the row is CREATED via
``leaf_platform.store.create_job`` (org-scoped + carrying ``spine_ref=<spine job_id>``),
and the running/terminal transitions UPDATE ``WHERE spine_ref = <spine job_id>`` — so
the sync SURVIVES a full app-process restart (the in-process map is gone after a
restart, but the durable row is still found by its unique ``spine_ref``, e.g. when the
orphan reaper terminates a restarted job). ``store.py`` has no update path and is not
this lane's to edit, so the UPDATE is issued here. A vestigial in-process ``_MAP`` is
still populated on submit (cheap within-process correlation) but the terminal sync no
longer DEPENDS on it. ``spine_ref`` is a globally-unique uuid4, so a spine-ref-scoped
UPDATE targets exactly one row (no cross-org contamination).

Imports the platform package under the ``leaf_platform`` alias, exactly as
``server/app.py`` does (the directory name ``platform/`` shadows the stdlib).
"""
from __future__ import annotations

import os
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

_lock = threading.Lock()
# spine_job_id -> {"platform_job_id": uuid.UUID, "org_id": uuid.UUID}
_MAP: Dict[str, Dict[str, Any]] = {}

_pkg: Optional[tuple] = None  # (store, db, platform_deps) once loaded

# spine terminal status -> platform JOB_STATUSES
_STATUS_MAP = {"complete": "succeeded", "failed": "failed"}


class ProjectSessionForbidden(PermissionError):
    """The current verified identity lacks the required project role."""


def _log(msg: str) -> None:
    print(f"[platform-link] {msg}", file=sys.stderr)


def _ensure_platform_package() -> None:
    """Load the platform package alias without importing its API dependencies."""
    import importlib.util

    if "leaf_platform" in sys.modules:
        return
    pkg_dir = Path(__file__).resolve().parent.parent / "platform"
    spec = importlib.util.spec_from_file_location(
        "leaf_platform",
        pkg_dir / "__init__.py",
        submodule_search_locations=[str(pkg_dir)],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["leaf_platform"] = mod
    spec.loader.exec_module(mod)


def _load_platform():
    """Load leaf_platform.{store,db} under the non-colliding alias (memoized).

    Raises on any import problem — callers wrap this. Never connects to the DB
    (the pool is lazy); import only pulls in psycopg/dataclasses.
    """
    global _pkg
    if _pkg is not None:
        return _pkg
    _ensure_platform_package()
    import leaf_platform.db as db  # noqa: PLC0415
    import leaf_platform.store as store  # noqa: PLC0415
    import leaf_platform.deps as platform_deps  # noqa: PLC0415

    _pkg = (store, db, platform_deps)
    return _pkg


def platform_store():
    """Return the canonical store through the collision-safe package alias."""
    store, _db, _platform_deps = _load_platform()
    return store


def platform_db():
    """Return the canonical database module through the collision-safe alias."""
    _ensure_platform_package()
    import leaf_platform.db as db  # noqa: PLC0415

    return db


def unit_economics_store():
    """Return the fleet unit-economics store through the canonical package alias."""
    _ensure_platform_package()
    import leaf_platform.unit_economics as store  # noqa: PLC0415

    return store


def overlay_store():
    """The T1 overlay store, through THE ONE package alias.

    This module already registers the repo's platform package under
    `leaf_platform` by file location, which no sys.path order can shadow —
    the property the T1 lane needed after `from platform import overlay_store`
    resolved to the stdlib module in the container and 500'd every request.

    It exists so there is exactly ONE alias in the process. A second
    file-location loader under a different name (the router briefly carried
    `leaf_platform_pkg`) imports the package twice: two `db` modules, two
    connection POOLS, and two copies of every module-level cache — the
    shadowing bug traded for a resource one (sol-critic PR #439 round 6).
    """
    _ensure_platform_package()
    import leaf_platform.overlay_store as store  # noqa: PLC0415

    return store


def resolve_caller_binding(tenant: Any) -> Any:
    """Resolve the calling identity's binding ONCE, for callers that will
    check several projects in the same request (review finding 4: the
    binding does not depend on the project, so re-resolving it per project on
    a list page is up to LIST_MAX_LIMIT redundant round trips). Raises
    ``ProjectSessionForbidden`` on no verified identity, mirroring
    ``require_project_access``'s own guard so a caller that skips memoizing
    sees the identical failure mode."""
    subject = getattr(tenant, "subject", None)
    if not isinstance(subject, str) or not subject:
        raise ProjectSessionForbidden("project access requires a verified identity")
    _ensure_platform_package()
    store, _db, _platform_deps = _load_platform()
    return store.resolve_active_identity_binding("auth0", subject)


def require_project_access(tenant: Any, project_id: Any, *, write: bool,
                           binding: Any = None) -> str:
    """Return the canonical org id after a fresh project-membership check.

    ``tenant`` is the verified ``TenantContext`` supplied by server deps.  The
    client never supplies an org or binding id.  A missing or cross-tenant
    project returns ``LookupError`` so routes can preserve their identical 404
    shape.  A same-tenant actor with the wrong or revoked role raises the
    explicit forbidden type.

    ``binding`` lets a caller checking several projects in one request (the
    session list route) supply the identity binding ``resolve_caller_binding``
    already resolved once, instead of this function re-resolving it per
    project (review finding 4). ``None`` (every other caller) resolves it
    here exactly as before.
    """
    _ensure_platform_package()
    import leaf_platform.project_lifecycle as lifecycle  # noqa: PLC0415
    try:
        org_id = uuid.UUID(str(tenant))
        project_uuid = uuid.UUID(str(project_id))
        resolved = binding if binding is not None else resolve_caller_binding(tenant)
        if resolved is None or resolved.platform_tenant_id != org_id:
            raise LookupError("project session is unavailable")
        lifecycle.require_project_role(
            org_id, project_uuid, resolved.binding_id, write=write,
        )
        return str(org_id)
    except lifecycle.LifecycleUnavailable as exc:
        raise LookupError("project session is unavailable") from exc
    except lifecycle.LifecycleForbidden as exc:
        raise ProjectSessionForbidden(str(exc)) from exc
    except (ValueError, AttributeError) as exc:
        raise LookupError("project session is unavailable") from exc


def require_project_session_access(
    session: Optional[Dict[str, Any]], tenant: Any, *, write: bool,
    binding: Any = None,
) -> Optional[Dict[str, Any]]:
    """Authorize one current session operation without changing legacy rows.

    ``binding``: see ``require_project_access``.
    """
    if session is None or str(session.get("tenant_id")) != str(tenant):
        return None
    org_id = session.get("org_id")
    project_id = session.get("project_id")
    if org_id is None and project_id is None:
        return session
    if org_id is None or project_id is None or str(org_id) != str(tenant):
        return None
    if binding is None:
        require_project_access(tenant, project_id, write=write)
    else:
        require_project_access(tenant, project_id, write=write, binding=binding)
    return session


def _db_configured() -> bool:
    """True iff the platform package imports AND a DATABASE_URL resolves (env or
    platform/.env.local). Never raises; a False here is the silent no-op path
    (the DB-less demo) — no per-run logging."""
    try:
        _store, db, _platform_deps = _load_platform()
        db.get_database_url()  # RuntimeError if unset
        return True
    except Exception:
        return False


def postgres_required() -> bool:
    """Explicit production gate. Default false preserves the database-free demo."""
    return os.environ.get("LEAF_PLATFORM_POSTGRES_REQUIRED", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


_AUTHORITY_SELECTORS = {
    "LEAF_JOBS_STORE": ({"legacy", "postgres"}, {"postgres"}),
    "LEAF_SESSIONS_STORE": (
        {"legacy", "dual_write", "dual_write_shadow", "shadow", "postgres"},
        {"dual_write", "dual_write_shadow", "shadow", "postgres"},
    ),
    "LEAF_SESSION_ANNEX_STORE": (
        {"legacy", "dual_write", "dual_write_shadow", "shadow", "postgres"},
        {"dual_write", "dual_write_shadow", "shadow", "postgres"},
    ),
    "LEAF_AGENT_STORE": ({"legacy", "postgres"}, {"postgres"}),
    "LEAF_GUEST_CAP_STORE": ({"memory", "postgres"}, {"postgres"}),
    "LEAF_DRAWING_STORE": ({"legacy", "postgres"}, {"postgres"}),
    "LEAF_UPLOAD_STORE": ({"legacy", "postgres"}, {"postgres"}),
}


def postgres_authorities_selected() -> bool:
    """Validate app authority selectors and report whether any needs PostgreSQL."""
    selected = False
    for name, (allowed, postgres_values) in _AUTHORITY_SELECTORS.items():
        default = "memory" if name == "LEAF_GUEST_CAP_STORE" else "legacy"
        value = os.environ.get(name, default).strip().lower()
        if value not in allowed:
            choices = ", ".join(sorted(allowed))
            raise RuntimeError(f"{name} must be one of: {choices}")
        selected = selected or value in postgres_values
    return selected


def postgres_startup_required() -> bool:
    """True when the explicit gate or any selected authority requires PostgreSQL."""
    return postgres_required() or postgres_authorities_selected()


def validate_canonical_upload_authority() -> None:
    """A required canonical platform must persist import proof in PostgreSQL."""
    if not postgres_required():
        return
    for name in ("LEAF_DRAWING_STORE", "LEAF_UPLOAD_STORE"):
        if os.environ.get(name, "legacy").strip().lower() != "postgres":
            raise RuntimeError(
                f"LEAF_PLATFORM_POSTGRES_REQUIRED requires {name}=postgres")
    if os.environ.get("LEAF_BLOB_STORE", "legacy").strip().lower() != "filesystem":
        raise RuntimeError(
            "LEAF_PLATFORM_POSTGRES_REQUIRED requires LEAF_BLOB_STORE=filesystem")
    if not os.environ.get("LEAF_DRAWING_MUTATIONS_FENCE_FILE", "").strip():
        raise RuntimeError(
            "LEAF_PLATFORM_POSTGRES_REQUIRED requires "
            "LEAF_DRAWING_MUTATIONS_FENCE_FILE")


def validate_session_annex_authority() -> None:
    """A PostgreSQL sessions authority must not leave its annex on SQLite.

    The failure this prevents is user-visible rather than internal. With
    ``LEAF_SESSIONS_STORE=postgres`` a session survives task replacement, while
    ``session_checkpoints`` and ``session_policies`` live in the SQLite file at
    ``SESSIONS_DB`` for every annex mode except ``postgres`` -- and staging
    leaves ``SESSIONS_DB`` unset, so that file is task-local. The session then
    outlives its own restore points: checkpoint reads 404 and a custom policy
    silently reverts to ``confirm_all``.

    ``postgres`` exactly, not "any PostgreSQL-touching mode". Under
    ``dual_write`` and both shadow modes the annex still READS SQLite, so they
    do not fix the ephemerality; only authority does.

    This is the executable half of the selector dependency recorded in
    platform/authority-inventory.json, matching how
    ``validate_canonical_upload_authority`` backs the upload/drawing one.
    """
    if os.environ.get("LEAF_SESSIONS_STORE", "legacy").strip().lower() != "postgres":
        return
    annex = os.environ.get("LEAF_SESSION_ANNEX_STORE", "legacy").strip().lower()
    if annex != "postgres":
        raise RuntimeError(
            "LEAF_SESSIONS_STORE=postgres requires LEAF_SESSION_ANNEX_STORE=postgres"
            f" (got {annex!r}); otherwise session checkpoints and policies stay on"
            " the SQLite file at SESSIONS_DB and do not survive task replacement"
        )


def validate_postgres_startup() -> Optional[Dict[str, Any]]:
    """Fail closed before serving when any selected authority needs PostgreSQL."""
    if (
        os.environ.get("LEAF_RUNTIME_ENV", "").strip().lower() == "production"
        and not postgres_required()
    ):
        raise RuntimeError(
            "production app requires LEAF_PLATFORM_POSTGRES_REQUIRED=1")
    if not postgres_startup_required():
        return None
    validate_canonical_upload_authority()
    validate_session_annex_authority()
    if postgres_required():
        # Lazy: deps imports platform_link lazily too, so this cannot cycle.
        # deps.auth_live() is THE canonical LEAF_AUTH_LIVE parser (broker and
        # checkout_capability delegate to it; platform/deps.py mirrors it under
        # a drift guard) — this assertion used to keep its own permissive copy
        # of the spelling set, which is how `LEAF_AUTH_LIVE=true` passed here
        # and disabled auth everywhere else.
        import deps as _deps  # noqa: PLC0415

        if not _deps.auth_live():
            raise RuntimeError(
                "LEAF_PLATFORM_POSTGRES_REQUIRED requires LEAF_AUTH_LIVE=1")
    if not os.environ.get("DATABASE_URL", "").strip():
        raise RuntimeError(
            "LEAF_PLATFORM_POSTGRES_REQUIRED requires DATABASE_URL in the environment")
    _store, db, _platform_deps = _load_platform()
    return db.assert_schema_current()


# --------------------------------------------------------------------------- #
# entry points (all no-op-safe; called from server/jobs.py)
# --------------------------------------------------------------------------- #
def resolve_submission_context(org_id: Optional[str], project_id: Optional[str],
                               authorization: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Resolve project ownership and authority through the canonical boundary."""
    if not project_id:
        if org_id:
            raise ValueError("X-Project-Id is required with X-Org-Id")
        return None
    if not _db_configured():
        raise ValueError("platform database is required for a project-scoped run")
    store, _db, platform_deps = _load_platform()
    if platform_deps.auth_live():
        resolved_org = platform_deps.get_write_org_id(
            x_org_id=None, authorization=authorization)
        if org_id is not None and uuid.UUID(str(org_id)) != resolved_org:
            raise ValueError("project context does not belong to the verified platform tenant")
    else:
        if not org_id:
            raise ValueError("X-Org-Id is required for a project-scoped run")
        resolved_org = uuid.UUID(str(org_id))
    resolved_project = uuid.UUID(str(project_id))
    if store.get_project(resolved_org, resolved_project) is None:
        raise ValueError("project context was not found for the verified platform tenant")
    authority_mode = store.get_authority_mode(resolved_org, resolved_project)
    if authority_mode != "postgres_canonical":
        raise ValueError(
            f"{authority_mode} project-scoped Marathon dispatch is locked; select "
            "postgres_canonical only after the durable worker is available")
    return {"org_id": resolved_org, "project_id": resolved_project,
            "authority_mode": authority_mode}


def _canonical_jobs_module():
    _load_platform()
    import leaf_platform.canonical_jobs as canonical_jobs  # noqa: PLC0415
    return canonical_jobs


class CanonicalEntitlementDenied(Exception):
    """The STORED platform org is not entitled to canonical submission.

    Defined here (not imported from the platform package) so the route layer
    can catch it even in a DB-less process where leaf_platform never loads.
    ``response`` is the documented 403/503 denial envelope, returned verbatim.
    """

    def __init__(self, response: Any):
        super().__init__("canonical entitlement denied")
        self.response = response


def submit_canonical_solve(context: Dict[str, Any], request_tenant_id: str,
                           tool_name: str, params: Dict[str, Any],
                           idempotency_key: Optional[str], input_version_id: str) -> str:
    """Submit directly to PostgreSQL; never creates or mirrors a SQLite row.

    Entitlement enforcement (P1 floor) happens INSIDE ``submit_solve_job``
    against the stored org row; a denial surfaces as the typed
    ``CanonicalEntitlementDenied`` (NOT best-effort-swallowed — enforcement is
    the one linkage concern that must affect the run)."""
    if context.get("authority_mode") != "postgres_canonical":
        raise ValueError("canonical solve submission requires postgres_canonical authority")
    _load_platform()
    import leaf_platform.entitlements as platform_entitlements  # noqa: PLC0415

    if tool_name == "arlo-design":
        from leaf_platform.arlo_lab import load_registered_request
        load_registered_request({**context, "input_version_id": input_version_id}, params)

    try:
        job = _canonical_jobs_module().submit_solve_job(
            context["org_id"], context["project_id"], str(request_tenant_id), tool_name,
            dict(params), idempotency_key, input_version_id=uuid.UUID(str(input_version_id)))
    except platform_entitlements.EntitlementDenied as exc:
        raise CanonicalEntitlementDenied(exc.response) from None
    return str(job["job_id"])


def _canonical_record(record: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if record is None:
        return None
    rec = dict(record)
    status = str(rec.get("status", ""))
    rec["status"] = {"queued": "submitted", "succeeded": "complete"}.get(status, status)
    rec["tenant_id"] = str(rec.pop("request_tenant_id", rec.get("tenant_id", "")))
    rec.setdefault("progress", "done" if rec["status"] == "complete" else rec["status"])
    rec.setdefault("elapsed_ms", None)
    rec.setdefault("lease", ({"owner": rec.get("lease_owner"),
                               "expires_at": rec.get("lease_expires_at"),
                               "heartbeat_at": rec.get("heartbeat_at")}
                              if rec.get("lease_owner") else None))
    return rec


def get_canonical_job(job_id: str, request_tenant_id: str) -> Optional[Dict[str, Any]]:
    try:
        return _canonical_record(_canonical_jobs_module().get_job_for_tenant(
            uuid.UUID(str(job_id)), str(request_tenant_id)))
    except (ValueError, RuntimeError):
        return None


def list_canonical_jobs(request_tenant_id: str, limit: int = 20) -> list[Dict[str, Any]]:
    try:
        return [_canonical_record(item) for item in
                _canonical_jobs_module().list_jobs_for_tenant(str(request_tenant_id), limit=limit)]
    except RuntimeError:
        return []


def canonical_worker_health(tool_name: str) -> Optional[Dict[str, Any]]:
    try:
        return _canonical_jobs_module().worker_health(tool_name)
    except RuntimeError:
        return None


def on_submit(spine_job_id: str, org_id: Optional[str], project_id: Optional[str],
              tool_name: Optional[str], params: Optional[Dict[str, Any]], *,
              context: Optional[Dict[str, Any]] = None) -> None:
    """Record a canonical platform Job for a run submitted WITH project context.

    No-op unless both headers are present AND the DB resolves. On success the
    spine_job_id -> platform-job mapping is stored so the terminal sync can find
    it. Any error logs one line and is swallowed — the run is never affected.
    """
    if context is None and (not org_id or not project_id):
        return  # no project context -> byte-identical legacy path
    if not _db_configured():
        return  # no DB -> byte-identical legacy path (silent)
    try:
        store, _db, _platform_deps = _load_platform()
        oid = (context or {}).get("org_id") or uuid.UUID(str(org_id))
        pid = (context or {}).get("project_id") or uuid.UUID(str(project_id))
        job = store.create_job(
            oid, pid, "run",
            tool_name=tool_name,
            params=dict(params or {}),
            spine_ref=str(spine_job_id),
        )
        with _lock:
            _MAP[str(spine_job_id)] = {"platform_job_id": job.job_id, "org_id": oid}
    except Exception as exc:  # noqa: BLE001
        _log(f"submit link failed (run unaffected): {type(exc).__name__}: {exc}")


def on_running(spine_job_id: str) -> None:
    """Flip the linked platform Job to 'running' by its durable ``spine_ref``.

    Gated on a resolvable platform DB; a spine_ref with no matching row (e.g. a run
    submitted WITHOUT project headers) is a harmless 0-row UPDATE. Never raises."""
    if not _db_configured():
        return
    try:
        _update_by_spine(spine_job_id, status="running")
    except Exception as exc:  # noqa: BLE001
        _log(f"running link failed (run unaffected): {type(exc).__name__}: {exc}")


def mirror_configured() -> bool:
    """True when a terminal mirror would actually be attempted for this process.

    Lets the SQLite authority decide, INSIDE its own transaction, whether a row
    needs an outstanding-mirror marker. Without this the DB-less demo would take
    a marker write and an immediate clearing write on every terminal callback.
    """
    return _db_configured()


def try_terminal(spine_job_id: str, spine_status: str,
                 result_env: Optional[Dict[str, Any]] = None,
                 error: Optional[Dict[str, Any]] = None) -> bool:
    """Attempt the terminal mirror and raise when delivery fails.

    Returns True when no mirror is needed (no linkage configured) or the UPDATE
    committed. The SQLite authority cannot share a transaction with PostgreSQL,
    so it records an outstanding-mirror marker in its OWN terminal transaction
    and clears it only after this call returns. A raised failure leaves durable
    work for the sweep to retry.

    Contrast ``terminal_in_transaction``, which CAN share the authority's
    transaction because there both tables live in the same database.
    """
    _pop(spine_job_id)  # housekeeping only: drop any in-process correlation (not depended on)
    if not _db_configured():
        return True
    status, result_json, cost_usd = _terminal_fields(spine_status, result_env)
    _update_by_spine(spine_job_id, status=status, result=result_json, cost_usd=cost_usd)
    return True


def on_terminal(spine_job_id: str, spine_status: str,
                result_env: Optional[Dict[str, Any]] = None,
                error: Optional[Dict[str, Any]] = None) -> None:
    """Sync the linked platform Job to its terminal state by its durable ``spine_ref``.

    succeeded/failed from the spine's complete/failed; on success ``result`` = the
    §3 envelope and ``cost_usd`` = ``result_env['cost']['usd_est']`` (may be null for a
    mock run). Finds the row by ``spine_ref`` — so it works even after an app restart
    dropped the in-process map (the durable-sync property, Contract 5a). Best-effort +
    env-gated; a spine_ref with no matching row is a harmless 0-row UPDATE. Never raises.

    Delegates to the raising ``try_terminal`` boundary and swallows only here,
    so legacy callers stay best-effort while durable callers can retain retry
    work on failure.
    """
    try:
        try_terminal(spine_job_id, spine_status, result_env, error)
    except Exception as exc:  # noqa: BLE001
        _log(f"terminal link failed (run unaffected): {type(exc).__name__}: {exc}")


def forget(spine_job_id: str) -> None:
    """Drop any in-process correlation for a spine job. Housekeeping only.

    Callers that sync the platform row inside the authority's own transaction (see
    ``terminal_in_transaction``) still need this, because they do not go through
    ``on_terminal``.
    """
    _pop(spine_job_id)


def terminal_in_transaction(conn, spine_job_id: str, spine_status: str,
                            result_env: Optional[Dict[str, Any]] = None,
                            error: Optional[Dict[str, Any]] = None) -> None:
    """Sync the linked platform Job on the caller's OWN connection, inside its transaction.

    The PostgreSQL job authority (``async_jobs``) and this linkage (``jobs``) are two
    tables in one database. Writing them in one transaction is what makes a terminal
    result atomic: either both land or neither does. Contrast ``on_terminal``, which
    runs after an already-committed authority write and therefore MUST swallow failure
    to avoid double-reporting a run that really did finish.

    Deliberately raises. A failure here must roll the authority write back with it, so
    the callback is retried against a clean state rather than leaving a caller polling
    a nonterminal platform Job forever. A spine_ref with no matching row stays a
    harmless 0-row UPDATE.
    """
    status, result_json, cost_usd = _terminal_fields(spine_status, result_env)
    _update_by_spine(spine_job_id, status=status, result=result_json,
                     cost_usd=cost_usd, conn=conn)


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #
def _pop(spine_job_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        return _MAP.pop(str(spine_job_id), None)


def _terminal_fields(spine_status: str,
                     result_env: Optional[Dict[str, Any]]) -> Tuple[str, Optional[Dict[str, Any]], Optional[float]]:
    """Derive the platform-side (status, result, cost_usd) from a spine terminal callback.

    Shared so the best-effort and in-transaction paths cannot drift apart.
    """
    status = _STATUS_MAP.get(spine_status, "failed")
    result_json = None
    cost_usd = None
    if status == "succeeded" and isinstance(result_env, dict):
        result_json = result_env
        cost = result_env.get("cost")
        if isinstance(cost, dict):
            usd = cost.get("usd_est")
            if isinstance(usd, (int, float)):
                cost_usd = float(usd)
    return status, result_json, cost_usd


def _update_by_spine(spine_job_id: str, *, status: str,
                     result: Optional[Dict[str, Any]] = None,
                     cost_usd: Optional[float] = None,
                     conn=None) -> None:
    """Issue the DURABLE terminal/running UPDATE via the platform pool, matched by the
    globally-unique ``spine_ref``. Survives an app restart (no in-process state needed).
    ``result``/``cost_usd`` are only touched on a terminal sync (passed None for the
    'running' flip, and left as-is by assigning them only when provided).

    When ``conn`` is given the statement runs on that connection, joining the caller's
    open transaction instead of taking its own pooled one."""
    from psycopg.types.json import Jsonb  # noqa: PLC0415

    _store, db, _platform_deps = _load_platform()
    sets = ["status = %(status)s", "updated_at = NOW()"]
    args: Dict[str, Any] = {"status": status, "spine_ref": str(spine_job_id)}
    if result is not None:
        sets.append("result = %(result)s")
        args["result"] = Jsonb(result)
    if cost_usd is not None:
        sets.append("cost_usd = %(cost_usd)s")
        args["cost_usd"] = cost_usd
    # The async spine's first accepted terminal callback is immutable. Mirror that
    # rule in the best-effort canonical linkage so a late/replayed failed callback
    # cannot relabel an already-succeeded platform Job (or vice versa). Running
    # syncs are similarly harmless after terminal state.
    sql = ("UPDATE jobs SET " + ", ".join(sets)
           + " WHERE spine_ref = %(spine_ref)s"
           + " AND status NOT IN ('succeeded', 'failed', 'cancelled')")
    if conn is not None:
        conn.execute(sql, args)
        return
    with db.cursor() as cur:
        cur.execute(sql, args)
