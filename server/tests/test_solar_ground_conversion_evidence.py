"""sf-w3-conversion evidence slice: the panel geometry LEAFTRACKERSTOPANELGROUPS writes, frozen on
the committed b18 terrain state, and panel-groups readiness on a Ground project."""
import copy
import hashlib
import importlib.util
import json
import math
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
ROOT = SERVER.parent
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import product_capability_availability as availability  # noqa: E402
from solar_design_graph import GraphValidationError, validate_graph  # noqa: E402
from test_solar_tool_solar_panel_groups import _sized, builtin, params  # noqa: E402
from test_w1_design_graph import graph  # noqa: E402,F401
from test_w1_sizing_groups import passing, service  # noqa: E402,F401

FIXTURE = ROOT / "docs" / "parity" / "evidence" / "ground" / "terrain" / "trackers-to-panelgroups-geometry.json"
FIXTURE_SHA256 = "c62e525b8f2b2cbdaf11f86e01ed63a149779a47a362f9231dac7a4c9a8d0ef2"
TOOL = "solar-panel-groups"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ds = _load("solar_ground_dsteps", SERVER / "solar_ground_dsteps.py")


def canon_sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def frame(x0, y0, x1, y1, **extra):
    return dict({"kind": "polyline", "layer": "LEAF-TRACKERS",
                 "vertices": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]}, **extra)


def drawn(ax, ay, bx, by, slots, width=2.0, row_index=1):
    return {"kind": "tracker", "axis_start": [ax, ay], "axis_end": [bx, by], "slots": slots,
            "row_index": row_index, "cross_axis_width_du": width}


SMALL = [drawn(0.0, 0.0, 0.0, 6.0, 3), frame(10.0, 0.0, 12.0, 10.0, slots=2, row_index=4)]
SMALL_EXPECTED = {
    "trackers": [
        {"ordinal": 0, "entity_index": 0, "entity_kind": "tracker", "group_number": 1, "row_index": 1,
         "module_slots": 3, "axis_start": [0.0, 0.0], "axis_end": [0.0, 6.0],
         "half_cross_axis": [-1.0, 0.0], "center": [0.0, 3.0],
         "outline": [[1.0, 0.0], [-1.0, 0.0], [-1.0, 6.0], [1.0, 6.0]],
         "row_angle_rad": 1.5707963267948966, "slot_length_du": 2.0, "panel_height_du": 2.0,
         "panels": [[0.0, 1.0], [0.0, 3.0], [0.0, 5.0]]},
        {"ordinal": 1, "entity_index": 1, "entity_kind": "polyline", "group_number": 2, "row_index": 4,
         "module_slots": 2, "axis_start": [11.0, 0.0], "axis_end": [11.0, 10.0],
         "half_cross_axis": [1.0, 0.0], "center": [11.0, 5.0],
         "outline": [[10.0, 0.0], [12.0, 0.0], [12.0, 10.0], [10.0, 10.0]],
         "row_angle_rad": 1.5707963267948966, "slot_length_du": 5.0, "panel_height_du": 2.0,
         "panels": [[11.0, 2.5], [11.0, 7.5]]},
    ],
    "panel_groups_created": 2, "panel_count": 5,
    "settings": {"PanelGroupNumber": 3, "PanelGroupColour": 2},
}


@pytest.fixture(scope="module")
def b18():
    evidence = _load("solar_ground_dsteps_evidence", ROOT / "scripts" / "solar_ground_dsteps_evidence.py")
    raw = (ROOT / "docs" / "parity" / "evidence" / "ground" / "terrain" / "intake.json").read_bytes()
    state = evidence.b18_state(json.loads(raw.decode("utf-8")))
    ents = evidence.bev.tracker_entities(state)
    layout = ds.tracker_panel_layout(ents, evidence.bev.MPU, state.get("settings"))
    return {"raw": raw, "state": state, "ents": ents, "mpu": evidence.bev.MPU, "layout": layout}


