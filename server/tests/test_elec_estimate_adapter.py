import pytest

from solver_adapters import elec_estimate


VALID_INPUT = {
    "drawing_id": "demo",
    "dwg_version": 1,
    "modules_per_string": 12,
    "string_count": 20,
    "module": {"watts": 550, "voc": 50, "vmp": 42, "isc": 10,
               "temperature_coefficient_pct_per_c": -0.3},
    "inverter": {"architecture": "central", "mppt_min_v": 300,
                 "mppt_max_v": 800, "max_dc_input_a": 15,
                 "design_min_temp_c": -10, "design_max_temp_c": 70},
    "rate_card": None,
    "expected_adapter_sha256": None,
}


def _resolver(drawing_id, version):
    assert (drawing_id, version) == ("demo", 1)
    return {"version": 1, "intake": {"entities": []}}


def test_elec_estimate_is_deterministic_and_not_calibrated_without_rate_card():
    first = elec_estimate.run(VALID_INPUT, intake_resolver=_resolver)
    second = elec_estimate.run(VALID_INPUT, intake_resolver=_resolver)
    assert first == second
    assert first["solver"] == "elec-estimate"
    assert first["solver_result"]["engine_tier"] == "elec_calc-python"
    assert first["solver_result"]["sizing"]["system_wdc"] == 132000.0
    assert first["solver_result"]["checks"]["failed"] == 0
    assert first["solver_result"]["monetary"]["status"] == "not_calibrated"
    assert len(first["adapter_sha256"]) == 64


def test_elec_estimate_only_returns_money_with_explicit_rate_card():
    calibrated = {**VALID_INPUT, "rate_card": {"currency": "USD", "per_wdc": 1.23}}
    result = elec_estimate.run(calibrated, intake_resolver=_resolver)
    assert result["solver_result"]["monetary"] == {
        "status": "calibrated", "currency": "USD", "amount": 162360.0,
        "basis": "explicit request rate_card.per_wdc",
    }


def test_elec_estimate_rejects_unknown_fields_and_unpinned_source():
    with pytest.raises(ValueError, match="rejects unknown fields"):
        elec_estimate.run({**VALID_INPUT, "surprise": True}, intake_resolver=_resolver)
    with pytest.raises(RuntimeError, match="approved digest"):
        elec_estimate.run({**VALID_INPUT, "expected_adapter_sha256": "0" * 64}, intake_resolver=_resolver)


def test_elec_estimate_rejects_a_missing_or_wrong_pinned_drawing_version():
    with pytest.raises(RuntimeError, match="immutable drawing version"):
        elec_estimate.run(VALID_INPUT, intake_resolver=lambda _drawing, _version: {"version": 2, "intake": {}})
    with pytest.raises(ValueError, match="not head/latest"):
        elec_estimate.run({**VALID_INPUT, "dwg_version": "head"}, intake_resolver=_resolver)
