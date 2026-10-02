"""Units series U4: a converted Ground frame stores metres on a metre drawing and on a feet drawing.

One physical site (the conversion suite's SMALL_ROWS) is drawn once in metres and once in feet. Ordinary frame
geometry (insertion point, module dimensions, slot centres) must agree in metres; the tracker axes and the stored
footprint stay in drawing units. No absolute graph digest is pinned here.
"""
import copy

import pytest

import solar_design_graph as sdg
import solar_ground_graph_codec as codec
import solar_ground_outlines as outlines
import solar_local_graph
import test_solar_ground_conversion as tc
import test_solar_ground_equipment as te
from test_solar_ground_equipment import graph, service, pinned  # noqa: F401

FT = 0.3048
UNITS = (("m", 1.0), ("ft", FT))

METRE_FRAMES = [
    {"insertion": [0.0, 3.0], "along": 2.0, "across": 2.0,
     "centres": [[0.0, 1.0], [0.0, 3.0], [0.0, 5.0]],
     "axis": [[0.0, 0.0], [0.0, 6.0]], "metres": [6.0, 2.0],
     "outline": [[1.0, 0.0], [-1.0, 0.0], [-1.0, 6.0], [1.0, 6.0]]},
    {"insertion": [4.0, 5.0], "along": 5.0, "across": 1.0,
     "centres": [[4.0, 2.5], [4.0, 7.5]],
     "axis": [[4.0, 0.0], [4.0, 10.0]], "metres": [10.0, 1.0],
     "outline": [[4.5, 0.0], [3.5, 0.0], [3.5, 10.0], [4.5, 10.0]]},
]
FEET_AXES = [
    [[0.0, 0.0], [0.0, 19.68503937007874]],
    [[13.123359580052492, 0.0], [13.123359580052492, 32.808398950131235]],
]
FEET_OUTLINES = [
    [[3.280839895013123, 0.0], [-3.280839895013123, 0.0],
     [-3.280839895013123, 19.68503937007874], [3.280839895013123, 19.68503937007874]],
    [[14.763779527559054, 0.0], [11.48293963254593, 0.0],
     [11.48293963254593, 32.808398950131235], [14.763779527559054, 32.808398950131235]],
]
OVERLAP = {"distinct_centres": 5, "centres_in_own_outline": 5, "centres_in_two_outlines": 0,
           "centres_in_two_outlines_same_source_command": 0, "centres_in_more_than_two_outlines": 0}
REPORT = {"schema": "leaf.solar-ground-conversion.v1", "counts": {"trackers": 2, "slots": 5},
          "overlap": OVERLAP, "panel_group_colour": 2}
STRING_FEET = [13.123359580052492, 16.404199475065617]
STRING_ROUTES = [[[0.0, 1.0], [0.0, 3.0], [0.0, 5.0]], [[4.0, 2.5], [4.0, 7.5]]]


def near(expected):
    return pytest.approx(expected, rel=1e-12, abs=1e-12)


def site(mpu):
    """SMALL_ROWS, the same physical site, expressed in a drawing whose unit is mpu metres."""
    rows = copy.deepcopy(tc.SMALL_ROWS)
    for row in rows:
        row["axis_start"] = [value / mpu for value in row["axis_start"]]
        row["axis_end"] = [value / mpu for value in row["axis_end"]]
        row["cross_axis_width_du"] = row["cross_axis_width_du"] / mpu
    return rows


def based(w1, name, mpu):
    g = tc.ground_base(w1)
    g["project"]["units"] = dict(g["project"]["units"], drawing_units=name, meters_per_unit=mpu,
                                 drawing_unit_is_feet=name == "ft")
    g["project"]["zip_code"] = te.ZIP
    return g


def frames_of(result):
    out = []
    for frame in result["frames"]:
        flat = codec.decode_slots(frame["ground_slots"]).centres
        out.append({"insertion": frame["insertion_point"], "along": frame["module_width_along_row"],
                    "across": frame["module_height_across_row"],
                    "centres": [list(flat[i:i + 2]) for i in range(0, len(flat), 2)],
                    "axis": [frame["tracker"]["axis_start"], frame["tracker"]["axis_end"]],
                    "metres": [frame["tracker"]["length_m"], frame["tracker"]["cross_axis_width_m"]],
                    "outline": [list(point) for point in outlines.frame_outline(frame)]})
    return out


def converted(w1, name, mpu):
    return tc.convert(w1, tc.small_doc(site(mpu), units=name), based(w1, name, mpu))


def tool_run(w1, name, mpu):
    g = based(w1, name, mpu)
    doc = tc.small_doc(site(mpu), units=name)
    before = copy.deepcopy((g, doc))
    builtin = solar_local_graph._load_builtin("solar-trackers-to-panel-groups")
    done = builtin.run(g, {"expected_rev": 0}, physical_state={"view": copy.deepcopy(tc.VIEW), "document": doc})
    return done, (g, doc), before


