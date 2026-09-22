"""Offline W1 sizing/group transaction checks using explicitly synthetic data."""
from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import solar_sizing_client as cloud
from leaf_cloud_grants import CloudError, CloudGrant
from mutation_plan import plan_sha256
from solar_design_graph import GraphValidationError, deserialize_graph, serialize_graph
from test_w1_design_graph import app_id, graph  # noqa: F401


def builtin(name):
    spec = importlib.util.spec_from_file_location(name, SERVER / "builtins" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


settings = builtin("solar_settings")
sizing = builtin("solar_size_strings")
groups = builtin("solar_panel_groups")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("offline tests must not use the network")
    monkeypatch.setattr(cloud.requests.sessions.Session, "request", refuse)


@pytest.fixture
def recorded():
    return json.loads((SERVER / "tests/fixtures/w1_string_length_recorded_response.json").read_text())


@pytest.fixture
def service(monkeypatch, recorded):
    calls = []

    def grant(reference, tenant):
        assert (reference, tenant) == ("fixture-grant", "fixture-tenant")
        return CloudGrant(tenant, "")

    def post(request, grant):
        calls.append(request.model_dump())
        return cloud.canonical_bytes(recorded["response"])

    monkeypatch.setattr(cloud, "resolve_grant", grant)
    monkeypatch.setattr(cloud, "post_string_length", post)
    return calls


def sizing_params(graph, recorded, mode="global", **overrides):
    ids = [graph["settings"]["id"]] if mode == "global" else [z["id"] for z in graph["electrical_zones"]]
    return {"expected_rev": graph["rev"], "mode": mode,
            "requests": {ref: copy.deepcopy(recorded["request"]) for ref in ids},
            "grant_ref": "fixture-grant", "confirm": True, **overrides}


def confirm(graph, params):
    return sizing.size_strings(graph, params, tenant_id="fixture-tenant", job_id="fixture-job")


def test_global_confirmation_and_reopen(graph, recorded, service):
    before = copy.deepcopy(graph)
    result = confirm(graph, sizing_params(graph, recorded))
    restored = deserialize_graph(serialize_graph(result["graph"]))
    assert graph == before
    assert result["confirmed"] is True
    assert restored["rev"] == 1 and restored["parent_rev"] == 0
    assert restored["settings"]["global_string_sizing_confirmed"] is True
    assert restored["settings"]["voc_cold"] == recorded["response"]["voc_cold"]
    evidence = cloud.require_sizing(restored)
    receipt = evidence["records"][graph["settings"]["id"]]
    assert receipt["request"] == recorded["request"]
    assert receipt["response"] == recorded["response"]
    assert receipt["endpoint"] == "https://api.leafdesign.ai/string-length"
    assert receipt["request_sha256"] == cloud.digest(recorded["request"])
    assert receipt["response_sha256"] == cloud.digest(recorded["response"])
    assert "grant_ref" not in json.dumps(evidence)
    assert "access_token" not in json.dumps(evidence)
    assert service == [recorded["request"]]


def test_complete_zone_confirmation(graph, recorded, service):
    result = confirm(graph, sizing_params(graph, recorded, "zones"))["graph"]
    assert result["settings"]["global_string_sizing_confirmed"] is False
    assert result["electrical_zones"][0]["voc_cold"] == recorded["response"]["voc_cold"]
    assert cloud.require_sizing(deserialize_graph(serialize_graph(result)))["mode"] == "zones"


@pytest.mark.parametrize("defect", ["missing", "overlap", "extra_request", "model"])
def test_zone_coverage_checked_before_service(graph, recorded, service, defect):
    if defect == "missing":
        graph["electrical_zones"][0]["panel_refs"].pop()
    elif defect == "overlap":
        other = copy.deepcopy(graph["electrical_zones"][0])
        other["id"] = app_id("zone-el", 2)
        graph["electrical_zones"].append(other)
    params = sizing_params(graph, recorded, "zones")
    if defect == "extra_request":
        params["requests"]["missing"] = recorded["request"]
    elif defect == "model":
        params["requests"][graph["electrical_zones"][0]["id"]]["module"]["model"] = "other"
    with pytest.raises(GraphValidationError):
        confirm(graph, params)
    assert service == []


def test_cancel_and_preview_leave_source_unchanged(graph, recorded, service):
    before = copy.deepcopy(graph)
    assert confirm(graph, sizing_params(graph, recorded, cancel=True))["graph"] == before
    assert service == []
    preview = confirm(graph, sizing_params(graph, recorded, confirm=False))
    assert preview["graph"] == graph == before and not preview["confirmed"]
    assert len(service) == 1
    assert settings.run(graph, {"expected_rev": 0, "cancel": True}) == before
    assert groups.create_groups(graph, {"expected_rev": 0, "cancel": True},
                                drawing_intake={})["graph"] == before


@pytest.mark.parametrize("field,value", [("num_mppt", -1), ("use_l2_collectors", "yes"),
                                         ("panels_in_sequence", True), ("global_string_sizing_confirmed", True)])
def test_invalid_settings(graph, field, value):
    before = copy.deepcopy(graph)
    with pytest.raises(GraphValidationError):
        settings.run(graph, {"expected_rev": 0, "changes": {field: value}})
    assert graph == before


def test_drawing_settings_invalidate_confirmation(graph, recorded, service):
    current = confirm(graph, sizing_params(graph, recorded))["graph"]
    changed = settings.run(current, {"expected_rev": 1, "changes": {"panels_in_sequence": 3}})
    assert changed["settings"]["panels_in_sequence"] == 3
    assert changed["settings"]["id"] == current["settings"]["id"]
    assert current["settings"]["panels_in_sequence"] == 2
    assert changed["rev"] == 2
    with pytest.raises(GraphValidationError, match="SIZING_CONFIRMATION_REQUIRED"):
        cloud.require_sizing(changed)


@pytest.mark.parametrize("defect", ["units", "scale", "missing_panel", "stale"])
def test_graph_preconditions(graph, recorded, service, defect):
    params = sizing_params(graph, recorded)
    if defect == "units":
        graph["project"]["units"]["drawing_units"] = "unknown"
    elif defect == "scale":
        graph["project"]["units"]["meters_per_unit"] = 1
    elif defect == "missing_panel":
        graph["panels"].pop()
    else:
        params["expected_rev"] = 9
    with pytest.raises(GraphValidationError):
        confirm(graph, params)
    assert service == []


@pytest.mark.parametrize("defect", ["blank_model", "unknown", "nonfinite", "secret", "units"])
def test_request_rejected_before_transport(graph, recorded, service, defect):
    params = sizing_params(graph, recorded)
    request = params["requests"][graph["settings"]["id"]]
    if defect == "blank_model":
        request["module"]["model"] = " "
    elif defect == "unknown":
        request["unexpected"] = 1
    elif defect == "nonfinite":
        request["module"]["voc"] = float("nan")
    elif defect == "secret":
        params["access_token"] = "synthetic-invalid"
    else:
        request["units"] = "unknown"
    with pytest.raises((CloudError, GraphValidationError)):
        confirm(graph, params)
    assert service == []


@pytest.mark.parametrize("raw", [b"{}", b"null", b"not json", b'{"panels_in_sequence":2,"panels_in_sequence":2}', None],
                         ids=["empty-object", "null", "invalid-json", "duplicate-key", "oversized"])
def test_bad_response_no_mutation(graph, recorded, service, monkeypatch, raw):
    if raw is None:
        raw = b"x" * 65537
    before = copy.deepcopy(graph)
    monkeypatch.setattr(cloud, "post_string_length", lambda *a: raw)
    with pytest.raises(CloudError, match="cloud_response_invalid"):
        confirm(graph, sizing_params(graph, recorded))
    assert graph == before


def test_failed_cold_voltage_cannot_confirm(graph, recorded, service, monkeypatch):
    response = copy.deepcopy(recorded["response"])
    response["voc_cold"].update(passes=False, per_module=400, string_voltage=800,
                               suggested_string_length=1)
    monkeypatch.setattr(cloud, "post_string_length", lambda *a: cloud.canonical_bytes(response))
    with pytest.raises(GraphValidationError, match="COLD_VOLTAGE_FAILED"):
        confirm(graph, sizing_params(graph, recorded))
    assert graph["rev"] == 0


def test_partial_zone_service_failure_is_atomic(graph, recorded, service, monkeypatch):
    other = copy.deepcopy(graph["electrical_zones"][0])
    other["id"] = app_id("zone-el", 2)
    other["panel_refs"] = [graph["electrical_zones"][0]["panel_refs"].pop()]
    graph["electrical_zones"].append(other)
    before = copy.deepcopy(graph)
    calls = []

    def post(*args):
        calls.append(True)
        if len(calls) == 2:
            raise CloudError("cloud_upstream_failure", 502)
        return cloud.canonical_bytes(recorded["response"])

    monkeypatch.setattr(cloud, "post_string_length", post)
    with pytest.raises(CloudError, match="cloud_upstream_failure"):
        confirm(graph, sizing_params(graph, recorded, "zones"))
    assert len(calls) == 2 and graph == before


@pytest.mark.parametrize("defect", ["geometry", "response", "length", "confirmed"])
def test_stale_or_modified_sizing_evidence_blocks_progress(graph, recorded, service, defect):
    result = confirm(graph, sizing_params(graph, recorded))["graph"]
    if defect == "geometry":
        result["panels"][0]["centre"][0] += 1
    elif defect == "response":
        evidence = result["settings"]["extra"]["string_sizing"]
        evidence["records"][graph["settings"]["id"]]["response"]["voc_cold"]["passes"] = False
    elif defect == "length":
        result["settings"]["panels_in_sequence"] = 3
    else:
        result["settings"]["global_string_sizing_confirmed"] = False
    with pytest.raises(GraphValidationError, match="SIZING_CONFIRMATION_REQUIRED"):
        cloud.require_sizing(result)


@pytest.fixture
def group_case(graph, recorded, service):
    frame = copy.deepcopy(graph["frames"][0])
    graph["frames"] = []
    for n, panel in enumerate(graph["panels"], 1):
        panel["frame_ref"] = None
        panel["matrix_cell"] = None
        panel["provenance"]["source_handle"] = f"A{n}"
    graph = confirm(graph, sizing_params(graph, recorded))["graph"]
    # Membership order is deliberately different from matrix traversal order.
    frame["panel_refs"] = list(reversed(frame["panel_refs"]))
    params = {"expected_rev": 1, "groups": [{"name": frame["name"], "panel_refs": frame["panel_refs"]}]}
    intake = {"polylines": [{"handle": p["provenance"]["source_handle"], "kind": "LWPOLYLINE",
                              "closed": True, "pts": [[0, 0], [1, 0], [1, 1], [0, 1]]}
                             for p in graph["panels"]]}
    calls = []

    def licensed(**kwargs):
        assert kwargs["timeout"] == 50
        assert b"LEAF_MUTATION_PLAN|3" in kwargs["plan"]
        calls.append(kwargs)
        return {"plan_sha256": plan_sha256(kwargs["plan"]), "frames": [copy.deepcopy(frame)]}

    return graph, params, intake, frame, licensed, calls


def test_licensed_groups_preserve_order_geometry_assignments_on_reopen(group_case):
    graph, params, intake, frame, licensed, calls = group_case
    before = copy.deepcopy(graph)
    result = groups.create_groups(graph, params, drawing_intake=intake, licensed_matrix=licensed)
    restored = deserialize_graph(serialize_graph(result["graph"]))
    actual = restored["frames"][0]
    for key in ("name", "panel_refs", "insertion_point", "matrix", "sequences", "panel_assignments"):
        assert actual[key] == frame[key]
    assert result["mutations"]["added_groups"][0]["members"] == ["A3", "A2", "A1"]
    assert restored["rev"] == 2 and graph == before and len(calls) == 1
    assert restored["strings"] == graph["strings"]
    assert restored["inverters"] == graph["inverters"]
    assert [p["centre"] for p in restored["panels"]] == [p["centre"] for p in graph["panels"]]
    cloud.require_sizing(restored)


@pytest.mark.parametrize("defect", ["missing", "duplicate", "name", "handle", "unsized"])
def test_group_preflight_blocks_lane(group_case, defect):
    graph, params, intake, frame, licensed, calls = group_case
    if defect == "missing":
        params["groups"][0]["panel_refs"][0] = app_id("panel", 99)
    elif defect == "duplicate":
        params["groups"][0]["panel_refs"][1] = params["groups"][0]["panel_refs"][0]
    elif defect == "name":
        params["groups"][0]["name"] = "bad[name"
    elif defect == "handle":
        intake["polylines"].pop()
    else:
        graph["settings"]["extra"].clear()
    with pytest.raises(ValueError):
        groups.create_groups(graph, params, drawing_intake=intake, licensed_matrix=licensed)
    assert calls == []


@pytest.mark.parametrize("defect", ["matrix", "sequence", "assignment", "geometry", "order", "hash"])
def test_bad_licensed_reply_is_atomic(group_case, defect):
    graph, params, intake, frame, licensed, calls = group_case
    before = copy.deepcopy(graph)

    def bad(**kwargs):
        reply = licensed(**kwargs)
        output = reply["frames"][0]
        if defect == "matrix":
            output["matrix"][0][0]["panel_ref"] = app_id("panel", 99)
        elif defect == "sequence":
            output["sequences"] = []
        elif defect == "assignment":
            output["panel_assignments"][0]["seq"] = 99
        elif defect == "geometry":
            output["matrix"][0][0]["x"] += 1
        elif defect == "order":
            output["panel_refs"].reverse()
        else:
            reply["plan_sha256"] = "0" * 64
        return reply

    with pytest.raises(GraphValidationError):
        groups.create_groups(graph, params, drawing_intake=intake, licensed_matrix=bad)
    assert graph == before


def test_groups_cancel_and_no_local_matrix_fallback(group_case):
    graph, params, intake, frame, licensed, calls = group_case
    cancelled = groups.create_groups(graph, dict(params, cancel=True),
                                     drawing_intake=intake, licensed_matrix=licensed)
    assert cancelled["graph"] == graph and cancelled["plan"] is None and calls == []
    with pytest.raises(GraphValidationError, match="LICENSED_MATRIX_REQUIRED"):
        groups.create_groups(graph, params, drawing_intake=intake)
    with pytest.raises(RuntimeError, match="broker"):
        sizing.run(graph, {})
    with pytest.raises(RuntimeError, match="broker"):
        groups.run(graph, {})


@pytest.mark.parametrize("status,code", [(401, "cloud_auth_missing"), (403, "cloud_tenant_unauthorized"),
                                        (302, "cloud_upstream_failure"), (500, "cloud_upstream_failure")])
def test_transport_status_and_bounds(recorded, monkeypatch, status, code):
    class Reply:
        status_code = status

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def post(url, **kwargs):
        assert url == cloud.SIZING_URL
        assert kwargs["timeout"] == (5, 45) and kwargs["allow_redirects"] is False
        assert kwargs["stream"] is True
        assert json.loads(kwargs["data"]) == recorded["request"]
        return Reply()

    monkeypatch.setattr(cloud.requests, "post", post)
    with pytest.raises(CloudError, match=code):
        cloud.post_string_length(cloud.SizingRequest.model_validate(recorded["request"]),
                                 CloudGrant("fixture-tenant", ""))


def test_transport_exception_is_sanitized(recorded, monkeypatch):
    def fail(*a, **k):
        raise cloud.requests.Timeout("private upstream detail")
    monkeypatch.setattr(cloud.requests, "post", fail)
    with pytest.raises(CloudError, match="^cloud_upstream_failure$"):
        cloud.post_string_length(cloud.SizingRequest.model_validate(recorded["request"]),
                                 CloudGrant("fixture-tenant", ""))


@pytest.mark.parametrize("oversized", [False, True])
def test_transport_reads_bounded_response(recorded, monkeypatch, oversized):
    raw = cloud.canonical_bytes(recorded["response"])

    class Reply:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_content(self, chunk_size):
            assert chunk_size == 16384
            yield b"x" * 65537 if oversized else raw

    monkeypatch.setattr(cloud.requests, "post", lambda *a, **k: Reply())
    request = cloud.SizingRequest.model_validate(recorded["request"])
    if oversized:
        with pytest.raises(CloudError, match="cloud_response_invalid"):
            cloud.post_string_length(request, CloudGrant("fixture-tenant", ""))
    else:
        assert cloud.post_string_length(request, CloudGrant("fixture-tenant", "")) == raw
