"""The drawing's current Ground physical state as compact Ground frames for the design graph
(sf-w3-conversion-graph piece 2, the conversion kernel).

LEAFTRACKERSTOPANELGROUPS turns every tracker on the drawing's tracker layer into one panel group
whose matrix is ONE row of module_slots panels (solar_ground_dsteps.tracker_panel_layout). This
module reads the tracker layer from the drawing's current Ground physical state (the document
solar_physical_head.load_physical_head returns: state["frames"] then state["tracker_rows"], scaled
by the document's units.meters_per_unit), runs that layout, and writes each tracker as one Ground
frame carrying a compact slot block (codec leaf.solar-ground-slots.v1,
solar_ground_graph_codec.encode_slots), so the b18 terrain site (237 trackers, 69,678 slots) fits
the graph's node limit. Ids are deterministic: frame k of a graph with source_hash H is
deterministic_id("frame", f"{H}:ground-frame:{k}") and its slot i is
deterministic_id("panel", f"{H}:ground-panel:{k}:{i}"), the scheme the scale record proved
(server/tests/test_solar_ground_graph_codec.py, uid).

Overlapping layouts are kept: the b18 site draws its trackers twice (LEAFTRACK and LEAFSAT),
25,284 slot centres lie inside two tracker outlines, and the result keeps every frame and slot and
REPORTS the overlap instead of removing anything.

Contract:
- Pure apart from convert_current_ground_state, which reads the head once. No clock, no graph
  write, no validation or expansion of the graph: the caller (piece 4, the tool) validates the
  graph it publishes. Inputs are never mutated; the result never aliases them.
- Fails closed with GroundConversionError(code), code in CODES, the message equal to the code and
  carrying no payload. Head read refusals (PhysicalHeadError) pass through unchanged.
- Bounded: at most codec.MAX_GRAPH_SLOTS slots, checked from the counts BEFORE any slot centre is
  built; at most codec.MAX_FRAME_SLOTS slots per frame; every string and number the frame carries
  is checked against the graph schema's bound. The overlap report is linear in slots plus grid
  cells, the grid capped at max(MAX_GRID_CELLS, 4 x trackers) cells.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import threading
import uuid

import solar_design_graph as sdg
import solar_ground_buildout as buildout
import solar_ground_dsteps as dsteps
import solar_ground_graph_codec as codec
import solar_ground_outlines as outlines
import solar_ground_layout as ground_layout
import solar_physical_state as ps  # first: its write_loop import puts da/ (store) on sys.path
import solar_physical_head as ph
from solar_design_graph import GraphValidationError

RESULT_SCHEMA = "leaf.solar-ground-conversion.v1"
CODES = frozenset({
    "GROUND_CONVERSION_PHYSICAL_HEAD_REQUIRED", "GROUND_CONVERSION_TRACKER_ROWS_REQUIRED",
    "GROUND_CONVERSION_GROUND_PROJECT_REQUIRED", "GROUND_CONVERSION_UNITS_MISMATCH",
    "GROUND_CONVERSION_INPUT_INVALID", "GROUND_CONVERSION_LIMIT_EXCEEDED",
})
MAX_REV = 1000000                    # the schema's entity rev bound
MAX_GRID_CELLS = 2_000_000           # overlap grid budget (cells summed over tracker boxes)
MAX_PROVENANCE_BYTES = 16384         # canonical JSON of the caller's provenance
# Deepest root: graph (0) -> frames (1) -> frame (2) -> ground_slots (3)
# -> panel (4) -> provenance (5); frame provenance alone sits at depth 3.
MAX_PROVENANCE_DEPTH = sdg.MAX_DEPTH - 5
MAX_PLAN_COORDINATE = 1_000_000_000  # $defs.plan_point item bound
MAX_TEXT = 4096                      # $defs.provenance string bound
_HEX64 = re.compile(r"[0-9a-f]{64}")
_INPUT_ERRORS = (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError,
                 ZeroDivisionError, RecursionError)
_PROVENANCE_VALIDATOR = None
_PROVENANCE_VALIDATOR_LOCK = threading.Lock()


class GroundConversionError(ValueError):
    """A named, payload-free refusal; `code` is one of CODES."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _invalid():
    return GroundConversionError("GROUND_CONVERSION_INPUT_INVALID")


