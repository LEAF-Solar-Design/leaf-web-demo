"""Broker adapter for Leaf Automation string-length sizing, pinned to the plugin StringSizer.

The request mirrors Branch2025 StringSizerRequest and the response mirrors FunctionResults
(LeafSolarDesign.Core). Requests serialize as the plugin's JsonConvert does: declaration
order, compact, null tracker fields and a null module_parameters omitted. The recommended
length is simulation_results.standard.string_length truncated to int, and the cold-Voc
guard ports StringSizerVocColdGuard / NecVocGate with maxDcVoltage =
standard.string_design_voltage. Where the plugin silently skips the guard (Voc or design
voltage not positive, length below one) this adapter refuses the response instead.
Only the authenticated broker calls size(); grants never enter graph receipts.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import time
from typing import Annotated, Literal, Optional

import requests
from pydantic import Field, field_validator, model_validator

from leaf_cloud_client import StrictModel, canonical_bytes
from leaf_cloud_grants import CloudError, resolve_grant
from solar_design_graph import GraphValidationError, _bounded_json, require_revision, validate_graph

SIZING_URL = "https://api.leafdesign.ai/string-length"
MAX_RESPONSE_BYTES = 65536
ADAPTER_VERSION = "2.0.0"
# Evidence written by the provisional pre-pin adapter; read on reopen, never produced.
LEGACY_ADAPTER_VERSION = "1.0.0"

Text = Annotated[str, Field(min_length=1, max_length=256, pattern=r"\S")]
Note = Annotated[str, Field(max_length=4096)]
Real = Annotated[float, Field(ge=-1e7, le=1e7)]


class ModuleParameters(StrictModel):
    """Off-database module electrical parameters, the plugin's names and order."""
    V_oc_ref: Real
    I_sc_ref: Real
    V_mp_ref: Real
    I_mp_ref: Real
    alpha_sc: Real
    beta_oc: Real
    N_s: int = Field(ge=1, le=10000)
    STC: Real
    gamma_r: Real
    T_NOCT: Real


class RackingParams(StrictModel):
    racking_type: Literal["fixed_tilt", "single_axis"]
    surface_tilt: Text
    surface_azimuth: Text
    albedo: Text
    axis_tilt: Optional[Text] = None
    axis_azimuth: Optional[Text] = None
    max_angle: Optional[Text] = None
    backtrack: Optional[bool] = None
    gcr: Optional[Text] = None

    @model_validator(mode="after")
    def tracker_fields(self):
        # The plugin sets all five for a tracker and nulls all five for fixed tilt.
        tracker = [self.axis_tilt, self.axis_azimuth, self.max_angle, self.backtrack, self.gcr]
        expected = self.racking_type == "single_axis"
        if any((value is not None) != expected for value in tracker):
            raise ValueError("inconsistent racking parameters")
        return self


class SizingRequest(StrictModel):
    module_name: Text
    full_inverter_name: Text
    bifacial: bool
    bifacial_coefficient: Text
    racking_params: RackingParams
    max_voltage: Text
    thermal_model_type: Text
    open_circuit_rise: bool
    zip_code: Text
    module_parameters: Optional[ModuleParameters] = None

    def wire(self):
        """The plugin's JSON object: declaration order, null optionals omitted."""
        return self.model_dump(exclude_none=True)


