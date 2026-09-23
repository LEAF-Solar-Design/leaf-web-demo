"""Studio's literal ports of four licensed plugin calculations, byte-exact.

Four production capabilities are pure calculations that a LEAF*DEMO command
exposes by running a fixed scenario list and writing a file, with no drawing
state. This module is the CALCULATION half; scripts/solar_probe_calcs_probes.py
holds Studio's copies of the scenario lists and writes the files.

Ported 2026-09-22 from C:/tmp/solar-parity/wt-b25-s17 (Branch2025), read-only:

  panel-snake-order
      LeafSolarDesign.Core/PanelGroupTradeCalculator.cs:583-607 (SnakeOrder)
      and :613-628 (BucketCoordinate), reached by
      Terrain/SnakeOrderDemoCommand.cs:208.
  project-summary-export-csv-json
      LeafSolarDesign.Core/PaletteHarness/ProjectSummaryReport.cs:36-84
      (ProjectSummary, ToCsv, ToJson), reached by
      Terrain/ProjectSummaryDemoCommand.cs:130-131.
  shade-limit-angle
      Terrain/ShadeLimitAngleCalculator.cs:56-124 (MaxGcrForSla,
      MinPitchForSla, AutoCalculate), Terrain/BacktrackingCalculator.cs:192-211
      (ComputeShadeLimitAngle) and Terrain/SunPositionCalculator.cs:43-136
      (Calculate), reached by Terrain/ShadeLimitAngleDemoCommand.cs:117-166.
  torque-tube-rear-shade
      Terrain/TorqueTubeShadeCalculator.cs:41-64
      (ComputeRearSelfShadeFraction), reached by
      Terrain/TorqueShadeDemoCommand.cs:148-149.

Byte-exactness is the contract, not an aspiration. Three rules carry it:

  * Every number is rendered through the C# formatter the plugin names, in
    InvariantCulture, reusing server/solar_nec.py's `format_fixed` so the
    away-from-zero tie rule has ONE implementation on this side. `F<n>` is the
    plugin's fixed-decimal form; `R` is its shortest round-trippable form
    (`format_roundtrip` below); an int renders as `int.ToString(ci)` does.
  * Line endings are CRLF, because `StringBuilder.AppendLine`,
    `StreamWriter.WriteLine` and Newtonsoft's `Formatting.Indented` all take
    `Environment.NewLine` and the plugin runs on Windows. Each CSV ends with a
    trailing CRLF; the JSON documents end with no trailing newline. The writers
    emit those bytes explicitly rather than leaning on the host's line-ending
    translation, so the output is identical on Windows and on Linux.
  * No field is quoted. The plugin's CSV schema has no quoting and no escape,
    which is exactly why `ToCsv` depends on the invariant-culture dot decimal
    separator (a comma would move a column boundary).

No AutoCAD, no network, no dependencies outside the standard library.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
from decimal import Decimal
import importlib.util
import json
import math
from pathlib import Path

CRLF = "\r\n"


def _load_solar_nec():
    """Load server/solar_nec.py by path so the import works from any cwd.

    The NEC port already carries the C# number formatters and their tie rule;
    re-deriving them here is how two renderings of the same double drift apart.
    """
    path = Path(__file__).resolve().with_name("solar_nec.py")
    spec = importlib.util.spec_from_file_location("solar_nec", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


nec = _load_solar_nec()

# C#'s default double rendering is "G15" under .NET Framework and the shortest
# round-trippable form under .NET Core; max(shortest, 15) satisfies both, the
# same bound server/solar_nec.py documents at _DEFAULT_MIN_DIGITS.
_MIN_THRESHOLD_DIGITS = 15


def format_fixed(value, decimals):
    """C# double.ToString("F<decimals>", InvariantCulture). See solar_nec."""
    return nec.format_fixed(value, decimals)


