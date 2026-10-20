"""Mint-to-admission/observation proofs through one real-loader PostgreSQL schema."""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.responses import JSONResponse

import demo_actor as adapter
import demo_sessions as demo
import deps
import entitlements
import platform_link
import solar_local_graph
import solar_project_context as project
import solar_tools
from routers import capabilities, jobs as route, project_drawings as drawings
from test_demo_sessions_postgres import postgres_registry, request
from test_demo_provisioning_postgres import _schema_url, real_loader
from test_sip_r3a_graph import INTAKE, UNITS
from test_demo_actor import legacy_close_store
from test_demo_actor import (ACTOR_ERRORS, assert_unavailable, canonical_run_setup,
                             catalog_preview_setup, admission_setup)


pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"),
    reason="PostgreSQL race test requires explicit DATABASE_URL")


@pytest.fixture
def scope(postgres_registry, monkeypatch):
    """Use the fixture's sole migrated schema through the real shared loader/pool."""
    state = postgres_registry
    db = real_loader()
    db.reset_pool()
    url = _schema_url(os.environ["DATABASE_URL"], state.schema)
    monkeypatch.setattr(db, "get_database_url", lambda: url)
    monkeypatch.setattr(demo, "_db", real_loader)
    monkeypatch.setattr(demo, "_now", lambda: datetime.now(timezone.utc))
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    monkeypatch.setenv("LEAF_CHECKOUT_CAP_SECRET", "demo-actor-test-checkout")
    try:
        tokens = [demo.mint(request(f"192.0.2.{i}")) for i in (1, 2)]
        tenants = [deps.demo_tenant_context(demo.verify(token.token)) for token in tokens]
        actors = [adapter.resolve_demo_actor(tenant) for tenant in tenants]
        yield SimpleNamespace(db=db, state=state, tokens=tokens, tenants=tenants, actors=actors)
    finally:
        db.reset_pool()


def execute(s, sql, args=()):
    return s.db.run_transaction(lambda conn: conn.execute(sql, args).fetchall()
        if sql.startswith("SELECT") else conn.execute(sql, args))


def test_mint_resolves_exact_binding_and_legal_revoked_history(scope):
    s = scope
    actor = adapter.resolve_demo_actor(s.tenants[0])
    assert actor.org_id != actor.project_id
    assert actor.server_tenant == str(s.tenants[0]) == "demo-" + s.tokens[0].token.split(".")[0]
    assert actor.member_role == "editor" and actor.binding.external_authority == "leaf-demo"
    rows = execute(s, "SELECT role FROM identity_bindings WHERE binding_id=%s", (actor.binding_id,))
    assert rows == [{"role": "read_only"}]
    execute(s, "INSERT INTO identity_bindings (binding_id, external_authority, external_subject, "
        "platform_tenant_id, platform_user_id, role, status, revoked_at) "
        "VALUES (%s, 'leaf-demo', %s, %s, %s, 'read_only', 'revoked', now())",
        (uuid4(), s.tenants[0].subject, actor.org_id, uuid4()))
    assert adapter.resolve_demo_actor(s.tenants[0]) == actor
    execute(s, "UPDATE identity_bindings SET status='revoked', revoked_at=now() WHERE binding_id=%s",
            (actor.binding_id,))
    with pytest.raises(adapter.DemoActorRefused):
        adapter.resolve_demo_actor(s.tenants[0])


ROW_FAILURES = ("binding_revoked", "binding_missing", "binding_mismatch", "inactive_org", "account_org",
                "deleted_at", "deleted_status", "second_project", "no_project",
                "member_revoked", "member_missing", "session_revoked", "session_expired")