def deterministic_id(kind, key):
    """leaf:<kind>:<UUIDv4 from the first 16 bytes of sha256(key)>; the scale record's uid."""
    digest = hashlib.sha256(key.encode("utf-8")).digest()[:16]
    return f"leaf:{kind}:{uuid.UUID(bytes=digest, version=4)}"


def tracker_entities(state):
    """Port of scripts/solar_ground_buildout_evidence.py tracker_entities: the tracker layer in
    drawing order, the frame polylines (t3) then the drawn rows (a8, a9), as the engines'
    neutral entities. A frame keeps only its layer and vertices, exactly as the script does."""
    frames = [{"kind": "polyline", "layer": f.get("layer"), "vertices": f["vertices"]}
              for f in state["frames"]]
    return frames + [dict(row, kind="tracker") for row in state["tracker_rows"]]


def _bounded(value, low, high, *, low_open=False):
    """A finite int or float within [low, high] ((low, high] when low_open); bool refused."""
    if type(value) not in (int, float) or not math.isfinite(value):
        raise _invalid()
    if (value <= low if low_open else value < low) or value > high:
        raise _invalid()
    return value


def _text(value, high):
    """None, or a string of 1..high characters."""
    if value is None:
        return None
    if type(value) is not str or not 1 <= len(value) <= high:
        raise _invalid()
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise _invalid() from None
    return value


def _hex64(value):
    return type(value) is str and _HEX64.fullmatch(value) is not None


def _inside(tracker, x, y):
    """Strictly inside the tracker's outline: along the axis in (0, 1), across in (-1, 1)."""
    (ax, ay), (bx, by) = tracker["axis_start"], tracker["axis_end"]
    hx, hy = tracker["half_cross_axis"]
    dx, dy = bx - ax, by - ay
    px, py = x - ax, y - ay
    along = (px * dx + py * dy) / (dx * dx + dy * dy)
    across = (px * hx + py * hy) / (hx * hx + hy * hy)
    return 0.0 < along < 1.0 and -1.0 < across < 1.0


def overlap_report(trackers, source_commands):
    """How the layouts overlap, counted per slot centre (nothing is removed): distinct centres,
    centres inside their own outline, centres inside exactly two outlines (and of those, the
    ones whose two trackers and own tracker share one non-null source command), and centres
    inside more than two. `trackers` are tracker_panel_layout trackers; `source_commands[k]` is
    tracker k's source command or None. A uniform grid of tracker bounding boxes, cell size the
    largest panel height or slot length, doubled until the boxes cover at most
    max(MAX_GRID_CELLS, 4 x trackers) cells; each centre tests only its own cell's trackers."""
    boxes, cell = [], 0.0
    for tracker in trackers:
        xs = [point[0] for point in tracker["outline"]]
        ys = [point[1] for point in tracker["outline"]]
        boxes.append((min(xs), max(xs), min(ys), max(ys)))
        cell = max(cell, tracker["panel_height_du"], tracker["slot_length_du"])
    report = {"distinct_centres": 0, "centres_in_own_outline": 0, "centres_in_two_outlines": 0,
              "centres_in_two_outlines_same_source_command": 0, "centres_in_more_than_two_outlines": 0}
    if not boxes:
        return report

    def span(low, high):
        return math.floor(high / cell) - math.floor(low / cell) + 1

    budget = max(MAX_GRID_CELLS, 4 * len(boxes))
    while sum(span(b[0], b[1]) * span(b[2], b[3]) for b in boxes) > budget:
        cell *= 2.0
    grid = {}
    for k, (x0, x1, y0, y1) in enumerate(boxes):
        for i in range(math.floor(x0 / cell), math.floor(x1 / cell) + 1):
            for j in range(math.floor(y0 / cell), math.floor(y1 / cell) + 1):
                grid.setdefault((i, j), []).append(k)
    distinct = set()
    for k, tracker in enumerate(trackers):
        command = source_commands[k]
        for x, y in tracker["panels"]:
            distinct.add((x, y))
            hits = [j for j in grid.get((math.floor(x / cell), math.floor(y / cell)), ())
                    if boxes[j][0] <= x <= boxes[j][1] and boxes[j][2] <= y <= boxes[j][3]
                    and _inside(trackers[j], x, y)]
            count = len(hits)
            report["centres_in_own_outline"] += k in hits
            report["centres_in_two_outlines"] += count == 2
            report["centres_in_more_than_two_outlines"] += count > 2
            report["centres_in_two_outlines_same_source_command"] += (
                count == 2 and command is not None
                and source_commands[hits[0]] == source_commands[hits[1]] == command)
    report["distinct_centres"] = len(distinct)
    return report


