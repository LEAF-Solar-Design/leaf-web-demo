"""Studio port of the plugin's ground build-out engines: LEAFBOM, LEAFTUBE3D,
LEAFGRADEMULTI, LEAFDRAWROAD and LEAFROAD.

Literal ports of Branch2025 (read 2026-09-23 at C:/tmp/solar-parity/wt-b25-s17):

  Terrain/BomCommand.cs                  LEAFBOM prompts, summary lines, CSV write (:23-160)
  Terrain/TrackerBomCalculator.cs        Compute, ToCsv, EscapeCsv (:65-241)
  Terrain/TrackerRowReader.cs            the tracker row reader over every tracker kind on
                                         the tracker layer: footprint polylines
                                         (PolylineToTrackerRow :287-337, TryReadBranchPolyline
                                         :342-352) and drawn tracker rows (TryParse... :563-626,
                                         BuildBranchTrackerRow :628-668, TryRead... :703-730),
                                         read in drawing order (ReadTrackerEntities :126-194)
  Terrain/TrackerRowGenerator.cs         TrackerRow defaults and PhysicalLengthMeters (:200-257)
  Terrain/TorqueTubeCommand.cs           LEAFTUBE3D (:31-141), AppendCylinder (:157-206)
  Terrain/TorqueTubeGeometryCalculator.cs  Compute (:86-137)
  Terrain/ModuleCommand.cs               GetActiveModule's torque tube radius (:34)
  Terrain/GradeCommand.cs                LEAFGRADEMULTI (:336-604), Centroid (:675-680)
  Terrain/GradingCalculator.cs           through server/solar_ground_analysis.py (its port)
  Pvcase/LeafDrawRoadCommand.cs          LEAFDRAWROAD (:26-114), DrawFilleted (:177-241),
                                         PolylineLength (:243-253)
  Terrain/RoadCommand.cs                 LEAFROAD (:30-235), ComputeOffsetPolyline (:245-289),
                                         DrawCrossSection (:325-380), GetMidpoint (:382-403)

Pure functions over plain data. No AutoCAD, no I/O, no network. The engines take and
return NEUTRAL structures only: a tracker entity as {"kind": "polyline", "vertices",
optional "layer", "row_index", "slots"} or {"kind": "tracker", ...the drawn row's named
fields...}, the terrain grid as the neutral grid of server/solar_ground_terrain.py,
drawing settings by product setting name, lines as vertices plus one bulge per vertex.
How the plugin encodes any of those in a drawing is not part of this module.

Floating point work is ordered exactly as the C# orders it (IEEE doubles on both sides,
banker's rounding where C# uses Math.Round, .NET "F<n>" through the analysis port), so
the CSV, the tube ends and the road vertices reproduce the plugin's numbers bit for bit.
A torque tube's bounding box is the exact axis-aligned box of its cylinder.

Every input is bounded and every malformed input fails closed with BuildoutInputError
(a ValueError); a bound breach raises BuildoutBoundsError. Every pass is linear in its
input (rows, pads x grade samples, centerline vertices); nothing is cached.
"""
from __future__ import annotations

import importlib.util
import math
from numbers import Real
from pathlib import Path


