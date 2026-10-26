"""Pure project sizing reconstruction against independently supplied custody bindings.

No admission, transport or persistence is established here. The caller supplies the
admitted manifest pin, parent intake identity and independently bound evidence pin.
"""
from __future__ import annotations

import copy
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
import unicodedata

import solar_sizing_client as cloud
from solar_design_graph import GraphValidationError
from solar_sizing_power import MODULE_POWER_MAX, record_module_power


MAX_REQUEST_BYTES = 65536
EVIDENCE_SCHEMA = "leaf.solar-project-sizing-evidence.v1"
SIP_R14_EVIDENCE_REJECTED = "SIP_R14_EVIDENCE_REJECTED"
SIP_R14_SYNTHETIC_EVIDENCE_REFUSED = "SIP_R14_SYNTHETIC_EVIDENCE_REFUSED"


def _bounded_request_json(value: object) -> bytes:
    try:
        chunks = []
        size = 0
        encoder = json.JSONEncoder(separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        for chunk in encoder.iterencode(value):
            encoded = chunk.encode("utf-8")
            size += len(encoded)
            if size > MAX_REQUEST_BYTES:
                raise ValueError()
            chunks.append(encoded)
        return b"".join(chunks)
    except (ValueError, TypeError, RecursionError, UnicodeError, OverflowError):
        raise GraphValidationError("CLOUD_REQUEST_INVALID") from None


def _bounded_request_wire(raw: bytes) -> bytes:
    if type(raw) is not bytes or len(raw) > MAX_REQUEST_BYTES:
        raise GraphValidationError("CLOUD_REQUEST_INVALID")
    return raw


def _request_voltage(request):
    try:
        voltage = Decimal(request.max_voltage)
        if (not voltage.is_finite() or not 0 < voltage <= 100000
                or voltage != voltage.to_integral_value()):
            raise ValueError()
        return voltage
    except (InvalidOperation, ValueError, TypeError):
        raise GraphValidationError("CLOUD_REQUEST_INVALID") from None


def _prepare_project(graph: dict, params: dict) -> tuple[dict, cloud.SizingRequest, bytes, dict]:
    _bounded_request_json(params)
    if type(params) is not dict:
        raise GraphValidationError("INVALID_SIZING_REQUEST")
    cloud._check_accepted_string_length(params)
    if params.get("mode") != "global":
        raise GraphValidationError("INVALID_SIZING_MODE")
    if (set(params) - {"expected_rev", "mode", "requests", "grant_ref", "confirm", "cancel",
                       "accepted_string_length"}
            or params.get("confirm") is not True or params.get("cancel", False) is not False):
        raise GraphValidationError("INVALID_SIZING_REQUEST")
    requests = params.get("requests")
    if type(requests) is not dict or len(requests) != 1:
        raise GraphValidationError("INVALID_SIZING_COVERAGE")
    target_id = next(iter(requests))
    try:
        parsed = cloud.SizingParams.model_validate(
            {"grant_ref": params.get("grant_ref"), "request": requests[target_id]})
        raw = _bounded_request_wire(cloud.wire_bytes(parsed.request))
    except (ValueError, TypeError, RecursionError, UnicodeError, OverflowError):
        raise GraphValidationError("CLOUD_REQUEST_INVALID") from None
    normalized = {
        "expected_rev": params.get("expected_rev"), "mode": "global",
        "requests": {target_id: parsed.request.wire()}, "grant_ref": parsed.grant_ref,
        "confirm": True, "cancel": False,
    }
    if "accepted_string_length" in params:
        normalized["accepted_string_length"] = params["accepted_string_length"]
    _bounded_request_json(normalized)
    _request_voltage(parsed.request)
    try:
        prepared = cloud.prepare_graph_sizing(graph, normalized)
    except cloud.CloudError as exc:
        raise GraphValidationError(exc.classification.upper()) from None
    return prepared, parsed.request, raw, normalized


def _parse_response(raw):
    try:
        return cloud.parse_response_bytes(raw)
    except cloud.CloudError:
        raise GraphValidationError("CLOUD_RESPONSE_INVALID") from None


def _derive_project_result(request: cloud.SizingRequest, parsed_response: dict,
                           accepted_string_length=None) -> dict:
    voltage = _request_voltage(request)
    try:
        _keys(parsed_response, {"wire_bytes", "response", "sizing", "wire_sha256",
                                "canonical_sha256"})
        fresh = _parse_response(parsed_response["wire_bytes"])
        for key in ("response", "sizing", "wire_sha256", "canonical_sha256"):
            if cloud.canonical_bytes(fresh[key]) != cloud.canonical_bytes(parsed_response[key]):
                raise ValueError()
        response = cloud.SizingResponse.model_validate(fresh["response"])
        if voltage != Decimal(str(response.simulation_results.standard.string_design_voltage)):
            raise ValueError()
        sizing = cloud.recommend(response, accepted_string_length)
    except (KeyError, ValueError, TypeError, RecursionError, InvalidOperation):
        raise GraphValidationError("CLOUD_RESPONSE_INVALID") from None
    if sizing["voc_cold"]["passes"] is not True:
        raise GraphValidationError("COLD_VOLTAGE_FAILED")
    try:
        power = record_module_power(cloud.sizing_record(request, fresh, "unused", "unused"))
    except GraphValidationError:
        raise GraphValidationError("CLOUD_RESPONSE_INVALID") from None
    return {**sizing, "module_power_watts": power}


def _keys(value, keys):
    if type(value) is not dict or set(value) != keys:
        raise ValueError()


def _id(value):
    if (type(value) is not str or not 1 <= len(value) <= 256 or value.strip() != value
            or any(unicodedata.category(char) == "Cc" for char in value)):
        raise ValueError()


def _hash(value, *, manifest=False):
    pattern = r"sha256:[0-9a-f]{64}" if manifest else r"[0-9a-f]{64}"
    if type(value) is not str or re.fullmatch(pattern, value) is None:
        raise ValueError()


def _identity(value):
    _keys(value, {"organization_id", "project_id", "drawing_id", "job_id", "actor_id",
                  "target_settings_id"})
    for item in value.values():
        _id(item)


def _producer(value):
    _keys(value, {"adapter_version", "service_identity", "http_status", "worker_id", "attempt"})
    if (type(value["adapter_version"]) is not str or value["adapter_version"] != cloud.ADAPTER_VERSION
            or type(value["service_identity"]) is not str or value["service_identity"] != cloud.SIZING_URL
            or type(value["http_status"]) is not int or value["http_status"] != 200
            or type(value["attempt"]) is not int or value["attempt"] < 1):
        raise ValueError()
    _id(value["worker_id"])


def _normalized_parameters(value, target_id):
    keys = {"expected_rev", "mode", "requests", "grant_ref", "confirm", "cancel"}
    if type(value) is dict and "accepted_string_length" in value:
        keys.add("accepted_string_length")
    _keys(value, keys)
    cloud._check_accepted_string_length(value)
    if (type(value["expected_rev"]) is not int or value["expected_rev"] < 0
            or value["mode"] != "global" or value["confirm"] is not True
            or value["cancel"] is not False):
        raise ValueError()
    _keys(value["requests"], {target_id})
    parsed = cloud.SizingParams.model_validate(
        {"grant_ref": value["grant_ref"], "request": value["requests"][target_id]})
    if cloud.canonical_bytes(parsed.request.wire()) != cloud.canonical_bytes(
            value["requests"][target_id]):
        raise ValueError()
    _request_voltage(parsed.request)
    _bounded_request_json(value)
    _bounded_request_wire(cloud.wire_bytes(parsed.request))
    return parsed.request


def _derived(value):
    _keys(value, {"panels_in_sequence", "voc_cold", "module_power_watts", "output_graph_sha256"})
    length = value["panels_in_sequence"]
    if type(length) is not int or not 1 <= length <= 4096:
        raise ValueError()
    cold = value["voc_cold"]
    _keys(cold, {"passes", "override_accepted", "suggested_string_length",
                 "per_module", "string_voltage", "max_dc_voltage"})
    if (cold["passes"] is not True or cold["override_accepted"] is not False
            or type(cold["suggested_string_length"]) is not int
            or cold["suggested_string_length"] != 0):
        raise ValueError()
    for key in ("per_module", "string_voltage", "max_dc_voltage"):
        if type(cold[key]) is not float or not math.isfinite(cold[key]) or cold[key] <= 0:
            raise ValueError()
    if (cold["string_voltage"] != cold["per_module"] * length
            or cold["string_voltage"] > cold["max_dc_voltage"]
            or not cold["max_dc_voltage"].is_integer() or cold["max_dc_voltage"] > 100000):
        raise ValueError()
    power = value["module_power_watts"]
    if type(power) is not float or not math.isfinite(power) or not 0 < power <= MODULE_POWER_MAX:
        raise ValueError()
    _hash(value["output_graph_sha256"])


def plan_sizing_request(params: dict, parent_graph: dict, parent_intake_sha256: str,
                        tool_manifest_sha256: str) -> dict:
    prepared, request, raw, normalized = _prepare_project(parent_graph, params)
    try:
        _hash(parent_intake_sha256)
        _hash(tool_manifest_sha256, manifest=True)
    except ValueError:
        raise GraphValidationError(SIP_R14_EVIDENCE_REJECTED) from None
    return {
        "wire_bytes": raw, "normalized_request": request.wire(),
        "wire_sha256": hashlib.sha256(raw).hexdigest(),
        "canonical_sha256": cloud.digest(request.wire()), "normalized_params": normalized,
        "target_settings_id": next(iter(prepared["targets"])),
        "parent_intake_sha256": parent_intake_sha256,
        "graph_sha256": cloud.digest(prepared["graph"]),
        "sizing_basis_sha256": cloud.sizing_basis(prepared["graph"], prepared["slots"]),
        "tool_manifest_sha256": tool_manifest_sha256,
    }


def apply_project_sizing(graph: dict, params: dict, parsed_response: dict, *,
                         organization_id: str, job_id: str) -> dict:
    prepared, request, _, _ = _prepare_project(graph, params)
    try:
        _id(organization_id)
        _id(job_id)
    except ValueError:
        raise GraphValidationError("CLOUD_REQUEST_INVALID") from None
    accepted = params.get("accepted_string_length")
    _derive_project_result(request, parsed_response, accepted)
    record = cloud.sizing_record(request, parsed_response, organization_id, job_id, accepted)
    return cloud.apply_sizing_records(prepared, {next(iter(prepared["targets"])): record})


def build_sizing_evidence(params: dict, parent_graph: dict, parent_intake_sha256: str,
                          tool_manifest_sha256: str, response_bytes: bytes, *,
                          identity: dict, parent_version_id: str, producer: dict,
                          classification: str) -> dict:
    plan = plan_sizing_request(params, parent_graph, parent_intake_sha256, tool_manifest_sha256)
    try:
        _identity(identity)
        _producer(producer)
        _id(parent_version_id)
        if identity["target_settings_id"] != plan["target_settings_id"]:
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise GraphValidationError(SIP_R14_EVIDENCE_REJECTED) from None
    parsed = _parse_response(response_bytes)
    applied = apply_project_sizing(
        parent_graph, plan["normalized_params"], parsed,
        organization_id=identity["organization_id"], job_id=identity["job_id"])
    derived = _derive_project_result(cloud.SizingRequest.model_validate(plan["normalized_request"]),
                                     parsed, plan["normalized_params"].get("accepted_string_length"))
    derived["output_graph_sha256"] = cloud.digest(applied["graph"])
    record = {
        "schema": EVIDENCE_SCHEMA, "identity": copy.deepcopy(identity),
        "input": {
            "parent_version_id": parent_version_id, "parent_intake_sha256": parent_intake_sha256,
            "graph_sha256": plan["graph_sha256"], "sizing_basis_sha256": plan["sizing_basis_sha256"],
            "parameters": plan["normalized_params"], "tool_manifest_sha256": tool_manifest_sha256,
        },
        "request": {
            "normalized": plan["normalized_request"], "wire_sha256": plan["wire_sha256"],
            "canonical_sha256": plan["canonical_sha256"],
        },
        "response": {"wire_sha256": parsed["wire_sha256"],
                     "canonical_sha256": parsed["canonical_sha256"]},
        "producer": copy.deepcopy(producer),
        "grant": {"reference": plan["normalized_params"]["grant_ref"]},
        "classification": classification, "derived": derived,
    }
    return validate_sizing_evidence(record)


def validate_sizing_evidence(record: dict) -> dict:
    try:
        _keys(record, {"schema", "identity", "input", "request", "response", "producer", "grant",
                       "classification", "derived"})
        if (type(record["schema"]) is not str or record["schema"] != EVIDENCE_SCHEMA
                or type(record["classification"]) is not str
                or record["classification"] not in ("live", "synthetic")):
            raise ValueError()
        _identity(record["identity"])
        target = record["identity"]["target_settings_id"]
        inputs = record["input"]
        _keys(inputs, {"parent_version_id", "parent_intake_sha256", "graph_sha256",
                       "sizing_basis_sha256", "parameters", "tool_manifest_sha256"})
        _id(inputs["parent_version_id"])
        for key in ("parent_intake_sha256", "graph_sha256", "sizing_basis_sha256"):
            _hash(inputs[key])
        _hash(inputs["tool_manifest_sha256"], manifest=True)
        request = _normalized_parameters(inputs["parameters"], target)
        _keys(record["request"], {"normalized", "wire_sha256", "canonical_sha256"})
        if cloud.canonical_bytes(record["request"]["normalized"]) != cloud.canonical_bytes(request.wire()):
            raise ValueError()
        for envelope in (record["request"], record["response"]):
            if envelope is record["response"]:
                _keys(envelope, {"wire_sha256", "canonical_sha256"})
            _hash(envelope["wire_sha256"])
            _hash(envelope["canonical_sha256"])
        _producer(record["producer"])
        _keys(record["grant"], {"reference"})
        if record["grant"]["reference"] != inputs["parameters"]["grant_ref"]:
            raise ValueError()
        _derived(record["derived"])
        cloud.canonical_bytes(record)
    except (ValueError, TypeError, KeyError, RecursionError, UnicodeError, OverflowError,
            GraphValidationError):
        raise GraphValidationError(SIP_R14_EVIDENCE_REJECTED) from None
    return copy.deepcopy(record)


def prove_sizing_evidence(record: dict, request_bytes: bytes, response_bytes: bytes,
                          parent_graph: dict, *, expected: dict,
                          allow_synthetic_for_test: bool = False) -> dict:
    _bounded_request_wire(request_bytes)
    parsed = _parse_response(response_bytes)
    record = validate_sizing_evidence(record)
    try:
        _keys(expected, {"identity", "parent_version_id", "parent_intake_sha256", "parameters",
                         "tool_manifest_sha256", "producer", "classification", "evidence_sha256"})
        _identity(expected["identity"])
        _producer(expected["producer"])
        _id(expected["parent_version_id"])
        _hash(expected["parent_intake_sha256"])
        _hash(expected["tool_manifest_sha256"], manifest=True)
        _hash(expected["evidence_sha256"])
        if (record["classification"] != expected["classification"]
                or record["identity"] != expected["identity"]
                or record["producer"] != expected["producer"]):
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise GraphValidationError(SIP_R14_EVIDENCE_REJECTED) from None
    if record["classification"] == "synthetic" and allow_synthetic_for_test is not True:
        raise GraphValidationError(SIP_R14_SYNTHETIC_EVIDENCE_REFUSED)
    evidence_sha256 = cloud.digest(record)
    if evidence_sha256 != expected["evidence_sha256"]:
        raise GraphValidationError(SIP_R14_EVIDENCE_REJECTED)
    plan = plan_sizing_request(expected["parameters"], parent_graph, expected["parent_intake_sha256"],
                               expected["tool_manifest_sha256"])
    if (request_bytes != plan["wire_bytes"]
            or cloud.canonical_bytes(record["request"]) != cloud.canonical_bytes({
                "normalized": plan["normalized_request"], "wire_sha256": plan["wire_sha256"],
                "canonical_sha256": plan["canonical_sha256"]})
            or record["response"] != {"wire_sha256": parsed["wire_sha256"],
                                      "canonical_sha256": parsed["canonical_sha256"]}):
        raise GraphValidationError(SIP_R14_EVIDENCE_REJECTED)
    rebuilt = build_sizing_evidence(
        expected["parameters"], parent_graph, expected["parent_intake_sha256"],
        expected["tool_manifest_sha256"], response_bytes, identity=expected["identity"],
        parent_version_id=expected["parent_version_id"], producer=expected["producer"],
        classification=expected["classification"])
    if cloud.canonical_bytes(rebuilt) != cloud.canonical_bytes(record):
        raise GraphValidationError(SIP_R14_EVIDENCE_REJECTED)
    applied = apply_project_sizing(
        parent_graph, expected["parameters"], parsed,
        organization_id=expected["identity"]["organization_id"], job_id=expected["identity"]["job_id"])
    return {"graph": applied["graph"], "derived": rebuilt["derived"],
            "evidence_sha256": evidence_sha256}
