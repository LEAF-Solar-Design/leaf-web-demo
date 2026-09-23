"""Studio port of the plugin's terrain analytics engines: LEAFSLOPE, LEAFTERRAINCSV,
LEAFGRADE and LEAFSURVEY.

Literal ports of Branch2025 (read 2026-09-23 at C:/tmp/solar-parity/wt-b25-s17):

  LeafSlopeCommand.cs                 LEAFSLOPE: classify every grid cell, one flat SOLID
                                      per cell on LEAF-SLOPE with an explicit ACI colour
  Terrain/SlopeHeatmapCalculator.cs   ComputeCells (max edge slope per cell), AciForBucket
  Terrain/LandXmlExportCommand.cs     LEAFTERRAINCSV (:117-200): the file it writes
  Terrain/TerrainExporter.cs          ToCsv (:50-68): header, node order, F3 formatting
  Terrain/GradeCommand.cs             LEAFGRADE (:29-320): Auto/Manual/Clearance target,
                                      pad polyline, centroid label, the grading setting
  Terrain/GradingCalculator.cs        ComputeFlat, FindBalancedElevation,
                                      FindClearanceElevation, IsInsidePolygon
  Terrain/SurveyCommand.cs            LEAFSURVEY (:19-138): recolour the selected faces
  Terrain/SurveyFaceAnalyzer.cs       Analyze, MaxEdgeSlope, EdgeSlopePercent
  Terrain/TerrainGridInterpolator.cs  InterpolateZ (:149-185), GetElevationRange
                                      (:190-203), AllNodes (:209-222), the default frame
                                      (:59-65)
  Terrain/TerrainMeshBuilder.cs       ClassifySlope (:51-56)
  Terrain/TerrainColors.cs            BucketColor (:10-16)
  Terrain/TerrainUnitsPrompt.cs       the units prompt, default Meters

Pure functions over NEUTRAL structures, no AutoCAD, no I/O, no network:
  grid     {"elevations": row-major metres, "rows", "cols", "x_min", "x_max", "y_min",
           "y_max" (drawing units), optional "frame": six numbers (origin x, origin y,
           x axis x, x axis y, y axis x, y axis y)}; the same neutral grid
           solar_ground_terrain.neutral_grid commits and reads.
  boundary [[x, y], ...] in drawing order (drawing units).
  face     four [x, y, z] corners in entity order (a triangle repeats its third).
How the plugin stores any of these in a drawing is not part of this module.

Floating point work is ordered exactly as the C# orders it (IEEE doubles on both
sides, left-to-right evaluation, sequential accumulation), so cell slopes, the balanced
grade and the exported text reproduce the plugin's values bit for bit. Number
formatting reproduces .NET's "F<n>" under the invariant culture (see format_fixed).

Every input is bounded and every malformed input fails closed with AnalysisInputError
(a ValueError); a bound breach raises AnalysisBoundsError. The plugin has no bounds;
these refuse inputs that would pin a worker instead of hanging it.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, localcontext
import math
from numbers import Real

# ---------------------------------------------------------------------------
#  Constants (each cites the line that defines it)
# ---------------------------------------------------------------------------

SLOPE_LAYER = "LEAF-SLOPE"                       # LeafSlopeCommand.cs:28
SLOPE_LAYER_COLOR_INDEX = 3                      # LeafSlopeCommand.cs:187
GRADE_LAYER = "LEAF-GRADE"                       # LayerNames.LeafGrade, GradeCommand.cs:244, :252
GRADE_LAYER_COLOR_INDEX = 3                      # GradeCommand.cs:244

SLOPE_GREEN_THRESHOLD = 5.0                      # TerrainMeshBuilder.cs:36
SLOPE_YELLOW_THRESHOLD = 15.0                    # TerrainMeshBuilder.cs:39
BUCKETS = ("Green", "Yellow", "Red")
BUCKET_ACI = {"Green": 3, "Yellow": 2, "Red": 1}  # SlopeHeatmapCalculator.cs:101-110
OTHER_BUCKET_ACI = 7                             # SlopeHeatmapCalculator.cs:108
BUCKET_RGB = {                                   # TerrainColors.cs:13-15
    "Green": (0, 200, 0),
    "Yellow": (255, 200, 0),
    "Red": (220, 0, 0),
}

METERS_KEYWORD = "Meters"                        # TerrainUnitsPrompt.cs:10
FEET_KEYWORD = "Feet"                            # TerrainUnitsPrompt.cs:11
FEET_METERS_PER_UNIT = 0.3048                    # TerrainUnitsPrompt.cs:12

CSV_HEADER = "X,Y,Z"                             # TerrainExporter.cs:58
# StringBuilder.AppendLine writes Environment.NewLine, "\r\n" on the Windows host the
# plugin runs on (TerrainExporter.cs:58, :64); File.WriteAllText(..., Encoding.UTF8)
# prefixes the UTF-8 byte order mark (LandXmlExportCommand.cs:180).
CSV_NEWLINE = "\r\n"
UTF8_BOM = b"\xef\xbb\xbf"
CSV_DIGITS = 3                                   # "{0:F3},{1:F3},{2:F3}", TerrainExporter.cs:64

GRADE_CELL_DIVISOR = 50.0                        # GradeCommand.cs:141-143, :169-171, :230-232
GRADE_MIN_CELL_DU = 1.0                          # GradeCommand.cs:143
BALANCE_MAX_ITERATIONS = 30                      # GradingCalculator.cs:156
BALANCE_TOLERANCE_M = 1e-3                       # GradingCalculator.cs:178
DEFAULT_CLEARANCE_M = 0.6                        # GradeCommand.cs:154
LABEL_HEIGHT_FACTOR = 0.05                       # GradeCommand.cs:260
MIN_LABEL_HEIGHT_M = 0.5                         # GradeCommand.cs:261
LABEL_PREFIX = "GRADE PAD\\P"                    # GradeCommand.cs:268 (MText paragraph break)
FEET_LABEL_BELOW_MPU = 0.5                       # GradeCommand.cs:45: "ft" when metersPerUnit < 0.5
GRADE_MODES = ("Auto", "Manual", "Clearance")    # GradeCommand.cs:124-126, default Auto (:132)
GRADING_MODE_SLOPE = 0                           # DrawingPropertiesJson.cs:27
GRADING_MODE_CLEARANCE = 1                       # DrawingPropertiesJson.cs:29
# The drawing settings LEAFGRADE writes (GradeCommand.cs:300-303), by product setting name.
GRADING_ELEVATION_SETTING = "GradingElevationM"
GRADING_MODE_SETTING = "GradingMode"

SURVEY_MIN_HORIZONTAL_M = 1e-9                   # SurveyFaceAnalyzer.cs:113

# The two .NET runtimes the plugin ships on (LeafSolarDesign20xx.csproj): net48 for
# AutoCAD 2018 to 2024, net8.0-windows for 2025 and 2026. They format "F<n>" differently
# (see format_fixed).
RUNTIME_NET8 = "net8"
RUNTIME_NETFX = "netfx"
RUNTIMES = (RUNTIME_NET8, RUNTIME_NETFX)

# Studio-side bounds.
MAX_GRID_NODES = 4_000_000
MAX_FACES = 500_000
MAX_BOUNDARY_VERTICES = 20_000
MAX_GRADE_SAMPLES = 4_000_000        # sample points one ComputeFlat pass may visit
MAX_BALANCE_ITERATIONS = 64
MAX_FORMAT_MAGNITUDE = 1e15          # |value| a fixed-point number may have (15 integer digits)


class AnalysisInputError(ValueError):
    """Malformed input: the engine refuses rather than guess."""


class AnalysisBoundsError(AnalysisInputError):
    """Input exceeds a Studio bound (grid size, face count, grade samples)."""


# ---------------------------------------------------------------------------
#  Validation helpers (fail closed)
# ---------------------------------------------------------------------------

def _finite(value, what):
    if isinstance(value, bool) or not isinstance(value, Real):
        raise AnalysisInputError(f"{what} must be a number, got {type(value).__name__}")
    v = float(value)
    if not math.isfinite(v):
        raise AnalysisInputError(f"{what} must be finite, got {v!r}")
    return v


def _int(value, what):
    if isinstance(value, bool) or not isinstance(value, int):
        raise AnalysisInputError(f"{what} must be an integer, got {type(value).__name__}")
    return value


def _meters_per_unit(value):
    """Every caller refuses metersPerUnit <= 0 (TerrainExporter.cs:54-55,
    GradingCalculator.cs:71-72, SurveyFaceAnalyzer.cs:57)."""
    v = _finite(value, "meters_per_unit")
    if v <= 0.0:
        raise AnalysisInputError("metersPerUnit must be > 0")
    return v


def meters_per_unit_for_keyword(keyword=None):
    """TerrainUnitsPrompt.TryPromptMetersPerDrawingUnit, :40-58: None is the empty
    answer and takes the default, Meters, for all four commands (LeafSlopeCommand.cs:146,
    LandXmlExportCommand.cs:208, GradeCommand.cs:42, SurveyCommand.cs:32)."""
    if keyword is None:
        return 1.0
    if isinstance(keyword, str) and keyword.upper() in (METERS_KEYWORD.upper(), FEET_KEYWORD.upper()):
        return FEET_METERS_PER_UNIT if keyword.upper() == FEET_KEYWORD.upper() else 1.0
    raise AnalysisInputError(f"units keyword must be {METERS_KEYWORD} or {FEET_KEYWORD}")


def _boundary(boundary):
    if not isinstance(boundary, (list, tuple)):
        raise AnalysisInputError("boundary must be a list of [x, y] vertices")
    if len(boundary) > MAX_BOUNDARY_VERTICES:
        raise AnalysisBoundsError(f"{len(boundary)} boundary vertices exceed the bound of {MAX_BOUNDARY_VERTICES}")
    out = []
    for i, v in enumerate(boundary):
        if not isinstance(v, (list, tuple)) or len(v) != 2:
            raise AnalysisInputError(f"boundary[{i}] must be an [x, y] pair")
        out.append((_finite(v[0], f"boundary[{i}].x"), _finite(v[1], f"boundary[{i}].y")))
    return out


# ---------------------------------------------------------------------------
#  Slope buckets and colours
# ---------------------------------------------------------------------------

def classify_slope(slope_percent):
    """TerrainMeshBuilder.ClassifySlope, :51-56: < 5 Green, <= 15 Yellow, else Red."""
    if slope_percent < SLOPE_GREEN_THRESHOLD:
        return "Green"
    if slope_percent <= SLOPE_YELLOW_THRESHOLD:
        return "Yellow"
    return "Red"


def aci_for_bucket(bucket):
    """SlopeHeatmapCalculator.AciForBucket, :101-110."""
    return BUCKET_ACI.get(bucket, OTHER_BUCKET_ACI)


def bucket_true_color(bucket):
    """TerrainColors.BucketColor, :10-16: Color.FromRgb, as the integer r<<16 | g<<8 | b.
    Any bucket other than Green or Yellow takes Red's colour."""
    r, g, b = BUCKET_RGB.get(bucket, BUCKET_RGB["Red"])
    return (r << 16) | (g << 8) | b


