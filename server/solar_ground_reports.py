"""Studio ports of the plugin's ground report engines: LEAFFENCE3DAUDIT, LEAFMESHDIFFVIEW,
LEAFVEGETATIONFROMCIVIL, LEAFFENCEMESHFROMCIVIL and LEAFOPTIMALSPACING.

Literal ports of Branch2025 (read 2026-09-23 at C:/tmp/solar-parity/wt-b25-s17):

  Pvcase/CivilLayers.cs               IsFenceLayer, IsSetbackLayer, EqualsLayer, LayerTokens,
                                      the fence and setback layer lists (:21-31, :58-75, :194-314)
  Pvcase/CivilDrawingTraversal.cs     TryReadLinearVertices(3d), the source-Z rule (:47-143, :239-243)
  Pvcase/FenceSurface.cs              FenceSurfaceAudit.FormatStatusLine, Audit, BuildMeshes,
                                      ReadFenceRuns, AppendFenceMesh, NormalizeVertices,
                                      CountMeshFaces, the two commands' prompts and messages
  Pvcase/VegetationMass.cs            ImportCivilWoodlandHeights, the woodland layer tests,
                                      TryParseHeightFeet, NormalizeClosedVertices,
                                      RegionSpatialIndex, AssignSamplesToRegions (orphan snap),
                                      the mesh face count of AppendVegetationMesh, the import
                                      status record SaveLastImportResult writes
  Pvcase/LeafShadingObjectCommand.cs  LEAFVEGETATIONFROMCIVIL prompt and messages (:92-165)
  Pvcase/LeafMeshDiffViewCommand.cs   MeshDiffDataBuilder (Build, BuildTopoWire counts,
                                      ReadPanelDiffPoints, AddPolylineCenter, AlignDatum,
                                      ComputeStats, SampleIndices, Median, Percentile),
                                      PanelReadResult.TryAdd, the status and scan lines
  Terrain/OptimalRowSpacingCalculator.cs, Terrain/OptimalRowSpacingCommand.cs
                                      the pitch sweep, its settings defaults, prompts, report
  Terrain/ShadeTableGenerator.cs, Terrain/ShadeCalculator.cs, Terrain/SunPositionCalculator.cs
                                      the 8 760-hour near-shading loss the sweep minimises
  System.Random (seeded, the .NET 5 compatible subtractive generator) for the mesh diff's
                                      reservoir sample past 6 000 points

Pure functions over plain data. No AutoCAD, no I/O, no network. A drawing is a list of
NEUTRAL entities, the flattened model-space traversal the plugin walks:

  {"type": "polyline" | "polyline2d" | "polyline3d" | "line" | "curve" | "text" | "mtext" |
           "face" | "block" | "other",
   "layer": str,
   "vertices": [[x, y] or [x, y, z], ...]   linear types, in drawing order (a curve gives the
                                            13 points TrySampleCurve3d takes)
   "closed": bool, "elevation": float       polylines; a 2-value vertex takes the elevation as z
   "text": str, "position": [x, y(, z)]     text and mtext
   "columns": int}                          a block on the third-party tracker layer: the column
                                            count its row metadata carries (0 = none)

Terrain is a callable (x, y) -> elevation in metres or None outside the grid, the port of
TerrainGridInterpolator.InterpolateZ (server/solar_ground_terrain.py), or None for no LEAFTOPO.

Floating point work follows the C# expression order. One loop is restructured, never
approximated, where the literal form would pin a worker: the shade table's collinear path
(annual_shade_loss), whose docstring proves it computes the same terms; the literal pair loop
stays beside it and the tests hold the two equal.

Every input is bounded and every malformed input fails closed with ReportInputError (a
ValueError); a bound breach raises ReportBoundsError.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, localcontext
import importlib.util
import math
from numbers import Real
from pathlib import Path
import re


def _load_sibling(name):
    """Load a server module by path so the import works from any cwd."""
    path = Path(__file__).resolve().with_name(name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_sweep = _load_sibling("solar_terrain")          # TrackerRowGenerator.Generate port
_layout = _load_sibling("solar_ground_layout")   # ModuleCommand.GetActiveModule, settings load

# ---------------------------------------------------------------------------
#  Constants (each cites the line that defines it)
# ---------------------------------------------------------------------------

LAYER_TRACKERS = "LEAF-TRACKERS"                              # LayerNames.cs:36
LAYER_SHADING_RESTRICTION = "LEAF-PVCASE-SHADING-RESTRICTION"  # LayerNames.cs:53
LAYER_FENCE_MESH = "LEAF-PVCASE-SHADING-FENCE-MESH"            # LayerNames.cs:69
LAYER_VEGETATION = "LEAF-PVCASE-SHADING-VEGETATION"            # LayerNames.cs:72
LAYER_VEGETATION_MESH = "LEAF-PVCASE-SHADING-VEGETATION-MESH"  # LayerNames.cs:79
LAYER_THIRD_PARTY_TRACKERS = "PVcase PV Modules (full frames)"  # TrackerRowReader.cs:32
MODULE_LAYER_FALLBACK = "PANEL"                               # LeafMeshDiffViewCommand.cs:719

FENCE_SHADING = "LEAF-PVCASE-SHADING-FENCE"   # CivilLayers.cs:21 (a prefix match, :237-241)
FENCE_VENDOR_SITE = "V-SITE-FENC-LINE"        # CivilLayers.cs:22
FENCE_THIRD_PARTY = "PVcase Fence"            # CivilLayers.cs:27
SETBACK_LAYERS = ("APX-BNDY-SBCK-LINE-FENCE", "APX-BNDY-SBCK-LINE-ARRAY",
                  "APX-BNDY-SBCK-LINE-COLLECTION", "V-PROP-SETBACK")   # CivilLayers.cs:29-32
# LEAFSETBACK's ring layer per kind (CivilLayers.cs:29-31), how Studio's rings are placed.
SETBACK_LAYER_BY_KIND = {"fence": SETBACK_LAYERS[0], "array": SETBACK_LAYERS[1],
                         "collection": SETBACK_LAYERS[2]}
FENCE_REJECT_TOKENS = ("SBCK", "SETBACK", "ROAD", "ROADWAY", "RD", "RDWY")      # CivilLayers.cs:210
FENCE_TOKENS = ("FENCE", "FENCES", "FENC", "FNC", "FENCING", "FENCELINE", "FENCE-LINE",
                "CLF", "CHAINLINK")                                            # CivilLayers.cs:213-215
WOODLAND_TOKENS = ("V-VEGE-WDLN", "C-VEGE-WDLN")                               # VegetationMass.cs:60-64

SOURCE_Z_EPS = 1e-6                     # CivilDrawingTraversal.cs:241
VERTEX_MERGE_EPS = 1e-6                 # FenceSurface.cs:379, VegetationMass.cs:689
DEFAULT_FENCE_HEIGHT_M = 2.0            # FenceSurface.cs:49
DEFAULT_FENCE_WIDTH_M = 0.2             # FenceSurface.cs:50
FEET_TO_METERS = 0.3048                 # VegetationMass.cs:65
MAX_INTERPOLATED_MESH_AXIS = 40         # VegetationMass.cs:66
ORPHAN_SNAP_TOLERANCE_FEET = 75.0       # VegetationMass.cs:73
MAX_REGION_CELLS = 256                  # VegetationMass.cs:114
HEIGHT_FEET = re.compile(r"(?P<feet>\d+(?:\.\d+)?)\s*'")   # VegetationMass.cs:74-75
IMPORT_RECORD_SCHEMA = 1                # VegetationMass.cs:370

MAX_TOPO_AXIS_SAMPLES = 72              # LeafMeshDiffViewCommand.cs:461
MAX_PANEL_SAMPLES = 6000                # LeafMeshDiffViewCommand.cs:462
MAX_EXPECTED_TRACKER_CLEARANCE_M = 25.0  # LeafMeshDiffViewCommand.cs:463
PANEL_SAMPLE_SEED = 1107                # LeafMeshDiffViewCommand.cs:880
AUTHORED_Z_EPS = 1e-9                   # LeafMeshDiffViewCommand.cs:709
SOURCE_LABELS = {"third-party": "PVcase in-block panel solids",   # :673
                 "module": "module-layer polylines",               # :674-676
                 "tracker": "LEAF-TRACKERS polylines"}             # :677

# LEAFOPTIMALSPACING defaults, OptimalRowSpacingCommand.cs:21-37.
SPACING_DEFAULTS = {"target_capture": 0.99, "sweep_steps": 20, "pitch_min_multiplier": 1.5,
                    "pitch_max_multiplier": 3.0, "module_along_axis_m": 1.000,
                    "module_cross_axis_m": 2.100, "module_gap_m": 0.020, "module_pmax_w": 600.0,
                    "latitude_deg": 35.0, "longitude_deg": 0.0, "module_height_m": 1.5}
LATITUDE_CLAMP_DEG = 89.9               # OptimalRowSpacingCommand.cs:242
SPACING_TOP_K = 5                       # OptimalRowSpacingCommand.cs:351
SHADE_YEAR = 2025                       # OptimalRowSpacingCalculator.cs:161
HOURS_PER_YEAR = 8760                   # ShadeTableGenerator.cs:95
SAME_POSITION_EPS = 1e-9                # ShadeCalculator.cs:62
CAPTURE_EPS = 1e-12                     # OptimalRowSpacingCalculator.cs:128
DEG_TO_RAD = math.pi / 180.0            # SunPositionCalculator.cs:30, ShadeCalculator.cs:17
RAD_TO_DEG = 180.0 / math.pi            # SunPositionCalculator.cs:31

# Studio-side bounds. The plugin has none; these refuse inputs that would pin a worker.
MAX_ENTITIES = 500_000
MAX_ENTITY_VERTICES = 100_000
MAX_TOTAL_VERTICES = 2_000_000
MAX_TEXT_CHARS = 4096
MAX_REGIONS = 20_000
MAX_SAMPLES = 50_000
MAX_SWEEP_STEPS = 200
MAX_SWEEP_COLUMNS = 200_000             # tracker columns one sweep pitch may generate
MAX_SHADE_ROWS = 1000                   # rows of one pitch on the collinear shade path
MAX_LITERAL_SHADE_ROWS = 64             # rows of one pitch on the literal pair loop
MAX_GRID_AXIS = 100_000


class ReportInputError(ValueError):
    """Malformed input: the engine refuses rather than guess."""


class ReportBoundsError(ReportInputError):
    """Input exceeds a Studio bound."""


# ---------------------------------------------------------------------------
#  Validation and formatting helpers
# ---------------------------------------------------------------------------

def _num(value, what):
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
        raise ReportInputError(f"{what} must be a finite number")
    return float(value)


def _int(value, what):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReportInputError(f"{what} must be an integer")
    return value


def _mpu(value):
    mpu = _num(value, "meters_per_unit")
    if mpu <= 0.0:
        raise ReportInputError("meters_per_unit must be positive")
    return mpu


def _quantize(value, digits, scale):
    """The exact binary value times `scale`, rounded to `digits` places, ties away from zero
    (.NET Core 3.0 and later format the exact value). Decimal(v) is exact; the 400-digit
    context keeps the product exact for every value these engines print."""
    v = _num(value, "formatted value")
    if not 0 <= digits <= 15:
        raise ReportInputError("digits must be 0 to 15")
    with localcontext() as ctx:
        ctx.prec = 400
        return (Decimal(v) * scale).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)


def net_fixed(value, digits):
    """value.ToString("F<digits>", InvariantCulture) read back as a number."""
    return float(_quantize(value, digits, 1))


def net_percent(value, digits):
    """value.ToString("P<digits>") read back as a number: the exact value times 100."""
    return float(_quantize(value, digits, 100))


def _fixed_text(value, digits):
    return str(_quantize(value, digits, 1))


def _percent_text(value, digits):
    return f"{_quantize(value, digits, 100)} %"      # invariant PercentPositivePattern 0: "n %"


def format_g6(value):
    """value.ToString("G6", InvariantCulture) for the finite scales these engines print."""
    text = "%.6g" % _num(value, "formatted value")
    return text.replace("e", "E")


def _xy(point, what):
    if not isinstance(point, (list, tuple)) or len(point) not in (2, 3):
        raise ReportInputError(f"{what} must be a point of 2 or 3 numbers")
    return tuple(_num(v, what) for v in point)


def _entities(entities):
    """Validate the neutral entity list once, with the vertex budget; fail closed."""
    if not isinstance(entities, (list, tuple)):
        raise ReportInputError("entities must be a list")
    if len(entities) > MAX_ENTITIES:
        raise ReportBoundsError(f"{len(entities)} entities exceed {MAX_ENTITIES}")
    total = 0
    for ent in entities:
        if not isinstance(ent, dict) or not isinstance(ent.get("type"), str):
            raise ReportInputError("every entity must be an object with a type")
        if not isinstance(ent.get("layer", ""), str):
            raise ReportInputError("an entity layer must be a string")
        verts = ent.get("vertices")
        if verts is not None:
            if not isinstance(verts, (list, tuple)):
                raise ReportInputError("entity vertices must be a list")
            if len(verts) > MAX_ENTITY_VERTICES:
                raise ReportBoundsError(f"an entity has {len(verts)} vertices, over {MAX_ENTITY_VERTICES}")
            total += len(verts)
        text = ent.get("text")
        if text is not None and (not isinstance(text, str) or len(text) > MAX_TEXT_CHARS):
            raise ReportInputError(f"entity text must be a string of at most {MAX_TEXT_CHARS} characters")
    if total > MAX_TOTAL_VERTICES:
        raise ReportBoundsError(f"{total} vertices exceed {MAX_TOTAL_VERTICES}")
    return entities


# ---------------------------------------------------------------------------
#  CivilLayers.cs
# ---------------------------------------------------------------------------

def _equals_layer(layer, expected):
    """EqualsLayer, cs:243-253: case-insensitive, or an xref-bound "|name" / "$0$name" tail."""
    if layer is None or expected is None:
        return layer is expected
    if layer.casefold() == expected.casefold():
        return True
    if not layer.strip() or not expected.strip():
        return False
    low = layer.casefold()
    return low.endswith(("|" + expected).casefold()) or low.endswith(("$0$" + expected).casefold())


def layer_tokens(layer):
    """LayerTokens, cs:255-278: maximal runs of letters or digits, upper-cased."""
    if layer is None or not layer.strip():
        return []
    tokens, current = [], []
    for ch in layer:
        if ch.isalnum():
            current.append(ch)
        elif current:
            tokens.append("".join(current).upper())
            current = []
    if current:
        tokens.append("".join(current).upper())
    return tokens


def _has_any(tokens, expected):
    upper = {e.upper() for e in expected}
    return any(t.upper() in upper for t in tokens)


def is_setback_layer(layer):
    """IsSetbackLayer, cs:221-235."""
    if any(_equals_layer(layer, name) for name in SETBACK_LAYERS):
        return True
    tokens = layer_tokens(layer)
    if not tokens:
        return False
    return _has_any(tokens, ("SETBACK", "SBCK")) or any(
        t.upper().endswith(s) for t in tokens for s in ("SETBACK", "SBCK"))


def is_fence_layer(layer):
    """IsFenceLayer, cs:194-219. The generated fence mesh layer is never a fence source."""
    if _equals_layer(layer, LAYER_FENCE_MESH):
        return False
    if (_equals_layer(layer, FENCE_VENDOR_SITE) or _equals_layer(layer, FENCE_THIRD_PARTY)
            or (layer is not None and layer.strip() != ""
                and layer.casefold().startswith(FENCE_SHADING.casefold()))):
        return True
    if is_setback_layer(layer):
        return False
    tokens = layer_tokens(layer)
    if not tokens:
        return False
    if _has_any(tokens, FENCE_REJECT_TOKENS):
        return False
    if _has_any(tokens, FENCE_TOKENS):
        return True
    return _has_any(tokens, ("CHAIN",)) and _has_any(tokens, ("LINK",))


def _woodland_token(normalized):
    return bool(normalized.strip()) and any(t in normalized for t in WOODLAND_TOKENS)


def is_woodland_boundary_layer(layer):
    """IsCivilWoodlandBoundaryLayer, VegetationMass.cs:738-747."""
    n = (layer or "").upper()
    if not _woodland_token(n):
        return False
    return "-TXT" not in n and "-TEXT" not in n and "-GRID" not in n and "LIMIT" not in n


def is_woodland_height_layer(layer):
    """IsCivilWoodlandHeightLayer, VegetationMass.cs:749-754."""
    n = (layer or "").upper()
    return _woodland_token(n) and ("-TXT" in n or "-TEXT" in n)


# ---------------------------------------------------------------------------
#  CivilDrawingTraversal.cs
# ---------------------------------------------------------------------------

LINEAR_POLYLINES = ("polyline", "polyline2d", "polyline3d")


def linear_vertices_3d(ent, require_closed=False):
    """TryReadLinearVertices3d, cs:74-143. Returns (vertices [(x, y, z)], closed,
    has_source_z) or None where the plugin returns false. A source Z is any vertex with
    |z| > 1e-6 (cs:239-243)."""
    kind = ent.get("type")
    closed = False
    if kind in LINEAR_POLYLINES:
        closed = bool(ent.get("closed", False))
        if require_closed and not closed:
            return None
        elevation = _num(ent.get("elevation", 0.0), "polyline elevation")
        raw = ent.get("vertices") or []
    elif require_closed:
        return None
    elif kind in ("line", "curve"):
        raw = ent.get("vertices") or []
        if kind == "line" and len(raw) != 2:
            raise ReportInputError("a line has exactly two points")
        elevation = 0.0
    else:
        return None
    verts, has_z = [], False
    for p in raw:
        q = _xy(p, "vertex")
        z = q[2] if len(q) == 3 else elevation
        if abs(z) > SOURCE_Z_EPS:
            has_z = True
        verts.append((q[0], q[1], z))
    if len(verts) < (3 if require_closed else 2):
        return None
    return verts, closed, has_z


def linear_vertices_2d(ent, require_closed=False):
    """TryReadLinearVertices, cs:47-72."""
    read = linear_vertices_3d(ent, require_closed)
    if read is None:
        return None
    verts, closed, _ = read
    out = [(x, y) for x, y, _ in verts]
    return (out, closed) if len(out) >= (3 if require_closed else 2) else None


def _layer(ent):
    return ent.get("layer") or ""


# ---------------------------------------------------------------------------
#  FenceSurface.cs
# ---------------------------------------------------------------------------

def _dist3(a, b):
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def normalize_fence_vertices(vertices, closed):
    """NormalizeVertices, FenceSurface.cs:371-391 (3D distance). A repeated first vertex is
    dropped and counts as closed for the return test only: C# passes `closed` by value, so
    the run keeps the entity's own flag (cs:265, :288)."""
    out = []
    for p in vertices:
        if out and _dist3(out[-1], p) <= VERTEX_MERGE_EPS:
            continue
        out.append(p)
    if len(out) > 1 and _dist3(out[-1], out[0]) <= VERTEX_MERGE_EPS:
        out.pop()
        closed = True
    return out if (closed or len(out) >= 2) else []