def format_roundtrip(value):
    """C# double.ToString("R", InvariantCulture): shortest round-trippable.

    Python's `repr` of a float IS the shortest round-trippable decimal, so the
    digits come straight from it; only C#'s PRESENTATION differs, in two ways
    this function fixes. C# prints an integral double with no fraction ("30",
    never "30.0"), and it switches to scientific notation on the same G rule
    the NEC port documents: fixed while the decimal exponent is greater than -5
    and less than the precision, scientific otherwise with an uppercase E and a
    signed, at-least-two-digit exponent.

    Every value rendered through this function in this module is a tilt, a
    length or a fraction inside [-90, 90], so the fixed branch is the only one
    the probe files reach; the scientific branch exists so the port does not
    quietly disagree with C# outside that range.
    """
    special = nec._non_finite(value)
    if special is not None:
        return special
    if value == 0.0:
        return "-0" if math.copysign(1.0, value) < 0 else "0"
    sign = "-" if value < 0 else ""
    shortest = Decimal(repr(abs(float(value))))
    exponent = shortest.adjusted()
    precision = max(len(shortest.as_tuple().digits), _MIN_THRESHOLD_DIGITS)
    if -5 < exponent < precision:
        return sign + nec._trim_fraction(format(shortest, "f"))
    mantissa = nec._trim_fraction(format(shortest.scaleb(-exponent), "f"))
    return sign + mantissa + "E" + ("+" if exponent >= 0 else "-") + "%02d" % abs(exponent)


def format_int(value):
    """C# int.ToString(InvariantCulture)."""
    return "%d" % int(value)


# --------------------------------------------------------------------------- #
# panel-snake-order
# PanelGroupTradeCalculator.SnakeOrder / BucketCoordinate
# --------------------------------------------------------------------------- #
class TradePanel:
    """The plugin's TradePanel projection SnakeOrder reads: handle, X, Y, Row, Col.

    X and Y are doubles and Row and Col are ints, matching the C# fields, because
    the probe file writes each one with its own type and a 0 where the plugin
    writes 0.0 is a parity failure.
    """

    __slots__ = ("handle", "x", "y", "row", "col")

    def __init__(self, handle, x, y, row=-1, col=-1):
        self.handle = handle
        self.x = float(x)
        self.y = float(y)
        self.row = int(row)
        self.col = int(col)


