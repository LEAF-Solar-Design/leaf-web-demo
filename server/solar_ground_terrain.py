"""Studio port of the plugin's ground terrain engines: LEAFTOPOFROM3DFACES,
LEAFTERRAINMESH, LEAFTRACKERSLOPEVIOLATIONS and LEAFCLEARTRACKERSLOPEVIOLATIONS.

Literal ports of Branch2025 (read 2026-09-23 at C:/tmp/solar-parity/wt-b25-s17):

  Terrain/TopoFrom3dFacesCommand.cs   faces -> terrain points, audit, persist flow
  Terrain/LandXmlImporter.cs          ResampleToGrid (IDW p=2) that builds the grid
  Terrain/TerrainCommand.cs           PersistElevationGrid (what the grid holds),
                                      DrawGridMesh, ClearTerrainMesh
  Terrain/TerrainMeshBuilder.cs       ClassifySlope buckets
  Terrain/TerrainColors.cs            bucket colours
  Terrain/TerrainUnitsPrompt.cs       Meters/Feet keyword, default Meters
  Pvcase/LeafTerrainMeshCommand.cs    TryReadGrid, the re-render
  Terrain/TrackerCommand.cs           TryReadTerrainInterpolator
  Terrain/TerrainGridInterpolator.cs  bilinear InterpolateZ over the affine frame
  Terrain/TerrainProfileCalculator.cs SampleLine, SamplePolyline
  Terrain/TrackerSlopeValidator.cs    row model, validation, FromPackedFrame
  Terrain/TrackerSlopeDrawing.cs      BuildReport, ReadRowsForSlope, overlays, clear
  Terrain/TrackerRowReader.cs         PolylineToTrackerRow
  LeafSolarDesign.Core/FramePreset.cs slope limit defaults
  LeafCivilCommand.cs:32 and :81      the two tracker slope commands

Pure functions over plain data (tuples, lists, dicts). No AutoCAD, no I/O, no
network. The engines take and return NEUTRAL structures only: the terrain grid as
a dict (see neutral_grid), a frame as its vertices with its row and column, a
tracker row as its vertices with its row index and module slots. How the plugin
encodes those in a drawing is not part of this module; the adapter that reads
that encoding lives with the plugin, outside this public repository. Floating point work is ordered exactly as the C# orders it (IEEE doubles
on both sides, no compensated sums, banker's rounding where C# uses Math.Round),
so a grid, a mesh and a report reproduce the plugin's numbers bit for bit.

Deterministic ordering:
  * terrain points keep first-seen order of their de-duplicated (x, y) key, as the
    plugin's Dictionary does (no removals, so insertion order);
  * grids are row-major, row index with drawing Y, column index with drawing X;
  * mesh faces are emitted row-major (row outer, column inner), as DrawGridMesh;
  * tracker rows keep modelspace order; overlays keep first-insertion row order.

Every input is bounded and every malformed input fails closed with
TerrainInputError (a ValueError); a bound breach raises TerrainBoundsError.
"""
from __future__ import annotations

import math
from bisect import bisect_right
from numbers import Real

# ---------------------------------------------------------------------------
#  Constants (each cites the line that defines it)
# ---------------------------------------------------------------------------

PREFERRED_TERRAIN_LAYER = "LEAF-TERRAIN"          # TopoFrom3dFacesCommand.cs:19
ALL_FACES_SOURCE_LABEL = "all modelspace 3DFACE layers"  # TopoFrom3dFacesCommand.cs:28
DEFAULT_TARGET_CELLS = 150                        # TopoFrom3dFacesCommand.cs:17
MAX_TARGET_CELLS = 300                            # TopoFrom3dFacesCommand.cs:18
MIN_TARGET_CELLS = 2                              # TopoFrom3dFacesCommand.cs:80, :207
VERTEX_KEY_SCALE = 1000000.0                      # TopoFrom3dFacesCommand.cs:437

METERS_KEYWORD = "Meters"                         # TerrainUnitsPrompt.cs:10
FEET_KEYWORD = "Feet"                             # TerrainUnitsPrompt.cs:11
FEET_METERS_PER_UNIT = 0.3048                     # TerrainUnitsPrompt.cs:12

TOPO_LAYER = "LEAF-TOPO"                          # TerrainCommand.cs:34
SLOPE_GREEN_THRESHOLD = 5.0                       # TerrainMeshBuilder.cs:36
SLOPE_YELLOW_THRESHOLD = 15.0                     # TerrainMeshBuilder.cs:39
BUCKET_RGB = {                                    # TerrainColors.cs:13-15
    "Green": (0, 200, 0),
    "Yellow": (255, 200, 0),
    "Red": (220, 0, 0),
}

TRACKER_LAYER = "LEAF-TRACKERS"                   # LayerNames.cs:36, SatCommand.cs:304
VIOLATION_LAYER = "LEAF-SLOPE-VIOLATIONS"         # TrackerSlopeDrawing.cs:12
VIOLATION_ENTITY_COLOR_INDEX = 256                # ByLayer, TrackerSlopeDrawing.cs:171
VIOLATION_CONSTANT_WIDTH = 0.25                   # TrackerSlopeDrawing.cs:175

# FramePreset.cs:35-36, :66-67, :71-74
DEFAULT_PRESET_LIMITS = {
    "MaxNsSlopePct": 8.5,
    "MaxRowToRowEwSlopePct": 10.0,
    "MaxAxialSlopePct": 8.5,
    "MaxCrossAxisSlopePct": 10.0,
    "MaxRowToRowSlopeDeg": 4.0,
    "MaxSlopePercent": 15.0,
}

# TrackerSlopeValidator.cs:143-146
_EPSILON = 1e-9
DEFAULT_AXIAL_SEGMENTS = 16
MAX_AXIAL_SEGMENTS = 64
CROSS_AXIS_SAMPLE_STATIONS = 5

# Studio-side bounds. The plugin has none; these refuse inputs that would pin a
# worker, with a named error, instead of hanging it.
MAX_FACES = 500_000
MAX_GRID_NODES = 4_000_000
MAX_IDW_OPERATIONS = 100_000_000   # rows * cols * unique vertices, the IDW cost
MAX_TRACKER_ROWS = 20_000
MAX_ENTITIES = 1_000_000
MAX_PROFILE_SAMPLES = 1_000_000


class TerrainInputError(ValueError):
    """Malformed input: the engine refuses rather than guess."""


class TerrainBoundsError(TerrainInputError):
    """Input exceeds a Studio bound (face count, grid size, IDW work, rows)."""


# ---------------------------------------------------------------------------
#  Validation helpers (fail closed)
# ---------------------------------------------------------------------------

def _num(value, what):
    """A real number (bool refused). Non-finite values pass; callers decide."""
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TerrainInputError(f"{what} must be a number, got {type(value).__name__}")
    return float(value)


def _finite(value, what):
    v = _num(value, what)
    if not math.isfinite(v):
        raise TerrainInputError(f"{what} must be finite, got {v!r}")
    return v


def _int(value, what):
    if isinstance(value, bool) or not isinstance(value, int):
        raise TerrainInputError(f"{what} must be an integer, got {type(value).__name__}")
    return value


def _point2(value, what):
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise TerrainInputError(f"{what} must be an (x, y) pair")
    return (_finite(value[0], f"{what}.x"), _finite(value[1], f"{what}.y"))


def _meters_per_unit(value):
    """TopoFrom3dFacesCommand.cs:204-205 and :296-297: must be > 0."""
    v = _num(value, "meters_per_unit")
    if not (v > 0.0) or not math.isfinite(v):
        raise TerrainInputError("metersPerUnit must be > 0")
    return v


