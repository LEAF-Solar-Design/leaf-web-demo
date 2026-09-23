"""Studio's ground shade engines, ported from the plugin (contract G23: b8, b9, b10, b11).

Literal ports, each function citing the plugin source it reproduces:
  LEAFSHADESIM       shade_sim              Pvcase/LeafShadeSimCommand.cs:485-875
    panel discovery  read_panel_centres     LeafShadeSimCommand.cs:2008-2161 (tracker polylines)
    clearance shift  bind_panel_datum       LeafShadeSimCommand.cs:2163-2233
    profile          select_profile         LeafShadeSimCommand.cs:2450-2524
    sun angles       make_sun_angle_grid    LeafSolarDesign.Core/Shading/ShadingEngine.cs:97-127
    CPU ray march    is_beam_blocked, run_shade
                                            ShadingEngine.cs:137-254 (the CPU fallback path)
    heatmap          heatmap_markers        LeafShadeSimCommand.cs:2313-2380,
                                            LeafSolarDesign.Core/ShadeLossGradient.cs:18-114
    exports          azal_matrix_text, sam_beam_text, per_panel_text
                                            LeafSolarDesign.Core/Shading/ShadingExports.cs:49-130
  LEAFSHADE          annual_shade           Terrain/ShadeCommand.cs:23-238
    tracker rows     tracker_rows_for_shade Terrain/TrackerRowReader.cs:118-194, :287-337, :563-730
    sun position     sun_position           Terrain/SunPositionCalculator.cs:43-136
    pair shade       shade_fraction         Terrain/ShadeCalculator.cs:35-103
    8760-hour table  shade_table_reference  Terrain/ShadeTableGenerator.cs:79-196 (literal)
                     shade_table            the same sums, bounded (see its docstring)
  LEAFSHADECOMPARE   shade_compare          Pvcase/LeafShadeCompareCommand.cs:24-83
  LEAFSHADEEXPLAIN   shade_explain          LeafShadeSimCommand.cs:79-251, :998-1050, :1344-1430

The plugin traces LEAFSHADESIM on a GPU when one is present; the CPU fallback is the reference
this port follows (ShadingEngine.Run). The GPU samples a raster snapshot of the same surface,
which the plugin itself notes may differ from the CPU trace (LeafShadeSimCommand.cs:219-220);
that difference is host-only (G23).

The drawing is neutral data: `entities` is model space in drawing order, each a dict:
  {"type": "LWPOLYLINE", "layer", "closed", "vertices": [(x, y), ...], "elevation"}
  {"type": "INSERT", "axis_start": (x, y), "axis_end": (x, y)}   a tracker-row block with its row axis
The shade surface is the terrain grid (an interpolator with rows, cols, elevations,
meters_per_unit and interpolate_z(x, y) in metres, as solar_ground_terrain.TerrainGridInterpolator).
Studio's ground state holds no DSM, vegetation masses or fence segments, so the plugin's composite
surface (LeafShadeSimCommand.cs:1601-1690) reduces to the terrain; they are not accepted here.

Every function is pure, bounded (MAX_* below) and fails closed: malformed input raises
ShadeInputError, never a partial result. Written files are returned as text exactly as the plugin
writes them (CRLF line ends; the LEAFSHADE table with its UTF-8 BOM as U+FEFF).
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
import math

TRACKERS_LAYER = "LEAF-TRACKERS"                 # LayerNames.LeafTrackers
HEATMAP_LAYER = "LEAF-SHADE-HEATMAP"             # LeafShadeSimCommand.HeatmapLayer
MARKER_HALF_SIZE = 2.0                           # DrawHeatmap, :2339
DEFAULT_TARGET_CLEARANCE_M = 1.5                 # ResolvePanelTargetClearanceMeters, :2223
BINDING_TOLERANCE_M = 0.25                       # BindPanelDatumToTerrain, :2193
CLEARANCE_SETTING = "TorqueTubeHeightM"          # DrawingPropertiesJson.cs:335, read at :2214
DEFAULT_LATITUDE = 37.0                          # ShadeCommand.cs:53
DEFAULT_LONGITUDE = 0.0                          # ShadeCommand.cs:54
DEFAULT_MODULE_HEIGHT_M = 2.0                    # ShadeCommand.cs:84-93
SIMULATION_YEAR = 2025                           # ShadeTableGenerator.Generate default, :84
HOURS_PER_YEAR = 8760
NEWLINE = "\r\n"                                 # StringBuilder.AppendLine on Windows
BOM = "﻿"                                   # File.WriteAllText(..., Encoding.UTF8), ShadeCommand.cs:136
DEG_TO_RAD = math.pi / 180.0                     # SunPositionCalculator.cs:30, ShadeCalculator.cs:17
RAD_TO_DEG = 180.0 / math.pi                     # SunPositionCalculator.cs:31

# The 20-bin loss gradient, ShadeLossGradient.cs:25-57.
BIN_UPPER_EDGES_PCT = (0.10, 0.25, 0.50, 0.75, 1.00, 1.50, 2.00, 2.50, 3.00, 4.00, 5.00,
                       6.00, 7.00, 8.00, 9.00, 10.00, 12.50, 15.00, 20.00)
BIN_COUNT = 20
GRADIENT_ANCHORS = ((0, 31, 142, 62), (5, 140, 200, 85), (10, 255, 220, 60),
                    (15, 240, 130, 40), (19, 200, 35, 35))

# Bounds: every loop below is linear in one of these.
MAX_ENTITIES = 1_000_000
MAX_VERTICES = 100_000
MAX_PANELS = 50_000
MAX_ANGLES = 5_000
MAX_RAY_STEPS = 100_000
MAX_PER_PANEL_LINES = 5_000_000
MAX_SHADE_ROWS = 5_000
MAX_ABS_COORDINATE = 1e12
# shade_table: a receiving row whose casters within the shadow length number more than this is
# summed caster by caster over its shadow half-plane (early exit at full shade) instead of by
# the near list plus the half-plane prefix sums. Either path computes the same sum.
NEAR_LIMIT = 48
_WINDOW_WIDEN = 1e-9                             # radians; the caster-by-caster path re-checks the sign


class ShadeInputError(ValueError):
    """A named refusal: nothing is computed or written."""


# -------------------------------------------------------------- validation --

def _finite(value, what):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ShadeInputError(f"{what} must be a finite number")
    value = float(value)
    if abs(value) > MAX_ABS_COORDINATE:
        raise ShadeInputError(f"{what} is out of range")
    return value


def _point2(value, what):
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        raise ShadeInputError(f"{what} must be an [x, y] point")
    return _finite(value[0], what), _finite(value[1], what)


def _meters_per_unit(value):
    mpu = _finite(value, "meters per unit")
    return mpu if mpu > 0.0 else 1.0


def _entities(entities):
    if not isinstance(entities, (list, tuple)):
        raise ShadeInputError("entities must be a list in drawing order")
    if len(entities) > MAX_ENTITIES:
        raise ShadeInputError(f"more than {MAX_ENTITIES} entities")
    for ent in entities:
        if not isinstance(ent, dict):
            raise ShadeInputError("an entity must be an object")
    return entities


def _vertices(ent):
    verts = ent.get("vertices")
    if not isinstance(verts, (list, tuple)) or len(verts) > MAX_VERTICES:
        raise ShadeInputError(f"a polyline carries a list of at most {MAX_VERTICES} vertices")
    return [_point2(v, "polyline vertex") for v in verts]


def _is_layer(ent, layer):
    name = ent.get("layer")
    return isinstance(name, str) and name.upper() == layer.upper()   # OrdinalIgnoreCase


# -------------------------------------------------------------- formatting --

def net_fixed(value, digits):
    """C# double.ToString("F<digits>", InvariantCulture) on .NET 8: the exact binary value
    rounded half to even at `digits` decimals, a negative that rounds to zero keeping its sign
    (the rule solar_ground_scene.net_fixed pins). Fails closed on non-finite input."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ShadeInputError("a formatted value must be finite")
    with localcontext() as ctx:
        ctx.prec = 64
        return format(Decimal(float(value)).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_EVEN), "f")


