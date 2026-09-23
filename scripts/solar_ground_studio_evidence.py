#!/usr/bin/env python3
"""Studio's ground evidence per step, computed from a G11 intake (contract v6, G10 to G17).

Reads the intake (inputs only: units, boundary, the active preset object, the pile
template object and, for the terrain fixture, the LEAF-TERRAIN faces), runs the G14
scenario list for that fixture through Studio's own engines
(server/solar_ground_frames.py, server/solar_ground_terrain.py) and writes one
`exports` evidence document per step. Step N is computed from Studio's OWN state after
step N-1, starting from the intake (G13); nothing here reads plugin output.

Evidence (G12 as amended by G16 and G17), after = {rows, source_revision: <step id>,
format: "ground-v1"}; every row is {id, type, quantity: 1, unit: "each", ...}:
  frame      vertices (four [x, y] points from the smallest rounded (y, x),
             counter-clockwise), row_index, col_index, color_index
  pile-set   frame (the frame id whose polygon holds the pile, or null), diameter,
             length (z_top - z_bottom, one length), count, piles ([x, y, z_bottom]
             points in ascending (y, x) order)
  marker     role ("collision" | "pile-range" | "slope"), bbox {min, max} [x, y] points
  terrain-grid  rows, cols, extent {min, max} [x, y] points, elevations (row-major
             length quantities)
  terrain-mesh  rows, cols, extent, cell_colors (row-major, one per grid cell)
  off-grid-face  vertices (four [x, y, z] points), color_index; Studio's mesh is its
             own grid, so Studio never emits one (only the plugin adapter can)
  removed    of (the row type the erased entities belong to), count (entities erased)
A step's rows are its DELTA: what the command added, plus removed counts. Frame,
marker and pile-set ids are <type>-<n>, n from 1; frames and markers number by
ascending (centroid y, centroid x) rounded to 1e-6, pile sets by their frame's id
(null last) then ascending length, removed rows by `of`. A pile set's `frame` names
the frame among ALL live frames after the step, numbered the same way. Rows are
emitted sorted by (type, n) (G9).

G17: every coordinate quantity is ONE point of 2 or 3 numbers, the only shape the
frozen comparator reads; a list of points is a JSON list of point quantities. Every
value keeps the 1 mm coordinate and length tolerance.

Document size, counted the way the comparator's _bounded counts (every dict, list and
scalar is a node; keys are free). A document is 66 fixed nodes plus one mapping entry
per id'd row plus its rows. A pile set of k piles is 17 + 7k nodes; a terrain grid of
R x C is 22 + 4RC, its mesh row 22 + (R-1)(C-1). The terrain fixture's t4 (1197 frames,
9576 piles, one length per frame) is 66 + 1197 * 2 (mapping: its pile sets and the frames they reference) + 1197 * 18 + 9576 * 7 = 91,038 nodes and
its t1 (a 90 x 150 grid plus its mesh) is 66 + 2 + 44 + 54,000 + 13,261 = 67,373, both
under the 100,000 bound. A 150-cell import fits at most 133 x 150 (99,580 nodes).
The tests rebuild documents at those shapes and assert these exact counts.

Colours: color_index is the frame's ACI (256 when ByLayer); a mesh cell's colour is
the bucket's true-colour integer r<<16 | g<<8 | b (TerrainColors.cs:10-16).

Fails closed: a malformed intake, a step list it does not know, an evidence document
over the comparator's bounds, or an untracked intake (no revision) is refused with a
named error, never a partial document.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
from pathlib import Path
import re
import subprocess
import sys

# The ledger version of every ground capability this producer emits (the gate rejects any other).
CAPABILITY_VERSION = "0"

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve string annotations through sys.modules
    spec.loader.exec_module(module)
    return module


compare = _load("solar_w1_compare", HERE / "solar_w1_compare.py")
frames = _load("solar_ground_frames", ROOT / "server" / "solar_ground_frames.py")
terrain = _load("solar_ground_terrain", ROOT / "server" / "solar_ground_terrain.py")

FORMAT = "ground-v1"
UNIT = "m"
KINDS = ("generate", "terrain")
# G14: the scenario list per fixture, in order: (step id, capability, Studio operation).
# The operation names the plugin command the step replays: topo-import is
# LEAFTOPOFROM3DFACES, mesh LEAFTERRAINMESH, generate LEAFGENERATE, collision
# LEAFCOLLISION, piling the piling command, range LEAFCOLLISIONRANGE, slope
# LEAFTRACKERSLOPEVIOLATIONS and slope-clear LEAFCLEARTRACKERSLOPEVIOLATIONS.
SCENARIOS = {
    "generate": (("g1", "frame-generate", "generate"),
                 ("g2", "frame-collision-detect", "collision"),
                 ("g3", "piling-generate", "piling"),
                 ("g4", "pile-length-range-check", "range"),
                 ("g5", "frame-generate", "generate"),
                 ("g6", "frame-collision-detect", "collision")),
    "terrain": (("t1", "terrain-import", "topo-import"),
                ("t2", "terrain-mesh-render", "mesh"),
                ("t3", "frame-generate", "generate"),
                ("t4", "piling-generate", "piling"),
                ("t5", "pile-length-range-check", "range"),
                ("t6", "tracker-slope-violations", "slope"),
                ("t7", "tracker-slope-violations", "slope-clear")),
}
# G11: the topo prompts took their defaults on both fixtures.
UNITS_KEYWORD = "Meters"
GRID_CELLS_LONG_AXIS = terrain.DEFAULT_TARGET_CELLS
# Drawing units the intake may declare, as metres per drawing unit (G11: "m" only).
METERS_PER_UNIT = {"m": 1.0}
FRAME = {"coordinate_system": "world", "transform": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
         "elevation_datum": "unrecorded", "crs": "none"}
INTAKE_KEYS = {"units", "boundary", "active_preset", "pile_template"}
MAX_BOUNDARY_VERTICES = 20_000
MAX_TERRAIN_FACES = 100_000
TERRAIN_LAYER = terrain.PREFERRED_TERRAIN_LAYER
GIT_TIMEOUT = 10


class EvidenceError(ValueError):
    """A named refusal: nothing is written."""


# ------------------------------------------------------------------- intake --

def _finite(value, what):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise EvidenceError(f"{what} must be a finite number")
    return float(value)


def _point(value, width, what):
    if not isinstance(value, list) or len(value) != width:
        raise EvidenceError(f"{what} must be a list of {width} numbers")
    return [_finite(v, what) for v in value]


def validate_intake(intake, kind):
    """G11, fail closed. Returns the intake unchanged when it is well formed."""
    if kind not in KINDS:
        raise EvidenceError(f"fixture kind must be one of {KINDS}")
    if not isinstance(intake, dict):
        raise EvidenceError("intake must be a JSON object")
    expected = INTAKE_KEYS | ({"terrain_faces"} if kind == "terrain" else set())
    if set(intake) != expected:
        raise EvidenceError(f"intake keys must be exactly {sorted(expected)}")
    if intake["units"] not in METERS_PER_UNIT:
        raise EvidenceError(f"intake units must be one of {sorted(METERS_PER_UNIT)}")
    boundary = intake["boundary"]
    if not isinstance(boundary, list) or not 3 <= len(boundary) <= MAX_BOUNDARY_VERTICES:
        raise EvidenceError(f"boundary must hold 3 to {MAX_BOUNDARY_VERTICES} vertices")
    for v in boundary:
        _point(v, 2, "boundary vertex")
    for key in ("active_preset", "pile_template"):
        if not isinstance(intake[key], dict):
            raise EvidenceError(f"{key} must be an object")
    name = intake["active_preset"].get("Name")
    if not isinstance(name, str) or not name.strip():
        raise EvidenceError("active_preset must carry its Name")
    if kind == "terrain":
        faces = intake["terrain_faces"]
        if not isinstance(faces, list) or not 1 <= len(faces) <= MAX_TERRAIN_FACES:
            raise EvidenceError(f"terrain_faces must hold 1 to {MAX_TERRAIN_FACES} faces")
        for face in faces:
            if not isinstance(face, list) or len(face) != 4:
                raise EvidenceError("a terrain face must be four [x, y, z] corners")
            for corner in face:
                _point(corner, 3, "terrain face corner")
    return intake


def load_stores(intake):
    """The active preset and the pile template store as the plugin loads them."""
    preset_obj = intake["active_preset"]
    name = preset_obj["Name"]
    try:
        store = frames.load_frame_preset_store(json.dumps({"ActiveName": name, "Presets": [preset_obj]}))
        preset = store.get_active()
        if store.corrupt or preset.name is None or preset.name.lower() != name.lower():
            raise EvidenceError("active_preset does not load as a frame preset")
        template = intake["pile_template"]
        template_name = template.get("Name")
        if not isinstance(template_name, str) or not template_name.strip():
            template_name = preset.pile_template_name or "Full"
        pile_store = frames.load_pile_template_store(
            json.dumps({"Templates": {template_name: template}, "ActiveTemplate": template_name}),
            legacy_default_piling=store.legacy_default_piling)
        if pile_store.corrupt:
            raise EvidenceError("pile_template does not load as a pile template")
    except frames.GroundFramesError as exc:
        raise EvidenceError(f"preset or pile template refused: {exc}") from None
    return preset, pile_store


def slope_limits(preset):
    """The FramePreset slope fields the terrain engine reads (terrain.preset_limits)."""
    return {"MaxNsSlopePct": preset.max_ns_slope_pct,
            "MaxRowToRowEwSlopePct": preset.max_row_to_row_ew_slope_pct,
            "MaxAxialSlopePct": preset.max_axial_slope_pct,
            "MaxCrossAxisSlopePct": preset.max_cross_axis_slope_pct,
            "MaxRowToRowSlopeDeg": preset.max_row_to_row_slope_deg,
            "MaxSlopePercent": preset.max_slope_percent,
            "Columns": preset.columns}


# ------------------------------------------------------------------- rows --

def _key(x, y):
    """Ascending (y, x) rounded to 1e-6, the G12 ordering key."""
    return (round(y, 6), round(x, 6))


def point(values):
    """G17: ONE coordinate quantity, a point of 2 or 3 finite numbers; fails closed."""
    values = list(values)
    if len(values) not in (2, 3):
        raise EvidenceError("a coordinate quantity is one point of 2 or 3 numbers")
    return {"kind": "coordinate", "value": [_finite(v, "coordinate") for v in values], "unit": UNIT}


def _min_max(xmin, ymin, xmax, ymax):
    return {"min": point((xmin, ymin)), "max": point((xmax, ymax))}


def _length(value):
    return {"kind": "length", "value": _finite(value, "length"), "unit": UNIT}


def canonical_corners(vertices):
    """G12: counter-clockwise, starting at the corner with the smallest rounded (y, x)."""
    pts = [(float(x), float(y)) for x, y in vertices]
    area = sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1]
               for i in range(len(pts)))
    if area < 0:
        pts.reverse()
    start = min(range(len(pts)), key=lambda i: _key(*pts[i]))
    return pts[start:] + pts[:start]


def _centroid(pts):
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def _number(items, centroid):
    """Stable order by the rounded (centroid y, centroid x) key."""
    return sorted(items, key=lambda item: _key(*centroid(item)))


def frame_ids(frame_entities):
    """Every live frame numbered by G12: [(id, polygon, handle)] in id order."""
    ordered = _number(frame_entities, lambda e: _centroid(e["vertices"]))
    return [(f"frame-{n}", [tuple(v) for v in e["vertices"]], e.get("handle"))
            for n, e in enumerate(ordered, 1)]


def frame_rows(new_frames):
    rows = []
    for n, ent in enumerate(_number(new_frames, lambda e: _centroid(e["vertices"])), 1):
        corners = canonical_corners(ent["vertices"])
        rows.append({"id": f"frame-{n}", "type": "frame", "quantity": 1, "unit": "each",
                     "vertices": [point(p) for p in corners],
                     "row_index": ent["row"], "col_index": ent["col"],
                     "color_index": ent["color"]["index"]})
    return rows


def _bbox(pts):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return [min(xs), min(ys), max(xs), max(ys)]


def marker_rows(markers):
    """markers: [{"role", "bbox": [xmin, ymin, xmax, ymax]}] added by the step."""
    def centre(m):
        b = m["bbox"]
        return ((b[0] + b[2]) * 0.5, (b[1] + b[3]) * 0.5)
    return [{"id": f"marker-{n}", "type": "marker", "quantity": 1, "unit": "each",
             "role": m["role"], "bbox": _min_max(*m["bbox"])}
            for n, m in enumerate(_number(markers, centre), 1)]


def pile_length(ent):
    """A pile's height, z_top - z_bottom, rounded to 1e-6 so float noise in the
    subtraction never splits one length into two sets (G17)."""
    c = ent["cylinder"]
    return round(_finite(c["top_z"], "pile top") - _finite(c["bottom_z"], "pile bottom"), 6)


def pile_set_rows(pile_entities, live_frames):
    """G16 as amended by G17: one pile-set per (frame, distinct length), in its
    frame's id order (null last), then ascending length.

    A pile belongs to the frame whose polygon holds its centre. The frame it was
    placed from is tried first (one polygon test, so the pass is linear in piles);
    only a pile with no source frame, or one its source does not hold, walks the
    frames in id order."""
    ids = frame_ids(live_frames)
    by_handle = {handle: (fid, poly) for fid, poly, handle in ids if handle}
    groups = {}
    for ent in pile_entities:
        cyl = ent["cylinder"]
        x, y = cyl["center_x"], cyl["center_y"]
        source = by_handle.get((ent.get("pile") or {}).get("source_tracker"))
        if source is not None and frames.point_in_polygon(x, y, source[1]):
            owner = source[0]
        else:
            owner = next((fid for fid, poly, _ in ids if frames.point_in_polygon(x, y, poly)), None)
        groups.setdefault((owner, pile_length(ent)), []).append(ent)
    order = {fid: i for i, (fid, _, _) in enumerate(ids)}
    rows = []
    for n, (owner, length) in enumerate(sorted(groups, key=lambda g: (g[0] is None, order.get(g[0], 0), g[1])), 1):
        members = sorted(groups[(owner, length)],
                         key=lambda e: _key(e["cylinder"]["center_x"], e["cylinder"]["center_y"]))
        diameters = {e["pile"]["diameter_m"] for e in members}
        if len(diameters) != 1:
            raise EvidenceError(f"piles of {owner or 'no frame'} carry {len(diameters)} diameters; a set has one")
        rows.append({"id": f"pile-set-{n}", "type": "pile-set", "quantity": 1, "unit": "each",
                     "frame": owner, "diameter": _length(diameters.pop()), "length": _length(length),
                     "count": len(members),
                     "piles": [point((e["cylinder"]["center_x"], e["cylinder"]["center_y"],
                                      e["cylinder"]["bottom_z"])) for e in members]})
    return rows


def _extent(grid):
    return _min_max(grid["x_min"], grid["y_min"], grid["x_max"], grid["y_max"])


def terrain_grid_row(grid):
    return {"id": "terrain-grid-1", "type": "terrain-grid", "quantity": 1, "unit": "each",
            "rows": grid["rows"], "cols": grid["cols"], "extent": _extent(grid),
            "elevations": [_length(z) for z in grid["elevations"]]}


def terrain_mesh_row(grid, mesh):
    return {"id": "terrain-mesh-1", "type": "terrain-mesh", "quantity": 1, "unit": "each",
            "rows": grid["rows"], "cols": grid["cols"], "extent": _extent(grid),
            "cell_colors": [face["color"]["true_color"] for face in mesh]}


def off_grid_face_row(n, corners, color_index):
    """G16/G17: a mesh face whose corners are not its grid cell's, four [x, y, z] points.
    The shape both sides share; Studio draws its mesh from its own grid and never emits one."""
    if not isinstance(corners, (list, tuple)) or len(corners) != 4:
        raise EvidenceError("an off-grid face is four [x, y, z] corners")
    vertices = [point(c) for c in corners]
    if any(len(v["value"]) != 3 for v in vertices):
        raise EvidenceError("an off-grid face corner is one [x, y, z] point")
    return {"id": f"off-grid-face-{n}", "type": "off-grid-face", "quantity": 1, "unit": "each",
            "vertices": vertices, "color_index": color_index}


def removed_rows(counts):
    """counts: {row type: entities erased}; zero counts are not rows."""
    return [{"id": f"removed-{n}", "type": "removed", "quantity": 1, "unit": "each", "of": of, "count": c}
            for n, (of, c) in enumerate(sorted((of, c) for of, c in counts.items() if c > 0), 1)]


def _row_order(row):
    prefix, _, n = row["id"].rpartition("-")
    return (row["type"], int(n))


# ------------------------------------------------------------------- steps --

def new_state():
    """Studio's committed ground state: the grid, the drawn mesh, frames, piles, markers."""
    return {"grid": None, "mesh_faces": 0, "frames": [], "piles": [], "collision_layer": [],
            "slope_markers": [], "next_handle": 1}


