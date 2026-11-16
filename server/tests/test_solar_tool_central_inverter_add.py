"""The missing L2 producer, exercised on product chains and the real commit adapter."""
import copy
from pathlib import Path
import re
import sys

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import solar_local_graph as local
import solar_tools
import store
from leaf_cloud_client import canonical_bytes
from solar_design_graph import GraphValidationError, validate_graph
from solar_sizing_client import digest
import test_solar_ground_equipment as ge
import test_solar_tool_solar_panels_from_drawing as roof_source
from test_solar_ground_equipment import service, pinned  # noqa: F401
from test_w1_design_graph import graph  # noqa: F401
from test_w1_local_graph_adapter import held

TOOL = "solar-central-inverter-add"
INVALID = "INVALID_CENTRAL_INVERTER_REQUEST"


def hardware(**changes):
    value = {"model": "Central 100", "max_dc_voltage": 1500, "max_ac_power_kw": 100,
             "mppt_count": 1, "total_dc_inputs": 1, "collector_capacity": 4}
    value.update(changes)
    return value


def request(g, **changes):
    value = {"expected_rev": g["rev"], "number": 1, "point": [100.0, 100.0], "hardware": hardware()}
    value.update(changes)
    return value


def mode(g, enabled=True):
    return local._load_builtin("solar-settings").run(
        g, {"expected_rev": g["rev"], "changes": {"use_l2_collectors": enabled}})


@pytest.fixture
def builtin():
    return local._load_builtin(TOOL)


@pytest.fixture
def bases(graph, service, pinned):
    sized = ge.sized(ge.converted(graph))
    equipped = ge.equip(ge.strung(sized, pinned))
    roof = local._load_builtin("solar-settings").run(
        roof_source.empty_graph(), {"expected_rev": 0, "project_changes": {"zip_code": "44224"}})
    roof = local._load_builtin("solar-panels-from-drawing").run(
        roof, {"expected_rev": roof["rev"]},
        source_intake={"polylines": [copy.deepcopy(roof_source.P1), copy.deepcopy(roof_source.P6)]})
    roof = ge.sized(roof)
    roof = local._load_builtin("solar-panel-groups").run(roof, {
        "expected_rev": roof["rev"], "groups": [{"name": "Roof",
            "panel_refs": [p["id"] for p in roof["panels"]], "alignment_tolerance": .5,
            "module_width_along_row": 77 * .3048, "module_height_across_row": 38.5 * .3048}]})
    roof = pinned.run(roof, {"expected_rev": roof["rev"], "operation": "add-string",
                             "ordered_panel_refs": [p["id"] for p in roof["panels"]]})
    return {"ground": mode(equipped), "equipped": equipped, "sized": mode(sized),
            "roof": mode(roof), "roof_off": roof}


def refusal(builtin, g, params, code, path="<root>"):
    before, snapshot = copy.deepcopy(g), copy.deepcopy(params)
    with pytest.raises(GraphValidationError) as error:
        builtin.run(g, params)
    assert (error.value.code, error.value.path) == (code, path)
    assert g == before
    # A NaN is unequal to itself: compare its spelling in that one malformed request.
    if code == "NONFINITE_NUMBER":
        assert repr(params) == repr(snapshot)
    else:
        assert params == snapshot


def assert_success(before, after, params):
    snapshot = copy.deepcopy(before)
    assert set(after) == set(snapshot)
    assert {k for k in snapshot if after[k] != snapshot[k]} == {"inverters", "parent_rev", "rev"}
    assert after["rev"] == snapshot["rev"] + 1
    assert after["parent_rev"] == snapshot["rev"]
    assert len(after["inverters"]) == len(snapshot["inverters"]) + 1
    assert after["inverters"][:-1] == snapshot["inverters"]
    added = after["inverters"][-1]
    assert added["is_l2"] is True
    assert added["equipment_type"] == "central_inverter"
    assert added["number"] == params.get("number", 1)
    assert {key: added[key] for key in params["hardware"]} == params["hardware"]
    mpu = snapshot["project"]["units"]["meters_per_unit"]
    assert added["position"] == [v * mpu for v in params["point"]]
    assert added["input_assignments"] == added["l1_assignments"] == []
    assert added["validity"] == {"state": "valid", "reasons": []}
    assert added["provenance"]["last_writer"] == added["provenance"]["tool_id"] == TOOL
    assert re.fullmatch(r"leaf:inverter:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", added["id"])
    held_ids = {item["id"] for value in snapshot.values() if isinstance(value, list)
                for item in value if isinstance(item, dict) and "id" in item}
    assert added["id"] not in held_ids
    assert after["strings"] == snapshot["strings"]
    assert validate_graph(copy.deepcopy(after)) == after