@pytest.mark.parametrize("failure", ROW_FAILURES)
def test_durable_row_refusals(scope, monkeypatch, failure):
    s, actor = scope, scope.actors[0]
    tenant = s.tenants[0]
    assert project._access(tenant, actor.project_id, write=True) == (actor.org_id, actor.binding_id)
    statements = {
        "binding_revoked": ("UPDATE identity_bindings SET status='revoked', revoked_at=now() WHERE binding_id=%s", actor.binding_id),
        "binding_missing": ("UPDATE identity_bindings SET external_subject='unrelated' WHERE binding_id=%s", actor.binding_id),
        "inactive_org": ("UPDATE orgs SET status='offboarding' WHERE org_id=%s", actor.org_id),
        "account_org": ("UPDATE orgs SET account_class='customer' WHERE org_id=%s", actor.org_id),
        "deleted_at": ("UPDATE projects SET deleted_at=now() WHERE project_id=%s", actor.project_id),
        "deleted_status": ("UPDATE projects SET status='deleted' WHERE project_id=%s", actor.project_id),
        "member_revoked": ("UPDATE project_member_bindings SET status='revoked', revoked_at=now() WHERE binding_id=%s", actor.binding_id),
        "member_missing": ("DELETE FROM project_member_bindings WHERE binding_id=%s", actor.binding_id),
        "session_revoked": ("UPDATE demo_sessions SET revoked_at=now() WHERE subject=%s", tenant.subject),
        "session_expired": ("UPDATE demo_sessions SET created_at=now()-interval '30 minutes', "
                            "expires_at=now()-interval '1 second' WHERE subject=%s", tenant.subject),
    }
    if failure == "binding_mismatch":
        execute(s, "DELETE FROM project_member_bindings WHERE binding_id=%s", (actor.binding_id,))
        execute(s, "UPDATE identity_bindings SET platform_tenant_id=%s WHERE binding_id=%s",
                (s.actors[1].org_id, actor.binding_id))
    elif failure == "second_project":
        execute(s, "INSERT INTO projects (org_id, project_id, name) VALUES (%s,%s,'Unjoined project')",
                (actor.org_id, uuid4()))
    elif failure == "no_project":
        execute(s, "DELETE FROM projects WHERE project_id=%s", (actor.project_id,))
    else:
        sql, value = statements[failure]
        execute(s, sql, (value,))
    with pytest.raises(adapter.DemoActorRefused):
        adapter.resolve_demo_actor(tenant)
    response = drawings._dispatch(lambda: project._access(tenant, actor.project_id, write=True))
    assert response.status_code == 403
    assert json.loads(response.body)["error"]["reason_code"] == "demo_actor_refused"
    if failure in {"session_revoked", "session_expired"}:
        from unittest.mock import Mock
        canonical = Mock(side_effect=AssertionError("canonical jobs must not be read"))
        legacy = Mock(side_effect=AssertionError("legacy jobs must not be read"))
        monkeypatch.setattr(platform_link, "list_canonical_jobs", canonical)
        monkeypatch.setattr(route.jobs, "list_jobs", legacy)
        response = route.list_jobs(tenant=tenant)
        assert response.status_code == 403
        assert json.loads(response.body)["error"]["reason_code"] == "demo_actor_refused"
        canonical.assert_not_called()
        legacy.assert_not_called()
        # Close must refresh the same durable liveness predicate even when the
        # requested job does not exist, before touching any observation store.
        monkeypatch.setattr(route.jobs, "get_job",
                            lambda *a: pytest.fail("job read after dead demo session"))
        monkeypatch.setattr(route.jobs, "mark_job_closed",
                            lambda *a: pytest.fail("close after dead demo session"))
        response = route.close_job(str(uuid4()), tenant)
        assert response.status_code == 403
        assert json.loads(response.body)["error"]["reason_code"] == "demo_actor_refused"


def drawing(s, monkeypatch, *, persist=False):
    """Test-only import preparation; production mint still creates no drawing."""
    from psycopg.types.json import Jsonb
    store = platform_link.platform_store()
    actor = s.actors[0]
    artifact = store.create_drawing_artifact(actor.org_id, actor.project_id)
    version = store.create_drawing_version(actor.org_id, actor.project_id, drawing_id=artifact.drawing_id)
    raw = solar_local_graph.canonical_bytes(deepcopy(INTAKE))
    digest = hashlib.sha256(raw).hexdigest()
    source = str(uuid4())
    key = f"tenants/{actor.org_id}/drawings/{source}/v/00000001.intake.json"
    provenance = {"schema": "leaf.drawing-import.v1", "source": {
        "kind": "account_upload", "tenant_id": str(actor.org_id), "drawing_id": source,
        "version": 1, "stored_object": {"ref": key[:-12] + ".dwg"},
        "intake": {"ref": key, "sha256": digest}}}
    execute(s, "UPDATE drawing_versions SET provenance=%s, intake_ref=%s, oss_object=%s WHERE version_id=%s",
        (Jsonb(provenance), key, key[:-12] + ".dwg", version.version_id))
    s.reads, s.writes, s.namespaces = [], [], []
    objects = {key: raw}

    def backend(tenant):
        s.namespaces.append(str(tenant))
        assert str(tenant) == str(actor.org_id)
        def get(ref):
            s.reads.append(ref)
            if not persist:
                assert ref == key
            return objects[ref]
        def put(ref, content):
            s.writes.append((ref, content))
            if persist:
                assert ref not in objects or objects[ref] == content
                objects[ref] = content
        return SimpleNamespace(get=get, put_if_absent_or_verify=put)
    monkeypatch.setattr(project.write_loop, "upload_backend_for_tenant", backend)
    return artifact.drawing_id, version.version_id, digest


