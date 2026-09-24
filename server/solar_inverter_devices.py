"""Studio ports of the plugin's inverter device commands (contract G35, G35a): inverter-add (i1
AddAllInverters and i18 ADDINVERTER), inverter-balance (i4 INVBALANCE), adopt-l2-inverters (i19
LEAFADOPTL2INVERTERS) and skid-reconcile (i10 LEAFSKIDRECONCILE).

Literal ports of Branch2025 (read 2026-09-24 at C:/tmp/solar-parity/wt-b25-s69, master 6b940d51, the
captured build):

  inverter-add, all (BranchCmd.InverterAdd(addAll: true), Commands.cs:1620-1676)
    BranchCmd.cs:11554-11560   ground pattern default: never on a Roof drawing
    BranchCmd.cs:11648-11658   L1/L2 mode: AddAllInverters places central (L2) inverters
    BranchCmd.cs:11737-11758   the unassigned strings (circuit "-", GetUnassignedStrings :16945-16982);
                               none: nothing placed
    BranchCmd.cs:11771-11814   existing equipment of the same type with spare capacity: refused
    BranchCmd.cs:11834-11846   numInverters = ceil(strings / strings per inverter)
    BranchCmd.cs:11848-11881   the L2 sizing recommendation (TryRecommendL2InverterSizing :10700-10722):
                               no panel-group DC capacity in the drawing, so none
    BranchCmd.cs:11961-11993   "Inverters count differs from StringSizer" (Yes proceeds, No stops)
    BranchCmd.cs:12011-12087   "Low utilization on last inverter" when the last one is under 50 % full
                               (Keep current proceeds, Cancel stops)
    BranchCmd.cs:12854-12972   the cloud optimizer, placement extents from the panel-group outlines
                               (GetPanelGroupExtentsOrFallback :15255-15292); NOT ported: the captured
                               call answered 503 after 600 s and the plugin took its fallback
    BranchCmd.cs:13083-13110   the fallback: BuildGridFallbackPlacement (:15898-15925) over
                               BuildGridPlacementPoints (:15927-15964): an aspect-shaped grid of cell
                               centres over the extents, row by row
    BranchCmd.cs:13186-13253   each point moved out of every panel-group outline it lies in: to the
                               nearest boundary point, then 50 units further along that direction
                               (PointIsInside, PolylineExtensions.cs:89-114, :357-453)
    BranchCmd.cs:13253, :13286-13295
                               the block (PlaceNewInverterBlock, StringHomeRunCmd.cs:1363-1586) with the
                               fallback marker AUTO_PLACED_NOT_OPTIMIZED (BranchCmd.cs:182)
  inverter-add, one (BranchCmd.InverterAdd(addAll: false), Commands.cs:1530-1571)
    BranchCmd.cs:11672-11727   "Select equipment type": L1 (combiner box) or L2 (central inverter)
    StringHomeRunCmd.cs:1384-1453
                               the number prompt (default the lowest unused number of that level) and
                               the insertion point
    BranchCmd.cs:13114-13122   an L2 inverter takes no strings; an L1 block goes on to
    BranchCmd.cs:13366-13456   the pre-assigned strings, else "Select string assignment mode
                               [Manual/AddLater]"; AddLater defers every assignment
  common to both
    StringHomeRunCmd.cs:1478-1484, AcadCommandBase.cs:121-141, ModuleScaleResolver.cs:23-119
                               the symbol scale: module scale x (3.0 central, 1.0 combiner) / the block's
                               native size; a host and block-definition quantity (see HOST INPUTS)
    StringHomeRunCmd.cs:1568-1575, InverterPlacementHelper.cs:21-38, Inverter.cs:260-272
                               the record: L2 flag, type key A, combiner input count (L1 only)
  inverter-balance (InverterBalanceCmd, InverterBalancingForm.cs)
    InverterBalancingForm.cs:171-208
                               the manager analyses App.gInverterList only, which holds the L1 blocks
                               (DocumentEventHandler.cs:310-318 files every L2 block under
                               gL2CollectorList instead); "Central Inverters" is a display tab
    InverterBalancingForm.cs:991-1000, MpptBalanceAnalyzer.cs:1158-1189
                               Auto-Balance All finds no swap when there is no summary, shows "No
                               auto-balance swaps available" and writes nothing. That is why the
                               captured i4 changed nothing: every device on that drawing is an L2
                               central inverter, so the L1 list is empty.
  adopt-l2-inverters (FixedL2InverterAdoptionCmd.cs:144-271)
    :155-196                   candidates: every block reference of the configured inverter block
                               (GetConfiguredCentralInverterNames :340-358); the plugin places both L1
                               and L2 blocks with that one block (StringHomeRunCmd.cs:1464-1478), so
                               every device row is a candidate
    :198-202                   ordered by X, then Y
    :403-426, :428-468, :470-512, :524-571
                               the hardware: the block name's database match and the dialog's fuzzy list
                               are database calls whose fallbacks are empty; the configured L2 selection
                               resolves through InverterCatalog (LeafSolarDesign.Core/InverterCatalog.cs:
                               56-165) to its display name and rated AC; the dialog's default mapping,
                               confirmed with Adopt, stamps match score 100 (:1123-1136)
    :211-262, FixedL2InverterMetadata.cs:84-110
                               the numbering and the record: L2, the type key, input count 0, FIXED_L2,
                               the model, the AC rating ("0.###") and the match score
  skid-reconcile (LeafSkidReconcileCommand.cs:49-111, LeafSolarDesign.Core/SkidReconciler.cs:101-211)
                               L1ToL2Assignments against L1CollectorsPerL2 slots per L2; the printed
                               verdict and messages

Declared divergence: none. Every capability here is zero-diff under G35.

HOST INPUTS. Several values the commands read live in the capture host's per-user settings or in a
block definition, never in the drawing state; the engines take them as the `host` argument, named
by the plugin setting they stand for (see the producer, scripts/solar_inverter_devices_evidence.py,
for the captured values and how each was measured).

Pure functions over plain data: no CAD host, no I/O, no network. Every engine takes a G35 state
(server/solar_inverter_state.py), never mutates it, and returns (new state, printed lines). Bounded:
linear in devices, strings and outline vertices. Malformed input fails closed with
InverterDeviceError; a path this port does not cover raises InverterNotPortedError, never a guess.
"""
from __future__ import annotations