def _ieee_div(a, b):
    """C# double division: x/0 is +-Infinity, 0/0 is NaN (Python would raise)."""
    if b == 0.0:
        if a == 0.0 or math.isnan(a):
            return math.nan
        return math.copysign(math.inf, a) * math.copysign(1.0, b)
    return a / b


def _cs_max(a, b):
    """System.Math.Max(double, double): NaN in either argument yields NaN."""
    if math.isnan(a) or math.isnan(b):
        return math.nan
    return a if a >= b else b


def _layer_equals(a, b):
    """string.Equals(..., StringComparison.OrdinalIgnoreCase)."""
    return isinstance(a, str) and a.upper() == b.upper()


# ---------------------------------------------------------------------------
#  Units (TerrainUnitsPrompt.cs)
# ---------------------------------------------------------------------------

def meters_per_unit_for_keyword(keyword=None, default_is_feet=False):
    """TerrainUnitsPrompt.TryPromptMetersPerDrawingUnit, :40-64.

    keyword None is the empty-Enter answer, which takes the default. Both
    LEAFTOPOFROM3DFACES (:66, defaultIsFeet: false) and LEAFTERRAINMESH
    (LeafTerrainMeshCommand.cs:95) default to Meters.
    """
    if keyword is None:
        is_feet = bool(default_is_feet)
    elif isinstance(keyword, str) and keyword.upper() in (METERS_KEYWORD.upper(), FEET_KEYWORD.upper()):
        is_feet = keyword.upper() == FEET_KEYWORD.upper()
    else:
        raise TerrainInputError(f"units keyword must be {METERS_KEYWORD} or {FEET_KEYWORD}")
    return FEET_METERS_PER_UNIT if is_feet else 1.0


# ---------------------------------------------------------------------------
#  Faces -> terrain points (TopoFrom3dFacesCommand.cs)
# ---------------------------------------------------------------------------

def _validate_faces(faces):
    """A face is {"layer": str, "vertices": [(x, y, z) * 1..4]}; ReadModelSpace3dFaces
    (:396-414) always yields the 3DFACE's four corners (a triangle repeats its third)."""
    if not isinstance(faces, (list, tuple)):
        raise TerrainInputError("faces must be a list")
    if len(faces) > MAX_FACES:
        raise TerrainBoundsError(f"{len(faces)} faces exceed the bound of {MAX_FACES}")
    out = []
    for i, face in enumerate(faces):
        if not isinstance(face, dict):
            raise TerrainInputError(f"faces[{i}] must be a dict")
        layer = face.get("layer")
        if not isinstance(layer, str):
            raise TerrainInputError(f"faces[{i}].layer must be a string")
        verts = face.get("vertices")
        if not isinstance(verts, (list, tuple)) or not 1 <= len(verts) <= 4:
            raise TerrainInputError(f"faces[{i}].vertices must hold 1 to 4 points")
        clean = []
        for j, v in enumerate(verts):
            if not isinstance(v, (list, tuple)) or len(v) != 3:
                raise TerrainInputError(f"faces[{i}].vertices[{j}] must be (x, y, z)")
            clean.append(tuple(_num(c, f"faces[{i}].vertices[{j}]") for c in v))
        out.append({"layer": layer, "vertices": clean})
    return out


def audit_faces(faces):
    """TopoFrom3dFacesCommand.Audit, :179-189, with the SourceFaces/SourceLabel
    rule of :25-28."""
    faces = _validate_faces(faces)
    preferred = sum(1 for f in faces if _layer_equals(f["layer"], PREFERRED_TERRAIN_LAYER))
    return {
        "all_faces": len(faces),
        "preferred_terrain_faces": preferred,
        "source_faces": preferred if preferred > 0 else len(faces),
        "source_label": PREFERRED_TERRAIN_LAYER if preferred > 0 else ALL_FACES_SOURCE_LABEL,
    }


def select_source_faces(faces):
    """PersistFrom3dFaces, :216-223: faces on LEAF-TERRAIN (case-insensitive) win;
    with none there, every modelspace 3DFACE is the source. Order kept."""
    faces = _validate_faces(faces)
    source = [f for f in faces if _layer_equals(f["layer"], PREFERRED_TERRAIN_LAYER)]
    if not source:
        return faces, ALL_FACES_SOURCE_LABEL
    return source, PREFERRED_TERRAIN_LAYER


def _vertex_key(x, y):
    """VertexKey, :435-441: (long)Math.Round(v * 1e6), banker's rounding."""
    return (int(round(x * VERTEX_KEY_SCALE)), int(round(y * VERTEX_KEY_SCALE)))


def build_terrain_points_from_faces(face_vertices, meters_per_unit):
    """BuildTerrainPointsFromFaces, :290-361.

    face_vertices: iterable of vertex lists (a None face is skipped, :321). Up to
    four vertices per face are read (:322); a vertex with a non-finite coordinate
    is skipped (:326-332). Vertices sharing an (x, y) key to 1e-6 drawing units
    merge: X/Y from the first seen, Z the mean of every contribution converted to
    metres (Z * metersPerUnit, :340). A triangle's repeated corner counts twice,
    exactly as the plugin counts it. X and Y stay in drawing units.
    Returns [(x, y, z_m)] in first-seen key order.
    """
    mpu = _meters_per_unit(meters_per_unit)
    if face_vertices is None:
        raise TerrainInputError("faces must not be None")   # :295
    faces = list(face_vertices)
    if len(faces) > MAX_FACES:
        raise TerrainBoundsError(f"{len(faces)} faces exceed the bound of {MAX_FACES}")
    accum = {}
    for fi, face in enumerate(faces):
        if face is None:
            continue
        if not isinstance(face, (list, tuple)):
            raise TerrainInputError(f"face {fi} must be a vertex list")
        for vi in range(min(len(face), 4)):
            v = face[vi]
            if v is None:
                continue
            if not isinstance(v, (list, tuple)) or len(v) != 3:
                raise TerrainInputError(f"face {fi} vertex {vi} must be (x, y, z)")
            x = _num(v[0], "vertex.x")
            y = _num(v[1], "vertex.y")
            z = _num(v[2], "vertex.z")
            if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
                continue
            key = _vertex_key(x, y)
            a = accum.get(key)
            if a is None:
                a = [x, y, 0.0, 0]
                accum[key] = a
            a[2] += z * mpu
            a[3] += 1
    return [(a[0], a[1], a[2] / a[3]) for a in accum.values() if a[3] > 0]


# ---------------------------------------------------------------------------
#  Resample to the LEAFTOPO grid (LandXmlImporter.ResampleToGrid, :144-245)
# ---------------------------------------------------------------------------

def grid_dimensions(x_span, y_span, target_cells):
    """ResampleToGrid step 2, :177-191. Math.Round is banker's; so is round()."""
    aspect = x_span / y_span
    if aspect >= 1.0:
        cols = max(2, target_cells)
        rows = max(2, int(round(target_cells / aspect)))
    else:
        rows = max(2, target_cells)
        cols = max(2, int(round(target_cells * aspect)))
    return rows, cols