def test_demo_w1_through_production_app_and_real_worker(scope, monkeypatch):
    """Only X-Demo-Session authenticates; no dependency or entitlement overrides."""
    from fastapi.testclient import TestClient
    import app as composition
    import canonical_worker
    import solar_project_jobs

    s, actor = scope, scope.actors[0]
    draw, version, digest = drawing(s, monkeypatch, persist=True)
    assert not composition.app.dependency_overrides
    client = TestClient(composition.app)  # No lifespan/background worker race.
    identity = {"X-Demo-Session": s.tokens[0].token}
    context_path = f"/api/projects/{actor.project_id}/drawing-versions/{version}/context"
    response = client.get(context_path, headers=identity)
    assert response.status_code == 200, response.text
    assert response.json()["context"]["drawing_id"] == str(draw)
    response = client.post(
        f"/api/projects/{actor.project_id}/drawings/{draw}/checkout", headers=identity)
    assert response.status_code == 200, response.text
    checkout = response.json()["checkout_capability"]
    headers = {**identity, "X-Org-Id": str(actor.org_id),
        "X-Project-Id": str(actor.project_id), "X-Checkout-Capability": checkout,
        "Idempotency-Key": "demo-w1-production-app"}
    tool = solar_tools.trusted_record("solar-settings")
    payload = {"tool": "solar-settings", "dwg": str(version),
        "catalog_digest": deps.catalog_tool_digest(tool), "params": {
            "drawing_id": str(draw), "expected_rev": 0,
            "changes": {"panels_in_sequence": 3}, "initialize": {
                "schema_version": 1, "source_intake_sha256": digest,
                "units": deepcopy(UNITS)}}}
    response = client.post("/api/run", headers=headers, json=payload)
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    assert canonical_worker.run_once("demo-w1-worker", tool_name="solar-settings") is True
    response = client.get(f"/api/jobs/{job_id}", headers=identity)
    assert response.status_code == 200, response.text
    # The HTTP job vocabulary maps the stored succeeded state to complete.
    assert response.json()["status"] == "complete", response.json()
    assert execute(s, "SELECT status FROM jobs WHERE job_id=%s", (UUID(job_id),)) == [
        {"status": "succeeded"}]

    before = snapshot(s)
    grant_reads = []
    def forbidden_grant(*args, **kwargs):
        grant_reads.append(args)
        pytest.fail("demo global sizing read a cloud grant")
    monkeypatch.setattr(solar_project_jobs.cloud, "resolve_grant", forbidden_grant)
    sizing_tool = solar_tools.trusted_record("solar-size-strings")
    paid = {"tool": "solar-size-strings", "dwg": str(version),
        "catalog_digest": deps.catalog_tool_digest(sizing_tool),
        "params": {"drawing_id": str(draw), "mode": "global"}}
    response = client.post("/api/run", headers={**headers,
        "Idempotency-Key": "demo-paid-refused"}, json=paid)
    assert response.status_code == 403, response.text
    assert response.json()["error"]["reason_code"] == "demo_paid_service_not_allowed"
    assert snapshot(s) == before and grant_reads == []
    response = client.post("/api/run", headers={**headers,
        "X-Project-Id": str(s.actors[1].project_id),
        "Idempotency-Key": "demo-foreign-refused"}, json=payload)
    assert response.status_code == 403, response.text
    assert response.json()["error"]["reason_code"] == "demo_actor_refused"
    response = client.get(context_path.replace(str(actor.project_id),
        str(s.actors[1].project_id)), headers=identity)
    assert response.status_code == 403, response.text
    assert client.post("/api/campaigns", headers=identity, json={}).status_code == 403
    response = client.post("/api/sessions", headers=identity, json={"drawing_id": str(draw)})
    assert response.status_code == 403, response.text
    assert response.json()["error"]["reason_code"] == "demo_actor_refused"
    assert snapshot(s) == before and grant_reads == []


def snapshot(s):
    return {table: sorted((dict(row) for row in execute(s, "SELECT * FROM " + table)), key=repr)
            for table in ("jobs", "drawing_versions", "project_drawing_checkouts", "drawing_artifacts")}


OPERATIONS = ("project_access", "context", "checkout_get", "checkout_take", "checkout_release",
              "catalog_preview", "admission", "job_get", "job_stream", "job_wait", "job_list")


