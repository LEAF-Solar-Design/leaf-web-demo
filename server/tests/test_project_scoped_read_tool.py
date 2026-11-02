"""C1a synchronous canonical read admission and side-effect boundaries."""
import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest

import broker_client
import deps
import entitlements
import platform_link
import solar_project_context as service
import tool_loader
from envelopes import ErrorCode, err_envelope, with_envelope_fields
from routers import jobs as route
from test_sip_r1_context import memory, O1, O2, P1, P2, D1, D2, V1, V2
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

platform_link._ensure_platform_package()
from leaf_platform import canonical_jobs, project_lifecycle
from leaf_platform import entitlements as stored_entitlements

_REAL_ACCESS = platform_link.require_project_access
_REAL_AUTHORITY = platform_link.resolve_project_authority
_REAL_STORED_VERDICT = stored_entitlements.stored_job_entitlement_verdict

BLANKET = "project-scoped canonical execution is enabled only for a connected solver adapter"


def forbidden(*args, **kwargs):
    pytest.fail("canonical read reached a mutation or legacy execution boundary")


def store_intake(memory, version, intake):
    raw = json.dumps(intake, sort_keys=True).encode()
    record = memory.versions[version]
    memory.blobs[record.intake_ref] = raw
    record.provenance["source"]["intake"]["sha256"] = hashlib.sha256(raw).hexdigest()


class SqlMemory:
    """Execute production SELECT predicates; omit PostgreSQL locking syntax only."""

    def __init__(self):
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.create_function("clock_timestamp", 0, lambda: datetime.now(timezone.utc).isoformat())

    def table(self, name, columns):
        self.db.execute(f"CREATE TABLE {name} ({columns})")

    def insert(self, name, values):
        values = {
            k: str(v) if isinstance(v, UUID) else
            json.dumps(v) if isinstance(v, dict) else v
            for k, v in values.items()
        }
        self.db.execute(
            f"INSERT INTO {name} ({','.join(values)}) VALUES "
            f"({','.join('?' for _ in values)})",
            tuple(values.values()),
        )
        self.db.commit()

    @contextmanager
    def connection(self):
        yield self

    @contextmanager
    def cursor(self):
        cursor = self.db.cursor()

        class Cursor:
            def execute(self, sql, args=None):
                if sql.startswith("SET LOCAL statement_timeout"):
                    return
                sql = re.sub(r" FOR (?:SHARE|UPDATE)\b", "", sql)
                sql = re.sub(r"%\((\w+)\)s", r":\1", sql)
                cursor.execute(
                    sql,
                    {
                        k: str(v) if isinstance(v, UUID) else
                        json.dumps(v.obj) if hasattr(v, "obj") else v
                        for k, v in (args or {}).items()
                    },
                )

            def fetchone(self):
                row = cursor.fetchone()
                if row is None:
                    return None
                row = dict(row)
                for key in (
                    "org_id", "project_id", "drawing_id", "version_id",
                    "binding_id", "platform_tenant_id",
                ):
                    if row.get(key) is not None:
                        row[key] = UUID(row[key])
                for key in ("provenance", "params", "result", "error", "execution_context"):
                    if isinstance(row.get(key), str):
                        row[key] = json.loads(row[key])
                for key in ("created_at", "updated_at", "started_at", "finished_at"):
                    if isinstance(row.get(key), str):
                        row[key] = datetime.fromisoformat(row[key])
                return row

            def fetchall(self):
                rows = []
                while (row := self.fetchone()) is not None:
                    rows.append(row)
                return rows

        try:
            yield Cursor()
        finally:
            cursor.close()

    def transaction(self, operation, **kwargs):
        with self.db:
            return operation(self)


@pytest.fixture
def real_rules(lane, monkeypatch):
    databases = []

    def install():
        m = lane.memory
        db = SqlMemory()
        databases.append(db)
        db.table(
            "live_projects",
            "org_id, project_id, name, status, created_at, updated_at",
        )
        db.table("orgs", "org_id, status")
        db.table(
            "live_project_authority_modes",
            "org_id, project_id, authority_mode",
        )
        db.table("tenant_authority_modes", "org_id, authority_mode")
        db.table(
            "drawing_artifacts",
            "org_id, project_id, drawing_id, status",
        )
        db.table(
            "drawing_versions",
            "version_id, drawing_id, org_id, project_id, seq INTEGER, "
            "deleted_at, intake_ref, oss_object, provenance",
        )
        db.table(
            "identity_bindings",
            "platform_tenant_id, binding_id, role, status",
        )
        db.table(
            "project_member_bindings",
            "org_id, project_id, binding_id, role, status",
        )
        db.insert("orgs", {"org_id": O1, "status": "active"})
        for pid in (P1, P2):
            db.insert(
                "live_projects",
                {"org_id": O1, "project_id": pid, "status": "active"},
            )
            db.insert(
                "live_project_authority_modes",
                {
                    "org_id": O1, "project_id": pid,
                    "authority_mode": m.authority,
                },
            )
            db.insert(
                "drawing_artifacts",
                {
                    "org_id": O1, "project_id": pid,
                    "drawing_id": D1, "status": "active",
                },
            )
        for version in m.versions.values():
            db.insert("drawing_versions", vars(version))
        db.insert(
            "identity_bindings",
            {
                "platform_tenant_id": O1,
                "binding_id": m.binding.binding_id,
                "role": "member", "status": "active",
            },
        )
        if m.role is not None:
            db.insert(
                "project_member_bindings",
                {
                    "org_id": O1, "project_id": P1,
                    "binding_id": m.binding.binding_id,
                    "role": m.role, "status": "active",
                },
            )

        graph = m.original_store
        monkeypatch.setattr(service, "graph_store", lambda: graph)
        monkeypatch.setattr(graph.db, "run_transaction", db.transaction)
        monkeypatch.setattr(
            project_lifecycle, "run_transaction", db.transaction,
        )
        monkeypatch.setattr(
            platform_link, "require_project_access", _REAL_ACCESS,
        )
        monkeypatch.setattr(
            platform_link, "resolve_project_authority", _REAL_AUTHORITY,
        )
        authority_store = SimpleNamespace(
            get_project=lambda oid, pid: SimpleNamespace(status="active")
                if oid == O1 and pid in (P1, P2) else None,
            get_authority_mode=lambda oid, pid: m.authority,
        )
        monkeypatch.setattr(
            platform_link, "_load_platform",
            lambda: (authority_store, None, None),
        )
        return db

    yield install
    for db in databases:
        db.db.close()


