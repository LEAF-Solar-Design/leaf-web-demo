"""sf-ground-outlines: a converted Ground frame keeps the tracker layout's exact four corners at
extra.ground_outline, and every reader takes them through solar_ground_outlines.frame_outline, which fails
closed. Every expected value was measured by running this module with python -B."""
from __future__ import annotations

import copy
import json
import math
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import solar_design_graph as sdg  # noqa: E402
import solar_ground_conversion as conv  # noqa: E402
import solar_ground_dsteps as dsteps  # noqa: E402
import solar_ground_graph_codec as codec  # noqa: E402
import solar_ground_outlines as og  # noqa: E402
from test_solar_ground_conversion import convert, drawn, site, small_doc  # noqa: E402,F401
from test_solar_ground_equipment import (  # noqa: E402,F401
    converted, equip, graph, pinned, service, sized, strung,
)
from test_solar_ground_graph_codec import b18, ground_base  # noqa: E402,F401

SMALL = [((1.0, 0.0), (-1.0, 0.0), (-1.0, 6.0), (1.0, 6.0)),
         ((4.5, 0.0), (3.5, 0.0), (3.5, 10.0), (4.5, 10.0))]
ROTATED = [[1.18848280743026, 1.858637894427305], [0.8115171925697399, 2.141362105572695],
           [3.81151719256974, 6.1413621055726955], [4.18848280743026, 5.8586378944273045]]


def refused(code, path, frame):
    before = copy.deepcopy(frame)
    with pytest.raises(og.GroundOutlineError) as error:
        og.frame_outline(frame)
    assert (error.value.code, str(error.value), error.value.path) == (code, code, path)
    assert frame == before
    return error.value


def small_frame(graph):
    return copy.deepcopy(convert(graph)["frames"][0])


def frame(points, start, end):
    return {"tracker": {"axis_start": start, "axis_end": end},
            "extra": {"ground_outline": {"point_units": "drawing", "points": points}}}


PI = og.POINTS_INVALID
PP = og.POINTS_PATH


def test_ground_outline_constants():
    assert (og.OUTLINE_KEY, og.POINT_UNITS) == ("ground_outline", "drawing")
    assert og.ENVELOPE_KEYS == frozenset({"point_units", "points"})
    assert og.CODES == frozenset({"GROUND_OUTLINE_MISSING", "GROUND_OUTLINE_ENVELOPE_INVALID",
                                  "GROUND_OUTLINE_POINTS_INVALID"})
    assert (og.ENVELOPE_PATH, og.POINTS_PATH) == ("extra.ground_outline", "extra.ground_outline.points")
    assert og.MAX_OUTLINE_COORDINATE == 2_000_000_000.0
    assert og.OUTLINE_ULPS == 16.0
    assert og.OUTLINE_MIN_EDGE_TOLERANCES == 64.0
    assert og.OUTLINE_SQUARE == 1e-9
    assert og.OUTLINE_SQUARE_TOLERANCES == 0.5
    assert not hasattr(og, "OUTLINE_TOLERANCE")
    assert issubclass(og.GroundOutlineError, ValueError)


def test_ground_outline_record_is_a_fresh_exact_copy():
    outline = [[1.0, 0.0], [-1.0, 0.0], [-1.0, 6.0], [1.0, 6.0]]
    before = copy.deepcopy(outline)
    record = og.outline_record(outline)
    assert record == {"point_units": "drawing", "points": before} and outline == before
    assert record["points"] is not outline and all(a is not b for a, b in zip(record["points"], outline))


def test_ground_outline_conversion_stores_the_layout_corners(graph):
    frames = convert(graph)["frames"]
    assert [frame["extra"] for frame in frames] == [
        {"ground_outline": {"point_units": "drawing", "points": [list(p) for p in corners]}} for corners in SMALL]
    assert [og.frame_outline(frame) for frame in frames] == SMALL
    assert [frame["tracker"]["axis_units"] for frame in frames] == ["drawing", "drawing"]