def test_central_inverter_add_ground_success(builtin, bases):
    before = bases["ground"]
    params = request(before)
    after = builtin.run(before, params)
    assert_success(before, after, params)
    assert before["inverters"][0]["number"] == after["inverters"][-1]["number"] == 1
    assert before["inverters"][0]["is_l2"] is False
    assert local._load_builtin("solar-feeders").input_readiness(after) == {
        "input_ready": True, "input_reason": None}
    assert local._load_builtin("solar-combiners").input_readiness(after)["input_reason"] == "unassigned_strings_required"


def test_central_inverter_add_rooftop_success(builtin, bases):
    before = bases["roof"]
    assert before["inverters"] == []
    params = request(before)
    after = builtin.run(before, params)
    assert_success(before, after, params)
    assert len(after["inverters"]) == 1
    assert local._load_builtin("solar-combiners").input_readiness(after) == {
        "input_ready": True, "input_reason": None}
    assert local._load_builtin("solar-feeders").input_readiness(after)["input_reason"] == "equipment_assignment_required"


def test_central_inverter_add_default_number(builtin, bases):
    before = bases["ground"]
    params = request(before)
    del params["number"]
    first = builtin.run(before, params)
    assert first["inverters"][-1]["number"] == 1
    params = request(first, point=[200.0, 100.0])
    del params["number"]
    second = builtin.run(first, params)
    assert [i["number"] for i in second["inverters"] if i["is_l2"]] == [1, 2]


def test_central_inverter_add_readiness(builtin, bases, monkeypatch):
    for key, reason in [("equipped", "valid_settings_required"), ("ground", None),
                        ("roof_off", "valid_settings_required"), ("sized", "valid_strings_required")]:
        g = bases[key]
        assert builtin.input_readiness(g) == {"input_ready": reason is None, "input_reason": reason}
        if reason:
            refusal(builtin, g, request(g), reason.upper())
    with monkeypatch.context() as patch:
        patch.setattr(builtin.bridge, "MAX_DEVICES", len(bases["ground"]["inverters"]))
        assert builtin.input_readiness(bases["ground"]) == {
            "input_ready": False, "input_reason": "capability_not_ready"}
        refusal(builtin, bases["ground"], request(bases["ground"]), "CAPABILITY_NOT_READY")


def test_central_inverter_add_stale_revision(builtin, bases):
    refusal(builtin, bases["ground"], request(bases["ground"], expected_rev=0), "STALE_GRAPH_REVISION")


def test_central_inverter_add_duplicate_number(builtin, bases):
    first = builtin.run(bases["ground"], request(bases["ground"]))
    refusal(builtin, first, request(first, point=[200.0, 100.0]), INVALID, "number")


def test_central_inverter_add_capacity_mismatch(builtin, bases):
    first = builtin.run(bases["ground"], request(bases["ground"]))
    refusal(builtin, first, request(first, number=2, hardware=hardware(collector_capacity=5)),
            INVALID, "hardware.collector_capacity")


# Every request-shape row in the measured case table, including the accepted boundaries.
SHAPES = [
    ("hardware", {"collector_capacity": 0}, INVALID, "hardware"),
    ("hardware", {"collector_capacity": 10001}, INVALID, "hardware"),
    ("hardware", {"collector_capacity": True}, INVALID, "hardware"),
    ("hardware", {"mppt_count": 0}, INVALID, "hardware"),
    ("hardware", {"max_dc_voltage": 0}, INVALID, "hardware"),
    ("hardware", {"max_dc_voltage": True}, INVALID, "hardware"),
    ("hardware", {"max_ac_power_kw": 1000001}, INVALID, "hardware"),
    ("hardware", {"model": "  "}, INVALID, "hardware"),
    ("hardware", {"model": ""}, INVALID, "hardware"),
    ("hardware", {"model": "m" * 4097}, INVALID, "hardware"),
    ("hardware", {"total_dc_inputs": 0}, INVALID, "hardware"),
    ("hardware", {"total_dc_inputs": 1000001}, INVALID, "hardware"),
    ("hardware", {"collector_capacity": 10000}, None, None),
    ("hardware", {"model": "m" * 4096}, None, None),
    ("hardware-missing", None, INVALID, "hardware"),
    ("hardware", {"extra": 1}, INVALID, "hardware"),
    ("point", [1000001, 0], INVALID, "point"),
    ("point", [1000000, -1000000], None, None),
    ("point", [1, 2, 3], INVALID, "point"),
    ("point", ["1", 2], INVALID, "point"),
    ("point", [True, 2], INVALID, "point"),
    ("point", [3, 4], None, None),
    ("number", 0, INVALID, "number"),
    ("number", 1000000, INVALID, "number"),
    ("number", 999999, None, None),
    ("number", True, INVALID, "number"),
    ("number", None, INVALID, "number"),
    ("number", "1", INVALID, "number"),
    ("expected_rev", True, INVALID, "expected_rev"),
    ("expected_rev", "6", INVALID, "expected_rev"),
    ("point", [float("nan"), 0.0], "NONFINITE_NUMBER", "<root>"),
    ("unknown", .01, INVALID, "<root>"),
    ("missing-hardware", None, INVALID, "<root>"),
    ("not-object", None, INVALID, "<root>"),
]


