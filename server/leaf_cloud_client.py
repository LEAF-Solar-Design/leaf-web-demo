"""Trusted Leaf Automation stringer adapter, proposal contract v1.

Wire JSON schema (to be pinned against a recorded plugin request in the w1 spike):
{"type":"object","additionalProperties":false,
 "required":["schema_version","panel_groups","matrix_cells","electrical","units"],
 "properties":{
   "schema_version":{"const":"leaf.stringer-request.v1"},
   "panel_groups":{"type":"array","minItems":1,"maxItems":128,
     "items":{"type":"object","additionalProperties":false,"required":["id","panel_ids"],
       "properties":{"id":{"type":"integer","minimum":0,"maximum":1000000},
       "panel_ids":{"type":"array","minItems":1,"maxItems":4096,
         "items":{"type":"integer","minimum":0,"maximum":1000000}}}}},
   "matrix_cells":{"type":"array","minItems":1,"maxItems":4096,
     "items":{"type":"object","additionalProperties":false,
       "required":["panel_id","row","column"],"properties":{
       "panel_id":{"type":"integer","minimum":0,"maximum":1000000},
       "row":{"type":"integer","minimum":0,"maximum":4095},
       "column":{"type":"integer","minimum":0,"maximum":4095}}}},
   "electrical":{"type":"object","additionalProperties":false,
     "required":["module_voc","max_system_voltage","design_min_temp_c","temp_coeff_pct_per_c"],
     "properties":{"module_voc":{"type":"number","exclusiveMinimum":0,"maximum":1000},
       "max_system_voltage":{"type":"number","exclusiveMinimum":0,"maximum":2000},
       "design_min_temp_c":{"type":"number","minimum":-100,"maximum":100},
       "temp_coeff_pct_per_c":{"type":"number","minimum":-10,"maximum":0}}},
   "units":{"const":"SI"}}}

The provisional response is {"strings": [{"panel_ids": [integer, ...]}]}.
Unknown response fields are rejected. This is not evidence of plugin parity.
Sizing and wiring can reuse the trusted transport and grant interface later.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Annotated, Literal

import requests
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from leaf_cloud_grants import CloudError, CloudGrant, resolve_grant

TOOL_NAME = "solar-solve-proposal"
SOLVER_URL = "https://api.leafdesign.ai/api/ml/"
MAX_RESPONSE_BYTES = 1024 * 1024
PanelId = Annotated[int, Field(strict=True, ge=0, le=1000000)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class PanelGroup(StrictModel):
    id: PanelId
    panel_ids: list[PanelId] = Field(min_length=1, max_length=4096)


class MatrixCell(StrictModel):
    panel_id: PanelId
    row: int = Field(ge=0, le=4095)
    column: int = Field(ge=0, le=4095)


class Electrical(StrictModel):
    module_voc: float = Field(gt=0, le=1000)
    max_system_voltage: float = Field(gt=0, le=2000)
    design_min_temp_c: float = Field(ge=-100, le=100)
    temp_coeff_pct_per_c: float = Field(ge=-10, le=0)


class StringerRequest(StrictModel):
    schema_version: Literal["leaf.stringer-request.v1"]
    panel_groups: list[PanelGroup] = Field(min_length=1, max_length=128)
    matrix_cells: list[MatrixCell] = Field(min_length=1, max_length=4096)
    electrical: Electrical
    units: Literal["SI"]

    @model_validator(mode="after")
    def coherent_matrix(self):
        panels = [p for g in self.panel_groups for p in g.panel_ids]
        cells = [c.panel_id for c in self.matrix_cells]
        if (len(panels) > 4096 or len(set(panels)) != len(panels)
                or len(set(g.id for g in self.panel_groups)) != len(self.panel_groups)
                or len(set(cells)) != len(cells) or set(cells) != set(panels)
                or len({(c.row, c.column) for c in self.matrix_cells}) != len(cells)):
            raise ValueError("incoherent panel matrix")
        return self


class ProposalParams(StrictModel):
    grant_ref: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    request: StringerRequest


class StringResult(StrictModel):
    panel_ids: list[PanelId] = Field(min_length=1, max_length=4096)


class StringerResponse(StrictModel):
    strings: list[StringResult] = Field(min_length=1, max_length=4096)


def validate_params(params: dict) -> ProposalParams:
    try:
        return ProposalParams.model_validate(params)
    except (ValidationError, ValueError, TypeError):
        raise CloudError("cloud_request_invalid", 400) from None


def canonical_bytes(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def proposal_provenance(result: dict, params: dict, tenant_id: str, job_id: str) -> dict:
    """Validate a broker receipt against the durable job before recording success."""
    try:
        parsed = validate_params(params)
        response = StringerResponse.model_validate(result["proposal"])
        panels = [p for string in response.strings for p in string.panel_ids]
        request_hash = hashlib.sha256(canonical_bytes(parsed.request.model_dump())).hexdigest()
        if (result.get("schema_version") != "leaf.solar-proposal.v1"
                or result.get("job_id") != job_id or result.get("tenant_id") != tenant_id
                or result.get("drawing_changed") is not False
                or result.get("request_sha256") != request_hash
                or result.get("solver") != {"endpoint": SOLVER_URL, "adapter_version": "1.0.0"}
                or not isinstance(result.get("response_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", result["response_sha256"])
                or len(panels) != len(set(panels))
                or set(panels) != {c.panel_id for c in parsed.request.matrix_cells}):
            raise ValueError()
        return {"execution_mode": "leaf_cloud_service", "solver": result["solver"],
                "request_sha256": request_hash, "response_sha256": result["response_sha256"]}
    except (KeyError, AttributeError, TypeError, ValueError, CloudError):
        raise ValueError("cloud proposal terminal proof rejected") from None


def post_stringer(request: StringerRequest, grant: CloudGrant) -> bytes:
    """Only the broker calls this transport; no redirects or response logging."""
    try:
        deadline = time.monotonic() + 50
        with requests.post(
            SOLVER_URL, data=canonical_bytes(request.model_dump()),
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
            chunks = bytearray()
            for chunk in response.iter_content(16384):
                chunks.extend(chunk)
                if len(chunks) > MAX_RESPONSE_BYTES or time.monotonic() > deadline:
                    raise CloudError("cloud_response_invalid", 502)
            return bytes(chunks)
    except requests.RequestException:
        raise CloudError("cloud_upstream_failure", 502) from None


def proposal(params: dict, tenant_id: str, job_id: str) -> dict:
    parsed = validate_params(params)
    grant = resolve_grant(parsed.grant_ref, tenant_id)
    raw = post_stringer(parsed.request, grant)
    try:
        if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError()
        result = StringerResponse.model_validate_json(raw)
        panels = [p for s in result.strings for p in s.panel_ids]
        expected = {c.panel_id for c in parsed.request.matrix_cells}
        if len(panels) != len(set(panels)) or set(panels) != expected:
            raise ValueError()
    except (ValidationError, ValueError, TypeError):
        raise CloudError("cloud_response_invalid", 502) from None
    return {"schema_version": "leaf.solar-proposal.v1", "job_id": job_id,
            "tenant_id": tenant_id, "drawing_changed": False,
            "request_sha256": hashlib.sha256(canonical_bytes(parsed.request.model_dump())).hexdigest(),
            "response_sha256": hashlib.sha256(raw).hexdigest(),
            "solver": {"endpoint": SOLVER_URL, "adapter_version": "1.0.0"},
            "proposal": result.model_dump()}
