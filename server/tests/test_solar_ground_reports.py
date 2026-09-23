"""Studio's ground report engines against the plugin, computed (contract G23).

Covered: the civil layer classifier (the plugin's CivilLayersTests fence cases, neutral xref
prefixes), the source-Z rule, LEAFFENCE3DAUDIT's counts and status line, LEAFFENCEMESHFROMCIVIL's
wipe, drape, skipped segments and by-value closed flag, the woodland layers, the height-label
parser, the vegetation import (containment, smallest region, orphan snap, skipped polygons,
restriction outlines, the mesh face count) and its status record and message, the mesh diff's
point sources, terrain filter, datum shift and topo wire counts, the seeded .NET generator, the
sun position references, the shade fraction, the collinear shade path against the literal pair
loop, the plugin's OptimalRowSpacingCommandTests and LeafOptimalRowSpacingTests cases, and the
licensed b3, b4, b12, b13 and b14 outputs: every value is COMPUTED from the committed terrain
intake and Studio's own module settings (G13), and must equal what the plugin printed.
"""
from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


reports = _load("solar_ground_reports", ROOT / "server" / "solar_ground_reports.py")
layout = reports._layout
INTAKE = ROOT / "docs" / "parity" / "evidence" / "ground" / "terrain" / "intake.json"


def plane(x, y):
    """Terrain over [0, 100] x [0, 100] at 10 m, None outside (InterpolateZ's rule)."""
    return 10.0 if 0.0 <= x <= 100.0 and 0.0 <= y <= 100.0 else None


def poly(layer, vertices, closed=False, elevation=0.0, kind="polyline"):
    return {"type": kind, "layer": layer, "vertices": vertices, "closed": closed, "elevation": elevation}


# ------------------------------------------------------------ civil layers --

@pytest.mark.parametrize("layer", ["LEAF-PVCASE-SHADING-FENCE", "LEAF-PVCASE-SHADING-FENCE-1", "V-SITE-FENC-LINE",
                                   "V-SITE-FENCE", "PVcase Fence", "C-FENCE", "C-FENC-LINE", "XREF-BASE|Fence",
                                   "XREF-BASE|V-SITE-FENC-LINE", "SITE-FENCELINE", "PERIMETER-FENCE", "CLF",
                                   "XREF-BASE|C-SITE-FENCES-LINE", "SECURITY-FENCING", "CHAIN-LINK"])
def test_fence_layers_recognized(layer):
    assert reports.is_fence_layer(layer)


@pytest.mark.parametrize("layer", [None, "", "0", "DEFENCE", "APX-BNDY-SBCK-LINE-FENCE",
                                   "XREF-BASE|APX-BNDY-SBCK-LINE-FENCE", "C-ROAD-FENCE-OFFSET", "PVcase Road",
                                   "LEAF-PVCASE-SHADING-FENCE-MESH", "LEAF-TRACKERS", "LEAF-BOUNDARY"])
def test_fence_layers_rejected(layer):
    assert not reports.is_fence_layer(layer)


@pytest.mark.parametrize("layer", ["APX-BNDY-SBCK-LINE-FENCE", "XREF-BASE|APX-BNDY-SBCK-LINE-FENCE",
                                   "XREF-BASE$0$APX-BNDY-SBCK-LINE-FENCE", "XREF-BASE|APX-BNDY-SBCK-LINE-ARRAY",
                                   "XREF-BASE|APX-BNDY-SBCK-LINE-COLLECTION", "PROPSETBACK"])
def test_setback_layers(layer):
    assert reports.is_setback_layer(layer)


def test_woodland_layers():
    assert reports.is_woodland_boundary_layer("V-VEGE-WDLN")
    assert reports.is_woodland_boundary_layer("xref|c-vege-wdln-edge")
    assert not reports.is_woodland_boundary_layer("V-VEGE-WDLN-TXT")
    assert not reports.is_woodland_boundary_layer("V-VEGE-WDLN-LIMIT")
    assert reports.is_woodland_height_layer("V-VEGE-WDLN-TEXT")
    assert not reports.is_woodland_height_layer("V-VEGE-WDLN")
    assert not reports.is_woodland_height_layer("LEAF-TRACKERS")


