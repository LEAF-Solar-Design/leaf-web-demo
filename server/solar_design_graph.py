"""Bounded validation and lossless JSON transport for the W1 solar graph.

The existing capability/job rails call these helpers before accepting a mutation.
No solver, drawing adapter, network client or execution rail lives here.
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from uuid import uuid4


SCHEMA_VERSION = 1
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "contract" / "solar-design-graph.v1.schema.json"
MAX_BYTES = 16 * 1024 * 1024
MAX_NODES = 500000
MAX_DEPTH = 32
COLLECTIONS = ("electrical_zones", "frames", "panels", "strings", "inverters", "routes", "schedules")
KINDS = {"project", "settings", "zone-el", "frame", "panel", "string", "inverter", "route", "schedule"}


class GraphValidationError(ValueError):
    """A named, payload-free refusal safe for the existing error envelope."""

    def __init__(self, code: str, path: str = "<root>"):
        self.code = code
        self.path = path
        super().__init__(f"{code}: {path}")


def new_id(kind: str) -> str:
    if kind not in KINDS:
        raise GraphValidationError("UNKNOWN_ENTITY_KIND")
    return f"leaf:{kind}:{uuid4()}"


def _bounded_json(value):
    stack = [(value, 0)]
    nodes = 0
    size = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > MAX_NODES or depth > MAX_DEPTH:
            raise GraphValidationError("GRAPH_LIMIT_EXCEEDED")
        if type(item) is dict:
            if len(item) > 100000 or any(type(key) is not str for key in item):
                raise GraphValidationError("INVALID_JSON_OBJECT")
            stack.extend((key, depth + 1) for key in item)
            stack.extend((child, depth + 1) for child in item.values())
        elif type(item) is list:
            if len(item) > 100000:
                raise GraphValidationError("GRAPH_LIMIT_EXCEEDED")
            stack.extend((child, depth + 1) for child in item)
        elif type(item) is str:
            try:
                size += len(item.encode("utf-8"))
            except UnicodeError:
                raise GraphValidationError("INVALID_JSON_STRING") from None
        elif type(item) in (int, float):
            if isinstance(item, float) and not math.isfinite(item):
                raise GraphValidationError("NONFINITE_NUMBER")
            if isinstance(item, int) and item.bit_length() > 64:
                raise GraphValidationError("NUMBER_LIMIT_EXCEEDED")
            size += 24
        elif item is None or type(item) is bool:
            size += 5
        else:
            raise GraphValidationError("INVALID_JSON_VALUE")
        if size > MAX_BYTES:
            raise GraphValidationError("GRAPH_LIMIT_EXCEEDED")


def load_schema() -> dict:
    # Trusted, packaged local asset; never resolve a schema supplied by a caller.
    with SCHEMA_PATH.open("rb") as source:
        raw = source.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise GraphValidationError("SCHEMA_LIMIT_EXCEEDED")
    return json.loads(raw)


def entities(graph: dict) -> list[dict]:
    return [graph["project"], graph["settings"]] + [
        entity for collection in COLLECTIONS for entity in graph[collection]
    ]


def validate_graph(graph: dict) -> dict:
    """Return an isolated, unchanged graph, or refuse unsupported/malformed input.

    Unknown fields are accepted at every level and copied, never projected away.
    Unknown versions are not interpreted, even for a purported read-only call.
    """
    _bounded_json(graph)
    if type(graph) is not dict:
        raise GraphValidationError("INVALID_GRAPH")
    if type(graph.get("graph_schema_version")) is not int or graph["graph_schema_version"] != SCHEMA_VERSION:
        raise GraphValidationError("UNSUPPORTED_SCHEMA_VERSION")
    from jsonschema import Draft202012Validator, FormatChecker

    schema = load_schema()
    project = graph.get("project")
    units = project.get("units") if type(project) is dict else None
    if not Draft202012Validator(schema["$defs"]["units"]).is_valid(units):
        raise GraphValidationError("UNKNOWN_UNITS", "project.units")
    error = next(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(graph), None)
    if error is not None:
        # Do not echo values or caller-controlled property names into logs.
        raise GraphValidationError("INVALID_GRAPH_SCHEMA")
    rev = graph["rev"]
    if (rev == 0 and graph["parent_rev"] is not None) or (rev > 0 and graph["parent_rev"] != rev - 1):
        raise GraphValidationError("INVALID_REVISION_CHAIN")
    all_entities = entities(graph)
    index = {entity["id"]: entity for entity in all_entities}
    if len(index) != len(all_entities):
        raise GraphValidationError("DUPLICATE_APPLICATION_ID")
    for entity in all_entities:
        if entity["id"].split(":")[1] != entity["kind"]:
            raise GraphValidationError("ID_KIND_MISMATCH")
        if entity["rev"] > rev or entity["provenance"]["source_rev"] > rev:
            raise GraphValidationError("FUTURE_ENTITY_REVISION")
    for schedule in graph["schedules"]:
        if schedule["source_rev"] > rev:
            raise GraphValidationError("FUTURE_SCHEDULE_REVISION")
        if len(schedule["column_units"]) != len(schedule["headers"]) or any(
            len(row) != len(schedule["headers"]) for row in schedule["rows"]
        ):
            raise GraphValidationError("INVALID_SCHEDULE_COLUMNS")
    members = {}
    for string in graph["strings"]:
        if string["module_count"] != len(string["ordered_panel_refs"]):
            raise GraphValidationError("STRING_COUNT_MISMATCH")
        for seq, panel_id in enumerate(string["ordered_panel_refs"]):
            if panel_id in members:
                raise GraphValidationError("DUPLICATE_PANEL_MEMBERSHIP")
            panel = index.get(panel_id)
            if panel is None or panel["kind"] != "panel":
                raise GraphValidationError("MISSING_PANEL")
            members[panel_id] = (string["id"], seq)
    for panel in graph["panels"]:
        assignment = panel["assignment"]
        if (assignment["string_ref"], assignment["seq"]) != members.get(panel["id"], (None, None)):
            raise GraphValidationError("PANEL_ASSIGNMENT_MISMATCH")
        frame_ref = panel["frame_ref"]
        if frame_ref is not None:
            frame = index.get(frame_ref)
            if frame is None or frame["kind"] != "frame" or panel["id"] not in frame["panel_refs"]:
                raise GraphValidationError("FRAME_MEMBERSHIP_MISMATCH")
    for frame in graph["frames"]:
        if frame["electrical_zone_ref"] is not None:
            zone = index.get(frame["electrical_zone_ref"])
            if zone is None or zone["kind"] != "zone-el":
                raise GraphValidationError("MISSING_ELECTRICAL_ZONE")
        if len(frame["matrix"]) != frame["module_rows"] or any(
            len(row) != frame["module_columns"] for row in frame["matrix"]
        ) or frame["module_slots"] != frame["module_rows"] * frame["module_columns"]:
            raise GraphValidationError("INVALID_MATRIX_DIMENSIONS")
        cell_panels = set()
        for row_number, row in enumerate(frame["matrix"]):
            for col_number, cell in enumerate(row):
                panel_id = cell["panel_ref"]
                if panel_id is None:
                    continue
                panel = index.get(panel_id)
                if panel is None or panel["kind"] != "panel" or panel_id in cell_panels:
                    raise GraphValidationError("INVALID_MATRIX_PANEL")
                location = panel["matrix_cell"]
                if panel["frame_ref"] != frame["id"] or location is None or (location["row"], location["col"]) != (row_number, col_number):
                    raise GraphValidationError("MATRIX_CELL_MISMATCH")
                if cell["seq"] != panel["assignment"]["seq"]:
                    raise GraphValidationError("MATRIX_SEQUENCE_MISMATCH")
                cell_panels.add(panel_id)
        if cell_panels != set(frame["panel_refs"]):
            raise GraphValidationError("FRAME_MEMBERSHIP_MISMATCH")
        assignment_panels = set()
        for assignment in frame["panel_assignments"]:
            panel_id = assignment["panel_ref"]
            if panel_id not in cell_panels or panel_id in assignment_panels:
                raise GraphValidationError("FRAME_ASSIGNMENT_MISMATCH")
            expected = index[panel_id]["assignment"]
            if any(assignment[key] != expected[key] for key in ("string_ref", "seq")):
                raise GraphValidationError("FRAME_ASSIGNMENT_MISMATCH")
            assignment_panels.add(panel_id)
        if assignment_panels != cell_panels:
            raise GraphValidationError("FRAME_ASSIGNMENT_MISMATCH")
        sequence_panels = set()
        for sequence in frame["sequences"]:
            string_id = sequence["string_ref"]
            string = index.get(string_id)
            if string is None or string["kind"] != "string":
                raise GraphValidationError("FRAME_SEQUENCE_MISMATCH")
            expected = [panel_id for panel_id in string["ordered_panel_refs"] if panel_id in cell_panels]
            if sequence["ordered_panel_refs"] != expected or sequence_panels.intersection(expected):
                raise GraphValidationError("FRAME_SEQUENCE_MISMATCH")
            sequence_panels.update(expected)
        if sequence_panels != {panel_id for panel_id in cell_panels if index[panel_id]["assignment"]["string_ref"] is not None}:
            raise GraphValidationError("FRAME_SEQUENCE_MISMATCH")
    for zone in graph["electrical_zones"]:
        if any(index.get(panel_id, {}).get("kind") != "panel" for panel_id in zone["panel_refs"]):
            raise GraphValidationError("MISSING_PANEL")
    assigned_strings = set()
    string_inputs = {}
    for inverter in graph["inverters"]:
        assignments = inverter["input_assignments"]
        slots = set()
        if len(assignments) > inverter["total_dc_inputs"]:
            raise GraphValidationError("INVERTER_CAPACITY_EXCEEDED")
        for assignment in assignments:
            string_id = assignment["string_ref"]
            slot = (assignment["mppt_letter"], assignment["input_number"])
            string = index.get(string_id)
            if string is None or string["kind"] != "string" or string["inverter_ref"] != inverter["id"]:
                raise GraphValidationError("INVERTER_ASSIGNMENT_MISMATCH")
            if slot in slots or string_id in assigned_strings:
                raise GraphValidationError("DUPLICATE_INVERTER_INPUT")
            if assignment["input_number"] >= inverter["total_dc_inputs"]:
                raise GraphValidationError("INVERTER_CAPACITY_EXCEEDED")
            slots.add(slot)
            assigned_strings.add(string_id)
            string_inputs[string_id] = (inverter["id"], assignment["input_number"])
        if len({slot[0] for slot in slots}) > inverter["mppt_count"]:
            raise GraphValidationError("INVERTER_CAPACITY_EXCEEDED")
    for string in graph["strings"]:
        if string["inverter_ref"] is not None and string["id"] not in assigned_strings:
            raise GraphValidationError("INVERTER_ASSIGNMENT_MISMATCH")
    for frame in graph["frames"]:
        records = frame["panel_assignments"] + [cell for row in frame["matrix"] for cell in row if cell["panel_ref"] is not None]
        for record in records:
            panel = index[record["panel_ref"]]
            expected = string_inputs.get(panel["assignment"]["string_ref"], (None, None))
            if (record["inverter_id"], record["string_input_number"]) != expected:
                raise GraphValidationError("MATRIX_INPUT_MISMATCH")
    return copy.deepcopy(graph)


def serialize_graph(graph: dict) -> str:
    result = json.dumps(validate_graph(graph), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(result.encode("utf-8")) > MAX_BYTES:
        raise GraphValidationError("GRAPH_LIMIT_EXCEEDED")
    return result


def deserialize_graph(payload: str | bytes) -> dict:
    if type(payload) not in (str, bytes) or len(payload) > MAX_BYTES:
        raise GraphValidationError("GRAPH_LIMIT_EXCEEDED")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise GraphValidationError("DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    try:
        graph = json.loads(payload, object_pairs_hook=pairs)
    except (ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, GraphValidationError):
            raise
        raise GraphValidationError("INVALID_JSON") from None
    return validate_graph(graph)


def require_revision(graph: dict, expected_rev: int) -> dict:
    result = validate_graph(graph)
    if type(expected_rev) is not int or result["rev"] != expected_rev:
        raise GraphValidationError("STALE_GRAPH_REVISION")
    return result
