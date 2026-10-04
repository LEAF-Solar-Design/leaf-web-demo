"""Pure, bounded admission and lossless geometry conversion for the G33 capture."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass
from uuid import UUID

import solar_design_graph as graph_contract
import solar_pvcase_solve as solver
from solar_design_graph import GraphValidationError

INPUT_SCHEMA = "leaf.pvcase-g33.v1"
CONVERSION_SCHEMA = "leaf.pvcase-g33-conversion.v1"
WRITER = "solar-pvcase-conversion"
CAPTURED_MATRIX = "pvcase-g33"
MAX_INPUT_BYTES = 16777216
MAX_BYTES = graph_contract.MAX_BYTES
MAX_NODES = graph_contract.MAX_NODES
MAX_DEPTH = graph_contract.MAX_DEPTH
MAX_LIST = 100000
MAX_MATRIX_AXIS = 10000
MAX_MATRIX_SLOTS = 1000000
SCALES = {"mm": 0.001, "cm": 0.01, "m": 1.0, "in": 0.0254, "ft": 0.3048}
IDENTITY = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]


def _refuse(code):
    raise GraphValidationError(code, "<root>") from None


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _scan_depth(text):
    # Count structural nesting only outside strings, before the recursive JSON parser.
    nesting = 0
    quoted = escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            nesting += 1
            if nesting > MAX_DEPTH + 1:
                _refuse("PVG_DEPTH_LIMIT")
        elif char in "]}":
            nesting -= 1


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            _refuse("PVG_INVALID_JSON")
        out[key] = value
    return out


def _constant(_):
    _refuse("PVG_INVALID_JSON")


def _json_integer(text):
    value = int(text)
    # The nested validator normalizes geometry to float; bound parser numeric failures
    # here as invalid JSON while retaining finite integer coordinates beyond int64.
    if not math.isfinite(value):
        _refuse("PVG_INVALID_JSON")
    return value


def _guard(value, invalid_code="PVG_INVALID_JSON", *, bytes_bound=False):
    # Iterator frames keep auxiliary memory proportional to depth, including wide objects.
    active = set()
    stack = [(iter((value,)), 0, None)]
    nodes = size = 0
    while stack:
        iterator, depth, owner = stack[-1]
        try:
            item = next(iterator)
        except StopIteration:
            stack.pop()
            if owner is not None:
                active.remove(owner)
            continue
        nodes += 1
        if depth > MAX_DEPTH:
            _refuse("PVG_DEPTH_LIMIT")
        if nodes > MAX_NODES:
            _refuse("PVG_NODE_LIMIT")
        if type(item) in (dict, list):
            if len(item) > MAX_LIST:
                _refuse("PVG_LIST_LIMIT")
            ident = id(item)
            if ident in active:
                _refuse(invalid_code)
            active.add(ident)
            if type(item) is dict:
                if any(type(key) is not str for key in item):
                    _refuse(invalid_code)
                children = (part for pair in item.items() for part in pair)
            else:
                children = iter(item)
            stack.append((children, depth + 1, ident))
        elif type(item) is str:
            try:
                size += len(item.encode("utf-8"))
            except UnicodeError:
                _refuse(invalid_code)
        elif type(item) in (int, float):
            if type(item) is float and not math.isfinite(item):
                _refuse(invalid_code)
            # Intake coordinates are normalized by the existing solver, which permits
            # finite integers beyond int64. Targets retain the graph's numeric bound.
            if type(item) is int and item.bit_length() > 64 and invalid_code != "PVG_INVALID_JSON":
                _refuse(invalid_code)
            size += 24
        elif item is None or type(item) is bool:
            size += 5
        else:
            _refuse(invalid_code)
        if bytes_bound and size > MAX_BYTES:
            _refuse("PVG_BYTE_LIMIT")


def validate_envelope(raw_bytes_or_obj):
    """Return an isolated v1 envelope containing validate_intake's normalized copy."""
    value = raw_bytes_or_obj
    if type(value) is bytes:
        if len(value) > MAX_INPUT_BYTES:
            _refuse("PVG_INPUT_BYTES_EXCEEDED")
        try:
            text = value.decode("utf-8", errors="strict")
            _scan_depth(text)
            value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant,
                               parse_int=_json_integer)
        except (ValueError, UnicodeError, OverflowError, RecursionError) as error:
            if isinstance(error, GraphValidationError):
                raise
            _refuse("PVG_INVALID_JSON")
    _guard(value)
    if type(value) is not dict or set(value) != {"schema", "intake"}:
        _refuse("PVG_ENVELOPE_FIELDS")
    if value["schema"] != INPUT_SCHEMA:
        _refuse("PVG_ENVELOPE_SCHEMA")
    try:
        intake = solver.validate_intake(value["intake"])
    except (ValueError, TypeError, OverflowError, RecursionError):
        _refuse("PVG_INVALID_INTAKE")
    return {"schema": INPUT_SCHEMA, "intake": intake}


