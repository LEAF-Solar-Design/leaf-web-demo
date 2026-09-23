"""Ground frame engines: LEAFGENERATE, LEAFCOLLISION, LEAFPILING, LEAFCOLLISIONRANGE.

Ports every case of the plugin's pure-engine tests (read-only source at
C:/tmp/solar-parity/wt-b25-s17/Tests/Tests): FramePackerTests.cs,
CollisionDetectorTests.cs, PilePlacerTests.cs, PileTemplateTests.cs, and the read
side of FramePresetStoreTests.cs. Same inputs, same expected numbers. The store
mutation cases (Save, Delete, Import, Export) have no Studio counterpart and are not
ported; Tests.Integration/PilingTests.cs needs a live AutoCAD database.
Then the command-level drawing contract, bounds and refusals.
"""
from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve string annotations through sys.modules
    spec.loader.exec_module(module)
    return module


gf = _load("solar_ground_frames", ROOT / "server" / "solar_ground_frames.py")


# ------------------------------------------------------------------ helpers --

def default_preset():
    """FramePackerTests.cs:28-42 (identical to BuildDefault)."""
    return gf.FramePreset(name="Default", module_length_m=2.384, module_width_m=1.303,
                          module_thickness_m=0.033, module_power_wp=715, framing_type="FixedTilt",
                          orientation="Portrait", rows=4, columns=12, tilt_degrees=20.0,
                          horizontal_gap_m=0.02, vertical_gap_m=0.02)


def rectangle(w, h):
    return [(0, 0), (w, 0), (w, h), (0, h)]


def rect(x0, y0, w, h, row=0, col=0):
    """CollisionDetectorTests.cs:21-33 / PilePlacerTests.cs:25-37."""
    return {"row": row, "col": col, "vertices": [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]}


def default_piling():
    """PilePlacerTests.cs:39-45."""
    return gf.PilingConfig(horizontal_poles_per_frame=4, vertical_poles_per_group=2, pile_diameter_m=0.15,
                           pile_reveal_m=0.0, pile_embedment_m=2.0)


def tracker_entity(verts, row, col, handle=None, layer="LEAF-TRACKERS"):
    ent = {"type": "LWPOLYLINE", "layer": layer, "closed": True, "vertices": verts, "row": row, "col": col}
    if handle:
        ent["handle"] = handle
    return ent


def fixture_entities():
    """res/drawings/leafgenerate_100x200.dwg: one closed rectangle on layer 0."""
    return [{"type": "LWPOLYLINE", "layer": "0", "closed": True, "vertices": rectangle(100.0, 200.0)}]


def canonical_frames(**kwargs):
    store = gf.load_frame_preset_store(None)
    return gf.generate_frames([rectangle(100.0, 200.0)], store.get_active(), 1.0, **kwargs)


def with_handles(entities):
    for i, ent in enumerate(entities):
        ent["handle"] = f"{0x200 + i:X}"
    return entities


def flat(points):
    """pytest.approx does not nest, so compare vertex lists as flat coordinate lists."""
    return [float(c) for p in points for c in p]


def refusal(code, fn, *args, **kwargs):
    with pytest.raises(gf.GroundFramesError) as info:
        fn(*args, **kwargs)
    assert info.value.code == code, str(info.value)


# ======================================================= FramePackerTests.cs --

def test_frame_footprint_portrait_matches_pvcase_math():
    w, h = gf.frame_footprint(default_preset())
    assert w == pytest.approx(5.272, abs=1e-6)
    assert h == pytest.approx(28.828, abs=1e-6)


def test_frame_footprint_landscape_swaps_module_dims():
    preset = default_preset()
    preset.orientation = "Landscape"
    w, h = gf.frame_footprint(preset)
    assert w == pytest.approx(4 * 2.384 + 3 * 0.02, abs=1e-6)
    assert h == pytest.approx(12 * 1.303 + 11 * 0.02, abs=1e-6)


def test_frame_footprint_multi_segment_tracker_pack_uses_pack_length():
    preset = gf.FramePreset(name="Tracker", module_length_m=2.0, module_width_m=1.0, module_power_wp=500,
                            rows=1, columns=4, orientation="Portrait",
                            tracker_pack=gf.TrackerPack.parse_pvcase_tracker_packs("Modules,2; JointGap,1; Modules,2"))
    preset.tracker_pack.joint_gap_width_m = 0.10
    w, h = gf.frame_footprint(preset)
    assert w == pytest.approx(1.0, abs=1e-9)
    assert h == pytest.approx(4.10, abs=1e-9)


def test_pack_canonical_100x200_rectangle_yields_108_frames():
    assert len(gf.pack_frames(rectangle(100.0, 200.0), default_preset(), meters_per_unit=1.0)) == 108


def test_pack_pitch_override_spaces_rows_by_pitch():
    frames = gf.pack_frames(rectangle(100.0, 200.0), default_preset(), meters_per_unit=1.0, pitch_m=40.0)
    assert len(frames) == 18 * 5


def test_pack_every_frame_has_four_vertices_and_closed_rectangle():
    for f in gf.pack_frames(rectangle(100.0, 200.0), default_preset(), 1.0):
        v = f["vertices"]
        assert len(v) == 4
        assert v[0][1] == pytest.approx(v[1][1], abs=1e-6)
        assert v[2][1] == pytest.approx(v[3][1], abs=1e-6)
        assert v[0][0] == pytest.approx(v[3][0], abs=1e-6)
        assert v[1][0] == pytest.approx(v[2][0], abs=1e-6)


def test_pack_every_frame_lies_entirely_inside_boundary():
    for f in gf.pack_frames(rectangle(100.0, 200.0), default_preset(), 1.0):
        for vx, vy in f["vertices"]:
            assert 0 <= vx <= 100 and 0 <= vy <= 200


def test_pack_tiny_boundary_returns_zero_frames():
    assert gf.pack_frames(rectangle(1.0, 1.0), default_preset(), 1.0) == []


def test_pack_feet_drawing_units_scales_correctly():
    mpf = 0.3048
    frames = gf.pack_frames(rectangle(100.0 / mpf, 200.0 / mpf), default_preset(), mpf)
    assert len(frames) == 108


def test_pack_landscape_orientation_uses_landscape_footprint():
    preset = default_preset()
    preset.orientation = "Landscape"
    assert len(gf.pack_frames(rectangle(100.0, 200.0), preset, 1.0)) == 10 * 12


def test_pack_l_shaped_boundary_clips_frames_outside_interior():
    l_shape = [(0, 0), (100, 0), (100, 80), (40, 80), (40, 200), (0, 200)]
    assert len(gf.pack_frames(l_shape, default_preset(), 1.0)) < len(
        gf.pack_frames(rectangle(100.0, 200.0), default_preset(), 1.0))


def test_pack_exclusion_polygon_removes_overlapping_frames():
    exclusion = [(0, 0), (50, 0), (50, 100), (0, 100)]
    baseline = gf.pack_frames(rectangle(100.0, 200.0), default_preset(), 1.0)
    excluded = gf.pack_frames(rectangle(100.0, 200.0), default_preset(), 1.0, exclusion_polys=[exclusion])
    assert len(excluded) < len(baseline)
    for f in excluded:
        assert not any(0 < vx < 50 and 0 < vy < 100 for vx, vy in f["vertices"])


def test_pack_null_exclusion_list_behaves_like_no_exclusion():
    a = gf.pack_frames(rectangle(100.0, 200.0), default_preset(), 1.0)
    b = gf.pack_frames(rectangle(100.0, 200.0), default_preset(), 1.0, exclusion_polys=None)
    assert a == b


def test_pack_slope_filter_rejects_frames_where_centre_exceeds_max():
    preset = default_preset()
    preset.max_slope_percent = 15.0
    filtered = gf.pack_frames(rectangle(100.0, 200.0), preset, 1.0,
                              slope_at_point_percent=lambda x, y: 50.0 if y < 100 else 3.0)
    baseline = gf.pack_frames(rectangle(100.0, 200.0), preset, 1.0)
    assert len(filtered) < len(baseline)
    for f in filtered:
        assert sum(y for _, y in f["vertices"]) / 4 >= 100.0


