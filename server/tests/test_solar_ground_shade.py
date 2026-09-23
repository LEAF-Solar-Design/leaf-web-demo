"""Studio's shade engines against the plugin, computed (contract G23: b8 to b11).

Covered: the sun-angle grids and the four profiles (the balanced profile's 216 angles and its
34,645,968 probe estimate), the sun position against the plugin's own reference figures, the
pair shade fraction, the bounded 8760-hour table against the literal O(8760 n^2) port on layouts
that exercise both of its summing paths, the LEAFSHADE table text, the tracker rows LEAFSHADE
reads, the CPU ray march (and that its early stop above the terrain's highest node never changes
an answer), the clearance binding, the loss gradient with its half-to-even rounding, the three
exports (an all-zero result for 1,197 panels on the balanced profile has exactly the byte sizes
of the files the plugin wrote at b8), the heatmap, LEAFSHADECOMPARE's sequence and
LEAFSHADEEXPLAIN. The terrain fixture itself (97.2% annual loss, 0/216 blocked for the explained
panel, the licensed tables) is computed from the committed intake in
scripts/test_solar_ground_shade_evidence.py, which runs Studio's whole chain to a13 first.
"""
from __future__ import annotations

import importlib.util
import math
import random
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


shade = _load("solar_ground_shade", ROOT / "server" / "solar_ground_shade.py")
terrain = _load("solar_ground_terrain", ROOT / "server" / "solar_ground_terrain.py")

GREEN = (31 << 16) | (142 << 8) | 62          # bin 0, the colour every b8 marker carries


def grid(fn, rows=21, cols=21, x_max=100.0, y_max=100.0):
    """A terrain interpolator over [0, x_max] x [0, y_max] with z = fn(x, y) at the nodes."""
    elevations = []
    for r in range(rows):
        for c in range(cols):
            elevations.append(float(fn(x_max * c / (cols - 1), y_max * r / (rows - 1))))
    return terrain.TerrainGridInterpolator(elevations, rows, cols, 0.0, x_max, 0.0, y_max, 1.0)


def frame(x, y, w=2.0, h=4.0, elevation=0.0, layer="LEAF-TRACKERS"):
    return {"type": "LWPOLYLINE", "layer": layer, "closed": True, "elevation": elevation,
            "vertices": [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]}


# ------------------------------------------------------------------ angles --

def test_balanced_profile_is_216_angles_altitude_major():
    p = shade.select_profile(1197)
    assert p["name"] == "balanced" and p["ray_step_m"] == 3.0 and p["max_ray_m"] == 400.0
    angles = p["angles"]
    assert len(angles) == 216
    assert [(a["azimuth_deg"], a["altitude_deg"]) for a in angles[:2]] == [(0.0, 5.0), (15.0, 5.0)]
    assert (angles[24]["azimuth_deg"], angles[24]["altitude_deg"]) == (0.0, 15.0)
    assert (angles[-1]["azimuth_deg"], angles[-1]["altitude_deg"]) == (345.0, 85.0)
    assert [a["index"] for a in angles] == list(range(216))
    assert p["estimated_samples"] == 34_645_968          # "~34,645,968 surface probes" at b8


@pytest.mark.parametrize("panels, name, count", [(1, "full", 468), (300, "full", 468), (301, "balanced", 216),
                                                  (1500, "balanced", 216), (1501, "large-site", 108),
                                                  (5000, "large-site", 108), (5001, "very-large-site", 48)])
def test_profile_thresholds(panels, name, count):
    p = shade.select_profile(panels)
    assert p["name"] == name and len(p["angles"]) == count


def test_sun_angle_grid_refuses_a_non_positive_step():
    with pytest.raises(shade.ShadeInputError):
        shade.make_sun_angle_grid(azimuth_step_deg=0.0)


# --------------------------------------------------------------------- sun --

def _noon_elevation(lat, day):
    return max(shade.sun_position(lat, 0.0, day, h / 100.0)[1] for h in range(1100, 1300))


