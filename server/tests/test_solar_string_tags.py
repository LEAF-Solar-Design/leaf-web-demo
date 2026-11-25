"""Prepared product tags: physical scale and nominal geometry checked independently."""
from copy import deepcopy
from fractions import Fraction
import math

import pytest

import solar_string_tags as tags
from solar_design_graph import GraphValidationError, validate_graph
from solar_sizing_client import checked_graph
from solar_solve_results import sync_assignments
from test_w2_string_add import solved as solved_fixture


@pytest.fixture
def graph():
    return solved_fixture.__wrapped__()


def request(graph, **changes):
    return {"expected_rev": graph["rev"], "paper_mm_per_drawing_unit": 1, **changes}


def boxes(rows):
    return [(r["position"][0], r["position"][1],
             r["position"][0] + 1.2 * len(r["text"]) * r["height"],
             r["position"][1] + r["height"]) for r in rows]


def assert_disjoint(rows):
    rects = boxes(rows)
    for i, (ax, ay, bx, by) in enumerate(rects):
        for cx, cy, dx, dy in rects[i + 1:]:
            assert bx < cx or dx < ax or by < cy or dy < ay


def test_exact_payload_sorted_repeatable_and_preserves_inputs(graph):
    params = request(graph)
    before, original = deepcopy(graph), deepcopy(params)
    plan = tags.prepare_tags(graph, params)
    assert set(plan) == {"schema", "tag_layer", "paper_mm_per_drawing_unit", "tags"}
    assert plan["schema"] == "leaf.solar-string-tags.v1"
    assert plan["tag_layer"] == "LEAF_STRING_TAGS"
    assert plan["paper_mm_per_drawing_unit"] == 1
    assert [r["string_ref"] for r in plan["tags"]] == sorted(s["id"] for s in graph["strings"])
    labels = {s["id"]: s["circuit_tag"] for s in graph["strings"]}
    for row in plan["tags"]:
        assert set(row) == {"string_ref", "text", "position", "height", "rotation_degrees"}
        assert row["text"] == labels[row["string_ref"]]
        assert row["height"] == 2 and row["rotation_degrees"] == 0
        assert row["position"][2] == 0
    assert_disjoint(plan["tags"])
    assert tags.prepare_tags(graph, params) == plan
    reordered = deepcopy(graph)
    reordered["strings"].reverse()
    assert tags.prepare_tags(reordered, params) == plan
    assert graph == before and params == original
    plan["tags"][0]["position"][0] = -123
    assert graph == before


@pytest.mark.parametrize("route", [False, True])
@pytest.mark.parametrize("unit,mpu,source", [
    ("in", Fraction(127, 5000), "insunits"),
    ("m", Fraction(1), "explicit"),
    # An originally unitless drawing is explicitly resolved to metres before this API.
    ("m", Fraction(1), "explicit-unitless"),
])
def test_ordered_midpoint_and_equivalent_physical_scales(graph, route, unit, mpu, source):
    units = graph["project"]["units"]
    units.update(drawing_units=unit, meters_per_unit=float(mpu), source="explicit" if source == "explicit-unitless" else source,
                 drawing_unit_is_feet=False)
    if source == "explicit-unitless":
        units["warnings"] = ["unitless input explicitly resolved by caller"]
    # Same paper mapping: one compute metre plots as 10 mm.
    factor = 10 * mpu
    height = Fraction(2) / factor
    margin = height / 4
    cw, ch = Fraction(17, 10) * height, Fraction(3, 2) * height
    centres = {p["id"]: p["centre"] for p in graph["panels"]}
    if route:
        for s in graph["strings"]:
            s["route"] = [[90, 90], [7, 4], [-9, -4], [100, 100]]
    checked = checked_graph(graph, graph["rev"])
    assert checked["project"]["units"]["meters_per_unit"] == float(mpu)
    plan = tags.prepare_tags(graph, request(graph, paper_mm_per_drawing_unit=float(factor)))
    reserved = set()
    for s, row in zip(sorted(graph["strings"], key=lambda s: s["id"]), plan["tags"]):
        point = s["route"][2] if route else centres[s["ordered_panel_refs"][2]]
        x, y = (Fraction(v) / mpu for v in point)
        column, line = math.floor(x / cw), math.floor(y / ch)
        while (column, line) in reserved:
            line += 1
        reserved.add((column, line))
        assert row["height"] == pytest.approx(float(height))
        assert row["position"] == pytest.approx([float(column * cw + margin), float(line * ch + margin), 0])
        assert row["height"] * float(factor) == pytest.approx(2)
    assert_disjoint(plan["tags"])


