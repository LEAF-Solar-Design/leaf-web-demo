"""Studio's ground terrain engines against the plugin, computed.

Three layers, hermetic, none skipping:

  1. The plugin's OWN unit tests, ported case for case (read 2026-09-23 at
     C:/tmp/solar-parity/wt-b25-s17):
       Tests/Tests/TopoFrom3dFacesTests.cs     both cases
       Tests/Tests/TrackerSlopeValidatorTests.cs all four cases
       Tests/Tests/LeafTerrainProfileTests.cs  every case (SampleLine,
                                               SamplePolyline, Q20 accuracy)
       Tests/Tests/LeafTopoTests.cs            ClassifySlope boundaries
     Omitted from LeafTopoTests, named here rather than left silent: the
     ElevationApiClient cases (USGS/PVGIS HTTP parsing, not these engines) and
     the TerrainMeshBuilder.Build / ComputeSlopePercent cases (a lat/lon
     Haversine mesh these commands never call; DrawGridMesh computes its own
     slopes, ported and tested below). The ResampleToGrid cases from
     Tests/Tests/LandXmlImporterTests.cs are ported as well, because
     LEAFTOPOFROM3DFACES builds its grid with that function.
  2. Hand-computed cases for every rule the port cites: vertex keys, the
     all-coincident guard, banker's rounding of grid dimensions, the neutral
     terrain grid the engines read and commit, mesh vertices, slope buckets and colours, row reading,
     overlays and the clear command.
  3. Bounds and malformed-input refusals.
"""
from __future__ import annotations

import importlib.util
import math
import random
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


engine = _load("solar_ground_terrain", ROOT / "server" / "solar_ground_terrain.py")


def V(x, y, z):
    return (x, y, z)


def face(layer, *verts):
    return {"layer": layer, "vertices": list(verts)}


# ===========================================================================
#  1. Ported plugin tests
# ===========================================================================

# ---- TopoFrom3dFacesTests.cs ----------------------------------------------

def test_build_terrain_points_de_duplicates_shared_vertices_and_scales_elevation():
    faces = [
        [V(0, 0, 10), V(10, 0, 11), V(10, 10, 12), V(0, 10, 13)],
        [V(10, 0, 11), V(20, 0, 14), V(20, 10, 15), V(10, 10, 12)],
    ]
    points = engine.build_terrain_points_from_faces(faces, 0.3048)
    assert len(points) == 6
    [p10_0] = [p for p in points if p[0] == 10 and p[1] == 0]
    [p10_10] = [p for p in points if p[0] == 10 and p[1] == 10]
    assert p10_0[2] == pytest.approx(11 * 0.3048, abs=1e-12)
    assert p10_10[2] == pytest.approx(12 * 0.3048, abs=1e-12)


def test_build_terrain_points_averages_duplicate_xy_elevations():
    faces = [
        [V(0, 0, 10), V(1, 0, 10), V(1, 1, 10), V(0, 1, 10)],
        [V(0, 0, 12), V(2, 0, 10), V(2, 1, 10), V(0, 1, 10)],
    ]
    points = engine.build_terrain_points_from_faces(faces, 1.0)
    [p] = [p for p in points if p[0] == 0 and p[1] == 0]
    assert p[2] == pytest.approx(11.0, abs=1e-12)


# ---- LeafTopoTests.cs: ClassifySlope_BoundaryValues_ReturnCorrectBuckets ---

@pytest.mark.parametrize("slope,bucket", [
    (0.0, "Green"), (4.9, "Green"),
    (5.0, "Yellow"), (10.0, "Yellow"), (15.0, "Yellow"),
    (15.1, "Red"), (50.0, "Red"),
])
def test_classify_slope_boundary_values(slope, bucket):
    assert engine.classify_slope(slope) == bucket


# ---- TrackerSlopeValidatorTests.cs -----------------------------------------

def _grid(elevations):
    return engine.TerrainGridInterpolator(elevations, 2, 2, 0, 10, 0, 10, 1.0)


def _row(index, x):
    return engine.tracker_row(index, (x, 0), (x, 10), module_slots=10, length_meters=10)


def test_validate_rows_flat_terrain_has_no_violations():
    report = engine.validate_rows([_row(1, 0), _row(2, 10)], _grid([5, 5, 5, 5]))
    assert report["axial_violation_rows"] == 0
    assert report["cross_axis_violation_pairs"] == 0
    assert report["row_to_row_angle_violation_pairs"] == 0


def test_validate_axial_slope_above_limit_flags_row_and_recommends_parts():
    report = engine.validate_rows([_row(7, 5)], _grid([0, 0, 2, 2]))
    assert report["axial_violation_rows"] == 1
    assert report["trackers_needing_terrain_following"] == 1
    assert report["row_reports"][0]["recommended_split_fractions"]


def test_validate_cross_axis_slope_above_limit_flags_adjacent_rows():
    preset = {"MaxAxialSlopePct": 8.5, "MaxCrossAxisSlopePct": 10.0, "MaxRowToRowSlopeDeg": 4.0}
    report = engine.validate_rows([_row(1, 0), _row(2, 10)], _grid([0, 2, 0, 2]), preset)
    assert report["axial_violation_rows"] == 0
    assert report["cross_axis_violation_pairs"] == 1
    assert report["row_to_row_angle_violation_pairs"] == 1


def test_validate_row_with_imported_parts_does_not_request_new_terrain_following():
    row = _row(7, 5)
    row["parts_count"] = 2
    report = engine.validate_rows([row], _grid([0, 0, 2, 2]))
    assert report["axial_violation_rows"] == 1
    assert report["trackers_needing_terrain_following"] == 0
    assert report["row_reports"][0]["recommended_split_fractions"][0] == pytest.approx(0.5, abs=1e-9)


