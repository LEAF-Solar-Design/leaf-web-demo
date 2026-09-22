"""Trusted Leaf Automation stringer adapter, pinned to the recorded grid contract.

Wire requests use PascalCase MatrixJson fields under "grid". The response is
kept in wire coordinates for provenance; visited_path in the proposal envelope
uses zero-based original matrix indices. No drawing is changed.
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
from solar_grid_restitch import is_response_failed

TOOL_NAME = "solar-solve-proposal"
SOLVER_URL = "https://api.leafdesign.ai/api/ml/"
MAX_RESPONSE_BYTES = 1024 * 1024


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Panel(StrictModel):
    Code: Annotated[int, Field(ge=-1, le=1)]
    Id: str = Field(max_length=128)
    Seq: int = Field(ge=0, le=900)
    InverterId: int = Field(ge=-1, le=1000000)
    StringInputNumber: int = Field(ge=0, le=1000000)
    X: float = Field(ge=-1e12, le=1e12)
    Y: float = Field(ge=-1e12, le=1e12)
    Angle: float = Field(ge=-1e6, le=1e6)


class PanelRow(StrictModel):
    Panels: list[Panel] = Field(min_length=1, max_length=30)


class MatrixJson(StrictModel):
    Dwgname: str = Field(max_length=256)
    Sequences: list[list[Annotated[int, Field(ge=0, le=900)]]] = Field(
        min_length=2, max_length=2)
    Rows: list[PanelRow] = Field(min_length=1, max_length=30)
    Modify: list[int] = Field(max_length=0)

    @model_validator(mode="before")
    @classmethod
    def reshape_sequences(cls, value):
        if isinstance(value, dict):
            sequences = value.get("Sequences")
            if (isinstance(sequences, list) and len(sequences) == 4
                    and all(type(n) is int for n in sequences)):
                value = dict(value, Sequences=[sequences[:2], sequences[2:]])
        return value

    @model_validator(mode="after")
    def coherent_matrix(self):
        if any(len(pair) != 2 for pair in self.Sequences):
            raise ValueError("expected two sequence lengths and two counts")
        if any(len(row.Panels) != len(self.Rows[0].Panels) for row in self.Rows):
            raise ValueError("expected rectangular matrix")
        panels = [p for row in self.Rows for p in row.Panels if p.Code == 1]
        ids = [p.Id for row in self.Rows for p in row.Panels if p.Id]
        if not panels or any(not p.Id for p in panels) or len(ids) != len(set(ids)):
            raise ValueError("expected unique panel ids")
        lengths, counts = self.Sequences
        if any(count and not length for length, count in zip(lengths, counts)):
            raise ValueError("invalid sequence length")
        if sum(length * count for length, count in zip(lengths, counts)) != len(panels):
            raise ValueError("sequence counts do not cover panels")
        return self


class StringerRequest(StrictModel):
    grid: MatrixJson

    @property
    def kept_row_indices(self) -> list[int]:
        return [r for r, row in enumerate(self.grid.Rows)
                if any(p.Code == 1 for p in row.Panels)]

    @property
    def kept_column_indices(self) -> list[int]:
        return [c for c in range(len(self.grid.Rows[0].Panels))
                if any(self.grid.Rows[r].Panels[c].Code == 1
                       for r in self.kept_row_indices)]

    def wire_payload(self) -> dict:
        grid = self.grid.model_dump()
        grid["Rows"] = [{"Panels": [grid["Rows"][r]["Panels"][c]
                                  for c in self.kept_column_indices]}
                        for r in self.kept_row_indices]
        return {"grid": grid}


class ProposalParams(StrictModel):
    grant_ref: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    request: StringerRequest


Count = Annotated[int, Field(ge=0, le=1000000)]
Coordinate = Annotated[int, Field(ge=1, le=30)]
ShortText = Annotated[str, Field(min_length=1, max_length=256)]


class StringerInfo(StrictModel):
    steps_taken: Count
    sequence_length: list[Annotated[int, Field(ge=1, le=900)]] = Field(
        min_length=1, max_length=900)
    num_panels: float = Field(ge=1, le=900)
    visited_path: list[Annotated[list[Coordinate], Field(min_length=2, max_length=2)]] = Field(
        min_length=1, max_length=900)
    steps_taken_in_string: Count
    horizontal_movements: Count
    vertical_movements: Count
    invalid_vertical_movements: Count
    distance_total: float = Field(ge=0, le=1e15)
    vertical_sequential: Count
    diagonal_movements: Count
    allow_one_gap_hops: bool
    max_gap_hop: Count
    gap_hop_movements: Count
    vertical_temp: int = Field(ge=-1, le=1000000)
    remaining_string_length: Count
    sequence_length_index: Count
    str_length_state_index: Count
    terminated: Literal[True]
    last_agent_x: Coordinate
    last_agent_y: Coordinate


class BestResult(StrictModel):
    last_action: Count
    terminated: Literal[True]
    info: StringerInfo
    beam_idx: Count
    grid_id: ShortText
    model_id: ShortText
    gumbel_scale: float = Field(ge=0, le=1e6)
    string_start_split_count: Count
    model: ShortText


class StringerData(StrictModel):
    status: Literal["completed"]
    best_result: BestResult
    final_grid: MatrixJson
    model_used: ShortText
    gumbel_scale_used: float = Field(ge=0, le=1e6)
    distance_total: float = Field(ge=0, le=1e15)
    total_valid_solutions: Count
    gumbel_summary: dict[Annotated[str, Field(max_length=64)], Count] = Field(max_length=128)
    first_pass_best_distance: float = Field(ge=0, le=1e15)
    improvement: Annotated[float, Field(ge=-1e15, le=1e15)] | None
    second_pass_triggered: bool


class StringerResponse(StrictModel):
    status: Literal["completed"]
    data: StringerData
    job_id: ShortText

    def original_visited_path(self, request: StringerRequest) -> list[list[int]]:
        """Validate complete coverage and map one-based wire cells to original indices."""
        info = self.data.best_result.info
        rows, cols = request.kept_row_indices, request.kept_column_indices
        path = []
        for row, col in info.visited_path:
            if row > len(rows) or col > len(cols):
                raise ValueError("visited cell outside submitted grid")
            path.append([rows[row - 1], cols[col - 1]])
        expected = {(r, c) for r, row in enumerate(request.grid.Rows)
                    for c, panel in enumerate(row.Panels) if panel.Code == 1}
        if (len(path) != len(expected) or {tuple(cell) for cell in path} != expected
                or info.num_panels != len(path) or info.steps_taken != len(path)
                or sum(info.sequence_length) != len(path)):
            raise ValueError("incomplete or duplicate visited path")
        lengths, counts = request.grid.Sequences
        expected_lengths = [length for length, count in zip(lengths, counts)
                            for _ in range(count)]
        if sorted(info.sequence_length) != sorted(expected_lengths):
            raise ValueError("unexpected string lengths")
        self.check_echo(request)
        return path

    def check_echo(self, request: StringerRequest) -> None:
        """The final_grid must echo the sent (cropped) grid apart from Seq, under the job id."""
        self.check_grid_echo(request)
        if self.data.best_result.grid_id != self.job_id:
            raise ValueError("response grid does not match request")

    def check_grid_echo(self, request: StringerRequest) -> None:
        """The final_grid alone: the one check a partial beam (no grid_id) can still keep."""
        sent = request.wire_payload()["grid"]
        final = self.data.final_grid.model_dump()
        for row in final["Rows"]:
            for panel in row["Panels"]:
                panel["Seq"] = 0
        for row in sent["Rows"]:
            for panel in row["Panels"]:
                panel["Seq"] = 0
        if final != sent:
            raise ValueError("response grid does not match request")


class PiecePanelRow(StrictModel):
    Panels: list[Panel] = Field(min_length=1, max_length=300)


class PieceMatrixJson(MatrixJson):
    """A split piece keeps its frame's raw rows and width; only the wire grid is capped at 30."""
    Rows: list[PiecePanelRow] = Field(min_length=1, max_length=300)


