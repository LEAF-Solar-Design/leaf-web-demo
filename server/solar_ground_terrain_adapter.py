"""Ground terrain operations over the drawing's reopened physical state: the slope-coloured
terrain mesh (LEAFTERRAINMESH) and the tracker slope check and its clear
(LEAFTRACKERSLOPEVIOLATIONS, LEAFCLEARTRACKERSLOPEVIOLATIONS), run by the literal ports in
server/solar_ground_terrain.py on the grid the current physical head holds.

Everything here is a Ground Physical PREVIEW: every result and every status record says
maturity "preview", and none of it is a production claim. The interpretation is frozen:

  * current state: the drawing's physical head (server/solar_physical_head.py), the newest
    published state of its log. An operation may name the head state it was shown
    (expected_head); a different current head is refused rather than overwritten;
  * frame: the head document's frame must be the world frame with the identity transform,
    and the grid must carry no affine "frame" of its own. Grid nodes are row-major, the row
    index runs with drawing Y and the column index with drawing X, from (x_min, y_min) to
    (x_max, y_max) inclusive, so a cell is (x_max - x_min) / (cols - 1) drawing units wide;
  * units: grid bounds and every X and Y are drawing units, elevations are metres. The
    metres per drawing unit is the head document's (1.0 for m, 0.3048 for ft), never a
    prompt: the plugin asks Meters or Feet and defaults to Meters, Studio reads the document;
  * elevation datum and CRS are the head frame's, reported and never applied (no datum
    shift, no reprojection).

An operation publishes one child of the head that changes only what the plugin command
changes in a drawing (mesh_faces for the mesh; slope_markers for the check and the clear)
plus its own record under state["status_records"], which binds the preview to the grid it
was computed from. A later grid (a LandXML re-import) leaves that record behind, so a read
reports the preview "stale" instead of trusting it. Running an operation whose child would
equal the head publishes nothing.

Every refusal is a named, payload-free TERRAIN_* code; a refusal from the physical state or
head store passes through with its own PHYSICAL_* code (TerrainAdapterError is a
PhysicalStateError, so one except clause covers both). No clock, network, environment read
or graph write lives here.
"""
import hashlib
import json
import math
import re

import write_loop  # first: it puts da/ on sys.path, which solar_physical_head's `import store` needs
import solar_ground_terrain as terrain
import solar_physical_head as ph
import solar_physical_state as ps
from solar_design_graph import GraphValidationError
from solar_graph_context import resolve_graph_context

VIEW_SCHEMA = "leaf.solar-terrain-view.v1"
RESULT_SCHEMA = "leaf.solar-terrain-operation.v1"
RECORD_SCHEMA = "leaf.solar-terrain-preview.v1"
MATURITY = "preview"
MESH_CAPABILITY = "terrain-mesh-render"          # the ledger's LEAFTERRAINMESH capability
SLOPE_CAPABILITY = "tracker-slope-violations"    # LEAFTRACKERSLOPEVIOLATIONS and its clear
OPERATIONS = {"mesh": MESH_CAPABILITY, "slope": SLOPE_CAPABILITY, "slope-clear": SLOPE_CAPABILITY}
RECORD_KEYS = (MESH_CAPABILITY, SLOPE_CAPABILITY)   # keys this module owns in status_records
IDENTITY_TRANSFORM = (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1)
MAX_MESH_NODES = 90_000                 # 300 x 300: LEAFTOPOFROM3DFACES's MaxTargetCells squared
MAX_GRID_SIDE = 300
MAX_FLOAT = 1e15
MAX_FRAMES = 20_000
MAX_PROJECT_ID_CHARS = 100
LIMIT_KEYS = tuple(terrain.DEFAULT_PRESET_LIMITS) + ("Columns",)
MAX_LIMIT_PERCENT = 1000.0
MAX_LIMIT_DEGREES = 90.0
MAX_COLUMNS = 10_000
BUCKETS = ("Green", "Yellow", "Red")
CODES = frozenset({
    "TERRAIN_WRITES_DRAINED", "TERRAIN_PROJECT_ID_INVALID", "TERRAIN_EXPECTED_HEAD_INVALID",
    "TERRAIN_LIMITS_INVALID", "TERRAIN_DRAWING_NOT_FOUND", "TERRAIN_GRAPH_REQUIRED",
    "TERRAIN_PROJECT_MISMATCH", "TERRAIN_STORE_UNAVAILABLE", "TERRAIN_STATE_NOT_FOUND",
    "TERRAIN_HEAD_MOVED", "TERRAIN_FRAME_UNSUPPORTED", "TERRAIN_GRID_MISSING", "TERRAIN_GRID_INVALID",
    "TERRAIN_GRID_TOO_LARGE", "TERRAIN_FRAMES_INVALID", "TERRAIN_TOO_MANY_ROWS",
    "TERRAIN_NO_TRACKER_ROWS",
})
_HEX64 = re.compile(r"[0-9a-f]{64}")