def shaped_request(g, key, value):
    params = request(g)
    if key == "hardware":
        params["hardware"] = hardware(**value)
    elif key == "hardware-missing":
        del params["hardware"]["mppt_count"]
    elif key == "missing-hardware":
        del params["hardware"]
    elif key == "not-object":
        params = [1]
    else:
        params[key] = copy.deepcopy(value)
    return params


@pytest.mark.parametrize("key,value,code,path", SHAPES,
                         ids=[f"shape-{n}-{row[0]}" for n, row in enumerate(SHAPES)])
def test_central_inverter_add_request_shape(builtin, bases, key, value, code, path):
    before = bases["ground"]
    params = shaped_request(before, key, value)
    if code:
        refusal(builtin, before, params, code, path)
    else:
        assert_success(before, builtin.run(before, params), params)


def test_central_inverter_add_deterministic_rerun(builtin, bases):
    before = bases["ground"]
    first = builtin.run(copy.deepcopy(before), request(before))
    second = builtin.run(copy.deepcopy(before), request(before))
    assert first == second
    assert digest(first) == digest(second)
    other = builtin.run(copy.deepcopy(before), request(before, point=[200.0, 100.0]))
    assert first["inverters"][-1]["id"] != other["inverters"][-1]["id"]


@pytest.mark.parametrize("route_reason,schedule_reason", [
    (None, None), ("settings_changed", None), (None, "routes_changed"),
    ("settings_changed", "routes_changed"),
])
def test_central_inverter_add_staleness(builtin, graph, route_reason, schedule_reason):
    before = mode(graph)
    assert len(before["routes"]) == len(before["schedules"]) == 1
    for key, reason in [("routes", route_reason), ("schedules", schedule_reason)]:
        before[key][0]["validity"] = {"state": "stale" if reason else "valid",
                                      "reasons": [reason] if reason else []}
    validate_graph(copy.deepcopy(before))
    snapshot = copy.deepcopy(before)
    after = builtin.run(before, request(before))
    for key, reason in [("routes", route_reason), ("schedules", schedule_reason)]:
        if not reason:
            assert after[key][0]["validity"] == {"state": "stale", "reasons": ["equipment_changed"]}
            assert after[key][0]["id"] == snapshot[key][0]["id"]
        else:
            assert after[key] == snapshot[key]
    assert before == snapshot
    for key in set(before) - {"inverters", "parent_rev", "rev", "routes", "schedules", "extra"}:
        assert after[key] == snapshot[key]
    assert {key: value for key, value in after["extra"].items()
            if key != "solve_coverage"} == snapshot["extra"]
    validate_graph(copy.deepcopy(after))


def test_central_inverter_add_exact_point(builtin, bases):
    before = bases["ground"]
    mpu = before["project"]["units"]["meters_per_unit"]
    px, py = [v / mpu for v in before["inverters"][0]["position"][:2]]
    for point in [[px, py], [px + .001, py]]:
        after = builtin.run(before, request(before, point=point))
        assert after["inverters"][-1]["position"] == [v * mpu for v in point]
        assert after["inverters"][:-1] == before["inverters"]


