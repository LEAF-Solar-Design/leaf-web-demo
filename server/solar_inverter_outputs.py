"""Studio's inverter-family output engines: schedules, the string export workbook, LBDs and trenches (G35).

Each engine takes a G35 state (server/solar_inverter_state.py, the plugin adapter's projection) and the
step's answers, and returns (after state, printed lines); the step's evidence is then the adapter's delta
(st.step_rows). Literal ports of the plugin source read 2026-09-24 at C:/tmp/solar-parity/wt-b25-s69
(Branch2025 master 6b940d51, the captured build), each rule cited:

  InsertSchedules      InsertSchedulesCmd.cs:31-121 (the run), :400-507 (circuits and string records),
                       :253-318 (the sizer enrichment), ScheduleRegistry.cs:24-91 (equipment), :97-300
                       (inverter, L1/L2), :306-399 (string), :434-471 (feeder), ScheduleTableBuilder.cs:29-111
                       (the cell grid, row height, column widths), :149-201 (one point, tables stacked down)
  AddLBD               LeafLBDCommand.cs:51-131, LBDPlacement.cs:96-123 (the offset), :53, :65, :68
  LEAFPLACELBD         Commands.cs:5314-5383 (the closest point on the picked feeder)
  LEAFTRENCHAUTO       Commands.cs:6199-6528 (scan, hub, idempotency, star routing), :6793-6809 (the INSERT
                       outline), :6815-6852 (centroid); TrenchRouting.cs:153-196 (options), :310-529 (the
                       grid Dijkstra), :550-564 (inside), :601-662 (its binary heap); read 2026-09-25 at
                       C:/tmp/solar-parity/wt-b25-main (Branch2025 master bdfd7f19, test build 4)
  LEAFCABLETOTRAYAUTO  Commands.cs:6505-6718 (every cable, nearest trench to its middle vertex, snap)
  LEAFCABLETOTRAY      Commands.cs:5960-6182 (one picked cable, the nearest trench within 5 m)
  HomerunAdjust        OptiAdjustCmd.cs:150-155 (no trunk layer: nothing to adjust)
  LEAFDEVICESPATTERN   PatternDevicePlacementCmd.cs:94-99 (no tracker rows: nothing placed)

Declared divergences (G35, each emits exactly the diffs it explains):
  trench-routing-auto  Each panel-group INSERT is one group, as the fixed plugin reads it (F6): its one
                       Closed definition boundary, else the definition extents rectangle, taken from the
                       chain intake's outlines (panel_group_outline; the Closed flag is read from the
                       vertices, inferred). Routed in the intake's order with the plugin's own star
                       routing, heap and grid limits (200 cells per axis); the metric grid options are
                       converted to drawing units as the plugin fix F16 does (R27). On the rooftop fixture
                       every start and the hub cell match the plugin's. The alignment test uses the band
                       of Branch2025 PR #332 (within_alignment_band). Against the test build 10 capture
                       (master 2e8700d7, with #332) eight of the eleven routes match; A608, A631 and A612
                       part a few cells from their start at a choice between equal-length paths whose
                       edges lie inside the group's own obstacle, where the 1e6 penalty scales the last
                       bits of the lattice coordinates; declared in
                       docs/parity/divergences/trench-routing-auto/equal-cost-ties-on-lattice-bits.md.
  cable-export         Until Branch2025 #309 the plugin's EPPlus save threw and wrote nothing. Studio writes
                       the Export All workbook the export form writes (StringHomerunExportForm.cs:349-413,
                       read 2026-09-25 at C:/tmp/solar-parity/wt-b25-main, master a94db8d9: Homeruns,
                       Equipment, Inverter and String Schedule, a Feeder Schedule under L2 collectors; stdlib
                       zip and XML) and the evidence carries it as a G20/G21 `file` row. Against the plugin's
                       saved workbook it matches row for row since Branch2025 #327 reads the catalog's AC power
                       as kW (the retired docs/parity/divergences/cable-export/inverter-rating-units.md).
                       The Homeruns row
                       order (an unstable sort over SelectAll order) is reproduced, the feeders' place in it
                       from the order RouteL2Feeders drew them in.

Host inputs (per-user settings, catalog rows and AutoCAD text layout that no drawing state carries) come in
as `host`; the producer names each and its evidence. Trench polylines, the HOMERUN-TRUNK layer and tracker
rows are not G35 row kinds; engines keep them in the private state keys `_trenches`, `_homerun_trunk` and
`_tracker_rows` (absent on every captured state), which publish() never writes.

Pure functions over plain data: no CAD host, no network. Fails closed with InverterOutputError.
"""
from __future__ import annotations

import copy
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation
import importlib.util
import io
import math
from pathlib import Path
import re
import sys
from xml.sax.saxutils import escape
import zipfile