def bucket_coordinate(value, all_values):
    """PanelGroupTradeCalculator.cs:613-628, including C#'s rounding rule.

    `(int)Math.Round(x)` is round-half-to-EVEN in C#, which is Python's own
    built-in `round`, and NOT the away-from-zero rule `format_fixed` uses. The
    two rules live one file apart in the plugin and disagree at every .5.
    """
    ordered = sorted(set(float(v) for v in all_values))
    if len(ordered) <= 1:
        return 0
    gaps = sorted(ordered[i] - ordered[i - 1] for i in range(1, len(ordered)))
    spacing = gaps[len(gaps) // 2]
    if spacing < 1e-6:
        return 0
    return int(round((float(value) - ordered[0]) / spacing))


def snake_order(panels):
    """PanelGroupTradeCalculator.cs:583-607. Returns the INPUT object unchanged
    for a null or <=1 panel list, which is the reference-equality the DEMO's
    `Unchanged` flag records.

    Row groups come from a dictionary keyed in first-appearance order and then
    sorted by key, which is `GroupBy(...).OrderBy(g => g.Key)`; the within-row
    sorts are stable in both languages, so ties keep the input's order.
    """
    if panels is None or len(panels) <= 1:
        return panels

    all_y = [panel.y for panel in panels]
    groups = OrderedDict()
    for panel in panels:
        key = panel.row if panel.row >= 0 else bucket_coordinate(panel.y, all_y)
        groups.setdefault(key, []).append(panel)

    result = []
    left_to_right = True
    for key in sorted(groups):
        # `p.Col >= 0 ? p.Col : p.X` is a DOUBLE in C#: the int widens.
        ordered = sorted(groups[key],
                         key=lambda p: float(p.col) if p.col >= 0 else p.x,
                         reverse=not left_to_right)
        result.extend(ordered)
        left_to_right = not left_to_right
    return result


# --------------------------------------------------------------------------- #
# project-summary-export-csv-json
# PaletteHarness/ProjectSummaryReport.cs:36-84
# --------------------------------------------------------------------------- #
class ProjectSummary:
    """The plugin's ProjectSummary, with its PROPERTY DECLARATION ORDER preserved.

    Newtonsoft serializes properties in declaration order, so this order is the
    JSON document's key order and is load-bearing, not cosmetic.
    """

    __slots__ = ("panel_count", "inverter_count", "string_count", "total_dc_kwp",
                 "total_ac_kw", "dc_ac_ratio", "total_trunk_length",
                 "total_branch_length", "strings_by_length", "critical_findings",
                 "warning_findings", "info_findings", "largest_cable_sizes")

    def __init__(self, panel_count=0, inverter_count=0, string_count=0,
                 total_dc_kwp=0.0, total_ac_kw=0.0, dc_ac_ratio=0.0,
                 total_trunk_length=0.0, total_branch_length=0.0,
                 critical_findings=0, warning_findings=0, info_findings=0):
        self.panel_count = int(panel_count)
        self.inverter_count = int(inverter_count)
        self.string_count = int(string_count)
        self.total_dc_kwp = float(total_dc_kwp)
        self.total_ac_kw = float(total_ac_kw)
        self.dc_ac_ratio = float(dc_ac_ratio)
        self.total_trunk_length = float(total_trunk_length)
        self.total_branch_length = float(total_branch_length)
        # Dictionary<int,int>: INSERTION order, which ToJson keeps and ToCsv sorts.
        self.strings_by_length = OrderedDict()
        self.critical_findings = int(critical_findings)
        self.warning_findings = int(warning_findings)
        self.info_findings = int(info_findings)
        self.largest_cable_sizes = []


def project_summary_to_csv(summary):
    """ProjectSummaryReport.cs:62-83. CRLF per row INCLUDING the last one.

    Three format decisions here are the whole reason the DEMO exists: F3 on the
    three power figures and F2 on the two lengths, ASCENDING key order for
    StringsByLength (against the JSON's insertion order), and a NULL cable size
    rendering as an EMPTY value rather than the string "null".
    """
    rows = [
        ("Metric", "Value"),
        ("PanelCount", format_int(summary.panel_count)),
        ("InverterCount", format_int(summary.inverter_count)),
        ("StringCount", format_int(summary.string_count)),
        ("TotalDcKwp", format_fixed(summary.total_dc_kwp, 3)),
        ("TotalAcKw", format_fixed(summary.total_ac_kw, 3)),
        ("DcAcRatio", format_fixed(summary.dc_ac_ratio, 3)),
        ("TotalTrunkLength", format_fixed(summary.total_trunk_length, 2)),
        ("TotalBranchLength", format_fixed(summary.total_branch_length, 2)),
        ("CriticalFindings", format_int(summary.critical_findings)),
        ("WarningFindings", format_int(summary.warning_findings)),
        ("InfoFindings", format_int(summary.info_findings)),
    ]
    for length in sorted(summary.strings_by_length):
        rows.append(("StringsOfLength_" + format_int(length),
                     format_int(summary.strings_by_length[length])))
    for index, size in enumerate(summary.largest_cable_sizes, start=1):
        rows.append(("CableSize_" + format_int(index), size if size is not None else ""))
    return "".join(metric + "," + value + CRLF for metric, value in rows)


def project_summary_to_json(summary):
    """ProjectSummaryReport.cs:57-60: JsonConvert.SerializeObject(this, Indented).

    Newtonsoft's Indented writer uses a two-space indent and Environment.NewLine,
    and emits no trailing newline. Python's `json.dumps(indent=2)` matches its
    layout exactly, and its float repr is the same shortest round-trippable form
    Newtonsoft writes (66.0, not 66); the CRLF substitution is safe because
    `json.dumps` escapes any newline inside a string value.
    """
    document = OrderedDict((
        ("PanelCount", summary.panel_count),
        ("InverterCount", summary.inverter_count),
        ("StringCount", summary.string_count),
        ("TotalDcKwp", summary.total_dc_kwp),
        ("TotalAcKw", summary.total_ac_kw),
        ("DcAcRatio", summary.dc_ac_ratio),
        ("TotalTrunkLength", summary.total_trunk_length),
        ("TotalBranchLength", summary.total_branch_length),
        ("StringsByLength", OrderedDict((format_int(key), value)
                                        for key, value in summary.strings_by_length.items())),
        ("CriticalFindings", summary.critical_findings),
        ("WarningFindings", summary.warning_findings),
        ("InfoFindings", summary.info_findings),
        ("LargestCableSizes", list(summary.largest_cable_sizes)),
    ))
    text = json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False)
    return text.replace("\n", CRLF)


# --------------------------------------------------------------------------- #
# shade-limit-angle
# ShadeLimitAngleCalculator + BacktrackingCalculator + SunPositionCalculator
# --------------------------------------------------------------------------- #
DEG_TO_RAD = math.pi / 180.0
RAD_TO_DEG = 180.0 / math.pi