def test_sun_position_matches_the_plugin_reference_figures():
    # SunPositionCalculator.cs:24-25: solstice noons at 40 N are ~73 and ~26.5 degrees.
    assert 72.9 < _noon_elevation(40.0, 172) < 73.9
    assert 26.0 < _noon_elevation(40.0, 355) < 27.1
    az_morning, _, night = shade.sun_position(37.0, 0.0, 100, 9.0)
    az_afternoon, _, _ = shade.sun_position(37.0, 0.0, 100, 15.0)
    assert not night and az_morning < 180.0 < az_afternoon
    assert shade.sun_position(37.0, 0.0, 100, 0.0)[0::2] == (0.0, True)


# -------------------------------------------------------------- pair shade --

def test_shade_fraction_cases():
    # Sun due south at 45 degrees: the shadow runs north 2 m; a row 4 m north is half shaded.
    assert shade.shade_fraction((0.0, 0.0), (0.0, 4.0), 180.0, 45.0, False, 2.0) == pytest.approx(0.5)
    assert shade.shade_fraction((0.0, 0.0), (0.0, 4.0), 0.0, 45.0, False, 2.0) == 0.0     # sun behind
    assert shade.shade_fraction((0.0, 0.0), (0.0, 0.0), 180.0, 45.0, False, 2.0) == 0.0   # co-located
    assert shade.shade_fraction((0.0, 0.0), (0.0, 4.0), 180.0, 45.0, True, 2.0) == 0.0    # night
    assert shade.shade_fraction((0.0, 0.0), (0.0, 4.0), 180.0, 1.0, False, 2.0) == 1.0    # clamped


def _rows(points):
    return [{"centre": p} for p in points]


def _assert_tables_equal(a, b):
    assert len(a["rows"]) == len(b["rows"]) and a["daylight_hours"] == b["daylight_hours"]
    for ra, rb in zip(a["rows"], b["rows"]):
        assert ra["row_id"] == rb["row_id"]
        assert ra["energy_weighted_pct"] == pytest.approx(rb["energy_weighted_pct"], abs=1e-7)
        assert ra["time_weighted_pct"] == pytest.approx(rb["time_weighted_pct"], abs=1e-7)
    assert a["annual_energy_weighted_pct"] == pytest.approx(b["annual_energy_weighted_pct"], abs=1e-7)
    assert a["annual_time_weighted_pct"] == pytest.approx(b["annual_time_weighted_pct"], abs=1e-7)


@pytest.fixture(scope="module")
def spread_layout():
    rng = random.Random(23)
    pts = [(rng.uniform(0.0, 60.0), rng.uniform(0.0, 60.0)) for _ in range(12)]
    pts.append(pts[3])                           # a co-located pair contributes nothing to each other
    return _rows(pts), shade.shade_table_reference(_rows(pts), 37.0, 0.0, 2.0)


@pytest.mark.parametrize("near_limit", [0, 3, 48])
def test_bounded_table_equals_the_literal_port_on_a_spread_layout(spread_layout, near_limit):
    rows, reference = spread_layout
    _assert_tables_equal(shade.shade_table(rows, 37.0, 0.0, 2.0, near_limit=near_limit), reference)


def test_bounded_table_equals_the_literal_port_on_stacked_rows():
    # Three stacked layouts on a 1.5 m pitch: most hours saturate, the edges do not.
    pts = [(x * 1.5 + dx, y * 6.0) for x in range(4) for y in range(3) for dx in (0.0, 0.4)]
    rows = _rows(pts)
    reference = shade.shade_table_reference(rows, 37.0, 0.0, 2.0)
    for near_limit in (0, 2, 48):
        _assert_tables_equal(shade.shade_table(rows, 37.0, 0.0, 2.0, near_limit=near_limit), reference)
    assert reference["annual_energy_weighted_pct"] > 0.0


