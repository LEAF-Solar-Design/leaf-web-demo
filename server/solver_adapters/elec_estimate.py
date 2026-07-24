"""Fail-closed electrical estimate checks exposed at ``POST /api/elec``.

This DRAFT adapter ports bounded Branch2025 checks.  A check without every
input needed for its architecture is an explicit review item, never a pass.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, Depends

import deps
import write_loop
from envelopes import ErrorCode, error_response, with_envelope_fields

TOOL_NAME = "elec-estimate"
ENGINE_TIER = "elec_calc-python"
_ALLOWED_KEYS = {
    "drawing_id", "dwg_version", "modules_per_string", "string_count",
    "module", "inverter", "rate_card", "expected_adapter_sha256",
}
_MODULE_KEYS = {
    "watts", "voc", "vmp", "isc",
    "beta_voc_pct_per_c", "beta_vmp_pct_per_c",
}
_INVERTER_KEYS = {
    "architecture", "topology", "mppt_min_v", "mppt_max_v", "max_dc_voltage",
    "max_dc_input_a", "optimizer_max_input_isc", "optimizer_max_input_voltage",
    "design_min_temp_c", "design_max_temp_c",
}
_RATE_CARD_KEYS = {"currency", "per_wdc"}
_STANDARD_OCPD_A = (15, 20, 25, 30, 35, 40, 45, 50, 60)

router = APIRouter()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _adapter_sha256() -> str:
    """Digest the exact local port so a caller can require reviewed source."""
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _positive_number(value: Any, name: str, *, allow_zero: bool = False) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    if number < 0 or (number == 0 and not allow_zero):
        comparator = "non-negative" if allow_zero else "greater than zero"
        raise ValueError(f"{name} must be {comparator}")
    return number


def _finite_number(value: Any, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _optional_number(value: Any, name: str, *, positive: bool = True) -> Optional[float]:
    if value is None:
        return None
    return _positive_number(value, name) if positive else _finite_number(value, name)


def _reject_unknown(value: Dict[str, Any], allowed: set[str], name: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{name} rejects unknown fields: {sorted(unknown)}")


def _validated_input(params: Dict[str, Any]) -> Dict[str, Any]:
    """Validate shapes strictly but retain omitted electrical inputs for review."""
    if not isinstance(params, dict):
        raise ValueError("elec estimate params must be an object")
    _reject_unknown(params, _ALLOWED_KEYS, "elec estimate")

    drawing_id = params.get("drawing_id")
    if not isinstance(drawing_id, str) or not drawing_id:
        raise ValueError("drawing_id must be a non-empty string")
    version = params.get("dwg_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("dwg_version must be a positive integer, not head/latest")

    modules_per_string = params.get("modules_per_string")
    string_count = params.get("string_count")
    for value, name in ((modules_per_string, "modules_per_string"),
                        (string_count, "string_count")):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"{name} must be a positive integer")

    module = params.get("module")
    inverter = params.get("inverter")
    if not isinstance(module, dict) or not isinstance(inverter, dict):
        raise ValueError("module and inverter must be objects")
    _reject_unknown(module, _MODULE_KEYS, "module")
    _reject_unknown(inverter, _INVERTER_KEYS, "inverter")

    normalized_module = {
        "watts": _optional_number(module.get("watts"), "module.watts"),
        "voc": _optional_number(module.get("voc"), "module.voc"),
        "vmp": _optional_number(module.get("vmp"), "module.vmp"),
        "isc": _optional_number(module.get("isc"), "module.isc"),
        "beta_voc_pct_per_c": _optional_number(
            module.get("beta_voc_pct_per_c"),
            "module.beta_voc_pct_per_c", positive=False),
        "beta_vmp_pct_per_c": _optional_number(
            module.get("beta_vmp_pct_per_c"),
            "module.beta_vmp_pct_per_c", positive=False),
    }
    for coefficient_name in ("beta_voc_pct_per_c", "beta_vmp_pct_per_c"):
        coefficient = normalized_module[coefficient_name]
        if coefficient is not None and coefficient >= 0:
            raise ValueError(f"module.{coefficient_name} must be negative")
        if coefficient is not None and coefficient < -1.0:
            # A magnitude beyond 1.0 %/C is treated as a data error. It can
            # drive a corrected voltage nonpositive and produce a false pass.
            raise ValueError(
                f"module.{coefficient_name} magnitude is non-physical (expected >= -1.0 %/degC)")
    # Cross-field physical consistency. Individually-plausible module values can
    # be mutually contradictory (e.g. Isc below the implied Imp, or Vmp >= Voc);
    # such a module cannot exist, and feeding it to the safety checks would
    # under-state current/voltage and emit a false PASS. Reject at input.
    _voc, _vmp = normalized_module["voc"], normalized_module["vmp"]
    _isc, _watts = normalized_module["isc"], normalized_module["watts"]
    if _voc is not None and _vmp is not None and _vmp >= _voc:
        raise ValueError("module.vmp must be less than module.voc (max-power voltage is below open-circuit)")
    if _watts is not None and _vmp is not None and _isc is not None and _isc <= _watts / _vmp:
        raise ValueError(
            "module.isc must exceed the implied Imp = watts / vmp (short-circuit current exceeds max-power current)")

    architecture = inverter.get("architecture")
    if architecture not in {"central", "solaredge"}:
        raise ValueError("inverter.architecture must be 'central' or 'solaredge'")
    topology = inverter.get("topology")
    allowed_topologies = ({"per_string_inputs", "combined_input"}
                          if architecture == "central" else {"optimizer_per_module"})
    if topology is not None and topology not in allowed_topologies:
        raise ValueError(f"inverter.topology must be one of {sorted(allowed_topologies)}")
    normalized_inverter = {
        "architecture": architecture,
        "topology": topology,
        "mppt_min_v": _optional_number(inverter.get("mppt_min_v"), "inverter.mppt_min_v"),
        "mppt_max_v": _optional_number(inverter.get("mppt_max_v"), "inverter.mppt_max_v"),
        "max_dc_voltage": _optional_number(inverter.get("max_dc_voltage"), "inverter.max_dc_voltage"),
        "max_dc_input_a": _optional_number(inverter.get("max_dc_input_a"), "inverter.max_dc_input_a"),
        "optimizer_max_input_isc": _optional_number(
            inverter.get("optimizer_max_input_isc"), "inverter.optimizer_max_input_isc"),
        "optimizer_max_input_voltage": _optional_number(
            inverter.get("optimizer_max_input_voltage"), "inverter.optimizer_max_input_voltage"),
        "design_min_temp_c": _optional_number(
            inverter.get("design_min_temp_c"), "inverter.design_min_temp_c", positive=False),
        "design_max_temp_c": _optional_number(
            inverter.get("design_max_temp_c"), "inverter.design_max_temp_c", positive=False),
    }
    low, high = normalized_inverter["design_min_temp_c"], normalized_inverter["design_max_temp_c"]
    if low is not None and high is not None and low > high:
        raise ValueError("inverter.design_min_temp_c must not exceed design_max_temp_c")
    # Physical-range bounds so a well-typed but wrong temperature cannot produce
    # a plausible-but-false PASS (e.g. a design "minimum" above STC under-
    # estimates cold Voc). Cold-Voc is evaluated at design_min (must be <= 25C,
    # below STC); hot-Vmp at design_max (must be >= 25C, above STC).
    if low is not None and low > 25:
        raise ValueError("inverter.design_min_temp_c must be <= 25 C (the cold-Voc minimum, below STC)")
    if high is not None and high < 25:
        raise ValueError("inverter.design_max_temp_c must be >= 25 C (the hot-Vmp maximum, above STC)")
    if low is not None and low < -60:
        raise ValueError("inverter.design_min_temp_c is below any physical ambient (>= -60 C)")
    if high is not None and high > 100:
        raise ValueError("inverter.design_max_temp_c exceeds any physical cell temperature (<= 100 C)")
    mppt_low, mppt_high = normalized_inverter["mppt_min_v"], normalized_inverter["mppt_max_v"]
    if mppt_low is not None and mppt_high is not None and mppt_low > mppt_high:
        raise ValueError("inverter.mppt_min_v must not exceed inverter.mppt_max_v")

    rate_card = params.get("rate_card")
    normalized_rate_card = None
    if rate_card is not None:
        if not isinstance(rate_card, dict):
            raise ValueError("rate_card must be an object when provided")
        _reject_unknown(rate_card, _RATE_CARD_KEYS, "rate_card")
        if set(rate_card) != _RATE_CARD_KEYS:
            raise ValueError("rate_card must provide currency and per_wdc")
        currency = rate_card["currency"]
        if not isinstance(currency, str) or len(currency) != 3 or not currency.isupper() or not currency.isalpha():
            raise ValueError("rate_card.currency must be a three-letter uppercase ISO code")
        normalized_rate_card = {"currency": currency,
                                "per_wdc": _positive_number(rate_card["per_wdc"], "rate_card.per_wdc", allow_zero=True)}

    expected_sha = params.get("expected_adapter_sha256")
    if expected_sha is not None and (not isinstance(expected_sha, str)
                                     or len(expected_sha) != 64
                                     or any(c not in "0123456789abcdef" for c in expected_sha)):
        raise ValueError("expected_adapter_sha256 must be a lowercase SHA-256 hex digest")

    return json.loads(_canonical_bytes({
        "drawing_id": drawing_id, "dwg_version": version,
        "modules_per_string": modules_per_string, "string_count": string_count,
        "module": normalized_module, "inverter": normalized_inverter,
        "rate_card": normalized_rate_card, "expected_adapter_sha256": expected_sha,
    }))


def _check(rule: str, status: str, measured: Optional[float], limit: Optional[float],
           unit: str, citation: str, note: str) -> Dict[str, Any]:
    if status not in {"pass", "fail", "insufficient_input", "requires_engineer_review", "requires_optimizer_model"}:
        raise ValueError(f"unsupported electrical check status: {status}")
    return {
        "rule": rule, "status": status, "passed": status == "pass",
        "measured": None if measured is None else round(measured, 4),
        "limit": None if limit is None else round(limit, 4), "unit": unit,
        "citation": citation, "note": note,
    }


def _missing(*values: Any) -> bool:
    return any(value is None for value in values)


def _summary(checks: list[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "passed": sum(item["status"] == "pass" for item in checks),
        "failed": sum(item["status"] == "fail" for item in checks),
        "needs_review": sum(item["status"] not in {"pass", "fail"} for item in checks),
        "items": checks,
    }


def _calculate(body: Dict[str, Any]) -> Dict[str, Any]:
    """Port central and SolarEdge safety checks without fabricating passes."""
    module, inverter = body["module"], body["inverter"]
    modules, strings = body["modules_per_string"], body["string_count"]
    beta_voc = module["beta_voc_pct_per_c"]
    beta_vmp = module["beta_vmp_pct_per_c"]
    cold_voc = hot_vmp = continuous_current = ocpd_required = None
    if not _missing(module["voc"], beta_voc, inverter["design_min_temp_c"]):
        cold_voc = module["voc"] * (1.0 + beta_voc / 100.0 * (inverter["design_min_temp_c"] - 25.0))
        # Fail closed on a non-physical correction: a real module's temperature-
        # corrected Voc is always > 0. A pathological coefficient/temperature
        # (e.g. an absurd beta magnitude, or a design "minimum" temperature that
        # is not actually the coldest ambient) can drive the factor nonpositive,
        # which would otherwise sail past a `<= max_dc_voltage` check as a false
        # PASS. Drop it so the check reports insufficient_input instead.
        if cold_voc <= 0:
            cold_voc = None
    if not _missing(module["vmp"], beta_vmp, inverter["design_max_temp_c"]):
        hot_vmp = module["vmp"] * (1.0 + beta_vmp / 100.0 * (inverter["design_max_temp_c"] - 25.0))
        if hot_vmp <= 0:
            hot_vmp = None
    if module["isc"] is not None:
        continuous_current = module["isc"] * 1.25
        ocpd_required = module["isc"] * 1.25 * 1.25
    checks: list[Dict[str, Any]] = []

    if inverter["architecture"] == "central":
        string_cold_voc = None if cold_voc is None else cold_voc * modules
        string_hot_vmp = None if hot_vmp is None else hot_vmp * modules
        if _missing(string_cold_voc, inverter["max_dc_voltage"]):
            checks.append(_check("NEC-690.7-COLD-VOC", "insufficient_input", string_cold_voc,
                                 inverter["max_dc_voltage"], "V", "NEC 690.7(A)(3)",
                                 "Requires module Voc, beta Voc, design minimum temperature, and inverter max_dc_voltage."))
        else:
            checks.append(_check("NEC-690.7-COLD-VOC", "pass" if string_cold_voc <= inverter["max_dc_voltage"] else "fail",
                                 string_cold_voc, inverter["max_dc_voltage"], "V", "NEC 690.7(A)(3)",
                                 "String cold Voc compared with inverter maximum DC voltage, never MPPT maximum."))
        if _missing(string_hot_vmp, inverter["mppt_min_v"]):
            checks.append(_check("MPPT-HOT-VMP", "insufficient_input", string_hot_vmp, inverter["mppt_min_v"], "V",
                                 "NEC 690.7(A)", "Requires module Vmp, beta Vmp, design maximum temperature, and MPPT minimum."))
        else:
            checks.append(_check("MPPT-HOT-VMP", "pass" if string_hot_vmp >= inverter["mppt_min_v"] else "fail",
                                 string_hot_vmp, inverter["mppt_min_v"], "V", "NEC 690.7(A)",
                                 "String Vmp at design maximum temperature must meet the MPPT floor."))
        if _missing(string_cold_voc, inverter["mppt_max_v"]):
            checks.append(_check("MPPT-COLD-VOC", "insufficient_input", string_cold_voc, inverter["mppt_max_v"], "V",
                                 "MPPT operating window", "Requires string cold Voc and MPPT maximum."))
        else:
            checks.append(_check("MPPT-COLD-VOC", "pass" if string_cold_voc <= inverter["mppt_max_v"] else "fail",
                                 string_cold_voc, inverter["mppt_max_v"], "V", "MPPT operating window",
                                 "MPPT tracking ceiling is reported separately from the absolute max DC safety limit."))
        topology = inverter["topology"]
        if _missing(continuous_current, inverter["max_dc_input_a"], topology):
            checks.append(_check("NEC-690.8-CONTINUOUS", "insufficient_input", continuous_current,
                                 inverter["max_dc_input_a"], "A", "NEC 690.8(A)",
                                 "Requires module Isc, inverter input limit, and central-inverter input topology."))
        else:
            measured = continuous_current if topology == "per_string_inputs" else continuous_current * strings
            note = ("Per-string Isc x 125 percent compared with one independently rated inverter input."
                    if topology == "per_string_inputs" else
                    "Aggregate Isc x 125 percent across string_count compared with the combined inverter input limit.")
            note += " This does not size conductors or verify NEC 690.8(B) conductor ampacity."
            checks.append(_check("NEC-690.8-CONTINUOUS", "pass" if measured <= inverter["max_dc_input_a"] else "fail",
                                 measured, inverter["max_dc_input_a"], "A", "NEC 690.8(A)", note))
        if ocpd_required is None:
            checks.append(_check("NEC-690.9-OCPD", "insufficient_input", None, None, "A", "NEC 690.9(B)",
                                 "Requires module Isc for cumulative 156.25 percent source-circuit OCPD sizing."))
        else:
            rating = next((item for item in _STANDARD_OCPD_A if item >= ocpd_required), None)
            checks.append(_check("NEC-690.9-OCPD", "pass" if rating is not None else "requires_engineer_review",
                                 ocpd_required, None if rating is None else float(rating), "A", "NEC 690.9(B)",
                                 "Minimum source-circuit fuse selection only. This does not verify module maximum-series-fuse rating, conductor ampacity, or OCPD coordination."))
    else:
        if _missing(cold_voc, inverter["optimizer_max_input_voltage"]):
            checks.append(_check("NEC-690.7-VOC-COLD-OPTIMIZER", "requires_engineer_review", cold_voc,
                                 inverter["optimizer_max_input_voltage"], "V", "NEC 690.7(A)(3)",
                                 "SolarEdge requires verified per-optimizer Absolute Maximum Input Voltage."))
        else:
            checks.append(_check("NEC-690.7-VOC-COLD-OPTIMIZER",
                                 "pass" if cold_voc <= inverter["optimizer_max_input_voltage"] else "fail",
                                 cold_voc, inverter["optimizer_max_input_voltage"], "V", "NEC 690.7(A)(3)",
                                 "Per-module cold Voc is compared with optimizer Absolute Maximum Input Voltage."))
        if _missing(module["isc"], inverter["optimizer_max_input_isc"], inverter["topology"]):
            checks.append(_check("OPTIMIZER-MAX-INPUT-ISC", "requires_engineer_review", module["isc"],
                                 inverter["optimizer_max_input_isc"], "A", "optimizer datasheet",
                                 "SolarEdge requires optimizer model, input topology, and MaxInputIsc."))
        else:
            checks.append(_check("OPTIMIZER-MAX-INPUT-ISC",
                                 "pass" if module["isc"] <= inverter["optimizer_max_input_isc"] else "fail",
                                 module["isc"], inverter["optimizer_max_input_isc"], "A", "optimizer datasheet",
                                 "Bare module Isc is compared with optimizer MaxInputIsc. This is a device rating, not conductor sizing."))
        checks.append(_check("MPPT-WINDOW", "requires_engineer_review", None, None, "V", "SolarEdge FFSV architecture",
                             "Fixed optimizer bus voltage is not a module-string MPPT window. Verify the approved SolarEdge design."))
        checks.append(_check("NEC-690.9-OCPD", "requires_optimizer_model", None, None, "A", "NEC 690.9(B)",
                             "Generic module-Isc OCPD is refused for SolarEdge. Optimizer output and bus current are required."))

    system_wdc = None if module["watts"] is None else module["watts"] * modules * strings
    rate_card = body["rate_card"]
    monetary = ({"status": "calibrated", "currency": rate_card["currency"],
                 "amount": round(system_wdc * rate_card["per_wdc"], 2),
                 "basis": "explicit request rate_card.per_wdc"}
                if rate_card and system_wdc is not None else
                {"status": "not_calibrated", "reason": "rate_card calibration is OPEN; provide currency, per_wdc, and module watts"})
    return {
        "engine_tier": ENGINE_TIER,
        "sizing": {"system_wdc": None if system_wdc is None else round(system_wdc, 4),
                   "modules_per_string": modules, "string_count": strings},
        "electrical": {"module_cold_voc": None if cold_voc is None else round(cold_voc, 4),
                       "string_cold_voc": None if cold_voc is None else round(cold_voc * modules, 4),
                       "module_hot_vmp": None if hot_vmp is None else round(hot_vmp, 4),
                       "string_hot_vmp": None if hot_vmp is None else round(hot_vmp * modules, 4),
                       "continuous_current_a": None if continuous_current is None else round(continuous_current, 4),
                       "ocpd_required_a": None if ocpd_required is None else round(ocpd_required, 4)},
        "checks": _summary(checks), "monetary": monetary,
    }


def _untrusted_pin_result(body: Dict[str, Any]) -> Dict[str, Any]:
    item = _check("ADAPTER-SOURCE-PIN", "insufficient_input", None, None, "sha256", "adapter source pin",
                  "expected_adapter_sha256 is required before electrical checks can run.")
    return {"engine_tier": ENGINE_TIER, "sizing": {"system_wdc": None,
            "modules_per_string": body["modules_per_string"], "string_count": body["string_count"]},
            "electrical": {}, "checks": _summary([item]),
            "monetary": {"status": "not_calibrated", "reason": "source pin is required"}}


IntakeResolver = Callable[[str, int], Dict[str, Any]]


def run(params: Dict[str, Any], *, intake_resolver: IntakeResolver) -> Dict[str, Any]:
    """Calculate against a pinned drawing version without claiming intake provenance."""
    body = _validated_input(params)
    source_before = _adapter_sha256()
    expected_sha = body["expected_adapter_sha256"]
    if expected_sha is None:
        result = _untrusted_pin_result(body)
    else:
        if expected_sha != source_before:
            raise RuntimeError("elec estimate adapter does not match the approved digest; refusing to run")
        view = intake_resolver(body["drawing_id"], body["dwg_version"])
        if not isinstance(view, dict) or view.get("version") != body["dwg_version"] or not isinstance(view.get("intake"), dict):
            raise RuntimeError("elec estimate could not resolve the requested immutable drawing version")
        result = _calculate(body)
    source_after = _adapter_sha256()
    if source_after != source_before:
        raise RuntimeError("elec estimate adapter changed during execution")
    return {"solver": TOOL_NAME, "solver_input": body, "request_sha256": _sha256(params),
            "input_sha256": _sha256(body), "result_sha256": _sha256(result),
            "adapter_sha256": source_before, "drawing_id": body["drawing_id"],
            "dwg_version": body["dwg_version"], "solver_result": result}


def _route_intake_resolver(tenant_id: str) -> IntakeResolver:
    def resolve(drawing_id: str, version: int) -> Dict[str, Any]:
        backend = write_loop.backend_for_tenant(
            tenant_id, aps_live=deps.APS_LIVE,
            da=deps.get_da_client() if deps.APS_LIVE else None,
        )
        return write_loop.intake_view(tenant_id, drawing_id, version, backend=backend)
    return resolve


@router.post("/api/elec")
def estimate(payload: Dict[str, Any], tenant_id: str = Depends(deps.require_tenant)) -> Dict[str, Any]:
    """Run the DRAFT electrical estimation contract for a pinned drawing version."""
    try:
        return with_envelope_fields(run(payload, intake_resolver=_route_intake_resolver(str(tenant_id))))
    except ValueError as exc:
        return error_response(ErrorCode.BAD_PARAMS, str(exc), retryable=False, status_code=400)
    except (KeyError, RuntimeError) as exc:
        return error_response(ErrorCode.BAD_PARAMS, str(exc), retryable=False, status_code=404)
