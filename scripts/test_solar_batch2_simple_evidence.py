"""Tests for scripts/solar_batch2_simple_evidence.py: documents from synthetic intakes validate under the
comparator, carry the plugin adapter's parameters and row shapes, and refuse missing or malformed intakes."""
import importlib.util
import json
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent / "solar_batch2_simple_evidence.py"
_SPEC = importlib.util.spec_from_file_location("solar_batch2_simple_evidence", _PATH)
prod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(prod)

REV = "a" * 40


SIZER_REQUEST = {"zip_code": "78701", "module_name": "Canadian_Solar_Inc__CS5T_130M",
                 "full_inverter_name": "Sungrow SG-HX SG250HX", "bifacial": False, "bifacial_coefficient": ".7",
                 "max_voltage": "1500", "thermal_model_type": "close mount glass glass", "open_circuit_rise": False,
                 "racking_params": {"albedo": ".25", "racking_type": "fixed_tilt", "surface_tilt": "5",
                                    "surface_azimuth": "180"}}
SIZER_SETTINGS = {"PanelsInSequence": 15, "VocColdPasses": None, "VocColdOverrideAccepted": False,
                  "VocColdSuggestedStringLength": 0, "VocColdPerModule": 0.0, "VocColdStringVoltage": 0.0,
                  "VocColdMaxDcVoltage": 0.0}


def write_intakes(tmp_path):
    intakes = {
        "z0-state.json": {"format": "zone-height-state-v1", "units": "in", "installation_design": "Roof",
                          "elevation_zones": [], "string_layer": "String"},
        "s1-markers.json": {"format": "heatmap-markers-v1", "units": "m", "marker_count": 2,
                            "markers_with_stored_hits": 0,
                            "markers": [{"has_stored_hits": False}, {"has_stored_hits": False}]},
        "s4-state.json": {"format": "shade-heatmap-state-v1", "shade_loss_heatmap_applied": False,
                          "shade_loss_per_module": {}},
        "f0-intake.json": {"format": "frame-information-intake-v1", "units": "m",
                           "frame_presets": {"SchemaVersion": 1, "ActiveName": "P", "Presets": [
                               {"Name": "P", "ModuleLengthM": 2.0, "ModuleWidthM": 1.0, "ModuleThicknessM": 0.03,
                                "ModulePowerWp": 500, "FramingType": "FixedTilt", "Orientation": "Portrait",
                                "Rows": 2, "Columns": 3, "TiltDegrees": 25.0, "HorizontalGapM": 0.02,
                                "VerticalGapM": 0.02}]},
                           "park_params": {"AzimuthDeg": 180.0, "ColumnSpacingM": 0.05, "FlatPitchM": 7.5,
                                           "FrameOrientation": "Portrait", "MaxPitchM": 8.0, "MinPitchM": 4.5,
                                           "ShadingLimitAngleDeg": 25.0},
                           "drawing_totals": {"frames": 0, "kwp_total": 0.0, "piles": 0, "frame_types": []}},
    }
    intakes["z1-assign.json"] = {
        "format": "zone-assign-intake-v1", "units": "in", "reference_panel": "7FA3",
        "elevation_zones": [{"name": "Zone 1", "offset": {"kind": "length", "value": 24.0, "unit": "in"},
                             "colour": 1, "strings": [], "panels": []}],
        "panels": [{"panel": "7FA4", "size": "8.0x4.0", "colour": 7}, {"panel": "7FA3", "size": "8.0x4.0", "colour": 7}],
        "strings": ["200"]}
    intakes["k0-intake.json"] = {"request": SIZER_REQUEST, "settings": dict(SIZER_SETTINGS)}
    intakes["k1-response.json"] = {"status": 200, "content_type": "application/json",
                                   "body_text": json.dumps(json.dumps({"cells": 60}))}
    intakes["k2-intake.json"] = {"request": SIZER_REQUEST, "settings": dict(SIZER_SETTINGS)}
    intakes["k2-response.json"] = {"status": 200, "content_type": "application/json", "body_text": json.dumps({
        "cells": 60, "voc": 40.0, "bvoc": -0.4, "isc": 5.0, "pmp": 130.0, "vmp": 30.0,
        "imp": 4.0, "bpmp": -0.6, "alpha_sc": 0.002,
        "simulation_results": {"standard": {"Conditions": "Synthetic", "max_module_voltage": 40.0,
                                            "string_design_voltage": 1500, "string_length": 41.0}}})}
    intakes["m0-intake.json"] = {"panels": [{"handle": h, "x": 10.0 * i, "y": 0.0}
                                            for i, h in enumerate(["8D3A", "A1", "A2", "8D9D"])],
                                 "half_extents": [4.0, 2.0], "diagonal": 8.944272}
    intakes["o0-intake.json"] = {"file_name": "customer_review.dwg", "opened_via_command": True,
                                 "settings": {"ProjectName": "", "ProjectZipCode": "", "InstallationDesign": "Roof",
                                              "LeafProjectCanceled": False, "CustomerWelcomeDismissed": False},
                                 "scan": {"pvcase_area_entities": 0, "pvcase_tracker_blocks": 0,
                                          "pvcase_xdata_blocks": 0, "branch_tracker_polylines": 0}}
    intakes["p0-intake.json"] = {"bound": False, "origin_overridden": False}
    for name, value in intakes.items():
        (tmp_path / name).write_text(json.dumps(value), encoding="utf-8")
    return tmp_path