def test_source_z_rule():
    flat = reports.linear_vertices_3d(poly("C-FENCE", [[0, 0], [1, 0]], elevation=1e-7))
    raised = reports.linear_vertices_3d(poly("C-FENCE", [[0, 0], [1, 0]], elevation=0.5))
    three = reports.linear_vertices_3d({"type": "line", "layer": "C-FENCE", "vertices": [[0, 0, 0], [1, 0, 2]]})
    assert flat[2] is False and raised[2] is True and three[2] is True
    assert reports.linear_vertices_3d({"type": "text", "layer": "C-FENCE", "text": "x"}) is None
    assert reports.linear_vertices_3d(poly("C-FENCE", [[0, 0]])) is None
    assert reports.linear_vertices_3d(poly("C-FENCE", [[0, 0], [1, 1]]), require_closed=True) is None


# ------------------------------------------------------------------ fences --

def test_fence_audit_counts():
    ents = [poly("C-FENCE", [[1, 1], [5, 1], [5, 5]]),                       # flat, drapeable
            poly("V-SITE-FENC-LINE", [[1, 1], [2, 2]], elevation=3.0),        # source 3D
            poly("APX-BNDY-SBCK-LINE-FENCE", [[0, 0], [9, 9]]),               # a setback, not a fence
            {"type": "face", "layer": "LEAF-PVCASE-SHADING-FENCE-MESH"},
            {"type": "face", "layer": "leaf-pvcase-shading-fence-mesh"}]
    audit = reports.fence_audit(ents, plane, 1.0)
    assert (audit["source_fences"], audit["source_3d"], audit["flat"], audit["drapable_flat"],
            audit["mesh_faces"]) == (2, 1, 1, 1, 2)
    no_topo = reports.fence_audit(ents, None, 1.0)
    assert no_topo["drapable_flat"] == 0 and not no_topo["has_topo"]
    assert "no LEAFTOPO for flat fence drape" in reports.fence_audit_status_line(no_topo)


def test_fence_mesh_build():
    ents = [poly("C-FENCE", [[1, 1], [5, 1], [5, 5], [1, 1]]),     # repeated first vertex, not closed
            poly("C-FENCE", [[90, 50], [150, 50]]),                  # second vertex off the terrain
            poly("PVcase Fence", [[0, 0], [10, 0]], closed=True, elevation=2.0),
            {"type": "face", "layer": "LEAF-PVCASE-SHADING-FENCE-MESH"}]
    out = reports.fence_mesh_build(ents, plane, 1.0, 2.0, 0.2)
    assert out["erased"] == 1
    assert (out["source_fences"], out["source_3d"], out["flat"], out["drapable_flat"]) == (3, 1, 2, 2)
    # Run 1 keeps its own closed flag (False): two segments of three normalized vertices.
    # Run 2 skips its one segment (NaN Z). Run 3 is closed over two vertices: two segments.
    assert out["runs"] == 3 and out["segments"] == 4 and out["skipped_segments"] == 1 and out["mesh_faces"] == 4
    a, b, top_b, top_a = out["faces"][0]
    assert a == (1.0, 1.0, 10.0) and top_b == (5.0, 1.0, 12.0) and top_a == (1.0, 1.0, 12.0)
    assert "; skipped 1 segment(s)." in reports.fence_mesh_message(out)
    assert reports.fence_mesh_build(ents, plane, 1.0, 0.0)["erased"] == 0     # cs:196 returns first


# -------------------------------------------------------------- vegetation --

@pytest.mark.parametrize("text,feet", [("35'", 35.0), ("H=40.5 '", 40.5), ("TREE 12' TALL 20'", 12.0),
                                       ("0'", None), ("no height", None), ("", None), (None, None),
                                       ("٣٥'", None)])
def test_parse_height_feet(text, feet):
    assert reports.parse_height_feet(text) == feet


def label(x, y, text="30'", layer="V-VEGE-WDLN-TXT"):
    return {"type": "text", "layer": layer, "text": text, "position": [x, y]}