class PieceStringerRequest(StringerRequest):
    grid: PieceMatrixJson

    @model_validator(mode="after")
    def wire_grid_within_limits(self):
        try:
            StringerRequest.model_validate(self.wire_payload())
        except ValidationError:
            raise ValueError("piece wire grid exceeds the stringer limits") from None
        return self


class PieceProposalParams(StrictModel):
    grant_ref: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    request: PieceStringerRequest


class PartialBeamInfo(StrictModel):
    """The service's partial-beam fallback: no path or lengths. The plugin commits it
    (GridSplitHelper.IsResponseFailed tests presence only), so a piece accepts it."""
    distance_total: float = Field(ge=0, le=1e15)


class PartialBeamBestResult(StrictModel):
    """Measured live 2026-09-22: the fallback's best_result carries ONLY info; last_action,
    terminated, beam_idx, grid_id, model_id, gumbel_scale, string_start_split_count and
    model are all absent, so this is a separate shape rather than a loosened BestResult."""
    info: PartialBeamInfo


class PieceStringerData(StringerData):
    """A full piece answer: the whole-frame data, plus the message the service may add."""
    message: Annotated[str, Field(max_length=256)] | None = None


class PartialBeamStringerData(StrictModel):
    """The fallback's data as the service sends it: no gumbel_summary, first_pass_best_distance,
    improvement or second_pass_triggered, and a message naming the finalizer fallback."""
    status: Literal["completed"]
    best_result: PartialBeamBestResult
    final_grid: MatrixJson
    model_used: ShortText
    gumbel_scale_used: float = Field(ge=0, le=1e6)
    distance_total: float = Field(ge=0, le=1e15)
    total_valid_solutions: Count
    message: Annotated[str, Field(min_length=1, max_length=256)]


