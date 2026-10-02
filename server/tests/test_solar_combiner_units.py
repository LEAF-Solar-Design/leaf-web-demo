"""Combiner reconstruction in drawing units, placement and routing in kernel inches."""
from copy import deepcopy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import solar_inverter_combiner as cb
import solar_inverter_cabling as cab
import solar_combiner_graph as cg
from test_solar_tool_combiners import i4, place
from test_solar_combiner_graph import HARDWARE, CREATED, minter
from test_solar_inverter_combiner import _intake, _pt, _string, _group
from test_solar_inverter_cabling import device, st

K_FT = 0.3048 / 0.0254
UNITS = {"in": 0.0254, "mm": 0.001, "cm": 0.01, "m": 1.0,
         "km": 1000.0, "ft": 0.3048, "yd": 0.9144}
OTHER_UNITS = ["mm", "cm", "m", "km", "ft", "yd"]


def centre(panel):
    points = panel["points"]
    return tuple((min(p[axis] for p in points) + max(p[axis] for p in points)) / 2
                 for axis in ("x", "y"))


def scale_point(point, scale, upper=False):
    for axis in (("X", "Y", "Z") if upper else ("x", "y", "z")):
        if axis in point:
            point[axis] *= scale
    if "LengthSquared" in point:
        point["LengthSquared"] *= scale * scale


def scale_bounds(bounds, scale):
    for point in bounds.values():
        scale_point(point, scale)


def normalized(intake, scale):
    out = deepcopy(intake)
    for panel in out["inputs"]["cadContext"]["geometry"]["panels"]:
        if "points" in panel:
            for point in panel["points"]:
                scale_point(point, scale)
    for group in out["inputs"]["panelGroups"]:
        scale_bounds(group["bounds"], scale)
    return out


@pytest.fixture(scope="module")
def site():
    return i4()


def variant(site, name):
    graph, intake, outlines = deepcopy(site)
    mpu = UNITS[name]
    scale = 0.0254 / mpu
    graph["project"]["units"]["meters_per_unit"] = mpu
    if "drawing_units" in graph["project"]["units"]:
        graph["project"]["units"]["drawing_units"] = name
    source_panels = site[1]["inputs"]["cadContext"]["geometry"]["panels"]
    source_centres = [centre(panel) if "points" in panel else None for panel in source_panels]
    inputs = intake["inputs"]
    panels = inputs["cadContext"]["geometry"]["panels"]
    for panel in panels:
        if "points" in panel:
            for point in panel["points"]:
                scale_point(point, scale)
        if panel.get("bounds"):
            scale_bounds(panel["bounds"], scale)
    for group in inputs["panelGroups"]:
        bounds = group["bounds"]
        if all(bounds[end][axis] == 0 for end in ("min", "max") for axis in ("x", "y")):
            continue
        members = [centre(panel) for source, panel, source_centre in zip(source_panels, panels, source_centres)
                   if "points" in source and "points" in panel
                   if all(bounds["min"][axis] <= source_centre[j] <= bounds["max"][axis]
                          for j, axis in enumerate(("x", "y")))]
        assert members
        for j, axis in enumerate(("x", "y")):
            bounds["min"][axis] = min(p[j] for p in members)
            bounds["max"][axis] = max(p[j] for p in members)
    for string in inputs["preBuiltStrings"]:
        for key in ("centroid", "endpointA", "endpointB"):
            scale_point(string[key], scale)
    for l2 in inputs["l2Inverters"]:
        scale_point(l2["InsertPt"], scale, upper=True)
    for l2 in inputs.get("existingL2s", []):
        if l2.get("insert"):
            scale_point(l2["insert"], scale)
    if inputs.get("alignmentLine"):
        for point in inputs["alignmentLine"]:
            scale_point(point, scale)
    for road in inputs.get("accessRoadLines", []):
        for point in road["points"] if isinstance(road, dict) else road:
            scale_point(point, scale)
    if intake["drawing"].get("extents"):
        scale_bounds(intake["drawing"]["extents"], scale)
    intake["drawing"]["metersPerUnit"] = mpu
    for group in outlines:
        group["outlines"] = [[[p[0] * scale, p[1] * scale, *p[2:]] for p in ring]
                             for ring in group["outlines"]]
    cb.reconstruct_panel_groups(intake)
    return graph, intake, outlines


