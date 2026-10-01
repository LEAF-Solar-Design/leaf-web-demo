"""Native Ground frame and pile operations over the drawing's current physical state (preview).

Four operations, each a port already proven against the plugin (server/solar_ground_frames.py)
run the way scripts/solar_ground_studio_evidence.py threads them, now applied to the drawing's
current Ground physical head (server/solar_physical_head.py) and published as its child:

  frame-generate           LEAFGENERATE: pack frames into one boundary, append them to "frames"
                           with fresh handles taken from "next_handle";
  frame-collision-detect   LEAFCOLLISION: overlap markers appended to "collision_layer";
  piling-generate          LEAFPILING for native LEAF-TRACKERS frames: "piles" replaced;
  pile-length-range-check  LEAFCOLLISIONRANGE: out-of-window markers appended to "collision_layer".

Units are one boundary, frozen here. The drawing units are the head document's (a first state
takes them from the caller and every later generate must name the same ones). Frame vertices,
pile X, Y and Z, and boundary coordinates are drawing units; the terrain grid's elevations are
metres and its bounds drawing units (the grid contract of server/solar_ground_terrain.neutral_grid,
which server/solar_landxml_import.py writes); the frame kernel divides a sampled elevation by the
document's metres per drawing unit, and a pile's committed depth stays in metres.

Piles need a terrain surface: the head state's "grid" (stored by the LandXML import). No grid is
FRAMES_PILES_TERRAIN_REQUIRED, a grid under 2 x 2 or malformed is FRAMES_PILES_TERRAIN_INVALID,
and a pile whose position the grid does not cover is FRAMES_PILES_OFF_TERRAIN, never a pile at
elevation zero. Frame generation drapes frames on the grid when the head carries one and counts
the frames it could not drape.

Current-state selection: every operation names the head state it was built on (`base`, the
state artifact id, or None for a drawing with no physical state). A different current head is
FRAMES_PILES_STALE_BASE, except the exact retry of this operation on that base, which returns
the head it already published (outcome "retry"). An operation that changes nothing publishes
nothing (outcome "unchanged"). A writer that loses the race for the next head entry is refused
PHYSICAL_HEAD_CONFLICT by the head module.

Refused rather than approximated: PVcase BlockReference pile sources (an INSERT among the
frames), and site constraints the plugin would honour but this preview does not map (non-empty
"restriction_outlines", "setback_rings" or "road_lines" for generate; "road_lines" for piling).

Every refusal is a named, payload-free FRAMES_PILES_* code; a refusal from the physical state or
head store passes through with its own PHYSICAL_* code (FramesPilesError is a
PhysicalStateError, so one except clause covers both). No clock, network, environment read or
graph write lives here. Maturity is "preview".
"""
import hashlib
import json
import math
import re

import write_loop  # first: it puts the drawing store (da/store.py) on the path solar_physical_head imports
import solar_ground_frames as frames
import solar_ground_terrain as terrain
import solar_physical_head as ph
import solar_physical_state as ps
from solar_design_graph import GraphValidationError
from solar_graph_context import resolve_graph_context

RESULT_SCHEMA = "leaf.solar-frames-piles.v1"
PREVIEW_SCHEMA = "leaf.solar-frames-piles-preview.v1"
MATURITY = "preview"
GENERATE = "frame-generate"
COLLISION = "frame-collision-detect"
PILING = "piling-generate"
RANGE = "pile-length-range-check"
OPERATIONS = (GENERATE, COLLISION, PILING, RANGE)
OUTCOMES = ("published", "retry", "unchanged")
MAX_PROJECT_ID_CHARS = 100
MAX_BOUNDARY_VERTICES = frames.MAX_POLYGON_VERTICES     # 20,000
MAX_ABS_COORDINATE = 1e9                                # drawing units, every boundary value
MAX_PRESET_CHARS = 1_000_000                            # compact JSON of a preset or a template
MAX_NAME_CHARS = 256                                    # a preset or template Name
# The keys the operations read or write, in the order a new state carries them (the evidence
# producer's new_state, so a first generate's state equals its g1 state exactly).
NEW_STATE = (("grid", None), ("mesh_faces", 0), ("frames", []), ("piles", []), ("collision_layer", []),
             ("slope_markers", []), ("next_handle", 1))