def _count_fence(counts, has_z, has_topo):
    """The per-fence tally shared by Audit (cs:140-146) and ReadFenceRuns (cs:254-263)."""
    counts["source_fences"] += 1
    if has_z:
        counts["source_3d"] += 1
    else:
        counts["flat"] += 1
        if has_topo:
            counts["drapable_flat"] += 1


def _fence_counts(has_topo, meters_per_unit):
    return {"source_fences": 0, "source_3d": 0, "flat": 0, "drapable_flat": 0, "mesh_faces": 0,
            "skipped_segments": 0, "has_topo": bool(has_topo), "meters_per_unit": meters_per_unit}


def fence_audit(entities, terrain_z, meters_per_unit):
    """FenceSurface.Audit, cs:113-154: every fence-layer linear entity with 2 or more
    vertices counts, split by source Z and drapeability; mesh faces are the 3DFACEs on the
    generated fence mesh layer. Read only."""
    mpu = _mpu(meters_per_unit)
    result = _fence_counts(terrain_z is not None, mpu)
    for ent in _entities(entities):
        if not is_fence_layer(_layer(ent)):
            continue
        read = linear_vertices_3d(ent, False)
        if read is None or len(read[0]) < 2:
            continue
        _count_fence(result, read[2], result["has_topo"])
    result["mesh_faces"] = sum(1 for ent in entities
                               if ent["type"] == "face" and _equals_exact(_layer(ent), LAYER_FENCE_MESH))
    return result