def test_ground_conversion_small_layout_is_exact():
    before = copy.deepcopy(SMALL)
    assert ds.tracker_panel_layout(SMALL) == SMALL_EXPECTED
    assert SMALL == before


def test_ground_conversion_group_numbers_follow_stored_counters():
    stored = {"PanelGroupNumber": 238, "PanelGroupColour": 237}
    out = ds.tracker_panel_layout(SMALL, stored=stored)
    assert [t["group_number"] for t in out["trackers"]] == [238, 239]
    assert out["settings"] == {"PanelGroupNumber": 240, "PanelGroupColour": 239}
    assert stored == {"PanelGroupNumber": 238, "PanelGroupColour": 237}


@pytest.mark.parametrize("ents,expected", [
    ([drawn(1.0, 1.0, 4.0, -3.0, 2, width=1.0)],
     {"ordinal": 0, "entity_index": 0, "entity_kind": "tracker", "group_number": 1, "row_index": 1,
      "module_slots": 2, "axis_start": [1.0, 1.0], "axis_end": [4.0, -3.0], "half_cross_axis": [0.4, 0.3],
      "center": [2.5, -1.0], "outline": [[0.6, 0.7], [1.4, 1.3], [4.4, -2.7], [3.6, -3.3]],
      "row_angle_rad": 5.355890089177974, "slot_length_du": 2.5, "panel_height_du": 1.0,
      "panels": [[1.75, 0.0], [3.25, -2.0]]}),
    ([drawn(5.0, 0.0, 1.0, 0.0, 4, width=0.5)],
     {"ordinal": 0, "entity_index": 0, "entity_kind": "tracker", "group_number": 1, "row_index": 1,
      "module_slots": 4, "axis_start": [5.0, 0.0], "axis_end": [1.0, 0.0], "half_cross_axis": [-0.0, -0.25],
      "center": [3.0, 0.0], "outline": [[5.0, 0.25], [5.0, -0.25], [1.0, -0.25], [1.0, 0.25]],
      "row_angle_rad": 3.141592653589793, "slot_length_du": 1.0, "panel_height_du": 0.5,
      "panels": [[4.5, 0.0], [3.5, 0.0], [2.5, 0.0], [1.5, 0.0]]}),
    ([{"kind": "tracker", "axis_start": [0, 0], "axis_end": [4, 0], "slots": 2, "row_index": 0,
       "scale": [4.0, -3.0, 1.0]}],
     {"ordinal": 0, "entity_index": 0, "entity_kind": "tracker", "group_number": 1, "row_index": 0,
      "module_slots": 2, "axis_start": [0.0, 0.0], "axis_end": [4.0, 0.0], "half_cross_axis": [-0.0, 1.5],
      "center": [2.0, 0.0], "outline": [[0.0, -1.5], [0.0, 1.5], [4.0, 1.5], [4.0, -1.5]],
      "row_angle_rad": 0.0, "slot_length_du": 2.0, "panel_height_du": 3.0,
      "panels": [[1.0, 0.0], [3.0, 0.0]]}),
], ids=["diagonal", "west", "scale-width"])
def test_ground_conversion_orientation_cases(ents, expected):
    out = ds.tracker_panel_layout(ents)
    assert out["trackers"] == [expected]
    assert [math.copysign(1.0, v) for v in out["trackers"][0]["half_cross_axis"]] == [
        math.copysign(1.0, v) for v in expected["half_cross_axis"]]
    assert (out["panel_groups_created"], out["panel_count"]) == (1, expected["module_slots"])