def test_shade_table_refusals():
    with pytest.raises(shade.ShadeInputError):
        shade.shade_table(_rows([(0.0, 0.0)]), 37.0, 0.0, 0.0)
    with pytest.raises(shade.ShadeInputError):
        shade.shade_table([{"centre": (0.0, float("nan"))}], 37.0, 0.0, 2.0)
    with pytest.raises(shade.ShadeInputError):
        shade.shade_table(_rows([(0.0, 0.0)]), 37.0, 0.0, 2.0, near_limit=-1)
    empty = shade.shade_table([], 37.0, 0.0, 2.0)
    assert empty["rows"] == [] and empty["annual_energy_weighted_pct"] == 0.0


# ---------------------------------------------------------------- LEAFSHADE --

def test_tracker_rows_read_frames_and_tracker_blocks_in_drawing_order():
    ents = [frame(0.0, 0.0), frame(10.0, 0.0, layer="OTHER"),
            {"type": "LWPOLYLINE", "layer": "leaf-trackers", "vertices": [(0, 0), (1, 0), (1, 1)]},
            {"type": "INSERT", "axis_start": (20.0, 0.0), "axis_end": (20.0, 10.0)},
            {"type": "INSERT"},
            frame(30.0, 0.0)]
    rows = shade.tracker_rows_for_shade(ents)
    assert [r["centre"] for r in rows] == [(1.0, 2.0), (20.0, 5.0), (31.0, 2.0)]
    # PolylineToTrackerRow: the axis runs from mid(v0, v1) to mid(v2, v3).
    assert rows[0]["axis_start"] == (1.0, 0.0) and rows[0]["axis_end"] == (1.0, 4.0)


def test_annual_shade_takes_the_prompt_defaults_and_writes_the_table():
    ents = [frame(0.0, 0.0), frame(0.0, 6.0), {"type": "INSERT", "axis_start": (5.0, 0.0), "axis_end": (5.0, 4.0)}]
    res = shade.annual_shade(ents)
    assert res["succeeded"] and res["row_count"] == 3
    assert (res["latitude_deg"], res["longitude_deg"], res["module_height_m"]) == (37.0, 0.0, 2.0)
    text = res["csv"]
    assert text.startswith("﻿LEAFSHADE Shade Analysis\r\nSite lat (deg),37.0000\r\nSite lon (deg),0.0000\r\n")
    lines = text.split("\r\n")
    assert lines[-1] == "" and len(lines) - 1 == 7 + 3
    assert lines[5] == "" and lines[6] == "Row,Clear-Sky Energy-Weighted Shade Loss (%),Time-Weighted Shade Loss (%)"
    for r, line in enumerate(lines[7:-1]):
        row_id, energy, time_pct = line.split(",")
        assert row_id == f"Row {r}"
        assert energy == shade.net_fixed(res["table"]["rows"][r]["energy_weighted_pct"], 2)
        assert time_pct == shade.net_fixed(res["table"]["rows"][r]["time_weighted_pct"], 2)
    assert lines[3] == ("Clear-sky energy-weighted annual shading loss (%),"
                        + shade.net_fixed(res["annual_loss_pct"], 2))
    assert res["message"].endswith(f"{shade.net_fixed(res['annual_loss_pct'], 1)}% (8760-hour simulation)")
    assert res["simulated_hours"] == 8760 and isinstance(res["simulated_hours"], int)


def test_annual_shade_without_rows_and_refusals():
    assert not shade.annual_shade([frame(0.0, 0.0, layer="OTHER")])["succeeded"]
    with pytest.raises(shade.ShadeInputError):
        shade.annual_shade([frame(0.0, 0.0)], module_height_m=0.0)
    with pytest.raises(shade.ShadeInputError):
        shade.annual_shade("not a list")
    assert shade.annual_shade([frame(0.0, 0.0)], lat_deg=95.0)["latitude_deg"] == 89.9


# --------------------------------------------------------------- ray march --

def test_flat_terrain_never_blocks_a_raised_panel():
    surface = shade.TerrainShadeSurface(grid(lambda x, y: 0.0), 1.0)
    for a in shade.select_profile(500)["angles"]:
        assert not shade.is_beam_blocked(50.0, 50.0, 1.5, a["azimuth_deg"], a["altitude_deg"], surface, 3.0, 400.0)