@pytest.fixture
def persisted_jobs(lane, monkeypatch):
    legacy, canonical = SqlMemory(), SqlMemory()
    legacy.table(
        "jobs",
        "job_id, tenant_id, tool, params_json, dwg, status, progress, "
        "created_at, started_at, updated_at, finished_at, elapsed_ms, "
        "result_json, error_json",
    )
    legacy.insert(
        "jobs",
        {
            "job_id": "existing-legacy", "tenant_id": str(O1),
            "tool": "sentinel", "params_json": "{}", "dwg": str(V2),
            "status": "complete", "progress": "done",
            "created_at": "2030-01-01",
        },
    )
    canonical.table("jobs", canonical_jobs._job_columns() + ", deleted_at")
    canonical.insert(
        "jobs",
        {
            "job_id": "existing-canonical", "request_tenant_id": str(O1),
            "org_id": O1, "project_id": P1, "status": "succeeded",
            "created_at": "2030-01-01",
        },
    )
    monkeypatch.setattr(route.jobs, "job_store_mode", lambda: "sqlite")
    monkeypatch.setattr(route.jobs, "_db", lambda: legacy.db)
    monkeypatch.setattr(
        canonical_jobs, "connection", canonical.connection,
    )
    monkeypatch.setattr(
        platform_link, "_canonical_jobs_module", lambda: canonical_jobs,
    )
    try:
        yield legacy, canonical
    finally:
        legacy.db.close()
        canonical.db.close()


def install_tool_body(monkeypatch, run):
    monkeypatch.setattr(tool_loader, "_sandbox_tier", lambda: "off")
    monkeypatch.setattr(
        tool_loader, "resolve_local_file", lambda *_: Path("synthetic.py"),
    )
    monkeypatch.setattr(
        tool_loader, "_load_module", lambda *_: SimpleNamespace(run=run),
    )


@pytest.fixture
def lane(memory, monkeypatch):
    memory.role = "read_only"
    memory.tenant.tier = "demo"
    store_intake(memory, V2, {"polylines": [{"layer": "PV-ROW"}, {"layer": "A"}],
                              "inserts": [{"layer": "PV-ROW"}], "faces3d": []})
    store_intake(memory, V1, {"polylines": [{"layer": "HISTORICAL"}],
                              "inserts": [], "faces3d": [{"layer": "OLD-TERRAIN"}]})
    monkeypatch.setattr(deps, "auth_live", lambda: True)
    monkeypatch.setattr(platform_link.platform_store(), "resolve_active_identity_binding",
                        lambda *_: memory.binding, raising=False)
    monkeypatch.setattr(deps, "backedge_run_identity", lambda tenant, *_: tenant)
    monkeypatch.setattr(route, "_checkout_identity", forbidden)
    monkeypatch.setattr(route.jobs, "submit_job", forbidden)
    monkeypatch.setattr(platform_link, "submit_canonical_solve", forbidden)
    monkeypatch.setattr(service.write_loop, "read_intake", forbidden)
    monkeypatch.setattr(broker_client, "run_via_broker", forbidden)
    monkeypatch.setattr(service.write_loop, "put_drawing", forbidden, raising=False)
    platform_link._ensure_platform_package()
    import leaf_platform.entitlements as stored
    monkeypatch.setattr(stored, "stored_job_entitlement_verdict", lambda *_: (None, None))
    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[deps.require_tenant] = lambda: memory.tenant
    with TestClient(app) as client:
        yield SimpleNamespace(memory=memory, client=client, stored=stored)
    assert memory.mutations == [] and memory.last_fence == 0


def request(lane, *, tool=None, params=None, headers=None, wait=0, **changes):
    name = (tool or {}).get("name", "instant-list-layers")
    row = tool if tool is not None else deps.find_tool(name, str(lane.memory.tenant))
    body = {"tool": name, "params": params or {}, "dwg": str(V2),
            "catalog_digest": deps.catalog_tool_digest(row)}
    body.update(changes)
    scope = {"X-Org-Id": str(O1), "X-Project-Id": str(P1)} if headers is None else headers
    return lane.client.post(f"/api/run?wait={wait}", json=body, headers=scope)


def tool_override(monkeypatch, **fields):
    tool = {"name": "published-read", "kind": "script", "entry": "list_layers.py",
            "capabilities": ["drawing.read"], "default_params": {}}
    tool.update(fields)
    monkeypatch.setattr(deps, "find_tool", lambda *_: tool)
    return tool


def layers(lane, version):
    intake = json.loads(lane.memory.blobs[lane.memory.versions[version].intake_ref])
    return sorted({entity["layer"] for kind in ("polylines", "inserts", "faces3d")
                   for entity in intake.get(kind, [])})


@pytest.fixture
def read_job_store(lane, real_rules, monkeypatch):
    """Run the production job INSERT/UPDATE and real board reader on one isolated SQL store."""
    db = real_rules()
    db.db.execute("ALTER TABLE orgs ADD COLUMN tier")
    db.db.execute("ALTER TABLE orgs ADD COLUMN account_class")
    db.db.execute("UPDATE orgs SET tier='hosted_pro', account_class='standard'")
    db.table("jobs", canonical_jobs._job_columns() +
             ", deleted_at, idempotency_key, submission_fingerprint, spine_ref, cost_usd, "
             "purge_requested_at, purge_completed_at")
    db.db.execute("CREATE UNIQUE INDEX read_job_key ON jobs(org_id,project_id,idempotency_key) "
                  "WHERE idempotency_key IS NOT NULL")
    db.db.commit()
    monkeypatch.setattr(platform_link, "platform_db", lambda: SimpleNamespace(run_transaction=db.transaction))
    monkeypatch.setattr(platform_link, "_canonical_jobs_module", lambda: canonical_jobs)
    monkeypatch.setattr(lane.stored, "stored_job_entitlement_verdict",
                        lambda *_: (None, SimpleNamespace(tier="hosted_pro", account_class="standard")))
    from leaf_platform import store
    monkeypatch.setattr(store, "cursor", db.cursor)
    return db


def _real_read_job(lane, *, version=V2, key="read-once", params=None, calls=None,
                   callback=None, context=None, tool=None):
    context = context or service.resolve_context(lane.memory.tenant, P1, version)
    tool = tool or deps.find_tool("instant-list-layers", str(lane.memory.tenant))
    params = params or {}

    def execute():
        if calls is not None:
            calls.append(context.binding.input_version_id)
        return tool_loader.run_tool_dynamic(
            tool, context.intake, params, aps_live=False, da=None, tenant_id=str(O1))

    return platform_link.run_project_read_job(
        lane.memory.tenant, context, tool, params, key, callback or execute)


