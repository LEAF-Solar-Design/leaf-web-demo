"""Feeders use the exact stored footprints of converted Ground frames."""
import copy
import json

import pytest

import test_solar_tool_solar_feeders as t
import test_solar_ground_equipment as ge
from test_solar_tool_solar_feeders import memory_transport, rooftop  # noqa: F401
from test_solar_ground_equipment import service, pinned  # noqa: F401
from test_w1_design_graph import graph  # noqa: F401

READY = {"input_ready": True, "input_reason": None}
F_SHA = "9ca79e124fc98e024b65293fe28302dacd2ccbad7087012a1e8799a5b1ba9ac7"
SUCCESS_SHA = "f3bf59e27d2dab5fd246f1a7cff37a1e715e3856429b55d36d5e5a515e727b5c"
RAIL_SHA = "b2caa7915765e0014ea05f6bc3bb31214e4a0c8ee8087386277edaa4b38387c8"
GROUPS = [{"outlines": [[[1.0, 0.0], [-1.0, 0.0], [-1.0, 6.0], [1.0, 6.0]],
                         [[4.5, 0.0], [3.5, 0.0], [3.5, 10.0], [4.5, 10.0]]]}]


def central_params(g):
    return {"expected_rev": g["rev"], "point": [2.0, 12.0],
            "hardware": {"model": "Central 100", "max_dc_voltage": 1500, "max_ac_power_kw": 100,
                         "mppt_count": 1, "total_dc_inputs": 1, "collector_capacity": 2}}


def mode(g):
    return t.local._load_builtin("solar-settings").run(
        g, {"expected_rev": g["rev"], "changes": {"use_l2_collectors": True}})


@pytest.fixture
def ground_chain(graph, service, pinned):
    converted = ge.converted(graph)
    sized = ge.sized(converted)
    strung = ge.strung(sized, pinned)
    equipped = ge.equip(strung)
    ground = mode(equipped)
    f = t.local._load_builtin("solar-central-inverter-add").run(ground, central_params(ground))
    assert f["rev"] == 7 and t.digest(f) == F_SHA
    return {"converted": converted, "sized": sized, "strung": strung,
            "equipped": equipped, "ground": ground, "F": f}


def refusal(g, params, code, path, reason=None):
    before = t.digest(g)
    t.refused(lambda: t.builtin().run(g, params), code, path)
    assert t.digest(g) == before
    if reason is not None:
        assert t.builtin().input_readiness(g) == {"input_ready": False, "input_reason": reason}


def drop_outline(g, index=0):
    del g["frames"][index]["extra"]["ground_outline"]


def feeder(g):
    rows = [r for r in g["routes"] if r["route_kind"] == "feeder"]
    assert len(rows) == 1
    return rows[0]


@pytest.mark.parametrize("stage,mode_on,reason", [
    ("converted", False, "valid_settings_required"),
    ("sized", True, "equipment_assignment_required"),
    ("strung", True, "equipment_assignment_required"),
    ("equipped", False, "valid_settings_required"),
    ("ground", False, "string_collectors_required"),
])
def test_feeders_ground_prerequisites(ground_chain, stage, mode_on, reason):
    g = ground_chain[stage]
    if mode_on:
        g = mode(g)
    refusal(g, {"expected_rev": g["rev"]}, reason.upper(), "<root>", reason)


def test_feeders_ground_success(ground_chain):
    f = ground_chain["F"]
    before = t.digest(f)
    out = t.builtin().run(f, {"expected_rev": 7})
    assert out["rev"] == 8 and t.digest(out) == SUCCESS_SHA
    route = feeder(out)
    assert route["from_ref"] == next(i["id"] for i in f["inverters"] if not i["is_l2"])
    assert route["to_ref"] == next(i["id"] for i in f["inverters"] if i["is_l2"])
    assert route["length_ft"] == pytest.approx(49.21259842519686, abs=1e-9, rel=0)
    assert len(route["points"]) == 3
    for point, expected in zip(route["points"], [[5, 0], [5, 12], [2, 12]]):
        assert point == pytest.approx(expected, abs=1e-9, rel=0)
    assert {key for key in f.keys() | out.keys() if f.get(key) != out.get(key)} == {
        "inverters", "parent_rev", "rev", "routes"}
    for key in ("frames", "panels", "strings"):
        assert out[key] == f[key]
    assert t.builtin().input_readiness(f) == READY
    assert t.digest(f) == before