def _load_sibling(name):
    """Load a server module by path so the import works from any cwd."""
    path = Path(__file__).resolve().with_name(name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_analysis = _load_sibling("solar_ground_analysis")  # GradingCalculator port, TerrainGrid, "F<n>"
_ground = _load_sibling("solar_ground_terrain")     # TryReadTerrainInterpolator port

# ---------------------------------------------------------------------------
#  Constants (each cites the line that defines it)
# ---------------------------------------------------------------------------

TRACKER_LAYER = "LEAF-TRACKERS"                   # LayerNames.cs:36, the tracker layer
TUBE_LAYER = "LEAF-TUBES"                         # LayerNames.cs:98
TUBE_LAYER_COLOR_INDEX = 8                        # TorqueTubeCommand.cs:29
GRADE_LAYER = "LEAF-GRADE"                        # LayerNames.cs:86
GRADE_LAYER_COLOR_INDEX = 3                       # GradeCommand.cs:476
ROAD_LAYER = "LEAF-ROAD"                          # LayerNames.cs:92, LEAFROAD's centerline
ROAD_EDGE_LAYER = "LEAF-ROAD-EDGE"                # RoadCommand.cs:27
ROAD_XSEC_LAYER = "LEAF-ROAD-XSEC"                # RoadCommand.cs:28
DRAW_ROAD_EDGE_LAYER = "LEAF-PVCASE-ROAD"         # LeafDrawRoadCommand.cs:23
DRAW_ROAD_OFFSET_LAYER = "LEAF-PVCASE-ROAD-OFFSET"  # LeafDrawRoadCommand.cs:24
CURRENT_LAYER = "0"   # LEAFDRAWROAD's two-point centerline takes the current layer (:160-166)

# Line roles (G23 road-line): the layer each role is drawn on.
ROLE_BY_LAYER = {ROAD_LAYER: "centerline", CURRENT_LAYER: "centerline", ROAD_EDGE_LAYER: "edge",
                 DRAW_ROAD_EDGE_LAYER: "edge", DRAW_ROAD_OFFSET_LAYER: "offset",
                 ROAD_XSEC_LAYER: "cross-section"}

# LEAFBOM
DEFAULT_PILE_SPACING_M = 5.0                      # TrackerBomCalculator.cs:99
CSV_HEADER = "Category,Description,Unit,Quantity"  # TrackerBomCalculator.cs:68
# StringBuilder.AppendLine writes Environment.NewLine, "\r\n" on the Windows host, after
# every line (TrackerBomCalculator.cs:68, :74); File.WriteAllText(..., Encoding.UTF8)
# prefixes the UTF-8 byte order mark (BomCommand.cs:131).
CSV_NEWLINE = "\r\n"
UTF8_BOM = b"\xef\xbb\xbf"
# The module line's separator. The source read above writes a hyphen (:177); the build the
# 2026-09-23 capture ran wrote U+2014 in its CSV, and Studio reproduces the live output.
MODULE_SEPARATOR = "\u2014"
TIMES = "\u00d7"                                  # "×", TrackerBomCalculator.cs:177, :229
# TrackerRow defaults (TrackerRowGenerator.cs:202-257): no overhang, no per-row power, no
# explicit pile count, piles estimated.
DEFAULT_MODULE_POWER_W = 0
NO_PILE_OVERRIDE = -1
# The drawer writes every drawn row with schema version 2 (SatCommand.cs BuildRowXData);
# a drawn row reads as a tracker row only at version 1 or later (TrackerRowReader.cs:584-585).
DRAWER_SCHEMA_VERSION = 2
MIN_AXIS_DU = 1e-9                                # TrackerRowReader.cs Epsilon (:76)

# LEAFTUBE3D
DEFAULT_TUBE_HEIGHT_M = 1.5                       # TorqueTubeCommand.cs:51
DEFAULT_TUBE_RADIUS_M = 0.08                      # ModuleCommand.cs:34
MIN_TUBE_LENGTH_DU = 1e-6                         # TorqueTubeCommand.cs:104, a degenerate segment

# LEAFGRADEMULTI
GRADE_MODES = ("Auto", "Manual", "Clearance")     # GradeCommand.cs:380-382, default Auto (:388)
DEFAULT_CLEARANCE_M = 0.6                         # GradeCommand.cs:412
LABEL_HEIGHT_FACTOR = 0.05                        # GradeCommand.cs:541
MIN_LABEL_HEIGHT_M = 0.5                          # GradeCommand.cs:542
FEET_LABEL_BELOW_MPU = 0.5                        # GradeCommand.cs:352
GRADING_ELEVATION_SETTING = _analysis.GRADING_ELEVATION_SETTING   # GradeCommand.cs:582
GRADING_MODE_SETTING = _analysis.GRADING_MODE_SETTING             # GradeCommand.cs:583-585
GRADING_MODE_SLOPE = _analysis.GRADING_MODE_SLOPE
GRADING_MODE_CLEARANCE = _analysis.GRADING_MODE_CLEARANCE

# LEAFDRAWROAD
DRAW_ROAD_DEFAULT_WIDTH = 4.0                     # LeafDrawRoadCommand.cs:38
DRAW_ROAD_DEFAULT_RADIUS = 12.0                   # LeafDrawRoadCommand.cs:44
DRAW_ROAD_DEFAULT_OFFSET = 1.0                    # LeafDrawRoadCommand.cs:50
FILLET_COLLINEAR_COS = 0.99995                    # LeafDrawRoadCommand.cs:204, about 0.5 deg
MIN_SEGMENT = 1e-9                                # LeafDrawRoadCommand.cs:195, RoadCommand.cs:248

# LEAFROAD
ROAD_DEFAULT_WIDTH_M = 6.0                        # RoadCommand.cs:97
ROAD_DEFAULT_SHOULDER_M = 1.0                     # RoadCommand.cs:111
ROAD_DEFAULT_CROSS_SLOPE_PCT = 2.0                # RoadCommand.cs:128
ROAD_MAX_CROSS_SLOPE_PCT = 10.0                   # RoadCommand.cs:137
XSEC_BELOW_FACTOR = 4.0                           # RoadCommand.cs:196
XSEC_VERTICAL_EXAGGERATION = 10.0                 # RoadCommand.cs:342
XSEC_MIN_SHOULDER_SLOPE_PCT = 4.0                 # RoadCommand.cs:338
XSEC_LABEL_HEIGHT_FACTOR = 0.08                   # RoadCommand.cs:372
XSEC_LABEL_BELOW_HEIGHTS = 3                      # RoadCommand.cs:373-374

RUNTIME_NET8 = _analysis.RUNTIME_NET8

# Studio-side bounds. The plugin has none; these refuse inputs that would pin a worker.
MAX_TRACKER_ENTITIES = 200_000
MAX_POLYLINE_VERTICES = 20_000
MAX_PADS = 1_000
MAX_CENTERLINE_VERTICES = 20_000


class BuildoutInputError(ValueError):
    """Malformed input: the engine refuses rather than guess."""


class BuildoutBoundsError(BuildoutInputError):
    """Input exceeds a Studio bound (entities, vertices, pads)."""


# ---------------------------------------------------------------------------
#  Validation helpers (fail closed)
# ---------------------------------------------------------------------------

def _finite(value, what):
    if isinstance(value, bool) or not isinstance(value, Real):
        raise BuildoutInputError(f"{what} must be a number, got {type(value).__name__}")
    v = float(value)
    if not math.isfinite(v):
        raise BuildoutInputError(f"{what} must be finite, got {v!r}")
    return v


def _mpu(value):
    v = _finite(value, "meters_per_unit")
    if v <= 0.0:
        raise BuildoutInputError("metersPerUnit must be > 0")
    return v


def _point2(value, what):
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        raise BuildoutInputError(f"{what} must be an [x, y] point")
    return (_finite(value[0], f"{what}.x"), _finite(value[1], f"{what}.y"))


def _points(value, what, max_count):
    if not isinstance(value, (list, tuple)):
        raise BuildoutInputError(f"{what} must be a list of [x, y] points")
    if len(value) > max_count:
        raise BuildoutBoundsError(f"{len(value)} {what} vertices exceed the bound of {max_count}")
    return [_point2(p, f"{what}[{i}]") for i, p in enumerate(value)]


def _round_half_even(value):
    """C# Math.Round(double): midpoint to even (Python's round on a float is the same)."""
    return int(round(value))


def fmt(value, digits, runtime=RUNTIME_NET8):
    """value.ToString("F<digits>") on the plugin's runtime (the analysis port)."""
    try:
        return _analysis.format_fixed(value, digits, runtime)
    except _analysis.AnalysisInputError as exc:
        raise BuildoutInputError(str(exc)) from None


def _grade_call(fn, *args):
    """A GradingCalculator port call; its refusals become this module's, keeping the
    bound/input distinction."""
    try:
        return fn(*args)
    except _analysis.AnalysisBoundsError as exc:
        raise BuildoutBoundsError(str(exc)) from None
    except _analysis.AnalysisInputError as exc:
        raise BuildoutInputError(str(exc)) from None


# ---------------------------------------------------------------------------
#  Tracker rows (TrackerRowReader.cs, TrackerRowGenerator.cs)
# ---------------------------------------------------------------------------

def _tracker_row(axis_start, axis_end, module_slots, length_m, rail_overhang_m, row_index):
    """A TrackerRow with the defaults of TrackerRowGenerator.cs:202-257."""
    return {"axis_start": axis_start, "axis_end": axis_end, "module_slots": module_slots,
            "length_m": length_m, "rail_overhang_m": rail_overhang_m, "row_index": row_index,
            "module_power_w": DEFAULT_MODULE_POWER_W, "pile_count_override": NO_PILE_OVERRIDE,
            "no_piles_estimated": False}


def physical_length_m(row):
    """TrackerRow.PhysicalLengthMeters, TrackerRowGenerator.cs:208."""
    return row["length_m"] + 2.0 * row["rail_overhang_m"]


def _optional_count(entity, key, what):
    raw = entity.get(key, 0)
    return _round_half_even(_finite(raw, what))


def polyline_tracker_row(entity, meters_per_unit=1.0):
    """TryReadBranchPolyline and PolylineToTrackerRow, TrackerRowReader.cs:342-352 and
    :287-337: a polyline on the tracker layer with at least four vertices reads as a row
    whose axis runs from midpoint(v0, v1) to midpoint(v2, v3). Its row index and module
    slots are the row fields it carries (0 when it carries none, as a frame does). None
    when it is not a tracker polyline."""
    layer = entity.get("layer", TRACKER_LAYER)
    if not isinstance(layer, str):
        raise BuildoutInputError("a polyline layer must be a string")
    if layer.upper() != TRACKER_LAYER.upper():                       # :346-347
        return None
    pts = _points(entity.get("vertices"), "tracker polyline", MAX_POLYLINE_VERTICES)
    if len(pts) < 4:                                                 # :348
        return None
    mpu = _mpu(meters_per_unit)
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = pts[:4]                 # :304-307
    ax = (x0 + x1) * 0.5
    ay = (y0 + y1) * 0.5
    bx = (x2 + x3) * 0.5
    by = (y2 + y3) * 0.5
    dx = bx - ax
    dy = by - ay
    length_du = math.sqrt(dx * dx + dy * dy)                          # :316
    return _tracker_row((ax, ay), (bx, by), _optional_count(entity, "slots", "polyline slots"),
                        length_du * mpu, 0.0, _optional_count(entity, "row_index", "polyline row index"))


def _row_field(entity, key, fallback):
    """GetDouble, TrackerRowReader.cs: the named field, or the fallback when absent."""
    if key not in entity or entity[key] is None:
        return fallback
    return _finite(entity[key], key)


def drawn_tracker_row(entity, meters_per_unit=1.0):
    """TryReadBranchTrackerBlock, TrackerRowReader.cs:703-730, through TryParse...
    (:563-626) and BuildBranchTrackerRow (:628-668). A drawn row is recognised by its
    row fields (schema version at least 1), not by block name or layer; it needs both
    axis ends. The table length is row_length_m, or the axis span when that is unset.
    None when the entity does not read as a tracker row."""
    schema = entity.get("schema_version", DRAWER_SCHEMA_VERSION)
    if _round_half_even(_finite(schema, "schema_version")) < 1:      # :585
        return None
    if entity.get("axis_start") is None or entity.get("axis_end") is None:
        return None                                                  # HasAxis false, :630
    ax, ay = _point2(entity["axis_start"], "axis_start")
    bx, by = _point2(entity["axis_end"], "axis_end")
    mpu = meters_per_unit if meters_per_unit > 0.0 else 1.0          # :632
    dx = bx - ax
    dy = by - ay
    axis_length_du = math.sqrt(dx * dx + dy * dy)                     # :636-638
    row_length = _row_field(entity, "row_length_m", 0.0)
    length_m = row_length if row_length > 0.0 else axis_length_du * mpu   # :641
    return _tracker_row((ax, ay), (bx, by), _optional_count(entity, "slots", "row slots"),
                        length_m, _row_field(entity, "rail_overhang_m", 0.0),
                        _optional_count(entity, "row_index", "row index"))


def read_tracker_rows(entities, meters_per_unit=1.0):
    """ReadTrackerRows, TrackerRowReader.cs:126-194: every tracker entity in drawing
    order, polylines through the polyline reader and drawn rows through the row-field
    reader; anything else is skipped. Linear in the entities."""
    if not isinstance(entities, (list, tuple)):
        raise BuildoutInputError("entities must be a list")
    if len(entities) > MAX_TRACKER_ENTITIES:
        raise BuildoutBoundsError(f"{len(entities)} entities exceed the bound of {MAX_TRACKER_ENTITIES}")
    mpu = _mpu(meters_per_unit)
    rows = []
    for i, ent in enumerate(entities):
        if not isinstance(ent, dict):
            raise BuildoutInputError(f"entities[{i}] must be an object")
        kind = ent.get("kind")
        if kind == "polyline":
            row = polyline_tracker_row(ent, mpu)
        elif kind == "tracker":
            row = drawn_tracker_row(ent, mpu)
        else:
            raise BuildoutInputError(f"entities[{i}].kind must be polyline or tracker")
        if row is not None:
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
#  LEAFBOM (TrackerBomCalculator.cs, BomCommand.cs)
# ---------------------------------------------------------------------------

def _module_spec(module):
    if not isinstance(module, dict):
        raise BuildoutInputError("module must be an object")               # ArgumentNullException
    return (_finite(module.get("cross_axis_m"), "module.cross_axis_m"),
            _finite(module.get("along_axis_m"), "module.along_axis_m"),
            _finite(module.get("pmax_w", 0.0), "module.pmax_w"))


def compute_bom(rows, module, pile_spacing_m=DEFAULT_PILE_SPACING_M, runtime=RUNTIME_NET8):
    """TrackerBomCalculator.Compute, :117-241. rows: read_tracker_rows output. module:
    {"cross_axis_m", "along_axis_m", "pmax_w"} (GetActiveModule). Returns {"total_rows",
    "total_modules", "total_tube_length_m", "total_piles", "total_drive_units",
    "total_dc_capacity_kwp", "lines": [{"category", "description", "unit", "quantity"}]}."""
    if not isinstance(rows, (list, tuple)):
        raise BuildoutInputError("rows must be a list")                   # :122
    cross_m, along_m, pmax_w = _module_spec(module)                       # :123
    spacing = _finite(pile_spacing_m, "pile_spacing_m")
    if spacing <= 0:
        raise BuildoutInputError("pileSpacingM must be > 0.")             # :124-125
    total_modules = 0
    total_tube_m = 0.0
    total_piles = 0
    total_drives = len(rows)                                              # :130
    dc_kwp = 0.0
    used_explicit = used_estimated = False
    for row in rows:                                                      # :135-157
        total_modules += row["module_slots"]
        tube_m = physical_length_m(row)
        total_tube_m += tube_m
        if row["pile_count_override"] >= 0:
            total_piles += row["pile_count_override"]
            used_explicit = True
        elif not row["no_piles_estimated"]:
            total_piles += max(1, math.ceil(tube_m / spacing))            # :149
            used_estimated = True
        power_w = row["module_power_w"] if row["module_power_w"] > 0 else pmax_w
        if power_w > 0:
            dc_kwp += row["module_slots"] * power_w / 1000.0              # :156
    lines = [{"category": "Module",                                       # :174-180
              "description": f"PV Module {MODULE_SEPARATOR} {fmt(cross_m, 3, runtime)} m {TIMES} "
                             f"{fmt(along_m, 3, runtime)} m portrait",
              "unit": "ea", "quantity": float(total_modules)}]
    if rows:                                                              # :183-199
        lines.append({"category": "Torque Tube",
                      "description": f"Torque tube / tracker rail ({len(rows)} sections, "
                                     f"{fmt(total_tube_m, 1, runtime)} m total)",
                      "unit": "m", "quantity": total_tube_m})
        lines.append({"category": "Torque Tube", "description": "Torque tube section count",
                      "unit": "ea", "quantity": float(len(rows))})
    spacing_text = fmt(spacing, 1, runtime)
    if used_explicit:                                                     # :202-206
        pile_description = (f"Ground pile / foundation (drawing XData where present; otherwise "
                            f"{spacing_text} m c/c estimated)" if used_estimated
                            else "Ground pile / foundation (from drawing XData)")
    else:
        pile_description = f"Ground pile / foundation (at {spacing_text} m c/c spacing, estimated)"
    lines.append({"category": "Pile", "description": pile_description, "unit": "ea",
                  "quantity": float(total_piles)})
    lines.append({"category": "Drive Unit",                               # :216-222
                  "description": "Single-axis tracker drive unit (one per table section)",
                  "unit": "ea", "quantity": float(total_drives)})
    if dc_kwp > 0:                                                        # :226-238
        description = (f"Total DC nameplate ({total_modules} {TIMES} {fmt(pmax_w, 0, runtime)} Wp)"
                       if pmax_w > 0 else
                       f"Total DC nameplate ({total_modules} modules; per-row Wp from drawing XData)")
        lines.append({"category": "DC Capacity", "description": description, "unit": "kWp",
                      "quantity": dc_kwp})
    return {"total_rows": len(rows), "total_modules": total_modules, "total_tube_length_m": total_tube_m,
            "total_piles": total_piles, "total_drive_units": total_drives,
            "total_dc_capacity_kwp": dc_kwp, "lines": lines}


def escape_csv(s):
    """TrackerBomResult.EscapeCsv, :79-85."""
    if s is None:
        return ""
    if "," in s or '"' in s or "\n" in s:
        return '"' + s.replace('"', '""') + '"'
    return s


def bom_csv_text(result, runtime=RUNTIME_NET8):
    """TrackerBomResult.ToCsv, :65-77: "ea" quantities as (int)Math.Round, the rest F2."""
    out = [CSV_HEADER, CSV_NEWLINE]
    for line in result["lines"]:
        qty = (str(_round_half_even(line["quantity"])) if line["unit"] == "ea"
               else fmt(line["quantity"], 2, runtime))
        out.append(f"{escape_csv(line['category'])},{escape_csv(line['description'])},"
                   f"{escape_csv(line['unit'])},{qty}")
        out.append(CSV_NEWLINE)
    return "".join(out)


def bom_csv_bytes(text):
    """The file BomCommand.cs:131 writes: File.WriteAllText with Encoding.UTF8 (a BOM)."""
    return UTF8_BOM + text.encode("utf-8")


def bom_command(entities, module, pile_spacing_m=None, meters_per_unit=1.0, runtime=RUNTIME_NET8):
    """LEAFBOM end to end, BomCommand.cs:23-160, every prompt at its default unless given.

    Returns {"succeeded", "rows_found", "result", "report", "messages", "csv_text",
    "csv_bytes"}. report holds the summary values as the command prints them (:79-86):
    tracker-sections, module-slots, tube-length (F1 text), estimated-piles, drives and,
    when positive, dc-capacity-kwp (F1 text). No rows is the plugin's message and no file."""
    spacing = DEFAULT_PILE_SPACING_M if pile_spacing_m is None else _finite(pile_spacing_m, "pile spacing")
    if spacing <= 0:
        raise BuildoutInputError("pile spacing must be > 0")             # AllowZero/AllowNegative false
    rows = read_tracker_rows(entities, meters_per_unit)                  # :59
    messages = [f"Found {len(rows)} tracker row(s) in drawing."]         # :60
    out = {"succeeded": False, "rows_found": len(rows), "result": None, "report": None,
           "messages": messages, "csv_text": None, "csv_bytes": None}
    if not rows:                                                         # :62-66
        messages.append("No tracker rows found.")
        return out
    bom = compute_bom(rows, module, spacing, runtime)                    # :76
    tube_text = fmt(bom["total_tube_length_m"], 1, runtime)
    report = {"tracker-sections": bom["total_rows"], "module-slots": bom["total_modules"],
              "tube-length": tube_text, "estimated-piles": bom["total_piles"],
              "drives": bom["total_drive_units"]}
    messages += ["--- LEAFBOM Summary ---",                              # :79-86
                 f"  Tracker table sections : {bom['total_rows']}",
                 f"  PV module slots        : {bom['total_modules']}",
                 f"  Total tube length      : {tube_text} m",
                 f"  Estimated piles        : {bom['total_piles']} (at {fmt(spacing, 1, runtime)} m c/c)",
                 f"  Drive units            : {bom['total_drive_units']}"]
    if bom["total_dc_capacity_kwp"] > 0:
        kwp = bom["total_dc_capacity_kwp"]
        report["dc-capacity-kwp"] = fmt(kwp, 1, runtime)
        messages.append(f"  DC capacity (nameplate): {fmt(kwp, 1, runtime)} kWp "
                        f"({fmt(kwp / 1000.0, 3, runtime)} MWp)")
    text = bom_csv_text(bom, runtime)
    out.update(succeeded=True, result=bom, report=report, csv_text=text, csv_bytes=bom_csv_bytes(text))
    return out


# ---------------------------------------------------------------------------
#  LEAFTUBE3D (TorqueTubeCommand.cs, TorqueTubeGeometryCalculator.cs)
# ---------------------------------------------------------------------------

def tube_parameters(stored_height_m=0.0, stored_radius_m=0.0):
    """The height (TorqueTubeCommand.cs:51) and radius (ModuleCommand.cs:34) LEAFTUBE3D
    uses: the stored setting when positive, else 1.5 m and 0.08 m."""
    h = _finite(stored_height_m, "torque tube height")
    r = _finite(stored_radius_m, "torque tube radius")
    return (h if h > 0 else DEFAULT_TUBE_HEIGHT_M, r if r > 0 else DEFAULT_TUBE_RADIUS_M)


def compute_tube_segments(rows, height_m, radius_m, grid=None, meters_per_unit=1.0):
    """TorqueTubeGeometryCalculator.Compute, :86-137: one segment per row in metres, its
    ends at the row's axis ends raised by height_m above the terrain there (the grid's
    bilinear Z, 0 off the grid), or at height_m with no terrain."""
    if not isinstance(rows, (list, tuple)):
        raise BuildoutInputError("rows must be a list")                   # :93-94
    height = _finite(height_m, "height_m")
    if height < 0:
        raise BuildoutInputError(f"heightM must be >= 0, got {height}.")  # :95-97
    radius = _finite(radius_m, "radius_m")
    if radius <= 0:
        raise BuildoutInputError(f"radiusM must be > 0, got {radius}.")   # :98-100
    mpu = _mpu(meters_per_unit)                                           # :101-103
    terrain = _ground.terrain_interpolator(grid, mpu) if grid is not None else None
    segments = []
    for row in rows:
        (sx, sy), (ex, ey) = row["axis_start"], row["axis_end"]
        start_xm, start_ym = sx * mpu, sy * mpu                           # :110-113
        end_xm, end_ym = ex * mpu, ey * mpu
        if terrain is not None:                                           # :116-123
            sz = terrain.interpolate_z(sx, sy)
            ez = terrain.interpolate_z(ex, ey)
            start_z = height + (sz if sz is not None else 0.0)
            end_z = height + (ez if ez is not None else 0.0)
        else:
            start_z = end_z = height
        dx = end_xm - start_xm
        dy = end_ym - start_ym
        dz = end_z - start_z
        segments.append({"start": (start_xm, start_ym, start_z), "end": (end_xm, end_ym, end_z),
                         "radius_m": radius, "length_m": math.sqrt(dx * dx + dy * dy + dz * dz),
                         "row_index": row["row_index"]})                  # TorqueTubeSegment, :29-44
    return segments, terrain is not None


def cylinder_bbox(start, end, radius):
    """The exact axis-aligned box of the solid cylinder AppendCylinder builds
    (TorqueTubeCommand.cs:157-206): its end discs, normal to the axis u, reach
    radius * sqrt(1 - u_i^2) beyond the axis ends along each world axis i."""
    d = [end[i] - start[i] for i in range(3)]
    length = math.sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2])
    lo, hi = [], []
    for i in range(3):
        u = d[i] / length
        reach = radius * math.sqrt(max(0.0, 1.0 - u * u))
        lo.append(min(start[i], end[i]) - reach)
        hi.append(max(start[i], end[i]) + reach)
    return tuple(lo), tuple(hi)