@pytest.mark.parametrize("operation", OPERATIONS)
def test_same_actor_then_foreign_actor_matrix(scope, monkeypatch, operation):
    """Test-only capability/stored-policy overrides isolate the tenancy layer."""
    from leaf_platform import canonical_jobs, entitlements as stored
    s, actor = scope, scope.actors[0]
    same, foreign = s.tenants
    draw, version, digest = drawing(s, monkeypatch)
    # These are the only entitlement changes: both attempts share these overrides.
    original_entitlements = entitlements.entitlements_for
    monkeypatch.setattr(entitlements, "entitlements_for", lambda tier, *args:
        {"run_write": True, "run_read": True} if tier == "demo_live" else original_entitlements(tier, *args))
    original_verdict = stored.stored_job_entitlement_verdict
    demo_orgs = {value.org_id for value in s.actors}
    monkeypatch.setattr(stored, "stored_job_entitlement_verdict", lambda org, kind:
        (None, platform_link.platform_store().get_org(org)) if org in demo_orgs
        else original_verdict(org, kind))
    tools = solar_tools.trusted_snapshot()
    tool = tools["solar-settings"]
    monkeypatch.setattr(deps, "find_tool", lambda name, *args: tools.get(name))
    monkeypatch.setattr(deps, "effective_tools_with_provenance", lambda *args:
        [(tool, None) for tool in tools.values()])
    monkeypatch.setattr(deps, "load_engine_registry_tools", lambda: [])
    monkeypatch.setattr(capabilities.mcp_tool_projection, "projected_tools", lambda *args: [])
    monkeypatch.setattr(capabilities.customization_service, "effective_catalog_pin", lambda *args: None)
    monkeypatch.setattr(route.jobs, "get_job", lambda *args: None)
    monkeypatch.setattr(route.jobs, "list_jobs", lambda *args: [])
    monkeypatch.setattr(route.jobs, "job_store_mode", lambda: "legacy")
    lease, token = project.acquire_project_checkout(same, actor.project_id, draw)
    params = {"drawing_id": str(draw), "expected_rev": 0, "changes": {"panels_in_sequence": 3},
        "initialize": {"schema_version": 1, "source_intake_sha256": digest, "units": deepcopy(UNITS)}}
    req = route.RunRequest(tool="solar-settings", params=params, dwg=str(version),
        catalog_digest=deps.catalog_tool_digest(tool))

    def submit(tenant):
        return route.run(req, wait=0, tenant_id=tenant, x_org_id=str(actor.org_id),
            x_project_id=str(actor.project_id), authorization=None,
            x_checkout_capability=token, idempotency_key="matrix-admission")

    job_id = None
    if operation.startswith("job_"):
        submitted = submit(same)
        assert submitted.status_code == 202, submitted.body
        job_id = json.loads(submitted.body)["job_id"]
        row = canonical_jobs.get_job_for_tenant(UUID(job_id), str(same))
        assert row["request_tenant_id"] == str(same)
        assert row["org_id"] == str(actor.org_id) and row["project_id"] == str(actor.project_id)

    def invoke(tenant):
        if operation == "project_access":
            try:
                return platform_link.require_project_access(tenant, actor.project_id, write=True)
            except adapter.DemoActorRefused:
                return route._demo_refusal()
        if operation == "context":
            return drawings.context_route(actor.project_id, version, drawing_id=draw, tenant=tenant,
                x_org_id=actor.org_id, x_project_id=actor.project_id)
        if operation == "checkout_get":
            return drawings.status_route(actor.project_id, draw, tenant, actor.org_id, actor.project_id)
        if operation == "checkout_take":
            return drawings.acquire_route(actor.project_id, draw, None, tenant, actor.org_id, actor.project_id, token)
        if operation == "checkout_release":
            return drawings.release_route(actor.project_id, draw, tenant, actor.org_id, actor.project_id, token)
        if operation == "admission":
            return submit(tenant)
        if operation == "catalog_preview":
            result = capabilities.capabilities(x_internal_role=None, x_ops_secret=None,
                tenant=tenant, project_id=str(actor.project_id),
                drawing_id=str(draw), input_version_id=str(version), x_org_id=str(actor.org_id),
                x_project_id=str(actor.project_id), authorization=None, x_checkout_capability=token,
                project_runs=json.dumps({"solar-settings": {"params": params,
                    "catalog_digest": req.catalog_digest, "idempotency_key": "preview"}}))
            if not isinstance(result, dict):
                return result
            return next(row["availability"] for family in result["families"]
                        for row in family["capabilities"] if row["name"] == "solar-settings")
        if operation == "job_get":
            return route.get_job(job_id, tenant)
        if operation == "job_list":
            return route.list_jobs(tenant=tenant)
        if operation == "job_wait":
            monkeypatch.setattr(route.jobs, "job_max_s", lambda: -30)
            return route._wait_project_solar(JSONResponse({"job_id": job_id}, status_code=202), tenant)
        if operation == "job_stream":
            async def observe():
                result = await route.stream_job(job_id, tenant)
                if isinstance(result, JSONResponse):
                    return result
                first = await anext(result.body_iterator)
                assert '"status": "submitted"' in first
                await result.body_iterator.aclose()
                return result
            return asyncio.run(observe())
        raise AssertionError(operation)

    control = invoke(same)
    if operation == "project_access":
        assert control == str(actor.org_id)
    elif operation == "catalog_preview":
        assert control["runnable"] is True, control
    elif operation == "job_get":
        assert isinstance(control, dict) and control["job_id"] == job_id
    elif operation == "job_list":
        assert [row["job_id"] for row in control["jobs"]] == [job_id]
    else:
        assert control.status_code in (200, 202), getattr(control, "body", None)
        if operation == "checkout_release":
            assert json.loads(control.body)["organization_id"] == str(actor.org_id)
    before, reads = snapshot(s), list(s.reads)
    refused = invoke(foreign)
    if operation == "job_list":
        assert refused["jobs"] == []
    else:
        assert refused.status_code == 403, getattr(refused, "body", None)
        assert json.loads(refused.body)["error"]["reason_code"] == "demo_actor_refused"
    assert snapshot(s) == before
    assert s.reads == reads and s.writes == []
    assert set(s.namespaces) <= {str(actor.org_id)}


