"""Studio's terrain analytics engines against the plugin, computed.

Three layers, hermetic, none skipping:

  1. The plugin's OWN unit tests, ported case for case (read 2026-09-23 at
     C:/tmp/solar-parity/wt-b25-s17):
       Tests/Tests/SlopeHeatmapCalculatorTests.cs  every case
       Tests/Tests/LeafSurveyTests.cs              every SurveyFaceAnalyzer case
       Tests/Tests/LeafGradeTests.cs               LeafGradeTests, LeafClearanceTests,
                                                   TerrainGridInterpolatorRowsColsTests,
                                                   the TerrainExporter ToCsv cases and
                                                   LeafGradeMultiTests
     Omitted, named here rather than left silent: the ElevationApiClient no-data cases in
     LeafSurveyTests (USGS HTTP parsing, not these engines), the ToLandXml cases (the
     LEAFLANDXML command, not LEAFTERRAINCSV) and LeafRoadTests (LEAFROAD).
  2. The terrain fixture (docs/parity/evidence/ground/terrain/intake.json, inputs only):
     Studio's own t1 grid from the intake's faces, then the licensed outputs reproduced by
     computation: the a1 slope counts, the a2 CSV file byte for byte (its SHA-256 and
     length, the file itself stays private), the a10 balanced elevation and label, and
     the a12 survey colours.
  3. Hand-computed rules (number formatting, solid corner order, the grade label) and
     bounds and malformed-input refusals.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INTAKE = ROOT / "docs" / "parity" / "evidence" / "ground" / "terrain" / "intake.json"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


engine = _load("solar_ground_analysis", ROOT / "server" / "solar_ground_analysis.py")
terrain = _load("solar_ground_terrain", ROOT / "server" / "solar_ground_terrain.py")


def grid_dict(elevs, x_min=0.0, x_max=0.0, y_min=0.0, y_max=0.0):
    """A neutral grid from a 2D elevation table (row 0 is the south row)."""
    rows, cols = len(elevs), len(elevs[0])
    return {"elevations": [float(e) for row in elevs for e in row], "rows": rows, "cols": cols,
            "x_min": float(x_min), "x_max": float(x_max), "y_min": float(y_min), "y_max": float(y_max)}


def heat_grid(elevs, x_min=0.0, x_max=0.0, y_min=0.0, y_max=0.0, mpu=1.0):
    """SlopeHeatmapCalculatorTests.Grid: a zero max defaults to one unit per cell."""
    rows, cols = len(elevs), len(elevs[0])
    if x_max == 0:
        x_max = cols - 1
    if y_max == 0:
        y_max = rows - 1
    return engine.TerrainGrid(grid_dict(elevs, x_min, x_max, y_min, y_max), mpu)


def interp(flat, rows, cols, x_min, x_max, y_min, y_max, mpu=1.0):
    return engine.TerrainGrid({"elevations": list(flat), "rows": rows, "cols": cols, "x_min": x_min,
                               "x_max": x_max, "y_min": y_min, "y_max": y_max}, mpu)


# ===========================================================================
#  1. Ported plugin tests
# ===========================================================================

# ---- SlopeHeatmapCalculatorTests.cs ----------------------------------------

def test_compute_cells_too_small_grid_returns_empty():
    assert engine.compute_slope_cells(heat_grid([[5]])) == []


def test_compute_cells_flat_terrain_all_green():
    cells = engine.compute_slope_cells(heat_grid([[10, 10, 10], [10, 10, 10], [10, 10, 10]]))
    assert len(cells) == 4
    for c in cells:
        assert c["slope_percent"] == pytest.approx(0.0, abs=1e-9)
        assert c["bucket"] == "Green"


def test_compute_cells_cell_geometry_matches_grid_nodes():
    cells = engine.compute_slope_cells(heat_grid([[0, 0, 0], [0, 0, 0], [0, 0, 0]],
                                                 x_min=0, x_max=20, y_min=0, y_max=10))
    assert len(cells) == 4
    assert (cells[0]["x0"], cells[0]["y0"], cells[0]["x1"], cells[0]["y1"]) == pytest.approx((0, 0, 10, 5), abs=1e-9)
    assert (cells[3]["x0"], cells[3]["y0"], cells[3]["x1"], cells[3]["y1"]) == pytest.approx((10, 5, 20, 10), abs=1e-9)


def test_compute_cells_gentle_green_below_5_percent():
    cells = engine.compute_slope_cells(heat_grid([[0.0, 0.2], [0.0, 0.2]], x_max=10, y_max=10))
    assert len(cells) == 1
    assert cells[0]["slope_percent"] == pytest.approx(2.0, abs=1e-9)
    assert cells[0]["bucket"] == "Green"


def test_compute_cells_moderate_yellow_between_5_and_15_percent():
    cells = engine.compute_slope_cells(heat_grid([[0, 1], [0, 1]], x_max=10, y_max=10))
    assert cells[0]["slope_percent"] == pytest.approx(10.0, abs=1e-9)
    assert cells[0]["bucket"] == "Yellow"


def test_compute_cells_steep_red_above_15_percent():
    cells = engine.compute_slope_cells(heat_grid([[0, 5], [0, 5]], x_max=10, y_max=10))
    assert cells[0]["slope_percent"] == pytest.approx(50.0, abs=1e-9)
    assert cells[0]["bucket"] == "Red"


def test_compute_cells_max_edge_wins_asymmetric_rise():
    cells = engine.compute_slope_cells(heat_grid([[0, 0], [0, 20]], x_max=10, y_max=10))
    assert cells[0]["slope_percent"] == pytest.approx(200.0, abs=1e-9)
    assert cells[0]["bucket"] == "Red"


def test_compute_cells_honors_meters_per_unit_feet_drawing():
    cells = engine.compute_slope_cells(heat_grid([[0, 1], [0, 1]], x_max=10, y_max=10, mpu=0.3048))
    assert cells[0]["slope_percent"] == pytest.approx(32.8084, abs=1e-3)
    assert cells[0]["bucket"] == "Red"


def test_aci_for_bucket_matches_pvcase_color_convention():
    assert engine.aci_for_bucket("Green") == 3
    assert engine.aci_for_bucket("Yellow") == 2
    assert engine.aci_for_bucket("Red") == 1


# ---- LeafSurveyTests.cs (SurveyFaceAnalyzer) --------------------------------

def V(x, y, z):
    return [float(x), float(y), float(z)]


def flat_quad(z=0.0):
    return [V(0, 0, z), V(10, 0, z), V(10, 10, z), V(0, 10, z)]


def test_edge_slope_horizontal_edge_returns_zero():
    assert engine.edge_slope_percent(V(0, 0, 100), V(50, 0, 100), 1.0) == pytest.approx(0.0, abs=1e-9)


def test_edge_slope_vertical_edge_returns_zero():
    assert engine.edge_slope_percent(V(5, 5, 0), V(5, 5, 100), 1.0) == pytest.approx(0.0, abs=1e-9)


def test_edge_slope_known_slope_returns_correct_percent():
    assert engine.edge_slope_percent(V(0, 0, 0), V(10, 0, 1), 1.0) == pytest.approx(10.0, abs=0.001)


def test_edge_slope_feet_drawing_units_converted_to_meters():
    assert engine.edge_slope_percent(V(0, 0, 0), V(1, 0, 1), 0.3048) == pytest.approx(100.0, abs=0.01)


def test_edge_slope_diagonal_edge_uses_euclidean_distance():
    assert engine.edge_slope_percent(V(0, 0, 0), V(3, 4, 5), 1.0) == pytest.approx(100.0, abs=0.001)


def test_max_edge_slope_flat_quad_returns_zero():
    assert engine.max_edge_slope(V(0, 0, 100), V(10, 0, 100), V(10, 10, 100), V(0, 10, 100), 1.0) == \
        pytest.approx(0.0, abs=1e-9)


def test_max_edge_slope_quad_with_one_steep_edge_returns_that_edge_slope():
    assert engine.max_edge_slope(V(0, 0, 0), V(10, 0, 5), V(10, 10, 5), V(0, 10, 0), 1.0) == \
        pytest.approx(50.0, abs=0.001)


def test_analyze_empty_input_returns_empty_list():
    assert engine.analyze_survey_faces([]) == []


def test_analyze_flat_faces_all_buckets_green():
    result = engine.analyze_survey_faces([flat_quad(0), flat_quad(100), flat_quad(-50)], 1.0)
    assert len(result) == 3
    assert all(f["bucket"] == "Green" for f in result)


def test_analyze_steep_face_bucket_is_red():
    result = engine.analyze_survey_faces([[V(0, 0, 0), V(10, 0, 2), V(10, 10, 2), V(0, 10, 0)]], 1.0)
    assert result[0]["bucket"] == "Red"
    assert result[0]["slope_percent"] == pytest.approx(20.0, abs=0.001)


def test_analyze_yellow_zone_bucket_is_yellow():
    result = engine.analyze_survey_faces([[V(0, 0, 0), V(10, 0, 1), V(10, 10, 1), V(0, 10, 0)]], 1.0)
    assert result[0]["bucket"] == "Yellow"


def test_analyze_triangular_face_accepted_as_degenerate_quad():
    [face] = engine.analyze_survey_faces([[V(0, 0, 0), V(10, 0, 0), V(5, 10, 0)]])
    assert face["vertices"][3] == face["vertices"][2]


def test_analyze_null_face_list_refuses():
    with pytest.raises(engine.AnalysisInputError):
        engine.analyze_survey_faces(None)


def test_analyze_invalid_meters_per_unit_refuses():
    with pytest.raises(engine.AnalysisInputError):
        engine.analyze_survey_faces([], meters_per_unit=0.0)


def test_analyze_face_vertex_count_returns_correct_slope_percent():
    result = engine.analyze_survey_faces([[V(0, 0, 0), V(20, 0, 3), V(20, 10, 3), V(0, 10, 0)]], 1.0)
    assert result[0]["slope_percent"] == pytest.approx(15.0, abs=0.001)
    assert result[0]["bucket"] == "Yellow"


# ---- LeafGradeTests.cs: TerrainGridInterpolator ----------------------------

def flat_grid_10m():
    return interp([10.0, 10.0, 10.0, 10.0], 2, 2, 0, 100, 0, 100)


def east_slope_grid():
    return interp([0.0, 20.0, 0.0, 20.0], 2, 2, 0, 100, 0, 100)


def square50():
    return [[25, 25], [75, 25], [75, 75], [25, 75]]


def test_constructor_null_elevations_refuses():
    with pytest.raises(engine.AnalysisInputError):
        engine.TerrainGrid({"elevations": None, "rows": 2, "cols": 2, "x_min": 0, "x_max": 100,
                            "y_min": 0, "y_max": 100}, 1.0)


def test_properties_reflect_constructor_args():
    grid = interp([5.0], 1, 1, 10, 20, 30, 40, 0.3048)
    assert (grid.x_min, grid.x_max, grid.y_min, grid.y_max) == (10, 20, 30, 40)
    assert grid.meters_per_unit == 0.3048


def test_interpolate_z_flat_grid_returns_constant_elevation():
    grid = flat_grid_10m()
    for x, y in ((50, 50), (0, 0), (100, 100)):
        assert grid.interpolate_z(x, y) == pytest.approx(10.0, abs=1e-9)


def test_interpolate_z_centre_of_east_slope_grid_returns_mid_elevation():
    assert east_slope_grid().interpolate_z(50, 50) == pytest.approx(10.0, abs=1e-9)


def test_interpolate_z_left_column_returns_zero():
    grid = east_slope_grid()
    assert grid.interpolate_z(0, 25) == pytest.approx(0.0, abs=1e-9)
    assert grid.interpolate_z(0, 75) == pytest.approx(0.0, abs=1e-9)


def test_interpolate_z_right_column_returns_20m():
    assert east_slope_grid().interpolate_z(100, 50) == pytest.approx(20.0, abs=1e-9)


def test_interpolate_z_outside_bounds_returns_none():
    grid = flat_grid_10m()
    for x, y in ((-1, 50), (101, 50), (50, -1), (50, 101)):
        assert grid.interpolate_z(x, y) is None


def test_interpolate_z_bilinear_interpolation_is_smooth():
    grid = interp([0.0, 4.0, 2.0, 6.0], 2, 2, 0, 100, 0, 100)
    assert grid.interpolate_z(50, 50) == pytest.approx(3.0, abs=1e-9)
    assert grid.interpolate_z(0, 0) == pytest.approx(0.0, abs=1e-9)
    assert grid.interpolate_z(100, 100) == pytest.approx(6.0, abs=1e-9)


def test_get_elevation_range_flat_grid_returns_same_min_and_max():
    assert flat_grid_10m().elevation_range() == (10.0, 10.0)


def test_get_elevation_range_sloped_grid_returns_correct_min_max():
    assert east_slope_grid().elevation_range() == (0.0, 20.0)


def test_rows_cols_reflect_constructor_args():
    grid = interp([0.0] * 6, 2, 3, 0, 100, 0, 100)
    assert (grid.rows, grid.cols) == (2, 3)


# ---- LeafGradeTests.cs: GradingCalculator ----------------------------------

def test_is_inside_polygon_centre_of_square_returns_true():
    assert engine.is_inside_polygon([tuple(p) for p in square50()], 50, 50)


def test_is_inside_polygon_outside_square_returns_false():
    poly = [tuple(p) for p in square50()]
    assert not engine.is_inside_polygon(poly, 10, 10)
    assert not engine.is_inside_polygon(poly, 90, 90)


def test_compute_flat_flat_grid_at_exact_elevation_zero_cut_fill():
    r = engine.compute_flat(flat_grid_10m(), 10.0, square50(), 1.0, 5.0)
    assert r["cut_m3"] == pytest.approx(0.0, abs=1e-6)
    assert r["fill_m3"] == pytest.approx(0.0, abs=1e-6)


def test_compute_flat_proposed_below_flat_only_cut_volume():
    r = engine.compute_flat(flat_grid_10m(), 5.0, square50(), 1.0, 5.0)
    assert r["fill_m3"] == pytest.approx(0.0, abs=1e-6)
    assert r["cut_m3"] > 0.0


def test_compute_flat_proposed_above_flat_only_fill_volume():
    r = engine.compute_flat(flat_grid_10m(), 15.0, square50(), 1.0, 5.0)
    assert r["cut_m3"] == pytest.approx(0.0, abs=1e-6)
    assert r["fill_m3"] > 0.0


def test_compute_flat_flat_grid_volume_approximates_area():
    r = engine.compute_flat(flat_grid_10m(), 8.0, square50(), 1.0, 1.0)
    assert r["cut_m3"] == pytest.approx(5000.0, abs=200.0)


def test_compute_flat_null_interpolator_refuses():
    with pytest.raises(engine.AnalysisInputError):
        engine.compute_flat(None, 0.0, square50())


def test_compute_flat_too_few_boundary_points_refuses():
    with pytest.raises(engine.AnalysisInputError):
        engine.compute_flat(flat_grid_10m(), 0.0, [[0, 0], [1, 1]])


def test_compute_flat_net_volume_is_correct_sign():
    assert engine.compute_flat(flat_grid_10m(), 8.0, square50(), 1.0, 5.0)["net_m3"] > 0.0


def pad_a():
    return [[10, 10], [40, 10], [40, 40], [10, 40]]


def pad_b():
    return [[60, 60], [90, 60], [90, 90], [60, 90]]


def test_compute_flat_per_area_null_interpolator_refuses():
    with pytest.raises(engine.AnalysisInputError):
        engine.compute_flat_per_area(None, [(10.0, square50())])


def test_compute_flat_per_area_null_pads_refuses():
    with pytest.raises(engine.AnalysisInputError):
        engine.compute_flat_per_area(flat_grid_10m(), None)


def test_compute_flat_per_area_empty_pads_returns_empty_list():
    assert engine.compute_flat_per_area(flat_grid_10m(), []) == []


def test_compute_flat_per_area_single_pad_matches_direct_compute_flat():
    direct = engine.compute_flat(flat_grid_10m(), 8.0, square50(), 1.0, 2.0)
    [per] = engine.compute_flat_per_area(flat_grid_10m(), [(8.0, square50())], 1.0, 2.0)
    assert per["cut_m3"] == pytest.approx(direct["cut_m3"], abs=1e-9)
    assert per["fill_m3"] == pytest.approx(direct["fill_m3"], abs=1e-9)
    assert per["samples"] == direct["samples"]
    assert per["proposed_elevation_m"] == pytest.approx(8.0, abs=1e-12)


def test_compute_flat_per_area_two_pads_each_result_matches_individual_compute_flat():
    direct_a = engine.compute_flat(east_slope_grid(), 5.0, pad_a(), 1.0, 2.0)
    direct_b = engine.compute_flat(east_slope_grid(), 15.0, pad_b(), 1.0, 2.0)
    per = engine.compute_flat_per_area(east_slope_grid(), [(5.0, pad_a()), (15.0, pad_b())], 1.0, 2.0)
    assert len(per) == 2
    assert (per[0]["cut_m3"], per[0]["fill_m3"]) == pytest.approx((direct_a["cut_m3"], direct_a["fill_m3"]), abs=1e-9)
    assert (per[1]["cut_m3"], per[1]["fill_m3"]) == pytest.approx((direct_b["cut_m3"], direct_b["fill_m3"]), abs=1e-9)


def test_compute_flat_per_area_two_pads_sum_matches_site_wide_total():
    direct_a = engine.compute_flat(east_slope_grid(), 5.0, pad_a(), 1.0, 2.0)
    direct_b = engine.compute_flat(east_slope_grid(), 15.0, pad_b(), 1.0, 2.0)
    per = engine.compute_flat_per_area(east_slope_grid(), [(5.0, pad_a()), (15.0, pad_b())], 1.0, 2.0)
    assert per[0]["cut_m3"] + per[1]["cut_m3"] == pytest.approx(direct_a["cut_m3"] + direct_b["cut_m3"], abs=1e-9)
    assert per[0]["fill_m3"] + per[1]["fill_m3"] == pytest.approx(direct_a["fill_m3"] + direct_b["fill_m3"], abs=1e-9)


def test_compute_flat_per_area_result_carries_independent_proposed_elev_per_pad():
    per = engine.compute_flat_per_area(flat_grid_10m(), [(7.0, pad_a()), (13.0, pad_b())], 1.0, 2.0)
    assert per[0]["proposed_elevation_m"] == pytest.approx(7.0, abs=1e-12)
    assert per[1]["proposed_elevation_m"] == pytest.approx(13.0, abs=1e-12)


def test_find_balanced_elevation_flat_grid_returns_same_elevation():
    assert engine.find_balanced_elevation(flat_grid_10m(), square50(), 1.0, 5.0) == pytest.approx(10.0, abs=0.01)


def test_find_balanced_elevation_sloped_grid_cut_equals_fill():
    balanced = engine.find_balanced_elevation(east_slope_grid(), square50(), 1.0, 2.0)
    r = engine.compute_flat(east_slope_grid(), balanced, square50(), 1.0, 2.0)
    assert abs(r["net_m3"]) < r["cut_m3"] * 0.05


def test_find_balanced_elevation_null_interpolator_refuses():
    with pytest.raises(engine.AnalysisInputError):
        engine.find_balanced_elevation(None, square50())


# ---- LeafGradeTests.cs: LeafClearanceTests ---------------------------------

def test_find_clearance_elevation_flat_grid_returns_max_plus_clearance():
    assert engine.find_clearance_elevation(flat_grid_10m(), square50(), 0.6, 1.0, 5.0) == pytest.approx(10.6, abs=0.001)


def test_find_clearance_elevation_zero_clearance_returns_max_terrain():
    assert engine.find_clearance_elevation(flat_grid_10m(), square50(), 0.0, 1.0, 5.0) == pytest.approx(10.0, abs=0.001)


def test_find_clearance_elevation_sloped_grid_returns_highest_point_plus_clearance():
    assert engine.find_clearance_elevation(east_slope_grid(), square50(), 1.0, 1.0, 2.0) == pytest.approx(15.8, abs=0.2)


def test_find_clearance_elevation_ensures_pad_above_terrain():
    elev = engine.find_clearance_elevation(east_slope_grid(), square50(), 1.5, 1.0, 2.0)
    for x in range(26, 75, 2):
        for y in range(26, 75, 2):
            z = east_slope_grid().interpolate_z(x, y)
            if z is not None:
                assert elev >= z + 1.5 - 1e-6


def test_find_clearance_elevation_null_interpolator_refuses():
    with pytest.raises(engine.AnalysisInputError):
        engine.find_clearance_elevation(None, square50(), 0.6)


def test_find_clearance_elevation_too_few_boundary_points_refuses():
    with pytest.raises(engine.AnalysisInputError):
        engine.find_clearance_elevation(flat_grid_10m(), [[0, 0], [1, 1]], 0.6)


def test_find_clearance_elevation_boundary_outside_terrain_refuses():
    far = [[1000, 1000], [2000, 1000], [2000, 2000], [1000, 2000]]
    with pytest.raises(engine.AnalysisInputError, match="No terrain data found"):
        engine.find_clearance_elevation(flat_grid_10m(), far, 0.6, cell_size_du=50.0)


# ---- LeafGradeTests.cs: TerrainExporterTests (ToCsv) -----------------------

def grid_2x3():
    return interp([1.0, 2.0, 3.0, 4.0, 5.0, 6.0], 2, 3, 0, 200, 0, 100)


def test_to_csv_contains_header():
    assert engine.terrain_csv_text(grid_2x3()).startswith("X,Y,Z")


def test_to_csv_row_count_matches_grid_points():
    lines = [line for line in engine.terrain_csv_text(grid_2x3()).split("\n") if line]
    assert len(lines) == 7


def test_to_csv_null_interpolator_refuses():
    with pytest.raises(engine.AnalysisInputError):
        engine.terrain_csv_text(None)


def test_to_csv_scales_coordinates():
    assert "60.960" in engine.terrain_csv_text(grid_2x3(), meters_per_unit=0.3048)


# ---- LeafGradeTests.cs: LeafGradeMultiTests --------------------------------

def low_grid():
    return interp([5.0] * 4, 2, 2, 0, 100, 0, 100)


def high_grid():
    return interp([15.0] * 4, 2, 2, 0, 100, 0, 100)


def test_two_pads_manual_mode_total_volume_equals_sum_of_individual():
    a = engine.compute_flat(low_grid(), 10.0, pad_a(), 1.0, 5.0)
    b = engine.compute_flat(low_grid(), 10.0, pad_b(), 1.0, 5.0)
    assert a["fill_m3"] + b["fill_m3"] > 0.0
    assert a["cut_m3"] + b["cut_m3"] == pytest.approx(0.0, abs=0.01)


def test_two_pads_auto_mode_each_pad_gets_independent_balanced_elevation():
    low = engine.find_balanced_elevation(low_grid(), pad_a(), 1.0, 5.0)
    high = engine.find_balanced_elevation(high_grid(), pad_b(), 1.0, 5.0)
    assert abs(low - high) > 0.1
    assert low == pytest.approx(5.0, abs=0.5)
    assert high == pytest.approx(15.0, abs=0.5)


def test_two_pads_shared_manual_elevation_higher_terrain_produces_cut():
    r = engine.compute_flat(high_grid(), 10.0, pad_a(), 1.0, 5.0)
    assert r["cut_m3"] > 0.0
    assert r["fill_m3"] == pytest.approx(0.0, abs=0.01)


def test_multi_area_sum_exceeds_single_pad():
    a = engine.compute_flat(low_grid(), 10.0, pad_a(), 1.0, 5.0)
    b = engine.compute_flat(low_grid(), 10.0, pad_b(), 1.0, 5.0)
    assert a["fill_m3"] + b["fill_m3"] > a["fill_m3"]


# ===========================================================================
#  2. The terrain fixture: licensed outputs reproduced by computation
# ===========================================================================

# The a2 file LEAFTERRAINCSV wrote on the 2026-09-23 capture (private; only its digest
# and size are recorded here).
A2_CSV_SHA256 = "1d72bde87f122458fba3fc8549d43873d6dcd879910b01d0c57a348f0c0b0f16"
A2_CSV_BYTES = 303036
A2_CSV_LINES = 13501


@pytest.fixture(scope="module")
def intake():
    return json.loads(INTAKE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def fixture_grid(intake):
    """Studio's own t1 grid: LEAFTOPOFROM3DFACES over the intake's LEAF-TERRAIN faces,
    units and grid size at their prompt defaults (G11). t2 to t7 never change it."""
    faces = [{"layer": terrain.PREFERRED_TERRAIN_LAYER, "vertices": f} for f in intake["terrain_faces"]]
    result = terrain.topo_from_3d_faces(faces, 1.0, terrain.DEFAULT_TARGET_CELLS)
    assert result["succeeded"]
    return result["grid"]


def test_fixture_grid_shape(fixture_grid):
    assert (fixture_grid["rows"], fixture_grid["cols"]) == (90, 150)


def test_a1_slope_counts_reproduce_the_capture(fixture_grid):
    result = engine.slope_heatmap(fixture_grid, 1.0)
    assert result["succeeded"]
    assert (result["green"], result["yellow"], result["red"]) == (13227, 34, 0)
    assert len(result["cells"]) == 89 * 149
    colors = [c["color_index"] for c in result["cells"]]
    assert colors.count(3) == 13227 and colors.count(2) == 34 and colors.count(1) == 0


def test_a1_last_solid_matches_the_captured_corner_cell(fixture_grid):
    # The capture's first dumped LEAF-SLOPE solid is the north-east cell: x 496.743624161074
    # to 500.1, y 296.629213483146 to 300, yellow (ACI 2).
    cells = engine.slope_heatmap(fixture_grid, 1.0)["cells"]
    solid = engine.slope_solid(cells[-1])
    assert solid["color_index"] == 2
    expected = [(496.743624161074, 296.629213483146), (500.1, 296.629213483146),
                (496.743624161074, 300.0), (500.1, 300.0)]
    for (x, y, z), (ex, ey) in zip(solid["vertices"], expected):
        assert (x, y, z) == pytest.approx((ex, ey, 0.0), abs=1e-9)


def test_a2_terrain_csv_reproduces_the_licensed_file_byte_for_byte(fixture_grid):
    data = engine.terrain_csv_export(fixture_grid, 1.0)["bytes"]
    assert len(data) == A2_CSV_BYTES
    assert hashlib.sha256(data).hexdigest() == A2_CSV_SHA256
    text = data[3:].decode("utf-8")
    assert data[:3] == engine.UTF8_BOM
    lines = text.split("\r\n")
    assert lines[-1] == "" and len(lines) - 1 == A2_CSV_LINES
    assert lines[:3] == ["X,Y,Z", "0.000,0.000,0.150", "3.356,0.000,0.205"]
    assert lines[-2] == "500.100,300.000,0.449"


def test_a10_grade_reproduces_the_balanced_elevation_and_label(intake, fixture_grid):
    result = engine.grade_pad(fixture_grid, intake["boundary"], 1.0, mode=None)
    assert result["succeeded"] and result["mode"] == "Auto"
    assert result["elevation_m"] == pytest.approx(0.26064200685635874, abs=1e-9)
    assert result["label"]["text"] == "GRADE PAD\\P0.26 m"
    assert result["label"]["at"] == [250.0, 150.0]
    assert result["label"]["height"] == pytest.approx(20.0, abs=1e-12)
    assert result["pad"]["vertices"] == [[0.0, 0.0], [500.0, 0.0], [500.0, 300.0], [0.0, 300.0]]
    assert result["settings"] == {"GradingElevationM": result["elevation_m"], "GradingMode": 0}


def test_a12_survey_colours_every_face_green(intake):
    result = engine.survey_colors(intake["terrain_faces"], 1.0)
    assert (result["green"], result["yellow"], result["red"]) == (700, 0, 0)
    assert result["colors"] == [(0 << 16) | (200 << 8) | 0] * 700


# ===========================================================================
#  3. Hand-computed rules, bounds and refusals
# ===========================================================================

def test_format_fixed_rounds_ties_away_from_zero():
    assert engine.format_fixed(0.0625, 3) == "0.063"          # an exact binary tie
    assert engine.format_fixed(-0.0625, 3) == "-0.063"
    assert engine.format_fixed(2.5, 0) == "3"
    assert engine.format_fixed(0.26064200685635874, 2) == "0.26"
    assert engine.format_fixed(500.1, 3) == "500.100"


def test_format_fixed_negative_zero_by_runtime():
    assert engine.format_fixed(-0.0001, 3) == "-0.000"
    assert engine.format_fixed(-0.0001, 3, engine.RUNTIME_NETFX) == "0.000"


def test_format_fixed_netfx_rounds_through_fifteen_digits():
    # 2.0004999999999997 is below 2.0005 exactly, but is 2.00050000000000 at 15 digits.
    assert engine.format_fixed(2.0004999999999997, 3) == "2.000"
    assert engine.format_fixed(2.0004999999999997, 3, engine.RUNTIME_NETFX) == "2.001"


def test_format_fixed_refuses_bad_input():
    for bad in (float("nan"), float("inf"), "1", True):
        with pytest.raises(engine.AnalysisInputError):
            engine.format_fixed(bad, 3)
    with pytest.raises(engine.AnalysisBoundsError):
        engine.format_fixed(1e16, 3)
    with pytest.raises(engine.AnalysisInputError):
        engine.format_fixed(1.0, 3, "mono")


def test_slope_solid_corner_order_is_bl_br_tl_tr():
    cell = engine.compute_slope_cells(heat_grid([[0, 0], [0, 0]], x_max=10, y_max=4))[0]
    assert engine.slope_solid(cell)["vertices"] == [(0, 0, 0), (10, 0, 0), (0, 4, 0), (10, 4, 0)]
    assert engine.slope_solid(cell)["layer"] == "LEAF-SLOPE"


def test_slope_heatmap_without_terrain_draws_nothing():
    result = engine.slope_heatmap(None, 1.0)
    assert not result["succeeded"] and result["cells"] == []


def test_slope_heatmap_reports_the_plugin_counts():
    result = engine.slope_heatmap(grid_dict([[0, 1, 1], [0, 1, 6]], 0, 20, 0, 10), 1.0)
    assert (result["green"], result["yellow"], result["red"]) == (0, 1, 1)
    assert result["counts"] == {"cells": 2, "green_cells": 0, "yellow_cells": 1, "red_cells": 1,
                                "max_slope_pct_x100": 5000}


def test_terrain_csv_text_is_crlf_with_all_nodes_row_major():
    text = engine.terrain_csv_text(grid_2x3())
    assert text == ("X,Y,Z\r\n0.000,0.000,1.000\r\n100.000,0.000,2.000\r\n200.000,0.000,3.000\r\n"
                    "0.000,100.000,4.000\r\n100.000,100.000,5.000\r\n200.000,100.000,6.000\r\n")
    assert engine.terrain_csv_file_bytes(grid_2x3()) == engine.UTF8_BOM + text.encode("utf-8")


def test_terrain_csv_export_without_terrain_writes_nothing():
    assert engine.terrain_csv_export(None)["bytes"] is None


def test_grade_pad_label_and_settings_on_a_sloped_grid():
    grid = grid_dict([[0.0, 20.0], [0.0, 20.0]], 0, 100, 0, 100)
    result = engine.grade_pad(grid, square50(), 1.0, mode="auto")
    # cell size max(100 / 50, 1) = 2; the east slope balances at its middle, 10 m.
    assert result["elevation_m"] == pytest.approx(10.0, abs=0.01)
    assert result["label"]["text"] == "GRADE PAD\\P" + engine.format_fixed(result["elevation_m"], 2) + " m"
    assert result["label"]["at"] == [50.0, 50.0]
    assert result["label"]["height"] == pytest.approx(2.5, abs=1e-12)     # (50 + 50) / 2 * 0.05
    assert result["settings"]["GradingMode"] == 0


def test_grade_pad_feet_label_and_clearance_mode():
    grid = grid_dict([[10.0, 10.0], [10.0, 10.0]], 0, 100, 0, 100)
    result = engine.grade_pad(grid, square50(), 0.3048, mode="Clearance")
    assert result["settings"]["GradingMode"] == 1
    assert result["elevation_m"] == pytest.approx(10.6, abs=1e-9)          # default 0.6 m
    assert result["label"]["text"].endswith(" ft")


def test_grade_pad_manual_default_and_no_terrain():
    grid = grid_dict([[3.0, 4.0], [5.0, 6.0]], 0, 100, 0, 100)
    assert engine.grade_pad(grid, square50(), 1.0, mode="Manual")["elevation_m"] == 3.0
    no_terrain = engine.grade_pad(None, square50(), 1.0)
    assert no_terrain["elevation_m"] == 0.0 and no_terrain["cut_fill"] is None


def test_grade_pad_with_too_few_vertices_aborts_with_the_plugin_message():
    result = engine.grade_pad(grid_dict([[0, 0], [0, 0]], 0, 1, 0, 1), [[0, 0], [1, 1]], 1.0)
    assert not result["succeeded"] and result["message"] == "Pad boundary has fewer than 3 vertices - aborting."


def test_survey_colors_bucket_true_colours():
    faces = [flat_quad(), [V(0, 0, 0), V(10, 0, 1), V(10, 10, 1), V(0, 10, 0)],
             [V(0, 0, 0), V(10, 0, 2), V(10, 10, 2), V(0, 10, 0)]]
    result = engine.survey_colors(faces, 1.0)
    assert result["colors"] == [0x00C800, 0xFFC800, 0xDC0000]
    assert (result["green"], result["yellow"], result["red"]) == (1, 1, 1)
    assert not engine.survey_colors([], 1.0)["succeeded"]


def test_units_keyword_defaults_to_meters():
    assert engine.meters_per_unit_for_keyword(None) == 1.0
    assert engine.meters_per_unit_for_keyword("feet") == 0.3048
    with pytest.raises(engine.AnalysisInputError):
        engine.meters_per_unit_for_keyword("Yards")


@pytest.mark.parametrize("grid", [
    "not a grid",
    {"elevations": [0.0] * 3, "rows": 2, "cols": 2, "x_min": 0, "x_max": 1, "y_min": 0, "y_max": 1},
    {"elevations": [0.0, 0.0, 0.0, float("nan")], "rows": 2, "cols": 2, "x_min": 0, "x_max": 1,
     "y_min": 0, "y_max": 1},
    {"elevations": [0.0] * 4, "rows": 2, "cols": 2, "x_min": 0, "x_max": "1", "y_min": 0, "y_max": 1},
    {"elevations": [0.0] * 4, "rows": 2.0, "cols": 2, "x_min": 0, "x_max": 1, "y_min": 0, "y_max": 1},
    {"elevations": [0.0] * 4, "rows": 2, "cols": 2, "x_min": 0, "x_max": 1, "y_min": 0, "y_max": 1,
     "frame": [0, 0, 1]},
])
def test_malformed_grids_refuse(grid):
    with pytest.raises(engine.AnalysisInputError):
        engine.slope_heatmap(grid, 1.0)


def test_bounds_refuse():
    with pytest.raises(engine.AnalysisBoundsError):
        engine.TerrainGrid({"elevations": [], "rows": 3000, "cols": 3000, "x_min": 0, "x_max": 1,
                            "y_min": 0, "y_max": 1}, 1.0)
    with pytest.raises(engine.AnalysisBoundsError):
        engine.compute_flat(flat_grid_10m(), 0.0, square50(), 1.0, 0.001)
    with pytest.raises(engine.AnalysisBoundsError):
        engine.find_balanced_elevation(flat_grid_10m(), square50(), 1.0, 5.0, max_iterations=1000)


def test_malformed_faces_and_boundaries_refuse():
    with pytest.raises(engine.AnalysisInputError):
        engine.analyze_survey_faces([[V(0, 0, 0), V(1, 0, 0)]])
    with pytest.raises(engine.AnalysisInputError):
        engine.analyze_survey_faces([[V(0, 0, 0), V(1, 0, 0), [1, 1], V(0, 1, 0)]])
    with pytest.raises(engine.AnalysisInputError):
        engine.grade_pad(None, [[0, 0], [1, "0"], [1, 1]], 1.0)
    with pytest.raises(engine.AnalysisInputError):
        engine.grade_pad(None, square50(), 1.0, mode="Sideways")
    with pytest.raises(engine.AnalysisInputError):
        engine.compute_flat(flat_grid_10m(), 0.0, square50(), 1.0, 0.0)