def test_pack_slope_filter_null_callback_is_noop():
    assert len(gf.pack_frames(rectangle(100.0, 200.0), default_preset(), 1.0, slope_at_point_percent=None)) == 108


def test_circle_to_polygon_produces_vertices_around_circle():
    poly = gf.circle_to_polygon(10.0, 20.0, 5.0, 16)
    assert len(poly) == 16
    for x, y in poly:
        assert math.hypot(x - 10.0, y - 20.0) == pytest.approx(5.0, abs=1e-9)


# ================================================== CollisionDetectorTests.cs --

def test_detect_overlaps_empty_list_returns_empty():
    assert gf.detect_overlaps([]) == []


def test_detect_overlaps_single_frame_never_collides_with_itself():
    assert gf.detect_overlaps([rect(0, 0, 5, 10)]) == []


def test_detect_overlaps_disjoint_frames_returns_empty():
    assert gf.detect_overlaps([rect(0, 0, 5, 10), rect(10, 0, 5, 10), rect(0, 20, 5, 10)]) == []


def test_detect_overlaps_shared_edge_does_not_collide():
    assert gf.detect_overlaps([rect(0, 0, 5, 10), rect(5, 0, 5, 10)]) == []


def test_detect_overlaps_partial_overlap_returns_intersection_box():
    hits = gf.detect_overlaps([rect(0, 0, 10, 10, row=1, col=1), rect(5, 5, 10, 10, row=2, col=2)])
    assert len(hits) == 1
    c = hits[0]
    assert (c["frame_a_index"], c["frame_b_index"]) == (0, 1)
    assert (c["frame_a_row"], c["frame_b_row"]) == (1, 2)
    assert (c["min_x"], c["min_y"], c["max_x"], c["max_y"]) == pytest.approx((5.0, 5.0, 10.0, 10.0), abs=1e-9)


def test_detect_overlaps_full_containment_returns_inner_rectangle():
    hits = gf.detect_overlaps([rect(0, 0, 20, 20), rect(5, 5, 5, 5)])
    assert len(hits) == 1
    assert (hits[0]["min_x"], hits[0]["min_y"], hits[0]["max_x"], hits[0]["max_y"]) == pytest.approx(
        (5.0, 5.0, 10.0, 10.0), abs=1e-9)


def test_detect_overlaps_three_way_overlap_returns_all_pairs():
    hits = gf.detect_overlaps([rect(0, 0, 10, 10), rect(5, 0, 10, 10), rect(0, 5, 10, 10)])
    assert [(h["frame_a_index"], h["frame_b_index"]) for h in hits] == [(0, 1), (0, 2), (1, 2)]


def test_detect_overlaps_canonical_frame_packer_output_has_zero_collisions():
    frames = gf.pack_frames(rectangle(100.0, 200.0), default_preset(), meters_per_unit=1.0)
    assert len(frames) == 108
    assert gf.detect_overlaps(frames) == []


def test_detect_overlaps_pair_order_matches_all_pairs_scan():
    """The X sweep must reproduce the plugin's i<j nested loop order exactly."""
    frames = [rect(30, 0, 10, 10), rect(0, 0, 35, 10), rect(5, 5, 30, 2), rect(100, 0, 1, 1), rect(32, 1, 1, 1)]
    expected = []
    for i in range(len(frames)):
        for j in range(i + 1, len(frames)):
            a, b = gf._bbox(frames[i]["vertices"]), gf._bbox(frames[j]["vertices"])
            if min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1]):
                expected.append((i, j))
    assert [(h["frame_a_index"], h["frame_b_index"]) for h in gf.detect_overlaps(frames)] == expected
    assert len(expected) == 5


# ======================================================== PilePlacerTests.cs --

def test_place_default_config_yields_8_piles_per_frame():
    assert len(gf.place_piles(rect(0, 0, 5.272, 28.828), default_piling())) == 8


def test_place_piles_lie_entirely_inside_frame_rectangle():
    for p in gf.place_piles(rect(10, 20, 5.272, 28.828), default_piling()):
        assert 10 < p["x"] < 10 + 5.272
        assert 20 < p["y"] < 20 + 28.828


def test_place_v1_flat_drape_all_piles_have_zero_z():
    assert all(p["z"] == 0.0 for p in gf.place_piles(rect(0, 0, 5.272, 28.828), default_piling()))


def test_place_even_spacing_horizontal_piles_use_half_cell_offset():
    cfg = gf.PilingConfig(horizontal_poles_per_frame=4, vertical_poles_per_group=2, pile_diameter_m=0.1,
                          pile_reveal_m=0.0, pile_embedment_m=1.0)
    xs = sorted({p["x"] for p in gf.place_piles(rect(0, 0, 8.0, 4.0), cfg)})
    assert xs == pytest.approx([1.0, 3.0, 5.0, 7.0], abs=1e-9)


def test_place_even_spacing_vertical_piles_use_half_cell_offset():
    ys = sorted({p["y"] for p in gf.place_piles(rect(0, 0, 8.0, 4.0), default_piling())})
    assert ys == pytest.approx([1.0, 3.0], abs=1e-9)


def test_place_custom_counts_scale():
    cfg = gf.PilingConfig(horizontal_poles_per_frame=3, vertical_poles_per_group=3, pile_diameter_m=0.1,
                          pile_reveal_m=0.0, pile_embedment_m=1.0)
    assert len(gf.place_piles(rect(0, 0, 6, 6), cfg)) == 9


def test_place_template_distances_uses_distance_segments():
    template = gf.PileTemplate(name="Full", horizontal_pole_count=2, vertical_pole_count=1,
                               horizontal_distances_m=[10, 80, 10], vertical_distances_m=[5, 5])
    piles = gf.place_piles(rect(0, 0, 100, 10), template)
    assert len(piles) == 2
    assert piles[0]["x"] == pytest.approx(10.0, abs=1e-9)
    assert piles[1]["x"] == pytest.approx(90.0, abs=1e-9)
    assert piles[0]["y"] == pytest.approx(5.0, abs=1e-9)


def test_place_axis_stations_local_x_uses_station_offsets_on_torque_tube_axis():
    template = gf.PileTemplate(name="Stationed", placement_mode="AxisStations", station_axis="LocalX", stations=[
        gf.PileStation(offset_m=1.0, kind="End", label="west"),
        gf.PileStation(offset_m=5.0, kind="Drive", label="drive"),
        gf.PileStation(offset_m=9.0, kind="Bearing", label="east")])
    piles = gf.place_piles(rect(10, 20, 10, 4), template)
    assert [p["x"] for p in piles] == pytest.approx([11.0, 15.0, 19.0], abs=1e-9)
    assert all(abs(p["y"] - 22.0) < 1e-9 for p in piles)
    assert piles[0]["placement_mode"] == "AxisStations"
    assert piles[1]["station_kind"] == "Drive"
    assert piles[1]["station_label"] == "drive"


def test_place_axis_stations_local_y_with_reverse_start_walks_from_high_y():
    template = gf.PileTemplate(name="Stationed", placement_mode="AxisStations", station_axis="LocalY",
                               reverse_station_start=True, stations=[
                                   gf.PileStation(offset_m=2.0, kind="Bearing"),
                                   gf.PileStation(offset_m=6.0, kind="Joint")])
    piles = gf.place_piles(rect(10, 20, 4, 10), template)
    assert len(piles) == 2
    assert piles[0]["x"] == pytest.approx(12.0, abs=1e-9)
    assert piles[0]["y"] == pytest.approx(28.0, abs=1e-9)
    assert piles[1]["y"] == pytest.approx(24.0, abs=1e-9)
    assert piles[1]["is_joint_pile"] is True
    assert piles[1]["station_kind"] == "Joint"