class TerrainAdapterError(ps.PhysicalStateError):
    """A named, payload-free refusal; a PhysicalStateError, so one except clause covers the
    adapter's codes and the physical state and head codes that pass through it."""


def _sha(value):
    """Canonical sha256 (sorted keys, compact, no NaN); tuples hash as lists."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def _project_id(value):
    if type(value) is not str or not 1 <= len(value) <= MAX_PROJECT_ID_CHARS:
        raise TerrainAdapterError("TERRAIN_PROJECT_ID_INVALID")
    return value


def _expected_head(value):
    if value is not None and (type(value) is not str or not _HEX64.fullmatch(value)):
        raise TerrainAdapterError("TERRAIN_EXPECTED_HEAD_INVALID")
    return value


def resolve_limits(limits):
    """The seven slope limits the check uses: the plugin's FramePreset defaults (Columns 0)
    for every key not given. Closed keys; every percent in 0..1000, degrees in 0..90,
    Columns an int in 0..10,000 (bool refused)."""
    if limits is None:
        return terrain.preset_limits(None)
    if type(limits) is not dict or not set(limits) <= set(LIMIT_KEYS):
        raise TerrainAdapterError("TERRAIN_LIMITS_INVALID")
    for key, value in limits.items():
        if key == "Columns":
            if type(value) is not int or not 0 <= value <= MAX_COLUMNS:
                raise TerrainAdapterError("TERRAIN_LIMITS_INVALID")
            continue
        top = MAX_LIMIT_DEGREES if key == "MaxRowToRowSlopeDeg" else MAX_LIMIT_PERCENT
        try:
            valid = (type(value) in (int, float) and math.isfinite(value)
                     and 0.0 <= value <= top)
        except OverflowError:
            valid = False
        if not valid:
            raise TerrainAdapterError("TERRAIN_LIMITS_INVALID")
    return terrain.preset_limits(limits)


def document_frame(document):
    """The frozen frame of a head document: units, metres per drawing unit, CRS and elevation
    datum. Refuses any transform other than the identity (TERRAIN_FRAME_UNSUPPORTED)."""
    frame = document["frame"]
    transform = frame["transform"]
    if len(transform) != 16 or any(float(a) != float(b) for a, b in zip(transform, IDENTITY_TRANSFORM)):
        raise TerrainAdapterError("TERRAIN_FRAME_UNSUPPORTED")
    return {"coordinate_system": frame["coordinate_system"], "transform": "identity",
            "drawing_units": document["units"]["drawing_units"],
            "meters_per_unit": document["units"]["meters_per_unit"],
            "crs": frame["crs"], "elevation_datum": frame["elevation_datum"],
            "horizontal": "drawing-units", "elevation": "metres"}


def document_grid(document):
    """The head's terrain grid, validated for the operations: the neutral grid's clean copy
    (server/solar_ground_terrain.neutral_grid), at least 2 x 2, at most MAX_MESH_NODES nodes,
    bounds strictly increasing, no affine frame. Refuses TERRAIN_GRID_MISSING when the state
    has no grid (absent or None)."""
    grid = document["state"].get("grid")
    if grid is None:
        raise TerrainAdapterError("TERRAIN_GRID_MISSING")
    if "frame" in grid:
        raise TerrainAdapterError("TERRAIN_FRAME_UNSUPPORTED")
    rows, cols = grid.get("rows"), grid.get("cols")
    if (type(rows) is int and type(cols) is int and rows > 0 and cols > 0
            and (rows > MAX_GRID_SIDE or cols > MAX_GRID_SIDE or rows * cols > MAX_MESH_NODES)):
        raise TerrainAdapterError("TERRAIN_GRID_TOO_LARGE")
    try:
        clean = terrain.neutral_grid(grid)
    except (terrain.TerrainInputError, OverflowError):
        raise TerrainAdapterError("TERRAIN_GRID_INVALID") from None
    if (clean["rows"] < 2 or clean["cols"] < 2
            or not (clean["x_min"] < clean["x_max"] and clean["y_min"] < clean["y_max"])
            or not all(math.isfinite(value) for value in clean["elevations"])):
        raise TerrainAdapterError("TERRAIN_GRID_INVALID")
    cell_x = (clean["x_max"] - clean["x_min"]) / (clean["cols"] - 1)
    cell_y = (clean["y_max"] - clean["y_min"]) / (clean["rows"] - 1)
    if (any(abs(clean[key]) > MAX_FLOAT for key in terrain.GRID_BOUNDS)
            or any(abs(value) > MAX_FLOAT for value in clean["elevations"])
            or not math.isfinite(cell_x) or cell_x > MAX_FLOAT
            or not math.isfinite(cell_y) or cell_y > MAX_FLOAT):
        raise TerrainAdapterError("TERRAIN_GRID_INVALID")
    # Mirror draw_grid_mesh's origins and endpoints, including float rounding.
    for col in range(clean["cols"] - 1):
        x0 = clean["x_min"] + col * cell_x
        if not x0 + cell_x > x0:
            raise TerrainAdapterError("TERRAIN_GRID_INVALID")
    for row in range(clean["rows"] - 1):
        y0 = clean["y_min"] + row * cell_y
        if not y0 + cell_y > y0:
            raise TerrainAdapterError("TERRAIN_GRID_INVALID")
    return clean


def grid_summary(grid, meters_per_unit):
    """Rows, columns, bounds (drawing units), cell size in drawing units and metres, the
    elevation range in metres, and the grid's canonical digest."""
    cell_x = (grid["x_max"] - grid["x_min"]) / (grid["cols"] - 1)
    cell_y = (grid["y_max"] - grid["y_min"]) / (grid["rows"] - 1)
    return {"rows": grid["rows"], "cols": grid["cols"],
            **{key: grid[key] for key in terrain.GRID_BOUNDS},
            "cell_x": cell_x, "cell_y": cell_y,
            "cell_x_m": cell_x * meters_per_unit, "cell_y_m": cell_y * meters_per_unit,
            "elevation_min_m": min(grid["elevations"]), "elevation_max_m": max(grid["elevations"]),
            "grid_sha256": _sha(grid)}