def torque_tube_command(entities, grid=None, stored_height_m=0.0, stored_radius_m=0.0,
                        meters_per_unit=1.0, runtime=RUNTIME_NET8):
    """LEAFTUBE3D end to end, TorqueTubeCommand.cs:31-141: one solid per tracker row on
    LEAF-TUBES, following the terrain when the drawing has a grid. Returns {"succeeded",
    "rows_found", "height_m", "radius_m", "has_terrain", "solids", "messages"}; each
    solid is {"layer", "start", "end" (drawing units), "radius_du", "length_du",
    "bbox_min", "bbox_max"}."""
    mpu = _mpu(meters_per_unit)
    height, radius = tube_parameters(stored_height_m, stored_radius_m)
    messages = [f"Torque tube centreline height: {fmt(height, 2, runtime)} m above ground",
                f"Torque tube outer radius     : {fmt(radius * 1000, 0, runtime)} mm",
                "(Change height/radius with LEAFMODULE.)"]
    rows = read_tracker_rows(entities, mpu)                               # :59
    messages.append(f"Found {len(rows)} tracker row(s) on layer {TRACKER_LAYER}.")
    out = {"succeeded": False, "rows_found": len(rows), "height_m": height, "radius_m": radius,
           "has_terrain": False, "solids": [], "messages": messages}
    if not rows:                                                          # :61-67
        messages.append(f"No tracker rows found on layer {TRACKER_LAYER}. Run LEAFTRACK or LEAFSAT first.")
        return out
    segments, has_terrain = compute_tube_segments(rows, height, radius, grid, mpu)
    messages.append("LEAFTOPO terrain found - tubes will follow terrain profile." if has_terrain
                    else "No LEAFTOPO terrain - tubes drawn at constant height.")
    solids = []
    for seg in segments:                                                  # :92-110
        start = tuple(c / mpu for c in seg["start"])
        end = tuple(c / mpu for c in seg["end"])
        length_du = seg["length_m"] / mpu
        radius_du = seg["radius_m"] / mpu
        if length_du < MIN_TUBE_LENGTH_DU:
            continue
        bbox_min, bbox_max = cylinder_bbox(start, end, radius_du)
        solids.append({"layer": TUBE_LAYER, "start": start, "end": end, "radius_du": radius_du,
                       "length_du": length_du, "bbox_min": bbox_min, "bbox_max": bbox_max})
    messages.append(f"LEAFTUBE3D complete -- {len(solids)} torque tube cylinder(s) on layer {TUBE_LAYER}.")
    messages.append(f"  Height: {fmt(height, 2, runtime)} m  |  OD: {fmt(radius * 2 * 1000, 0, runtime)} mm")
    out.update(succeeded=True, has_terrain=has_terrain, solids=solids)
    return out


