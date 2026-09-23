"""Studio's ground layout engines against the plugin, computed.

Three layers, hermetic, none skipping:

  1. The plugin's OWN unit tests, ported case for case where they test these
     engines (read 2026-09-23 at C:/tmp/solar-parity/wt-b25-s17):
       Tests/Tests/LeafSpacingTests.cs   ComputeMinimumPitch basic cases, argument
                                         validation, PitchFromGcr / GcrFromPitch,
                                         the polar winter null
     plus the ShadeLimitAngle formula cases the SLA doc comment states
     (BacktrackingCalculator.cs:174-191).
  2. The licensed outputs of the 2026-09-23 terrain capture, COMPUTED from the
     committed terrain intake (docs/parity/evidence/ground/terrain/intake.json)
     through Studio's own grid: LEAFMODULE's nine writes, LEAFSPACING's minimum
     pitch 4.2279994425599625, LEAFTRACK's 119 rows and 34986 slots, LEAFSAT's
     118 rows, 34692 slots and shade limit angle 29.780934315714596, LEAFSETBACK's
     5 m ring, and the settings store's committed routing catalog.
  3. Hand-computed rules (banker's rounding, the E-W slope, the drawer, the offset)
     and bounds and malformed-input refusals.
"""
from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INTAKE = ROOT / "docs" / "parity" / "evidence" / "ground" / "terrain" / "intake.json"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


layout = _load("solar_ground_layout", ROOT / "server" / "solar_ground_layout.py")
ground = _load("solar_ground_terrain_for_layout_tests", ROOT / "server" / "solar_ground_terrain.py")

MODULE_WRITES = {  # a3, the capture's settings delta (every prompt at its default)
    "TrackerModuleAlongAxisM": 1.0, "TrackerModuleCrossAxisM": 2.1, "TrackerModuleGapM": 0.02,
    "TrackerRailOverhangM": 0.05, "TorqueTubeHeightM": 1.5, "TrackerCorridorGapM": 0.0,
    "TrackerSecondaryCorridorGapM": 0.0, "TrackerModulePmaxW": 400.0, "TrackerTorqueTubeRadiusM": 0.08}
MIN_PITCH = 4.2279994425599625
SLA = 29.780934315714596


# ------------------------------------------------ 1. the plugin's own cases --

def test_minimum_pitch_tropical_site_is_not_null():
    assert layout.compute_minimum_pitch(10.0, 10.0, 2.0, 355, 9.0, 15.0) is not None


def test_minimum_pitch_high_latitude_larger_than_low():
    lo = layout.compute_minimum_pitch(20.0, 20.0, 2.0, 355, 9.0, 15.0)
    hi = layout.compute_minimum_pitch(50.0, 20.0, 2.0, 355, 9.0, 15.0)
    assert hi["min_pitch_m"] > lo["min_pitch_m"]


def test_minimum_pitch_zero_tilt_is_the_footprint():
    r = layout.compute_minimum_pitch(35.0, 0.0, 2.0, 355, 9.0, 15.0)
    assert r["min_pitch_m"] == pytest.approx(r["frame_footprint_m"], abs=0.001)


def test_minimum_pitch_wider_window_needs_at_least_as_much_pitch():
    narrow = layout.compute_minimum_pitch(35.0, 20.0, 2.0, 355, 10.0, 14.0)
    wide = layout.compute_minimum_pitch(35.0, 20.0, 2.0, 355, 8.0, 16.0)
    assert wide["min_pitch_m"] >= narrow["min_pitch_m"]


def test_minimum_pitch_gcr_is_footprint_over_pitch_and_in_range():
    r = layout.compute_minimum_pitch(35.0, 20.0, 2.0, 355, 9.0, 15.0)
    assert r["gcr"] == pytest.approx(r["frame_footprint_m"] / r["min_pitch_m"], abs=1e-6)
    assert 0.0 < r["gcr"] < 1.0
    assert r["min_pitch_m"] > r["frame_footprint_m"]
    assert r["worst_case_solar_elevation_deg"] > 0
    assert 2.5 <= r["min_pitch_m"] <= 8.0


@pytest.mark.parametrize("tilt,length,start,end", [(-5.0, 2.0, 9.0, 15.0), (20.0, 0.0, 9.0, 15.0),
                                                   (20.0, 2.0, 15.0, 9.0)])
