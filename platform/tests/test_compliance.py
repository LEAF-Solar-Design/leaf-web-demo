import pytest

from leaf_platform import compliance


PACK = {
    "pack_id": "leaf.electrical.narrow-v1", "edition": "NEC 2023",
    "version": "0.1.0", "status": "candidate",
    "rules": [
        {"rule_id": "NEC-690.7-COLD-VOC", "rule_type": "max_cold_string_voltage",
         "citation": {"authority": "NEC", "edition": "2023", "section": "690.7(A)(3)"},
         "inputs": {"modules_in_series": "modules", "module_voc_stc_v": "voc",
                    "beta_voc_per_c": "beta", "minimum_temperature_c": "min_temp",
                    "inverter_max_dc_voltage_v": "max_voltage"}},
        {"rule_id": "NEC-690.8-SOURCE-CURRENT", "rule_type": "min_source_circuit_ampacity",
         "citation": {"authority": "NEC", "edition": "2023", "section": "690.8(A)"},
         "continuous_factor": "1.25",
         "inputs": {"parallel_strings": "strings", "module_isc_a": "isc",
                    "conductor_ampacity_a": "ampacity"}},
    ],
}

INPUTS = {"modules": 20, "voc": "50", "beta": "-0.0028", "min_temp": "-10",
          "max_voltage": "1000", "strings": 2, "isc": "13", "ampacity": "30"}


def test_candidate_pack_produces_typed_deterministic_advisories():
    first = compliance.evaluate(PACK, INPUTS, standards_snapshot_id="standards-1",
                                ahj_snapshot_id="ahj-1")
    second = compliance.evaluate(PACK, dict(reversed(list(INPUTS.items()))),
                                 standards_snapshot_id="standards-1", ahj_snapshot_id="ahj-1")
    assert first == second
    assert [item["rule_id"] for item in first] == sorted(item["rule_id"] for item in first)
    voltage = next(item for item in first if item["rule_type"] == "max_cold_string_voltage")
    assert voltage["actual"] == "1098"
    assert voltage["limit"] == "1000"
    assert voltage["result"] == "fail"
    assert voltage["effect"] == "advisory"
    assert voltage["citation"]["section"] == "690.7(A)(3)"
    current = next(item for item in first if item["rule_type"] == "min_source_circuit_ampacity")
    assert current["limit"] == "32.5"
    assert current["actual"] == "30"
    assert current["result"] == "fail"


def test_only_ratified_pack_plus_authoritative_ahj_is_binding():
    pack = {**PACK, "status": "ratified"}
    unknown = compliance.evaluate(pack, INPUTS, standards_snapshot_id="s", ahj_snapshot_id="a")
    binding = compliance.evaluate(pack, INPUTS, standards_snapshot_id="s", ahj_snapshot_id="a",
                                  ahj_authority="authoritative")
    assert all(item["effect"] == "advisory" for item in unknown)
    assert all(item["effect"] == "binding" for item in binding)


@pytest.mark.parametrize("mutation,match", [
    ({"rules": []}, "at least one rule"),
    ({"status": "accepted"}, "candidate or ratified"),
])
def test_invalid_pack_fails_closed(mutation, match):
    with pytest.raises(ValueError, match=match):
        compliance.evaluate({**PACK, **mutation}, INPUTS,
                            standards_snapshot_id="s", ahj_snapshot_id="a")


def test_missing_or_nonfinite_inputs_fail_closed():
    with pytest.raises(ValueError, match="available minimum_temperature_c"):
        compliance.evaluate(PACK, {k: v for k, v in INPUTS.items() if k != "min_temp"},
                            standards_snapshot_id="s", ahj_snapshot_id="a")
    with pytest.raises(ValueError, match="finite"):
        compliance.evaluate(PACK, {**INPUTS, "isc": "NaN"},
                            standards_snapshot_id="s", ahj_snapshot_id="a")