class ShadeLimitError(ValueError):
    """The plugin's ArgumentException from this engine, kept as a refusal.

    A domain violation is never answered with a number here, exactly as
    MaxGcrForSla throws rather than clamping an out-of-domain SLA.
    """


def max_gcr_for_sla(sla_deg, max_tilt_deg):
    """ShadeLimitAngleCalculator.cs:56-76. Closed-form inverse of the SLA formula."""
    if sla_deg < 0 or sla_deg >= 90:
        raise ShadeLimitError("slaDeg must be in [0, 90).")
    if max_tilt_deg <= 0 or max_tilt_deg >= 90:
        raise ShadeLimitError("maxTiltDeg must be in (0, 90).")
    if sla_deg == 0.0:
        return 0.0
    tan_sla = math.tan(sla_deg * DEG_TO_RAD)
    sin_t = math.sin(max_tilt_deg * DEG_TO_RAD)
    cos_t = math.cos(max_tilt_deg * DEG_TO_RAD)
    denom = sin_t + tan_sla * cos_t
    if denom <= 0:
        return 1.0
    gcr = tan_sla / denom
    if gcr < 0:
        gcr = 0.0
    if gcr > 1:
        gcr = 1.0
    return gcr


def min_pitch_for_sla(sla_deg, cross_axis_m, max_tilt_deg):
    """ShadeLimitAngleCalculator.cs:78-86. Infinite when the max GCR is zero."""
    if cross_axis_m <= 0:
        raise ShadeLimitError("crossAxisM must be > 0.")
    gcr = max_gcr_for_sla(sla_deg, max_tilt_deg)
    if gcr <= 0:
        return math.inf
    return cross_axis_m / gcr


def compute_shade_limit_angle(gcr, max_tilt_deg):
    """BacktrackingCalculator.cs:192-211. The forward direction, SLA in degrees."""
    if gcr <= 0 or gcr >= 1.0:
        raise ShadeLimitError("gcr must be in the open interval (0, 1).")
    if max_tilt_deg <= 0 or max_tilt_deg >= 90.0:
        raise ShadeLimitError("maxTiltDeg must be in the open interval (0, 90).")
    tilt_rad = max_tilt_deg * math.pi / 180.0
    sin_tilt = math.sin(tilt_rad)
    cos_tilt = math.cos(tilt_rad)
    denominator = 1.0 - gcr * cos_tilt
    if denominator <= 0:
        return 90.0
    return math.atan(gcr * sin_tilt / denominator) * 180.0 / math.pi


class SunPosition:
    __slots__ = ("azimuth_deg", "elevation_deg", "is_nighttime")

    def __init__(self, azimuth_deg, elevation_deg, is_nighttime):
        self.azimuth_deg = azimuth_deg
        self.elevation_deg = elevation_deg
        self.is_nighttime = is_nighttime


def sun_position(lat_deg, lon_deg, utc):
    """SunPositionCalculator.cs:43-136. Spencer (1971) plus the Blanco azimuth.

    `utc.timetuple().tm_yday` is DateTime.DayOfYear. Operation order follows the
    C# line for line: a reassociated sum of these terms moves the last bits, and
    the DEMO's F12 columns show the last bits.
    """
    day_of_year = utc.timetuple().tm_yday
    b = (2.0 * math.pi / 365.0) * (day_of_year - 1)

    eqt_min = 229.18 * (0.000075
                        + 0.001868 * math.cos(b)
                        - 0.032077 * math.sin(b)
                        - 0.014615 * math.cos(2.0 * b)
                        - 0.040890 * math.sin(2.0 * b))

    decl_rad = (0.006918
                - 0.399912 * math.cos(b)
                + 0.070257 * math.sin(b)
                - 0.006758 * math.cos(2.0 * b)
                + 0.000907 * math.sin(2.0 * b)
                - 0.002697 * math.cos(3.0 * b)
                + 0.001480 * math.sin(3.0 * b))

    utc_hours = utc.hour + utc.minute / 60.0 + utc.second / 3600.0
    solar_hours = utc_hours + lon_deg / 15.0 + eqt_min / 60.0
    hour_angle_rad = (solar_hours - 12.0) * 15.0 * DEG_TO_RAD

    lat_rad = lat_deg * DEG_TO_RAD
    sin_elev = (math.sin(lat_rad) * math.sin(decl_rad)
                + math.cos(lat_rad) * math.cos(decl_rad) * math.cos(hour_angle_rad))
    sin_elev = max(-1.0, min(1.0, sin_elev))

    elev_rad = math.asin(sin_elev)
    elev_deg = elev_rad * RAD_TO_DEG
    if elev_deg <= 0.0:
        return SunPosition(0.0, elev_deg, True)

    cos_elev = math.cos(elev_rad)
    cos_lat = math.cos(lat_rad)
    cos_az = (math.sin(decl_rad) - math.sin(lat_rad) * sin_elev) / (cos_elev * cos_lat)
    cos_az = max(-1.0, min(1.0, cos_az))
    azimuth_deg = math.acos(cos_az) * RAD_TO_DEG
    if hour_angle_rad > 0.0:
        azimuth_deg = 360.0 - azimuth_deg
    return SunPosition(azimuth_deg, elev_deg, False)