def test_minimum_pitch_argument_validation(tilt, length, start, end):
    with pytest.raises(layout.LayoutInputError):
        layout.compute_minimum_pitch(35.0, tilt, length, 355, start, end)


def test_pitch_from_gcr_round_trip_and_ordering():
    pitch = layout.spacing_pitch_from_gcr(2.0, 20.0, 0.35)
    assert layout.spacing_gcr_from_pitch(2.0, 20.0, pitch) == pytest.approx(0.35, abs=1e-9)
    assert layout.spacing_pitch_from_gcr(2.0, 20.0, 0.45) < layout.spacing_pitch_from_gcr(2.0, 20.0, 0.30)
    with pytest.raises(layout.LayoutInputError):
        layout.spacing_gcr_from_pitch(2.0, 20.0, 0.0)
    for bad in (0.0, 1.0):
        with pytest.raises(layout.LayoutInputError):
            layout.spacing_pitch_from_gcr(2.0, 20.0, bad)


def test_polar_winter_returns_null():
    assert layout.compute_minimum_pitch(89.0, 20.0, 2.0, 355, 9.0, 15.0) is None


def test_shade_limit_angle_formula_and_validation():
    g, tilt = 0.4, 60.0
    expected = math.degrees(math.atan(g * math.sin(math.radians(tilt)) / (1 - g * math.cos(math.radians(tilt)))))
    assert layout.compute_shade_limit_angle(g, tilt) == pytest.approx(expected, abs=1e-12)
    assert layout.compute_shade_limit_angle(0.5, 60.0) > layout.compute_shade_limit_angle(0.3, 60.0)
    for gcr, t in ((0.0, 60.0), (1.0, 60.0), (0.4, 0.0), (0.4, 90.0)):
        with pytest.raises(layout.LayoutInputError):
            layout.compute_shade_limit_angle(gcr, t)


# ----------------------------------------- 2. the licensed outputs, computed --

def _settings_after(*writes):
    stored = {}
    for w in writes:
        stored = layout.save_settings(stored, w)
    return stored


@pytest.fixture(scope="module")
def fixture():
    """The terrain intake and Studio's own t1 grid from its faces (what LEAFTRACK reads)."""
    intake = json.loads(INTAKE.read_text(encoding="utf-8"))
    faces = [{"layer": ground.PREFERRED_TERRAIN_LAYER, "vertices": f} for f in intake["terrain_faces"]]
    result = ground.topo_from_3d_faces(faces, 1.0, ground.DEFAULT_TARGET_CELLS)
    assert result["succeeded"]
    module = layout.module_command(layout.load_settings({}))
    spacing = layout.spacing_command(layout.load_settings(_settings_after(module)))
    return {"intake": intake, "grid": result["grid"], "module": module, "spacing": spacing,
            "settings": _settings_after(module, spacing["writes"])}


def test_leafmodule_writes_the_captured_nine_settings(fixture):
    assert fixture["module"] == MODULE_WRITES


def test_leafmodule_changes_exactly_the_captured_seven_settings():
    changed = dict(layout.changed_settings({}, _settings_after(MODULE_WRITES)))
    assert sorted(changed) == ["TorqueTubeHeightM", "TrackerModuleAlongAxisM", "TrackerModuleCrossAxisM",
                               "TrackerModuleGapM", "TrackerModulePmaxW", "TrackerRailOverhangM",
                               "TrackerTorqueTubeRadiusM"]


def test_leafspacing_minimum_pitch_and_its_one_write(fixture):
    r = fixture["spacing"]["result"]
    assert r["min_pitch_m"] == pytest.approx(MIN_PITCH, abs=1e-12)
    assert r["design_day_of_year"] == 355 and r["worst_case_hour_angle_deg"] == -45.0
    assert list(fixture["spacing"]["writes"]) == ["LeafSpacingMinPitchM"]