def test_compact_slot_fallback_matches_stored_panel_fallback(graph):
    import solar_ground_graph_codec as codec

    expected = tags.prepare_tags(graph, request(graph))
    graph["project"]["installation_design"] = "Ground"
    panels = {p["id"]: p for p in graph["panels"]}
    for frame in graph["frames"]:
        refs = frame["panel_refs"][:]
        frame["installation_design"] = "Ground"
        frame["tracker"] = {
            "tracker_model": "single_axis_tracker", "axis_start": [0, 0], "axis_end": [1, 0],
            "axis_units": "drawing", "module_slots": len(refs), "row_index": 0,
            "length_m": 1, "rail_overhang_m": 0, "cross_axis_width_m": None,
            "max_tilt_deg": None, "source": {"entity_kind": "tracker", "handle": None,
                "layer": None, "block_name": None, "source_command": None}}
        template = {k: deepcopy(panels[refs[0]][k]) for k in ("rev", "provenance", "validity", "extra")}
        frame["ground_slots"] = codec.encode_slots(refs, [panels[r]["centre"] for r in refs], 0.0, template)
        for key in ("panel_refs", "matrix", "panel_assignments", "sequences"):
            frame[key] = []
    graph["panels"] = []
    graph = validate_graph(graph)
    before = deepcopy(graph)
    assert tags.prepare_tags(graph, request(graph)) == expected
    assert graph == before


def test_collocated_routes_get_separate_cells(graph):
    for s in graph["strings"]:
        s["route"] = [[0, 0]]
    rows = tags.prepare_tags(graph, request(graph))["tags"]
    assert [r["position"] for r in rows] == [[0.5, 0.5, 0], [0.5, 3.5, 0]]
    assert_disjoint(rows)


@pytest.mark.parametrize("old,new", [("A" * 24, "B" * 24), ("A", "B" * 24), ("A" * 24, "B")])
@pytest.mark.parametrize("gap", [8, 800])
def test_rename_keeps_separate_route_label_near_its_anchor(graph, old, new, gap):
    renamed, neighbor = sorted(graph["strings"], key=lambda s: s["id"])
    mpu = graph["project"]["units"]["meters_per_unit"]
    neighbor.update(circuit_tag="S2", route=[[20 * mpu, 0]])
    renamed["route"] = [[(20 + gap) * mpu, 0]]
    positions = []
    for label in (old, new):
        renamed["circuit_tag"] = label
        rows = tags.prepare_tags(graph, request(graph))["tags"]
        assert_disjoint(rows)
        x, y, _ = rows[1]["position"]
        assert abs(x - 20) <= 2 * rows[1]["height"]
        assert abs(y) <= rows[1]["height"]
        positions.append(rows[1]["position"])
    assert positions[0] == positions[1]


def test_long_label_spans_neighboring_route_anchor(graph):
    left, right = sorted(graph["strings"], key=lambda s: s["id"])
    mpu = graph["project"]["units"]["meters_per_unit"]
    left.update(circuit_tag="A" * 32, route=[[0, 0]])
    right.update(circuit_tag="B", route=[[20 * mpu, 0]])
    rows = tags.prepare_tags(graph, request(graph))["tags"]
    assert abs(rows[1]["position"][0] - 20) <= 2 * rows[1]["height"]
    assert_disjoint(rows)


def test_large_collocated_plan_and_reservation_work_is_bounded(graph, monkeypatch):
    class Counted(dict):
        operations = 0

        def get(self, key, default):
            self.operations += 1
            return super().get(key, default)

        def __setitem__(self, key, value):
            self.operations += 1
            return super().__setitem__(key, value)

    # Exercise the actual reservation path with an independent operation counter.
    next_rows = Counted()
    reserve = tags._reserve

    def counted(_next_rows, columns, row):
        return reserve(next_rows, columns, row)

    monkeypatch.setattr(tags, "_reserve", counted)
    prototype = deepcopy(graph["strings"][0])
    prototype.update(ordered_panel_refs=[], module_count=0, from_ref=None, to_ref=None,
                     route=[[0, 0]], circuit_tag="S")
    for key in ("panels", "frames", "electrical_zones"):
        graph[key] = []
    graph["strings"] = []
    for i in range(10000):
        string = deepcopy(prototype)
        string["id"] = f"leaf:string:00000000-0000-4000-8000-{i:012d}"
        graph["strings"].append(string)
    rows = tags.prepare_tags(graph, request(graph))["tags"]
    assert len(rows) == 10000
    assert [r["string_ref"] for r in rows] == [s["id"] for s in graph["strings"]]
    rects = boxes(rows)
    assert all(a[3] < b[1] for a, b in zip(rects, rects[1:]))
    assert 0 < next_rows.operations < 12 * len(rows)
    extra = deepcopy(prototype)
    extra["id"] = "leaf:string:00000000-0000-4000-8000-000000010000"
    graph["strings"].append(extra)
    with pytest.raises(GraphValidationError, match="STRING_TAGS_LIMIT_EXCEEDED"):
        tags.prepare_tags(graph, request(graph))


@pytest.mark.parametrize("value", [1.8, 2, 3.25])
def test_paper_height_boundary(graph, value):
    rows = tags.prepare_tags(graph, request(graph, paper_height_mm=value, paper_mm_per_drawing_unit=0.254))["tags"]
    for r in rows:
        assert r["height"] == pytest.approx(float(Fraction(str(value)) / Fraction(127, 500)))
        assert r["height"] * 0.254 >= 1.8 - 1e-15