@dataclass(frozen=True)
class _Measure:
    nodes: int
    size: int
    canonical_bytes: int
    depth: int
    list_limit: bool = False


def _array(parts):
    """Measure a list from (child measure, repetition count) pairs, without expansion."""
    nodes, size, encoded, depth, count, over = 1, 0, 2, 0, 0, False
    for child, repetitions in parts:
        if not repetitions:
            continue
        count += repetitions
        nodes += child.nodes * repetitions
        size += child.size * repetitions
        encoded += child.canonical_bytes * repetitions
        depth = max(depth, child.depth + 1)
        over |= child.list_limit
    return _Measure(nodes, size, encoded + max(0, count - 1), depth,
                    over or count > MAX_LIST)


def _object(parts):
    nodes, size, encoded, depth, count, over = 1, 0, 2, 0, 0, False
    for key, child in parts:
        key_measure = _measure(key)
        count += 1
        nodes += key_measure.nodes + child.nodes
        size += key_measure.size + child.size
        encoded += key_measure.canonical_bytes + 1 + child.canonical_bytes
        depth = max(depth, 1, child.depth + 1)
        over |= child.list_limit
    return _Measure(nodes, size, encoded + max(0, count - 1), depth,
                    over or count > MAX_LIST)


def _measure(value):
    # Called only on guarded inputs or small, internally authored fragments.
    if type(value) is dict:
        return _object((key, _measure(child)) for key, child in value.items())
    if type(value) is list:
        return _array((_measure(child), 1) for child in value)
    size = len(value.encode("utf-8")) if type(value) is str else (
        24 if type(value) in (int, float) else 5)
    return _Measure(1, size, len(_canonical(value)), 0)


def _admit(measure):
    # Shape is checked separately, before this fixed precedence.
    if measure.list_limit:
        _refuse("PVG_LIST_LIMIT")
    if measure.depth > MAX_DEPTH:
        _refuse("PVG_DEPTH_LIMIT")
    if measure.nodes > MAX_NODES:
        _refuse("PVG_NODE_LIMIT")
    if max(measure.size, measure.canonical_bytes) > MAX_BYTES:
        _refuse("PVG_BYTE_LIMIT")


def _normalized(handle):
    return handle.upper().lstrip("0") or "0"


def _identity_digest(intake):
    identity = copy.deepcopy(intake)
    for group in identity["panel_groups"]:
        group["handle"] = _normalized(group["handle"])
        for row in group["rows"]:
            for cell in row:
                if cell is not None:
                    cell["id"] = _normalized(cell["id"])
    return hashlib.sha256(_canonical(identity)).hexdigest()


def _entity_id(graph, digest, kind, handle):
    namespace = (CONVERSION_SCHEMA + ":" + graph["source_hash"] + ":" + digest
                 + ":" + kind + ":" + _normalized(handle))
    raw = hashlib.sha256(namespace.encode("utf-8")).digest()[:16]
    return f"leaf:{kind}:{UUID(bytes=raw, version=4)}"