def test_ground_conversion_counts_agree_with_trackers_to_panel_groups():
    ents = [frame(0.0, 0.0, 2.0, 10.0), drawn(5.0, 0.0, 5.0, 10.0, 84), frame(10.0, 0.0, 12.0, 10.0, slots=12),
            {"kind": "polyline", "layer": "LEAF-TRACKERS", "slots": 6, "vertices": [[1, 1], [1, 1], [1, 1], [1, 1]]},
            drawn(20.0, 0.0, 20.0, 10.0, 0)]
    out = ds.tracker_panel_layout(ents)
    assert [(t["entity_index"], t["entity_kind"], t["module_slots"]) for t in out["trackers"]] == [
        (1, "tracker", 84), (2, "polyline", 12)]
    assert (out["panel_groups_created"], out["panel_count"]) == (2, 96)
    assert ds.trackers_to_panel_groups(ents) == {"trackers": 2, "panel_groups_created": 2, "panel_group_slots": 96}
    assert canon_sha(out) == "1b160845c173108f986194a7ed113b2849342ade53d6a30809c659dce670e1d1"


@pytest.mark.parametrize("ents,stored,message", [
    ([drawn(0.0, 0.0, 0.0, 6.0, 3, width=0.0)], None, "a drawn tracker needs a positive cross-axis width"),
    ([{"kind": "tracker", "axis_start": [0, 0], "axis_end": [0, 6], "slots": 3, "row_index": 0}], None,
     "a drawn tracker needs cross_axis_width_du or a block scale"),
    ([drawn(0.0, 0.0, 0.0, 6.0, 3, width=float("nan"))], None,
     "tracker cross-axis width must be finite and within 1e+15, got nan"),
    ("x", None, "entities must be a list"),
    (SMALL, [], "stored settings must be an object"),
    (SMALL, {"PanelGroupNumber": "1"}, "PanelGroupNumber must be a 32-bit integer"),
    (SMALL, {"PanelGroupNumber": True}, "PanelGroupNumber must be a 32-bit integer"),
], ids=["zero-width", "no-width-no-scale", "nan-width", "not-a-list", "stored-not-object",
        "stored-string-number", "stored-bool-number"])
def test_ground_conversion_refusals(ents, stored, message):
    with pytest.raises(ds.DStepsInputError) as error:
        ds.tracker_panel_layout(ents, stored=stored)
    assert str(error.value) == message


def test_ground_conversion_panel_bound(monkeypatch):
    assert ds.MAX_LAYOUT_PANELS == 1_000_000
    monkeypatch.setattr(ds, "MAX_LAYOUT_PANELS", 4)
    with pytest.raises(ds.DStepsInputError) as error:
        ds.tracker_panel_layout(SMALL)
    assert str(error.value) == "more than 4 module slots"
    monkeypatch.setattr(ds, "MAX_LAYOUT_PANELS", 5)
    assert ds.tracker_panel_layout(SMALL)["panel_count"] == 5


def test_ground_conversion_panel_bound_precedes_panel_list(monkeypatch):
    def no_panel_range(*args):
        raise AssertionError("panel list started before the slot bound refusal")

    monkeypatch.setattr(ds, "MAX_LAYOUT_PANELS", 4)
    monkeypatch.setattr(ds, "range", no_panel_range, raising=False)
    with pytest.raises(ds.DStepsInputError, match="^more than 4 module slots$"):
        ds.tracker_panel_layout([drawn(0, 0, 0, 6, 5)])


@pytest.mark.parametrize("length", [0.0, ds.TRACKER_EPSILON], ids=["zero", "epsilon"])
def test_ground_conversion_drawn_axis_must_be_non_degenerate(length):
    ent = drawn(0, 0, length, 0, 3)
    with pytest.raises(ds.DStepsInputError, match="^a drawn tracker needs a non-degenerate axis$"):
        ds.tracker_panel_layout([ent])
    assert ds.trackers_to_panel_groups([ent]) == {
        "trackers": 1, "panel_groups_created": 1, "panel_group_slots": 3}


def test_ground_conversion_drawn_axis_magnitude_is_bounded():
    with pytest.raises(ds.DStepsInputError):
        ds.tracker_panel_layout([drawn(0, 0, 1e308, 0, 3)])
    json.dumps(ds.tracker_panel_layout(SMALL), allow_nan=False)


