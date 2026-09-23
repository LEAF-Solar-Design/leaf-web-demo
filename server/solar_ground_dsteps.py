"""Studio port of the plugin's terrain d-step engines (contract G28): LEAFTRENCH,
LEAFSHOWEXPORT / LEAFHIDEEXPORT, LEAFYIELDEXPORT and LEAFTRACKERSTOPANELGROUPS.

Literal ports of Branch2025 (read 2026-09-23 at C:/tmp/solar-parity/wt-b25-s53):

  LeafSolarDesign.Core/TrenchRouting.cs   RoutePath (:240-459), PointInPolygon (:480-494),
                                          DistancePointToSegment (:501-523), BinaryHeap (:531-592)
  Commands.cs                             LEAFTRENCH (:5714-5933): obstacle and existing-trench
                                          collection (:5757-5845), options (:5848-5852), the
                                          polyline's vertices (:5889-5896), the trench record
  LeafSolarDesign.Core/TrenchXData.cs     record defaults: width 0.6 (:58), depth 1.0 (:62),
                                          voltage class MIXED (:66)
  Pvcase/LeafExportSupportCommands.cs     LEAFSHOWEXPORT (:200-264), LEAFHIDEEXPORT (:269-286),
                                          AddRectangle (:304-328), ErasePreviewEntities (:330-346)
  Pvcase/LeafYieldExportCommand.cs        LEAFYIELDEXPORT's bundle (CollectBundle :307-469), the
                                          warnings (:471-522), layout rows (:524-705), shading and
                                          vegetation rows (:371-461, :707-760), cable BOM (:764-878,
                                          :1144-1301), WriteZip (:1303-1356), pile grouping
                                          (:1357-1379), the manifest (:1422-1492)
  LayoutMetricsAggregator.cs              Compute (:74-127), SumPolylineAreas (:135-188),
                                          BuildFrameTypes (:284-308)
  ProjectOverviewBomFormatter.cs          BuildProjectOverviewCsv (:31-72), the total row (:119-246)
  PileRevealBucketer.cs                   Bucket (:94-149), ToPileGroupingCsv (:39-87)
  PileDrawingReader.cs                    ReadPileEntities (:13-51)
  LeafSolarDesign.Core/PileTemplate.cs    DefaultRevealBucketBoundariesM (:107-111)
  Pvcase/LeafTrackersToPanelGroupsCommand.cs  CollectTrackers (:251-317), TryBuildLeafPolylineSpec
                                          (:551-590), TryBuildLeafBlockSpec (:592-616)
  Terrain/TrackerRowReader.cs             through server/solar_ground_buildout.py (its port): the
                                          key-aware reader d3 uses instead of the positional one

Pure functions over NEUTRAL data: polylines as {"layer", "vertices", "closed"}, circles as
{"layer", "center", "radius"}, blocks as {"name", "extents"}, tracker entities as the buildout
port reads them. How the plugin stores anything in a drawing (application names, dictionary
keys) is not part of this module. Floating point work runs in the C# order on IEEE doubles.

Text is the plugin's byte for byte: .NET custom numeric formats ("0.###", "0.##", "0.0##")
format a double from its 15 significant digits and round half away from zero (net8,
Number.Formatting.cs), StringBuilder.AppendLine ends a line with "\\r\\n" on the Windows
host, the zip entries are BOM-less UTF-8, the manifest is Newtonsoft's indented JSON with
LF line ends.

Every input is bounded (MAX_* below) and malformed input fails closed with
DStepsInputError; nothing is cached, every pass is linear in its input except the route,
which is Dijkstra over at most MaxGridCellsPerAxis squared nodes.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP, localcontext
import hashlib
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


_bo = _load_sibling("solar_ground_buildout")      # TrackerRowReader and TrackerBomCalculator ports
_reports = _load_sibling("solar_ground_reports")  # CivilLayers.IsFenceLayer port

# ---------------------------------------------------------------------------
#  Constants (each cites the line that defines it)
# ---------------------------------------------------------------------------

# LEAFTRENCH
TRENCH_LAYER = "LEAF-PVCASE-TRENCH"               # Commands.cs:5754, DrawingPropertiesJson.cs:561
TRENCH_LAYER_COLOR_INDEX = 30                     # Commands.cs:5865
PANEL_GROUP_LAYER = "Panel Group"                 # Settings.settings:57, the obstacle layer (:5762)
INVERTER_NAME_TOKEN = "inverter"                  # Commands.cs:5822
EXISTING_TRENCH_BEND_RADIUS_M = 0.5               # Commands.cs:5808
TRENCH_DEFAULT_WIDTH_M = 0.6                      # TrenchXData.cs:58
TRENCH_DEFAULT_DEPTH_M = 1.0                      # TrenchXData.cs:62
TRENCH_DEFAULT_VOLTAGE_CLASS = "MIXED"            # TrenchXData.cs:66
TRENCH_GRID_STEP_M = 1.0                          # Commands.cs:5850
TRENCH_GRID_PADDING_M = 5.0                       # Commands.cs:5851
OBSTACLE_PENALTY = 1_000_000.0                    # TrenchRouting.cs:163
TRENCH_ALIGNMENT_BONUS = 0.5                      # TrenchRouting.cs:169
MIN_EDGE_WEIGHT = 1e-3                            # TrenchRouting.cs:175
MAX_GRID_CELLS_PER_AXIS = 200                     # TrenchRouting.cs:181
# The 8-connected lattice, N, NE, E, SE, S, SW, W, NW (TrenchRouting.cs:330-331).
D_COL = (0, 1, 1, 1, 0, -1, -1, -1)
D_ROW = (1, 1, 0, -1, -1, -1, 0, 1)

# LEAFSHOWEXPORT / LEAFHIDEEXPORT
PREVIEW_LAYER = "LEAF-EXPORT-PREVIEW"             # LeafExportSupportCommands.cs:134
# The colour rule (:240-248): the array rectangle cyan (ACI 4), the clearance polygon yellow (ACI 2).
ROLE_ARRAY_OUTLINE, ROLE_CLEARANCE = "array-outline", "clearance"
PREVIEW_COLOR_BY_ROLE = {ROLE_ARRAY_OUTLINE: 4, ROLE_CLEARANCE: 2}

# LEAFYIELDEXPORT
LAYER_TREES = "LEAF-PVCASE-SHADING-TREE"          # LeafYieldExportCommand.cs:56
LAYER_STATIONS = "LEAF-PVCASE-SHADING-STATION"    # :57
LAYER_RESTRICT = "LEAF-PVCASE-SHADING-RESTRICTION"  # :58
LAYER_VEGETATION = "LEAF-PVCASE-SHADING-VEGETATION"  # :59, LayerNames.cs:72
LAYER_PILING = "LEAF-PILING"                      # PileDrawingReader.cs:28, LeafPilingCommand
LAYER_TRACKERS = "LEAF-TRACKERS"                  # LayoutMetricsAggregator.cs:160
LAYER_PVCASE_TRACKERS = "PVcase PV Modules (full frames)"  # TrackerRowReader.PvcaseLayerName
LAYER_PVCASE_BOUNDARY = "PVcase PV Area"          # LayoutMetricsAggregator.cs:133
SITE_BOUNDS_LAYER = "0"                           # LeafYieldExportCommand.cs:385
FT_TO_M = 0.3048                                  # PileRevealBucketer.cs:92
SQM_PER_SQFT = 0.09290304                         # ProjectOverviewBomFormatter.cs:15
METERS_PER_FOOT = 0.3048                          # ProjectOverviewBomFormatter.cs:16
CURRENT_DRAWING = "Current drawing"               # LeafYieldExportCommand.cs:318, formatter :36
# Replaces the plugin's retired display name at :1441; the Studio value follows the operator naming law (declared divergence).
EXPORTER = "Leaf Automation"                      # :1441
SCHEMA = "yield-export/1"                         # :1442
UNKNOWN_UNITS = "Unknown"                         # :216
DC_WHICH = ("HomeRunStart", "HomeRunEnd")         # IsDcCableXData, :1184-1188
AC_WHICH = ("Feeder",)                            # IsAcCableXData, :1190-1193
COPPER_LB_PER_CMIL_FT = 0.000003027               # CopperMassLb, :1259
# CopperCircularMils, :1262-1301.
CIRCULAR_MILS = {"14AWG": 4110, "12AWG": 6530, "10AWG": 10380, "8AWG": 16510, "6AWG": 26240,
                 "4AWG": 41740, "3AWG": 52620, "2AWG": 66360, "1AWG": 83690,
                 "1/0AWG": 105600, "0AWG": 105600, "2/0AWG": 133100, "00AWG": 133100,
                 "3/0AWG": 167800, "000AWG": 167800, "4/0AWG": 211600, "0000AWG": 211600}
# The zip's side files in entry order (WriteZip, :1307-1337); manifest.json is written last (:1354).
LAYOUT_CSV_HEADER = "frame_idx,x_du,y_du,kwp,tracker_handle"               # :322
SHADING_CSV_HEADER = "kind,x_du,y_du,radius_du"                            # :371
VEGETATION_CSV_HEADER = ("vegetation_id,centroid_x_du,centroid_y_du,height_du,equivalent_radius_du,"
                         "vertex_count,vertices_du")                        # :372
DC_BOM_HEADER = ("circuit,which,wire_gauge,length_ft,copper_conductors,copper_length_ft,copper_mass_lb,"
                 "polyline_handle,layer")                                   # :766-767
DC_SEGMENT_HEADER = ("circuit,which,wire_gauge,polyline_handle,segment_index,start_x_du,start_y_du,"
                     "end_x_du,end_y_du,length_ft")                        # :768-769
AC_BOM_HEADER = ("circuit,which,wire_gauge,length_ft,current_carrying_conductors,copper_length_ft,"
                 "copper_mass_lb,polyline_handle,layer")                   # :770-771
AC_SEGMENT_HEADER = DC_SEGMENT_HEADER                                      # :772-773
README_TEXT = (                                                            # :1317-1329
    "Leaf Automation - Yield export bundle\n"
    "Upload this ZIP to Yield's 'Edit Layout' flow.\n"
    "manifest.json  - site metadata + export timestamp\n"
    "layout.csv     - frame rollup (kWp)\n"
    "bom_project_overview.csv - project-level BOM rollup\n"
    "dc_homerun_bom.csv - per-circuit DC homerun BOM rollup\n"
    "dc_homerun_segments.csv - DC homerun segment detail\n"
    "ac_feeder_bom.csv - per-circuit AC feeder BOM rollup\n"
    "ac_feeder_segments.csv - AC feeder segment detail\n"
    "shading.csv    - shading objects (trees / stations / fences / vegetation masses)\n"
    "vegetation_masses.csv - woodland-mass boundaries + imported height values\n"
    "Pile_grouping.csv - pile reveal buckets when terrain and piles are present\n")
CRLF = "\r\n"             # StringBuilder.AppendLine on the Windows host (formatter :265, bucketer :42)
MANIFEST_FIELDS_HOST = ("exported_at_utc", "project_name", "drawing_path", "plugin_version")  # G28
INSUNITS_NAMES = {0: "Unitless", 1: "Inches", 2: "Feet", 4: "Millimeters", 5: "Centimeters",
                  6: "Meters"}                                             # InsUnitsToName, :281-293

# LEAFTRACKERSTOPANELGROUPS
TRACKER_EPSILON = 1e-9                            # LeafTrackersToPanelGroupsCommand.cs:42

# Studio-side bounds. The plugin has none; these refuse inputs that would pin a worker.
MAX_ENTITIES = 500_000
MAX_VERTICES = 20_000
MAX_ARRAYS = 1_000
MAX_CABLES = 100_000
MAX_ABS = 1e15            # keeps every custom-format text exact inside Decimal's precision


class DStepsInputError(ValueError):
    """Malformed input: the engine refuses rather than guess."""


# ---------------------------------------------------------------------------
#  Validation helpers (fail closed)
# ---------------------------------------------------------------------------

def _finite(value, what):
    if isinstance(value, bool) or not isinstance(value, Real):
        raise DStepsInputError(f"{what} must be a number, got {type(value).__name__}")
    v = float(value)
    if not math.isfinite(v) or abs(v) > MAX_ABS:
        raise DStepsInputError(f"{what} must be finite and within {MAX_ABS:g}, got {v!r}")
    return v


def _int(value, what):
    if isinstance(value, bool) or not isinstance(value, int) or abs(value) > 2**31 - 1:
        raise DStepsInputError(f"{what} must be a 32-bit integer")
    return value


def _xy(value, what):
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        raise DStepsInputError(f"{what} must be an [x, y] point")
    return (_finite(value[0], f"{what}.x"), _finite(value[1], f"{what}.y"))


def _vertices(value, what):
    if not isinstance(value, (list, tuple)):
        raise DStepsInputError(f"{what} must be a list of [x, y] points")
    if len(value) > MAX_VERTICES:
        raise DStepsInputError(f"{len(value)} {what} vertices exceed the bound of {MAX_VERTICES}")
    return [_xy(p, f"{what}[{i}]") for i, p in enumerate(value)]


def _entities(entities):
    if not isinstance(entities, (list, tuple)):
        raise DStepsInputError("entities must be a list")
    if len(entities) > MAX_ENTITIES:
        raise DStepsInputError(f"{len(entities)} entities exceed the bound of {MAX_ENTITIES}")
    for i, ent in enumerate(entities):
        if not isinstance(ent, dict):
            raise DStepsInputError(f"entities[{i}] must be an object")
        layer = ent.get("layer")
        if layer is not None and not isinstance(layer, str):
            raise DStepsInputError(f"entities[{i}].layer must be a string")
    return entities


def _layer_is(layer, expected):
    """string.Equals(..., StringComparison.OrdinalIgnoreCase)."""
    return isinstance(layer, str) and layer.upper() == expected.upper()


# ---------------------------------------------------------------------------
#  .NET number text (net8, CultureInfo.InvariantCulture)
# ---------------------------------------------------------------------------

def net_custom(value, max_decimals, min_decimals=0):
    """double.ToString("0.<min zeros><#s>") on .NET Core 3.0+: the double is taken to 15
    significant digits (DoublePrecisionCustomFormat, correctly rounded), then rounded half
    away from zero at `max_decimals`; trailing zeros past `min_decimals` are dropped. A
    value that rounds to zero prints without a sign (no digits left, no negative sign)."""
    v = _finite(value, "formatted value")
    with localcontext() as ctx:
        ctx.prec = 64
        d = Decimal(f"{v:.14e}").quantize(Decimal(1).scaleb(-max_decimals), rounding=ROUND_HALF_UP)
        negative = d < 0
        text = format(abs(d), "f")
    whole, _, frac = text.partition(".")
    frac = frac.rstrip("0")
    if len(frac) < min_decimals:
        frac = frac + "0" * (min_decimals - len(frac))
    out = whole + ("." + frac if frac else "")
    return ("-" + out) if negative and d != 0 else out


def net_round_away(value, digits):
    """Math.Round(value, digits, MidpointRounding.AwayFromZero) on .NET Core: scale by
    10^digits, split off the fraction (ModF), step away from zero at a half, unscale."""
    v = _finite(value, "rounded value")
    if abs(v) >= 1e16:
        return v
    power10 = 10.0 ** digits
    v *= power10
    fraction = math.fmod(v, 1.0)
    v = v - fraction
    if abs(fraction) >= 0.5:
        v += math.copysign(1.0, fraction)
    return v / power10


def net_integral_text(value):
    """Math.Round(value).ToString(): an integral double as .NET prints it."""
    v = _finite(value, "integral value")
    if v == 0.0:
        return "-0" if math.copysign(1.0, v) < 0 else "0"
    return str(int(v))


def format_csv_value(value):
    """FormatCsvValue, LeafYieldExportCommand.cs:1236-1245: numbers as "0.###", text as is,
    None as empty, then EscapeCsv."""
    if value is None:
        return ""
    if isinstance(value, bool):
        raise DStepsInputError("a CSV cell is never a boolean")
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, float):
        text = net_custom(value, 3)
    elif isinstance(value, str):
        text = value
    else:
        raise DStepsInputError(f"a CSV cell cannot be {type(value).__name__}")
    return escape_csv(text)


def escape_csv(value):
    """EscapeCsv, :1247-1253."""
    if not value:
        return ""
    if any(c in value for c in ',"\r\n'):
        return '"' + value.replace('"', '""') + '"'
    return value


def csv_row(*values):
    """AppendCsvRow, :1231-1234."""
    return ",".join(format_csv_value(v) for v in values)


# ---------------------------------------------------------------------------
#  LEAFTRENCH (TrenchRouting.cs, Commands.cs)
# ---------------------------------------------------------------------------

class _BinaryHeap:
    """TrenchRouting.BinaryHeap, :531-592: array-backed min-heap keyed by distance; ties
    resolve by the sift rules exactly (strict less-than), which fixes the route among equal
    paths."""

    __slots__ = ("_nodes", "_keys")

    def __init__(self):
        self._nodes = []
        self._keys = []

    def __len__(self):
        return len(self._nodes)

    def push(self, node, key):
        nodes, keys = self._nodes, self._keys
        nodes.append(node)
        keys.append(key)
        i = len(nodes) - 1
        while i > 0:                                                     # SiftUp, :564-576
            parent = (i - 1) // 2
            if keys[i] < keys[parent]:
                nodes[i], nodes[parent] = nodes[parent], nodes[i]
                keys[i], keys[parent] = keys[parent], keys[i]
                i = parent
            else:
                break

    def pop(self):
        nodes, keys = self._nodes, self._keys
        node, key = nodes[0], keys[0]
        last_node, last_key = nodes.pop(), keys.pop()
        count = len(nodes)
        if count > 0:                                                    # :557-561
            nodes[0], keys[0] = last_node, last_key
            i = 0
            while True:                                                  # SiftDown, :578-591
                left = 2 * i + 1
                right = 2 * i + 2
                smallest = i
                if left < count and keys[left] < keys[smallest]:
                    smallest = left
                if right < count and keys[right] < keys[smallest]:
                    smallest = right
                if smallest == i:
                    break
                nodes[i], nodes[smallest] = nodes[smallest], nodes[i]
                keys[i], keys[smallest] = keys[smallest], keys[i]
                i = smallest
        return node, key


def point_in_polygon(px, py, outline):
    """PointInPolygon, TrenchRouting.cs:480-494: ray casting, the ring closed implicitly."""
    n = len(outline)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = outline[i]
        xj, yj = outline[j]
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / ((yj - yi) + 1e-30) + xi):
            inside = not inside
        j = i
    return inside


def distance_point_to_segment(px, py, ax, ay, bx, by):
    """DistancePointToSegment, TrenchRouting.cs:501-523."""
    dx = bx - ax
    dy = by - ay
    len_sq = dx * dx + dy * dy
    if len_sq < 1e-30:
        ex = px - ax
        ey = py - ay
        return math.sqrt(ex * ex + ey * ey)
    t = ((px - ax) * dx + (py - ay) * dy) / len_sq
    if t < 0:
        t = 0
    elif t > 1:
        t = 1
    cx = ax + t * dx
    cy = ay + t * dy
    rx = px - cx
    ry = py - cy
    return math.sqrt(rx * rx + ry * ry)


def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def route_path(start, end, obstacles=(), existing_trenches=(), grid_step_m=1.0, grid_padding_m=5.0,
               obstacle_penalty=OBSTACLE_PENALTY, alignment_bonus=TRENCH_ALIGNMENT_BONUS,
               min_edge_weight=MIN_EDGE_WEIGHT, max_cells_per_axis=MAX_GRID_CELLS_PER_AXIS):
    """TrenchRouting.RoutePath, :240-459. start, end: (x, y). obstacles: outlines (lists of
    (x, y)). existing_trenches: {"start", "end", "width_m"}. Returns {"success", "segments":
    [((x, y), (x, y))], "total_length_m", "error"}. Dijkstra over the padded lattice; the
    route is the lattice edges traversed, not simplified."""
    sx, sy = _xy(start, "start")
    ex, ey = _xy(end, "end")
    step = _finite(grid_step_m, "GridStepM")
    pad = _finite(grid_padding_m, "GridPaddingM")
    if math.sqrt((sx - ex) ** 2 + (sy - ey) ** 2) < 1e-9:                # :253-258
        return {"success": True, "segments": [((sx, sy), (ex, ey))], "total_length_m": 0.0, "error": ""}
    if step <= 0:                                                        # :260-261
        return {"success": False, "segments": [], "total_length_m": 0.0, "error": "GridStepM must be > 0"}
    outlines_in = [_vertices(o, "obstacle outline") for o in obstacles]
    trenches_in = []
    for t in existing_trenches:
        if not isinstance(t, dict):
            raise DStepsInputError("an existing trench is {start, end, width_m}")
        trenches_in.append((_xy(t["start"], "trench start"), _xy(t["end"], "trench end"),
                            _finite(t.get("width_m", TRENCH_DEFAULT_WIDTH_M), "trench width")))
    min_x, min_y = min(sx, ex), min(sy, ey)                              # :264-267
    max_x, max_y = max(sx, ex), max(sy, ey)
    for pts in outlines_in + [[a, b] for a, b, _ in trenches_in]:         # :269-284
        for x, y in pts:
            if x < min_x:
                min_x = x
            if y < min_y:
                min_y = y
            if x > max_x:
                max_x = x
            if y > max_y:
                max_y = y
    min_x -= pad                                                         # :286-289
    min_y -= pad
    max_x += pad
    max_y += pad
    cols = math.ceil((max_x - min_x) / step) + 1                          # :291-292
    rows = math.ceil((max_y - min_y) / step) + 1
    if cols <= 0 or rows <= 0:
        return {"success": False, "segments": [], "total_length_m": 0.0, "error": "degenerate grid extent"}
    if cols > max_cells_per_axis or rows > max_cells_per_axis:            # :296-300
        return {"success": False, "segments": [], "total_length_m": 0.0,
                "error": (f"grid {cols}x{rows} exceeds MaxGridCellsPerAxis ({max_cells_per_axis}); "
                          f"increase GridStepM or shrink the extent")}
    # Snap start and end to the nearest node (:303-306): Math.Round is banker's, as is round().
    s_col = _clamp(int(round((sx - min_x) / step)), 0, cols - 1)
    s_row = _clamp(int(round((sy - min_y) / step)), 0, rows - 1)
    e_col = _clamp(int(round((ex - min_x) / step)), 0, cols - 1)
    e_row = _clamp(int(round((ey - min_y) / step)), 0, rows - 1)
    start_id = s_row * cols + s_col
    end_id = e_row * cols + e_col
    outlines = [o for o in outlines_in if len(o) >= 3]                   # :312-321
    trenches = [(a, b, w) for a, b, w in trenches_in                     # :324-327, LengthM > 1e-9
                if math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) > 1e-9]
    n = cols * rows
    dist = [math.inf] * n
    prev = [-1] * n
    dist[start_id] = 0.0
    heap = _BinaryHeap()
    heap.push(start_id, 0.0)
    reached = False
    while len(heap) > 0:                                                 # :350-416
        u, du = heap.pop()
        if du > dist[u]:
            continue
        if u == end_id:
            reached = True
            break
        ur, uc = divmod(u, cols)
        ux = min_x + uc * step
        uy = min_y + ur * step
        for k in range(8):
            vc = uc + D_COL[k]
            vr = ur + D_ROW[k]
            if vc < 0 or vc >= cols or vr < 0 or vr >= rows:
                continue
            v = vr * cols + vc
            vx = min_x + vc * step
            vy = min_y + vr * step
            mid_x = 0.5 * (ux + vx)
            mid_y = 0.5 * (uy + vy)
            dx = vx - ux
            dy = vy - uy
            base_len = math.sqrt(dx * dx + dy * dy)
            weight = base_len
            for outline in outlines:                                     # :379-388
                if point_in_polygon(mid_x, mid_y, outline):
                    weight += base_len * obstacle_penalty
                    break
            best_bonus = 0.0                                             # :392-405
            for (ax, ay), (bx, by), width in trenches:
                half = 0.5 * max(width, step)
                if distance_point_to_segment(mid_x, mid_y, ax, ay, bx, by) <= half:
                    bonus = base_len * alignment_bonus
                    if bonus > best_bonus:
                        best_bonus = bonus
            weight -= best_bonus
            if weight < min_edge_weight:
                weight = min_edge_weight
            alt = du + weight
            if alt < dist[v]:
                dist[v] = alt
                prev[v] = u
                heap.push(v, alt)
    if not reached or math.isinf(dist[end_id]):                          # :418-422
        return {"success": False, "segments": [], "total_length_m": 0.0,
                "error": "destination unreachable - obstacles fully enclose start or end"}
    path = [end_id]                                                      # :425-436
    cursor = end_id
    safety = n + 1
    while cursor != start_id and safety > 0:
        safety -= 1
        cursor = prev[cursor]
        if cursor < 0:
            break
        path.append(cursor)
    if cursor != start_id:
        return {"success": False, "segments": [], "total_length_m": 0.0, "error": "path reconstruction failed"}
    path.reverse()
    segments = []
    total = 0.0
    for a, b in zip(path, path[1:]):                                     # :442-456
        ar, ac = divmod(a, cols)
        br, bc = divmod(b, cols)
        pa = (min_x + ac * step, min_y + ar * step)
        pb = (min_x + bc * step, min_y + br * step)
        segments.append((pa, pb))
        total += math.sqrt((pa[0] - pb[0]) ** 2 + (pa[1] - pb[1]) ** 2)
    return {"success": True, "segments": segments, "total_length_m": total, "error": ""}


def trench_obstacles(entities, panel_group_layer=PANEL_GROUP_LAYER, trench_layer=TRENCH_LAYER):
    """LEAFTRENCH's model-space scan, Commands.cs:5767-5845, in drawing order: a polyline on
    the panel-group layer with at least three vertices is an obstacle; a polyline on the
    trench layer gives one existing-trench segment per leg (bend radius 0.5, the record's
    default depth and width); a block whose name holds "inverter" is its extents box."""
    obstacles, trenches = [], []
    for ent in _entities(entities):
        kind = ent.get("type")
        layer = ent.get("layer", "")
        if kind == "polyline":
            if panel_group_layer and _layer_is(layer, panel_group_layer):
                verts = _vertices(ent.get("vertices"), "panel group")
                if len(verts) >= 3:
                    obstacles.append(verts)
                continue
            if _layer_is(layer, trench_layer):
                verts = _vertices(ent.get("vertices"), "trench")
                for a, b in zip(verts, verts[1:]):
                    trenches.append({"start": a, "end": b, "width_m": TRENCH_DEFAULT_WIDTH_M,
                                     "depth_m": TRENCH_DEFAULT_DEPTH_M,
                                     "bend_radius_m": EXISTING_TRENCH_BEND_RADIUS_M})
        elif kind == "block":
            name = ent.get("name") or ""
            if not isinstance(name, str) or INVERTER_NAME_TOKEN not in name.lower():
                continue
            ext = ent.get("extents")
            if ext is None:
                continue                                                 # GeometricExtents threw, :5836-5841
            x0, y0, x1, y1 = (_finite(v, "inverter extents") for v in ext)
            obstacles.append([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
    return obstacles, trenches


def trench_command(start, end, entities=()):
    """LEAFTRENCH end to end, Commands.cs:5714-5933, from the two picked points. Returns
    {"succeeded", "message", "trench"}: the trench is {"layer", "vertices" (drawing order:
    the first segment's start, then every segment's end, :5889-5896), "closed": False,
    "depth_m", "width_m", "voltage_class"} (the record the command stamps, :5902-5906)."""
    obstacles, existing = trench_obstacles(entities)
    result = route_path(start, end, obstacles, existing, TRENCH_GRID_STEP_M, TRENCH_GRID_PADDING_M)
    if not result["success"] or not result["segments"]:                  # :5856-5862
        return {"succeeded": False, "trench": None,
                "message": f"LEAFTRENCH: route failed - {result['error']}"}
    segments = result["segments"]
    vertices = [segments[0][0]] + [b for _, b in segments]
    trench = {"layer": TRENCH_LAYER, "vertices": vertices, "closed": False,
              "depth_m": TRENCH_DEFAULT_DEPTH_M, "width_m": TRENCH_DEFAULT_WIDTH_M,
              "voltage_class": TRENCH_DEFAULT_VOLTAGE_CLASS}
    return {"succeeded": True, "trench": trench,
            "message": (f"LEAFTRENCH: trench drawn on {TRENCH_LAYER}, "      # :5916-5920
                        f"length={_bo.fmt(result['total_length_m'], 2)}m, segments={len(segments)}, "
                        f"voltage={TRENCH_DEFAULT_VOLTAGE_CLASS}.")}


# ---------------------------------------------------------------------------
#  LEAFSHOWEXPORT / LEAFHIDEEXPORT (LeafExportSupportCommands.cs)
# ---------------------------------------------------------------------------

def _rectangle(cx, cy, half_x, half_y, cos_a, sin_a):
    """AddRectangle, :304-328: corners (-hx,-hy), (hx,-hy), (hx,hy), (-hx,hy) rotated about
    the centre, in that order."""
    out = []
    for x, y in ((-half_x, -half_y), (half_x, -half_y), (half_x, half_y), (-half_x, half_y)):
        out.append((x * cos_a - y * sin_a + cx, x * sin_a + y * cos_a + cy))
    return out


def show_export(arrays, maintenance_margin_m, prior_preview_count=0):
    """LEAFSHOWEXPORT, :200-264. arrays: the array store in read order (LeafArrayStore.ReadAll),
    each {"centre_x", "centre_y", "half_x", "half_y", "azimuth_deg", "modules_x", "modules_y"}.
    Returns {"succeeded", "message", "erased", "polylines": [{"layer", "role", "color_index",
    "vertices", "closed": True}], "modules_total"}. No arrays prints the plugin's message and
    changes nothing; otherwise any prior preview is erased first (:226)."""
    if not isinstance(arrays, (list, tuple)) or len(arrays) > MAX_ARRAYS:
        raise DStepsInputError(f"arrays must be a list of at most {MAX_ARRAYS} records")
    prior = _int(prior_preview_count, "prior preview count")
    if not arrays:                                                       # :213-222
        return {"succeeded": False, "erased": 0, "polylines": [], "modules_total": 0,
                "message": "LEAFSHOWEXPORT: no arrays defined. Use LEAFDEFINEARRAY first."}
    margin = _finite(maintenance_margin_m, "maintenance margin")
    polylines, modules_total = [], 0
    for a in arrays:                                                     # :234-249
        if not isinstance(a, dict):
            raise DStepsInputError("an array record must be an object")
        modules_total += _int(a["modules_x"], "modules_x") * _int(a["modules_y"], "modules_y")
        a_rad = _finite(a["azimuth_deg"], "azimuth") * math.pi / 180.0
        cos_a, sin_a = math.cos(a_rad), math.sin(a_rad)
        cx, cy = _finite(a["centre_x"], "centre_x"), _finite(a["centre_y"], "centre_y")
        hx, hy = _finite(a["half_x"], "half_x"), _finite(a["half_y"], "half_y")
        polylines.append({"layer": PREVIEW_LAYER, "role": ROLE_ARRAY_OUTLINE,
                          "color_index": PREVIEW_COLOR_BY_ROLE[ROLE_ARRAY_OUTLINE],
                          "vertices": _rectangle(cx, cy, hx, hy, cos_a, sin_a), "closed": True})
        polylines.append({"layer": PREVIEW_LAYER, "role": ROLE_CLEARANCE,
                          "color_index": PREVIEW_COLOR_BY_ROLE[ROLE_CLEARANCE],
                          "vertices": _rectangle(cx, cy, hx + margin, hy + margin, cos_a, sin_a),
                          "closed": True})
    return {"succeeded": True, "erased": prior, "polylines": polylines, "modules_total": modules_total,
            "message": (f"LEAFSHOWEXPORT: drew {len(arrays)} array(s) (cyan) + clearance (yellow) on "
                        f"layer {PREVIEW_LAYER}. Total modules: {modules_total}. Run LEAFHIDEEXPORT to clear.")}


def hide_export(preview_count):
    """LEAFHIDEEXPORT, :269-286: erase every entity on the preview layer."""
    n = _int(preview_count, "preview count")
    return {"erased": n, "message": f"LEAFHIDEEXPORT: erased {n} preview entit{'y' if n == 1 else 'ies'}."}


# ---------------------------------------------------------------------------
#  LEAFTRACKERSTOPANELGROUPS (LeafTrackersToPanelGroupsCommand.cs, as intended)
# ---------------------------------------------------------------------------

def trackers_to_panel_groups(tracker_entities, meters_per_unit=1.0):
    """LEAFTRACKERSTOPANELGROUPS as INTENDED (G28): CollectTrackers (:251-317) keeps every
    tracker with module slots, reading each tracker's row fields through the key-aware row
    reader (the buildout port of TrackerRowReader.cs) instead of the positional read that
    throws on the drawer's rows (:618-637). A footprint polyline also needs a non-degenerate
    axis and cross-axis half width (TryBuildLeafPolylineSpec, :557-578). Each tracker becomes
    one panel group of its slots (the direct path, :693-731). Returns {"trackers",
    "panel_groups_created", "panel_group_slots"}."""
    ents = _entities(tracker_entities)
    trackers, slots = 0, 0
    for ent in ents:
        row = _bo.read_tracker_rows([ent], meters_per_unit)
        if not row or row[0]["module_slots"] <= 0:                       # :560, :598
            continue
        if ent.get("kind") == "polyline":
            (x0, y0), (x1, y1) = _vertices(ent.get("vertices"), "tracker polyline")[:2]
            (ax, ay), (bx, by) = row[0]["axis_start"], row[0]["axis_end"]
            if math.sqrt((bx - ax) ** 2 + (by - ay) ** 2) <= TRACKER_EPSILON:   # :574
                continue
            if math.sqrt((x1 - ax) ** 2 + (y1 - ay) ** 2) <= TRACKER_EPSILON:   # :578
                continue
        trackers += 1
        slots += row[0]["module_slots"]
    return {"trackers": trackers, "panel_groups_created": trackers, "panel_group_slots": slots}


# ---------------------------------------------------------------------------
#  LEAFYIELDEXPORT: layout metrics and the project overview
# ---------------------------------------------------------------------------

def _polygon_area(vertices):
    """Polyline.Area of a straight-edged closed polyline: the shoelace magnitude."""
    s = 0.0
    n = len(vertices)
    for i in range(n):
        x0, y0 = vertices[i]
        x1, y1 = vertices[(i + 1) % n]
        s += x0 * y1 - x1 * y0
    return abs(s) * 0.5


def _row_coverage_du_sq(ent):
    """A tracker row's CoverageAreaDrawingUnitsSq: a footprint polyline's |Area|
    (TrackerRowReader.cs:320-321), a drawn row's axis length times its cross-axis width
    (:645)."""
    if ent.get("kind") == "polyline":
        return _polygon_area(_vertices(ent.get("vertices"), "tracker polyline"))
    (ax, ay), (bx, by) = _xy(ent["axis_start"], "axis_start"), _xy(ent["axis_end"], "axis_end")
    width = _finite(ent.get("cross_axis_width_du", 0.0) or 0.0, "cross_axis_width_du")
    return math.sqrt((bx - ax) ** 2 + (by - ay) ** 2) * max(0.0, width)


def layout_metrics(tracker_entities, module, closed_polylines=(), meters_per_unit=1.0):
    """LayoutMetricsAggregator.Compute, :74-127, for a drawing with no PV area store
    (PvAreaStore.Load returns none, :114-124). closed_polylines: every closed polyline in
    model space, {"layer", "vertices"}, for SumPolylineAreas (:135-188). Returns {"frames",
    "modules", "kwp_total", "piles", "boundary_area_sqm", "coverage_ratio", "frame_types"}."""
    mpu = _finite(meters_per_unit, "meters per unit")
    unit_sq = mpu * mpu
    rows, explicit_sqm, explicit_du_sq = [], 0.0, 0.0
    for ent in _entities(list(tracker_entities)):                        # ReadTrackerRows, :82-88
        row = _bo.read_tracker_rows([ent], mpu)
        if row:
            rows.append(row[0])
            cov = _row_coverage_du_sq(ent)
            explicit_sqm += max(0.0, cov * mpu * mpu)
            explicit_du_sq += max(0.0, cov)
    has_pvcase_rows = explicit_du_sq > 0.0                               # :85
    pvcase_unit_sq = explicit_sqm / explicit_du_sq if explicit_du_sq > 0.0 else unit_sq
    out = {"frames": 0, "modules": 0, "kwp_total": 0.0, "piles": 0, "boundary_area_sqm": 0.0,
           "coverage_ratio": None, "frame_types": []}
    pmax = _finite(module.get("pmax_w", 0.0), "module power")
    if rows:                                                             # :90-99
        bom = _bo.compute_bom(rows, module)
        out.update(frames=bom["total_rows"], modules=bom["total_modules"],
                   kwp_total=bom["total_dc_capacity_kwp"], piles=bom["total_piles"])
        groups = {}                                                      # BuildFrameTypes, :284-308
        for r in rows:
            power = r["module_power_w"] if r["module_power_w"] > 0 else int(round(pmax))
            piles = r["pile_count_override"] if r["pile_count_override"] >= 0 else None
            groups.setdefault((r["module_slots"], power, piles), []).append(r)
        types = []
        for (slots, power, piles), members in groups.items():
            types.append({"module_slots": slots,
                          "string_count": slots // 6 if slots > 0 and slots % 6 == 0 else 0,
                          "count": len(members), "module_power_w": power, "piles_per_frame": piles})
        out["frame_types"] = sorted(types, key=lambda t: t["module_slots"])   # stable OrderBy
    fallback_sq = tracker_sq = pvcase_boundary_sq = 0.0                  # SumPolylineAreas
    for poly in closed_polylines:
        verts = _vertices(poly.get("vertices"), "closed polyline")
        if len(verts) < 3:
            continue
        area = _polygon_area(verts)
        layer = poly.get("layer") or ""
        if _layer_is(layer, LAYER_TRACKERS) or _layer_is(layer, LAYER_PVCASE_TRACKERS):
            tracker_sq += area
        elif _layer_is(layer, LAYER_PVCASE_BOUNDARY):
            pvcase_boundary_sq += area
            if not has_pvcase_rows:
                fallback_sq += area
        elif not layer.upper().startswith("LEAF-"):
            fallback_sq += area
    if has_pvcase_rows and pvcase_boundary_sq > 0.0:                     # :179-187
        boundary_sqm = pvcase_boundary_sq * pvcase_unit_sq
    else:
        boundary_sqm = fallback_sq * unit_sq
    out["boundary_area_sqm"] = boundary_sqm                              # :107-110
    if boundary_sqm > 0:                                                 # :111-112
        out["coverage_ratio"] = min((tracker_sq * unit_sq + explicit_sqm) / boundary_sqm, 1.0)
    return out


def _csv_line(*cells):
    """AppendRow, ProjectOverviewBomFormatter.cs:263-274 (its own EscapeCsv, AppendLine)."""
    def esc(v):
        if v is None:
            return ""
        if "," in v or '"' in v or "\n" in v:
            return '"' + v.replace('"', '""') + '"'
        return v
    return ",".join(esc(c) for c in cells) + CRLF


def project_overview_csv(metrics, project_name=None):
    """BuildProjectOverviewCsv, :31-72, with no area metrics: the header block, then the
    Total row from the drawing-level metrics (BuildTotalAreaMetric, :157-172)."""
    if project_name is None or not str(project_name).strip():
        project_name = CURRENT_DRAWING
    types = metrics["frame_types"]
    total_modules = sum(t["module_slots"] * t["count"] for t in types)           # :233-239
    powered = sorted((t for t in types if t["module_power_w"] > 0), key=lambda t: -t["count"])
    primary_power = float(powered[0]["module_power_w"]) if powered else 0.0       # :248-256
    out = [_csv_line("Project name: " + project_name, "Project name: " + project_name),
           _csv_line("General information", "General information"),
           _csv_line("Total capacity, kWp", net_custom(metrics["kwp_total"], 2)),
           _csv_line("Module power, Wp", net_custom(primary_power, 0)),
           _csv_line("Module quantity", str(total_modules)),
           _csv_line("Road length, ft.", "0"),
           _csv_line("Trench length, ft.", "0"),
           _csv_line("Fence length, ft.", "0"),
           _csv_line("Total grade CUT, yd3", "0"),
           _csv_line("Total grade FILL, yd3", "0"),
           _csv_line("Total grade NET, yd3", "0"),
           _csv_line(*(["Information by area"] * 10)),
           _csv_line("No.", "14 String  ", "18 String ", "Modules", "Max. pitch, ft", "Min. pitch, ft",
                     "Area coverage, %", "GCR", "Capacity, kWp", "Covered Area, ft2")]
    ratio = metrics["coverage_ratio"]
    covered_sqm = metrics["boundary_area_sqm"] * ratio if ratio is not None else 0.0
    count14 = sum(t["count"] for t in types if t["string_count"] == 14)           # :145-152
    count18 = sum(t["count"] for t in types if t["string_count"] == 18)
    covered_ft2 = covered_sqm / SQM_PER_SQFT if covered_sqm > 0.0 else 0.0
    out.append(_csv_line("Total", str(count14), str(count18), str(total_modules),  # :119-143
                         net_custom(0.0, 15), net_custom(0.0, 15),
                         net_custom(ratio * 100.0 if ratio is not None else 0.0, 3),
                         net_custom(0.0, 3), net_custom(metrics["kwp_total"], 2),
                         net_custom(covered_ft2, 3)))
    return "".join(out)


# ---------------------------------------------------------------------------
#  LEAFYIELDEXPORT: pile grouping (PileRevealBucketer.cs, PileDrawingReader.cs)
# ---------------------------------------------------------------------------

def default_reveal_boundaries_m():
    """PileTemplate.DefaultRevealBucketBoundariesM, :107-111."""
    return [3.0 * FT_TO_M, 4.0 * FT_TO_M, 5.0 * FT_TO_M, 6.0 * FT_TO_M, 7.0 * FT_TO_M]


def pile_entities(entities):
    """ReadPileEntities, :13-51: every entity on the piling layer as its extents' centre and
    top. A pile circle of radius r extruded by its thickness spans centre +- r in plan and
    its bottom to bottom + thickness in z."""
    piles = []
    for ent in _entities(entities):
        if not _layer_is(ent.get("layer", ""), LAYER_PILING):
            continue
        cx, cy = _xy(ent["center"], "pile centre")
        r = _finite(ent["radius"], "pile radius")
        top = _finite(ent["top_z"], "pile top")
        piles.append({"x": ((cx - r) + (cx + r)) * 0.5, "y": ((cy - r) + (cy + r)) * 0.5, "top_z": top})
    return piles


def _format_ft(value):
    """FormatFt, :82-87."""
    if abs(value - round(value)) < 1e-9:
        return net_integral_text(float(round(value)))
    return net_custom(value, 3)


def _format_limit(value):
    """FormatCsvLimit, :75-80."""
    if value == -math.inf:
        return "-Infinity"
    if value == math.inf:
        return "Infinity"
    return _format_ft(value)


def pile_grouping_csv(piles, terrain_z, boundaries_m=None):
    """TryBuildPileGroupingCsv, LeafYieldExportCommand.cs:1357-1379, through Bucket (:94-139)
    and ToPileGroupingCsv (:39-52). None when there is no terrain, no pile, or no pile over
    the terrain."""
    if terrain_z is None or not piles:
        return None
    raw = default_reveal_boundaries_m() if boundaries_m is None else list(boundaries_m)
    seen, bounds = set(), []
    for v in raw:                                                        # :102-107
        v = float(v)
        if math.isfinite(v) and v > 0.0 and v not in seen:
            seen.add(v)
            bounds.append(v)
    bounds.sort()
    buckets = []
    lo = -math.inf
    for hi in bounds:
        buckets.append([lo, hi, 0])
        lo = hi
    buckets.append([bounds[-1] if bounds else -math.inf, math.inf, 0])
    sampled = 0
    for p in piles:                                                      # :122-136
        ground = terrain_z(p["x"], p["y"])
        if ground is None:
            continue
        reveal = max(0.0, p["top_z"] - ground)
        index = next((i for i, b in enumerate(bounds) if reveal < b), len(bounds))
        buckets[index][2] += 1
        sampled += 1
    if sampled == 0:
        return None
    lines = ['"Pile reveal min, ft","Pile reveal max, ft",Pile count' + CRLF]
    for lo, hi, count in buckets:
        min_ft = lo if lo == -math.inf else lo / FT_TO_M
        max_ft = hi if hi == math.inf else hi / FT_TO_M
        lines.append(f"{_format_limit(min_ft)},{_format_limit(max_ft)},{count}{CRLF}")
    return "".join(lines)


# ---------------------------------------------------------------------------
#  LEAFYIELDEXPORT: the bundle, the zip entries and the manifest
# ---------------------------------------------------------------------------

def _centroid(vertices):
    """CentroidOfPolyline, :707-719: the vertex mean."""
    if not vertices:
        return (0.0, 0.0)
    sx = sy = 0.0
    for x, y in vertices:
        sx += x
        sy += y
    return (sx / len(vertices), sy / len(vertices))


def _max_distance(vertices, centre):
    """MaxDistanceToVertices, :721-735."""
    best = 0.0
    for x, y in vertices:
        d = math.sqrt((x - centre[0]) ** 2 + (y - centre[1]) ** 2)
        if d > best:
            best = d
    return best


def _extents(vertices):
    xs = [v[0] for v in vertices]
    ys = [v[1] for v in vertices]
    return (min(xs), min(ys), max(xs), max(ys))


def _overlaps(a, b):
    """OverlapsBounds, :756-760."""
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def _copper_mass_lb(gauge, conductor_length_ft):
    """CopperMassLb and CopperCircularMils, :1255-1301."""
    normalized = gauge.strip().upper().replace(" ", "").replace("-", "")
    cmil = CIRCULAR_MILS.get(normalized)
    if cmil is None:
        cmil = 0.0
        if normalized.endswith("KCMIL"):
            try:
                cmil = float(normalized[:-5]) * 1000.0
            except ValueError:
                cmil = 0.0
    if cmil <= 0.0 or conductor_length_ft <= 0.0:
        return 0.0
    return conductor_length_ft * cmil * COPPER_LB_PER_CMIL_FT


def _cable_rows(cables, bundle):
    """CollectDcHomerunBom, :764-878, for cable polylines carrying cable records. Each cable is
    {"circuit", "which", "wire_gauge", "length_ft", "handle", "layer", "points", "closed"}; a
    gauge the routing config would pick (empty or NA, NormalizeWireGauge :1210-1229) is
    refused, since Studio holds no routing config to pick it."""
    if len(cables) > MAX_CABLES:
        raise DStepsInputError(f"{len(cables)} cables exceed the bound of {MAX_CABLES}")
    for c in cables:
        which = c.get("which") or ""
        is_ac = any(which.upper() == w.upper() for w in AC_WHICH)
        is_dc = any(which.upper() == w.upper() for w in DC_WHICH)
        if not (is_dc or is_ac):
            continue
        points = _vertices(c.get("points"), "cable path")
        if len(points) < 2:
            continue
        closed = bool(c.get("closed", False))
        segment_count = len(points) if closed else len(points) - 1
        geometry_ft = 0.0
        for i in range(segment_count):
            a, b = points[i], points[(i + 1) % len(points)]
            geometry_ft += math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) / 12.0
        stored = _finite(c.get("length_ft", 0.0), "cable length")
        length_ft = stored if stored > 0.0 else geometry_ft
        gauge = c.get("wire_gauge") or ""
        if not gauge.strip() or gauge.upper() == "NA":
            raise DStepsInputError("a cable without a wire gauge needs the routing config's pick")
        gauge = gauge.strip()
        conductors = 3 if is_ac else 2
        copper_ft = length_ft * conductors
        mass = _copper_mass_lb(gauge, copper_ft)
        key = "ac" if is_ac else "dc"
        bundle[key + "_copper_length_ft"] += copper_ft
        bundle[key + "_copper_mass_lb"] += mass
        handle = str(c.get("handle", "")).upper()
        bundle[key + "_bom"].append(csv_row(c.get("circuit"), which, gauge, length_ft, conductors, copper_ft,
                                            mass, handle, c.get("layer")))
        for i in range(segment_count):
            a, b = points[i], points[(i + 1) % len(points)]
            bundle[key + "_segments"].append(csv_row(
                c.get("circuit"), which, gauge, handle, i, a[0], a[1], b[0], b[1],
                math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) / 12.0))


def _layout_rows(panel_groups):
    """CollectPanelGroupLayoutRows, :524-606, and the layout.csv rows (:353-365). A panel
    group is {"row_index", "x", "y", "module_slots", "module_power_w", "tracker_handle"} in
    drawing order; one without slots or power, or at the origin, is skipped."""
    rows = []
    for pg in panel_groups:
        slots, power = _int(pg["module_slots"], "slots"), _int(pg["module_power_w"], "power")
        if slots <= 0 or power <= 0:
            continue
        x, y = _finite(pg["x"], "x"), _finite(pg["y"], "y")
        if abs(x) <= 1e-9 and abs(y) <= 1e-9:
            continue
        rows.append({"ordinal": len(rows), "row_index": _int(pg.get("row_index", 0), "row index"),
                     "x": x, "y": y, "kwp": slots * power / 1000.0, "handle": pg.get("tracker_handle") or ""})
    use_stored = bool(rows) and (len(rows) == 1 or (
        len({r["row_index"] for r in rows}) == len(rows) and any(r["row_index"] != 0 for r in rows)))
    if use_stored:
        rows.sort(key=lambda r: (r["row_index"], r["ordinal"]))
    return [f"{r['row_index'] if use_stored else r['ordinal']},{net_custom(r['x'], 3)},{net_custom(r['y'], 3)},"
            f"{net_custom(r['kwp'], 3)},{r['handle']}" for r in rows], rows


def yield_bundle(tracker_entities, module, entities=(), closed_polylines=(), panel_groups=(), cables=(),
                 terrain_z=None, reveal_boundaries_m=None, drawing_file_name=None, meters_per_unit=1.0):
    """CollectBundle, :307-469. entities: model space in drawing order, the neutral shapes the
    shading pass reads (circles on the tree layer, polylines on the station, fence, vegetation
    and site-bounds layers, anything on the restriction layer with vertices) and the piles
    (circles on the piling layer with their top). Returns the bundle: counts, CSV rows and the
    overview text."""
    ents = _entities(entities)
    m = layout_metrics(tracker_entities, module, closed_polylines, meters_per_unit)
    b = {"frames": m["frames"], "kwp_total": m["kwp_total"], "piles": m["piles"], "trees": 0, "stations": 0,
         "fences": 0, "vegetation": 0, "clipped": 0, "warnings": [],
         "dc_bom": [DC_BOM_HEADER], "dc_segments": [DC_SEGMENT_HEADER],
         "ac_bom": [AC_BOM_HEADER], "ac_segments": [AC_SEGMENT_HEADER],
         "dc_copper_length_ft": 0.0, "dc_copper_mass_lb": 0.0,
         "ac_copper_length_ft": 0.0, "ac_copper_mass_lb": 0.0}
    b["overview"] = project_overview_csv(m, drawing_file_name)
    layout_lines, layout = _layout_rows(panel_groups)
    b["layout"] = [LAYOUT_CSV_HEADER] + layout_lines
    if layout:                                                           # :329-351
        layout_kwp = sum(r["kwp"] for r in layout)
        if b["frames"] <= 0:
            b["frames"] = len(layout)
        if b["kwp_total"] <= 0.0:
            b["kwp_total"] = layout_kwp
    b["pile_grouping"] = pile_grouping_csv(pile_entities(ents), terrain_z, reveal_boundaries_m)
    b["shading"] = [SHADING_CSV_HEADER]
    b["vegetation_rows"] = [VEGETATION_CSV_HEADER]
    site_bounds = None
    for ent in ents:                                                     # :380-389, the last one wins
        if ent.get("layer") == SITE_BOUNDS_LAYER and ent.get("type") == "polyline":
            verts = _vertices(ent.get("vertices"), "site polyline")
            if verts:
                site_bounds = _extents(verts)
    for ent in ents:                                                     # :391-461
        kind, layer = ent.get("type"), ent.get("layer", "")
        if kind == "circle" and layer == LAYER_TREES:
            cx, cy = _xy(ent["center"], "tree centre")
            b["trees"] += 1
            b["shading"].append(f"tree,{net_custom(cx, 3)},{net_custom(cy, 3)},"
                                f"{net_custom(_finite(ent['radius'], 'tree radius'), 3)}")
        elif kind == "polyline" and layer == LAYER_STATIONS:
            cen = _centroid(_vertices(ent.get("vertices"), "station"))
            b["stations"] += 1
            b["shading"].append(f"station,{net_custom(cen[0], 3)},{net_custom(cen[1], 3)},0")
        elif kind == "polyline" and _reports.is_fence_layer(layer):
            cen = _centroid(_vertices(ent.get("vertices"), "fence"))
            b["fences"] += 1
            b["shading"].append(f"fence,{net_custom(cen[0], 3)},{net_custom(cen[1], 3)},0")
        elif kind == "polyline" and _layer_is(layer, LAYER_VEGETATION):
            verts = _vertices(ent.get("vertices"), "vegetation")
            cen = _centroid(verts)
            radius = _max_distance(verts, cen)
            height = _finite(ent.get("height_du", 0.0), "vegetation height")
            b["vegetation"] += 1
            b["shading"].append(f"vegetation,{net_custom(cen[0], 3)},{net_custom(cen[1], 3)},"
                                f"{net_custom(radius, 3)}")
            b["vegetation_rows"].append(csv_row(
                f"vegetation-{b['vegetation']}", cen[0], cen[1], height, radius, len(verts),
                ";".join(f"{net_custom(x, 3)}:{net_custom(y, 3)}" for x, y in verts)))
        elif layer == LAYER_RESTRICT and site_bounds is not None:
            verts = ent.get("vertices")
            if verts:
                if _overlaps(_extents(_vertices(verts, "restriction")), site_bounds):
                    b["clipped"] += 1
    _cable_rows(list(cables), b)                                         # :465
    _readiness_warnings(b)                                               # :466
    b["overview"] = _with_cable_summary(b)                               # :467
    return b


def _readiness_warnings(b):
    """AddExportReadinessWarnings, :471-513 (the warnings the outcome shows; not in the zip)."""
    def once(w):
        if w not in b["warnings"]:
            b["warnings"].append(w)
    if len(b["layout"]) <= 1:
        once("layout.csv contains no frame rows: run Step 8 tracker-to-PanelGroup conversion before "
             "uploading to Yield.")
    if len(b["dc_segments"]) <= 1:
        once("dc_homerun_segments.csv contains no cable runs: run Step 9 devices/cabling before "
             "uploading to Yield.")


def _with_cable_summary(b):
    """AppendDcCableSummaryRows, :1144-1182: LF-ended rows after the overview."""
    text = b["overview"] or ""
    if text and not text.endswith("\n"):
        text += "\n"
    return (text + "DC cable information,DC cable information\n"
            + f"DC cable runs,{max(0, len(b['dc_bom']) - 1)}\n"
            + f"DC copper length, ft.,{net_custom(b['dc_copper_length_ft'], 3)}\n"
            + f"DC copper mass, lb,{net_custom(b['dc_copper_mass_lb'], 3)}\n"
            + "AC feeder information,AC feeder information\n"
            + f"AC feeder runs,{max(0, len(b['ac_bom']) - 1)}\n"
            + f"AC feeder copper length, ft.,{net_custom(b['ac_copper_length_ft'], 3)}\n"
            + f"AC feeder copper mass, lb,{net_custom(b['ac_copper_mass_lb'], 3)}\n")


def side_files(b):
    """WriteZip's side files, :1307-1337, as (entry name, text) in entry order."""
    files = [("layout.csv", "\n".join(b["layout"]) + "\n"),
             ("bom_project_overview.csv", b["overview"] or ""),
             ("dc_homerun_bom.csv", "\n".join(b["dc_bom"]) + "\n"),
             ("dc_homerun_segments.csv", "\n".join(b["dc_segments"]) + "\n"),
             ("ac_feeder_bom.csv", "\n".join(b["ac_bom"]) + "\n"),
             ("ac_feeder_segments.csv", "\n".join(b["ac_segments"]) + "\n"),
             ("shading.csv", "\n".join(b["shading"]) + "\n"),
             ("vegetation_masses.csv", "\n".join(b["vegetation_rows"]) + "\n"),
             ("README.txt", README_TEXT)]
    if b.get("pile_grouping") and b["pile_grouping"].strip():
        files.append(("Pile_grouping.csv", b["pile_grouping"]))
    return files