@pytest.mark.parametrize("units,mpu", [("m", 1.0), ("ft", 0.3048)])
def test_ground_outline_is_in_drawing_units_whatever_the_unit(graph, units, mpu):
    g = ground_base(graph)
    g["project"]["units"] = dict(g["project"]["units"], drawing_units=units, meters_per_unit=mpu)
    frames = convert(graph, small_doc(units=units), g)["frames"]
    assert [og.frame_outline(frame) for frame in frames] == SMALL


def test_ground_outline_keeps_the_emitted_floats_of_a_rotated_row(graph):
    rows = [drawn(1.0, 2.0, 4.0, 6.0, 4, width=0.4712070185756503),
            drawn(10.0, 0.0, 10.0, 7.5, 3, width=1.3, row_index=2)]
    frames = convert(graph, small_doc(rows))["frames"]
    assert frames[0]["extra"]["ground_outline"]["points"] == ROTATED
    assert og.frame_outline(frames[0]) == tuple(tuple(point) for point in ROTATED)
    assert og.frame_outline(frames[1]) == ((10.65, 0.0), (9.35, 0.0), (9.35, 7.5), (10.65, 7.5))


def test_ground_outline_equals_the_layout_on_the_b18_site(site, graph):
    frames = site["result"](graph)["frames"]
    state = site["state"]
    layout = dsteps.tracker_panel_layout(conv.tracker_entities(state), 1.0, state.get("settings", {}))
    assert len(frames) == len(layout["trackers"]) == 237
    for frame, tracker in zip(frames, layout["trackers"]):
        assert frame["extra"]["ground_outline"]["points"] == tracker["outline"]
        assert og.frame_outline(frame) == tuple(tuple(point) for point in tracker["outline"])


def test_ground_outline_validates_as_graph_json_and_survives_expansion(graph):
    g = ground_base(graph)
    g["frames"] = convert(graph)["frames"]
    sdg._reset_validation_caches()
    assert sdg.validate_graph(g) == g
    assert json.loads(json.dumps(g, allow_nan=False))["frames"][0]["extra"] == g["frames"][0]["extra"]
    expanded = codec.expand_graph(g)
    assert [og.frame_outline(frame) for frame in expanded["frames"]] == SMALL


def test_ground_outline_survives_sizing_stringing_and_equipment(graph, service, pinned):
    base = converted(graph)
    expected = [og.frame_outline(frame) for frame in base["frames"]]
    assert expected == SMALL
    sized_graph = sized(base)
    equipped = equip(strung(copy.deepcopy(sized_graph), pinned))
    for stage in (sized_graph, equipped):
        assert [og.frame_outline(frame) for frame in stage["frames"]] == expected


def test_ground_outline_read_never_mutates_and_returns_fresh_tuples(graph):
    frame = small_frame(graph)
    before = copy.deepcopy(frame)
    first, second = og.frame_outline(frame), og.frame_outline(frame)
    assert frame == before and first == second == SMALL[0]
    assert type(first) is tuple and all(type(point) is tuple for point in first)


def test_ground_outline_accepts_integer_coordinates(graph):
    frame = small_frame(graph)
    frame["extra"]["ground_outline"]["points"] = [[1, 0], [-1, 0], [-1, 6], [1, 6]]
    assert og.frame_outline(frame) == ((1, 0), (-1, 0), (-1, 6), (1, 6))


def test_ground_outline_missing_on_a_frame_converted_before_this_record(graph):
    frame = small_frame(graph)
    frame["extra"] = {}
    refused("GROUND_OUTLINE_MISSING", "extra.ground_outline", frame)
    del frame["extra"]
    refused("GROUND_OUTLINE_MISSING", "extra.ground_outline", frame)
    for value in (None, [], "frame", 7):
        refused("GROUND_OUTLINE_MISSING", "extra.ground_outline", value)