def test_place_axis_stations_converts_meters_to_drawing_units():
    template = gf.PileTemplate(name="Foot Drawing", placement_mode="AxisStations", station_axis="LocalX",
                               stations=[gf.PileStation(offset_m=3.048, kind="Bearing")])
    piles = gf.place_piles(rect(0, 0, 20, 4), template, meters_per_unit=0.3048)
    assert len(piles) == 1
    assert piles[0]["x"] == pytest.approx(10.0, abs=1e-9)
    assert piles[0]["y"] == pytest.approx(2.0, abs=1e-9)


def _joint_template():
    cfg = gf.PilingConfig(horizontal_poles_per_frame=1, vertical_poles_per_group=2, pile_diameter_m=0.1)
    cfg.set_pile_depth_m(1.0)
    template = gf.PileTemplate.from_legacy_config(cfg)
    template.should_place_piles_at_joints = True
    return template


def test_place_with_tracker_pack_markers_adds_joint_pile_at_axis_offset():
    pack = gf.TrackerPack.parse_pvcase_tracker_packs("Modules,2; JointGap,1; Modules,7")
    pack.joint_gap_width_m = 0.10
    piles = gf.place_piles(rect(0, 0, 2, 10), _joint_template(), None, pack.joint_markers_along_axis_m(1.0))
    assert len(piles) == 3
    joint = [p for p in piles if p["is_joint_pile"]]
    assert len(joint) == 1
    assert joint[0]["x"] == pytest.approx(1.0, abs=1e-9)
    assert joint[0]["y"] == pytest.approx(2.05, abs=1e-9)
    assert joint[0]["joint_kind"] == "JointGap"


def test_place_with_tracker_pack_markers_does_not_duplicate_grid_pile_near_joint():
    pack = gf.TrackerPack.parse_pvcase_tracker_packs("Modules,2; JointGap,1; Modules,7")
    pack.joint_gap_width_m = 1.0
    piles = gf.place_piles(rect(0, 0, 2, 10), _joint_template(), None, pack.joint_markers_along_axis_m(1.0))
    assert len(piles) == 2
    assert not any(p["is_joint_pile"] for p in piles)


def test_place_pile_indices_are_unique_and_cover_all_cells():
    seen = {(p["pile_h"], p["pile_v"]) for p in gf.place_piles(rect(0, 0, 8, 4), default_piling())}
    assert seen == {(h, v) for h in range(4) for v in range(2)}


def test_place_frame_indices_pass_through():
    for p in gf.place_piles(rect(0, 0, 8, 4, row=7, col=13), default_piling()):
        assert (p["frame_row"], p["frame_col"]) == (7, 13)


def test_place_with_terrain_sampler_sets_z_from_sample():
    for p in gf.place_piles(rect(0, 0, 8, 4), default_piling(), lambda x, y: 0.1 * x + 0.2 * y):
        assert p["z"] == pytest.approx(0.1 * p["x"] + 0.2 * p["y"], abs=1e-9)


def test_place_with_null_returning_sampler_falls_back_to_zero_z():
    assert all(p["z"] == 0.0 for p in gf.place_piles(rect(0, 0, 8, 4), default_piling(), lambda x, y: None))


def test_place_with_null_sampler_preserves_flat_drape():
    assert all(p["z"] == 0.0 for p in gf.place_piles(rect(0, 0, 8, 4), default_piling(), None))


def test_place_all_108_canonical_frames_yields_864_piles():
    frames = gf.pack_frames(rectangle(100.0, 200.0), default_preset(), meters_per_unit=1.0)
    assert len(frames) == 108
    assert len(gf.place_all_piles(frames, default_piling())) == 864


# ======================================================= PileTemplateTests.cs --

FULL_JSON = ('{"Templates":{"Full":{"AreEqualMargins":false,"ShouldPlacePilesAtJoints":false,'
             '"IsMirrorFromMiddle":false,"DistributionType":2,"Name":"Full",'
             '"HorizontalDistances":[46.105,368.796,46.099],"VerticalDistances":[3.775,3.775],'
             '"MiddleDistribution":0.0,"SelectedMiddlePole":false,"HorizontalPoleCount":2,"VerticalPoleCount":1}}}')

SUMMIT_LAKE_HEX = (
    "7B2254656D706C61746573223A7B2246756C6C223A7B22417265457175616C4D617267696E73223A66616C73652C2253686F756C64"
    "506C61636550696C657341744A6F696E7473223A66616C73652C2249734D6972726F7246726F6D4D6964646C65223A66616C73652C"
    "22446973747269627574696F6E54797065223A322C224E616D65223A2246756C6C222C22486F72697A6F6E74616C44697374616E63"
    "6573223A5B34362E3130352C3336382E3739362C34362E3039395D2C22566572746963616C44697374616E636573223A5B332E3737"
    "352C332E3737355D2C224D6964646C65446973747269627574696F6E223A302E302C2253656C65637465644D6964646C65506F6C65"
    "223A66616C73652C22486F72697A6F6E74616C506F6C65436F756E74223A322C22566572746963616C506F6C65436F756E74223A31"
    "7D7D7D")


def test_parse_pvcase_piling_json_reads_full_template():
    t = gf.parse_pvcase_piling_json(FULL_JSON)
    assert t.name == "Full"
    assert (t.horizontal_pole_count, t.vertical_pole_count) == (2, 1)
    assert len(t.horizontal_distances_m) == 3
    assert len(t.vertical_distances_m) == 2
    assert t.piles_per_frame == 2
    assert t.placement_mode == "Grid"


def test_deserialize_template_without_placement_mode_defaults_to_grid():
    t = gf.deserialize_pile_template('{"Name":"Legacy","HorizontalPoleCount":3,"VerticalPoleCount":1}')
    assert t.placement_mode == "Grid"
    assert t.piles_per_frame == 3


def test_axis_station_template_round_trips_station_data():
    text = json.dumps({"Templates": {
        "Full": {"Name": "Full", "HorizontalPoleCount": 2, "VerticalPoleCount": 1,
                 "HorizontalDistancesM": [1.0, 1.0, 1.0], "VerticalDistancesM": [1.0, 1.0]},
        "Mapped": {"Name": "Mapped", "HorizontalPoleCount": 2, "VerticalPoleCount": 1,
                   "PlacementMode": "AxisStations", "StationAxis": "LocalY", "ReverseStationStart": True,
                   "Stations": [{"OffsetM": 1.2, "Kind": "Drive", "Label": "motor", "CrossAxisOffsetM": 0.3}]}},
        "ActiveTemplate": "Mapped"}, indent=2)
    loaded = gf.load_pile_template_store(text).get_active()
    assert loaded.placement_mode == "AxisStations"
    assert loaded.station_axis == "LocalY"
    assert loaded.reverse_station_start is True
    assert len(loaded.stations) == 1
    assert loaded.stations[0].kind == "Drive"
    assert loaded.stations[0].label == "motor"
    assert loaded.stations[0].cross_axis_offset_m == pytest.approx(0.3, abs=1e-9)


def test_parse_pvcase_piling_json_summit_lake_blob_places_two_piles():
    t = gf.parse_pvcase_piling_json(bytes.fromhex(SUMMIT_LAKE_HEX).decode("utf-8"))
    frame = {"row": 0, "col": 0, "vertices": [(-216.0, -3.775), (-216.0, 3.775), (216.0, 3.775), (216.0, -3.775)]}
    piles = gf.place_piles(frame, t)
    assert t.name == "Full"
    assert t.piles_per_frame == 2
    assert len(piles) == 2
    assert not any(p["is_joint_pile"] for p in piles)


def test_store_round_trips_schema_with_active_template():
    text = json.dumps({"Templates": {
        "Edge": {"Name": "Edge", "HorizontalPoleCount": 3, "VerticalPoleCount": 1,
                 "HorizontalDistancesM": [1, 2, 2, 1], "VerticalDistancesM": [1, 1]},
        "Full": {"Name": "Full"}}, "ActiveTemplate": "Edge"})
    store = gf.load_pile_template_store(text)
    assert store.get_active().name == "Edge"
    assert store.get("Edge").horizontal_pole_count == 3