def _load_sibling(name):
    """A server module by path (any cwd), shared through sys.modules so its error classes are one."""
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).resolve().with_name(name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


st = _load_sibling("solar_inverter_state")
devices = _load_sibling("solar_inverter_devices")
_load_sibling("solar_design_graph")             # solar_interchange's import, resolved from sys.modules at any cwd
interchange = _load_sibling("solar_interchange")


class InverterOutputError(ValueError):
    """A malformed input or an unported path: nothing is computed from it."""


# Bounds (fail closed on a runaway input).
MAX_VERTICES = 100_000
MAX_TRENCHES = 10_000
MAX_SCHEDULE_ROWS = 50_000
MAX_CELL_CHARS = 16_000

# ---------------------------------------------------------------- constants --

# Commands.cs:6215-6217, :5992-5994: the trench layer when the setting is empty.
DEFAULT_TRENCH_LAYER = "LEAF-PVCASE-TRENCH"
# Commands.cs:6344: a group already served by a trench within this distance is skipped.
IDEMPOTENCY_THRESHOLD = 1.0
# Commands.cs:6368-6372 and TrenchRouting.cs:158-187 (the options the command sets and the defaults).
# grid_step and grid_padding are METRES (GridStepM, GridPaddingM); routing_options_for_units converts them
# to drawing units before route_path, whose arithmetic is all in drawing units (F16, R27).
ROUTING_OPTIONS = {"grid_step": 1.0, "grid_padding": 5.0, "obstacle_penalty": 1_000_000.0,
                   "alignment_bonus": 0.5, "min_edge_weight": 1e-3, "max_cells_per_axis": 200}
# TrenchXData.DefaultWidthM, the trench width the alignment bonus reads (Commands.cs:6292-6293).
TRENCH_WIDTH = 0.6
# Commands.cs:6523-6525 (the setting when positive, else 5.0) and :5997 (LEAFCABLETOTRAY: always 5.0).
DEFAULT_SNAP_DISTANCE = 5.0
TRAY_MAX_DISTANCE = 5.0
# LBDPlacement.cs:53 (the LBD device type), :65 (the offset), :68 (the marker radius).
LBD_DEVICE_TYPE = 2
LBD_OFFSET = 1.5
LBD_MARKER_RADIUS = 0.5
# OptiAdjustCmd.cs:28.
TRUNK_LAYER = "HOMERUN-TRUNK"
# InsertSchedulesCmd.cs:24.
CIRCUIT_REGEX = re.compile(r"\+(\d+)/\d+([a-zA-Z].*)")
# The homerun circuit the plugin writes (+<string>/<type><device><mppt>, BranchCmd.cs:14754-14761), read
# for the device number as CircuitTagParser.Parse does under no TaggingInformation (StringTag.cs:60-70);
# the adapter reads it the same way (inverter_evidence.py:116).
_CIRCUIT_DEVICE = re.compile(r"[+-]?(?P<string>[0-9]+)/(?P<type>[A-Za-z]?)(?P<device>[0-9]+)(?P<mppt>[A-Za-z]*)")
STRING_KIND = "String"
# ScheduleRegistry.cs:325-332.
STRING_HEADERS_L2 = ("CB", "Inv", "String", "Zone", "Modules", "Voc (V)", "Voc_cold (V)", "Vmax (V)",
                     "Margin (V)", "Vmp (V)", "Isc (A)", "Isc\u00d71.25", "Power (W)", "HR (ft)", "Gauge",
                     "Amp (A)", "Vd (%)", "Std")
STRING_HEADERS = ("Inv", "MPPT") + STRING_HEADERS_L2[2:]
INVERTER_HEADERS = ("Inverter", "MPPT", "Strings", "Mod/String", "Total Modules", "DC Power (kW)",
                    "AC Power (kW)", "DC/AC Ratio")
COMBINER_HEADERS = ("Inverter", "Combiner") + INVERTER_HEADERS[2:]
FEEDER_HEADERS = ("Combiner Box #", "Inverter #", "Feeder Length (ft)")
# The workbook's role in a G20 file row, and the G21 chunk bound.
WORKBOOK_ROLE = "string-export-xlsx"
MAX_CHUNK = 16_000


# ------------------------------------------------------------ number format --

def _decimal(value):
    try:
        return Decimal(repr(float(value)))
    except (InvalidOperation, TypeError, ValueError):
        raise InverterOutputError(f"{value!r} is not a number") from None


def cs_round(value, digits):
    """Math.Round(double, digits): midpoint to even on the value as written."""
    quantum = Decimal(1).scaleb(-digits)
    return float(_decimal(value).quantize(quantum, rounding=ROUND_HALF_EVEN))


def cs_text(value):
    """double.ToString(): the shortest text that reads back, integral values without a point."""
    value = float(value)
    if not math.isfinite(value):
        raise InverterOutputError("a schedule number must be finite")
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


def cs_format(value, pattern):
    """d.ToString("N0"/"N1"/"0"): half away from zero, group separators for N."""
    decimals = int(pattern[1:]) if pattern.startswith("N") else 0
    quantized = _decimal(value).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    return f"{quantized:,.{decimals}f}" if pattern.startswith("N") else f"{quantized:.{decimals}f}"


def cs_fixed(value, digits):
    """d.ToString("F<digits>") on .NET 8 (the AutoCAD 2025 host that saved the workbook): the exact binary
    value rounded half away from zero, no group separators. Fails closed on a non-finite or huge value."""
    value = float(value)
    if not math.isfinite(value):
        raise InverterOutputError("an export number must be finite")
    try:
        quantized = Decimal(value).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        raise InverterOutputError(f"{value!r} is out of the export's number range") from None
    return f"{quantized:.{digits}f}"


def safe_format(value, pattern):
    """ScheduleRegistry.SafeFormat (:558-564): a parseable number formatted, else the text itself."""
    if value is None or value == "":
        return "" if value is None else value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        return str(value)
    return cs_format(number, pattern)


def safe_format_int(value):
    """ScheduleRegistry.SafeFormatInt (:566-572)."""
    if value is None or value == "":
        return "" if value is None else value
    text = str(value).strip()
    return str(int(text)) if re.fullmatch(r"[+-]?[0-9]+", text) else str(value)


def safe_parse_double(value, default=0.0):
    """ScheduleRegistry.SafeParseDouble (:551-556)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


# --------------------------------------------------------------- geometry --

def _xy(point, what="point"):
    if isinstance(point, dict):
        return tuple(st.point_of(point, what))
    if not isinstance(point, (list, tuple)) or len(point) < 2:
        raise InverterOutputError(f"{what} must be [x, y]")
    x, y = point[0], point[1]
    for value in (x, y):
        if type(value) not in (int, float) or not math.isfinite(value):
            raise InverterOutputError(f"{what} must be finite")
    return float(x), float(y)


def _points(vertices, what="vertices"):
    if not isinstance(vertices, list) or len(vertices) > MAX_VERTICES:
        raise InverterOutputError(f"{what} must be a list of at most {MAX_VERTICES} points")
    return [_xy(p, what) for p in vertices]


def project_to_polyline(px, py, pts):
    """The closest point of an open polyline to (px, py): each segment clamped, the first minimum wins
    (Commands.cs ProjectToPolyline, the snap both tray commands use)."""
    if not pts:
        raise InverterOutputError("a polyline has no vertices")
    if len(pts) == 1:
        return pts[0]
    best, best_d = pts[0], math.inf
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        t = 0.0 if length2 < 1e-24 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
        cx, cy = ax + t * dx, ay + t * dy
        d = (cx - px) ** 2 + (cy - py) ** 2
        if d < best_d:
            best, best_d = (cx, cy), d
    return best


def _distance_to_polyline(px, py, pts):
    x, y = project_to_polyline(px, py, pts)
    return math.hypot(x - px, y - py)


def polyline_length(pts, bulges=None, closed=False):
    """Polyline.Length: straight segments, arcs by their bulge, the closing segment when closed."""
    pairs = list(zip(pts, pts[1:])) + ([(pts[-1], pts[0])] if closed and len(pts) > 1 else [])
    bulges = list(bulges or [])
    total = 0.0
    for index, ((ax, ay), (bx, by)) in enumerate(pairs):
        chord = math.hypot(bx - ax, by - ay)
        bulge = bulges[index] if index < len(bulges) and type(bulges[index]) in (int, float) else 0.0
        if abs(bulge) > 1e-12 and chord > 0.0:
            theta = 4.0 * math.atan(abs(bulge))
            total += chord * theta / (2.0 * math.sin(theta / 2.0))
        else:
            total += chord
    return total


def centroid(verts):
    """Commands.cs ComputeCentroid (:6815-6852) to the bit: signed-area centroid, the sums times
    1 / (3 * area2), the bounding-box centre when |area2| < 1e-9 (degenerate)."""
    if not verts:
        return 0.0, 0.0
    if len(verts) == 1:
        return verts[0]
    area2 = cx = cy = 0.0
    n = len(verts)
    j = n - 1
    for i in range(n):
        xi, yi = verts[i]
        xj, yj = verts[j]
        cross = xj * yi - xi * yj
        area2 += cross
        cx += (xi + xj) * cross
        cy += (yi + yj) * cross
        j = i
    if abs(area2) < 1e-9:
        x0, y0, x1, y1 = _bbox(verts)
        return 0.5 * (x0 + x1), 0.5 * (y0 + y1)
    inv = 1.0 / (3.0 * area2)
    return cx * inv, cy * inv


# An outline whose last vertex lies within this of its first repeats it: the plugin's open joined loop.
OPEN_LOOP_TOLERANCE = 1e-6


def _is_closed_boundary(outline):
    """Whether an intake outline stands for a Closed definition polyline (Commands.cs:6313 counts only
    `Closed && NumberOfVertices >= 3`). The intake carries no Closed flag; Studio reads it from the
    vertices (inferred, measured on the committed rooftop intake): a loop drawn open ends on a copy of
    its first vertex (gap < 1e-8 on every such outline there), a Closed one does not (gap > 28)."""
    if len(outline) < 3:
        return False
    (x0, y0), (x1, y1) = outline[0], outline[-1]
    return math.hypot(x1 - x0, y1 - y0) > OPEN_LOOP_TOLERANCE


def panel_group_outline(outlines):
    """The one routing outline LEAFTRENCHAUTO takes per panel-group INSERT (Commands.cs:6309-6350,
    ExtractTrenchBlockOutline :6793-6809): the definition's Closed boundary when it holds exactly one
    (_is_closed_boundary), else the definition extents as a rectangle (min, (max.x, min.y), max,
    (min.x, max.y)). Studio reads the extents over every outline the intake carries (inferred: the
    definition's other children lie inside them); the outlines are world coordinates, so the corners
    are too. On the rooftop intake this puts every group's start on the plugin's start cell."""
    rings = [o for o in outlines if len(o) >= 3]
    closed = [o for o in rings if _is_closed_boundary(o)]
    if len(closed) == 1:
        return [tuple(p) for p in closed[0]]
    if not rings:
        return []
    x0, y0, x1, y1 = _bbox([p for ring in rings for p in ring])
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def point_in_polygon(px, py, outline):
    """TrenchRouting.PointInPolygon (:480-494): ray casting, the ring closed implicitly."""
    if outline is None or len(outline) < 3:
        return False
    inside = False
    n = len(outline)
    j = n - 1
    for i in range(n):
        xi, yi = outline[i]
        xj, yj = outline[j]
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / ((yj - yi) + 1e-30) + xi):
            inside = not inside
        j = i
    return inside


def distance_point_to_segment(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 < 1e-24:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def routing_options_for_units(units, options=None):
    """The routing options with the metric grid step and padding in drawing units (F16, R27):
    grid_step / metres_per_unit and grid_padding / metres_per_unit, metres_per_unit read from
    solar_interchange.UNIT_SCALES (drawing_units -> meters_per_unit). Unitless or unknown units keep the
    metric values (1 unit = 1 m), so metric drawings are unchanged. Every other option is untouched."""
    o = dict(ROUTING_OPTIONS, **(options or {}))
    scale = interchange.UNIT_SCALES.get(units) if isinstance(units, str) else None
    if scale:
        o["grid_step"] = o["grid_step"] / scale
        o["grid_padding"] = o["grid_padding"] / scale
    return o


class _CsBinaryHeap:
    """TrenchRouting.BinaryHeap (:601-662), entry for entry: push appends and sifts up while strictly
    smaller than the parent; pop moves the last entry to the root and sifts down to the strictly smaller
    child, left first. Equal keys therefore pop in the plugin's heap-position order, not by node id, which
    decides every tie between equal-cost lattice paths."""
    __slots__ = ("keys", "nodes")

    def __init__(self):
        self.keys, self.nodes = [], []

    def push(self, node, key):
        keys, nodes = self.keys, self.nodes
        i = len(keys)
        keys.append(key)
        nodes.append(node)
        while i > 0:
            parent = (i - 1) // 2
            if key < keys[parent]:
                keys[i], nodes[i] = keys[parent], nodes[parent]
                i = parent
            else:
                break
        keys[i], nodes[i] = key, node

    def pop(self):
        keys, nodes = self.keys, self.nodes
        node, key = nodes[0], keys[0]
        last_key, last_node = keys.pop(), nodes.pop()
        count = len(keys)
        if count:
            i = 0
            while True:
                left = 2 * i + 1
                right = left + 1
                smallest, small_key = i, last_key
                if left < count and keys[left] < small_key:
                    smallest, small_key = left, keys[left]
                if right < count and keys[right] < small_key:
                    smallest = right
                if smallest == i:
                    break
                keys[i], nodes[i] = keys[smallest], nodes[smallest]
                i = smallest
            keys[i], nodes[i] = last_key, last_node
        return node, key


def _bbox(points):
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def within_alignment_band(d, half):
    """TrenchRouting.WithinAlignmentBand (Branch2025 PR #332, replacing the bare `d <= half` at
    TrenchRouting.cs:469): a midpoint exactly half a cell from a trench is inside the band whatever the
    last bits of the lattice origin, so the alignment bonus no longer turns on float noise."""
    return d <= half * (1 + 1e-9)


def route_path(start, end, obstacles, trenches, options=None):
    """TrenchRouting.RoutePath (:310-529): (ok, segments [(a, b)], message). An 8-connected lattice over
    the padded bounding box of the ends, obstacles and trenches, refused past the per-axis cap; edge
    weight is its length, times the obstacle penalty when its midpoint is inside an obstacle, less the
    alignment bonus within the band of a trench (within_alignment_band), floored; Dijkstra on the plugin's own binary heap (_CsBinaryHeap), so
    equal-cost ties resolve as the plugin's do. The obstacle and trench tests are the plugin's exact
    float tests behind bounding-box prefilters that only skip what the exact test would reject, so the
    lattice work is bounded by the settled cells, not cells x trench segments."""
    o = dict(ROUTING_OPTIONS, **(options or {}))
    (sx, sy), (ex, ey) = start, end
    if math.hypot(ex - sx, ey - sy) < 1e-9:
        return True, [(start, end)], ""
    step = o["grid_step"]
    if step <= 0:
        return False, [], "GridStepM must be > 0"
    min_x, min_y, max_x, max_y = min(sx, ex), min(sy, ey), max(sx, ex), max(sy, ey)
    for x, y in [p for outline in obstacles for p in outline] + [p for seg in trenches for p in seg[:2]]:
        min_x, min_y, max_x, max_y = min(min_x, x), min(min_y, y), max(max_x, x), max(max_y, y)
    min_x -= o["grid_padding"]
    min_y -= o["grid_padding"]
    max_x += o["grid_padding"]
    max_y += o["grid_padding"]
    cols = int(math.ceil((max_x - min_x) / step)) + 1
    rows = int(math.ceil((max_y - min_y) / step)) + 1
    if cols <= 0 or rows <= 0:
        return False, [], "degenerate grid extent"
    cap = o["max_cells_per_axis"]
    if cols > cap or rows > cap:
        return False, [], (f"grid {cols}x{rows} exceeds MaxGridCellsPerAxis ({cap}); increase GridStepM "
                           "or shrink the extent")

    def cell(v, low, count):
        return max(0, min(count - 1, int(_round_half_even((v - low) / step))))

    start_id = cell(sy, min_y, rows) * cols + cell(sx, min_x, cols)
    end_id = cell(ey, min_y, rows) * cols + cell(ex, min_x, cols)
    # Outside an outline's bounding box (with a margin for the crossing arithmetic) the ray cast finds an
    # even crossing count, so the prefilter never changes an answer.
    outlines = []
    for outline in obstacles:
        if len(outline) >= 3:
            x0, y0, x1, y1 = _bbox(outline)
            margin = 1e-9 * (1.0 + abs(x0) + abs(x1) + abs(y0) + abs(y1))
            outlines.append((x0 - margin, y0 - margin, x1 + margin, y1 + margin, outline))
    # Trench segments bucketed on the lattice by their bounding box grown by their bonus half-width (and
    # a margin), so a midpoint is tested exactly against every segment that could be within reach.
    buckets = {}
    for seg in trenches:
        (ax, ay), (bx, by) = seg[0], seg[1]
        dx, dy = bx - ax, by - ay
        if not math.sqrt(dx * dx + dy * dy) > 1e-9:
            continue
        half = 0.5 * max(seg[2], step)
        reach = half + 1e-9 * (1.0 + abs(ax) + abs(ay) + abs(bx) + abs(by) + half)
        c0 = int(math.floor((min(ax, bx) - reach - min_x) / step))
        c1 = int(math.floor((max(ax, bx) + reach - min_x) / step))
        r0 = int(math.floor((min(ay, by) - reach - min_y) / step))
        r1 = int(math.floor((max(ay, by) + reach - min_y) / step))
        if (c1 - c0 + 1) * (r1 - r0 + 1) > 4 * cols * rows:
            raise InverterOutputError("a trench segment spans more of the lattice than it holds")
        entry = (ax, ay, bx, by, half)
        for r in range(r0, r1 + 1):
            for c in range(c0, c1 + 1):
                buckets.setdefault((c, r), []).append(entry)
    penalty, bonus_factor, floor = o["obstacle_penalty"], o["alignment_bonus"], o["min_edge_weight"]

    def blocked(px, py):
        for x0, y0, x1, y1, outline in outlines:
            if x0 <= px <= x1 and y0 <= py <= y1 and point_in_polygon(px, py, outline):
                return True
        return False

    def aligned(px, py):
        for ax, ay, bx, by, half in buckets.get((int(math.floor((px - min_x) / step)),
                                                 int(math.floor((py - min_y) / step))), ()):
            if within_alignment_band(distance_point_to_segment(px, py, ax, ay, bx, by), half):
                return True
        return False

    d_col = (0, 1, 1, 1, 0, -1, -1, -1)
    d_row = (1, 1, 0, -1, -1, -1, 0, 1)
    n = cols * rows
    dist = [math.inf] * n
    prev = [-1] * n
    dist[start_id] = 0.0
    heap = _CsBinaryHeap()
    heap.push(start_id, 0.0)
    reached = False
    while heap.keys:
        u, du = heap.pop()
        if du > dist[u]:
            continue
        if u == end_id:
            reached = True
            break
        ur, uc = divmod(u, cols)
        ux, uy = min_x + uc * step, min_y + ur * step
        for k in range(8):
            vc, vr = uc + d_col[k], ur + d_row[k]
            if vc < 0 or vc >= cols or vr < 0 or vr >= rows:
                continue
            v = vr * cols + vc
            vx, vy = min_x + vc * step, min_y + vr * step
            mid_x, mid_y = 0.5 * (ux + vx), 0.5 * (uy + vy)
            dx, dy = vx - ux, vy - uy
            base = math.sqrt(dx * dx + dy * dy)
            weight = base
            if blocked(mid_x, mid_y):
                weight += base * penalty
            if aligned(mid_x, mid_y):
                bonus = base * bonus_factor
                if bonus > 0.0:
                    weight -= bonus
            if weight < floor:
                weight = floor
            alt = du + weight
            if alt < dist[v]:
                dist[v] = alt
                prev[v] = u
                heap.push(v, alt)
    if not reached or math.isinf(dist[end_id]):
        return False, [], "destination unreachable - obstacles fully enclose start or end"
    path = [end_id]
    cursor = end_id
    while cursor != start_id and len(path) <= n:
        cursor = prev[cursor]
        if cursor < 0:
            break
        path.append(cursor)
    if cursor != start_id:
        return False, [], "path reconstruction failed"
    path.reverse()
    segments = []
    for a, b in zip(path, path[1:]):
        (ar, ac), (br, bc) = divmod(a, cols), divmod(b, cols)
        segments.append(((min_x + ac * step, min_y + ar * step), (min_x + bc * step, min_y + br * step)))
    return True, segments, ""


def _round_half_even(value):
    """(int)Math.Round(double): midpoint to even."""
    return float(Decimal(repr(value)).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


# ------------------------------------------------------------------ helpers --

def _setting(state, name):
    value = state["setting"].get(name, st.DECLARED_DEFAULTS.get(name))
    return value


def _trench_layer(state):
    value = _setting(state, "TrenchLayer")
    return value if isinstance(value, str) and value else DEFAULT_TRENCH_LAYER


def _trenches(state):
    items = state.get("_trenches") or []
    if not isinstance(items, list) or len(items) > MAX_TRENCHES:
        raise InverterOutputError(f"trenches must be a list of at most {MAX_TRENCHES}")
    return [_points(t.get("vertices") if isinstance(t, dict) else None, "trench vertices") for t in items]


def _host(host, name):
    if not isinstance(host, dict) or name not in host:
        raise InverterOutputError(f"the host input {name!r} is missing")
    return host[name]


def _cable_candidates(state):
    """Every polyline carrying the plugin's cable record, as (holder, vertices): the String-layer strings
    (geometry) and the homerun and feeder cables (rows), in that order."""
    out = []
    for item in state["geometry"]["strings"]:
        if isinstance(item, dict) and isinstance(item.get("vertices"), list):
            out.append((item, _points(item["vertices"], "string vertices")))
    for item in state["rows"]["cable"]:
        out.append((item, _points(item["vertices"], "cable vertices")))
    return out


def _set_vertices(holder, pts):
    if "cable_kind" in holder:
        holder["vertices"] = [st.coordinate(x, y) for x, y in pts]
    else:
        holder["vertices"] = [[x, y] for x, y in pts]


def _worst_joint(pts):
    """The largest joint angle in degrees (Commands.cs:6653-6670, :6105-6121)."""
    worst = 0.0
    for (ax, ay), (bx, by), (cx, cy) in zip(pts, pts[1:], pts[2:]):
        v1x, v1y, v2x, v2y = bx - ax, by - ay, cx - bx, cy - by
        n1, n2 = math.hypot(v1x, v1y), math.hypot(v2x, v2y)
        if n1 < 1e-9 or n2 < 1e-9:
            continue
        cos_a = max(-1.0, min(1.0, (v1x * v2x + v1y * v2y) / (n1 * n2)))
        worst = max(worst, math.degrees(math.acos(cos_a)))
    return worst


# BendRadiusValidator (NEC 300.34, 12 mm conductor) is not ported: it only sets the printed warning count,
# which no G35 row carries. Studio counts a joint sharper than a right angle (inferred).
BEND_WARNING_DEG = 90.0


def _snap(pts, trench):
    """Interior vertices projected onto the trench, the ends kept (Commands.cs:6644-6651)."""
    return [pts[0]] + [project_to_polyline(x, y, trench) for x, y in pts[1:-1]] + [pts[-1]]


# ------------------------------------------------------------------ trenches --

def trench_routing_auto(state, panel_groups, host=None, units=st.UNITS):
    """LEAFTRENCHAUTO (Commands.cs:6199-6486) with panel-group INSERTs taken too (declared, see the
    module docstring). `panel_groups` is [{handle, outlines}] for the state's panel groups; `units` is the
    drawing's units (the chain intake's `units` field, "in" on the rooftop fixture, which is the G35
    state's unit), by which the metric grid options become drawing units."""
    options = routing_options_for_units(units)
    after = copy.deepcopy(state)
    layer = _setting(after, "PanelGroupLayer")
    if not isinstance(layer, str) or not layer:
        return after, ["LEAFTRENCHAUTO: PanelGroupLayer is not configured. Run panel-group placement first."]
    groups = devices.validate_panel_groups(panel_groups, after) if after["geometry"]["panel_groups"] else []
    centroids, obstacles = [], []
    for group in groups:
        outline = panel_group_outline(group["outlines"])
        if not outline:
            continue
        obstacles.append(outline)
        centroids.append(centroid(outline))
    # Inverter blocks are obstacles by their extents (Commands.cs:6298-6317). The state carries no block
    # extents; Studio uses the declared InverterBlockSize times the device scale, centred (inferred).
    size = _setting(after, "InverterBlockSize")
    size = float(size) if type(size) in (int, float) and math.isfinite(size) else 24.0
    for item in after["rows"]["device"]:
        if item["role"] in ("inverter", "l2-inverter"):
            x, y = st.point_of(item["position"])
            half = 0.5 * size * float(item["scale"] or 1.0)
            obstacles.append([(x - half, y - half), (x + half, y - half), (x + half, y + half), (x - half, y + half)])
    if not centroids:
        return after, [f"LEAFTRENCHAUTO: no panel groups on layer '{layer}'. Nothing to route."]
    # Commands.cs:6386-6393: a plain running sum, then one divide (builtin sum() compensates since 3.12).
    hub_x = hub_y = 0.0
    for cx, cy in centroids:
        hub_x += cx
        hub_y += cy
    hub = (hub_x / len(centroids), hub_y / len(centroids))
    existing = _trenches(after)
    segments = [(a, b, TRENCH_WIDTH) for poly in existing for a, b in zip(poly, poly[1:])]
    routed = skipped = failed = 0
    trenches = after.setdefault("_trenches", [])
    for cx, cy in centroids:
        if any(_distance_to_polyline(cx, cy, poly) <= IDEMPOTENCY_THRESHOLD for poly in existing if poly):
            skipped += 1
            continue
        if math.hypot(cx - hub[0], cy - hub[1]) < 1e-6:
            skipped += 1
            continue
        ok, path, _ = route_path((cx, cy), hub, obstacles, segments, options)
        if not ok or not path:
            failed += 1
            continue
        poly = [path[0][0]] + [b for _, b in path]
        trenches.append({"vertices": [[x, y] for x, y in poly]})
        existing.append(poly)
        segments.extend((a, b, TRENCH_WIDTH) for a, b in path)
        routed += 1
    if not trenches:
        del after["_trenches"]
    return after, [f"LEAFTRENCHAUTO: routed {routed} panel groups; {skipped} skipped (existing); {failed} failed."]


def trench_rows(before, after):
    """The trenches a step drew, as `trench` evidence rows (Studio only: G35 has no trench row kind),
    ordered by their first vertex (G12), ids trench-<n>."""
    old = {st.canonical(t) for t in (before.get("_trenches") or [])}
    new = [t for t in (after.get("_trenches") or []) if st.canonical(t) not in old]
    new.sort(key=lambda t: st.order_key(*t["vertices"][0]))
    return [{"id": {"entity_id": f"trench-{n}"}, "type": "trench", "quantity": 1, "unit": "each",
             "vertices": [st.coordinate(x, y) for x, y in t["vertices"]]} for n, t in enumerate(new, 1)]


def cable_to_tray_snap_auto(state):
    """LEAFCABLETOTRAYAUTO (Commands.cs:6505-6718)."""
    after = copy.deepcopy(state)
    configured = _setting(after, "TrenchSnapDistanceM")
    max_distance = float(configured) if type(configured) in (int, float) and configured > 0 else DEFAULT_SNAP_DISTANCE
    trenches = [t for t in _trenches(after) if len(t) >= 2]
    snapped = skipped = warnings = 0
    for holder, pts in _cable_candidates(after):
        if len(pts) < 2:
            continue                                 # Commands.cs:6543: never collected
        if holder.get("_trench") is not None or len(pts) < 2:
            skipped += 1
            continue
        mx, my = pts[len(pts) // 2]
        best, best_d = None, math.inf
        for index, trench in enumerate(trenches):
            px, py = project_to_polyline(mx, my, trench)
            d = math.hypot(px - mx, py - my)
            if d < best_d:
                best, best_d = index, d
        if best is None or best_d > max_distance:
            skipped += 1
            continue
        result = _snap(pts, trenches[best])
        if _worst_joint(result) > BEND_WARNING_DEG:
            warnings += 1
        _set_vertices(holder, result)
        holder["_trench"] = best
        snapped += 1
    return after, [f"LEAFCABLETOTRAYAUTO: snapped {snapped} cables; {skipped} skipped; "
                   f"{warnings} bend-radius warnings."]


def _picked_cable(state, pick, kinds=None):
    """The cable an entity pick lands on: the cable row nearest the pick point (the capture's pick names
    a handle the chain created, which never leaves the adapter)."""
    px, py = _xy(pick, "pick point")
    best, best_d = None, math.inf
    for item in state["rows"]["cable"]:
        if kinds is not None and item["cable_kind"] not in kinds:
            continue
        pts = _points(item["vertices"], "cable vertices")
        if not pts:
            continue
        x, y = project_to_polyline(px, py, pts)
        d = math.hypot(x - px, y - py)
        if d < best_d:
            best, best_d = item, d
    return best


def _answer_pick(host, answers, what):
    if not isinstance(answers, (list, tuple)) or len(answers) != 1 or not isinstance(answers[0], str):
        raise InverterOutputError(f"{what} takes one answer: the picked cable")
    picks = _host(host, "CablePicks")
    if not isinstance(picks, dict) or answers[0] not in picks:
        raise InverterOutputError(f"the host records no pick point for {answers[0]!r}")
    return picks[answers[0]]


def cable_to_tray_snap(state, host, answers):
    """LEAFCABLETOTRAY (Commands.cs:5960-6182) on the picked cable. The drawing settings are loaded and
    saved on this step: the reopened capture carries one more CreateDefault() catalog pair after it
    than before it (the G27 per-save duplication, st.save_drawing_properties)."""
    after = copy.deepcopy(state)
    cable = _picked_cable(after, _answer_pick(host, answers, "LEAFCABLETOTRAY"))
    if cable is None:
        return after, ["LEAFCABLETOTRAY: cancelled."]
    st.save_drawing_properties(after)
    pts = _points(cable["vertices"], "cable vertices")
    if len(pts) < 2:
        return after, ["LEAFCABLETOTRAY: cable polyline has fewer than 2 vertices."]
    mx, my = pts[len(pts) // 2]
    best, best_d = None, math.inf
    for index, trench in enumerate(t for t in _trenches(after) if len(t) >= 2):
        px, py = project_to_polyline(mx, my, trench)
        d = math.hypot(px - mx, py - my)
        if d < best_d:
            best, best_d = trench, d
    if best is None or best_d > TRAY_MAX_DISTANCE:
        return after, [f"LEAFCABLETOTRAY: No trench within {TRAY_MAX_DISTANCE:.1f}m of selected cable. "
                       "Run LEAFTRENCH first."]
    result = _snap(pts, best)
    _set_vertices(cable, result)
    cable["_trench"] = True
    return after, [f"LEAFCABLETOTRAY: cable snapped to trench; vertices={len(result)}, "
                   f"worst_joint={_worst_joint(result):.1f}deg."]


def homerun_adjust(state):
    """HomerunAdjust (OptiAdjustCmd.cs:150-155): without the trunk layer there is nothing to adjust. The
    interactive adjust itself (move, angle, insert, delete vertex) has no G35 answers and is refused."""
    after = copy.deepcopy(state)
    if not after.get("_homerun_trunk"):
        return after, ["HOMERUNADJUST - Adjust HOMERUN-TRUNK polylines.", f"No {TRUNK_LAYER} layer found."]
    raise InverterOutputError("the interactive trunk adjust is not ported: G35 records no adjust answers")


def devices_pattern_place(state):
    """LEAFDEVICESPATTERN (PatternDevicePlacementCmd.cs:94-99): no tracker rows, nothing placed. Pattern
    placement over tracker rows (G35a: none on this fixture) is refused, never approximated."""
    after = copy.deepcopy(state)
    if not after.get("_tracker_rows"):
        return after, ["LEAFDEVICESPATTERN: no tracker rows found. Run LEAFTRACKERSTOPANELGROUPS or load a "
                       "PVCase tracker drawing first."]
    raise InverterOutputError("tracker-row pattern placement is not ported: this fixture has no tracker rows")


# ---------------------------------------------------------------------- LBD --

def lbd_add(state, host):
    """AddLBD (LeafLBDCommand.cs:51-131): the picked upstream device (the device nearest the capture's
    pick point: an interactive entity pick, which G35 records no answer for) gets a marker circle the
    default offset away toward the pick (LBDPlacement.OffsetFromUpstream, :96-123). The marker's stored
    reference is the device's handle, which is null in evidence: no device pre-exists the chain."""
    after = copy.deepcopy(state)
    px, py = _xy(_host(host, "AddLbdPick"), "AddLBD pick point")
    best, best_d = None, math.inf
    for item in after["rows"]["device"]:
        x, y = st.point_of(item["position"])
        d = math.hypot(x - px, y - py)
        if d < best_d:
            best, best_d = (x, y), d
    if best is None:
        return after, ["AddLBD: cancelled."]
    ux, uy = best
    dx, dy = px - ux, py - uy
    mag = math.sqrt(dx * dx + dy * dy)
    nx, ny = (1.0, 0.0) if mag < 1e-12 else (dx / mag, dy / mag)
    x, y = ux + nx * LBD_OFFSET, uy + ny * LBD_OFFSET
    after["rows"]["lbd"].append({"lbd_kind": "marker", "position": st.coordinate(x, y),
                                 "feeder": {"ref": "device", "id": None},
                                 "_pair": st.new_pair(after, "lbd"),
                                 "_detail": {"device_type": LBD_DEVICE_TYPE, "radius": LBD_MARKER_RADIUS}})
    st.sort_rows(after)
    return after, [f"AddLBD: placed LBD at ({x:.2f}, {y:.2f}) on layer LEAF-LBDS."]


def lbd_place(state, host, answers):
    """LEAFPLACELBD (Commands.cs:5314-5383): an LBD block at the closest point of the picked feeder to the
    pick (Polyline.GetClosestPointTo, no extension), its stored reference the feeder (null in evidence:
    the feeder was created by the chain). Cable-split stays deferred, as in the plugin (:5306-5311)."""
    after = copy.deepcopy(state)
    pick = _answer_pick(host, answers, "LEAFPLACELBD")
    cable = _picked_cable(after, pick)
    if cable is None:
        return after, ["LEAFPLACELBD: cancelled."]
    px, py = _xy(pick, "pick point")
    x, y = project_to_polyline(px, py, _points(cable["vertices"], "cable vertices"))
    after["rows"]["lbd"].append({"lbd_kind": "block", "position": st.coordinate(x, y),
                                 "feeder": {"ref": "cable", "id": None},
                                 "_pair": st.new_pair(after, "lbd"),
                                 "_detail": {"device_type": LBD_DEVICE_TYPE}})
    st.sort_rows(after)
    layer = _setting(after, "LbdLayer") or "LBD"
    return after, [f"LEAFPLACELBD: placed LBD at ({x:.2f}, {y:.2f}) on layer {layer}.",
                   "  Note: cable-split deferred - parent feeder polyline left intact (G55 follow-up)."]


# ---------------------------------------------------------------- schedules --

def device_number_of(circuit):
    """Cable.mInverterNumber (Cable.cs:36): the circuit's device number, -1 when it has none."""
    match = _CIRCUIT_DEVICE.fullmatch(circuit.strip()) if isinstance(circuit, str) else None
    return int(match.group("device")) if match else -1


def _cables(state):
    """FindHomeruns (InsertSchedulesCmd.cs:123-148): every polyline carrying a cable record, with its
    record kind, circuit, panel count and Polyline.Length (drawing units); `source` is the string's
    handle (a string) or the row's from end (a cable)."""
    by_handle = {g.get("string"): g for g in state["geometry"]["strings"] if isinstance(g, dict)}
    out = []
    for item in state["rows"]["string-assignment"]:
        detail = item.get("_detail") or {}
        geometry = by_handle.get(item["string"]) if item["string"] is not None else None
        pts = _points(geometry["vertices"], "string vertices") if geometry else []
        out.append({"kind": STRING_KIND, "circuit": detail.get("circuit"), "panels": detail.get("panel_count"),
                    "segment": None, "source": item["string"],
                    "length": polyline_length(pts) if len(pts) > 1 else 0.0})
    for item in state["rows"]["cable"]:
        detail = item.get("_detail") or {}
        pts = _points(item["vertices"], "cable vertices")
        out.append({"kind": item["cable_kind"], "circuit": detail.get("circuit"), "panels": None,
                    "segment": item.get("segment"), "source": item.get("from"),
                    "length": polyline_length(pts, item.get("bulges"), bool(detail.get("closed")))})
    return out


def _int_or_zero(value):
    text = str(value).strip() if value is not None else ""
    return int(text) if re.fullmatch(r"[+-]?[0-9]+", text) else 0


def string_records(state, host):
    """GroupCablesByCircuit and BuildStringRecords (InsertSchedulesCmd.cs:400-507) plus the sizer
    enrichment (:253-318, the global response: this drawing has no electrical zones)."""
    circuits = {}
    for cable in _cables(state):
        if cable["circuit"] is None:
            continue                                  # no cable record: never collected
        circuits.setdefault(cable["circuit"] or "", []).append(cable)
    sizer = host.get("StringSizerStandard") if isinstance(host, dict) else None
    records = []
    for circuit, members in circuits.items():
        string_cable, total = None, 0.0
        for cable in members:
            total += cable["length"]
            if cable["kind"] == STRING_KIND:
                string_cable = cable
        if string_cable is None or device_number_of(string_cable["circuit"]) <= 0:
            continue
        match = CIRCUIT_REGEX.search(string_cable["circuit"])
        if not match:
            continue
        letter = match.group(2)[:1].upper()
        record = {"inverter": device_number_of(string_cable["circuit"]), "mppt": letter,
                  "string": _int_or_zero(match.group(1)), "modules": _int_or_zero(string_cable["panels"]),
                  "homerun_inches": total, "zone": "", "circuit": string_cable["circuit"],
                  "cold_voc_per_module": 0.0, "vmax": 0, "standard": None}
        if isinstance(sizer, dict):
            record["cold_voc_per_module"] = float(sizer.get("max_module_voltage") or 0.0)
            record["vmax"] = int(sizer.get("string_design_voltage") or 0)
            record["standard"] = sizer.get("Conditions") or "Standard"
        records.append(record)
    records.sort(key=lambda r: (r["inverter"], r["mppt"].encode("utf-8"), r["string"]))
    return records


def _l1_to_l2(state, host):
    """UseL2Collectors (host) and the drawing's L1ToL2Assignments; empty turns L1/L2 mode off."""
    if not host.get("UseL2Collectors"):
        return None
    raw = state["setting"].get("L1ToL2Assignments") or {}
    if not isinstance(raw, dict) or not raw:
        return None
    try:
        return {int(k): int(v) for k, v in raw.items()}
    except (TypeError, ValueError):
        raise InverterOutputError("L1ToL2Assignments must map numbers to numbers") from None


def equipment_schedule(inverter, module, records, host):
    """ScheduleRegistry.EquipmentSchedule (:24-91)."""
    rows = []
    if inverter is not None:
        rows += [["- INVERTER -", ""], ["Manufacturer", inverter.get("companyName") or ""],
                 ["Model", inverter.get("modelName") or ""], ["Series", inverter.get("seriesName") or ""],
                 ["Max DC Power (W)", safe_format(inverter.get("maxDCPower"), "N0")],
                 ["Max DC Voltage (V)", safe_format(inverter.get("maxDCVoltage"), "N0")],
                 ["Min DC Voltage (V)", safe_format(inverter.get("minDCVoltageFeed"), "N0")],
                 ["MPPT Voltage Range (V)", safe_format(inverter.get("mpptVoltageRangeMin"), "0") + " - "
                  + safe_format(inverter.get("mpptVoltageRangeMax"), "0")],
                 ["Number of MPPTs", safe_format_int(inverter.get("numMpptTrackers"))],
                 ["Total DC Inputs", safe_format_int(inverter.get("DCInputers"))],
                 ["Max AC Power (W)", safe_format(inverter.get("maxACPower"), "N0")],
                 ["Nominal AC Voltage (V)", safe_format(inverter.get("nominalACVoltage"), "N0")],
                 ["Max AC Current (A)", safe_format(inverter.get("maxACCurrent"), "N1")]]
    if module is not None:
        if inverter is not None:
            rows.append(["", ""])
        vpmax = float(module["VpMax"])
        imp = float(module["Pmax"]) / vpmax if vpmax > 0 else 0.0
        total = sum(r["modules"] for r in records)
        rows += [["- PV MODULE -", ""], ["Manufacturer", module.get("Manufacturer") or ""],
                 ["Model", module.get("Model") or ""], ["Pmax (W)", cs_format(module["Pmax"], "N1")],
                 ["Voc (V)", cs_format(module["Voc"], "N1")], ["Isc (A)", cs_format(module["IpMax"], "N2")],
                 ["Vmp (V)", cs_format(vpmax, "N1")], ["Imp (A)", cs_format(imp, "N2")],
                 ["Temp Coeff Voc (%/C)", cs_format(module["BVoc"], "N3")], ["", ""],
                 ["Quantity", cs_format(total, "N0") if total > 0 else "-"]]
    if host.get("UseOptimizers"):
        raise InverterOutputError("the optimizer section is not ported: the capture host has no optimizers")
    return {"title": "EQUIPMENT SCHEDULE", "headers": ["Field", "Value"], "rows": rows}


def _kw(value):
    return cs_text(cs_round(value, 2))


def combiner_schedule(inverter, module, records, l1_to_l2):
    """ScheduleRegistry.InverterScheduleL1L2 (:210-300)."""
    ac_kw = safe_parse_double(inverter.get("maxACPower")) / 1000.0 if inverter is not None else 0.0
    pmax = float(module["Pmax"]) if module is not None else 0.0
    rows = []
    grand_strings = grand_modules = 0
    grand_dc = grand_ac = 0.0
    by_l2 = {}
    for r in records:
        by_l2.setdefault(l1_to_l2.get(r["inverter"], 0), []).append(r)
    for l2 in sorted(by_l2):
        name = f"INV-{l2}" if l2 > 0 else "?"
        l2_strings = l2_modules = 0
        by_cb = {}
        for r in by_l2[l2]:
            by_cb.setdefault(r["inverter"], []).append(r)
        for cb in sorted(by_cb):
            group = by_cb[cb]
            modules = sum(r["modules"] for r in group)
            counts = list(dict.fromkeys(r["modules"] for r in group))
            per = str(counts[0]) if len(counts) == 1 else ", ".join(str(r["modules"]) for r in group)
            dc = cs_format(modules * pmax / 1000.0, "N2") if pmax > 0 else "-"
            rows.append([name, f"CB-{cb}", str(len(group)), per, str(modules), dc, "-", "-"])
            l2_strings += len(group)
            l2_modules += modules
        l2_dc = l2_modules * pmax / 1000.0 if pmax > 0 else 0.0
        l2_ac = min(l2_dc, ac_kw) if ac_kw > 0 else 0.0
        rows.append([name + " Total", "", str(l2_strings), "", str(l2_modules),
                     _kw(l2_dc) if pmax > 0 else "-", _kw(l2_ac) if ac_kw > 0 else "-",
                     _kw(l2_dc / l2_ac) if (pmax > 0 and ac_kw > 0 and l2_ac > 0) else "-"])
        grand_strings += l2_strings
        grand_modules += l2_modules
        grand_dc += l2_dc
        grand_ac += l2_ac
    rows.append(_grand_total(grand_strings, grand_modules, grand_dc, grand_ac, pmax))
    return {"title": "COMBINER / INVERTER SCHEDULE", "headers": list(COMBINER_HEADERS), "rows": rows}


def _grand_total(strings, modules, dc, ac, pmax):
    return ["GRAND TOTAL", "", str(strings), "", str(modules), _kw(dc) if pmax > 0 else "-",
            _kw(ac) if ac > 0 else "-", _kw(dc / ac) if (dc > 0 and ac > 0) else "-"]


def inverter_schedule_standard(inverter, module, records):
    """ScheduleRegistry.InverterScheduleStandard (:128-208)."""
    ac_kw = safe_parse_double(inverter.get("maxACPower")) / 1000.0 if inverter is not None else 0.0
    pmax = float(module["Pmax"]) if module is not None else 0.0
    rows = []
    grand_strings = grand_modules = 0
    grand_dc = grand_ac = 0.0
    by_inv = {}
    for r in records:
        by_inv.setdefault(r["inverter"], []).append(r)
    for inv in sorted(by_inv):
        inv_strings = inv_modules = 0
        by_mppt = {}
        for r in by_inv[inv]:
            by_mppt.setdefault(r["mppt"], []).append(r)
        for letter in sorted(by_mppt, key=lambda s: s.encode("utf-8")):
            group = by_mppt[letter]
            modules = sum(r["modules"] for r in group)
            counts = list(dict.fromkeys(r["modules"] for r in group))
            per = str(counts[0]) if len(counts) == 1 else ", ".join(str(r["modules"]) for r in group)
            dc = cs_format(modules * pmax / 1000.0, "N2") if pmax > 0 else "-"
            rows.append([f"INV-{inv}", letter, str(len(group)), per, str(modules), dc, "-", "-"])
            inv_strings += len(group)
            inv_modules += modules
        inv_dc = inv_modules * pmax / 1000.0 if pmax > 0 else 0.0
        inv_ac = min(inv_dc, ac_kw) if ac_kw > 0 else 0.0
        rows.append([f"INV-{inv} Total", "", str(inv_strings), "", str(inv_modules),
                     _kw(inv_dc) if pmax > 0 else "-", _kw(inv_ac) if ac_kw > 0 else "-",
                     _kw(inv_dc / inv_ac) if (pmax > 0 and ac_kw > 0 and inv_ac > 0) else "-"])
        grand_strings += inv_strings
        grand_modules += inv_modules
        grand_dc += inv_dc
        grand_ac += inv_ac
    rows.append(_grand_total(grand_strings, grand_modules, grand_dc, grand_ac, pmax))
    return {"title": "INVERTER SCHEDULE", "headers": list(INVERTER_HEADERS), "rows": rows}


def string_schedule(module, records, l1_to_l2):
    """ScheduleRegistry.StringSchedule (:306-399)."""
    headers = list(STRING_HEADERS_L2 if l1_to_l2 else STRING_HEADERS)
    rows = []
    if not records:
        return {"title": "STRING SCHEDULE", "headers": headers, "rows": rows}
    voc = float(module["Voc"]) if module else 0.0
    isc = float(module["IpMax"]) if module else 0.0
    pmax = float(module["Pmax"]) if module else 0.0
    vmp = float(module["VpMax"]) if module else 0.0
    total_modules, total_power = 0, 0.0
    for r in records:
        voc_cold = r["modules"] * r["cold_voc_per_module"] if r["cold_voc_per_module"] > 0 else 0.0
        vmax = r["vmax"]
        margin = vmax - voc_cold if (voc_cold > 0 and vmax > 0) else 0.0
        col1 = (str(l1_to_l2[r["inverter"]]) if r["inverter"] in l1_to_l2 else "?") if l1_to_l2 else r["mppt"]
        rows.append([
            str(r["inverter"]), col1, str(r["string"]), r["zone"] or "-", str(r["modules"]),
            cs_text(cs_round(voc, 1)) if module else "-",
            cs_text(cs_round(voc_cold, 1)) if voc_cold > 0 else "-",
            str(vmax) if vmax > 0 else "-",
            cs_text(cs_round(margin, 1)) if (voc_cold > 0 and vmax > 0) else "-",
            cs_text(cs_round(r["modules"] * vmp, 1)) if module else "-",
            cs_text(cs_round(isc, 2)) if module else "-",
            cs_text(cs_round(isc * 1.25, 2)) if module else "-",
            cs_text(cs_round(r["modules"] * pmax, 0)) if module else "-",
            cs_text(cs_round(r["homerun_inches"] / 12.0, 1)),
            "-", "-", "-",                              # cable sizing runs only with a module (:330)
            r["standard"] or "-"])
        total_modules += r["modules"]
        total_power += r["modules"] * pmax
    totals = [""] * len(headers)
    totals[0], totals[2], totals[4] = "TOTAL", str(len(records)), str(total_modules)
    totals[12] = cs_text(cs_round(total_power, 0)) if module else "-"
    rows.append(totals)
    return {"title": "STRING SCHEDULE", "headers": headers, "rows": rows}


def feeder_schedule(state, host, l1_to_l2):
    """ScheduleRegistry.FeederSchedule (:434-471): one row per feeder cable, its L1 and L2 numbers when
    its source is an assigned L1 (the feeder's stored F<from>/<to> circuit names it), its length. The
    plugin reads the feeders from its in-memory cable index (App.gCableDictionary, :448), not from the
    drawing, so on a drawing freshly reopened from its file (G13) it lists none; `host` says whether the
    session index holds feeders (SessionCableIndexHasFeeders)."""
    rows = []
    if not host.get("UseL2Collectors") or not l1_to_l2 or not host.get("SessionCableIndexHasFeeders"):
        return {"title": "FEEDER SCHEDULE", "headers": list(FEEDER_HEADERS), "rows": rows}
    for item in state["rows"]["cable"]:
        if item["cable_kind"] != "feeder":
            continue
        source = item["from"] if type(item["from"]) is int else None
        l1, l2 = (source, l1_to_l2[source]) if source in l1_to_l2 else (0, 0)
        detail = item.get("_detail") or {}
        length = polyline_length(_points(item["vertices"]), item.get("bulges"), bool(detail.get("closed")))
        rows.append([str(l1), str(l2), cs_text(cs_round(length / 12.0, 1))])
    return {"title": "FEEDER SCHEDULE", "headers": list(FEEDER_HEADERS), "rows": rows}


def build_schedules(state, host):
    """InsertSchedulesCmd.Run steps 1-6 (:31-104): the non-empty schedules in insertion order. Cable
    sizing needs a module and the capture host resolves none (every module column prints "-")."""
    inverter = host.get("InverterCatalogRecord")
    module = host.get("ModuleCatalogRecord")
    records = string_records(state, host)
    l1_to_l2 = _l1_to_l2(state, host)
    tables = [equipment_schedule(inverter, module, records, host)]
    if records:
        tables.append(combiner_schedule(inverter, module, records, l1_to_l2) if l1_to_l2
                      else inverter_schedule_standard(inverter, module, records))
    tables.append(string_schedule(module, records, l1_to_l2))
    tables.append(feeder_schedule(state, host, l1_to_l2))
    if module is not None and records:
        raise InverterOutputError("cable sizing with a module is not ported: the capture host resolves none")
    out = [t for t in tables if t["rows"]]
    if sum(len(t["rows"]) for t in out) > MAX_SCHEDULE_ROWS:
        raise InverterOutputError(f"schedules exceed {MAX_SCHEDULE_ROWS} rows")
    return out


def table_cells(table):
    """ScheduleTableBuilder.Build (:29-86): the title row, the header row, the data rows, every cell's
    text (a short data row leaves its trailing cells empty)."""
    cols = len(table["headers"])
    grid = [[table["title"] or "SCHEDULE"] + [""] * (cols - 1), list(table["headers"])]
    for data in table["rows"]:
        grid.append([(data[c] if c < len(data) and data[c] is not None else "") for c in range(cols)])
    for row_cells in grid:
        for text in row_cells:
            if len(text) > MAX_CELL_CHARS:
                raise InverterOutputError("a schedule cell exceeds the comparator's string bound")
    return grid


def _parse_point(answer):
    parts = answer.split(",") if isinstance(answer, str) else []
    if len(parts) not in (2, 3):
        raise InverterOutputError(f"{answer!r} is not a point answer")
    try:
        values = [float(p) for p in parts]
    except ValueError:
        raise InverterOutputError(f"{answer!r} is not a point answer") from None
    if not all(math.isfinite(v) for v in values):
        raise InverterOutputError(f"{answer!r} is not finite")
    return values[0], values[1]


def insert_schedules(state, host, answers):
    """InsertSchedules (InsertSchedulesCmd.cs:31-121, ScheduleTableBuilder.InsertAll :149-201): one point,
    tables stacked downward by each table's laid-out height plus a gap of four text heights. The text
    height is the drawing's TEXTSIZE (host); a table's height is its rows times 2.5 text heights unless
    its text wraps, when AutoCAD's own layout decides it (host: `ScheduleLayoutHeights`, by title)."""
    after = copy.deepcopy(state)
    if not isinstance(answers, (list, tuple)) or len(answers) != 1:
        raise InverterOutputError("InsertSchedules takes one answer: the insertion point")
    x, y = _parse_point(answers[0])
    tables = build_schedules(after, host)
    if not tables:
        return after, ["No schedule data found. Run string sizing and CableExport first."]
    text = float(host.get("DrawingTextSize") or 0.0)
    gap = text * 4 if text > 0 else 10.0
    height = text if text > 0 else 2.5
    measured = host.get("ScheduleLayoutHeights") or {}
    current = y
    for table in tables:
        grid = table_cells(table)
        after["rows"]["schedule"].append({"index": 0, "position": st.coordinate(x, current),
                                          "rows": len(grid), "cols": len(grid[0]), "cells": grid,
                                          "_pair": st.new_pair(after, "schedule"), "_detail": {"grid": True}})
        laid_out = measured.get(table["title"], len(grid) * height * 2.5)
        current -= (laid_out + gap)
    ordered = sorted(after["rows"]["schedule"], key=lambda item: _schedule_key(item))
    for index, item in enumerate(ordered, 1):
        item["index"] = index
    st.sort_rows(after)
    return after, [f"Inserted {len(tables)} schedule table(s)."]


def _schedule_key(item):
    x, y = st.point_of(item["position"])
    return st.order_key(x, y)


# ------------------------------------------------------------- cable export --

def _column(index):
    name = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name
    return name


def write_workbook(sheets):
    """A minimal, deterministic .xlsx (stdlib zip and XML, inline strings, a fixed timestamp):
    sheets is [(name, grid)]."""
    names = [name for name, _ in sheets]
    if not names or len(set(names)) != len(names) or any(len(n) > 31 or re.search(r"[\[\]:*?/\\]", n)
                                                          for n in names):
        raise InverterOutputError("workbook sheet names must be unique, at most 31 characters, no []:*?/\\")
    parts = {
        "[Content_Types].xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
                      'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                      for i in range(1, len(sheets) + 1)) + "</Types>",
        "_rels/.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
            + "".join(f'<sheet name="{escape(n, {chr(34): "&quot;"})}" sheetId="{i}" r:id="rId{i}"/>'
                      for i, n in enumerate(names, 1)) + "</sheets></workbook>",
        "xl/_rels/workbook.xml.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(f'<Relationship Id="rId{i}" '
                      'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                      f'Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(sheets) + 1))
            + "</Relationships>",
    }
    for i, (_, grid) in enumerate(sheets, 1):
        body = []
        for r, row_cells in enumerate(grid, 1):
            cells = "".join(f'<c r="{_column(c)}{r}" t="inlineStr"><is><t xml:space="preserve">'
                            f"{escape(str(text))}</t></is></c>" for c, text in enumerate(row_cells) if text != "")
            body.append(f'<row r="{r}">{cells}</row>')
        parts[f"xl/worksheets/sheet{i}.xml"] = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
            + "".join(body) + "</sheetData></worksheet>")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in sorted(parts):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, parts[name].encode("utf-8"))
    return buffer.getvalue()