def _equals_exact(layer, expected):
    """string.Equals(..., OrdinalIgnoreCase), no xref tail (FenceSurface.cs:420, :446)."""
    return layer.casefold() == expected.casefold()


def fence_audit_status_line(audit):
    """FenceSurfaceAudit.FormatStatusLine (cs:30-37) plus the command's scale (cs:488-491)."""
    drape = (f"{audit['drapable_flat']} flat drapeable to LEAFTOPO" if audit["has_topo"]
             else "no LEAFTOPO for flat fence drape")
    return (f"LEAFFENCE3DAUDIT: Fence 3D: {audit['source_3d']} source-3D + {audit['flat']} flat ({drape}); "
            f"{audit['mesh_faces']} generated mesh face(s). scale={format_g6(audit['meters_per_unit'])} m/unit.")


def fence_mesh_build(entities, terrain_z, meters_per_unit, height_m=DEFAULT_FENCE_HEIGHT_M,
                     width_m=DEFAULT_FENCE_WIDTH_M):
    """FenceSurface.BuildMeshes, cs:182-228, with ReadFenceRuns (cs:230-294) and
    AppendFenceMesh (cs:331-369). Returns the counts, `erased` (every model-space entity on
    the fence mesh layer, which the wipe removes first, cs:404-426) and `faces` (one
    four-corner face per segment whose two base Z values are finite). A flat fence takes its
    Z from the terrain (NaN outside it, so its segments are skipped). The width is the ray-hit
    width LEAFSHADE reads; the mesh does not use it (cs:182-228)."""
    mpu = _mpu(meters_per_unit)
    height_m = _num(height_m, "fence height")
    _num(width_m, "fence width")
    result = _fence_counts(terrain_z is not None, mpu)
    result.update(runs=0, segments=0, erased=0, faces=[])
    height_du = max(0.0, height_m) / mpu                                     # cs:195
    if height_du <= 0.0:                                                     # cs:196
        return result
    ents = _entities(entities)
    result["erased"] = sum(1 for ent in ents if _equals_exact(_layer(ent), LAYER_FENCE_MESH))
    runs = []
    for ent in ents:
        if not is_fence_layer(_layer(ent)):
            continue
        read = linear_vertices_3d(ent, False)
        if read is None or len(read[0]) < 2:
            continue
        raw, closed, has_z = read
        _count_fence(result, has_z, result["has_topo"])
        verts = normalize_fence_vertices(raw, closed)
        if len(verts) < 2:
            continue
        resolved = []
        for x, y, z in verts:
            if not has_z:
                tz = None if terrain_z is None else terrain_z(x, y)
                z = tz / mpu if tz is not None else math.nan                  # cs:275-279
            resolved.append((x, y, z))
        runs.append((resolved, closed))
    result["runs"] = len(runs)
    for verts, closed in runs:
        count = len(verts) if closed else len(verts) - 1
        for i in range(count):
            a = verts[i]
            b = verts[(i + 1) % len(verts)]
            if not (math.isfinite(a[2]) and math.isfinite(b[2])):
                result["skipped_segments"] += 1
                continue
            result["faces"].append((a, b, (b[0], b[1], b[2] + height_du), (a[0], a[1], a[2] + height_du)))
            result["segments"] += 1
    result["mesh_faces"] = result["segments"]                                # cs:223
    return result


def fence_mesh_message(result):
    """LEAFFENCEMESHFROMCIVIL's report, FenceSurface.cs:528-535."""
    drape = (f"({result['drapable_flat']} draped to LEAFTOPO)" if result["has_topo"]
             else "(no LEAFTOPO drape)")
    skipped = f"; skipped {result['skipped_segments']} segment(s)" if result["skipped_segments"] > 0 else ""
    return (f"LEAFFENCEMESHFROMCIVIL: {result['source_fences']} recognized fence linework item(s); "
            f"{result['source_3d']} source-3D, {result['flat']} flat {drape}; wrote {result['mesh_faces']} "
            f"mesh face(s) on {LAYER_FENCE_MESH}{skipped}.")


# ---------------------------------------------------------------------------
#  VegetationMass.cs
# ---------------------------------------------------------------------------

def parse_height_feet(text):
    """TryParseHeightFeet, cs:704-718: the first "<number>'" in the text, positive. A digit
    run .NET's invariant parser would refuse (a non-ASCII digit) is refused here too."""
    if text is None or not text.strip():
        return None
    match = HEIGHT_FEET.search(text)
    if match is None:
        return None
    raw = match.group("feet")
    if not raw.isascii():
        return None
    value = float(raw)
    return value if value > 0.0 else None