# =================================================== FramePresetStoreTests.cs --

def test_fresh_store_no_file_on_disk_exposes_default_preset():
    store = gf.load_frame_preset_store(None)
    assert [p.name for p in store.list()] == ["Default"]
    assert store.get_active().name == "Default"


def test_default_preset_matches_pvcase_reference_values():
    d = gf.load_frame_preset_store(None).get_active()
    assert (d.module_length_m, d.module_width_m, d.module_thickness_m, d.module_power_wp) == (2.384, 1.303, 0.033, 715)
    assert (d.framing_type, d.orientation) == ("FixedTilt", "Portrait")
    assert d.rows > 0 and d.columns > 0
    assert (d.axis_azimuth_deg, d.rom_min_deg, d.rom_max_deg) == (180.0, -60.0, 60.0)
    assert (d.height_at_low_pose_m, d.height_at_high_pose_m) == (0.8, 1.62)
    assert (d.max_ns_slope_pct, d.max_row_to_row_ew_slope_pct) == (8.5, 10.0)
    assert (d.rows, d.columns, d.tilt_degrees, d.color_index, d.pile_template_name) == (4, 12, 20.0, 7, "Full")


def _saved(*presets, active="Default"):
    """The file shape FramePresetStore.Persist writes (FramePresetStore.cs:198-211)."""
    return json.dumps({"SchemaVersion": 2, "ActiveName": active, "Presets": list(presets)}, indent=2)


TRACKER_2P_28 = {
    "Name": "Tracker_2P_28", "ModuleLengthM": 2.384, "ModuleWidthM": 1.303, "ModuleThicknessM": 0.033,
    "ModulePowerWp": 715, "InverterTypeName": "", "ColorIndex": 7, "FramingType": "SingleAxisTracker",
    "Orientation": "Portrait", "Rows": 2, "Columns": 28, "TiltDegrees": 0.0, "HorizontalGapM": 0.02,
    "VerticalGapM": 0.02, "AxisAzimuthDeg": -90.0, "RomMinDeg": -20.0, "RomMaxDeg": 20.0,
    "HeightAtLowPoseM": 0.8001, "HeightAtHighPoseM": 1.6201, "MaxNsSlopePct": 8.5, "MaxRowToRowEwSlopePct": 10.0,
    "MaxAxialSlopePct": 8.5, "MaxCrossAxisSlopePct": 10.0, "MaxRowToRowSlopeDeg": 4.0, "MaxSlopePercent": 15.0,
    "TrackerPack": {"Segments": [{"Kind": "Modules", "Count": 56}], "JointGapWidthM": 0.05, "MotorGapWidthM": 0.3,
                    "IsMotorGapEnabled": True, "PlacePilesAtJoints": True},
    "PileTemplateName": "Full",
    "Piling": {"HorizontalPolesPerFrame": 4, "VerticalPolesPerGroup": 2, "PileDiameterM": 0.15, "PileRevealM": 0.5,
               "PileEmbedmentM": 1.5, "PileDepthM": 2.0, "MinPileLengthM": 1.0, "MaxPileLengthM": 6.0}}


def test_save_preset_then_load_from_disk_round_trips_all_fields():
    loaded = gf.load_frame_preset_store(_saved(TRACKER_2P_28)).get("Tracker_2P_28")
    assert loaded is not None
    assert loaded.name == "Tracker_2P_28"
    assert loaded.module_length_m == pytest.approx(2.384, abs=1e-6)
    assert loaded.module_power_wp == 715
    assert (loaded.framing_type, loaded.orientation, loaded.rows, loaded.columns) == (
        "SingleAxisTracker", "Portrait", 2, 28)
    assert loaded.horizontal_gap_m == pytest.approx(0.02, abs=1e-6)
    assert (loaded.axis_azimuth_deg, loaded.rom_min_deg, loaded.rom_max_deg) == (-90.0, -20.0, 20.0)
    assert (loaded.height_at_low_pose_m, loaded.height_at_high_pose_m) == (0.8001, 1.6201)
    assert (loaded.max_ns_slope_pct, loaded.max_row_to_row_ew_slope_pct) == (8.5, 10.0)
    assert loaded.piling.pile_embedment_m == pytest.approx(1.5, abs=1e-12)


def test_load_legacy_preset_without_tracker_geometry_applies_tracker_defaults():
    text = """{
  "SchemaVersion": 1,
  "ActiveName": "LegacyTracker",
  "Presets": [
    {"Name": "LegacyTracker", "ModuleLengthM": 2.384, "ModuleWidthM": 1.303, "ModuleThicknessM": 0.033,
     "ModulePowerWp": 715, "FramingType": "SingleAxisTracker", "Orientation": "Portrait", "Rows": 2,
     "Columns": 28, "TiltDegrees": 0.0, "HorizontalGapM": 0.02, "VerticalGapM": 0.02}
  ]
}"""
    loaded = gf.load_frame_preset_store(text).get_active()
    assert loaded.name == "LegacyTracker"
    assert (loaded.axis_azimuth_deg, loaded.rom_min_deg, loaded.rom_max_deg) == (180.0, -60.0, 60.0)
    assert (loaded.height_at_low_pose_m, loaded.height_at_high_pose_m) == (0.8, 1.62)
    assert (loaded.max_ns_slope_pct, loaded.max_row_to_row_ew_slope_pct) == (8.5, 10.0)


def test_frame_power_is_derived_from_rows_columns_and_module_power():
    assert gf.FramePreset(module_power_wp=715, rows=2, columns=28).frame_power_kwp == pytest.approx(40.04, abs=1e-6)


def test_set_active_persists_across_instances():
    text = _saved({"Name": "Tracker_2P_28", "Rows": 2, "Columns": 28, "ModulePowerWp": 715},
                  {"Name": "FixedTilt_4x12", "Rows": 4, "Columns": 12, "ModulePowerWp": 550},
                  active="FixedTilt_4x12")
    assert gf.load_frame_preset_store(text).get_active().name == "FixedTilt_4x12"


def test_new_preset_default_color_index_is_aci7():
    assert gf.FramePreset().color_index == 7


def test_save_preset_with_custom_color_index_round_trips_through_disk():
    text = _saved({"Name": "Tracker_Magenta", "Rows": 2, "Columns": 28, "ModulePowerWp": 715, "ColorIndex": 6})
    assert gf.load_frame_preset_store(text).get("Tracker_Magenta").color_index == 6


def test_old_preset_json_without_color_index_defaults_to_aci7_on_load():
    text = '{"ActiveName": "Legacy", "Presets": [{"Name": "Legacy", "Rows": 2, "Columns": 10, "ModulePowerWp": 500}]}'
    assert gf.load_frame_preset_store(text).get("Legacy").color_index == 7


# ============================================== preset and template loading --

def test_corrupt_preset_file_falls_back_to_default_only():
    store = gf.load_frame_preset_store("{ not valid json")
    assert store.corrupt is True
    assert [p.name for p in store.list()] == ["Default"]


def test_type_error_anywhere_discards_the_whole_file():
    bad = dict(TRACKER_2P_28, Orientation="Sideways")
    store = gf.load_frame_preset_store(_saved(bad, active="Tracker_2P_28"))
    assert store.corrupt is True
    assert store.get_active().name == "Default"
    assert gf.load_frame_preset_store(_saved(dict(TRACKER_2P_28, Rows=2.0))).corrupt is True


def test_unknown_active_name_falls_back_to_default_and_names_match_case_insensitively():
    store = gf.load_frame_preset_store(_saved({"name": "Field-A", "ROWS": 3, "columns": 9}, active="Nope"))
    assert store.get_active().name == "Default"
    assert store.get("FIELD-a").rows == 3


