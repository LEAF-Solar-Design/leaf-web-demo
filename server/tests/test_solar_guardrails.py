"""Tests for server/solar_guardrails.py (G36 guardrails: the plugin's 14 rules, display order and health banner)."""
import copy
import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "solar_guardrails.py"
_SPEC = importlib.util.spec_from_file_location("solar_guardrails", _PATH)
eng = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(eng)

HOST = {"Voc": 52.58, "BVoc": -0.13145, "Pmp": 595.0, "Vmp": 44.64, "Imp": 13.33, "ModuleSelection": "Module X",
        "NumPanelsInSequence": 14, "NumMppt": 12, "StringsPerMppt": 2, "OptimizerModel": "", "UseCombinerBox": False,
        "InverterSelection": "Maker Series M250", "SuggestedInverterCount": 5}
CATALOG = {"model_name": "Maker Series M250", "is_solar_edge": False, "max_dc_power_kw": 375.0,
           "max_dc_voltage": 1500.0, "min_dc_voltage_feed": 0.0, "mppt_voltage_range_min": 500.0,
           "mppt_voltage_range_max": 1500.0, "num_mppt_trackers": 12, "total_dc_inputs": 24,
           "max_ac_power_kw": 250.0, "max_ac_current": 0.0}
# The plugin's captured g1 order: 13 pass and 2 info, HEALTHY, with its in-memory inverter list empty.
PLUGIN_ORDER = ["NEC-690.7-VOC-COLD", "NEC-690.8A-ISC", "MPPT-WINDOW", "MPPT-WINDOW", "NEC-690.9-OCPD",
                "NEC-690.7-VOC-COLD-OPTIMIZER", "HW-OPTI-COMPAT", "HW-STRING-DELTA", "HW-OPTI-FAMILY",
                "DESIGN-DC-AC", "DESIGN-STRINGS-MPPT", "DESIGN-MPPT-BAL", "SAFE-NULL-STATE", "SAFE-DIV-ZERO",
                "SAFE-DB-VALUES"]


def string(inverter, letter, panels=14):
    return {"circuit": f"+1/{inverter}{letter}", "inverter": inverter, "mppt": letter, "panel_count": panels}


def intake(strings=None, host=None, catalog=CATALOG, lat=0.0, lon=0.0):
    return {"format": "guardrails-intake-v1", "units": "in", "rules": [], "devices": [],
            "strings": strings if strings is not None else [string(1, "a"), string(1, "a"), string(1, "b"),
                                                            string(1, "b")],
            "drawing": {"inverter_types": {}, "num_mppt": 3, "panels_in_sequence": 14, "project_latitude": lat,
                        "project_longitude": lon, "project_zip_code_set": False, "strings_per_mppt": 3,
                        "use_l2_collectors": False},
            "host_settings": dict(host or HOST), "host_settings_recorded": True,
            "inverter_catalog": copy.deepcopy(catalog),
            "runtime_lists": {"inverter_list_count": 0, "reset_on_document_activation": True}}


def statuses(rows):
    return [(fields["rule_id"], fields["status"]) for _, fields in rows["verdict"]]


def report(rows):
    return {fields["name"]: fields["value"] for _, fields in rows["report"]}


def test_plugin_mode_reproduces_the_captured_verdicts():
    rows = eng.guardrail_rows(intake(), "plugin")
    assert [rule for rule, _ in statuses(rows)] == PLUGIN_ORDER
    assert [s for _, s in statuses(rows)].count("info") == 2
    assert dict(statuses(rows))["DESIGN-DC-AC"] == "info" and dict(statuses(rows))["DESIGN-STRINGS-MPPT"] == "info"
    assert report(rows) == {"status": "HEALTHY", "info-count": 2, "pass-count": 13}
    assert [row_id for row_id, _ in rows["report"]] == ["report-info-count", "report-pass-count", "report-status"]