@pytest.mark.parametrize("key", ["paper_height_mm", "paper_mm_per_drawing_unit"])
@pytest.mark.parametrize("value", [True, False, None, "2", 0, -1, float("nan"), float("inf"), 10**1000])
def test_bad_physical_scale(graph, key, value):
    with pytest.raises(GraphValidationError, match="INVALID_STRING_TAG_SCALE"):
        tags.prepare_tags(graph, request(graph, **{key: value}))


@pytest.mark.parametrize("height,factor", [(1.799999999999, 1), (2, 1e-320), (2, 1e308), (1e308, 1)])
def test_small_height_and_arithmetic_limits(graph, height, factor):
    with pytest.raises(GraphValidationError, match="STRING_TAG_(HEIGHT_TOO_SMALL|SCALE_LIMIT|COORDINATE_LIMIT)"):
        tags.prepare_tags(graph, request(graph, paper_height_mm=height, paper_mm_per_drawing_unit=factor))


@pytest.mark.parametrize("value", ["", "0", "x\ny", "x\ty", "é", "x" * 129, None, 7,
                                       *list('<>/\\":;?*|=')])
def test_unsafe_layers(graph, value):
    with pytest.raises(GraphValidationError, match="INVALID_STRING_TAG_LAYER"):
        tags.prepare_tags(graph, request(graph, tag_layer=value))


@pytest.mark.parametrize("value", ["", "x\ny", "x\ry", "é", "x" * 129, "\x7f"])
def test_unsafe_labels(graph, value):
    graph["strings"][0]["circuit_tag"] = value
    with pytest.raises(GraphValidationError, match="INVALID_STRING_TAG_LABEL") as error:
        tags.prepare_tags(graph, request(graph))
    assert error.value.path == "strings[0]"
    if value:
        assert value not in str(error.value)


def test_printable_ascii_boundaries_and_custom_layer(graph):
    for s in graph["strings"]:
        s["circuit_tag"] = "~" * 128
        s["route"] = [[0, 0]]
    plan = tags.prepare_tags(graph, request(graph, tag_layer="A" * 128))
    assert plan["tag_layer"] == "A" * 128
    assert all(r["text"] == "~" * 128 for r in plan["tags"])
    assert_disjoint(plan["tags"])


@pytest.mark.parametrize("params", [None, [], {}, {"expected_rev": True, "paper_mm_per_drawing_unit": 1},
    {"expected_rev": "0", "paper_mm_per_drawing_unit": 1}, {"expected_rev": 0},
    {"paper_mm_per_drawing_unit": 1}, {"expected_rev": 0, "paper_mm_per_drawing_unit": 1, "unknown": 1}])
def test_request_shape(graph, params):
    with pytest.raises(GraphValidationError, match="INVALID_STRING_TAGS_REQUEST"):
        tags.prepare_tags(graph, params)


def test_stale_revision_and_empty_strings(graph):
    with pytest.raises(GraphValidationError, match="STALE_GRAPH_REVISION"):
        tags.prepare_tags(graph, request(graph, expected_rev=graph["rev"] + 1))
    graph["strings"] = []
    sync_assignments(graph)
    with pytest.raises(GraphValidationError, match="STRING_TAGS_REQUIRE_STRINGS"):
        tags.prepare_tags(graph, request(graph))


def test_no_position_is_refused_without_origin_invention(graph):
    graph["strings"][0].update(ordered_panel_refs=[], module_count=0, from_ref=None, to_ref=None)
    sync_assignments(graph)
    with pytest.raises(GraphValidationError, match="STRING_TAG_POSITION_REQUIRED") as error:
        tags.prepare_tags(graph, request(graph))
    assert error.value.path == "strings[0]"


@pytest.mark.parametrize("value", [True, 0, -1, float("nan"), float("inf"), None])
def test_invalid_graph_units(graph, value):
    graph["project"]["units"]["meters_per_unit"] = value
    with pytest.raises(GraphValidationError):
        tags.prepare_tags(graph, request(graph))


def test_unresolved_units_are_refused(graph):
    graph["project"]["units"]["meters_per_unit"] = 1
    with pytest.raises(GraphValidationError, match="UNRESOLVED_UNITS"):
        tags.prepare_tags(graph, request(graph))


@pytest.mark.parametrize("value", [1e100, -1e100, 1e-320])
def test_pathological_coordinate_is_refused(graph, value):
    graph["strings"][0]["route"] = [[value, 0]]
    with pytest.raises(GraphValidationError, match="STRING_TAG_COORDINATE_LIMIT"):
        tags.prepare_tags(graph, request(graph))


def test_coordinate_to_cell_ratio_is_bounded(graph):
    graph["strings"][0]["route"] = [[1e8, 0]]
    with pytest.raises(GraphValidationError, match="STRING_TAG_COORDINATE_LIMIT"):
        tags.prepare_tags(graph, request(graph, paper_mm_per_drawing_unit=1e8))