def _terrain_z(state):
    interp = terrain.terrain_interpolator(state["grid"], terrain.meters_per_unit_for_keyword(UNITS_KEYWORD))
    return None if interp is None else interp.interpolate_z


def _topo_entities(count):
    return [{"layer": terrain.TOPO_LAYER}] * count


def step_terrain_import(state, ctx):
    faces = [{"layer": TERRAIN_LAYER, "vertices": corners} for corners in ctx["intake"]["terrain_faces"]]
    result = terrain.topo_from_3d_faces(faces, terrain.meters_per_unit_for_keyword(UNITS_KEYWORD),
                                        GRID_CELLS_LONG_AXIS, _topo_entities(state["mesh_faces"]))
    if not result["succeeded"]:
        return []
    state["grid"] = result["grid"]
    state["mesh_faces"] = len(result["mesh"])
    rows = [terrain_grid_row(result["grid"])]
    if result["mesh"]:
        rows.append(terrain_mesh_row(result["grid"], result["mesh"]))
    return rows + removed_rows({"terrain-mesh": result["old_mesh_faces_cleared"]})


def step_terrain_mesh(state, ctx):
    result = terrain.terrain_mesh_rerender(state["grid"], terrain.meters_per_unit_for_keyword(UNITS_KEYWORD),
                                           _topo_entities(state["mesh_faces"]))
    if not result["succeeded"]:
        return []
    state["mesh_faces"] = len(result["mesh"])
    rows = [terrain_mesh_row(state["grid"], result["mesh"])] if result["mesh"] else []
    return rows + removed_rows({"terrain-mesh": len(result["cleared_entity_indices"])})