def _entity(graph, digest, kind, handle, evidence):
    return {"id": _entity_id(graph, digest, kind, handle), "kind": kind,
            "rev": graph["rev"], "extra": {},
            "validity": {"state": "valid", "reasons": []},
            "provenance": {"created_by": WRITER,
                           "created_at": graph["project"]["provenance"]["created_at"],
                           "last_writer": WRITER, "source_rev": graph["rev"],
                           "source_hash": graph["source_hash"], "source_handle": handle,
                           "pvcase": evidence}}


def _usable(group):
    for r, row in enumerate(group["rows"]):
        for c, cell in enumerate(row):
            if cell is not None and cell["code"] != 0:
                yield r, c, cell


def _geometry(group, cell, scale):
    width = group["panel_size"]["width_along_row"] * scale
    height = group["panel_size"]["height_across_row"] * scale
    x, y = cell["x"] * scale, cell["y"] * scale
    radians = group["row_angle_rad"]
    angle = math.degrees(radians)
    if (not all(math.isfinite(v) for v in (width, height, x, y, angle))
            or width <= 0 or height <= 0):
        _refuse("PVG_GEOMETRY_RANGE")
    u, v = math.cos(radians), math.sin(radians)
    for a in (-1, 1):
        for b in (-1, 1):
            cx = x + a * (width / 2) * u - b * (height / 2) * v
            cy = y + a * (width / 2) * v + b * (height / 2) * u
            if not math.isfinite(cx) or not math.isfinite(cy):
                _refuse("PVG_GEOMETRY_RANGE")
    return [x, y], width, height, angle


def _panel(graph, digest, group, index, r, c, cell, scale, frame_id):
    centre, _, _, angle = _geometry(group, cell, scale)
    panel = _entity(graph, digest, "panel", cell["id"], {
        "group_index": index, "row": r, "col": c, "code": cell["code"],
        "seq": cell["seq"], "inverter_id": cell["inverter_id"],
        "string_input_number": cell["string_input_number"]})
    panel.update(frame_ref=frame_id, matrix_cell={"row": r, "col": c},
                 centre=centre, angle=angle, assignment={"string_ref": None, "seq": None})
    return panel


def _empty_cell():
    return {"code": "empty", "panel_ref": None, "seq": None, "inverter_id": None,
            "string_input_number": None, "x": 0.0, "y": 0.0, "angle": 0.0}


def _occupied(panel):
    return {"code": "panel", "panel_ref": panel["id"], "seq": None, "inverter_id": None,
            "string_input_number": None, "x": panel["centre"][0],
            "y": panel["centre"][1], "angle": panel["angle"]}


def _assignment(panel):
    return {"panel_ref": panel["id"], "string_ref": None, "seq": None,
            "inverter_id": None, "string_input_number": None}


def _frame(graph, digest, group, index, source, scale):
    _, _, first = next(_usable(group))
    centre, width, height, _ = _geometry(group, first, scale)
    rows = len(group["rows"])
    columns = max(map(len, group["rows"]))
    frame = _entity(graph, digest, "frame", group["handle"], {
        **source, "group_index": index,
        "source_row_lengths": [len(row) for row in group["rows"]],
        "source_sequences": group["sequences"][:],
        "source_row_angle_rad": group["row_angle_rad"]})
    frame.update(name="PVcase " + _normalized(group["handle"]),
                 installation_design=group["installation"], insertion_point=centre,
                 module_rows=rows, module_columns=columns, module_slots=rows * columns,
                 module_power_watts=0, module_width_along_row=width,
                 module_height_across_row=height, electrical_zone_ref=None,
                 panel_refs=[], panel_assignments=[], matrix=[], sequences=[])
    if group["installation"] == "Ground":
        frame["captured_matrix"] = CAPTURED_MATRIX
    return frame


