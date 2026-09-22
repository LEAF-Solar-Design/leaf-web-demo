"""Offline W1 sizing/group transaction checks.

The plugin fixture is the real cached StringSizer response; it fails the plugin's own
cold-Voc guard at its recommended 28. Flows that must confirm use the SYNTHETIC passing
variant (the same response with standard.string_length 27), named `passing` below.
"""
from __future__ import annotations

import copy
import hashlib
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

# NecVocGate.ComputeVocCold on the real response: Voc 52.58 V, bvoc -0.13145 read as
# %/degC, min_temp -2.700000047683716 degC, same operation order as the C#.
PER_MODULE = 52.58 * (1.0 + -0.13145 / 100.0 * (-2.700000047683716 - 25.0))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("offline tests must not use the network")
    monkeypatch.setattr(cloud.requests.sessions.Session, "request", refuse)


@pytest.fixture
def plugin():
    return json.loads((SERVER / "tests/fixtures/w1_plugin_stringsizer_response.json").read_text())


@pytest.fixture
def passing():
    document = json.loads((SERVER / "tests/fixtures/w1_string_length_recorded_response.json").read_text())
    assert document["evidence_kind"] == "synthetic"
    return document


@pytest.fixture
def service(monkeypatch, passing):
    calls = []

    def grant(reference, tenant):
        assert (reference, tenant) == ("fixture-grant", "fixture-tenant")
        return CloudGrant(tenant, "")

    def post(request, grant):
        calls.append(request.wire())
        return cloud.canonical_bytes(passing["response"])

    monkeypatch.setattr(cloud, "resolve_grant", grant)
    monkeypatch.setattr(cloud, "post_string_length", post)
    return calls


def sizing_params(graph, recorded, mode="global", **overrides):
    if mode == "global":
        requests = {graph["settings"]["id"]: copy.deepcopy(recorded["request"])}
    else:
        requests = {z["id"]: dict(copy.deepcopy(recorded["request"]), module_name=z["module_model"],
                                  full_inverter_name=z["inverter_model_a"])
                    for z in graph["electrical_zones"]}
    return {"expected_rev": graph["rev"], "mode": mode, "requests": requests,
            "grant_ref": "fixture-grant", "confirm": True, **overrides}


def confirm(graph, params):
    return sizing.size_strings(graph, params, tenant_id="fixture-tenant", job_id="fixture-job")


def outcome(recorded):
    return cloud.recommend(cloud.SizingResponse.model_validate(recorded["response"]))


def test_plugin_guard_on_real_response(plugin):
    result = outcome(plugin)
    assert result["panels_in_sequence"] == 28
    assert result["voc_cold"] == {
        "passes": False, "override_accepted": False, "suggested_string_length": 27,
        "per_module": PER_MODULE, "string_voltage": PER_MODULE * 28, "max_dc_voltage": 1500.0}
    assert result["voc_cold"]["per_module"] == pytest.approx(54.4945246, abs=1e-6)
    assert result["voc_cold"]["string_voltage"] == pytest.approx(1525.846689, abs=1e-5)


def test_plugin_guard_passes_on_synthetic_length(passing):
    assert outcome(passing) == {"panels_in_sequence": 27, "voc_cold": {
        "passes": True, "override_accepted": False, "suggested_string_length": 0,
        "per_module": PER_MODULE, "string_voltage": PER_MODULE * 27, "max_dc_voltage": 1500.0}}


@pytest.mark.parametrize("length,expected", [(27.9, 27), (1.5, 1)])
def test_recommendation_truncates_like_the_plugin_cast(plugin, length, expected):
    # SYNTHETIC lengths: the C# (int) cast truncates standard.string_length toward zero.
    plugin["response"]["simulation_results"]["standard"]["string_length"] = length
    assert outcome(plugin)["panels_in_sequence"] == expected