# ---- LeafTerrainProfileTests.cs --------------------------------------------

def flat_terrain_10m():
    return engine.TerrainGridInterpolator([10.0] * 4, 2, 2, 0, 100, 0, 100, 1.0)


def test_sample_line_null_terrain_raises():
    with pytest.raises(engine.TerrainInputError):
        engine.sample_line(None, (0, 0), (10, 0), 5)


@pytest.mark.parametrize("count", [0, -1])
def test_sample_line_non_positive_sample_count_raises(count):
    with pytest.raises(engine.TerrainInputError):
        engine.sample_line(flat_terrain_10m(), (0, 0), (10, 0), count)


def test_sample_line_single_sample_returns_one_point_at_distance_zero_and_start_elevation():
    result = engine.sample_line(flat_terrain_10m(), (50, 50), (80, 50), 1)
    assert len(result) == 1
    assert result[0]["distance_m"] == pytest.approx(0.0, abs=1e-9)
    assert result[0]["elevation_m"] == pytest.approx(10.0, abs=0.01)


def test_sample_line_multiple_samples_correct_count():
    assert len(engine.sample_line(flat_terrain_10m(), (0, 50), (100, 50), 11)) == 11


def test_sample_line_multiple_samples_first_distance_is_zero():
    result = engine.sample_line(flat_terrain_10m(), (0, 50), (100, 50), 5)
    assert result[0]["distance_m"] == pytest.approx(0.0, abs=1e-9)


def test_sample_line_multiple_samples_last_distance_equals_total_length():
    result = engine.sample_line(flat_terrain_10m(), (10, 50), (40, 50), 5)
    assert result[-1]["distance_m"] == pytest.approx(30.0, abs=0.001)


def test_sample_line_flat_terrain_all_elevations_equal():
    result = engine.sample_line(flat_terrain_10m(), (10, 10), (90, 90), 10)
    assert all(not math.isnan(p["elevation_m"]) for p in result)
    for p in result:
        assert p["elevation_m"] == pytest.approx(10.0, abs=0.01)


def test_sample_line_point_outside_grid_elevation_is_nan():
    result = engine.sample_line(flat_terrain_10m(), (50, 50), (200, 50), 3)
    assert math.isnan(result[-1]["elevation_m"])


def test_sample_line_distances_are_monotonically_increasing():
    result = engine.sample_line(flat_terrain_10m(), (0, 0), (100, 100), 6)
    for i in range(1, len(result)):
        assert result[i]["distance_m"] > result[i - 1]["distance_m"]


def test_sample_polyline_null_terrain_raises():
    with pytest.raises(engine.TerrainInputError):
        engine.sample_polyline(None, [(0, 0), (10, 0)], 3)


def test_sample_polyline_null_vertices_raises():
    with pytest.raises(engine.TerrainInputError):
        engine.sample_polyline(flat_terrain_10m(), None, 3)


def test_sample_polyline_too_few_vertices_raises():
    with pytest.raises(engine.TerrainInputError):
        engine.sample_polyline(flat_terrain_10m(), [(0, 0)], 3)


def test_sample_polyline_zero_samples_per_segment_raises():
    with pytest.raises(engine.TerrainInputError):
        engine.sample_polyline(flat_terrain_10m(), [(0, 0), (10, 0)], 0)


def test_sample_polyline_two_segments_total_point_count():
    result = engine.sample_polyline(flat_terrain_10m(), [(0, 50), (50, 50), (100, 50)], 5)
    assert len(result) == 11


def test_sample_polyline_two_segments_cumulative_distances_increase():
    result = engine.sample_polyline(flat_terrain_10m(), [(0, 50), (50, 50), (100, 50)], 5)
    for i in range(1, len(result)):
        assert result[i]["distance_m"] > result[i - 1]["distance_m"]


def test_sample_polyline_two_segments_total_cumulative_distance_correct():
    result = engine.sample_polyline(flat_terrain_10m(), [(0, 50), (50, 50), (100, 50)], 5)
    assert result[-1]["distance_m"] == pytest.approx(100.0, abs=0.001)


def test_sample_polyline_single_segment_equivalent_to_sample_line():
    poly = engine.sample_polyline(flat_terrain_10m(), [(10, 10), (90, 10)], 6)
    line = engine.sample_line(flat_terrain_10m(), (10, 10), (90, 10), 7)
    assert len(poly) == len(line) == 7


def _sinusoidal_ridge():
    n, spacing = 11, 10.0
    elevations = [math.sin(2.0 * math.pi * (c * spacing) / 100.0)
                  for r in range(n) for c in range(n)]
    return engine.TerrainGridInterpolator(elevations, n, n, 0, (n - 1) * spacing,
                                          0, (n - 1) * spacing, 1.0)


def _truth(x):
    return math.sin(2.0 * math.pi * x / 100.0)


def test_q20_sinusoidal_profile_error_under_5cm_everywhere():
    profile = engine.sample_line(_sinusoidal_ridge(), (0.0, 50.0), (100.0, 50.0), 201)
    worst = max(abs(p["elevation_m"] - _truth(p["x"])) for p in profile)
    assert worst <= 0.05


def test_q20_sinusoidal_profile_exact_at_grid_nodes():
    for p in engine.sample_line(_sinusoidal_ridge(), (0.0, 50.0), (100.0, 50.0), 11):
        assert p["elevation_m"] == pytest.approx(_truth(p["x"]), abs=1e-12)