def json_string(value):
    """Newtonsoft's default string escaping: quote, backslash, control characters and
    U+0085, U+2028, U+2029."""
    if not isinstance(value, str):
        raise DStepsInputError("a manifest string must be text")
    short = {'"': '\\"', "\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b", "\f": "\\f"}
    out = []
    for ch in value:
        if ch in short:
            out.append(short[ch])
        elif ord(ch) < 0x20 or ch in "\u0085\u2028\u2029":
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def always_decimal(value):
    """AlwaysDecimal, :1487-1492: rounded half away from zero to 2 places, then "0.0##"."""
    return net_custom(net_round_away(value, 2), 3, 1)


def manifest_json(b, entries, exported_at_utc="", project_name="", drawing_path="", plugin_version="",
                  exported_units="Meters"):
    """BuildManifestJson, :1422-1485: Newtonsoft indented (two spaces, LF), then one LF.
    entries: [(name, bytes)] of the side files as written."""
    def s(v):
        return json_string(v if v is not None else "")
    files = []
    for name, data in entries:
        files.append("    {\n"
                     f"      \"name\": {s(name)},\n"
                     f"      \"bytes\": {len(data)},\n"
                     f"      \"sha256\": {s(hashlib.sha256(data).hexdigest())}\n"
                     "    }")
    files_text = "[]" if not files else "[\n" + ",\n".join(files) + "\n  ]"
    return ("{\n"
            f"  \"exporter\": {s(EXPORTER)},\n"
            f"  \"schema\": {s(SCHEMA)},\n"
            f"  \"exported_at_utc\": {s(exported_at_utc)},\n"
            f"  \"frames\": {int(b['frames'])},\n"
            f"  \"kwp_total\": {always_decimal(b['kwp_total'])},\n"
            f"  \"piles\": {int(b['piles'])},\n"
            "  \"shading_counts\": {\n"
            f"    \"trees\": {b['trees']},\n"
            f"    \"stations\": {b['stations']},\n"
            f"    \"fences\": {b['fences']},\n"
            f"    \"vegetation\": {b['vegetation']}\n"
            "  },\n"
            f"  \"clipped_objects\": {b['clipped']},\n"
            f"  \"project_name\": {s(project_name)},\n"
            f"  \"drawing_path\": {s(drawing_path)},\n"
            f"  \"plugin_version\": {s(plugin_version)},\n"
            f"  \"exported_units\": {s(exported_units if exported_units is not None else UNKNOWN_UNITS)},\n"
            f"  \"files\": {files_text}\n"
            "}\n")


def yield_zip_entries(b, exported_units="Meters", exported_at_utc="", project_name="", drawing_path="",
                      plugin_version=""):
    """WriteZip, :1303-1356: [(entry name, bytes)] in zip order, BOM-less UTF-8, the manifest
    last. The host fields (G28) default to the empty string."""
    entries = [(name, text.encode("utf-8")) for name, text in side_files(b)]
    manifest = manifest_json(b, entries, exported_at_utc, project_name, drawing_path, plugin_version,
                             exported_units)
    return entries + [("manifest.json", manifest.encode("utf-8"))]


def insunits_name(insunits):
    """InsUnitsToName, :281-293."""
    return INSUNITS_NAMES.get(_int(insunits, "INSUNITS"), f"Unit({insunits})")
