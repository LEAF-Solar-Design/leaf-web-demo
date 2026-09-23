#!/usr/bin/env python3
"""Studio's literal port of the plugin's harness planner and tracker BOM writers.

Three licensed engines, ported case for case from the Branch2025 plugin source
read 2026-09-22 at C:/tmp/solar-parity/wt-b25-s17:

  * Tracker/HarnessCalculator.cs:82-193      -> `plan`
  * Terrain/TrackerBomCalculator.cs:96-242   -> `compute_bom`
  * Terrain/TrackerBomXlsxExporter.cs:73-396 -> `generate_pile_coordinates`,
        `aggregate_harness_extensions` and `build_bom_workbook`

`build_bom_workbook` returns the seven tabs as the (name, rows) shape
server/solar_xlsx.py writes, rather than writing them itself, so the tab
contract can be asserted without going through a zip archive.

TWO STRINGS WHERE THE PLUGIN'S WORKING TREE HAS DRIFTED FROM THE CAPTURE. The
licensed outputs were captured on AutoCAD 2025 on 2026-09-23 and they are the
contract; the source tree read on 2026-09-22 is the same code with two
characters changed since. The capture's module description reads
"PV Module <em dash> 2.000 m x 1.000 m portrait" (U+2014), where the tree now
has an ASCII hyphen, and the capture's parallel-trunk refusal reads "requires a
tracker row  fixed-tilt" with TWO spaces, which is an em dash removed by
`_sanitize_message`'s ASCII filter, where the tree now has an ASCII hyphen that
would have survived it. Both are reproduced as the CAPTURE has them, because the
capture is the licensed output this port must equal. Change them only against a
newer licensed capture.

Number rendering is not re-derived here: `server/solar_probe_calcs.py` already
carries C#'s "F<n>" (ties away from zero) and "R" (shortest round-trippable)
formatters, and this module borrows them so two renderings of one double cannot
drift apart.

No network, no dependencies outside the standard library.
"""

from __future__ import annotations

import importlib.util
import math
from pathlib import Path