import copy
from decimal import ROUND_HALF_UP, Decimal
import importlib.util
import math
from pathlib import Path
import re
import sys


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

# BranchCmd.cs:182.
FALLBACK_MARKER = "AUTO_PLACED_NOT_OPTIMIZED"
# FixedL2InverterMetadata.cs:76.
FIXED_L2 = "FIXED_L2"
# ModuleScaleResolver.cs:25-26 (ModuleVisualScale).
CENTRAL_INVERTER_MULTIPLIER, COMBINER_BOX_MULTIPLIER = 3.0, 1.0
# BranchCmd.cs:13238-13240: the distance a point is moved past the nearest outline boundary.
OUTLINE_CLEARANCE = 50.0
# BranchCmd.cs:13222: segments shorter than this are skipped in the nearest-boundary search.
MIN_SEGMENT = 0.001
# BranchCmd.cs:12028-12029: the low-utilization dialog fires below this fill of the last inverter.
LOW_FILL_PERCENT = 50.0
# LeafSkidReconcileCommand.cs:78-79.
DEFAULT_L1_PER_L2 = 12
ROOF = "Roof"                                  # AppConstants.Roof
UNASSIGNED_CIRCUIT = "-"                       # AppConstants.UnassignedCircuitText
MAX_DEVICE_NUMBER = 100_000                    # StringHomeRunCmd.cs:1389
MAX_OUTLINE_VERTICES_TOTAL = 1_000_000
MAX_PANEL_GROUPS = 20_000
MAX_INVERTERS = 10_000

# The dialog answers (G30a form values) and what each selects.
EQUIPMENT_CHOICES = {"Combiner box": False, "String inverter": False, "Central inverter": True}
RESIZE_KEEP, RESIZE_APPLY, RESIZE_CANCEL = "Keep current", "Apply recommended", "Cancel"
ASSIGN_MANUAL, ASSIGN_LATER = "Manual", "AddLater"

HOST_KEYS = frozenset({
    "UseL2Collectors", "UseCombinerBox", "UsePatternPlacement", "StringsPerCentralInverter",
    "CombinerBoxConnections", "SuggestedInverterCount", "L1CollectorsPerL2", "L2InverterSelection",
    "CentralInverterSymbolScale", "CombinerSymbolScale", "OsnapApertureDrawingUnits", "SessionColorCounter"})


class InverterDeviceError(ValueError):
    """A malformed state, host input or answer: nothing is placed."""


class InverterNotPortedError(InverterDeviceError):
    """The command would take a path this port does not cover (named in the message)."""


# ------------------------------------------------------------------ inputs --

def _host(host):
    if not isinstance(host, dict) or set(host) - HOST_KEYS:
        raise InverterDeviceError(f"host inputs must be an object of {sorted(HOST_KEYS)}")
    return host


def _host_value(host, key, kinds, what):
    value = host.get(key)
    if value is None or type(value) not in kinds or (type(value) is float and not math.isfinite(value)):
        raise InverterDeviceError(f"host input {key} ({what}) is required")
    return value


def _positive(host, key, what):
    value = _host_value(host, key, (int, float), what)
    if value <= 0:
        raise InverterDeviceError(f"host input {key} ({what}) must be positive")
    return value


def _is_l2(device):
    """A device's L2 flag as its record stores it (Inverter.cs:121-167): the private detail when
    present, else its role (a combiner is the only L1 role)."""
    detail = device.get("_detail")
    if isinstance(detail, dict) and type(detail.get("is_l2")) is bool:
        return detail["is_l2"]
    return device["role"] != "combiner"


def _number(device):
    value = device.get("_number")
    return value if type(value) is int and value > 0 else None


def _next_number(devices):
    """The lowest number no device of the list holds (StringHomeRunCmd.cs:1386-1396)."""
    used = {n for n in (_number(d) for d in devices) if n is not None}
    for candidate in range(1, MAX_DEVICE_NUMBER):
        if candidate not in used:
            return candidate
    return 1


def _levels(state):
    """(L1 list, L2 list): App.gInverterList and App.gL2CollectorList (DocumentEventHandler.cs:
    310-318)."""
    devices = state["rows"]["device"]
    return [d for d in devices if not _is_l2(d)], [d for d in devices if _is_l2(d)]


def _setting(state, name, default):
    value = state["setting"].get(name, default)
    return value if value is not None else default