def test_q20_sinusoidal_mid_cell_error_matches_closed_form():
    [p] = engine.sample_line(_sinusoidal_ridge(), (5.0, 50.0), (5.0, 50.0), 1)
    expected = math.sin(math.pi * 10.0 / 100.0) - math.sin(2.0 * math.pi * 10.0 / 100.0) / 2.0
    assert _truth(5.0) - p["elevation_m"] == pytest.approx(expected, abs=1e-9)


# ---- LandXmlImporterTests.cs: ResampleToGrid (the grid LEAFTOPOFROM3DFACES stores)

FLAT_SQUARE = [(0.0, 0.0, 100.0), (10.0, 0.0, 100.0), (0.0, 10.0, 100.0), (10.0, 10.0, 100.0)]
SLOPED_SQUARE = [(0.0, 0.0, 100.0), (10.0, 0.0, 102.0), (0.0, 10.0, 104.0), (10.0, 10.0, 106.0)]


def test_resample_null_points_raises():
    with pytest.raises(engine.TerrainInputError):
        engine.resample_to_grid(None, 10)


def test_resample_fewer_than_three_points_raises():
    with pytest.raises(engine.TerrainInputError):
        engine.resample_to_grid([(0, 0, 100), (1, 1, 101)], 10)


def test_resample_target_cells_less_than_two_raises():
    with pytest.raises(engine.TerrainInputError):
        engine.resample_to_grid(FLAT_SQUARE, 1)


def test_resample_flat_terrain_all_elevations_equal():
    grid = engine.resample_to_grid(FLAT_SQUARE, 5)
    assert all(abs(z - 100.0) < 1e-6 for z in grid["elevations"])


def test_resample_drawing_extents_match_point_bounding_box():
    grid = engine.resample_to_grid(SLOPED_SQUARE, 5)
    assert (grid["x_min"], grid["x_max"], grid["y_min"], grid["y_max"]) == (0.0, 10.0, 0.0, 10.0)


def test_resample_source_points_and_array_length():
    grid = engine.resample_to_grid(SLOPED_SQUARE, 6)
    assert grid["source_points"] == 4
    assert len(grid["elevations"]) == grid["rows"] * grid["cols"]


def test_resample_square_domain_rows_and_cols_at_least_two():
    grid = engine.resample_to_grid(FLAT_SQUARE, 4)
    assert grid["rows"] >= 2 and grid["cols"] >= 2


def test_resample_wider_domain_cols_equals_target_cells():
    pts = [(0, 0, 100), (100, 0, 100), (0, 10, 100), (100, 10, 100)]
    grid = engine.resample_to_grid(pts, 20)
    assert grid["cols"] == 20
    assert grid["rows"] < 20


def test_resample_sloped_terrain_bounded_by_source_range():
    grid = engine.resample_to_grid(SLOPED_SQUARE, 10)
    assert min(grid["elevations"]) >= 100.0 - 1e-6
    assert max(grid["elevations"]) <= 106.0 + 1e-6


def test_resample_exact_coincidence_returns_source_elevation():
    grid = engine.resample_to_grid(FLAT_SQUARE, 2)
    assert grid["elevations"] == [100.0, 100.0, 100.0, 100.0]


# ===========================================================================
#  2. Hand-computed rules
# ===========================================================================

def test_vertex_key_merges_within_half_a_micro_unit_only():
    faces = [[V(0, 0, 1), V(4e-7, 0, 3), V(6e-7, 0, 5)]]
    points = engine.build_terrain_points_from_faces(faces, 1.0)
    assert points == [(0, 0, 2.0), (6e-7, 0, 5.0)]


def test_non_finite_vertices_and_none_faces_are_skipped():
    faces = [None, [V(0, 0, 1), V(math.nan, 0, 1), V(1, math.inf, 1), (2, 2, 7), None]]
    assert engine.build_terrain_points_from_faces(faces, 1.0) == [(0, 0, 1.0), (2, 2, 7.0)]


def test_triangle_repeated_corner_counts_twice_in_the_average():
    faces = [[V(0, 0, 0), V(1, 0, 0), V(1, 1, 3), V(1, 1, 3)],
             [V(1, 1, 0), V(2, 1, 0), V(2, 2, 0), V(2, 2, 0)]]
    points = engine.build_terrain_points_from_faces(faces, 1.0)
    [p] = [p for p in points if (p[0], p[1]) == (1, 1)]
    assert p[2] == 2.0


def test_only_first_four_vertices_are_read():
    points = engine.build_terrain_points_from_faces(
        [[V(0, 0, 1), V(1, 0, 1), V(1, 1, 1), V(0, 1, 1), V(9, 9, 9)]], 1.0)
    assert (9, 9, 9.0) not in points and len(points) == 4


def test_points_keep_first_seen_order_and_first_seen_xy():
    points = engine.build_terrain_points_from_faces(
        [[V(5, 5, 1), V(0, 0, 1)], [V(5.0000001, 5, 3)]], 1.0)
    assert points == [(5, 5, 2.0), (0, 0, 1.0)]


def test_idw_center_blend():
    pts = [(0, 0, 0), (10, 0, 0), (0, 10, 0), (10, 10, 4)]
    grid = engine.resample_to_grid(pts, 3)
    assert (grid["rows"], grid["cols"]) == (3, 3)
    assert grid["elevations"][4] == pytest.approx(1.0, abs=1e-12)
    assert grid["elevations"][8] == 4.0


def test_all_coincident_guard_resets_both_maxima_as_the_plugin_does():
    grid = engine.resample_to_grid([(0, 0, 1), (5, 0, 2), (10, 0, 3)], 4)
    # yMin >= yMax trips the guard, which also rewrites xMax to xMin + 1.
    assert (grid["x_min"], grid["x_max"], grid["y_min"], grid["y_max"]) == (0, 1.0, 0, 1.0)
    assert (grid["rows"], grid["cols"]) == (4, 4)