def _preflight(graph, intake, source, digest, indices, skipped):
    # Complete accounting. No panel list, matrix, padding or assignment list is built here.
    for index in indices:
        group = intake["panel_groups"][index]
        rows = len(group["rows"])
        columns = max(map(len, group["rows"]))
        if (rows > MAX_MATRIX_AXIS or columns > MAX_MATRIX_AXIS
                or rows * columns > MAX_MATRIX_SLOTS):
            _refuse("PVG_MATRIX_LIMIT")
    scale = SCALES[intake["units"]]
    frame_parts = []
    empty = _measure(_empty_cell())
    # Only measures are retained, one aggregate per frame; entity fragments are ephemeral.
    total_panel_nodes = total_panel_size = total_panel_bytes = total_panels = 0
    panel_depth = 0
    panel_over = False
    for index in indices:
        group = intake["panel_groups"][index]
        frame = _frame(graph, digest, group, index, source, scale)
        columns = frame["module_columns"]
        refs_nodes = refs_size = refs_bytes = assignments_nodes = assignments_size = assignments_bytes = 0
        count = 0
        assignment_depth = 0
        matrix_rows = []
        for r, row in enumerate(group["rows"]):
            occupied_parts = []
            occupied_count = 0
            for c, cell in enumerate(row):
                if cell is None or cell["code"] == 0:
                    continue
                panel = _panel(graph, digest, group, index, r, c, cell, scale, frame["id"])
                pm, rm, am = _measure(panel), _measure(panel["id"]), _measure(_assignment(panel))
                total_panel_nodes += pm.nodes
                total_panel_size += pm.size
                total_panel_bytes += pm.canonical_bytes
                panel_depth = max(panel_depth, pm.depth + 1)
                panel_over |= pm.list_limit
                total_panels += 1
                count += 1
                refs_nodes += rm.nodes
                refs_size += rm.size
                refs_bytes += rm.canonical_bytes
                assignments_nodes += am.nodes
                assignments_size += am.size
                assignments_bytes += am.canonical_bytes
                assignment_depth = max(assignment_depth, am.depth + 1)
                occupied_parts.append((_measure(_occupied(panel)), 1))
                occupied_count += 1
            # Positions affect materialization, but not these exact additive size metrics.
            occupied_parts.append((empty, columns - occupied_count))
            matrix_rows.append((_array(occupied_parts), 1))
        refs = _Measure(1 + refs_nodes, refs_size, 2 + refs_bytes + max(0, count - 1),
                        1 if count else 0, count > MAX_LIST)
        assignments = _Measure(1 + assignments_nodes, assignments_size,
                               2 + assignments_bytes + max(0, count - 1),
                               assignment_depth, count > MAX_LIST)
        replacements = {"panel_refs": refs, "panel_assignments": assignments,
                        "matrix": _array(matrix_rows)}
        fm = _object((key, replacements[key] if key in replacements else _measure(value))
                     for key, value in frame.items())
        frame_parts.append((fm, 1))
    panels = _Measure(1 + total_panel_nodes, total_panel_size,
                      2 + total_panel_bytes + max(0, total_panels - 1), panel_depth,
                      panel_over or total_panels > MAX_LIST)
    replacements = {"frames": _array(frame_parts), "panels": panels,
                    "extra": _measure({**graph["extra"], "pvcase": {
                        **source, "panels_per_string": intake["panels_per_string"],
                        "skipped_groups": skipped}})}
    measure = _object((key, replacements[key] if key in replacements else _measure(value))
                      for key, value in graph.items())
    _admit(measure)
    return measure


def _materialize(graph, intake, source, digest, indices, skipped):
    # Alias-free copy. A validated target may share one list or dict between keys (frames
    # and panels, or metadata pointing at a collection); a deep copy keeps that sharing, so
    # an append here would reach every alias and break preflight's per-occurrence
    # accounting. The graph validator admits only exact JSON types with finite floats and
    # string keys, so a JSON round trip is faithful and shares nothing.
    result = json.loads(json.dumps(graph, ensure_ascii=False, allow_nan=False))
    result["extra"]["pvcase"] = {**source, "panels_per_string": intake["panels_per_string"],
                                 "skipped_groups": copy.deepcopy(skipped)}
    scale = SCALES[intake["units"]]
    for index in indices:
        group = intake["panel_groups"][index]
        frame = _frame(graph, digest, group, index, source, scale)
        for r, row in enumerate(group["rows"]):
            cells = []
            for c in range(frame["module_columns"]):
                cell = row[c] if c < len(row) else None
                if cell is None or cell["code"] == 0:
                    cells.append(_empty_cell())
                    continue
                panel = _panel(graph, digest, group, index, r, c, cell, scale, frame["id"])
                result["panels"].append(panel)
                frame["panel_refs"].append(panel["id"])
                frame["panel_assignments"].append(_assignment(panel))
                cells.append(_occupied(panel))
            frame["matrix"].append(cells)
        result["frames"].append(frame)
    return result