@pytest.mark.parametrize("envelope", [
    None, [], "drawing", 4,
    {"points": [[1.0, 0.0], [-1.0, 0.0], [-1.0, 6.0], [1.0, 6.0]]},
    {"point_units": "drawing"},
    {"point_units": "m", "points": [[1.0, 0.0], [-1.0, 0.0], [-1.0, 6.0], [1.0, 6.0]]},
    {"point_units": "Drawing", "points": [[1.0, 0.0], [-1.0, 0.0], [-1.0, 6.0], [1.0, 6.0]]},
    {"point_units": ["drawing"], "points": [[1.0, 0.0], [-1.0, 0.0], [-1.0, 6.0], [1.0, 6.0]]},
    {"point_units": "drawing", "points": [[1.0, 0.0], [-1.0, 0.0], [-1.0, 6.0], [1.0, 6.0]], "closed": True},
], ids=["none", "list", "text", "number", "no-units", "no-points", "metres", "case", "units-list", "extra-key"])
def test_ground_outline_envelope_refusals(graph, envelope):
    frame = small_frame(graph)
    frame["extra"]["ground_outline"] = envelope
    refused("GROUND_OUTLINE_ENVELOPE_INVALID", "extra.ground_outline", frame)


GOOD = [[1.0, 0.0], [-1.0, 0.0], [-1.0, 6.0], [1.0, 6.0]]


def with_point(index, value):
    points = copy.deepcopy(GOOD)
    points[index] = value
    return points


@pytest.mark.parametrize("points", [
    None, "points", {}, GOOD[:3], GOOD + [[1.0, 0.0]],
    with_point(0, (1.0, 0.0)), with_point(1, [-1.0]), with_point(2, [-1.0, 6.0, 0.0]),
    with_point(3, {"x": 1.0, "y": 6.0}),
    with_point(0, [True, 0.0]), with_point(0, ["1.0", 0.0]), with_point(0, [None, 0.0]),
    with_point(0, [float("nan"), 0.0]), with_point(0, [float("inf"), 0.0]), with_point(0, [1.0, float("-inf")]),
    with_point(0, [2000000000.0000002, 0.0]), with_point(0, [2000000001, 0.0]), with_point(0, [10 ** 400, 0.0]),
], ids=["none", "text", "mapping", "three", "five", "tuple-pair", "one-number", "three-numbers", "mapping-pair",
        "boolean", "numeric-text", "null", "nan", "inf", "neg-inf", "over-bound-float", "over-bound-int",
        "huge-int"])
def test_ground_outline_point_shape_refusals(graph, points):
    frame = small_frame(graph)
    frame["extra"]["ground_outline"]["points"] = points
    refused("GROUND_OUTLINE_POINTS_INVALID", "extra.ground_outline.points", frame)


@pytest.mark.parametrize("points", [
    [[0.0, 0.0], [0.0, 0.0], [0.0, 6.0], [0.0, 6.0]],            # no cross-axis width
    [[1.0, 3.0], [-1.0, 3.0], [-1.0, 3.0], [1.0, 3.0]],          # no length along the axis
    [[0.0, -1.0], [0.0, 1.0], [0.0, 7.0], [0.0, 5.0]],           # every corner on the axis line
    [[1.0, 0.0], [-1.0, 0.0], [-2.0, 6.0], [2.0, 6.0]],          # the two cross edges differ
    [[2.0, 0.0], [0.0, 0.0], [0.0, 6.0], [2.0, 6.0]],            # not centred on the axis
    [[1.0, 0.0], [-1.0, 0.0], [-1.0, 7.0], [1.0, 7.0]],          # the far edge misses the axis end
    [[-1.0, 0.0], [1.0, 0.0], [-1.0, 6.0], [1.0, 6.0]],          # corners out of order (a bow tie)
    [[1.000001, 0.0], [-1.0, 0.0], [-1.0, 6.0], [1.0, 6.0]],     # one corner a millionth off
], ids=["zero-width", "zero-length", "collinear", "trapezoid", "off-axis", "wrong-end", "bow-tie", "nudged"])
def test_ground_outline_geometry_refusals(graph, points):
    frame = small_frame(graph)
    frame["extra"]["ground_outline"]["points"] = points
    refused("GROUND_OUTLINE_POINTS_INVALID", "extra.ground_outline.points", frame)