def test_every_step_builds_a_valid_document(tmp_path):
    docs = prod.run_steps(write_intakes(tmp_path), REV)
    assert set(docs) == {"z1", "z2", "z3", "s2", "s3", "s4", "f1", "f2", "q1", "m1", "o1", "k1", "k2", "p1"}
    for step, doc in docs.items():
        prod.compare.validate_evidence(doc, "exports")
        assert doc["after"]["source_revision"] == step and doc["after"]["format"] == "batch2-v1"
        assert doc["state"] == "committed" and doc["revision"] == REV
    assert docs["z1"]["parameters"] == {"answers": [], "form_values": {"add": 1, "offset_in": 24}}
    assert docs["s2"]["parameters"] == {"answers": []}
    assert docs["z1"]["units"] == "in" and docs["f1"]["units"] == "m"


def test_rows_follow_the_plugin_shape(tmp_path):
    docs = prod.run_steps(write_intakes(tmp_path), REV)
    report = docs["s2"]["after"]["rows"]
    assert [row["id"]["entity_id"] for row in report] == ["report-heatmap-markers", "report-message"]
    assert report[0] == {"id": {"entity_id": "report-heatmap-markers"}, "type": "report", "quantity": 1,
                         "unit": "each", "name": "heatmap-markers", "value": 2}
    frame = docs["f1"]["after"]["rows"]
    assert [row["id"]["entity_id"] for row in frame][:2] == ["frame-info-1", "frame-info-2"]
    assert frame[12]["field"] == "frame_power_kwp" and frame[12]["value"] == 3.0
    assert docs["f1"]["entity_mapping"] == {r["id"]["entity_id"]: r["id"]["entity_id"] for r in frame}


def test_fixture_is_the_intake_hash_and_q1_uses_the_adapter_marker(tmp_path):
    root = write_intakes(tmp_path)
    docs = prod.run_steps(root, REV)
    z0 = json.loads((root / "z0-state.json").read_text(encoding="utf-8"))
    assert docs["z1"]["fixture_sha256"] == prod.compare.semantic_hash(z0)
    assert docs["q1"]["fixture_sha256"] == prod.compare.semantic_hash({"deep_search_sessions": None})