def normalize_closed_vertices(vertices, closed):
    """NormalizeClosedVertices, cs:679-702 (2D distance)."""
    out = []
    for p in vertices:
        if out and math.hypot(out[-1][0] - p[0], out[-1][1] - p[1]) <= VERTEX_MERGE_EPS:
            continue
        out.append(p)
    if len(out) > 1 and math.hypot(out[-1][0] - out[0][0], out[-1][1] - out[0][1]) <= VERTEX_MERGE_EPS:
        out.pop()
        closed = True
    return out if (closed or len(out) >= 3) else []


def _bounds(vertices):
    xs = [p[0] for p in vertices]
    ys = [p[1] for p in vertices]
    return (min(xs), min(ys), max(xs), max(ys))


def _in_bounds(b, p):
    return b[0] <= p[0] <= b[2] and b[1] <= p[1] <= b[3]


def signed_area(vertices):
    """SignedArea, cs:1455-1466."""
    if len(vertices) < 3:
        return 0.0
    area = 0.0
    for i in range(len(vertices)):
        a = vertices[i]
        b = vertices[(i + 1) % len(vertices)]
        area += a[0] * b[1] - b[0] * a[1]
    return area * 0.5


def point_in_polygon(vertices, p):
    """PointInPolygon, cs:1468-1486 (even-odd, a horizontal edge divides by 1e-12)."""
    inside = False
    n = len(vertices)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        pi, pj = vertices[i], vertices[j]
        dy = pj[1] - pi[1]
        if ((pi[1] > p[1]) != (pj[1] > p[1])) and (
                p[0] < (pj[0] - pi[0]) * (p[1] - pi[1]) / (1e-12 if dy == 0.0 else dy) + pi[0]):
            inside = not inside
        j = i
    return inside


def polygon_centroid(vertices):
    """PolygonCentroid, cs:1246-1260: the vertex average."""
    if not vertices:
        return (0.0, 0.0)
    x = y = 0.0
    for p in vertices:
        x += p[0]
        y += p[1]
    return (x / len(vertices), y / len(vertices))


def distance_to_boundary(vertices, p):
    """DistanceToBoundary, cs:896-924: (distance, nearest point) over the closed ring."""
    best_sq = math.inf
    proj = vertices[0]
    n = len(vertices)
    for i in range(n):
        a = vertices[i]
        b = vertices[(i + 1) % n]
        abx, aby = b[0] - a[0], b[1] - a[1]
        len_sq = abx * abx + aby * aby
        t = ((p[0] - a[0]) * abx + (p[1] - a[1]) * aby) / len_sq if len_sq > 1e-12 else 0.0
        t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
        qx, qy = a[0] + abx * t, a[1] + aby * t
        d_sq = (p[0] - qx) * (p[0] - qx) + (p[1] - qy) * (p[1] - qy)
        if d_sq < best_sq:
            best_sq = d_sq
            proj = (qx, qy)
    return math.sqrt(best_sq), proj


class _RegionIndex:
    """RegionSpatialIndex, cs:112-210: a uniform grid of about sqrt(n) cells per axis; a
    region spanning more than 256 cells is a large region every query returns last."""

    def __init__(self, regions):
        self.cells, self.large = {}, []
        if not regions:
            self.min_x = self.min_y = 0.0
            self.cell = 1.0
            return
        min_x = min(r["bounds"][0] for r in regions)
        min_y = min(r["bounds"][1] for r in regions)
        max_x = max(r["bounds"][2] for r in regions)
        max_y = max(r["bounds"][3] for r in regions)
        self.min_x, self.min_y = min_x, min_y
        width = max(1.0, max_x - min_x)
        height = max(1.0, max_y - min_y)
        self.cell = max(1.0, max(width, height) / max(1.0, math.sqrt(len(regions))))
        for region in regions:
            self._add(region)

    def _cx(self, x):
        return math.floor((x - self.min_x) / self.cell)

    def _cy(self, y):
        return math.floor((y - self.min_y) / self.cell)

    def _add(self, region):
        b = region["bounds"]
        ix0, ix1, iy0, iy1 = self._cx(b[0]), self._cx(b[2]), self._cy(b[1]), self._cy(b[3])
        count = (ix1 - ix0 + 1) * (iy1 - iy0 + 1)
        if count <= 0 or count > MAX_REGION_CELLS:
            self.large.append(region)
            return
        for ix in range(ix0, ix1 + 1):
            for iy in range(iy0, iy1 + 1):
                self.cells.setdefault((ix, iy), []).append(region)

    def query(self, p):
        return self.cells.get((self._cx(p[0]), self._cy(p[1])), []) + self.large


def assign_samples_to_regions(regions, samples, snap_tolerance_du):
    """AssignSamplesToRegions, cs:806-894: each label goes to the smallest-area region that
    contains it; a label inside none snaps half a unit inside the nearest boundary within the
    tolerance. Mutates the regions' `samples` and a snapped sample's point; returns the
    snapped count."""
    index = _RegionIndex(regions)
    unassigned = []
    for sample in samples:
        best, best_area = None, math.inf
        for region in index.query(sample["point"]):
            if not _in_bounds(region["bounds"], sample["point"]):
                continue
            if not point_in_polygon(region["vertices"], sample["point"]):
                continue
            if region["area"] >= best_area:
                continue
            best, best_area = region, region["area"]
        if best is not None:
            best["samples"].append(sample)
        else:
            unassigned.append(sample)
    snapped = 0
    if snap_tolerance_du <= 0.0:
        return snapped
    tol = snap_tolerance_du
    for sample in unassigned:
        best, best_dist, best_proj = None, tol, (0.0, 0.0)
        px, py = sample["point"]
        for region in regions:
            b = region["bounds"]
            if px < b[0] - tol or px > b[2] + tol or py < b[1] - tol or py > b[3] + tol:
                continue
            dist, proj = distance_to_boundary(region["vertices"], sample["point"])
            if dist >= best_dist:
                continue
            best, best_dist, best_proj = region, dist, proj
        if best is None:
            continue
        cx, cy = polygon_centroid(best["vertices"])
        dx, dy = cx - best_proj[0], cy - best_proj[1]
        length = math.sqrt(dx * dx + dy * dy)
        nudge = min(0.5, length)
        sample["point"] = ((best_proj[0] + dx / length * nudge, best_proj[1] + dy / length * nudge)
                           if length > 1e-9 else best_proj)
        best["samples"].append(sample)
        snapped += 1
    return snapped


def _choose_mesh_grid(bounds, sample_count):
    """ChooseMeshGrid, cs:1187-1210."""
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    max_side = max(width, height)
    if max_side <= 1e-6:
        return 0, 0
    target = min(MAX_INTERPOLATED_MESH_AXIS, max(10, 12 + sample_count * 3))
    cols = max(3, math.ceil(target * (width / max_side)))
    rows = max(3, math.ceil(target * (height / max_side)))
    return cols, rows


def vegetation_mesh_face_count(vertices, height_du, terrain_z, meters_per_unit, sample_count):
    """The faces AppendVegetationMesh writes (cs:944-998): one side face per edge, then the
    interpolated top (cs:1048-1117, a cell whose four grid nodes are inside the polygon) or,
    when that is empty, the fan (cs:1119-1158, one face per vertex). None without terrain, a
    positive height or terrain under the vertex centroid. Every top point has a value (the
    centroid's terrain Z is the fallback), so the count needs no Z."""
    if len(vertices) < 3 or terrain_z is None or height_du <= 0.0:
        return 0
    _num(meters_per_unit, "meters_per_unit")   # scales Z only (cs:956), which no count reads
    centroid = polygon_centroid(vertices)
    if terrain_z(centroid[0], centroid[1]) is None:
        return 0
    sides = len(vertices)
    b = _bounds(vertices)
    cols, rows = _choose_mesh_grid(b, sample_count)
    top = 0
    if cols >= 2 and rows >= 2:
        dx = (b[2] - b[0]) / (cols - 1)
        dy = (b[3] - b[1]) / (rows - 1)
        inside = [[point_in_polygon(vertices, (b[0] + dx * c, b[1] + dy * r)) for c in range(cols)]
                  for r in range(rows)]
        for r in range(rows - 1):
            for c in range(cols - 1):
                if inside[r][c] and inside[r][c + 1] and inside[r + 1][c + 1] and inside[r + 1][c]:
                    top += 1
    if top == 0:
        top = len(vertices)
    return sides + top