def test_a_ridge_blocks_the_sun_behind_it_only():
    surface = shade.TerrainShadeSurface(grid(lambda x, y: 30.0 if x >= 60.0 else 0.0), 1.0)
    assert shade.is_beam_blocked(30.0, 50.0, 1.5, 90.0, 5.0, surface, 3.0, 400.0)       # east, into the ridge
    assert not shade.is_beam_blocked(30.0, 50.0, 1.5, 270.0, 5.0, surface, 3.0, 400.0)  # west, off the grid
    assert shade.is_beam_blocked(30.0, 50.0, 1.5, 0.0, 0.0, surface, 3.0, 400.0)        # night is shaded


def test_the_early_stop_above_the_highest_node_never_changes_an_answer():
    bumpy = grid(lambda x, y: 4.0 * math.sin(x / 7.0) * math.cos(y / 11.0) + 0.05 * x)
    capped = shade.TerrainShadeSurface(bumpy, 1.0)
    uncapped = shade.TerrainShadeSurface(bumpy, 1.0)
    uncapped.z_cap = None
    rng = random.Random(7)
    angles = shade.select_profile(1000)["angles"]
    seen = set()
    for _ in range(60):
        x, y = rng.uniform(5.0, 95.0), rng.uniform(5.0, 95.0)
        z = bumpy.interpolate_z(x, y) + rng.uniform(0.0, 3.0)
        for a in angles:
            args = (x, y, z, a["azimuth_deg"], a["altitude_deg"])
            got = shade.is_beam_blocked(*args, capped, 3.0, 400.0)
            assert got == shade.is_beam_blocked(*args, uncapped, 3.0, 400.0)
            seen.add(got)
    assert seen == {True, False}


def test_run_shade_aggregates_like_the_engine():
    surface = shade.TerrainShadeSurface(grid(lambda x, y: 30.0 if x >= 60.0 else 0.0), 1.0)
    angles = shade.make_sun_angle_grid(90.0, 40.0, 5.0, 85.0, False)      # az 0/90/180/270, alt 5/45/85
    panels = [{"x": 30.0, "y": 50.0, "z": 1.5}, {"x": 20.0, "y": 50.0, "z": 1.5}]
    res = shade.run_shade(panels, angles, surface, 3.0, 400.0)
    east_low = next(a["index"] for a in angles if a["azimuth_deg"] == 90.0 and a["altitude_deg"] == 5.0)
    assert res["shade"][0][east_low] == 1 and res["avg_by_angle"][east_low] == 1.0
    weights = [max(0.0, math.sin(a["altitude_deg"] * math.pi / 180.0)) for a in angles]
    for p in range(2):
        expected = sum(w for w, s in zip(weights, res["shade"][p]) if s) / sum(weights)
        assert res["weighted_per_panel"][p] == pytest.approx(expected, rel=1e-15)
    with pytest.raises(shade.ShadeInputError):
        shade.run_shade(panels, angles, surface, 0.0, 400.0)


# ------------------------------------------------------------------ panels --

def test_panels_bind_to_the_target_clearance():
    dtm = grid(lambda x, y: 0.02 * x)
    panels = shade.read_panel_centres([frame(10.0, 10.0), frame(20.0, 10.0, elevation=0.0)], dtm)
    assert [p["z"] for p in panels] == [pytest.approx(0.02 * 11.0), pytest.approx(0.02 * 21.0)]
    binding = shade.bind_panel_datum(panels, dtm, 1.0, shade.target_clearance_m({}))
    assert binding["mode"] == "shifted" and binding["shift_m"] == pytest.approx(1.5)
    assert binding["median_clearance_m"] == pytest.approx(0.0, abs=1e-12)
    assert panels[0]["z"] == pytest.approx(0.02 * 11.0 + 1.5)
    raised = shade.read_panel_centres([frame(10.0, 10.0, elevation=0.02 * 11.0 + 1.4)], dtm)
    assert shade.bind_panel_datum(raised, dtm, 1.0, 1.5)["mode"] == "bound"
    assert shade.target_clearance_m({"TorqueTubeHeightM": 2.0}) == 2.0
    assert shade.target_clearance_m({"TorqueTubeHeightM": 25.0}) == 1.5
    open_poly = dict(frame(0.0, 0.0), closed=False)
    assert shade.read_panel_centres([open_poly, frame(0.0, 0.0, layer="X")], dtm) == []