def resample_to_grid(points, target_cells, max_idw_operations=MAX_IDW_OPERATIONS):
    """IDW (p=2) resample of [(x, y, z)] onto a rows x cols grid, :144-245.

    Returns {"elevations", "rows", "cols", "x_min", "x_max", "y_min", "y_max",
    "source_points"}. Refuses fewer than 3 points and targetCells < 2 (:148-150).
    The all-coincident guard (:167-172) resets BOTH maxima to min + 1 when either
    span is empty, as the plugin does. A query within sqrt(1e-12) of a source point
    takes that point's elevation (:221-226). Summation runs in point order.
    """
    if points is None:
        raise TerrainInputError("points must not be None")
    pts = []
    for i, p in enumerate(points):
        if not isinstance(p, (list, tuple)) or len(p) != 3:
            raise TerrainInputError(f"points[{i}] must be (x, y, z)")
        pts.append((_finite(p[0], f"points[{i}].x"), _finite(p[1], f"points[{i}].y"),
                    _finite(p[2], f"points[{i}].z")))
    if len(pts) < 3:
        raise TerrainInputError("At least 3 points are required to resample a grid.")
    target_cells = _int(target_cells, "target_cells")
    if target_cells < 2:
        raise TerrainInputError("targetCells must be >= 2.")

    x_min = y_min = 1.7976931348623157e308
    x_max = y_max = -1.7976931348623157e308
    for x, y, _ in pts:
        if x < x_min: x_min = x
        if x > x_max: x_max = x
        if y < y_min: y_min = y
        if y > y_max: y_max = y
    if x_min >= x_max or y_min >= y_max:
        eps = 1.0
        x_max = x_min + eps
        y_max = y_min + eps

    x_span = x_max - x_min
    y_span = y_max - y_min
    rows, cols = grid_dimensions(x_span, y_span, target_cells)
    if rows * cols > MAX_GRID_NODES:
        raise TerrainBoundsError(f"grid {rows}x{cols} exceeds {MAX_GRID_NODES} nodes")
    work = rows * cols * len(pts)
    if work > max_idw_operations:
        raise TerrainBoundsError(
            f"IDW work {rows}x{cols}x{len(pts)} = {work} exceeds the bound of {max_idw_operations}")

    col_step = x_span / (cols - 1) if cols > 1 else x_span
    row_step = y_span / (rows - 1) if rows > 1 else y_span

    xs = [p[0] for p in pts]
    zs = [p[2] for p in pts]
    elevations = [0.0] * (rows * cols)
    for r in range(rows):
        gy = y_min + r * row_step
        dy2s = [(p[1] - gy) * (p[1] - gy) for p in pts]   # dy * dy, same op per point
        base = r * cols
        for c in range(cols):
            gx = x_min + c * col_step
            num = 0.0
            den = 0.0
            exact = None
            for px, pz, dy2 in zip(xs, zs, dy2s):
                dx = px - gx
                d2 = dx * dx + dy2
                if d2 < 1e-12:
                    exact = pz
                    break
                w = 1.0 / d2
                num += w * pz
                den += w
            elevations[base + c] = exact if exact is not None else num / den
    return {
        "elevations": elevations, "rows": rows, "cols": cols,
        "x_min": x_min, "x_max": x_max, "y_min": y_min, "y_max": y_max,
        "source_points": len(pts),
    }


# ---------------------------------------------------------------------------
#  The terrain grid (TerrainCommand.PersistElevationGrid, :1548-1570, :1681-1741)
# ---------------------------------------------------------------------------

GRID_BOUNDS = ("x_min", "x_max", "y_min", "y_max")


def neutral_grid(grid):
    """Validate the neutral terrain grid and return a clean copy (fails closed).

    {"elevations": row-major metres, rows * cols of them, "rows", "cols", "x_min",
    "x_max", "y_min", "y_max" (drawing units)} plus an optional "frame" of six
    numbers (origin x, origin y, x axis x, x axis y, y axis x, y axis y). These are
    the values PersistElevationGrid commits (:1708-1725; LEAFTOPOFROM3DFACES commits
    no frame, AddAffineFrame :1814-1831 adds one on other import paths) and the
    values TryReadGrid and TryReadTerrainInterpolator read back. Bounds may be NaN,
    as the plugin's are when an importer has no Y extent (:1716).
    """
    if not isinstance(grid, dict):
        raise TerrainInputError("grid must be a dict")
    rows = _int(grid.get("rows"), "grid.rows")
    cols = _int(grid.get("cols"), "grid.cols")
    if rows < 0 or cols < 0 or rows * cols > MAX_GRID_NODES:
        raise TerrainBoundsError(f"grid {rows}x{cols} is out of bounds")
    elevations = grid.get("elevations")
    if not isinstance(elevations, (list, tuple)) or len(elevations) != rows * cols:
        raise TerrainInputError(f"grid.elevations must hold rows * cols = {rows * cols} values")
    clean = {"elevations": [_num(e, "grid.elevation") for e in elevations],
             "rows": rows, "cols": cols}
    for key in GRID_BOUNDS:
        clean[key] = _num(grid.get(key), "grid." + key)
    frame = grid.get("frame")
    if frame is not None:
        if not isinstance(frame, (list, tuple)) or len(frame) != 6:
            raise TerrainInputError("grid.frame must be six numbers (origin, x axis, y axis)")
        clean["frame"] = [_num(v, "grid.frame") for v in frame]
    return clean


def mesh_grid(grid):
    """LeafTerrainMeshCommand.TryReadGrid, :102-137: None when there is no grid or
    it is under 2x2 (the plugin returns false); a malformed grid raises."""
    if grid is None:
        return None
    clean = neutral_grid(grid)
    return clean if clean["rows"] >= 2 and clean["cols"] >= 2 else None


def terrain_interpolator(grid, meters_per_unit):
    """TrackerCommand.TryReadTerrainInterpolator, :275-333. None where the plugin
    returns null: no grid, or any malformed value (its catch-all, :332). The affine
    frame is used when the grid carries one (:312-326)."""
    if grid is None:
        return None
    try:
        clean = neutral_grid(grid)
        mpu = _num(meters_per_unit, "meters_per_unit")
        bounds = [clean[k] for k in GRID_BOUNDS]
        return TerrainGridInterpolator(clean["elevations"], clean["rows"], clean["cols"], *bounds,
                                       mpu, *(clean.get("frame") or ()))
    except TerrainInputError:
        return None


# ---------------------------------------------------------------------------
#  Slope mesh (TerrainCommand.DrawGridMesh, :1572-1632)
# ---------------------------------------------------------------------------

def classify_slope(slope_percent):
    """TerrainMeshBuilder.ClassifySlope, :51-56: < 5 Green, <= 15 Yellow, else Red
    (NaN falls through both comparisons to Red, as in C#)."""
    if slope_percent < SLOPE_GREEN_THRESHOLD:
        return "Green"
    if slope_percent <= SLOPE_YELLOW_THRESHOLD:
        return "Yellow"
    return "Red"


def bucket_color(bucket):
    """TerrainColors.BucketColor, :10-16: true colour (Color.FromRgb), no ACI index.
    The true-colour integer is r<<16 | g<<8 | b."""
    rgb = BUCKET_RGB.get(bucket, BUCKET_RGB["Red"])
    return {"method": "ByColor", "rgb": rgb,
            "true_color": (rgb[0] << 16) | (rgb[1] << 8) | rgb[2]}


def _validate_grid(elevations, rows, cols):
    rows = _int(rows, "rows")
    cols = _int(cols, "cols")
    if rows < 0 or cols < 0 or rows * cols > MAX_GRID_NODES:
        raise TerrainBoundsError(f"grid {rows}x{cols} is out of bounds")
    if not isinstance(elevations, (list, tuple)):
        raise TerrainInputError("elevations must be a list")
    if len(elevations) < rows * cols:
        raise TerrainInputError(
            f"elevations hold {len(elevations)} values, a {rows}x{cols} grid needs {rows * cols}")
    return [_num(e, "elevation") for e in elevations], rows, cols