def vegetation_import(entities, terrain_z, meters_per_unit, create_restrictions=True):
    """ImportCivilWoodlandHeights, cs:212-344: woodland boundary polygons and height labels
    from the civil layers, labels assigned (and orphans snapped), one vegetation mass per
    labelled polygon at its tallest label, its mesh face count, and a restriction outline
    when asked. Returns the counts, the warnings in the plugin's order, `masses` (outline,
    height in metres) and `restrictions` (outlines)."""
    mpu = _mpu(meters_per_unit)
    regions, samples = [], []
    for ent in _entities(entities):
        layer = _layer(ent)
        if is_woodland_boundary_layer(layer):
            read = linear_vertices_2d(ent, False)
            if read is not None:
                verts = normalize_closed_vertices(read[0], read[1])
                if len(verts) < 3:
                    continue
                regions.append({"vertices": verts, "bounds": _bounds(verts), "layer": layer,
                                "area": abs(signed_area(verts)), "samples": []})
                continue
        if is_woodland_height_layer(layer) and ent["type"] in ("text", "mtext"):
            feet = parse_height_feet(ent.get("text"))
            if feet is None:
                continue
            pos = _xy(ent.get("position"), "label position")
            samples.append({"point": (pos[0], pos[1]), "feet": feet})
    if len(regions) > MAX_REGIONS or len(samples) > MAX_SAMPLES:
        raise ReportBoundsError(f"{len(regions)} regions or {len(samples)} labels exceed "
                                f"{MAX_REGIONS} or {MAX_SAMPLES}")
    result = {"source_regions": len(regions), "height_labels": len(samples), "imported_regions": 0,
              "mesh_faces": 0, "restriction_regions": 0, "skipped_regions": 0, "snapped_labels": 0,
              "meters_per_unit": mpu, "warnings": [], "masses": [], "restrictions": []}
    tol = (ORPHAN_SNAP_TOLERANCE_FEET * FEET_TO_METERS) / mpu                   # cs:271-272
    result["snapped_labels"] = assign_samples_to_regions(regions, samples, tol)
    for region in regions:
        if not region["samples"]:
            result["skipped_regions"] += 1
            continue
        height_feet = max(s["feet"] for s in region["samples"])
        height_m = height_feet * FEET_TO_METERS
        height_du = height_m / mpu
        result["mesh_faces"] += vegetation_mesh_face_count(region["vertices"], height_du, terrain_z, mpu,
                                                           len(region["samples"]))
        result["masses"].append({"vertices": region["vertices"], "height_m": height_m,
                                 "source": "civil:" + region["layer"], "labels": len(region["samples"])})
        result["imported_regions"] += 1
        if create_restrictions:
            result["restrictions"].append(region["vertices"])
            result["restriction_regions"] += 1
    if result["source_regions"] == 0:                                            # cs:336-341
        result["warnings"].append("No civil woodland boundary polylines found.")
    if result["height_labels"] == 0:
        result["warnings"].append("No civil woodland height labels found.")
    if result["imported_regions"] == 0 and result["source_regions"] > 0:
        result["warnings"].append("No woodland regions contained height labels.")
    return result


def vegetation_status_record(result):
    """The last-import record SaveLastImportResult writes (cs:369-379), neutral field names,
    without its timestamp (the host's clock)."""
    return {"schema": IMPORT_RECORD_SCHEMA,
            "source_regions": result["source_regions"], "height_labels": result["height_labels"],
            "imported_regions": result["imported_regions"], "mesh_faces": result["mesh_faces"],
            "restriction_regions": result["restriction_regions"],
            "skipped_regions": result["skipped_regions"], "snapped_labels": result["snapped_labels"],
            "meters_per_unit": result["meters_per_unit"]}


def vegetation_message(result):
    """LEAFVEGETATIONFROMCIVIL's report, LeafShadingObjectCommand.cs:133-147."""
    text = (f"LEAFVEGETATIONFROMCIVIL: imported {result['imported_regions']} vegetation mass region(s) "
            f"from {result['source_regions']} civil woodland polygon(s) and {result['height_labels']} "
            f"height label(s); wrote {result['mesh_faces']} mesh face(s).")
    if result["imported_regions"] > 0 and result["mesh_faces"] == 0:
        text += " No 3D vegetation mesh faces were created; check LEAFTOPO coverage."
    if result["restriction_regions"] > 0:
        text += f" Added {result['restriction_regions']} restriction region(s)."
    if result["skipped_regions"] > 0:
        text += f" Skipped {result['skipped_regions']} polygon(s) with no height labels."
    text += f" Drawing scale={format_g6(result['meters_per_unit'])} m/unit."
    return [text] + ["  " + w for w in result["warnings"]]


# ---------------------------------------------------------------------------
#  System.Random, seeded (Net5CompatSeedImpl)
# ---------------------------------------------------------------------------

class NetRandom:
    """new System.Random(seed): Knuth's subtractive generator as .NET seeds it."""

    MBIG = 2147483647
    MSEED = 161803398

    def __init__(self, seed):
        seed = _int(seed, "seed")
        if not -2147483648 <= seed <= 2147483647:
            raise ReportInputError("seed must be a 32-bit integer")
        s = [0] * 56
        subtraction = self.MBIG if seed == -2147483648 else abs(seed)
        mj = self.MSEED - subtraction
        s[55] = mj
        mk = 1
        ii = 0
        for _ in range(1, 55):
            ii += 21
            if ii >= 55:
                ii -= 55
            s[ii] = mk
            mk = mj - mk
            if mk < 0:
                mk += self.MBIG
            mj = s[ii]
        for _ in range(1, 5):
            for i in range(1, 56):
                n = i + 30
                if n >= 55:
                    n -= 55
                s[i] -= s[1 + n]
                if s[i] < 0:
                    s[i] += self.MBIG
        self._s, self._inext, self._inextp = s, 0, 21

    def _internal_sample(self):
        inext = self._inext + 1
        if inext >= 56:
            inext = 1
        inextp = self._inextp + 1
        if inextp >= 56:
            inextp = 1
        ret = self._s[inext] - self._s[inextp]
        if ret == self.MBIG:
            ret -= 1
        if ret < 0:
            ret += self.MBIG
        self._s[inext] = ret
        self._inext, self._inextp = inext, inextp
        return ret

    def next(self, max_value):
        """Next(maxValue): (int)(Sample() * maxValue)."""
        return int(self._internal_sample() * (1.0 / self.MBIG) * max_value)


# ---------------------------------------------------------------------------
#  LeafMeshDiffViewCommand.cs
# ---------------------------------------------------------------------------

class _PanelReadResult:
    """PanelReadResult, cs:877-933: every candidate counted, those inside the terrain kept,
    a reservoir sample past MaxPanelSamples."""

    def __init__(self, max_samples):
        self.max_samples = max_samples
        self.random = None                      # new Random(1107), made on first need
        self.total = 0
        self.skipped_outside = 0
        self.sampled = []

    def try_add(self, x, y, authored_z, preserve_authored_z, terrain_z, mpu):
        self.total += 1
        tz = terrain_z(x, y)
        if tz is None:
            self.skipped_outside += 1
            return
        topo_z = tz / mpu
        panel_z = authored_z if preserve_authored_z else topo_z
        diff_du = panel_z - topo_z
        point = [x, y, topo_z, panel_z, diff_du, diff_du * mpu]
        if len(self.sampled) < self.max_samples:
            self.sampled.append(point)
            return
        if self.random is None:
            self.random = NetRandom(PANEL_SAMPLE_SEED)
        slot = self.random.next(max(1, self.total))
        if slot < self.max_samples:
            self.sampled[slot] = point


def sample_indices(count, max_samples):
    """SampleIndices, cs:813-825: every step-th index plus the last."""
    if count <= 0:
        return []
    step = max(1, math.ceil(count / float(max(1, max_samples))))
    out = list(range(0, count, step))
    if out[-1] != count - 1:
        out.append(count - 1)
    return out


def _median(values):
    """Median, cs:835-842 (sorts in place)."""
    if not values:
        return 0.0
    values.sort()
    mid = len(values) // 2
    return values[mid] if len(values) % 2 == 1 else (values[mid - 1] + values[mid]) * 0.5


def _percentile(values, p):
    """Percentile, cs:844-854 (linear between ranks; sorts in place)."""
    if not values:
        return 0.0
    values.sort()
    raw = max(0.0, min(1.0, p)) * (len(values) - 1)
    lo, hi = math.floor(raw), math.ceil(raw)
    if lo == hi:
        return values[lo]
    t = raw - lo
    return values[lo] * (1.0 - t) + values[hi] * t