@pytest.mark.parametrize("change", ["no-tracker", "tracker-list", "no-axis-start", "axis-text", "axis-moved"])
def test_ground_outline_must_agree_with_the_frames_own_tracker_axis(graph, change):
    frame = small_frame(graph)
    if change == "no-tracker":
        del frame["tracker"]
    elif change == "tracker-list":
        frame["tracker"] = []
    elif change == "no-axis-start":
        del frame["tracker"]["axis_start"]
    elif change == "axis-text":
        frame["tracker"]["axis_end"] = "0,6"
    else:
        frame["tracker"]["axis_start"] = [0.5, 0.0]
    refused("GROUND_OUTLINE_POINTS_INVALID", "extra.ground_outline.points", frame)


def test_ground_outline_tolerance_follows_the_coordinate_scale():
    s = 987654321.0
    points = [[s+1, s], [s-1, s], [s-1, s+6], [s+1, s+6]]
    nudged = copy.deepcopy(points)
    nudged[0][0] += 4 * math.ulp(s)
    assert og.frame_outline(frame(nudged, [s, s], [s, s+6])) == tuple(tuple(p) for p in nudged)
    nudged = copy.deepcopy(points)
    nudged[0][0] += 1e-3
    refused(PI, PP, frame(nudged, [s, s], [s, s+6]))


@pytest.mark.parametrize("case", ["bow-tie-far", "trapezoid-far", "parallelogram"])
def test_ground_outline_refuses_what_a_scaled_tolerance_accepted(case):
    s = 999999990.0
    start, end = [s, s], [s, s+6]
    if case == "bow-tie-far":
        points = [[s+.1, s], [s-.1, s], [s+.1, s+6], [s-.1, s+6]]
    elif case == "trapezoid-far":
        points = [[s+.1, s], [s-.1, s], [s-.4, s+6], [s+.4, s+6]]
    else:
        points = [[1, 1], [-1, -1], [-1, 5], [1, 7]]
        start, end = [0, 0], [0, 6]
    refused(PI, PP, frame(points, start, end))


def test_ground_outline_accepts_a_rectangle_at_far_coordinates():
    s = 999999990.0
    points = [[s+1.0, s], [s-1.0, s], [s-1.0, s+6], [s+1.0, s+6]]
    assert og.frame_outline(frame(points, [s, s], [s, s+6])) == tuple(tuple(p) for p in points)


@pytest.mark.parametrize("case", ["start-x", "start-y", "end-x", "end-y"])
def test_ground_outline_each_midpoint_check_is_isolated(case):
    start = [0, 0]
    if case.endswith("x"):
        end = [0, 6]
        points = [[1, 0], [-1, 0], [-1, 6], [1, 6]]
    else:
        end = [6, 0]
        points = [[0, -1], [0, 1], [6, 1], [6, -1]]
    assert og.frame_outline(frame(points, start, end)) == tuple(tuple(p) for p in points)
    cx, cy = end if case.startswith("start") else start
    theta = 1e-9
    cosine, sine = math.cos(theta), math.sin(theta)
    rotated = [[cx + (x-cx)*cosine - (y-cy)*sine,
                cy + (x-cx)*sine + (y-cy)*cosine] for x, y in points]
    refused(PI, PP, frame(rotated, start, end))


@pytest.mark.parametrize("coordinate", ["x", "y"])
def test_ground_outline_each_cross_agreement_check_is_isolated(coordinate):
    d = 1e-9
    if coordinate == "x":
        end = [0, 6]
        points = [[1, 0], [-1, 0], [-1-d/2, 6], [1+d/2, 6]]
    else:
        end = [6, 0]
        points = [[0, -1], [0, 1], [6, 1+d/2], [6, -1-d/2]]
    refused(PI, PP, frame(points, [0, 0], end))


@pytest.mark.parametrize("case", ["zero-width", "zero-length", "below-floor"])
def test_ground_outline_unresolvable_edges_are_refused(case):
    end = [0, 1]
    if case == "zero-width":
        points = [[0, 0], [0, 0], [0, 1], [0, 1]]
    elif case == "zero-length":
        end = [0, 0]
        points = [[1, 0], [-1, 0], [-1, 0], [1, 0]]
    else:
        h = 2.0 ** -43   # a cross edge of exactly 64 tolerances at unit scale: at the floor, not above it
        points = [[h, 0], [-h, 0], [-h, 1], [h, 1]]
    refused(PI, PP, frame(points, [0, 0], end))


