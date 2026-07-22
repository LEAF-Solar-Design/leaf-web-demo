"""Small deterministic compliance kernel; rule data remains external and pinned."""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Dict, List, Mapping

SUPPORTED_RULE_TYPES = ("max_cold_string_voltage", "min_source_circuit_ampacity")


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


def _text(value: Decimal) -> str:
    normalized = value.normalize()
    return format(normalized, "f") if normalized != 0 else "0"


def _hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_rule_pack(pack: Mapping[str, Any]) -> None:
    if not isinstance(pack, Mapping):
        raise ValueError("rule pack must be an object")
    for name in ("pack_id", "edition", "version", "status", "rules"):
        if name not in pack:
            raise ValueError(f"rule pack requires {name}")
    if pack["status"] not in {"candidate", "ratified"}:
        raise ValueError("rule pack status must be candidate or ratified")
    if not isinstance(pack["rules"], list) or not pack["rules"]:
        raise ValueError("rule pack requires at least one rule")
    seen = set()
    for rule in pack["rules"]:
        if not isinstance(rule, Mapping):
            raise ValueError("each rule must be an object")
        required = {"rule_id", "rule_type", "citation", "inputs"}
        if not required.issubset(rule):
            raise ValueError(f"rule requires {sorted(required)}")
        if rule["rule_id"] in seen:
            raise ValueError("rule IDs must be unique")
        seen.add(rule["rule_id"])
        if rule["rule_type"] not in SUPPORTED_RULE_TYPES:
            raise ValueError(f"unsupported rule type {rule['rule_type']!r}")
        citation = rule["citation"]
        if not isinstance(citation, Mapping) or not all(citation.get(k)
                                                       for k in ("authority", "edition", "section")):
            raise ValueError("citation requires authority, edition, and section")
        if not isinstance(rule["inputs"], Mapping):
            raise ValueError("rule inputs must map semantic names to input fields")


def _read(inputs: Mapping[str, Any], mapping: Mapping[str, str], semantic: str) -> Decimal:
    field = mapping.get(semantic)
    if not isinstance(field, str) or field not in inputs:
        raise ValueError(f"rule input mapping requires available {semantic}")
    return _decimal(inputs[field], field)


def _derive(rule: Mapping[str, Any], inputs: Mapping[str, Any]) -> Dict[str, Any]:
    fields = rule["inputs"]
    with localcontext() as context:
        context.prec = 28
        if rule["rule_type"] == "max_cold_string_voltage":
            modules = _read(inputs, fields, "modules_in_series")
            voc = _read(inputs, fields, "module_voc_stc_v")
            beta = _read(inputs, fields, "beta_voc_per_c")
            minimum = _read(inputs, fields, "minimum_temperature_c")
            limit = _read(inputs, fields, "inverter_max_dc_voltage_v")
            if modules <= 0 or voc <= 0 or limit <= 0:
                raise ValueError("voltage rule requires positive module count, Voc, and limit")
            factor = Decimal(1) + beta * (minimum - Decimal(25))
            actual = modules * voc * factor
            return {"actual": actual, "limit": limit, "units": "V", "operator": "<=",
                    "passed": actual <= limit,
                    "derivation": {
                        "formula": "modules_in_series * module_voc_stc_v * "
                                   "(1 + beta_voc_per_c * (minimum_temperature_c - 25))",
                        "terms": {"modules_in_series": _text(modules),
                                  "module_voc_stc_v": _text(voc),
                                  "beta_voc_per_c": _text(beta),
                                  "minimum_temperature_c": _text(minimum),
                                  "temperature_reference_c": "25",
                                  "correction_factor": _text(factor)}}}
        parallel = _read(inputs, fields, "parallel_strings")
        isc = _read(inputs, fields, "module_isc_a")
        actual = _read(inputs, fields, "conductor_ampacity_a")
        if parallel <= 0 or isc <= 0 or actual <= 0:
            raise ValueError("ampacity rule requires positive strings, Isc, and ampacity")
        factor = _decimal(rule.get("continuous_factor", "1.25"), "continuous_factor")
        limit = parallel * isc * factor
        return {"actual": actual, "limit": limit, "units": "A", "operator": ">=",
                "passed": actual >= limit,
                "derivation": {
                    "formula": "parallel_strings * module_isc_a * continuous_factor",
                    "terms": {"parallel_strings": _text(parallel), "module_isc_a": _text(isc),
                              "continuous_factor": _text(factor)}}}


def evaluate(pack: Mapping[str, Any], inputs: Mapping[str, Any], *,
             standards_snapshot_id: str, ahj_snapshot_id: str,
             ahj_authority: str = "unknown") -> List[Dict[str, Any]]:
    """Evaluate supported typed rules and emit hash-stable, provenance-complete findings."""
    validate_rule_pack(pack)
    if not standards_snapshot_id or not ahj_snapshot_id:
        raise ValueError("standards and AHJ snapshot IDs are required")
    if ahj_authority not in {"authoritative", "unknown", "conflicting"}:
        raise ValueError("AHJ authority must be authoritative, unknown, or conflicting")
    authoritative = pack["status"] == "ratified" and ahj_authority == "authoritative"
    findings = []
    input_digest = _hash(inputs)
    for rule in sorted(pack["rules"], key=lambda item: item["rule_id"]):
        result = _derive(rule, inputs)
        finding = {
            "rule_id": rule["rule_id"], "rule_type": rule["rule_type"],
            "rule_pack_id": pack["pack_id"], "rule_version": pack["version"],
            "edition": pack["edition"], "standards_snapshot_id": standards_snapshot_id,
            "ahj_snapshot_id": ahj_snapshot_id, "input_sha256": input_digest,
            "actual": _text(result["actual"]), "limit": _text(result["limit"]),
            "units": result["units"], "operator": result["operator"],
            "result": "pass" if result["passed"] else "fail",
            "effect": "binding" if authoritative else "advisory",
            "authority_reason": ("ratified_pack_and_authoritative_ahj" if authoritative else
                                 "candidate_pack" if pack["status"] != "ratified" else
                                 f"ahj_{ahj_authority}"),
            "citation": dict(rule["citation"]), "derivation": result["derivation"],
        }
        finding["finding_sha256"] = _hash(finding)
        findings.append(finding)
    return findings