# ---------------------------------------------------------------------------
#  The terrain grid (TerrainGridInterpolator.cs)
# ---------------------------------------------------------------------------

class TerrainGrid:
    """The neutral grid read as TrackerCommand.TryReadTerrainInterpolator builds its
    TerrainGridInterpolator. Every value finite (fails closed where the plugin would
    carry NaN into its output)."""

    __slots__ = ("elevations", "rows", "cols", "x_min", "x_max", "y_min", "y_max",
                 "meters_per_unit", "origin_x", "origin_y", "x_axis_x", "x_axis_y",
                 "y_axis_x", "y_axis_y")

    def __init__(self, grid, meters_per_unit):
        if not isinstance(grid, dict):
            raise AnalysisInputError("grid must be a dict")
        rows = _int(grid.get("rows"), "grid.rows")
        cols = _int(grid.get("cols"), "grid.cols")
        if rows < 0 or cols < 0 or rows * cols > MAX_GRID_NODES:
            raise AnalysisBoundsError(f"grid {rows}x{cols} is out of bounds")
        elevations = grid.get("elevations")
        if not isinstance(elevations, (list, tuple)) or len(elevations) != rows * cols:
            raise AnalysisInputError(f"grid.elevations must hold rows * cols = {rows * cols} values")
        self.elevations = [_finite(e, "grid elevation") for e in elevations]
        self.rows, self.cols = rows, cols
        self.x_min = _finite(grid.get("x_min"), "grid.x_min")
        self.x_max = _finite(grid.get("x_max"), "grid.x_max")
        self.y_min = _finite(grid.get("y_min"), "grid.y_min")
        self.y_max = _finite(grid.get("y_max"), "grid.y_max")
        mpu = _finite(meters_per_unit, "meters_per_unit")
        self.meters_per_unit = mpu if mpu > 0 else 1.0                      # :97
        frame = grid.get("frame")
        if frame is None:                                                   # :59-65
            frame = (self.x_min, self.y_min, self.x_max - self.x_min, 0.0,
                     0.0, self.y_max - self.y_min)
        elif not isinstance(frame, (list, tuple)) or len(frame) != 6:
            raise AnalysisInputError("grid.frame must be six numbers (origin, x axis, y axis)")
        (self.origin_x, self.origin_y, self.x_axis_x, self.x_axis_y,
         self.y_axis_x, self.y_axis_y) = (_finite(v, "grid.frame") for v in frame)

    def elevation_range(self):
        """GetElevationRange, :190-203."""
        if not self.elevations:
            return (0.0, 0.0)
        lo = hi = self.elevations[0]
        for e in self.elevations:
            if e < lo:
                lo = e
            if e > hi:
                hi = e
        return (lo, hi)

    def interpolate_z(self, x, y):
        """InterpolateZ, :149-185: bilinear metres, None outside the grid."""
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
        col_left = min(int(frac_x), cols - 2)
        row_bot = min(int(frac_y), rows - 2)
        tx = frac_x - col_left
        ty = frac_y - row_bot
        e = self.elevations
        e_bl = e[row_bot * cols + col_left]
        e_br = e[row_bot * cols + col_left + 1]
        e_tl = e[(row_bot + 1) * cols + col_left]
        e_tr = e[(row_bot + 1) * cols + col_left + 1]
        return ((1 - ty) * ((1 - tx) * e_bl + tx * e_br)
                + ty * ((1 - tx) * e_tl + tx * e_tr))

    def all_nodes(self):
        """AllNodes, :209-222: (x, y, elevation metres) row-major, x and y in drawing
        units through the frame. A generator: no list of the whole grid is built."""
        rows, cols = self.rows, self.cols
        row_div, col_div = max(1, rows - 1), max(1, cols - 1)
        for r in range(rows):
            v = r / row_div
            for c in range(cols):
                u = c / col_div
                yield (self.origin_x + self.x_axis_x * u + self.y_axis_x * v,
                       self.origin_y + self.x_axis_y * u + self.y_axis_y * v,
                       self.elevations[r * cols + c])