@pytest.mark.parametrize("x_span,y_span,target,expected", [
    (2.0, 1.0, 5, (2, 5)),     # 5 / 2 = 2.5 rounds half to even: 2
    (2.0, 1.0, 7, (4, 7)),     # 3.5 -> 4
    (1.0, 2.0, 5, (5, 2)),     # 5 * 0.5 = 2.5 -> 2
    (1000.0, 1.0, 150, (2, 150)),
])
def test_grid_dimensions_use_bankers_rounding(x_span, y_span, target, expected):
    assert engine.grid_dimensions(x_span, y_span, target) == expected


def gridrec(elevations, rows, cols, x_min, x_max, y_min, y_max, frame=None):
    """The neutral terrain grid the engines read (engine.neutral_grid)."""
    grid = {"elevations": list(elevations), "rows": rows, "cols": cols,
            "x_min": x_min, "x_max": x_max, "y_min": y_min, "y_max": y_max}
    if frame is not None:
        grid["frame"] = list(frame)
    return grid


def test_neutral_grid_is_a_clean_float_copy():
    grid = engine.neutral_grid(gridrec([1, 2, 3, 4], 2, 2, 10, 20, 30, 40))
    assert grid == {"elevations": [1.0, 2.0, 3.0, 4.0], "rows": 2, "cols": 2,
                    "x_min": 10.0, "x_max": 20.0, "y_min": 30.0, "y_max": 40.0}
    assert all(type(v) is float for v in grid["elevations"])
    framed = engine.neutral_grid(gridrec([1.0] * 4, 2, 2, 0, 1, 0, 1, frame=(1, 2, 3, 4, 5, 6)))
    assert framed["frame"] == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    nan_y = engine.neutral_grid(gridrec([1.0] * 4, 2, 2, 0, 1, math.nan, math.nan))
    assert math.isnan(nan_y["y_min"])


def test_mesh_grid_reads_and_refuses_missing_or_thin_grids():
    grid = gridrec([1.0, 2.0, 3.0, 4.0], 2, 2, 10.0, 20.0, 30.0, 40.0)
    assert engine.mesh_grid(grid) == grid
    assert engine.mesh_grid(None) is None
    assert engine.mesh_grid(gridrec([1.0, 2.0], 1, 2, 0, 1, 0, 1)) is None


def test_interpolator_reader_uses_the_affine_frame():
    # Grid rotated 90 degrees: column axis runs along drawing +Y.
    grid = gridrec([0.0, 10.0, 0.0, 10.0], 2, 2, 0, 10, 0, 10,
                   frame=(0.0, 0.0, 0.0, 10.0, -10.0, 0.0))
    terrain = engine.terrain_interpolator(grid, 1.0)
    assert terrain.interpolate_z(0.0, 5.0) == pytest.approx(5.0)
    assert terrain.interpolate_z(5.0, 5.0) is None
    assert engine.terrain_interpolator(None, 1.0) is None
    # The plugin's catch-all: any malformed value reads as "no terrain".
    assert engine.terrain_interpolator(gridrec([0.0] * 4, "x", 2, 0, 1, 0, 1), 1.0) is None
    assert engine.terrain_interpolator(gridrec([0.0] * 3, 2, 2, 0, 1, 0, 1), 1.0) is None


def test_interpolator_degenerate_frame_and_outside_return_none():
    t = engine.TerrainGridInterpolator([1.0] * 4, 2, 2, 0, 0, 0, 10)
    assert t.interpolate_z(0, 5) is None
    t = engine.TerrainGridInterpolator([1.0] * 4, 2, 2, 0, 10, 0, 10, meters_per_unit=-3)
    assert t.meters_per_unit == 1.0
    assert t.interpolate_z(-0.001, 5) is None


def test_draw_grid_mesh_vertices_slopes_and_colours():
    # 2 rows x 3 cols over x 0..20, y 0..10, metres.
    elev = [0.0, 0.4, 3.0,
            1.0, 0.0, 0.0]
    faces = engine.draw_grid_mesh(elev, 2, 3, 0.0, 20.0, 0.0, 10.0, 1.0)
    assert len(faces) == 2
    f0, f1 = faces
    assert f0["vertices"] == [(0.0, 0.0, 0.0), (10.0, 0.0, 0.4), (10.0, 10.0, 0.0), (0.0, 10.0, 1.0)]
    assert f0["layer"] == "LEAF-TOPO" and f0["edges_visible"] == (True, True, True, True)
    assert f0["slope_percent"] == pytest.approx(10.0)       # max(4 %, 10 %)
    assert f0["bucket"] == "Yellow"
    assert f0["color"] == {"method": "ByColor", "rgb": (255, 200, 0), "true_color": 0xFFC800}
    assert f1["slope_percent"] == pytest.approx(26.0)
    assert f1["bucket"] == "Red" and f1["color"]["rgb"] == (220, 0, 0)
    assert (f1["row"], f1["col"]) == (0, 1)


def test_draw_grid_mesh_green_and_feet_units():
    faces = engine.draw_grid_mesh([3.048, 3.048, 3.048, 3.048], 2, 2, 0, 10, 0, 10, 0.3048)
    assert faces[0]["bucket"] == "Green"
    assert faces[0]["color"]["rgb"] == (0, 200, 0)
    for v in faces[0]["vertices"]:
        assert v[2] == pytest.approx(10.0)


def test_draw_grid_mesh_is_row_major_and_empty_below_two_by_two():
    faces = engine.draw_grid_mesh([0.0] * 12, 3, 4, 0, 3, 0, 2, 1.0)
    assert [(f["row"], f["col"]) for f in faces] == [(r, c) for r in range(2) for c in range(3)]
    assert engine.draw_grid_mesh([0.0], 1, 1, 0, 1, 0, 1, 1.0) == []


