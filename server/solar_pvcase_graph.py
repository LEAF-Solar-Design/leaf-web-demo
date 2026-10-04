"""Pure graph projection and local G33 parity solve; no equipment is inferred."""
from __future__ import annotations

import hashlib
import json
import math
import re
from uuid import UUID

import solar_design_graph as contract
import solar_pvcase_conversion as conversion
import solar_pvcase_solve as raw
import solar_solve_results as results
from solar_design_graph import GraphValidationError

CODES = (
    "PVG_INVALID_TARGET", "PVG_INVALID_SOURCE", "PVG_TARGET_CONTEXT",
    "FRAME_MEMBERSHIP_MISMATCH", "MATRIX_CELL_MISMATCH", "PVG_TARGET_NOT_EMPTY",
    "PVG_INVALID_INTAKE", "PVG_GEOMETRY_RANGE", "PVG_INVALID_RESULT",
)
SCHEMA = "leaf.pvcase-g33-solve.v1"
# ArithmeticError covers ZeroDivisionError, OverflowError and FloatingPointError, so an
# arithmetic failure inside the kernel is refused without its payload like every other.
DOMAIN_ERRORS = (ValueError, TypeError, KeyError, IndexError, AttributeError,
                 ArithmeticError, RecursionError)


def _refuse(code):
    raise GraphValidationError(code, "<root>") from None


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _validated(graph, *, output=False):
    preserved = {"FRAME_MEMBERSHIP_MISMATCH", "MATRIX_CELL_MISMATCH"}
    if output:
        preserved.update(("FRAME_SEQUENCE_MISMATCH", "MATRIX_INPUT_MISMATCH"))
    try:
        return contract.validate_graph(graph)
    except DOMAIN_ERRORS as error:
        if (output and isinstance(error, GraphValidationError)
                and error.code == "INVERTER_ASSIGNMENT_MISMATCH"):
            _refuse("MATRIX_INPUT_MISMATCH")
        if isinstance(error, GraphValidationError) and error.code in preserved:
            _refuse(error.code)
        if (isinstance(error, GraphValidationError) and error.code == "UNKNOWN_UNITS"
                and conversion._metric_variant_valid(graph)):
            _refuse("PVG_TARGET_CONTEXT")
        _refuse("PVG_INVALID_TARGET")