def test_central_inverter_add_inputs_not_mutated(builtin, bases, monkeypatch):
    before = bases["ground"]
    params = request(before)
    snapshot, saved = copy.deepcopy(before), copy.deepcopy(params)
    builtin.run(before, params)
    assert before == snapshot and params == saved
    for key, value, code, path in SHAPES:
        if code:
            refusal(builtin, before, shaped_request(before, key, value), code, path)
    refusal(builtin, before, request(before, expected_rev=0), "STALE_GRAPH_REVISION")
    for key, code in [("equipped", "VALID_SETTINGS_REQUIRED"), ("sized", "VALID_STRINGS_REQUIRED")]:
        refusal(builtin, bases[key], request(bases[key]), code)
    first = builtin.run(before, request(before))
    refusal(builtin, first, request(first), INVALID, "number")
    refusal(builtin, first, request(first, number=2, hardware=hardware(collector_capacity=5)),
            INVALID, "hardware.collector_capacity")
    with monkeypatch.context() as patch:
        patch.setattr(builtin.bridge, "MAX_DEVICES", len(before["inverters"]))
        refusal(builtin, before, request(before), "CAPABILITY_NOT_READY")
    for owner, name, exception, path in [
        (builtin.bridge, "state_from_graph", builtin.bridge.ElectricalBridgeError("bridge-refusal"), "bridge-refusal"),
        (builtin.devices, "inverter_add", builtin.devices.InverterDeviceError("kernel-refusal"), "InverterDeviceError"),
    ]:
        def fail(*args, **kwargs):
            raise exception
        with monkeypatch.context() as patch:
            patch.setattr(owner, name, fail)
            refusal(builtin, before, request(before), "CAPABILITY_NOT_READY", path)


def test_central_inverter_add_registry(builtin):
    entry = solar_tools.get(TOOL)
    assert entry is not None
    assert {key: entry[key] for key in ("family", "adapter", "readiness", "wave", "order", "maturity", "trusted_inputs")} == {
        "family": "equipment", "adapter": "local-graph-commit", "readiness": {"kind": "hook"},
        "wave": 2, "order": 105, "maturity": "preview", "trusted_inputs": []}
    assert len(solar_tools.entries()) == len(list((SERVER / "solar_tools").glob("*.json")))
    assert builtin.REQUIRED_KEYS | builtin.OPTIONAL_KEYS == set(entry["record"]["params"]["properties"]) - {"drawing_id"}


def test_central_inverter_add_adapter_commit(builtin, bases, monkeypatch):
    before = bases["ground"]
    direct = builtin.run(copy.deepcopy(before), request(before))
    monkeypatch.setenv("LEAF_DRAWING_STORE", "legacy")
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "1")
    monkeypatch.setenv("LEAF_UPLOAD_IMPORT_MUTATIONS_ENABLED", "1")
    monkeypatch.delenv("LEAF_DRAWING_MUTATIONS_FENCE_FILE", raising=False)
    backend = store.InMemoryBackend()
    payload = canonical_bytes({"solar_design_graph": before,
                               "solar_design_graph_sha256": digest(before), "polylines": []})
    with monkeypatch.context() as patch:
        patch.setattr(store, "_read", lambda path: payload)
        store.ingest_drawing(backend, "fixture-tenant", "in-memory-only", drawing_id="solar")
    params = dict(request(before), drawing_id="solar")
    with held(backend) as fence:
        receipt = local.run_local_graph_commit(
            backend, "fixture-tenant", TOOL, params, drawing_id="solar", source_version=1,
            holder="fixture-owner", fence=fence, job_id="central-inverter-add")
        replay = local.run_local_graph_commit(
            backend, "fixture-tenant", TOOL, params, drawing_id="solar", source_version=1,
            holder="fixture-owner", fence=fence, job_id="central-inverter-add")
    assert receipt["tool"] == TOOL
    assert receipt["before_rev"] == before["rev"]
    assert receipt["after_rev"] == receipt["before_rev"] + 1
    assert receipt["replayed"] is False
    assert receipt["graph_sha256"] == digest(direct)
    assert replay["replayed"] is True
    assert replay["graph_sha256"] == receipt["graph_sha256"]
    assert len(store.load_manifest(backend, "fixture-tenant", "solar")["versions"]) == 2
    proof = local.graph_commit_provenance(receipt, params, "fixture-tenant", "central-inverter-add", TOOL, 1, backend=backend)
    assert isinstance(proof, dict)
    assert set(proof) == {"adapter", "execution_mode", "graph_sha256", "intake_sha256",
                          "new_version", "request_sha256", "source_version"}