def net_general(value):
    """C# double.ToString(InvariantCulture) on .NET Core 3.0+ (shortest round-trip text), for
    the angle labels the exports print: integral values without a decimal point, "-0" for
    negative zero, otherwise Python's repr with .NET's exponent spelling. Fails closed on values
    whose .NET text switches to exponent form where repr does not (|v| >= 1e15)."""
    v = _finite(value, "printed value")
    if abs(v) >= 1e15:
        raise ShadeInputError("a printed value must be below 1e15")
    if v == 0.0:
        return "-0" if math.copysign(1.0, v) < 0 else "0"
    if v.is_integer():
        return str(int(v))
    text = repr(v)
    if "e" in text:
        mantissa, exponent = text.split("e")
        e = int(exponent)
        text = f"{mantissa}E{'+' if e >= 0 else '-'}{abs(e):02d}"
    return text


def printed(value, digits):
    """A value the plugin prints with F<digits>, as the number that text reads back as (G23:
    compared at the printed precision)."""
    return float(net_fixed(value, digits))


# ------------------------------------------------------------ sun geometry --

def sun_position(lat_deg, lon_deg, day_of_year, utc_hours):
    """SunPositionCalculator.Calculate, :43-136. Returns (azimuth_deg, elevation_deg,
    is_night); azimuth is 0 at night."""
    b = (2.0 * math.pi / 365.0) * (day_of_year - 1)                                  # :49
    eqt_min = 229.18 * (0.000075
                        + 0.001868 * math.cos(b)
                        - 0.032077 * math.sin(b)
                        - 0.014615 * math.cos(2.0 * b)
                        - 0.040890 * math.sin(2.0 * b))                              # :54-59
    decl = (0.006918
            - 0.399912 * math.cos(b)
            + 0.070257 * math.sin(b)
            - 0.006758 * math.cos(2.0 * b)
            + 0.000907 * math.sin(2.0 * b)
            - 0.002697 * math.cos(3.0 * b)
            + 0.001480 * math.sin(3.0 * b))                                          # :64-71
    solar_hours = utc_hours + lon_deg / 15.0 + eqt_min / 60.0                         # :79
    hour_angle = (solar_hours - 12.0) * 15.0 * DEG_TO_RAD                             # :82
    lat = lat_deg * DEG_TO_RAD                                                        # :87
    sin_elev = math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(hour_angle)
    sin_elev = max(-1.0, min(1.0, sin_elev))                                          # :94
    elev = math.asin(sin_elev)
    elev_deg = elev * RAD_TO_DEG
    if elev_deg <= 0.0:                                                               # :99-107
        return 0.0, elev_deg, True
    cos_az = (math.sin(decl) - math.sin(lat) * sin_elev) / (math.cos(elev) * math.cos(lat))
    cos_az = max(-1.0, min(1.0, cos_az))                                              # :120
    az = math.acos(cos_az) * RAD_TO_DEG
    if hour_angle > 0.0:                                                              # :127-128
        az = 360.0 - az
    return az, elev_deg, False


def clear_sky_weight(elev_deg, is_night):
    """ShadeTableGenerator.ClearSkyIrradianceWeight, :188-196."""
    if is_night or elev_deg <= 0.0:
        return 0.0
    weight = math.sin(elev_deg * math.pi / 180.0)
    return weight if weight > 0.0 else 0.0


def _year_hours(year):
    """(day of year, UTC hour) for each simulation hour: new DateTime(year,1,1).AddHours(h), :99-114."""
    start = datetime(year, 1, 1)
    out = []
    for h in range(HOURS_PER_YEAR):
        t = start + timedelta(hours=h)
        out.append((t.timetuple().tm_yday, t.hour + t.minute / 60.0 + t.second / 3600.0))
    return out


# ---------------------------------------------------------- tracker rows --

def tracker_rows_for_shade(entities, meters_per_unit=1.0):
    """TrackerRowReader.ReadTrackerRows, :118-194, for the entities Studio draws: a LEAF-TRACKERS
    LWPOLYLINE with at least four vertices (TryReadBranchPolyline, :342-352, axis from
    PolylineToTrackerRow, :304-318) and a tracker-row block carrying its axis (BuildBranchTrackerRow,
    :628-671), in drawing order. Returns [{"axis_start", "axis_end", "centre"}]; the pair shade
    reads only the axis centre (ShadeCalculator.cs:52-55)."""
    _meters_per_unit(meters_per_unit)
    rows = []
    for ent in _entities(entities):
        kind = ent.get("type")
        if kind == "LWPOLYLINE":
            if not _is_layer(ent, TRACKERS_LAYER):
                continue
            verts = _vertices(ent)
            if len(verts) < 4:
                continue
            (x0, y0), (x1, y1), (x2, y2), (x3, y3) = verts[:4]
            ax, ay = (x0 + x1) * 0.5, (y0 + y1) * 0.5                                 # :309-310
            bx, by = (x2 + x3) * 0.5, (y2 + y3) * 0.5                                 # :311-312
        elif kind == "INSERT":
            if ent.get("axis_start") is None or ent.get("axis_end") is None:
                continue                                                              # :631, no axis
            ax, ay = _point2(ent["axis_start"], "tracker axis start")
            bx, by = _point2(ent["axis_end"], "tracker axis end")
        else:
            continue
        rows.append({"axis_start": (ax, ay), "axis_end": (bx, by),
                     "centre": ((ax + bx) * 0.5, (ay + by) * 0.5)})                   # ShadeCalculator.cs:52-55
        if len(rows) > MAX_SHADE_ROWS:
            raise ShadeInputError(f"more than {MAX_SHADE_ROWS} tracker rows")
    return rows