def _selected_turn(lane, monkeypatch, version=V1, *, selected=True):
    import time
    import session_store
    import project_execution_context

    session = {"session_id": "selected-session", "tenant_id": str(O1),
               "org_id": str(O1), "project_id": str(P1), "drawing_id": "rooftop_demo",
               "active_turn_id": "selected-turn", "turn_started_at": time.time()}
    frozen = project_execution_context.freeze(
        lane.memory.tenant, session,
        {"project_id": str(P1), "input_version_id": str(version)})
    monkeypatch.setattr(session_store, "get_session", lambda sid: session if sid == "selected-session" else None)
    monkeypatch.setattr(session_store, "turn_started_data", lambda sid, tid, tenant: (
        {project_execution_context.KEY: frozen} if selected else {})
        if (sid, tid, tenant) == ("selected-session", "selected-turn", str(O1)) else None)
    return {"X-Authority-Session-Id": "selected-session",
            "X-Authority-Turn-Id": "selected-turn", "Idempotency-Key": "selected-once"}


@pytest.mark.parametrize("version", [V1, V2])
def test_c02_api_persists_exact_stored_selection_and_matching_retry(lane, read_job_store, monkeypatch, version):
    from leaf_platform import store
    headers = _selected_turn(lane, monkeypatch, version)
    calls = []
    actual_run = tool_loader.run_tool_dynamic

    def execute(*args, **kwargs):
        calls.append(True)
        return actual_run(*args, **kwargs)

    monkeypatch.setattr(tool_loader, "run_tool_dynamic", execute)
    first = request(lane, headers=headers, dwg=str(D1), dwg_version=999)
    second = request(lane, headers=headers, dwg=str(D1), dwg_version=999)
    assert first.status_code == second.status_code == 202, first.text
    job = first.json()
    assert job["job_id"] == second.json()["job_id"] and calls == [True]
    assert job["result"]["result"]["layers"] == layers(lane, version)
    board = [row.to_dict() for row in store.list_jobs(O1, P1)]
    assert len(board) == 1 and board[0]["job_id"] == job["job_id"]
    assert board[0]["input_version_id"] == str(version)
    assert board[0]["result"]["project_context"]["drawing_id"] == str(D1)


@pytest.mark.parametrize("conflict", ["drawing", "project", "organization", "missing", "store"])
def test_c02_api_cannot_fall_back_from_selected_project(lane, read_job_store, monkeypatch, conflict):
    import session_store
    headers = _selected_turn(lane, monkeypatch, selected=conflict != "missing")
    changes = {"dwg": str(D2 if conflict == "drawing" else D1)}
    if conflict == "project":
        headers["X-Project-Id"] = str(P2)
    elif conflict == "organization":
        headers["X-Org-Id"] = str(O2)
    elif conflict == "store":
        monkeypatch.setattr(session_store, "get_session", lambda _sid: None)
    response = request(lane, headers=headers, **changes)
    assert response.status_code == (403 if conflict == "store" else 409), response.text
    assert read_job_store.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_c02_selected_turn_preserves_entity_scope_boundary(lane, read_job_store, monkeypatch):
    headers = _selected_turn(lane, monkeypatch)
    monkeypatch.setattr(route.entity_scope, "resolve_turn_binding", lambda *_: {"entity_scope": "stored"})
    response = request(lane, headers=headers, dwg=str(D1))
    assert response.status_code == 403
    assert response.json()["reason_code"] == route.entity_scope.SCOPED_MUTATION_REASON
    assert read_job_store.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


@pytest.mark.parametrize("version", [V1, V2], ids=["historical", "selected-head"])
def test_c02_real_persisted_read_and_project_board_tuple(lane, read_job_store, version):
    from leaf_platform import store
    calls = []
    job = _real_read_job(lane, version=version, calls=calls)
    UUID(job["job_id"])
    assert job["status"] == "complete" and calls == [version]
    assert job["tenant_id"] == str(O1) and job["project_id"] == str(P1)
    assert job["input_version_id"] == str(version)
    assert job["result"]["result"] == {"count": len(layers(lane, version)),
                                        "layers": layers(lane, version)}
    expected = {"organization_id": str(O1), "project_id": str(P1), "drawing_id": str(D1),
                "input_version_id": str(version), "intake_sha256": hashlib.sha256(
                    lane.memory.blobs[lane.memory.versions[version].intake_ref]).hexdigest()}
    assert job["result"]["project_context"] == expected
    # The exact board SQL and Job.from_row/to_dict run, not a fake board response.
    board = [row.to_dict() for row in store.list_jobs(O1, P1)]
    assert len(board) == 1 and board[0]["job_id"] == job["job_id"]
    assert board[0]["input_version_id"] == str(version)
    assert board[0]["result"]["project_context"] == expected
    assert store.list_jobs(O2, P1) == [] and store.list_jobs(O1, P2) == []
    row = read_job_store.db.execute("SELECT status,kind,attempt,max_attempts,execution_context "
                                   "FROM jobs").fetchone()
    assert tuple(row[:4]) == ("succeeded", "run", 1, 1)
    assert json.loads(row["execution_context"])["catalog_digest"] == deps.catalog_tool_digest(
        deps.find_tool("instant-list-layers", str(lane.memory.tenant)))
    assert read_job_store.db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0] == 0
    assert all(not write for write in lane.memory.accesses)


def test_c02_matching_retry_reads_same_real_job_without_execution(lane, read_job_store):
    calls = []
    first = _real_read_job(lane, calls=calls)
    second = _real_read_job(lane, calls=calls)
    assert second["job_id"] == first["job_id"] and second["result"] == first["result"]
    assert calls == [V2]
    assert read_job_store.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


@pytest.mark.parametrize("change", ["version", "params", "catalog"])
def test_c02_changed_input_cannot_retarget_accepted_key(lane, read_job_store, change):
    calls = []
    _real_read_job(lane, calls=calls)
    changes = {"version": V1} if change == "version" else {"params": {"prefix": "OLD"}}
    if change == "catalog":
        tool = dict(deps.find_tool("instant-list-layers", str(lane.memory.tenant)))
        tool["version"] = "99.0.0"
        changes = {"tool": tool}
    with pytest.raises(ValueError, match="idempotency"):
        _real_read_job(lane, calls=calls, **changes)
    assert calls == [V2]
    assert read_job_store.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


def test_c02_callback_failure_is_a_real_replayable_failed_job(lane, read_job_store):
    calls = []

    def failed():
        calls.append(True)
        raise RuntimeError("private callback detail must not escape")

    job = _real_read_job(lane, callback=failed)
    replay = _real_read_job(lane, callback=failed)
    assert calls == [True] and replay["job_id"] == job["job_id"]
    assert job["status"] == "failed" and job["result"]["ok"] is False
    assert job["error"]["error_code"] == ErrorCode.INTERNAL
    assert job["result"]["project_context"]["input_version_id"] == str(V2)
    assert "private callback" not in json.dumps(job)
    assert read_job_store.db.execute("SELECT status FROM jobs").fetchone()[0] == "failed"


def test_c02_actual_failed_tool_envelope_keeps_its_error(lane, read_job_store):
    error = err_envelope(ErrorCode.BAD_PARAMS, "actual tool refusal", retryable=False)
    job = _real_read_job(lane, callback=lambda: error)
    assert job["status"] == "failed" and job["error"] == error["error"]


