"""Tests for server/solar_batch2_simple.py (G36 zone height, shade reports, frame information, deep search)."""
import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "solar_batch2_simple.py"
_SPEC = importlib.util.spec_from_file_location("solar_batch2_simple", _PATH)
eng = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(eng)


def zone_state(zones=None, design="Roof"):
    return {"format": "zone-height-state-v1", "units": "in", "installation_design": design,
            "elevation_zones": zones or [], "string_layer": "String"}


def test_zone_height_adds_zone_one_red_with_the_applied_offset():
    zones = eng.zone_height(zone_state(), {"add": 1, "offset_in": 24})
    assert zones == [{"name": "Zone 1", "offset": {"kind": "length", "value": 24.0, "unit": "in"}, "colour": 1,
                      "strings": [], "panels": []}]
    assert eng.zone_rows(zone_state(), zones) == [("elevation-zone-1", zones[0])]


def test_zone_height_names_by_count_and_restarts_the_colour_cycle():
    existing = [{"name": "Zone 1", "offset": {"kind": "length", "value": 6.0, "unit": "in"}, "colour": 1,
                 "strings": ["A1"], "panels": []}]
    zones = eng.zone_height(zone_state(existing), {"add": 1, "offset_in": 12.345})
    assert [z["name"] for z in zones] == ["Zone 1", "Zone 2"]
    assert zones[1]["colour"] == 1            # the cursor starts at 0 every time the form opens
    assert zones[1]["offset"]["value"] == 12.35   # two decimals, half away from zero
    assert zones[0]["strings"] == ["A1"]


def test_zone_height_refuses_non_rooftop_and_out_of_range_offsets():
    with pytest.raises(eng.BatchTwoError):
        eng.zone_height(zone_state(design="Ground"), {"add": 1, "offset_in": 24})
    with pytest.raises(eng.BatchTwoError):
        eng.zone_height(zone_state(), {"add": 1, "offset_in": -1})
    with pytest.raises(eng.BatchTwoError):
        eng.zone_height(zone_state(), {"add": 1, "offset_in": 10000.01})
    with pytest.raises(eng.BatchTwoError):
        eng.zone_height(zone_state(), {"add": 0, "offset_in": 1})


def test_zone_rows_empty_when_nothing_changed():
    existing = [{"name": "Zone 1", "offset": {"kind": "length", "value": 6.0, "unit": "in"}, "colour": 1,
                 "strings": [], "panels": []}]
    assert eng.zone_rows(zone_state(existing), [dict(existing[0])]) == []


def markers(n, hits=False):
    return {"format": "heatmap-markers-v1", "units": "m", "marker_count": n, "markers_with_stored_hits": 0,
            "markers": [{"has_stored_hits": hits and i == 0} for i in range(n)]}


def test_shadow_curtain_reports_counts_and_refuses_a_drawing_outcome():
    assert eng.shadow_curtain(markers(3)) == [("report-heatmap-markers", {"name": "heatmap-markers", "value": 3}),
                                              ("report-message", {"name": "message", "value": "no-stored-hits"})]
    assert eng.shadow_curtain(markers(0))[1][1]["value"] == "no-heatmap-markers"
    with pytest.raises(eng.BatchTwoError):
        eng.shadow_curtain(markers(2, hits=True))
    bad = markers(2)
    bad["marker_count"] = 5
    with pytest.raises(eng.BatchTwoError):
        eng.shadow_curtain(bad)


def test_heatmap_commands_report_no_data_and_refuse_a_populated_map():
    empty = {"shade_loss_per_module": {}, "shade_loss_heatmap_applied": False}
    assert eng.shade_loss_heatmap(empty) == [("report-message", {"name": "message", "value": "no-shading-data"})]
    assert eng.shade_loss_heatmap_clear(empty) == eng.shade_loss_heatmap(empty)
    full = {"shade_loss_per_module": {"A1": 3.2}, "shade_loss_heatmap_applied": True}
    for command in (eng.shade_loss_heatmap, eng.shade_loss_heatmap_clear):
        with pytest.raises(eng.BatchTwoError):
            command(full)