def test_draw_grid_mesh_zero_width_step_divides_like_csharp():
    faces = engine.draw_grid_mesh([0.0, 1.0, 0.0, 1.0], 2, 2, 5.0, 5.0, 0.0, 10.0, 1.0)
    assert math.isinf(faces[0]["slope_percent"]) and faces[0]["bucket"] == "Red"
    faces = engine.draw_grid_mesh([0.0, 0.0, 0.0, 0.0], 2, 2, 5.0, 5.0, 0.0, 10.0, 1.0)
    assert math.isnan(faces[0]["slope_percent"]) and faces[0]["bucket"] == "Red"


def test_units_keyword_defaults_to_meters():
    assert engine.meters_per_unit_for_keyword() == 1.0
    assert engine.meters_per_unit_for_keyword("Feet") == 0.3048
    assert engine.meters_per_unit_for_keyword("meters") == 1.0
    assert engine.meters_per_unit_for_keyword(None, default_is_feet=True) == 0.3048


def _terrain_faces(layer="LEAF-TERRAIN"):
    return [face(layer, V(0, 0, 0), V(10, 0, 0), V(10, 10, 1), V(0, 10, 1)),
            face(layer, V(10, 0, 0), V(20, 0, 2), V(20, 10, 3), V(10, 10, 1))]


def test_topo_from_3d_faces_prefers_leaf_terrain_case_insensitively():
    faces = [face("OTHER", V(100, 100, 50), V(101, 100, 50), V(101, 101, 50), V(100, 101, 50))]
    faces += _terrain_faces("leaf-terrain")
    existing = [{"layer": "LEAF-TOPO", "kind": "3DFACE"}, {"layer": "leaf-topo", "kind": "LINE"},
                {"layer": "LEAF-TOPO", "erased": True}, {"layer": "0"}]
    res = engine.topo_from_3d_faces(faces, 1.0, 4, existing_entities=existing)
    assert res["succeeded"]
    assert (res["all_faces"], res["source_faces"], res["unique_vertices"]) == (3, 2, 6)
    assert res["source_label"] == "LEAF-TERRAIN"
    assert (res["rows"], res["cols"]) == (2, 4)            # 20 x 10: 4 / 2 = 2 rows
    assert res["cleared_entity_indices"] == [0, 1]
    assert res["mesh_faces_drawn"] == 3 == len(res["mesh"])
    assert sorted(res["grid"]) == ["cols", "elevations", "rows", "x_max", "x_min", "y_max", "y_min"]
    assert (res["grid"]["rows"], res["grid"]["cols"], len(res["grid"]["elevations"])) == (2, 4, 8)
    assert [res["grid"][k] for k in ("x_min", "x_max", "y_min", "y_max")] == [0.0, 20.0, 0.0, 10.0]
    assert res["message"] == (
        "LEAFTOPOFROM3DFACES complete: persisted LEAFTOPO 2x4 grid from 6 unique "
        "vertex/vertices and 2 face(s) on LEAF-TERRAIN; replaced 2 old LEAF-TOPO face(s), "
        "drew 3 new face(s).")
    assert engine.outcome_counts(res) == {
        "all_faces": 3, "source_faces": 2, "unique_vertices": 6, "rows": 2, "cols": 4,
        "old_mesh_faces_cleared": 2, "mesh_faces_drawn": 3}


def test_topo_from_3d_faces_falls_back_to_every_layer():
    res = engine.topo_from_3d_faces(_terrain_faces("C-TOPO"), 0.3048)
    assert res["source_label"] == "all modelspace 3DFACE layers"
    assert (res["rows"], res["cols"]) == (75, 150)
    # Z is stored in metres: the corner at (0, 0, 0 ft) is 0 m, (20, 10, 3 ft) is 0.9144 m.
    assert res["grid"]["elevations"][0] == 0.0
    assert res["grid"]["elevations"][-1] == pytest.approx(3 * 0.3048)
    # The mesh converts back to drawing units.
    assert res["mesh"][-1]["vertices"][2][2] == pytest.approx(3.0)


def test_topo_from_3d_faces_refusal_messages():
    res = engine.topo_from_3d_faces([], 1.0)
    assert not res["succeeded"] and res["grid"] is None
    assert res["message"] == "LEAFTOPOFROM3DFACES: no modelspace 3DFACE entities found."
    res = engine.topo_from_3d_faces([face("X", V(0, 0, 0), V(0, 0, 1), V(1, 1, 1), V(1, 1, 1))], 1.0)
    assert res["message"] == ("LEAFTOPOFROM3DFACES: only 2 unique terrain vertices found; "
                              "need at least 3.")
    assert res["mesh"] is None


@pytest.mark.parametrize("asked,cells", [(1, 2), (-5, 2), (999, 300)])
def test_topo_from_3d_faces_clamps_target_cells(asked, cells):
    res = engine.topo_from_3d_faces(_terrain_faces(), 1.0, asked)
    assert res["cols"] == cells


def test_audit_counts():
    faces = _terrain_faces() + [face("OTHER", V(0, 0, 0))]
    assert engine.audit_faces(faces) == {"all_faces": 3, "preferred_terrain_faces": 2,
                                         "source_faces": 2, "source_label": "LEAF-TERRAIN"}
    assert engine.audit_faces([face("A", V(0, 0, 0))])["source_label"] == \
        "all modelspace 3DFACE layers"


