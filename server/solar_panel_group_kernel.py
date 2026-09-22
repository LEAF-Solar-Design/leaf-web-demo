"""Pure port of the plugin's PanelGroupCreate grouping and group matrix.

Standard library only, no I/O, inputs never mutated. The Branch2025 C# is the
authority; every step names the line it follows (paths relative to the plugin
repo root):

* ``panels_from_intake``: SelectAndGroupPanels (BranchCmd.cs:5825-6104) with
  Polyline.IsRectangular (PolylineExtensions.cs:48), PanelPolylineDefinition
  (AcadCommandBaseCore.cs:257), Rectangle ctor (Rectangle.cs:59) and
  Panel.SetRectangle (Panel.cs:76).
* ``group_panels``: groupOutlines (BranchCmdCore.cs:2297), the island loop
  (BranchCmd.cs:6165-6191), the angle sub-split (BranchCmd.cs:6214-6247,
  Panel.GetAngleKey Panel.cs:174, PanelGroup ctor PanelGroup.cs:25) and
  PrecomputeGroup (BranchCmdCore.cs:186-266) with BucketPanelsByDistance
  (:3228) and BuildHandleToIndexMap (:3265).

Island partition. The plugin grows every panel into a rectangle of
``ColumnDimension + 2*maxOffset`` by ``RowDimension + 2*maxOffset`` rotated by
the panel angle around its centre (Panel.CreatePolyline, Panel.cs:186), where
``maxOffset = (branch_max_offset / 2) * 1.05`` (BranchCmdCore.cs:2310), unites
them with AutoCAD region booleans and assigns each panel to the outline that
contains its centre. A panel's centre always lies inside its own grown
rectangle, so the united face containing it is exactly its connected component
of overlapping grown rectangles; that is what this kernel computes, with an
exact separating-axis overlap test for rotated rectangles (touching counts as
united, as BoolUnite merges shared edges).

Panel order. Every order-dependent step (angle-key insertion, first-match
bucketing, first-wins matrix cell) runs over panels in selection order. A
select-all selection returns model space in database order, which is ascending
handle, and the plugin's recorded member lists are in exactly that order, so
panels are processed in ascending handle (as a hex integer).

DELIBERATE DIVERGENCES from the plugin, both documented and tested:

1. Group order and names. The plugin orders islands by the AutoCAD handle of
   the outline polyline it creates for each (BranchCmd.cs:6214), which is not
   derivable from geometry: the same seven groups come out in different orders
   on two drawings. This kernel returns groups in ascending smallest member
   handle (hex integer) and names none of them.
2. An island lying wholly inside a hole of another island: the plugin's
   first-match PointIsInside over outline loops (BranchCmd.cs:6167) can hand it
   to the enclosing outline depending on that same non-derivable outline order.
   This kernel always keeps connected components apart.

INPUT-PRECISION ADAPTATION. The plugin reads full-precision DWG coordinates;
the Studio intake stores them rounded to 3 decimals. On slightly rotated
panels that rounding alone moves opposite edge lengths apart by about 0.001,
which the plugin's IsRectangular tolerance (0.001, PolylineExtensions.cs:52)
rejects although the plugin accepted the same polylines (92 of 2345 on
rooftop_demo). The rectangle test therefore widens each of its tolerances by
the worst error that quantization of half a unit in the third decimal per
coordinate can introduce (edge-length equality and the parallel and right
angle checks); every other plugin rule is unchanged.

Handles are returned in the intake's own spelling (the plugin lowercases
``mHandle`` internally, Panel.cs:78, and uppercases it for the member list,
BranchCmdCore.cs:298).
"""
from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable, Mapping, Sequence

TWO_PI = math.pi * 2
ROOF = "Roof"
GROUND = "Ground"