# ---------------------------------------------------------------- gradient --

def test_loss_bins_and_colours():
    assert [shade.loss_bin(v) for v in (0.0, -1.0, 0.10, 0.1000001, 20.0, 20.01, float("nan"))] == [0, 0, 0, 1, 18, 19, 0]
    assert shade.bin_color(0) == (31, 142, 62) and shade.bin_color(19) == (200, 35, 35)
    assert shade.bin_color(7) == (186, 208, 75)
    assert shade.bin_color(12) == (249, 184, 52)
    assert shade.bin_color(17) == (220, 82, 38)       # 82.5 and 37.5 round half to even, as Math.Round


def test_net_formatting():
    assert [shade.net_general(v) for v in (0.0, 15.0, 345.0, 0.5, -0.0, 1e-05)] == ["0", "15", "345", "0.5", "-0", "1E-05"]
    assert shade.net_fixed(97.2444, 1) == "97.2" and shade.net_fixed(0.125, 2) == "0.12"
    assert shade.printed(1.4999, 2) == 1.5


# ----------------------------------------------------------------- exports --

def _zero_result(panels, angles):
    return {"angles": angles, "shade": [bytearray(len(angles)) for _ in range(panels)],
            "avg_by_angle": [0.0] * len(angles), "weighted_per_panel": [0.0] * panels}


def test_all_zero_exports_have_the_b8_file_sizes():
    # 1,197 panels on the balanced profile, nothing blocked: the b8 files are 1,653, 3,211 and
    # 4,816,417 bytes (G23 file rows, sizes read from the private capture).
    res = _zero_result(1197, shade.select_profile(1197)["angles"])
    azal = shade.azal_matrix_text(res)
    sam = shade.sam_beam_text(res)
    per_panel = shade.per_panel_text(res)
    assert [len(t.encode("utf-8")) for t in (azal, sam, per_panel)] == [1653, 3211, 4816417]
    assert azal.startswith("Altitude\\Azimuth,0,15,30,45,60,75,90,105,") and azal.endswith(",0.0000\r\n")
    assert azal.split("\r\n")[1].startswith("5,0.0000,")
    assert sam.split("\r\n")[:3] == ["Solar Azimuth (deg),Solar Altitude (deg),Beam Shading Loss Factor",
                                     "0,5,0.0000", "15,5,0.0000"]
    lines = per_panel.split("\r\n")
    assert len(lines) - 1 == 258_553 and lines[1] == "0,0,5,0.0000" and lines[-2] == "1196,345,85,0.0000"


def test_exports_print_shaded_fractions():
    angles = shade.make_sun_angle_grid(180.0, 80.0, 5.0, 85.0, False)
    res = {"angles": angles, "shade": [bytearray([1, 0, 0, 0]), bytearray([1, 1, 0, 0])],
           "avg_by_angle": [1.0, 0.5, 0.0, 0.0], "weighted_per_panel": [0.1, 0.2]}
    assert shade.per_panel_text(res).split("\r\n")[1:3] == ["0,0,5,1.0000", "0,180,5,0.0000"]
    assert shade.sam_beam_text(res).split("\r\n")[2] == "180,5,0.5000"
    assert shade.azal_matrix_text(res).split("\r\n")[:3] == ["Altitude\\Azimuth,0,180", "5,1.0000,0.5000",
                                                             "85,0.0000,0.0000"]


# --------------------------------------------------- commands on flat ground --

def _field():
    return [frame(10.0 + 6.0 * i, 20.0 + 8.0 * j) for j in range(3) for i in range(5)]