def test_net_format_matches_dotnet_custom_formats():
    assert eng.net_format(2.384, 3) == "2.384"
    assert eng.net_format(0.02, 3) == "0.02"
    assert eng.net_format(8.0, 2) == "8"
    assert eng.net_format(4.29, 2) == "4.29"
    assert eng.net_format(0.0, 2) == "0"
    assert eng.net_format(1.2345678, 2) == "1.23"


def frame_intake(active="Tiny"):
    preset = {"Name": "Tiny", "ModuleLengthM": 2.384, "ModuleWidthM": 1.303, "ModuleThicknessM": 0.033,
              "ModulePowerWp": 715, "FramingType": "FixedTilt", "Orientation": "Portrait", "Rows": 6, "Columns": 1,
              "TiltDegrees": 20.0, "HorizontalGapM": 0.02, "VerticalGapM": 0.02}
    other = dict(preset, Name="Other", Rows=2)
    return {"format": "frame-information-intake-v1", "units": "m",
            "frame_presets": {"SchemaVersion": 1, "ActiveName": active, "Presets": [other, preset]},
            "park_params": {"AzimuthDeg": 180.0, "ColumnSpacingM": 0.05, "FlatPitchM": 7.5,
                            "FrameOrientation": "Landscape", "MaxPitchM": 8.0, "MinPitchM": 4.5,
                            "ShadingLimitAngleDeg": 25.0},
            "drawing_totals": {"frames": 0, "kwp_total": 0.0, "piles": 0, "frame_types": []}}


def test_frame_information_fields_in_dialog_order():
    rows = eng.frame_rows(frame_intake())
    assert len(rows) == 24
    by_field = {fields["field"]: fields["value"] for _, fields in rows}
    assert rows[0] == ("frame-info-1", {"group": "active", "field": "preset_name", "value": "Tiny"})
    assert by_field["frame_power_kwp"] == 4.29           # 6 x 1 x 715 / 1000
    assert by_field["frame_orientation"] == "Landscape"
    assert by_field["frame_types"] == "-"
    assert by_field["max_pitch"] == {"kind": "length", "value": 8.0, "unit": "m"}


def test_frame_information_falls_back_to_the_first_preset():
    rows = eng.frame_rows(frame_intake(active="Missing"))
    assert rows[0][1]["value"] == "Other"


def test_frame_information_refuses_a_malformed_intake():
    bad = frame_intake()
    del bad["park_params"]["AzimuthDeg"]
    with pytest.raises(eng.BatchTwoError):
        eng.frame_rows(bad)


def test_deep_search_empty_state_and_refusal():
    assert eng.deep_search_status([]) == [("report-message", {"name": "message", "value": "no-sessions"}),
                                          ("report-sessions", {"name": "sessions", "value": 0})]
    with pytest.raises(eng.BatchTwoError):
        eng.deep_search_status([{"id": 1}])


# --- frame and park settings (f2) ----------------------------------------------------------------------------------

def park_intake(preset):
    return {"frame_presets": {"SchemaVersion": 1, "ActiveName": preset["Name"], "Presets": [preset]}}


def test_f2_ok_commits_when_the_stored_pack_matches_rows_by_columns():
    preset = {"Name": "Tiny", "FramingType": "FixedTilt", "Rows": 6, "Columns": 1,
              "TrackerPack": {"Segments": [{"Kind": "Modules", "Count": 1}] * 6}}
    rows = dict((f["name"], f["value"]) for _, f in eng.frame_park_settings(park_intake(preset), {"ok": 1, "cancel": 1}))
    assert rows == {"active-preset": "Tiny", "committed": True, "pack-check": "passed", "store-changed": False}


def test_f2_ok_is_refused_on_a_pack_that_does_not_match():
    preset = {"Name": "Grown", "FramingType": "FixedTilt", "Rows": 4, "Columns": 24,
              "TrackerPack": {"Segments": [{"Kind": "Modules", "Count": 96}, {"Kind": "Modules", "Count": 96},
                                           {"Kind": "Gap", "Count": 3}]}}
    rows = dict((f["name"], f["value"]) for _, f in eng.frame_park_settings(park_intake(preset), {"ok": 1, "cancel": 1}))
    assert rows["pack-check"] == "failed" and rows["pack-modules"] == 192 and rows["pack-target"] == 96
    assert rows["committed"] is False


