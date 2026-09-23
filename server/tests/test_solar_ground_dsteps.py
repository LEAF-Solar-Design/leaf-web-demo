"""Studio's terrain d-step engines against the plugin source (contract G28).

Covered: the trench router's heap, lattice and snapping (a straight run, a diagonal, a detour
round an obstacle, the zero-length and oversized-grid cases), LEAFTRENCH's obstacle and
existing-trench collection, its record defaults and message; LEAFSHOWEXPORT's two rectangles
and colour rule and LEAFHIDEEXPORT's count; the .NET custom number formats the yield bundle
prints; the layout metrics and the project overview with its coverage; the pile reveal
buckets; the shading, vegetation, clipped-object and cable rows; the zip entry order and the
manifest; and LEAFTRACKERSTOPANELGROUPS read through the key-aware row reader. Every expected
value is hand-computed from the cited plugin lines.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ds = _load("solar_ground_dsteps", ROOT / "server" / "solar_ground_dsteps.py")
MODULE = {"cross_axis_m": 2.0, "along_axis_m": 1.0, "pmax_w": 400.0}


def frame(x0, y0, x1, y1, **extra):
    return dict({"kind": "polyline", "layer": "LEAF-TRACKERS",
                 "vertices": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]}, **extra)


def drawn(ax, ay, bx, by, slots, width=2.0, row_index=1):
    return {"kind": "tracker", "axis_start": [ax, ay], "axis_end": [bx, by], "slots": slots,
            "row_index": row_index, "cross_axis_width_du": width}


# ------------------------------------------------------------------ trench --

def test_heap_pops_in_key_order():
    heap = ds._BinaryHeap()
    keys = [5.0, 1.0, 4.0, 1.5, 9.0, 0.5, 3.0, 2.0]
    for node, key in enumerate(keys):
        heap.push(node, key)
    popped = [heap.pop()[1] for _ in keys]
    assert popped == sorted(keys) and len(heap) == 0


def test_straight_route_is_every_lattice_node():
    result = ds.route_path((0.0, 0.0), (10.0, 0.0))
    assert result["success"] and len(result["segments"]) == 10
    assert result["segments"][0][0] == (0.0, 0.0)
    assert [b for _, b in result["segments"]] == [(float(i), 0.0) for i in range(1, 11)]
    assert result["total_length_m"] == 10.0


def test_diagonal_route():
    result = ds.route_path((0.0, 0.0), (5.0, 5.0))
    assert [b for _, b in result["segments"]] == [(float(i), float(i)) for i in range(1, 6)]
    assert result["total_length_m"] == pytest.approx(5 * math.sqrt(2.0))


def test_route_detours_round_an_obstacle():
    box = [(4.0, -2.0), (6.0, -2.0), (6.0, 2.0), (4.0, 2.0)]
    result = ds.route_path((0.0, 0.0), (10.0, 0.0), [box])
    assert result["success"]
    for a, b in result["segments"]:
        mid = ((a[0] + b[0]) * 0.5, (a[1] + b[1]) * 0.5)
        assert not ds.point_in_polygon(mid[0], mid[1], box)
    assert result["segments"][-1][1] == (10.0, 0.0) and result["total_length_m"] > 10.0


def test_existing_trench_is_preferred_over_free_ground():
    # A trench one metre off the direct line, from x=0 to x=10: following it costs half.
    trench = {"start": (0.0, 1.0), "end": (10.0, 1.0), "width_m": 0.6}
    result = ds.route_path((0.0, 0.0), (10.0, 0.0), existing_trenches=[trench])
    ys = [b[1] for _, b in result["segments"]]
    assert 1.0 in ys


def test_zero_length_and_oversized_grid():
    zero = ds.route_path((1.0, 1.0), (1.0, 1.0))
    assert zero["success"] and zero["segments"] == [((1.0, 1.0), (1.0, 1.0))]
    big = ds.route_path((0.0, 0.0), (300.0, 0.0))
    assert not big["success"]
    assert big["error"] == ("grid 311x11 exceeds MaxGridCellsPerAxis (200); increase GridStepM "
                            "or shrink the extent")
    out = ds.trench_command((0.0, 0.0), (300.0, 0.0))
    assert not out["succeeded"] and out["message"].startswith("LEAFTRENCH: route failed - grid 311x11")


def test_trench_command_record_and_message():
    out = ds.trench_command((0.0, 0.0), (10.0, 0.0), [])
    t = out["trench"]
    assert t["layer"] == "LEAF-PVCASE-TRENCH" and t["closed"] is False
    assert (t["depth_m"], t["width_m"], t["voltage_class"]) == (1.0, 0.6, "MIXED")
    assert t["vertices"] == [(float(i), 0.0) for i in range(11)]
    assert out["message"] == ("LEAFTRENCH: trench drawn on LEAF-PVCASE-TRENCH, length=10.00m, "
                              "segments=10, voltage=MIXED.")


def test_scenario_trench_is_a_monotone_lattice_walk():
    out = ds.trench_command((20.0, 20.0), (120.0, 70.0))
    v = out["trench"]["vertices"]
    assert v[0] == (20.0, 20.0) and v[-1] == (120.0, 70.0) and len(v) == 101
    steps = {(b[0] - a[0], b[1] - a[1]) for a, b in zip(v, v[1:])}
    assert steps <= {(1.0, 0.0), (1.0, 1.0)}


def test_trench_obstacle_collection():
    ents = [{"type": "polyline", "layer": "panel group", "vertices": [[0, 0], [1, 0], [1, 1]]},
            {"type": "polyline", "layer": "Panel Group", "vertices": [[0, 0], [1, 0]]},
            {"type": "polyline", "layer": "leaf-pvcase-trench", "vertices": [[0, 0], [5, 0], [5, 5]]},
            {"type": "block", "name": "SMA_Inverter_A", "extents": [1, 2, 3, 4]},
            {"type": "block", "name": "Combiner", "extents": [1, 2, 3, 4]},
            {"type": "block", "name": "inverter-x", "extents": None},
            {"type": "polyline", "layer": "LEAF-TRACKERS", "vertices": [[0, 0], [1, 0], [1, 1]]}]
    obstacles, trenches = ds.trench_obstacles(ents)
    assert obstacles == [[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)],
                         [(1.0, 2.0), (3.0, 2.0), (3.0, 4.0), (1.0, 4.0)]]
    assert [(t["start"], t["end"], t["width_m"], t["bend_radius_m"]) for t in trenches] == \
        [((0.0, 0.0), (5.0, 0.0), 0.6, 0.5), ((5.0, 0.0), (5.0, 5.0), 0.6, 0.5)]
    with pytest.raises(ds.DStepsInputError):
        ds.route_path((0.0, float("nan")), (1.0, 1.0))


# ---------------------------------------------------------- export preview --

ARRAY = {"centre_x": 10.0, "centre_y": 20.0, "half_x": 3.0, "half_y": 2.0, "azimuth_deg": 0.0,
         "modules_x": 2, "modules_y": 3}


def test_show_export_draws_the_outline_and_the_clearance():
    out = ds.show_export([ARRAY], 1.0)
    assert out["succeeded"] and out["erased"] == 0 and out["modules_total"] == 6
    outline, clearance = out["polylines"]
    assert (outline["role"], outline["color_index"], clearance["role"], clearance["color_index"]) == \
        ("array-outline", 4, "clearance", 2)
    assert outline["vertices"] == [(7.0, 18.0), (13.0, 18.0), (13.0, 22.0), (7.0, 22.0)]
    assert clearance["vertices"] == [(6.0, 17.0), (14.0, 17.0), (14.0, 23.0), (6.0, 23.0)]
    assert all(p["layer"] == "LEAF-EXPORT-PREVIEW" and p["closed"] for p in out["polylines"])
    assert out["message"] == ("LEAFSHOWEXPORT: drew 1 array(s) (cyan) + clearance (yellow) on layer "
                              "LEAF-EXPORT-PREVIEW. Total modules: 6. Run LEAFHIDEEXPORT to clear.")


def test_show_export_rotates_by_the_azimuth_and_erases_first():
    out = ds.show_export([dict(ARRAY, azimuth_deg=90.0)], 1.0, prior_preview_count=2)
    assert out["erased"] == 2
    first = out["polylines"][0]["vertices"][0]          # (-hx, -hy) turned 90 degrees
    assert first == pytest.approx((12.0, 17.0))


def test_show_export_without_arrays_and_hide():
    out = ds.show_export([], 2.0)
    assert not out["succeeded"] and out["polylines"] == []
    assert out["message"] == "LEAFSHOWEXPORT: no arrays defined. Use LEAFDEFINEARRAY first."
    assert ds.hide_export(2) == {"erased": 2, "message": "LEAFHIDEEXPORT: erased 2 preview entities."}
    assert ds.hide_export(1)["message"] == "LEAFHIDEEXPORT: erased 1 preview entity."


# ----------------------------------------------------------- number text --

@pytest.mark.parametrize("value,max_dec,min_dec,text", [
    (1.2345, 3, 0, "1.235"),        # 15 significant digits first, then half away from zero
    (2.0, 3, 0, "2"),
    (0.5, 0, 0, "1"),
    (-0.0001, 3, 0, "0"),
    (-1.5, 3, 0, "-1.5"),
    (27871.2, 2, 0, "27871.2"),
    (540.0, 3, 1, "540.0"),
    (0.0, 15, 0, "0"),
])
def test_net_custom(value, max_dec, min_dec, text):
    assert ds.net_custom(value, max_dec, min_dec) == text


def test_always_decimal_and_csv_cells():
    assert ds.always_decimal(540.0) == "540.0"
    assert ds.always_decimal(27871.2) == "27871.2"
    assert ds.always_decimal(12.345678) == "12.35"
    assert ds.net_integral_text(3.0) == "3"
    assert ds.csv_row("a,b", 1, 2.5, None, 'q"') == '"a,b",1,2.5,,"q"""'
    with pytest.raises(ds.DStepsInputError):
        ds.format_csv_value(True)
    assert ds.json_string('a"b\\c\n ') == '"a\\"b\\\\c\\n\\u2028"'
    assert ds.insunits_name(6) == "Meters" and ds.insunits_name(3) == "Unit(3)"