# Panel.CreatePolyline grows each side by 2*maxOffset; maxOffset is 5% more
# than half the branch max offset (BranchCmdCore.cs:2308-2310).
GROW_FACTOR = 1.05
# DoubleExtensions.vClose default tolerance (DoubleExtensions.cs:17).
VCLOSE_DEFAULT_TOL = 0.001
# AutoCAD Tolerance(0.001, 0.001) used by IsRectangular (PolylineExtensions.cs:52).
_GEOM_TOL = 0.001
# DedupedLineSegments vertex tolerance (PolylineExtensions.cs:144).
_VERTEX_TOL = 0.01
# Input-precision adaptation (module docstring): the intake rounds every
# coordinate to 3 decimals, so each coordinate is off by at most half a unit in
# the third decimal and each point by at most that times sqrt(2).
INTAKE_COORD_QUANTUM = 0.0005
_INTAKE_POINT_ERR = INTAKE_COORD_QUANTUM * math.sqrt(2)
# A segment length moves by at most 2 * _INTAKE_POINT_ERR, so the difference of
# two lengths by at most 4 * _INTAKE_POINT_ERR on top of the plugin's 0.001.
_INTAKE_LENGTH_TOL = _GEOM_TOL + 4 * _INTAKE_POINT_ERR
# Separating-axis slack: rectangles closer than this are treated as touching.
_TOUCH_EPS = 1e-9


class PanelGroupKernelError(ValueError):
    """Malformed kernel input; raised instead of guessing (fails closed)."""


# ----------------------------------------------------------------- primitives


def v_close(d1: float, d2: float, tol: float = VCLOSE_DEFAULT_TOL) -> bool:
    """DoubleExtensions.vClose: strict open interval d2 - tol < d1 < d2 + tol."""
    return (d1 < (d2 + tol)) and (d1 > (d2 - tol))


def r_to_d(rad: float) -> float:
    """DoubleExtensions.RtoD with its snaps to 360/270/180/90/0 degrees."""
    deg = rad * (180.0 / math.pi)
    for snap in (360.0, 270.0, 180.0, 90.0, 0.0):
        if v_close(deg, snap):
            return snap
    return deg


def angle_rationalise(ang: float) -> float:
    """DoubleExtensions.AngleRationalise: fold into [0, 2pi] (2pi itself stays)."""
    while ang > TWO_PI:
        ang -= TWO_PI
    while ang < 0.0:
        ang += TWO_PI
    return ang


def vector_angle(dx: float, dy: float) -> float:
    """AutoCAD Vector2d.Angle: counter-clockwise from +X in [0, 2pi)."""
    a = math.atan2(dy, dx)
    if a < 0.0:
        a += TWO_PI
    return a


def _csharp_format_0_0(value: float) -> str:
    """.NET ``{value:0.0}``: exact binary value, midpoint away from zero."""
    q = Decimal(value).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    text = format(q, "f")
    return "0.0" if text == "-0.0" else text


def angle_key(angle: float) -> str:
    """Panel.GetAngleKey (Panel.cs:174-182): degrees folded to [0, 180), one decimal."""
    deg = math.fmod(r_to_d(angle), 360.0)
    if deg < 0:
        deg += 360.0
    if deg >= 180.0:
        deg -= 180.0
    return _csharp_format_0_0(deg)


def group_row_angle(angle: float) -> float:
    """PanelGroup ctor (PanelGroup.cs:25-37): rationalise, then collapse to [0, pi)."""
    normalized = angle_rationalise(angle)
    if normalized >= math.pi:
        normalized -= math.pi
    return normalized


def grow_offset(branch_max_offset: float) -> float:
    """BranchCmdCore.cs:2310 maxOffset = (BranchMaxOffset / 2) * 1.05."""
    return (branch_max_offset / 2) * GROW_FACTOR