def _provenance_validator():
    """Build the packaged provenance schema validator once per process."""
    global _PROVENANCE_VALIDATOR
    validator = _PROVENANCE_VALIDATOR
    if validator is None:
        with _PROVENANCE_VALIDATOR_LOCK:
            validator = _PROVENANCE_VALIDATOR
            if validator is None:
                from jsonschema import Draft202012Validator

                validator = Draft202012Validator(
                    {"$ref": "#/$defs/provenance", "$defs": sdg.load_schema()["$defs"]},
                    format_checker=sdg.graph_format_checker())
                _PROVENANCE_VALIDATOR = validator
    return validator


def _check_provenance(provenance):
    """The caller's provenance: created_by, created_at and last_writer strings of 1..4096
    characters, source_rev an int in 0..MAX_REV, canonical JSON at most MAX_PROVENANCE_BYTES."""
    if type(provenance) is not dict:
        raise _invalid()
    for key in ("created_by", "created_at", "last_writer"):
        if _text(provenance.get(key), MAX_TEXT) is None:
            raise _invalid()
    source_rev = provenance.get("source_rev")
    if type(source_rev) is not int or not 0 <= source_rev <= MAX_REV:
        raise _invalid()
    for key in ("tool_id", "source_handle"):
        if key in provenance:
            value = provenance[key]
            if type(value) is not str or len(value) > MAX_TEXT:
                raise _invalid()
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                raise _invalid() from None
    if "catalog_versions" in provenance:
        versions = provenance["catalog_versions"]
        if type(versions) is not dict:
            raise _invalid()
        for key, value in versions.items():
            if type(key) is not str or type(value) is not str or len(value) > MAX_TEXT:
                raise _invalid()
            try:
                key.encode("utf-8")
                value.encode("utf-8")
            except UnicodeEncodeError:
                raise _invalid() from None
    try:
        stack = [(provenance, 0)]
        nodes = 0
        while stack:
            value, depth = stack.pop()
            nodes += 1
            if nodes > sdg.MAX_NODES or depth > MAX_PROVENANCE_DEPTH:
                raise _invalid()
            if type(value) is dict:
                stack.extend((key, depth + 1) for key in value)
                stack.extend((child, depth + 1) for child in value.values())
            elif type(value) is list:
                stack.extend((child, depth + 1) for child in value)
            elif type(value) is str:
                if len(value) > MAX_TEXT:
                    raise _invalid()
                value.encode("utf-8")
            elif type(value) not in (int, float, bool, type(None)):
                raise _invalid()
    except _INPUT_ERRORS:
        raise _invalid() from None
    try:
        size = len(json.dumps(provenance, sort_keys=True, separators=(",", ":"),
                              allow_nan=False, ensure_ascii=False).encode("utf-8"))
    except _INPUT_ERRORS:
        raise _invalid() from None
    if size > MAX_PROVENANCE_BYTES:
        raise _invalid()
    try:
        if not _provenance_validator().is_valid(provenance):
            raise _invalid()
        if not sdg.is_rfc3339_date_time(provenance["created_at"]):
            raise _invalid()
    except _INPUT_ERRORS:
        raise _invalid() from None