def test_c02_reserved_job_does_not_survive_commit_failure(lane, read_job_store, monkeypatch):
    calls = []

    def fail_commit(operation, **kwargs):
        assert kwargs == {"max_attempts": 1}
        with read_job_store.db:
            operation(read_job_store)
            raise OSError("synthetic commit failure")

    monkeypatch.setattr(read_job_store, "transaction", fail_commit)
    with pytest.raises(OSError, match="commit failure"):
        _real_read_job(lane, calls=calls)
    assert calls == [V2]
    assert read_job_store.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["drawing", "organization", "intake", "membership", "tier"])
def test_c02_refuses_stale_or_foreign_binding_without_job(lane, read_job_store, change):
    from dataclasses import replace
    context = service.resolve_context(lane.memory.tenant, P1, V2)
    if change in {"drawing", "organization"}:
        field = "drawing_id" if change == "drawing" else "organization_id"
        value = D2 if change == "drawing" else O2
        context = replace(context, binding=replace(context.binding, **{field: value}))
    elif change == "intake":
        lane.memory.blobs[context.intake_ref] = b'{"changed":true}'
    elif change == "membership":
        read_job_store.db.execute("UPDATE project_member_bindings SET status='revoked'")
        read_job_store.db.commit()
    else:
        read_job_store.db.execute("UPDATE orgs SET tier='hosted_free'")
        read_job_store.db.commit()
    with pytest.raises((service.ProjectContextError, platform_link.CanonicalEntitlementDenied)):
        _real_read_job(lane, context=context, callback=forbidden)
    assert read_job_store.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_c02_stored_extract_entitlement_cannot_be_bypassed(lane, read_job_store, monkeypatch):
    denial = JSONResponse({"error": "denied"}, status_code=403)
    monkeypatch.setattr(lane.stored, "stored_job_entitlement_verdict", lambda *_: (denial, None))
    with pytest.raises(platform_link.CanonicalEntitlementDenied) as exc:
        _real_read_job(lane, callback=forbidden)
    assert exc.value.response is denial
    assert read_job_store.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def success(lane, response, version=V2):
    assert response.status_code == 200, response.text
    body = response.json()
    expected = layers(lane, version)
    assert body["ok"] is True
    assert body["result"] == {"count": len(expected), "layers": expected}
    assert body["project_context"] == {
        "organization_id": str(O1), "project_id": str(P1), "drawing_id": str(D1),
        "input_version_id": str(version), "intake_sha256": hashlib.sha256(
            lane.memory.blobs[lane.memory.versions[version].intake_ref]).hexdigest()}
    assert response.headers["cache-control"] == "no-store"
    assert not {"job_id", "output_version_id", "source_version", "status"} & body.keys()


@pytest.mark.parametrize("wait", [0, 1, 0], ids=["C1A-01", "C1A-02", "C1A-27"])
def test_c1a_real_read_and_unchanged_job_lists(
    lane, monkeypatch, wait, persisted_jobs,
):
    snapshot = tuple(tuple(db.db.iterdump()) for db in persisted_jobs)
    before = (
        route.jobs.list_jobs(str(O1), 20),
        platform_link.list_canonical_jobs(str(O1), limit=20),
    )
    assert [row["job_id"] for row in before[0]] == ["existing-legacy"]
    assert [row["job_id"] for row in before[1]] == ["existing-canonical"]
    versions = deepcopy(lane.memory.versions)

    success(lane, request(lane, wait=wait))

    assert tuple(tuple(db.db.iterdump()) for db in persisted_jobs) == snapshot
    assert (
        route.jobs.list_jobs(str(O1), 20),
        platform_link.list_canonical_jobs(str(O1), limit=20),
    ) == before
    assert lane.memory.versions == versions


@pytest.mark.parametrize("version", [UUID(int=99), UUID(int=100)], ids=["C1A-03", "C1A-04"])
def test_c1a_unavailable_version(lane, version, real_rules):
    if version.int == 99:
        foreign = deepcopy(lane.memory.versions[V1])
        foreign.project_id = P2
        foreign.version_id = version
        lane.memory.versions[version] = foreign
    real_rules()

    response = request(lane, dwg=str(version))

    assert response.status_code == 404
    assert response.json()["error"]["reason_code"] == "SIP_R1_CONTEXT_NOT_FOUND"
    assert lane.memory.reads == []


def test_c1a_revoked_member(lane, real_rules):
    """C1A-05"""
    lane.memory.role = None
    real_rules()

    response = request(lane)

    assert response.status_code == 403
    assert response.json()["error"]["error_code"] == "FORBIDDEN"
    assert response.json()["error"]["message"] == "project access denied"
    assert lane.memory.reads == []


@pytest.mark.parametrize("declaration", [["drawing.write"], "missing", ["drawing.read", "drawing.write"],
                                          [], None, "drawing.read", ["future.write"], ["solve"]],
                         ids=["C1A-06", "C1A-07", "C1A-08", "C1A-09-empty", "C1A-09-null",
                              "C1A-09-malformed", "C1A-09-unknown", "C1A-09-solve"])
def test_c1a_ineligible_declarations(lane, monkeypatch, declaration):
    """C1A-09: malformed and unknown capabilities never grant read admission."""
    tool = tool_override(monkeypatch, capabilities=declaration)
    if declaration == "missing":
        del tool["capabilities"]
    assert route._is_project_read_tool(tool) is False
    monkeypatch.setattr(route, "_checkout_identity", lambda *_: ("anonymous", None))
    monkeypatch.setattr(platform_link, "resolve_submission_context", lambda *_: {
        "org_id": O1, "project_id": P1, "authority_mode": "postgres_canonical"})
    response = request(lane, tool=tool)
    assert response.status_code == 409
    assert response.json()["error"]["message"] == BLANKET
    assert lane.memory.reads == []


@pytest.mark.parametrize("changes,message,status", [
    ({"dwg_version": 1}, "dwg_version applies to the legacy path; canonical runs pin by version UUID in dwg", 409),
    ({"expected_drawing_head": 1}, "expected_drawing_head applies only to legacy versioned drawings", 409),
    ({"dwg": "invalid"}, "a canonical drawing version UUID is required", 400)],
    ids=["C1A-10", "C1A-11", "C1A-12"])
def test_c1a_legacy_pin_ambiguity(lane, changes, message, status):
    response = request(lane, **changes)
    assert response.status_code == status
    assert response.json()["error"]["message"] == message
    assert lane.memory.reads == []


def test_c1a_artifact_conflict(lane, real_rules):
    """C1A-13"""
    real_rules()

    response = request(lane, params={"drawing_id": str(D2)})

    assert response.status_code == 404
    assert response.json()["error"]["reason_code"] == "SIP_R1_CONTEXT_NOT_FOUND"


