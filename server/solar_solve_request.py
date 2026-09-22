"""Build frame-bound stringer requests using the plugin's measured sizing rule.

The frame supplies placement; graph panels supply geometry. Requests are checked
against both the wire contract and the solve binding before leaving this module.
"""
from __future__ import annotations

from pydantic import ValidationError

from leaf_cloud_client import StringerRequest
from solar_design_graph import GraphValidationError
from solar_solve_results import _frame_request


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