def fail_demo_store_boundary(s, monkeypatch, boundary, stage, error):
    """Fail pool acquisition or real SQL, leaving both authorization passes real.

    Actor lookup uses read-only transactions. Lifecycle membership uses its
    separate repeatable-read transaction; authority uses the store cursor.
    Select only that later boundary, never replace its resolver or role check.
    """
    store = platform_link.platform_store()
    original_pool = s.db.get_pool
    original_transaction = s.db.transaction
    original_cursor = store.cursor
    active, hits = [False], []

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def __getattr__(self, name):
            return getattr(self.cursor, name)

        def execute(self, sql, *args, **kwargs):
            marker = "FROM live_projects" if boundary == "membership" else "FROM projects"
            if marker in sql:
                hits.append("sql")
                raise error("private database details")
            return self.cursor.execute(sql, *args, **kwargs)

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        @contextmanager
        def cursor(self, *args, **kwargs):
            with self.conn.cursor(*args, **kwargs) as cur:
                yield Cursor(cur)

    class Pool:
        @contextmanager
        def connection(self, *args, **kwargs):
            if active[0] and stage == "acquire":
                hits.append("acquire")
                raise error("private database details")
            with original_pool().connection(*args, **kwargs) as conn:
                yield Connection(conn) if active[0] else conn

    @contextmanager
    def transaction(**kwargs):
        previous = active[0]
        active[0] = kwargs.get("isolation") == "repeatable read"
        try:
            with original_transaction(**kwargs) as conn:
                yield conn
        finally:
            active[0] = previous

    @contextmanager
    def cursor():
        previous = active[0]
        active[0] = True
        try:
            with original_cursor() as cur:
                yield cur
        finally:
            active[0] = previous

    monkeypatch.setattr(s.db, "get_pool", lambda: Pool())
    if boundary == "membership":
        monkeypatch.setattr(s.db, "transaction", transaction)
    else:
        monkeypatch.setattr(store, "cursor", cursor)
    return hits