def test_newtonsoft_list_reuse_duplicates_a_persisted_tracker_pack():
    """Declared plugin behaviour: TrackerPack's lazy getter builds [Modules 48] from
    Rows x Columns, Newtonsoft appends the file's segments to it, and the pack becomes
    two segments, so FramePacker switches to the pack length 96 x ModuleWidthM."""
    preset = {"Name": "Default", "ModuleLengthM": 2.384, "ModuleWidthM": 1.303, "ModuleThicknessM": 0.033,
              "ModulePowerWp": 715, "Rows": 4, "Columns": 12, "TiltDegrees": 20.0, "HorizontalGapM": 0.02,
              "VerticalGapM": 0.02, "TrackerPack": {"Segments": [{"Kind": "Modules", "Count": 48}]}}
    replayed = gf.load_frame_preset_store(_saved(preset)).get_active()
    assert [(s.kind, s.count) for s in replayed.tracker_pack.segments] == [("Modules", 48), ("Modules", 48)]
    assert gf.frame_footprint(replayed)[1] == pytest.approx(96 * 1.303, abs=1e-9)
    assert len(gf.pack_frames(rectangle(100.0, 200.0), replayed, 1.0)) == 18

    intended = gf.load_frame_preset_store(_saved(preset), replicate_list_reuse=False).get_active()
    assert [(s.kind, s.count) for s in intended.tracker_pack.segments] == [("Modules", 48)]
    assert len(gf.pack_frames(rectangle(100.0, 200.0), intended, 1.0)) == 108


def test_tracker_pack_before_rows_seeds_a_zero_count_segment_that_changes_nothing():
    preset = {"Name": "Early", "TrackerPack": {"Segments": [{"Kind": "Modules", "Count": 48}]},
              "ModuleLengthM": 2.384, "ModuleWidthM": 1.303, "Rows": 4, "Columns": 12,
              "HorizontalGapM": 0.02, "VerticalGapM": 0.02}
    p = gf.load_frame_preset_store(_saved(preset, active="Early")).get_active()
    assert [(s.kind, s.count) for s in p.tracker_pack.segments] == [("Modules", 0), ("Modules", 48)]
    assert gf.frame_footprint(p)[1] == pytest.approx(28.828, abs=1e-9)


def test_piling_depth_setter_is_order_dependent():
    a = gf.load_frame_preset_store(_saved({"Name": "A", "Piling": {"PileRevealM": 1.0, "PileDepthM": 3.0},
                                           "PileTemplateName": "Full"}, active="A")).get_active()
    b = gf.load_frame_preset_store(_saved({"Name": "A", "Piling": {"PileDepthM": 3.0, "PileRevealM": 1.0},
                                           "PileTemplateName": "Full"}, active="A")).get_active()
    assert a.piling.pile_embedment_m == pytest.approx(2.0)
    assert b.piling.pile_embedment_m == pytest.approx(2.5)


def test_legacy_piling_preset_resolves_to_a_default_template_from_its_config():
    text = _saved({"Name": "Old", "Rows": 4, "Columns": 12, "ModuleLengthM": 2.384, "ModuleWidthM": 1.303,
                   "Piling": {"HorizontalPolesPerFrame": 3, "VerticalPolesPerGroup": 2}}, active="Old")
    store = gf.load_frame_preset_store(text)
    preset = store.get_active()
    assert preset.pile_template_name == "Default"
    assert store.legacy_piling_preset_names == ["Old"]
    template = gf.load_pile_template_store(None, legacy_default_piling=store.legacy_default_piling).resolve(preset)
    assert (template.name, template.horizontal_pole_count, template.vertical_pole_count) == ("Default", 3, 2)
    assert template.horizontal_distances_m == []


def test_pile_store_default_full_and_resolve_fallbacks():
    store = gf.load_pile_template_store(None)
    full = store.resolve(gf.FramePreset(pile_template_name="Missing"))
    assert (full.name, full.horizontal_pole_count, full.vertical_pole_count) == ("Full", 2, 1)
    assert full.horizontal_distances_m == [1.0, 1.0, 1.0]
    assert gf.load_pile_template_store("{broken").corrupt is True


MACHINE_PILE_STORE = json.dumps({"Templates": {"Full": {
    "Name": "Full", "DistributionType": 1,
    "HorizontalDistancesM": [4.915, 9.83, 9.83, 9.83, 9.83, 4.917],
    "VerticalDistancesM": [4.536, 4.536, 4.5353051145682635], "HorizontalPoleCount": 5, "VerticalPoleCount": 2,
    "PlacementMode": "Grid", "StationAxis": "LocalX", "Stations": [],
    "RevealBucketBoundariesM": [0.9144000000000001, 1.2192, 1.524, 1.8288000000000002, 2.1336],
    "PilesPerFrame": 10}}, "ActiveTemplate": "Full"})


def test_pile_store_replays_reveal_bucket_growth_and_reads_distances():
    replayed = gf.load_pile_template_store(MACHINE_PILE_STORE).get_active()
    assert len(replayed.reveal_bucket_boundaries_m) == 10
    assert len(gf.load_pile_template_store(MACHINE_PILE_STORE, replicate_list_reuse=False)
               .get_active().reveal_bucket_boundaries_m) == 5
    piles = gf.place_piles(rect(0, 0, 5.272, 28.828), replayed)
    assert len(piles) == 10
    assert piles[0]["x"] == pytest.approx(5.272 * 4.915 / 49.152, abs=1e-12)
    assert piles[0]["y"] == pytest.approx(28.828 * 4.536 / (4.536 + 4.536 + 4.5353051145682635), abs=1e-12)


def test_tracker_pack_text_parsing():
    pack = gf.TrackerPack.parse_pvcase_tracker_packs('"Modules,12; joint_gap, 2 ;Motor,0; Modules,1.5; Bogus,3;"')
    assert [(s.kind, s.count) for s in pack.segments] == [("Modules", 12), ("JointGap", 2), ("Motor", 1),
                                                          ("Modules", 2)]
    markers = pack.joint_markers_along_axis_m(1.0)
    assert [m.kind for m in markers] == ["JointGap", "JointGap", "Motor"]
    assert [m.offset_m for m in markers] == pytest.approx([12.025, 12.075, 12.25])
    assert [m.gap_width_m for m in markers] == pytest.approx([0.05, 0.05, 0.3])
    assert pack.tracker_length_m(1.0) == pytest.approx(12 + 0.1 + 0.3 + 2)
    assert pack.has_intra_tracker_segments() is True
    assert gf.TrackerPack.default_uniform(48).has_intra_tracker_segments() is False


# ================================================= LEAFGENERATE: picking --

def test_pick_boundaries_auto_selects_the_single_fixture_rectangle():
    picked = gf.pick_boundaries(fixture_entities(), multi=False)
    assert picked["mode"] == "auto"
    assert picked["boundaries"] == [rectangle(100.0, 200.0)]


def test_pick_boundaries_rules():
    ents = fixture_entities() + [
        {"type": "LWPOLYLINE", "layer": "site", "closed": True, "vertices": rectangle(10, 10)},
        {"type": "LWPOLYLINE", "layer": "leaf-trackers", "closed": True, "vertices": rectangle(5, 5)},
        {"type": "LWPOLYLINE", "layer": "0", "closed": False, "vertices": rectangle(5, 5)},
        {"type": "LWPOLYLINE", "layer": "0", "closed": True, "vertices": [(0, 0), (1, 1)]},
        {"type": "CIRCLE", "layer": "0", "center": (0, 0), "radius": 1}]
    assert gf.find_boundary_candidates(ents) == [0, 1]
    single = gf.pick_boundaries(ents, multi=False)
    assert (single["mode"], single["boundaries"]) == ("prompt", None)
    assert gf.pick_boundaries(ents, multi=False, selection=[1])["boundaries"] == [rectangle(10, 10)]
    assert gf.pick_boundaries(ents, multi=False, selection=[3])["boundaries"] == []
    assert len(gf.pick_boundaries(ents, multi=True)["boundaries"]) == 2
    refusal("selection_invalid", gf.pick_boundaries, ents, False, [0, 1])
    refusal("selection_invalid", gf.pick_boundaries, ents, False, [99])


# ================================================= LEAFGENERATE: drawing --