def test_c1a_historical_real_execution(lane):
    """C1A-14"""
    response = request(lane, dwg=str(V1))
    success(lane, response, V1)
    assert set(response.json()["result"]["layers"]).isdisjoint(layers(lane, V2))
    assert lane.memory.reads == [lane.memory.versions[V1].intake_ref]


@pytest.mark.parametrize("checkout", [None, "stale", "foreign"], ids=["C1A-15-absent", "C1A-15-stale", "C1A-15-foreign"])
def test_c1a_checkout_not_consulted(lane, checkout):
    """C1A-15"""
    headers = {"X-Org-Id": str(O1), "X-Project-Id": str(P1)}
    if checkout is not None:
        headers["X-Checkout-Capability"] = checkout
    success(lane, request(lane, headers=headers))


@pytest.mark.parametrize("mismatch", ["header", "catalog"], ids=["C1A-16-header", "C1A-16-catalog"])
def test_c1a_tenant_binding(lane, monkeypatch, mismatch):
    """C1A-16"""
    headers = {"X-Org-Id": str(O2 if mismatch == "header" else O1), "X-Project-Id": str(P1)}
    if mismatch == "catalog":
        monkeypatch.setattr(route, "_canonical_tenant_id", lambda *_: str(O2))
    response = request(lane, headers=headers)
    assert response.status_code == 409
    assert lane.memory.reads == []


@pytest.mark.parametrize("digest", [None, "stale"], ids=["C1A-17-missing", "C1A-17-stale"])
def test_c1a_catalog_confirmation(lane, digest):
    """C1A-17"""
    response = request(lane, catalog_digest=digest)
    assert response.status_code == 409
    assert "refresh tools and confirm again" in response.json()["error"]["message"]
    assert lane.memory.reads == []


@pytest.mark.parametrize("gate", ["request", "stored", "policy"], ids=["C1A-18-request", "C1A-18-stored", "C1A-18-policy"])
def test_c1a_entitlement_boundaries(lane, monkeypatch, gate):
    """C1A-18"""
    if gate == "request":
        monkeypatch.setattr(entitlements, "entitlements_for", lambda *_: {})
        monkeypatch.setattr(
            entitlements, "w1_tool_availability", lambda *_, **__: None,
        )
    else:
        org = SimpleNamespace(status="active", tier="guest")
        monkeypatch.setattr(
            lane.stored.store, "get_org",
            lambda oid: org if oid == O1 else None,
        )
        stored_policy = lane.stored._server_entitlements()
        if gate == "policy":
            def unreadable():
                raise stored_policy.EntitlementsError(
                    "synthetic unreadable policy",
                )
            monkeypatch.setattr(stored_policy, "load_policy", unreadable)

        calls, returned = [], []

        def verdict(org_id, kind):
            calls.append((org_id, kind))
            answer = _REAL_STORED_VERDICT(org_id, kind)
            returned.append(answer)
            return answer

        monkeypatch.setattr(
            lane.stored, "stored_job_entitlement_verdict", verdict,
        )
        denial = (
            lane.stored.policy_unavailable_response(org, "extract")
            if gate == "policy"
            else entitlements.entitlement_denied_response("run_read", "guest")
        )

    response = request(lane)

    assert response.status_code == (503 if gate == "policy" else 403)
    assert response.json()["entitlement_required"] is True
    assert response.json()["required"] == "run_read"
    if gate != "request":
        assert calls == [(O1, "extract")]
        assert returned[0][1] is org
        assert response.content == returned[0][0].body == denial.body
        assert response.json()["tier"] == "guest"
        assert lane.memory.tenant.tier == "demo"


def test_c1a_missing_proof(lane):
    """C1A-19"""
    lane.memory.versions[V2].provenance = {}
    response = request(lane)
    assert response.status_code == 409
    assert response.json()["error"]["reason_code"] == "SIP_R1_INTAKE_PROOF_REQUIRED"


@pytest.mark.parametrize("damage,status,reason", [
    ("hash", 500, "INTAKE_DIGEST_MISMATCH"), ("json", 500, "INTAKE_INVALID"),
    ("reference", 500, "INTAKE_REFERENCE_INVALID"), ("bytes", 503, "INTAKE_UNAVAILABLE"),
    ("store", 503, "STORE_UNAVAILABLE")],
    ids=["C1A-20-hash", "C1A-20-json", "C1A-20-reference", "C1A-20-bytes", "C1A-20-store"])
def test_c1a_intake_corruption(lane, monkeypatch, damage, status, reason):
    """C1A-20"""
    version = lane.memory.versions[V2]
    if damage == "hash":
        lane.memory.blobs[version.intake_ref] += b" "
    elif damage == "json":
        lane.memory.set_bytes(b'{"a":1,"a":2}')
    elif damage == "reference":
        version.oss_object = "foreign.dwg"
    elif damage == "bytes":
        lane.memory.blobs.clear()
    else:
        def unavailable(*_, **__):
            raise RuntimeError("private backend detail")
        monkeypatch.setattr(lane.memory, "resolve_version_binding", unavailable)
    response = request(lane)
    assert response.status_code == status
    assert response.json()["error"]["reason_code"] == "SIP_R1_" + reason
    assert "private backend detail" not in response.text


def test_c1a_legacy_authority_locked(lane, real_rules):
    """C1A-21"""
    lane.memory.authority = "legacy_sqlite"
    real_rules()
    response = request(lane)
    assert response.status_code == 409 and "locked" in response.json()["error"]["message"]
    assert lane.memory.reads == []


@pytest.mark.parametrize("wait", [0, 1], ids=["C1A-22-async", "C1A-22-wait"])
def test_c1a_standalone_contract(lane, monkeypatch, wait):
    """C1A-22"""
    monkeypatch.setattr(
        route, "_checkout_identity", lambda *_: ("anonymous", None),
    )
    monkeypatch.setattr(
        platform_link, "resolve_submission_context", lambda *_: None,
    )
    monkeypatch.setattr(deps, "APS_LIVE", False)
    monkeypatch.setattr(route.jobs, "job_max_s", lambda: 5)
    submitted, waited = [], []
    monkeypatch.setattr(
        route.jobs, "submit_job",
        lambda *a, **kw: submitted.append((a, kw)) or "fixed-job",
    )
    env = with_envelope_fields(
        {"ok": True, "result": {"layers": ["standalone"]}},
    )

    def terminal(*args, **kwargs):
        waited.append((args, kwargs))
        return {
            "job_id": "fixed-job", "status": "complete", "result": env,
        }

    monkeypatch.setattr(route.jobs, "wait_for_terminal", terminal)
    tool = deps.find_tool("instant-list-layers", str(lane.memory.tenant))
    params = dict(tool.get("default_params", {}))
    params["prefix"] = "A"

    response = request(
        lane, headers={"Idempotency-Key": "standalone-read"},
        params={"prefix": "A"}, wait=wait,
    )

    expected = env if wait else deps.tenant_echo(
        with_envelope_fields(
            {"job_id": "fixed-job", "status": "submitted"},
        ),
        lane.memory.tenant,
    )
    assert response.status_code == (200 if wait else 202)
    assert response.content == JSONResponse(content=expected).body
    assert len(submitted) == 1 and submitted[0][0][3] == str(V2)
    assert submitted == [
        (
            (lane.memory.tenant, tool, params, str(V2)),
            {
                "aps_live": False, "org_id": None, "project_id": None,
                "dwg_version": None, "idempotency_key": "standalone-read",
                "authority_mode": "legacy_sqlite", "platform_context": None,
                "checkout_holder": "anonymous", "checkout_fence": None,
                "entity_scope": None,
            },
        ),
    ]
    assert waited == (
        [(("fixed-job",), {"timeout_s": 35})] if wait else []
    )
    assert lane.memory.reads == []