def _tracker(entity, tracker, row, mpu):
    """The frame's `tracker` metadata ($defs.tracker), every value inside its schema bound."""
    if tracker["entity_kind"] == "tracker":
        width = entity.get("cross_axis_width_du")
        if width is None:
            width = entity["scale"][1]
        width_m = abs(width) * mpu
        source = {"entity_kind": "tracker", "handle": None, "layer": None,
                  "block_name": _text(entity.get("block"), 255),
                  "source_command": _text(entity.get("source_command"), 64)}
        tilt = entity.get("max_tilt_deg")
    else:
        width_m = tracker["panel_height_du"] * mpu
        source = {"entity_kind": "polyline", "handle": None, "layer": _text(entity.get("layer"), 255),
                  "block_name": None, "source_command": None}
        tilt = None
    for value in (*tracker["axis_start"], *tracker["axis_end"]):
        _bounded(value, -MAX_PLAN_COORDINATE, MAX_PLAN_COORDINATE)
    if type(row["row_index"]) is not int:
        raise _invalid()
    return {
        "tracker_model": "single_axis_tracker", "axis_start": list(tracker["axis_start"]),
        "axis_end": list(tracker["axis_end"]), "axis_units": "drawing",
        "module_slots": tracker["module_slots"],
        "row_index": _bounded(row["row_index"], 0, 1000000),
        "length_m": _bounded(row["length_m"], 0, 100000, low_open=True),
        "rail_overhang_m": _bounded(row["rail_overhang_m"], 0, 1000),
        "cross_axis_width_m": _bounded(width_m, 0, 1000, low_open=True),
        "max_tilt_deg": None if tilt is None else _bounded(tilt, 0, 90),
        "source": source,
    }