def terrain_grid(grid, meters_per_unit):
    """TryReadTerrainInterpolator: None when the drawing has no grid, else the grid
    (a malformed grid raises)."""
    return None if grid is None else TerrainGrid(grid, meters_per_unit)


# ---------------------------------------------------------------------------
#  Number formatting (.NET "F<n>", CultureInfo.InvariantCulture)
# ---------------------------------------------------------------------------

def format_fixed(value, digits, runtime=RUNTIME_NET8):
    """value.ToString("F<digits>", CultureInfo.InvariantCulture), no allocation beyond
    one Decimal.

    net8 (AutoCAD 2025 and later): the exact binary value rounded to `digits` places,
    ties away from zero; a negative value that rounds to zero keeps its sign ("-0.000").
    netfx (net48, AutoCAD 2024 and earlier): the value is first taken to 15 significant
    digits, then rounded the same way, and a zero result never carries a sign."""
    v = _finite(value, "formatted value")
    digits = _int(digits, "digits")
    if not 0 <= digits <= 15:
        raise AnalysisInputError("digits must be 0 to 15")
    if abs(v) >= MAX_FORMAT_MAGNITUDE:
        raise AnalysisBoundsError(f"{v!r} is too large to format as fixed point")
    if runtime == RUNTIME_NET8:
        exact = Decimal(v)
    elif runtime == RUNTIME_NETFX:
        exact = Decimal(f"{v:.14e}")
    else:
        raise AnalysisInputError(f"runtime must be one of {RUNTIMES}")
    with localcontext() as ctx:
        ctx.prec = 64
        text = format(exact.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP), "f")
    if runtime == RUNTIME_NETFX and text.startswith("-") and not text.strip("-0."):
        text = text[1:]
    return text