@pytest.mark.parametrize("ent,field", [
    (drawn(0, 0, 1e16, 0, 3), "axis_end.x"),
    (drawn(0, 0, 10, 0, 3, width=1e16), "tracker cross-axis width"),
    (drawn(1e16, 0, 1e16 + 10, 0, 3), "axis_start.x"),
], ids=["axis-end", "width", "axis-start"])
def test_ground_conversion_finite_magnitude_over_the_bound_is_refused(ent, field):
    with pytest.raises(ds.DStepsInputError) as error:
        ds.tracker_panel_layout([ent])
    assert field in str(error.value)
    assert "within 1e+15" in str(error.value)


def test_ground_conversion_magnitude_at_the_bound_is_accepted():
    result = ds.tracker_panel_layout([drawn(0, 0, 1e15, 0, 3)])
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("ents,mpu", [
    ([drawn(0, 0, 0, 6, 3, width=10**400)], 1.0),
    ([drawn(0, 0, 0, 6, 10**400)], 1.0),
    ([drawn(0, 0, 0, 6, 3, row_index=10**400)], 1.0),
    ([drawn(0, 0, float("nan"), 6, 3)], 1.0),
    ([drawn(0, 0, 0, 6, True)], 1.0),
    ([drawn(0, 0, 0, 6, 3)], float("nan")),
    ([{"kind": "polyline", "layer": "LEAF-TRACKERS", "slots": 3,
       "vertices": [[0, 0]] * 20_001}], 1.0),
], ids=["huge-width", "huge-slots", "huge-row-index", "nan-axis", "bool-slots",
        "nan-meters-per-unit", "too-many-vertices"])
def test_ground_conversion_reader_refusals_are_dsteps_input_errors(ents, mpu):
    with pytest.raises(ds.DStepsInputError):
        ds.tracker_panel_layout(ents, meters_per_unit=mpu)


@pytest.mark.parametrize("error_type", [ds._bo.BuildoutInputError, ds._bo.BuildoutBoundsError,
                                       OverflowError, ZeroDivisionError])
def test_ground_conversion_boundary_preserves_refusal_messages(monkeypatch, error_type):
    def refuse(*args):
        raise error_type("original reader refusal")

    monkeypatch.setattr(ds._bo, "read_tracker_rows", refuse)
    with pytest.raises(ds.DStepsInputError, match="^original reader refusal$"):
        ds.tracker_panel_layout(SMALL)
    with pytest.raises(error_type, match="^original reader refusal$"):
        ds.trackers_to_panel_groups(SMALL)


def test_ground_conversion_boundary_passes_other_exceptions(monkeypatch):
    failure = RuntimeError("unexpected reader failure")

    def refuse(*args):
        raise failure

    monkeypatch.setattr(ds._bo, "read_tracker_rows", refuse)
    with pytest.raises(RuntimeError) as error:
        ds.tracker_panel_layout(SMALL)
    assert error.value is failure


def test_ground_conversion_output_guard_refuses_non_finite_numbers(monkeypatch):
    monkeypatch.setattr(ds, "_half_cross_axis", lambda ent, row: (float("nan"), 0.0))
    with pytest.raises(ds.DStepsInputError, match="^tracker panel layout must emit only finite numbers$"):
        ds.tracker_panel_layout([drawn(0, 0, 0, 6, 3)])


def test_ground_conversion_polyline_cross_axis_guard_is_independent():
    ent = {"kind": "polyline", "layer": "LEAF-TRACKERS", "slots": 3,
           "vertices": [[0, 0], [0, 0], [1, 6], [-1, 6]]}
    row = ds._bo.read_tracker_rows([ent], 1.0)[0]
    assert row["axis_start"] == (0.0, 0.0)
    assert row["axis_end"] == (0.0, 6.0)
    assert ds.tracker_panel_layout([ent]) == {
        "trackers": [], "panel_groups_created": 0, "panel_count": 0,
        "settings": {"PanelGroupNumber": 1, "PanelGroupColour": 0}}
    assert ds.trackers_to_panel_groups([ent]) == {
        "trackers": 0, "panel_groups_created": 0, "panel_group_slots": 0}