def workbook_text(sheets):
    """The workbook as the file row's text: per sheet a "# <name>" line, then one line per row, cells
    joined by tabs (tabs and line feeds inside a cell become spaces)."""
    lines = []
    for name, grid in sheets:
        lines.append(f"# {name}")
        for row_cells in grid:
            lines.append("\t".join(re.sub(r"[\t\r\n]", " ", str(text)) for text in row_cells))
    return "\n".join(lines) + "\n"


def g21_chunks(text, limit=MAX_CHUNK):
    """G21: chunks that concatenate to the text, split only after a line feed, each at most `limit`."""
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        if len(line) > limit:
            raise InverterOutputError(f"a workbook line exceeds {limit} characters")
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current or not chunks:
        chunks.append(current)
    return chunks


def file_row(text, role=WORKBOOK_ROLE, number=1):
    """A G20/G21 `file` row: role, chunks, lines."""
    lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    return {"id": {"entity_id": f"file-{number}"}, "type": "file", "quantity": 1, "unit": "each",
            "role": role, "chunks": g21_chunks(text), "lines": lines}


# ----------------------------------------------------------- export sheets --
# StringHomerunExportForm.cs (Branch2025 master a94db8d9, read 2026-09-25). A sheet is a list of rows, each
# row the cells the form sets from column A through the last one it sets (a row it skips is empty), so
# workbook_text renders exactly the cells the plugin wrote.