def _finite_xy(point, what):
    if not isinstance(point, (list, tuple)) or len(point) < 2:
        raise InverterDeviceError(f"{what} must be a point")
    x, y = point[0], point[1]
    for v in (x, y):
        if type(v) not in (int, float) or not math.isfinite(v):
            raise InverterDeviceError(f"{what} must be finite")
    return float(x), float(y)


def validate_panel_groups(panel_groups, state):
    """The panel groups the fallback reads, in GetPanelGroupData order: [{handle, outlines}], outlines
    world-coordinate polygons (every polyline of the group's block definition, PanelGroupData.cs:
    47-86). The handles must be exactly the state's own panel groups, so an intake of another drawing
    is refused."""
    if not isinstance(panel_groups, list) or len(panel_groups) > MAX_PANEL_GROUPS:
        raise InverterDeviceError(f"panel groups must be a list of at most {MAX_PANEL_GROUPS}")
    out, total = [], 0
    for group in panel_groups:
        if not isinstance(group, dict) or not isinstance(group.get("handle"), str):
            raise InverterDeviceError("a panel group is {handle, outlines}")
        outlines = []
        for poly in group.get("outlines") or []:
            if not isinstance(poly, list):
                raise InverterDeviceError("an outline is a list of points")
            total += len(poly)
            if total > MAX_OUTLINE_VERTICES_TOTAL:
                raise InverterDeviceError(f"outlines hold more than {MAX_OUTLINE_VERTICES_TOTAL} vertices")
            outlines.append([_finite_xy(p, "outline vertex") for p in poly])
        out.append({"handle": group["handle"].upper().lstrip("0") or "0", "outlines": outlines})
    state_groups = {g.get("group") for g in state["geometry"]["panel_groups"]}
    if {g["handle"] for g in out} != state_groups:
        raise InverterDeviceError("the panel-group outlines are not this drawing's panel groups")
    return out


# ------------------------------------------------------------ geometry ports --

def _quadrant(u, v, pu, pv):
    """PointInPoly.GetQuadrant (PolylineExtensions.cs:363-368)."""
    return (0 if v > pv else 3) if u > pu else (1 if v > pv else 2)


def point_is_inside(x, y, polygon):
    """Polyline.PointIsInside (PolylineExtensions.cs:89-114) through PointInPoly.PolygonContains
    (:414-453): Weiler's quadrant-angle sum, the closing edge implied; inside only at +4 or -4."""
    n = len(polygon)
    if n == 0:
        return False
    quad = _quadrant(polygon[0][0], polygon[0][1], x, y)
    total = 0
    for i in range(n):
        u, v = polygon[i]
        nu, nv = polygon[i + 1 if i + 1 < n else 0]
        next_quad = _quadrant(nu, nv, x, y)
        delta = next_quad - quad
        if delta == 3:           # AdjustDelta (:382-398)
            delta = -1
        elif delta == -3:
            delta = 1
        elif delta in (2, -2):
            if nu - ((nv - y) * ((u - nu) / (v - nv))) > x:
                delta = -delta
        total += delta
        quad = next_quad
    return total == 4 or total == -4


def move_out_of_outlines(x, y, panel_groups):
    """BranchCmd.cs:13196-13251: for every outline of every group in order, a point inside it moves to
    the nearest point of its boundary (each closed segment, parameter clamped to the segment) and then
    OUTLINE_CLEARANCE further along the direction from the point to that boundary point. A zero
    direction throws in the plugin (GetNormal) and the catch keeps the point."""
    for group in panel_groups:
        for outline in group["outlines"]:
            if not point_is_inside(x, y, outline):
                continue
            best, best_d = (x, y), math.inf
            count = len(outline)
            for i in range(count):
                sx, sy = outline[i]
                ex, ey = outline[(i + 1) % count]
                vx, vy = ex - sx, ey - sy
                length = math.hypot(vx, vy)
                if length > MIN_SEGMENT:
                    t = ((x - sx) * vx + (y - sy) * vy) / (length * length)
                    t = max(0.0, min(1.0, t))
                    px, py = sx + vx * t, sy + vy * t
                    d = math.hypot(x - px, y - py)
                    if d < best_d:
                        best_d, best = d, (px, py)
            dx, dy = best[0] - x, best[1] - y
            norm = math.hypot(dx, dy)
            if norm == 0:
                continue
            x, y = best[0] + dx / norm * OUTLINE_CLEARANCE, best[1] + dy / norm * OUTLINE_CLEARANCE
    return x, y


def placement_extents(panel_groups, strings):
    """GetPanelGroupExtentsOrFallback (BranchCmd.cs:15255-15292): the union of every outline's
    extents, else the unassigned strings' extents (:11816-11821). (min x, min y, max x, max y)."""
    points = [p for g in panel_groups for outline in g["outlines"] for p in outline]
    if not points:
        points = [_finite_xy(v, "string vertex") for s in strings for v in s.get("vertices") or []]
    if not points:
        raise InverterDeviceError("no panel-group outline and no string geometry to place against")
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def grid_placement_points(count, extents):
    """BuildGridPlacementPoints (BranchCmd.cs:15927-15964) positions: columns from the extents' aspect,
    rows to hold the count, cell centres row by row from the lower left."""
    if count <= 0:
        return []
    min_x, min_y, max_x, max_y = extents
    if max_x <= min_x:
        max_x = min_x + 1.0
    if max_y <= min_y:
        max_y = min_y + 1.0
    width, height = max_x - min_x, max_y - min_y
    aspect = width / height if height > 0 else 1.0
    columns = max(1, math.ceil(math.sqrt(count * max(0.25, aspect))))
    rows = max(1, math.ceil(count / columns))
    while columns * rows < count:
        columns += 1
    dx, dy = width / columns, height / rows
    points = []
    for r in range(rows):
        for c in range(columns):
            if len(points) >= count:
                return points
            points.append((min_x + (c + 0.5) * dx, min_y + (r + 0.5) * dy))
    return points