def shade_fraction(cast_centre, recv_centre, azimuth_deg, elevation_deg, is_night, module_height):
    """ShadeCalculator.ComputeShadeFraction, :35-103, on the two rows' axis centres."""
    if is_night or elevation_deg <= 0.0:
        return 0.0
    if module_height <= 0.0:
        return 0.0
    dx = recv_centre[0] - cast_centre[0]
    dy = recv_centre[1] - cast_centre[1]
    dist = math.sqrt(dx * dx + dy * dy)
    if dist < 1e-9:
        return 0.0
    hx = dx / dist
    hy = dy / dist
    az = azimuth_deg * DEG_TO_RAD
    projection = -math.sin(az) * hx + -math.cos(az) * hy                              # :75-81
    if projection <= 0.0:
        return 0.0
    shadow_total = module_height / math.tan(elevation_deg * DEG_TO_RAD)               # :92-94
    return min(1.0, max(0.0, shadow_total * projection / dist))                       # :96-102


def _sun_hours(lat_deg, lon_deg, year):
    """The daylight hours ShadeTableGenerator.Generate integrates (:111-121), each with its
    clear-sky weight and sun direction."""
    out = []
    for day, hours in _year_hours(year):
        az, elev, night = sun_position(lat_deg, lon_deg, day, hours)
        w = clear_sky_weight(elev, night)
        if night or w <= 0.0:
            continue
        out.append((w, az, elev))
    return out


def _table_result(row_count, row_sums, total_ew, total_w, total_t, daylight):
    rows = []
    for r in range(row_count):
        ew, w, t, n = row_sums[r]
        rows.append({"row_id": "Row " + str(r),                                        # :157
                     "energy_weighted_pct": (ew / w) * 100.0 if w > 0.0 else 0.0,      # :160-162
                     "time_weighted_pct": (t / n) * 100.0 if n > 0 else 0.0})          # :163-165
    energy = (total_ew / total_w) * 100.0 if total_w > 0.0 else 0.0                   # :171-173
    time_pct = (total_t / (daylight * row_count)) * 100.0 if daylight > 0 and row_count > 0 else 0.0
    return {"rows": rows, "annual_energy_weighted_pct": energy, "annual_time_weighted_pct": time_pct,
            "daylight_hours": daylight}


def _check_table_inputs(rows, lat_deg, lon_deg, module_height):
    if not isinstance(rows, (list, tuple)) or len(rows) > MAX_SHADE_ROWS:
        raise ShadeInputError(f"rows must be a list of at most {MAX_SHADE_ROWS} tracker rows")
    centres = [_point2(r.get("centre") if isinstance(r, dict) else None, "row centre") for r in rows]
    module_height = _finite(module_height, "module height")
    if module_height <= 0.0:
        raise ShadeInputError("moduleHeightM must be positive.")                      # :87-88
    return centres, _finite(lat_deg, "latitude"), _finite(lon_deg, "longitude"), module_height


def shade_table_reference(rows, lat_deg, lon_deg, module_height, year=SIMULATION_YEAR):
    """ShadeTableGenerator.Generate, :79-186, literally: every hour, every receiving row, every
    casting row, the running fraction capped at 1 after each caster (:128-137). O(8760 n^2), for
    small layouts and as the oracle shade_table is tested against."""
    centres, lat_deg, lon_deg, module_height = _check_table_inputs(rows, lat_deg, lon_deg, module_height)
    n = len(centres)
    sums = [[0.0, 0.0, 0.0, 0] for _ in range(n)]
    total_ew = total_w = total_t = 0.0
    daylight = 0
    for w, az, elev in _sun_hours(lat_deg, lon_deg, year):
        daylight += 1
        for recv in range(n):
            frac = 0.0
            for cast in range(n):
                if cast == recv:
                    continue
                frac = min(1.0, frac + shade_fraction(centres[cast], centres[recv], az, elev, False,
                                                      module_height))
            s = sums[recv]
            s[0] += frac * w
            s[1] += w
            s[2] += frac
            s[3] += 1
            total_t += frac
            total_ew += frac * w
            total_w += w
    return _table_result(n, sums, total_ew, total_w, total_t, daylight)


def _half_plane_windows(phi, widen):
    """The open interval of caster directions theta = atan2(d) with cos(theta - phi) > 0, as one
    or two (low, high) ranges inside atan2's [-pi, pi], each widened by `widen`."""
    lo = phi - math.pi / 2.0 - widen
    hi = phi + math.pi / 2.0 + widen
    if lo < -math.pi:
        return ((lo + 2.0 * math.pi, math.inf), (-math.inf, hi))
    if hi > math.pi:
        return ((lo, math.inf), (-math.inf, hi - 2.0 * math.pi))
    return ((lo, hi),)