@pytest.mark.parametrize("error", ACTOR_ERRORS)
@pytest.mark.parametrize("stage", ["acquire", "sql"])
def test_demo_membership_reread_outage_transport(scope, monkeypatch, error, stage):
    import conversations

    s, actor, tenant = scope, scope.actors[0], scope.tenants[0]
    state = SimpleNamespace(org=actor.org_id, project=actor.project_id, tenant=tenant)
    _context, run = canonical_run_setup(state, monkeypatch)
    monkeypatch.setenv("LEAF_CONV_DURABLE_ENABLED", "1")
    hits = fail_demo_store_boundary(s, monkeypatch, "membership", stage, error)
    # This still traverses the real mint/session/binding/org/project lookup.
    assert adapter.resolve_demo_actor(tenant) == actor
    assert hits == []
    with pytest.raises(adapter.DemoActorUnavailable) as caught:
        platform_link.require_project_access(tenant, actor.project_id, write=True)
    assert isinstance(caught.value.__cause__, error)
    assert hits == [stage]
    monkeypatch.setattr(platform_link, "submit_canonical_solve",
                        lambda *a, **kw: pytest.fail("submission after membership outage"))
    monkeypatch.setattr(platform_link.platform_store(), "resolve_active_identity_binding",
                        lambda *a, **kw: pytest.fail("Auth0 fallback"))
    assert_unavailable(run(), "job store unavailable")
    assert_unavailable(route.platform_capabilities(tenant, str(actor.org_id), str(actor.project_id), None),
                       "job store unavailable")
    conversation_calls = [
        lambda: conversations.api_create_conversation(str(actor.project_id),
            conversations.CreateConversationRequest(title="outage"), tenant),
        lambda: conversations.api_list_conversations(str(actor.project_id), tenant),
        lambda: conversations.api_get_conversation(str(actor.project_id), str(uuid4()), tenant),
        lambda: conversations.api_recover_conversation(str(actor.project_id), tenant),
    ]
    for invoke in conversation_calls:
        assert_unavailable(invoke(), "conversation store unavailable")
    assert hits == [stage] * 7


@pytest.mark.parametrize("error", ACTOR_ERRORS)
@pytest.mark.parametrize("stage", ["acquire", "sql"])
def test_demo_authority_read_outage_transport(scope, monkeypatch, error, stage):
    s, actor, tenant = scope, scope.actors[0], scope.tenants[0]
    state = SimpleNamespace(org=actor.org_id, project=actor.project_id, tenant=tenant)
    _context, run = canonical_run_setup(state, monkeypatch)
    _admission, admit = admission_setup(state, monkeypatch)
    preview = catalog_preview_setup(state, monkeypatch)
    hits = fail_demo_store_boundary(s, monkeypatch, "authority", stage, error)
    assert platform_link.require_project_access(tenant, actor.project_id, write=True) == str(actor.org_id)
    assert hits == []
    with pytest.raises(adapter.DemoActorUnavailable) as caught:
        platform_link.resolve_submission_context(str(actor.org_id), str(actor.project_id),
                                                 None, tenant_context=tenant)
    assert isinstance(caught.value.__cause__, error)
    assert hits == [stage]
    monkeypatch.setattr(platform_link, "submit_canonical_solve",
                        lambda *a, **kw: pytest.fail("submission after authority outage"))
    monkeypatch.setattr(platform_link.platform_store(), "resolve_active_identity_binding",
                        lambda *a, **kw: pytest.fail("Auth0 fallback"))
    assert_unavailable(run(), "job store unavailable")
    assert_unavailable(route.platform_capabilities(tenant, str(actor.org_id), str(actor.project_id), None),
                       "job store unavailable")
    result = admit()
    assert_unavailable(JSONResponse(result["content"], status_code=result["status_code"]))
    assert_unavailable(preview(tenant))
    assert hits == [stage] * 5


def test_demo_authority_removed_between_checks_is_refused(scope, monkeypatch):
    s, actor, tenant = scope, scope.actors[0], scope.tenants[0]
    store = platform_link.platform_store()
    original_cursor = store.cursor
    removed = []

    @contextmanager
    def cursor():
        # Authority reads are reached only after real actor and membership
        # transactions succeeded. Delete through a real separate transaction.
        if not removed:
            execute(s, "UPDATE projects SET deleted_at=now() WHERE project_id=%s", (actor.project_id,))
            removed.append(True)
        with original_cursor() as cur:
            yield cur

    monkeypatch.setattr(store, "cursor", cursor)
    with pytest.raises(adapter.DemoActorRefused) as caught:
        platform_link.resolve_submission_context(str(actor.org_id), str(actor.project_id),
                                                 None, tenant_context=tenant)
    assert isinstance(caught.value.__cause__, platform_link.ProjectAuthorityNotFound)
    assert removed == [True]


@pytest.mark.parametrize("mode", ["legacy", "postgres"])
def test_close_completely_unknown_job_is_404(scope, monkeypatch, legacy_close_store, mode):
    s, tenant = scope, scope.tenants[0]
    job_id = str(uuid4())
    monkeypatch.setattr(route.jobs, "job_store_mode", lambda: mode)
    statements = []
    legacy_close_store.set_trace_callback(statements.append)
    assert route.jobs.get_job(job_id) is None
    assert platform_link.get_canonical_job(job_id, str(tenant), strict=True) is None
    before = snapshot(s)
    response = route.close_job(job_id, tenant)
    assert response.status_code == 404
    error = json.loads(response.body)["error"]
    assert error["message"] == f"unknown job_id: {job_id}"
    assert error["retryable"] is False
    assert snapshot(s) == before
    assert route.jobs.get_job(job_id) is None
    assert not any(sql.startswith("UPDATE") for sql in statements)


