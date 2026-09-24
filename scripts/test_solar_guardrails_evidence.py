"""Tests for scripts/solar_guardrails_evidence.py: g1 and g2 documents from a synthetic intake validate under the
comparator, carry the plugin adapter's row shapes, and refuse missing or malformed intakes."""
import importlib.util
import json
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent / "solar_guardrails_evidence.py"
_SPEC = importlib.util.spec_from_file_location("solar_guardrails_evidence", _PATH)
prod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(prod)

REV = "b" * 40
INTAKE = {
    "format": "guardrails-intake-v1", "units": "in", "rules": [], "devices": [],
    "strings": [{"circuit": "+1/1a", "inverter": 1, "mppt": "a", "panel_count": 14},
                {"circuit": "+2/1b", "inverter": 1, "mppt": "b", "panel_count": 14}],
    "drawing": {"inverter_types": {}, "num_mppt": 3, "panels_in_sequence": 14, "project_latitude": 0.0,
                "project_longitude": 0.0, "project_zip_code_set": False, "strings_per_mppt": 3,
                "use_l2_collectors": False},
    "host_settings": {"Voc": 50.0, "BVoc": -0.13, "Pmp": 500.0, "Vmp": 42.0, "Imp": 12.0, "ModuleSelection": "M",
                      "NumPanelsInSequence": 14, "NumMppt": 2, "StringsPerMppt": 2, "OptimizerModel": "",
                      "UseCombinerBox": False, "InverterSelection": "Maker M10", "SuggestedInverterCount": 1},
    "host_settings_recorded": True,
    "inverter_catalog": {"model_name": "Maker M10", "is_solar_edge": False, "max_dc_power_kw": 15.0,
                         "max_dc_voltage": 1000.0, "min_dc_voltage_feed": 0.0, "mppt_voltage_range_min": 200.0,
                         "mppt_voltage_range_max": 950.0, "num_mppt_trackers": 2, "total_dc_inputs": 4,
                         "max_ac_power_kw": 10.0, "max_ac_current": 0.0},
    "runtime_lists": {"inverter_list_count": 0, "reset_on_document_activation": True},
}


def write_intake(tmp_path, value=INTAKE):
    (tmp_path / "g0-intake.json").write_text(json.dumps(value), encoding="utf-8")
    return tmp_path


def test_both_steps_validate_and_carry_the_adapter_shape(tmp_path):
    docs = prod.run_steps(write_intake(tmp_path), REV)
    assert sorted(docs) == ["g1", "g2"]
    for step, doc in docs.items():
        prod.compare.validate_evidence(doc, "exports")
        rows = doc["after"]["rows"]
        assert doc["after"]["source_revision"] == step and doc["after"]["format"] == "batch2-v1"
        assert [row["type"] for row in rows] == sorted(row["type"] for row in rows)
        assert all(row["quantity"] == 1 and row["unit"] == "each" for row in rows)
        assert doc["provenance"]["list_mode"] == "drawing"
        assert doc["fixture_sha256"] == prod.compare.semantic_hash(INTAKE)
    assert docs["g1"]["after"]["rows"] == [dict(r) for r in docs["g2"]["after"]["rows"]]


def test_the_report_counts_match_the_verdicts(tmp_path):
    doc = prod.run_steps(write_intake(tmp_path), REV, "g1")["g1"]
    verdicts = [row for row in doc["after"]["rows"] if row["type"] == "verdict"]
    counts = {row["name"]: row["value"] for row in doc["after"]["rows"] if row["type"] == "report"}
    assert counts["pass-count"] == sum(1 for row in verdicts if row["status"] == "pass")
    assert counts["status"] in ("HEALTHY", "WARNINGS", "ERRORS DETECTED", "CRITICAL")


def test_main_writes_both_files(tmp_path, capsys):
    out = tmp_path / "out"
    assert prod.main(["--intakes", str(write_intake(tmp_path)), "--out", str(out), "--revision", REV]) == 0
    assert sorted(p.name for p in out.iterdir()) == ["g1.json", "g2.json"]
    assert "guardrails-monitoring" in capsys.readouterr().out


def test_missing_or_malformed_intakes_refuse(tmp_path):
    with pytest.raises(prod.EvidenceError):
        prod.run_steps(tmp_path)
    (tmp_path / "g0-intake.json").write_text("{", encoding="utf-8")
    with pytest.raises(prod.EvidenceError):
        prod.run_steps(tmp_path)
    write_intake(tmp_path, dict(INTAKE, host_settings_recorded=False))
    with pytest.raises(prod.EvidenceError):
        prod.run_steps(tmp_path)


def test_a_bad_revision_refuses(tmp_path):
    with pytest.raises(prod.EvidenceError):
        prod.run_steps(write_intake(tmp_path), "HEAD")