def shade_table(rows, lat_deg, lon_deg, module_height, year=SIMULATION_YEAR, near_limit=NEAR_LIMIT):
    """ShadeTableGenerator.Generate, :79-186, as the same sums in bounded time.

    For one sun position the plugin's pair fraction is f = min(1, H (s . d) / |d|^2) when s . d > 0
    (else 0), with d the receiving centre minus the casting centre, s the unit shadow direction
    and H = moduleHeight / tan(elevation) (ShadeCalculator.cs:57-102). A caster farther than H
    can never reach the cap (f <= H / |d| < 1), so its term is linear in s: H s . (d / |d|^2).
    Per receiving row the casters are sorted once by distance and once by direction; an hour
    sums the casters within H one by one and every farther caster in the shadow half-plane
    through prefix sums of d / |d|^2 over the direction order. When more than `near_limit`
    casters lie within H (a low sun), the row sums its half-plane casters one by one, stopping
    at full shade. The running cap of :136 equals min(1, total) because every term is
    non-negative, so both paths return the plugin's value up to the order of float additions
    (relative 1e-15). O(n^2 log n) to build, O(8760 n (log n + near_limit)) to sum; memory O(n)."""
    centres, lat_deg, lon_deg, module_height = _check_table_inputs(rows, lat_deg, lon_deg, module_height)
    if isinstance(near_limit, bool) or not isinstance(near_limit, int) or near_limit < 0:
        raise ShadeInputError("near_limit must be a non-negative integer")
    n = len(centres)
    hours = []
    for w, az, elev in _sun_hours(lat_deg, lon_deg, year):
        az_rad = az * DEG_TO_RAD
        sx, sy = -math.sin(az_rad), -math.cos(az_rad)                                 # ShadeCalculator.cs:75-76
        big_h = module_height / math.tan(elev * DEG_TO_RAD)                           # :92-94
        phi = math.atan2(sy, sx)
        hours.append((w, big_h, sx, sy, _half_plane_windows(phi, 0.0), _half_plane_windows(phi, _WINDOW_WIDEN)))
    xs = [c[0] for c in centres]
    ys = [c[1] for c in centres]
    sums = []
    total_ew = total_w = total_t = 0.0
    for r in range(n):
        rx, ry = xs[r], ys[r]
        casters = []
        for c in range(n):
            if c == r:
                continue                                                              # :130
            dx = rx - xs[c]
            dy = ry - ys[c]
            d2 = dx * dx + dy * dy
            if math.sqrt(d2) < 1e-9:
                continue                                                              # ShadeCalculator.cs:62-63
            casters.append((d2, dx, dy, c))
        by_dist = sorted(casters)[:near_limit + 1]
        near_d = [math.sqrt(t[0]) for t in by_dist]
        near_dx = [t[1] for t in by_dist]
        near_dy = [t[2] for t in by_dist]
        near_d2 = [t[0] for t in by_dist]
        by_angle = sorted((math.atan2(dy, dx), dx, dy, d2) for d2, dx, dy, _ in casters)
        theta = [t[0] for t in by_angle]
        ang_dx = [t[1] for t in by_angle]
        ang_dy = [t[2] for t in by_angle]
        ang_d2 = [t[3] for t in by_angle]
        px = [0.0]
        py = [0.0]
        for _, dx, dy, d2 in by_angle:
            px.append(px[-1] + dx / d2)
            py.append(py[-1] + dy / d2)
        ew = wsum = tsum = 0.0
        daylight = 0
        for w, big_h, sx, sy, windows, wide in hours:
            k = bisect_right(near_d, big_h)
            if k <= near_limit:
                acc = 0.0
                ux = uy = 0.0
                for i in range(k):
                    sd = sx * near_dx[i] + sy * near_dy[i]
                    if sd > 0.0:
                        d2 = near_d2[i]
                        f = big_h * sd / d2
                        acc += 1.0 if f > 1.0 else f
                        ux += near_dx[i] / d2
                        uy += near_dy[i] / d2
                if acc >= 1.0:
                    frac = 1.0
                else:
                    all_x = all_y = 0.0
                    for low, high in windows:
                        i0 = bisect_right(theta, low)
                        i1 = bisect_left(theta, high)
                        all_x += px[i1] - px[i0]
                        all_y += py[i1] - py[i0]
                    frac = acc + big_h * (sx * (all_x - ux) + sy * (all_y - uy))
                    frac = 1.0 if frac > 1.0 else (0.0 if frac < 0.0 else frac)
            else:
                acc = 0.0
                for low, high in wide:
                    i1 = bisect_left(theta, high)
                    for j in range(bisect_right(theta, low), i1):
                        sd = sx * ang_dx[j] + sy * ang_dy[j]
                        if sd <= 0.0:
                            continue
                        f = big_h * sd / ang_d2[j]
                        acc += 1.0 if f > 1.0 else f
                        if acc >= 1.0:
                            break
                    if acc >= 1.0:
                        break
                frac = 1.0 if acc >= 1.0 else acc
            ew += frac * w                                                            # :141
            wsum += w                                                                 # :142
            tsum += frac                                                              # :140
            daylight += 1
        sums.append((ew, wsum, tsum, daylight))
        total_ew += ew
        total_w += wsum
        total_t += tsum
    return _table_result(n, sums, total_ew, total_w, total_t, len(hours))


def shade_csv_text(table, lat_deg, lon_deg):
    """ShadeCommand.BuildCsv, :184-204, as File.WriteAllText writes it with Encoding.UTF8: a
    BOM, CRLF line ends. Row ids never carry a comma or quote, so EscapeCsv (:232-238) is the
    identity here; it is applied anyway."""
    def escape(value):
        if "," in value or '"' in value:
            return '"' + value.replace('"', '""') + '"'
        return value
    lines = ["LEAFSHADE Shade Analysis",
             "Site lat (deg)," + net_fixed(lat_deg, 4),
             "Site lon (deg)," + net_fixed(lon_deg, 4),
             "Clear-sky energy-weighted annual shading loss (%)," + net_fixed(table["annual_energy_weighted_pct"], 2),
             "Time-weighted annual shading loss (%)," + net_fixed(table["annual_time_weighted_pct"], 2),
             "",
             "Row,Clear-Sky Energy-Weighted Shade Loss (%),Time-Weighted Shade Loss (%)"]
    for row in table["rows"]:
        lines.append(escape(row["row_id"]) + "," + net_fixed(row["energy_weighted_pct"], 2) + ","
                     + net_fixed(row["time_weighted_pct"], 2))
    return BOM + "".join(line + NEWLINE for line in lines)


def annual_shade(entities, meters_per_unit=1.0, lat_deg=None, lon_deg=None, module_height_m=None,
                 engine=None):
    """LEAFSHADE, ShadeCommand.cs:23-168, without the file system. None takes a prompt's
    default: latitude 37 and longitude 0 (Studio's ground state carries no site location, so
    TryReadLatLon, :244-294, leaves the defaults), module height 2.0 m. `engine` is the table
    function (shade_table unless a caller passes shade_table_reference).

    Returns {"succeeded", "message", "row_count", "latitude_deg", "longitude_deg",
    "module_height_m", "table", "csv", "annual_loss_pct", "simulated_hours"}; simulated_hours is
    the hour count the completion line prints (ShadeCommand.cs:142-144)."""
    mpu = _meters_per_unit(meters_per_unit)
    rows = tracker_rows_for_shade(entities, mpu)
    if not rows:                                                                      # :43-47
        return {"succeeded": False, "row_count": 0, "table": None, "csv": None, "annual_loss_pct": None,
                "latitude_deg": None, "longitude_deg": None, "module_height_m": None, "simulated_hours": None,
                "message": "No LEAF-TRACKERS tracker rows found. Run LEAFTRACK or LEAFSAT first."}
    lat = DEFAULT_LATITUDE if lat_deg is None else _finite(lat_deg, "latitude")
    lat = max(-89.9, min(89.9, lat))                                                  # :67
    lon = DEFAULT_LONGITUDE if lon_deg is None else _finite(lon_deg, "longitude")
    height_m = DEFAULT_MODULE_HEIGHT_M if module_height_m is None else _finite(module_height_m, "module height")
    if height_m <= 0.0:
        raise ShadeInputError("module height must be positive")                       # :88-89 AllowNegative/AllowZero false
    height_du = height_m / mpu                                                        # :95
    table = (engine or shade_table)(rows, lat, lon, height_du)
    return {"succeeded": True, "row_count": len(rows), "latitude_deg": lat, "longitude_deg": lon,
            "module_height_m": height_m, "table": table, "csv": shade_csv_text(table, lat, lon),
            "annual_loss_pct": table["annual_energy_weighted_pct"],                  # AnnualShadeLoss, :181
            "simulated_hours": HOURS_PER_YEAR,                                         # :142-144
            "message": ("LEAFSHADE complete. Clear-sky energy-weighted annual shading loss: "
                        f"{net_fixed(table['annual_energy_weighted_pct'], 1)}% ({HOURS_PER_YEAR}-hour simulation)")}