def step_frame_generate(state, ctx):
    result = frames.generate_frames([ctx["intake"]["boundary"]], ctx["preset"], ctx["mpu"],
                                    terrain_z_m=_terrain_z(state))
    added = result["entities"]
    for ent in added:
        ent["handle"] = f"{state['next_handle']:X}"
        state["next_handle"] += 1
    state["frames"].extend(added)
    return frame_rows(added)


def step_collision(state, ctx):
    result = frames.run_collision(state["frames"])
    markers = [{"role": "collision", "bbox": _bbox(m["vertices"])} for m in result["markers"]]
    state["collision_layer"].extend(markers)
    return marker_rows(markers)


def step_piling(state, ctx):
    result = frames.run_piling(state["frames"] + state["piles"], ctx["preset"], ctx["pile_store"], ctx["mpu"],
                               terrain_z_m=_terrain_z(state))
    if result["status"] == "no_sources":
        return []
    erased = len(result["erase_indexes"])
    state["piles"] = list(result["entities"])
    return pile_set_rows(result["entities"], state["frames"]) + removed_rows({"pile-set": erased})


def step_range(state, ctx):
    result = frames.run_pile_range_check(state["piles"], ctx["preset"], ctx["mpu"])
    markers = [{"role": "pile-range", "bbox": _bbox(m["vertices"])} for m in result["markers"]]
    state["collision_layer"].extend(markers)
    return marker_rows(markers)


