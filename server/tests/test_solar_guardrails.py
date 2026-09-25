"""Tests for server/solar_guardrails.py (G36 guardrails: the plugin's 14 rules, display order and health banner)."""
import copy
import importlib.util
import json
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


def test_plugin_mode_reads_only_l1_devices():
    doc = intake()
    doc["devices"] = [{"is_l2": True, "role": "l2-inverter", "type_key": "A"}] * 3
    assert report(eng.guardrail_rows(doc, "plugin"))["status"] == "HEALTHY"   # every device is an L2: L1 list empty
    doc["devices"].append({"is_l2": False, "role": "inverter", "type_key": "A"})
    with pytest.raises(eng.GuardrailError):
        eng.guardrail_rows(doc, "plugin")                                        # the runtime count says 0, one L1
    doc["runtime_lists"]["inverter_list_count"] = 1
    with pytest.raises(eng.GuardrailError):
        eng.guardrail_rows(doc, "plugin")                                        # a non-empty L1 list is not carried


def test_unknown_mode_refuses():
    with pytest.raises(eng.GuardrailError):
        eng.guardrail_rows(intake(), "memory")


def test_committed_intake_links_no_string_by_device_number():
    # The committed g0 intake records the i19 device numbers (9..31); its string circuits name 1, 2, 5, 6 and 7,
    # which step i18's fixed-L2 adoption renumbered away (Solar residuals R33), so the plugin's collector links no
    # string and the captured palette is 1 warning (DC/AC fallback), 1 info, 13 pass (receipt batch2-g1).
    path = _PATH.parents[1] / "docs/parity/evidence/batch2/g0-intake.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["device_numbers_recorded"] is True
    assert sorted(device["number"] for device in doc["devices"]) == list(range(9, 32))
    assert len(doc["strings"]) == 173 and sum(s["panel_count"] for s in doc["strings"]) == 2345
    snapshot = eng.build_snapshot(doc, "drawing")
    assert snapshot["strings"] == []
    assert snapshot["inverter_count"] == 23
    assert snapshot["dc_inputs_per_mppt"] == 2
    assert eng.dc_ac_ratio(snapshot) == [eng.WARNING]
    assert eng.strings_per_mppt(snapshot) == [eng.PASS]
    assert eng.mppt_balance(snapshot) == [eng.INFO]
    rows = eng.guardrail_rows(doc, "drawing")
    assert report(rows) == {"status": "WARNINGS", "warning-count": 1, "info-count": 1, "pass-count": 13}
    assert report(eng.guardrail_rows(doc, "plugin")) == {
        "status": "HEALTHY", "info-count": 2, "pass-count": 13}


def numbered(numbers, strings):
    doc = intake(strings)
    doc["device_numbers_recorded"] = True
    doc["devices"] = [{"is_l2": True, "role": "l2-inverter", "type_key": "A", "number": n} for n in numbers]
    return doc


def test_recorded_devices_with_no_matching_strings_take_the_no_strings_fallback():
    snapshot = eng.build_snapshot(numbered([9, 10], [string(1, "a"), string(2, "b")]), "drawing")
    assert [summary["number"] for summary in snapshot["summaries"]] == [9, 10]
    assert all(summary["strings_by_mppt"] == {} for summary in snapshot["summaries"])
    assert snapshot["strings"] == [] and snapshot["inverter_count"] == 2
    assert eng.dc_ac_ratio(snapshot) == [eng.WARNING]       # 2 x 12 x 2 x 14 x 595 W = 399.8 kW against 500 kW
    assert eng.strings_per_mppt(snapshot) == [eng.PASS]
    assert eng.mppt_balance(snapshot) == [eng.INFO]


def test_recorded_devices_follow_intake_order_and_link_strings_by_number():
    strings = [string(1, "a"), string(3, "a"), string(1, "b"), string(3, "b", panels=12)]
    snapshot = eng.build_snapshot(numbered([3, 1, 4], strings), "drawing")
    assert [summary["number"] for summary in snapshot["summaries"]] == [3, 1, 4]
    assert snapshot["summaries"][0]["strings_by_mppt"] == {"a": [14], "b": [12]}
    assert snapshot["summaries"][2]["strings_by_mppt"] == {}
    assert [(item["inverter"], item["mppt"]) for item in snapshot["strings"]] == [(3, "a"), (3, "b"), (1, "a"), (1, "b")]
    assert snapshot["inverter_count"] == 3