def draw_grid_mesh(elevations, rows, cols, x_min, x_max, y_min, y_max, meters_per_unit):
    """The 3DFACEs DrawGridMesh appends, row-major, one per grid cell.

    Each face: vertices (x0,y0,z00), (x1,y0,z10), (x1,y1,z11), (x0,y1,z01) with Z
    back in drawing units (elev / metersPerUnit, :1602-1605); layer LEAF-TOPO;
    all four edges visible (:1620); colour from the slope bucket of
    max(slopeX, slopeY), each the absolute rise over one cell step in percent,
    measured from the cell's (row, col) corner (:1607-1613). Fewer than 2 rows or
    columns draws nothing (:1581). Zero-width steps divide as C# doubles do.
    """
    faces = []
    if _int(rows, "rows") < 2 or _int(cols, "cols") < 2:
        return faces
    elevations, rows, cols = _validate_grid(elevations, rows, cols)
    mpu = _num(meters_per_unit, "meters_per_unit")
    x_min = _num(x_min, "x_min"); x_max = _num(x_max, "x_max")
    y_min = _num(y_min, "y_min"); y_max = _num(y_max, "y_max")
    dx = (x_max - x_min) / (cols - 1)
    dy = (y_max - y_min) / (rows - 1)
    for row in range(rows - 1):
        for col in range(cols - 1):
            x0 = x_min + col * dx
            y0 = y_min + row * dy
            x1 = x0 + dx
            y1 = y0 + dy
            z00 = _ieee_div(elevations[row * cols + col], mpu)
            z10 = _ieee_div(elevations[row * cols + col + 1], mpu)
            z01 = _ieee_div(elevations[(row + 1) * cols + col], mpu)
            z11 = _ieee_div(elevations[(row + 1) * cols + col + 1], mpu)
            run_x = dx * mpu
            run_y = dy * mpu
            slope_x = _ieee_div(abs((z10 - z00) * mpu), run_x) * 100.0
            slope_y = _ieee_div(abs((z01 - z00) * mpu), run_y) * 100.0
            slope = _cs_max(slope_x, slope_y)
            bucket = classify_slope(slope)
            faces.append({
                "kind": "3DFACE",
                "layer": TOPO_LAYER,
                "vertices": [(x0, y0, z00), (x1, y0, z10), (x1, y1, z11), (x0, y1, z01)],
                "edges_visible": (True, True, True, True),
                "slope_percent": slope,
                "bucket": bucket,
                "color": bucket_color(bucket),
                "row": row,
                "col": col,
            })
    return faces


def _entity_layers(entities):
    if not isinstance(entities, (list, tuple)):
        raise TerrainInputError("entities must be a list")
    if len(entities) > MAX_ENTITIES:
        raise TerrainBoundsError(f"{len(entities)} entities exceed the bound of {MAX_ENTITIES}")
    for i, e in enumerate(entities):
        if not isinstance(e, dict) or not isinstance(e.get("layer"), str):
            raise TerrainInputError(f"entities[{i}] must be a dict with a string layer")
    return entities


def terrain_mesh_entities_cleared(entities):
    """ClearTerrainMesh / EraseLayerEntities, :1634-1679: indices of every live
    modelspace entity (any kind) on LEAF-TOPO, case-insensitive, in modelspace order.
    An entity marked {"erased": True} is skipped (:1660)."""
    return [i for i, e in enumerate(_entity_layers(entities))
            if not e.get("erased") and _layer_equals(e["layer"], TOPO_LAYER)]


# ---------------------------------------------------------------------------
#  LEAFTOPOFROM3DFACES and LEAFTERRAINMESH, end to end
# ---------------------------------------------------------------------------

def topo_from_3d_faces(faces, meters_per_unit, target_cells=DEFAULT_TARGET_CELLS,
                       existing_entities=None, max_idw_operations=MAX_IDW_OPERATIONS):
    """PersistFrom3dFaces, :191-288: what LEAFTOPOFROM3DFACES commits.

    faces: modelspace 3DFACEs as {"layer", "vertices"} in modelspace order.
    target_cells is clamped to [2, 300] (:207); the prompt's empty answer is 150
    (:92-94). existing_entities (optional) lets the result name which old
    LEAF-TOPO entities the command erases. Returns the plugin's result fields plus
    "grid" (the neutral grid the command commits, see neutral_grid) and "mesh" (the
    new faces); on a refusal the message is the plugin's and no grid or mesh is
    produced. PersistElevationGrid also stamps a new site layout revision (:1740);
    that write is not modelled.
    """
    mpu = _meters_per_unit(meters_per_unit)
    target_cells = _int(target_cells, "target_cells")
    cells = max(MIN_TARGET_CELLS, min(MAX_TARGET_CELLS, target_cells))
    faces = _validate_faces(faces)
    result = {"succeeded": False, "all_faces": len(faces), "source_faces": 0,
              "unique_vertices": 0, "rows": 0, "cols": 0, "old_mesh_faces_cleared": 0,
              "mesh_faces_drawn": 0, "source_label": None, "message": None,
              "grid": None, "mesh": None, "cleared_entity_indices": []}
    if not faces:
        result["message"] = "LEAFTOPOFROM3DFACES: no modelspace 3DFACE entities found."
        return result
    source, label = select_source_faces(faces)
    result["source_faces"] = len(source)
    result["source_label"] = label
    points = build_terrain_points_from_faces([f["vertices"] for f in source], mpu)
    result["unique_vertices"] = len(points)
    if len(points) < 3:
        result["message"] = (f"LEAFTOPOFROM3DFACES: only {len(points)} unique terrain vertices "
                             "found; need at least 3.")
        return result
    grid = resample_to_grid(points, cells, max_idw_operations=max_idw_operations)
    grid = {key: grid[key] for key in ("elevations", "rows", "cols") + GRID_BOUNDS}
    cleared = terrain_mesh_entities_cleared(existing_entities) if existing_entities else []
    mesh = draw_grid_mesh(grid["elevations"], grid["rows"], grid["cols"], grid["x_min"],
                          grid["x_max"], grid["y_min"], grid["y_max"], mpu)
    result.update(succeeded=True, rows=grid["rows"], cols=grid["cols"], grid=grid,
                  mesh=mesh, cleared_entity_indices=cleared,
                  old_mesh_faces_cleared=len(cleared), mesh_faces_drawn=len(mesh))
    result["message"] = (
        f"LEAFTOPOFROM3DFACES complete: persisted LEAFTOPO {grid['rows']}x{grid['cols']} grid "
        f"from {len(points):,} unique vertex/vertices and {len(source):,} face(s) on {label}; "
        f"replaced {len(cleared):,} old {TOPO_LAYER} face(s), "
        f"drew {len(mesh):,} new face(s).")
    return result


def outcome_counts(result):
    """BuildOutcomeCounts, :165-177."""
    keys = ("all_faces", "source_faces", "unique_vertices", "rows", "cols",
            "old_mesh_faces_cleared", "mesh_faces_drawn")
    return {k: int((result or {}).get(k) or 0) for k in keys}