def mesh_of(document):
    """The slope-coloured mesh LEAFTERRAINMESH draws from the document's grid, in its own
    units: one face per cell, row-major (server/solar_ground_terrain.draw_grid_mesh). Pure."""
    document_frame(document)
    grid = document_grid(document)
    mesh = terrain.draw_grid_mesh(grid["elevations"], grid["rows"], grid["cols"], grid["x_min"],
                                  grid["x_max"], grid["y_min"], grid["y_max"],
                                  document["units"]["meters_per_unit"])
    if not all(math.isfinite(face["slope_percent"]) for face in mesh):
        raise TerrainAdapterError("TERRAIN_GRID_INVALID")
    if any(not math.isfinite(value) or abs(value) > MAX_FLOAT
           for face in mesh for vertex in face["vertices"] for value in vertex):
        raise TerrainAdapterError("TERRAIN_GRID_INVALID")
    return mesh


def _mesh_record(grid, mpu, mesh):
    buckets = {name: 0 for name in BUCKETS}
    for face in mesh:
        buckets[face["bucket"]] += 1
    return {"schema": RECORD_SCHEMA, "capability": MESH_CAPABILITY, "maturity": MATURITY,
            "grid_sha256": _sha(grid), "meters_per_unit": mpu, "faces": len(mesh),
            "buckets": buckets, "max_slope_percent": max(face["slope_percent"] for face in mesh),
            "mesh_sha256": _sha(mesh)}