def _slope_entities(state):
    ents = [{"kind": "LWPOLYLINE", "layer": f["layer"], "vertices": f["vertices"],
             "frame_cell": {"row": f["row"], "col": f["col"]}} for f in state["frames"]]
    return ents + [{"kind": "LWPOLYLINE", "layer": terrain.VIOLATION_LAYER} for _ in state["slope_markers"]]


def step_slope_clear(state, ctx):
    cleared = terrain.clear_tracker_slope_violations(_slope_entities(state))["cleared_entity_indices"]
    state["slope_markers"] = []
    return removed_rows({"marker": len(cleared)})


def step_slope(state, ctx):
    result = terrain.tracker_slope_violations(state["grid"], _slope_entities(state), slope_limits(ctx["preset"]),
                                              ctx["mpu"])
    if not result["succeeded"]:
        return []
    markers = [{"role": "slope", "bbox": _bbox(o["vertices"])} for o in result["overlays"]]
    state["slope_markers"] = markers
    return marker_rows(markers) + removed_rows({"marker": len(result["cleared_entity_indices"])})


# Each operation's Studio step: it updates the state and returns the step's rows.
STEPS = {
    "topo-import": step_terrain_import,
    "mesh": step_terrain_mesh,
    "generate": step_frame_generate,
    "collision": step_collision,
    "piling": step_piling,
    "range": step_range,
    "slope": step_slope,
    "slope-clear": step_slope_clear,
}