def terrain_mesh_rerender(grid, meters_per_unit, existing_entities=None):
    """LEAFTERRAINMESH, LeafTerrainMeshCommand.cs:50-78: read the committed grid
    (neutral, or None when the drawing has none), erase LEAF-TOPO, redraw. Returns
    {"succeeded", "message", "mesh", "cleared_entity_indices"}; with no grid the
    plugin's message and no change."""
    mpu = _meters_per_unit(meters_per_unit)
    grid = mesh_grid(grid)
    if grid is None:
        return {"succeeded": False, "mesh": None, "cleared_entity_indices": [],
                "message": ("LEAFTERRAINMESH: no LEAFTOPO grid found in this drawing. "
                            "Run LEAFTOPO first to import terrain data, then LEAFTERRAINMESH "
                            "to re-render.")}
    cleared = terrain_mesh_entities_cleared(existing_entities) if existing_entities else []
    mesh = draw_grid_mesh(grid["elevations"], grid["rows"], grid["cols"], grid["x_min"],
                          grid["x_max"], grid["y_min"], grid["y_max"], mpu)
    faces = (grid["rows"] - 1) * (grid["cols"] - 1)
    return {"succeeded": True, "mesh": mesh, "cleared_entity_indices": cleared,
            "message": (f"LEAFTERRAINMESH complete - replaced {len(cleared)} old face(s), "
                        f"{faces} faces drawn on {TOPO_LAYER} from {grid['rows']}x{grid['cols']} "
                        "elevation grid.")}


# ---------------------------------------------------------------------------
#  Terrain interpolator and profile (TerrainGridInterpolator.cs, TerrainProfileCalculator.cs)
# ---------------------------------------------------------------------------

class TerrainGridInterpolator:
    """TerrainGridInterpolator.cs:19-223. Bilinear Z (metres) at a drawing point;
    None outside the grid (no clamping, :164-166)."""

    __slots__ = ("elevations", "rows", "cols", "x_min", "x_max", "y_min", "y_max",
                 "meters_per_unit", "origin_x", "origin_y", "x_axis_x", "x_axis_y",
                 "y_axis_x", "y_axis_y")

    def __init__(self, elevations, rows, cols, x_min, x_max, y_min, y_max,
                 meters_per_unit=1.0, origin_x=None, origin_y=None, x_axis_x=None,
                 x_axis_y=None, y_axis_x=None, y_axis_y=None):
        if elevations is None:
            raise TerrainInputError("elevations must not be None")   # :90
        self.elevations, self.rows, self.cols = _validate_grid(elevations, rows, cols)
        self.x_min = _num(x_min, "x_min"); self.x_max = _num(x_max, "x_max")
        self.y_min = _num(y_min, "y_min"); self.y_max = _num(y_max, "y_max")
        mpu = _num(meters_per_unit, "meters_per_unit")
        self.meters_per_unit = mpu if mpu > 0 else 1.0                 # :97
        frame = (origin_x, origin_y, x_axis_x, x_axis_y, y_axis_x, y_axis_y)
        if all(v is None for v in frame):                               # :59-65
            frame = (self.x_min, self.y_min, self.x_max - self.x_min, 0.0,
                     0.0, self.y_max - self.y_min)
        elif any(v is None for v in frame):
            raise TerrainInputError("an affine frame needs all six values")
        (self.origin_x, self.origin_y, self.x_axis_x, self.x_axis_y,
         self.y_axis_x, self.y_axis_y) = (_num(v, "frame") for v in frame)

    def interpolate_z(self, x, y):
        """InterpolateZ, :149-185."""
        rows, cols = self.rows, self.cols
        if rows < 1 or cols < 1:
            return None
        det = self.x_axis_x * self.y_axis_y - self.x_axis_y * self.y_axis_x
        if abs(det) <= 1e-12:
            return None
        dx = x - self.origin_x
        dy = y - self.origin_y
        u = (dx * self.y_axis_y - dy * self.y_axis_x) / det
        v = (self.x_axis_x * dy - self.x_axis_y * dx) / det
        frac_x = u * (cols - 1)
        frac_y = v * (rows - 1)
        if frac_x < 0 or frac_x > cols - 1 or frac_y < 0 or frac_y > rows - 1:
            return None
        if math.isnan(frac_x) or math.isnan(frac_y):
            return None
        col_left = min(int(frac_x), cols - 2)
        row_bot = min(int(frac_y), rows - 2)
        col_right = col_left + 1
        row_top = row_bot + 1
        tx = frac_x - col_left
        ty = frac_y - row_bot
        e = self.elevations
        e_bl = e[row_bot * cols + col_left]
        e_br = e[row_bot * cols + col_right]
        e_tl = e[row_top * cols + col_left]
        e_tr = e[row_top * cols + col_right]
        return ((1 - ty) * ((1 - tx) * e_bl + tx * e_br)
                + ty * ((1 - tx) * e_tl + tx * e_tr))


def sample_line(terrain, start, end, sample_count):
    """TerrainProfileCalculator.SampleLine, :54-84. Returns [{"distance_m",
    "elevation_m" (NaN outside the grid), "x", "y"}]."""
    if terrain is None:
        raise TerrainInputError("terrain must not be None")
    sample_count = _int(sample_count, "sample_count")
    if sample_count < 1:
        raise TerrainInputError("sampleCount must be >= 1.")
    if sample_count > MAX_PROFILE_SAMPLES:
        raise TerrainBoundsError(f"{sample_count} samples exceed {MAX_PROFILE_SAMPLES}")
    sx, sy = _point2(start, "start")
    ex, ey = _point2(end, "end")
    dx = ex - sx
    dy = ey - sy
    len_m = math.sqrt(dx * dx + dy * dy) * terrain.meters_per_unit
    out = []
    for i in range(sample_count):
        t = 0.0 if sample_count == 1 else i / (sample_count - 1)
        x = sx + t * dx
        y = sy + t * dy
        z = terrain.interpolate_z(x, y)
        out.append({"distance_m": t * len_m, "elevation_m": math.nan if z is None else z,
                    "x": x, "y": y})
    return out


def sample_polyline(terrain, vertices, samples_per_segment):
    """TerrainProfileCalculator.SamplePolyline, :107-154; (N-1)*k+1 points, each
    segment after the first skipping its shared start."""
    if terrain is None:
        raise TerrainInputError("terrain must not be None")
    if vertices is None:
        raise TerrainInputError("vertices must not be None")
    verts = [_point2(v, "vertex") for v in vertices]
    if len(verts) < 2:
        raise TerrainInputError("vertices must have at least 2 vertices.")
    samples_per_segment = _int(samples_per_segment, "samples_per_segment")
    if samples_per_segment < 1:
        raise TerrainInputError("samplesPerSegment must be >= 1.")
    if (len(verts) - 1) * samples_per_segment + 1 > MAX_PROFILE_SAMPLES:
        raise TerrainBoundsError("polyline samples exceed the bound")
    out = []
    cumulative = 0.0
    for seg in range(len(verts) - 1):
        (fx, fy), (tx_, ty_) = verts[seg], verts[seg + 1]
        dx = tx_ - fx
        dy = ty_ - fy
        len_m = math.sqrt(dx * dx + dy * dy) * terrain.meters_per_unit
        for i in range(0 if seg == 0 else 1, samples_per_segment + 1):
            t = i / samples_per_segment
            x = fx + t * dx
            y = fy + t * dy
            z = terrain.interpolate_z(x, y)
            out.append({"distance_m": cumulative + t * len_m,
                        "elevation_m": math.nan if z is None else z, "x": x, "y": y})
        cumulative += len_m
    return out


# ---------------------------------------------------------------------------
#  Tracker rows (TrackerRowGenerator.cs:172-299, TrackerSlopeValidator.cs:230-258,
#  TrackerRowReader.cs:287-337)
# ---------------------------------------------------------------------------