def test_vegetation_import():
    square = poly("V-VEGE-WDLN", [[0, 0], [14, 0], [14, 14], [0, 14]], closed=True)
    inner = poly("V-VEGE-WDLN", [[2, 2], [4, 2], [4, 4], [2, 4]], closed=True)
    far = poly("C-VEGE-WDLN", [[60, 60], [70, 60], [70, 70], [60, 70]], closed=True)
    lonely = poly("V-VEGE-WDLN", [[30, 80], [31, 80], [31, 81], [30, 81]], closed=True)
    ents = [square, inner, far, lonely, label(3, 3, "20'"), label(7, 7, "40'"), label(75, 65, "10'"),
            label(500, 500, "5'")]
    out = reports.vegetation_import(ents, plane, 1.0, create_restrictions=True)
    assert out["source_regions"] == 4 and out["height_labels"] == 4
    # (3, 3) goes to the smaller inner square, (7, 7) to the big one, (75, 65) snaps 5 m to
    # the far square (inside 75 ft), (500, 500) is beyond every tolerance.
    assert out["snapped_labels"] == 1 and out["imported_regions"] == 3 and out["skipped_regions"] == 1
    assert out["restriction_regions"] == 3 and len(out["restrictions"]) == 3
    assert [m["height_m"] for m in out["masses"]] == [40 * 0.3048, 20 * 0.3048, 10 * 0.3048]
    assert out["warnings"] == []
    none = reports.vegetation_import(ents, plane, 1.0, create_restrictions=False)
    assert none["restriction_regions"] == 0 and none["restrictions"] == []


def test_vegetation_mesh_face_count():
    # A 14 m square, one label: a 15 x 15 grid at 1 m; nodes with x < 14 and y < 14 are
    # inside (the even-odd rule counts the left and bottom edges in, the right and top out),
    # so 13 x 13 top cells, plus four side faces.
    sq = [(0.0, 0.0), (14.0, 0.0), (14.0, 14.0), (0.0, 14.0)]
    assert reports.vegetation_mesh_face_count(sq, 5.0, plane, 1.0, 1) == 4 + 169
    assert reports.vegetation_mesh_face_count(sq, 5.0, None, 1.0, 1) == 0
    assert reports.vegetation_mesh_face_count([(200, 200), (201, 200), (201, 201)], 5.0, plane, 1.0, 1) == 0


def test_vegetation_empty_site_record_and_message():
    out = reports.vegetation_import([poly("LEAF-TRACKERS", [[0, 0], [1, 0], [1, 1]], closed=True)], plane, 1.0)
    assert reports.vegetation_status_record(out) == {
        "schema": 1, "source_regions": 0, "height_labels": 0, "imported_regions": 0, "mesh_faces": 0,
        "restriction_regions": 0, "skipped_regions": 0, "snapped_labels": 0, "meters_per_unit": 1.0}
    assert reports.vegetation_message(out) == [
        "LEAFVEGETATIONFROMCIVIL: imported 0 vegetation mass region(s) from 0 civil woodland polygon(s) and "
        "0 height label(s); wrote 0 mesh face(s). Drawing scale=1 m/unit.",
        "  No civil woodland boundary polylines found.",
        "  No civil woodland height labels found."]


# --------------------------------------------------------------- mesh diff --

def test_mesh_diff_sources_and_filter():
    frames = [poly("LEAF-TRACKERS", [[1, 1], [3, 1], [3, 5], [1, 5]], closed=True, elevation=11.5),
              poly("leaf-trackers", [[10, 10], [12, 10], [12, 14], [10, 14]], closed=True),
              poly("LEAF-TRACKERS", [[200, 1], [202, 1], [202, 5]], closed=True),        # off the terrain
              poly("LEAF-TRACKERS", [[5, 5], [6, 5], [6, 6]], closed=False),             # open: not a point
              {"type": "block", "layer": "PVcase PV Modules (full frames)", "columns": 0}]
    out = reports.mesh_diff(frames, plane, 150, 90, 1.0)
    assert out["source"] == "tracker" and out["panel_points"] == 2 and out["raw_candidates"] == 3
    assert out["skipped_outside_topo"] == 1 and out["scanned_entities"] == 5
    assert out["third_party_block_references"] == 1 and out["third_party_blocks_without_metadata"] == 1
    assert out["median_diff_m"] == pytest.approx(0.75) and not out["datum_shift_applied"]
    # SampleIndices(150, 72): every 3rd of 150 plus the last = 51; (90, 72): every 2nd
    # (0 to 88) plus the last = 46.
    assert reports.sample_indices(90, 72)[-2:] == [88, 89]
    assert out["topo_points"] == 51 * 46 and out["topo_lines"] == 51 * 45 + 50 * 46
    msg, scan = reports.mesh_diff_message(out)
    assert msg == "LEAFMESHDIFFVIEW: showing 2 sampled panel/design point(s) from LEAF-TRACKERS polylines against LEAFTOPO"
    assert scan.startswith("Scanned 5 modelspace entity/entities; 1 PVcase block reference(s), 1 skipped")
    panel = frames + [poly("PANEL", [[20, 20], [21, 20], [21, 21]], closed=True)]
    assert reports.mesh_diff(panel, plane, 2, 2, 1.0)["source"] == "module"
    assert reports.mesh_diff(frames, None, 2, 2, 1.0)["status"] == "no-terrain"
    with pytest.raises(reports.ReportInputError):
        reports.mesh_diff([{"type": "block", "layer": "PVcase PV Modules (full frames)", "columns": 3}],
                          plane, 2, 2, 1.0)