class PieceStringerResponse(StringerResponse):
    # Full shape first, so a full answer is never read as the fallback; a full answer missing
    # any field is refused by both members. Whole-frame StringerResponse never takes the fallback.
    data: PieceStringerData | PartialBeamStringerData = Field(union_mode="left_to_right")

    @property
    def partial_beam(self) -> bool:
        return isinstance(self.data, PartialBeamStringerData)

    def original_visited_path(self, request: StringerRequest) -> list[list[int]]:
        """Full answers keep every single-frame check; a partial beam has no path, lengths or
        grid_id, so it keeps the final_grid echo and yields no path."""
        if self.partial_beam:
            self.check_grid_echo(request)
            return []
        return super().original_visited_path(request)


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
        path = response.original_visited_path(parsed.request)
        request_hash = hashlib.sha256(canonical_bytes(parsed.request.wire_payload())).hexdigest()
        if (result.get("schema_version") != "leaf.solar-proposal.v1"
                or result.get("job_id") != job_id or result.get("tenant_id") != tenant_id
                or result.get("drawing_changed") is not False
                or result.get("request_sha256") != request_hash
                or result.get("solver") != {"endpoint": SOLVER_URL, "adapter_version": "1.0.0"}
                or not isinstance(result.get("response_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", result["response_sha256"])
                or result.get("visited_path") != path):
            raise ValueError()
        return {"execution_mode": "leaf_cloud_service", "solver": result["solver"],
                "request_sha256": request_hash, "response_sha256": result["response_sha256"]}
    except (KeyError, AttributeError, TypeError, ValueError, CloudError):
        raise ValueError("cloud proposal terminal proof rejected") from None


def validate_piece_params(params: dict) -> PieceProposalParams:
    try:
        return PieceProposalParams.model_validate(params)
    except (ValidationError, ValueError, TypeError):
        raise CloudError("cloud_request_invalid", 400) from None


def _piece_response(raw_or_object, request: PieceStringerRequest) -> tuple[PieceStringerResponse, list]:
    """Refuse what the plugin would retry, then apply the piece contract. Fails closed."""
    probe = raw_or_object.decode("utf-8") if isinstance(raw_or_object, bytes) else raw_or_object
    if is_response_failed(probe):
        raise CloudError("cloud_piece_failed", 502)
    if isinstance(raw_or_object, bytes):
        response = PieceStringerResponse.model_validate_json(raw_or_object)
    else:
        response = PieceStringerResponse.model_validate(raw_or_object)
    return response, response.original_visited_path(request)


def piece_proposal_provenance(result: dict, params: dict, tenant_id: str, job_id: str) -> dict:
    """proposal_provenance for one split piece; adds the partial-beam flag."""
    try:
        parsed = validate_piece_params(params)
        response, path = _piece_response(result["proposal"], parsed.request)
        request_hash = hashlib.sha256(canonical_bytes(parsed.request.wire_payload())).hexdigest()
        if (result.get("schema_version") != "leaf.solar-proposal.v1"
                or result.get("job_id") != job_id or result.get("tenant_id") != tenant_id
                or result.get("drawing_changed") is not False
                or result.get("request_sha256") != request_hash
                or result.get("solver") != {"endpoint": SOLVER_URL, "adapter_version": "1.0.0"}
                or not isinstance(result.get("response_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", result["response_sha256"])
                or result.get("visited_path") != path):
            raise ValueError()
        return {"execution_mode": "leaf_cloud_service", "solver": result["solver"],
                "request_sha256": request_hash, "response_sha256": result["response_sha256"],
                "partial_beam": response.partial_beam}
    except (KeyError, AttributeError, TypeError, ValueError, CloudError):
        raise ValueError("cloud piece proposal terminal proof rejected") from None


def piece_proposal(params: dict, tenant_id: str, job_id: str) -> dict:
    """One stringer call for one split piece. A plugin-failed answer raises
    cloud_piece_failed so the split retry loop can escalate; nothing else is retried."""
    parsed = validate_piece_params(params)
    grant = resolve_grant(parsed.grant_ref, tenant_id)
    raw = post_stringer(parsed.request, grant)
    if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
        raise CloudError("cloud_response_invalid", 502)
    try:
        result, path = _piece_response(raw, parsed.request)
    except CloudError:
        raise
    except (ValidationError, ValueError, TypeError):
        raise CloudError("cloud_response_invalid", 502) from None
    return {"schema_version": "leaf.solar-proposal.v1", "job_id": job_id,
            "tenant_id": tenant_id, "drawing_changed": False,
            "request_sha256": hashlib.sha256(canonical_bytes(parsed.request.wire_payload())).hexdigest(),
            "response_sha256": hashlib.sha256(raw).hexdigest(),
            "solver": {"endpoint": SOLVER_URL, "adapter_version": "1.0.0"},
            "proposal": result.model_dump(exclude_none=False), "visited_path": path}


def post_stringer(request: StringerRequest, grant: CloudGrant) -> bytes:
    """Only the broker calls this transport; no redirects or response logging."""
    try:
        deadline = time.monotonic() + 50
        with requests.post(
            SOLVER_URL, data=canonical_bytes(request.wire_payload()),
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
        path = result.original_visited_path(parsed.request)
    except (ValidationError, ValueError, TypeError):
        raise CloudError("cloud_response_invalid", 502) from None
    return {"schema_version": "leaf.solar-proposal.v1", "job_id": job_id,
            "tenant_id": tenant_id, "drawing_changed": False,
            "request_sha256": hashlib.sha256(canonical_bytes(parsed.request.wire_payload())).hexdigest(),
            "response_sha256": hashlib.sha256(raw).hexdigest(),
            "solver": {"endpoint": SOLVER_URL, "adapter_version": "1.0.0"},
            "proposal": result.model_dump(), "visited_path": path}