GENERATE_CONSTRAINT_KEYS = ("restriction_outlines", "setback_rings", "road_lines")
PILING_CONSTRAINT_KEYS = ("road_lines",)
CODES = frozenset({
    "FRAMES_PILES_WRITES_DRAINED", "FRAMES_PILES_PROJECT_ID_INVALID", "FRAMES_PILES_BASE_INVALID",
    "FRAMES_PILES_DRAWING_UNITS_INVALID", "FRAMES_PILES_BOUNDARY_INVALID", "FRAMES_PILES_PRESET_INVALID",
    "FRAMES_PILES_PILE_TEMPLATE_INVALID", "FRAMES_PILES_DRAWING_NOT_FOUND", "FRAMES_PILES_GRAPH_REQUIRED",
    "FRAMES_PILES_PROJECT_MISMATCH", "FRAMES_PILES_STORE_UNAVAILABLE", "FRAMES_PILES_STALE_BASE",
    "FRAMES_PILES_STATE_REQUIRED", "FRAMES_PILES_UNITS_MISMATCH", "FRAMES_PILES_TERRAIN_REQUIRED",
    "FRAMES_PILES_TERRAIN_INVALID", "FRAMES_PILES_OFF_TERRAIN", "FRAMES_PILES_SITE_CONSTRAINTS_UNSUPPORTED",
    "FRAMES_PILES_PVCASE_UNSUPPORTED", "FRAMES_PILES_NO_FRAMES_FIT", "FRAMES_PILES_NO_FRAMES",
    "FRAMES_PILES_NO_PILES", "FRAMES_PILES_RANGE_INVALID", "FRAMES_PILES_STATE_INVALID",
    "FRAMES_PILES_LIMIT_EXCEEDED",
})
# Kernel refusals raised while an operation runs, by what caused them (the state otherwise).
_PRESET_KERNEL_CODES = frozenset({"frame_footprint_degenerate", "preset_rows_columns", "preset_missing",
                                  "piling_counts", "piling_config_missing", "pitch_invalid"})
_TEMPLATE_KERNEL_CODES = frozenset({"template_counts", "template_missing"})
_HEX64 = re.compile(r"[0-9a-f]{64}")


class FramesPilesError(ps.PhysicalStateError):
    """A named, payload-free refusal; a PhysicalStateError, so one except clause covers this
    module's codes and the physical state and head codes that pass through it."""