def test_request_serializes_like_jsonconvert(plugin):
    request = dict(plugin["request"], racking_params=dict(
        plugin["request"]["racking_params"], axis_tilt=None, axis_azimuth=None,
        max_angle=None, backtrack=None, gcr=None), module_parameters=None)
    parsed = cloud.SizingRequest.model_validate(request)
    assert cloud.wire_bytes(parsed) == (
        '{"module_name":"JA_Solar_JAM72D40-595/MB","full_inverter_name":"Sungrow SG-HX SG250HX",'
        '"bifacial":false,"bifacial_coefficient":".7","racking_params":{"racking_type":"fixed_tilt",'
        '"surface_tilt":"5","surface_azimuth":"180","albedo":".25"},"max_voltage":"1500",'
        '"thermal_model_type":"close mount glass glass","open_circuit_rise":false,'
        '"zip_code":"44224"}').encode()
    assert parsed.wire() == plugin["request"]


def test_tracker_request_serializes_tracker_fields_in_plugin_order(plugin):
    # SYNTHETIC tracker values, shaped as StringSizerInputForm sets them.
    request = copy.deepcopy(plugin["request"])
    request["racking_params"] = {"racking_type": "single_axis", "surface_tilt": "0",
                                 "surface_azimuth": "0", "albedo": ".25", "gcr": "0.35",
                                 "backtrack": True, "max_angle": "60", "axis_azimuth": "180",
                                 "axis_tilt": "0"}
    raw = cloud.wire_bytes(cloud.SizingRequest.model_validate(request))
    assert (b'"racking_params":{"racking_type":"single_axis","surface_tilt":"0","surface_azimuth":"0",'
            b'"albedo":".25","axis_tilt":"0","axis_azimuth":"180","max_angle":"60",'
            b'"backtrack":true,"gcr":"0.35"}') in raw


def test_global_confirmation_and_reopen(graph, passing, service):
    before = copy.deepcopy(graph)
    result = confirm(graph, sizing_params(graph, passing))
    restored = deserialize_graph(serialize_graph(result["graph"]))
    assert graph == before
    assert result["confirmed"] is True
    assert restored["rev"] == 1 and restored["parent_rev"] == 0
    assert restored["settings"]["global_string_sizing_confirmed"] is True
    assert restored["settings"]["panels_in_sequence"] == 27
    assert restored["settings"]["voc_cold"] == outcome(passing)["voc_cold"]
    evidence = cloud.require_sizing(restored)
    receipt = evidence["records"][graph["settings"]["id"]]
    assert receipt["request"] == passing["request"]
    assert receipt["response"] == passing["response"]
    assert receipt["sizing"] == outcome(passing)
    assert receipt["endpoint"] == "https://api.leafdesign.ai/string-length"
    assert receipt["adapter_version"] == "2.0.0"
    assert receipt["request_sha256"] == cloud.digest(passing["request"])
    assert receipt["response_sha256"] == cloud.digest(passing["response"])
    assert receipt["wire_response_sha256"] == hashlib.sha256(
        cloud.canonical_bytes(passing["response"])).hexdigest()
    assert "grant_ref" not in json.dumps(evidence)
    assert "access_token" not in json.dumps(evidence)
    assert service == [passing["request"]]


def test_complete_zone_confirmation(graph, passing, service):
    result = confirm(graph, sizing_params(graph, passing, "zones"))["graph"]
    assert result["settings"]["global_string_sizing_confirmed"] is False
    assert result["electrical_zones"][0]["voc_cold"] == outcome(passing)["voc_cold"]
    assert result["electrical_zones"][0]["panels_in_sequence"] == 27
    assert cloud.require_sizing(deserialize_graph(serialize_graph(result)))["mode"] == "zones"