def test_membership_downgrade_and_auth0_baseline(scope):
    s, actor = scope, scope.actors[0]
    assert platform_link.require_project_access(s.tenants[0], actor.project_id, write=True) == str(actor.org_id)
    execute(s, "UPDATE project_member_bindings SET role='read_only' WHERE binding_id=%s", (actor.binding_id,))
    assert platform_link.require_project_access(s.tenants[0], actor.project_id, write=False) == str(actor.org_id)
    with pytest.raises(adapter.DemoActorRefused):
        platform_link.require_project_access(s.tenants[0], actor.project_id, write=True)
    # The existing Auth0 binding resolver and membership path stay authoritative.
    binding = platform_link.platform_store().create_identity_binding(actor.org_id, "auth0", "account-baseline")
    execute(s, "INSERT INTO project_member_bindings "
        "(membership_id,org_id,project_id,binding_id,role,invited_by_binding_id) "
        "VALUES (%s,%s,%s,%s,'editor',%s)",
        (uuid4(), actor.org_id, actor.project_id, binding.binding_id, binding.binding_id))
    tenant = deps.TenantContext(str(actor.org_id), org_id=str(actor.org_id), subject="account-baseline")
    assert adapter.resolve_demo_actor(tenant) is None
    assert platform_link.require_project_access(tenant, actor.project_id, write=True) == str(actor.org_id)
    assert route._access_error(dict(tenant_id=str(actor.org_id), org_id=str(actor.org_id),
        project_id=str(actor.project_id), execution_context={"schema": "leaf.solar-project-graph-job.v1"}),
        tenant, "baseline") is None


@pytest.mark.parametrize("transport", ["stream", "wait"])
@pytest.mark.parametrize("failure", ["revoked", "expired", "disabled"])
def test_observation_refreshes_real_session_after_first_resolution(scope, monkeypatch, transport, failure):
    """Test-only capability/stored-policy overrides isolate the tenancy layer."""
    from leaf_platform import entitlements as stored
    s, actor, tenant = scope, scope.actors[0], scope.tenants[0]
    draw, version, digest = drawing(s, monkeypatch)
    original_entitlements = entitlements.entitlements_for
    monkeypatch.setattr(entitlements, "entitlements_for", lambda tier, *args:
        {"run_write": True, "run_read": True} if tier == "demo_live" else original_entitlements(tier, *args))
    original_verdict = stored.stored_job_entitlement_verdict
    demo_orgs = {value.org_id for value in s.actors}
    monkeypatch.setattr(stored, "stored_job_entitlement_verdict", lambda org, kind:
        (None, platform_link.platform_store().get_org(org)) if org in demo_orgs
        else original_verdict(org, kind))
    _lease, token = project.acquire_project_checkout(tenant, actor.project_id, draw)
    tool = solar_tools.trusted_record("solar-settings")
    req = route.RunRequest(tool="solar-settings", dwg=str(version),
        catalog_digest=deps.catalog_tool_digest(tool), params={"drawing_id": str(draw),
            "expected_rev": 0, "changes": {"panels_in_sequence": 3}, "initialize": {
                "schema_version": 1, "source_intake_sha256": digest, "units": deepcopy(UNITS)}})
    submitted = route._project_solar_admission(tool, tenant, req, str(actor.org_id),
        str(actor.project_id), None, token, idempotency_key="observe-refresh", preview=False)
    assert submitted.status_code == 202, submitted.body
    job_id = json.loads(submitted.body)["job_id"]
    monkeypatch.setattr(route.jobs, "get_job", lambda *args: None)
    monkeypatch.setattr(route.jobs, "job_store_mode", lambda: "legacy")
    assert isinstance(route.get_job(job_id, tenant), dict)

    def invalidate():
        if failure == "disabled":
            monkeypatch.setenv("LEAF_DEMO_LIVE_ENABLED", "0")
        elif failure == "revoked":
            execute(s, "UPDATE demo_sessions SET revoked_at=now() WHERE subject=%s", (tenant.subject,))
        else:
            execute(s, "UPDATE demo_sessions SET created_at=now()-interval '30 minutes', "
                "expires_at=now()-interval '1 second' WHERE subject=%s", (tenant.subject,))

    if transport == "stream":
        async def observe():
            response = await route.stream_job(job_id, tenant)
            iterator = response.body_iterator
            assert '"status": "submitted"' in await anext(iterator)
            invalidate()
            final = await anext(iterator)
            assert "demo_actor_refused" in final and '"progress"' not in final
            with pytest.raises(StopAsyncIteration):
                await anext(iterator)
        asyncio.run(observe())
    else:
        monkeypatch.setattr(route.time, "sleep", lambda _seconds: invalidate())
        response = route._wait_project_solar(submitted, tenant)
        assert response.status_code == 403
        assert json.loads(response.body)["error"]["reason_code"] == "demo_actor_refused"


