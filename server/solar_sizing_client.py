"""Broker adapter for Leaf Automation string-length sizing.

Provisional request model, TO BE PINNED against the plugin StringSizer:
{schema_version: 'leaf.string-length.v1', module: {model, voc,
 temp_coeff_pct_per_c}, inverter: {model, max_dc_voltage},
 design_min_temp_c, panels_in_sequence, units: 'SI'}.
The response has panels_in_sequence and voc_cold with all six graph fields.
The recorded fixture is synthetic, not evidence of the live wire contract.
Only the authenticated broker calls size(); grants never enter graph receipts.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import time
from typing import Literal

import requests
from pydantic import Field, model_validator

from leaf_cloud_client import StrictModel, canonical_bytes
from leaf_cloud_grants import CloudError, resolve_grant
from solar_design_graph import GraphValidationError, _bounded_json, require_revision, validate_graph

SIZING_URL = "https://api.leafdesign.ai/string-length"
MAX_RESPONSE_BYTES = 65536


class Module(StrictModel):
    model: str = Field(min_length=1, max_length=256, pattern=r"\S")
    voc: float = Field(gt=0, le=1000)
    temp_coeff_pct_per_c: float = Field(ge=-10, le=0)


class Inverter(StrictModel):
    model: str = Field(min_length=1, max_length=256, pattern=r"\S")
    max_dc_voltage: float = Field(gt=0, le=2000)


class SizingRequest(StrictModel):
    schema_version: Literal["leaf.string-length.v1"]
    module: Module
    inverter: Inverter
    design_min_temp_c: float = Field(ge=-100, le=25)
    panels_in_sequence: int = Field(ge=1, le=4096)
    units: Literal["SI"]


class ColdVoltage(StrictModel):
    passes: bool
    override_accepted: Literal[False]
    suggested_string_length: int = Field(ge=1, le=4096)
    per_module: float = Field(gt=0, le=2000)
    string_voltage: float = Field(gt=0, le=10000000)
    max_dc_voltage: float = Field(gt=0, le=2000)


class SizingResponse(StrictModel):
    panels_in_sequence: int = Field(ge=1, le=4096)
    voc_cold: ColdVoltage

    @model_validator(mode="after")
    def coherent(self):
        cold = self.voc_cold
        if (not math.isclose(cold.string_voltage, cold.per_module * self.panels_in_sequence,
                             rel_tol=1e-8, abs_tol=1e-6)
                or cold.passes != (cold.string_voltage <= cold.max_dc_voltage)
                or cold.suggested_string_length * cold.per_module > cold.max_dc_voltage + 1e-6):
            raise ValueError("inconsistent cold voltage")
        return self


class SizingParams(StrictModel):
    grant_ref: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    request: SizingRequest


def validate_params(params):
    try:
        _bounded_json(params)
        return SizingParams.model_validate(params)
    except (ValueError, TypeError):
        raise CloudError("cloud_request_invalid", 400) from None


def post_string_length(request, grant):
    try:
        deadline = time.monotonic() + 50
        with requests.post(
            SIZING_URL, data=canonical_bytes(request.model_dump()),
            headers={"Authorization": "Bearer " + grant.access_token,
                     "Content-Type": "application/json"},
            timeout=(5, 45), allow_redirects=False, stream=True,
        ) as response:
            if response.status_code == 401:
                raise CloudError("cloud_auth_missing", 401)
            if response.status_code == 403:
                raise CloudError("cloud_tenant_unauthorized", 403)
            if response.status_code != 200:
                raise CloudError("cloud_upstream_failure", 502)
            raw = bytearray()
            for chunk in response.iter_content(16384):
                raw.extend(chunk)
                if len(raw) > MAX_RESPONSE_BYTES or time.monotonic() > deadline:
                    raise CloudError("cloud_response_invalid", 502)
            return bytes(raw)
    except requests.RequestException:
        raise CloudError("cloud_upstream_failure", 502) from None


def validate_response(value, request):
    try:
        result = SizingResponse.model_validate(value)
        if (result.panels_in_sequence != request.panels_in_sequence
                or result.voc_cold.max_dc_voltage != request.inverter.max_dc_voltage):
            raise ValueError()
        return result.model_dump()
    except (ValueError, TypeError):
        raise CloudError("cloud_response_invalid", 502) from None


def size(params, tenant_id, job_id):
    parsed = validate_params(params)
    if any(type(value) is not str or not 1 <= len(value) <= 256 for value in (tenant_id, job_id)):
        raise CloudError("cloud_request_invalid", 400)
    grant = resolve_grant(parsed.grant_ref, tenant_id)
    raw = post_string_length(parsed.request, grant)
    try:
        if type(raw) is not bytes or len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError()

        def unique(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError()
                result[key] = value
            return result

        result = validate_response(json.loads(raw, object_pairs_hook=unique), parsed.request)
    except (ValueError, TypeError, RecursionError):
        raise CloudError("cloud_response_invalid", 502) from None
    request = parsed.request.model_dump()
    return {"endpoint": SIZING_URL, "adapter_version": "1.0.0", "tenant_id": tenant_id,
            "job_id": job_id, "request": request, "response": result,
            "request_sha256": digest(request), "response_sha256": digest(result),
            "wire_response_sha256": hashlib.sha256(raw).hexdigest()}


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def checked_graph(graph, expected_rev):
    result = require_revision(graph, expected_rev)
    units = result["project"]["units"]
    scale = {"m": 1, "mm": .001, "cm": .01, "km": 1000,
             "in": .0254, "ft": .3048, "yd": .9144}[units["drawing_units"]]
    if (not math.isclose(units["meters_per_unit"], scale, rel_tol=1e-9)
            or units["drawing_unit_is_feet"] != (units["drawing_units"] == "ft")):
        raise GraphValidationError("UNRESOLVED_UNITS")
    return result


def advance(graph, changed, tool):
    previous = graph["rev"]
    graph["rev"], graph["parent_rev"] = previous + 1, previous
    for entity in changed:
        entity["rev"] = graph["rev"]
        entity["provenance"].update(last_writer=tool, source_rev=previous, tool_id=tool)
    return validate_graph(graph)


def sizing_basis(graph):
    return digest({"project": graph["project"], "catalog_versions": graph["catalog_versions"],
                   "panels": [{"id": p["id"], "centre": p["centre"], "angle": p["angle"]}
                              for p in graph["panels"]],
                   "zones": [{key: z[key] for key in ("id", "panel_refs", "module_model",
                                                      "inverter_model_a")}
                             for z in graph["electrical_zones"]]})


def sizing_targets(graph, mode):
    panels = {p["id"] for p in graph["panels"]}
    if not panels:
        raise GraphValidationError("MISSING_PANEL")
    if mode == "global":
        return {graph["settings"]["id"]: graph["settings"]}
    if mode != "zones":
        raise GraphValidationError("INVALID_SIZING_MODE")
    covered = set()
    for zone in graph["electrical_zones"]:
        refs = zone["panel_refs"]
        if not refs or covered.intersection(refs) or not set(refs) <= panels:
            raise GraphValidationError("INVALID_ZONE_COVERAGE")
        covered.update(refs)
    if covered != panels:
        raise GraphValidationError("INVALID_ZONE_COVERAGE")
    return {z["id"]: z for z in graph["electrical_zones"]}


def require_sizing(graph):
    """Recheck drawing-owned evidence before grouping, including after reopen."""
    try:
        evidence = graph["settings"]["extra"]["string_sizing"]
        if evidence["basis_sha256"] != sizing_basis(graph):
            raise ValueError()
        targets = sizing_targets(graph, evidence["mode"])
        if set(evidence["records"]) != set(targets):
            raise ValueError()
        if graph["settings"]["global_string_sizing_confirmed"] != (evidence["mode"] == "global"):
            raise ValueError()
        for target_id, target in targets.items():
            record = evidence["records"][target_id]
            request = SizingRequest.model_validate(record["request"])
            response = validate_response(record["response"], request)
            if (record["endpoint"] != SIZING_URL or record["adapter_version"] != "1.0.0"
                    or record["request_sha256"] != digest(record["request"])
                    or record["response_sha256"] != digest(response)
                    or not response["voc_cold"]["passes"]
                    or target["voc_cold"] != response["voc_cold"]
                    or target["panels_in_sequence"] != response["panels_in_sequence"]):
                raise ValueError()
            if evidence["mode"] == "zones" and (
                    target["module_model"] != request.module.model
                    or target["inverter_model_a"] != request.inverter.model):
                raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise GraphValidationError("SIZING_CONFIRMATION_REQUIRED") from None
    return copy.deepcopy(evidence)