def test_leaftrack_on_the_fixture_gives_119_rows_and_34986_slots(fixture):
    out = layout.tracker_command(fixture["intake"]["boundary"], layout.load_settings(fixture["settings"]),
                                 fixture["grid"])
    rows = out["placements"]
    assert out["variable_pitch"] and out["pitch_m"] == 4.2 and out["gcr"] == pytest.approx(0.5, abs=1e-15)
    assert len(rows) == 119 and sum(r["slots"] for r in rows) == 34986 == out["slots"]
    assert [r["row_index"] for r in rows] == list(range(119))
    first, second, last = rows[0], rows[1], rows[-1]
    assert first["insert"][0] == 2.1 and first["insert"][1] == pytest.approx(150.0, abs=1e-9)
    # The captured inserts (printed to 12 decimals): the terrain slope compresses each advance.
    assert second["insert"][0] == pytest.approx(6.299989062621, abs=1e-9)
    assert last["insert"][0] == pytest.approx(497.697944860936, abs=1e-9)
    assert first["axis_start"] == (2.1, pytest.approx(0.01, abs=1e-9))
    assert first["axis_end"][1] == pytest.approx(299.99, abs=1e-9)
    assert first["rotation_rad"] == pytest.approx(1.570796326795, abs=1e-12)
    assert first["scale"][0] == pytest.approx(299.98, abs=1e-9) and first["scale"][1] == 2.1
    assert (first["block"], first["source_command"], first["tracker_model"]) == (
        "LEAFSAT", "LEAFTRACK", "single_axis_tracker")
    assert (first["row_length_m"], first["rail_overhang_m"], first["row_pitch_m"]) == (
        pytest.approx(299.88, abs=1e-9), 0.05, 4.2)
    assert set(r["slots"] for r in rows) == {294}


def test_leafsat_on_the_fixture_gives_118_rows_34692_slots_and_the_sla(fixture):
    out = layout.sat_command(fixture["intake"]["boundary"], layout.load_settings(fixture["settings"]),
                             fixture["grid"])
    rows = out["placements"]
    assert len(rows) == 118 and sum(r["slots"] for r in rows) == 34692
    assert out["gcr"] == pytest.approx(0.496688807208, abs=1e-12)
    assert out["pitch_m"] == pytest.approx(MIN_PITCH, abs=1e-12)
    assert out["shade_limit_angle_deg"] == pytest.approx(SLA, abs=1e-12)
    assert out["writes"] == {"ShadeLimitAngleDeg": out["shade_limit_angle_deg"]}
    assert rows[0]["insert"][0] == pytest.approx(2.113999721280, abs=1e-9)
    assert rows[-1]["insert"][0] == pytest.approx(496.787801215011, abs=1e-9)
    assert rows[0]["source_command"] == "LEAFSAT"


def test_leafsetback_array_5_is_the_captured_ring(fixture):
    out = layout.setback_command(fixture["intake"]["boundary"], {"kind": "Array", "distance": 5})
    assert (out["kind"], out["distance"], out["marked"]) == ("array", 5.0, False)
    assert out["rings"] == [[(5.0, 5.0), (495.0, 5.0), (495.0, 295.0), (5.0, 295.0)]]


def test_settings_store_appends_the_default_catalog_on_every_saving_load():
    # DrawingPropertiesJson.cs:533 seeds two cables; the stored ones are appended on load,
    # so each saving command commits two more (the capture: absent, then 4 after a4, 6 after a9).
    s3 = _settings_after(MODULE_WRITES)
    assert len(s3["HomerunRouting"]["CableCatalog"]) == 2
    assert "HomerunRouting" not in dict(layout.changed_settings({}, s3))
    s4 = layout.save_settings(s3, {"LeafSpacingMinPitchM": MIN_PITCH})
    assert len(s4["HomerunRouting"]["CableCatalog"]) == 4
    assert [n for n, _ in layout.changed_settings(s3, s4)] == ["HomerunRouting", "LeafSpacingMinPitchM"]
    s9 = layout.save_settings(s4, {"ShadeLimitAngleDeg": SLA})
    assert len(s9["HomerunRouting"]["CableCatalog"]) == 6
    assert [n for n, _ in layout.changed_settings(s4, s9)] == ["HomerunRouting", "ShadeLimitAngleDeg"]
    assert layout.load_settings(s9)["HomerunRouting"]["CableCatalog"][:2] == layout.routing_default()["CableCatalog"]


