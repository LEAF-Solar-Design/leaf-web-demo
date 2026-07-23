import copy

import pytest

from solver_adapters import elec_estimate


VALID_INPUT = {
    "drawing_id": "demo",
    "dwg_version": 1,
    "modules_per_string": 12,
    "string_count": 2,
    "module": {"watts": 550, "voc": 50, "vmp": 42, "isc": 10,
               "temperature_coefficient_pct_per_c": -0.3},
    "inverter": {"architecture": "central", "topology": "combined_input",
                 "mppt_min_v": 300, "mppt_max_v": 800, "max_dc_voltage": 1000,
                 "max_dc_input_a": 30, "optimizer_max_input_isc": None,
                 "optimizer_max_input_voltage": None,
                 "design_min_temp_c": -10, "design_max_temp_c": 70},
    "rate_card": None,
}


def _pinned(params):
    return {**params, "expected_adapter_sha256": elec_estimate._adapter_sha256()}


def _resolver(drawing_id, version):
    assert (drawing_id, version) == ("demo", 1)
    return {"version": 1, "intake": {"entities": []}}


def _item(result, rule):
    return next(item for item in result["solver_result"]["checks"]["items"] if item["rule"] == rule)


def test_elec_estimate_is_deterministic_and_never_claims_intake_provenance():
    first = elec_estimate.run(_pinned(VALID_INPUT), intake_resolver=_resolver)
    second = elec_estimate.run(_pinned(VALID_INPUT), intake_resolver=_resolver)
    assert first == second
    assert first["solver"] == "elec-estimate"
    assert first["solver_result"]["engine_tier"] == "elec_calc-python"
    assert first["solver_result"]["sizing"]["system_wdc"] == 13200.0
    assert first["solver_result"]["checks"]["failed"] == 0
    assert first["solver_result"]["checks"]["needs_review"] == 0
    assert "intake_sha256" not in first


def test_cold_voc_fails_against_max_dc_voltage_not_higher_mppt_maximum():
    candidate = copy.deepcopy(VALID_INPUT)
    candidate["inverter"]["max_dc_voltage"] = 620
    candidate["inverter"]["mppt_max_v"] = 800
    result = elec_estimate.run(_pinned(candidate), intake_resolver=_resolver)
    cold = _item(result, "NEC-690.7-COLD-VOC")
    assert cold["status"] == "fail"
    assert cold["measured"] > cold["limit"]
    assert _item(result, "MPPT-COLD-VOC")["status"] == "pass"


def test_nonphysical_negative_cold_voc_from_garbage_coefficient_fails_closed_not_pass():
    # A pathological coefficient/temperature that drives the correction factor
    # nonpositive must NOT sail past a `<= max_dc_voltage` check as a false PASS.
    candidate = copy.deepcopy(VALID_INPUT)
    candidate["module"]["temperature_coefficient_pct_per_c"] = -10.0
    candidate["inverter"]["design_min_temp_c"] = 40.0  # not the coldest ambient; produces factor < 0
    candidate["inverter"]["max_dc_voltage"] = 1000
    result = elec_estimate.run(_pinned(candidate), intake_resolver=_resolver)
    cold = _item(result, "NEC-690.7-COLD-VOC")
    assert cold["status"] == "insufficient_input"  # fail closed, never "pass" on a negative voltage
    assert cold["status"] != "pass"


def test_solaredge_nonphysical_cold_voc_also_fails_closed():
    candidate = copy.deepcopy(VALID_INPUT)
    candidate["inverter"]["architecture"] = "solaredge"
    candidate["inverter"]["topology"] = None  # solaredge uses per-optimizer limits, not central topology
    candidate["inverter"]["optimizer_max_input_voltage"] = 60
    candidate["module"]["temperature_coefficient_pct_per_c"] = -10.0
    candidate["inverter"]["design_min_temp_c"] = 40.0
    result = elec_estimate.run(_pinned(candidate), intake_resolver=_resolver)
    voc = _item(result, "NEC-690.7-VOC-COLD-OPTIMIZER")
    assert voc["status"] in {"insufficient_input", "requires_engineer_review"}
    assert voc["status"] != "pass"


def test_hot_vmp_exact_mppt_minimum_is_a_pass_boundary():
    candidate = copy.deepcopy(VALID_INPUT)
    candidate["inverter"]["mppt_min_v"] = 42 * (1 - 0.003 * (70 - 25)) * 12
    result = elec_estimate.run(_pinned(candidate), intake_resolver=_resolver)
    hot = _item(result, "MPPT-HOT-VMP")
    assert hot["status"] == "pass"
    assert hot["measured"] == hot["limit"]


