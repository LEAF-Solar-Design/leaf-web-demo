"""Deterministic assignment bytes from current graph data, without electrical sizing."""
import re

import solar_design_graph as contract
import solar_pvcase_graph as graph_kernel

CODES = graph_kernel.CODES + (
    "FRAME_SEQUENCE_MISMATCH", "MATRIX_INPUT_MISMATCH", "PVG_BYTE_LIMIT",
)
COLUMNS = (
    "panel_handle", "parity_l2_number", "parity_string_number", "group_handle",
    "frame_ref", "panel_ref", "string_ref", "row", "col", "centre_m", "inverter_ref",
)


def assignment_export(graph: dict, envelope: dict | bytes) -> bytes:
    """Validate membership and export current assignments; never invoke the solver."""
    target, projection = graph_kernel._checked(graph, envelope, output=True)
    refuse = graph_kernel._refuse
    if any(target[k] for k in ("inverters", "routes", "schedules")):
        refuse("PVG_TARGET_NOT_EMPTY")
    if (any(s["inverter_ref"] is not None for s in target["strings"])
            or any(a["inverter_id"] is not None or a["string_input_number"] is not None
                   for f in target["frames"] for a in f["panel_assignments"]
                   + [cell for row in f["matrix"] for cell in row])):
        refuse("MATRIX_INPUT_MISMATCH")
    try:
        metadata = target["extra"]["pvcase_solve"]
        expected_keys = {"schema", "basis_graph_sha256", "panels_per_string", "panels_assigned",
                         "strings_created", "written", "l2_count"}
        if (type(metadata) is not dict or set(metadata) != expected_keys
                or metadata["schema"] != graph_kernel.SCHEMA
                or type(metadata["basis_graph_sha256"]) is not str
                or re.fullmatch(r"[0-9a-f]{64}", metadata["basis_graph_sha256"]) is None
                or any(type(metadata[k]) is not int for k in expected_keys
                       - {"schema", "basis_graph_sha256"})
                or metadata["panels_per_string"] != projection["panels_per_string"]
                or metadata["panels_assigned"] != len(target["panels"])
                or metadata["written"] != len(target["panels"])
                or metadata["strings_created"] != len(target["strings"])
                or metadata["l2_count"] != 0 or not target["strings"]):
            refuse("PVG_INVALID_RESULT")
        numbers, coverage = set(), set()
        strings = {s["id"]: s for s in target["strings"]}
        panels = {p["id"]: p for p in target["panels"]}
        for string in target["strings"]:
            parity = string["extra"]["pvcase"]
            number = parity["parity_string_number"]
            refs = string["ordered_panel_refs"]
            if (type(parity["parity_l2_number"]) is not int or parity["parity_l2_number"] != 1
                    or type(number) is not int or number <= 0 or number in numbers
                    or not refs or coverage.intersection(refs)
                    or len({panels[r]["frame_ref"] for r in refs}) != 1):
                refuse("PVG_INVALID_RESULT")
            numbers.add(number)
            coverage.update(refs)
        if coverage != set(panels) or numbers != set(range(1, len(strings) + 1)):
            refuse("PVG_INVALID_RESULT")
        frames = {f["id"]: f for f in target["frames"]}
        rows = []
        for group in projection["panel_groups"]:
            for projected in group["panels"]:
                panel = panels[projection["panel_refs"][projected.handle]]
                frame = frames[panel["frame_ref"]]
                string = strings[panel["assignment"]["string_ref"]]
                parity = string["extra"]["pvcase"]
                rows.append([
                    panel["provenance"]["source_handle"], parity["parity_l2_number"],
                    parity["parity_string_number"], frame["provenance"]["source_handle"],
                    frame["id"], panel["id"], string["id"], panel["matrix_cell"]["row"],
                    panel["matrix_cell"]["col"], panel["centre"][:], string["inverter_ref"],
                ])
        data = graph_kernel.canonical_json({
            "schema": "leaf.pvcase-g33-assignments.v1", "columns": list(COLUMNS),
            "rows": rows, "electrical_sizing": "not-evaluated"}) + b"\n"
    except graph_kernel.DOMAIN_ERRORS:
        refuse("PVG_INVALID_RESULT")
    if len(data) > contract.MAX_BYTES:
        refuse("PVG_BYTE_LIMIT")
    return data