def graph_place(site, hardware=None):
    return cg.place_combiners(*site, hardware=HARDWARE if hardware is None else hardware,
                             new_id=minter(), created_at=CREATED)[0]


def rectangle(handle, low, high, ylow=None, yhigh=None):
    ylow = low if ylow is None else ylow
    yhigh = high if yhigh is None else yhigh
    return {"type": "Polyline", "handle": handle, "points": [
        _pt(low, ylow), _pt(high, ylow), _pt(high, yhigh), _pt(low, yhigh)]}


def test_combiner_units_tie_witness_arithmetic():
    assert K_FT == 12.000000000000002
    assert (0.1 + 0.2) / 2 == 0.15000000000000002
    assert (0.1 * K_FT + 0.2 * K_FT) / 2 == 1.8000000000000003
    assert 0.15000000000000002 * K_FT == 1.8000000000000005
    assert (0.1 * K_FT + 0.2 * K_FT) / 2 < 0.15000000000000002 * K_FT
    assert (0.1 + 0.6) / 2 == 0.35
    assert (0.1 * K_FT + 0.6 * K_FT) / 2 == 4.200000000000001
    assert 0.35 * K_FT == 4.2
    assert (0.1 * K_FT + 0.6 * K_FT) / 2 > 0.35 * K_FT


def check_ties(intake, gid, expected, scaled):
    rebuilt = cb.reconstruct_panel_groups(intake)
    assert len(rebuilt) == 1
    assert sorted(p.center for p in rebuilt[0].panels) == expected
    with pytest.raises(cb.PlacementError) as error:
        cb.reconstruct_panel_groups(normalized(intake, K_FT))
    assert str(error.value) == f"panel group {gid}: 0 CAD panels inside its bounds, the dump counted 2"
    groups = cb._scaled_inputs(K_FT, [], [], rebuilt, None, [])[2]
    assert sorted(p.center for p in groups[0].panels) == scaled


def test_combiner_units_exact_ties_survive_in_drawing_units():
    lo, hi = 0.15000000000000002, 0.35
    intake = _intake([], [], [_group("G", (lo, lo), (hi, hi), 2)],
                     [rectangle("P1", 0.1, 0.2), rectangle("P2", 0.1, 0.6), rectangle("P3", 0.3, 0.42)])
    check_ties(intake, "G", [(lo, lo), (hi, hi)],
               [(1.8000000000000005, 1.8000000000000005), (4.2, 4.2)])


def test_combiner_units_exact_ties_survive_negative_mirror():
    lo, hi = -0.35, -0.15000000000000002
    intake = _intake([], [], [_group("N", (lo, lo), (hi, hi), 2)],
                     [rectangle("P1", -0.2, -0.1), rectangle("P2", -0.6, -0.1)])
    check_ties(intake, "N", [(lo, lo), (hi, hi)],
               [(-4.2, -4.2), (-1.8000000000000005, -1.8000000000000005)])


def test_combiner_units_module_bucket_edge_selected_in_source_units():
    panels = [rectangle("M1", 0.0, 0.1000001, 0.0, 0.05),
              rectangle("M2", 0.0, 0.1000004, 1.0, 1.05),
              rectangle("M3", 0.0, 0.1000004, 2.0, 2.05)]
    centres = [centre(p) for p in panels]
    intake = _intake([], [], [_group("M", tuple(min(p[j] for p in centres) for j in (0, 1)),
                                   tuple(max(p[j] for p in centres) for j in (0, 1)), 3)], panels)
    source = cb.reconstruct_panel_groups(intake)
    assert all((p.width_along_row, p.height_across_row) == (0.1000001, 0.05) for p in source[0].panels)
    before = cb.reconstruct_panel_groups(normalized(intake, K_FT))
    assert all((p.width_along_row, p.height_across_row) == (1.2000048000000003, 0.6000000000000014)
               for p in before[0].panels)
    after = cb._scaled_inputs(K_FT, [], [], source, None, [])[2]
    assert (0.1000001 * K_FT, 0.05 * K_FT) == (1.2000012000000002, 0.6000000000000001)
    assert all((p.width_along_row, p.height_across_row) == (1.2000012000000002, 0.6000000000000001)
               for p in after[0].panels)