def test_terrain_mesh_rerender():
    grid = gridrec([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], 2, 3, 0, 20, 0, 10)
    res = engine.terrain_mesh_rerender(grid, 1.0, [{"layer": "LEAF-TOPO"}])
    assert res["succeeded"] and len(res["mesh"]) == 2
    assert res["message"] == ("LEAFTERRAINMESH complete - replaced 1 old face(s), 2 faces drawn "
                              "on LEAF-TOPO from 2x3 elevation grid.")
    missing = engine.terrain_mesh_rerender(None, 1.0)
    assert not missing["succeeded"]
    assert missing["message"].startswith("LEAFTERRAINMESH: no LEAFTOPO grid found in this drawing.")


def test_axial_violation_numbers():
    rep = engine.validate_row(_row(3, 5), _grid([0, 0, 2, 2]))
    assert rep["axial_segments_checked"] == 10
    v = rep["axial_violations"][0]
    assert v["type"] == "Axial" and v["other_row_index"] == -1
    assert v["slope_percent"] == pytest.approx(20.0)
    assert v["slope_degrees"] == pytest.approx(math.degrees(math.atan(0.2)))
    assert v["limit"] == 8.5
    assert (v["x0"], v["y0"], v["x1"], v["y1"]) == (5, 0, 5, 1)
    assert rep["recommended_split_fractions"] == pytest.approx([0.1, 0.2, 0.3])


def test_axial_segment_count_defaults_and_caps():
    t = _grid([0, 0, 0, 0])
    assert engine.validate_row(engine.tracker_row(0, (5, 0), (5, 10)), t)["axial_segments_checked"] == 16
    assert engine.validate_row(engine.tracker_row(0, (5, 0), (5, 10), 200), t)["axial_segments_checked"] == 64


def test_status_line_within_budget_and_exceeding():
    flat = engine.validate_rows([_row(1, 0), _row(2, 10)], _grid([5, 5, 5, 5]))
    assert flat["status"] == "Tracker slope: 0/2 axial / 0/1 cross - within ASCE 7-16 budget."
    steep = engine.validate_rows([_row(1, 0), _row(2, 10)], _grid([0, 2, 0, 2]))
    assert steep["status"] == ("Tracker slope: 0/2 axial / 1/1 cross / 1/1 row-to-row deg "
                               "exceed active preset limits.")


def test_from_packed_frame_and_polyline_rows():
    verts = [(0, 0), (2, 0), (2, 10), (0, 10)]
    row = engine.from_packed_frame(verts, 4, {"Columns": 28}, 0.3048)
    assert row["axis_start"] == (1.0, 0.0) and row["axis_end"] == (1.0, 10.0)
    assert row["module_slots"] == 28
    assert row["length_meters"] == pytest.approx(3.048)
    assert engine.from_packed_frame(verts, 0, None, -1)["length_meters"] == 10.0
    prow = engine.polyline_to_tracker_row(verts, 9, 12, 1.0)
    assert (prow["row_index"], prow["module_slots"]) == (9, 12)


def _tracker_poly(x, layer="LEAF-TRACKERS", **identity):
    """A LEAF-TRACKERS polyline with its neutral identity (tracker_row or frame_cell)."""
    return dict({"kind": "LWPOLYLINE", "layer": layer,
                 "vertices": [(x - 1, 0), (x + 1, 0), (x + 1, 10), (x - 1, 10)]}, **identity)


def test_read_rows_for_slope_order_and_renumbering():
    ents = [
        _tracker_poly(0, tracker_row={"row_index": 7, "module_slots": 10},
                      frame_cell={"row": 1, "col": 1}),              # tracker row wins
        _tracker_poly(3, frame_cell={"row": 5, "col": 6}),
        _tracker_poly(6),                                            # no identity: skipped
        _tracker_poly(9, layer="OTHER", tracker_row={}),             # wrong layer
        {"kind": "LWPOLYLINE", "layer": "LEAF-TRACKERS", "vertices": [(0, 0), (1, 1)],
         "tracker_row": {}},                                         # < 4 vertices
        {"kind": "PVCASE_TRACKER", "row": {"row_index": 99, "axis_start": (12, 0),
                                           "axis_end": (12, 10), "module_slots": 4}},
        {"kind": "CIRCLE", "layer": "LEAF-TRACKERS"},
    ]
    rows = engine.read_rows_for_slope(ents, {"Columns": 30}, 1.0)
    assert [r["row_index"] for r in rows] == [0, 1, 2]
    assert [r["axis_start"][0] for r in rows] == [0.0, 3.0, 12.0]
    assert [r["module_slots"] for r in rows] == [10, 30, 4]


def test_tracker_slope_violations_end_to_end_and_overlays():
    grid = gridrec([0.0, 2.0, 0.0, 2.0], 2, 2, 0, 10, 0, 10)
    trow = {"row_index": 0, "module_slots": 10}
    ents = [_tracker_poly(1, tracker_row=trow),
            _tracker_poly(9, tracker_row=trow),
            {"layer": "LEAF-SLOPE-VIOLATIONS", "kind": "LWPOLYLINE"},
            {"layer": "leaf-slope-violations", "kind": "LINE"}]
    res = engine.tracker_slope_violations(grid, ents, None, 1.0)
    assert res["succeeded"]
    assert res["cleared_entity_indices"] == [2, 3]
    overlays = res["overlays"]
    # Cross-axis pair: row 1 (x=9, lower cross projection) looks up to row 0.
    assert [o["row_index"] for o in overlays] == [0, 1]
    assert overlays[0] == {"kind": "LWPOLYLINE", "layer": "LEAF-SLOPE-VIOLATIONS",
                           "color_index": 256, "vertices": [(1.0, 0.0), (1.0, 10.0)],
                           "bulges": [0.0, 0.0], "constant_width": 0.25, "row_index": 0}
    assert res["message"] == (
        "LEAFTRACKERSLOPEVIOLATIONS: Tracker slope: 0/2 axial / 1/1 cross / 1/1 row-to-row "
        "deg exceed active preset limits. Drew 2 red overlay(s) on LEAF-SLOPE-VIOLATIONS.")