# ---------------------------------------------------------------------------
#  LEAFGRADEMULTI (GradeCommand.cs:336-604)
# ---------------------------------------------------------------------------

def net_cut_fill_text(net_m3):
    """{netM3:+F1;-F1;0} (GradeCommand.cs:573) as .NET formats it: a custom format whose
    positive and negative sections hold no digit placeholder print "+F1" and "-F1"
    literally; a value that rounds to zero in those sections takes the zero section."""
    net = _finite(net_m3, "net volume")
    if abs(net) < 0.5:
        return "0"
    return "+F1" if net > 0 else "-F1"


def grade_multi(grid, pads, meters_per_unit=1.0, mode=None, value_du=None, runtime=RUNTIME_NET8):
    """LEAFGRADEMULTI end to end with the selected pad polylines, GradeCommand.cs:336-604.

    mode is the shared elevation prompt's answer (None is Enter, Auto, :388); value_du the
    Manual elevation or Clearance height in drawing units (None takes the prompt default:
    the grid's lowest elevation for Manual, 0.6 m for Clearance; with no terrain the pad
    elevation, default 0). pads are boundary vertex lists in selection order; one with
    fewer than 3 vertices is skipped (:484). Returns {"succeeded", "message", "mode",
    "pads", "total_cut_m3", "total_fill_m3", "net_m3", "settings", "messages"}; each pad
    is {"index", "layer", "vertices" (its own order), "closed", "elevation_m", "label":
    {"layer", "text", "at" (vertex centroid), "height"}, "cut_fill"}."""
    mpu = _mpu(meters_per_unit)
    unit_label = "ft" if mpu < FEET_LABEL_BELOW_MPU else "m"               # :352
    try:
        terrain = _analysis.terrain_grid(grid, mpu)                       # :357
    except _analysis.AnalysisInputError as exc:
        raise BuildoutInputError(f"terrain grid refused: {exc}") from None
    has_terrain = terrain is not None
    cell = _analysis.grade_cell_size_du(terrain) if has_terrain else 1.0  # :365-367
    fixed_m = 0.0
    clearance_m = 0.0
    if mode is not None and (not isinstance(mode, str)
                             or mode.lower() not in (m.lower() for m in GRADE_MODES)):
        raise BuildoutInputError(f"mode must be one of {GRADE_MODES} or None")
    if has_terrain:                                                       # :376-427
        chosen = "Auto" if mode is None else next(m for m in GRADE_MODES if m.lower() == mode.lower())
        is_auto, is_manual, is_clearance = chosen == "Auto", chosen == "Manual", chosen == "Clearance"
        if is_manual:
            default_du = terrain.elevation_range()[0] / mpu               # :397
            fixed_m = (default_du if value_du is None else _finite(value_du, "elevation")) * mpu
        elif is_clearance:
            clearance_du = DEFAULT_CLEARANCE_M / mpu if value_du is None else _finite(value_du, "clearance")
            if clearance_du < 0:
                raise BuildoutInputError("clearance must not be negative")   # AllowNegative = false
            clearance_m = clearance_du * mpu                              # :425
    else:                                                                 # :428-443
        chosen = "Manual"
        is_auto, is_manual, is_clearance = False, True, False
        fixed_m = (0.0 if value_du is None else _finite(value_du, "elevation")) * mpu
    out = {"succeeded": False, "message": None, "mode": chosen, "pads": [], "total_cut_m3": 0.0,
           "total_fill_m3": 0.0, "net_m3": 0.0, "settings": None, "messages": []}
    if not isinstance(pads, (list, tuple)):
        raise BuildoutInputError("pads must be a list of boundary vertex lists")
    if len(pads) > MAX_PADS:
        raise BuildoutBoundsError(f"{len(pads)} pads exceed the bound of {MAX_PADS}")
    if not pads:                                                          # :457-461
        out["message"] = "No polylines selected - LEAFGRADEMULTI cancelled."
        return out
    messages = out["messages"]
    messages.append(f"{len(pads)} pad boundary polyline(s) selected.")   # :464
    total_cut = total_fill = 0.0
    last_elev_m = fixed_m                                                 # :471
    for pad_idx, pad in enumerate(pads):                                  # :478-559
        boundary = _points(pad, f"pad {pad_idx + 1}", MAX_POLYLINE_VERTICES)
        if len(boundary) < 3:
            continue
        if is_manual:
            elev_m = fixed_m
        elif is_clearance and has_terrain:
            try:
                elev_m = _analysis.find_clearance_elevation(terrain, boundary, clearance_m, mpu, cell)
            except _analysis.AnalysisBoundsError as exc:
                raise BuildoutBoundsError(str(exc)) from None
            except _analysis.AnalysisInputError as exc:                   # :499-504
                messages.append(f"  Pad {pad_idx + 1}: clearance computation failed - {exc}")
                continue
        elif is_auto and has_terrain:
            elev_m = _grade_call(_analysis.find_balanced_elevation, terrain, boundary, mpu, cell)
        else:
            elev_m = fixed_m
        last_elev_m = elev_m                                              # :516
        cut_fill = None
        if has_terrain:                                                   # :519-526
            cut_fill = _grade_call(_analysis.compute_flat, terrain, elev_m, boundary, mpu, cell)
            total_cut += cut_fill["cut_m3"]
            total_fill += cut_fill["fill_m3"]
        cx, cy = _analysis._centroid(boundary)                            # :539, Centroid :675-680
        min_x, min_y, max_x, max_y = _analysis.bounding_box(boundary)
        label_height = max(((max_y - min_y) + (max_x - min_x)) / 2.0 * LABEL_HEIGHT_FACTOR,
                           MIN_LABEL_HEIGHT_M / mpu)                      # :540-542
        elev_text = fmt(elev_m / mpu, 2, runtime)
        out["pads"].append({"index": pad_idx + 1, "layer": GRADE_LAYER, "vertices": [list(p) for p in boundary],
                            "closed": True, "elevation_m": elev_m,
                            "label": {"layer": GRADE_LAYER, "text": f"PAD {pad_idx + 1}\\P{elev_text} {unit_label}",
                                      "at": [cx, cy], "height": label_height},
                            "cut_fill": cut_fill})                        # :529-550
        messages.append(f"  Pad {pad_idx + 1}: elev = {elev_text} {unit_label}"
                        + (f"  cut={fmt(cut_fill['cut_m3'], 1, runtime)} m\u00b3  "
                           f"fill={fmt(cut_fill['fill_m3'], 1, runtime)} m\u00b3" if cut_fill is not None else ""))
    messages.append(f"LEAFGRADEMULTI complete \u2014 {len(out['pads'])} pad(s) graded:")   # :567
    net = total_cut - total_fill
    if has_terrain:                                                       # :568-576
        messages.append(f"  Total cut     : {fmt(total_cut, 1, runtime)} m\u00b3")
        messages.append(f"  Total fill    : {fmt(total_fill, 1, runtime)} m\u00b3")
        messages.append(f"  Net (cut-fill): {net_cut_fill_text(net)} m\u00b3"
                        + (" (surplus cut)" if net > 1 else " (import fill required)" if net < -1 else " (balanced)"))
    out.update(succeeded=True, total_cut_m3=total_cut, total_fill_m3=total_fill, net_m3=net,
               settings={GRADING_ELEVATION_SETTING: last_elev_m,               # :581-586
                         GRADING_MODE_SETTING: GRADING_MODE_CLEARANCE if is_clearance else GRADING_MODE_SLOPE})
    return out