# ------------------------------------------------- metrics and the overview --

TRACKERS = [frame(0.0, 0.0, 2.0, 10.0), drawn(5.0, 0.0, 5.0, 10.0, 84)]
RING = {"layer": "APX-BNDY-SBCK-LINE-ARRAY", "vertices": [[0, 0], [100, 0], [100, 100], [0, 100]]}
CLOSED = [{"layer": "LEAF-TRACKERS", "vertices": TRACKERS[0]["vertices"]}, RING,
          {"layer": "LEAF-BOUNDARY", "vertices": [[0, 0], [500, 0], [500, 500], [0, 500]]}]


def test_layout_metrics_counts_types_and_coverage():
    m = ds.layout_metrics(TRACKERS, MODULE, CLOSED)
    assert (m["frames"], m["modules"], m["piles"]) == (2, 84, 4)
    assert m["kwp_total"] == pytest.approx(33.6)
    assert [(t["module_slots"], t["string_count"], t["count"], t["module_power_w"]) for t in m["frame_types"]] == \
        [(0, 0, 1, 400), (84, 14, 1, 400)]
    # The ring (not a LEAF-* layer) is the boundary; the frame counts as a polyline AND as an
    # explicit row area (20 + 20), the drawn row as its axis times its width (20).
    assert m["boundary_area_sqm"] == 10000.0
    assert m["coverage_ratio"] == pytest.approx(60.0 / 10000.0)