@pytest.mark.parametrize("defect", ["missing", "overlap", "extra_request", "model"])
def test_zone_coverage_checked_before_service(graph, passing, service, defect):
    if defect == "missing":
        graph["electrical_zones"][0]["panel_refs"].pop()
    elif defect == "overlap":
        other = copy.deepcopy(graph["electrical_zones"][0])
        other["id"] = app_id("zone-el", 2)
        graph["electrical_zones"].append(other)
    params = sizing_params(graph, passing, "zones")
    if defect == "extra_request":
        params["requests"]["missing"] = passing["request"]
    elif defect == "model":
        params["requests"][graph["electrical_zones"][0]["id"]]["module_name"] = "other"
    with pytest.raises(GraphValidationError):
        confirm(graph, params)
    assert service == []


def test_cancel_and_preview_leave_source_unchanged(graph, passing, service):
    before = copy.deepcopy(graph)
    assert confirm(graph, sizing_params(graph, passing, cancel=True))["graph"] == before
    assert service == []
    preview = confirm(graph, sizing_params(graph, passing, confirm=False))
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


def test_drawing_settings_invalidate_confirmation(graph, passing, service):
    current = confirm(graph, sizing_params(graph, passing))["graph"]
    changed = settings.run(current, {"expected_rev": 1, "changes": {"panels_in_sequence": 3}})
    assert changed["settings"]["panels_in_sequence"] == 3
    assert changed["settings"]["id"] == current["settings"]["id"]
    assert current["settings"]["panels_in_sequence"] == 27
    assert changed["rev"] == 2
    with pytest.raises(GraphValidationError, match="SIZING_CONFIRMATION_REQUIRED"):
        cloud.require_sizing(changed)


@pytest.mark.parametrize("defect", ["units", "scale", "missing_panel", "stale"])
def test_graph_preconditions(graph, passing, service, defect):
    params = sizing_params(graph, passing)
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


@pytest.mark.parametrize("defect", ["blank_model", "unknown", "nonfinite", "secret", "racking", "null"])
def test_request_rejected_before_transport(graph, passing, service, defect):
    params = sizing_params(graph, passing)
    request = params["requests"][graph["settings"]["id"]]
    if defect == "blank_model":
        request["module_name"] = " "
    elif defect == "unknown":
        request["unexpected"] = 1
    elif defect == "nonfinite":
        request["module_parameters"] = {
            "V_oc_ref": float("nan"), "I_sc_ref": 14.0, "V_mp_ref": 44.64, "I_mp_ref": 13.33,
            "alpha_sc": 0.007, "beta_oc": -0.13, "N_s": 81, "STC": 595.0, "gamma_r": -0.25,
            "T_NOCT": 45.0}
    elif defect == "secret":
        params["access_token"] = "synthetic-invalid"
    elif defect == "racking":
        request["racking_params"]["gcr"] = "0.35"
    else:
        request["zip_code"] = None
    with pytest.raises((CloudError, GraphValidationError)):
        confirm(graph, params)
    assert service == []


@pytest.mark.parametrize("raw", [b"{}", b"null", b"not json", b'{"voc":1,"voc":1}', None],
                         ids=["empty-object", "null", "invalid-json", "duplicate-key", "oversized"])
def test_bad_response_no_mutation(graph, passing, service, monkeypatch, raw):
    if raw is None:
        raw = b"x" * 65537
    before = copy.deepcopy(graph)
    monkeypatch.setattr(cloud, "post_string_length", lambda *a: raw)
    with pytest.raises(CloudError, match="cloud_response_invalid"):
        confirm(graph, sizing_params(graph, passing))
    assert graph == before


@pytest.mark.parametrize("defect", ["missing_standard", "fractional_design_voltage", "zero_voc", "extra_key"])
def test_response_contract_refusals(graph, plugin, passing, service, monkeypatch, defect):
    response = copy.deepcopy(plugin["response"])
    if defect == "missing_standard":
        del response["simulation_results"]["standard"]
    elif defect == "fractional_design_voltage":
        response["simulation_results"]["standard"]["string_design_voltage"] = 1500.5
    elif defect == "zero_voc":
        response["voc"] = 0
    else:
        response["unexpected"] = 1
    before = copy.deepcopy(graph)
    monkeypatch.setattr(cloud, "post_string_length", lambda *a: cloud.canonical_bytes(response))
    with pytest.raises(CloudError, match="cloud_response_invalid"):
        confirm(graph, sizing_params(graph, passing))
    assert graph == before