def canonical_sha256(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def _project_id(value):
    if type(value) is not str or not 1 <= len(value) <= MAX_PROJECT_ID_CHARS:
        raise FramesPilesError("FRAMES_PILES_PROJECT_ID_INVALID")
    return value


def _base(value):
    if value is not None and (type(value) is not str or not _HEX64.fullmatch(value)):
        raise FramesPilesError("FRAMES_PILES_BASE_INVALID")
    return value


def _boundary(value):
    """[[x, y], ...]: 3 to 20,000 points of two finite numbers, each at most 1e9 in magnitude."""
    if type(value) is not list or not 3 <= len(value) <= MAX_BOUNDARY_VERTICES:
        raise FramesPilesError("FRAMES_PILES_BOUNDARY_INVALID")
    out = []
    for point in value:
        if type(point) is not list or len(point) != 2:
            raise FramesPilesError("FRAMES_PILES_BOUNDARY_INVALID")
        for number in point:
            try:
                valid = (type(number) in (int, float) and math.isfinite(number)
                         and abs(number) <= MAX_ABS_COORDINATE)
            except OverflowError:
                valid = False
            if not valid:
                raise FramesPilesError("FRAMES_PILES_BOUNDARY_INVALID")
        out.append([point[0], point[1]])
    return out


def _object_text(value, code, names=("Name",)):
    """The compact JSON of a preset or template object, bounded; refused when it is not one.
    Every name member the module reports back (`names`) is a string of at most 256 characters."""
    if type(value) is not dict:
        raise FramesPilesError(code)
    try:
        text = json.dumps(value, separators=(",", ":"), allow_nan=False)
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise FramesPilesError(code) from None
    if len(text) > MAX_PRESET_CHARS:
        raise FramesPilesError(code)
    for key in names:
        matches = [name for member, name in value.items()
                   if type(member) is str and member.lower() == key.lower()]
        if len(matches) > 1 or any(type(name) is not str or len(name) > MAX_NAME_CHARS
                                  for name in matches):
            raise FramesPilesError(code)
    return text


def load_preset(preset):
    """(FramePreset, FramePresetStore) loaded the way the evidence producer loads its intake's
    active preset (Newtonsoft list reuse replicated, as the plugin reads it)."""
    _object_text(preset, "FRAMES_PILES_PRESET_INVALID", ("Name", "PileTemplateName"))
    name = preset.get("Name")
    if type(name) is not str or not name.strip():
        raise FramesPilesError("FRAMES_PILES_PRESET_INVALID")
    try:
        store = frames.load_frame_preset_store(json.dumps({"ActiveName": name, "Presets": [preset]}))
        loaded = store.get_active()
        if store.corrupt or loaded.name is None or loaded.name.lower() != name.lower():
            raise FramesPilesError("FRAMES_PILES_PRESET_INVALID")
        width, height = frames.frame_footprint(loaded)
    except (frames.GroundFramesError, OverflowError, ValueError, TypeError):
        raise FramesPilesError("FRAMES_PILES_PRESET_INVALID") from None
    if not (math.isfinite(width) and math.isfinite(height)) or width <= 0.0 or height <= 0.0:
        raise FramesPilesError("FRAMES_PILES_PRESET_INVALID")
    return loaded, store


def load_pile_store(template, preset, preset_store):
    """The PileTemplateStore holding `template` as its active template (named by its Name, else
    the preset's PileTemplateName, else "Full"), as the evidence producer builds it."""
    _object_text(template, "FRAMES_PILES_PILE_TEMPLATE_INVALID")
    name = template.get("Name")
    if not isinstance(name, str) or not name.strip():
        name = preset.pile_template_name or "Full"
    try:
        store = frames.load_pile_template_store(
            json.dumps({"Templates": {name: template}, "ActiveTemplate": name}),
            legacy_default_piling=preset_store.legacy_default_piling)
    except (frames.GroundFramesError, OverflowError, ValueError, TypeError):
        raise FramesPilesError("FRAMES_PILES_PILE_TEMPLATE_INVALID") from None
    if store.corrupt:
        raise FramesPilesError("FRAMES_PILES_PILE_TEMPLATE_INVALID")
    return store


def _kernel_refusal(exc):
    code = exc.code
    if code.endswith("_over_cap") or code.endswith("_too_many_vertices") or code == "stations_too_many":
        return "FRAMES_PILES_LIMIT_EXCEEDED"
    if code in _PRESET_KERNEL_CODES:
        return "FRAMES_PILES_PRESET_INVALID"
    if code in _TEMPLATE_KERNEL_CODES:
        return "FRAMES_PILES_PILE_TEMPLATE_INVALID"
    if code == "terrain_sample_invalid":
        return "FRAMES_PILES_TERRAIN_INVALID"
    if code.startswith("boundary_"):
        return "FRAMES_PILES_BOUNDARY_INVALID"
    return "FRAMES_PILES_STATE_INVALID"


def _context(backend, tenant_id, drawing_id, project_id):
    try:
        return resolve_graph_context(backend, tenant_id, drawing_id, "head", project_id=project_id)
    except GraphValidationError as exc:
        code = {"PROJECT_MISMATCH": "FRAMES_PILES_PROJECT_MISMATCH",
                "GRAPH_CONTEXT_UNAVAILABLE": "FRAMES_PILES_DRAWING_NOT_FOUND"}.get(
                    exc.code, "FRAMES_PILES_GRAPH_REQUIRED")
        raise FramesPilesError(code) from None
    except (OSError, RuntimeError):
        raise FramesPilesError("FRAMES_PILES_STORE_UNAVAILABLE") from None


def terrain_sampler(state, *, required):
    """(interpolate_z or None, terrain summary). The surface is the state's "grid"."""
    grid = state.get("grid")
    if grid is None:
        if required:
            raise FramesPilesError("FRAMES_PILES_TERRAIN_REQUIRED")
        return None, {"present": False, "rows": None, "cols": None, "grid_sha256": None}
    try:
        clean = terrain.mesh_grid(grid)
        interpolator = None if clean is None else terrain.terrain_interpolator(clean, 1.0)
        digest = canonical_sha256(clean) if clean is not None else None
    except (terrain.TerrainInputError, ValueError, TypeError, RecursionError):
        raise FramesPilesError("FRAMES_PILES_TERRAIN_INVALID") from None
    if interpolator is None:
        raise FramesPilesError("FRAMES_PILES_TERRAIN_INVALID")
    return interpolator.interpolate_z, {"present": True, "rows": clean["rows"], "cols": clean["cols"],
                                        "grid_sha256": digest}


def _list(state, key):
    value = state.get(key)
    if value is None:
        return []
    if type(value) is not list:
        raise FramesPilesError("FRAMES_PILES_STATE_INVALID")
    return value


def _constraints(state, keys):
    if any(_list(state, key) for key in keys):
        raise FramesPilesError("FRAMES_PILES_SITE_CONSTRAINTS_UNSUPPORTED")


def _bbox(vertices):
    xs = [v[0] for v in vertices]
    ys = [v[1] for v in vertices]
    return [min(xs), min(ys), max(xs), max(ys)]


# ---------------------------------------------------------------- operations --

def _generate(state, mpu, inputs):
    _constraints(state, GENERATE_CONSTRAINT_KEYS)
    sample, terrain_view = terrain_sampler(state, required=False)
    preset = inputs["preset"]
    result = frames.generate_frames([inputs["boundary"]], preset, mpu, terrain_z_m=sample)
    added = result["entities"]
    if not added:
        raise FramesPilesError("FRAMES_PILES_NO_FRAMES_FIT")
    handle = state.get("next_handle", 1)
    for ent in added:
        ent["handle"] = f"{handle:X}"
        handle += 1
    off = 0
    if sample is not None:
        for ent in added:
            vertices = ent["vertices"]
            cx = sum(v[0] for v in vertices) / len(vertices)
            cy = sum(v[1] for v in vertices) / len(vertices)
            off += sample(cx, cy) is None
    state["frames"] = _list(state, "frames") + added
    state["next_handle"] = handle
    return terrain_view, {"frames_added": len(added), "frames_off_terrain": off, "preset_name": preset.name}


def _collision(state, mpu, inputs):
    result = frames.run_collision(_list(state, "frames"))
    if result["status"] == "no_frames":
        raise FramesPilesError("FRAMES_PILES_NO_FRAMES")
    markers = [{"role": "collision", "bbox": _bbox(m["vertices"])} for m in result["markers"]]
    if markers:
        state["collision_layer"] = _list(state, "collision_layer") + markers
    return None, {"frames_checked": result["frame_count"], "collisions": len(markers)}


def _piling(state, mpu, inputs):
    frame_entities = _list(state, "frames")
    if any(type(ent) is dict and str(ent.get("type", "")).upper() == "INSERT" for ent in frame_entities):
        raise FramesPilesError("FRAMES_PILES_PVCASE_UNSUPPORTED")
    _constraints(state, PILING_CONSTRAINT_KEYS)
    sample, terrain_view = terrain_sampler(state, required=True)
    previous = _list(state, "piles")
    result = frames.run_piling(frame_entities + previous, inputs["preset"], inputs["pile_store"], mpu,
                               terrain_z_m=sample)
    if result["status"] == "no_sources":
        raise FramesPilesError("FRAMES_PILES_NO_FRAMES")
    if any(sample(p["x"], p["y"]) is None for p in result["piles"]):
        raise FramesPilesError("FRAMES_PILES_OFF_TERRAIN")
    if not result["entities"]:
        raise FramesPilesError("FRAMES_PILES_NO_PILES")
    state["piles"] = list(result["entities"])
    return terrain_view, {"native_frames": result["native_frame_count"], "piles": len(result["entities"]),
                          "piles_replaced": len(result["erase_indexes"]),
                          "grid_piles": result["grid_piles"], "joint_piles": result["joint_piles"],
                          "station_piles": result["station_piles"],
                          "short_trackers": len(result["short_trackers"]),
                          "template_name": result["template_name"]}


def _range(state, mpu, inputs):
    cfg = inputs["preset"].piling if inputs["preset"].piling is not None else frames.PilingConfig()
    if not (math.isfinite(cfg.min_pile_length_m) and math.isfinite(cfg.max_pile_length_m)):
        raise FramesPilesError("FRAMES_PILES_RANGE_INVALID")  # "1e400" text loads as infinity
    result = frames.run_pile_range_check(_list(state, "piles"), inputs["preset"], mpu)
    if result["status"] == "invalid_range":
        raise FramesPilesError("FRAMES_PILES_RANGE_INVALID")
    if result["status"] == "no_piles":
        raise FramesPilesError("FRAMES_PILES_NO_PILES")
    markers = [{"role": "pile-range", "bbox": _bbox(m["vertices"])} for m in result["markers"]]
    if markers:
        state["collision_layer"] = _list(state, "collision_layer") + markers
    return None, {"total_piles": result["total_piles"], "out_of_range": len(markers),
                  "min_m": result["min_m"], "max_m": result["max_m"]}


_RUN = {GENERATE: _generate, COLLISION: _collision, PILING: _piling, RANGE: _range}


def _operate(backend, tenant_id, drawing_id, operation, base, project_id, request, inputs, drawing_units=None):
    """The shared transaction: context, head, retry, stale base, units, run, publish."""
    context = _context(backend, tenant_id, drawing_id, project_id)
    project = context["project_id"]
    head, head_document = ph.load_physical_head(backend, tenant_id, drawing_id, project_id=project)
    request_sha256 = canonical_sha256(request)
    if (head is not None and head["parent"] == base and head_document["capability"] == operation
            and head_document["source"]["sha256"] == request_sha256):
        return _result(operation, "retry", False, drawing_id, project, base, head_document, head,
                       _terrain_view(head_document["state"]), None)
    if (None if head is None else head["state"]["artifact_id"]) != base:
        raise FramesPilesError("FRAMES_PILES_STALE_BASE")
    if head is None:
        if operation != GENERATE:
            raise FramesPilesError("FRAMES_PILES_STATE_REQUIRED")
        state = {key: (list(value) if type(value) is list else value) for key, value in NEW_STATE}
        frame = json.loads(json.dumps(ps.DEFAULT_FRAME))
        units = drawing_units
    else:
        units = head_document["units"]["drawing_units"]
        if drawing_units is not None and drawing_units != units:
            raise FramesPilesError("FRAMES_PILES_UNITS_MISMATCH")
        state = dict(head_document["state"])
        frame = head_document["frame"]
    mpu = ps.UNITS[units]
    stored_terrain_view = _terrain_view(state) if operation in (COLLISION, RANGE) else None
    try:
        terrain_view, summary = _RUN[operation](state, mpu, inputs)
    except frames.GroundFramesError as exc:
        raise FramesPilesError(_kernel_refusal(exc)) from None
    except ps.PhysicalStateError:
        # A typed refusal (including FramesPilesError) keeps its code; only an untyped exception becomes FRAMES_PILES_STATE_INVALID.
        raise
    except (TypeError, ValueError, IndexError, KeyError, AttributeError, OverflowError,
            ArithmeticError, RecursionError):
        # Only a typed refusal leaves an operation.
        raise FramesPilesError("FRAMES_PILES_STATE_INVALID") from None
    if terrain_view is None:
        terrain_view = stored_terrain_view
    if head is not None and repr(state) == repr(head_document["state"]):
        return _result(operation, "unchanged", False, drawing_id, project, base, head_document, head,
                       terrain_view, summary)
    document = ps.physical_document(state, drawing_units=units, source_sha256=request_sha256,
                                    capability=operation, parent=base, frame=frame)
    published = ph.publish_physical_state(backend, tenant_id, drawing_id, document, project_id=project)
    return _result(operation, "published" if published["created"] else "retry", published["created"],
                   drawing_id, project, base, document, published["head"], terrain_view, summary)


def _terrain_view(state):
    """The terrain summary for a result that ran no sampler; absent grids are allowed,
    but invalid stored grids propagate the terrain refusal before anything is published."""
    return terrain_sampler(state, required=False)[1]


def _result(operation, outcome, created, drawing_id, project, base, document, head, terrain_view, summary):
    return {"schema": RESULT_SCHEMA, "operation": operation, "maturity": MATURITY, "outcome": outcome,
            "created": created, "drawing_id": drawing_id, "project_id": project, "base": base,
            "units": {"drawing_units": document["units"]["drawing_units"],
                      "meters_per_unit": document["units"]["meters_per_unit"]},
            "terrain": dict(terrain_view, sampled=terrain_view["present"] and operation in (GENERATE, PILING)),
            "summary": summary, "preview": state_summary(document["state"]),
            "head": head}


def _common(project_id, base):
    if write_loop.drawing_mutations_refusal() is not None:
        raise FramesPilesError("FRAMES_PILES_WRITES_DRAINED")
    if project_id is not None:
        _project_id(project_id)
    return _base(base)


def generate_frames(backend, tenant_id, drawing_id, *, base, boundary, preset, drawing_units, project_id=None):
    """frame-generate: pack frames into `boundary` (drawing units) with the plugin preset object."""
    base = _common(project_id, base)
    if type(drawing_units) is not str or drawing_units not in ps.UNITS:
        raise FramesPilesError("FRAMES_PILES_DRAWING_UNITS_INVALID")
    boundary = _boundary(boundary)
    loaded, _ = load_preset(preset)
    request = {"operation": GENERATE, "base": base, "drawing_units": drawing_units, "boundary": boundary,
               "preset": preset}
    return _operate(backend, tenant_id, drawing_id, GENERATE, base, project_id, request,
                    {"boundary": boundary, "preset": loaded}, drawing_units)


def detect_collisions(backend, tenant_id, drawing_id, *, base, project_id=None):
    """frame-collision-detect: overlap markers for the head's native frames."""
    base = _common(project_id, base)
    request = {"operation": COLLISION, "base": base}
    return _operate(backend, tenant_id, drawing_id, COLLISION, base, project_id, request, {})


def generate_piles(backend, tenant_id, drawing_id, *, base, preset, pile_template, project_id=None):
    """piling-generate: piles for the head's native frames, on the head's terrain."""
    base = _common(project_id, base)
    loaded, preset_store = load_preset(preset)
    pile_store = load_pile_store(pile_template, loaded, preset_store)
    request = {"operation": PILING, "base": base, "preset": preset, "pile_template": pile_template}
    return _operate(backend, tenant_id, drawing_id, PILING, base, project_id, request,
                    {"preset": loaded, "pile_store": pile_store})


def check_pile_lengths(backend, tenant_id, drawing_id, *, base, preset, project_id=None):
    """pile-length-range-check: markers for piles outside the preset's length window."""
    base = _common(project_id, base)
    loaded, _ = load_preset(preset)
    request = {"operation": RANGE, "base": base, "preset": preset}
    return _operate(backend, tenant_id, drawing_id, RANGE, base, project_id, request, {"preset": loaded})


# ------------------------------------------------------------------- preview --

def state_summary(state):
    """The reopenable preview counts of a state (bounded ints only)."""
    if type(state) is not dict:
        state = {}
    roles = {"collision": 0, "pile-range": 0}
    markers = state.get("collision_layer")
    for marker in markers if type(markers) is list else []:
        role = marker.get("role") if type(marker) is dict else None
        if type(role) is str and role in roles:
            roles[role] += 1
    frame_entities = state.get("frames")
    pile_entities = state.get("piles")
    return {"schema": PREVIEW_SCHEMA, "maturity": MATURITY,
            "frames": len(frame_entities) if type(frame_entities) is list else 0,
            "piles": len(pile_entities) if type(pile_entities) is list else 0,
            "collision_markers": roles["collision"], "range_markers": roles["pile-range"],
            "terrain": state.get("grid") is not None}


def load_frames_piles(backend, tenant_id, drawing_id, *, project_id):
    """(head view, document, preview summary) of the drawing's current physical head, the state
    exactly as stored; (None, None, None) for a drawing with no physical state."""
    _project_id(project_id)
    head, document = ph.load_physical_head(backend, tenant_id, drawing_id, project_id=project_id)
    if head is None:
        return None, None, None
    return head, document, state_summary(document["state"])