def test_layout_metrics_without_a_boundary_has_no_coverage():
    m = ds.layout_metrics(TRACKERS, MODULE, CLOSED[:1])
    assert m["boundary_area_sqm"] == 0.0 and m["coverage_ratio"] is None


def test_project_overview_text():
    text = ds.project_overview_csv(ds.layout_metrics(TRACKERS, MODULE, CLOSED))
    lines = text.split("\r\n")
    assert lines[0] == "Project name: Current drawing,Project name: Current drawing"
    assert lines[2:5] == ['"Total capacity, kWp",33.6', '"Module power, Wp",400', "Module quantity,84"]
    assert lines[11] == ",".join(["Information by area"] * 10)
    assert lines[12] == ('No.,14 String  ,18 String ,Modules,"Max. pitch, ft","Min. pitch, ft",'
                         '"Area coverage, %",GCR,"Capacity, kWp","Covered Area, ft2"')
    assert lines[13] == "Total,1,0,84,0,0,0.6,0,33.6,645.835"
    assert lines[14] == "" and len(lines) == 15
    named = ds.project_overview_csv(ds.layout_metrics(TRACKERS, MODULE), "site, one.dwg")
    assert named.startswith('"Project name: site, one.dwg","Project name: site, one.dwg"\r\n')


# ----------------------------------------------------------- pile grouping --