def test_ground_conversion_b18_fixture_pins(b18):
    raw = FIXTURE.read_bytes()
    assert hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest() == FIXTURE_SHA256
    fixture = json.loads(raw.decode("utf-8"))
    assert fixture["source"]["intake_sha256"] == hashlib.sha256(b18["raw"]).hexdigest()
    assert fixture["source"]["meters_per_unit"] == b18["mpu"] == 1.0
    layout = b18["layout"]
    ents = b18["ents"]
    assert fixture["counts"]["trackers"] == layout["panel_groups_created"] == 237
    assert fixture["counts"]["panels"] == layout["panel_count"] == 69678
    assert fixture["counts"]["settings"] == layout["settings"] == {"PanelGroupNumber": 238, "PanelGroupColour": 237}
    by_cmd = {}
    for tracker in layout["trackers"]:
        cmd = ents[tracker["entity_index"]]["source_command"]
        row = by_cmd.setdefault(cmd, {"trackers": 0, "panels": 0, "first_ordinal": tracker["ordinal"],
                                      "last_ordinal": tracker["ordinal"]})
        row["trackers"] += 1
        row["panels"] += tracker["module_slots"]
        row["last_ordinal"] = tracker["ordinal"]
    assert by_cmd == fixture["counts"]["by_source_command"]
    assert canon_sha(layout) == fixture["digests"]["layout"]
    assert canon_sha([{k: v for k, v in t.items() if k != "panels"} for t in layout["trackers"]]) == \
        fixture["digests"]["trackers_without_panels"]
    assert canon_sha([t["panels"] for t in layout["trackers"]]) == fixture["digests"]["panels"]
    for sample in fixture["samples"]:
        tracker = layout["trackers"][sample["ordinal"]]
        assert {k: v for k, v in tracker.items() if k != "panels"} == {
            k: v for k, v in sample.items() if k not in ("source_command", "panel_samples")}
        assert ents[tracker["entity_index"]]["source_command"] == sample["source_command"]
        for panel in sample["panel_samples"]:
            assert tracker["panels"][panel["slot"]] == panel["centre"]


def test_ground_conversion_b18_matches_d3_receipt_counts(b18):
    receipt = json.loads((ROOT / "docs" / "parity" / "receipts" / "trackers-to-panelgroups" /
                          "ground-terrain-d3.json").read_text(encoding="utf-8"))
    rows = {row["name"]: row["value"] for row in receipt["comparison"]["plugin"]["after"]["rows"]}
    layout = b18["layout"]
    assert rows == {"panel-group-slots": layout["panel_count"],
                    "panel-groups-created": layout["panel_groups_created"],
                    "PanelGroupColour": layout["settings"]["PanelGroupColour"],
                    "PanelGroupNumber": layout["settings"]["PanelGroupNumber"]}
    assert ds.trackers_to_panel_groups(b18["ents"], b18["mpu"]) == {
        "trackers": 237, "panel_groups_created": 237, "panel_group_slots": 69678}


def _inside(tracker, x, y):
    (ax, ay), (bx, by) = tracker["axis_start"], tracker["axis_end"]
    hx, hy = tracker["half_cross_axis"]
    dx, dy = bx - ax, by - ay
    px, py = x - ax, y - ay
    along = (px * dx + py * dy) / (dx * dx + dy * dy)
    across = (px * hx + py * hy) / (hx * hx + hy * hy)
    return 0.0 < along < 1.0 and -1.0 < across < 1.0