def test_combiner_units_typed_conversion():
    string = cb.StringSummary()
    values = dict(l2_number=1, string_number=7, group_id="cable-7", row_index=3, col_index_min=2,
                  col_index_max=2, row_angle_rad=0.25, physical_row_key="R3", centroid=(5.0, 0.0),
                  endpoint_a=(0.0, 0.0), endpoint_b=(10.0, 0.0),
                  panels=[(0.0, 0.0), (5.0, 0.0), (10.0, 0.0)])
    for key, value in values.items():
        setattr(string, key, value)
    strings, l2s, groups, trench, roads = cb._scaled_inputs(
        K_FT, [string], [(4, (20.0, 30.0))],
        [cb.PanelGroupInput("Q", 0.5, [cb.PanelInput((100.0, 50.0), 77.0, 38.5)], True)],
        [(0.0, 0.0), (10.0, 0.0)], [[(0.0, 5.0), (10.0, 5.0)], None])
    assert strings == [string]
    assert string.centroid == (60.00000000000001, 0.0)
    assert string.endpoint_a == (0.0, 0.0)
    assert string.endpoint_b == (120.00000000000001, 0.0)
    assert string.panels == [(0.0, 0.0), (60.00000000000001, 0.0), (120.00000000000001, 0.0)]
    for key in ("l2_number", "string_number", "group_id", "row_index", "col_index_min", "col_index_max",
                "row_angle_rad", "physical_row_key"):
        assert getattr(string, key) == values[key]
    assert l2s == [(4, (240.00000000000003, 360.00000000000006))]
    group = groups[0]
    assert (group.group_id, group.row_angle_rad, group.is_synthesized_from_outline) == ("Q", 0.5, True)
    panel = group.panels[0]
    assert panel.center == (1200.0000000000002, 600.0000000000001)
    assert panel.width_along_row == 924.0000000000001
    assert panel.height_across_row == 462.00000000000006
    assert trench == [(0.0, 0.0), (120.00000000000001, 0.0)]
    assert roads == [[(0.0, 60.00000000000001), (120.00000000000001, 60.00000000000001)], None]


@pytest.mark.parametrize("value", [0, -1.0, float("nan"), float("inf"), True, "12"])
def test_combiner_units_scale_refusals(site, value):
    with pytest.raises(cb.PlacementError) as error:
        cb.place(site[1], coordinate_scale=value)
    assert str(error.value) == "coordinate_scale must be a finite positive number"


def test_combiner_units_scale_overflow_refused(site):
    with pytest.raises(cb.PlacementError) as error:
        cb.place(site[1], coordinate_scale=1e308)
    assert str(error.value) == "string 1 centroid is not finite in kernel inches"


def test_combiner_units_inch_path_never_scales(site, monkeypatch):
    def forbidden(*args):
        raise AssertionError("inch path scaled")
    monkeypatch.setattr(cb, "_scaled_inputs", forbidden)
    assert cb.place(site[1]) == cb.place(site[1], coordinate_scale=1.0)
    assert len(graph_place(site)["routes"]) == 360
    assert cg._kernel_outlines(site[2], 0.0254) is site[2]


@pytest.mark.parametrize("name", ["mm", "ft", "km"])
def test_combiner_units_kernel_feet_equals_inch(site, name):
    reference = cb.place(site[1])
    actual = cb.place(variant(site, name)[1], coordinate_scale=UNITS[name] / 0.0254)
    keys = ("L1Number", "ParentL2Number", "ServedStringIds", "InputCountUsed", "InputCountSku")
    assert len(actual["combiners"]) == len(reference["combiners"]) == 14
    assert [tuple(c[k] for k in keys) for c in actual["combiners"]] == [
        tuple(c[k] for k in keys) for c in reference["combiners"]]
    assert actual["l1ToL2Assignments"] == reference["l1ToL2Assignments"]
    for a, b in zip(actual["combiners"], reference["combiners"]):
        for axis in ("x", "y"):
            assert a["location"][axis] == pytest.approx(b["location"][axis], rel=1e-12, abs=0)
    assert len(actual["detectedAlleys"]) == len(reference["detectedAlleys"]) == 85
    for a, b in zip(actual["detectedAlleys"], reference["detectedAlleys"]):
        assert a["kind"] == b["kind"]
        assert a["width"] == pytest.approx(b["width"], rel=1e-12, abs=0)