def test_ground_outline_accepts_an_edge_just_above_the_floor():
    h = 2.0 ** -43 * (1 + 2.0 ** -6)   # 65 tolerances
    points = [[h, 0], [-h, 0], [-h, 1], [h, 1]]
    assert og.frame_outline(frame(points, [0, 0], [0, 1])) == tuple(tuple(p) for p in points)


@pytest.mark.parametrize("s", [0.0, 1.0e7, 999999990.0, 2.0 ** 30, 1999999990.0],
                         ids=["origin", "1e7", "near-1e9", "2^30", "near-2e9"])
def test_ground_outline_refuses_a_sheared_footprint_near_the_edge_floor(s):
    # Cross edges equal, both midpoints exactly on the axis, edges about five tolerances long, 63 degrees.
    h = 2.5 * og.OUTLINE_ULPS * math.ulp(max(1.0, s))
    points = [[s+h, s], [s-h, s], [s, s+2*h], [s+2*h, s+2*h]]
    refused(PI, PP, frame(points, [s, s], [s+h, s+2*h]))


def test_ground_outline_squareness_allowance_is_half_a_tolerance_per_edge():
    # Edges of 128 tolerances at 2**30 (one tolerance is 2**-18), cross edges equal, midpoints exact: only
    # the squareness check separates the two cases.
    s, e = 2.0 ** 30, 2.0 ** -11

    def sheared(d):
        points = [[s, s], [s+e, s], [s+e+d, s+e], [s+d, s+e]]
        return frame(points, [s+e/2, s], [s+e/2+d, s+e]), points

    value, points = sheared(2.0 ** -19)      # half a tolerance of shear: inside the allowance
    assert og.frame_outline(value) == tuple(tuple(p) for p in points)
    value, points = sheared(2.0 ** -17)      # two tolerances: a 2.0 allowance accepted this
    refused(PI, PP, value)


@pytest.mark.parametrize("ax,h,over_h", [(1999999999.5, 0.5, 1.0), (1999999999, 1, 2)],
                         ids=["float", "int"])
def test_ground_outline_coordinate_bound_is_enforced_on_a_valid_footprint(ax, h, over_h):
    zero, six = (0.0, 6.0) if type(ax) is float else (0, 6)
    start, end = [ax, zero], [ax, six]
    points = [[ax+h, zero], [ax-h, zero], [ax-h, six], [ax+h, six]]
    assert og.frame_outline(frame(points, start, end)) == tuple(tuple(p) for p in points)
    points = [[ax+over_h, zero], [ax-over_h, zero], [ax-over_h, six], [ax+over_h, six]]
    refused(PI, PP, frame(points, start, end))


def test_ground_outline_point_units_must_be_the_exact_str():
    class S(str):
        pass

    value = frame(copy.deepcopy(GOOD), [0, 0], [0, 6])
    value["extra"]["ground_outline"]["point_units"] = S("drawing")
    refused(og.ENVELOPE_INVALID, og.ENVELOPE_PATH, value)
    value["extra"]["ground_outline"]["point_units"] = "drawing"
    assert og.frame_outline(value) == SMALL[0]


def test_ground_outline_conversion_refuses_an_unresolvable_footprint(graph):
    with pytest.raises(conv.GroundConversionError) as error:
        convert(graph, small_doc([drawn(1e7, 1e7, 1e7+6, 1e7, 1, width=1e-7)]))
    assert error.value.code == "GROUND_CONVERSION_INPUT_INVALID" and str(error.value) == error.value.code
    result = convert(graph, small_doc([drawn(1e7, 1e7, 1e7+6, 1e7, 1, width=2.0)]))
    assert len(result["frames"]) == 1
    points = result["frames"][0]["extra"]["ground_outline"]["points"]
    assert og.frame_outline(result["frames"][0]) == tuple(tuple(p) for p in points)


def test_ground_outline_conversion_never_stores_a_shared_list(graph):
    result = convert(graph)
    first = result["frames"][0]["extra"]["ground_outline"]["points"]
    first[0][0] = 99.0
    again = convert(graph)["frames"][0]["extra"]["ground_outline"]["points"]
    assert again[0] == [1.0, 0.0]
