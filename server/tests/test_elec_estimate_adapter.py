import copy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import deps
from solver_adapters import elec_estimate


VALID_INPUT = {
    "drawing_id": "demo",
    "dwg_version": 1,
    "modules_per_string": 12,
    "string_count": 2,
    "module": {"watts": 550, "voc": 50, "vmp": 42, "isc": 14,
               "beta_voc_pct_per_c": -0.27,
               "beta_vmp_pct_per_c": -0.40},
    "inverter": {"architecture": "central", "topology": "combined_input",
                 "mppt_min_v": 300, "mppt_max_v": 800, "max_dc_voltage": 1000,
                 "max_dc_input_a": 40, "optimizer_max_input_isc": None,
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


def test_garbage_large_coefficient_is_rejected_at_input_validation():
    # A magnitude beyond ~1.0 %/degC is non-physical; reject before any calc so
    # it cannot produce a small-positive garbage voltage that passes a limit.
    candidate = copy.deepcopy(VALID_INPUT)
    candidate["module"]["beta_voc_pct_per_c"] = -5.0
    with pytest.raises(ValueError, match="non-physical"):
        elec_estimate.run(_pinned(candidate), intake_resolver=_resolver)


def test_design_min_temp_above_stc_is_rejected():
    # A "minimum" temperature above STC under-estimates cold Voc -> false pass.
    candidate = copy.deepcopy(VALID_INPUT)
    candidate["inverter"]["design_min_temp_c"] = 40.0
    with pytest.raises(ValueError, match="design_min_temp_c"):
        elec_estimate.run(_pinned(candidate), intake_resolver=_resolver)


def test_design_max_temp_below_stc_is_rejected():
    candidate = copy.deepcopy(VALID_INPUT)
    candidate["inverter"]["design_max_temp_c"] = 10.0
    with pytest.raises(ValueError, match="design_max_temp_c"):
        elec_estimate.run(_pinned(candidate), intake_resolver=_resolver)


def test_physically_contradictory_module_isc_below_imp_is_rejected():
    # Isc must exceed the implied Imp = watts/vmp; a lower Isc is a contradictory
    # module that would under-state current into a false PASS. (This is exactly
    # the class the original fixture accidentally contained.)
    candidate = copy.deepcopy(VALID_INPUT)
    candidate["module"]["isc"] = 10  # watts 550 / vmp 42 = 13.1 A implied Imp
    with pytest.raises(ValueError, match="isc must exceed"):
        elec_estimate.run(_pinned(candidate), intake_resolver=_resolver)


def test_module_vmp_not_below_voc_is_rejected():
    candidate = copy.deepcopy(VALID_INPUT)
    candidate["module"]["vmp"] = 55  # >= voc 50
    with pytest.raises(ValueError, match="vmp must be less than"):
        elec_estimate.run(_pinned(candidate), intake_resolver=_resolver)


def test_negative_module_voc_is_rejected():
    candidate = copy.deepcopy(VALID_INPUT)
    candidate["module"]["voc"] = -50
    with pytest.raises(ValueError):
        elec_estimate.run(_pinned(candidate), intake_resolver=_resolver)


def test_legitimate_extreme_cold_still_evaluates_not_rejected():
    # A genuinely cold Tmin (-40C) with a real coefficient must evaluate to
    # pass/fail, never be over-rejected by the new bounds.
    candidate = copy.deepcopy(VALID_INPUT)
    candidate["inverter"]["design_min_temp_c"] = -40.0
    result = elec_estimate.run(_pinned(candidate), intake_resolver=_resolver)
    assert _item(result, "NEC-690.7-COLD-VOC")["status"] in {"pass", "fail"}


def test_hot_vmp_exact_mppt_minimum_is_a_pass_boundary():
    candidate = copy.deepcopy(VALID_INPUT)
    candidate["inverter"]["mppt_min_v"] = 42 * (1 - 0.004 * (70 - 25)) * 12
    result = elec_estimate.run(_pinned(candidate), intake_resolver=_resolver)
    hot = _item(result, "MPPT-HOT-VMP")
    assert hot["status"] == "pass"
    assert hot["measured"] == hot["limit"]


def test_distinct_beta_vmp_prevents_known_hot_mppt_false_pass():
    candidate = copy.deepcopy(VALID_INPUT)
    candidate.update({"modules_per_string": 5, "string_count": 1})
    candidate["module"].update({
        "watts": 400, "voc": 42.1, "vmp": 34.2, "isc": 12,
        "beta_voc_pct_per_c": -0.27,
        "beta_vmp_pct_per_c": -0.40,
    })
    candidate["inverter"].update({
        "design_min_temp_c": -40, "design_max_temp_c": 65,
        "mppt_min_v": 150,
    })
    result = elec_estimate.run(_pinned(candidate), intake_resolver=_resolver)
    electrical = result["solver_result"]["electrical"]
    assert electrical["module_cold_voc"] == 49.4886
    assert electrical["string_cold_voc"] == 247.4428
    assert electrical["module_hot_vmp"] == 28.728
    assert electrical["string_hot_vmp"] == 143.64
    assert _item(result, "MPPT-HOT-VMP")["status"] == "fail"
    assert 34.2 * (1 - 0.0027 * (65 - 25)) * 5 > 150


def test_missing_beta_vmp_isolates_only_hot_vmp_checks():
    candidate = copy.deepcopy(VALID_INPUT)
    del candidate["module"]["beta_vmp_pct_per_c"]
    result = elec_estimate.run(_pinned(candidate), intake_resolver=_resolver)
    hot = _item(result, "MPPT-HOT-VMP")
    assert hot["status"] == "insufficient_input"
    assert hot["passed"] is False
    assert result["solver_result"]["checks"]["needs_review"] >= 1
    assert _item(result, "NEC-690.7-COLD-VOC")["status"] in {"pass", "fail"}


def test_missing_beta_voc_isolates_only_cold_voc_checks():
    candidate = copy.deepcopy(VALID_INPUT)
    del candidate["module"]["beta_voc_pct_per_c"]
    result = elec_estimate.run(_pinned(candidate), intake_resolver=_resolver)
    cold = _item(result, "NEC-690.7-COLD-VOC")
    assert cold["status"] == "insufficient_input"
    assert cold["passed"] is False
    assert result["solver_result"]["checks"]["needs_review"] >= 1
    assert _item(result, "MPPT-HOT-VMP")["status"] in {"pass", "fail"}


def test_ambiguous_legacy_temperature_coefficient_is_rejected():
    candidate = copy.deepcopy(VALID_INPUT)
    candidate["module"]["temperature_coefficient_pct_per_c"] = -0.3
    with pytest.raises(ValueError, match="module rejects unknown fields"):
        elec_estimate.run(_pinned(candidate), intake_resolver=_resolver)


@pytest.mark.parametrize("field", ["beta_voc_pct_per_c", "beta_vmp_pct_per_c"])
def test_each_voltage_coefficient_is_validated_independently(field):
    candidate = copy.deepcopy(VALID_INPUT)
    candidate["module"][field] = 0.1
    with pytest.raises(ValueError, match=field):
        elec_estimate.run(_pinned(candidate), intake_resolver=_resolver)


def test_solver_input_retains_both_distinct_voltage_coefficients():
    result = elec_estimate.run(_pinned(VALID_INPUT), intake_resolver=_resolver)
    assert result["solver_input"]["module"]["beta_voc_pct_per_c"] == -0.27
    assert result["solver_input"]["module"]["beta_vmp_pct_per_c"] == -0.40


def test_central_current_is_topology_and_string_count_aware():
    per_string = copy.deepcopy(VALID_INPUT)
    per_string["inverter"]["topology"] = "per_string_inputs"
    per_string["inverter"]["max_dc_input_a"] = 20
    aggregate = copy.deepcopy(per_string)
    aggregate["inverter"]["topology"] = "combined_input"
    assert _item(elec_estimate.run(_pinned(per_string), intake_resolver=_resolver), "NEC-690.8-CONTINUOUS")["status"] == "pass"
    aggregate_check = _item(elec_estimate.run(_pinned(aggregate), intake_resolver=_resolver), "NEC-690.8-CONTINUOUS")
    assert aggregate_check["status"] == "fail"
    assert aggregate_check["measured"] == 35.0


def test_central_ocpd_standard_fuse_boundary_fails_closed_above_60a():
    at_boundary = copy.deepcopy(VALID_INPUT)
    at_boundary["module"].update({"watts": 900, "vmp": 25, "isc": 38.4})
    above_boundary = copy.deepcopy(at_boundary)
    above_boundary["module"]["isc"] = 38.5
    boundary = _item(elec_estimate.run(_pinned(at_boundary), intake_resolver=_resolver), "NEC-690.9-OCPD")
    above = _item(elec_estimate.run(_pinned(above_boundary), intake_resolver=_resolver), "NEC-690.9-OCPD")
    assert boundary["status"] == "pass"
    assert boundary["measured"] == boundary["limit"] == 60.0
    assert above["status"] == "requires_engineer_review"
    assert above["passed"] is False


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


def _route_client(monkeypatch, resolver):
    app = FastAPI()
    app.include_router(elec_estimate.router)
    tenant_calls = []

    def tenant_dependency():
        tenant_calls.append("tenant-a")
        return "tenant-a"

    app.dependency_overrides[deps.require_tenant] = tenant_dependency
    monkeypatch.setattr(
        elec_estimate, "_route_intake_resolver",
        lambda tenant_id: resolver if tenant_id == "tenant-a" else None,
    )
    return TestClient(app, raise_server_exceptions=False), tenant_calls


def test_http_route_uses_tenant_dependency_and_pinned_resolver(monkeypatch):
    client, tenant_calls = _route_client(monkeypatch, _resolver)
    response = client.post("/api/elec", json=_pinned(VALID_INPUT))
    assert response.status_code == 200
    assert response.json()["solver"] == "elec-estimate"
    assert tenant_calls == ["tenant-a"]


def test_http_route_maps_validation_and_resolution_errors(monkeypatch):
    def unresolved(_drawing_id, _version):
        raise RuntimeError("requested drawing version is unavailable")

    client, _tenant_calls = _route_client(monkeypatch, unresolved)
    invalid = client.post("/api/elec", json={**_pinned(VALID_INPUT), "dwg_version": "head"})
    missing = client.post("/api/elec", json=_pinned(VALID_INPUT))
    assert invalid.status_code == 400
    assert missing.status_code == 404