# :181-187, the list view's columns (no elevation or electrical zone columns: the G35 state carries neither).
HOMERUN_HEADERS = ("Inverter/MPPT", "Circuit", "Which", "Mod / String", "Length (ft)", "Circuit Length (ft)",
                   "One-Way Length (ft)")
# CableData.mCableType, the list view's "Which" (:2606), per G35 cable kind and homerun segment.
HOMERUN_WHICH = {("dc-homerun", "start"): "Start Homerun", ("dc-homerun", "end"): "End Homerun",
                 ("feeder", None): "Feeder"}
# CableData.mNumPanels of every cable that is not a string, the list view's "Mod / String" (:2607).
NO_PANELS = "NA"
# AppConstants.UnassignedCircuitText (LeafSolarDesign.Core/AppConstants.cs:33): each such cable is its own
# circuit, after the grouped ones (StringCalculations.cs:34-45).
UNASSIGNED_CIRCUIT = "-"
# ListViewItemCircuitComparer's key of a row whose circuit does not match (:2577-2578, :2682-2684).
INT_MAX = 2**31 - 1
# Array.IntrosortSizeThreshold (.NET 8): partitions this small are insertion sorted.
INTROSORT_SIZE_THRESHOLD = 16
# :772.
EXPORT_EQUIPMENT_HEADERS = ("Tag", "Description", "Manufacturer", "Model", "Qty", "Rating", "Listing", "NEC Ref")
# :865, the AC disconnect's OCPD sizes (above the last, the ceiling of 125 % of the current, :867).
AC_OCPD_SIZES = (15, 20, 25, 30, 35, 40, 45, 50, 60, 70, 80, 90, 100, 125, 150, 175, 200)
# :908-914.
NEC_FOOTNOTES = ("690.4 - Installation requirements for PV equipment",
                 "690.7(A)(3) - Maximum system voltage: Voc corrected for lowest expected ambient temperature",
                 "690.8(A) - Maximum circuit current: Isc × 1.25 for continuous duty",
                 "690.13 - DC photovoltaic disconnecting means",
                 "690.54 - Interactive system point of interconnection")