def _frame_entities(state):
    """The tracker frames in the state as the LWPOLYLINEs the plugin reads for slope: each
    frame is a LEAF-TRACKERS polyline carrying its generated frame cell. Fails closed on a
    malformed frame list."""
    frames = state.get("frames", [])
    entities = []
    for frame in frames:
        if (type(frame) is not dict or type(frame.get("layer")) is not str
                or type(frame.get("vertices")) not in (list, tuple)
                or type(frame.get("row")) is not int or type(frame.get("col")) is not int):
            raise TerrainAdapterError("TERRAIN_FRAMES_INVALID")
        entities.append({"kind": "LWPOLYLINE", "layer": frame["layer"], "vertices": frame["vertices"],
                         "frame_cell": {"row": frame["row"], "col": frame["col"]}})
    return entities


def _report_summary(report):
    keys = ("tracker_count", "axial_rows_checked", "axial_violation_rows", "cross_axis_pairs_checked",
            "cross_axis_violation_pairs", "row_to_row_pairs_checked", "row_to_row_angle_violation_pairs",
            "trackers_needing_terrain_following", "has_violations", "status")
    return {key: report[key] for key in keys}


def _context(backend, tenant_id, drawing_id, project_id):
    try:
        return resolve_graph_context(backend, tenant_id, drawing_id, "head", project_id=project_id)
    except GraphValidationError as exc:
        code = {"PROJECT_MISMATCH": "TERRAIN_PROJECT_MISMATCH",
                "GRAPH_CONTEXT_UNAVAILABLE": "TERRAIN_DRAWING_NOT_FOUND"}.get(exc.code, "TERRAIN_GRAPH_REQUIRED")
        raise TerrainAdapterError(code) from None
    except (OSError, RuntimeError):
        raise TerrainAdapterError("TERRAIN_STORE_UNAVAILABLE") from None


def _preflight(project_id, expected_head):
    """The checks every mutating operation makes before it reads anything: drained first,
    then the optional project id and expected head."""
    if write_loop.drawing_mutations_refusal() is not None:
        raise TerrainAdapterError("TERRAIN_WRITES_DRAINED")
    if project_id is not None:
        _project_id(project_id)
    _expected_head(expected_head)


def _current(backend, tenant_id, drawing_id, project_id, expected_head):
    """(context, head view, head document) for a mutating operation: the graph context, then
    the current head (TERRAIN_STATE_NOT_FOUND when there is none, then TERRAIN_HEAD_MOVED
    when it is not the expected one). _preflight has run."""
    context = _context(backend, tenant_id, drawing_id, project_id)
    head, document = ph.load_physical_head(backend, tenant_id, drawing_id, project_id=context["project_id"])
    if head is None:
        raise TerrainAdapterError("TERRAIN_STATE_NOT_FOUND")
    if expected_head is not None and head["state"]["artifact_id"] != expected_head:
        raise TerrainAdapterError("TERRAIN_HEAD_MOVED")
    return context, head, document


def _with_record(state, capability, record):
    """A copy of the state's status_records with this capability's record set (or removed
    when record is None); the head's own dict is never mutated."""
    records = dict(state.get("status_records") or {})
    if record is None:
        records.pop(capability, None)
    else:
        records[capability] = record
    return records