def _polyline_centre(ent):
    """AddPolylineCenter, cs:688-712: the vertex average, the polyline's elevation as its
    authored Z (kept when it is not zero)."""
    verts = [_xy(p, "vertex") for p in ent.get("vertices") or []]
    cx = cy = 0.0
    for p in verts:
        cx += p[0]
        cy += p[1]
    cx /= len(verts)
    cy /= len(verts)
    elevation = _num(ent.get("elevation", 0.0), "polyline elevation")
    return cx, cy, elevation, abs(elevation) > AUTHORED_Z_EPS


def mesh_diff(entities, terrain_z, grid_rows, grid_cols, meters_per_unit, module_layer=""):
    """MeshDiffDataBuilder.Build, cs:465-502, over ReadPanelDiffPoints (cs:567-679): the
    panel/design points are the third-party tracker blocks' panels, else the module-layer
    closed polylines' centres, else the LEAF-TRACKERS closed polylines' centres, each
    sampled against the terrain. Returns the counts the command prints and reports, the
    datum alignment and the residual statistics. A third-party block carrying column
    metadata needs its panel enumeration, which this port does not carry: it refuses."""
    mpu = _mpu(meters_per_unit)
    if terrain_z is None:
        return {"succeeded": False, "status": "no-terrain"}                   # cs:46-54
    rows_n = _int(grid_rows, "grid rows")
    cols_n = _int(grid_cols, "grid cols")
    if not (0 <= rows_n <= MAX_GRID_AXIS and 0 <= cols_n <= MAX_GRID_AXIS):
        raise ReportBoundsError(f"grid axes over {MAX_GRID_AXIS}")
    if not isinstance(module_layer, str):
        raise ReportInputError("module_layer must be a string")
    ents = _entities(entities)
    # BuildTopoWire, cs:504-565: a node per sampled (row, col), a line to each sampled neighbour.
    r_s = len(sample_indices(rows_n, MAX_TOPO_AXIS_SAMPLES))
    c_s = len(sample_indices(cols_n, MAX_TOPO_AXIS_SAMPLES))
    topo_points = r_s * c_s
    topo_lines = r_s * max(0, c_s - 1) + max(0, r_s - 1) * c_s
    third_party = _PanelReadResult(MAX_PANEL_SAMPLES)
    module = _PanelReadResult(MAX_PANEL_SAMPLES)
    tracker = _PanelReadResult(MAX_PANEL_SAMPLES)
    scanned = blocks = blocks_no_metadata = 0
    for ent in ents:
        scanned += 1
        layer = _layer(ent)
        if ent["type"] == "block" and _equals_exact(layer, LAYER_THIRD_PARTY_TRACKERS):
            blocks += 1
            if _int(ent.get("columns", 0), "block columns") <= 0:            # cs:623-627
                blocks_no_metadata += 1
                continue
            raise ReportInputError("a third-party tracker block with row metadata needs panel "
                                   "enumeration, which this port does not carry")
        if ent["type"] not in ("polyline",) or not ent.get("closed") or len(ent.get("vertices") or []) < 3:
            continue                                                          # cs:653-655
        if ((module_layer.strip() and _equals_exact(layer, module_layer))
                or _equals_exact(layer, MODULE_LAYER_FALLBACK)):              # cs:714-720
            module.try_add(*_polyline_centre(ent), terrain_z, mpu)
            continue
        if _equals_exact(layer, LAYER_TRACKERS):
            tracker.try_add(*_polyline_centre(ent), terrain_z, mpu)
    if third_party.sampled:                                                   # cs:478-482
        source, selected = "third-party", third_party
    elif module.sampled:
        source, selected = "module", module
    else:
        source, selected = "tracker", tracker
    points = [list(p) for p in selected.sampled]
    out = {"succeeded": bool(points), "status": "ok" if points else "no-points",
           "source": source, "source_label": SOURCE_LABELS[source],
           "panel_points": len(points), "raw_candidates": selected.total,
           "skipped_outside_topo": selected.skipped_outside, "scanned_entities": scanned,
           "third_party_block_references": blocks, "third_party_blocks_without_metadata": blocks_no_metadata,
           "third_party_definitions_built": 0, "third_party_definition_cache_hits": 0,
           "third_party_definitions_empty": 0, "topo_points": topo_points, "topo_lines": topo_lines,
           "datum_shift_applied": False, "raw_median_clearance_m": 0.0, "vertical_datum_shift_m": 0.0,
           "min_diff_m": 0.0, "median_diff_m": 0.0, "max_diff_m": 0.0, "color_scale_m": 1.0}
    if not points:
        return out
    # AlignDatum, cs:722-747.
    median_du = _median([p[4] for p in points])
    out["raw_median_clearance_m"] = median_du * mpu
    if abs(out["raw_median_clearance_m"]) > MAX_EXPECTED_TRACKER_CLEARANCE_M:
        shift_du = -median_du
        out["vertical_datum_shift_m"] = shift_du * mpu
        out["datum_shift_applied"] = True
        for p in points:
            p[3] += shift_du
            p[4] += shift_du
            p[5] = p[4] * mpu
    # ComputeStats, cs:749-769.
    diffs = [p[5] for p in points]
    out["min_diff_m"] = min(diffs)
    out["max_diff_m"] = max(diffs)
    abs_diffs = [abs(d) for d in diffs]
    out["median_diff_m"] = _median(diffs)
    out["color_scale_m"] = max(0.25, _percentile(abs_diffs, 0.95))
    return out


def mesh_diff_message(result):
    """The command's success line, cs:106-109 (its elapsed seconds are the host's), and the
    scan line (MeshDiffData.FormatScanLine, cs:974-982)."""
    scan = (f"Scanned {result['scanned_entities']:,} modelspace entity/entities; "
            f"{result['third_party_block_references']:,} PVcase block reference(s), "
            f"{result['third_party_blocks_without_metadata']:,} skipped without PVcase column metadata, "
            f"{result['third_party_definitions_built']:,} block definition(s) built, "
            f"{result['third_party_definition_cache_hits']:,} cache hit(s), "
            f"{result['third_party_definitions_empty']:,} empty definition(s).")
    return (f"LEAFMESHDIFFVIEW: showing {result['panel_points']:,} sampled panel/design point(s) from "
            f"{result['source_label']} against LEAFTOPO", scan)


# ---------------------------------------------------------------------------
#  SunPositionCalculator.cs, ShadeCalculator.cs, ShadeTableGenerator.cs
# ---------------------------------------------------------------------------

def sun_position(lat_deg, lon_deg, day_of_year, utc_hours):
    """SunPositionCalculator.Calculate, cs:43-136: (azimuth deg, elevation deg, is night).
    Spencer (1971) declination and equation of time, the Blanco azimuth."""
    b = (2.0 * math.pi / 365.0) * (day_of_year - 1)
    eqt_min = 229.18 * (0.000075
                        + 0.001868 * math.cos(b)
                        - 0.032077 * math.sin(b)
                        - 0.014615 * math.cos(2.0 * b)
                        - 0.040890 * math.sin(2.0 * b))
    decl = (0.006918
            - 0.399912 * math.cos(b)
            + 0.070257 * math.sin(b)
            - 0.006758 * math.cos(2.0 * b)
            + 0.000907 * math.sin(2.0 * b)
            - 0.002697 * math.cos(3.0 * b)
            + 0.001480 * math.sin(3.0 * b))
    solar_hours = utc_hours + lon_deg / 15.0 + eqt_min / 60.0
    hour_angle = (solar_hours - 12.0) * 15.0 * DEG_TO_RAD
    lat = lat_deg * DEG_TO_RAD
    sin_elev = math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(hour_angle)
    sin_elev = max(-1.0, min(1.0, sin_elev))
    elev_rad = math.asin(sin_elev)
    elev_deg = elev_rad * RAD_TO_DEG
    if elev_deg <= 0.0:
        return 0.0, elev_deg, True
    cos_az = (math.sin(decl) - math.sin(lat) * sin_elev) / (math.cos(elev_rad) * math.cos(lat))
    cos_az = max(-1.0, min(1.0, cos_az))
    az = math.acos(cos_az) * RAD_TO_DEG
    if hour_angle > 0.0:
        az = 360.0 - az
    return az, elev_deg, False


def clear_sky_weight(elevation_deg, is_night):
    """ShadeTableGenerator.ClearSkyIrradianceWeight, cs:188-196."""
    if is_night or elevation_deg <= 0.0:
        return 0.0
    w = math.sin(elevation_deg * math.pi / 180.0)
    return w if w > 0.0 else 0.0