# :942-943.
EXPORT_INVERTER_HEADERS = ("Inverter", "MPPT", "Strings", "Inputs", "Mod/String", "Modules", "DC (kW)",
                           "Voc_cold (V)", "Max Vdc", "Isc×1.25 (A)", "Status")
# :1088-1092.
EXPORT_STRING_HEADERS = ("String ID", "Inv", "MPPT", "Elec Zone", "Modules", "Voc (V)", "Voc_cold (V)",
                         "Inv Vmax (V)", "Margin (V)", "Vmp (V)", "Isc (A)", "Isc×1.25 (A)", "Power (W)",
                         "HR Length (ft)", "Wire Gauge", "Ampacity (A)", "Vdrop (%)", "Design Std")
# :43, the design low when no project location resolves an ASHRAE temperature (:126-147).
DEFAULT_DESIGN_MIN_TEMP_C = -40.0
_VOC_COLD_RULE = "Voc × (1 + βvoc/100 × (Tmin − 25°C)), Tmin = "


def _export_host(host):
    """(inverter count, design low in C, project location) the export form reads (:43, :126-149): the
    SuggestedInverterCount setting (1 when not positive), the design low and the location."""
    count = host.get("SuggestedInverterCount")
    count = 0 if count is None else count
    if isinstance(count, bool) or not isinstance(count, int):
        raise InverterOutputError("SuggestedInverterCount must be an integer")
    low = host.get("DesignMinTempC", DEFAULT_DESIGN_MIN_TEMP_C)
    if isinstance(low, bool) or not isinstance(low, (int, float)) or not math.isfinite(low):
        raise InverterOutputError("DesignMinTempC must be a finite number")
    location = host.get("ProjectLocation")
    if location is not None and not isinstance(location, str):
        raise InverterOutputError("ProjectLocation must be text")
    return (count if count > 0 else 1), float(low), location or None