def tracker_row(row_index, axis_start, axis_end, module_slots=0, length_meters=0.0,
                parts_count=1):
    """The TrackerRow fields slope validation reads. parts_count is
    TerrainFollowing.PartsCount, default 1 (FramePreset.cs:144)."""
    return {
        "row_index": _int(row_index, "row_index"),
        "axis_start": _point2(axis_start, "axis_start"),
        "axis_end": _point2(axis_end, "axis_end"),
        "module_slots": _int(module_slots, "module_slots"),
        "length_meters": _num(length_meters, "length_meters"),
        "parts_count": _int(parts_count, "parts_count"),
    }


def preset_limits(preset=None):
    """FramePreset slope limits with the plugin's defaults for any key not given."""
    limits = dict(DEFAULT_PRESET_LIMITS)
    limits["Columns"] = 0
    if preset is None:
        return limits
    if not isinstance(preset, dict):
        raise TerrainInputError("preset must be a dict")
    for key in list(DEFAULT_PRESET_LIMITS) + ["Columns"]:
        if key in preset:
            limits[key] = (_int(preset[key], key) if key == "Columns"
                           else _num(preset[key], key))
    return limits


def _distance(a, b):
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    return math.sqrt(dx * dx + dy * dy)


def from_packed_frame(vertices, row_index, preset, meters_per_unit):
    """TrackerSlopeValidator.FromPackedFrame, :230-258: axis from midpoint(v0, v1)
    to midpoint(v2, v3); ModuleSlots = max(0, preset.Columns); a non-positive
    metersPerUnit becomes 1.0 (:238)."""
    if not isinstance(vertices, (list, tuple)) or len(vertices) < 4:
        raise TerrainInputError("Frame must contain four vertices.")
    p = [_point2(v, "frame vertex") for v in vertices[:4]]
    mpu = _num(meters_per_unit, "meters_per_unit")
    if mpu <= 0:
        mpu = 1.0
    start = ((p[0][0] + p[1][0]) * 0.5, (p[0][1] + p[1][1]) * 0.5)
    end = ((p[2][0] + p[3][0]) * 0.5, (p[2][1] + p[3][1]) * 0.5)
    columns = preset_limits(preset)["Columns"]
    return tracker_row(_int(row_index, "row_index"), start, end, max(0, columns),
                       _distance(start, end) * mpu)


def polyline_to_tracker_row(vertices, row_index, module_slots, meters_per_unit):
    """TrackerRowReader.PolylineToTrackerRow, :287-337, given the row index and
    module slots the tracker row carries (0 and 0 when it carries none, :297-301)."""
    if not isinstance(vertices, (list, tuple)) or len(vertices) < 4:
        raise TerrainInputError("tracker polyline needs four vertices")
    p = [_point2(v, "tracker vertex") for v in vertices[:4]]
    mpu = _num(meters_per_unit, "meters_per_unit")
    start = ((p[0][0] + p[1][0]) * 0.5, (p[0][1] + p[1][1]) * 0.5)
    end = ((p[2][0] + p[3][0]) * 0.5, (p[2][1] + p[3][1]) * 0.5)
    return tracker_row(_int(row_index, "row_index"), start, end, _int(module_slots, "module_slots"),
                       _distance(start, end) * mpu)


def _neutral_fields(value, keys, what):
    """A neutral sub-record: a dict whose listed keys are integers (missing is 0)."""
    if not isinstance(value, dict):
        raise TerrainInputError(f"{what} must be a dict")
    return [_int(value.get(k, 0), f"{what}.{k}") for k in keys]


def read_rows_for_slope(entities, preset, meters_per_unit):
    """TrackerSlopeDrawing.ReadRowsForSlope, :89-137, over modelspace in order.

    Entity shapes (neutral: the caller has already read each entity's identity):
      {"kind": "LWPOLYLINE", "layer", "vertices", "tracker_row": {"row_index",
       "module_slots"}} or {..., "frame_cell": {"row", "col"}}
        read when on LEAF-TRACKERS with >= 4 vertices (TryReadLeafPolyline,
        :237-279): a tracker row wins, else a generated frame's cell (ReadFrameCell,
        :281-294, reads only its row), else the polyline is skipped.
      {"kind": "PVCASE_TRACKER", "row": tracker_row(...)}
        a PVcase block the adapter already read with TrackerRowReader.
        TryReadPvcaseTracker is not ported here; the adapter owns that read.
    Every accepted row is renumbered to its position in the list (:112, :126).
    """
    if not isinstance(entities, (list, tuple)):
        raise TerrainInputError("entities must be a list")
    if len(entities) > MAX_ENTITIES:
        raise TerrainBoundsError(f"{len(entities)} entities exceed the bound of {MAX_ENTITIES}")
    rows = []
    for i, ent in enumerate(entities):
        if not isinstance(ent, dict):
            raise TerrainInputError(f"entities[{i}] must be a dict")
        kind = ent.get("kind")
        row = None
        if kind == "LWPOLYLINE":
            if not _layer_equals(ent.get("layer"), TRACKER_LAYER):
                continue
            verts = ent.get("vertices")
            if not isinstance(verts, (list, tuple)) or len(verts) < 4:
                continue
            if ent.get("tracker_row") is not None:
                row_index, slots = _neutral_fields(ent["tracker_row"], ("row_index", "module_slots"),
                                                   f"entities[{i}].tracker_row")
                row = polyline_to_tracker_row(verts, row_index, slots, meters_per_unit)
            elif ent.get("frame_cell") is not None:
                frame_row, _ = _neutral_fields(ent["frame_cell"], ("row", "col"),
                                               f"entities[{i}].frame_cell")
                row = from_packed_frame(verts, frame_row, preset, meters_per_unit)
        elif kind == "PVCASE_TRACKER":
            src = ent.get("row")
            if not isinstance(src, dict):
                raise TerrainInputError(f"entities[{i}].row must be a tracker row")
            row = tracker_row(src.get("row_index", 0), src.get("axis_start"), src.get("axis_end"),
                              src.get("module_slots", 0), src.get("length_meters", 0.0),
                              src.get("parts_count", 1))
        if row is not None:
            if len(rows) >= MAX_TRACKER_ROWS:
                raise TerrainBoundsError(f"more than {MAX_TRACKER_ROWS} tracker rows")
            row["row_index"] = len(rows)
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
#  Validation (TrackerSlopeValidator.cs)
# ---------------------------------------------------------------------------

def _validate_row_input(row):
    if not isinstance(row, dict):
        raise TerrainInputError("row must be a tracker_row dict")
    return tracker_row(row.get("row_index", 0), row.get("axis_start"), row.get("axis_end"),
                       row.get("module_slots", 0), row.get("length_meters", 0.0),
                       row.get("parts_count", 1))


def _point_at(row, fraction):
    """PointAt, :506-511."""
    (sx, sy), (ex, ey) = row["axis_start"], row["axis_end"]
    return (sx + (ex - sx) * fraction, sy + (ey - sy) * fraction)


def _axial_segment_count(row):
    """AxialSegmentCount, :260-266."""
    parts = max(1, row["parts_count"])
    slots = row["module_slots"] if row["module_slots"] > 0 else DEFAULT_AXIAL_SEGMENTS
    return max(1, min(MAX_AXIAL_SEGMENTS, max(parts, slots)))


def _violation(vtype, row_index, other, segment, t0, t1, pct, deg, limit, a, b):
    return {"type": vtype, "row_index": row_index, "other_row_index": other,
            "segment_index": segment, "start_fraction": t0, "end_fraction": t1,
            "slope_percent": pct, "slope_degrees": deg, "limit": limit,
            "x0": a[0], "y0": a[1], "x1": b[0], "y1": b[1]}


def _slope_degrees(pct):
    return math.atan(pct / 100.0) * 180.0 / math.pi