def _checked(graph, envelope, *, output=False):
    target = _validated(graph, output=output)
    try:
        intake = conversion.validate_envelope(envelope)["intake"]
        digest = conversion._identity_digest(intake)
        source = target["extra"]["pvcase"]
        if (source["schema"] != conversion.INPUT_SCHEMA
                or source["conversion_schema"] != conversion.CONVERSION_SCHEMA
                or source["normalized_intake_sha256"] != digest
                or any(type(source[k]) is not str or re.fullmatch(r"[0-9a-f]{64}", source[k]) is None
                       for k in ("source_artifact_id", "source_sha256"))):
            _refuse("PVG_INVALID_SOURCE")
    except DOMAIN_ERRORS:
        _refuse("PVG_INVALID_SOURCE")
    units = target["project"]["units"]
    if (units["compute_units"] != "m" or units["drawing_units"] != intake["units"]
            or units["meters_per_unit"] != conversion.SCALES[intake["units"]]
            or units["wcs_to_ucs"] != conversion.IDENTITY or units["crs"] is not None
            or any(g["installation"] != target["project"]["installation_design"]
                   for g in intake["panel_groups"])
            or any(len(p["centre"]) > 2 and p["centre"][2] != 0 for p in target["panels"])):
        _refuse("PVG_TARGET_CONTEXT")
    expected_groups = [(i, g, list(conversion._usable(g)))
                       for i, g in enumerate(intake["panel_groups"])]
    skipped = [{"group_index": i, "source_handle": g["handle"], "reason": "no-usable-panels"}
               for i, g, cells in expected_groups if not cells]
    if source.get("skipped_groups") != skipped:
        _refuse("PVG_INVALID_SOURCE")
    expected_groups = [(i, g, cells) for i, g, cells in expected_groups if cells]
    frames = {f["id"]: f for f in target["frames"]}
    panels = {p["id"]: p for p in target["panels"]}
    expected_frames = {conversion._entity_id(target, digest, "frame", g["handle"])
                       for _, g, _ in expected_groups}
    expected_panels = {conversion._entity_id(target, digest, "panel", cell["id"])
                       for _, _, cells in expected_groups for _, _, cell in cells}
    if set(frames) != expected_frames or set(panels) != expected_panels:
        _refuse("FRAME_MEMBERSHIP_MISMATCH")
    projected, refs = [], {}
    for index, group, cells in expected_groups:
        frame = frames[conversion._entity_id(target, digest, "frame", group["handle"])]
        try:
            evidence = frame["provenance"]["pvcase"]
            if (any(evidence.get(k) != source[k] for k in (
                    "schema", "conversion_schema", "source_artifact_id", "source_sha256",
                    "normalized_intake_sha256")) or evidence["group_index"] != index
                    or conversion._normalized(frame["provenance"]["source_handle"])
                    != conversion._normalized(group["handle"])):
                _refuse("PVG_INVALID_SOURCE")
            if evidence["source_sequences"] != group["sequences"]:
                _refuse("PVG_INVALID_SOURCE")
            if evidence["source_row_angle_rad"] != group["row_angle_rad"]:
                _refuse("PVG_INVALID_SOURCE")
            lengths = [len(row) for row in group["rows"]]
            if (evidence["source_row_lengths"] != lengths
                    or frame["module_rows"] != len(lengths)
                    or frame["module_columns"] != max(lengths)
                    or frame["module_slots"] != len(lengths) * max(lengths)):
                _refuse("MATRIX_CELL_MISMATCH")
        except GraphValidationError:
            raise
        except DOMAIN_ERRORS:
            _refuse("PVG_INVALID_SOURCE")
        if frame["installation_design"] != group["installation"]:
            _refuse("PVG_TARGET_CONTEXT")
        positions = {(r, c): conversion._entity_id(target, digest, "panel", cell["id"])
                     for r, c, cell in cells}
        inventory = list(positions.values())
        actual = {(r, c): cell["panel_ref"] for r, row in enumerate(frame["matrix"])
                  for c, cell in enumerate(row) if cell["panel_ref"] is not None}
        if (len(frame["panel_refs"]) != len(inventory)
                or set(frame["panel_refs"]) != set(inventory)
                or len(frame["panel_assignments"]) != len(inventory)
                or {a["panel_ref"] for a in frame["panel_assignments"]} != set(inventory)
                or len(actual) != len(inventory) or set(actual.values()) != set(inventory)):
            _refuse("FRAME_MEMBERSHIP_MISMATCH")
        if actual != positions:
            _refuse("MATRIX_CELL_MISMATCH")
        width, height = frame["module_width_along_row"], frame["module_height_across_row"]
        if width <= 0 or height <= 0 or not all(math.isfinite(v) for v in (width, height)):
            _refuse("PVG_GEOMETRY_RANGE")
        inputs = []
        for r, c, cell in cells:
            panel = panels[positions[r, c]]
            try:
                evidence = panel["provenance"]["pvcase"]
                handle = panel["provenance"]["source_handle"]
                if (conversion._normalized(handle) != conversion._normalized(cell["id"])
                        or evidence["group_index"] != index
                        or any(evidence[k] != cell[k] for k in (
                            "code", "seq", "inverter_id", "string_input_number"))):
                    _refuse("PVG_INVALID_SOURCE")
                if evidence["row"] != r or evidence["col"] != c:
                    _refuse("MATRIX_CELL_MISMATCH")
            except GraphValidationError:
                raise
            except DOMAIN_ERRORS:
                _refuse("PVG_INVALID_SOURCE")
            if panel["frame_ref"] != frame["id"]:
                _refuse("FRAME_MEMBERSHIP_MISMATCH")
            if (panel["matrix_cell"]["row"], panel["matrix_cell"]["col"]) != (r, c):
                _refuse("MATRIX_CELL_MISMATCH")
            refs[handle] = panel["id"]
            inputs.append(raw.PanelInput(handle, panel["centre"][0], panel["centre"][1],
                                         width, height, 0, 0, r, c))
        angle = math.radians(panels[inventory[0]]["angle"])
        if not math.isfinite(angle):
            _refuse("PVG_GEOMETRY_RANGE")
        projected.append({"group_id": frame["provenance"]["source_handle"],
                          "row_angle_rad": angle, "panels": inputs})
    setting = source.get("panels_per_string")
    if (type(setting) is not int or not raw.INT32_MIN <= setting <= raw.INT32_MAX
            or len(projected) > raw.MAX_GROUPS
            or sum(1 + len(row) for f in target["frames"] for row in f["matrix"]) > raw.MAX_CELLS):
        _refuse("PVG_INVALID_INTAKE")
    if not projected:
        _refuse("PVG_INVALID_SOURCE")
    return target, {"panel_groups": projected,
                    "panels_per_string": raw.effective_panels_per_string(setting), "panel_refs": refs}