# ---------------------------------------------------------------- devices --

def _device_row(state, *, is_l2, x, y, scale, placement, number, box_inputs):
    """The G35 device row of a block PlaceNewInverterBlock inserts (StringHomeRunCmd.cs:1478-1575):
    no stored number (the number lives on an attribute, inverter_evidence.py:22-30), rotation 0, the
    record's L2 flag, type key A and input count; `_number` keeps the number for later numbering."""
    return {"number": None, "role": "inverter" if is_l2 else "combiner",
            "position": st.coordinate(x, y), "scale": float(scale), "rotation": st.angle(0.0),
            "placement": placement, "hardware": None,
            "_pair": st.new_pair(state, "device"), "_number": number,
            "_detail": {"type_key": "A", "is_l2": is_l2, "box_input_count": box_inputs, "colour": None}}


def _symbol_scale(host, is_l2):
    """EquipmentSymbolScale (AcadCommandBase.cs:121-141): a central inverter (an L2 block) takes the
    central multiplier, every other block the combiner one; the host resolves the product."""
    key = "CentralInverterSymbolScale" if is_l2 else "CombinerSymbolScale"
    return float(_positive(host, key, "equipment symbol scale"))


def _unassigned_strings(state):
    """GetUnassignedStrings (BranchCmd.cs:16945-16982): String-layer strings whose circuit is "-",
    in drawing order, with their geometry."""
    geometry = {g.get("string"): g for g in state["geometry"]["strings"]}
    result = []
    for item in state["rows"]["string-assignment"]:
        detail = item.get("_detail") if isinstance(item.get("_detail"), dict) else {}
        if detail.get("circuit") == UNASSIGNED_CIRCUIT:
            result.append(geometry.get(item["string"], {}))
    return result


def inverter_add_all(state, panel_groups, host, form_values):
    """AddAllInverters on the Studio state: the L2 fleet by the plugin's deterministic grid fallback.
    `form_values`: {"inverters_count_differs_from_stringsizer": "Yes"|"No",
    "low_utilization_on_last_inverter": "Keep current"|"Apply recommended"|"Cancel"} (each read only
    when its dialog fires). Returns (new state, printed lines)."""
    host = _host(host)
    new = copy.deepcopy(state)
    lines = []
    use_l2 = _host_value(host, "UseL2Collectors", (bool,), "L1/L2 mode")
    installation = _setting(new, "InstallationDesign", "Ground")
    if str(installation).lower() != ROOF.lower() and use_l2 and host.get("UsePatternPlacement") is not False:
        raise InverterNotPortedError("AddAllInverters would run pattern device placement (not a Roof drawing)")
    if not use_l2:
        return _inverter_add_all_legacy(new, panel_groups, host, form_values, lines)
    # BranchCmd.cs:11648-11658: L1/L2 mode places central inverters.
    lines.append("L1/L2 mode detected: AddAllInverters will place central inverters.")
    strings_per_inverter = int(_positive(host, "StringsPerCentralInverter", "NumMppt x StringsPerMppt"))

    unassigned = _unassigned_strings(new)
    if not unassigned:
        lines.append("No unassigned strings found. AddAllInverters has nothing to place.")
        return new, lines
    _, l2_list = _levels(new)
    if l2_list:
        # BranchCmd.cs:11775-11813: the state carries no device number, so the strings a device holds
        # cannot be counted; an existing central inverter is taken as having spare capacity.
        raise InverterNotPortedError("AddAllInverters over existing central inverters (their fill is unknown)")

    total = len(unassigned)
    count = total // strings_per_inverter
    remainder = total % strings_per_inverter
    if remainder != 0:
        count += 1
    if count > MAX_INVERTERS:
        raise InverterDeviceError(f"{count} inverters exceed the bound of {MAX_INVERTERS}")

    # BranchCmd.cs:11961-11993 (the L2 sizing recommendation is null: no panel-group DC capacity).
    suggested = host.get("SuggestedInverterCount")
    if suggested is None or (type(suggested) is int and suggested > 0 and suggested != count):
        answer = (form_values or {}).get("inverters_count_differs_from_stringsizer")
        if answer not in ("Yes", "No"):
            raise InverterDeviceError("the count-differs dialog needs a Yes or No answer")
        if answer == "No":
            return new, lines
    # BranchCmd.cs:12022-12087: the legacy path's low-utilization dialog.
    if remainder > 0 and remainder / strings_per_inverter * 100.0 < LOW_FILL_PERCENT:
        answer = (form_values or {}).get("low_utilization_on_last_inverter")
        if answer == RESIZE_CANCEL:
            return new, lines
        if answer == RESIZE_APPLY:
            raise InverterNotPortedError("the resize dialog's recommended size is not ported")
        if answer != RESIZE_KEEP:
            raise InverterDeviceError("the low-utilization dialog needs Keep current, Apply recommended or Cancel")

    groups = validate_panel_groups(panel_groups, new)
    extents = placement_extents(groups, unassigned)
    # BranchCmd.cs:13083-13110: the cloud call is not ported; its fallback is.
    lines.append("Falling back to deterministic grid placement. Blocks will be marked auto-placed, not optimized.")
    scale = _symbol_scale(host, True)
    placed = []
    for gx, gy in grid_placement_points(count, extents):
        x, y = move_out_of_outlines(gx, gy, groups)
        number = _next_number(_levels(new)[1])
        device = _device_row(new, is_l2=True, x=x, y=y, scale=scale, placement=FALLBACK_MARKER,
                             number=number, box_inputs=0)
        new["rows"]["device"].append(device)
        placed.append((number, x, y))
    lines.append(f"--- Placed {len(placed)} inverter(s) ---")
    lines.extend(f"  inverter #{n} at ({x:.1f}, {y:.1f})" for n, x, y in placed)
    st.sort_rows(new)
    return new, lines