def test_generate_canonical_fixture_draws_108_frame_polylines_with_their_cells():
    result = canonical_frames()
    assert result["status"] == "ok"
    assert result["frame_count"] == 108
    ents = result["entities"]
    assert len(ents) == 108
    first, last = ents[0], ents[-1]
    assert first["type"] == "LWPOLYLINE" and first["layer"] == "LEAF-TRACKERS" and first["closed"] is True
    assert first["color"] == {"method": "ByAci", "index": 7}
    assert first["elevation"] == 0.0
    assert flat(first["vertices"]) == pytest.approx(flat([(0.0, 0.0), (5.272, 0.0), (5.272, 28.828), (0.0, 28.828)]))
    assert (first["row"], first["col"], first["site_revision"]) == (0, 0, None)
    assert (last["row"], last["col"]) == (5, 17)
    assert last["vertices"][2] == pytest.approx((18 * 5.272, 6 * 28.828))
    assert [(e["row"], e["col"]) for e in ents[:19]] == [(0, c) for c in range(18)] + [(1, 0)]
    assert sorted(first) == ["closed", "col", "color", "elevation", "layer", "row", "site_revision",
                             "type", "vertices"]


def test_generate_site_revision_and_colour_rules():
    ents = canonical_frames(site_revision="rev-7")["entities"]
    assert ents[0]["site_revision"] == "rev-7"
    assert canonical_frames(site_revision="  ")["entities"][0]["site_revision"] is None
    assert gf.frame_color(0) == {"method": "ByLayer", "index": 256}
    assert gf.frame_color(256) == {"method": "ByLayer", "index": 256}
    assert gf.frame_color(255) == {"method": "ByAci", "index": 255}
    big = gf.frame_entities([{"row": 40000, "col": 3, "vertices": rectangle(1, 1)}], 7, 1.0)[0]
    assert (big["row"], big["col"]) == (32767, 3)


def test_generate_with_terrain_sets_elevation_and_keeps_gentle_frames():
    result = canonical_frames(terrain_z_m=lambda x, y: 0.01 * x + 0.02 * y)
    assert result["frame_count"] == 108
    cx, cy = 5.272 / 2, 28.828 / 2
    assert result["entities"][0]["elevation"] == pytest.approx(0.01 * cx + 0.02 * cy)


def test_generate_slope_filter_uses_forward_differences_and_skips_off_grid():
    assert canonical_frames(terrain_z_m=lambda x, y: 0.2 * y)["frame_count"] == 0
    off_grid = canonical_frames(terrain_z_m=lambda x, y: None)
    assert off_grid["frame_count"] == 108
    assert off_grid["entities"][0]["elevation"] == 0.0
    slope = gf.slope_filter(lambda x, y: 0.03 * x + 0.04 * y, default_preset(), 1.0)
    assert slope(10.0, 10.0) == pytest.approx(5.0)
    assert gf.slope_filter(lambda x, y: 0.0, gf.FramePreset(max_slope_percent=0.0), 1.0) is None


def test_generate_setbacks_keep_only_frames_inside_every_setback():
    result = canonical_frames(array_setbacks=[[(0, 0), (50, 0), (50, 200), (0, 200)]])
    assert result["frame_count"] == 54
    assert result["skipped_by_setback"] == 54
    assert all(e["vertices"][1][0] <= 50 for e in result["entities"])


def test_generate_shading_restriction_and_road_buffers_exclude_frames():
    restriction = gf.restriction_polygons([
        {"type": "CIRCLE", "layer": "leaf-pvcase-shading-restriction", "center": (50.0, 100.0), "radius": 10.0},
        {"type": "LWPOLYLINE", "layer": "LEAF-PVCASE-SHADING-RESTRICTION", "closed": True,
         "vertices": [(0, 0), (6, 0), (6, 6)]},
        {"type": "CIRCLE", "layer": "0", "center": (0.0, 0.0), "radius": 1.0}])
    assert [len(p) for p in restriction] == [32, 3]
    result = canonical_frames(exclusion_polys=restriction)
    assert result["frame_count"] < 108
    assert (result["entities"][0]["row"], result["entities"][0]["col"]) != (0, 0)


def test_generate_multi_restarts_indexes_per_boundary():
    store = gf.load_frame_preset_store(None)
    shifted = [(x + 500, y) for x, y in rectangle(100.0, 200.0)]
    result = gf.generate_frames([rectangle(100.0, 200.0), shifted], store.get_active(), 1.0)
    assert result["frame_count"] == 216
    assert (result["entities"][108]["row"], result["entities"][108]["col"]) == (0, 0)
    assert result["frames"][108]["boundary_index"] == 1
    assert gf.generate_frames([], store.get_active(), 1.0)["status"] == "no_boundary"


def test_generate_areas_uses_area_preset_and_pitch_and_counts_skips():
    store = gf.load_frame_preset_store(_saved({"Name": "Wide", "ModuleLengthM": 2.384, "ModuleWidthM": 1.303,
                                               "Rows": 4, "Columns": 12, "HorizontalGapM": 0.02,
                                               "VerticalGapM": 0.02, "ColorIndex": 30}))
    areas = [None, {"name": "A", "boundary": rectangle(100.0, 200.0), "frame_preset_name": "Wide",
                    "pitch_override_m": 40.0},
             {"name": "B", "boundary": [(0, 0), (1, 1)]},
             {"name": "C", "boundary": rectangle(100.0, 200.0), "frame_preset_name": "Unknown"}]
    result = gf.generate_frames_for_areas(areas, store, 1.0)
    assert result["areas_generated"] == 2
    assert result["areas_skipped"] == [0, 2]
    assert result["frame_count"] == 90 + 108
    assert result["entities"][0]["color"] == {"method": "ByAci", "index": 30}
    assert result["entities"][90]["color"] == {"method": "ByAci", "index": 7}


# ============================================================ LEAFCOLLISION --

def test_collision_on_generated_frames_reports_none_and_draws_nothing():
    result = gf.run_collision(canonical_frames()["entities"])
    assert (result["status"], result["frame_count"], result["markers"]) == ("no_overlaps", 108, [])


def test_collision_marks_a_nudged_frame_on_leaf_collision():
    ents = canonical_frames()["entities"] + [tracker_entity(rectangle(2, 2), 99, 99)]
    ents[-1]["vertices"] = [(1, 1), (3, 1), (3, 3), (1, 3)]
    result = gf.run_collision(ents)
    assert result["status"] == "overlaps"
    assert len(result["collisions"]) == 1
    hit = result["collisions"][0]
    assert (hit["frame_a_index"], hit["frame_b_index"], hit["frame_b_row"], hit["frame_b_col"]) == (0, 108, 99, 99)
    marker = result["markers"][0]
    assert marker["layer"] == "LEAF-COLLISION" and marker["closed"] and marker["color"]["method"] == "ByLayer"
    assert marker["vertices"] == [(1, 1), (3, 1), (3, 3), (1, 3)]


def test_collision_reads_only_valid_tracker_frames():
    ents = [tracker_entity(rectangle(5, 5), 0, 0),
            tracker_entity(rectangle(5, 5), 0, 1, layer="0"),
            dict(tracker_entity(rectangle(5, 5), 0, 2), closed=False),
            dict(tracker_entity(rectangle(5, 5), 0, 3), row=None, col=None),   # not a generated frame
            dict(tracker_entity(rectangle(5, 5), 0, 4), vertices=[(0, 0), (5, 0), (5, 5)]),
            {"type": "LWPOLYLINE", "layer": "LEAF-TRACKERS", "closed": True, "vertices": rectangle(5, 5)}]
    frames = gf.read_tracker_frames(ents)
    assert [(f["row"], f["col"]) for f in frames] == [(0, 0)]
    assert gf.run_collision([])["status"] == "no_frames"


# =============================================================== LEAFPILING --