# ---------------------------------------------------------------------------
#  LEAFSLOPE (LeafSlopeCommand.cs, SlopeHeatmapCalculator.cs)
# ---------------------------------------------------------------------------

def compute_slope_cells(grid, meters_per_unit=1.0):
    """SlopeHeatmapCalculator.ComputeCells, :45-95: one cell per grid quad, row-major
    (row outer, column inner), each {"x0", "y0", "x1", "y1", "slope_percent", "bucket",
    "color_index"}. The slope is the largest of the four edge slopes, each |rise| over
    one cell step in metres, in percent. Under 2x2 nodes, or a non-positive span or
    spacing, there are no cells. O(rows * cols), one list of cells."""
    terrain = grid if isinstance(grid, TerrainGrid) else TerrainGrid(grid, meters_per_unit)
    rows, cols = terrain.rows, terrain.cols
    if rows < 2 or cols < 2:
        return []
    x_span = terrain.x_max - terrain.x_min
    y_span = terrain.y_max - terrain.y_min
    if x_span <= 0 or y_span <= 0:
        return []
    x_du = x_span / (cols - 1)
    y_du = y_span / (rows - 1)
    x_m = x_du * terrain.meters_per_unit
    y_m = y_du * terrain.meters_per_unit
    if x_m <= 0 or y_m <= 0:
        return []
    elev = terrain.elevations           # AllNodes yields the elevations row-major (:63-67)
    cells = []
    for r in range(rows - 1):
        y0 = terrain.y_min + y_du * r
        y1 = y0 + y_du
        for c in range(cols - 1):
            x0 = terrain.x_min + x_du * c
            x1 = x0 + x_du
            e_bl = elev[r * cols + c]
            e_br = elev[r * cols + (c + 1)]
            e_tl = elev[(r + 1) * cols + c]
            e_tr = elev[(r + 1) * cols + (c + 1)]
            s_bot = abs(e_br - e_bl) / x_m * 100.0
            s_top = abs(e_tr - e_tl) / x_m * 100.0
            s_left = abs(e_tl - e_bl) / y_m * 100.0
            s_right = abs(e_tr - e_br) / y_m * 100.0
            slope = max(max(s_bot, s_top), max(s_left, s_right))
            bucket = classify_slope(slope)
            cells.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1, "slope_percent": slope,
                          "bucket": bucket, "color_index": aci_for_bucket(bucket)})
    return cells


def slope_solid(cell):
    """The 2D SOLID LEAFSLOPE draws for a cell, LeafSlopeCommand.cs:158-169: corners
    BL, BR, TL, TR at z = 0, layer LEAF-SLOPE, explicit ACI colour."""
    x0, y0, x1, y1 = cell["x0"], cell["y0"], cell["x1"], cell["y1"]
    return {"kind": "SOLID", "layer": SLOPE_LAYER, "color_index": cell["color_index"],
            "vertices": [(x0, y0, 0.0), (x1, y0, 0.0), (x0, y1, 0.0), (x1, y1, 0.0)]}


