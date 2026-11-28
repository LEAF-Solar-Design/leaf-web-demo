"""Drawing-unit contract for the catalog string-target builtin."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import itertools
import json
import math
import sys
from collections import Counter
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = SERVER_DIR.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import tool_validate  # noqa: E402


def _load_string_panels():
    path = SERVER_DIR / "builtins" / "string_panels.py"
    spec = importlib.util.spec_from_file_location("string_panels_tool", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


string_panels = _load_string_panels()
INCH_DIGEST = "ea1d6371cf45ad84b99e7b51b3f4cd18c0ce1e973b8a986ad3b7b6a3d0b8f9f9"
PARAM_ERROR = "meters_per_unit must be a finite number greater than 0 and at most 1000"
GRAPH_ERROR = (
    "the drawing's Solar design does not declare a usable meters_per_unit; "
    "pass meters_per_unit"
)
OVERFLOW_ERROR = "drawing coordinates are not finite at this meters_per_unit"
SCALES = [(0.0254, 1.0), (25.4, 0.001), (1 / 12, 0.3048),
          (2.54, 0.01), (0.0254, 1)]
DESCRIPTION = (
    "Metres per drawing unit: inches 0.0254, metres 1.0, millimetres 0.001, "
    "feet 0.3048. Leave empty to use the units this drawing's Solar design "
    "declares, or inches when it declares none. Lengths are returned in feet; "
    "coordinates stay in drawing units."
)


def scaled(intake, f):
    out = copy.deepcopy(intake)
    for pl in out["polylines"]:
        pl["pts"] = [[c * f for c in pt] for pt in pl["pts"]]
    return out


def with_graph(intake, graph):
    out = copy.deepcopy(intake)
    out["solar_design_graph"] = graph
    return out


def g(mpu):
    return {"project": {"units": {"drawing_units": "x", "meters_per_unit": mpu}}}


def digest(pair):
    return hashlib.sha256(
        json.dumps(pair, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def run(intake, params):
    # Enforce immutability of the intake and of the params on every successful and refused call.
    before = copy.deepcopy(intake)
    params_before = copy.deepcopy(params)
    try:
        return string_panels.run(intake, params)
    finally:
        assert intake == before
        assert params == params_before


def rect(x, w, h, f=1.0):
    pts = [[x - w / 2, -h / 2], [x + w / 2, -h / 2], [x + w / 2, h / 2], [x - w / 2, h / 2]]
    return {"layer": "Panels", "closed": True, "pts": [[c * f for c in pt] for pt in pts]}


def assert_equals_baseline(pair, baseline):
    result, base = pair[0], baseline[0]
    assert [s["length_ft"] for s in result["strings"]] == [
        s["length_ft"] for s in base["strings"]]
    assert [s["modules"] for s in result["strings"]] == [
        s["modules"] for s in base["strings"]]
    assert result["stats"] == base["stats"]


def assert_refused(intake, params, message):
    with pytest.raises(ValueError) as exc:
        run(intake, params)
    assert str(exc.value) == message


@pytest.fixture(scope="module")
def intake():
    return json.loads(
        (REPO_DIR / "data" / "rooftop_demo.intake.json").read_text(encoding="utf-8")
    )


@pytest.fixture(scope="module")
def baseline(intake):
    return run(intake, {})


@pytest.mark.parametrize("minimum,maximum", [(12, 14), (11, 14), (10, 13)])
def test_constrained_stringing_covers_full_rooftop(intake, minimum, maximum):
    from statistics import median
    from solar_string_partition import DEFAULT_STATE_BUDGET

    panels = [panel for panel in intake["polylines"]
              if panel.get("layer") == "Panels" and panel.get("closed") and panel.get("pts")]
    centres = [string_panels._centroid(panel) for panel in panels]
    rounded = [tuple(round(value, 3) for value in centre) for centre in centres]
    expected = Counter(rounded)
    banks = string_panels._cluster_banks(centres, string_panels.DEFAULTS["cluster_radius_factor"])
    bank_for_point, max_step = {}, {}
    for bank_id, bank in enumerate(banks):
        sides = []
        for index in bank:
            bank_for_point[rounded[index]] = bank_id
            points = panels[index]["pts"]
            sides.append(max(math.dist(left[:2], right[:2])
                             for left, right in zip(points, points[1:] + points[:1])))
        max_step[bank_id] = 1.5 * median(sides)
    result, _ = run(intake, {"min_modules_per_string": minimum,
                             "max_modules_per_string": maximum})
    assert result["planning"]["status"] == "complete"
    assert result["planning"]["searched_states"] <= DEFAULT_STATE_BUDGET == 50_000
    assert sum(expected.values()) == len(expected) == 2345
    assert Counter(tuple(point) for string in result["strings"] for point in string["pts"]) == expected
    for string in result["strings"]:
        assert minimum <= string["modules"] == len(string["pts"]) <= maximum
        path = [tuple(point) for point in string["pts"]]
        bank_id = bank_for_point[path[0]]
        assert all(bank_for_point[point] == bank_id for point in path)
        # Allow for coordinate rounding in the output.
        assert all(math.dist(left, right) <= max_step[bank_id] + 0.002
                   for left, right in zip(path, path[1:]))


def test_string_panels_units_inch_continuity_result(intake, baseline):
    for drawing, params in [
        (intake, {}), (intake, {"meters_per_unit": 0.0254}),
        (intake, {"meters_per_unit": None}),
        (with_graph(intake, g(0.0254)), {}),
        (with_graph(intake, None), {}),
    ]:
        assert digest(run(drawing, params)) == INCH_DIGEST
    result = baseline[0]
    assert result["stats"]["bank_count"] == 11
    assert result["stats"]["string_count"] == 169
    assert result["stats"]["full_string_count"] == 20
    assert result["stats"]["total_wire_ft"] == 13916.3
    assert result["strings"][0]["length_ft"] == 94.2
    assert result["strings"][0]["modules"] == 15


@pytest.mark.parametrize("f,mpu", SCALES)
def test_string_panels_units_explicit_scale_matches_inch(intake, baseline, f, mpu):
    pair = run(scaled(intake, f), {"meters_per_unit": mpu})
    assert_equals_baseline(pair, baseline)
    for string, base in zip(pair[0]["strings"], baseline[0]["strings"]):
        assert len(string["pts"]) == len(base["pts"])
        for pt, pb in zip(string["pts"], base["pts"]):
            assert len(pt) == 2
            assert all(abs(c - cb * f) <= 0.00100001 * f
                       for c, cb in zip(pt, pb))


def test_string_panels_units_today_was_wrong(intake):
    metres = run(scaled(intake, 0.0254), {"meters_per_unit": 0.0254})[0]
    assert metres["stats"]["total_wire_ft"] == 352.8
    millimetres = run(scaled(intake, 25.4), {"meters_per_unit": 0.0254})[0]
    assert millimetres["stats"]["string_count"] == 2345
    assert millimetres["stats"]["total_wire_ft"] == 0.0


@pytest.mark.parametrize("f,mpu", SCALES[:4])
def test_string_panels_units_graph_scale_used_when_param_absent(
        intake, baseline, f, mpu):
    drawing = with_graph(scaled(intake, f), g(mpu))
    explicit = run(scaled(intake, f), {"meters_per_unit": mpu})
    for params in ({}, {"meters_per_unit": None}):
        pair = run(drawing, params)
        assert_equals_baseline(pair, baseline)
        assert digest(pair) == digest(explicit)


@pytest.mark.parametrize("f,mpu", SCALES)
def test_string_panels_units_param_beats_graph(intake, baseline, f, mpu):
    pair = run(with_graph(scaled(intake, f), g(123.0)), {"meters_per_unit": mpu})
    assert_equals_baseline(pair, baseline)


@pytest.mark.parametrize("value", [
    True, False, "1", 0, 0.0, -1, float("nan"), float("inf"), -float("inf"),
    1001, 1000.0000001, [1], {}, 10 ** 400,
])
def test_string_panels_units_bad_param_refused(intake, value):
    for drawing in (intake, with_graph(intake, g(0.0254))):
        assert_refused(drawing, {"meters_per_unit": value}, PARAM_ERROR)


@pytest.mark.parametrize("graph", [
    {}, {"project": None}, {"project": {}}, {"project": {"units": None}},
    {"project": {"units": {}}}, g(None), g(True), g(0), g(-1.0), g("1"),
    g(float("nan")), g(float("inf")), g(1000.0000001), [], "x", 5, False,
    {"project": []}, {"project": {"units": [1.0]}},
])
def test_string_panels_units_bad_graph_refused(intake, graph):
    drawing = with_graph(intake, graph)
    assert_refused(drawing, {}, GRAPH_ERROR)
    assert digest(run(drawing, {"meters_per_unit": 0.0254})) == INCH_DIGEST


def test_string_panels_units_bounds_accepted(intake):
    for value in (1000.0, 1000, 1.0):
        stats = run(intake, {"meters_per_unit": value})[0]["stats"]
        assert stats["string_count"] == 2345
        assert stats["bank_count"] == 2345
        assert stats["total_wire_ft"] == 0.0
    tiny = run(intake, {"meters_per_unit": 5e-324})
    assert tiny[0]["stats"]["string_count"] == 169
    assert tiny[0]["stats"]["bank_count"] == 11
    # Not the inch reading: every length collapses and every point returns to the origin.
    assert tiny[0]["stats"]["total_wire_ft"] == 0.0
    assert tiny[0]["strings"][0]["pts"][0] == [0.0, 0.0]
    assert digest(tiny) != INCH_DIGEST
    assert run(with_graph(intake, g(1000)), {})[0]["stats"]["string_count"] == 2345


def test_string_panels_units_overflow_refused(intake):
    drawing = copy.deepcopy(intake)
    first = next(pl for pl in drawing["polylines"]
                 if pl.get("layer") == "Panels" and pl.get("closed"))
    first["pts"][0][0] = 1e308
    assert_refused(drawing, {"meters_per_unit": 1000}, OVERFLOW_ERROR)
    assert run(drawing, {})[0]["stats"]["string_count"] == 171


def test_string_panels_units_result_overflow_refused():
    # The scaled coordinates are finite (about 0.0017 inch); the point overflows on the way back
    # to drawing units, so only the check on the returned points can refuse this drawing.
    x = 1.7e308
    drawing = {"polylines": [{"layer": "Panels", "closed": True,
                              "pts": [[x, 0.0], [x, 1.0], [x, 1.0], [x, 0.0]]}]}
    assert x * (2.54e-313 / 0.0254) < 0.01
    assert_refused(drawing, {"meters_per_unit": 2.54e-313}, OVERFLOW_ERROR)
    result = run(drawing, {"meters_per_unit": 2.54e-312})[0]
    assert result["stats"]["string_count"] == 1
    assert len(result["strings"][0]["pts"]) == 1
    assert all(math.isfinite(c) for c in result["strings"][0]["pts"][0])


def test_string_panels_units_cluster_threshold_tie_is_a_declared_limit():
    # DECLARED LIMIT, not a guarantee. Two panels exactly 200 inches apart sit on the inclusive
    # neighbour window. A feet drawing stores 200/12, the scale is 12.000000000000002, and the
    # product lands one float step outside the window, so bank counts disagree at the tie.
    # Continuity still prevents a 100-module step even within a connected bank.
    def summary(drawing, params):
        result = run(drawing, params)[0]
        return (result["stats"]["bank_count"], [s["modules"] for s in result["strings"]],
                [s["length_ft"] for s in result["strings"]])

    feet = {"meters_per_unit": 0.3048}
    assert summary({"polylines": [rect(0, 2, 1), rect(200, 2, 1)]}, {}) == (1, [1, 1], [0.0, 0.0])
    assert summary({"polylines": [rect(0, 2, 1, 1 / 12), rect(200, 2, 1, 1 / 12)]}, feet) == (
        2, [1, 1], [0.0, 0.0])
    assert summary({"polylines": [rect(0, 2, 1), rect(199, 2, 1)]}, {}) == (1, [1, 1], [0.0, 0.0])
    assert summary({"polylines": [rect(0, 2, 1, 1 / 12), rect(199, 2, 1, 1 / 12)]}, feet) == (
        1, [1, 1], [0.0, 0.0])


def test_string_panels_units_intake_not_mutated(intake):
    drawing = scaled(intake, 0.0254)
    before = copy.deepcopy(drawing)
    run(drawing, {"meters_per_unit": 1.0})
    assert drawing == before
    assert_refused(drawing, {"meters_per_unit": True}, PARAM_ERROR)
    assert drawing == before
    drawing = with_graph(drawing, g(1.0))
    before = copy.deepcopy(drawing)
    run(drawing, {})
    assert drawing == before
    drawing = with_graph(drawing, {})
    before = copy.deepcopy(drawing)
    assert_refused(drawing, {}, GRAPH_ERROR)
    assert drawing == before


def test_string_panels_units_defaults_unchanged():
    assert string_panels.DEFAULTS == {
        "module_model": "Q.Peak DUO XL-G11.7 / 425", "voc": 48.5,
        "temp_coeff_pct_per_c": -0.27, "design_min_temp_c": -25.0,
        "max_system_voltage": 1000.0, "panel_layer": "Panels",
        "cluster_radius_factor": 3.0,
    }


def test_string_panels_units_catalog_schema():
    catalog = json.loads((SERVER_DIR / "catalog_tools.json").read_text(encoding="utf-8"))
    record = next(t for t in catalog["tools"] if t["name"] == "autofill-string-targets")
    assert record["params"]["properties"]["meters_per_unit"] == {
        "type": ["number", "null"], "exclusiveMinimum": 0,
        "maximum": 1000.0, "description": DESCRIPTION,
    }
    assert "meters_per_unit" not in record["default_params"]
    for params in ({}, {"meters_per_unit": None},
                   *({"meters_per_unit": v} for v in (1.0, 1, 1000))):
        assert tool_validate.validate_params(record, params) == []
    for value in (0, -1, "1", True, 1001):
        errors = tool_validate.validate_params(record, {"meters_per_unit": value})
        assert len(errors) == 1
        assert errors[0].startswith("meters_per_unit:")


def test_string_panels_units_no_panels_message_unchanged():
    for drawing in (None, {}):
        assert_refused(drawing, {}, "no closed polylines found on layer 'Panels'")


def test_string_panels_continuity_inside_connected_bank():
    # A full lower row connects both sides of the upper row's empty slot.
    centres = [(80 * x, 0) for x in range(11)] + [(80 * x, 40) for x in (0, 1, 3, 4)]
    drawing = {"polylines": [{"layer": "Panels", "closed": True,
        "pts": [[x - 38.5, y - 19.25], [x + 38.5, y - 19.25],
                [x + 38.5, y + 19.25], [x - 38.5, y + 19.25]]}
        for x, y in centres]}
    result, _ = run(drawing, {})
    assert result["stats"]["bank_count"] == 1
    points = [tuple(p) for string in result["strings"] for p in string["pts"]]
    assert sorted(points) == sorted(centres)
    assert len(points) == len(set(points)) == 15
    assert all(1 <= string["modules"] <= 18 for string in result["strings"])
    assert all(math.dist(a, b) <= 1.5 * 77 for string in result["strings"]
               for a, b in zip(string["pts"], string["pts"][1:]))


def test_string_panels_continuity_actual_demo_rooftop(intake):
    panels = [p for p in intake["polylines"] if p.get("layer") == "Panels" and p.get("closed")]
    centres = [tuple(round(sum(p["pts"][i][axis] for i in range(4)) / 4, 3)
                     for axis in (0, 1)) for p in panels]
    result, overlay = run(intake, {})
    points = [tuple(p) for string in result["strings"] for p in string["pts"]]
    assert len(points) == len(set(points)) == 2345
    assert sorted(points) == sorted(centres)
    assert result["stats"]["bank_count"] == 11
    assert result["electrical"]["pass"] is True
    assert all(1 <= string["modules"] <= result["electrical"]["max_modules_per_string"]
               for string in result["strings"])
    assert overlay["items"] == [{"id": s["id"], "pts": s["pts"]} for s in result["strings"]]
    # Independent dimensional oracle from the repo demo's 77-inch module edge.
    assert all(math.dist(a, b) <= 1.5 * 77 + 0.002 for string in result["strings"]
               for a, b in zip(string["pts"], string["pts"][1:]))


@pytest.mark.parametrize("faces,factor,params", [
    ([(0, 0, (0, 80)), (120, 0, (0, 80))], 1, {}),
    ([(240, 0.25, (0, 80)), (240, -0.25, (0, 80))], 1,
     {"min_modules_per_string": 2, "max_modules_per_string": 2}),
    ([(0, 0.12, (0, 80)), (200, 0.12, (20, 100))], 25.4,
     {"meters_per_unit": 0.001}),
])
def test_string_panels_keeps_overlapping_roof_faces_separate(faces, factor, params):
    panels = []
    for elevation, slope, centres in faces:
        for centre in centres:
            panel = rect(centre, 77, 38.5)
            panel["pts"] = [[x * factor, y * factor, round(elevation + slope * x, 3) * factor]
                            for x, y in panel["pts"]]
            panels.append(panel)
    result, _ = run({"polylines": panels[::-1]}, params)
    assert result["stats"]["bank_count"] == 2
    assert sorted(tuple(sorted(round(x / factor, 3) for x, _ in string["pts"]))
                  for string in result["strings"]) == sorted(centres for _, _, centres in faces)
    assert [string["length_ft"] for string in result["strings"]] == [6.7, 6.7]


@pytest.mark.parametrize("gap,modules", [(115.5, [2]), (115.501, [1, 1])])
def test_string_panels_continuity_adjacent_slot_boundary(gap, modules):
    drawing = {"polylines": [rect(x, 77, 38.5) for x in (0.0, gap)]}
    result, _ = run(drawing, {})
    assert result["stats"]["bank_count"] == 1
    assert [string["modules"] for string in result["strings"]] == modules


@pytest.mark.parametrize("count", [18, 19, 20, 35, 36, 37])
def test_string_panels_balances_contiguous_run_without_short_remainder(count):
    intake = {"polylines": [rect(index * 60.0, 40.0, 80.0) for index in range(count)]}
    result, _ = run(intake, {})
    sizes = [string["modules"] for string in result["strings"]]
    maximum = result["electrical"]["max_modules_per_string"]
    assert maximum == 18
    assert len(sizes) == math.ceil(count / maximum)
    assert max(sizes) - min(sizes) <= 1
    assert sum(sizes) == count
    assert max(sizes) <= maximum
    assert result["electrical"]["pass"] is True
    points = [tuple(point) for string in result["strings"] for point in string["pts"]]
    assert len(points) == len(set(points)) == count
    assert set(points) == {(index * 60.0, 0.0) for index in range(count)}
    assert all(math.dist(left, right) <= 120.0 for string in result["strings"]
               for left, right in zip(string["pts"], string["pts"][1:]))


def test_string_panels_balancing_preserves_gap_and_order_boundaries():
    positions = list(range(20)) + list(range(100, 119))
    cents = [(float(x), 0.0) for x in positions]
    ordered = list(range(len(cents)))
    before = copy.deepcopy((ordered, cents))
    chunks = list(string_panels._continuous_chunks(ordered, cents, 18, 1.5))
    assert [len(chunk) for chunk in chunks] == [10, 10, 10, 9]
    assert [index for chunk in chunks for index in chunk] == ordered
    assert all(math.dist(cents[left], cents[right]) <= 1.5 for chunk in chunks
               for left, right in zip(chunk, chunk[1:]))
    assert (ordered, cents) == before


@pytest.mark.parametrize("count,lower,upper,expected", [
    (20, 9, 11, [10, 10]), (37, 12, 14, [13, 12, 12]),
])
def test_constrained_stringing_public_count_variants(count, lower, upper, expected):
    intake = {"polylines": [rect(index * 60.0, 40.0, 80.0) for index in range(count)]}
    result, _ = run(intake, {"min_modules_per_string": lower, "max_modules_per_string": upper})
    assert result["planning"]["status"] == "complete"
    assert result["planning"]["minimum_modules"] == lower
    assert result["planning"]["maximum_modules"] == upper
    assert [string["modules"] for string in result["strings"]] == expected
    assert result["stats"]["modules_stringed"] == count


def test_constrained_stringing_public_island_refuses_false_success():
    intake = {"polylines": [rect(index * 60.0, 40.0, 80.0) for index in range(11)]}
    result, overlay = run(intake, {"min_modules_per_string": 12, "max_modules_per_string": 14})
    assert result["planning"]["status"] == "infeasible"
    assert result["planning"]["reason"] == "component_cardinality"
    assert result["planning"]["witness"]["panel_count"] == 11
    assert result["strings"] == [] and overlay["items"] == []
    assert result["stats"]["modules_stringed"] == 0


@pytest.mark.parametrize("scale,mpu", [(1.0, 0.0254), (0.0254, 1.0), (25.4, 0.001)])
def test_constrained_stringing_equivalent_units(scale, mpu):
    drawing = {"polylines": [rect(index * 60.0, 40.0, 80.0, scale) for index in range(20)]}
    result, _ = run(drawing, {"min_modules_per_string": 9, "max_modules_per_string": 11,
                              "meters_per_unit": mpu})
    assert result["planning"]["status"] == "complete"
    assert [item["modules"] for item in result["strings"]] == [10, 10]
    points = [point for item in result["strings"] for point in item["pts"]]
    assert sorted(round(point[0] / scale, 3) for point in points) == [i * 60.0 for i in range(20)]
    assert [item["length_ft"] for item in result["strings"]] == [45.0, 45.0]


def test_constrained_stringing_maximum_only_and_electrical_cap():
    drawing = {"polylines": [rect(index * 60.0, 40.0, 80.0) for index in range(20)]}
    result, _ = run(drawing, {"max_modules_per_string": 11})
    assert result["planning"]["minimum_modules"] == 1
    assert result["planning"]["maximum_modules"] == 11
    assert [item["modules"] for item in result["strings"]] == [10, 10]
    capped, _ = run(drawing, {"max_modules_per_string": 100})
    assert capped["planning"]["maximum_modules"] == 18
    assert capped["electrical"]["pass"] is True
    assert max(item["modules"] for item in capped["strings"]) <= 18
    refused, overlay = run(drawing, {"min_modules_per_string": 19})
    assert refused["planning"]["reason"] == "electrical_capacity"
    assert refused["planning"]["status"] == "infeasible"
    assert refused["strings"] == [] and overlay["items"] == []
    assert refused["electrical"]["pass"] is None


@pytest.mark.parametrize("params", [
    {"min_modules_per_string": True}, {"min_modules_per_string": 0},
    {"max_modules_per_string": 2.5}, {"max_modules_per_string": None},
    {"min_modules_per_string": 12, "max_modules_per_string": 11},
])
def test_constrained_stringing_bad_bounds_refused_at_builtin(params):
    with pytest.raises(ValueError, match="bounds"):
        run({"polylines": [rect(0, 40, 80)]}, params)


def test_constrained_stringing_budget_returns_no_partial_plan(monkeypatch):
    import solar_string_partition as partition
    monkeypatch.setattr(partition, "DEFAULT_STATE_BUDGET", 0)
    result, overlay = run({"polylines": [rect(i * 60, 40, 80) for i in range(20)]},
                           {"min_modules_per_string": 9, "max_modules_per_string": 11})
    assert result["planning"]["status"] == "unresolved"
    assert result["planning"]["reason"] == "search_budget"
    assert result["strings"] == [] and overlay["items"] == []
    assert result["stats"]["modules_stringed"] == 0
    assert result["electrical"]["pass"] is None


def test_constrained_stringing_registered_aliases_declare_bounds():
    for filename, name in ((REPO_DIR / "engine/registry.json", "string-panels"),
                           (SERVER_DIR / "catalog_tools.json", "autofill-string-targets")):
        record = next(item for item in json.loads(filename.read_text(encoding="utf-8"))["tools"]
                      if item["name"] == name)
        assert tool_validate.validate_params(record, {"min_modules_per_string": 9,
                                                      "max_modules_per_string": 11}) == []
        for params in ({"min_modules_per_string": True}, {"max_modules_per_string": 0}):
            assert tool_validate.validate_params(record, params)
        assert "planning" in record["returns"]["properties"]


@pytest.mark.parametrize("name,filename", [
    ("string-panels", REPO_DIR / "engine/registry.json"),
    ("autofill-string-targets", SERVER_DIR / "catalog_tools.json"),
])
def test_constrained_stringing_dynamic_registered_entry(name, filename):
    import tool_loader
    record = next(item for item in json.loads(filename.read_text(encoding="utf-8"))["tools"]
                  if item["name"] == name)
    drawing = {"polylines": [rect(i * 60, 40, 80) for i in range(20)]}
    before = copy.deepcopy(drawing)
    envelope = tool_loader.run_tool_dynamic(record, drawing,
        {"min_modules_per_string": 9, "max_modules_per_string": 11}, aps_live=False)
    assert envelope["ok"], envelope.get("error")
    assert envelope["result"]["planning"]["status"] == "complete"
    assert [item["modules"] for item in envelope["result"]["strings"]] == [10, 10]
    assert drawing == before


def test_constrained_stringing_hole_and_reversed_input():
    panels = []
    for row in range(3):
        for col in range(3):
            if (row, col) != (1, 1):
                panel = rect(col * 80, 40, 80)
                panel["pts"] = [[x, y + row * 80] for x, y in panel["pts"]]
                panels.append(panel)
    results = [run({"polylines": order}, {"min_modules_per_string": 4,
                                        "max_modules_per_string": 4})[0]
               for order in (panels, list(reversed(panels)))]
    for result in results:
        assert result["planning"]["status"] == "complete"
        assert [item["modules"] for item in result["strings"]] == [4, 4]
        points = [tuple(point) for item in result["strings"] for point in item["pts"]]
        assert len(points) == len(set(points)) == 8
        assert (80.0, 80.0) not in points
        assert all(math.dist(a, b) <= 120 for item in result["strings"]
                   for a, b in zip(item["pts"], item["pts"][1:]))
    assert results[0]["strings"] == results[1]["strings"]


# Constrained planner oracles share the registered string-units CI suite.
from solar_string_partition import plan_partition, validate_partition


def exact_possible(points, minimum, maximum, step):
    """Enumerate permutations and all cuts, independently of planner traversal."""
    for order in itertools.permutations(range(len(points))):
        for cuts in itertools.product((False, True), repeat=max(0, len(order) - 1)):
            paths, current = [], [order[0]]
            for index, cut in enumerate(cuts):
                if cut:
                    paths.append(current)
                    current = []
                current.append(order[index + 1])
            paths.append(current)
            if all(minimum <= len(path) <= maximum and all(
                    math.dist(points[a], points[b]) <= step
                    for a, b in zip(path, path[1:])) for path in paths):
                return True
    return False


@pytest.mark.parametrize("points", [
    [(0, 0), (1, 0), (2, 0), (3, 0)],
    [(0, 0), (1, 0), (-0.5, 0.86), (-0.5, -0.86)],
    [(0, 0), (1, 0), (0, 1), (1, 1), (2, 1)],
    [(0, 0), (1, 0), (10, 0), (11, 0)],
])
@pytest.mark.parametrize("minimum,maximum", [(2, 2), (2, 3), (3, 4), (4, 4)])
def test_partition_matches_independent_permutation_and_cut_oracle(points, minimum, maximum):
    before = copy.deepcopy(points)
    result = plan_partition(list(range(len(points))), points, minimum, maximum, 1.01)
    possible = exact_possible(points, minimum, maximum, 1.01)
    assert result["status"] == ("complete" if possible else "infeasible")
    if possible:
        validate_partition(list(range(len(points))), points, result["paths"], minimum, maximum, 1.01)
    else:
        assert result["paths"] == []
    assert points == before


def test_exhaustive_obstruction_differs_from_budget_exhaustion():
    # A three-leaf star has four panels but no four-panel simple path.
    points = [(0, 0), (1, 0), (-0.5, 0.86), (-0.5, -0.86)]
    exhausted = plan_partition([0, 1, 2, 3], points, 4, 4, 1.01, state_budget=1)
    assert exhausted["status"] == "unresolved"
    assert exhausted["reason"] == "search_budget" and exhausted["paths"] == []
    proved = plan_partition([0, 1, 2, 3], points, 4, 4, 1.01)
    assert proved["status"] == "infeasible"
    assert proved["reason"] == "exhaustive_path_search" and proved["paths"] == []


@pytest.mark.parametrize("paths", [
    [[0, 1], [1, 2]], [[0, 1]], [[0, 3], [1, 2]],
    [[0], [1, 2, 3]], [[0, 1], [2, True]],
])
def test_complete_validator_rejects_wrong_coverage_sizes_or_edges(paths):
    with pytest.raises(ValueError, match="invalid string partition"):
        validate_partition([0, 1, 2, 3], [(0, 0), (1, 0), (2, 0), (3, 0)],
                           paths, 2, 2, 1.01)


def test_empty_and_reordered_inputs_are_immutable_and_deterministic():
    assert plan_partition([], [], 2, 3, 1.0)["status"] == "complete"
    points = [(0, 0), (1, 0), (2, 0), (3, 0), (4, 0)]
    order = [4, 3, 2, 1, 0]
    before = copy.deepcopy((order, points))
    first = plan_partition(order, points, 2, 3, 1.01)
    assert first == plan_partition(order, points, 2, 3, 1.01)
    assert (order, points) == before


@pytest.mark.parametrize("minimum,maximum,budget", [(True, 3, 1), (0, 3, 1),
    (4, 3, 1), (2, 3, -1), (2, 3, True)])
def test_invalid_planner_contract_refused(minimum, maximum, budget):
    with pytest.raises(ValueError, match="bounds or budget"):
        plan_partition([0], [(0, 0)], minimum, maximum, 1.0, state_budget=budget)


@pytest.mark.parametrize("paths,message", [
    ([[0, 1], [1, 2]], "coverage"),
    ([[0, 1]], "coverage"),
    ([[0, 1], [2, 4]], "coverage"),
    ([[0], [1, 2, 3]], "size"),
    ([[0, 2], [1, 3]], "adjacency"),
])
def test_independent_validator_rejects_corrupt_complete_paths(paths, message):
    points = [(0, 0), (1, 0), (2, 0), (3, 0)]
    with pytest.raises(ValueError, match=message):
        validate_partition([0, 1, 2, 3], points, paths, 2, 2, 1.01)


def test_anchor_may_be_inside_a_string_and_empty_component_is_complete():
    points = [(0, 0), (-1, 0), (1, 0)]
    found = plan_partition([0, 1, 2], points, 3, 3, 1.01)
    assert found["status"] == "complete"
    assert found["paths"][0][1] == 0
    empty = plan_partition([], [], 1, 2, 1.0)
    assert empty["status"] == "complete" and empty["paths"] == []


@pytest.mark.parametrize("scale,reverse", [(1.0, False), (25.4, False), (1.0, True)])
def test_anchor_actual_roof_component_completes_within_existing_budget(intake, scale, reverse):
    import statistics
    from solar_string_partition import DEFAULT_STATE_BUDGET
    # Bank discovery consumes normalized inches, as the public builtin does.
    panels = [panel for panel in intake["polylines"]
              if panel.get("layer") == "Panels" and panel.get("closed") and panel.get("pts")]
    if reverse:
        panels.reverse()
    points = [string_panels._centroid(panel) for panel in panels]
    banks = string_panels._cluster_banks(points, string_panels.DEFAULTS["cluster_radius_factor"])
    bank = next(group for group in banks if len(group) == 123)
    order = string_panels._bank_serpentine(panels, bank, points)
    module_length = statistics.median(max(math.dist(a[:2], b[:2]) for a, b in zip(
        panels[index]["pts"], panels[index]["pts"][1:] + panels[index]["pts"][:1]))
        for index in bank)
    max_step = string_panels.MAX_STEP_MODULE_LENGTHS * module_length * scale
    points = [(x * scale, y * scale) for x, y in points]
    before = copy.deepcopy((order, points))
    result = plan_partition(order, points, 12, 14, max_step)
    assert result["status"] == "complete"
    assert result["searched_states"] <= DEFAULT_STATE_BUDGET == 50_000
    validate_partition(order, points, result["paths"], 12, 14, max_step)
    assert len({index for path in result["paths"] for index in path}) == 123
    assert all(12 <= len(path) <= 14 for path in result["paths"])
    assert (order, points) == before



def _simple_suffix_oracle(neighbors, remaining, last, anchor, slots):
    for length in range(min(slots, len(remaining)) + 1):
        for suffix in itertools.permutations(sorted(remaining), length):
            path = (last,) + suffix
            if path[-1] == anchor and all(right in neighbors[left]
                                         for left, right in zip(path, path[1:])):
                return True
    return False


@pytest.mark.parametrize("edges", [
    [(0, 1), (1, 2), (2, 3)],
    [(0, 1), (1, 3), (0, 2), (2, 3)],
    [(0, 1), (1, 2), (2, 0), (2, 3)],
    [(0, 1), (2, 3)],
])
@pytest.mark.parametrize("remaining", [{1, 2, 3}, {2, 3}, {3}])
@pytest.mark.parametrize("slots", [0, 1, 2, 3])
def test_anchor_reachability_matches_independent_simple_suffix_oracle(edges, remaining, slots):
    from solar_string_partition import _anchor_reachable
    neighbors = {index: set() for index in range(4)}
    for left, right in edges:
        neighbors[left].add(right)
        neighbors[right].add(left)
    before = copy.deepcopy((neighbors, remaining))
    assert _anchor_reachable(remaining, 0, 3, slots, neighbors) == _simple_suffix_oracle(
        neighbors, remaining, 0, 3, slots)
    assert (neighbors, remaining) == before


def test_anchor_reachability_preserves_unused_alternative_and_zero_hop_anchor():
    from solar_string_partition import _anchor_reachable
    neighbors = {0: {1, 3}, 1: {0, 2}, 2: {1, 3}, 3: {0, 2}}
    assert not _anchor_reachable({2}, 0, 2, 2, neighbors)
    assert _anchor_reachable({2, 3}, 0, 2, 2, neighbors)
    assert not _anchor_reachable({2, 3}, 0, 2, 1, neighbors)
    assert _anchor_reachable({1, 2, 3}, 0, 0, 0, neighbors)

@pytest.mark.parametrize("params,cause,action", [
    ({"panel_layer": "Roof-West"}, "no closed polylines", "panel_layer"),
    ({"max_system_voltage": 30}, "0 modules per string", "module"),
    ({"min_modules_per_string": 12, "max_modules_per_string": 8}, "bounds", "min_modules_per_string"),
])
@pytest.mark.parametrize("name,filename", [
    ("string-panels", REPO_DIR / "engine/registry.json"),
    ("autofill-string-targets", SERVER_DIR / "catalog_tools.json"),
])
def test_string_refusal_recovery_survives_job_wait(name, filename, params, cause, action):
    import jobs
    import tool_loader

    tools = json.loads(filename.read_text(encoding="utf-8"))["tools"]
    record = next(tool for tool in tools if tool["name"] == name)
    drawing = {"polylines": [rect(0, 40, 80)]}
    original = copy.deepcopy(drawing)
    result = tool_loader.run_tool_dynamic(record, drawing, params, aps_live=False)
    for envelope in (result, jobs.failed_envelope_from(result)):
        error = envelope["error"]
        assert error["error_code"] == "BAD_PARAMS"
        assert error["actor"] == "user"
        assert error["retry_class"] == "after_action"
        assert cause in error["message"]
        assert params.get("panel_layer", "Panels") in error["message"]
        assert action in error["next_action"]
        assert envelope["result"] is None
    assert drawing == original


def test_autofill_registration_trust_is_limited_to_shipped_read_only_body():
    import tool_loader

    tools = json.loads((SERVER_DIR / "catalog_tools.json").read_text(encoding="utf-8"))["tools"]
    record = next(tool for tool in tools if tool["name"] == "autofill-string-targets")
    builtin = SERVER_DIR / "builtins" / "string_panels.py"
    assert tool_loader._is_trusted_builtin_path(record, builtin)
    assert not tool_loader._is_trusted_builtin_path(
        {**record, "capabilities": ["drawing.write"]}, builtin)
    assert not tool_loader._is_trusted_builtin_path(
        record, SERVER_DIR / "builtins" / "solar_solve_proposal.py")
    assert not tool_loader._is_trusted_builtin_path(
        record, SERVER_DIR / "authored" / "string_panels.py")