def test_mesh_diff_datum_shift():
    high = [poly("LEAF-TRACKERS", [[1, 1], [3, 1], [3, 5], [1, 5]], closed=True, elevation=50.0)]
    out = reports.mesh_diff(high, plane, 2, 2, 1.0)
    assert out["datum_shift_applied"] and out["raw_median_clearance_m"] == 40.0
    assert out["vertical_datum_shift_m"] == -40.0 and out["median_diff_m"] == 0.0


def test_net_random_is_deterministic_and_bounded():
    a, b = reports.NetRandom(1107), reports.NetRandom(1107)
    seq = [a.next(6001) for _ in range(500)]
    assert seq == [b.next(6001) for _ in range(500)]
    assert all(0 <= v < 6001 for v in seq) and len(set(seq)) > 400
    assert [reports.NetRandom(7).next(1000) for _ in range(3)] != seq[:3]


# ---------------------------------------------------------- sun and shade --

def test_sun_position_reference_accuracy():
    """SunPositionCalculator's documented reference: solar noon at 40 N reaches about 73 deg
    at the summer solstice and 26.5 deg at the winter one."""
    summer = max(reports.sun_position(40.0, 0.0, 172, h / 4.0)[1] for h in range(40, 56))
    winter = max(reports.sun_position(40.0, 0.0, 355, h / 4.0)[1] for h in range(40, 56))
    assert abs(summer - 73.0) < 1.0 and abs(winter - 26.5) < 1.0
    az, elev, night = reports.sun_position(40.0, 0.0, 172, 0.0)
    assert night and az == 0.0 and elev <= 0.0


def test_shade_fraction():
    sun = (90.0, 45.0, False)                       # sun due east: shadows fall west
    assert reports.shade_fraction((10.0, 0.0), (5.0, 0.0), sun, 1.0) == pytest.approx(0.2)
    assert reports.shade_fraction((5.0, 0.0), (10.0, 0.0), sun, 1.0) == 0.0
    assert reports.shade_fraction((5.0, 0.0), (5.0, 0.0), sun, 1.0) == 0.0
    assert reports.shade_fraction((10.0, 0.0), (9.9, 0.0), sun, 1.0) == 1.0
    assert reports.shade_fraction((10.0, 0.0), (5.0, 0.0), (0.0, -3.0, True), 1.0) == 0.0


def rows_at(xs, ys=None):
    ys = ys or [0.0] * len(xs)
    return [SimpleNamespace(axis_start=(x, y - 10.0), axis_end=(x, y + 10.0)) for x, y in zip(xs, ys)]


@pytest.mark.parametrize("xs", [[0.0, 3.15, 6.3, 9.45, 12.6], [0.0, 1.0, 2.5, 7.0], [0.0, 30.0]])
def test_collinear_path_equals_literal_loop(xs):
    rows = rows_at(xs)
    fast = reports.annual_shade_loss(rows, 35.0, 0.0, 1.5)
    literal = reports.shade_table_literal(rows, 35.0, 0.0, 1.5)["annual_shade_loss"]
    assert fast == pytest.approx(literal, rel=1e-12, abs=1e-12)