def test_overlays_axial_first_then_pairs_once_each():
    t = engine.TerrainGridInterpolator([0, 0, 0, 10, 10, 10], 2, 3, 0, 20, 0, 10)
    rows = [_row(0, 20), _row(1, 10), _row(2, 0)]
    report = engine.validate_rows(rows, t)
    assert all(r["has_axial_violations"] for r in report["row_reports"])
    assert [o["row_index"] for o in engine.violation_overlays(report)] == [0, 1, 2]
    assert engine.violation_overlays(None) == []


def test_tracker_slope_violations_refusals():
    res = engine.tracker_slope_violations(None, [], None, 1.0)
    assert res["message"] == ("LEAFTRACKERSLOPEVIOLATIONS: Tracker slope: no LEAFTOPO terrain "
                              "found; validation skipped.")
    grid = gridrec([0.0] * 4, 2, 2, 0, 10, 0, 10)
    res = engine.tracker_slope_violations(grid, [{"kind": "LINE", "layer": "0"}], None, 1.0)
    assert res["message"] == "LEAFTRACKERSLOPEVIOLATIONS: Tracker slope: no tracker rows found."
    assert res["cleared_entity_indices"] == [] and res["overlays"] == []


def test_clear_command():
    ents = [{"layer": "0"}, {"layer": "LEAF-SLOPE-VIOLATIONS"}, {"layer": "Leaf-Slope-Violations"}]
    res = engine.clear_tracker_slope_violations(ents)
    assert res == {"cleared_entity_indices": [1, 2],
                   "message": "LEAFCLEARTRACKERSLOPEVIOLATIONS: removed 2 overlay(s)."}


def _plugin_pairs(projections):
    """BuildAdjacentCrossAxisPairs, TrackerSlopeValidator.cs:344-377, literally."""
    result, seen = [], set()
    for i, a in enumerate(projections):
        best, best_delta = None, 1.7976931348623157e308
        for j, b in enumerate(projections):
            if i == j:
                continue
            delta = b["cross"] - a["cross"]
            if delta <= 1e-9 or delta >= best_delta:
                continue
            overlap = min(a["amax"], b["amax"]) - max(a["amin"], b["amin"])
            if overlap <= 1e-9 or overlap < min(a["span"], b["span"]) * 0.25:
                continue
            best, best_delta = b, delta
        if best is None:
            continue
        key = tuple(sorted((a["row"]["row_index"], best["row"]["row_index"])))
        if key not in seen:
            seen.add(key)
            result.append((a["row"]["row_index"], best["row"]["row_index"]))
    return result


def _engine_pairs(rows):
    projections = engine._row_projections(rows)
    return [(a["row"]["row_index"], b["row"]["row_index"])
            for a, b in engine.adjacent_cross_axis_pairs(projections)], projections


@pytest.mark.parametrize("seed", range(12))
def test_adjacent_pair_scan_matches_the_plugins_all_pairs_scan(seed):
    rng = random.Random(seed)
    rows = []
    for i in range(rng.randint(2, 40)):
        x = rng.choice([0, 5, 5, 10, 15, 20, 25]) + rng.choice([0.0, 0.0, 0.5])
        y0 = rng.choice([0, 0, 3, 20, 40])
        length = rng.choice([10, 10, 20, 2])
        tilt = rng.choice([0.0, 0.0, 0.3])
        if rng.random() < 0.1:
            rows.append(engine.tracker_row(i, (x, y0), (x, y0)))           # degenerate
        elif rng.random() < 0.2:
            rows.append(engine.tracker_row(i, (x, y0 + length), (x + tilt, y0)))  # reversed
        else:
            rows.append(engine.tracker_row(i, (x, y0), (x + tilt, y0 + length)))
    got, projections = _engine_pairs(rows)
    assert got == _plugin_pairs(projections)


def test_adjacent_pair_scan_breaks_rounded_delta_ties_by_lowest_index():
    rows = [engine.tracker_row(0, (3.0, 0), (3.0, 10)),
            engine.tracker_row(1, (-2e-17, 0), (-2e-17, 10)),
            engine.tracker_row(2, (-1e-17, 0), (-1e-17, 10))]
    got, projections = _engine_pairs(rows)
    assert got == _plugin_pairs(projections) == [(0, 1)]


# ===========================================================================
#  3. Bounds and refusals
# ===========================================================================

@pytest.mark.parametrize("faces", [
    "nope",
    [["not a dict"]],
    [{"layer": 5, "vertices": [(0, 0, 0)]}],
    [{"layer": "A", "vertices": []}],
    [{"layer": "A", "vertices": [(0, 0)]}],
    [{"layer": "A", "vertices": [(0, 0, "1")]}],
    [{"layer": "A", "vertices": [(0, True, 1)]}],
    [{"layer": "A", "vertices": [(0, 0, 0)] * 5}],
])
def test_malformed_faces_are_refused(faces):
    with pytest.raises(engine.TerrainInputError):
        engine.topo_from_3d_faces(faces, 1.0)


@pytest.mark.parametrize("mpu", [0, -1.0, math.nan, math.inf, "1", None, True])
def test_bad_meters_per_unit_is_refused(mpu):
    with pytest.raises(engine.TerrainInputError):
        engine.topo_from_3d_faces(_terrain_faces(), mpu)
    with pytest.raises(engine.TerrainInputError):
        engine.build_terrain_points_from_faces([[V(0, 0, 0)]], mpu)