# ---------------------------------------------------------------------------
#  Road geometry (RoadCommand.cs, LeafDrawRoadCommand.cs)
# ---------------------------------------------------------------------------

def compute_offset_polyline(center_pts, offset_du):
    """RoadCommand.ComputeOffsetPolyline, :245-289: each vertex moved along the average
    of its adjacent segments' left normals, renormalised; (0, 1) where that vanishes."""
    if not isinstance(center_pts, (list, tuple)) or len(center_pts) < 2:
        raise BuildoutInputError("Need at least 2 centre points.")
    pts = _points(center_pts, "centerline", MAX_CENTERLINE_VERTICES)
    offset = _finite(offset_du, "offset")
    n = len(pts)
    normals = []
    for i in range(n):
        dx = dy = 0.0
        count = 0
        if i < n - 1:
            ex = pts[i + 1][0] - pts[i][0]
            ey = pts[i + 1][1] - pts[i][1]
            length = math.sqrt(ex * ex + ey * ey)
            if length > MIN_SEGMENT:
                dx += -ey / length
                dy += ex / length
                count += 1
        if i > 0:
            ex = pts[i][0] - pts[i - 1][0]
            ey = pts[i][1] - pts[i - 1][1]
            length = math.sqrt(ex * ex + ey * ey)
            if length > MIN_SEGMENT:
                dx += -ey / length
                dy += ex / length
                count += 1
        if count > 1:
            dx /= count
            dy /= count
        nlen = math.sqrt(dx * dx + dy * dy)
        normals.append((dx / nlen, dy / nlen) if nlen > MIN_SEGMENT else (0.0, 1.0))
    return [(pts[i][0] + normals[i][0] * offset, pts[i][1] + normals[i][1] * offset) for i in range(n)]


