"""Studio's LEAFCOMBINERAUTO placement port against the plugin source it ports (contract G35c).

Covered: the input plan (ApplyCombinerInputTargetToOptions, the physical SKU) and ChooseSku; the
string partitioner (target fill, the don't-mix-rows rule, the pull rebalance, the walk from the L2's
end); the panel-group rebuild from the intake (origin groups, CAD polylines by bounds, the footprint
tie-break, the per-group module, refusals); the alley detector (inner-row widths, the inter-group
aisle, aisle thresholds scaled by the module); the clearance grid against the plugin's linear scan;
.NET's Math.Round in the mount key; place() end to end (row-end snap nearest the L2, L1 numbering,
assignments, the alley shape) and the joint optimizer's L2 reassignment; the CLI and its refusals.
Intakes are synthetic and authored here; the committed capture is exercised when it is present.
"""
from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path
import random
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
CAPTURED_INTAKE = ROOT / "docs" / "parity" / "evidence" / "rooftop" / "inverters" / "combiner-intake.json"


def _load(name, path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cb = _load("solar_inverter_combiner", ROOT / "server" / "solar_inverter_combiner.py")


def _pt(x, y):
    return {"x": x, "y": y, "z": 0.0}


def _string(number, l2, row, col, a, b, count=4, angle=0.0):
    return {"L2Number": l2, "StringNumber": number, "GroupId": f"cable-{number}", "RowIndex": row,
            "PhysicalRowKey": None, "ColIndexMin": col, "ColIndexMax": col, "RowAngleRad": angle,
            "PanelCount": count, "centroid": _pt((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0),
            "endpointA": _pt(*a), "endpointB": _pt(*b)}


def _rect(cx, cy, w, h):
    return {"type": "Polyline", "handle": f"P{cx}_{cy}", "points": [
        _pt(cx - w / 2, cy - h / 2), _pt(cx + w / 2, cy - h / 2), _pt(cx + w / 2, cy + h / 2),
        _pt(cx - w / 2, cy + h / 2)]}


def _intake(strings, l2s, groups=(), cad=(), options=None, target=4, **inputs):
    opts = {"TargetInputs": 16, "MinInputs": 8, "MaxInputs": 24, "MaxL1PerL2": 4,
            "EnableJointL2RowEndOptimization": True}
    opts.update(options or {})
    body = {"panelGroups": list(groups), "l2Inverters": [
        {"Handle": f"H{n}", "InsertPt": {"X": x, "Y": y, "LengthSquared": x * x + y * y}, "Number": n}
        for n, (x, y) in l2s],
        "preBuiltStrings": list(strings), "trackerRows": [], "accessRoadLines": [], "alignmentLine": None,
        "cadContext": {"geometry": {"panels": list(cad)}}, "options": opts}
    body.update(inputs)
    return {"format": "combiner-intake-v1", "schema": "leaf.combiner-placement-dump.v1",
            "stage": "input-before-placement", "source": "LEAFCOMBINERAUTO",
            "commandContext": {"command": "LEAFCOMBINERAUTO", "combinerBoxConnections": target,
                               "l2NumMppt": 6, "l2StringsPerMppt": 6, "routeHomerunsInCloud": False},
            "inputs": body}


def _options(**overrides):
    raw = {"TargetInputs": 4, "MinInputs": 3, "MaxInputs": 4, "AllowRebalanceAboveTarget": False}
    raw.update(overrides)
    return cb.Options(raw)


def _summaries(raw_strings):
    return cb.build_strings({"inputs": {"preBuiltStrings": raw_strings}})


# ── input plan and SKU ───────────────────────────────────────────────────────


def test_input_plan_applies_target_sku_min_and_l2_inputs():
    o = cb.Options({})
    cb.apply_input_target(o, 20, 6, 6)
    assert (o.TargetInputs, o.MaxInputs, o.MinInputs, o.AllowRebalanceAboveTarget, o.MaxL1PerL2) == \
        (20, 20, 10, False, 36)
    o = cb.Options({"MaxL1PerL2": 4})
    cb.apply_input_target(o, 5, 0, 6)
    assert (o.TargetInputs, o.MaxInputs, o.MinInputs, o.MaxL1PerL2) == (5, 8, 2, 4)
    assert cb.resolve_physical_sku(33) == 33
    o = cb.Options({})
    cb.apply_input_target(o, 0, 6, 6)
    assert o.TargetInputs == 16


def test_choose_sku_smallest_catalog_fit_within_max_inputs():
    o = cb.Options({"MaxInputs": 20, "TargetInputs": 20})
    assert [cb.choose_sku(n, o) for n in (1, 8, 9, 13, 17, 20, 21)] == [8, 8, 12, 16, 20, 20, 20]


# ── partitioner ──────────────────────────────────────────────────────────────


def test_partition_fills_to_target_and_pulls_to_min_inputs():
    raw = [_string(n, 1, 0, n, (n * 100.0, 0.0), (n * 100.0 + 60.0, 0.0)) for n in range(1, 6)]
    groups = cb.partition(_summaries(raw), _options(), {1: (-500.0, 0.0)})
    assert [[s.string_number for s in g.strings] for g in groups] == [[1, 2, 3], [4, 5]]


def test_partition_walks_from_the_l2_end_and_splits_rows_once_min_is_met():
    raw = [_string(n, 1, 0, n, (n * 100.0, 0.0), (n * 100.0 + 60.0, 0.0)) for n in range(1, 4)]
    raw += [_string(n, 1, 1, n - 3, ((n - 3) * 100.0, 50.0), ((n - 3) * 100.0 + 60.0, 50.0))
            for n in range(4, 7)]
    groups = cb.partition(_summaries(raw), _options(), {1: (5000.0, 0.0)})
    assert [[s.string_number for s in g.strings] for g in groups] == [[3, 2, 1], [6, 5, 4]]


def test_partition_merges_short_tail_when_it_fits_and_validates_options():
    raw = [_string(n, 2, 0, n, (n * 100.0, 0.0), (n * 100.0 + 60.0, 0.0)) for n in range(1, 6)]
    groups = cb.partition(_summaries(raw), _options(TargetInputs=4, MinInputs=2, MaxInputs=8,
                                                    AllowRebalanceAboveTarget=True), None)
    assert [len(g.strings) for g in groups] == [5]
    with pytest.raises(cb.PlacementError):
        cb.partition([], _options(MinInputs=5))


# ── panel-group rebuild ──────────────────────────────────────────────────────


def _group(gid, lo, hi, count, angle=0.0):
    return {"id": gid, "rowAngleRad": angle, "isSynthesizedFromOutline": False, "panelCount": count,
            "bounds": {"min": _pt(*lo), "max": _pt(*hi)}, "stringNumbers": [], "l2Numbers": []}


def test_rebuild_puts_zero_bound_groups_at_origin_and_sizes_panels_per_group_module():
    cad = [_rect(x, y, 77.0, 38.5) for x in (0.0, 80.0) for y in (1000.0, 1100.0)]
    groups = [_group("G1", (0.0, 1000.0), (80.0, 1100.0), 4), _group("G0", (0.0, 0.0), (0.0, 0.0), 3)]
    rebuilt = cb.reconstruct_panel_groups(_intake([], [], groups, cad))
    g1, g0 = rebuilt
    assert sorted(p.center for p in g1.panels) == [(0.0, 1000.0), (0.0, 1100.0), (80.0, 1000.0), (80.0, 1100.0)]
    assert {(p.width_along_row, p.height_across_row) for p in g1.panels} == {(77.0, 38.5)}
    assert [p.center for p in g0.panels] == [(0.0, 0.0)] * 3
    ground = cb.reconstruct_panel_groups(_intake([], [], groups, cad), installation="Ground")
    assert (ground[0].panels[0].width_along_row, ground[0].panels[0].height_across_row) == (38.5, 77.0)


def test_rebuild_resolves_overlapping_bounds_by_footprint_and_refuses_bad_counts():
    # (100, 10) and (100, 0) lie inside both groups' bounds; each follows its footprint.
    cad = [_rect(0.0, 0.0, 77.0, 38.5), _rect(100.0, 10.0, 77.0, 38.5),
           _rect(100.0, 0.0, 60.0, 20.0), _rect(200.0, 10.0, 60.0, 20.0)]
    groups = [_group("A", (0.0, 0.0), (100.0, 10.0), 2), _group("B", (100.0, 0.0), (200.0, 10.0), 2)]
    a, b = cb.reconstruct_panel_groups(_intake([], [], groups, cad))
    assert sorted(p.center for p in a.panels) == [(0.0, 0.0), (100.0, 10.0)]
    assert sorted(p.center for p in b.panels) == [(100.0, 0.0), (200.0, 10.0)]
    assert (a.panels[0].height_across_row, b.panels[0].height_across_row) == (38.5, 20.0)
    bad = [_group("A", (0.0, 0.0), (100.0, 0.0), 3)]
    with pytest.raises(cb.PlacementError):
        cb.reconstruct_panel_groups(_intake([], [], bad, cad[:2]))


def test_rebuild_reads_module_sides_of_rotated_polylines():
    c, s = math.cos(0.4), math.sin(0.4)
    pts = [(-40.0, -20.0), (40.0, -20.0), (40.0, 20.0), (-40.0, 20.0)]
    poly = {"type": "Polyline", "handle": "R", "points": [_pt(x * c - y * s + 500.0, x * s + y * c) for x, y in pts]}
    xs = [p["x"] for p in poly["points"]]
    ys = [p["y"] for p in poly["points"]]
    centre = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
    (g,) = cb.reconstruct_panel_groups(_intake([], [], [_group("R", centre, centre, 1)], [poly]))
    assert g.panels[0].height_across_row == pytest.approx(40.0, abs=1e-9)
    assert g.panels[0].width_along_row == pytest.approx(80.0, abs=1e-9)


# ── alley detector ───────────────────────────────────────────────────────────


def _panels(points, h=38.5, w=77.0):
    return [cb.PanelInput(p, w, h) for p in points]


def test_inner_row_alley_width_is_centre_gap_minus_module_height():
    pts = [(x, y) for y in (0.0, 100.0) for x in (0.0, 80.0, 160.0)]
    g = cb.PanelGroupInput("G", 0.0, _panels(pts), False)
    (alley,) = cb.detect_alleys([g])
    assert alley.kind == "InnerRow" and alley.group_id == "G"
    assert alley.width == pytest.approx(100.0 - 38.5)
    assert alley.start == pytest.approx((0.0, 50.0)) and alley.end == pytest.approx((160.0, 50.0))
    tight = cb.PanelGroupInput("T", 0.0, _panels([(0.0, 0.0), (0.0, 40.0)]), False)
    assert cb.detect_alleys([tight]) == []


def test_inter_group_aisle_uses_module_scaled_thresholds():
    top = cb.PanelGroupInput("A", 0.0, _panels([(0.0, 300.0), (200.0, 300.0)], h=38.5), False)
    bottom = cb.PanelGroupInput("B", 0.0, _panels([(100.0, 0.0), (300.0, 0.0)], h=38.5), False)
    alleys = cb.detect_alleys([top, bottom])
    (aisle,) = [a for a in alleys if a.kind == "InterGroup"]
    assert aisle.width == 300.0 and aisle.start == (100.0, 150.0) and aisle.end == (200.0, 150.0)
    near = cb.PanelGroupInput("C", 0.0, _panels([(100.0, 100.0), (300.0, 100.0)], h=38.5), False)
    assert [a for a in cb.detect_alleys([near, bottom]) if a.kind == "InterGroup"] == []   # 100 < 3 x 38.5


# ── clearance grid and mount key ─────────────────────────────────────────────


def test_clearance_index_matches_the_plugin_linear_scan():
    rng = random.Random(7)
    panels = [cb.PanelInput((rng.uniform(0, 2000), rng.uniform(0, 2000)), rng.choice((77.0, 80.0)),
                            rng.choice((38.5, 40.0))) for _ in range(400)]
    panels += [cb.PanelInput((0.0, 0.0), 77.0, 38.5)] * 50
    index = cb.ClearanceIndex(panels)

    def linear(p):
        for pn in panels:
            r = max(pn.width_along_row, pn.height_across_row) * 0.5
            if r > 0 and (p[0] - pn.center[0]) ** 2 + (p[1] - pn.center[1]) ** 2 < r * r:
                return True
        return False

    probes = [(rng.uniform(-100, 2100), rng.uniform(-100, 2100)) for _ in range(3000)]
    probes += [(pn.center[0] + 38.4, pn.center[1]) for pn in panels[:50]]
    assert [index.violates(p) for p in probes] == [linear(p) for p in probes]
    assert cb.ClearanceIndex([]).violates((0.0, 0.0)) is False


def test_mount_key_rounds_half_to_even_like_dotnet():
    assert [cb._net_round1(v) for v in (0.25, 0.75, 1.25, -0.25, 12.34)] == [0.2, 0.8, 1.2, -0.2, 12.3]


# ── place() end to end ───────────────────────────────────────────────────────


def _row_strings(l2, count=4):
    return [_string(n, l2, 0, n - 1, ((n - 1) * 100.0, 0.0), ((n - 1) * 100.0 + 60.0, 0.0))
            for n in range(1, count + 1)]


def test_place_snaps_to_the_row_end_nearest_the_l2():
    intake = _intake(_row_strings(1), [(1, (-100.0, 0.0))])
    solution = cb.place(intake)
    (c,) = solution["combiners"]
    assert c["L1Number"] == 1 and c["ParentL2Number"] == 1
    assert c["ServedStringIds"] == [1, 2, 3, 4] and c["InputCountUsed"] == 4 and c["InputCountSku"] == 8
    assert c["snap"] == "RowEndNearestInverter"
    assert (c["location"]["x"], c["location"]["y"], c["location"]["z"]) == (-4.0, 0.0, 0.0)
    assert solution["l1ToL2Assignments"] == {"1": 1}
    assert solution["detectedAlleys"] == []


def test_place_numbers_combiners_across_l2s_in_number_order():
    strings = _row_strings(2) + [_string(n, 1, 1, n - 5, ((n - 5) * 100.0, 500.0), ((n - 5) * 100.0 + 60.0, 500.0))
                                 for n in range(5, 9)]
    solution = cb.place(_intake(strings, [(2, (-100.0, 0.0)), (1, (-100.0, 500.0))],
                                options={"EnableJointL2RowEndOptimization": False}))
    assert [(c["L1Number"], c["ParentL2Number"], c["ServedStringIds"]) for c in solution["combiners"]] == \
        [(1, 1, [5, 6, 7, 8]), (2, 2, [1, 2, 3, 4])]
    assert solution["l1ToL2Assignments"] == {"1": 1, "2": 2}


def test_joint_optimizer_moves_a_far_combiner_to_the_near_l2():
    l2s = [(1, (10000.0, 3000.0)), (2, (-100.0, 0.0))]
    moved = cb.place(_intake(_row_strings(1), l2s))
    kept = cb.place(_intake(_row_strings(1), l2s, options={"EnableJointL2RowEndOptimization": False}))
    assert kept["combiners"][0]["ParentL2Number"] == 1
    assert moved["combiners"][0]["ParentL2Number"] == 2
    assert moved["l1ToL2Assignments"] == {"1": 2}
    assert moved["combiners"][0]["location"]["x"] == -4.0


def test_place_emits_alleys_in_the_dump_shape():
    cad = [_rect(x, y, 77.0, 38.5) for y in (0.0, 100.0) for x in (0.0, 80.0, 160.0)]
    groups = [_group("G", (0.0, 0.0), (160.0, 100.0), 6)]
    solution = cb.place(_intake(_row_strings(1), [(1, (-100.0, 0.0))], groups, cad))
    (alley,) = solution["detectedAlleys"]
    assert set(alley) == {"kind", "width", "rowAngleRad", "start", "end", "center"}
    assert alley["kind"] == "InnerRow" and alley["width"] == pytest.approx(61.5)
    assert alley["center"] == pytest.approx({"x": 80.0, "y": 50.0, "z": 0.0})


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(format="other"),
    lambda d: d.update(stage="after"),
    lambda d: d["inputs"].update(trackerRows=[{"handle": "T"}]),
    lambda d: d["commandContext"].update(routeHomerunsInCloud=True),
    lambda d: d["inputs"]["preBuiltStrings"][0].update(centroid=_pt(float("nan"), 0.0)),
    lambda d: d["inputs"]["preBuiltStrings"][0].update(PanelCount="4"),
    lambda d: d["inputs"]["options"].update(TargetInputs=True),
    lambda d: d["inputs"].pop("l2Inverters"),
])
def test_place_refuses_malformed_or_unported_intakes(mutate):
    intake = _intake(_row_strings(1), [(1, (-100.0, 0.0))])
    mutate(intake)
    with pytest.raises(cb.PlacementError):
        cb.place(intake)