def outcome_counts(cells, green, yellow, red, max_slope):
    """LeafSlopeCommand.BuildOutcomeCounts, :126-141 (Math.Round is banker's rounding,
    as Python's round)."""
    return {"cells": max(0, cells), "green_cells": max(0, green), "yellow_cells": max(0, yellow),
            "red_cells": max(0, red), "max_slope_pct_x100": max(0, int(round(max_slope * 100.0)))}


def slope_heatmap(grid, meters_per_unit=1.0, runtime=RUNTIME_NET8):
    """LEAFSLOPE end to end, LeafSlopeCommand.cs:31-124: what it draws and reports.

    grid None is a drawing with no terrain: nothing is drawn. Returns {"succeeded",
    "message", "cells", "green", "yellow", "red", "max_slope_percent", "counts"}; the
    solids are slope_solid(cell) for each cell, in cell order. LEAFSLOPE never erases
    earlier LEAF-SLOPE solids (DrawCells only appends, :149-176)."""
    mpu = _meters_per_unit(meters_per_unit)
    result = {"succeeded": False, "message": None, "cells": [], "green": 0, "yellow": 0, "red": 0,
              "max_slope_percent": 0.0, "counts": None}
    terrain = terrain_grid(grid, mpu)
    if terrain is None:
        result["message"] = ("LEAFSLOPE: no LEAFTOPO terrain data found. "
                             "Run LEAFTOPO first to import terrain before using LEAFSLOPE.")
        return result
    cells = compute_slope_cells(terrain)
    if not cells:
        result["message"] = ("LEAFSLOPE: terrain grid is too small to classify "
                             "(need at least 2\u00d72 nodes).")
        return result
    green = yellow = red = 0
    max_slope = 0.0
    for c in cells:
        if c["slope_percent"] > max_slope:
            max_slope = c["slope_percent"]
        if c["bucket"] == "Green":
            green += 1
        elif c["bucket"] == "Yellow":
            yellow += 1
        elif c["bucket"] == "Red":
            red += 1
    result.update(succeeded=True, cells=cells, green=green, yellow=yellow, red=red,
                  max_slope_percent=max_slope,
                  counts=outcome_counts(len(cells), green, yellow, red, max_slope))
    result["message"] = (f"LEAFSLOPE complete on layer {SLOPE_LAYER}:"
                         f"\n  Cells classified : {len(cells)}"
                         f"\n  Green  (<5%)     : {green}"
                         f"\n  Yellow (5-15%)   : {yellow}"
                         f"\n  Red    (>15%)    : {red}"
                         f"\n  Max slope        : {format_fixed(max_slope, 2, runtime)}%")
    return result


# ---------------------------------------------------------------------------
#  LEAFTERRAINCSV (LandXmlExportCommand.cs:117-200, TerrainExporter.ToCsv)
# ---------------------------------------------------------------------------

def terrain_csv_text(grid, meters_per_unit=1.0, runtime=RUNTIME_NET8):
    """TerrainExporter.ToCsv, :50-68: "X,Y,Z" then one line per grid node in AllNodes
    order (row-major), x and y scaled to metres, z the stored metres, each "F3"; every
    line ends with "\\r\\n". O(nodes) with one string join."""
    mpu = _meters_per_unit(meters_per_unit)
    terrain = grid if isinstance(grid, TerrainGrid) else TerrainGrid(grid, mpu)
    lines = [CSV_HEADER]
    for x, y, z in terrain.all_nodes():
        lines.append(f"{format_fixed(x * mpu, CSV_DIGITS, runtime)},"
                     f"{format_fixed(y * mpu, CSV_DIGITS, runtime)},"
                     f"{format_fixed(z, CSV_DIGITS, runtime)}")
    return CSV_NEWLINE.join(lines) + CSV_NEWLINE


def terrain_csv_file_bytes(grid, meters_per_unit=1.0, runtime=RUNTIME_NET8):
    """The exact bytes File.WriteAllText(path, csv, Encoding.UTF8) writes
    (LandXmlExportCommand.cs:180): the UTF-8 byte order mark, then the text."""
    return UTF8_BOM + terrain_csv_text(grid, meters_per_unit, runtime).encode("utf-8")


def terrain_csv_export(grid, meters_per_unit=1.0, runtime=RUNTIME_NET8):
    """LEAFTERRAINCSV end to end, :118-200. Returns {"succeeded", "message", "bytes",
    "points", "rows", "cols"}; with no grid the plugin's message and no file. The output
    path (default <drawing>_terrain.csv beside the drawing, :147-171) is the caller's."""
    mpu = _meters_per_unit(meters_per_unit)
    terrain = terrain_grid(grid, mpu)
    if terrain is None:
        return {"succeeded": False, "bytes": None, "points": 0, "rows": 0, "cols": 0,
                "message": "No LEAFTOPO terrain data found. Run LEAFTOPO first to import terrain."}
    data = terrain_csv_file_bytes(terrain, mpu, runtime)
    points = terrain.rows * terrain.cols
    return {"succeeded": True, "bytes": data, "points": points, "rows": terrain.rows,
            "cols": terrain.cols,
            "message": f"LEAFTERRAINCSV - Points: {points}, Grid: {terrain.rows} rows \u00d7 {terrain.cols} cols"}