def fillet_polyline(sharp_pts, radius):
    """LeafDrawRoadCommand.DrawFilleted, :177-241: each interior vertex becomes two
    trim-back vertices joined by an arc (the bulge on the first); skipped at a
    near-collinear vertex or where the trim-back exceeds half an adjacent segment.
    Returns (vertices, bulges), one bulge per vertex."""
    pts = [(float(x), float(y)) for x, y in sharp_pts]
    verts = [(pts[0][0], pts[0][1], 0.0)]
    for i in range(1, len(pts) - 1):
        prev, here, nxt = pts[i - 1], pts[i], pts[i + 1]
        in_x, in_y = here[0] - prev[0], here[1] - prev[1]
        out_x, out_y = nxt[0] - here[0], nxt[1] - here[1]
        in_len = math.sqrt(in_x * in_x + in_y * in_y)
        out_len = math.sqrt(out_x * out_x + out_y * out_y)
        if in_len < MIN_SEGMENT or out_len < MIN_SEGMENT:
            verts.append((here[0], here[1], 0.0))
            continue
        din_x, din_y = in_x / in_len, in_y / in_len
        dout_x, dout_y = out_x / out_len, out_y / out_len
        cos_def = din_x * dout_x + din_y * dout_y
        if cos_def >= FILLET_COLLINEAR_COS:
            verts.append((here[0], here[1], 0.0))
            continue
        cross_z = din_x * dout_y - din_y * dout_x
        defl = math.acos(max(-1.0, min(1.0, cos_def)))
        interior = math.pi - defl
        tan_half = math.tan(interior / 2.0)
        trim = radius / tan_half if tan_half != 0.0 else math.inf     # C# double division
        max_trim = min(in_len, out_len) * 0.5
        if trim > max_trim or math.isinf(trim) or math.isnan(trim):
            verts.append((here[0], here[1], 0.0))
            continue
        a = (here[0] - din_x * trim, here[1] - din_y * trim)
        b = (here[0] + dout_x * trim, here[1] + dout_y * trim)
        bulge = (-1.0 if cross_z > 0 else 1.0) * math.tan(defl / 4.0)   # :223-224
        verts.append((a[0], a[1], bulge))
        verts.append((b[0], b[1], 0.0))
    verts.append((pts[-1][0], pts[-1][1], 0.0))
    return [(x, y) for x, y, _ in verts], [bg for _, _, bg in verts]