def test_an_absent_setting_written_at_its_default_is_not_a_change():
    assert layout.changed_settings({}, {"TrackerCorridorGapM": 0.0, "HomerunRouting": layout.routing_default()}) == []
    assert layout.changed_settings({"TrackerCorridorGapM": 1.0}, {}) == [("TrackerCorridorGapM", 0.0)]


# ------------------------------------------------- 3. rules and refusals --

def test_net_round_is_dotnet_math_round_to_even():
    assert layout.net_round(MIN_PITCH, 1) == 4.2
    assert layout.net_round(0.25, 1) == 0.2 and layout.net_round(1.25, 1) == 1.2
    assert layout.net_round(2.1 / 0.3048, 2) == 6.89


def test_ew_slope_is_the_mean_column_pair_slope():
    q = layout.TerrainSlopeQuery([0.0, 1.0, 3.0, 0.0, 1.0, 5.0], 2, 3, 0.0, 20.0, 1.0)
    assert q.ew_slope_rad(5.0) == math.atan2(1.0, 10.0)
    assert q.ew_slope_rad(15.0) == (math.atan2(2.0, 10.0) + math.atan2(4.0, 10.0)) / 2
    assert q.ew_slope_rad(-100.0) == q.ew_slope_rad(0.0) and q.ew_slope_rad(1e9) == q.ew_slope_rad(15.0)
    assert layout.TerrainSlopeQuery([1.0], 1, 1, 0.0, 1.0).ew_slope_rad(0.5) == 0.0
    assert layout.terrain_slope_query(None, 1.0) is None
    assert layout.terrain_slope_query({"rows": 2}, 1.0) is None


def test_flat_layout_without_a_grid_and_without_a_stored_module():
    # No stored module: the module prompts' defaults (1.0 x 2.1 m, gap 0.02, no overhang).
    out = layout.tracker_command([[0, 0], [20, 0], [20, 10.3], [0, 10.3]], {}, None, {"pitch": 5})
    assert not out["variable_pitch"] and out["pitch_m"] == 5.0
    assert [p["insert"][0] for p in out["placements"]] == [2.5, 7.5, 12.5, 17.5]
    assert {p["slots"] for p in out["placements"]} == {10}
    assert out["placements"][0]["axis_start"] == (2.5, pytest.approx(0.05, abs=1e-9))
    assert out["placements"][0]["rail_overhang_m"] == 0.0 and out["placements"][0]["module_gap_m"] == 0.02


def test_default_pitch_is_six_metres_without_a_spacing_result():
    out = layout.tracker_command([[0, 0], [13, 0], [13, 5], [0, 5]], {}, None)
    assert out["pitch_m"] == 6.0 and [p["insert"][0] for p in out["placements"]] == [3.0, 9.0]


def test_drawer_skips_a_degenerate_axis_and_clamps_row_fields():
    module = layout.get_active_module({})
    rows = [layout._sweep.TrackerRowLayout((0.0, 0.0), (0.0, 0.0), 3, 3.06, 0.0, 0),
            layout._sweep.TrackerRowLayout((1.0, 0.0), (1.0, 4.0), 40000, 3.06, 0.0, 70000)]
    placed = layout.draw_rows(rows, module, 1.0, 0.0, 5.0, "LEAFTRACK")
    assert len(placed) == 1 and (placed[0]["row_index"], placed[0]["slots"]) == (32767, 32767)
    assert placed[0]["gcr"] == 2.1 / 5.0 and placed[0]["insert"] == (1.0, 2.0, 0.0)


def test_sat_gcr_prompt_reprompts_out_of_range_and_never_substitutes():
    assert layout.resolve_gcr([40, 1.0, None], 0.4) == 0.4
    assert layout.resolve_gcr([45, 0.45], 0.4) == 0.45
    assert layout.resolve_gcr([45], 0.4) is None
    out = layout.sat_command([[0, 0], [20, 0], [20, 10], [0, 10]], {}, None, {"gcr_entries": [40]})
    assert out["placements"] == [] and out["writes"] == {}


def test_sat_default_gcr_without_a_spacing_result_is_0_40():
    out = layout.sat_command([[0, 0], [20, 0], [20, 10], [0, 10]], {}, None)
    assert out["gcr"] == 0.4 and out["pitch_m"] == pytest.approx(5.25, abs=1e-12)