# --------------------------------------------------- inverter-add, legacy --

def polyline_midpoint(vertices):
    """GetCableMidpoint (BranchCmd.cs:16032-16059): the point halfway along the polyline, else its first
    vertex (straight segments: the string polylines carry no bulge)."""
    points = [_finite_xy(v, "string vertex") for v in vertices or []]
    if not points:
        raise InverterDeviceError("a string has no vertices")
    lengths = [math.dist(points[i], points[i + 1]) for i in range(len(points) - 1)]
    total = sum(lengths)
    if total <= 0:
        return points[0]
    target, walked = total * 0.5, 0.0
    for i, length in enumerate(lengths):
        if walked + length >= target and length > 0:
            t = (target - walked) / length
            a, b = points[i], points[i + 1]
            return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
        walked += length
    return points[-1]


def nearest_placement_point(points, loads, target, capacity):
    """FindNearestPlacementPoint (BranchCmd.cs:16061-16101) with preferCapacity: the nearest point below
    capacity, else the nearest point."""
    best, best_d, fallback, fallback_d = 0, math.inf, 0, math.inf
    capped = max(1, capacity)
    for i, (x, y) in enumerate(points):
        d = (target[0] - x) ** 2 + (target[1] - y) ** 2
        if d < fallback_d:
            fallback_d, fallback = d, i
        if loads[i] >= capped:
            continue
        if d < best_d:
            best_d, best = d, i
    return best if best_d < math.inf else fallback


def _inverter_add_all_legacy(new, panel_groups, host, form_values, lines):
    """AddAllInverters without L1/L2 collectors: string inverters (BranchCmd.cs:11634-11658 falls through),
    placed by the grid fallback the capture took (the cloud answered 503), each string assigned to its point's
    inverter (BuildGridPlacementPoints with assignStrings, :15966-15992; the placement loop :13155-13365).
    Returns (new state, printed lines)."""
    strings_module = _load_sibling("solar_inverter_strings")
    strings_per_inverter = int(_positive(host, "StringsPerCentralInverter", "NumMppt x StringsPerMppt"))
    geometry = {g.get("string"): g for g in new["geometry"]["strings"]}
    rows = [item for item in new["rows"]["string-assignment"]
            if (item.get("_detail") or {}).get("circuit") == UNASSIGNED_CIRCUIT]
    if not rows:
        lines.append("No unassigned strings found. AddAllInverters has nothing to place.")
        return new, lines
    l1_list, _ = _levels(new)
    if l1_list:
        raise InverterNotPortedError("AddAllInverters over existing string inverters (their fill is unknown)")
    total = len(rows)
    count, remainder = divmod(total, strings_per_inverter)
    if remainder:
        count += 1
    if count > MAX_INVERTERS:
        raise InverterDeviceError(f"{count} inverters exceed the bound of {MAX_INVERTERS}")
    suggested = host.get("SuggestedInverterCount")
    if suggested is None or (type(suggested) is int and suggested > 0 and suggested != count):
        answer = (form_values or {}).get("inverters_count_differs_from_stringsizer")
        if answer not in ("Yes", "No"):
            raise InverterDeviceError("the count-differs dialog needs a Yes or No answer")
        if answer == "No":
            return new, lines
    if remainder > 0 and remainder / strings_per_inverter * 100.0 < LOW_FILL_PERCENT:
        answer = (form_values or {}).get("low_utilization_on_last_inverter")
        if answer == RESIZE_CANCEL:
            return new, lines
        if answer == RESIZE_APPLY:
            raise InverterNotPortedError("the resize dialog's recommended size is not ported")
        if answer != RESIZE_KEEP:
            raise InverterDeviceError("the low-utilization dialog needs Keep current, Apply recommended or Cancel")

    groups = validate_panel_groups(panel_groups, new)
    extents = placement_extents(groups, [geometry.get(item["string"], {}) for item in rows])
    lines.append("Falling back to deterministic grid placement. Blocks will be marked auto-placed, not optimized.")
    points = grid_placement_points(count, extents)
    ordered = sorted(((polyline_midpoint(geometry.get(item["string"], {}).get("vertices")), item) for item in rows),
                     key=lambda pair: (pair[0][0], pair[0][1]))       # OrderBy X then Y (stable)
    buckets, loads = [[] for _ in points], [0] * len(points)
    for midpoint, item in ordered:
        nearest = nearest_placement_point(points, loads, midpoint, strings_per_inverter)
        buckets[nearest].append(item)
        loads[nearest] += 1

    scale = _symbol_scale(host, False)
    counter = host.get("SessionColorCounter", 1)
    if type(counter) is not int or counter < 0:
        raise InverterDeviceError("host input SessionColorCounter must be a non-negative integer")
    string_number = 1 + max((int((item.get("_detail") or {}).get("string_number") or 0)
                             for item in new["rows"]["string-assignment"]), default=0)   # :13141-13149
    placed = []
    for n, (gx, gy) in enumerate(points):
        x, y = move_out_of_outlines(gx, gy, groups)
        number = _next_number(_levels(new)[0])
        family = strings_module.type_colours(new, number)
        colour = family[counter % len(family)]
        counter += 1
        device = _device_row(new, is_l2=False, x=x, y=y, scale=scale, placement=FALLBACK_MARKER,
                             number=number, box_inputs=0)
        device["_detail"]["colour"] = colour
        new["rows"]["device"].append(device)
        placed.append((number, x, y))
        num_mppt = _setting(new, "NumMppt", 0)
        strings_per_mppt = _setting(new, "StringPerMppt", 0)
        for item in buckets[n]:                                         # AssignStringToInverter :16475-16570
            letter = strings_module.mppt_letter(string_number, num_mppt, strings_per_mppt)
            tag = strings_module.tag_text(number, string_number, num_mppt, strings_per_mppt)
            detail = dict(item.get("_detail") or {})
            detail.update({"circuit": tag, "num_mppt": num_mppt, "strings_per_mppt": strings_per_mppt,
                           "string_number": string_number, "mppt": letter,
                           "strings_on_inverter": num_mppt * strings_per_mppt,
                           "terminal_number": strings_module.TERMINAL_NUMBER, "color_counter": colour})
            item.update({"device": number, "input": ord(letter) - ord("a") + 1, "label": tag,
                         "colour": colour, "_detail": detail})
            string_number += 1
    lines.append(f"--- Placed {len(placed)} inverter(s) ---")
    lines.extend(f"  inverter #{n} at ({x:.1f}, {y:.1f})" for n, x, y in placed)
    st.sort_rows(new)
    return new, lines