# ---------------------------------------------------------- shade sim grid --

def make_sun_angle_grid(azimuth_step_deg=10.0, altitude_step_deg=10.0, min_altitude_deg=5.0,
                        max_altitude_deg=85.0, dense_low_altitude=True):
    """ShadingEngine.MakeSunAngleGrid, :97-127: [{"azimuth_deg", "altitude_deg", "index"}] in
    altitude-major order, both loops accumulating their step as the plugin's for loops do."""
    az_step = _finite(azimuth_step_deg, "azimuth step")
    alt_step = _finite(altitude_step_deg, "altitude step")
    lo = _finite(min_altitude_deg, "minimum altitude")
    hi = _finite(max_altitude_deg, "maximum altitude")
    if az_step <= 0.0 or alt_step <= 0.0:
        raise ShadeInputError("angle steps must be positive")
    angles = []

    def emit(alt):
        az = 0.0
        while az < 360.0 - 1e-9:
            if len(angles) >= MAX_ANGLES:
                raise ShadeInputError(f"more than {MAX_ANGLES} sun angles")
            angles.append({"azimuth_deg": az, "altitude_deg": alt, "index": len(angles)})
            az += az_step

    if dense_low_altitude:
        alt = 1.0
        while alt < lo and alt <= 9.0:                                                # :122
            emit(alt)
            alt += 1.0
    alt = lo
    while alt <= hi + 1e-9:                                                           # :125
        emit(alt)
        alt += alt_step
    return angles


def create_profile(name, angles, ray_step_m, max_ray_m, panel_count):
    """LeafShadeSimCommand.CreateProfile, :2505-2524."""
    ray_steps = math.ceil(max_ray_m / max(0.1, ray_step_m))
    samples = max(0, panel_count) * max(0, len(angles)) * max(0, ray_steps)
    return {"name": name, "angles": angles, "ray_step_m": ray_step_m, "max_ray_m": max_ray_m,
            "estimated_samples": samples}


def select_profile(panel_count):
    """LeafShadeSimCommand.SelectShadeSimulationProfile, :2450-2503."""
    if panel_count <= 300:
        return create_profile("full", make_sun_angle_grid(), 1.0, 400.0, panel_count)
    if panel_count <= 1500:
        return create_profile("balanced", make_sun_angle_grid(15.0, 10.0, 5.0, 85.0, False), 3.0, 400.0,
                              panel_count)
    if panel_count <= 5000:
        return create_profile("large-site", make_sun_angle_grid(20.0, 15.0, 5.0, 80.0, False), 5.0, 400.0,
                              panel_count)
    return create_profile("very-large-site", make_sun_angle_grid(30.0, 20.0, 10.0, 70.0, False), 10.0, 400.0,
                          panel_count)


# ------------------------------------------------------------ the surface --

class TerrainShadeSurface:
    """CompositeShadingSurface.InterpolateZ, :1644-1690, with no DSM, vegetation or fences:
    the terrain Z in drawing units, None off the grid. `z_cap` bounds every value it returns
    (the grid's highest node, since a bilinear value never exceeds its cell's corners, plus a
    margin for float rounding); a rising ray above it can meet nothing further along."""

    __slots__ = ("_dtm", "_mpu", "z_cap")

    def __init__(self, dtm, meters_per_unit):
        if dtm is None or not callable(getattr(dtm, "interpolate_z", None)):
            raise ShadeInputError("the shade surface needs a terrain grid")
        self._dtm = dtm
        self._mpu = _meters_per_unit(meters_per_unit)
        finite = [z for z in getattr(dtm, "elevations", ()) if isinstance(z, (int, float)) and math.isfinite(z)]
        if finite:
            top = max(finite) / self._mpu
            self.z_cap = top + 1e-9 * (1.0 + abs(top))
        else:
            self.z_cap = None

    def z(self, x, y):
        zm = self._dtm.interpolate_z(x, y)
        return None if zm is None else zm / self._mpu


def surface_snapshot(dtm):
    """TryBuildGpuSurfaceSnapshot, LeafShadeSimCommand.cs:939-996, sized only: the snapshot is
    the reference grid's nodes, rows x cols, each at its terrain Z (no DSM, vegetation or fences
    here, so every node has one). None where the plugin fails it: no grid, or under 2x2 (:945-954).
    Returns {"rows", "cols", "cells"}, the values LEAFSHADESIM prints at :584-586. O(1)."""
    if dtm is None:
        return None
    rows, cols = getattr(dtm, "rows", None), getattr(dtm, "cols", None)
    if isinstance(rows, bool) or isinstance(cols, bool) or not isinstance(rows, int) or not isinstance(cols, int):
        raise ShadeInputError("the shade surface grid needs integer rows and cols")
    if rows < 2 or cols < 2:
        return None
    return {"rows": rows, "cols": cols, "cells": rows * cols}


def is_beam_blocked(px, py, pz, azimuth_deg, altitude_deg, surface, step, max_ray):
    """ShadingEngine.IsBeamBlocked, :137-163. The march stops early, with the same answer, once
    the rising ray is above surface.z_cap: no later probe can exceed it."""
    if altitude_deg <= 0.0:
        return True                                                                   # :145
    az = azimuth_deg * math.pi / 180.0
    alt = altitude_deg * math.pi / 180.0
    dx = math.sin(az) * step
    dy = math.cos(az) * step
    dz = math.tan(alt) * step
    cap = surface.z_cap if dz > 0.0 else None
    zfn = surface.z
    d = step
    while d <= max_ray:
        k = d / step
        ray_z = pz + k * dz
        if cap is not None and ray_z > cap:
            return False
        s = zfn(px + k * dx, py + k * dy)
        if s is None:
            return False                                                              # :159
        if s > ray_z:
            return True                                                               # :160
        d += step
    return False