@pytest.mark.parametrize(
    "project,wait", [(True, 0), (True, 1), (False, 0)],
    ids=["C1A-23-async", "C1A-23-wait", "C1A-24"],
)
def test_c1a_canonical_solver_unchanged(
    lane, monkeypatch, project, wait,
):
    tool = tool_override(
        monkeypatch, name="string-autofill-opt", canonical_only=True,
    )
    monkeypatch.setattr(
        route, "_checkout_identity", lambda *_: ("anonymous", None),
    )
    context = {
        "org_id": O1, "project_id": P1,
        "authority_mode": "postgres_canonical",
    }
    monkeypatch.setattr(
        platform_link, "resolve_submission_context",
        lambda *_: context if project else None,
    )
    monkeypatch.setattr(route.jobs, "job_max_s", lambda: 5)
    calls, polls, sleeps = [], [], []
    monkeypatch.setattr(
        platform_link, "submit_canonical_solve",
        lambda *a: calls.append(a) or "canonical-job",
    )
    env = with_envelope_fields(
        {"ok": True, "result": {"solver": "complete"}},
    )
    records = [
        {"job_id": "canonical-job", "status": "submitted"},
        {
            "job_id": "canonical-job", "status": "complete",
            "result": env,
        },
    ]

    def poll(*args):
        polls.append(args)
        return records.pop(0)

    monkeypatch.setattr(route, "_job_for_tenant", poll)
    monkeypatch.setattr(
        route.time, "sleep", lambda seconds: sleeps.append(seconds),
    )
    headers = {"Idempotency-Key": "canonical-read"}
    if project:
        headers.update(
            {"X-Org-Id": str(O1), "X-Project-Id": str(P1)},
        )

    response = request(lane, tool=tool, headers=headers, wait=wait)

    if project:
        expected = env if wait else deps.tenant_echo(
            with_envelope_fields(
                {"job_id": "canonical-job", "status": "submitted"},
            ),
            lane.memory.tenant,
        )
        assert response.status_code == (200 if wait else 202)
        assert response.content == JSONResponse(content=expected).body
        if not wait:
            assert response.json()["job_id"] == "canonical-job"
        assert len(calls) == 1 and calls[0][-1] == str(V2)
        assert calls == [
            (
                context, str(O1), "string-autofill-opt", {},
                "canonical-read", str(V2),
            ),
        ]
        assert polls == (
            [("canonical-job", str(O1))] * 2 if wait else []
        )
        assert sleeps == ([0.1] if wait else [])
    else:
        assert response.status_code == 400
        assert response.json()["error"]["message"] == (
            "X-Org-Id and X-Project-Id are required for this canonical solver"
        )
        assert calls == []
        assert polls == sleeps == []
    assert lane.memory.reads == []


def test_c1a_entity_scope_denies_project_read(lane, monkeypatch):
    """C1A-25"""
    monkeypatch.setattr(route.entity_scope, "resolve_turn_binding", lambda *_: {"entity_scope": "stored"})
    response = request(lane, headers={"X-Org-Id": str(O1), "X-Project-Id": str(P1),
                                     "X-Authority-Session-Id": "session", "X-Authority-Turn-Id": "turn"})
    assert response.status_code == 403
    assert response.json()["reason_code"] == route.entity_scope.SCOPED_MUTATION_REASON
    assert lane.memory.reads == []


@pytest.mark.parametrize("code", [ErrorCode.BAD_PARAMS, ErrorCode.INTERNAL], ids=["C1A-26-params", "C1A-26-execution"])
def test_c1a_loader_failure_and_owned_metadata(lane, monkeypatch, code):
    """C1A-26"""
    env = err_envelope(code, "loader refusal", False)
    env["project_context"] = {"organization_id": "forged"}
    def loader(tool, intake, params, **kwargs):
        assert kwargs == {"aps_live": False, "da": None, "tenant_id": str(O1)}
        return env
    monkeypatch.setattr(tool_loader, "run_tool_dynamic", loader)
    response = request(lane)
    assert response.status_code == route.DEFAULT_HTTP_STATUS[code]
    assert response.json()["error"] == env["error"]
    assert response.json()["project_context"]["organization_id"] == str(O1)
    assert response.headers["cache-control"] == "no-store"


def test_c1a_real_loader_parameter_validation(lane):
    """C1A-26: the real catalog schema and loader reject malformed parameters."""
    response = request(lane, params={"prefix": 17})
    assert response.status_code == 400
    assert response.json()["ok"] is False
    assert response.json()["error"]["error_code"] == "BAD_PARAMS"
    assert "params schema" in response.json()["error"]["message"]
    assert response.json()["project_context"]["input_version_id"] == str(V2)


@pytest.mark.parametrize("special", [{"solar": {}}, {"graph_input": "solar-w1-graph"}], ids=["C1A-28-packaged", "C1A-28-graph"])
def test_c1a_specialized_contract_not_generic(lane, monkeypatch, special):
    """C1A-28"""
    tool = tool_override(monkeypatch, **special)
    assert route._is_project_read_tool(tool) is False
    monkeypatch.setattr(route, "_checkout_identity", lambda *_: ("anonymous", None))
    monkeypatch.setattr(entitlements, "w1_tool_availability", lambda *_, **__: None)
    monkeypatch.setattr(platform_link, "resolve_submission_context", lambda *_: {
        "org_id": O1, "project_id": P1, "authority_mode": "postgres_canonical"})
    response = request(lane, tool=tool, params={"drawing_id": "conflicting"})
    assert response.status_code == 409
    assert response.json()["error"]["message"] in (BLANKET, "DRAWING_ID_CONFLICT")
    assert lane.memory.reads == []