def test_central_current_is_topology_and_string_count_aware():
    per_string = copy.deepcopy(VALID_INPUT)
    per_string["inverter"]["topology"] = "per_string_inputs"
    per_string["inverter"]["max_dc_input_a"] = 15
    aggregate = copy.deepcopy(per_string)
    aggregate["inverter"]["topology"] = "combined_input"
    assert _item(elec_estimate.run(_pinned(per_string), intake_resolver=_resolver), "NEC-690.8-CONTINUOUS")["status"] == "pass"
    aggregate_check = _item(elec_estimate.run(_pinned(aggregate), intake_resolver=_resolver), "NEC-690.8-CONTINUOUS")
    assert aggregate_check["status"] == "fail"
    assert aggregate_check["measured"] == 25.0


def test_solaredge_never_runs_generic_module_isc_ocpd_and_requires_optimizer_model():
    candidate = copy.deepcopy(VALID_INPUT)
    candidate["inverter"].update({
        "architecture": "solaredge", "topology": "optimizer_per_module",
        "max_dc_voltage": None, "max_dc_input_a": None,
        "optimizer_max_input_isc": None, "optimizer_max_input_voltage": None,
    })
    result = elec_estimate.run(_pinned(candidate), intake_resolver=_resolver)
    assert _item(result, "NEC-690.7-VOC-COLD-OPTIMIZER")["status"] == "requires_engineer_review"
    assert _item(result, "OPTIMIZER-MAX-INPUT-ISC")["status"] == "requires_engineer_review"
    assert _item(result, "NEC-690.9-OCPD")["status"] == "requires_optimizer_model"
    assert result["solver_result"]["checks"]["passed"] == 0


def test_solaredge_optimizer_limits_use_per_module_voc_and_bare_module_isc():
    candidate = copy.deepcopy(VALID_INPUT)
    candidate["inverter"].update({
        "architecture": "solaredge", "topology": "optimizer_per_module",
        "max_dc_voltage": None, "max_dc_input_a": None,
        "optimizer_max_input_isc": 9, "optimizer_max_input_voltage": 50,
    })
    result = elec_estimate.run(_pinned(candidate), intake_resolver=_resolver)
    assert _item(result, "NEC-690.7-VOC-COLD-OPTIMIZER")["status"] == "fail"
    assert _item(result, "OPTIMIZER-MAX-INPUT-ISC")["status"] == "fail"
    assert _item(result, "NEC-690.9-OCPD")["status"] == "requires_optimizer_model"


def test_missing_required_input_is_structured_insufficient_input_not_pass():
    candidate = copy.deepcopy(VALID_INPUT)
    del candidate["inverter"]["max_dc_voltage"]
    result = elec_estimate.run(_pinned(candidate), intake_resolver=_resolver)
    cold = _item(result, "NEC-690.7-COLD-VOC")
    assert cold["status"] == "insufficient_input"
    assert cold["passed"] is False


def test_unknown_nested_fields_are_rejected():
    candidate = copy.deepcopy(VALID_INPUT)
    candidate["inverter"]["nested"] = {"unexpected": True}
    with pytest.raises(ValueError, match="inverter rejects unknown fields"):
        elec_estimate.run(_pinned(candidate), intake_resolver=_resolver)


def test_source_pin_is_required_and_mismatch_fails_closed():
    no_pin = elec_estimate.run(VALID_INPUT, intake_resolver=_resolver)
    pin = _item(no_pin, "ADAPTER-SOURCE-PIN")
    assert pin["status"] == "insufficient_input"
    assert pin["passed"] is False
    with pytest.raises(RuntimeError, match="approved digest"):
        elec_estimate.run({**VALID_INPUT, "expected_adapter_sha256": "0" * 64}, intake_resolver=_resolver)


def test_elec_estimate_only_returns_money_with_explicit_rate_card_and_watts():
    calibrated = _pinned({**VALID_INPUT, "rate_card": {"currency": "USD", "per_wdc": 1.23}})
    result = elec_estimate.run(calibrated, intake_resolver=_resolver)
    assert result["solver_result"]["monetary"] == {
        "status": "calibrated", "currency": "USD", "amount": 16236.0,
        "basis": "explicit request rate_card.per_wdc",
    }


def test_elec_estimate_rejects_a_wrong_pinned_drawing_version():
    with pytest.raises(RuntimeError, match="immutable drawing version"):
        elec_estimate.run(_pinned(VALID_INPUT), intake_resolver=lambda _drawing, _version: {"version": 2, "intake": {}})
    with pytest.raises(ValueError, match="not head/latest"):
        elec_estimate.run(_pinned({**VALID_INPUT, "dwg_version": "head"}), intake_resolver=_resolver)