def convert_physical_state(view, document, graph, *, provenance, rev):
    """The compact Ground frames of the physical head (`view`, `document`) for `graph`.

    Returns {"schema": RESULT_SCHEMA, "source": {"head_index", "state_artifact_id",
    "state_content_sha256"}, "frames": [...], "settings": {"PanelGroupNumber",
    "PanelGroupColour"}, "counts": {"trackers", "slots"}, "overlap": overlap_report(...)}.
    Every frame and slot template carries `rev` and a deep copy of `provenance`; the frame's
    module power is the stored module's TrackerModulePmaxW (solar_ground_layout.get_active_module).
    Units: a frame's insertion_point, module_width_along_row, module_height_across_row and slot
    centres are METRES (the layout's drawing-unit values times the document's meters_per_unit; a
    metre drawing stores the layout's own values unchanged). The tracker axis_start / axis_end and
    the stored outline stay in DRAWING units; length_m, rail_overhang_m and cross_axis_width_m are
    metres.
    Refusals, in this order: PHYSICAL_HEAD_REQUIRED, INPUT_INVALID (view, document, rev,
    provenance, graph), GROUND_PROJECT_REQUIRED, UNITS_MISMATCH, TRACKER_ROWS_REQUIRED,
    INPUT_INVALID (the tracker layer), TRACKER_ROWS_REQUIRED (none accepted), LIMIT_EXCEEDED
    (site), INPUT_INVALID (layout), LIMIT_EXCEEDED (frame), INPUT_INVALID (frame values)."""
    if view is None or document is None:
        raise GroundConversionError("GROUND_CONVERSION_PHYSICAL_HEAD_REQUIRED")
    head_state = view.get("state") if type(view) is dict else None
    if (type(head_state) is not dict or type(view.get("index")) is not int
            or not _hex64(head_state.get("artifact_id")) or not _hex64(head_state.get("content_sha256"))):
        raise _invalid()
    if (type(document) is not dict or type(document.get("state")) is not dict
            or type(document.get("units")) is not dict):
        raise _invalid()
    mpu = document["units"].get("meters_per_unit")
    if type(mpu) is not float or mpu not in ps.UNITS.values():
        raise _invalid()
    if type(rev) is not int or not 0 <= rev <= MAX_REV:
        raise _invalid()
    _check_provenance(provenance)
    project = graph.get("project") if type(graph) is dict else None
    if type(project) is not dict or not _hex64(graph.get("source_hash")):
        raise _invalid()
    if project.get("installation_design") != "Ground":
        raise GroundConversionError("GROUND_CONVERSION_GROUND_PROJECT_REQUIRED")
    units = project.get("units")
    if type(units) is not dict or units.get("meters_per_unit") != mpu:
        raise GroundConversionError("GROUND_CONVERSION_UNITS_MISMATCH")
    if view.get('kind') == 'canonical_version_intake':
        return _canonical_frames(view, document, graph, provenance, rev)
    state = document["state"]
    rows = state.get("tracker_rows")
    if type(rows) is not list or not rows:
        raise GroundConversionError("GROUND_CONVERSION_TRACKER_ROWS_REQUIRED")
    frames_in = state.get("frames", [])
    settings = state.get("settings", {})
    if type(frames_in) is not list or type(settings) is not dict:
        raise _invalid()
    try:
        entities = tracker_entities({"frames": frames_in, "tracker_rows": rows})
        counts = dsteps.trackers_to_panel_groups(entities, mpu)
    except _INPUT_ERRORS:
        raise _invalid() from None
    if counts["trackers"] == 0:
        raise GroundConversionError("GROUND_CONVERSION_TRACKER_ROWS_REQUIRED")
    if counts["panel_group_slots"] > codec.MAX_GRAPH_SLOTS:
        raise GroundConversionError("GROUND_CONVERSION_LIMIT_EXCEEDED")
    try:
        layout = dsteps.tracker_panel_layout(entities, mpu, settings)
        power = ground_layout.get_active_module(ground_layout.load_settings(settings)).pmax_w
    except _INPUT_ERRORS:
        raise _invalid() from None
    if any(tracker["module_slots"] > codec.MAX_FRAME_SLOTS for tracker in layout["trackers"]):
        raise GroundConversionError("GROUND_CONVERSION_LIMIT_EXCEEDED")
    seed = graph["source_hash"]
    frames, commands = [], []
    for k, tracker in enumerate(layout["trackers"]):
        entity = entities[tracker["entity_index"]]
        try:
            row = buildout.read_tracker_rows([entity], mpu)[0]
            metadata = _tracker(entity, tracker, row, mpu)
        except GroundConversionError:
            raise
        except _INPUT_ERRORS:
            raise _invalid() from None
        slots = tracker["module_slots"]
        commands.append(metadata["source"]["source_command"])
        # Ordinary graph geometry is metres (contract/CONTRACT.md); the tracker axes and the
        # stored outline stay in drawing units. A metre drawing keeps the layout's own values.
        if mpu == 1.0:
            centre, centres = list(tracker["center"]), tracker["panels"]
            along, across = tracker["slot_length_du"], tracker["panel_height_du"]
        else:
            centre = [value * mpu for value in tracker["center"]]
            centres = [[x * mpu, y * mpu] for x, y in tracker["panels"]]
            along, across = tracker["slot_length_du"] * mpu, tracker["panel_height_du"] * mpu
        frame = {
            "id": deterministic_id("frame", f"{seed}:ground-frame:{k}"), "kind": "frame", "rev": rev,
            "provenance": copy.deepcopy(provenance),
            "extra": {outlines.OUTLINE_KEY: outlines.outline_record(tracker["outline"])},
            "validity": {"state": "valid", "reasons": []}, "name": f"Group {tracker['group_number']}",
            "insertion_point": centre, "installation_design": "Ground",
            "panel_refs": [], "module_rows": 1, "module_columns": slots, "module_slots": slots,
            "module_power_watts": power, "module_width_along_row": along,
            "module_height_across_row": across, "electrical_zone_ref": None,
            "matrix": [], "sequences": [], "panel_assignments": [], "tracker": metadata,
        }
        ids = [deterministic_id("panel", f"{seed}:ground-panel:{k}:{i}") for i in range(slots)]
        try:
            frame["ground_slots"] = codec.encode_slots(
                ids, centres, tracker["row_angle_rad"],
                {"rev": rev, "provenance": copy.deepcopy(provenance),
                 "validity": {"state": "valid", "reasons": []}, "extra": {}})
        except GraphValidationError:
            raise _invalid() from None
        try:
            outlines.frame_outline(frame)   # never publish an outline its own reader would refuse
        except outlines.GroundOutlineError:
            raise _invalid() from None
        frames.append(frame)
    return {"schema": RESULT_SCHEMA,
            "source": {"head_index": view["index"], "state_artifact_id": head_state["artifact_id"],
                       "state_content_sha256": head_state["content_sha256"]},
            "frames": frames, "settings": dict(layout["settings"]),
            "counts": {"trackers": len(frames), "slots": layout["panel_count"]},
            "overlap": overlap_report(layout["trackers"], commands)}