def test_staggered_rows_take_the_literal_loop():
    rows = rows_at([0.0, 3.0, 6.0], [0.0, 1.0, 0.0])
    assert reports._collinear_sides([reports._centre(r) for r in rows]) is None
    assert reports.annual_shade_loss(rows, 35.0, 0.0, 1.5) == \
        reports.shade_table_literal(rows, 35.0, 0.0, 1.5)["annual_shade_loss"]
    with pytest.raises(reports.ReportInputError):
        reports.annual_shade_loss(rows, 35.0, 0.0, 0.0)


# -------------------------------------------------------- optimal spacing --

def module():
    return reports._sweep.TrackerModuleSpec(along_axis_m=1.0, cross_axis_m=2.1, gap_m=0.02, pmax_w=600.0)


LARGE = [(0.0, 0.0), (200.0, 0.0), (200.0, 200.0), (0.0, 200.0)]
SMALL = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)]


def calc(shade, **kw):
    return reports.optimal_row_spacing(LARGE, module(), 35.0, 0.0, 1.5, shade_loss=shade, **kw)


@pytest.mark.parametrize("kw,text", [({"target_capture": 0.0}, "targetCapture must be in (0, 1)"),
                                     ({"target_capture": 1.0}, "targetCapture must be in (0, 1)"),
                                     ({"target_capture": -0.5}, "targetCapture must be in (0, 1)"),
                                     ({"sweep_steps": 1}, "sweepSteps must be >= 2"),
                                     ({"sweep_steps": 0}, "sweepSteps must be >= 2"),
                                     ({"pitch_min_multiplier": 3.0, "pitch_max_multiplier": 1.5}, "must be <"),
                                     ({"pitch_min_multiplier": 0.0}, "pitchMinMultiplier must be > 0"),
                                     ({"pitch_min_multiplier": -1.0}, "pitchMinMultiplier must be > 0")])
def test_calculator_refusals(kw, text):
    with pytest.raises(reports.ReportInputError, match=text.replace("(", r"\(").replace(")", r"\)")):
        calc(lambda rows: 1.0, **kw)


def test_calculator_null_inputs():
    with pytest.raises(reports.ReportInputError, match="boundary is null"):
        reports.optimal_row_spacing(None, module(), 35.0, 0.0, 1.5, shade_loss=lambda rows: 1.0)
    with pytest.raises(reports.ReportInputError, match="module is null"):
        reports.optimal_row_spacing(LARGE, None, 35.0, 0.0, 1.5, shade_loss=lambda rows: 1.0)


def test_sweep_shape():
    r = calc(lambda rows: 0.0, sweep_steps=6)
    assert len(r["sweep"]) == 6
    assert r["sweep"][0]["pitch_m"] == pytest.approx(1.5 * 2.1) and r["sweep"][-1]["pitch_m"] == pytest.approx(3.0 * 2.1)
    pitches = [e["pitch_m"] for e in r["sweep"]]
    assert all(b > a for a, b in zip(pitches, pitches[1:]))
    assert all(e["gcr"] == pytest.approx(2.1 / e["pitch_m"]) for e in r["sweep"])
    r5 = calc(lambda rows: 5.0)
    assert all(e["irradiance_fraction"] == pytest.approx(0.95) for e in r5["sweep"])


def test_smallest_passing_pitch_and_widest_fallback():
    def by_pitch(rows):
        if len(rows) < 2:
            return 0.0
        (x0, y0), (x1, y1) = rows[0].axis_start, rows[1].axis_start
        return max(0.0, 10.0 - math.hypot(x1 - x0, y1 - y0))
    r = calc(by_pitch, sweep_steps=20, pitch_min_multiplier=1.5, pitch_max_multiplier=6.0)
    assert r["target_achieved"] and r["optimal_pitch_m"] >= 9.0 - 1e-6
    idx = [e["pitch_m"] for e in r["sweep"]].index(r["optimal_pitch_m"])
    assert all(e["irradiance_fraction"] < 0.99 for e in r["sweep"][:idx])
    miss = calc(lambda rows: 15.0)
    assert not miss["target_achieved"] and miss["optimal_pitch_m"] == pytest.approx(6.3)
    assert miss["irradiance_fraction"] == pytest.approx(0.85)
    easy = calc(lambda rows: 0.5)
    assert easy["optimal_gcr"] == pytest.approx(2.1 / easy["optimal_pitch_m"])