def _publish(backend, tenant_id, drawing_id, context, head, document, state, capability):
    """Publish `state` as a child of the head (created False and nothing written when it
    equals the head's state)."""
    if state == document["state"]:
        return False, head
    child = ps.physical_document(state, drawing_units=document["units"]["drawing_units"],
                                 source_sha256=document["source"]["sha256"], capability=capability,
                                 parent=head["state"]["artifact_id"], frame=document["frame"])
    published = ph.publish_physical_state(backend, tenant_id, drawing_id, child,
                                          project_id=context["project_id"])
    return published["created"], published["head"]


def _result(operation, created, drawing_id, project_id, frame, grid, record, replaced, head, **extra):
    return {"schema": RESULT_SCHEMA, "maturity": MATURITY, "operation": operation,
            "capability": OPERATIONS[operation], "created": created, "drawing_id": drawing_id,
            "project_id": project_id, "frame": frame, "grid": grid, "record": record,
            "replaced": replaced, **extra, "head": head}


def render_mesh(backend, tenant_id, drawing_id, *, project_id=None, expected_head=None):
    """LEAFTERRAINMESH over the current head: render the mesh from the head's grid in the
    head's units and publish a child whose mesh_faces is the face count drawn, with the
    mesh's preview record. `replaced` is the head's mesh_faces (the faces the plugin erases)."""
    _preflight(project_id, expected_head)
    context, head, document = _current(backend, tenant_id, drawing_id, project_id, expected_head)
    frame = document_frame(document)
    grid = document_grid(document)
    mesh = mesh_of(document)
    mpu = frame["meters_per_unit"]
    record = _mesh_record(grid, mpu, mesh)
    replaced = document["state"].get("mesh_faces", 0)
    state = dict(document["state"])
    state["mesh_faces"] = len(mesh)
    state["status_records"] = _with_record(state, MESH_CAPABILITY, record)
    created, head_view = _publish(backend, tenant_id, drawing_id, context, head, document, state,
                                  MESH_CAPABILITY)
    return _result("mesh", created, drawing_id, context["project_id"], frame, grid_summary(grid, mpu),
                   record, replaced, head_view)


def check_tracker_slope(backend, tenant_id, drawing_id, *, limits=None, project_id=None,
                        expected_head=None):
    """LEAFTRACKERSLOPEVIOLATIONS over the current head: read the state's tracker frames,
    validate them on the head's grid against the limits, and publish a child whose
    slope_markers are the violation overlays (each {"role": "slope", "bbox": [x_min, y_min,
    x_max, y_max]} of the row axis drawn), with the check's preview record. `replaced` is the
    head's marker count (the overlays the plugin erases first)."""
    _preflight(project_id, expected_head)
    resolved = resolve_limits(limits)
    context, head, document = _current(backend, tenant_id, drawing_id, project_id, expected_head)
    frame = document_frame(document)
    grid = document_grid(document)
    mpu = frame["meters_per_unit"]
    if len(document["state"].get("frames", [])) > MAX_FRAMES:
        raise TerrainAdapterError("TERRAIN_FRAMES_INVALID")
    entities = _frame_entities(document["state"])
    try:
        report, _ = terrain.build_report(grid, entities, resolved, mpu)
    except terrain.TerrainBoundsError:
        raise TerrainAdapterError("TERRAIN_TOO_MANY_ROWS") from None
    except terrain.TerrainInputError:
        raise TerrainAdapterError("TERRAIN_FRAMES_INVALID") from None
    if report is None or not any(report[key] for key in (
            "axial_rows_checked", "cross_axis_pairs_checked", "row_to_row_pairs_checked")):
        raise TerrainAdapterError("TERRAIN_NO_TRACKER_ROWS")
    overlays = terrain.violation_overlays(report)
    markers = []
    for overlay in overlays:
        (x0, y0), (x1, y1) = overlay["vertices"]
        markers.append({"role": "slope", "bbox": [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]})
    summary = _report_summary(report)
    record = {"schema": RECORD_SCHEMA, "capability": SLOPE_CAPABILITY, "maturity": MATURITY,
              "grid_sha256": _sha(grid), "meters_per_unit": mpu, "limits": resolved,
              "frames": len(entities), "markers": len(markers), "status": summary["status"],
              "report_sha256": _sha(report)}
    replaced = len(document["state"].get("slope_markers", []))
    state = dict(document["state"])
    state["slope_markers"] = markers
    state["status_records"] = _with_record(state, SLOPE_CAPABILITY, record)
    created, head_view = _publish(backend, tenant_id, drawing_id, context, head, document, state,
                                  SLOPE_CAPABILITY)
    return _result("slope", created, drawing_id, context["project_id"], frame, grid_summary(grid, mpu),
                   record, replaced, head_view, report=summary)