def _check_march(step, max_ray):
    step = _finite(step, "ray step")
    max_ray = _finite(max_ray, "max ray")
    if step <= 0.0 or max_ray <= 0.0:
        raise ShadeInputError("ray step and max ray must be positive")
    if max_ray / step > MAX_RAY_STEPS:
        raise ShadeInputError(f"a ray of more than {MAX_RAY_STEPS} steps")
    return step, max_ray


def run_shade(panels, angles, surface, step, max_ray):
    """ShadingEngine.Run, :170-254, serially. Returns {"shade": [bytearray per panel, 1 = blocked],
    "avg_by_angle", "weighted_per_panel", "angles"}; blocked counts are integers, so the plugin's
    parallel per-worker sums (:207-239) add to the same values."""
    step, max_ray = _check_march(step, max_ray)
    if len(panels) > MAX_PANELS or len(angles) > MAX_ANGLES:
        raise ShadeInputError("too many panels or sun angles")
    n_a = len(angles)
    alts = [a["altitude_deg"] for a in angles]
    azs = [a["azimuth_deg"] for a in angles]
    weights = []
    weight_sum = 0.0
    for alt in alts:                                                                  # :191-200
        w = math.sin(alt * math.pi / 180.0)
        if w < 0:
            w = 0.0
        weights.append(w)
        weight_sum += w
    counts = [0] * n_a
    shade = []
    weighted = []
    for p in panels:
        flags = bytearray(n_a)
        panel_weighted = 0.0
        for a in range(n_a):
            blocked = is_beam_blocked(p["x"], p["y"], p["z"], azs[a], alts[a], surface, step, max_ray)
            s = 1.0 if blocked else 0.0
            if blocked:
                flags[a] = 1
                counts[a] += 1
            panel_weighted += s * weights[a]
        shade.append(flags)
        weighted.append(panel_weighted / weight_sum if weight_sum > 0.0 else 0.0)     # :227-229
    n_p = len(panels)
    avg = [c / n_p if n_p > 0 else 0.0 for c in counts]                               # :241-245
    return {"shade": shade, "avg_by_angle": avg, "weighted_per_panel": weighted, "angles": angles}


# ----------------------------------------------------------------- panels --

def _terrain_z_du(dtm, x, y, mpu):
    """VegetationMass.TryTerrainZDrawingUnits, VegetationMass.cs:1262-1272."""
    zm = dtm.interpolate_z(x, y)
    return None if zm is None else zm / mpu


def read_panel_centres(entities, dtm, meters_per_unit=1.0):
    """ReadPanelCentres, :2008-2103, for Studio's drawing: no PVcase blocks and no module-layer
    polylines exist, so the panels are the closed LEAF-TRACKERS polylines of at least three
    vertices, in drawing order (AppendPolylinePanelSample, :2105-2133; BuildPanelSample,
    :2135-2161). Each sample is {"index", "x", "y", "z", "entity"} (the entity's position)."""
    mpu = _meters_per_unit(meters_per_unit)
    panels = []
    for pos, ent in enumerate(_entities(entities)):
        if ent.get("type") != "LWPOLYLINE" or not ent.get("closed") or not _is_layer(ent, TRACKERS_LAYER):
            continue
        verts = _vertices(ent)
        n = len(verts)
        if n < 3:
            continue
        cx = cy = 0.0
        for vx, vy in verts:
            cx += vx
            cy += vy
        cx /= n
        cy /= n
        z = _finite(ent.get("elevation", 0.0), "polyline elevation")
        if abs(z) <= 1e-9:                                                            # :2130 with :2145
            tz = _terrain_z_du(dtm, cx, cy, mpu) if dtm is not None else None
            if tz is not None:
                z = tz
        panels.append({"index": len(panels), "x": cx, "y": cy, "z": z, "entity": pos})
        if len(panels) > MAX_PANELS:
            raise ShadeInputError(f"more than {MAX_PANELS} panels")
    return panels


def target_clearance_m(settings):
    """ResolvePanelTargetClearanceMeters, :2209-2224: the stored torque-tube height when it is
    in (0, 20) m, else 1.5 m."""
    value = (settings or {}).get(CLEARANCE_SETTING) if isinstance(settings, dict) else None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0.0 < value < 20.0:
        return float(value)
    return DEFAULT_TARGET_CLEARANCE_M


def _median(values):
    """Median, :2226-2233."""
    values = sorted(values)
    mid = len(values) // 2
    if len(values) % 2 == 1:
        return values[mid]
    return (values[mid - 1] + values[mid]) * 0.5


def bind_panel_datum(panels, dtm, meters_per_unit, target_m):
    """BindPanelDatumToTerrain, :2163-2207, in place. Returns {"mode": None | "bound" |
    "shifted", "median_clearance_m", "target_m", "shift_m"}: None without a grid or any panel
    on it, "bound" inside the 0.25 m tolerance, else every panel shifted by the difference."""
    if not panels or dtm is None:
        return {"mode": None, "median_clearance_m": None, "target_m": None, "shift_m": 0.0}
    mpu = _meters_per_unit(meters_per_unit)
    clearances = []
    for p in panels:
        tz = _terrain_z_du(dtm, p["x"], p["y"], mpu)
        if tz is None:
            continue
        clearances.append(p["z"] - tz)
    if not clearances:
        return {"mode": None, "median_clearance_m": None, "target_m": None, "shift_m": 0.0}
    median = _median(clearances)
    median_m = median * mpu
    target = target_m / mpu
    if abs(target_m - median_m) <= BINDING_TOLERANCE_M:
        return {"mode": "bound", "median_clearance_m": median_m, "target_m": target_m, "shift_m": 0.0}
    shift = target - median
    for p in panels:
        p["z"] += shift
    return {"mode": "shifted", "median_clearance_m": median_m, "target_m": target_m, "shift_m": shift * mpu}


# ----------------------------------------------------------------- heatmap --

def loss_bin(loss_pct):
    """ShadeLossGradient.GetBinIndex, :64-70."""
    if math.isnan(loss_pct) or loss_pct <= 0.0:
        return 0
    for i, edge in enumerate(BIN_UPPER_EDGES_PCT):
        if loss_pct <= edge:
            return i
    return BIN_COUNT - 1