def test_c1a_full_envelope_allowlist(lane, monkeypatch):
    """C1A-26: a tool cannot manufacture job or publication claims."""
    fabricated = {
        "ok": True,
        "result": {},
        "job_id": "fictional-job",
        "status": "submitted",
        "output_version_id": "00000000-0000-0000-0000-000000000309",
        "source_version": 123,
        "project_context": {"organization_id": "forged"},
        "overlay": {"highlight_handles": ["A1"]},
        "tool": "published-read",
        "version": "1.0.0",
        "timing_ms": 7,
        "cost": None,
        "error": None,
        "unexpected": {"claim": True},
        "execution_provenance": {"forged": True},
    }
    tool = tool_override(monkeypatch)
    install_tool_body(
        monkeypatch, lambda intake, params: deepcopy(fabricated),
    )

    response = request(lane, tool=tool)

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "ok": True,
        "result": {},
        "overlay": {"highlight_handles": ["A1"]},
        "tool": "published-read",
        "version": "1.0.0",
        "timing_ms": 7,
        "cost": None,
        "error": None,
        "degraded_mode": False,
        "project_context": {
            "organization_id": str(O1),
            "project_id": str(P1),
            "drawing_id": str(D1),
            "input_version_id": str(V2),
            "intake_sha256": hashlib.sha256(
                lane.memory.blobs[
                    lane.memory.versions[V2].intake_ref
                ],
            ).hexdigest(),
        },
    }
    assert not {
        "job_id", "status", "output_version_id", "source_version",
        "unexpected", "execution_provenance",
    } & body.keys()
    assert response.headers["cache-control"] == "no-store"


def test_c1a_real_tool_exception(lane, monkeypatch):
    """C1A-26: exercise the loader's tool-body exception conversion."""
    tool = tool_override(monkeypatch)
    calls = []

    def explode(intake, params):
        calls.append((deepcopy(intake), deepcopy(params)))
        raise RuntimeError("synthetic tool failure")

    install_tool_body(monkeypatch, explode)

    response = request(lane, tool=tool)

    assert len(calls) == 1
    assert calls[0] == (
        json.loads(
            lane.memory.blobs[lane.memory.versions[V2].intake_ref],
        ),
        {},
    )
    assert response.status_code == 500
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["error_code"] == "INTERNAL"
    assert body["error"]["message"] == (
        "tool 'published-read' raised RuntimeError: synthetic tool failure"
    )
    assert body["error"]["retryable"] is False
    assert body["project_context"]["organization_id"] == str(O1)
    assert body["project_context"]["input_version_id"] == str(V2)
    assert response.headers["cache-control"] == "no-store"


@pytest.fixture
def job_list_binding_lane(monkeypatch):
    """Real deps/list/membership helpers with counted synthetic storage edges."""
    import auth
    import tenancy
    from starlette.requests import Request
    from test_campaign_capability_job import context as capability_context

    calls = {"binding": [], "membership": [], "mirror": [], "lists": []}
    state = SimpleNamespace(
        binding=SimpleNamespace(platform_tenant_id=O1, binding_id=UUID(int=3)),
        error=None, denied=set(), records=[], legacy=[], linked={})
    subject = "auth0|job-list-synthetic-reader"
    def resolve(provider, value):
        assert (provider, value) == ("auth0", subject)
        calls["binding"].append(value)
        if state.error:
            raise state.error
        return state.binding
    store = SimpleNamespace(resolve_active_identity_binding=resolve,
                            get_org=lambda org: SimpleNamespace(status="active", tier="hosted_pro"))
    monkeypatch.setattr(platform_link, "platform_store", lambda: store)
    monkeypatch.setattr(platform_link, "_load_platform", lambda: (store, None, None))
    monkeypatch.setattr(deps, "auth_live", lambda: True)
    monkeypatch.setattr(auth, "verify_platform_token", lambda authorization: {"sub": subject, "roles": []})
    monkeypatch.setattr(auth, "extract_tenant_claims", lambda payload:
                        {"tenant_id": str(state.binding.platform_tenant_id), "tier": "hosted_pro"})
    monkeypatch.setattr(deps, "admin_elevated_tier", lambda claimed, actor, tier: tier)
    monkeypatch.setattr(tenancy, "get_store", lambda: SimpleNamespace(resolve_workspace=lambda org: None))
    def caller():
        request = Request({"type": "http", "method": "GET", "path": "/api/jobs",
                           "headers": [(b"authorization", b"Bearer synthetic-fixture-only")]})
        tenant = deps.require_tenant(request, x_tenant_id=None,
            authorization="Bearer synthetic-fixture-only", x_dispatch_secret=None,
            x_guest_session=None, x_demo_session=None)
        assert tenant.authority_resolved and len(calls["binding"]) == 1
        calls["binding"].clear()
        return tenant
    def role(org, project, binding_id, *, write):
        assert org == state.binding.platform_tenant_id and binding_id == state.binding.binding_id
        calls["membership"].append((str(project), write))
        if str(project) in state.denied:
            raise project_lifecycle.LifecycleForbidden("synthetic revoked membership")
    monkeypatch.setattr(project_lifecycle, "require_project_role", role)
    monkeypatch.setattr(platform_link, "require_project_access", _REAL_ACCESS)
    def seed(count, kind):
        state.records, state.linked = [], {}
        for index in range(count):
            org = str(state.binding.platform_tenant_id)
            rec = {"job_id": str(UUID(int=10000 + index)), "tenant_id": org,
                   "org_id": org, "project_id": str(UUID(int=1000 + index)),
                   "tool": "count-by-layer", "status": "running", "progress": "running",
                   "result": None, "error": None, "elapsed_ms": 0}
            if kind == "graph":
                rec["execution_context"] = {"schema": "leaf.solar-project-graph-job.v1"}
            else:
                context = capability_context()
                context.update(tenant_id=org, org_id=org, project_id=rec["project_id"])
                rec.update(tool=context["tool_name"], capability_provenance=context)
            state.linked[rec["job_id"]] = rec
            state.records.append({key: deepcopy(value) for key, value in rec.items()
                                  if key != "capability_provenance"})
    def canonical(tenant, *, limit, **kwargs):
        calls["lists"].append(("canonical", tenant, limit, kwargs))
        return deepcopy(state.records)
    def legacy(tenant, limit):
        calls["lists"].append(("legacy", tenant, limit, {}))
        return deepcopy(state.legacy)
    def mirror(job, tenant):
        calls["mirror"].append((job, tenant))
        return deepcopy(state.linked[job])
    monkeypatch.setattr(route.jobs.platform_link, "list_canonical_jobs", canonical)
    monkeypatch.setattr(route.jobs, "list_jobs", legacy)
    monkeypatch.setattr(route.jobs, "job_store_mode", lambda: "postgres")
    monkeypatch.setattr(route.jobs, "_pg_store", SimpleNamespace(linked_capability_job=mirror))
    return SimpleNamespace(state=state, calls=calls, caller=caller, seed=seed, subject=subject)