def flat(x, y):
    return 10.0 if 0.0 <= x <= 100.0 and 0.0 <= y <= 100.0 else None


def pile(x, y, top):
    return {"type": "circle", "layer": "LEAF-PILING", "center": [x, y], "radius": 0.1, "top_z": top}


def test_pile_grouping_buckets():
    piles = ds.pile_entities([pile(1, 1, 10.5), pile(2, 2, 11.0), pile(3, 3, 12.5), pile(4, 4, 9.0),
                              pile(500, 500, 11.0), {"type": "circle", "layer": "LEAF-TRACKERS"}])
    assert len(piles) == 5 and piles[0]["x"] == pytest.approx(1.0) and piles[0]["top_z"] == 10.5
    text = ds.pile_grouping_csv(piles, flat)
    assert text == ('"Pile reveal min, ft","Pile reveal max, ft",Pile count\r\n'
                    "-Infinity,3,2\r\n3,4,1\r\n4,5,0\r\n5,6,0\r\n6,7,0\r\n7,Infinity,1\r\n")


def test_pile_grouping_custom_boundaries_and_absences():
    piles = ds.pile_entities([pile(1, 1, 10.7)])
    text = ds.pile_grouping_csv(piles, flat, [1.0, 0.5, 1.0, -1.0, float("inf")])
    assert text.split("\r\n")[1:4] == ["-Infinity,1.64,0", "1.64,3.281,1", "3.281,Infinity,0"]
    assert ds.pile_grouping_csv(piles, None) is None
    assert ds.pile_grouping_csv([], flat) is None
    assert ds.pile_grouping_csv(ds.pile_entities([pile(500, 500, 1.0)]), flat) is None


# ----------------------------------------------------------------- the bundle --

SHADING = [{"type": "circle", "layer": "LEAF-PVCASE-SHADING-TREE", "center": [1.23456, 2.0], "radius": 3.0},
           {"type": "polyline", "layer": "LEAF-PVCASE-SHADING-STATION", "vertices": [[0, 0], [2, 0], [2, 2], [0, 2]]},
           {"type": "polyline", "layer": "leaf-pvcase-shading-vegetation", "vertices": [[0, 0], [4, 0], [4, 3]]},
           {"type": "polyline", "layer": "0", "vertices": [[0, 0], [10, 0]]},
           {"type": "polyline", "layer": "LEAF-PVCASE-SHADING-RESTRICTION",
            "vertices": [[5, -1], [6, -1], [6, 1], [5, 1]]},
           {"type": "polyline", "layer": "LEAF-PVCASE-SHADING-RESTRICTION",
            "vertices": [[50, 50], [60, 50], [60, 60]]}]


def test_bundle_shading_vegetation_and_clipped_rows():
    b = ds.yield_bundle(TRACKERS, MODULE, SHADING, CLOSED)
    assert b["shading"] == ["kind,x_du,y_du,radius_du", "tree,1.235,2,3", "station,1,1,0",
                            "vegetation,2.667,1,2.848"]
    assert b["vegetation_rows"][1] == "vegetation-1,2.667,1,0,2.848,3,0:0;4:0;4:3"
    assert (b["trees"], b["stations"], b["fences"], b["vegetation"], b["clipped"]) == (1, 1, 0, 1, 1)
    assert b["layout"] == ["frame_idx,x_du,y_du,kwp,tracker_handle"]
    assert b["overview"].endswith(
        "DC cable information,DC cable information\nDC cable runs,0\nDC copper length, ft.,0\n"
        "DC copper mass, lb,0\nAC feeder information,AC feeder information\nAC feeder runs,0\n"
        "AC feeder copper length, ft.,0\nAC feeder copper mass, lb,0\n")
    assert b["pile_grouping"] is None