def polyline_length(pts):
    """LeafDrawRoadCommand.PolylineLength, :243-253 (and Polyline.Length of a line with
    no arcs, RoadCommand.cs:159)."""
    total = 0.0
    for i in range(len(pts) - 1):
        dx = pts[i + 1][0] - pts[i][0]
        dy = pts[i + 1][1] - pts[i][1]
        total += math.sqrt(dx * dx + dy * dy)
    return total


def _line(layer, vertices, bulges=None):
    vertices = [tuple(v) for v in vertices]
    return {"layer": layer, "role": ROLE_BY_LAYER[layer], "vertices": vertices,
            "bulges": list(bulges) if bulges is not None else [0.0] * len(vertices), "closed": False}


def _positive_prompt(value, default, what):
    """PromptDouble with AllowZero and AllowNegative false; None takes the default."""
    if value is None:
        return default
    v = _finite(value, what)
    if v <= 0:
        raise BuildoutInputError(f"{what} must be > 0")
    return v


def _centerline(centerline):
    pts = _points(centerline, "centerline", MAX_CENTERLINE_VERTICES)
    return pts


def draw_road(centerline, drawn=True, width=None, radius=None, offset=None, runtime=RUNTIME_NET8):
    """LEAFDRAWROAD end to end, LeafDrawRoadCommand.cs:26-114. centerline: the picked
    centerline vertices; drawn is True when the command draws it from two picked points
    (Enter at the selection prompt, :143-170), on the current layer. Returns {"succeeded",
    "message", "lines", "centerline_length"}: lines in drawing order (the drawn centerline,
    then the two edges and the two offset guides, each filleted by the rounding radius)."""
    w = _positive_prompt(width, DRAW_ROAD_DEFAULT_WIDTH, "road width")          # :38
    r = _positive_prompt(radius, DRAW_ROAD_DEFAULT_RADIUS, "edge rounding radius")  # :44
    o = _positive_prompt(offset, DRAW_ROAD_DEFAULT_OFFSET, "offset from road")  # :50
    pts = _centerline(centerline)
    if drawn and len(pts) != 2:
        raise BuildoutInputError("a drawn centerline is two picked points")
    lines = [_line(CURRENT_LAYER, pts)] if drawn else []
    out = {"succeeded": False, "message": None, "lines": lines, "centerline_length": None}
    if len(pts) < 2:                                                      # :77-82
        out["message"] = "LEAFDRAWROAD: centerline has fewer than 2 vertices, aborting."
        return out
    half = w / 2.0                                                        # :84
    for signed, layer in ((+half, DRAW_ROAD_EDGE_LAYER), (-half, DRAW_ROAD_EDGE_LAYER),
                          (+(half + o), DRAW_ROAD_OFFSET_LAYER), (-(half + o), DRAW_ROAD_OFFSET_LAYER)):
        vertices, bulges = fillet_polyline(compute_offset_polyline(pts, signed), r)   # :92-99
        lines.append(_line(layer, vertices, bulges))
    length = polyline_length(pts)                                         # :103
    out.update(succeeded=True, centerline_length=length,
               message=(f"LEAFDRAWROAD: road width={fmt(w, 2, runtime)} radius={fmt(r, 2, runtime)} "
                        f"offset={fmt(o, 2, runtime)} centerline-length={fmt(length, 2, runtime)}"))
    return out