def bin_color(bin_index):
    """ShadeLossGradient.GetColorForBin, :82-104; Math.Round is half to even, as round()."""
    first, last = GRADIENT_ANCHORS[0], GRADIENT_ANCHORS[-1]
    if bin_index <= first[0]:
        return first[1:]
    if bin_index >= last[0]:
        return last[1:]
    for a, b in zip(GRADIENT_ANCHORS, GRADIENT_ANCHORS[1:]):
        if a[0] <= bin_index <= b[0]:
            t = (bin_index - a[0]) / (b[0] - a[0])
            return tuple(max(0, min(255, int(round(a[i] + (b[i] - a[i]) * t)))) for i in (1, 2, 3))
    return last[1:]


def heatmap_markers(panels, result):
    """DrawHeatmap, :2313-2380: one closed 4 m square per panel on LEAF-SHADE-HEATMAP at the
    panel's elevation, coloured by the loss bin of its weighted shade. The ray-interception
    fields the plugin appends to a marker (:2364-2371) are storage only and are not modelled."""
    out = []
    s = MARKER_HALF_SIZE
    for p, panel in enumerate(panels):
        shade = result["weighted_per_panel"][p]
        b = loss_bin(shade * 100.0)
        r, g, bl = bin_color(b)
        x, y = panel["x"], panel["y"]
        out.append({"layer": HEATMAP_LAYER, "panel_index": panel["index"], "entity": panel.get("entity"),
                    "vertices": [(x - s, y - s), (x + s, y - s), (x + s, y + s), (x - s, y + s)],
                    "elevation": panel["z"], "rgb": (r, g, bl), "true_color": (r << 16) | (g << 8) | bl,
                    "shade": shade, "loss_bin": b})
    return out


# ----------------------------------------------------------------- exports --

def azal_matrix_text(result):
    """ShadingExports.WriteAzAlMatrix, :49-86 (UTF-8, no BOM)."""
    alts, azs, idx = [], [], {}
    for a in result["angles"]:
        if a["altitude_deg"] not in alts:
            alts.append(a["altitude_deg"])
        if a["azimuth_deg"] not in azs:
            azs.append(a["azimuth_deg"])
        idx[(a["altitude_deg"], a["azimuth_deg"])] = a["index"]
    alts.sort()
    azs.sort()
    avg = result["avg_by_angle"]
    lines = ["Altitude\\Azimuth" + "".join("," + net_general(az) for az in azs)]
    for alt in alts:
        cells = []
        for az in azs:
            i = idx.get((alt, az))
            cells.append("," + (net_fixed(avg[i], 4) if i is not None else "0.0000"))
        lines.append(net_general(alt) + "".join(cells))
    return "".join(line + NEWLINE for line in lines)


def sam_beam_text(result):
    """ShadingExports.WriteSamBeamTable, :92-104."""
    avg = result["avg_by_angle"]
    lines = ["Solar Azimuth (deg),Solar Altitude (deg),Beam Shading Loss Factor"]
    for a in result["angles"]:
        lines.append(net_general(a["azimuth_deg"]) + "," + net_general(a["altitude_deg"]) + ","
                     + net_fixed(avg[a["index"]], 4))
    return "".join(line + NEWLINE for line in lines)


def per_panel_text(result):
    """ShadingExports.WritePerPanelTable, :111-130: one line per (panel, angle); a fraction is
    exactly 0 or 1 (ShadingEngine.cs:221), printed F4."""
    shade = result["shade"]
    angles = result["angles"]
    if len(shade) * len(angles) > MAX_PER_PANEL_LINES:
        raise ShadeInputError(f"a per-panel table of more than {MAX_PER_PANEL_LINES} lines")
    middles = ["," + net_general(a["azimuth_deg"]) + "," + net_general(a["altitude_deg"]) + "," for a in angles]
    values = ("0.0000" + NEWLINE, "1.0000" + NEWLINE)
    parts = ["PanelIndex,AzimuthDeg,AltitudeDeg,ShadeFraction" + NEWLINE]
    for p, flags in enumerate(shade):
        head = str(p)
        parts.extend(head + middles[a] + values[flags[a]] for a in range(len(angles)))
    return "".join(parts)


# ---------------------------------------------------------------- commands --

def _panels_and_surface(entities, dtm, meters_per_unit, settings):
    mpu = _meters_per_unit(meters_per_unit)
    surface = TerrainShadeSurface(dtm, mpu)
    panels = read_panel_centres(entities, dtm, mpu)
    binding = bind_panel_datum(panels, dtm, mpu, target_clearance_m(settings))
    return mpu, surface, panels, binding


def shade_sim(entities, dtm, meters_per_unit=1.0, settings=None, existing_heatmap=0):
    """LEAFSHADESIM, LeafShadeSimCommand.cs:485-875, on the CPU path, without the file system
    and without the signed-in TMY and PVWatts re-weighting (:640-757), which needs a network
    account and a stored site location; the capture ran without either (its "TMY-weighted
    unavailable" line). `existing_heatmap` is the number of LEAF-SHADE-HEATMAP entities the run
    wipes (WipeLayerEntities, :2382-2412).

    Returns {"succeeded", "message", "panels", "binding", "profile", "surface", "result", "markers",
    "cleared", "mean_shade", "files": {"shade-azal-matrix", "shade-sam", "shade-per-panel"}};
    surface is surface_snapshot's size of the shade surface (:576-593), None when not built."""
    if dtm is None:                                                                   # :505-515
        return {"succeeded": False, "message": "LEAFSHADESIM: no terrain grid - run a terrain import first.",
                "panels": [], "binding": None, "profile": None, "result": None, "markers": [], "cleared": 0,
                "mean_shade": None, "files": {}}
    if isinstance(existing_heatmap, bool) or not isinstance(existing_heatmap, int) or existing_heatmap < 0:
        raise ShadeInputError("existing_heatmap must be a non-negative count")
    mpu, surface, panels, binding = _panels_and_surface(entities, dtm, meters_per_unit, settings)
    if not panels:                                                                    # :533-544
        return {"succeeded": False, "panels": [], "binding": binding, "profile": None, "result": None,
                "markers": [], "cleared": 0, "mean_shade": None, "files": {},
                "message": "LEAFSHADESIM: no PVcase panel solids, module-layer panels, or LEAF-TRACKERS frames found."}
    profile = select_profile(len(panels))                                             # :555
    result = run_shade(panels, profile["angles"], surface,
                       profile["ray_step_m"] / mpu, profile["max_ray_m"] / mpu)       # :572-573, :628-634
    markers = heatmap_markers(panels, result)                                         # :769
    weighted = result["weighted_per_panel"]
    mean_shade = sum(weighted) / max(1, len(weighted))                                # :797-799
    return {"succeeded": True, "panels": panels, "binding": binding, "profile": profile,
            "surface": surface_snapshot(dtm), "result": result,                       # :576-593
            "markers": markers, "cleared": existing_heatmap, "mean_shade": mean_shade,
            "files": {"shade-azal-matrix": azal_matrix_text(result), "shade-sam": sam_beam_text(result),
                      "shade-per-panel": per_panel_text(result)},
            "message": (f"LEAFSHADESIM: cos(zenith)-weighted = {net_fixed(mean_shade * 100, 2)}%. "
                        f"{len(markers)} panels tinted on {HEATMAP_LAYER}"
                        + (f" ({existing_heatmap} prior heatmap entities replaced)." if existing_heatmap > 0 else "."))}