def convert_current_ground_state(backend, tenant_id, drawing_id, graph, *, project_id, provenance, rev):
    """convert_physical_state over the drawing's current physical head, read once
    (solar_physical_head.load_physical_head); its PhysicalHeadError refusals pass through."""
    view, document = ph.load_physical_head(backend, tenant_id, drawing_id, project_id=project_id)
    return convert_physical_state(view, document, graph, provenance=provenance, rev=rev)


def _canonical_frames(view, document, graph, provenance, rev):
    """One compact row group per real module row, with exact source-frame association."""
    try:
        params = view['parameters']
        models, associations = params['generation_models'], params['frame_generations']
        source_frames = document['state']['frames']
        handles = [frame['handle'] for frame in source_frames]
        if (not source_frames or type(models) is not list or type(associations) is not dict
                or len(set(handles)) != len(handles) or set(handles) != set(associations)):
            raise _invalid()
        total = 0
        for handle in handles:
            index = associations[handle]
            if type(index) is not int or not 0 <= index < len(models):
                raise _invalid()
            model = models[index]
            rows, columns = model['rows'], model['columns']
            if (type(rows) is not int or type(columns) is not int or rows < 1 or columns < 1
                    or columns > codec.MAX_FRAME_SLOTS):
                raise _invalid()
            total += rows * columns
        if total > codec.MAX_GRAPH_SLOTS:
            raise GroundConversionError('GROUND_CONVERSION_LIMIT_EXCEEDED')
        mpu, seed = document['units']['meters_per_unit'], graph['source_hash']
        number = graph['settings']['panel_group_number']
        frames = []
        for source in source_frames:
            handle = source['handle']
            model = models[associations[handle]]
            rows, columns = model['rows'], model['columns']
            length = _bounded(model['module_length_m'], 0, 100000, low_open=True)
            width = _bounded(model['module_width_m'], 0, 100000, low_open=True)
            power = _bounded(model['module_power_wp'], 0, 100000, low_open=True)
            horizontal = _bounded(model['horizontal_gap_m'], 0, 100000)
            vertical = _bounded(model['vertical_gap_m'], 0, 100000)
            if model['orientation'] not in ('Portrait', 'Landscape'):
                raise _invalid()
            across, along = (length, width) if model['orientation'] == 'Landscape' else (width, length)
            pack = model['tracker_pack']
            segments = [segment for segment in pack['segments'] if segment is not None and segment['count'] > 0]
            packed = len(segments) > 1 or any(segment['kind'] != 'Modules' for segment in segments)
            positions, height = [], 0.0
            if packed:
                if along != width:
                    raise _invalid()  # The actual native pack length uses module width.
                for segment in segments:
                    count = segment['count']
                    if type(count) is not int or count < 1 or count > codec.MAX_FRAME_SLOTS:
                        raise _invalid()
                    if segment['kind'] == 'Modules':
                        positions.extend(height + (col + .5) * width for col in range(count))
                        height += count * width
                    elif segment['kind'] == 'JointGap':
                        height += count * _bounded(pack['joint_gap_width_m'], 0, 100000)
                    elif segment['kind'] == 'Motor':
                        height += count * (_bounded(pack['motor_gap_width_m'], 0, 100000)
                                           if pack['is_motor_gap_enabled'] else 0)
                    else:
                        raise _invalid()
                if len(positions) != columns:
                    raise _invalid()
            else:
                positions = [(col + .5) * along + col * vertical for col in range(columns)]
                height = columns * along + (columns - 1) * vertical
            full_width = rows * across + (rows - 1) * horizontal
            vertices = source['vertices']
            if len(vertices) != 4:
                raise _invalid()
            origin = vertices[0]
            u = [(vertices[1][i] - origin[i]) * mpu / full_width for i in (0, 1)]
            v = [(vertices[3][i] - origin[i]) * mpu / height for i in (0, 1)]
            if (not math.isclose(math.hypot(*u), 1, rel_tol=1e-9, abs_tol=1e-9)
                    or not math.isclose(math.hypot(*v), 1, rel_tol=1e-9, abs_tol=1e-9)
                    or abs(sum(u[i]*v[i] for i in (0, 1))) > 1e-9
                    or any(not math.isclose(vertices[2][i], vertices[1][i]+vertices[3][i]-origin[i],
                                            rel_tol=1e-9, abs_tol=1e-8) for i in (0, 1))):
                raise _invalid()
            def point(x, y):
                return [origin[i] + (u[i]*x + v[i]*y)/mpu for i in (0, 1)]
            angle = math.atan2(v[1], v[0])
            model_sha = hashlib.sha256(json.dumps(model, sort_keys=True, separators=(',', ':'),
                                                 allow_nan=False).encode()).hexdigest()
            for row in range(rows):
                left = row * (across + horizontal)
                centre = point(left + across/2, height/2)
                centres = [[n*mpu for n in point(left+across/2, y)] for y in positions]
                association = {'source_frame_handle': handle, 'source_module_row': row,
                               'source_generation': associations[handle], 'source_preset_sha256': model_sha,
                               'source_framing_type': model['framing_type']}
                prov = {**copy.deepcopy(provenance), 'source_handle': handle}
                outline = [point(left, 0), point(left+across, 0), point(left+across, height), point(left, height)]
                frame = {'id': deterministic_id('frame', f'{seed}:native-frame:{handle}:row:{row}'),
                    'kind': 'frame', 'rev': rev, 'provenance': prov,
                    'extra': {outlines.OUTLINE_KEY: outlines.outline_record(outline), 'physical_frame': association},
                    'validity': {'state': 'valid', 'reasons': []}, 'name': f'Group {number}',
                    'insertion_point': [n*mpu for n in centre], 'installation_design': 'Ground',
                    'panel_refs': [], 'module_rows': 1, 'module_columns': columns, 'module_slots': columns,
                    'module_power_watts': power, 'module_width_along_row': along,
                    'module_height_across_row': across, 'electrical_zone_ref': None,
                    'matrix': [], 'sequences': [], 'panel_assignments': [],
                    'tracker': {'tracker_model': 'single_axis_tracker', 'axis_start': point(left+across/2, 0),
                        'axis_end': point(left+across/2, height), 'axis_units': 'drawing', 'module_slots': columns,
                        'row_index': row, 'length_m': height, 'rail_overhang_m': 0.0,
                        'cross_axis_width_m': across, 'max_tilt_deg': None,
                        # Each row group is a logical view, not another native entity.
                        # The real handle remains explicit in physical_frame and panel provenance.
                        'source': {'entity_kind': 'polyline', 'handle': None, 'layer': source['layer'],
                                   'block_name': None, 'source_command': None}}}
                ids = [deterministic_id('panel', f'{seed}:native-panel:{handle}:{row}:{col}') for col in range(columns)]
                frame['ground_slots'] = codec.encode_slots(ids, centres, angle,
                    {'rev': rev, 'provenance': copy.deepcopy(prov), 'validity': {'state': 'valid', 'reasons': []},
                     'extra': copy.deepcopy(association)})
                outlines.frame_outline(frame)
                frames.append(frame)
                number += 1
        return {'schema': RESULT_SCHEMA,
                'source': {'head_index': view['index'], 'state_artifact_id': view['state']['artifact_id'],
                           'state_content_sha256': view['state']['content_sha256']},
                'frames': frames, 'settings': {'PanelGroupNumber': number, 'PanelGroupColour': 0},
                'counts': {'trackers': len(source_frames), 'groups': len(frames), 'slots': total},
                'overlap': {}}
    except GroundConversionError:
        raise
    except (_INPUT_ERRORS + (GraphValidationError,)):
        raise _invalid() from None