def road_midpoint(pts, total_length):
    """RoadCommand.GetMidpoint, :382-403: the point at half the length along the line."""
    target = total_length / 2.0
    walked = 0.0
    for i in range(len(pts) - 1):
        dx = pts[i + 1][0] - pts[i][0]
        dy = pts[i + 1][1] - pts[i][1]
        seg = math.sqrt(dx * dx + dy * dy)
        if walked + seg >= target:
            t = (target - walked) / seg if seg > MIN_SEGMENT else 0.0
            return (pts[i][0] + t * dx, pts[i][1] + t * dy)
        walked += seg
    return pts[-1]


def road_cross_section(origin, road_width_du, shoulder_width_du, cross_slope_pct, meters_per_unit, runtime=RUNTIME_NET8):
    """RoadCommand.DrawCrossSection, :325-380: the five-point surface profile (10x vertical
    exaggeration) and its label. Returns (profile vertices, label)."""
    ox, oy = origin
    road_w_m = road_width_du * meters_per_unit
    shoulder_m = shoulder_width_du * meters_per_unit
    half_total = road_w_m / 2.0 + shoulder_m
    crown_h = road_w_m / 2.0 * (cross_slope_pct / 100.0)
    shoulder_slope = max(cross_slope_pct, XSEC_MIN_SHOULDER_SLOPE_PCT) / 100.0
    shoulder_h = crown_h + shoulder_m * shoulder_slope
    x_scale = 1.0 / meters_per_unit
    y_scale = x_scale * XSEC_VERTICAL_EXAGGERATION
    profile = [(ox + (-half_total) * x_scale, oy + 0.0),
               (ox + (-road_w_m / 2.0) * x_scale, oy + (shoulder_h - crown_h) * y_scale),
               (ox + 0.0 * x_scale, oy + shoulder_h * y_scale),
               (ox + (road_w_m / 2.0) * x_scale, oy + (shoulder_h - crown_h) * y_scale),
               (ox + half_total * x_scale, oy + 0.0)]
    height = max(road_width_du * XSEC_LABEL_HEIGHT_FACTOR, 0.5 / meters_per_unit)
    text = (f"CROSS-SECTION (1:{int(XSEC_VERTICAL_EXAGGERATION)} V.E.)\\P"
            f"Road: {fmt(road_w_m, 1, runtime)} m  |  Shoulder: {fmt(shoulder_m, 1, runtime)} m  |  "
            f"Xslope: {fmt(cross_slope_pct, 1, runtime)}%")
    label = {"layer": ROAD_XSEC_LAYER, "text": text, "at": (ox, oy - height * XSEC_LABEL_BELOW_HEIGHTS),
             "height": height}
    return profile, label


def road_design(centerline, drawn=True, meters_per_unit=1.0, width_du=None, shoulder_du=None,
                cross_slope_pct=None, runtime=RUNTIME_NET8):
    """LEAFROAD end to end, RoadCommand.cs:30-235. centerline: the picked vertices (a line
    with no arcs); drawn is True when the command draws it from two points on LEAF-ROAD.
    Returns {"succeeded", "message", "lines", "labels", "summary", "messages"}: lines in
    drawing order (the drawn centerline, the carriageway and shoulder edges, the cross
    section), summary {"centerline_length_m", "carriageway_m", "total_width_m",
    "surface_area_m2", "cross_slope_pct"}."""
    mpu = _mpu(meters_per_unit)
    unit_label = "ft" if mpu < FEET_LABEL_BELOW_MPU else "m"               # :46
    pts = _centerline(centerline)
    if drawn and len(pts) != 2:
        raise BuildoutInputError("a drawn centerline is two picked points")
    lines = [_line(ROAD_LAYER, pts)] if drawn else []                     # :78-91
    width = _positive_prompt(width_du, round(ROAD_DEFAULT_WIDTH_M / mpu, 2), "carriageway width")  # :97-109
    if shoulder_du is None:
        shoulder = round(ROAD_DEFAULT_SHOULDER_M / mpu, 2)                # :111
    else:
        shoulder = _finite(shoulder_du, "shoulder width")
        if shoulder < 0:
            raise BuildoutInputError("shoulder width must not be negative")
    if cross_slope_pct is None:
        slope = ROAD_DEFAULT_CROSS_SLOPE_PCT                              # :128
    else:
        slope = _finite(cross_slope_pct, "cross slope")
        if slope < 0:
            raise BuildoutInputError("cross slope must not be negative")
    slope = min(slope, ROAD_MAX_CROSS_SLOPE_PCT)                          # :137
    out = {"succeeded": False, "message": None, "lines": lines, "labels": [], "summary": None,
           "messages": []}
    if len(pts) < 2:                                                      # :163-167
        out["message"] = "Centerline has fewer than 2 vertices - aborting."
        return out
    length_du = polyline_length(pts)                                      # :159
    half_road = width / 2.0
    half_total = half_road + shoulder
    for signed in (+half_road, -half_road, +half_total, -half_total):     # :185-188
        lines.append(_line(ROAD_EDGE_LAYER, compute_offset_polyline(pts, signed)))
    mid_x, mid_y = road_midpoint(pts, length_du)                          # :195
    profile, label = road_cross_section((mid_x, mid_y - half_total * XSEC_BELOW_FACTOR), width, shoulder,
                                        slope, mpu, runtime)              # :196-199
    lines.append(_line(ROAD_XSEC_LAYER, profile))
    length_m = length_du * mpu                                            # :207-210
    carriageway_m = width * mpu
    total_width_m = (width + 2 * shoulder) * mpu
    area_m2 = length_m * carriageway_m
    out["messages"] = ["LEAFROAD \u2014 Road design summary:",           # :212-218
                       f"  Centerline length   : {fmt(length_m, 1, runtime)} m",
                       f"  Carriageway width   : {fmt(carriageway_m, 1, runtime)} m",
                       f"  Total width (w/ shoulders): {fmt(total_width_m, 1, runtime)} m",
                       f"  Carriageway surface area  : {fmt(area_m2, 0, runtime)} m\u00b2",
                       f"  Cross-slope         : {fmt(slope, 1, runtime)} %",
                       "  Cross-section view placed below centerline midpoint."]
    out.update(succeeded=True, labels=[label], unit_label=unit_label,
               summary={"centerline_length_m": length_m, "carriageway_m": carriageway_m,
                        "total_width_m": total_width_m, "surface_area_m2": area_m2, "cross_slope_pct": slope})
    return out