def test_central_inverter_add_mode_off_after_central(builtin, bases):
    after = builtin.run(bases["ground"], request(bases["ground"]))
    snapshot = copy.deepcopy(after)
    with pytest.raises(GraphValidationError) as error:
        mode(after, False)
    assert (error.value.code, error.value.path) == ("DESIGN_PRESET_L2_EQUIPMENT_PRESENT", "<root>")
    assert after == snapshot


def test_central_inverter_add_refusal_copy_scan(builtin, bases, monkeypatch):
    allowed = {None, "valid_settings_required", "valid_strings_required", "capability_not_ready"}
    for g in bases.values():
        row = builtin.input_readiness(g)
        assert row["input_reason"] in allowed
        assert row["input_ready"] is (row["input_reason"] is None)
    with monkeypatch.context() as patch:
        patch.setattr(builtin.bridge, "MAX_DEVICES", len(bases["ground"]["inverters"]))
        assert builtin.input_readiness(bases["ground"])["input_reason"] == "capability_not_ready"


def test_x02_add_all_explicit_fallback_graph(bases):
    graph = bases["roof"]
    before = copy.deepcopy(graph)
    tool = local._load_builtin("solar-add-all-inverters")
    params = {"expected_rev": graph["rev"], "hardware": hardware(),
              "placement_strategy": "deterministic_grid"}
    intake = {"panel_groups": [{"handle": "A5", "outlines": [
        [[0, 0], [20, 0], [20, 20], [0, 20]]]}]}
    after = tool.run(graph, params, source_intake=intake)
    assert graph == before
    assert after["rev"] == graph["rev"] + 1
    assert after["inverters"]
    proof = after["extra"]["inverter_placement"]
    assert proof["engine"] == "deterministic_fallback"
    assert proof["label"] == "Deterministic fallback"
    assert proof["source_graph_sha256"] == digest(before)
    assert proof["request_sha256"] == digest(params)
    assert tool.run(graph, params, source_intake=intake) == after
    assert validate_graph(copy.deepcopy(after)) == after


def test_x02_add_all_requires_explicit_strategy_and_preserves_input(bases):
    graph = bases["roof"]
    before = copy.deepcopy(graph)
    tool = local._load_builtin("solar-add-all-inverters")
    with pytest.raises(GraphValidationError) as error:
        tool.run(graph, {"expected_rev": graph["rev"], "hardware": hardware()}, source_intake={})
    assert error.value.code == "INVALID_INVERTER_PLACEMENT_REQUEST"
    assert graph == before


def test_x02_add_all_canonical_preparation_carries_producing_attempt(bases, monkeypatch):
    import json
    import hashlib
    import deps
    import solar_project_graph as adapter
    from test_sip_r3a_graph import Memory, NOW
    memory = Memory()
    graph = bases["roof"]
    intake = {"solar_design_graph": graph, "solar_design_graph_sha256": digest(graph),
              "panel_groups": [{"handle": "A5", "outlines": [
                  [[0, 0], [20, 0], [20, 20], [0, 20]]]}]}
    raw = canonical_bytes(intake)
    context = adapter.ProjectGraphContext(memory.org, memory.project, memory.drawing,
        memory.parent.version_id, raw, hashlib.sha256(raw).hexdigest(), NOW.isoformat())
    monkeypatch.setattr(adapter, "require_project_sizing", lambda *args, **kwargs: None)
    params = {"expected_rev": graph["rev"], "hardware": hardware(),
              "placement_strategy": "deterministic_grid"}
    prepared = adapter.prepare_project_graph_commit(context, "solar-add-all-inverters", params,
        checkout=memory.lease, job_id=memory.job, attempt=2,
        tool_manifest_sha256=deps.catalog_tool_digest(solar_tools.trusted_record("solar-add-all-inverters")))
    receipt = json.loads(prepared.receipt_bytes)
    assert receipt["engine"] == "deterministic_fallback"
    assert receipt["engine_label"] == "Deterministic fallback"
    assert receipt["producing_attempt"] == {"job_id": str(memory.job), "attempt": 2}
    assert receipt["graph_sha256"] == digest(json.loads(prepared.output_intake_bytes)["solar_design_graph"])
    assert intake["solar_design_graph"] == graph