def parse_point(text):
    """A typed point "x,y[,z]" (the point prompt's command-line answer)."""
    parts = text.split(",") if isinstance(text, str) else []
    if len(parts) not in (2, 3):
        raise InverterDeviceError(f"point answer {text!r} is not x,y[,z]")
    try:
        values = [float(p) for p in parts]
    except ValueError:
        raise InverterDeviceError(f"point answer {text!r} is not x,y[,z]") from None
    if not all(math.isfinite(v) for v in values):
        raise InverterDeviceError(f"point answer {text!r} is not finite")
    return values[0], values[1]


def snap_candidates(state):
    """The points the host's running object snaps offer: every cable and string vertex (endpoint),
    every device and LBD insertion point (insertion)."""
    points = []
    for cable in state["rows"]["cable"]:
        points.extend(st.point_of(v, "cable vertex") for v in cable["vertices"])
    for kind in ("device", "lbd"):
        points.extend(st.point_of(item["position"]) for item in state["rows"][kind])
    for string in state["geometry"]["strings"]:
        points.extend(_finite_xy(v, "string vertex") for v in string.get("vertices") or [])
    return points


def snap_point(x, y, state, aperture):
    """A point typed through (command ...) under running object snaps: the nearest candidate within
    the aperture, else the point itself; the first of equals wins."""
    best, best_d = (x, y), math.inf
    for px, py in snap_candidates(state):
        d = math.hypot(px - x, py - y)
        if d <= aperture and d < best_d:
            best, best_d = (float(px), float(py)), d
    return best