def test_face_count_bound(monkeypatch):
    monkeypatch.setattr(engine, "MAX_FACES", 1)
    with pytest.raises(engine.TerrainBoundsError):
        engine.topo_from_3d_faces(_terrain_faces(), 1.0)
    with pytest.raises(engine.TerrainBoundsError):
        engine.build_terrain_points_from_faces([[V(0, 0, 0)], [V(1, 1, 1)]], 1.0)


def test_idw_work_bound_refuses_before_computing():
    with pytest.raises(engine.TerrainBoundsError, match="IDW work"):
        engine.topo_from_3d_faces(_terrain_faces(), 1.0, 150, max_idw_operations=1000)


def test_grid_node_bound(monkeypatch):
    monkeypatch.setattr(engine, "MAX_GRID_NODES", 10)
    with pytest.raises(engine.TerrainBoundsError):
        engine.resample_to_grid(FLAT_SQUARE, 5)
    grid = gridrec([0.0] * 16, 4, 4, 0, 1, 0, 1)
    with pytest.raises(engine.TerrainBoundsError):
        engine.neutral_grid(grid)
    with pytest.raises(engine.TerrainBoundsError):
        engine.mesh_grid(grid)
    assert engine.terrain_interpolator(grid, 1.0) is None


def test_malformed_grid_inputs_are_refused():
    with pytest.raises(engine.TerrainInputError):
        engine.draw_grid_mesh([0.0, 0.0, 0.0], 2, 2, 0, 1, 0, 1, 1.0)
    with pytest.raises(engine.TerrainInputError):
        engine.draw_grid_mesh([0.0] * 4, 2.0, 2, 0, 1, 0, 1, 1.0)
    with pytest.raises(engine.TerrainInputError):
        engine.mesh_grid(gridrec([0.0] * 4, "2", 2, 0, 1, 0, 1))
    with pytest.raises(engine.TerrainBoundsError):
        engine.mesh_grid(gridrec([], -2, -2, 0, 1, 0, 1))
    with pytest.raises(engine.TerrainInputError):
        engine.neutral_grid(gridrec([0.0] * 4, 2, 2, 0, 1, 0, 1, frame=(1, 2, 3)))
    with pytest.raises(engine.TerrainInputError):
        engine.neutral_grid(gridrec([0.0] * 4, 2, 2, 0, "1", 0, 1))
    with pytest.raises(engine.TerrainInputError):
        engine.neutral_grid([0.0] * 4)
    with pytest.raises(engine.TerrainInputError):
        engine.read_rows_for_slope([_tracker_poly(0, tracker_row={"row_index": "7"})], None, 1.0)
    with pytest.raises(engine.TerrainInputError):
        engine.TerrainGridInterpolator([0.0] * 4, 2, 2, 0, 1, 0, 1, 1.0, 0.0, 0.0)
    with pytest.raises(engine.TerrainInputError):
        engine.resample_to_grid([(0, 0, 1), (1, 0, 1), (0, 1, math.nan)], 4)
    with pytest.raises(engine.TerrainInputError):
        engine.resample_to_grid(FLAT_SQUARE, 4.0)


def test_units_keyword_refuses_unknown():
    with pytest.raises(engine.TerrainInputError):
        engine.meters_per_unit_for_keyword("Inches")


def test_tracker_inputs_are_refused():
    t = _grid([0, 0, 0, 0])
    with pytest.raises(engine.TerrainInputError):
        engine.validate_rows(None, t)
    with pytest.raises(engine.TerrainInputError):
        engine.validate_rows([_row(0, 5)], None)
    with pytest.raises(engine.TerrainInputError):
        engine.validate_rows([{"row_index": 0, "axis_start": (0, math.nan), "axis_end": (0, 1)}], t)
    with pytest.raises(engine.TerrainInputError):
        engine.validate_rows([_row(0, 5)], t, {"MaxAxialSlopePct": "8"})
    with pytest.raises(engine.TerrainInputError):
        engine.from_packed_frame([(0, 0), (1, 0), (1, 1)], 0, None, 1.0)
    with pytest.raises(engine.TerrainInputError):
        engine.read_rows_for_slope([{"kind": "PVCASE_TRACKER", "row": None}], None, 1.0)
    with pytest.raises(engine.TerrainInputError):
        engine.clear_tracker_slope_violations([{"layer": None}])


def test_tracker_row_bound(monkeypatch):
    monkeypatch.setattr(engine, "MAX_TRACKER_ROWS", 2)
    with pytest.raises(engine.TerrainBoundsError):
        engine.validate_rows([_row(i, i) for i in range(3)], _grid([0, 0, 0, 0]))
    trow = {"row_index": 0, "module_slots": 1}
    with pytest.raises(engine.TerrainBoundsError):
        engine.read_rows_for_slope([_tracker_poly(i, tracker_row=trow) for i in range(3)], None, 1.0)


def test_entity_and_profile_bounds(monkeypatch):
    monkeypatch.setattr(engine, "MAX_ENTITIES", 1)
    with pytest.raises(engine.TerrainBoundsError):
        engine.clear_tracker_slope_violations([{"layer": "0"}, {"layer": "0"}])
    monkeypatch.setattr(engine, "MAX_PROFILE_SAMPLES", 3)
    with pytest.raises(engine.TerrainBoundsError):
        engine.sample_line(flat_terrain_10m(), (0, 0), (1, 1), 4)
    with pytest.raises(engine.TerrainBoundsError):
        engine.sample_polyline(flat_terrain_10m(), [(0, 0), (1, 1), (2, 2)], 2)