def shade_compare(entities, dtm, meters_per_unit, settings, existing_heatmap, array_records, export_scene):
    """LEAFSHADECOMPARE, LeafShadeCompareCommand.cs:24-83: re-runs LEAFSHADESIM, then the scene
    export (`export_scene(dtm, array_records)`, the existing LEAFEXPORTSCENE port), then the
    export preview (LEAFSHOWEXPORT), which draws nothing when no arrays are defined ("no arrays
    defined", as captured). The preview of defined arrays is not ported: with arrays present
    this refuses rather than omit an overlay. The compare window and the layer visibility
    toggles (:49-50, :69) change no entity. Returns {"sim", "export", "preview"}."""
    if not callable(export_scene):
        raise ShadeInputError("export_scene must be the scene export function")
    if not isinstance(array_records, (list, tuple)):
        raise ShadeInputError("array_records must be the array store")
    sim = shade_sim(entities, dtm, meters_per_unit, settings, existing_heatmap)       # :35-37
    export = export_scene(dtm, list(array_records))                                   # :39-41
    preview = []
    if export.get("succeeded") and array_records:                                     # :43-47
        raise ShadeInputError("the export preview of defined arrays is not ported")
    return {"sim": sim, "export": export, "preview": preview}


def find_nearest_panel(panels, x, y):
    """FindNearestPanelIndex, :1344-1370: the first panel at the least squared distance."""
    best, best_d2 = -1, math.inf
    for i, p in enumerate(panels):
        dx = p["x"] - x
        dy = p["y"] - y
        d2 = dx * dx + dy * dy
        if d2 < best_d2:
            best_d2 = d2
            best = i
    return best, math.sqrt(best_d2)


def explain_panel(panel, profile, surface, step, max_ray, meters_per_unit=1.0):
    """ExplainPanelShade, :1379-1430, with the terrain as the only blocker source
    (TryExplainBlocker, :1692-1768, reduces to the terrain). Returns {"angle_count",
    "blocked_angles", "total_weight", "blocked_weight", "weighted_shade", "hits"}; hits are
    sorted by distance."""
    step, max_ray = _check_march(step, max_ray)
    mpu = _meters_per_unit(meters_per_unit)
    angle_count = blocked = 0
    total_w = blocked_w = 0.0
    hits = []
    zfn = surface.z
    for angle in profile["angles"]:
        angle_count += 1
        az = angle["azimuth_deg"] * math.pi / 180.0
        alt = angle["altitude_deg"] * math.pi / 180.0
        weight = max(0.0, math.sin(alt))                                              # :1398
        total_w += weight
        if angle["altitude_deg"] <= 0.0:
            continue
        dxs = math.sin(az) * step
        dys = math.cos(az) * step
        dzs = math.tan(alt) * step
        cap = surface.z_cap if dzs > 0.0 else None
        d = step
        while d <= max_ray + 1e-9:                                                    # :1406
            k = d / step
            ray_z = panel["z"] + k * dzs
            if cap is not None and ray_z > cap:
                break
            s = zfn(panel["x"] + k * dxs, panel["y"] + k * dys)
            if s is not None and s > ray_z:                                           # :1412, :1703
                hits.append({"source": "terrain", "azimuth_deg": angle["azimuth_deg"],
                             "altitude_deg": angle["altitude_deg"], "distance_m": d * mpu,
                             "blocker_z": s, "ray_z": ray_z, "over_ray_m": (s - ray_z) * mpu})
                blocked += 1
                blocked_w += weight
                break
            if s is None:                                                             # :1422-1423
                break
            d += step
    hits.sort(key=lambda h: h["distance_m"])                                          # :1428
    return {"angle_count": angle_count, "blocked_angles": blocked, "total_weight": total_w,
            "blocked_weight": blocked_w, "weighted_shade": blocked_w / total_w if total_w > 0.0 else 0.0,
            "hits": hits}


def shade_explain(entities, dtm, meters_per_unit, settings, pick_xy):
    """LEAFSHADEEXPLAIN, :79-251, answered with Enter at the marker prompt and a picked point
    (:1042-1049), so no stored heatmap summary is read. Returns {"succeeded", "message",
    "panel_index", "pick_distance_m", "panel_count", "binding", "profile", "explanation"}."""
    px, py = _point2(pick_xy, "picked point")
    if dtm is None:                                                                   # :146-151
        return {"succeeded": False, "message": "LEAFSHADEEXPLAIN: no terrain grid found.", "panel_index": -1,
                "pick_distance_m": None, "panel_count": 0, "binding": None, "profile": None, "explanation": None}
    mpu, surface, panels, binding = _panels_and_surface(entities, dtm, meters_per_unit, settings)
    if not panels:                                                                    # :165-170
        return {"succeeded": False, "message": "LEAFSHADEEXPLAIN: no panel samples found.", "panel_index": -1,
                "pick_distance_m": None, "panel_count": 0, "binding": binding, "profile": None,
                "explanation": None}
    index, dist = find_nearest_panel(panels, px, py)                                  # :172-174
    profile = select_profile(len(panels))                                             # :184
    explanation = explain_panel(panels[index], profile, surface, profile["ray_step_m"] / mpu,
                                profile["max_ray_m"] / mpu, mpu)                      # :192-198
    return {"succeeded": True, "panel_index": index, "pick_distance_m": dist * mpu, "panel_count": len(panels),
            "binding": binding, "profile": profile, "explanation": explanation,
            "message": (f"Result: {explanation['blocked_angles']}/{explanation['angle_count']} angle(s) blocked; "
                        f"cos(zenith)-weighted shade {net_fixed(explanation['weighted_shade'] * 100, 2)}%.")}