@pytest.mark.parametrize("groups", [
    [{"handle": "A5", "outlines": [[]]}],
    [{"handle": "A5", "outlines": [[[0, 0], [1, 1]]]}],
    [{"handle": "A5", "outlines": [[[0, 0], [1, 0], [1, 1]]]},
     {"handle": "0a5", "outlines": [[[0, 0], [1, 0], [1, 1]]]}],
])
def test_x02_add_all_invalid_outlines_preserve_state(bases, groups):
    graph = bases["roof"]
    before = copy.deepcopy(graph)
    tool = local._load_builtin("solar-add-all-inverters")
    with pytest.raises(GraphValidationError) as error:
        tool.run(graph, {"expected_rev": graph["rev"], "hardware": hardware(),
            "placement_strategy": "deterministic_grid"}, source_intake={"panel_groups": groups})
    assert error.value.code == "INVALID_DRAWING_CONTEXT"
    assert graph == before


def test_x02_add_all_uses_existing_rooftop_intake_without_private_group_dump(service, pinned):
    import json
    fixture = json.loads((SERVER.parent / "data/rooftop_demo.intake.json").read_text())
    assert "panel_groups" not in fixture
    graph = local._load_builtin("solar-settings").run(roof_source.empty_graph(), {
        "expected_rev": 0, "project_changes": {"zip_code": "44224"},
        "changes": {"use_l2_collectors": True, "panel_layer_contains": "Panels"}})
    graph = local._load_builtin("solar-panels-from-drawing").run(
        graph, {"expected_rev": graph["rev"]}, source_intake=fixture)
    graph = ge.sized(graph)
    import math
    import solar_panel_group_kernel as group_kernel
    recognised = group_kernel.panels_from_intake(fixture, layer_contains="Panels")
    source_panels = {item["handle"].upper(): item for item in recognised}
    memberships = {}
    for panel in graph["panels"]:
        key = group_kernel.angle_key(math.radians(panel["angle"]))
        memberships.setdefault(key, []).append(panel)
    groups = []
    for index, members in enumerate(memberships.values()):
        geometry = source_panels[members[0]["provenance"]["source_handle"].upper()]
        groups.append({"name": f"X02 fixture group {index + 1}",
            "panel_refs": [panel["id"] for panel in members], "alignment_tolerance": .5,
            "module_width_along_row": geometry["column_dim"],
            "module_height_across_row": geometry["row_dim"]})
    graph = local._load_builtin("solar-panel-groups").run(
        graph, {"expected_rev": graph["rev"], "groups": groups})
    assert all(panel["frame_ref"] is not None for panel in graph["panels"])
    frame = max(graph["frames"], key=lambda item: len(item["panel_refs"]))
    graph = pinned.run(graph, {"expected_rev": graph["rev"], "operation": "add-string",
        "ordered_panel_refs": frame["panel_refs"][:2]})
    before = copy.deepcopy(graph)
    tool = local._load_builtin("solar-add-all-inverters")
    params = {"expected_rev": graph["rev"], "hardware": hardware(),
              "placement_strategy": "deterministic_grid"}
    after = tool.run(graph, params, source_intake=fixture)
    assert graph == before
    proof = after["extra"]["inverter_placement"]
    assert proof["outline_source"] == "canonical_graph_frame_bounds"
    assert proof["engine"] == "deterministic_fallback"
    assert after["inverters"] and after["rev"] == graph["rev"] + 1
    assert tool.run(graph, params, source_intake=fixture) == after


def test_x02_canonical_frame_outlines_have_exact_bridge_unit_binding(bases):
    graph = bases["roof"]
    before = copy.deepcopy(graph)
    tool = local._load_builtin("solar-add-all-inverters")
    groups = tool._graph_panel_groups(graph)
    scale = graph["project"]["units"]["meters_per_unit"]
    frame = graph["frames"][0]
    outline = groups[0]["outlines"][0]
    assert len(outline) == 4
    angle = frame["provenance"]["row_angle"]
    import math
    cosine, sine = math.cos(angle), math.sin(angle)
    along = [scale * (x * cosine + y * sine) for x, y in outline]
    across = [scale * (-x * sine + y * cosine) for x, y in outline]
    members = [p for p in graph["panels"] if p["id"] in frame["panel_refs"]]
    projected = [p["centre"][0] * cosine + p["centre"][1] * sine for p in members]
    assert min(along) == pytest.approx(min(projected) - frame["module_width_along_row"] / 2)
    assert max(along) == pytest.approx(max(projected) + frame["module_width_along_row"] / 2)
    assert max(across) > min(across)
    graph["frames"][0]["validity"] = {"state": "stale", "reasons": ["synthetic edit"]}
    with pytest.raises(GraphValidationError) as error:
        tool._graph_panel_groups(graph)
    assert error.value.code == "SOLAR_OUTPUT_NOT_CURRENT"
    assert error.value.path == before["frames"][0]["id"]