@pytest.mark.parametrize("operation", ["canonical_run", "project_read", "platform_capabilities", "close"])
def test_extended_real_database_scope_matrix(scope, monkeypatch, legacy_close_store, operation):
    """Resolve real minted actors and membership in the fixture's single schema."""
    import tool_loader
    from leaf_platform import canonical_jobs, entitlements as stored
    from test_demo_actor import canonical_run_setup

    s, actor = scope, scope.actors[0]
    same, foreign = s.tenants
    draw, version, _digest = drawing(s, monkeypatch)
    state = SimpleNamespace(org=actor.org_id, project=actor.project_id, tenant=same)
    context, run = canonical_run_setup(state, monkeypatch, input_version=version)
    # Entitlements are deliberately fixed for both actors; authorization and
    # canonical submission still use the real loader, pool, rows and membership.
    monkeypatch.setattr(stored, "stored_job_entitlement_verdict", lambda org, kind:
                        (None, platform_link.platform_store().get_org(org)))
    monkeypatch.setattr(route.jobs, "job_store_mode", lambda: "legacy")
    monkeypatch.setattr(platform_link, "canonical_worker_health", lambda *a: None)
    executed = []
    def execute_tool(tool, intake, params, **kw):
        executed.append(kw["tenant_id"])
        return {"ok": True, "result": {"read": True}}
    monkeypatch.setattr(tool_loader, "run_tool_dynamic", execute_tool)
    job_id = None
    if operation == "close":
        submitted = run()
        assert submitted.status_code == 202, submitted.body
        job_id = json.loads(submitted.body)["job_id"]
        job_before = canonical_jobs.get_job_for_tenant(UUID(job_id), str(same))

    def invoke(tenant):
        state.tenant = tenant
        if operation == "canonical_run":
            return run()
        if operation == "platform_capabilities":
            return route.platform_capabilities(tenant, str(actor.org_id), str(actor.project_id), None)
        if operation == "close":
            return route.close_job(job_id, tenant)
        req = route.RunRequest(tool="scope-read", dwg=str(version))
        return route._run_project_read({"name": "scope-read"}, tenant,
            (str(actor.org_id), str(actor.project_id)), req, {"drawing_id": str(draw)})

    control = invoke(same)
    if operation == "close":
        assert control.status_code == 404, control.body
        assert json.loads(control.body)["error"]["message"] == f"unknown job_id: {job_id}"
        assert canonical_jobs.get_job_for_tenant(UUID(job_id), str(same)) == job_before
        assert route.jobs.get_job(job_id) is None
        # Auth0/legacy callers retain their canonical-only 404 as well.
        account = deps.TenantContext(str(actor.org_id), subject="auth0|account")
        baseline = route.close_job(job_id, account)
        assert baseline.status_code == 404
        assert json.loads(baseline.body)["error"]["message"] == f"unknown job_id: {job_id}"
    elif operation == "platform_capabilities":
        assert control["projectId"] == str(actor.project_id)
    else:
        assert control.status_code in (200, 202), control.body
        body = json.loads(control.body)
        if operation == "canonical_run":
            row = canonical_jobs.get_job_for_tenant(UUID(body["job_id"]), str(same))
            assert row["request_tenant_id"] == str(same)
            assert row["org_id"] == str(actor.org_id)
            assert row["project_id"] == str(actor.project_id)
        else:
            assert body["project_context"]["organization_id"] == str(actor.org_id)
            assert body["project_context"]["project_id"] == str(actor.project_id)
            assert executed == [str(same)]
    before, reads, executions = snapshot(s), list(s.reads), list(executed)
    refused = invoke(foreign)
    if operation == "close":
        assert refused.status_code == 404, refused.body
        assert json.loads(refused.body)["error"]["message"] == f"unknown job_id: {job_id}"
    else:
        assert refused.status_code == 403, refused.body
        assert json.loads(refused.body)["error"]["reason_code"] == "demo_actor_refused"
    assert snapshot(s) == before and s.reads == reads
    assert executed == executions
    if operation == "close":
        assert canonical_jobs.get_job_for_tenant(UUID(job_id), str(same)) == job_before
    assert set(s.namespaces) <= {str(actor.org_id)}