@pytest.mark.parametrize("count", [0, 1, 20])
@pytest.mark.parametrize("kind", ["graph", "capability"])
def test_job_list_reuses_one_fresh_binding_with_actual_membership(job_list_binding_lane, count, kind):
    lane = job_list_binding_lane
    tenant = lane.caller()
    lane.seed(count, kind)
    result = route.list_jobs(20, tenant)
    assert len(result["jobs"]) == count
    assert len(lane.calls["binding"]) == 1
    assert len(lane.calls["membership"]) == len(lane.calls["mirror"]) == count
    assert all(not write for project, write in lane.calls["membership"])
    assert all(tenant_id == str(O1) for _source, tenant_id, _limit, _kwargs in lane.calls["lists"])
    print("BSP34_COUNT=" + json.dumps({"kind": kind, "records": count,
          "handlerBindingCalls": len(lane.calls["binding"]),
          "membershipCalls": len(lane.calls["membership"]), "returned": len(result["jobs"])}))


@pytest.mark.parametrize("kind", ["graph", "capability"])
def test_job_list_retains_per_record_membership_denial(job_list_binding_lane, kind):
    lane = job_list_binding_lane
    tenant = lane.caller()
    lane.seed(20, kind)
    lane.state.denied.add(str(UUID(int=1007)))
    result = route.list_jobs(20, tenant)
    assert len(result["jobs"]) == 19
    assert str(UUID(int=10007)) not in {row["job_id"] for row in result["jobs"]}
    assert len(lane.calls["binding"]) == 1 and len(lane.calls["membership"]) == 20


def test_job_list_binding_does_not_survive_a_request(job_list_binding_lane):
    lane = job_list_binding_lane
    first = lane.caller()
    lane.seed(1, "graph")
    assert len(route.list_jobs(20, first)["jobs"]) == 1
    assert len(lane.calls["binding"]) == 1
    lane.calls["binding"].clear()
    lane.state.binding = SimpleNamespace(platform_tenant_id=O2, binding_id=UUID(int=4))
    second = lane.caller()
    lane.seed(1, "capability")
    assert str(second) == str(O2)
    assert len(route.list_jobs(20, second)["jobs"]) == 1
    assert len(lane.calls["binding"]) == 1
    assert [tenant for _kind, tenant, _limit, _options in lane.calls["lists"]] == [str(O1)] * 2 + [str(O2)] * 2


def test_job_list_forged_resolved_marker_keeps_server_namespace(job_list_binding_lane):
    lane = job_list_binding_lane
    forged = deps.TenantContext(str(O2), org_id=str(O2), tier="hosted_pro",
                               subject=lane.subject, authority_resolved=True)
    lane.seed(1, "graph")
    assert route.list_jobs(20, forged)["jobs"] == []
    assert len(lane.calls["binding"]) == 1
    assert [tenant for _kind, tenant, _limit, _options in lane.calls["lists"]] == [str(O1)] * 2
    assert lane.calls["membership"] == []


@pytest.mark.parametrize("failure", ["revoked", "unavailable"])
def test_job_list_refuses_missing_binding_or_store_failure(job_list_binding_lane, failure):
    lane = job_list_binding_lane
    tenant = lane.caller()
    if failure == "revoked":
        lane.state.binding = None
        assert route.list_jobs(20, tenant)["jobs"] == []
        assert [value for _kind, value, _limit, _options in lane.calls["lists"]] == [str(tenant)] * 2
    else:
        lane.state.error = RuntimeError("synthetic binding store unavailable")
        with pytest.raises(RuntimeError, match="synthetic binding store unavailable"):
            route.list_jobs(20, tenant)
        assert lane.calls["lists"] == []
    assert len(lane.calls["binding"]) == 1


@pytest.mark.parametrize("kind", ["graph", "capability"])
def test_job_list_keeps_foreign_org_and_provenance_fences(job_list_binding_lane, kind):
    lane = job_list_binding_lane
    tenant = lane.caller()
    lane.seed(1, kind)
    rec = next(iter(lane.state.linked.values()))
    rec["org_id"] = str(O2)
    if kind == "capability":
        rec["capability_provenance"]["org_id"] = str(O2)
    assert route.list_jobs(20, tenant)["jobs"] == []
    assert len(lane.calls["binding"]) == 1
    rec["tenant_id"] = str(O2)
    assert route.list_jobs(20, tenant)["jobs"] == []
    assert len(lane.calls["binding"]) == 2


def test_job_list_keeps_duplicates_and_limit(job_list_binding_lane):
    lane = job_list_binding_lane
    tenant = lane.caller()
    lane.seed(20, "graph")
    lane.state.legacy = [deepcopy(next(iter(lane.state.linked.values())))]
    result = route.list_jobs(1, tenant)
    assert len(result["jobs"]) == 1
    assert len(lane.calls["binding"]) == 1 and len(lane.calls["membership"]) == 21
    assert all(limit == 1 for _source, _tenant, limit, _kwargs in lane.calls["lists"])


def test_single_job_observation_defaults_still_resolve_fresh(job_list_binding_lane):
    lane = job_list_binding_lane
    tenant = lane.caller()
    lane.seed(1, "capability")
    rec = next(iter(lane.state.linked.values()))
    assert route._access_error(rec, tenant, rec["job_id"]) is None
    assert len(lane.calls["binding"]) == 2 and len(lane.calls["membership"]) == 1
    lane.state.binding = None
    assert route._access_error(rec, tenant, rec["job_id"]).status_code == 404
    assert len(lane.calls["binding"]) == 4 and len(lane.calls["membership"]) == 1


def test_job_list_keeps_auth_off_and_demo_paths(job_list_binding_lane, monkeypatch):
    lane = job_list_binding_lane
    monkeypatch.setattr(deps, "auth_live", lambda: False)
    lane.state.legacy = [{"job_id": "legacy", "tenant_id": "standalone", "status": "running"}]
    assert route.list_jobs(20, "standalone")["jobs"][0]["job_id"] == "legacy"
    assert lane.calls["binding"] == []
    actor = SimpleNamespace(server_tenant="demo-owned", org_id=O1, project_id=P1)
    monkeypatch.setattr(route, "is_demo_context", lambda tenant: tenant == "demo")
    monkeypatch.setattr(route, "resolve_demo_actor", lambda tenant: actor)
    lane.state.legacy = [{"job_id": "demo-job", "tenant_id": "demo-owned", "status": "running",
                          "org_id": str(O1), "project_id": str(P1)}]
    assert route.list_jobs(20, "demo")["jobs"][0]["job_id"] == "demo-job"
    assert lane.calls["binding"] == lane.calls["membership"] == []