def inverter_add(state, host, answers, form_values):
    """ADDINVERTER on the Studio state. `answers`: [number, point, assignment keyword] (G22);
    `form_values`: {"select_equipment_type": "Combiner box" | "String inverter" | "Central inverter"}.
    Returns (new state, printed lines)."""
    host = _host(host)
    new = copy.deepcopy(state)
    lines = []
    if not isinstance(answers, (list, tuple)) or len(answers) != 3 or not all(isinstance(a, str) for a in answers):
        raise InverterDeviceError("ADDINVERTER takes three answers: number, point, assignment mode")
    number_text, point_text, mode = answers
    use_l2 = _host_value(host, "UseL2Collectors", (bool,), "L1/L2 mode")
    if not use_l2:
        raise InverterNotPortedError("ADDINVERTER outside L1/L2 mode")
    choice = (form_values or {}).get("select_equipment_type")
    if choice not in EQUIPMENT_CHOICES:
        raise InverterDeviceError(f"select_equipment_type must be one of {sorted(EQUIPMENT_CHOICES)}")
    place_l2 = EQUIPMENT_CHOICES[choice]
    label = "inverter" if place_l2 else "combiner box"

    # StringHomeRunCmd.cs:1386-1424: the number prompt (positive, no zero).
    l1_list, l2_list = _levels(new)
    default = _next_number(l2_list if place_l2 else l1_list)
    if number_text == "":
        number = default
    elif re.fullmatch(r"[1-9][0-9]{0,5}", number_text.strip()):
        number = int(number_text.strip())
    else:
        raise InverterDeviceError(f"number answer {number_text!r} is not a positive integer")
    lines.append(f"Assign {label} number <{default}>: {number_text}")
    x, y = parse_point(point_text)
    lines.append(f"Click to specify {label} ({number}) insertion point: {point_text}")
    aperture = float(_positive(host, "OsnapApertureDrawingUnits", "running object snap aperture"))
    x, y = snap_point(x, y, new, aperture)

    box_inputs = 0 if place_l2 else int(_positive(host, "CombinerBoxConnections", "combiner inputs"))
    device = _device_row(new, is_l2=place_l2, x=x, y=y, scale=_symbol_scale(host, place_l2),
                         placement=None, number=number, box_inputs=box_inputs)
    new["rows"]["device"].append(device)
    if not place_l2:
        # BranchCmd.cs:13373-13440: strings already tagged with this number are assigned at once.
        if any(item.get("device") == number for item in new["rows"]["string-assignment"]):
            raise InverterNotPortedError("strings pre-assigned to the new number would be assigned")
        if mode == ASSIGN_LATER:
            lines.append(f"String assignment deferred. Use AssignStrings to assign strings to this {label} later.")
        elif mode in ("", ASSIGN_MANUAL):
            raise InverterNotPortedError("manual string selection")
        else:
            raise InverterDeviceError(f"assignment mode {mode!r} is not Manual or AddLater")
    st.sort_rows(new)
    return new, lines


def inverter_balance(state, host, form_values):
    """INVBALANCE: the Inverter Manager. `form_values` carries the tab, the action and the closing
    button. Auto-Balance All analyses the L1 list only; with no L1 block there is no summary and no
    swap, and nothing is written. Returns (new state, printed lines: the manager prints none)."""
    _host(host)
    new = copy.deepcopy(state)
    action = (form_values or {}).get("branch_inverter_manager_action")
    l1_list, _ = _levels(new)
    if action == "Auto-Balance All" and l1_list:
        raise InverterNotPortedError("the MPPT balance analyzer over L1 blocks")
    return new, []


def _catalog():
    """InverterCatalog.Specs (LeafSolarDesign.Core/InverterCatalog.cs:58-124): manufacturer, series,
    model, aliases, rated AC."""
    return (
        ("TMEIC", "NINJA", "NINJA-840", ("TMEIC 840", "TMEIC-840", "TMEIC_NINJA_840"), 840.0),
        ("TMEIC", "NINJA", "NINJA-5.05", ("TMEIC NINJA 5.05", "TMEIC_NINJA_5_05", "TMEIC_NINJA_505",
                                          "NINJA 5.05", "NINJA-5.05", "NINJA505", "TMEIC 5.05", "TMEIC 5050"),
         5050.0),
        ("TMEIC", "NINJA", "NINJA-4.20", ("TMEIC NINJA 4.20", "TMEIC_NINJA_4_20", "TMEIC_NINJA_420",
                                          "NINJA 4.20", "NINJA-4.20", "NINJA420", "TMEIC 4.20", "TMEIC 4200"),
         4200.0),
    )


def _normalize_name(value):
    return "".join(ch.upper() for ch in (value or "") if ch.isalnum())


def _display_name(manufacturer, series, model):
    """InverterSpec.DisplayName (InverterCatalog.cs:37-53)."""
    if series and model and model.upper().startswith(series.upper()):
        series = ""
    return " ".join(part for part in (manufacturer, series, model) if part and part.strip())


def catalog_find(name):
    """InverterCatalog.FindByName (InverterCatalog.cs:136-156): (display name, rated AC) or None."""
    if not isinstance(name, str) or not name.strip():
        return None
    normalized = _normalize_name(name)
    for manufacturer, series, model, aliases, rated in _catalog():
        display = _display_name(manufacturer, series, model)
        if _normalize_name(display) == normalized or _normalize_name(model) == normalized or \
                _normalize_name(model) in normalized or \
                any(_normalize_name(a) and (normalized == _normalize_name(a) or _normalize_name(a) in normalized)
                    for a in aliases):
            return display, rated
    return None


def resolve_hardware(name, score):
    """ResolveHardwareFromCatalogName (FixedL2InverterAdoptionCmd.cs:524-571) past its database lookup:
    the catalog's display name and rated AC, or None."""
    if not isinstance(name, str) or not name.strip():
        return None
    found = catalog_find(name.strip())
    if found is None or found[1] <= 0:
        return None
    return {"model": found[0], "ac_kw": found[1], "match_score": score}


def _ac_text_value(value):
    """hardware.AcKw.ToString("0.###") read back as a number (FixedL2InverterMetadata.cs:103-104)."""
    return float(Decimal(repr(float(value))).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))