def _load(name):
    path = Path(__file__).resolve().with_name(name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


calcs = _load("solar_probe_calcs")
CRLF = calcs.CRLF

# HarnessType (Tracker/HarnessCalculator.cs:10-30). The C# enum's ToString() is
# the member NAME, and the Cable Tray sort orders by string.CompareOrdinal over
# exactly that name, so the port carries the names and never an ordinal.
END_OF_ROW = "EndOfRow"
PARALLEL_TRUNK = "ParallelTrunk"
MOTOR = "Motor"
HARNESS_TYPES = (END_OF_ROW, PARALLEL_TRUNK, MOTOR)

# NEC 690.8 + Table 310.16 90 C column, PV wire (HarnessCalculator.cs:84-91).
AMPACITY_LADDER = ((30.0, 10), (55.0, 8), (75.0, 6), (95.0, 4), (130.0, 2))

DEFAULT_PILE_SPACING_M = 5.0

# The exact placeholder strings the three empty branches write. The EMPTY demo
# exists to pin these, so they are literals here and never built from a template.
CABLE_TRAY_PLACEHOLDER = ("Cable / harness detail will be populated when Q27 "
                          "(Harness Manager) ships. Placeholder tab for parity.")
ELECTRICAL_PLACEHOLDER = "No electrical line items. Provide module.PmaxW to populate."
PILING_PLACEHOLDER = ("Pile coordinates not provided. Call GeneratePileCoordinates "
                      "or supply a pile list to populate.")

BOM_HEADER = ("Category", "Description", "Unit", "Quantity")
CABLE_TRAY_HEADER = ("HarnessType", "CableGaugeAwg", "DropLengthM", "Count", "TotalLengthM")
LAYOUT_HEADER = ("RowIndex", "AxisStartX", "AxisStartY", "AxisEndX", "AxisEndY",
                 "LengthMeters", "RailOverhangM", "PhysicalLengthMeters", "ModuleSlots")
PILING_HEADER = ("PileIndex", "RowIndex", "X", "Y", "TerrainZ", "PileHeightAboveGrade")
TAB_NAMES = ("Overview", "By Area", "Modules", "Cable Tray", "Layout",
             "Electrical", "Piling")


class HarnessArgumentError(ValueError):
    """C# ArgumentException / ArgumentNullException out of HarnessCalculator.Plan."""


class HarnessSupportError(ValueError):
    """C# InvalidOperationException: an unsupported combination or an over-ladder current."""


class BomArgumentError(ValueError):
    """C# ArgumentException / ArgumentNullException out of the BOM calculator and exporter."""


# --------------------------------------------------------------------------- #
# harness-cable-plan (Tracker/HarnessCalculator.cs)
# --------------------------------------------------------------------------- #
class HarnessStringInput:
    """HarnessStringInput (HarnessCalculator.cs:35-60), field for field."""

    __slots__ = ("module_count", "module_pitch_m", "trunk_tap_from_start_m",
                 "string_max_amps", "is_tracker_row", "requested_type")

    def __init__(self, module_count=0, module_pitch_m=0.0, trunk_tap_from_start_m=0.0,
                 string_max_amps=0.0, is_tracker_row=False, requested_type=END_OF_ROW):
        self.module_count = module_count
        self.module_pitch_m = module_pitch_m
        self.trunk_tap_from_start_m = trunk_tap_from_start_m
        self.string_max_amps = string_max_amps
        self.is_tracker_row = is_tracker_row
        self.requested_type = requested_type


class HarnessSpec:
    """HarnessSpec (HarnessCalculator.cs:65-72). Lengths are metres."""

    __slots__ = ("type", "connectors", "cable_gauge_awg", "module_drop_lengths_m",
                 "total_cable_length_m")

    def __init__(self, type=END_OF_ROW, connectors=0, cable_gauge_awg=0,
                 module_drop_lengths_m=(), total_cable_length_m=0.0):
        self.type = type
        self.connectors = connectors
        self.cable_gauge_awg = cable_gauge_awg
        self.module_drop_lengths_m = module_drop_lengths_m
        self.total_cable_length_m = total_cable_length_m


def _reject_unsupported_combos(spec):
    """HarnessCalculator.RejectUnsupportedCombos (cs:122-133).

    The parallel-trunk message carries the capture's em dash; see the module
    docstring for why it is not the tree's ASCII hyphen.
    """
    if spec.is_tracker_row:
        return
    if spec.requested_type == PARALLEL_TRUNK:
        raise HarnessSupportError(
            "Parallel trunk harness requires a tracker row \u2014 fixed-tilt "
            "rows lack the interior conduit path for in-row parallel taps.")
    if spec.requested_type == MOTOR:
        raise HarnessSupportError(
            "Motor harness only applies to tracker rows (no slew drive "
            "on a fixed-tilt system).")


def select_gauge(amps):
    """HarnessCalculator.SelectGauge (cs:135-143). Fails closed past the ladder."""
    for ceiling, awg in AMPACITY_LADDER:
        if amps <= ceiling:
            return awg
    raise HarnessSupportError(
        "String current %s A exceeds the largest supported "
        "PV-wire gauge in the ampacity ladder (%s A). "
        "Split the string or use a combiner."
        % (calcs.format_fixed(amps, 1), calcs.format_roundtrip(AMPACITY_LADDER[-1][0])))


def _plan_end_of_row(spec, awg):
    """HarnessCalculator.PlanEndOfRow (cs:145-166): drop[i] = |i * pitch - tap|."""
    pitch = spec.module_pitch_m
    tap = spec.trunk_tap_from_start_m
    drops = []
    total = 0.0
    for index in range(spec.module_count):
        drop = abs(index * pitch - tap)
        drops.append(drop)
        total += drop
    return HarnessSpec(type=END_OF_ROW, connectors=spec.module_count, cable_gauge_awg=awg,
                       module_drop_lengths_m=tuple(drops), total_cable_length_m=total)


def _plan_parallel_trunk(spec, awg):
    """HarnessCalculator.PlanParallelTrunk (cs:168-178): end-of-row math, other type."""
    plan = _plan_end_of_row(spec, awg)
    plan.type = PARALLEL_TRUNK
    return plan


def _plan_motor(spec, awg):
    """HarnessCalculator.PlanMotor (cs:180-192): one drop to the row midpoint."""
    mid_row_m = (spec.module_count - 1) * 0.5 * spec.module_pitch_m
    drop_to_mid = abs(mid_row_m - spec.trunk_tap_from_start_m)
    return HarnessSpec(type=MOTOR, connectors=1, cable_gauge_awg=awg,
                       module_drop_lengths_m=(drop_to_mid,),
                       total_cable_length_m=drop_to_mid)


_PLANNERS = {END_OF_ROW: _plan_end_of_row,
             PARALLEL_TRUNK: _plan_parallel_trunk,
             MOTOR: _plan_motor}


def plan(spec):
    """HarnessCalculator.Plan (cs:93-120). Validates, then dispatches by type."""
    if spec is None:
        raise HarnessArgumentError("input must not be null")
    if spec.module_count <= 0:
        raise HarnessArgumentError("ModuleCount must be > 0.")
    if spec.module_pitch_m <= 0.0:
        raise HarnessArgumentError("ModulePitchM must be > 0.")
    if spec.string_max_amps < 0.0:
        raise HarnessArgumentError("StringMaxAmps cannot be negative.")
    _reject_unsupported_combos(spec)
    awg = select_gauge(spec.string_max_amps)
    planner = _PLANNERS.get(spec.requested_type)
    if planner is None:
        raise HarnessSupportError("Unsupported HarnessType '%s'." % (spec.requested_type,))
    return planner(spec, awg)


# --------------------------------------------------------------------------- #
# tracker BOM (Terrain/TrackerBomCalculator.cs)
# --------------------------------------------------------------------------- #
class TrackerModuleSpec:
    """TrackerModuleSpec (Terrain/TrackerRowGenerator.cs:10-70), the fields the BOM reads.

    The defaults are the C# property initializers, including PmaxW = 0, which is
    the "not specified" sentinel that omits the DC capacity line entirely.
    """

    __slots__ = ("along_axis_m", "cross_axis_m", "gap_m", "pmax_w")

    def __init__(self, along_axis_m=1.000, cross_axis_m=2.100, gap_m=0.020, pmax_w=0.0):
        self.along_axis_m = along_axis_m
        self.cross_axis_m = cross_axis_m
        self.gap_m = gap_m
        self.pmax_w = pmax_w


class TrackerRow:
    """TrackerRow (Terrain/TrackerRowGenerator.cs:172-282), the fields the BOM reads."""

    __slots__ = ("axis_start", "axis_end", "module_slots", "length_meters",
                 "rail_overhang_m", "row_index", "module_power_watts",
                 "pile_count_override", "no_piles_estimated")

    def __init__(self, axis_start=(0.0, 0.0), axis_end=(0.0, 0.0), module_slots=0,
                 length_meters=0.0, rail_overhang_m=0.0, row_index=0,
                 module_power_watts=0, pile_count_override=-1, no_piles_estimated=False):
        self.axis_start = axis_start
        self.axis_end = axis_end
        self.module_slots = module_slots
        self.length_meters = length_meters
        self.rail_overhang_m = rail_overhang_m
        self.row_index = row_index
        self.module_power_watts = module_power_watts
        self.pile_count_override = pile_count_override
        self.no_piles_estimated = no_piles_estimated

    @property
    def physical_length_meters(self):
        """TrackerRow.PhysicalLengthMeters (cs:208): length plus overhang at BOTH ends."""
        return self.length_meters + 2.0 * self.rail_overhang_m


class BomLine:
    """BomLine (TrackerBomCalculator.cs:10-23)."""

    __slots__ = ("category", "description", "unit", "quantity")

    def __init__(self, category, description, unit, quantity):
        self.category = category
        self.description = description
        self.unit = unit
        self.quantity = quantity


class TrackerBomResult:
    """TrackerBomResult (TrackerBomCalculator.cs:28-86)."""

    __slots__ = ("lines", "total_rows", "total_modules", "total_tube_length_m",
                 "total_piles", "total_drive_units", "total_dc_capacity_kwp")

    def __init__(self, lines=None, total_rows=0, total_modules=0, total_tube_length_m=0.0,
                 total_piles=0, total_drive_units=0, total_dc_capacity_kwp=0.0):
        self.lines = list(lines) if lines else []
        self.total_rows = total_rows
        self.total_modules = total_modules
        self.total_tube_length_m = total_tube_length_m
        self.total_piles = total_piles
        self.total_drive_units = total_drive_units
        self.total_dc_capacity_kwp = total_dc_capacity_kwp


def compute_bom(rows, module, pile_spacing_m=DEFAULT_PILE_SPACING_M):
    """TrackerBomCalculator.Compute (cs:117-241), line for line and in line order."""
    if rows is None:
        raise BomArgumentError("rows must not be null")
    if module is None:
        raise BomArgumentError("module must not be null")
    if pile_spacing_m <= 0:
        raise BomArgumentError("pileSpacingM must be > 0.")

    rows = list(rows)
    total_modules = 0
    total_tube_m = 0.0
    total_piles = 0
    total_drives = len(rows)          # one drive per table section
    dc_capacity_kwp = 0.0
    used_explicit = False
    used_estimated = False

    for row in rows:
        total_modules += row.module_slots
        tube_m = row.physical_length_meters
        total_tube_m += tube_m
        if row.pile_count_override >= 0:
            total_piles += row.pile_count_override
            used_explicit = True
        elif not row.no_piles_estimated:
            # At least 1 per row, then one per full spacing interval.
            total_piles += max(1, math.ceil(tube_m / pile_spacing_m))
            used_estimated = True
        power_w = row.module_power_watts if row.module_power_watts > 0 else module.pmax_w
        if power_w > 0:
            dc_capacity_kwp += row.module_slots * power_w / 1000.0

    result = TrackerBomResult(total_rows=len(rows), total_modules=total_modules,
                              total_tube_length_m=total_tube_m, total_piles=total_piles,
                              total_drive_units=total_drives,
                              total_dc_capacity_kwp=dc_capacity_kwp)
    fixed = calcs.format_fixed

    # 1. PV Modules. The em dash is the capture's; see the module docstring.
    result.lines.append(BomLine(
        "Module",
        "PV Module \u2014 %s m \u00d7 %s m portrait"
        % (fixed(module.cross_axis_m, 3), fixed(module.along_axis_m, 3)),
        "ea", float(total_modules)))

    # 2. Torque tubes, one pair of lines and only when there are rows at all.
    if rows:
        result.lines.append(BomLine(
            "Torque Tube",
            "Torque tube / tracker rail (%d sections, %s m total)"
            % (len(rows), fixed(total_tube_m, 1)),
            "m", total_tube_m))
        result.lines.append(BomLine(
            "Torque Tube", "Torque tube section count", "ea", float(len(rows))))

    # 3. Piles. Three descriptions, chosen by which counting paths were used.
    if used_explicit and used_estimated:
        pile_description = ("Ground pile / foundation (drawing XData where present; "
                            "otherwise %s m c/c estimated)" % fixed(pile_spacing_m, 1))
    elif used_explicit:
        pile_description = "Ground pile / foundation (from drawing XData)"
    else:
        pile_description = ("Ground pile / foundation (at %s m c/c spacing, estimated)"
                            % fixed(pile_spacing_m, 1))
    result.lines.append(BomLine("Pile", pile_description, "ea", float(total_piles)))

    # 4. Drive units.
    result.lines.append(BomLine(
        "Drive Unit", "Single-axis tracker drive unit (one per table section)",
        "ea", float(total_drives)))

    # 5. DC capacity, only when some module power was known.
    if dc_capacity_kwp > 0:
        if module.pmax_w > 0:
            description = ("Total DC nameplate (%d \u00d7 %s Wp)"
                           % (total_modules, fixed(module.pmax_w, 0)))
        else:
            description = ("Total DC nameplate (%d modules; per-row Wp from drawing XData)"
                           % total_modules)
        result.lines.append(BomLine("DC Capacity", description, "kWp", dc_capacity_kwp))

    return result


def bom_to_csv(bom):
    """TrackerBomResult.ToCsv (cs:65-85): header plus one row per line item.

    An "ea" quantity renders as a rounded integer and everything else as "F2",
    and a value holding a comma, a quote or a newline is quoted C#-style.
    """
    lines = ["Category,Description,Unit,Quantity"]
    for line in bom.lines:
        if line.unit == "ea":
            quantity = "%d" % int(_round_half_even(line.quantity))
        else:
            quantity = calcs.format_fixed(line.quantity, 2)
        lines.append(",".join((_escape_csv(line.category), _escape_csv(line.description),
                               _escape_csv(line.unit), quantity)))
    return "".join(line + CRLF for line in lines)


def _round_half_even(value):
    """C# Math.Round(double) with no MidpointRounding argument rounds half to EVEN.

    Python's built-in `round` is the same rule, so this is a name for the fact
    rather than an implementation: the "F<n>" formatter one file away rounds
    half AWAY from zero, and confusing the two silently moves a quantity.
    """
    return round(value)


def _escape_csv(text):
    """TrackerBomResult.EscapeCsv (cs:79-85)."""
    if text is None:
        return ""
    if "," in text or '"' in text or "\n" in text:
        return '"' + text.replace('"', '""') + '"'
    return text


# --------------------------------------------------------------------------- #
# tracker-bom-xlsx-export (Terrain/TrackerBomXlsxExporter.cs)
# --------------------------------------------------------------------------- #
class PileRecord:
    """PileRecord (TrackerBomXlsxExporter.cs:12-35)."""

    __slots__ = ("pile_index", "row_index", "x", "y", "terrain_z",
                 "pile_height_above_grade")

    def __init__(self, pile_index=0, row_index=0, x=0.0, y=0.0, terrain_z=0.0,
                 pile_height_above_grade=0.0):
        self.pile_index = pile_index
        self.row_index = row_index
        self.x = x
        self.y = y
        self.terrain_z = terrain_z
        self.pile_height_above_grade = pile_height_above_grade


class HarnessExtensionSummary:
    """HarnessExtensionSummary (TrackerBomXlsxExporter.cs:42-50)."""

    __slots__ = ("type", "cable_gauge_awg", "drop_length_m", "count")

    def __init__(self, type, cable_gauge_awg, drop_length_m, count):
        self.type = type
        self.cable_gauge_awg = cable_gauge_awg
        self.drop_length_m = drop_length_m
        self.count = count

    @property
    def total_length_m(self):
        return self.drop_length_m * self.count


def generate_pile_coordinates(rows, pile_spacing_m, terrain=None,
                              pile_height_above_grade_m=1.2):
    """TrackerBomXlsxExporter.GeneratePileCoordinates (cs:124-173).

    Piles land at BOTH endpoints: `max(2, ceil(lenDu / spacingDu) + 1)` of them,
    evenly spaced, which is deliberately a different count from the BOM's
    bucket formula. `terrain` is any object with `interpolate_z(x, y)`; None
    leaves every TerrainZ at zero, which is the branch the demos exercise.
    """
    if rows is None:
        raise BomArgumentError("rows must not be null")
    if pile_spacing_m <= 0.0:
        raise BomArgumentError("pileSpacingM must be > 0.")
    piles = []
    next_index = 1
    for row in rows:
        dx = row.axis_end[0] - row.axis_start[0]
        dy = row.axis_end[1] - row.axis_start[1]
        len_du = math.sqrt(dx * dx + dy * dy)
        if len_du < 1e-9:
            continue
        len_m = row.physical_length_meters
        mpu = len_m / len_du                      # metres per drawing unit
        spacing_du = pile_spacing_m / mpu
        pile_count = max(2, math.ceil(len_du / spacing_du) + 1)
        step = len_du / (pile_count - 1)
        ux = dx / len_du
        uy = dy / len_du
        for index in range(pile_count):
            t = index * step
            px = row.axis_start[0] + ux * t
            py = row.axis_start[1] + uy * t
            z = terrain.interpolate_z(px, py) if terrain is not None else 0.0
            piles.append(PileRecord(pile_index=next_index, row_index=row.row_index,
                                    x=px, y=py, terrain_z=z,
                                    pile_height_above_grade=pile_height_above_grade_m))
            next_index += 1
    return piles


def aggregate_harness_extensions(harnesses):
    """TrackerBomXlsxExporter.AggregateHarnessExtensions (cs:272-312).

    One bucket per (Type, Gauge, drop length quantised to 1 mm), sorted by the
    type NAME ordinally, then gauge, then drop length. The quantisation is what
    groups drops an integer-times-pitch formula produced despite float drift.
    """
    if harnesses is None:
        raise BomArgumentError("harnesses must not be null")
    buckets = {}
    for harness in harnesses:
        if harness is None or harness.module_drop_lengths_m is None:
            continue
        for length in harness.module_drop_lengths_m:
            key = (harness.type, harness.cable_gauge_awg,
                   int(_round_half_even(length * 1000.0)))
            buckets[key] = buckets.get(key, 0) + 1
    summaries = [HarnessExtensionSummary(type=key[0], cable_gauge_awg=key[1],
                                         drop_length_m=key[2] / 1000.0, count=count)
                 for key, count in buckets.items()]
    summaries.sort(key=lambda entry: (entry.type, entry.cable_gauge_awg,
                                      entry.drop_length_m))
    return summaries


def _bom_rows(lines):
    """The shared Category/Description/Unit/Quantity block (cs:380-395)."""
    return [[line.category, line.description, line.unit, line.quantity] for line in lines]


def build_bom_workbook(bom, rows, piles=None, harnesses=None):
    """TrackerBomXlsxExporter.Write (cs:91-114) as the seven (name, rows) tabs.

    `piles` and `harnesses` keep the C# overload's NULL-versus-EMPTY distinction,
    which the EMPTY demo exists to pin: Cable Tray writes its placeholder for
    None and its HEADER ONLY for an empty list, while Piling routes both through
    the same placeholder. Row indices are the sheet's own, so a placeholder that
    sits under a header row keeps row 2 rather than collapsing to row 1.
    """
    if bom is None:
        raise BomArgumentError("bom must not be null")
    if rows is None:
        raise BomArgumentError("rows must not be null")
    rows = list(rows)

    overview = [["Metric", "Value", "Unit"],
                ["Total rows (sections)", bom.total_rows, "ea"],
                ["Total modules", bom.total_modules, "ea"],
                ["Total tube length", bom.total_tube_length_m, "m"],
                ["Total piles", bom.total_piles, "ea"],
                ["Total drive units", bom.total_drive_units, "ea"],
                ["DC nameplate capacity", bom.total_dc_capacity_kwp, "kWp"]]

    # By Area: GroupBy keeps FIRST-OCCURRENCE group order and within-group order.
    grouped = []
    for category in _first_occurrence_order(line.category for line in bom.lines):
        grouped.extend(line for line in bom.lines if line.category == category)
    by_area = [list(BOM_HEADER)] + _bom_rows(grouped)

    modules = [list(BOM_HEADER)] + _bom_rows(
        [line for line in bom.lines if line.category == "Module"])

    if harnesses is None:
        cable_tray = [[CABLE_TRAY_PLACEHOLDER]]
    else:
        cable_tray = [list(CABLE_TRAY_HEADER)]
        for entry in aggregate_harness_extensions(harnesses):
            cable_tray.append([entry.type, entry.cable_gauge_awg, entry.drop_length_m,
                               entry.count, entry.drop_length_m * entry.count])

    layout = [list(LAYOUT_HEADER)]
    for row in rows:
        layout.append([row.row_index, row.axis_start[0], row.axis_start[1],
                       row.axis_end[0], row.axis_end[1], row.length_meters,
                       row.rail_overhang_m, row.physical_length_meters,
                       row.module_slots])

    electrical_lines = [line for line in bom.lines if line.category == "DC Capacity"]
    electrical = [list(BOM_HEADER)]
    if electrical_lines:
        electrical.extend(_bom_rows(electrical_lines))
    else:
        electrical.append([ELECTRICAL_PLACEHOLDER])

    piling = [list(PILING_HEADER)]
    if not piles:
        piling.append([PILING_PLACEHOLDER])
    else:
        for pile in piles:
            piling.append([pile.pile_index, pile.row_index, pile.x, pile.y,
                           pile.terrain_z, pile.pile_height_above_grade])

    tabs = (overview, by_area, modules, cable_tray, layout, electrical, piling)
    return [(name, [(index, cells) for index, cells in enumerate(tab, start=1)])
            for name, tab in zip(TAB_NAMES, tabs)]


def _first_occurrence_order(values):
    """LINQ GroupBy's key order: first occurrence, never sorted."""
    seen = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen


# --------------------------------------------------------------------------- #
# The harness-plan CSV rendering (Terrain/HarnessPlanDemoCommand.cs:224-306)
# --------------------------------------------------------------------------- #
HARNESS_PLAN_HEADER = ("Label|Type|ModuleCount|ModulePitchM|TrunkTapFromStartM|"
                       "StringMaxAmps|IsTrackerRow|ResultType|CableGaugeAwg|"
                       "Connectors|TotalCableLengthM|Drops")


def sanitize_message(message):
    """HarnessPlanDemoCommand.SanitizeMessage (cs:294-306).

    Keeps printable ASCII only, turning the delimiter and any line break into a
    space and DROPPING everything outside 32..126. That drop is why the capture
    reads "tracker row  fixed-tilt" with two spaces.
    """
    if not message:
        return ""
    out = []
    for char in message:
        if char in ("|", "\r", "\n"):
            out.append(" ")
            continue
        if 32 <= ord(char) < 127:
            out.append(char)
    return "".join(out)


def format_drops(drops):
    """HarnessPlanDemoCommand.FormatDrops (cs:282-292): "R" values joined by ';'."""
    if not drops:
        return ""
    return ";".join(calcs.format_roundtrip(drop) for drop in drops)


def harness_plan_row(label, spec):
    """One CSV line for one scenario, OK or ERROR (HarnessPlanDemoCommand.AppendRow).

    The demo catches only InvalidOperationException, so an argument refusal is
    NOT a row: it would propagate and abort the file, exactly as it does in C#.
    """
    prefix = "|".join((
        label,
        spec.requested_type,
        "%d" % spec.module_count,
        calcs.format_roundtrip(spec.module_pitch_m),
        calcs.format_roundtrip(spec.trunk_tap_from_start_m),
        calcs.format_roundtrip(spec.string_max_amps),
        "True" if spec.is_tracker_row else "False"))
    try:
        planned = plan(spec)
    except HarnessSupportError as error:
        result_type = ("ERROR_AMPS" if "exceeds the largest" in str(error)
                       else "ERROR_SUPPORT")
        return "|".join((prefix, result_type, "", "", "", sanitize_message(str(error))))
    return "|".join((
        prefix, "OK",
        "%d" % planned.cable_gauge_awg,
        "%d" % planned.connectors,
        calcs.format_roundtrip(planned.total_cable_length_m),
        format_drops(planned.module_drop_lengths_m)))


def harness_plan_csv(scenarios):
    """The whole leafharnessplan_demo.csv text: header, one row each, CRLF throughout."""
    lines = [HARNESS_PLAN_HEADER]
    lines.extend(harness_plan_row(label, spec) for label, spec in scenarios)
    return "".join(line + CRLF for line in lines)