def find_side(c: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> int:
    """PointExtensions.FindSide (PointExtensions.cs:109-156), ported literally."""
    if v_close(b[0], a[0]):
        if c[0] < b[0]:
            return 1 if b[1] > a[1] else -1
        if c[0] > b[0]:
            return -1 if b[1] > a[1] else 1
        return 0
    if v_close(b[1], a[1]):
        if c[1] < b[1]:
            return -1 if b[0] > a[0] else 1
        if c[1] > b[1]:
            return 1 if b[0] > a[0] else -1
        return 0
    slope = (b[1] - a[1]) / (b[0] - a[0])
    y_intercept = a[1] - a[0] * slope
    c_solution = (slope * c[0]) + y_intercept
    if slope != 0:
        if c[1] > c_solution:
            return 1 if b[0] > a[0] else -1
        if c[1] < c_solution:
            return -1 if b[0] > a[0] else 1
        return 0
    return 0


def segment_distance(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    """LineSegment2d.GetDistanceTo(point): distance to the closed segment ab."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    len_sq = dx * dx + dy * dy
    if len_sq == 0.0:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / len_sq
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def _polar(pt: tuple[float, float], angle: float, dist: float) -> tuple[float, float]:
    return (pt[0] + dist * math.cos(angle), pt[1] + dist * math.sin(angle))


def bucket_by_distance(
    items: Sequence[Any], distances: Sequence[float], tolerance: float
) -> tuple[dict[float, list[Any]], list[float]]:
    """BucketPanelsByDistance (BranchCmdCore.cs:3228-3263).

    Linear scan in input order; each distance joins the FIRST existing bucket
    whose key (the raw distance of that bucket's first member) is vClose, so
    the result depends on input order. Returns (buckets, ascending keys).
    """
    buckets: dict[float, list[Any]] = {}
    for item, dist in zip(items, distances):
        for key, members in buckets.items():
            if v_close(dist, key, tolerance):
                members.append(item)
                break
        else:
            buckets[dist] = [item]
    return buckets, sorted(buckets)


# --------------------------------------------------------------------- panels


def _is_parallel(u: tuple[float, float], v: tuple[float, float]) -> bool:
    lu, lv = math.hypot(*u), math.hypot(*v)
    if lu == 0.0 or lv == 0.0:
        return False
    return abs(u[0] * v[1] - u[1] * v[0]) / (lu * lv) <= _GEOM_TOL


def _deduped_segments(pts: list[tuple[float, float]], closed: bool):
    """Polyline.DedupedLineSegments (PolylineExtensions.cs:144-194)."""
    if len(pts) < 3:
        return None
    unique = [pts[0]]
    for pt in pts[1:]:
        prev = unique[-1]
        if math.hypot(pt[0] - prev[0], pt[1] - prev[1]) > _VERTEX_TOL:
            unique.append(pt)
    if len(unique) > 1 and math.hypot(unique[-1][0] - unique[0][0], unique[-1][1] - unique[0][1]) <= _VERTEX_TOL:
        unique.pop()
    if len(unique) < 3:
        return None
    segs = [(unique[i], unique[i + 1]) for i in range(len(unique) - 1)]
    ends_same = v_close(pts[0][0], pts[-1][0], _VERTEX_TOL) and v_close(pts[0][1], pts[-1][1], _VERTEX_TOL)
    if closed or ends_same:
        segs.append((unique[-1], unique[0]))
    # MergeCollinearSegments (PolylineExtensions.cs:201-239).
    if len(segs) <= 2:
        return segs
    merged = []
    current = segs[0]
    for nxt in segs[1:]:
        cur_v = (current[1][0] - current[0][0], current[1][1] - current[0][1])
        nxt_v = (nxt[1][0] - nxt[0][0], nxt[1][1] - nxt[0][1])
        if _is_parallel(cur_v, nxt_v) and math.hypot(current[1][0] - nxt[0][0], current[1][1] - nxt[0][1]) < 0.01:
            current = (current[0], nxt[1])
        else:
            merged.append(current)
            current = nxt
    merged.append(current)
    if len(merged) > 1:
        last, first = merged[-1], merged[0]
        last_v = (last[1][0] - last[0][0], last[1][1] - last[0][1])
        first_v = (first[1][0] - first[0][0], first[1][1] - first[0][1])
        if _is_parallel(last_v, first_v) and math.hypot(last[1][0] - first[0][0], last[1][1] - first[0][1]) < 0.01:
            merged[0] = (last[0], first[1])
            merged.pop()
    return merged


def _seg_len(seg) -> float:
    return math.hypot(seg[1][0] - seg[0][0], seg[1][1] - seg[0][1])


def _seg_vec(seg) -> tuple[float, float]:
    return (seg[1][0] - seg[0][0], seg[1][1] - seg[0][1])


def _quantized_direction_tol(u: tuple[float, float], v: tuple[float, float]) -> float:
    """_GEOM_TOL widened by the worst direction error intake quantization can add.

    Each endpoint moves at most _INTAKE_POINT_ERR, so a segment's direction
    turns by at most 2 * _INTAKE_POINT_ERR / length radians; the normalised
    cross or dot product of two segments moves by at most the sum of both.
    """
    lu, lv = math.hypot(*u), math.hypot(*v)
    if lu == 0.0 or lv == 0.0:
        return _GEOM_TOL
    return _GEOM_TOL + 2 * _INTAKE_POINT_ERR * (1 / lu + 1 / lv)


def _is_rectangular(segs) -> bool:
    """Polyline.IsRectangular / PanelPolylineDefinition checks (PolylineExtensions.cs:48-84).

    The plugin's tolerances (0.001) are widened by the intake's coordinate
    quantization (module docstring, input-precision adaptation); every
    comparison is otherwise the plugin's.
    """
    if segs is None or len(segs) != 4:
        return False
    s1, s2, s3, s4 = segs
    if not v_close(_seg_len(s1), _seg_len(s3), _INTAKE_LENGTH_TOL) or not v_close(
        _seg_len(s2), _seg_len(s4), _INTAKE_LENGTH_TOL
    ):
        return False
    for u, v, test in (
        (_seg_vec(s1), _seg_vec(s3), "parallel"),
        (_seg_vec(s2), _seg_vec(s4), "parallel"),
        (_seg_vec(s1), _seg_vec(s2), "perpendicular"),
        (_seg_vec(s3), _seg_vec(s4), "perpendicular"),
    ):
        lu, lv = math.hypot(*u), math.hypot(*v)
        if lu == 0.0 or lv == 0.0:
            return False
        if test == "parallel":
            measure = abs(u[0] * v[1] - u[1] * v[0]) / (lu * lv)
        else:
            measure = abs(u[0] * v[0] + u[1] * v[1]) / (lu * lv)
        if measure > _quantized_direction_tol(u, v):
            return False
    return True


def _layer_matches(layer: str, contains: str) -> bool:
    # Selection filter "*{PanelLayerContains}*" (BranchCmd.cs:5837); AutoCAD
    # layer wildcards compare case-insensitively.
    return contains.casefold() in layer.casefold()


def panel_from_polyline(poly: Mapping[str, Any], installation_design: str = ROOF) -> dict | None:
    """One panel from one intake polyline, or None where the plugin rejects it."""
    raw = poly.get("pts")
    handle = poly.get("handle")
    if not isinstance(raw, list) or not isinstance(handle, str) or not handle:
        return None
    try:
        pts = [(float(p[0]), float(p[1])) for p in raw]
    except (TypeError, ValueError, IndexError):
        return None
    if not all(math.isfinite(c) for pt in pts for c in pt):
        return None
    closed = bool(poly.get("closed"))
    ends_same = len(pts) >= 3 and v_close(pts[0][0], pts[-1][0]) and v_close(pts[0][1], pts[-1][1])
    # Rectangle.TryCreateFromPolyline (Rectangle.cs:29): closed loop and rectangular.
    if not (closed or ends_same):
        return None
    deduped = _deduped_segments(pts, closed)
    if not _is_rectangular(deduped):
        return None
    # Rectangle ctor (Rectangle.cs:62-81) reads the RAW LineSegments.
    raw_segs = [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    if closed:
        raw_segs.append((pts[-1], pts[0]))
    if len(raw_segs) < 3:
        return None
    side1, side2 = _seg_len(raw_segs[0]), _seg_len(raw_segs[1])
    if side1 >= side2:
        rotation = vector_angle(*_seg_vec(raw_segs[0]))
        a, b = raw_segs[0][0], raw_segs[1][1]
    else:
        rotation = vector_angle(*_seg_vec(raw_segs[1]))
        a, b = raw_segs[1][0], raw_segs[2][1]
    centre = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    # Panel.SetRectangle (Panel.cs:85-96).
    angle = rotation + (math.pi / 2) if installation_design == GROUND else rotation
    angle = angle_rationalise(angle)
    # PanelPolylineDefinition (AcadCommandBaseCore.cs:329-330): width = side1,
    # height = side2 of the deduped segments; PanelDef.ColumnDimension/RowDimension
    # (PanelDef.cs:41-66) take max/min for Roof, min/max otherwise.
    d1, d2 = _seg_len(deduped[0]), _seg_len(deduped[1])
    if installation_design == ROOF:
        column_dim, row_dim = max(d1, d2), min(d1, d2)
    else:
        column_dim, row_dim = min(d1, d2), max(d1, d2)
    return {
        "handle": handle,
        "centre": centre,
        "angle": angle,
        "column_dim": column_dim,
        "row_dim": row_dim,
    }


def panels_from_intake(
    intake: Mapping[str, Any], *, layer_contains: str = "Panel", installation_design: str = ROOF
) -> list[dict]:
    """Every panel the plugin's select-all would take from a Studio intake.

    One panel per closed rectangular polyline whose layer contains
    ``layer_contains``; order is the intake's. Duplicate handles fail closed.
    """
    if not isinstance(intake, Mapping):
        raise PanelGroupKernelError("intake must be a mapping")
    polylines = intake.get("polylines") or []
    if not isinstance(polylines, list):
        raise PanelGroupKernelError("intake.polylines must be a list")
    panels: list[dict] = []
    seen: set[str] = set()
    for poly in polylines:
        if not isinstance(poly, Mapping):
            continue
        layer = poly.get("layer")
        if not isinstance(layer, str) or not _layer_matches(layer, layer_contains):
            continue
        panel = panel_from_polyline(poly, installation_design)
        if panel is None:
            continue
        key = panel["handle"].upper()
        if key in seen:
            raise PanelGroupKernelError(f"duplicate panel handle {panel['handle']}")
        seen.add(key)
        panels.append(panel)
    return panels


# -------------------------------------------------------------------- islands


def handle_sort_key(handle: str) -> int:
    try:
        return int(handle, 16)
    except (TypeError, ValueError) as exc:
        raise PanelGroupKernelError(f"panel handle {handle!r} is not hexadecimal") from exc


def grown_corners(panel: Mapping[str, Any], max_offset: float) -> list[tuple[float, float]]:
    """Panel.CreatePolyline (Panel.cs:186-231): grown rectangle, rotated by mAngle."""
    x = panel["column_dim"] + (2 * max_offset)
    y = panel["row_dim"] + (2 * max_offset)
    cx, cy = panel["centre"]
    ca, sa = math.cos(panel["angle"]), math.sin(panel["angle"])
    out = []
    for lx, ly in ((-x / 2, -y / 2), (x / 2, -y / 2), (x / 2, y / 2), (-x / 2, y / 2)):
        out.append((cx + lx * ca - ly * sa, cy + lx * sa + ly * ca))
    return out


def _rects_overlap(ca: list[tuple[float, float]], cb: list[tuple[float, float]]) -> bool:
    """Separating-axis test for two convex quads; touching counts as overlap."""
    for corners in (ca, cb):
        for i in range(2):
            ex = corners[i + 1][0] - corners[i][0]
            ey = corners[i + 1][1] - corners[i][1]
            nx, ny = -ey, ex
            norm = math.hypot(nx, ny)
            if norm == 0.0:
                continue
            nx, ny = nx / norm, ny / norm
            pa = [p[0] * nx + p[1] * ny for p in ca]
            pb = [p[0] * nx + p[1] * ny for p in cb]
            if max(pa) < min(pb) - _TOUCH_EPS or max(pb) < min(pa) - _TOUCH_EPS:
                return False
    return True


def island_partition(panels: Sequence[Mapping[str, Any]], branch_max_offset: float) -> list[list[int]]:
    """Connected components of overlapping grown rectangles, as panel indices.

    Spatial hash on the bounding circle (radius per BuildGroupOutlineRegionInputs,
    BranchCmdCore.cs:2360-2368) keeps this near linear; each component lists its
    indices ascending.
    """
    max_offset = grow_offset(branch_max_offset)
    n = len(panels)
    corners = [grown_corners(p, max_offset) for p in panels]
    radii = []
    for p in panels:
        x = p["column_dim"] + (2 * max_offset)
        y = p["row_dim"] + (2 * max_offset)
        radii.append(math.sqrt((x * x) + (y * y)) / 2.0)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    if n:
        cell = 2 * max(radii) or 1.0
        grid: dict[tuple[int, int], list[int]] = {}
        for i, p in enumerate(panels):
            cx, cy = p["centre"]
            grid.setdefault((math.floor(cx / cell), math.floor(cy / cell)), []).append(i)
        for (gx, gy), members in grid.items():
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    other = grid.get((gx + dx, gy + dy))
                    if not other:
                        continue
                    for i in members:
                        ci = panels[i]["centre"]
                        for j in other:
                            if j <= i:
                                continue
                            cj = panels[j]["centre"]
                            if math.hypot(ci[0] - cj[0], ci[1] - cj[1]) > radii[i] + radii[j] + _TOUCH_EPS:
                                continue
                            ri, rj = find(i), find(j)
                            if ri != rj and _rects_overlap(corners[i], corners[j]):
                                parent[max(ri, rj)] = min(ri, rj)
    components: dict[int, list[int]] = {}
    for i in range(n):
        components.setdefault(find(i), []).append(i)
    return list(components.values())


# --------------------------------------------------------------------- matrix


def group_matrix(
    panels: Sequence[Mapping[str, Any]], row_angle: float, alignment_tolerance: float
) -> list[list[str | None]]:
    """PrecomputeGroup matrix (BranchCmdCore.cs:192-266); cells hold the handle or None."""
    xs = [p["centre"][0] for p in panels]
    ys = [p["centre"][1] for p in panels]
    ext_min, ext_max = (min(xs), min(ys)), (max(xs), max(ys))
    centre = ((ext_min[0] + ext_max[0]) / 2, (ext_min[1] + ext_max[1]) / 2)
    diagonal = math.hypot(ext_max[0] - ext_min[0], ext_max[1] - ext_min[1])
    x_axis = (_polar(centre, row_angle + math.pi, diagonal), _polar(centre, row_angle, diagonal))
    y_axis = (
        _polar(centre, row_angle - (math.pi / 2), diagonal),
        _polar(centre, row_angle + (math.pi / 2), diagonal),
    )
    dist_x = []
    dist_y = []
    for p in panels:
        c = p["centre"]
        dist_x.append(segment_distance(c, *x_axis) * find_side(c, *x_axis))
        dist_y.append(segment_distance(c, *y_axis) * find_side(c, *y_axis) * -1)
    idx = list(range(len(panels)))
    x_buckets, sorted_x = bucket_by_distance(idx, dist_x, alignment_tolerance)
    y_buckets, sorted_y = bucket_by_distance(idx, dist_y, alignment_tolerance)
    sorted_y.reverse()
    row_of = {i: r for r, key in enumerate(sorted_x) for i in x_buckets[key]}
    col_of = {i: c for c, key in enumerate(sorted_y) for i in y_buckets[key]}
    matrix: list[list[str | None]] = [[None] * len(sorted_y) for _ in sorted_x]
    for i, p in enumerate(panels):
        r, c = row_of[i], col_of[i]
        if matrix[r][c] is None:
            matrix[r][c] = p["handle"]
    return matrix


# --------------------------------------------------------------------- groups


def group_panels(
    panels: Iterable[Mapping[str, Any]],
    *,
    branch_max_offset: float,
    alignment_tolerance: float,
    installation_design: str = ROOF,
) -> list[dict]:
    """Partition panels into the plugin's groups, each with its matrix.

    Returns ``[{"members": [handle...], "angle_key", "row_angle", "matrix"}]``
    in ascending smallest member handle (see the module docstring for why the
    plugin's own group order is not reproduced). ``members`` are in processing
    (ascending handle) order.
    """
    for name, value in (("branch_max_offset", branch_max_offset), ("alignment_tolerance", alignment_tolerance)):
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
            raise PanelGroupKernelError(f"{name} must be a positive finite number")
    if installation_design not in (ROOF, GROUND):
        raise PanelGroupKernelError(f"installation_design must be {ROOF!r} or {GROUND!r}")
    ordered = sorted(panels, key=lambda p: handle_sort_key(p["handle"]))
    groups: list[dict] = []
    for island in island_partition(ordered, branch_max_offset):
        by_key: dict[str, dict] = {}
        for i in island:
            panel = ordered[i]
            key = angle_key(panel["angle"])
            if key not in by_key:
                by_key[key] = {"angle_key": key, "row_angle": group_row_angle(panel["angle"]), "panels": []}
            by_key[key]["panels"].append(panel)
        for entry in by_key.values():
            members = entry["panels"]
            groups.append({
                "members": [p["handle"] for p in members],
                "angle_key": entry["angle_key"],
                "row_angle": entry["row_angle"],
                "matrix": group_matrix(members, entry["row_angle"], alignment_tolerance),
            })
    groups.sort(key=lambda g: min(handle_sort_key(h) for h in g["members"]))
    return groups


def group_panels_by_zone(
    panels: Iterable[Mapping[str, Any]],
    zones: Sequence[Mapping[str, Any]] | None,
    *,
    branch_max_offset: float,
    alignment_tolerance: float,
    installation_design: str = ROOF,
) -> list[dict]:
    """PanelGroupCreateZoneAware (BranchCmdCore.BuildAndSaveGroupsForZone).

    The ordinary grouping runs ONCE PER ZONE over only that zone's panels, in the
    zones' own order, and every group carries the zone name it came from. With no
    zones the plugin falls back to plain PanelGroupCreate, so ``zones`` empty or
    None returns exactly ``group_panels(panels, ...)`` with ``zone`` None on each
    group. ``group_panels`` itself is untouched: within a zone the same island,
    angle-key and matrix rules apply, so a zone's groups still come back in
    ascending smallest member handle.

    ``zones`` is ``[{"name": str, "members": [handle, ...]}, ...]``. Handles match
    case-insensitively. A member that is not one of ``panels``, a duplicate zone
    name and a panel claimed by two zones all fail closed: zones partition, and a
    kernel that guessed here would silently group a panel twice. A zone whose
    members select nothing contributes no group.
    """
    ordered = list(panels)
    by_handle: dict[str, Mapping[str, Any]] = {}
    for panel in ordered:
        handle = panel["handle"]
        if not isinstance(handle, str) or not handle:
            raise PanelGroupKernelError("panel handle must be a nonempty string")
        key = handle.upper()
        if key in by_handle:
            raise PanelGroupKernelError(f"duplicate panel handle {handle}")
        by_handle[key] = panel
    settings = {"branch_max_offset": branch_max_offset, "alignment_tolerance": alignment_tolerance,
                "installation_design": installation_design}
    if not zones:
        return [{**group, "zone": None} for group in group_panels(ordered, **settings)]
    if isinstance(zones, (str, bytes, Mapping)):
        raise PanelGroupKernelError("zones must be a sequence of zone mappings")
    grouped: list[dict] = []
    names: set[str] = set()
    claimed: set[str] = set()
    for zone in zones:
        if not isinstance(zone, Mapping) or not {"name", "members"} <= set(zone):
            raise PanelGroupKernelError("a zone needs a name and a member list")
        name, members = zone["name"], zone["members"]
        if not isinstance(name, str) or not name.strip() or len(name) > 4096:
            raise PanelGroupKernelError("zone name must be a nonempty string")
        if name.casefold() in names:
            raise PanelGroupKernelError(f"duplicate zone name {name}")
        names.add(name.casefold())
        if not isinstance(members, list):
            raise PanelGroupKernelError("zone members must be a list of handles")
        selected = []
        for handle in members:
            if not isinstance(handle, str) or not handle:
                raise PanelGroupKernelError("zone member handle must be a nonempty string")
            key = handle.upper()
            panel = by_handle.get(key)
            if panel is None:
                raise PanelGroupKernelError(f"zone member {handle} is not one of the panels")
            if key in claimed:
                raise PanelGroupKernelError(f"panel {handle} belongs to more than one zone")
            claimed.add(key)
            selected.append(panel)
        grouped.extend({**group, "zone": name} for group in group_panels(selected, **settings))
    return grouped