def adopt_l2_inverters(state, host, form_values):
    """LEAFADOPTL2INVERTERS on the Studio state. `form_values`: {"select_l2_inverter_hardware_mapping":
    "default", "select_l2_inverter_hardware": "Adopt" | "Cancel"}. Every device is adopted as a fixed
    L2 inverter with the configured hardware; the command's drawing-properties save appends the
    default cable catalog once (the G27 save finding). Returns (new state, printed lines)."""
    host = _host(host)
    new = copy.deepcopy(state)
    form_values = form_values or {}
    candidates = sorted(new["rows"]["device"],
                        key=lambda d: (st.point_of(d["position"])[0], st.point_of(d["position"])[1]))
    if not candidates:
        return new, ["0 fixed L2 inverter block(s) adopted."]
    if form_values.get("select_l2_inverter_hardware_mapping") != "default":
        raise InverterNotPortedError("a hardware mapping other than the dialog's default")
    # :179-181, :409-426: the configured L2 selection at score 60; :486-499: the dialog's row.
    configured = host.get("L2InverterSelection")
    current = resolve_hardware(configured, 60) if isinstance(configured, str) else None
    selected = current["model"] if current else (configured.strip() if isinstance(configured, str) else "")
    if form_values.get("select_l2_inverter_hardware") != "Adopt" or not selected:
        return new, ["Fixed L2 inverter adoption was cancelled."]
    # :1123-1136: the confirmed name is not in the (empty) fuzzy list, so its score is 100.
    hardware = resolve_hardware(selected, 100)
    if hardware is None:
        return new, ["Fixed L2 inverter adoption was cancelled."]

    used = {n for n in (_number(c) for c in candidates) if n is not None}
    next_number = 1
    for device in candidates:
        number = _number(device)
        if number is None or (number in used and sum(1 for c in candidates if _number(c) == number) > 1):
            while next_number in used:
                next_number += 1
            number = next_number
            used.add(number)
        detail = dict(device.get("_detail") or {})
        detail.update({"is_l2": True, "box_input_count": 0})
        detail.setdefault("type_key", "A")
        device.update({"role": "l2-inverter", "placement": FIXED_L2,
                       "hardware": {"model": hardware["model"].strip(), "ac_kw": _ac_text_value(hardware["ac_kw"]),
                                    "match_score": int(hardware["match_score"])},
                       "_number": number, "_detail": detail})
    st.save_drawing_properties(new)
    st.sort_rows(new)
    return new, [f"Adopted {len(candidates)} fixed L2 inverter block(s)."]


def skid_reconcile(state, host):
    """LEAFSKIDRECONCILE: L1ToL2Assignments reconciled against L1CollectorsPerL2 slots per L2.
    Changes nothing. Returns (the same state, the lines it prints)."""
    host = _host(host)
    new = copy.deepcopy(state)
    raw = new["setting"].get("L1ToL2Assignments") or {}
    if not isinstance(raw, dict):
        raise InverterDeviceError("L1ToL2Assignments must be an object")
    l1_to_l2 = {}
    for key, value in raw.items():
        if not re.fullmatch(r"-?[0-9]{1,9}", str(key)) or type(value) is not int:
            raise InverterDeviceError("L1ToL2Assignments maps integer L1 numbers to integer L2 numbers")
        l1_to_l2[int(key)] = value
    if not l1_to_l2:
        return new, ["LEAFSKIDRECONCILE: no L1->L2 combiner assignments found. "
                     "Run LEAFCOMBINERAUTO (with Use L2 Collectors enabled) first."]
    per_l2 = host.get("L1CollectorsPerL2")
    if type(per_l2) is not int:
        raise InverterDeviceError("host input L1CollectorsPerL2 is required")
    if per_l2 <= 0:
        per_l2 = DEFAULT_L1_PER_L2
    capacities = {}
    for l2 in l1_to_l2.values():
        capacities.setdefault(l2, per_l2)
    result = reconcile(list(l1_to_l2), l1_to_l2, capacities)
    lines = ["LEAFSKIDRECONCILE: " + ("RECONCILED" if result["reconciled"] else "MISMATCH") + ".",
             f"  L1 collectors: {result['expected']}, L2 input slots: {result['available']} "
             f"({len(capacities)} L2 x {per_l2} slots)."]
    lines.extend("  " + message for message in result["messages"])
    return new, lines


def reconcile(string_ids, string_to_combiner, capacities):
    """SkidReconciler.Reconcile (SkidReconciler.cs:101-211)."""
    available = sum(max(0, c) for c in capacities.values())
    load, assigned, unassigned = {}, 0, []
    for sid in string_ids:
        if sid not in string_to_combiner or string_to_combiner[sid] not in capacities:
            unassigned.append(sid)
            continue
        assigned += 1
        combiner = string_to_combiner[sid]
        load[combiner] = load.get(combiner, 0) + 1
    oversized = [(c, n, capacities.get(c, 0)) for c, n in sorted(load.items()) if n > capacities.get(c, 0)]
    unassigned.sort()
    mismatch = len(string_ids) != available
    reconciled = not unassigned and not oversized and not mismatch
    messages = []
    if reconciled:
        messages.append(f"Reconciled: {len(string_ids)} string(s) matched to {available} combiner slot(s); "
                        "zero unassigned, no combiner over capacity.")
    else:
        if mismatch:
            messages.append(f"Slot mismatch: {len(string_ids)} string(s) vs {available} combiner slot(s).")
        if unassigned:
            messages.append(f"{len(unassigned)} unassigned string(s): " + ", ".join(str(s) for s in unassigned))
        for c, n, cap in oversized:
            messages.append(f"Combiner {c} oversized: {n} string(s) assigned, capacity {cap} (over by {n - cap}).")
    return {"reconciled": reconciled, "expected": len(string_ids), "available": available,
            "assigned": assigned, "unassigned": unassigned, "oversized": oversized, "messages": messages}