def test_shade_sim_on_flat_ground():
    dtm = grid(lambda x, y: 0.0)
    sim = shade.shade_sim(_field(), dtm, 1.0, {}, existing_heatmap=4)
    assert sim["succeeded"] and len(sim["panels"]) == 15 and sim["cleared"] == 4
    assert sim["profile"]["name"] == "full" and sim["mean_shade"] == 0.0
    assert sim["binding"]["mode"] == "shifted" and sim["binding"]["shift_m"] == pytest.approx(1.5)
    m = sim["markers"][0]
    assert m["true_color"] == GREEN and m["loss_bin"] == 0 and m["elevation"] == pytest.approx(1.5)
    assert m["vertices"] == [(9.0, 20.0), (13.0, 20.0), (13.0, 24.0), (9.0, 24.0)]
    assert set(sim["files"]) == {"shade-azal-matrix", "shade-sam", "shade-per-panel"}
    assert "(4 prior heatmap entities replaced)" in sim["message"]
    assert sim["surface"] == {"rows": 21, "cols": 21, "cells": 441}
    assert not shade.shade_sim(_field(), None)["succeeded"]
    assert not shade.shade_sim([], dtm)["succeeded"]


def test_surface_snapshot_is_the_reference_grid_size():
    assert shade.surface_snapshot(grid(lambda x, y: 0.0, rows=90, cols=150)) == {"rows": 90, "cols": 150,
                                                                                 "cells": 13500}
    assert shade.surface_snapshot(None) is None

    class Thin:
        rows, cols = 1, 150
    assert shade.surface_snapshot(Thin()) is None

    class Broken:
        rows, cols = 2.0, 3
    with pytest.raises(shade.ShadeInputError):
        shade.surface_snapshot(Broken())


def test_shade_sim_tints_panels_below_a_ridge():
    dtm = grid(lambda x, y: 40.0 if x >= 80.0 else 0.0)
    sim = shade.shade_sim(_field(), dtm, 1.0, {})
    assert sim["mean_shade"] > 0.0
    for m, w in zip(sim["markers"], sim["result"]["weighted_per_panel"]):
        r, g, b = shade.bin_color(shade.loss_bin(w * 100.0))
        assert m["true_color"] == (r << 16) | (g << 8) | b


def test_shade_explain_on_flat_ground_finds_no_blocker():
    ents = _field()
    last = ents[-1]["vertices"]
    pick = (sum(p[0] for p in last) / 4.0, sum(p[1] for p in last) / 4.0)
    res = shade.shade_explain(ents, grid(lambda x, y: 0.0), 1.0, {}, pick)
    assert res["succeeded"] and res["panel_index"] == 14 and res["pick_distance_m"] == 0.0
    x = res["explanation"]
    assert (x["blocked_angles"], x["angle_count"], x["weighted_shade"]) == (0, 468, 0.0)
    assert res["message"] == "Result: 0/468 angle(s) blocked; cos(zenith)-weighted shade 0.00%."


def test_shade_explain_attributes_a_ridge():
    res = shade.shade_explain(_field(), grid(lambda x, y: 40.0 if x >= 80.0 else 0.0), 1.0, {}, (40.0, 30.0))
    x = res["explanation"]
    assert x["blocked_angles"] > 0 and x["hits"][0]["source"] == "terrain"
    assert [h["distance_m"] for h in x["hits"]] == sorted(h["distance_m"] for h in x["hits"])


def test_shade_compare_runs_the_sim_then_the_export():
    calls = []

    def export(dtm, records):
        calls.append(records)
        return {"succeeded": True, "dae": "d", "pvc": "p", "rows": 2, "cols": 2, "array_count": len(records)}
    out = shade.shade_compare(_field(), grid(lambda x, y: 0.0), 1.0, {}, 15, [], export)
    assert out["sim"]["succeeded"] and out["sim"]["cleared"] == 15 and out["preview"] == [] and calls == [[]]
    with pytest.raises(shade.ShadeInputError):
        shade.shade_compare(_field(), grid(lambda x, y: 0.0), 1.0, {}, 0, [{"key": "array_0"}], export)