def chain(w1, name, mpu, add):
    done, _, _ = tool_run(w1, name, mpu)
    g = te.strung(te.sized(done), add)
    sdg._reset_validation_caches()
    sdg.validate_graph(copy.deepcopy(g))
    return g


def test_ground_units_metre_drawing_keeps_layout_values(graph):
    assert frames_of(converted(graph, "m", 1.0)) == METRE_FRAMES


def test_ground_units_feet_drawing_stores_metres(graph):
    feet = frames_of(converted(graph, "ft", FT))
    assert len(feet) == len(METRE_FRAMES)
    for got, want in zip(feet, METRE_FRAMES):
        assert got["insertion"] == near(want["insertion"])
        assert got["along"] == near(want["along"])
        assert got["across"] == near(want["across"])
        assert len(got["centres"]) == len(want["centres"])
        for point, expected in zip(got["centres"], want["centres"]):
            assert point == near(expected)
    assert abs(feet[0]["insertion"][1] - 9.84251968503937) > 1.0
    assert abs(feet[0]["along"] - 6.561679790026247) > 1.0
    assert abs(feet[1]["insertion"][0] - 13.123359580052492) > 1.0


def test_ground_units_feet_axes_and_outline_stay_in_drawing_units(graph):
    feet = frames_of(converted(graph, "ft", FT))
    for got, axis, outline, want in zip(feet, FEET_AXES, FEET_OUTLINES, METRE_FRAMES):
        for point, expected in zip(got["axis"], axis):
            assert point == near(expected)
        assert len(got["outline"]) == 4
        for point, expected in zip(got["outline"], outline):
            assert list(point) == near(expected)
        assert got["metres"] == near(want["metres"])


def test_ground_units_counts_and_overlap_agree(graph):
    metre = converted(graph, "m", 1.0)
    feet = converted(graph, "ft", FT)
    assert metre["counts"] == feet["counts"] == {"trackers": 2, "slots": 5}
    assert metre["overlap"] == feet["overlap"] == OVERLAP


def test_ground_units_string_lengths_agree(graph, service, pinned):
    for name, mpu in UNITS:
        strings = chain(graph, name, mpu, pinned)["strings"]
        assert [string["length_ft"] for string in strings] == near(STRING_FEET)
        assert len(strings) == len(STRING_ROUTES)
        for string, route in zip(strings, STRING_ROUTES):
            assert len(string["route"]) == len(route)
            for point, expected in zip(string["route"], route):
                assert list(point) == near(expected)


@pytest.mark.parametrize("name, mpu", [("mm", 0.001), ("in", 0.0254)])
def test_ground_units_unsupported_units_still_refused(graph, name, mpu):
    with pytest.raises(Exception) as refused:
        tc.small_doc(site(mpu), units=name)
    assert type(refused.value).__name__ == "PhysicalStateError"
    assert str(refused.value) == "PHYSICAL_STATE_UNITS_UNSUPPORTED"
    doc = tc.small_doc(site(mpu), units="m")
    doc["units"] = {"drawing_units": name, "meters_per_unit": mpu}
    with pytest.raises(Exception) as direct:
        tc.convert(graph, doc, based(graph, name, mpu))
    assert type(direct.value).__name__ == "GroundConversionError"
    assert str(direct.value) == "GROUND_CONVERSION_INPUT_INVALID"


def test_ground_units_mismatch_still_refused(graph):
    with pytest.raises(Exception) as refused:
        tc.convert(graph, tc.small_doc(units="m"), based(graph, "ft", FT))
    assert type(refused.value).__name__ == "GroundConversionError"
    assert str(refused.value) == "GROUND_CONVERSION_UNITS_MISMATCH"


@pytest.mark.parametrize("name, mpu", UNITS)
def test_ground_units_tool_path_agrees(graph, name, mpu):
    done, inputs, before = tool_run(graph, name, mpu)
    assert done["rev"] == 1
    assert inputs == before
    assert done["extra"]["ground_conversion"] == REPORT
    frames = frames_of({"frames": done["frames"]})
    assert len(frames) == len(METRE_FRAMES)
    for got, want in zip(frames, METRE_FRAMES):
        assert got["insertion"] == near(want["insertion"])
        assert got["along"] == near(want["along"])
        assert got["across"] == near(want["across"])
        for point, expected in zip(got["centres"], want["centres"]):
            assert point == near(expected)
    sdg._reset_validation_caches()
    sdg.validate_graph(copy.deepcopy(done))


def test_ground_units_inputs_not_mutated(graph):
    for name, mpu in UNITS:
        g = based(graph, name, mpu)
        doc = tc.small_doc(site(mpu), units=name)
        before = copy.deepcopy((g, doc))
        tc.convert(graph, doc, g)
        assert (g, doc) == before


def test_ground_units_deterministic(graph):
    first = tc.canon_sha(converted(graph, "ft", FT))
    second = tc.canon_sha(converted(graph, "ft", FT))
    assert first == second