# ---------------------------------------------------------------- documents --

def parameters_for(intake):
    """G11: the prompts' answers and the active preset name."""
    return {"units_keyword": UNITS_KEYWORD, "grid_cells_long_axis": GRID_CELLS_LONG_AXIS,
            "active_preset": intake["active_preset"]["Name"]}


def build_document(intake, kind, step_id, capability, operation, rows, revision):
    """One G12 `exports` evidence document, validated under the comparator's bounds."""
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise EvidenceError("revision must be a 40-character lowercase git commit")
    rows = sorted(rows, key=_row_order)
    parameters = parameters_for(intake)
    # G8: ids and the frame a pile set points at are both entity references.
    def _ref(row):
        out = dict(row, id={"entity_id": row["id"]})
        if row.get("frame"):
            out["frame"] = {"entity_id": row["frame"]}
        return out
    after = {"rows": [_ref(row) for row in rows],
             "source_revision": step_id, "format": FORMAT}
    try:
        fixture = compare.semantic_hash(intake)
        hashes = (compare.semantic_hash({"fixture_sha256": fixture, "parameters": parameters}),
                  compare.semantic_hash(after))
    except compare.InputError as exc:
        raise EvidenceError(f"step {step_id} evidence refused by the comparator: {exc}") from None
    doc = {
        "fixture_sha256": fixture,
        "input_sha256": hashes[0],
        "output_sha256": hashes[1],
        "revision": revision,
        "versions": {"schema": compare.SCHEMA, "producer": "studio", "capability": CAPABILITY_VERSION,
                     "engine": "server-builtin",  # the gate's engine vocabulary; the modules are solar_ground_frames and solar_ground_terrain
                     "catalog": "none", "solver": "none"},
        "parameters": parameters,
        "units": intake["units"],
        "frame": json.loads(json.dumps(FRAME)),
        # G8: every id the evidence references: its rows and the frames its pile sets point at.
        "entity_mapping": {ref: ref for ref in sorted({row["id"] for row in rows}
                                                     | {row["frame"] for row in rows if row.get("frame")})},
        "before": {"recorded": False},
        "after": after,
        "changes": {"created": [], "modified": [], "deleted": []},
        "warnings": [],
        "rejected_inputs": [],
        "provenance": {"side": "studio", "fixture_kind": kind, "step": step_id, "capability": capability,
                       "operation": operation},
        "elapsed_ms": 0,
        "execution_mode": "live",
        "state": "committed",
        "survived_reopen": True,
        "synthetic_fields": ["before/recorded", "changes/unrecorded"],
        "fallback_fields": [],
        "synthetic_flagged": True,
    }
    try:
        compare.validate_evidence(doc, "exports")
        compare._normalize(doc["after"], doc["entity_mapping"])
    except compare.InputError as exc:
        raise EvidenceError(f"step {step_id} evidence refused by the comparator: {exc}") from None
    if len(_serialize(doc).encode("utf-8")) > compare.MAX_BYTES:
        raise EvidenceError(f"step {step_id} evidence exceeds {compare.MAX_BYTES} bytes")
    return doc