def project_graph(graph: dict, envelope: dict | bytes) -> dict:
    """Project current geometry, using capture data solely as a membership witness."""
    return _checked(graph, envelope)[1]


def solve_graph(graph: dict, envelope: dict | bytes) -> dict:
    target, projection = _checked(graph, envelope)
    if any(target[k] for k in ("strings", "inverters", "routes", "schedules")):
        _refuse("PVG_TARGET_NOT_EMPTY")
    length = projection["panels_per_string"]
    chunks = []
    for group in projection["panel_groups"]:
        for start in range(0, len(group["panels"]), length):
            chunks.append(group["panels"][start:start + length])
    assigned = len(projection["panel_refs"])
    snapshots = [(p, (p.handle, p.x, p.y, p.width_along_row, p.height_across_row,
                      p.row_index, p.col_index)) for chunk in chunks for p in chunk]
    try:
        counts = raw.solve(projection["panel_groups"], [], length)
        if (counts != {"panels_assigned": assigned, "strings_created": len(chunks),
                       "strings_per_l2": {1: len(chunks)}}
                or type(counts["panels_assigned"]) is not int
                or type(counts["strings_created"]) is not int
                or any(type(k) is not int or type(v) is not int
                       for k, v in counts["strings_per_l2"].items())
                or any((p.handle, p.x, p.y, p.width_along_row, p.height_across_row,
                        p.row_index, p.col_index) != snapshot for p, snapshot in snapshots)):
            _refuse("PVG_INVALID_RESULT")
        if any(type(p.l2_number) is not int or p.l2_number != 1
               or type(p.string_number) is not int or p.string_number != n
               for n, chunk in enumerate(chunks, 1) for p in chunk):
            _refuse("PVG_INVALID_RESULT")
    except DOMAIN_ERRORS:
        _refuse("PVG_INVALID_RESULT")
    # A JSON round trip also separates aliases within the caller's graph.
    result = json.loads(canonical_json(target))
    panels = {p["id"]: p for p in result["panels"]}
    for number, chunk in enumerate(chunks, 1):
        refs = [projection["panel_refs"][p.handle] for p in chunk]
        frame_id = panels[refs[0]]["frame_ref"]
        identity = canonical_json([SCHEMA, target["source_hash"], frame_id, number, refs])
        ident = "leaf:string:" + str(UUID(bytes=hashlib.sha256(identity).digest()[:16], version=4))
        route = [panels[ref]["centre"][:] for ref in refs]
        try:
            # Pad each centre to 3D as the other string writers do, so a planar [x, y]
            # beside an [x, y, 0] centre measures the same planar distance.
            distance = sum(math.dist(a + [0] * (3 - len(a)), b + [0] * (3 - len(b)))
                           for a, b in zip(route, route[1:])) / 0.3048
        except (ValueError, OverflowError):
            _refuse("PVG_GEOMETRY_RANGE")
        if not math.isfinite(distance):
            _refuse("PVG_GEOMETRY_RANGE")
        result["strings"].append({
            "id": ident, "kind": "string", "rev": target["rev"],
            "validity": {"state": "valid", "reasons": []},
            "provenance": {"created_by": "solar-pvcase-solve", "last_writer": "solar-pvcase-solve",
                           "created_at": target["project"]["provenance"]["created_at"],
                           "source_rev": target["rev"], "source_hash": target["source_hash"]},
            "extra": {"pvcase": {"parity_l2_number": 1, "parity_string_number": number}},
            "circuit_tag": f"S{number}", "circuit_kind": "String",
            "ordered_panel_refs": refs, "module_count": len(refs),
            "from_ref": refs[0], "to_ref": refs[-1], "tag_text_ref": None,
            "route": route, "length_ft": distance, "wire_gauge": "", "inverter_ref": None})
    diagnostics = {"status": "solved", "panels_assigned": assigned,
                   "strings_created": len(chunks), "written": assigned, "l2_count": 0}
    result["extra"]["pvcase_solve"] = {
        "schema": SCHEMA, "basis_graph_sha256": hashlib.sha256(canonical_json(graph)).hexdigest(),
        "panels_per_string": length, **{k: v for k, v in diagnostics.items() if k != "status"}}
    try:
        results.sync_assignments(result)
        result = contract.validate_graph(result)
    except DOMAIN_ERRORS:
        _refuse("PVG_INVALID_RESULT")
    return {"graph": result, "diagnostics": diagnostics}