def equivalent(actual, reference, total, feeder_total):
    for collection in ("inverters", "strings", "routes"):
        assert [r["id"] for r in actual[collection]] == [r["id"] for r in reference[collection]]
    assert [s["inverter_ref"] for s in actual["strings"]] == [s["inverter_ref"] for s in reference["strings"]]
    assert sum(not i["is_l2"] for i in actual["inverters"]) == 14
    assert len(actual["routes"]) == 360
    assert sorted({r["route_kind"] for r in actual["routes"]}) == ["end homerun", "feeder", "start homerun"]
    for a, b in zip(actual["inverters"], reference["inverters"]):
        for x, y in zip(a["position"], b["position"]):
            assert x == pytest.approx(y, rel=1e-12, abs=0)
    for a, b in zip(actual["routes"], reference["routes"]):
        assert tuple(a[k] for k in ("from_ref", "to_ref", "route_kind")) == tuple(
            b[k] for k in ("from_ref", "to_ref", "route_kind"))
        assert a["length_ft"] == pytest.approx(b["length_ft"], rel=1e-9, abs=0)
    assert round(sum(r["length_ft"] for r in actual["routes"]), 6) == total
    assert round(sum(r["length_ft"] for r in actual["routes"] if r["route_kind"] == "feeder"), 6) == feeder_total


@pytest.mark.parametrize("name", OTHER_UNITS)
def test_combiner_units_graph_equivalence(site, name):
    equivalent(graph_place(variant(site, name)), graph_place(site), 24198.287070, 2647.891417)


def with_roads(site):
    out = deepcopy(site)
    inputs = out[1]["inputs"]
    points = [p for panel in inputs["cadContext"]["geometry"]["panels"]
              if "points" in panel for p in panel["points"]]
    min_x, max_x = min(p["x"] for p in points), max(p["x"] for p in points)
    min_y, max_y = min(p["y"] for p in points), max(p["y"] for p in points)
    inputs["alignmentLine"] = [_pt(min_x - 120.0, (min_y + max_y) / 2),
                               _pt(max_x + 120.0, (min_y + max_y) / 2)]
    inputs["accessRoadLines"] = [{"points": [_pt(min_x - 240.0, min_y - 60.0),
                                           _pt(max_x + 240.0, min_y - 60.0)]}]
    return out


@pytest.mark.parametrize("name", OTHER_UNITS)
def test_combiner_units_graph_equivalence_with_roads(site, name):
    roads = with_roads(site)
    reference = graph_place(roads)
    assert reference != graph_place(site)
    equivalent(graph_place(variant(roads, name)), reference, 27043.189911, 2728.678229)


def test_combiner_units_inputs_not_mutated(site):
    inputs = variant(site, "ft")
    hardware = deepcopy(HARDWARE)
    snapshot = deepcopy((inputs, hardware))
    graph_place(inputs, hardware)
    assert (inputs, hardware) == snapshot


def matcher_state(k, source_x, offset=0.0, duplicate=False):
    x = (source_x + offset) * k
    rows = [device((x, 2.0 * k), "inverter")]
    if duplicate:
        rows.append(device(((source_x + 0.4e-6) * k, 2.0 * k), "inverter"))
    return {"rows": {"device": rows}, "geometry": {"strings": [
        {"string": "S7", "vertices": [[x, 2.0 * k], [3.0 * k, 2.0 * k]]}]}}


def test_combiner_units_matchers_compare_in_source_units():
    for k, source_x, intake_x, offset in [(K_FT, 1.0, 1.0, 0.0), (K_FT, 1.0, 1.0, 0.5e-6),
                                         (K_FT, 1.0 + 2e-7, 1.0 - 2e-7, 0.0), (1.0, 1.0, 1.0, 0.0)]:
        intake = _intake([_string(7, 1, 0, 0, (intake_x, 2.0), (3.0, 2.0))], [(4, (intake_x, 2.0))])
        new = matcher_state(k, source_x, offset)
        if k == 1.0:
            cab._number_l2_from_intake(new, intake)
            assert cab._string_ids(new, intake) == {7: "S7"}
        else:
            cab._number_l2_from_intake(new, intake, k)
            assert cab._string_ids(new, intake, k) == {7: "S7"}
        assert new["rows"]["device"][0]["_number"] == 4
    intake = _intake([_string(7, 1, 0, 0, (1.0, 2.0), (3.0, 2.0))], [(4, (1.0, 2.0))])
    for function, message in [(cab._number_l2_from_intake, "the intake's L2 4 matches no single L2 device of the state"),
                              (cab._string_ids, "the intake's string 7 matches no single string of the state")]:
        with pytest.raises(cab.InverterCablingError) as error:
            function(matcher_state(K_FT, 1.0, 2e-6), intake, K_FT)
        assert str(error.value) == message
    with pytest.raises(cab.InverterCablingError) as error:
        cab._number_l2_from_intake(matcher_state(K_FT, 1.0, duplicate=True), intake, K_FT)
    assert str(error.value) == "the intake's L2 4 matches no single L2 device of the state"