def _metric_variant_valid(graph):
    # UNKNOWN_UNITS is a context conflict only when the target names another known unit for
    # its compute units and would validate with compute units in metres. A missing,
    # malformed or unknown compute unit, or any other defect, is an invalid target. The
    # caller's graph is read, never written: the variant is shallow copies along one path.
    if type(graph) is not dict:
        return False
    project = graph.get("project")
    units = project.get("units") if type(project) is dict else None
    if type(units) is not dict:
        return False
    compute = units.get("compute_units")
    known = graph_contract._schema_validators()[0].schema["properties"]["drawing_units"]["enum"]
    if type(compute) is not str or compute == "m" or compute not in known:
        return False
    variant = {**graph, "project": {**project, "units": {**units, "compute_units": "m"}}}
    try:
        graph_contract.validate_graph(variant)
    except (ValueError, TypeError, OverflowError, RecursionError):
        return False
    return True


def convert(graph, envelope, *, source_artifact_id, source_sha256):
    """Return a NEW validated graph, or raise a payload-free GraphValidationError."""
    intake = validate_envelope(envelope)["intake"]
    if any(type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None
           for value in (source_artifact_id, source_sha256)):
        _refuse("PVG_INVALID_SOURCE")
    _guard(graph, "PVG_INVALID_TARGET", bytes_bound=True)
    _admit(_measure(graph))
    try:
        target = graph_contract.validate_graph(graph)
    except (ValueError, TypeError, OverflowError, RecursionError) as error:
        if (isinstance(error, GraphValidationError) and error.code == "UNKNOWN_UNITS"
                and _metric_variant_valid(graph)):
            _refuse("PVG_TARGET_CONTEXT")
        _refuse("PVG_INVALID_TARGET")
    if any(target[key] for key in graph_contract.COLLECTIONS) or "pvcase" in target["extra"]:
        _refuse("PVG_TARGET_NOT_EMPTY")
    units = target["project"]["units"]
    if (units["drawing_units"] != intake["units"]
            or units["meters_per_unit"] != SCALES[intake["units"]]
            or units["compute_units"] != "m" or units["wcs_to_ucs"] != IDENTITY
            or units["crs"] is not None
            or any(group["installation"] != target["project"]["installation_design"]
                   for group in intake["panel_groups"])):
        _refuse("PVG_TARGET_CONTEXT")
    if not intake["panel_groups"]:
        _refuse("PVG_NO_PANEL_GROUPS")
    indices, skipped = [], []
    for index, group in enumerate(intake["panel_groups"]):
        if next(_usable(group), None) is None:
            skipped.append({"group_index": index, "source_handle": group["handle"],
                            "reason": "no-usable-panels"})
        else:
            indices.append(index)
    if not indices:
        _refuse("PVG_NO_USABLE_PANELS")
    digest = _identity_digest(intake)
    source = {"schema": INPUT_SCHEMA, "conversion_schema": CONVERSION_SCHEMA,
              "source_artifact_id": source_artifact_id, "source_sha256": source_sha256,
              "normalized_intake_sha256": digest}
    _preflight(target, intake, source, digest, indices, skipped)
    result = _materialize(target, intake, source, digest, indices, skipped)
    _guard(result, "PVG_INVALID_RESULT", bytes_bound=True)
    _admit(_measure(result))
    try:
        return graph_contract.validate_graph(result)
    except (ValueError, TypeError, OverflowError, RecursionError):
        _refuse("PVG_INVALID_RESULT")