def _centre(row):
    return ((row.axis_start[0] + row.axis_end[0]) * 0.5, (row.axis_start[1] + row.axis_end[1]) * 0.5)


def shade_fraction(cast_centre, recv_centre, sun, module_height_m):
    """ShadeCalculator.ComputeShadeFraction, cs:35-103, over the two rows' axis centres."""
    az, elev, night = sun
    if night or elev <= 0.0 or module_height_m <= 0.0:
        return 0.0
    dx = recv_centre[0] - cast_centre[0]
    dy = recv_centre[1] - cast_centre[1]
    dist = math.sqrt(dx * dx + dy * dy)
    if dist < SAME_POSITION_EPS:
        return 0.0
    hx, hy = dx / dist, dy / dist
    az_rad = az * DEG_TO_RAD
    projection = -math.sin(az_rad) * hx + -math.cos(az_rad) * hy
    if projection <= 0.0:
        return 0.0
    shadow_total = module_height_m / math.tan(elev * DEG_TO_RAD)
    return min(1.0, max(0.0, shadow_total * projection / dist))


def _daylight_hours(lat_deg, lon_deg):
    """The year's non-night hours as (weight, azimuth, elevation), cs:111-121. The
    simulation starts 1 January 00:00 UTC, so hour h falls on day h // 24 + 1 at h % 24."""
    out = []
    for h in range(HOURS_PER_YEAR):
        az, elev, night = sun_position(lat_deg, lon_deg, h // 24 + 1, float(h % 24))
        w = clear_sky_weight(elev, night)
        if night or w <= 0.0:
            continue
        out.append((w, az, elev))
    return out


def shade_table_literal(rows, lat_deg, lon_deg, module_height_m, hours=None):
    """ShadeTableGenerator.Generate, cs:79-186, the pair loop as written (per-row hourly
    arrays are not kept). Bounded at MAX_LITERAL_SHADE_ROWS rows. Returns the energy- and
    time-weighted annual losses in percent and each row's."""
    h_m = _num(module_height_m, "module height")
    if h_m <= 0.0:
        raise ReportInputError("moduleHeightM must be positive.")
    n = len(rows)
    if n > MAX_LITERAL_SHADE_ROWS:
        raise ReportBoundsError(f"{n} rows exceed the literal shade loop bound {MAX_LITERAL_SHADE_ROWS}")
    centres = [_centre(r) for r in rows]
    hours = _daylight_hours(lat_deg, lon_deg) if hours is None else hours
    tw_total = ew_total = w_total = 0.0
    row_tw, row_ew, row_w = [0.0] * n, [0.0] * n, [0.0] * n
    for w, az, elev in hours:
        sun = (az, elev, False)
        for recv in range(n):
            frac = 0.0
            for cast in range(n):
                if cast == recv:
                    continue
                frac = min(1.0, frac + shade_fraction(centres[cast], centres[recv], sun, h_m))
            row_tw[recv] += frac
            row_ew[recv] += frac * w
            row_w[recv] += w
            tw_total += frac
            ew_total += frac * w
            w_total += w
    day = len(hours)
    return {"annual_shade_loss": (ew_total / w_total) * 100.0 if w_total > 0.0 else 0.0,
            "time_weighted_loss": (tw_total / (day * n)) * 100.0 if day > 0 and n > 0 else 0.0,
            "rows": [{"energy_weighted": (row_ew[i] / row_w[i]) * 100.0 if row_w[i] > 0.0 else 0.0,
                      "time_weighted": (row_tw[i] / day) * 100.0 if day > 0 else 0.0} for i in range(n)]}


def _collinear_sides(centres):
    """For rows whose axis centres share one Y (every row of an east-west sweep over a
    rectangle): per receiver, the nearest caster distance and the sum of reciprocal distances
    on each side. dy is exactly 0.0, so dist = sqrt(dx*dx) = |dx| and hx = dx / dist is
    exactly +1 or -1; any other value returns None and the literal loop runs."""
    y0 = centres[0][1]
    if any(c[1] != y0 for c in centres):
        return None
    sides = []
    for i, (rx, ry) in enumerate(centres):
        east = [None, 0.0]    # casters with hx = +1 (receiver east of caster)
        west = [None, 0.0]    # casters with hx = -1
        for j, (cx, cy) in enumerate(centres):
            if j == i:
                continue
            dx = rx - cx
            dy = ry - cy
            dist = math.sqrt(dx * dx + dy * dy)
            if dist < SAME_POSITION_EPS:
                continue
            hx = dx / dist
            if dy / dist != 0.0:
                return None
            side = east if hx == 1.0 else (west if hx == -1.0 else None)
            if side is None:
                return None
            if side[0] is None or dist < side[0]:
                side[0] = dist
            side[1] += 1.0 / dist
        sides.append((east, west))
    return sides


def annual_shade_loss(rows, lat_deg, lon_deg, module_height_m, hours=None):
    """ShadeTableGenerator.Generate's AnnualShadeLoss (cs:171-173), the energy-weighted
    percent. Rows whose centres share one Y take the collinear path: the same hours, the
    same receivers in order, the same terms. There every caster on the shadow's side has
    projection |sin(az)| exactly, so its term is t / d with t = shadowTotal * |sin(az)|;
    the running capped sum reaches exactly 1.0 when t / d_nearest >= 1 (that term alone
    saturates it, and later terms keep it at 1), and otherwise equals min(1, t * sum(1/d)),
    the same terms summed in another order (a difference in the last bits, far under the
    two decimals the plugin prints). Anything else runs the literal pair loop, bounded."""
    h_m = _num(module_height_m, "module height")
    if h_m <= 0.0:
        raise ReportInputError("moduleHeightM must be positive.")
    if not rows:
        return 0.0
    if len(rows) > MAX_SHADE_ROWS:
        raise ReportBoundsError(f"{len(rows)} rows exceed {MAX_SHADE_ROWS}")
    centres = [_centre(r) for r in rows]
    sides = _collinear_sides(centres)
    hours = _daylight_hours(lat_deg, lon_deg) if hours is None else hours
    if sides is None:
        return shade_table_literal(rows, lat_deg, lon_deg, h_m, hours)["annual_shade_loss"]
    east = [tuple(s[0]) for s in sides]
    west = [tuple(s[1]) for s in sides]
    n = len(centres)
    ew_total = w_total = 0.0
    for w, az, elev in hours:
        az_rad = az * DEG_TO_RAD
        shadow_x = -math.sin(az_rad)
        shadow_total = h_m / math.tan(elev * DEG_TO_RAD)
        if shadow_x > 0.0:
            active, t = east, shadow_total * shadow_x
        elif shadow_x < 0.0:
            active, t = west, shadow_total * (shadow_x * -1.0)
        else:                                   # projection is +-0: every term is 0 (cs:84-85)
            for _ in range(n):
                w_total += w
            continue
        for d_min, inv_sum in active:           # receivers in index order (cs:124)
            if d_min is not None:               # no caster on the shadow's side adds only 0.0
                if t / d_min >= 1.0:
                    ew_total += w               # rowFraction 1.0
                else:
                    frac = t * inv_sum
                    ew_total += (1.0 if frac > 1.0 else frac) * w
            w_total += w
    return (ew_total / w_total) * 100.0 if w_total > 0.0 else 0.0


# ---------------------------------------------------------------------------
#  OptimalRowSpacingCalculator.cs, OptimalRowSpacingCommand.cs
# ---------------------------------------------------------------------------

def _sweep_rows(boundary_m, pitch_m, module):
    """TrackerRowGenerator.Generate at metersPerUnit 1, azimuth 0, no terrain
    (OptimalRowSpacingCalculator.cs:106-112), bounded before it runs."""
    xs = [p[0] for p in boundary_m]
    columns = (max(xs) - min(xs)) / (pitch_m * 0.1) + 1.0
    if columns > MAX_SWEEP_COLUMNS:
        raise ReportBoundsError(f"a pitch of {pitch_m} m sweeps {columns:.0f} columns, over {MAX_SWEEP_COLUMNS}")
    try:
        return _sweep.generate_tracker_rows([tuple(p) for p in boundary_m], pitch_m, module, 1.0, 0.0, None)
    except (TypeError, ValueError) as exc:
        raise ReportInputError(str(exc)) from None


def optimal_row_spacing(boundary_m, module, lat_deg, lon_deg, module_height_m, target_capture=0.99,
                        sweep_steps=10, pitch_min_multiplier=1.5, pitch_max_multiplier=3.0,
                        shade_loss=None):
    """OptimalRowSpacingCalculator.Calculate, cs:64-144 (the shade-table overload, cs:151-177,
    when `shade_loss` is None; otherwise `shade_loss(rows)` returns the percent, as the
    delegate overload takes). The smallest pitch whose capture meets the target, else the
    widest pitch."""
    if boundary_m is None:
        raise ReportInputError("boundary is null")
    if module is None:
        raise ReportInputError("module is null")
    target = _num(target_capture, "targetCapture")
    steps = _int(sweep_steps, "sweepSteps")
    lo = _num(pitch_min_multiplier, "pitchMinMultiplier")
    hi = _num(pitch_max_multiplier, "pitchMaxMultiplier")
    if target <= 0.0 or target >= 1.0:
        raise ReportInputError(f"targetCapture must be in (0, 1); got {target}.")
    if steps < 2:
        raise ReportInputError(f"sweepSteps must be >= 2; got {steps}.")
    if steps > MAX_SWEEP_STEPS:
        raise ReportBoundsError(f"{steps} sweep steps exceed {MAX_SWEEP_STEPS}")
    if lo <= 0.0:
        raise ReportInputError(f"pitchMinMultiplier must be > 0; got {lo}.")
    if lo >= hi:
        raise ReportInputError(f"pitchMinMultiplier ({lo}) must be < pitchMaxMultiplier ({hi}).")
    boundary = [_xy(p, "boundary vertex")[:2] for p in boundary_m]
    if shade_loss is None:
        lat = _num(lat_deg, "latitude")
        lon = _num(lon_deg, "longitude")
        height = _num(module_height_m, "module height")
        hours = _daylight_hours(lat, lon)          # the same sun for every pitch

        def shade_loss(rows):                       # cs:166-172
            if not rows:
                return 0.0
            return annual_shade_loss(rows, lat, lon, height, hours)
    cross = module.cross_axis_m
    min_pitch = lo * cross
    max_pitch = hi * cross
    step = (max_pitch - min_pitch) / (steps - 1)
    sweep, first_passing, widest = [], None, None
    for i in range(steps):
        pitch = min_pitch + step * i
        rows = _sweep_rows(boundary, pitch, module)
        loss = shade_loss(rows)
        capture = 1.0 - loss / 100.0
        entry = {"pitch_m": pitch, "gcr": cross / pitch, "annual_shade_loss_pct": loss,
                 "irradiance_fraction": capture, "row_count": len(rows)}
        sweep.append(entry)
        widest = entry
        if first_passing is None and capture + CAPTURE_EPS >= target:
            first_passing = entry
    chosen = first_passing if first_passing is not None else widest
    return {"optimal_pitch_m": chosen["pitch_m"], "optimal_gcr": chosen["gcr"],
            "irradiance_fraction": chosen["irradiance_fraction"],
            "annual_shade_loss_pct": chosen["annual_shade_loss_pct"],
            "target_achieved": first_passing is not None, "sweep": sweep}


SPACING_ANSWERS = ("units", "module_cross_axis_m", "module_along_axis_m", "module_gap_m", "module_pmax_w",
                   "latitude_deg", "longitude_deg", "module_height_m", "target_capture", "sweep_steps",
                   "pitch_min_multiplier", "pitch_max_multiplier")


def spacing_default_settings(settings, site_lat_lon=None):
    """CreateDefaultSettings, OptimalRowSpacingCommand.cs:173-201: the drawing's active module
    (ModuleCommand.GetActiveModule), its Pmax when positive, the torque tube height when
    positive, and the site latitude and longitude when the drawing carries one."""
    try:
        loaded = _layout.load_settings(settings or {})
        module = _layout.get_active_module(loaded)
    except _layout.LayoutInputError as exc:
        raise ReportInputError(str(exc)) from None
    out = dict(SPACING_DEFAULTS)
    out["module_along_axis_m"] = module.along_axis_m
    out["module_cross_axis_m"] = module.cross_axis_m
    out["module_gap_m"] = module.gap_m
    if module.pmax_w > 0.0:
        out["module_pmax_w"] = module.pmax_w
    tube = loaded.get("TorqueTubeHeightM", 0.0)
    if isinstance(tube, (int, float)) and not isinstance(tube, bool) and tube > 0.0:
        out["module_height_m"] = float(tube)
    if site_lat_lon is not None:
        out["latitude_deg"] = _num(site_lat_lon[0], "site latitude")
        out["longitude_deg"] = _num(site_lat_lon[1], "site longitude")
    return out


def spacing_command(boundary, settings, answers=None, site_lat_lon=None):
    """LEAFOPTIMALSPACING, OptimalRowSpacingCommand.cs:47-113: units (Meters), the picked
    boundary in metres, the module, site and sweep prompts (each its default unless answered),
    the latitude clamped to +-89.9, then the sweep. Returns {"settings", "result"}; a refusal
    of the calculator is {"error": message} as the command prints it."""
    a = dict(answers or {})
    unknown = set(a) - set(SPACING_ANSWERS)
    if unknown:
        raise ReportInputError(f"unknown answers {sorted(unknown)}")
    units = a.get("units", "Meters")
    mpu = {"Meters": 1.0, "Feet": FEET_TO_METERS}.get(units)
    if mpu is None:
        raise ReportInputError("units must be Meters or Feet")
    if not isinstance(boundary, (list, tuple)) or len(boundary) > MAX_ENTITY_VERTICES:
        raise ReportInputError("boundary must be a bounded list of points")
    pts = [_xy(p, "boundary vertex") for p in boundary]
    boundary_m = [(p[0] * mpu, p[1] * mpu) for p in pts]                     # ExtractVertices, cs:322-332
    if len(boundary_m) < 3:
        return {"error": "boundary polyline has fewer than 3 vertices."}
    s = spacing_default_settings(settings, site_lat_lon)
    for key in SPACING_ANSWERS[1:]:
        if key in a:
            s[key] = _int(a[key], key) if key == "sweep_steps" else _num(a[key], key)
    s["latitude_deg"] = max(-LATITUDE_CLAMP_DEG, min(LATITUDE_CLAMP_DEG, s["latitude_deg"]))
    module = _sweep.TrackerModuleSpec(along_axis_m=s["module_along_axis_m"], cross_axis_m=s["module_cross_axis_m"],
                                      gap_m=s["module_gap_m"], pmax_w=s["module_pmax_w"])   # cs:126-132
    try:
        result = optimal_row_spacing(boundary_m, module, s["latitude_deg"], s["longitude_deg"],
                                     s["module_height_m"], s["target_capture"], s["sweep_steps"],
                                     s["pitch_min_multiplier"], s["pitch_max_multiplier"])
    except ReportBoundsError:
        raise
    except ReportInputError as exc:
        return {"settings": s, "error": str(exc)}
    return {"settings": s, "result": result}


def spacing_report_lines(result, settings):
    """ReportResults, OptimalRowSpacingCommand.cs:334-362, line by line."""
    lines = ["LEAFOPTIMALSPACING - Optimal Row Spacing Results:",
             f"  Target capture : {_percent_text(settings['target_capture'], 1)}",
             f"  Site           : lat {_fixed_text(settings['latitude_deg'], 2)}, "
             f"lon {_fixed_text(settings['longitude_deg'], 2)}",
             f"  Module         : {_fixed_text(settings['module_cross_axis_m'], 3)} m cross-axis, "
             f"h={_fixed_text(settings['module_height_m'], 2)} m",
             f"  Optimal pitch  : {_fixed_text(result['optimal_pitch_m'], 3)} m",
             f"  Optimal GCR    : {_fixed_text(result['optimal_gcr'], 3)} "
             f"({_fixed_text(result['optimal_gcr'] * 100.0, 1)}%)",
             f"  Capture        : {_percent_text(result['irradiance_fraction'], 2)} "
             f"(shade loss {_fixed_text(result['annual_shade_loss_pct'], 2)}%)",
             "  Target achieved: " + ("yes" if result["target_achieved"] else "no - widest pitch reported"),
             "  Sweep:",
             "    Pitch    GCR    Capture    ShadeLoss  Rows"]
    for e in result["sweep"][:SPACING_TOP_K]:
        lines.append("    {0:>6}  {1:>5}  {2:>8}  {3:>8}%  {4:>4}".format(
            _fixed_text(e["pitch_m"], 3), _fixed_text(e["gcr"], 3), _percent_text(e["irradiance_fraction"], 2),
            _fixed_text(e["annual_shade_loss_pct"], 2), e["row_count"]))
    return lines