@pytest.mark.parametrize("name", ["in"] + OTHER_UNITS)
def test_combiner_units_binding_and_matchers_share_one_projection(site, name):
    graph, intake, outlines = variant(site, name)
    g = cg.validate_graph(graph)
    mpu = cg.legacy._meters_per_unit(g)
    scale = mpu / 0.0254
    state, _ = cg.rb.state_from_graph(g, metres_per_unit=0.0254)
    graph_l2 = sorted(cg._du(i["position"], mpu) for i in g["inverters"] if i["is_l2"])
    state_l2 = sorted(cab._source_xy(cab._xy(r["position"]), scale)
                      for r in state["rows"]["device"] if cab.dev._is_l2(r))
    assert graph_l2 == state_l2
    assert len(graph_l2) == 8
    graph_strings = sorted((cg._du(s["route"][0], mpu), cg._du(s["route"][-1], mpu))
                           for s in g["strings"] if s["route"])
    state_strings = sorted((cab._source_xy(cab.dev._finite_xy(x["vertices"][0], "v"), scale),
                            cab._source_xy(cab.dev._finite_xy(x["vertices"][-1], "v"), scale))
                           for x in state["geometry"]["strings"]
                           if x.get("vertices") and x.get("string") is not None)
    assert graph_strings == state_strings
    assert len(graph_strings) == 173


def test_combiner_units_epsilon_edge_refuses_at_binding_l2(site):
    graph, intake, outlines = variant(site, "mm")
    inverter = next(i for i in graph["inverters"] if i["is_l2"] and i["number"] == 2)
    item = next(i for i in intake["inputs"]["l2Inverters"] if i["Number"] == 2)
    item["InsertPt"]["X"] = inverter["position"][0] / 0.001
    item["InsertPt"]["Y"] = 40032.88581574792
    with pytest.raises(cg.CombinerGraphError) as error:
        graph_place((graph, intake, outlines))
    assert str(error.value) == "COMBINER_INTAKE_L2_MISMATCH"


def test_combiner_units_epsilon_edge_refuses_at_binding_string(site):
    graph, intake, outlines = variant(site, "mm")
    binding = cg._bind(cg.validate_graph(graph), intake)[0]
    string = next(s for s in graph["strings"] if s["id"] == binding["strings"]["2"])
    item = next(s for s in intake["inputs"]["preBuiltStrings"] if s["StringNumber"] == 2)
    item["endpointA"]["x"] = string["route"][0][0] / 0.001
    item["endpointA"]["y"] = 82234.09905797365
    with pytest.raises(cg.CombinerGraphError) as error:
        graph_place((graph, intake, outlines))
    assert str(error.value) == "COMBINER_INTAKE_STRING_MISMATCH"


@pytest.mark.parametrize("value", [10**400, -(10**400), 2**1024])
def test_combiner_units_huge_integer_scale_refused(site, value):
    snapshot = deepcopy(site[1])
    with pytest.raises(cb.PlacementError) as error:
        cb.place(site[1], coordinate_scale=value)
    assert str(error.value) == "coordinate_scale must be a finite positive number"
    assert site[1] == snapshot


@pytest.mark.parametrize("name", ["mm", "ft"])
def test_combiner_units_route_points_match_inch(site, name):
    reference = graph_place(site)
    actual = graph_place(variant(site, name))
    reference_routes = {r["id"]: r for r in reference["routes"]}
    assert {r["id"] for r in actual["routes"]} == set(reference_routes)
    for route in actual["routes"]:
        expected = reference_routes[route["id"]]
        assert len(route["points"]) == len(expected["points"])
        for point, expected_point in zip(route["points"], expected["points"]):
            assert len(point) == len(expected_point)
            for coordinate, expected_coordinate in zip(point, expected_point):
                assert coordinate == pytest.approx(expected_coordinate, rel=0, abs=1e-9)
