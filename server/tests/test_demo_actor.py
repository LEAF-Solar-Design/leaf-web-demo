"""Verified demo identity, refusal transports and scope controls without a database."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

import demo_actor as adapter
import deps
import platform_link
import solar_project_context as project
from routers import jobs as route, project_drawings as drawings


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class Registry:
    """Row double that independently applies each durable liveness predicate."""
    def __init__(self):
        self.org, self.project, self.binding = [uuid4() for _ in range(3)]
        session = uuid4()
        self.tenant = deps.TenantContext("demo-" + str(session), org_id=str(self.org),
            subject="demo:" + str(session), tier="demo_live", verified_demo=True)
        self.session_live = True
        self.bindings = [dict(binding_id=self.binding, external_authority="leaf-demo",
            external_subject=self.tenant.subject, platform_tenant_id=self.org,
            platform_user_id=uuid4(), status="active", revoked_at=None)]
        self.org_active, self.account_class = True, "demo"
        self.projects = [dict(project_id=self.project, status="active", deleted_at=None)]
        self.members = [dict(role="editor", status="active", revoked_at=None)]
        self.statements = []

    def execute(self, sql, args):
        self.statements.append(sql)
        if "FROM demo_sessions" in sql:
            assert "revoked_at IS NULL" in sql and "expires_at >" in sql
            return Rows([dict(subject=self.tenant.subject, tenant_id=str(self.tenant), org_id=self.org)]
                        if self.session_live else [])
        if "FROM identity_bindings" in sql:
            assert "external_authority = 'leaf-demo'" in sql
            assert "status = 'active'" in sql and "revoked_at IS NULL" in sql
            return Rows([b for b in self.bindings if b["status"] == "active"
                         and b["revoked_at"] is None and b["external_subject"] == args[0]])
        if "FROM orgs" in sql:
            assert "status = 'active'" in sql and "account_class = 'demo'" in sql
            return Rows([dict(org_id=self.org)] if self.org_active and self.account_class == "demo" else [])
        if "FROM live_projects" in sql:
            return Rows([p for p in self.projects if p["deleted_at"] is None and p["status"] != "deleted"])
        if "FROM project_member_bindings" in sql:
            assert args == (self.org, self.project, self.binding)
            assert "status = 'active'" in sql and "revoked_at IS NULL" in sql
            return Rows([m for m in self.members if m["status"] == "active" and m["revoked_at"] is None])
        raise AssertionError(sql)

    def run_transaction(self, operation, **kwargs):
        return operation(self)


@pytest.fixture
def registry(monkeypatch):
    state = Registry()
    monkeypatch.setenv("LEAF_DEMO_LIVE_ENABLED", "1")
    monkeypatch.setattr(platform_link, "platform_db", lambda: state)
    platform_link._ensure_platform_package()
    from leaf_platform import project_lifecycle

    def membership(org, proj, binding, *, write):
        assert (org, proj, binding) == (state.org, state.project, state.binding)
        if not state.members or (write and state.members[0]["role"] not in {"owner", "editor"}):
            raise project_lifecycle.LifecycleForbidden()
        return state.members[0]["role"]

    monkeypatch.setattr(project_lifecycle, "require_project_role", membership)
    monkeypatch.setattr(platform_link, "platform_store", lambda: SimpleNamespace(
        get_org=lambda org: SimpleNamespace(status="active"), is_account_upload_source_id=lambda value: True))
    monkeypatch.setattr(platform_link, "resolve_project_authority", lambda org, proj:
        dict(org_id=org, project_id=proj, authority_mode="postgres_canonical"))
    return state


def test_verified_actor_binding_interface_and_execution_identity(registry):
    s = registry
    actor = adapter.resolve_demo_actor(s.tenant)
    assert (actor.org_id, actor.project_id, actor.binding_id, actor.member_role, actor.server_tenant) == (
        s.org, s.project, s.binding, "editor", str(s.tenant))
    assert platform_link.resolve_caller_binding(s.tenant) == actor.binding
    assert actor.binding.platform_tenant_id == s.org
    assert platform_link.require_project_access(s.tenant, s.project, write=True) == str(s.org)
    assert project._access(s.tenant, s.project, write=True) == (s.org, s.binding)
    assert route._bound_tenant_id(s.tenant) == str(s.tenant)
    assert route._canonical_tenant_id(s.tenant, dict(org_id=s.org, project_id=s.project)) == str(s.tenant)
    assert drawings._identity(s.tenant, s.project, uuid4())["organization_id"] == str(s.org)


CASES = ("no_binding", "revoked_binding", "duplicate_binding", "wrong_org", "inactive_org",
         "account_org", "zero_projects", "two_projects", "deleted_at", "deleted_status",
         "no_member", "revoked_member", "session_dead", "switch_off")


def corrupt(s, case, monkeypatch):
    if case == "no_binding": s.bindings.clear()
    elif case == "revoked_binding": s.bindings[0].update(status="revoked", revoked_at="now")
    elif case == "duplicate_binding": s.bindings.append(dict(s.bindings[0], binding_id=uuid4()))
    elif case == "wrong_org": s.bindings[0]["platform_tenant_id"] = uuid4()
    elif case == "inactive_org": s.org_active = False
    elif case == "account_org": s.account_class = "standard"
    elif case == "zero_projects": s.projects.clear()
    elif case == "two_projects": s.projects.append(dict(project_id=uuid4(), status="active", deleted_at=None))
    elif case == "deleted_at": s.projects[0]["deleted_at"] = "now"
    elif case == "deleted_status": s.projects[0]["status"] = "deleted"
    elif case == "no_member": s.members.clear()
    elif case == "revoked_member": s.members[0].update(status="revoked", revoked_at="now")
    elif case == "session_dead": s.session_live = False
    elif case == "switch_off": monkeypatch.setenv("LEAF_DEMO_LIVE_ENABLED", "0")


@pytest.mark.parametrize("case", CASES)
def test_refusal_rows_have_typed_403_and_consumer_envelopes(registry, monkeypatch, case):
    """The entitlement override isolates the tenancy layer in admission."""
    import entitlements
    import solar_project_admission as admission
    import solar_tools
    s = registry
    assert adapter.resolve_demo_actor(s.tenant).binding_id == s.binding
    corrupt(s, case, monkeypatch)
    with pytest.raises(adapter.DemoActorRefused) as exc:
        adapter.resolve_demo_actor(s.tenant)
    assert exc.value.status_code == 403 and exc.value.code == "demo_actor_refused"
    response = drawings._dispatch(lambda: project._access(s.tenant, s.project, write=False))
    assert response.status_code == 403
    assert json.loads(response.body)["error"]["reason_code"] == "demo_actor_refused"
    response = drawings._dispatch(lambda: drawings._headers(s.tenant, s.project, None, None))
    assert response.status_code == 403
    record = dict(tenant_id=str(s.tenant), org_id=str(s.org), project_id=str(s.project))
    assert route._access_error(record, s.tenant, "job").status_code == 403
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    original_entitlements = entitlements.entitlements_for
    monkeypatch.setattr(entitlements, "entitlements_for", lambda tier, *args:
        {"run_write": True, "run_read": True} if tier == "demo_live" else original_entitlements(tier, *args))
    tool = solar_tools.trusted_record("solar-settings")
    result = admission.project_tool_admission(tool, s.tenant, org_header=str(s.org),
        project_header=str(s.project), authorization=None, input_version_id=str(uuid4()),
        drawing_id=str(uuid4()), params={}, catalog_digest=deps.catalog_tool_digest(tool),
        idempotency_key="key", checkout_capability=None, preview=True)
    assert result["status_code"] == 403
    assert result["content"]["error"]["reason_code"] == "demo_actor_refused"


def test_revoked_history_is_legal_and_roles_refresh(registry):
    s = registry
    s.bindings.append(dict(s.bindings[0], binding_id=uuid4(), status="revoked", revoked_at="now"))
    assert adapter.resolve_demo_actor(s.tenant).binding_id == s.binding
    s.members[0]["role"] = "read_only"
    assert platform_link.require_project_access(s.tenant, s.project, write=False) == str(s.org)
    with pytest.raises(adapter.DemoActorRefused):
        platform_link.require_project_access(s.tenant, s.project, write=True)


@pytest.mark.parametrize("kind", ["auth0", "guest", "dispatch", "auth_off", "unmarked_demo"])
def test_non_demo_keeps_auth0_authority_and_submission_signature(monkeypatch, kind):
    tenant = deps.TenantContext(str(uuid4()), subject="account" if kind == "auth0" else None)
    if kind == "dispatch": tenant = deps.TenantContext(str(tenant), backedge=True)
    if kind == "auth_off": tenant = str(tenant)
    if kind == "unmarked_demo":
        tenant = deps.TenantContext("demo-" + str(uuid4()), tier="demo_live", subject="demo:" + str(uuid4()))
    assert adapter.resolve_demo_actor(tenant) is None
    calls = []
    binding = SimpleNamespace(platform_tenant_id=UUID(int=1), binding_id=uuid4())
    store = SimpleNamespace(resolve_active_identity_binding=lambda *args: calls.append(args) or binding)
    monkeypatch.setattr(platform_link, "_load_platform", lambda: (store, None, None))
    monkeypatch.setattr(platform_link, "platform_store", lambda: store)
    monkeypatch.setattr(deps, "auth_live", lambda: kind != "auth_off")
    if getattr(tenant, "subject", None):
        assert platform_link.resolve_caller_binding(tenant) is binding
        assert calls == [("auth0", tenant.subject)]
        calls.clear()
        assert route._bound_tenant_id(tenant) == str(binding.platform_tenant_id)
        assert calls == [("auth0", tenant.subject)]
    else:
        with pytest.raises(platform_link.ProjectSessionForbidden):
            platform_link.resolve_caller_binding(tenant)
        assert calls == []
        assert route._bound_tenant_id(tenant) == str(tenant)
    # No keyword is passed to legacy three-positional-argument fixtures.
    assert platform_link.resolve_submission_context(None, None, None) is None
    drawings._headers(tenant, UUID(int=2), None, None)
    assert drawings._identity(tenant, UUID(int=2), UUID(int=3))["organization_id"] == str(tenant)
    response = drawings._dispatch(lambda: project._access(tenant, UUID(int=2), write=False))
    expected = {"auth0": 404, "guest": 403, "dispatch": 403, "auth_off": 403, "unmarked_demo": 400}
    assert response.status_code == expected[kind]
    # A non-demo submission still delegates to the same Bearer/platform dependency.
    write_calls = []
    platform_deps = SimpleNamespace(auth_live=lambda: kind != "auth_off",
        get_write_org_id=lambda **kwargs: write_calls.append(kwargs) or UUID(int=1))
    monkeypatch.setattr(platform_link, "_db_configured", lambda: True)
    monkeypatch.setattr(platform_link, "_load_platform", lambda: (store, None, platform_deps))
    monkeypatch.setattr(platform_link, "resolve_project_authority", lambda org, proj:
        dict(org_id=org, project_id=proj, authority_mode="postgres_canonical"))
    result = platform_link.resolve_submission_context(str(UUID(int=1)), str(UUID(int=2)), "Bearer original")
    assert result == dict(org_id=UUID(int=1), project_id=UUID(int=2), authority_mode="postgres_canonical")
    assert write_calls == ([] if kind == "auth_off" else [{"x_org_id": None, "authorization": "Bearer original"}])
    # The pre-existing authority requirement for account, anonymous and back-edge jobs.
    if kind in {"guest", "dispatch"}:
        with pytest.raises(ValueError):
            route._canonical_tenant_id(tenant, result)
    else:
        assert route._canonical_tenant_id(tenant, result) == str(UUID(int=1))
    assert route._access_error({"tenant_id": str(tenant)}, tenant, "legacy") is None


def test_project_and_header_scope_refuse_before_any_read(registry):
    s = registry
    drawings._headers(s.tenant, s.project, s.org, s.project)
    for proj, org, header in ((uuid4(), None, None), (s.project, uuid4(), None),
                              (s.project, None, uuid4())):
        response = drawings._dispatch(lambda: drawings._headers(s.tenant, proj, org, header))
        assert response.status_code == 403
    with pytest.raises(adapter.DemoActorRefused):
        platform_link.require_project_access(s.tenant, uuid4(), write=False)
    with pytest.raises(adapter.DemoActorRefused):
        platform_link.resolve_submission_context(str(uuid4()), str(s.project), tenant_context=s.tenant)


@pytest.mark.parametrize("field", ["tenant_id", "org_id", "project_id", "missing_scope"])
def test_every_job_record_requires_all_scope_even_without_provenance(registry, field):
    s = registry
    rec = dict(tenant_id=str(s.tenant), org_id=str(s.org), project_id=str(s.project))
    assert route._access_error(rec, s.tenant, "job") is None
    if field == "missing_scope": rec.pop("project_id")
    else: rec[field] = str(uuid4())
    response = route._access_error(rec, s.tenant, "job")
    assert response.status_code == 403
    assert json.loads(response.body)["error"]["reason_code"] == "demo_actor_refused"


@pytest.mark.parametrize("failure", ["revoked", "expired", "database"])
def test_list_validates_demo_before_any_job_read(registry, monkeypatch, failure):
    from unittest.mock import Mock
    s = registry
    assert adapter.resolve_demo_actor(s.tenant).binding_id == s.binding
    if failure == "database":
        monkeypatch.setattr(platform_link, "platform_db",
            Mock(side_effect=RuntimeError("private connection details")))
    else:
        # Both revoked and expired sessions are absent from the live-record query.
        s.session_live = False
    canonical = Mock(side_effect=AssertionError("canonical jobs must not be read"))
    legacy = Mock(side_effect=AssertionError("legacy jobs must not be read"))
    monkeypatch.setattr(platform_link, "list_canonical_jobs", canonical)
    monkeypatch.setattr(route.jobs, "list_jobs", legacy)
    response = route.list_jobs(tenant=s.tenant)
    assert response.status_code == (503 if failure == "database" else 403)
    if failure == "database":
        assert b"job store unavailable" in response.body
        assert b"demo_actor_refused" not in response.body
        assert b"private connection" not in response.body
    else:
        assert json.loads(response.body)["error"]["reason_code"] == "demo_actor_refused"
    canonical.assert_not_called()
    legacy.assert_not_called()


@pytest.mark.parametrize("store_mode", ["legacy", "postgres"])
def test_list_demo_resolves_once_and_filters_every_scope(registry, monkeypatch, store_mode):
    from unittest.mock import Mock
    s, foreign = registry, Registry()
    own = dict(job_id="own", tenant_id=str(s.tenant), org_id=str(s.org), project_id=str(s.project))
    other = dict(job_id="other", tenant_id=str(foreign.tenant), org_id=str(foreign.org),
        project_id=str(foreign.project))
    account = dict(own, job_id="auth0", tenant_id=str(uuid4()))
    wrong_org = dict(own, job_id="wrong-org", org_id=str(foreign.org))
    wrong_project = dict(own, job_id="wrong-project", project_id=str(foreign.project))
    missing = dict(own, job_id="missing")
    missing.pop("project_id")
    canonical = Mock(return_value=[own, other, wrong_org])
    legacy = Mock(return_value=[own, account, wrong_project, missing])
    resolver = Mock(wraps=adapter.resolve_demo_actor)
    monkeypatch.setattr(route, "resolve_demo_actor", resolver)
    monkeypatch.setattr(platform_link, "list_canonical_jobs", canonical)
    monkeypatch.setattr(route.jobs, "list_jobs", legacy)
    monkeypatch.setattr(route.jobs, "job_store_mode", lambda: store_mode)
    monkeypatch.setattr(route.jobs, "_pg_store", SimpleNamespace(
        linked_capability_job=lambda *args: None))
    result = route.list_jobs(limit=10, tenant=s.tenant)
    assert [rec["job_id"] for rec in result["jobs"]] == ["own"]
    resolver.assert_called_once_with(s.tenant)
    canonical.assert_called_once_with(str(s.tenant), limit=10, strict=True)
    legacy.assert_called_once_with(str(s.tenant), 10)


def test_list_auth0_keeps_binding_and_per_job_authorization(monkeypatch):
    from unittest.mock import Mock
    org = uuid4()
    tenant = deps.TenantContext(str(uuid4()), subject="auth0-list")
    binding = SimpleNamespace(platform_tenant_id=org)
    authority = Mock(return_value=binding)
    monkeypatch.setattr(deps, "auth_live", lambda: True)
    monkeypatch.setattr(platform_link, "platform_store", lambda: SimpleNamespace(
        resolve_active_identity_binding=authority))
    own = dict(job_id="own", tenant_id=str(org))
    denied = dict(job_id="denied", tenant_id=str(org))
    foreign = dict(job_id="foreign", tenant_id=str(uuid4()))
    canonical = Mock(return_value=[own, denied, foreign])
    legacy = Mock(return_value=[own])
    access = Mock(side_effect=lambda rec, *args: object() if rec is denied else None)
    demo_resolver = Mock(side_effect=AssertionError("Auth0 must not resolve a demo actor"))
    monkeypatch.setattr(route, "resolve_demo_actor", demo_resolver)
    monkeypatch.setattr(route, "_access_error", access)
    monkeypatch.setattr(platform_link, "list_canonical_jobs", canonical)
    monkeypatch.setattr(route.jobs, "list_jobs", legacy)
    monkeypatch.setattr(route.jobs, "job_store_mode", lambda: "legacy")
    result = route.list_jobs(limit=10, tenant=tenant)
    assert [rec["job_id"] for rec in result["jobs"]] == ["own"]
    canonical.assert_called_once_with(str(org), limit=10)
    legacy.assert_called_once_with(str(org), 10)
    authority.assert_called_with("auth0", "auth0-list")
    assert [call.args[0] for call in access.call_args_list] == [own, denied, own]
    demo_resolver.assert_not_called()


@pytest.mark.parametrize("failure", ["revoked", "expired", "switch"])
def test_stream_refresh_ends_with_refusal(registry, monkeypatch, failure):
    s = registry
    rec = dict(job_id="job", tenant_id=str(s.tenant), org_id=str(s.org), project_id=str(s.project),
        status="running", progress=0, elapsed_ms=0, error=None)
    monkeypatch.setattr(route, "_job_for_tenant", lambda *args: rec)

    async def observe():
        response = await route.stream_job("job", s.tenant)
        iterator = response.body_iterator
        first = await anext(iterator)
        assert '"status": "running"' in first
        if failure == "switch": monkeypatch.setenv("LEAF_DEMO_LIVE_ENABLED", "0")
        else: s.session_live = False
        final = await anext(iterator)
        assert "demo_actor_refused" in final and '"progress"' not in final
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)
    asyncio.run(observe())


def test_wait_loop_refresh_refuses_before_result(registry, monkeypatch):
    s = registry
    rec = dict(tenant_id=str(s.tenant), org_id=str(s.org), project_id=str(s.project), status="running")
    monkeypatch.setattr(route, "_job_for_tenant", lambda *args: rec)

    def expire(_seconds):
        s.session_live = False
        rec.update(status="complete", result={"private": "never disclose"})
    monkeypatch.setattr(route.time, "sleep", expire)
    from fastapi.responses import JSONResponse
    result = route._wait_project_solar(JSONResponse({"job_id": "job"}, status_code=202), s.tenant)
    assert result.status_code == 403 and b"never disclose" not in result.body


def test_database_failure_keeps_unavailable_envelopes(registry, monkeypatch):
    """The test-only entitlement override isolates the tenancy layer in admission."""
    import entitlements
    import solar_project_admission as admission
    import solar_tools
    def unavailable(*args, **kwargs):
        raise RuntimeError("database down")
    monkeypatch.setattr(platform_link, "platform_db", unavailable)
    s = registry
    response = drawings._dispatch(lambda: project._access(s.tenant, s.project, write=False))
    assert response.status_code == 503
    assert json.loads(response.body)["error"]["reason_code"] == "SIP_R1_STORE_UNAVAILABLE"
    response = drawings._dispatch(lambda: drawings._headers(s.tenant, s.project, None, None))
    assert response.status_code == 503
    assert route._access_error({}, s.tenant, "job").status_code == 503
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    original_entitlements = entitlements.entitlements_for
    monkeypatch.setattr(entitlements, "entitlements_for", lambda tier, *args:
        {"run_write": True, "run_read": True} if tier == "demo_live" else original_entitlements(tier, *args))
    tool = solar_tools.trusted_record("solar-settings")
    result = admission.project_tool_admission(tool, s.tenant, org_header=str(s.org),
        project_header=str(s.project), authorization=None, input_version_id=str(uuid4()),
        drawing_id=str(uuid4()), params={}, catalog_digest=deps.catalog_tool_digest(tool),
        idempotency_key="key", checkout_capability=None, preview=True)
    assert result["status_code"] == 503
    assert result["content"]["error"]["reason_code"] == "SIP_R1_STORE_UNAVAILABLE"


@pytest.mark.parametrize("transport", ["get", "stream", "wait", "stream_poll", "wait_poll"])
def test_job_lookup_failure_keeps_unavailable_envelope(registry, monkeypatch, transport):
    from fastapi.responses import JSONResponse
    s = registry
    record = dict(job_id="job", tenant_id=str(s.tenant), org_id=str(s.org),
        project_id=str(s.project), status="running", progress=0, elapsed_ms=0, error=None)
    failed = not transport.endswith("_poll")

    def lookup(*args):
        if failed:
            raise RuntimeError("database down: private connection details")
        return record

    def fail(_seconds):
        nonlocal failed
        failed = True

    monkeypatch.setattr(route, "_job_for_tenant", lookup)
    if transport.startswith("stream"):
        async def observe():
            nonlocal failed
            response = await route.stream_job("job", s.tenant)
            if transport == "stream_poll":
                iterator = response.body_iterator
                assert '"status": "running"' in await anext(iterator)
                failed = True
                final = await anext(iterator)
                assert "job store unavailable" in final and "private connection" not in final
                assert '"progress"' not in final and "demo_actor_refused" not in final
                with pytest.raises(StopAsyncIteration):
                    await anext(iterator)
                return
            assert response.status_code == 503
            assert b"demo_actor_refused" not in response.body
            assert b"private connection" not in response.body
        asyncio.run(observe())
    else:
        monkeypatch.setattr(route.time, "sleep", fail)
        response = (route.get_job("job", s.tenant) if transport == "get" else
            route._wait_project_solar(JSONResponse({"job_id": "job"}, status_code=202), s.tenant))
        assert response.status_code == 503
        assert b"demo_actor_refused" not in response.body
        assert b"private connection" not in response.body


@pytest.mark.parametrize("operation", ["project_access", "context", "checkout_get", "checkout_take",
                                        "checkout_release", "catalog_preview", "admission"])
def test_success_control_then_foreign_scope_matrix(registry, monkeypatch, operation):
    """Test-only entitlement overrides isolate the tenancy layer for both actors."""
    import entitlements
    import solar_project_jobs
    import solar_tools
    from routers import capabilities
    from test_sip_r1_context import Memory, O1, P1, D1, V2, A

    s = registry
    s.org, s.project, s.binding = O1, P1, A
    s.tenant = deps.TenantContext(str(s.tenant), org_id=str(O1), subject=s.tenant.subject,
        tier="demo_live", verified_demo=True)
    s.bindings[0].update(platform_tenant_id=O1, binding_id=A)
    s.projects[0]["project_id"] = P1
    memory = Memory()
    memory.original_store = project.graph_store()
    monkeypatch.setattr(project, "graph_store", lambda: memory)
    namespaces, submissions = [], []

    def backend(tenant):
        namespaces.append(str(tenant))
        assert str(tenant) == str(O1)
        return memory
    monkeypatch.setattr(project.write_loop, "upload_backend_for_tenant", backend)
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    monkeypatch.setenv("LEAF_CHECKOUT_CAP_SECRET", "unit-demo-actor-checkout")
    original_entitlements = entitlements.entitlements_for
    monkeypatch.setattr(entitlements, "entitlements_for", lambda tier, *args:
        {"run_write": True, "run_read": True} if tier == "demo_live" else original_entitlements(tier, *args))
    tools = solar_tools.trusted_snapshot()
    tool = tools["solar-settings"]
    monkeypatch.setattr(deps, "find_tool", lambda name, *args: tools.get(name))
    monkeypatch.setattr(deps, "effective_tools_with_provenance", lambda *args:
        [(tool, None) for tool in tools.values()])
    monkeypatch.setattr(deps, "load_engine_registry_tools", lambda: [])
    monkeypatch.setattr(capabilities.mcp_tool_projection, "projected_tools", lambda *args: [])
    monkeypatch.setattr(capabilities.customization_service, "effective_catalog_pin", lambda *args: None)

    def submit(tenant, project_id, drawing_id, version, *args, preview=False, **kwargs):
        project.verify_at_admission(tenant, project_id, drawing_id, version,
            checkout_capability=kwargs["checkout_capability"])
        submissions.append((str(tenant), project_id, preview))
        return {"admissible": True} if preview else {"job_id": "unit-job"}
    monkeypatch.setattr(solar_project_jobs, "submit_project_graph_job", submit)
    lease, token = project.acquire_project_checkout(s.tenant, P1, D1)
    req = route.RunRequest(tool="solar-settings", params={"drawing_id": str(D1)}, dwg=str(V2),
        catalog_digest=deps.catalog_tool_digest(tool))
    # The foreign actor has a valid provisioned identity in its own registry.
    foreign_state = Registry()
    foreign = foreign_state.tenant
    original = s.run_transaction
    monkeypatch.setattr(s, "run_transaction", lambda fn, **kw:
        original(fn, **kw) if not getattr(s, "foreign", False) else foreign_state.run_transaction(fn, **kw))

    def invoke(tenant):
        s.foreign = tenant is foreign
        if operation == "project_access":
            try:
                return platform_link.require_project_access(tenant, P1, write=True)
            except adapter.DemoActorRefused:
                return route._demo_refusal()
        if operation == "context":
            return drawings.context_route(P1, V2, D1, tenant, O1, P1)
        if operation == "checkout_get":
            return drawings.status_route(P1, D1, tenant, O1, P1)
        if operation == "checkout_take":
            return drawings.acquire_route(P1, D1, None, tenant, O1, P1, token)
        if operation == "checkout_release":
            return drawings.release_route(P1, D1, tenant, O1, P1, token)
        if operation == "admission":
            return route.run(req, wait=0, tenant_id=tenant, x_org_id=str(O1), x_project_id=str(P1),
                authorization=None, x_checkout_capability=token, idempotency_key="key")
        result = capabilities.capabilities(x_internal_role=None, x_ops_secret=None,
            tenant=tenant, project_id=str(P1), drawing_id=str(D1), input_version_id=str(V2),
            x_org_id=str(O1), x_project_id=str(P1), authorization=None, x_checkout_capability=token,
            project_runs=json.dumps({"solar-settings": {"params": req.params,
                "catalog_digest": req.catalog_digest, "idempotency_key": "preview"}}))
        if not isinstance(result, dict):
            return result
        return next(row["availability"] for family in result["families"]
                    for row in family["capabilities"] if row["name"] == "solar-settings")

    control = invoke(s.tenant)
    if operation == "project_access": assert control == str(O1)
    elif operation == "catalog_preview": assert control["runnable"] is True, control
    else: assert control.status_code in (200, 202), control.body
    before = deepcopy((memory.reads, memory.mutations, memory.checkout, memory.last_fence,
                       memory.blobs, submissions, namespaces))
    refused = invoke(foreign)
    assert refused.status_code == 403
    assert json.loads(refused.body)["error"]["reason_code"] == "demo_actor_refused"
    assert (memory.reads, memory.mutations, memory.checkout, memory.last_fence,
            memory.blobs, submissions, namespaces) == before
    assert all(tenant == str(s.tenant) for tenant, _proj, _preview in submissions)


# Infrastructure exceptions must retain their classification regardless of their
# Python base class. These doubles exercise the adapter, not a replacement for it.
from psycopg import OperationalError

ACTOR_ERRORS = [RuntimeError, ValueError, KeyError, TypeError, AttributeError, OperationalError]


def actor_outage(monkeypatch, error):
    def fail(*args, **kwargs):
        raise error("private database details")
    monkeypatch.setattr(platform_link, "platform_db", fail)


def assert_unavailable(response, message=None):
    assert response.status_code == 503
    body = json.loads(response.body)
    assert body["error"]["retryable"] is True
    if message:
        assert body["error"]["message"] == message
    assert b"private database details" not in response.body
    assert b"demo_actor_refused" not in response.body


@pytest.mark.parametrize("error", ACTOR_ERRORS)
@pytest.mark.parametrize("stage", ["acquire", "transaction"])
def test_actor_transaction_exception_matrix(registry, monkeypatch, error, stage):
    def fail(*args, **kwargs):
        raise error("private database details")
    if stage == "acquire":
        monkeypatch.setattr(platform_link, "platform_db", fail)
    else:
        monkeypatch.setattr(registry, "run_transaction", fail)
    with pytest.raises(adapter.DemoActorUnavailable) as caught:
        adapter.resolve_demo_actor(registry.tenant)
    assert isinstance(caught.value.__cause__, error)


@pytest.mark.parametrize("error", ACTOR_ERRORS)
@pytest.mark.parametrize("statement", range(5))
@pytest.mark.parametrize("stage", ["execute", "fetch"])
def test_actor_sql_exception_matrix(registry, monkeypatch, error, statement, stage):
    execute = registry.execute
    count = 0

    def sql(*args):
        nonlocal count
        index = count
        count += 1
        if index == statement:
            if stage == "execute":
                raise error("private database details")
            def fail():
                raise error("private database details")
            return SimpleNamespace(fetchone=fail, fetchall=fail)
        return execute(*args)
    monkeypatch.setattr(registry, "execute", sql)
    with pytest.raises(adapter.DemoActorUnavailable) as caught:
        adapter.resolve_demo_actor(registry.tenant)
    assert isinstance(caught.value.__cause__, error)


def admission_setup(registry, monkeypatch):
    import entitlements
    import solar_project_admission as admission
    import solar_tools
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    monkeypatch.setattr(entitlements, "entitlements_for",
                        lambda *args: {"run_read": True, "run_write": True})
    tool = solar_tools.trusted_record("solar-settings")
    def invoke():
        return admission.project_tool_admission(tool, registry.tenant,
            org_header=str(registry.org), project_header=str(registry.project),
            authorization=None, input_version_id=str(uuid4()), drawing_id=str(uuid4()),
            params={}, catalog_digest=deps.catalog_tool_digest(tool),
            idempotency_key="key", checkout_capability=None, preview=True)
    return admission, invoke


@pytest.mark.parametrize("error", ACTOR_ERRORS)
@pytest.mark.parametrize("consumer", ["access", "headers", "identity", "project_read",
    "admission_first", "admission_second", "get", "stream", "wait", "close"])
def test_actor_failure_classification_at_every_consumer(registry, monkeypatch, error, consumer):
    from fastapi.responses import JSONResponse
    s = registry
    admission, admit = admission_setup(s, monkeypatch)
    if consumer == "admission_second":
        monkeypatch.setattr(platform_link, "resolve_submission_context", lambda *a, **kw:
            dict(org_id=s.org, project_id=s.project, authority_mode="postgres_canonical"))
    monkeypatch.setattr(route.jobs, "get_job", lambda *a: pytest.fail("read before actor check"))
    monkeypatch.setattr(route.jobs, "mark_job_closed", lambda *a: pytest.fail("mutation after failure"))
    actor_outage(monkeypatch, error)
    if consumer in {"access", "headers", "identity"}:
        call = {"access": lambda: project._access(s.tenant, s.project, write=False),
                "headers": lambda: drawings._headers(s.tenant, s.project, None, None),
                "identity": lambda: drawings._identity(s.tenant, s.project, uuid4())}[consumer]
        response = drawings._dispatch(call)
    elif consumer.startswith("admission"):
        result = admit()
        response = JSONResponse(result["content"], status_code=result["status_code"])
    elif consumer == "project_read":
        monkeypatch.setattr(route, "_resolve_project_read_context",
                            lambda *a: adapter.resolve_demo_actor(s.tenant))
        response = route._run_project_read({}, s.tenant, (), None, {})
    elif consumer == "get":
        response = route.get_job("job", s.tenant)
    elif consumer == "stream":
        response = asyncio.run(route.stream_job("job", s.tenant))
    elif consumer == "close":
        response = route.close_job("job", s.tenant)
    else:
        response = route._wait_project_solar(JSONResponse({"job_id": "job"}, status_code=202), s.tenant)
    assert_unavailable(response)


def canonical_run_setup(registry, monkeypatch, input_version=None):
    import entitlements
    import solar_authored_graph
    tool = {"name": "actor-test-solver", "canonical_only": True, "default_params": {}}
    monkeypatch.setattr(deps, "backedge_run_identity", lambda tenant, *a: tenant)
    monkeypatch.setattr(deps, "find_tool", lambda *a: tool)
    monkeypatch.setattr(deps, "effective_tools_with_provenance", lambda *a: [(tool, None)])
    monkeypatch.setattr(entitlements, "entitlements_for", lambda *a: {"run_read": True})
    monkeypatch.setattr(entitlements, "tool_required_capability", lambda *a: "run_read")
    monkeypatch.setattr(entitlements, "w1_tool_availability", lambda *a, **kw: None)
    monkeypatch.setattr(solar_authored_graph, "reads_graph", lambda *a: False)
    monkeypatch.setattr(route, "_checkout_identity", lambda *a: ("anonymous", None))
    monkeypatch.setattr(route, "_is_project_read_tool", lambda *a: False)
    context = dict(org_id=registry.org, project_id=registry.project,
                   authority_mode="postgres_canonical")
    request = route.RunRequest(tool=tool["name"], dwg=str(input_version or uuid4()),
                               catalog_digest=deps.catalog_tool_digest(tool))
    def invoke(wait=0):
        return route.run(request, wait=wait, tenant_id=registry.tenant,
            x_org_id=str(registry.org), x_project_id=str(registry.project),
            authorization=None, x_checkout_capability=None, idempotency_key="actor-test")
    return context, invoke


@pytest.mark.parametrize("stage", ["submission", "canonical_tenant"])
@pytest.mark.parametrize("error", ACTOR_ERRORS)
def test_canonical_run_actor_failure_is_retryable_503(registry, monkeypatch, stage, error):
    context, invoke = canonical_run_setup(registry, monkeypatch)
    calls = []
    submission_context = platform_link.resolve_submission_context
    if stage == "canonical_tenant":
        def first(*args, **kwargs):
            calls.append("submission")
            result = submission_context(*args, **kwargs)
            assert result == context
            actor_outage(monkeypatch, error)
            return result
        monkeypatch.setattr(platform_link, "resolve_submission_context", first)
    monkeypatch.setattr(platform_link, "submit_canonical_solve",
                        lambda *a, **kw: pytest.fail("submission after actor failure"))
    monkeypatch.setattr(route.jobs, "submit_job", lambda *a, **kw: pytest.fail("legacy fallback"))
    monkeypatch.setattr(route, "_active_binding_tenant", lambda *a: pytest.fail("Auth0 fallback"))
    if stage == "submission":
        actor_outage(monkeypatch, error)
    assert_unavailable(invoke(), "job store unavailable")
    assert calls == (["submission"] if stage == "canonical_tenant" else [])


@pytest.mark.parametrize("stage", ["submission", "canonical_tenant"])
@pytest.mark.parametrize("error", ACTOR_ERRORS)
def test_platform_capabilities_actor_failure_is_retryable_503(registry, monkeypatch, stage, error):
    s = registry
    calls = []
    monkeypatch.setattr(route, "_active_binding_tenant", lambda *a: pytest.fail("Auth0 fallback"))
    monkeypatch.setattr(platform_link, "canonical_worker_health",
                        lambda *a: pytest.fail("capability projection after actor failure"))
    submission_context = platform_link.resolve_submission_context
    if stage == "canonical_tenant":
        def first(*args, **kwargs):
            calls.append("submission")
            result = submission_context(*args, **kwargs)
            assert result["org_id"] == s.org and result["project_id"] == s.project
            actor_outage(monkeypatch, error)
            return result
        monkeypatch.setattr(platform_link, "resolve_submission_context", first)
    if stage == "submission":
        actor_outage(monkeypatch, error)
    response = route.platform_capabilities(s.tenant, str(s.org), str(s.project), None)
    assert_unavailable(response, "job store unavailable")
    assert calls == (["submission"] if stage == "canonical_tenant" else [])


def own_job(s):
    return dict(job_id=str(uuid4()), tenant_id=str(s.tenant), org_id=str(s.org),
                project_id=str(s.project), status="running", progress=0, elapsed_ms=0, error=None)


@pytest.mark.parametrize("error", ACTOR_ERRORS)
def test_stream_actor_failure_after_first_event_ends_with_unavailable(registry, monkeypatch, error):
    record = own_job(registry)
    monkeypatch.setattr(route, "_job_for_tenant", lambda *a: record)
    async def observe():
        response = await route.stream_job(record["job_id"], registry.tenant)
        iterator = response.body_iterator
        assert '"status": "running"' in await anext(iterator)
        actor_outage(monkeypatch, error)
        final = await anext(iterator)
        body = json.loads(final.removeprefix("data: "))
        assert body["error"]["message"] == "job store unavailable"
        assert body["error"]["retryable"] is True
        assert "progress" not in body and "private database details" not in final
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)
    asyncio.run(observe())


@pytest.mark.parametrize("error", ACTOR_ERRORS)
def test_project_wait_actor_failure_before_terminal_result(registry, monkeypatch, error):
    from fastapi.responses import JSONResponse
    record = own_job(registry)
    monkeypatch.setattr(route, "_job_for_tenant", lambda *a: record)
    def fail(_seconds):
        record.update(status="complete", result={"private": "never disclose"})
        actor_outage(monkeypatch, error)
    monkeypatch.setattr(route.time, "sleep", fail)
    response = route._wait_project_solar(JSONResponse({"job_id": record["job_id"]},
                                         status_code=202), registry.tenant)
    assert_unavailable(response, "job store unavailable")
    assert b"never disclose" not in response.body


@pytest.mark.parametrize("error", ACTOR_ERRORS)
def test_canonical_run_wait_actor_failure_before_terminal_result(registry, monkeypatch, error):
    context, invoke = canonical_run_setup(registry, monkeypatch)
    record = own_job(registry)
    monkeypatch.setattr(platform_link, "resolve_submission_context", lambda *a, **kw: context)
    monkeypatch.setattr(platform_link, "submit_canonical_solve", lambda *a, **kw: record["job_id"])
    monkeypatch.setattr(route, "_job_for_tenant", lambda *a: record)
    def fail(_seconds):
        record.update(status="complete", result={"private": "never disclose"})
        actor_outage(monkeypatch, error)
    monkeypatch.setattr(route.time, "sleep", fail)
    response = invoke(wait=1)
    assert_unavailable(response, "job store unavailable")
    assert b"never disclose" not in response.body


@pytest.mark.parametrize("failure", ["revoked", "expired", "disabled", "revoked_binding"])
def test_close_validates_demo_before_lookup(registry, monkeypatch, failure):
    if failure in {"revoked", "expired"}:
        registry.session_live = False
    else:
        corrupt(registry, "switch_off" if failure == "disabled" else failure, monkeypatch)
    monkeypatch.setattr(route.jobs, "get_job", lambda *a: pytest.fail("read before refusal"))
    monkeypatch.setattr(route.jobs, "mark_job_closed", lambda *a: pytest.fail("mutation after refusal"))
    response = route.close_job("unknown", registry.tenant)
    assert response.status_code == 403
    assert b"demo_actor_refused" in response.body


@pytest.fixture
def legacy_close_store(monkeypatch):
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(route.jobs._SCHEMA)
    monkeypatch.setattr(route.jobs, "_db", lambda: conn)
    monkeypatch.setattr(route.jobs, "job_store_mode", lambda: "legacy")
    try:
        yield conn
    finally:
        conn.close()


@pytest.mark.parametrize("kind", ["demo_canonical", "demo_legacy", "auth0_canonical"])
def test_close_only_mutates_legacy_jobs(registry, monkeypatch, legacy_close_store, kind):
    record = own_job(registry)
    tenant = registry.tenant
    if kind == "auth0_canonical":
        tenant = deps.TenantContext(str(registry.org), subject="auth0|account")
        record["tenant_id"] = str(tenant)
    canonical = deepcopy(record)
    canonical_reads = []
    def lookup(*args, **kwargs):
        canonical_reads.append(args)
        return deepcopy(canonical)

    # Exercise the actual get/mark/read path against an isolated legacy store.
    conn = legacy_close_store
    statements = []
    conn.set_trace_callback(statements.append)
    monkeypatch.setattr(platform_link, "get_canonical_job", lookup)
    if kind == "demo_legacy":
        conn.execute("INSERT INTO jobs (job_id, tenant_id, tool, params_json, dwg, status, "
            "created_at, updated_at, org_id, project_id) VALUES (?, ?, ?, '{}', '', 'running', 1, 1, ?, ?)",
            (record["job_id"], str(tenant), "test", str(registry.org), str(registry.project)))
        conn.commit()
    response = route.close_job(record["job_id"], tenant)
    if kind == "demo_legacy":
        assert response["closed"] is True
        assert route.jobs.get_job(record["job_id"])["progress"] == route.jobs.CLOSED_PROGRESS
        assert canonical_reads == []
    else:
        assert response.status_code == 404
        error = json.loads(response.body)["error"]
        assert error["message"] == f"unknown job_id: {record['job_id']}"
        assert route.jobs.get_job(record["job_id"]) is None
        assert bool(canonical_reads) == (kind == "demo_canonical")
        assert not any(sql.startswith("UPDATE") for sql in statements)
    assert canonical == record


@pytest.mark.parametrize("case", ["viewer", "foreign", "unknown"])
def test_close_demo_requires_write_role(registry, monkeypatch, case):
    record = own_job(registry)
    if case == "viewer":
        registry.members[0]["role"] = "read_only"
    if case == "foreign":
        record["org_id"] = str(uuid4())
    monkeypatch.setattr(route, "_job_for_tenant", lambda *a: None if case == "unknown" else record)
    monkeypatch.setattr(route.jobs, "mark_job_closed", lambda *a: pytest.fail("mutation after denial"))
    response = route.close_job(record["job_id"], registry.tenant)
    assert response.status_code == (404 if case == "unknown" else 403)
    if case == "unknown":
        assert json.loads(response.body)["error"]["message"] == f"unknown job_id: {record['job_id']}"


@pytest.mark.parametrize("error", ACTOR_ERRORS)
@pytest.mark.parametrize("stage", ["lookup", "reread", "mutation", "after"])
def test_close_demo_failure_is_503(registry, monkeypatch, error, stage):
    record = own_job(registry)
    reads, writes = [], []
    def read(*args):
        reads.append(args)
        if stage == "reread" or (stage == "after" and len(reads) == 2):
            raise error("private database details")
        return record
    def mark(*args):
        writes.append(args)
        if stage == "mutation":
            raise error("private database details")
        return True
    def lookup(*args):
        if stage == "lookup":
            raise error("private database details")
        return record
    monkeypatch.setattr(route, "_job_for_tenant", lookup)
    monkeypatch.setattr(route.jobs, "get_job", read)
    monkeypatch.setattr(route.jobs, "mark_job_closed", mark)
    assert_unavailable(route.close_job(record["job_id"], registry.tenant), "job store unavailable")
    if stage in {"lookup", "reread"}:
        assert writes == []


@pytest.mark.parametrize("error", ACTOR_ERRORS)
def test_demo_canonical_lookup_does_not_swallow_store_failure(registry, monkeypatch, error):
    def fail(*a, **kw):
        raise error("private database details")
    monkeypatch.setattr(route.jobs, "get_job", lambda *a: None)
    monkeypatch.setattr(route.jobs, "job_store_mode", lambda: "legacy")
    monkeypatch.setattr(platform_link, "_canonical_jobs_module",
                        lambda: SimpleNamespace(get_job_for_tenant=fail))
    assert_unavailable(route.get_job(str(uuid4()), registry.tenant), "job store unavailable")
    if error in {RuntimeError, ValueError}:
        assert platform_link.get_canonical_job(str(uuid4()), "auth0-tenant") is None
    assert platform_link.get_canonical_job("invalid", str(registry.tenant), strict=True) is None


@pytest.mark.parametrize("error", ACTOR_ERRORS)
@pytest.mark.parametrize("operation", ["get", "list"])
def test_canonical_helpers_preserve_non_demo_failure_behavior(monkeypatch, error, operation):
    def fail(*a, **kw):
        raise error("private database details")
    monkeypatch.setattr(platform_link, "_canonical_jobs_module", lambda: SimpleNamespace(
        get_job_for_tenant=fail, list_jobs_for_tenant=fail))
    def invoke(**kwargs):
        if operation == "get":
            return platform_link.get_canonical_job(str(uuid4()), "auth0-tenant", **kwargs)
        return platform_link.list_canonical_jobs("auth0-tenant", **kwargs)
    swallowed = (RuntimeError, ValueError) if operation == "get" else (RuntimeError,)
    if error in swallowed:
        assert invoke() == (None if operation == "get" else [])
    else:
        with pytest.raises(error):
            invoke()
    # Strict mode must retain every store failure, including the exceptions
    # deliberately swallowed by the legacy default above.
    with pytest.raises(error):
        invoke(strict=True)


@pytest.mark.parametrize("error", ACTOR_ERRORS)
@pytest.mark.parametrize("store", ["canonical", "legacy"])
def test_demo_list_store_failure_is_503(registry, monkeypatch, error, store):
    def fail(*a, **kw):
        raise error("private database details")
    monkeypatch.setattr(platform_link, "_canonical_jobs_module",
        lambda: SimpleNamespace(list_jobs_for_tenant=fail if store == "canonical" else lambda *a, **kw: []))
    monkeypatch.setattr(route.jobs, "list_jobs", fail if store == "legacy" else lambda *a: [])
    assert_unavailable(route.list_jobs(tenant=registry.tenant), "job store unavailable")
    if error is RuntimeError and store == "canonical":
        assert platform_link.list_canonical_jobs("auth0-tenant") == []


@pytest.mark.parametrize("error", ACTOR_ERRORS)
def test_demo_list_link_hydration_failure_is_503(registry, monkeypatch, error):
    record = own_job(registry)
    monkeypatch.setattr(platform_link, "list_canonical_jobs", lambda *a, **kw: [record])
    monkeypatch.setattr(route.jobs, "list_jobs", lambda *a: [])
    monkeypatch.setattr(route.jobs, "job_store_mode", lambda: "postgres")
    def fail(*a):
        raise error("private database details")
    monkeypatch.setattr(route.jobs, "_pg_store", SimpleNamespace(linked_capability_job=fail))
    assert_unavailable(route.list_jobs(tenant=registry.tenant), "job store unavailable")


@pytest.mark.parametrize("stage", ["authorization", "principal"])
@pytest.mark.parametrize("failure", [adapter.DemoActorRefused, adapter.DemoActorUnavailable])
def test_campaign_execute_demo_actor_transport(registry, monkeypatch, stage, failure):
    from routers import campaigns
    store = SimpleNamespace()
    monkeypatch.setattr(campaigns, "_store", lambda: store)
    def fail(*a, **kw):
        raise failure()
    if stage == "authorization":
        monkeypatch.setattr(platform_link, "require_project_access", fail)
    else:
        monkeypatch.setattr(platform_link, "resolve_caller_binding", fail)
    def operation(store, org):
        campaigns._principal(registry.tenant)
        pytest.fail("operation after failure")
    response = campaigns._execute(registry.tenant, registry.project, operation, "campaign")
    assert response.status_code == (403 if failure is adapter.DemoActorRefused else 503)
    body = json.loads(response.body)
    assert body["error"]["retryable"] is (failure is adapter.DemoActorUnavailable)
    if failure is adapter.DemoActorRefused:
        assert body["error"]["reason_code"] == "demo_actor_refused"


def catalog_preview_setup(registry, monkeypatch):
    import entitlements
    import solar_tools
    from routers import capabilities
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    tool = solar_tools.trusted_record("solar-settings")
    monkeypatch.setattr(deps, "effective_tools_with_provenance", lambda *a: [(tool, None)])
    monkeypatch.setattr(deps, "load_engine_registry_tools", lambda: [])
    monkeypatch.setattr(capabilities.mcp_tool_projection, "projected_tools", lambda *a: [])
    monkeypatch.setattr(capabilities.customization_service, "effective_catalog_pin", lambda *a: None)
    monkeypatch.setattr(entitlements, "entitlements_for",
                        lambda *a: {"run_read": True, "run_write": True})
    def invoke(tenant):
        return capabilities.capabilities(x_internal_role=None, x_ops_secret=None,
            tenant=tenant, project_id=str(registry.project), drawing_id=str(uuid4()),
            input_version_id=str(uuid4()), x_org_id=str(registry.org),
            x_project_id=str(registry.project), authorization=None,
            x_checkout_capability=None, project_runs=json.dumps({"solar-settings": {
                "params": {}, "catalog_digest": deps.catalog_tool_digest(tool), "idempotency_key": "preview"}}))
    return invoke


def test_catalog_demo_actor_refusal_is_http_403(registry, monkeypatch):
    invoke = catalog_preview_setup(registry, monkeypatch)
    registry.session_live = False
    response = invoke(registry.tenant)
    assert response.status_code == 403
    assert json.loads(response.body)["error"]["reason_code"] == "demo_actor_refused"


@pytest.mark.parametrize("error", ACTOR_ERRORS)
def test_catalog_demo_actor_unavailable_is_http_503(registry, monkeypatch, error):
    invoke = catalog_preview_setup(registry, monkeypatch)
    actor_outage(monkeypatch, error)
    assert_unavailable(invoke(registry.tenant))


@pytest.mark.parametrize("status,reason", [(403, "demo_actor_refused"), (503, "SIP_R1_STORE_UNAVAILABLE")])
def test_catalog_non_demo_preview_keeps_nested_admission(registry, monkeypatch, status, reason):
    from fastapi.responses import JSONResponse
    invoke = catalog_preview_setup(registry, monkeypatch)
    monkeypatch.setattr(route, "_project_solar_admission", lambda *a, **kw:
        JSONResponse({"error": {"reason_code": reason}}, status_code=status))
    response = invoke(deps.TenantContext(str(uuid4()), subject="auth0|control"))
    assert isinstance(response, dict)
    state = next(row["availability"] for family in response["families"]
                 for row in family["capabilities"] if row["name"] == "solar-settings")
    assert state["admission"]["status_code"] == status
    assert state["admission"]["error"]["reason_code"] == reason


@pytest.mark.parametrize("error", ACTOR_ERRORS)
def test_actor_row_conversion_exception_is_unavailable(registry, monkeypatch, error):
    from leaf_platform.models import IdentityBinding
    def fail(*a, **kw):
        raise error("private database details")
    monkeypatch.setattr(IdentityBinding, "from_row", fail)
    with pytest.raises(adapter.DemoActorUnavailable) as caught:
        adapter.resolve_demo_actor(registry.tenant)
    assert isinstance(caught.value.__cause__, error)


@pytest.mark.parametrize("error", ACTOR_ERRORS)
@pytest.mark.parametrize("stage", ["package", "model_import"])
def test_actor_preparation_failure_is_unavailable(registry, monkeypatch, error, stage):
    import builtins
    import conversations

    _context, run = canonical_run_setup(registry, monkeypatch)
    monkeypatch.setenv("LEAF_CONV_DURABLE_ENABLED", "1")
    cause = error("private database details")
    original_import = builtins.__import__
    def fail(*a, **kw):
        raise cause
    def import_model(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "leaf_platform.models" and "IdentityBinding" in (fromlist or ()):
            raise cause
        return original_import(name, globals, locals, fromlist, level)
    if stage == "package":
        monkeypatch.setattr(platform_link, "_ensure_platform_package", fail)
    else:
        monkeypatch.setattr(builtins, "__import__", import_model)
    with pytest.raises(adapter.DemoActorUnavailable) as caught:
        adapter.resolve_demo_actor(registry.tenant)
    assert caught.value.__cause__ is cause
    assert_unavailable(run(), "job store unavailable")
    assert_unavailable(route.platform_capabilities(registry.tenant,
        str(registry.org), str(registry.project), None), "job store unavailable")
    assert_unavailable(conversations.api_list_conversations(str(registry.project), registry.tenant),
                       "conversation store unavailable")
    assert registry.statements == []


@pytest.mark.parametrize("error", ACTOR_ERRORS)
@pytest.mark.parametrize("store", ["legacy", "linked"])
def test_demo_observation_store_read_failure_is_503(registry, monkeypatch, error, store):
    def fail(*a, **kw):
        raise error("private database details")
    monkeypatch.setattr(route.jobs, "get_job", fail if store == "legacy" else lambda *a: None)
    monkeypatch.setattr(route.jobs, "job_store_mode", lambda: "postgres")
    monkeypatch.setattr(route.jobs, "_pg_store", SimpleNamespace(linked_capability_job=fail))
    monkeypatch.setattr(platform_link, "get_canonical_job",
                        lambda *a, **kw: pytest.fail("fallback after failed store read"))
    assert_unavailable(route.get_job(str(uuid4()), registry.tenant), "job store unavailable")