def _catalog_ac_kw(inverter):
    """The inverter's AC power in kW. The catalog stores maxACPower in kW (the SG250HX row reads 250 beside
    a maxDCPower of 375), and the export form reads it as kW since Branch2025 #327 (the retired
    docs/parity/divergences/cable-export/inverter-rating-units.md)."""
    return safe_parse_double(inverter.get("maxACPower")) if inverter is not None else 0.0


# --------------------------------------------- .NET 8 List<T>.Sort(IComparer<T>) --
# List<T>.Sort -> Array.Sort -> ArraySortHelper<T>.Sort -> IntrospectiveSort (dotnet/runtime release/8.0,
# src/libraries/System.Private.CoreLib/src/System/Collections/Generic/ArraySortHelper.cs). The plugin runs
# on AutoCAD 2025 (LeafSolarDesign2025.csproj: net8.0-windows). Unstable: rows the comparer ties keep the
# positions this exact algorithm leaves them in, so it is ported step for step. Every function works on the
# window keys[lo:lo + size] in place; recursion depth is bounded by the depth limit (2 * (log2 n + 1)).

def _swap_if_greater(keys, compare, i, j):
    if compare(keys[i], keys[j]) > 0:
        keys[i], keys[j] = keys[j], keys[i]


def _insertion_sort(keys, lo, size, compare):
    for i in range(size - 1):
        t = keys[lo + i + 1]
        j = i
        while j >= 0 and compare(t, keys[lo + j]) < 0:
            keys[lo + j + 1] = keys[lo + j]
            j -= 1
        keys[lo + j + 1] = t