@pytest.mark.parametrize("source_intake", [
    {}, None, {"panel_groups": []}, {"panel_groups": "junk"},
    {"panel_groups": [{"outlines": [[[0, 0], [9, 0], [9, 9], [0, 9]]]}]}, [1],
])
def test_feeders_ground_intake_not_read(ground_chain, source_intake):
    f = ground_chain["F"]
    before = t.digest(f)
    out = t.builtin().run(f, {"expected_rev": 7}, source_intake=source_intake)
    assert t.digest(out) == SUCCESS_SHA and t.digest(f) == before


@pytest.mark.parametrize("edit,index,suffix", [
    ("drop", 0, ""), ("units", 0, ""), ("three", 0, ".points"),
    ("degenerate", 0, ".points"), ("shift", 0, ".points"), ("drop", 1, ""),
    ("extra", 0, ""), ("both", 0, ""),
])
def test_feeders_ground_footprint_refusals(ground_chain, edit, index, suffix):
    f = copy.deepcopy(ground_chain["F"])
    outline = f["frames"][index]["extra"]["ground_outline"]
    if edit in ("drop", "both"):
        drop_outline(f, index)
        if edit == "both":
            drop_outline(f, 1)
    elif edit == "units":
        outline["point_units"] = "m"
    elif edit == "three":
        outline["points"] = [[0, 0], [1, 0], [1, 1]]
    elif edit == "degenerate":
        outline["points"] = [[0, 0]] * 4
    elif edit == "shift":
        outline["points"] = [[x + 1, y] for x, y in outline["points"]]
    else:
        outline["note"] = "x"
    refusal(f, {"expected_rev": 7}, "INVALID_DRAWING_CONTEXT",
            f"frames[{index}].extra.ground_outline{suffix}", "invalid_drawing_context")


@pytest.mark.parametrize("stage,reason", [("equipped", "valid_settings_required"),
                                         ("ground", "string_collectors_required")])
def test_feeders_ground_prerequisites_precede_footprints(ground_chain, stage, reason):
    g = copy.deepcopy(ground_chain[stage])
    drop_outline(g)
    refusal(g, {"expected_rev": g["rev"]}, reason.upper(), "<root>", reason)


@pytest.mark.parametrize("params,code", [({"expected_rev": 0}, "STALE_GRAPH_REVISION"),
                                       ({"expected_rev": 7, "x": 1}, "INVALID_FEEDER_REQUEST")])
def test_feeders_ground_request_precedes_footprints(ground_chain, params, code):
    f = copy.deepcopy(ground_chain["F"])
    drop_outline(f)
    refusal(f, params, code, "<root>", "invalid_drawing_context")


def test_feeders_ground_groups_shape_and_purity(ground_chain):
    f = ground_chain["F"]
    before = t.digest(f)
    groups = t.builtin().ground_panel_groups(f)
    assert groups == GROUPS
    assert all(type(p) is list and len(p) == 2 for ring in groups[0]["outlines"] for p in ring)
    groups[0]["outlines"][0][0][0] = 999
    assert t.builtin().ground_panel_groups(f) == GROUPS and t.digest(f) == before
    empty = {"frames": []}
    before = t.digest(empty)
    t.refused(lambda: t.builtin().ground_panel_groups(empty), "INVALID_DRAWING_CONTEXT", "frames")
    assert t.digest(empty) == before


def test_feeders_ground_readiness_never_routes(ground_chain, monkeypatch):
    def fail(*args):
        raise AssertionError("readiness routed")
    monkeypatch.setattr(t.fg, "route_feeders", fail)
    f = ground_chain["F"]
    before = t.digest(f)
    assert t.builtin().input_readiness(f) == READY
    assert t.digest(f) == before
    broken = copy.deepcopy(f)
    drop_outline(broken)
    before = t.digest(broken)
    assert t.builtin().input_readiness(broken) == {
        "input_ready": False, "input_reason": "invalid_drawing_context"}
    assert t.digest(broken) == before


@pytest.mark.parametrize("code", ["FEEDER_POSTCONDITION_FAILED", "BRIDGE_INVALID_REQUEST"])
def test_feeders_ground_kernel_refusals_map(ground_chain, monkeypatch, code):
    def fail(*args):
        if code == "FEEDER_POSTCONDITION_FAILED":
            t.fg._fail(code)
        raise t.fg.rb.ElectricalBridgeError(code)
    monkeypatch.setattr(t.fg, "route_feeders", fail)
    f = ground_chain["F"]
    refusal(f, {"expected_rev": 7}, "CAPABILITY_NOT_READY", code)
    assert t.builtin().input_readiness(f) == READY


def test_feeders_ground_rerun(ground_chain):
    f = ground_chain["F"]
    before = t.digest(f)
    first = t.builtin().run(f, {"expected_rev": 7})
    again = t.builtin().run(first, {"expected_rev": 8})
    assert again["rev"] == 9
    for key in ("from_ref", "to_ref", "length_ft", "points"):
        assert feeder(first)[key] == feeder(again)[key]
    assert t.digest(t.builtin().run(f, {"expected_rev": 7})) == t.digest(first)
    assert t.digest(f) == before