def test_real_response_fails_the_guard_and_cannot_confirm(graph, plugin, passing, service, monkeypatch):
    monkeypatch.setattr(cloud, "post_string_length",
                        lambda *a: cloud.canonical_bytes(plugin["response"]))
    preview = confirm(graph, sizing_params(graph, plugin, confirm=False))
    record = preview["records"][graph["settings"]["id"]]
    assert record["sizing"] == outcome(plugin) and record["response"] == plugin["response"]
    with pytest.raises(GraphValidationError, match="COLD_VOLTAGE_FAILED"):
        confirm(graph, sizing_params(graph, plugin))
    assert graph["rev"] == 0


def test_partial_zone_service_failure_is_atomic(graph, passing, service, monkeypatch):
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
        return cloud.canonical_bytes(passing["response"])

    monkeypatch.setattr(cloud, "post_string_length", post)
    with pytest.raises(CloudError, match="cloud_upstream_failure"):
        confirm(graph, sizing_params(graph, passing, "zones"))
    assert len(calls) == 2 and graph == before


@pytest.mark.parametrize("defect", ["geometry", "response", "sizing", "length", "confirmed"])
def test_stale_or_modified_sizing_evidence_blocks_progress(graph, passing, service, defect):
    result = confirm(graph, sizing_params(graph, passing))["graph"]
    record = result["settings"]["extra"]["string_sizing"]["records"][graph["settings"]["id"]]
    if defect == "geometry":
        result["panels"][0]["centre"][0] += 1
    elif defect == "response":
        record["response"]["simulation_results"]["standard"]["string_length"] = 26.0
    elif defect == "sizing":
        record["sizing"]["voc_cold"]["passes"] = False
    elif defect == "length":
        result["settings"]["panels_in_sequence"] = 3
    else:
        result["settings"]["global_string_sizing_confirmed"] = False
    with pytest.raises(GraphValidationError, match="SIZING_CONFIRMATION_REQUIRED"):
        cloud.require_sizing(result)


@pytest.fixture
def group_case(graph, passing, service):
    frame = copy.deepcopy(graph["frames"][0])
    graph["frames"] = []
    for n, panel in enumerate(graph["panels"], 1):
        panel["frame_ref"] = None
        panel["matrix_cell"] = None
        panel["provenance"]["source_handle"] = f"A{n}"
    graph = confirm(graph, sizing_params(graph, passing))["graph"]
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
def test_transport_status_and_bounds(plugin, monkeypatch, status, code):
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
        assert json.loads(kwargs["data"]) == plugin["request"]
        return Reply()

    monkeypatch.setattr(cloud.requests, "post", post)
    with pytest.raises(CloudError, match=code):
        cloud.post_string_length(cloud.SizingRequest.model_validate(plugin["request"]),
                                 CloudGrant("fixture-tenant", ""))


def test_transport_exception_is_sanitized(plugin, monkeypatch):
    def fail(*a, **k):
        raise cloud.requests.Timeout("private upstream detail")
    monkeypatch.setattr(cloud.requests, "post", fail)
    with pytest.raises(CloudError, match="^cloud_upstream_failure$"):
        cloud.post_string_length(cloud.SizingRequest.model_validate(plugin["request"]),
                                 CloudGrant("fixture-tenant", ""))


@pytest.mark.parametrize("oversized", [False, True])
def test_transport_reads_bounded_response(plugin, monkeypatch, oversized):
    raw = cloud.canonical_bytes(plugin["response"])

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
    request = cloud.SizingRequest.model_validate(plugin["request"])
    if oversized:
        with pytest.raises(CloudError, match="cloud_response_invalid"):
            cloud.post_string_length(request, CloudGrant("fixture-tenant", ""))
    else:
        assert cloud.post_string_length(request, CloudGrant("fixture-tenant", "")) == raw