def test_place_without_l2s_places_nothing():
    assert cb.place(_intake(_row_strings(1), [])) == {"combiners": [], "l1ToL2Assignments": {},
                                                       "detectedAlleys": []}


def test_cli_writes_solution_and_refuses_bad_intake(tmp_path, capsys):
    good = tmp_path / "intake.json"
    good.write_text(json.dumps(_intake(_row_strings(1), [(1, (-100.0, 0.0))])), encoding="utf-8")
    out = tmp_path / "out" / "solution.json"
    assert cb.main(["--intake", str(good), "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["l1ToL2Assignments"] == {"1": 1}
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"format": "nope"}), encoding="utf-8")
    assert cb.main(["--intake", str(bad), "--out", str(tmp_path / "x.json")]) == 2
    assert not (tmp_path / "x.json").exists()
    assert "intake format" in capsys.readouterr().err


def test_captured_intake_places_fourteen_combiners_when_committed():
    if not CAPTURED_INTAKE.exists():
        pytest.skip("combiner-intake.json not committed yet (the lead commits it)")
    solution = cb.place(json.loads(CAPTURED_INTAKE.read_text(encoding="utf-8-sig")))
    assert len(solution["combiners"]) == 14
    assert sorted(s for c in solution["combiners"] for s in c["ServedStringIds"]) == list(range(1, 174))
    assert len(solution["detectedAlleys"]) == 85