def _down_heap(keys, lo, i, n, compare):
    d = keys[lo + i - 1]
    while i <= n >> 1:
        child = 2 * i
        if child < n and compare(keys[lo + child - 1], keys[lo + child]) < 0:
            child += 1
        if not compare(d, keys[lo + child - 1]) < 0:
            break
        keys[lo + i - 1] = keys[lo + child - 1]
        i = child
    keys[lo + i - 1] = d


def _heap_sort(keys, lo, size, compare):
    for i in range(size >> 1, 0, -1):
        _down_heap(keys, lo, i, size, compare)
    for i in range(size, 1, -1):
        keys[lo], keys[lo + i - 1] = keys[lo + i - 1], keys[lo]
        _down_heap(keys, lo, 1, i - 1, compare)


def _pick_pivot_and_partition(keys, lo, size, compare):
    """Median of three (low, middle, high) put in order, the middle the pivot parked at hi - 1, then the
    Hoare scan; returns the pivot's final index relative to lo."""
    hi = size - 1
    middle = hi >> 1
    _swap_if_greater(keys, compare, lo, lo + middle)
    _swap_if_greater(keys, compare, lo, lo + hi)
    _swap_if_greater(keys, compare, lo + middle, lo + hi)
    pivot = keys[lo + middle]
    keys[lo + middle], keys[lo + hi - 1] = keys[lo + hi - 1], keys[lo + middle]
    left, right = 0, hi - 1
    while left < right:
        left += 1
        while compare(keys[lo + left], pivot) < 0:
            left += 1
        right -= 1
        while compare(pivot, keys[lo + right]) < 0:
            right -= 1
        if left >= right:
            break
        keys[lo + left], keys[lo + right] = keys[lo + right], keys[lo + left]
    if left != hi - 1:
        keys[lo + left], keys[lo + hi - 1] = keys[lo + hi - 1], keys[lo + left]
    return left


def _intro_sort(keys, lo, size, depth_limit, compare):
    while size > 1:
        if size <= INTROSORT_SIZE_THRESHOLD:
            if size == 2:
                _swap_if_greater(keys, compare, lo, lo + 1)
            elif size == 3:
                _swap_if_greater(keys, compare, lo, lo + 1)
                _swap_if_greater(keys, compare, lo, lo + 2)
                _swap_if_greater(keys, compare, lo + 1, lo + 2)
            else:
                _insertion_sort(keys, lo, size, compare)
            return
        if depth_limit == 0:
            _heap_sort(keys, lo, size, compare)
            return
        depth_limit -= 1
        p = _pick_pivot_and_partition(keys, lo, size, compare)
        _intro_sort(keys, lo + p + 1, size - p - 1, depth_limit, compare)
        size = p


def dotnet_list_sort(keys, compare):
    """List<T>.Sort(IComparer<T>) on .NET 8, in place: IntrospectiveSort with the depth limit
    2 * (Log2(n) + 1). `compare(a, b)` is IComparer<T>.Compare (negative, zero, positive). O(n log n)."""
    if len(keys) > 1:
        _intro_sort(keys, 0, len(keys), 2 * len(keys).bit_length(), compare)
    return keys


def _handle_value(handle):
    """A drawing handle's number, None when it is not hex text."""
    return int(handle, 16) if isinstance(handle, str) and re.fullmatch(r"[0-9A-Fa-f]{1,16}", handle) else None


def _feeder_creation_ranks(state):
    """{L1 number: the order its feeder was drawn in}: RouteL2Feeders draws one feeder per assignment in
    the assignment dictionary's insertion order (OptiHomerunCmd.cs:1271-1305 via :1080-1135), which is the
    L1 list List.Sort'ed by the distance to the nearest L2 after the phantom guard (:1051, :1085-1096).
    Measured: on the plugin drawing (raw/i9.txt) the feeder handles AB39..AB46 ascend in exactly this
    order (F8, F12, F7, F1, F5, F4, F11, F14, F2, F9, F13, F3, F10, F6). O(L1 * L2)."""
    cab = _load_sibling("solar_inverter_cabling")
    try:
        l1, l2 = cab.levels(state)
        l2, _ = cab.filter_phantom_l2(l1, l2)
    except cab.InverterCablingError as exc:
        raise InverterOutputError(str(exc)) from None
    keyed = [(min((cab._dist(item["position"], other["position"]) for other in l2), default=math.inf),
              item["number"]) for item in l1 if item["number"] is not None]
    dotnet_list_sort(keyed, lambda a, b: (a[0] > b[0]) - (a[0] < b[0]))
    return {number: rank for rank, (_, number) in enumerate(keyed)}


def _select_all_order(cable, feeder_ranks):
    """Where Editor.SelectAll puts a cable polyline (StringCalculations.cs:51-92): the database's newest
    entity first. Measured on the plugin drawing CableExport read (receipt w7-testbuild2-20260925,
    raw/i9.txt, ssget "X" in the same order): the 14 feeders first, newest drawn first (feeder_ranks,
    _feeder_creation_ranks), then the homeruns, string by string in ascending string handle, each
    circuit's end homerun before its start homerun, then the string polylines in descending handle. A
    feeder whose L1 is not ranked follows the ranked ones in state order."""
    if cable["kind"] == "feeder":
        rank = feeder_ranks.get(cable["source"]) if type(cable["source"]) is int else None
        return (0, 0, -rank) if rank is not None else (0, 1, 0)
    handle = _handle_value(cable["source"])
    if cable["kind"] == STRING_KIND:
        return (2, 0, -handle) if handle is not None else (2, 1, 0)
    segment = {"end": 0, "start": 1}.get(cable["segment"], 2)
    return (1, 0, handle, segment) if handle is not None else (1, 1, 0, segment)


def _compare_circuit_keys(a, b):
    """ListViewItemCircuitComparer (:2572-2581): the circuit number only."""
    return (a[0] > b[0]) - (a[0] < b[0])