def validate_row(row, terrain, preset=None):
    """TrackerSlopeValidator.Validate, :157-204, plus AddRecommendedSplitFractions,
    :268-288. Returns a row report dict."""
    if terrain is None:
        raise TerrainInputError("terrain must not be None")
    row = _validate_row_input(row)
    limits = preset_limits(preset)
    report = {"row": row, "row_index": row["row_index"], "axial_segments_checked": 0,
              "axial_violations": [], "recommended_split_fractions": []}
    segments = _axial_segment_count(row)
    for i in range(segments):
        t0 = i / segments
        t1 = (i + 1) / segments
        p0 = _point_at(row, t0)
        p1 = _point_at(row, t1)
        z0 = terrain.interpolate_z(p0[0], p0[1])
        z1 = terrain.interpolate_z(p1[0], p1[1])
        if z0 is None or z1 is None:
            continue
        distance_m = _distance(p0, p1) * terrain.meters_per_unit
        if distance_m <= _EPSILON:
            continue
        pct = abs(z1 - z0) / distance_m * 100.0
        deg = _slope_degrees(pct)
        report["axial_segments_checked"] += 1
        if pct > limits["MaxAxialSlopePct"] + _EPSILON:
            report["axial_violations"].append(_violation(
                "Axial", row["row_index"], -1, i, t0, t1, pct, deg,
                limits["MaxAxialSlopePct"], p0, p1))
    splits = report["recommended_split_fractions"]
    parts = max(1, row["parts_count"])
    if parts > 1:
        splits.extend(k / parts for k in range(1, parts))
    else:
        for v in report["axial_violations"]:
            if len(splits) >= 3:
                break
            split = v["end_fraction"]
            if split <= _EPSILON or split >= 1.0 - _EPSILON:
                continue
            if not any(abs(s - split) < 1e-6 for s in splits):
                splits.append(split)
    report["has_axial_violations"] = bool(report["axial_violations"])
    # NeedsTerrainFollowingParts, TrackerSlopeValidator.cs:45-52 (PartsCount, not max(1, ...))
    report["needs_terrain_following_parts"] = (report["has_axial_violations"]
                                               and row["parts_count"] <= 1)
    return report


def _row_projections(rows):
    """BuildRowProjections, :297-342. Every projection uses the FIRST valid row's
    unit axis as the reference, as the plugin does."""
    out = []
    ref_x, ref_y, have_ref = 0.0, 1.0, False
    for row in rows:
        ux = row["axis_end"][0] - row["axis_start"][0]
        uy = row["axis_end"][1] - row["axis_start"][1]
        length = math.sqrt(ux * ux + uy * uy)
        if length <= _EPSILON:
            continue
        ux /= length
        uy /= length
        if not have_ref:
            ref_x, ref_y, have_ref = ux, uy, True
        cx = (row["axis_start"][0] + row["axis_end"][0]) * 0.5
        cy = (row["axis_start"][1] + row["axis_end"][1]) * 0.5
        s0 = row["axis_start"][0] * ref_x + row["axis_start"][1] * ref_y
        s1 = row["axis_end"][0] * ref_x + row["axis_end"][1] * ref_y
        cross_x = -ref_y
        cross_y = ref_x
        amin, amax = min(s0, s1), max(s0, s1)
        out.append({"row": row, "cross": cx * cross_x + cy * cross_y,
                    "s0": s0, "s1": s1, "amin": amin, "amax": amax, "span": amax - amin})
    return out


def _axis_overlap(a, b):
    return min(a["amax"], b["amax"]) - max(a["amin"], b["amin"])


def _pair_key(a, b):
    return (a, b) if a <= b else (b, a)


def adjacent_cross_axis_pairs(projections):
    """BuildAdjacentCrossAxisPairs, :344-377: for each row (in order) the row with
    the smallest cross-axis offset above it (delta > 1e-9) whose axis overlap is
    positive and at least a quarter of the shorter span; first index wins a tie.

    Same answer as the plugin's all-pairs scan without its n^2 walk: candidates
    are walked in ascending cross projection, and rounded subtraction is monotone,
    so deltas never decrease along the walk. The first qualifying candidate holds
    the minimum delta; every candidate whose rounded delta ties it is then checked
    so the lowest index wins, as the plugin's strict `delta >= bestDelta` skip does.
    """
    order = sorted(range(len(projections)), key=lambda k: projections[k]["cross"])
    crosses = [projections[k]["cross"] for k in order]

    def qualifies(a, b):
        overlap = _axis_overlap(a, b)
        return not (overlap <= _EPSILON or overlap < min(a["span"], b["span"]) * 0.25)

    result = []
    seen = set()
    for a in projections:
        best = best_index = best_delta = None
        pos = bisect_right(crosses, a["cross"])
        while pos < len(order):
            b = projections[order[pos]]
            delta = b["cross"] - a["cross"]
            if delta > _EPSILON and qualifies(a, b):
                best, best_index, best_delta = b, order[pos], delta
                break
            pos += 1
        if best is None:
            continue
        pos += 1
        while pos < len(order):
            b = projections[order[pos]]
            if b["cross"] - a["cross"] != best_delta:
                break
            if order[pos] < best_index and qualifies(a, b):
                best, best_index = b, order[pos]
            pos += 1
        key = _pair_key(a["row"]["row_index"], best["row"]["row_index"])
        if key not in seen:
            seen.add(key)
            result.append((a, best))
    return result


def _fraction_for_axis_station(p, station):
    """FractionForAxisStation, :469-477."""
    span = p["s1"] - p["s0"]
    if abs(span) <= _EPSILON:
        return 0.0
    t = (station - p["s0"]) / span
    return 0.0 if t < 0.0 else 1.0 if t > 1.0 else t


def _validate_cross_axis_pair(a, b, terrain, limits, batch):
    """ValidateCrossAxisPair, :379-467: five stations across the axis overlap,
    the steepest sample judged against both cross-axis limits."""
    omin = max(a["amin"], b["amin"])
    omax = min(a["amax"], b["amax"])
    overlap = omax - omin
    if overlap <= _EPSILON:
        return
    sampled = False
    best_i, best_pct, best_deg = -1, 0.0, 0.0
    best_a = best_b = (0.0, 0.0)
    n = CROSS_AXIS_SAMPLE_STATIONS
    for i in range(n):
        f = 0.5 if n == 1 else i / (n - 1)
        station = omin + overlap * f
        pa = _point_at(a["row"], _fraction_for_axis_station(a, station))
        pb = _point_at(b["row"], _fraction_for_axis_station(b, station))
        za = terrain.interpolate_z(pa[0], pa[1])
        zb = terrain.interpolate_z(pb[0], pb[1])
        if za is None or zb is None:
            continue
        distance_m = _distance(pa, pb) * terrain.meters_per_unit
        if distance_m <= _EPSILON:
            continue
        pct = abs(zb - za) / distance_m * 100.0
        deg = _slope_degrees(pct)
        sampled = True
        if pct > best_pct:
            best_pct, best_deg, best_i, best_a, best_b = pct, deg, i, pa, pb
    if not sampled:
        return
    batch["cross_axis_pairs_checked"] += 1
    batch["row_to_row_pairs_checked"] += 1
    ai, bi = a["row"]["row_index"], b["row"]["row_index"]
    if best_pct > limits["MaxCrossAxisSlopePct"] + _EPSILON:
        batch["cross_axis_violations"].append(_violation(
            "CrossAxis", ai, bi, best_i, 0.0, 0.0, best_pct, best_deg,
            limits["MaxCrossAxisSlopePct"], best_a, best_b))
    if best_deg > limits["MaxRowToRowSlopeDeg"] + _EPSILON:
        batch["row_to_row_angle_violations"].append(_violation(
            "RowToRowAngle", ai, bi, best_i, 0.0, 0.0, best_pct, best_deg,
            limits["MaxRowToRowSlopeDeg"], best_a, best_b))