# ---------------------------------------------------------------------------
#  LEAFGRADE (GradeCommand.cs, GradingCalculator.cs)
# ---------------------------------------------------------------------------

def bounding_box(poly):
    """GradingCalculator.GetBoundingBox, :328-341."""
    min_x = min_y = math.inf
    max_x = max_y = -math.inf
    for x, y in poly:
        if x < min_x:
            min_x = x
        if x > max_x:
            max_x = x
        if y < min_y:
            min_y = y
        if y > max_y:
            max_y = y
    return (min_x, min_y, max_x, max_y)


def is_inside_polygon(poly, px, py):
    """GradingCalculator.IsInsidePolygon, :347-364: even-odd ray cast. The division only
    runs when the edge straddles py, so it never divides by zero."""
    n = len(poly)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _sample_axis(lo, hi, cell):
    """The C# sample loop `for (v = lo + cell * 0.5; v < hi; v += cell)`, accumulated
    exactly as it is (GradingCalculator.cs:84, :86)."""
    v = lo + cell * 0.5
    while v < hi:
        yield v
        v += cell


def _grade_inputs(terrain, boundary, meters_per_unit, cell_size_du):
    if not isinstance(terrain, TerrainGrid):
        raise AnalysisInputError("a terrain grid is required")          # ArgumentNullException
    poly = _boundary(boundary)
    if len(poly) < 3:
        raise AnalysisInputError("Boundary must have at least 3 vertices.")
    cell = _finite(cell_size_du, "cell_size_du")
    if cell <= 0:
        raise AnalysisInputError("cellSizeDu must be > 0.")
    mpu = _meters_per_unit(meters_per_unit)
    box = bounding_box(poly)
    samples = (math.floor((box[2] - box[0]) / cell) + 1) * (math.floor((box[3] - box[1]) / cell) + 1)
    if samples > MAX_GRADE_SAMPLES:
        raise AnalysisBoundsError(f"{samples} grade samples exceed the bound of {MAX_GRADE_SAMPLES}")
    return poly, mpu, cell, box


def compute_flat(terrain, proposed_elev_m, boundary, meters_per_unit=1.0, cell_size_du=1.0):
    """GradingCalculator.ComputeFlat, :58-104: cut and fill (m3) of a flat surface at
    proposed_elev_m over the boundary, sampled at cell centres inside it. Returns
    {"cut_m3", "fill_m3", "net_m3", "samples", "cell_size_du", "proposed_elevation_m"}.
    O(samples * boundary vertices), samples bounded by MAX_GRADE_SAMPLES."""
    poly, mpu, cell, (min_x, min_y, max_x, max_y) = _grade_inputs(terrain, boundary, meters_per_unit,
                                                                  cell_size_du)
    proposed = _finite(proposed_elev_m, "proposed_elev_m")
    cell_area = (cell * mpu) * (cell * mpu)
    cut = fill = 0.0
    samples = 0
    for x in _sample_axis(min_x, max_x, cell):
        for y in _sample_axis(min_y, max_y, cell):
            if not is_inside_polygon(poly, x, y):
                continue
            z = terrain.interpolate_z(x, y)
            if z is None:
                continue
            samples += 1
            delta = z - proposed
            if delta > 0:
                cut += delta * cell_area
            else:
                fill += (-delta) * cell_area
    return {"cut_m3": cut, "fill_m3": fill, "net_m3": cut - fill, "samples": samples,
            "cell_size_du": cell, "proposed_elevation_m": proposed}


def compute_flat_per_area(terrain, pads, meters_per_unit=1.0, cell_size_du=1.0):
    """GradingCalculator.ComputeFlatPerArea, :124-139: one compute_flat per
    (proposed elevation, boundary) pad, in order."""
    if not isinstance(pads, (list, tuple)):
        raise AnalysisInputError("pads must be a list of (elevation, boundary) pairs")
    if not isinstance(terrain, TerrainGrid):
        raise AnalysisInputError("a terrain grid is required")
    return [compute_flat(terrain, elev, boundary, meters_per_unit, cell_size_du) for elev, boundary in pads]


def find_balanced_elevation(terrain, boundary, meters_per_unit=1.0, cell_size_du=1.0,
                            max_iterations=BALANCE_MAX_ITERATIONS):
    """GradingCalculator.FindBalancedElevation, :151-182: bisection between the grid's
    lowest and highest elevation until the bracket is under 1 mm; more cut than fill
    raises the low end. Returns the bracket's midpoint in metres."""
    if not isinstance(terrain, TerrainGrid):
        raise AnalysisInputError("a terrain grid is required")
    iterations = _int(max_iterations, "max_iterations")
    if not 0 <= iterations <= MAX_BALANCE_ITERATIONS:
        raise AnalysisBoundsError(f"max_iterations must be 0 to {MAX_BALANCE_ITERATIONS}")
    _grade_inputs(terrain, boundary, meters_per_unit, cell_size_du)
    lo, hi = terrain.elevation_range()
    for _ in range(iterations):
        mid = (lo + hi) / 2.0
        if compute_flat(terrain, mid, boundary, meters_per_unit, cell_size_du)["net_m3"] > 0:
            lo = mid
        else:
            hi = mid
        if hi - lo < BALANCE_TOLERANCE_M:
            break
    return (lo + hi) / 2.0