def run_scenario(intake, kind, revision, only=None):
    """Every G14 step in order from the intake (G13); returns {step id: document},
    all steps, or only `only` (its predecessors still run: they are its state)."""
    validate_intake(intake, kind)
    steps = SCENARIOS[kind]
    if only is not None and only not in {step for step, _, _ in steps}:
        raise EvidenceError(f"step {only!r} is not in the {kind} scenario list")
    preset, pile_store = load_stores(intake)
    ctx = {"intake": intake, "preset": preset, "pile_store": pile_store, "mpu": METERS_PER_UNIT[intake["units"]]}
    state = new_state()
    out = {}
    for step_id, capability, operation in steps:
        try:
            rows = STEPS[operation](state, ctx)
        except (frames.GroundFramesError, terrain.TerrainInputError) as exc:
            raise EvidenceError(f"step {step_id} ({operation}) refused: {exc}") from None
        if only is None or step_id == only:
            out[step_id] = build_document(intake, kind, step_id, capability, operation, rows, revision)
        if step_id == only:
            break
    return out


# --------------------------------------------------------------------- CLI --

def _serialize(doc):
    """Compact canonical JSON: a terrain grid's document stays inside the 2 MB file
    bound that the comparator's reader enforces, which indentation would breach."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n"


def fixture_revision(path):
    """Rule 2: the last commit touching the intake, in the repository that holds it.
    An untracked file or no git is an error, never a default."""
    path = Path(path).resolve()

    def git(*args):
        # Run from the intake's own folder with its bare name, so no absolute path has
        # to match git's spelling of the work tree.
        return subprocess.run(["git", *args], cwd=path.parent, capture_output=True, text=True,
                              timeout=GIT_TIMEOUT, check=True).stdout.strip()
    try:
        git("rev-parse", "--show-toplevel")
        git("ls-files", "--error-unmatch", "--", path.name)
        rev = git("log", "-1", "--format=%H", "--", path.name)
    except (OSError, subprocess.SubprocessError) as exc:
        raise EvidenceError(f"intake revision unavailable: {exc}") from None
    if not re.fullmatch(r"[0-9a-f]{40}", rev):
        raise EvidenceError("intake has no committed revision")
    return rev


def main(argv=None):
    parser = argparse.ArgumentParser(description="Studio ground evidence per G14 step from a G11 intake.")
    parser.add_argument("--intake", type=Path, required=True)
    parser.add_argument("--kind", choices=KINDS, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--step")
    args = parser.parse_args(argv)
    try:
        intake = compare.load_evidence(args.intake)
        docs = run_scenario(intake, args.kind, fixture_revision(args.intake), args.step)
        args.out_dir.mkdir(parents=True, exist_ok=True)
        for step_id, doc in docs.items():
            target = args.out_dir / f"{step_id}.json"
            target.write_text(_serialize(doc), encoding="utf-8")
            if compare.load_evidence(target) != json.loads(_serialize(doc)):
                raise EvidenceError(f"{target} did not read back as written")
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        print(f"solar-ground-studio-evidence: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