def test_setback_zero_marks_the_boundary_and_too_deep_an_offset_gives_nothing():
    square = [[0, 0], [10, 0], [10, 10], [0, 10]]
    marked = layout.setback_command(square)
    assert marked["marked"] and marked["rings"] == [[(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]]
    assert layout.setback_command(square, {"distance": 6})["rings"] == []
    cw = layout.setback_command(list(reversed(square)), {"kind": "fence", "distance": 1})
    assert cw["kind"] == "fence" and cw["rings"] == [[(1.0, 9.0), (9.0, 9.0), (9.0, 1.0), (1.0, 1.0)]]


def test_setback_offset_of_a_concave_ring_keeps_its_reflex_corner():
    ell = [[0, 0], [10, 0], [10, 4], [4, 4], [4, 10], [0, 10]]
    ring = layout.offset_inward(ell, 1)[0]
    assert ring == [(1.0, 1.0), (9.0, 1.0), (9.0, 3.0), (3.0, 3.0), (3.0, 9.0), (1.0, 9.0)]


@pytest.mark.parametrize("call", [
    lambda: layout.tracker_command([[0, 0], [1, 0]], {}, None),
    lambda: layout.tracker_command([[0, 0], [1, 0], [float("nan"), 1]], {}, None),
    lambda: layout.tracker_command([[0, 0], [1, 0], [1, 1]], {}, None, {"pitch": -1}),
    lambda: layout.tracker_command([[0, 0], [1, 0], [1, 1]], {}, None, {"bogus": 1}),
    lambda: layout.tracker_command([[0, 0], [1, 0], [1, 1]], MODULE_WRITES, None, {"use_stored_module": "Maybe"}),
    lambda: layout.module_command({}, {"preset": "4"}),
    lambda: layout.module_command({}, {"torque_tube_height_m": 0}),
    lambda: layout.module_command([], None),
    lambda: layout.setback_command([[0, 0], [1, 0], [1, 1]], {"kind": "Road"}),
    lambda: layout.save_settings({}, {"NotASetting": 1}),
    lambda: layout.load_settings({"HomerunRouting": []}),
    lambda: layout.resolve_gcr([None] * 65, 0.4),
], ids=["two-vertex", "nan", "negative-pitch", "unknown-answer", "bad-keyword", "bad-preset",
        "zero-height", "settings-not-a-map", "bad-kind", "unknown-setting", "bad-routing", "too-many-entries"])
def test_malformed_input_is_refused(call):
    with pytest.raises(layout.LayoutInputError):
        call()


def test_bounds_refuse_runaway_inputs():
    with pytest.raises(layout.LayoutBoundsError):
        layout.tracker_command([[0, 0], [1e7, 0], [1e7, 1], [0, 1]], {}, None, {"pitch": 0.01})
    many = [[math.cos(2 * math.pi * i / 3000) * 100, math.sin(2 * math.pi * i / 3000) * 100] for i in range(3000)]
    with pytest.raises(layout.LayoutBoundsError):
        layout.offset_inward(many, 1)


def test_manual_module_and_answers_override_defaults():
    writes = layout.module_command({}, {"preset": "Manual", "manual_cross_m": 2.4, "manual_along_m": 1.1,
                                        "manual_gap_mm": 10, "pmax_w": 600})
    assert (writes["TrackerModuleCrossAxisM"], writes["TrackerModuleAlongAxisM"]) == (2.4, 1.1)
    assert writes["TrackerModuleGapM"] == 0.01 and writes["TrackerModulePmaxW"] == 600.0
    # Manual keeps the last-saved Pmax as the default (cs:173-174), 550 when none.
    assert layout.module_command({}, {"preset": "Manual"})["TrackerModulePmaxW"] == 550.0
    assert layout.module_command({}, {"preset": "3"})["TrackerModulePmaxW"] == 665.0
    # Saved non-default values become the next run's defaults (cs:101, :118, :191).
    again = layout.module_command({"TrackerRailOverhangM": 0.1, "TorqueTubeHeightM": 2.0,
                                   "TrackerTorqueTubeRadiusM": 0.06})
    assert (again["TrackerRailOverhangM"], again["TorqueTubeHeightM"], again["TrackerTorqueTubeRadiusM"]) == (
        0.1, 2.0, 0.06)