def homeruns_sheet(state):
    """WriteHomerunsSheet (:1862-1888) over the detailed view PreCalculateViewItems builds (:2457-2552,
    CreateListViewItem :2584-2689): the header, then one row per cable. The rows are built in mCables
    order (StringCalculations.performCalc, :87-88): the cables in SelectAll order (_select_all_order),
    grouped by circuit in order of first appearance, each group's cables in that order, the unassigned
    ("-") cables after as groups of one (StringCalculations.cs:34-45); then List.Sort by the circuit
    number, int.MaxValue for an unmatched circuit (:2540-2542), which is .NET's unstable introsort
    (dotnet_list_sort). The permutation it applies depends only on the key sequence, so with the feeders
    entering in the plugin's SelectAll order every row lands where the plugin's does. Fails closed on a
    cable kind the plugin has no name for."""
    cables = [cable for cable in _cables(state) if cable["circuit"] is not None]           # None: never collected
    feeder_ranks = _feeder_creation_ranks(state) if any(c["kind"] == "feeder" for c in cables) else {}
    ordered = sorted(cables, key=lambda cable: _select_all_order(cable, feeder_ranks))
    groups, unassigned = {}, []
    for cable in ordered:
        if cable["circuit"] == UNASSIGNED_CIRCUIT:
            unassigned.append([cable])
        else:
            groups.setdefault(cable["circuit"], []).append(cable)
    items = []
    for members in list(groups.values()) + unassigned:
        circuit = members[0]["circuit"]
        match = CIRCUIT_REGEX.search(circuit)
        total_ft = sum(cable["length"] for cable in members) / 12.0
        for cable in members:
            if cable["kind"] == STRING_KIND:
                which, panels = STRING_KIND, "" if cable["panels"] is None else str(cable["panels"])
            else:
                which, panels = HOMERUN_WHICH.get((cable["kind"], cable["segment"])), NO_PANELS
                if which is None:
                    raise InverterOutputError(f"cable kind {cable['kind']!r} ({cable['segment']!r}) has no "
                                              "export name")
            length = cs_fixed(cable["length"] / 12.0, 2)
            if match:
                items.append((int(match.group(1)),
                              [f"{device_number_of(circuit)} - {match.group(2)}", match.group(1), which, panels,
                               length, cs_fixed(total_ft, 2), cs_fixed(total_ft / 2, 2)]))
            else:
                items.append((INT_MAX, ["-", "-", which, panels, length, "N/A", "N/A"]))
            if len(items) > MAX_SCHEDULE_ROWS:
                raise InverterOutputError(f"the homerun sheet exceeds {MAX_SCHEDULE_ROWS} rows")
    dotnet_list_sort(items, _compare_circuit_keys)
    return [list(HOMERUN_HEADERS)] + [row for _, row in items]


def export_equipment_sheet(inverter, count, low, location):
    """WriteEquipmentSchedule (:757-925) with no module and no optimizer (cable_export refuses both): the
    title merged over eight columns, a gap row, the tagged inverter and disconnect rows, a gap row, DESIGN
    PARAMETERS, a gap row and NEC REFERENCES. None when there is no inverter (:759)."""
    if inverter is None:
        return None
    ac_v = safe_parse_double(inverter.get("nominalACVoltage"))
    ac_a = safe_parse_double(inverter.get("maxACCurrent"))
    max_dc = safe_parse_double(inverter.get("maxDCVoltage"))
    rating = (cs_format(_catalog_ac_kw(inverter), "N1") + "kW AC, " + cs_format(ac_v, "0") + "V, "
              + cs_format(ac_a, "N1") + "A")
    ocpd = next((size for size in AC_OCPD_SIZES if size >= ac_a * 1.25), 0) or math.ceil(ac_a * 1.25)
    grid = [["EQUIPMENT SCHEDULE"], [], list(EXPORT_EQUIPMENT_HEADERS),
            [f"INV-1..{count}" if count > 1 else "INV-1", "String Inverter", inverter.get("companyName") or "",
             inverter.get("modelName") or "", str(count), rating,
             "UL 1741 SA" if inverter.get("solarEdgeOptimized") is True else "UL 1741", "690.4"],
            ["DC-DISC", "DC Disconnect", "(by installer)", "-", str(count),
             cs_format(max_dc, "0") + "V, 30A" if max_dc > 0 else "Per inverter spec", "UL 98", "690.13"],
            ["AC-DISC", "AC Disconnect", "(by installer)", "-", str(count),
             cs_format(ac_v, "0") + "V, " + str(ocpd) + "A", "UL 98", "690.54"],
            [], ["DESIGN PARAMETERS"],
            ["Design Min Temp", cs_format(low, "0") + "°C per NEC 690.7(A)(3)"]]
    if location:
        grid.append(["Project Location", location])
    return grid + [[], ["NEC REFERENCES"]] + [[note] for note in NEC_FOOTNOTES]


def _by(records, key):
    groups = {}
    for record in records:
        groups.setdefault(record[key], []).append(record)
    return groups


def _modules_per_string(group):
    counts = list(dict.fromkeys(r["modules"] for r in group))
    return str(counts[0]) if len(counts) == 1 else ", ".join(str(r["modules"]) for r in group)


def export_inverter_sheet(inverter, records, low):
    """WriteInverterScheduleCD (:927-1075) with no module, so every module column prints "-" and no row
    has a status: one row per inverter and MPPT, each inverter's subtotal, a gap row, the grand total, a
    gap row and the Voc_cold footnote. None without string records (:929)."""
    if not records:
        return None
    max_dc = safe_parse_double(inverter.get("maxDCVoltage")) if inverter is not None else 0.0
    inputs = _int_or_zero(inverter.get("DCInputers")) if inverter is not None else 0
    mppts = _int_or_zero(inverter.get("numMpptTrackers")) if inverter is not None else 0
    per_mppt = -(-inputs // mppts) if (mppts > 0 and inputs > 0) else 0
    ac_kw = _catalog_ac_kw(inverter)
    grid = [list(EXPORT_INVERTER_HEADERS)]
    grand_strings = grand_modules = 0
    by_inverter = _by(records, "inverter")
    for number in sorted(by_inverter):
        inverter_strings = inverter_modules = 0
        by_mppt = _by(by_inverter[number], "mppt")
        for letter in sorted(by_mppt):
            group = by_mppt[letter]
            modules = sum(r["modules"] for r in group)
            grid.append([f"INV-{number}", letter, str(len(group)),
                         f"{len(group)}/{per_mppt}" if per_mppt > 0 else str(len(group)),
                         _modules_per_string(group), str(modules), "-", "-",
                         cs_text(max_dc) if max_dc > 0 else "-", "-", ""])
            inverter_strings += len(group)
            inverter_modules += modules
        grid.append([f"INV-{number} Total", "", str(inverter_strings), "", "", str(inverter_modules), "-",
                     "", "", "", "DC/AC: -" if ac_kw > 0 else ""])
        grand_strings += inverter_strings
        grand_modules += inverter_modules
    grid += [[], ["GRAND TOTAL", "", str(grand_strings), "", "", str(grand_modules), "-", "", "", "",
                  "DC/AC: -" if ac_kw * len(by_inverter) > 0 else ""],
             [], ["Voc_cold calculated per NEC 690.7(A)(3): " + _VOC_COLD_RULE + cs_format(low, "0") + "°C"]]
    return grid


def export_string_sheet(inverter, records, low):
    """WriteStringSchedule (:1077-1284) with no module: one row per string record (the sizer's cold Voc,
    the inverter's Vmax, the homerun length), a gap row, the total, a gap row and two footnotes. Cable
    sizing runs only with a module (:635), and the legacy fallback's key <inv>/<mppt><string> (:1160)
    never names a stored +<string>/<device><mppt> circuit, so gauge, ampacity and Vdrop print "-". None
    without string records (:1079)."""
    if not records:
        return None
    max_dc = safe_parse_double(inverter.get("maxDCVoltage")) if inverter is not None else 0.0
    grid = [list(EXPORT_STRING_HEADERS)]
    total_modules = 0
    for r in records:
        voc_cold = r["modules"] * r["cold_voc_per_module"] if r["cold_voc_per_module"] > 0 else 0.0
        vmax = r["vmax"] if r["vmax"] > 0 else int(max_dc)
        grid.append([f"S{r['inverter']}-{r['mppt']}{r['string']}", str(r["inverter"]), r["mppt"],
                     r["zone"] or "-", str(r["modules"]), "-",
                     cs_text(cs_round(voc_cold, 1)) if voc_cold > 0 else "-",
                     str(vmax) if vmax > 0 else "-",
                     cs_text(cs_round(vmax - voc_cold, 1)) if (voc_cold > 0 and vmax > 0) else "-",
                     "-", "-", "-", "-", cs_text(cs_round(r["homerun_inches"] / 12.0, 1)), "-", "-", "-",
                     r["standard"] or "-"])
        total_modules += r["modules"]
    return grid + [[], ["TOTAL", "", f"{len(records)} strings", "", str(total_modules)], [],
                   ["Voc_cold per NEC 690.7(A)(3): " + _VOC_COLD_RULE + cs_format(low, "0")
                    + "°C | Isc×1.25 per NEC 690.8(A)(1) continuous duty"],
                   ["Cable sizing per NEC 310.16 (ampacity) + NEC 210.19 FPN (voltage drop ≤ 2% recommended)"]]


def cable_export(state, host, form_values):
    """CableExport, Export All (StringExportCmd.cs:33-94, StringHomerunExportForm.cs:318-413; declared, see
    the module docstring): the homerun cables found, the workbook the form writes, in its sheet order:
    Homeruns, Equipment, Inverter and String Schedule, then the Feeder Schedule when L2 collectors are on
    and the drawing holds L1ToL2Assignments (:373, :710-755). A module (its columns and the Cable Sizing
    Schedule) or an optimizer is not ported and is refused. Returns (after state, printed lines, workbook
    or None), the workbook {"sheets", "bytes", "text"}; the drawing is unchanged."""
    after = copy.deepcopy(state)
    if (form_values or {}).get("branch_string_export") != "Export All":
        raise InverterOutputError("CableExport's dialog answer must be Export All")
    if not any(True for cable in _cables(after) if cable["circuit"] is not None):
        return after, ["No homerun polylines found."], None
    if host.get("ModuleCatalogRecord") is not None:
        raise InverterOutputError("the export's module columns and cable sizing are not ported: the capture "
                                  "host resolves no module")
    if host.get("UseOptimizers"):
        raise InverterOutputError("the export's optimizer row is not ported: the capture host has no optimizers")
    inverter = host.get("InverterCatalogRecord")
    count, low, location = _export_host(host)
    records = string_records(after, host)
    sheets = [("Homeruns", homeruns_sheet(after))]
    for name, grid in (("Equipment Schedule", export_equipment_sheet(inverter, count, low, location)),
                       ("Inverter Schedule", export_inverter_sheet(inverter, records, low)),
                       ("String Schedule", export_string_sheet(inverter, records, low))):
        if grid is not None:
            sheets.append((name, grid))
    l1_to_l2 = _l1_to_l2(after, host)
    if l1_to_l2:
        sheets.append(("Feeder Schedule", [list(FEEDER_HEADERS)] + feeder_schedule(after, host, l1_to_l2)["rows"]))
    if sum(len(grid) for _, grid in sheets) > MAX_SCHEDULE_ROWS:
        raise InverterOutputError(f"the workbook exceeds {MAX_SCHEDULE_ROWS} rows")
    if any(len(text) > MAX_CELL_CHARS for _, grid in sheets for row_cells in grid for text in row_cells):
        raise InverterOutputError("a workbook cell exceeds the comparator's string bound")
    data = write_workbook(sheets)
    return after, [f"CableExport: wrote {len(sheets)} sheet(s)."], \
        {"sheets": sheets, "bytes": data, "text": workbook_text(sheets)}