def find_clearance_elevation(terrain, boundary, clearance_m, meters_per_unit=1.0, cell_size_du=1.0):
    """GradingCalculator.FindClearanceElevation, :203-244: the highest sampled terrain
    inside the boundary plus the clearance. No sample raises (the plugin's
    InvalidOperationException)."""
    poly, _, cell, (min_x, min_y, max_x, max_y) = _grade_inputs(terrain, boundary, meters_per_unit,
                                                                cell_size_du)
    clearance = _finite(clearance_m, "clearance_m")
    highest = -1.7976931348623157e308       # double.MinValue
    samples = 0
    for x in _sample_axis(min_x, max_x, cell):
        for y in _sample_axis(min_y, max_y, cell):
            if not is_inside_polygon(poly, x, y):
                continue
            z = terrain.interpolate_z(x, y)
            if z is None:
                continue
            if z > highest:
                highest = z
            samples += 1
    if samples == 0:
        raise AnalysisInputError("No terrain data found within the boundary. "
                                 "Ensure LEAFTOPO covers this area before using Clearance mode.")
    return highest + clearance


def grade_cell_size_du(terrain):
    """GradeCommand.cs:141-143: a fiftieth of the grid's X extent, at least 1 unit."""
    return max((terrain.x_max - terrain.x_min) / GRADE_CELL_DIVISOR, GRADE_MIN_CELL_DU)


def _centroid(pts):
    """GradeCommand.Centroid, :675-680: the vertex average."""
    sum_x = sum_y = 0.0
    for x, y in pts:
        sum_x += x
        sum_y += y
    return (sum_x / len(pts), sum_y / len(pts))


def grade_pad(grid, boundary, meters_per_unit=1.0, mode=None, value_du=None, runtime=RUNTIME_NET8):
    """LEAFGRADE end to end with a selected pad polyline, GradeCommand.cs:29-320.

    mode is the target prompt's answer (None is Enter, which is Auto, :132); value_du is
    the Manual elevation or the Clearance height in drawing units (None takes the
    prompt's default: the grid's lowest elevation for Manual, 0.6 m for Clearance; with
    no terrain, the pad elevation, default 0). Returns {"succeeded", "message",
    "elevation_m", "mode", "pad", "label", "cut_fill", "settings"}:
      pad      {"layer", "vertices" (the boundary in its own order), "closed": True}
      label    {"layer", "text", "at" (the vertex centroid), "height"}
      settings the drawing settings the command writes (GradingElevationM, GradingMode)
    On a refusal the plugin's message and no pad."""
    mpu = _meters_per_unit(meters_per_unit)
    poly = _boundary(boundary)
    result = {"succeeded": False, "message": None, "elevation_m": None, "mode": None, "pad": None,
              "label": None, "cut_fill": None, "settings": None}
    if len(poly) < 3:
        result["message"] = "Pad boundary has fewer than 3 vertices - aborting."
        return result
    if mode is not None and (not isinstance(mode, str) or mode.lower() not in (m.lower() for m in GRADE_MODES)):
        raise AnalysisInputError(f"mode must be one of {GRADE_MODES} or None")
    unit_label = "ft" if mpu < FEET_LABEL_BELOW_MPU else "m"
    terrain = terrain_grid(grid, mpu)
    grading_mode = GRADING_MODE_SLOPE
    if terrain is not None:
        cell = grade_cell_size_du(terrain)
        chosen = "Auto" if mode is None else next(m for m in GRADE_MODES if m.lower() == mode.lower())
        if chosen == "Auto":
            target = find_balanced_elevation(terrain, poly, mpu, cell)
        elif chosen == "Clearance":
            grading_mode = GRADING_MODE_CLEARANCE
            clearance_du = DEFAULT_CLEARANCE_M / mpu if value_du is None else _finite(value_du, "clearance")
            if clearance_du < 0:
                raise AnalysisInputError("clearance must not be negative")     # AllowNegative = false
            try:
                target = find_clearance_elevation(terrain, poly, clearance_du * mpu, mpu, cell)
            except AnalysisBoundsError:
                raise
            except AnalysisInputError as exc:
                result["message"] = str(exc)
                return result
        else:
            raw = terrain.elevation_range()[0] / mpu if value_du is None else _finite(value_du, "elevation")
            target = raw * mpu
    else:
        chosen = None
        raw = 0.0 if value_du is None else _finite(value_du, "elevation")
        target = raw * mpu
    cut_fill = (compute_flat(terrain, target, poly, mpu, grade_cell_size_du(terrain))
                if terrain is not None else None)
    min_x, min_y, max_x, max_y = bounding_box(poly)
    height = max(((max_y - min_y) + (max_x - min_x)) / 2.0 * LABEL_HEIGHT_FACTOR,
                 MIN_LABEL_HEIGHT_M / mpu)
    text = f"{LABEL_PREFIX}{format_fixed(target / mpu, 2, runtime)} {unit_label}"
    result.update(succeeded=True, elevation_m=target, mode=chosen, cut_fill=cut_fill,
                  pad={"layer": GRADE_LAYER, "vertices": [list(p) for p in poly], "closed": True},
                  label={"layer": GRADE_LAYER, "text": text, "at": list(_centroid(poly)), "height": height},
                  settings={GRADING_ELEVATION_SETTING: target, GRADING_MODE_SETTING: grading_mode})
    result["message"] = (f"LEAFGRADE - Pad elevation : {format_fixed(target / mpu, 2, runtime)} {unit_label}"
                         f" ({format_fixed(target, 3, runtime)} m)")
    return result