def test_bundle_cable_rows():
    cable = {"circuit": "C1", "which": "HomeRunStart", "wire_gauge": "10 AWG", "length_ft": 0.0,
             "handle": "1a", "layer": "DC", "points": [[0, 0], [120, 0]], "closed": False}
    b = ds.yield_bundle(TRACKERS, MODULE, cables=[cable])
    assert b["dc_bom"][1] == "C1,HomeRunStart,10 AWG,10,2,20,0.628,1A,DC"
    assert b["dc_segments"][1] == "C1,HomeRunStart,10 AWG,1A,0,0,0,120,0,10"
    assert "DC cable runs,1\nDC copper length, ft.,20\nDC copper mass, lb,0.628\n" in b["overview"]
    with pytest.raises(ds.DStepsInputError):
        ds.yield_bundle(TRACKERS, MODULE, cables=[dict(cable, wire_gauge="NA")])


def test_layout_rows_use_the_stored_row_index():
    lines, rows = ds._layout_rows([
        {"row_index": 2, "x": 10.5, "y": 20.0, "module_slots": 6, "module_power_w": 400, "tracker_handle": "B"},
        {"row_index": 1, "x": 1.0, "y": 2.0, "module_slots": 6, "module_power_w": 400, "tracker_handle": "A"},
        {"row_index": 3, "x": 0.0, "y": 0.0, "module_slots": 6, "module_power_w": 400}])
    assert lines == ["1,1,2,2.4,A", "2,10.5,20,2.4,B"]


def test_zip_entries_and_manifest():
    b = ds.yield_bundle(TRACKERS, MODULE, SHADING + [pile(1, 1, 10.5)], CLOSED, terrain_z=flat)
    entries = ds.yield_zip_entries(b, "Meters")
    names = [n for n, _ in entries]
    assert names == ["layout.csv", "bom_project_overview.csv", "dc_homerun_bom.csv", "dc_homerun_segments.csv",
                     "ac_feeder_bom.csv", "ac_feeder_segments.csv", "shading.csv", "vegetation_masses.csv",
                     "README.txt", "Pile_grouping.csv", "manifest.json"]
    data = dict(entries)
    assert data["README.txt"].decode("utf-8") == ds.README_TEXT
    assert data["layout.csv"] == b"frame_idx,x_du,y_du,kwp,tracker_handle\n"
    text = data["manifest.json"].decode("utf-8")
    assert text.startswith('{\n  "exporter": "Leaf Automation",\n  "schema": "yield-export/1",\n'
                           '  "exported_at_utc": "",\n  "frames": 2,\n  "kwp_total": 33.6,\n  "piles": 4,\n'
                           '  "shading_counts": {\n    "trees": 1,\n')
    assert text.endswith("\n    }\n  ]\n}\n")
    manifest = json.loads(text)
    assert list(manifest) == ["exporter", "schema", "exported_at_utc", "frames", "kwp_total", "piles",
                              "shading_counts", "clipped_objects", "project_name", "drawing_path",
                              "plugin_version", "exported_units", "files"]
    assert manifest["clipped_objects"] == 1 and manifest["exported_units"] == "Meters"
    assert [f["name"] for f in manifest["files"]] == names[:-1]
    for f in manifest["files"]:
        assert f["bytes"] == len(data[f["name"]]) and f["sha256"] == hashlib.sha256(data[f["name"]]).hexdigest()
    assert ds.manifest_json(b, [])[-14:] == '"files": []\n}\n'


# ------------------------------------------------- trackers to panel groups --

def test_trackers_to_panel_groups_reads_row_fields_by_name():
    ents = TRACKERS + [frame(10.0, 0.0, 12.0, 10.0, slots=12),
                       {"kind": "polyline", "layer": "LEAF-TRACKERS", "slots": 6,
                        "vertices": [[1, 1], [1, 1], [1, 1], [1, 1]]},
                       drawn(20.0, 0.0, 20.0, 10.0, 0)]
    out = ds.trackers_to_panel_groups(ents)
    assert out == {"trackers": 2, "panel_groups_created": 2, "panel_group_slots": 96}
    with pytest.raises(ds.DStepsInputError):
        ds.trackers_to_panel_groups("not a list")