def test_ground_conversion_b18_membership(b18):
    layout, ents = b18["layout"], b18["ents"]
    trackers = layout["trackers"]
    boxes, buckets = [], {}
    for k, tracker in enumerate(trackers):
        xs = [p[0] for p in tracker["outline"]]
        ys = [p[1] for p in tracker["outline"]]
        boxes.append((min(xs), max(xs), min(ys), max(ys)))
        for b in range(math.floor(min(xs) / 10.0), math.floor(max(xs) / 10.0) + 1):
            buckets.setdefault(b, []).append(k)
    own = two = two_same = 0
    for k, tracker in enumerate(trackers):
        cmd = ents[tracker["entity_index"]]["source_command"]
        for x, y in tracker["panels"]:
            hits = [j for j in buckets.get(math.floor(x / 10.0), ())
                    if boxes[j][0] <= x <= boxes[j][1] and boxes[j][2] <= y <= boxes[j][3]
                    and _inside(trackers[j], x, y)]
            own += k in hits
            two += len(hits) == 2
            two_same += len(hits) == 2 and ents[trackers[hits[0]]["entity_index"]]["source_command"] == \
                ents[trackers[hits[1]]["entity_index"]]["source_command"] == cmd
            assert len(hits) in (1, 2)
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))["membership"]
    assert fixture == {"distinct_centres": len({(x, y) for t in trackers for x, y in t["panels"]}),
                       "centres_in_own_outline": own, "centres_in_two_outlines": two,
                       "centres_in_two_outlines_same_source_command": two_same}
    assert (own, two, two_same) == (69678, 25284, 0)


def test_ground_conversion_block_formula_agrees(b18):
    """TryBuildLeafBlockSpec (:592-616) builds the axis and half cross axis from the block's
    position, rotation and scale; the layout reads the keyed axis and width. Both agree."""
    worst_axis = worst_half = 0.0
    for tracker in b18["layout"]["trackers"]:
        ent = b18["ents"][tracker["entity_index"]]
        cos_r, sin_r = math.cos(ent["rotation_rad"]), math.sin(ent["rotation_rad"])
        half_len, half_cross = ent["scale"][0] * 0.5, ent["scale"][1] * 0.5
        cx, cy = ent["insert"][0], ent["insert"][1]
        start = (cx - half_len * cos_r, cy - half_len * sin_r)
        end = (cx + half_len * cos_r, cy + half_len * sin_r)
        half = (-half_cross * sin_r, half_cross * cos_r)
        for got, want in ((tracker["axis_start"], start), (tracker["axis_end"], end)):
            worst_axis = max(worst_axis, abs(got[0] - want[0]), abs(got[1] - want[1]))
        worst_half = max(worst_half, abs(tracker["half_cross_axis"][0] - half[0]),
                         abs(tracker["half_cross_axis"][1] - half[1]))
    assert worst_axis < 1e-12 and worst_half < 1e-12


def _ground(graph, passing, sized, design="Ground"):
    value = copy.deepcopy(graph)
    value["frames"] = []
    value["project"]["installation_design"] = design
    for panel, centre, handle in zip(value["panels"], ((1, 0), (2, 0), (3, 0)), ("A1", "A2", "A3")):
        panel.update(frame_ref=None, matrix_cell=None, angle=0, centre=list(centre))
        panel["provenance"]["source_handle"] = handle
    return _sized(value, passing) if sized else value


@pytest.mark.parametrize("sized", [True, False], ids=["sized", "unsized"])
def test_ground_conversion_readiness_refuses_panel_groups_on_ground(graph, passing, service, sized):
    value = _ground(graph, passing, sized)
    assert validate_graph(value) == value
    expected = {"input_ready": False, "input_reason": "roof_installation_required"}
    assert availability.w1_graph_readiness(value)[TOOL] == expected
    state = availability.w1_availability(TOOL, entitled=True, inputs=expected)
    assert state["runnable"] is False and state["refusal_reasons"] == ["roof_installation_required"]


def test_ground_conversion_builtin_still_refuses_ground(graph, passing, service):
    value = _ground(graph, passing, True)
    before = copy.deepcopy(value)
    with pytest.raises(GraphValidationError) as error:
        builtin().run(value, params())
    assert error.value.code == "INSTALLATION_DESIGN_MISMATCH"
    assert value == before


def test_ground_conversion_roof_readiness_unchanged(graph, passing, service):
    value = _ground(graph, passing, True, design="Roof")
    assert availability.w1_graph_readiness(value)[TOOL] == {"input_ready": True, "input_reason": None}