# ---------------------------------------------------------------------------
#  LEAFSURVEY (SurveyCommand.cs, SurveyFaceAnalyzer.cs)
# ---------------------------------------------------------------------------

def _vertex(value, what):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise AnalysisInputError(f"{what} must be an [x, y, z] corner")
    return (_finite(value[0], what), _finite(value[1], what), _finite(value[2], what))


def edge_slope_percent(a, b, meters_per_unit):
    """SurveyFaceAnalyzer.EdgeSlopePercent, :106-116: |dz| over the Euclidean XY
    distance, in percent; a horizontal distance under 1e-9 m is slope 0."""
    dx = (b[0] - a[0]) * meters_per_unit
    dy = (b[1] - a[1]) * meters_per_unit
    horiz = math.sqrt(dx * dx + dy * dy)
    if horiz < SURVEY_MIN_HORIZONTAL_M:
        return 0.0
    delta_z = abs(b[2] - a[2]) * meters_per_unit
    return (delta_z / horiz) * 100.0


def max_edge_slope(p1, p2, p3, p4, meters_per_unit):
    """SurveyFaceAnalyzer.MaxEdgeSlope, :91-101."""
    s12 = edge_slope_percent(p1, p2, meters_per_unit)
    s23 = edge_slope_percent(p2, p3, meters_per_unit)
    s34 = edge_slope_percent(p3, p4, meters_per_unit)
    s41 = edge_slope_percent(p4, p1, meters_per_unit)
    return max(max(s12, s23), max(s34, s41))


def analyze_survey_faces(faces, meters_per_unit=1.0):
    """SurveyFaceAnalyzer.Analyze, :52-80: each face (3 or 4 corners; a triangle repeats
    its third) with its max edge slope and bucket, in input order."""
    if faces is None:
        raise AnalysisInputError("faces must not be None")
    if not isinstance(faces, (list, tuple)):
        raise AnalysisInputError("faces must be a list")
    mpu = _meters_per_unit(meters_per_unit)
    if len(faces) > MAX_FACES:
        raise AnalysisBoundsError(f"{len(faces)} faces exceed the bound of {MAX_FACES}")
    out = []
    for i, quad in enumerate(faces):
        if not isinstance(quad, (list, tuple)) or not 3 <= len(quad) <= 4:
            raise AnalysisInputError("Each face must have 3 or 4 vertices.")
        p1, p2, p3 = (_vertex(quad[k], f"faces[{i}][{k}]") for k in range(3))
        p4 = _vertex(quad[3], f"faces[{i}][3]") if len(quad) == 4 else p3
        slope = max_edge_slope(p1, p2, p3, p4, mpu)
        out.append({"vertices": [p1, p2, p3, p4], "slope_percent": slope, "bucket": classify_slope(slope)})
    return out


def survey_colors(faces, meters_per_unit=1.0):
    """LEAFSURVEY end to end over the selected 3DFACEs in selection order,
    SurveyCommand.cs:32-122: each face is recoloured with its bucket's true colour.
    Returns {"succeeded", "message", "colors" (true-colour integers, face order),
    "buckets", "green", "yellow", "red"}; an empty selection changes nothing."""
    analysed = analyze_survey_faces(faces, meters_per_unit)
    if not analysed:
        return {"succeeded": False, "colors": [], "buckets": [], "green": 0, "yellow": 0, "red": 0,
                "message": "No valid 3D Face entities found in selection."}
    green = yellow = red = 0
    for face in analysed:
        if face["bucket"] == "Green":
            green += 1
        elif face["bucket"] == "Yellow":
            yellow += 1
        else:
            red += 1
    return {"succeeded": True, "colors": [bucket_true_color(f["bucket"]) for f in analysed],
            "buckets": [f["bucket"] for f in analysed], "green": green, "yellow": yellow, "red": red,
            "message": (f"LEAFSURVEY complete - {len(analysed)} faces coloured: "
                        f"{green} green (\u22645%), {yellow} yellow (5-15%), {red} red (>15%).")}
