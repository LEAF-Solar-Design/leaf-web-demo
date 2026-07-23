"""Adapter-first electrical estimate checks exposed at ``POST /api/elec``.

This is a local port of the deterministic electrical guardrail chain used by
the Branch2025 design-studio proto.  It deliberately accepts data, not a
caller-supplied filename.  The route resolves a tenant's immutable intake
version through the existing drawing store, so the adapter never crosses a
host-file boundary on behalf of a request.

No rate card is bundled here.  A monetary result is emitted only when the
caller supplies a complete explicit calibration object.
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
_MODULE_KEYS = {"watts", "voc", "vmp", "isc", "temperature_coefficient_pct_per_c"}
_INVERTER_KEYS = {"architecture", "mppt_min_v", "mppt_max_v", "max_dc_input_a",
                  "design_min_temp_c", "design_max_temp_c"}
_RATE_CARD_KEYS = {"currency", "per_wdc"}
_STANDARD_OCPD_A = (15, 20, 25, 30, 35, 40, 45, 50, 60)

router = APIRouter()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _adapter_sha256() -> str:
    """Digest the exact local port so callers can pin a reviewed implementation."""
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


def _reject_unknown(value: Dict[str, Any], allowed: set[str], name: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{name} rejects unknown fields: {sorted(unknown)}")


def _validated_input(params: Dict[str, Any]) -> Dict[str, Any]:
    """Validate the narrow public contract before resolving or calculating."""
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
    if set(module) != _MODULE_KEYS:
        raise ValueError("module must provide watts, voc, vmp, isc, and temperature_coefficient_pct_per_c")
    if set(inverter) != _INVERTER_KEYS:
        raise ValueError("inverter must provide the complete electrical check inputs")

    normalized_module = {
        "watts": _positive_number(module["watts"], "module.watts"),
        "voc": _positive_number(module["voc"], "module.voc"),
        "vmp": _positive_number(module["vmp"], "module.vmp"),
        "isc": _positive_number(module["isc"], "module.isc"),
        "temperature_coefficient_pct_per_c": _finite_number(
            module["temperature_coefficient_pct_per_c"],
            "module.temperature_coefficient_pct_per_c"),
    }
    # The C# source rule assumes the normal negative PV voltage coefficient.
    if normalized_module["temperature_coefficient_pct_per_c"] >= 0:
        raise ValueError("module.temperature_coefficient_pct_per_c must be negative")

    architecture = inverter["architecture"]
    if architecture not in {"central", "solaredge"}:
        raise ValueError("inverter.architecture must be 'central' or 'solaredge'")
    normalized_inverter = {
        "architecture": architecture,
        "mppt_min_v": _positive_number(inverter["mppt_min_v"], "inverter.mppt_min_v", allow_zero=True),
        "mppt_max_v": _positive_number(inverter["mppt_max_v"], "inverter.mppt_max_v", allow_zero=True),
        "max_dc_input_a": _positive_number(inverter["max_dc_input_a"], "inverter.max_dc_input_a", allow_zero=True),
        "design_min_temp_c": _finite_number(inverter["design_min_temp_c"], "inverter.design_min_temp_c"),
        "design_max_temp_c": _finite_number(inverter["design_max_temp_c"], "inverter.design_max_temp_c"),
    }
    if normalized_inverter["design_min_temp_c"] > normalized_inverter["design_max_temp_c"]:
        raise ValueError("inverter.design_min_temp_c must not exceed design_max_temp_c")
    if (normalized_inverter["mppt_max_v"] and normalized_inverter["mppt_min_v"]
            and normalized_inverter["mppt_min_v"] > normalized_inverter["mppt_max_v"]):
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
        "drawing_id": drawing_id,
        "dwg_version": version,
        "modules_per_string": modules_per_string,
        "string_count": string_count,
        "module": normalized_module,
        "inverter": normalized_inverter,
        "rate_card": normalized_rate_card,
        "expected_adapter_sha256": expected_sha,
    }))


def _check(rule: str, passed: bool, measured: float, limit: Optional[float], unit: str,
           citation: str, note: str) -> Dict[str, Any]:
    return {"rule": rule, "passed": passed, "measured": round(measured, 4),
            "limit": None if limit is None else round(limit, 4), "unit": unit,
            "citation": citation, "note": note}


def _calculate(body: Dict[str, Any]) -> Dict[str, Any]:
    """Port the guarded cold-Voc, hot-Vmp, and current/OCPD calculations."""
    module, inverter = body["module"], body["inverter"]
    modules = body["modules_per_string"]
    coefficient = module["temperature_coefficient_pct_per_c"] / 100.0
    cold_factor = 1.0 + coefficient * (inverter["design_min_temp_c"] - 25.0)
    hot_factor = 1.0 + coefficient * (inverter["design_max_temp_c"] - 25.0)
    cold_voc = module["voc"] * cold_factor * modules
    hot_vmp = module["vmp"] * hot_factor * modules
    continuous_current = module["isc"] * 1.25
    ocpd_required = module["isc"] * 1.25 * 1.25
    standard_ocpd = next((rating for rating in _STANDARD_OCPD_A if rating >= ocpd_required), None)
    checks: list[Dict[str, Any]] = []

    if inverter["architecture"] == "solaredge":
        checks.append(_check("MPPT-WINDOW", True, hot_vmp, None, "V", "NEC 690.7(A)",
                             "not applicable to fixed-string-voltage architecture"))
    else:
        if inverter["mppt_min_v"]:
            checks.append(_check("MPPT-HOT-VMP", hot_vmp >= inverter["mppt_min_v"], hot_vmp,
                                 inverter["mppt_min_v"], "V", "NEC 690.7(A)",
                                 "string Vmp at design maximum temperature"))
        if inverter["mppt_max_v"]:
            checks.append(_check("MPPT-COLD-VOC", cold_voc <= inverter["mppt_max_v"], cold_voc,
                                 inverter["mppt_max_v"], "V", "NEC 690.7(A)",
                                 "string Voc at design minimum temperature"))
    if inverter["max_dc_input_a"]:
        checks.append(_check("NEC-690.8-CONTINUOUS", continuous_current <= inverter["max_dc_input_a"],
                             continuous_current, inverter["max_dc_input_a"], "A", "NEC 690.8(A)",
                             "Isc multiplied by 125 percent"))
    checks.append(_check("NEC-690.9-OCPD", standard_ocpd is not None, ocpd_required,
                         float(standard_ocpd) if standard_ocpd is not None else None, "A", "NEC 690.9(B)",
                         "minimum standard OCPD from cumulative Isc multiplied by 156.25 percent"))

    system_wdc = module["watts"] * modules * body["string_count"]
    rate_card = body["rate_card"]
    monetary = ( {"status": "calibrated", "currency": rate_card["currency"],
                   "amount": round(system_wdc * rate_card["per_wdc"], 2),
                   "basis": "explicit request rate_card.per_wdc"}
                 if rate_card else
                 {"status": "not_calibrated", "reason": "rate_card calibration is OPEN; provide currency and per_wdc"})
    passed = sum(1 for check in checks if check["passed"])
    return {
        "engine_tier": ENGINE_TIER,
        "sizing": {"system_wdc": round(system_wdc, 4), "modules_per_string": modules,
                   "string_count": body["string_count"]},
        "electrical": {"cold_voc": round(cold_voc, 4), "hot_vmp": round(hot_vmp, 4),
                       "continuous_current_a": round(continuous_current, 4),
                       "ocpd_required_a": round(ocpd_required, 4), "standard_ocpd_a": standard_ocpd},
        "checks": {"passed": passed, "failed": len(checks) - passed, "items": checks},
        "monetary": monetary,
    }


IntakeResolver = Callable[[str, int], Dict[str, Any]]


def run(params: Dict[str, Any], *, intake_resolver: IntakeResolver) -> Dict[str, Any]:
    """Resolve one pinned intake and calculate it without arbitrary file access."""
    body = _validated_input(params)
    source_before = _adapter_sha256()
    expected_sha = body["expected_adapter_sha256"]
    if expected_sha is not None and expected_sha != source_before:
        raise RuntimeError("elec estimate adapter does not match the approved digest; refusing to run")
    view = intake_resolver(body["drawing_id"], body["dwg_version"])
    if not isinstance(view, dict) or view.get("version") != body["dwg_version"] or not isinstance(view.get("intake"), dict):
        raise RuntimeError("elec estimate could not resolve the requested immutable drawing version")
    result = _calculate(body)
    source_after = _adapter_sha256()
    if source_after != source_before:
        raise RuntimeError("elec estimate adapter changed during execution")
    return {
        "solver": TOOL_NAME,
        "solver_input": body,
        "request_sha256": _sha256(params),
        "input_sha256": _sha256(body),
        "result_sha256": _sha256(result),
        "adapter_sha256": source_before,
        "drawing_id": body["drawing_id"],
        "dwg_version": body["dwg_version"],
        "intake_sha256": _sha256(view["intake"]),
        "solver_result": result,
    }


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