def test_drawing_mode_validates_the_assigned_inverters():
    rows = eng.guardrail_rows(intake(), "drawing")
    got = dict(statuses(rows))
    assert got["DESIGN-STRINGS-MPPT"] == "pass" and got["DESIGN-MPPT-BAL"] == "pass"
    assert got["DESIGN-DC-AC"] == "warning"      # 4 x 14 x 595 W = 33.3 kW DC against one 250 kW inverter
    assert report(rows)["status"] == "WARNINGS"


def test_strings_over_mppt_capacity_are_errors_and_sort_first_in_their_section():
    strings = [string(1, "a") for _ in range(3)] + [string(1, "b")]
    rows = eng.guardrail_rows(intake(strings), "drawing")
    design = [(rule, s) for rule, s in statuses(rows) if rule.startswith("DESIGN")]
    assert design[0] == ("DESIGN-STRINGS-MPPT", "error")
    assert ("DESIGN-MPPT-BAL", "warning") in design           # 42 against 14 panels is a 66.7% imbalance
    assert report(rows)["status"] == "ERRORS DETECTED" and report(rows)["error-count"] == 1


def test_cold_voc_goes_critical_past_the_inverter_limit():
    host = dict(HOST, NumPanelsInSequence=30)
    rows = eng.guardrail_rows(intake(host=host), "plugin")
    assert statuses(rows)[0] == ("NEC-690.7-VOC-COLD", "critical")
    assert report(rows)["status"] == "CRITICAL" and list(report(rows))[0] == "critical-count"


def test_design_temperature_follows_the_plugin_lookup():
    assert eng.build_snapshot(intake(), "plugin")["min_temp_c"] == -40.0
    assert "DesignMinTempC" in eng.build_snapshot(intake(), "plugin")["synthetic"]
    assert eng.build_snapshot(intake(lat=41.0, lon=-71.0), "plugin")["min_temp_c"] == -18.0
    assert eng.build_snapshot(intake(lat=10.0, lon=200.0), "plugin")["min_temp_c"] == 5.0   # longitude unchecked
    assert [eng.min_temp_c(lat, 1.0) for lat in (60, 50, 45, 40, 35, 30, 23.5, 0)] == [
        -40.0, -32.0, -25.0, -18.0, -10.0, -5.0, 0.0, 5.0]


def test_mppt_letter_is_the_circuits_last_lowercase_letter():
    assert eng.mppt_letter("+1/1c") == "c"
    assert eng.mppt_letter("+1/1C") == "a" and eng.mppt_letter("") == "a" and eng.mppt_letter(None) == "a"


def test_without_a_catalog_row_the_type_comes_from_settings():
    rows = eng.guardrail_rows(intake(catalog=None), "plugin")
    got = statuses(rows)
    assert ("MPPT-WINDOW", "pass") not in got                 # no window data: the rule emits nothing for the type
    assert dict(got)["NEC-690.7-VOC-COLD"] == "info"          # no MaxDcVoltage configured
    assert dict(got)["DESIGN-DC-AC"] == "info"                # no AC power


def test_database_value_contradictions_warn():
    catalog = dict(CATALOG, mppt_voltage_range_min=1600.0)
    assert dict(statuses(eng.guardrail_rows(intake(catalog=catalog), "plugin")))["SAFE-DB-VALUES"] == "warning"


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(host_settings_recorded=False),
    lambda d: d.update(strings="nope"),
    lambda d: d["drawing"].update(inverter_types={"A": {}}),
    lambda d: d["drawing"].update(project_zip_code_set=True),
    lambda d: d.update(drawing=None),
])
def test_malformed_or_uncarried_intakes_refuse(mutate):
    doc = intake()
    mutate(doc)
    with pytest.raises(eng.GuardrailError):
        eng.guardrail_rows(doc, "drawing")


def test_unknown_mode_refuses():
    with pytest.raises(eng.GuardrailError):
        eng.guardrail_rows(intake(), "memory")