def test_two_devices_sharing_a_number_both_get_the_strings():
    snapshot = eng.build_snapshot(numbered([5, 5], [string(5, "a"), string(5, "b")]), "drawing")
    assert [summary["strings_by_mppt"] for summary in snapshot["summaries"]] == [{"a": [14], "b": [14]}] * 2
    assert len(snapshot["strings"]) == 4 and snapshot["inverter_count"] == 2


def test_a_string_naming_no_device_adds_nothing():
    snapshot = eng.build_snapshot(numbered([1], [string(1, "a"), string(9, "a"), string(9, "b")]), "drawing")
    assert len(snapshot["summaries"]) == 1
    assert sum(item["panel_count"] for item in snapshot["strings"]) == 14


def test_unrecorded_device_numbers_keep_the_string_derived_summaries():
    strings = [string(2, "a"), string(1, "a"), string(1, "b")]
    base = eng.build_snapshot(intake(strings), "drawing")
    for flag in (None, False):
        doc = numbered([7, 8, 9], strings)
        if flag is None:
            del doc["device_numbers_recorded"]
        else:
            doc["device_numbers_recorded"] = flag
        snapshot = eng.build_snapshot(doc, "drawing")
        assert [summary["number"] for summary in snapshot["summaries"]] == [1, 2]
        assert snapshot["summaries"] == base["summaries"] and snapshot["strings"] == base["strings"]
        assert snapshot["inverter_count"] == 2


@pytest.mark.parametrize("mutate", [
    lambda d: d["devices"][0].update(number=-1),
    lambda d: d["devices"][0].update(number="3"),
    lambda d: d["devices"][0].update(number=True),
    lambda d: d["devices"][0].update(number=2.0),
    lambda d: d["devices"][0].pop("number"),
    lambda d: d["devices"].append(None),
    lambda d: d.update(device_numbers_recorded="yes"),
])
def test_malformed_recorded_device_numbers_refuse(mutate):
    doc = numbered([1, 2], [string(1, "a")])
    mutate(doc)
    with pytest.raises(eng.GuardrailError):
        eng.guardrail_rows(doc, "drawing")


def test_committed_intake_with_recorded_device_numbers_links_no_string():
    # R34: the fixture's 23 devices carry numbers 9..31 while its strings name 1, 2, 5, 6 and 7.
    path = _PATH.parents[1] / "docs/parity/evidence/batch2/g0-intake.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["device_numbers_recorded"] = True
    for number, device in zip(range(9, 32), doc["devices"]):
        device["number"] = number
    snapshot = eng.build_snapshot(doc, "drawing")
    assert snapshot["inverter_count"] == 23 and snapshot["strings"] == []
    assert report(eng.guardrail_rows(doc, "drawing")) == {
        "status": "WARNINGS", "warning-count": 1, "info-count": 1, "pass-count": 13}


def test_empty_string_buckets_do_not_explain_clean_host_plugin_verdicts():
    # R31b: missing mStrings alone cannot yield INFO / INFO / PASS.
    snapshot = eng.build_snapshot(intake(), "drawing")
    snapshot["strings"] = []
    snapshot["summaries"][0]["strings_by_mppt"] = {}
    assert eng.dc_ac_ratio(snapshot) == [eng.WARNING]  # positive count enables the fallback
    assert eng.strings_per_mppt(snapshot) == [eng.PASS]  # capacity is known
    assert eng.mppt_balance(snapshot) == [eng.INFO]     # zero buckets is single-MPPT
    snapshot["summaries"] = []
    snapshot["inverter_count"] = 0
    assert eng.dc_ac_ratio(snapshot) == [eng.INFO]
    assert eng.strings_per_mppt(snapshot) == [eng.INFO]
    assert eng.mppt_balance(snapshot) == [eng.PASS]