def test_f2_default_pack_and_refusals():
    preset = {"Name": "Plain", "FramingType": "FixedTilt", "Rows": 2, "Columns": 3}
    assert eng.tracker_pack_modules(preset) == 6
    with pytest.raises(eng.BatchTwoError):
        eng.frame_park_settings(park_intake(dict(preset, FramingType="SingleAxisTracker")), {"ok": 1, "cancel": 1})
    with pytest.raises(eng.BatchTwoError):
        eng.frame_park_settings(park_intake(preset), {"ok": 1})
    with pytest.raises(eng.BatchTwoError):
        eng.frame_park_settings(park_intake(dict(preset, Rows="2")), {"ok": 1, "cancel": 1})


# --- elevation zone assignment (z2, z3) ----------------------------------------------------------------------------

def assign_intake(zones=None):
    zone = {"name": "Zone 1", "offset": {"kind": "length", "value": 24.0, "unit": "in"}, "colour": 1,
            "strings": [], "panels": []}
    return {"format": "zone-assign-intake-v1", "units": "in",
            "elevation_zones": zones if zones is not None else [zone], "reference_panel": "7FA3",
            "panels": [{"panel": "7FA4", "size": "8.0x4.0", "colour": 7},
                       {"panel": "7FA3", "size": "8.0x4.0", "colour": 1},
                       {"panel": "7FA2", "size": "8.0x5.0", "colour": 7}],
            "strings": ["200", "1F0"]}


def test_assign_panels_takes_the_reference_signature_only():
    intake = assign_intake()
    zones = [eng._zone(z, "in") for z in intake["elevation_zones"]]
    after, recoloured, skipped = eng.zone_assign_panels(intake, zones, "zone 1", "7FA3")
    assert after[0]["panels"] == ["7FA4", "7FA3"] and skipped == 1
    assert recoloured == {"7FA4": 1}                      # 7FA3 already carries the zone colour
    rows = eng.zone_assign_rows(zones, after, recoloured)
    assert [row_id for row_id, _ in rows["elevation-zone"]] == ["elevation-zone-1"]
    assert rows["recoloured"] == [("recoloured-1", {"panel": "7FA4", "colour": 1})]


def test_assigning_moves_handles_out_of_other_zones():
    other = {"name": "Zone 2", "offset": {"kind": "length", "value": 6.0, "unit": "in"}, "colour": 3,
             "strings": ["200"], "panels": ["7FA4"]}
    first = {"name": "Zone 1", "offset": {"kind": "length", "value": 24.0, "unit": "in"}, "colour": 1,
             "strings": [], "panels": []}
    intake = assign_intake([first, other])
    zones = [eng._zone(z, "in") for z in intake["elevation_zones"]]
    after, _, _ = eng.zone_assign_panels(intake, zones, "Zone 1", "7FA3")
    assert after[1]["panels"] == [] and after[0]["panels"] == ["7FA4", "7FA3"]
    strings = eng.zone_assign_strings(intake, after, "Zone 1")
    assert strings[0]["strings"] == ["200", "1F0"] and strings[1]["strings"] == []


def test_assign_refuses_a_missing_zone_or_reference():
    intake = assign_intake()
    zones = [eng._zone(z, "in") for z in intake["elevation_zones"]]
    with pytest.raises(eng.BatchTwoError):
        eng.zone_assign_panels(intake, zones, "Zone 9", "7FA3")
    with pytest.raises(eng.BatchTwoError):
        eng.zone_assign_panels(intake, zones, "Zone 1", "ABCD")
    bad = dict(intake, panels=[{"panel": "not-hex", "size": None, "colour": 7}])
    with pytest.raises(eng.BatchTwoError):
        eng.zone_assign_panels(bad, zones, "Zone 1", "7FA3")