def test_piling_canonical_fixture_with_default_templates():
    ents = with_handles(canonical_frames()["entities"])
    store = gf.load_frame_preset_store(None)
    result = gf.run_piling(ents, store.get_active(), gf.load_pile_template_store(None), 1.0)
    assert result["status"] == "ok"
    assert result["template_name"] == "Full"
    assert len(result["piles"]) == 216 and len(result["entities"]) == 216
    assert result["expected_total"] == 216 and result["short_trackers"] == []
    assert (result["grid_piles"], result["joint_piles"], result["station_piles"]) == (216, 0, 0)
    first = result["entities"][0]
    assert first["type"] == "CIRCLE" and first["layer"] == "LEAF-PILING"
    assert first["color"] == {"method": "ByLayer", "index": 256}
    assert first["center"] == pytest.approx((5.272 / 3, 14.414, -1.5))
    assert first["radius"] == pytest.approx(0.075)
    assert first["thickness"] == pytest.approx(2.0)
    assert first["normal"] == (0.0, 0.0, 1.0)
    assert first["cylinder"] == pytest.approx({"center_x": 5.272 / 3, "center_y": 14.414, "diameter_du": 0.15,
                                               "bottom_z": -1.5, "top_z": 0.5})
    assert first["pile"] == {"placement": "Grid", "placement_detail": "", "source_tracker": "200",
                             "template": "Full", "mapping": None, "station_label": None,
                             "diameter_m": 0.15, "reveal_m": 0.5, "embedment_m": 1.5, "depth_m": 2.0}
    assert sorted(first) == ["center", "color", "cylinder", "layer", "normal", "pile", "radius",
                             "thickness", "type"]
    assert result["entities"][1]["center"][0] == pytest.approx(2 * 5.272 / 3)
    assert result["terrain_draped"] is False


def test_piling_with_terrain_drapes_in_drawing_units_and_erases_old_piles():
    frame = tracker_entity([(0, 0), (8, 0), (8, 4), (0, 4)], 0, 0, handle="2a")
    old = {"type": "CIRCLE", "layer": "leaf-piling", "center": (0, 0, 0), "radius": 1.0}
    result = gf.run_piling([old, frame], default_preset(), gf.load_pile_template_store(None), 0.5,
                           terrain_z_m=lambda x, y: 0.1 * x + 0.2 * y)
    assert result["erase_indexes"] == [0]
    p = result["piles"][0]
    z_du = (0.1 * p["x"] + 0.2 * p["y"]) / 0.5
    assert p["z"] == pytest.approx(z_du)
    ent = result["entities"][0]
    assert ent["radius"] == pytest.approx(0.15)
    assert ent["thickness"] == pytest.approx(4.0)
    assert ent["center"][2] == pytest.approx(z_du - 3.0)
    assert ent["pile"]["source_tracker"] == "2A"


def test_piling_places_joint_piles_from_the_preset_tracker_pack():
    preset = gf.FramePreset(name="T", module_length_m=2.0, module_width_m=1.0, rows=1, columns=4,
                            tracker_pack=gf.TrackerPack.parse_pvcase_tracker_packs("Modules,2; JointGap,1; Modules,2"))
    preset.tracker_pack.joint_gap_width_m = 0.10
    ents = gf.generate_frames([rectangle(1.0, 4.1)], preset, 1.0)["entities"]
    assert len(ents) == 1
    store = gf.load_pile_template_store('{"Templates":{"Full":{"Name":"Full","HorizontalPoleCount":1,'
                                        '"VerticalPoleCount":2}},"ActiveTemplate":"Full"}')
    result = gf.run_piling(ents, preset, store, 1.0)
    assert len(result["piles"]) == 3
    assert result["joint_piles"] == 1
    joint = result["piles"][2]
    assert (joint["x"], joint["y"]) == pytest.approx((0.5, 2.05))
    joint_record = result["entities"][2]["pile"]
    assert (joint_record["placement"], joint_record["placement_detail"]) == ("Joint", "JointGap")
    assert store.get_active().should_place_piles_at_joints is False


def test_piling_template_overrides_road_clip_and_native_mapping():
    ents = [tracker_entity([(0, 0), (8, 0), (8, 4), (0, 4)], 0, 0, handle="A1")]
    store = gf.load_pile_template_store(json.dumps({"Templates": {"Full": {
        "Name": "Full", "HorizontalPoleCount": 4, "VerticalPoleCount": 1, "PileDiameterM": 0.2,
        "PileRevealM": 1.0, "PileEmbedmentM": 2.5}}}))
    result = gf.run_piling(ents, default_preset(), store, 1.0, in_road_buffer=lambda x, y: x < 2.0)
    assert result["road_skipped"] == 1
    assert [p["x"] for p in result["piles"]] == pytest.approx([3.0, 5.0, 7.0])
    assert result["short_trackers"] == [{"handle": "A1", "expected": 4, "actual": 3, "missing": 1}]
    sizes = result["entities"][0]["pile"]
    assert [sizes[k] for k in ("diameter_m", "reveal_m", "embedment_m", "depth_m")] == [0.2, 1.0, 2.5, 3.5]
    no_handle = [tracker_entity([(0, 0), (8, 0), (8, 4), (0, 4)], 0, 0)]
    assert gf.run_piling(no_handle, default_preset(), store, 1.0, in_road_buffer=lambda x, y: True)["road_skipped"] == 0
    mapping = {"template": gf.PileTemplate(name="Mapped", horizontal_pole_count=1, vertical_pole_count=1),
               "signature": "NATIVE|LEAF-TRACKERS|ACTIVE-PRESET"}
    mapped = gf.run_piling(ents, default_preset(), store, 1.0, native_mapping=mapping)
    assert len(mapped["piles"]) == 1
    record = mapped["entities"][0]["pile"]
    assert (record["template"], record["mapping"]) == ("Mapped", "NATIVE|LEAF-TRACKERS|ACTIVE-PRESET")


def test_piling_without_frames_reports_no_sources():
    result = gf.run_piling(fixture_entities(), default_preset(), gf.load_pile_template_store(None), 1.0)
    assert (result["status"], result["entities"], result["erase_indexes"]) == ("no_sources", [], [])


def test_pile_record_names_placement_and_omits_blank_text():
    base = {"placement_mode": "AxisStations", "station_kind": "Drive", "is_joint_pile": False,
            "joint_kind": "Modules", "source_tracker_handle": None, "template_name": " ",
            "mapping_signature": "", "station_label": "motor"}
    record = gf.pile_record(base, 0.2, 1.0, 2.0, 3.0)
    assert record == {"placement": "Station", "placement_detail": "Drive", "source_tracker": None,
                      "template": None, "mapping": None, "station_label": "motor",
                      "diameter_m": 0.2, "reveal_m": 1.0, "embedment_m": 2.0, "depth_m": 3.0}


# ====================================================== LEAFCOLLISIONRANGE --

def _drawn_piles():
    ents = with_handles(canonical_frames()["entities"])
    store = gf.load_frame_preset_store(None)
    return gf.run_piling(ents, store.get_active(), gf.load_pile_template_store(None), 1.0)["entities"]


def test_range_check_default_window_passes_every_drawn_pile():
    result = gf.run_pile_range_check(_drawn_piles(), gf.load_frame_preset_store(None).get_active(), 1.0)
    assert (result["status"], result["total_piles"], result["markers"]) == ("all_within_range", 216, [])


def test_range_check_marks_short_piles_with_their_xy_extents():
    preset = default_preset()
    preset.piling = gf.PilingConfig(min_pile_length_m=2.5, max_pile_length_m=6.0)
    result = gf.run_pile_range_check(_drawn_piles(), preset, 1.0)
    assert result["status"] == "out_of_range" and len(result["hits"]) == 216
    hit = result["hits"][0]
    assert hit["length_m"] == pytest.approx(2.0) and hit["below_min"] is True
    x = 5.272 / 3
    assert flat(result["markers"][0]["vertices"]) == pytest.approx(flat(
        [(x - 0.075, 14.414 - 0.075), (x + 0.075, 14.414 - 0.075), (x + 0.075, 14.414 + 0.075),
         (x - 0.075, 14.414 + 0.075)]))
    assert result["markers"][0]["layer"] == "LEAF-COLLISION"