def _count_distinct_pairs(violations):
    """CountDistinctPairs, :116-138."""
    seen = set()
    for v in violations:
        a, b = v["row_index"], v["other_row_index"]
        seen.add((a,) if b < 0 else _pair_key(a, b))
    return len(seen)


def format_status(batch):
    """TrackerSlopeBatchReport.FormatStatus, :99-114."""
    text = ("Tracker slope: "
            f"{batch['axial_violation_rows']}/{batch['axial_rows_checked']} axial / "
            f"{batch['cross_axis_violation_pairs']}/{batch['cross_axis_pairs_checked']} cross")
    if batch["row_to_row_angle_violation_pairs"] > 0:
        text += (f" / {batch['row_to_row_angle_violation_pairs']}/"
                 f"{batch['row_to_row_pairs_checked']} row-to-row deg")
    return text + (" exceed active preset limits." if batch["has_violations"]
                   else " - within ASCE 7-16 budget.")


def validate_rows(rows, terrain, preset=None):
    """TrackerSlopeValidator.ValidateRows, :206-228, with the batch report's
    derived counts (:69-97) and status line."""
    if rows is None:
        raise TerrainInputError("rows must not be None")
    if terrain is None:
        raise TerrainInputError("terrain must not be None")
    if not isinstance(rows, (list, tuple)):
        raise TerrainInputError("rows must be a list")
    if len(rows) > MAX_TRACKER_ROWS:
        raise TerrainBoundsError(f"{len(rows)} tracker rows exceed {MAX_TRACKER_ROWS}")
    limits = preset_limits(preset)
    clean = [_validate_row_input(r) for r in rows]
    batch = {"tracker_count": len(clean), "row_reports": [], "cross_axis_violations": [],
             "row_to_row_angle_violations": [], "axial_rows_checked": 0,
             "cross_axis_pairs_checked": 0, "row_to_row_pairs_checked": 0}
    for row in clean:
        rep = validate_row(row, terrain, limits)
        batch["row_reports"].append(rep)
        if rep["axial_segments_checked"] > 0:
            batch["axial_rows_checked"] += 1
    for a, b in adjacent_cross_axis_pairs(_row_projections(clean)):
        _validate_cross_axis_pair(a, b, terrain, limits, batch)
    reps = batch["row_reports"]
    batch["axial_violation_rows"] = sum(1 for r in reps if r["has_axial_violations"])
    batch["cross_axis_violation_pairs"] = _count_distinct_pairs(batch["cross_axis_violations"])
    batch["row_to_row_angle_violation_pairs"] = _count_distinct_pairs(
        batch["row_to_row_angle_violations"])
    batch["trackers_needing_terrain_following"] = sum(
        1 for r in reps if r["needs_terrain_following_parts"])
    batch["has_violations"] = (batch["axial_violation_rows"] > 0
                               or batch["cross_axis_violation_pairs"] > 0
                               or batch["row_to_row_angle_violation_pairs"] > 0)
    batch["status"] = format_status(batch)
    return batch


# ---------------------------------------------------------------------------
#  LEAFTRACKERSLOPEVIOLATIONS / LEAFCLEARTRACKERSLOPEVIOLATIONS
# ---------------------------------------------------------------------------

def build_report(grid, entities, preset, meters_per_unit):
    """TrackerSlopeDrawing.BuildReport, :63-87. Returns (report or None, message).
    grid is the drawing's committed neutral grid or None. meters_per_unit is what
    ActiveMetersPerUnit (:15-41) resolved in the drawing; that resolution reads
    drawing state and is the adapter's input here."""
    terrain = terrain_interpolator(grid, meters_per_unit)
    if terrain is None:
        return None, "Tracker slope: no LEAFTOPO terrain found; validation skipped."
    rows = read_rows_for_slope(entities, preset, meters_per_unit)
    if not rows:
        return None, "Tracker slope: no tracker rows found."
    report = validate_rows(rows, terrain, preset)
    return report, report["status"]


def violation_overlays(report):
    """DrawViolationOverlays, :139-186: one 2-vertex LWPOLYLINE along the axis of
    every row with an axial violation, then both rows of every cross-axis and
    row-to-row violation; a row is drawn once, in first-insertion order (the
    plugin's Dictionary<int, TrackerRow>). Colour ByLayer (256), constant width
    0.25, zero bulges, on LEAF-SLOPE-VIOLATIONS. None draws nothing (:141)."""
    if report is None:
        return []
    rows_to_draw = {}
    for rep in report["row_reports"]:
        if rep["has_axial_violations"] and rep.get("row") is not None:
            rows_to_draw[rep["row"]["row_index"]] = rep["row"]
    for v in report["cross_axis_violations"] + report["row_to_row_angle_violations"]:
        for rep in report["row_reports"]:        # AddRowsForPair, :221-235
            row = rep.get("row")
            if row is None:
                continue
            if row["row_index"] in (v["row_index"], v["other_row_index"]):
                rows_to_draw[row["row_index"]] = row
    return [{
        "kind": "LWPOLYLINE",
        "layer": VIOLATION_LAYER,
        "color_index": VIOLATION_ENTITY_COLOR_INDEX,
        "vertices": [row["axis_start"], row["axis_end"]],
        "bulges": [0.0, 0.0],
        "constant_width": VIOLATION_CONSTANT_WIDTH,
        "row_index": row["row_index"],
    } for row in rows_to_draw.values()]


def violation_overlay_entities_cleared(entities):
    """ClearViolationOverlays, :188-219, the whole of LEAFCLEARTRACKERSLOPEVIOLATIONS
    (LeafCivilCommand.cs:81-110) and the first step of every
    LEAFTRACKERSLOPEVIOLATIONS run (:142): indices of every modelspace entity, of any
    kind, on LEAF-SLOPE-VIOLATIONS (case-insensitive), in modelspace order."""
    return [i for i, e in enumerate(_entity_layers(entities))
            if _layer_equals(e["layer"], VIOLATION_LAYER)]


def tracker_slope_violations(grid, entities, preset, meters_per_unit):
    """LEAFTRACKERSLOPEVIOLATIONS, LeafCivilCommand.cs:32-79, as committed state.

    Returns {"succeeded", "message", "report", "cleared_entity_indices",
    "overlays"}. With no report nothing is cleared or drawn (the plugin returns
    before DrawViolationOverlays)."""
    report, message = build_report(grid, entities, preset, meters_per_unit)
    if report is None:
        return {"succeeded": False, "report": None, "cleared_entity_indices": [],
                "overlays": [], "message": "LEAFTRACKERSLOPEVIOLATIONS: " + message}
    cleared = violation_overlay_entities_cleared(entities)
    overlays = violation_overlays(report)
    return {"succeeded": True, "report": report, "cleared_entity_indices": cleared,
            "overlays": overlays,
            "message": (f"LEAFTRACKERSLOPEVIOLATIONS: {message} Drew {len(overlays)} red "
                        f"overlay(s) on {VIOLATION_LAYER}.")}


def clear_tracker_slope_violations(entities):
    """LEAFCLEARTRACKERSLOPEVIOLATIONS, LeafCivilCommand.cs:81-110."""
    cleared = violation_overlay_entities_cleared(entities)
    return {"cleared_entity_indices": cleared,
            "message": f"LEAFCLEARTRACKERSLOPEVIOLATIONS: removed {len(cleared)} overlay(s)."}