def wire_bytes(request):
    return json.dumps(request.wire(), separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


class SimulationResult(StrictModel):
    Conditions: Note
    max_module_voltage: Real
    string_design_voltage: float = Field(gt=0, le=100000)
    safety_factor: Optional[Real] = None
    string_length: float = Field(ge=1, le=4096)
    cell_temperature: Optional[Real] = Field(default=None, alias="Cell Temperature")
    poa_irradiance: Optional[Real] = Field(default=None, alias="POA Irradiance")
    long_note: Optional[Note] = None
    short_note: Optional[Note] = None

    @field_validator("string_design_voltage")
    @classmethod
    def integral_voltage(cls, value):
        # FunctionResults declares string_design_voltage as int.
        if not float(value).is_integer():
            raise ValueError("string_design_voltage must be integral")
        return value


class SimulationResults(StrictModel):
    standard: SimulationResult
    conservative: Optional[SimulationResult] = None
    day: Optional[SimulationResult] = None
    nsrdb: Optional[SimulationResult] = None
    ashrae_1: Optional[SimulationResult] = None
    ashrae_2: Optional[SimulationResult] = None


class SizingResponse(StrictModel):
    cells: int = Field(ge=0, le=10000)
    voc: float = Field(gt=0, le=2000)
    isc: Real
    pmp: Real
    vmp: Real
    imp: Real
    bpmp: Real
    bvoc: Real
    alpha_sc: Real
    weather_mode: Optional[Note] = None
    min_temp: float = Field(ge=-100, le=100)
    mintemp: Optional[Real] = None
    simulation_results: SimulationResults


def compute_voc_cold(voc_stc, temp_coeff_pct_per_c, temp_min_c):
    """NecVocGate.ComputeVocCold, same operation order so the doubles match."""
    delta = temp_min_c - 25.0
    return voc_stc * (1.0 + temp_coeff_pct_per_c / 100.0 * delta)


def recommend(response):
    """Plugin recommendation and StringSizerVocColdGuard.Evaluate on the standard scenario."""
    standard = response.simulation_results.standard
    length = int(standard.string_length)  # C# (int) cast truncates toward zero
    max_dc_voltage = float(int(standard.string_design_voltage))
    per_module = compute_voc_cold(response.voc, response.bvoc, response.min_temp)
    if length < 1 or not math.isfinite(per_module) or per_module <= 0:
        raise ValueError("incomplete guard inputs")
    string_voltage = per_module * length
    passes = string_voltage <= max_dc_voltage
    suggested = 0 if passes else max(1, math.floor(max_dc_voltage / per_module))
    return {"panels_in_sequence": length, "voc_cold": {
        "passes": passes, "override_accepted": False, "suggested_string_length": suggested,
        "per_module": per_module, "string_voltage": string_voltage,
        "max_dc_voltage": max_dc_voltage}}


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
            SIZING_URL, data=wire_bytes(request),
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


def validate_response(value):
    """Validate a service response and return {panels_in_sequence, voc_cold}."""
    try:
        return recommend(SizingResponse.model_validate(value))
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

        response = json.loads(raw, object_pairs_hook=unique)
        sizing = validate_response(response)
        response_sha256 = digest(response)
    except (ValueError, TypeError, RecursionError):
        raise CloudError("cloud_response_invalid", 502) from None
    request = parsed.request.wire()
    return {"endpoint": SIZING_URL, "adapter_version": ADAPTER_VERSION, "tenant_id": tenant_id,
            "job_id": job_id, "request": request, "response": response, "sizing": sizing,
            "request_sha256": digest(request), "response_sha256": response_sha256,
            "wire_response_sha256": hashlib.sha256(raw).hexdigest()}


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def units_resolved(graph):
    """True when the drawing's unit name and scale agree, so lengths convert without a guess."""
    units = graph["project"]["units"]
    scale = {"m": 1, "mm": .001, "cm": .01, "km": 1000,
             "in": .0254, "ft": .3048, "yd": .9144}[units["drawing_units"]]
    return (math.isclose(units["meters_per_unit"], scale, rel_tol=1e-9)
            and units["drawing_unit_is_feet"] == (units["drawing_units"] == "ft"))


def checked_graph(graph, expected_rev):
    result = require_revision(graph, expected_rev)
    if not units_resolved(result):
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


class _LegacyModule(StrictModel):
    model: str = Field(min_length=1, max_length=256, pattern=r"\S")
    voc: float = Field(gt=0, le=1000)
    temp_coeff_pct_per_c: float = Field(ge=-10, le=0)


class _LegacyInverter(StrictModel):
    model: str = Field(min_length=1, max_length=256, pattern=r"\S")
    max_dc_voltage: float = Field(gt=0, le=2000)


class _LegacyRequest(StrictModel):
    schema_version: Literal["leaf.string-length.v1"]
    module: _LegacyModule
    inverter: _LegacyInverter
    design_min_temp_c: float = Field(ge=-100, le=25)
    panels_in_sequence: int = Field(ge=1, le=4096)
    units: Literal["SI"]


class _LegacyColdVoltage(StrictModel):
    passes: bool
    override_accepted: Literal[False]
    suggested_string_length: int = Field(ge=1, le=4096)
    per_module: float = Field(gt=0, le=2000)
    string_voltage: float = Field(gt=0, le=10000000)
    max_dc_voltage: float = Field(gt=0, le=2000)


class _LegacyResponse(StrictModel):
    panels_in_sequence: int = Field(ge=1, le=4096)
    voc_cold: _LegacyColdVoltage

    @model_validator(mode="after")
    def coherent(self):
        cold = self.voc_cold
        if (not math.isclose(cold.string_voltage, cold.per_module * self.panels_in_sequence,
                             rel_tol=1e-8, abs_tol=1e-6)
                or cold.passes != (cold.string_voltage <= cold.max_dc_voltage)
                or cold.suggested_string_length * cold.per_module > cold.max_dc_voltage + 1e-6):
            raise ValueError("inconsistent cold voltage")
        return self


def _record_outcome(record):
    """(sizing, module model, inverter model) re-derived from one evidence record."""
    if record["adapter_version"] == ADAPTER_VERSION:
        request = SizingRequest.model_validate(record["request"])
        if (record["request"] != request.wire()
                or record["response_sha256"] != digest(record["response"])
                or record["sizing"] != recommend(SizingResponse.model_validate(record["response"]))):
            raise ValueError()
        return record["sizing"], request.module_name, request.full_inverter_name
    if record["adapter_version"] != LEGACY_ADAPTER_VERSION:
        raise ValueError()
    request = _LegacyRequest.model_validate(record["request"])
    response = _LegacyResponse.model_validate(record["response"]).model_dump()
    if (response["panels_in_sequence"] != request.panels_in_sequence
            or response["voc_cold"]["max_dc_voltage"] != request.inverter.max_dc_voltage
            or record["response_sha256"] != digest(response)):
        raise ValueError()
    return response, request.module.model, request.inverter.model


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
            sizing, module_model, inverter_model = _record_outcome(record)
            if (record["endpoint"] != SIZING_URL
                    or record["request_sha256"] != digest(record["request"])
                    or not sizing["voc_cold"]["passes"]
                    or target["voc_cold"] != sizing["voc_cold"]
                    or target["panels_in_sequence"] != sizing["panels_in_sequence"]):
                raise ValueError()
            if evidence["mode"] == "zones" and (
                    target["module_model"] != module_model
                    or target["inverter_model_a"] != inverter_model):
                raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise GraphValidationError("SIZING_CONFIRMATION_REQUIRED") from None
    return copy.deepcopy(evidence)
