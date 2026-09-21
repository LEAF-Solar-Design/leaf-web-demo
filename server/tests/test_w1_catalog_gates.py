"""W1 catalog admission uses persisted producer state, with no network."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import catalog
import deps
import entitlements
import product_capability_availability as availability
import write_loop
from test_w1_design_graph import graph  # noqa: F401
from test_w1_equipment import case, equipment, licensed  # noqa: F401
from test_w1_graph_versions import drawing, commit, request_for, TENANT, DRAWING  # noqa: F401
from solar_design_graph import deserialize_graph, serialize_graph
from solar_wiring_client import local_routes


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    import requests

    def refuse(*args, **kwargs):
        pytest.fail("catalog gates must not use the network")

    monkeypatch.setattr(requests.sessions.Session, "request", refuse)
    monkeypatch.setattr(deps, "load_tenant_repo_tools", lambda tenant: [])
    monkeypatch.setattr(deps, "_AUTHORED", [])
    monkeypatch.delenv("LEAF_ENTITLEMENTS_FILE", raising=False)


def rows(tenant=TENANT, **context):
    families = catalog.build_catalog(deps.all_tools(TENANT))
    if "drawing_version" in context:
        context["version"] = context.pop("drawing_version")
    families = availability.annotate_w1_availability(families, tenant, **context)
    return {row["name"]: row for family in families
        for row in family["capabilities"]}


def test_all_nine_resolve_once_through_normal_catalog():
    tools = deps.all_tools(TENANT)
    families = catalog.build_catalog(tools)
    families = availability.annotate_w1_availability(families, TENANT)
    flattened = [row for family in families for row in family["capabilities"]]
    for name in availability.W1_CAPABILITIES:
        matches = [row for row in flattened if row["name"] == name]
        assert len(matches) == 1
        assert deps.find_tool(name, TENANT)["entry"] == "builtins/" + name.replace("-", "_") + ".py"
        state = matches[0]["availability"]
        assert state["implemented"] is True
        assert state["entitled"] is True
        assert state["engine_ready"] is (name == "solar-solve-proposal")
        if name == "solar-solve-proposal":
            assert state["input_ready"] is True
            assert state["input_reason"] is None
            assert state["runnable"] is True
            assert state["refusal_reasons"] == []
        else:
            assert state["input_ready"] is False
            assert "drawing_context_required" in state["refusal_reasons"]
    assert len(flattened) == sum(len(f["capabilities"]) for f in families)
    assert len(flattened) == len({row["name"] for row in flattened})


def test_no_new_entitlement_grant():
    starter = SimpleNamespace(tier="hosted_starter")
    restricted = SimpleNamespace(tier="restricted")
    assert rows(starter)["solar-solve-proposal"]["availability"]["entitled"] is False
    assert rows(starter)["solar-settings"]["availability"]["entitled"] is True
    assert all(not rows(restricted)[name]["availability"]["entitled"]
               for name in availability.W1_CAPABILITIES)
    assert entitlements.tool_required_capability({"name": "solar-settings"}) == "run_write"


def test_completed_flags_and_confirmation_boolean_are_not_evidence(graph):
    graph["extra"]["completed_steps"] = list(availability.W1_CAPABILITIES)
    result = availability.w1_graph_readiness(graph)
    assert result["solar-size-strings"]["input_ready"] is True
    assert result["solar-panel-groups"] == {
        "input_ready": False, "input_reason": "sizing_confirmation_required"}
    assert result["solar-schedule"]["input_ready"] is False


def test_producer_state_survives_reopen_and_stale_inputs_disable(case):
    graph, params, intake = case
    result = availability.w1_graph_readiness(graph)
    assert result["solar-panel-groups"]["input_ready"]
    assert result["solar-solve-proposal"]["input_ready"]
    assert result["solar-assign-equipment"]["input_ready"]
    assert not result["solar-homeruns"]["input_ready"]
    assigned = equipment.assign_equipment(
        graph, params, drawing_intake=intake, licensed_equipment=licensed)["graph"]
    assert availability.w1_graph_readiness(assigned)["solar-homeruns"]["input_ready"]
    assigned["routes"] = local_routes(assigned)
    reopened = deserialize_graph(serialize_graph(assigned))
    assert availability.w1_graph_readiness(reopened)["solar-schedule"]["input_ready"]
    reopened["routes"].pop()
    assert not availability.w1_graph_readiness(reopened)["solar-schedule"]["input_ready"]
    assigned["strings"][0]["validity"] = {"state": "stale", "reasons": ["upstream_corrected"]}
    result = availability.w1_graph_readiness(assigned)
    assert not result["solar-assign-equipment"]["input_ready"]
    assert result["solar-correct-string"]["input_ready"]
    graph["panels"][0]["centre"][0] += 1
    assert not availability.w1_graph_readiness(graph)["solar-panel-groups"]["input_ready"]


def test_persisted_bundle_is_tenant_drawing_and_version_scoped(drawing, case, monkeypatch):
    graph, _, _ = case
    backend, _ = drawing
    request = request_for(backend, graph)
    commit(drawing, request)
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    context = {"drawing_id": DRAWING, "project_id": graph["project"]["id"]}
    row = rows(**context)["solar-solve-proposal"]
    assert row["availability"]["runnable"] is True
    assert not rows(**context)["solar-settings"]["availability"]["engine_ready"]
    for tenant, drawing_id, project, version in (
        ("another-tenant", DRAWING, context["project_id"], "head"),
        (TENANT, "another-drawing", context["project_id"], "head"),
        (TENANT, DRAWING, "wrong-project", "head"),
        (TENANT, DRAWING, context["project_id"], 1),
    ):
        state = availability.w1_input_readiness(tenant, drawing_id, project_id=project, version=version)
        assert not any(item["input_ready"] for item in state.values())


@pytest.mark.parametrize("drawing_id,version", [("../other", "head"), ([], "head"),
                                               (DRAWING, True), (DRAWING, "0")])
def test_malformed_context_refuses_before_storage(monkeypatch, drawing_id, version):
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: pytest.fail("invalid context read"))
    result = availability.w1_input_readiness(TENANT, drawing_id, version=version)
    assert not any(item["input_ready"] for item in result.values())


@pytest.mark.parametrize("name", [
    name for name, record in availability.W1_CAPABILITIES.items()
    if record["requires_persisted_graph"]
])
def test_api_run_refuses_unavailable_capability_before_submission(monkeypatch, name):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import jobs as route

    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[deps.require_tenant] = lambda: TENANT
    monkeypatch.setattr(deps, "backedge_run_identity", lambda tenant, *a: tenant)
    monkeypatch.setattr(deps, "effective_tools_with_provenance", lambda tenant: [])
    monkeypatch.setattr(route.jobs, "submit_job", lambda *a, **k: pytest.fail("unavailable tool submitted"))
    tool = deps.find_tool(name, TENANT)
    response = TestClient(app).post("/api/run", json={
        "tool": tool["name"], "dwg": DRAWING, "params": {"expected_rev": 0},
        "catalog_digest": deps.catalog_tool_digest(tool),
    })
    assert response.status_code == 409
    assert response.json()["reason_code"] == "broker_adapter_unavailable"
    assert response.json()["availability"]["input_ready"] is False
    assert response.json()["availability"]["input_reason"] == "persisted_graph_unavailable"
    assert "persisted_graph_unavailable" in response.json()["availability"]["refusal_reasons"]
    assert response.json()["availability"]["implemented"] is True


def test_capabilities_route_passes_authenticated_context(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import capabilities as route

    app = FastAPI()
    app.include_router(route.router)
    tenant = deps.TenantContext(TENANT, tier="restricted")
    app.dependency_overrides[deps.require_tenant] = lambda: tenant
    monkeypatch.setattr(deps, "effective_tools_with_provenance", lambda tenant: [])
    monkeypatch.setattr(route.customization_service, "effective_catalog_pin", lambda tenant: None)
    captured = {}
    build_calls = []

    def build(tools, *, include_internal):
        build_calls.append((tools, include_internal))
        return []

    def annotate(families, tenant, drawing_id, *, project_id, version):
        captured.update(tenant=tenant, drawing_id=drawing_id,
                        project_id=project_id, version=version)
        return families

    monkeypatch.setattr(catalog, "build_catalog", build)
    monkeypatch.setattr(route.availability, "annotate_w1_availability", annotate)
    response = TestClient(app).get("/api/capabilities", params={
        "drawing_id": DRAWING, "project_id": "project-1", "drawing_version": "2"})
    assert response.status_code == 200
    assert build_calls == [([], False)]
    assert captured["tenant"] is tenant
    assert captured["drawing_id"] == DRAWING
    assert captured["project_id"] == "project-1"
    assert captured["version"] == "2"


def test_adapter_table_is_the_single_engine_ready_source(monkeypatch, drawing, case):
    graph, _, _ = case
    backend, _ = drawing
    commit(drawing, request_for(backend, graph))
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    monkeypatch.setitem(availability.W1_CAPABILITIES["solar-settings"], "adapter", "test-adapter")
    state = rows(drawing_id=DRAWING, project_id=graph["project"]["id"])["solar-settings"]["availability"]
    assert state["input_ready"] is True
    assert state["engine_ready"] is True
    assert "broker_adapter_unavailable" not in state["refusal_reasons"]
    assert availability.is_cloud_proposal({"name": "solar-settings"}) is False
    assert availability.is_cloud_proposal({"name": "solar-solve-proposal"}) is True
    assert availability.is_cloud_proposal({"name": "no-such-tool"}) is False
    assert availability.capability_adapter(None) is None
    assert all("adapter" in row for row in availability.W1_CAPABILITIES.values())
    with pytest.raises(TypeError):
        availability.is_cloud_proposal(["solar-solve-proposal"])


def test_malformed_tool_record_fails_terminal_validation():
    import jobs

    for tool in (["solar-solve-proposal"], "other"):
        with pytest.raises(TypeError):
            jobs._validate_terminal_context(
                "complete", {"ok": True, "result": {}},
                {"attempt": 1, "execution_path": "local"}, 1,
                {"tool": tool, "aps_live": False})
    jobs._validate_terminal_context(
        "complete", {"ok": True, "result": {}},
        {"attempt": 1, "execution_path": "local"}, 1,
        {"tool": {"name": "other"}, "aps_live": False})


def test_no_routing_literal_outside_the_table():
    # A routing literal outside the table is the defect this slice removes.
    # This row stops it coming back.
    call_sites = 0
    for relative in ("routers/jobs.py", "jobs.py", "broker_client.py", "broker.py"):
        source = (SERVER / relative).read_text(encoding="utf-8")
        assert "solar-solve-proposal" not in source, relative
        call_sites += sum(line.count("is_cloud_proposal(")
                          for line in source.splitlines()
                          if not line.lstrip().startswith("from "))
    assert call_sites == 12
    source = (SERVER / "product_capability_availability.py").read_text(encoding="utf-8")
    for comparison in ('== "solar-solve-proposal"', '!= "solar-solve-proposal"',
                       'is "solar-solve-proposal"'):
        assert comparison not in source, "product_capability_availability.py"