def test_missing_or_malformed_intakes_refuse(tmp_path):
    root = write_intakes(tmp_path)
    (root / "s4-state.json").unlink()
    with pytest.raises(prod.EvidenceError):
        prod.run_steps(root, REV)
    root = write_intakes(tmp_path)
    (root / "z0-state.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(prod.EvidenceError):
        prod.run_steps(root, REV, only="z1")
    with pytest.raises(prod.EvidenceError):
        prod.run_steps(write_intakes(tmp_path), "not-a-sha", only="s3")


def test_cli_writes_each_step(tmp_path):
    out = tmp_path / "out"
    assert prod.main(["--intakes", str(write_intakes(tmp_path)), "--out", str(out), "--revision", REV]) == 0
    assert sorted(p.name for p in out.iterdir()) == ["f1.json", "f2.json", "k1.json", "k2.json", "m1.json", "o1.json", "p1.json", "q1.json", "s2.json", "s3.json", "s4.json", "z1.json", "z2.json", "z3.json"]


def test_f2_reports_the_ok_on_the_active_preset(tmp_path):
    doc = prod.run_steps(write_intakes(tmp_path), REV, "f2")["f2"]
    values = {row["name"]: row["value"] for row in doc["after"]["rows"]}
    assert values["active-preset"] == "P" and values["store-changed"] is False
    assert doc["parameters"] == {"answers": [], "form_values": {"ok": 1, "cancel": 1}}


def test_zone_assign_steps_chain_z3_on_studio_z2(tmp_path):
    docs = prod.run_steps(write_intakes(tmp_path), REV)
    z2, z3 = docs["z2"], docs["z3"]
    assert z2["parameters"]["answers"] == ["handle:7FA3", "ALL", ""]
    kinds = [row["type"] for row in z2["after"]["rows"]]
    assert kinds.count("recoloured") == 2 and kinds.count("elevation-zone") == 1
    zone = [row for row in z3["after"]["rows"] if row["type"] == "elevation-zone"][0]
    assert zone["panels"] == ["7FA4", "7FA3"] and zone["strings"] == ["200"]
    assert z2["fixture_sha256"] == z3["fixture_sha256"]


def test_k1_reports_the_refused_double_encoded_response(tmp_path):
    doc = prod.run_steps(write_intakes(tmp_path), REV, "k1")["k1"]
    values = {row["name"]: row["value"] for row in doc["after"]["rows"]}
    assert values == {"calculation": "failed", "error": "response-not-an-object"}
    assert doc["parameters"]["form_values"]["zip_code"] == "78701"


def test_k2_commits_synthetic_shorter_string(tmp_path):
    doc = prod.run_steps(write_intakes(tmp_path), REV, "k2")["k2"]
    values = {row["name"]: row["value"] for row in doc["after"]["rows"]}
    assert values == {"calculation": "succeeded", "PanelsInSequence": 34, "VocColdPasses": True,
                      "VocColdPerModule": 44.0, "VocColdStringVoltage": 1496.0, "VocColdMaxDcVoltage": 1500.0}
    assert doc["parameters"] == {"answers": [], "form_values": {
        "zip_code": "78701", "module": "Canadian Solar Inc  CS5T 130M", "inverter": "Sungrow SG-HX SG250HX",
        "array_type": "Fixed Tilt", "thermal_model": "close mount glass glass", "tilt": "5", "azimuth": "180",
        "calculate": 1, "design_standard": "standard", "voc_cold_resolution": "pick-shorter", "result_form": "Close"}}


def test_k2_committed_intakes_match_plugin_settings_exactly():
    doc = prod.run_steps(prod.DEFAULT_INTAKES, REV, "k2")["k2"]
    prod.compare.validate_evidence(doc, "exports")
    rows = doc["after"]["rows"]
    assert [(r["name"], r["value"]) for r in rows if r["type"] == "report"] == [("calculation", "succeeded")]
    assert {r["name"]: r["value"] for r in rows if r["type"] == "setting"} == {
        "PanelsInSequence": 39, "VocColdMaxDcVoltage": 1500.0, "VocColdPasses": True,
        "VocColdPerModule": 37.505023875, "VocColdStringVoltage": 1462.695931125}


def test_m1_and_o1_rows(tmp_path):
    docs = prod.run_steps(write_intakes(tmp_path), REV)
    (row,) = docs["m1"]["after"]["rows"]
    assert row["panels"] == ["8D3A", "A1", "A2", "8D9D"] and row["label_index"] == 2 and row["label_text"] == "MID"
    assert docs["m1"]["parameters"] == {"answers": ["handle:8D3A", "handle:8D9D"]}
    assert [(r["name"], r["value"]) for r in docs["o1"]["after"]["rows"]] == [("ProjectName", "Customer Review")]


def test_p1_commits_nothing_over_the_platform_intake(tmp_path):
    doc = prod.run_steps(write_intakes(tmp_path), REV, "p1")["p1"]
    assert doc["after"]["rows"] == [] and doc["entity_mapping"] == {}
    assert doc["provenance"]["capability"] == "leaf-platform-webview"
    assert doc["fixture_sha256"] == prod.compare.semantic_hash({"bound": False, "origin_overridden": False})
