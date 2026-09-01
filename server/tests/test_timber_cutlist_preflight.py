"""timber-cutlist-preflight: the local, zero-cost preview of what the AppBundle will read.

Cross-checked against CutLists.Core on the six-views fixture (the client's spec drawing:
six labelled frames, rule-coded layers). The engine's own preflight on the same drawing
reports 6 views, RAFTER_45X145 with 11 segments, and WINDOW/DOOR/ROOF_COVER/0 as not
counted (CutLists.Core.Tests/PreflightTests.cs); this suite pins the same facts here.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_DIR))

import dxf_intake  # noqa: E402
import tool_loader  # noqa: E402

FIXTURE = SERVER_DIR / "tests" / "fixtures" / "six_views.dxf"


def _tool():
    spec = importlib.util.spec_from_file_location("timber_cutlist_preflight_test", SERVER_DIR / "builtins" / "timber_cutlist_preflight.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _six_views_intake():
    return dxf_intake.parse_dxf_file(FIXTURE, source_name="six_views.dxf")


def test_layer_rule_mirrors_the_engine():
    t = _tool()
    assert t.parse_layer("WOOD_45X70")["key"] == "WOOD_45X70"
    assert t.parse_layer("mål 1x50")["key"] == "MÅL_1X50"
    assert t.parse_layer("RIDGE_50 X 150")["key"] == "RIDGE_50X150"
    assert t.parse_layer("WOOD_45,5X70")["key"] == "WOOD_45.5X70"
    for bad in ("0", "grundplan", "WINDOW", "WOOD_0X70", "_45X70", "", "A" * 300 + "_45X70"):
        assert t.parse_layer(bad) is None, bad
    assert t.classify("Facade Syd-Øst") == "ElevationEast"
    assert t.classify("GROUND FLOOR PLAN") == "Plan"
    assert t.classify("Note: kloak") == "Unknown"


def test_six_views_fixture_matches_the_engine_preflight():
    intake = _six_views_intake()
    assert "texts" in intake and len(intake["texts"]) == 6
    result, overlay = _tool().run(intake, {})
    assert result["view_count"] == 6
    kinds = {v["kind"] for v in result["views"]}
    assert kinds == {"Plan", "RoofPlan", "ElevationNorth", "ElevationEast", "ElevationSouth", "ElevationWest"}
    assert next(v for v in result["views"] if v["kind"] == "Plan")["label"] == "GROUND FLOOR PLAN"
    rows = {r[0]: r for r in result["table"]["rows"]}
    assert rows["RAFTER_45X145"][1] == "RAFTER_45X145" and rows["RAFTER_45X145"][2] == 11 and rows["RAFTER_45X145"][3] == "yes"
    assert rows["RIDGE_50X150"][3] == "yes"
    for uncounted in ("WINDOW", "DOOR", "ROOF_COVER"):
        assert rows[uncounted][1] == "not Material_W x H" and rows[uncounted][3] == "no"
    # frame edges (layer 0) are not segments of any counted layer
    assert "0" not in rows or rows["0"][3] == "no"
    # counted rows first, every segment lands in exactly one view
    counted_flags = [r[3] for r in result["table"]["rows"]]
    assert counted_flags == sorted(counted_flags, key=lambda s: s != "yes")
    assert sum(v["segments"] for v in result["views"]) == sum(r[2] for r in result["table"]["rows"])
    assert len(overlay["markers"]) == 6 and all("(" in m["label"] for m in overlay["markers"])
    assert len(overlay["highlight_handles"]) == 6
    assert any(p["color"] == "#a03c14" for p in overlay["polylines"])  # rafters in rafter brown
    assert not any("no labelled view frames" in w for w in result["warnings"])
    assert any("WINDOW" in w for w in result["warnings"])


def test_runs_through_the_dynamic_loader_without_degraded_flag():
    reg = json.loads((SERVER_DIR.parent / "engine" / "registry.json").read_text(encoding="utf-8"))
    tool = next(t for t in reg["tools"] if t["name"] == "timber-cutlist-preflight")
    assert tool["local_only"] is True and tool["entry"] == "builtins/timber_cutlist_preflight.py"
    env = tool_loader.run_tool_dynamic(tool, _six_views_intake(), {}, aps_live=True, da=None)
    assert env["ok"] is True, env
    assert env["degraded_mode"] is False
    assert env["result"]["view_count"] == 6


def test_empty_and_hostile_intakes_are_bounded():
    t = _tool()
    result, overlay = t.run({"polylines": [], "texts": []}, {})
    assert result["view_count"] == 0 and result["counted_segments"] == 0
    assert any("no rule-coded" in w for w in result["warnings"])
    many = {"polylines": [{"layer": "WOOD_45X70", "closed": False, "pts": [[i, 0, 0], [i + 1, 0, 0]], "handle": str(i)}
                          for i in range(25_000)], "texts": []}
    result, overlay = t.run(many, {})
    assert result["counted_segments"] == 25_000
    assert len(overlay["polylines"]) == t._MAX_OVERLAY_POLYLINES
    assert any("overlay truncated" in w for w in result["warnings"])