def test_command_valid_inputs_report_a_result():
    """OptimalRowSpacingCommandTests.RunCalculation_ValidInputs_ReturnsReportableResult."""
    out = reports.spacing_command([list(p) for p in SMALL], {}, {"sweep_steps": 2})
    r = out["result"]
    assert len(r["sweep"]) == 2 and r["optimal_pitch_m"] > 0.0
    assert 0.0 <= r["optimal_gcr"] <= 1.0 and 0.0 <= r["irradiance_fraction"] <= 1.0
    assert reports.spacing_command([[0, 0], [1, 1]], {})["error"] == "boundary polyline has fewer than 3 vertices."
    assert "targetCapture" in reports.spacing_command([list(p) for p in SMALL], {}, {"target_capture": 1.0})["error"]


def test_default_settings_come_from_the_drawing():
    stored = layout.save_settings({}, layout.module_command(layout.load_settings({})))
    s = reports.spacing_default_settings(stored)
    assert (s["module_cross_axis_m"], s["module_along_axis_m"], s["module_gap_m"], s["module_pmax_w"],
            s["module_height_m"], s["latitude_deg"], s["longitude_deg"]) == (2.1, 1.0, 0.02, 400.0, 1.5, 35.0, 0.0)
    assert reports.spacing_default_settings({})["module_pmax_w"] == 600.0
    assert reports.spacing_default_settings({}, (51.5, -0.1))["latitude_deg"] == 51.5


# ------------------------------------------------------ the licensed site --
# The plugin's printed b3, b4, b12, b13 and b14 outputs on the terrain fixture (the
# captures stay private; these are the values they print).

B3_LINE = ("LEAFFENCE3DAUDIT: Fence 3D: 0 source-3D + 0 flat (0 flat drapeable to LEAFTOPO); "
           "0 generated mesh face(s). scale=1 m/unit.")
B13_LINE = ("LEAFFENCEMESHFROMCIVIL: 0 recognized fence linework item(s); 0 source-3D, 0 flat "
            "(0 draped to LEAFTOPO); wrote 0 mesh face(s) on LEAF-PVCASE-SHADING-FENCE-MESH.")
B14_LINES = [
    "LEAFOPTIMALSPACING - Optimal Row Spacing Results:",
    "  Target capture : 99.0 %",
    "  Site           : lat 35.00, lon 0.00",
    "  Module         : 2.100 m cross-axis, h=1.50 m",
    "  Optimal pitch  : 6.300 m",
    "  Optimal GCR    : 0.333 (33.3%)",
    "  Capture        : 39.79 % (shade loss 60.21%)",
    "  Target achieved: no - widest pitch reported",
    "  Sweep:",
    "    Pitch    GCR    Capture    ShadeLoss  Rows",
    "     3.150  0.667   20.34 %     79.66%   159",
    "     3.316  0.633   21.42 %     78.58%   151",
    "     3.482  0.603   22.46 %     77.54%   144",
    "     3.647  0.576   23.51 %     76.49%   137",
    "     3.813  0.551   24.55 %     75.45%   131",
]


def site_entities():
    """The terrain fixture's civil content as the plugin traverses it: the boundary and the
    terrain faces (no fence, no woodland layer)."""
    intake = json.loads(INTAKE.read_text(encoding="utf-8"))
    return intake, ([poly("LEAF-BOUNDARY", intake["boundary"], closed=True)]
                    + [{"type": "face", "layer": "LEAF-TERRAIN"}] * len(intake["terrain_faces"]))


def test_licensed_b3_b13_lines():
    _, ents = site_entities()
    assert reports.fence_audit_status_line(reports.fence_audit(ents, plane, 1.0)) == B3_LINE
    assert reports.fence_mesh_message(reports.fence_mesh_build(ents, plane, 1.0)) == B13_LINE


def test_licensed_b14_report():
    """LEAFOPTIMALSPACING on the fixture boundary with Studio's own a3 module settings and
    every prompt at its default, computed: the full 8 760-hour shade sweep."""
    intake, _ = site_entities()
    stored = layout.save_settings({}, layout.module_command(layout.load_settings({})))
    out = reports.spacing_command(intake["boundary"], stored)
    assert reports.spacing_report_lines(out["result"], out["settings"]) == B14_LINES
    assert len(out["result"]["sweep"]) == 20
