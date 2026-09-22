"""Build frame-bound stringer requests using the plugin's measured sizing rule.

The frame supplies placement; graph panels supply geometry. Requests are checked
against both the wire contract and the solve binding before leaving this module.
"""
from __future__ import annotations

from pydantic import ValidationError

from leaf_cloud_client import StringerRequest
from solar_design_graph import GraphValidationError
from solar_grid_restitch import escalate, failed_group_indices
from solar_grid_split import needs_splitting, split_group
from solar_solve_results import _frame_pieces, _frame_request


def plugin_sequences(panel_count: int, max_string_length: int) -> list[int]:
    if any(type(value) is not int or not 1 <= value <= 900
           for value in (panel_count, max_string_length)):
        raise GraphValidationError("INVALID_STRING_SIZING")
    count = (panel_count + max_string_length - 1) // max_string_length
    base, remainder = divmod(panel_count, count)
    if base + 1 <= max_string_length:
        return [base + 1, base, remainder, count - remainder]
    return [base, base - 1, count, 0]


def build_stringer_request(graph: dict, frame_ref: str, *,
                           max_string_length: int, dwgname: str) -> dict:
    frame = next((frame for frame in graph["frames"] if frame["id"] == frame_ref), None)
    if frame is None:
        raise GraphValidationError("MISSING_FRAME")
    panels = {panel["id"]: panel for panel in graph["panels"]}
    rows, count = [], 0
    for row in frame["matrix"]:
        cells = []
        for cell in row:
            ref = cell["panel_ref"]
            wire = {"Code": 0, "Id": "", "Seq": 0, "InverterId": -1,
                    "StringInputNumber": 0, "X": 0.0, "Y": 0.0, "Angle": 0.0}
            if ref is not None:
                if ref not in panels:
                    raise GraphValidationError("SOLVE_GRID_MISMATCH")
                panel = panels[ref]
                wire.update(Code=1, Id=ref, X=panel["centre"][0],
                            Y=panel["centre"][1], Angle=panel["angle"])
                count += 1
            cells.append(wire)
        rows.append({"Panels": cells})
    request = {"grid": {"Dwgname": dwgname,
                        "Sequences": plugin_sequences(count, max_string_length),
                        "Rows": rows, "Modify": []}}
    try:
        StringerRequest.model_validate(request)
    except ValidationError:
        raise GraphValidationError("SOLVE_REQUEST_INVALID") from None
    _frame_request(graph, frame_ref, request)
    return request


# BranchCmd.cs SolveBatchAsync: at most five rounds; escalate() exhausts after four.
MAX_RETRY_ROUNDS = 5


def _frame(graph, frame_ref):
    frame = next((frame for frame in graph["frames"] if frame["id"] == frame_ref), None)
    if frame is None:
        raise GraphValidationError("MISSING_FRAME")
    return frame


def frame_split_grid(graph: dict, frame_ref: str, *, max_string_length: int) -> dict:
    """The plugin's group grid for one frame: panel ids in cells, plugin sizing."""
    frame = _frame(graph, frame_ref)
    rows, count = [], 0
    for row in frame["matrix"]:
        cells = []
        for cell in row:
            ref = cell["panel_ref"]
            cells.append({"Code": 1, "Id": ref, "Seq": 0} if ref is not None
                         else {"Code": 0, "Id": "", "Seq": 0})
            count += ref is not None
        rows.append({"Panels": cells})
    return {"Sequences": plugin_sequences(count, max_string_length), "Rows": rows}


def needs_split_solve(graph: dict, frame_ref: str, *, max_string_length: int) -> bool:
    """GridSplitHelper.NeedsSplitting on the frame: 200+ panels or over 30 rows or columns."""
    return needs_splitting(frame_split_grid(graph, frame_ref,
                                            max_string_length=max_string_length))


def build_split_requests(graph: dict, frame_ref: str, *, max_string_length: int,
                         dwgname: str, depth: int = 10, jogs: int = 1) -> list[dict]:
    """One bound stringer request per piece of split_group, in emission order.

    Each piece keeps the port's layout and Sequences; cells carry Studio's panel
    geometry and Modify is empty. row_indices map piece rows to frame rows.
    """
    grid = frame_split_grid(graph, frame_ref, max_string_length=max_string_length)
    panels = {panel["id"]: panel for panel in graph["panels"]}
    pieces = []
    for piece in split_group(grid, depth=depth, jogs=jogs):
        rows = []
        for row in piece["Rows"]:
            cells = []
            for cell in row["Panels"]:
                wire = {"Code": 0, "Id": "", "Seq": 0, "InverterId": -1,
                        "StringInputNumber": 0, "X": 0.0, "Y": 0.0, "Angle": 0.0}
                if cell["Code"] == 1:
                    panel = panels.get(cell["Id"])
                    if panel is None:
                        raise GraphValidationError("SOLVE_GRID_MISMATCH")
                    wire.update(Code=1, Id=cell["Id"], X=panel["centre"][0],
                                Y=panel["centre"][1], Angle=panel["angle"])
                cells.append(wire)
            rows.append({"Panels": cells})
        pieces.append({
            "request": {"grid": {"Dwgname": dwgname, "Sequences": list(piece["Sequences"]),
                                 "Rows": rows, "Modify": []}},
            "row_indices": list(piece.get("RowIndices", range(len(rows)))),
        })
    _frame_pieces(graph, frame_ref, pieces)
    return pieces


def solve_split_frame(graph: dict, frame_ref: str, *, max_string_length: int,
                      dwgname: str, solve_piece) -> dict:
    """The plugin's retry loop (BranchCmd.cs SolveBatchAsync) for one frame.

    solve_piece(index, piece) makes one stringer call and returns the proposal
    envelope, or None when the service answer is one the plugin retries
    (cloud_piece_failed). Any other error propagates, as the plugin throws.
    Any failed piece re-splits the whole frame with escalated settings:
    jogs 1 to 2, then depth 10 to 1 to 0, then SPLIT_SOLVE_EXHAUSTED.
    """
    state = {"jogs": 1, "depth": 10, "exhausted": False}
    for _ in range(MAX_RETRY_ROUNDS):
        pieces = build_split_requests(graph, frame_ref, max_string_length=max_string_length,
                                      dwgname=dwgname, depth=state["depth"],
                                      jogs=state["jogs"])
        results = [solve_piece(index, piece) for index, piece in enumerate(pieces)]
        responses = [result["proposal"] if isinstance(result, dict) and "proposal" in result
                     else "" for result in results]
        if not failed_group_indices(responses, [list(range(len(pieces)))]):
            return {"pieces": pieces, "proposals": results,
                    "split_state": {"jogs": state["jogs"], "depth": state["depth"]}}
        state = escalate(state)
        if state["exhausted"]:
            break
    raise GraphValidationError("SPLIT_SOLVE_EXHAUSTED")