class ShadeLimitAutoResult:
    __slots__ = ("sun_elevation_deg", "sun_azimuth_deg", "is_daytime", "max_gcr",
                 "min_pitch_m")

    def __init__(self, sun_elevation_deg, sun_azimuth_deg, is_daytime, max_gcr,
                 min_pitch_m):
        self.sun_elevation_deg = sun_elevation_deg
        self.sun_azimuth_deg = sun_azimuth_deg
        self.is_daytime = is_daytime
        self.max_gcr = max_gcr
        self.min_pitch_m = min_pitch_m


def shade_limit_auto_calculate(latitude_deg, longitude_deg, design_utc,
                               module_cross_axis_m, max_tilt_deg):
    """ShadeLimitAngleCalculator.cs:92-124. Sun elevation at the design time IS
    the target SLA, clamped into MaxGcrForSla's open domain at 89.999."""
    if module_cross_axis_m <= 0:
        raise ShadeLimitError("ModuleCrossAxisM must be > 0.")
    if max_tilt_deg <= 0 or max_tilt_deg >= 90:
        raise ShadeLimitError("MaxTiltDeg must be in (0, 90).")
    sun = sun_position(latitude_deg, longitude_deg, design_utc)
    if sun.is_nighttime or sun.elevation_deg <= 0:
        return ShadeLimitAutoResult(sun.elevation_deg, sun.azimuth_deg,
                                    not sun.is_nighttime, 0.0, math.inf)
    sla = min(89.999, max(0.0, sun.elevation_deg))
    max_gcr = max_gcr_for_sla(sla, max_tilt_deg)
    min_pitch = module_cross_axis_m / max_gcr if max_gcr > 0 else math.inf
    return ShadeLimitAutoResult(sun.elevation_deg, sun.azimuth_deg,
                                not sun.is_nighttime, max_gcr, min_pitch)


# --------------------------------------------------------------------------- #
# torque-tube-rear-shade
# TorqueTubeShadeCalculator.cs:41-64
# --------------------------------------------------------------------------- #
def rear_self_shade_fraction(cross_axis_m, radius_m, top_gap_m, bottom_gap_m,
                             tilt_deg):
    """band = 2R + (topGap + botGap) * |sin(tilt)|, clamped into [0, 1].

    Two guards come BEFORE the arithmetic and are what half the DEMO probes
    exist to pin: a non-positive cross axis returns 0 rather than dividing, and
    a tube with no radius and no gaps returns 0 at any tilt.
    """
    if cross_axis_m <= 0.0:
        return 0.0
    radius = max(0.0, radius_m)
    top_gap = max(0.0, top_gap_m)
    bottom_gap = max(0.0, bottom_gap_m)
    if radius <= 0.0 and top_gap <= 0.0 and bottom_gap <= 0.0:
        return 0.0
    sin_abs = abs(math.sin(tilt_deg * (math.pi / 180.0)))
    band = 2.0 * radius + (top_gap + bottom_gap) * sin_abs
    fraction = band / cross_axis_m
    if fraction < 0.0:
        return 0.0
    if fraction > 1.0:
        return 1.0
    return fraction


def utc(year, month, day, hour=0, minute=0, second=0):
    """A UTC DateTime, spelled once so a scenario list cannot forget the tzinfo."""
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