def test_range_check_falls_back_to_z_extent_and_rejects_bad_windows():
    preset = default_preset()
    stretched = {"type": "CIRCLE", "layer": "LEAF-PILING", "center": (0.0, 0.0, -10.0), "radius": 1.0,
                 "thickness": 10.0, "pile": {"depth_m": None}}
    long_pile = gf.run_pile_range_check([stretched], preset, 1.0)
    assert (long_pile["status"], long_pile["total_piles"]) == ("out_of_range", 1)
    assert long_pile["hits"][0]["length_m"] == pytest.approx(10.0)
    assert long_pile["hits"][0]["below_min"] is False
    in_feet = gf.run_pile_range_check([stretched], preset, 0.3048)
    assert (in_feet["status"], in_feet["total_piles"], in_feet["hits"]) == ("all_within_range", 1, [])
    assert gf.run_pile_range_check([], preset, 1.0)["status"] == "no_piles"
    no_extents = {"type": "TEXT", "layer": "LEAF-PILING"}
    assert gf.run_pile_range_check([no_extents], preset, 1.0)["status"] == "no_piles"
    preset.piling = gf.PilingConfig(min_pile_length_m=5.0, max_pile_length_m=4.0)
    assert gf.run_pile_range_check([stretched], preset, 1.0)["status"] == "invalid_range"
    preset.piling = None
    assert gf.run_pile_range_check([stretched], preset, 1.0)["status"] == "out_of_range"


def test_read_pile_depth_takes_the_committed_positive_depth_only():
    assert gf.read_pile_depth_m({"pile": {"depth_m": 4.25}}) == 4.25
    assert gf.read_pile_depth_m({"pile": {"depth_m": 3}}) == 3.0
    assert gf.read_pile_depth_m({"pile": {"depth_m": -1.0}}) is None
    assert gf.read_pile_depth_m({"pile": {"depth_m": True}}) is None
    assert gf.read_pile_depth_m({"pile": {"depth_m": "9"}}) is None
    assert gf.read_pile_depth_m({"pile": None}) is None
    assert gf.read_pile_depth_m({}) is None


# ===================================================== end to end, fixture --

def test_fixture_end_to_end_generate_collide_pile_range():
    drawing = fixture_entities()
    picked = gf.pick_boundaries(drawing, multi=False)
    store = gf.load_frame_preset_store(None)
    generated = gf.generate_frames(picked["boundaries"], store.get_active(), 1.0)
    drawing += with_handles(generated["entities"])
    assert gf.run_collision(drawing)["status"] == "no_overlaps"
    piling = gf.run_piling(drawing, store.get_active(), gf.load_pile_template_store(None), 1.0)
    drawing = [e for i, e in enumerate(drawing) if i not in set(piling["erase_indexes"])] + piling["entities"]
    assert len(drawing) == 1 + 108 + 216
    assert gf.run_pile_range_check(drawing, store.get_active(), 1.0)["status"] == "all_within_range"


# ================================================== bounds and refusals --

def test_pack_refusals():
    p = default_preset()
    refusal("boundary_too_few_vertices", gf.pack_frames, [(0, 0), (1, 1)], p)
    refusal("boundary_malformed", gf.pack_frames, [(0, 0), (1, math.nan), (1, 1)], p)
    refusal("boundary_malformed", gf.pack_frames, "not points", p)
    refusal("boundary_too_many_vertices", gf.pack_frames, [(i, i % 7) for i in range(gf.MAX_POLYGON_VERTICES + 1)], p)
    refusal("meters_per_unit_invalid", gf.pack_frames, rectangle(10, 10), p, 0.0)
    refusal("meters_per_unit_invalid", gf.pack_frames, rectangle(10, 10), p, math.inf)
    refusal("preset_missing", gf.pack_frames, rectangle(10, 10), None)
    refusal("preset_rows_columns", gf.pack_frames, rectangle(10, 10), gf.FramePreset(rows=0, columns=4))
    refusal("frame_footprint_degenerate", gf.pack_frames, rectangle(10, 10),
            gf.FramePreset(rows=1, columns=1, module_length_m=1.0, module_width_m=0.0))
    refusal("frame_footprint_degenerate", gf.pack_frames, rectangle(10, 10),
            gf.FramePreset(rows=1, columns=1, module_length_m=1.0, module_width_m=-1.0))
    refusal("candidate_cells_over_cap", gf.pack_frames, rectangle(100.0, 200.0),
            gf.FramePreset(rows=1, columns=1, module_length_m=0.001, module_width_m=0.001))
    refusal("pitch_invalid", gf.pack_frames, rectangle(10, 10), p, 1.0, None, math.nan)
    refusal("exclusion_malformed", gf.pack_frames, rectangle(10, 10), p, 1.0, [[(0, 0), (1, "x"), (1, 1)]])
    refusal("exclusions_over_cap", gf.pack_frames, rectangle(10, 10), p, 1.0,
            [rectangle(1, 1)] * (gf.MAX_EXCLUSIONS + 1))


def test_entity_and_frame_cell_refusals():
    refusal("entities_malformed", gf.run_collision, {"not": "a list"})
    refusal("entities_malformed", gf.run_collision, [42])
    bad = tracker_entity(rectangle(5, 5), "zero", 1)
    refusal("frame_cell_malformed", gf.run_collision, [bad])
    bad = tracker_entity(rectangle(5, 5), 0, None)
    refusal("frame_cell_malformed", gf.run_collision, [bad])
    bad = tracker_entity(rectangle(5, 5), True, 0)
    refusal("frame_cell_malformed", gf.run_collision, [bad])
    corner = tracker_entity([(0, 0), (1, 0), (1, math.inf), (0, 1)], 0, 0)
    refusal("tracker_frame_malformed", gf.run_collision, [corner])
    refusal("frame_without_vertices", gf.detect_overlaps, [{"row": 0, "col": 0, "vertices": []}])
    refusal("area_malformed", gf.generate_frames_for_areas, ["area"], gf.load_frame_preset_store(None), 1.0)


def test_pile_refusals():
    refusal("piling_counts", gf.place_piles, rect(0, 0, 1, 1), gf.PilingConfig(horizontal_poles_per_frame=0))
    refusal("template_counts", gf.place_piles, rect(0, 0, 1, 1), gf.PileTemplate(vertical_pole_count=0))
    refusal("template_counts_over_cap", gf.place_piles, rect(0, 0, 1, 1),
            gf.PileTemplate(horizontal_pole_count=gf.MAX_POLES_PER_AXIS + 1))
    refusal("frame_malformed", gf.place_piles, {"row": 0, "col": 0, "vertices": [(0, 0)]}, gf.PileTemplate())
    ents = [tracker_entity([(0, 0), (8, 0), (8, 4), (0, 4)], 0, 0)]
    store = gf.load_pile_template_store(None)
    refusal("terrain_sample_invalid", gf.run_piling, ents, default_preset(), store, 1.0,
            terrain_z_m=lambda x, y: math.inf)
    refusal("meters_per_unit_invalid", gf.run_piling, ents, default_preset(), store, 0.0)
    refusal("preset_missing", gf.run_piling, ents, None, store, 1.0)
    refusal("pvcase_piling_empty", gf.parse_pvcase_piling_json, "  ")
    refusal("pvcase_piling_malformed", gf.parse_pvcase_piling_json, "[1, 2]")
    refusal("pile_template_malformed", gf.deserialize_pile_template, '{"HorizontalPoleCount": "many"}')


def test_store_text_bounds():
    refusal("frame_presets_too_large", gf.load_frame_preset_store, " " * (gf.MAX_JSON_CHARS + 1))
    refusal("frame_presets_not_text", gf.load_frame_preset_store, b"{}")
    refusal("pile_templates_too_large", gf.load_pile_template_store, " " * (gf.MAX_JSON_CHARS + 1))
    refusal("preset_file_unreadable", gf.load_frame_preset_store,
            _saved({"Name": {"nested": 1}, "Piling": {}}))