def clear_tracker_slope(backend, tenant_id, drawing_id, *, project_id=None, expected_head=None):
    """LEAFCLEARTRACKERSLOPEVIOLATIONS over the current head: publish a child with no slope
    markers and no slope record. Needs no grid. Nothing is published when the head has no
    markers and no slope record."""
    _preflight(project_id, expected_head)
    context, head, document = _current(backend, tenant_id, drawing_id, project_id, expected_head)
    frame = document_frame(document)
    current = document["state"]
    replaced = len(current.get("slope_markers", []))
    if replaced == 0 and SLOPE_CAPABILITY not in (current.get("status_records") or {}):
        created, head_view = False, head
    else:
        state = dict(current)
        state["slope_markers"] = []
        state["status_records"] = _with_record(state, SLOPE_CAPABILITY, None)
        created, head_view = _publish(backend, tenant_id, drawing_id, context, head, document, state,
                                      SLOPE_CAPABILITY)
    return _result("slope-clear", created, drawing_id, context["project_id"], frame, None, None,
                   replaced, head_view)


def _preview_state(record, grid_sha256, mpu, count):
    """"absent" with no record; "current" when the record was computed from this grid in these
    units and the state still holds what it wrote; "stale" otherwise."""
    if record is None:
        return "absent"
    if (type(record) is not dict or record.get("grid_sha256") != grid_sha256
            or record.get("meters_per_unit") != mpu
            or record.get("faces", record.get("markers")) != count):
        return "stale"
    return "current"


def terrain_view(document):
    """The frozen reading of one physical state document: its frame, its grid summary (None
    when the state has no grid) and each preview's record with its standing. Pure."""
    frame = document_frame(document)
    state = document["state"]
    mpu = frame["meters_per_unit"]
    if state.get("grid") is None:
        grid, digest = None, None
    else:
        grid = grid_summary(document_grid(document), mpu)
        digest = grid["grid_sha256"]
    records = state.get("status_records") or {}
    counts = {MESH_CAPABILITY: state.get("mesh_faces", 0),
              SLOPE_CAPABILITY: len(state.get("slope_markers", []))}
    previews = {}
    for capability in RECORD_KEYS:
        record = records.get(capability)
        previews[capability] = {"state": _preview_state(record, digest, mpu, counts[capability]),
                                "record": record}
    return {"schema": VIEW_SCHEMA, "maturity": MATURITY, "frame": frame, "grid": grid,
            "mesh_faces": counts[MESH_CAPABILITY], "slope_markers": counts[SLOPE_CAPABILITY],
            "previews": previews}


def read_terrain(backend, tenant_id, drawing_id, *, project_id):
    """(head view, terrain view) of the drawing's current physical head; (None, None) for a
    drawing with no head. Reads only."""
    _project_id(project_id)
    head, document = ph.load_physical_head(backend, tenant_id, drawing_id, project_id=project_id)
    if head is None:
        return None, None
    return head, dict(terrain_view(document), drawing_id=drawing_id, project_id=head["project_id"])