def test_feeders_ground_roof_never_reads_footprints(rooftop, monkeypatch):
    def fail(*args):
        raise AssertionError("Rooftop read a Ground footprint")
    monkeypatch.setattr(t.builtin().ground_outlines, "frame_outline", fail)
    g, groups = rooftop
    before = t.digest(g)
    out = t.builtin().run(g, {"expected_rev": 1}, source_intake={"panel_groups": groups})
    assert t.digest(out) == t.EXPECTED_SHA
    assert t.builtin().input_readiness(g) == READY and t.digest(g) == before


def walk(graph, pinned, monkeypatch, through=8):
    g = ge.ground_base(graph)
    g["project"]["zip_code"] = ge.ZIP
    backend = t.stored(monkeypatch, g, {})
    ge.ph.publish_physical_state(backend, t.TENANT, "solar", ge.small_doc())
    real_load = t.local._load_builtin
    monkeypatch.setattr(t.local, "_load_builtin",
                        lambda tool: pinned if tool == "solar-string-add" else real_load(tool))
    steps = [("solar-trackers-to-panel-groups", lambda h: {"expected_rev": 0}),
             ("solar-size-strings", ge.size_params),
             ("solar-string-add", lambda h: {"operation": "add-string", "expected_rev": h["rev"],
                                            "ordered_panel_refs": ge.slots(h)[:3]}),
             ("solar-string-add", lambda h: {"operation": "add-string", "expected_rev": h["rev"],
                                            "ordered_panel_refs": ge.slots(h)[3:5]}),
             (ge.EQUIPMENT, ge.equipment_params),
             ("solar-settings", lambda h: {"expected_rev": h["rev"],
                                           "changes": {"use_l2_collectors": True}}),
             ("solar-central-inverter-add", central_params),
             (t.TOOL, lambda h: {"expected_rev": h["rev"]})]
    receipt = None
    for version, (tool, make) in enumerate(steps[:through], 1):
        head = t.head_graph(backend)
        params = make(head)
        job = f"ground-feeders-{version}"
        if tool == t.TOOL:
            assert t.builtin().input_readiness(head) == READY
        receipt = ge.dispatch(backend, tool, params, version, job)
        assert receipt["new_version"] == {"drawing_id": "solar", "version": version + 1, "parent": version}
        assert ge.prove(backend, receipt, tool, params, version, job)["execution_mode"] == "local_graph_commit"
        _, key = t.store.resolve_version(backend, t.TENANT, "solar", version + 1)
        assert "panel_groups" not in json.loads(backend.get(key))
    return backend, receipt


def test_feeders_ground_rail(graph, service, pinned, monkeypatch):
    backend, receipt = walk(graph, pinned, monkeypatch)
    out = t.head_graph(backend)
    assert out["rev"] == 8 and t.digest(out) == RAIL_SHA
    assert feeder(out)["length_ft"] == pytest.approx(49.21259842519686, abs=1e-9, rel=0)
    assert t.latest(backend) == 9
    replay = ge.dispatch(backend, t.TOOL, {"expected_rev": 7}, 8, "ground-feeders-8")
    assert replay["new_version"] == receipt["new_version"] and replay["replayed"] is True
    assert t.latest(backend) == 9 and t.head_graph(backend) == out


def test_feeders_ground_rail_refusal_publishes_nothing(ground_chain, graph, pinned, monkeypatch):
    rail, _ = walk(graph, pinned, monkeypatch, through=7)
    assert t.latest(rail) == 8 and t.head_graph(rail)["rev"] == 7
    broken = copy.deepcopy(ground_chain["F"])
    drop_outline(broken)
    before = t.digest(broken)
    backend = t.stored(monkeypatch, broken, {})
    t.refused(lambda: t.dispatch(backend, {"expected_rev": 7}),
              "INVALID_DRAWING_CONTEXT", "frames[0].extra.ground_outline")
    assert t.digest(broken) == before
    assert t.builtin().input_readiness(broken) == {
        "input_ready": False, "input_reason": "invalid_drawing_context"}
    assert t.latest(backend) == 1 and t.head_graph(backend) == broken


def test_feeders_ground_declaration_unchanged():
    declaration = t.solar_tools.get(t.TOOL)
    assert declaration["readiness"] == {"kind": "hook"}
    assert declaration["trusted_inputs"] == ["source_intake"]
    assert (declaration["family"], declaration["wave"], declaration["order"]) == ("routing", 3, 60)
