#!/usr/bin/env python3
"""Studio's copies of four licensed DEMO scenario lists, and their probe files.

Each LEAF*DEMO command named below runs a FIXED scenario list through one plugin
engine and writes a file to %TEMP%. This module holds Studio's own copy of those
lists and runs them through server/solar_probe_calcs.py, emitting files in the
plugin's exact byte shape: same header, same column order, same number
formatting, same CRLF line endings, same trailing newline or lack of one.

Scenario lists ported from, read 2026-09-22 at C:/tmp/solar-parity/wt-b25-s17:

  * Terrain/SnakeOrderDemoCommand.cs:74-171    -> leafsnakeorder_probes.json
  * Terrain/ProjectSummaryDemoCommand.cs:137-206
        -> leafprojectsummary_demo_f1/f2/f3 .csv and .json
  * Terrain/ShadeLimitAngleDemoCommand.cs:70-168 -> leafshadelimit_demo.csv
  * Terrain/TorqueShadeDemoCommand.cs:84-127     -> leaftorqueshade_demo.csv

Contract: the scenario list is the FIXTURE (joint identity contract v5 rule E1),
so it is written here as literal inputs and NEVER read from the plugin's output.
Every number and every rendered string in a probe file is COMPUTED by
server/solar_probe_calcs.py; nothing is copied from the licensed capture.

The project summary is three FIXTURES, each emitting a CSV and a JSON, so this
module's unit of output is a FILE, not a demo: `file_names(demo)` returns every
file a demo writes and the capability and probe-type maps below are keyed by
file name, which is what lets one receipt cover each of the six.

No network, no dependencies outside the standard library.

Usage:
    python scripts/solar_probe_calcs_probes.py --out-dir <dir> --all
    python scripts/solar_probe_calcs_probes.py --out-dir <dir> --demo leaftorqueshade
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def _load_probe_calcs():
    """Load server/solar_probe_calcs.py by path so the import works from any cwd."""
    path = ROOT / "server" / "solar_probe_calcs.py"
    spec = importlib.util.spec_from_file_location("solar_probe_calcs", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


calcs = _load_probe_calcs()
CRLF = calcs.CRLF


# --------------------------------------------------------------------------- #
# LEAFSNAKEORDERDEMO -- PanelGroupTradeCalculator.SnakeOrder
# (name, panels or None), each panel (handle, X, Y, Row, Col)
# --------------------------------------------------------------------------- #
SNAKEORDER_SCENARIOS = (
    ("null_panels", None),
    ("empty_panels", ()),
    ("single_panel", (("A", 0, 0, 0, 0),)),
    # Input order [B then A] on one row; the sort must return [A, B].
    ("two_same_row_sorted", (("B", 1, 0, 0, 1), ("A", 0, 0, 0, 0))),
    ("grid_2x2", (("A", 0, 0, 0, 0), ("B", 1, 0, 0, 1),
                  ("C", 0, 1, 1, 0), ("D", 1, 1, 1, 1))),
    ("grid_2x3", (("A", 0, 0, 0, 0), ("B", 1, 0, 0, 1), ("C", 2, 0, 0, 2),
                  ("D", 0, 1, 1, 0), ("E", 1, 1, 1, 1), ("F", 2, 1, 1, 2))),
    ("three_rows_two_cols", (("A", 0, 0, 0, 0), ("B", 1, 0, 0, 1),
                             ("C", 0, 1, 1, 0), ("D", 1, 1, 1, 1),
                             ("E", 0, 2, 2, 0), ("F", 1, 2, 2, 1))),
    # Row 2 arrives first: sorted row keys must still drive the walk.
    ("rows_out_of_order_input", (("E", 0, 2, 2, 0), ("F", 1, 2, 2, 1),
                                 ("A", 0, 0, 0, 0), ("B", 1, 0, 0, 1),
                                 ("C", 0, 1, 1, 0), ("D", 1, 1, 1, 1))),
    # Row = -1, so the row key comes from BucketCoordinate over Y.
    ("row_neg_uses_y", (("A", 0, 0, -1, -1), ("B", 0, 10, -1, -1),
                        ("C", 0, 20, -1, -1))),
    # Col = -1, so the within-row sort key is X: B(2), A(5), C(8).
    ("col_neg_uses_x", (("A", 5, 0, 0, -1), ("B", 2, 0, 0, -1),
                        ("C", 8, 0, 0, -1))),
    ("irregular_row_sizes", (("A", 0, 0, 0, 0), ("B", 1, 0, 0, 1),
                             ("C", 0, 1, 1, 0), ("D", 0, 2, 2, 0),
                             ("E", 1, 2, 2, 1), ("F", 2, 2, 2, 2))),
)


# --------------------------------------------------------------------------- #
# LEAFPROJECTSUMMARYDEMO -- ProjectSummary.ToCsv / ToJson
# --------------------------------------------------------------------------- #
def _fixture_f1_empty():
    """All defaults: the header-plus-eleven-rows branch and zero-precision F3/F2."""
    return calcs.ProjectSummary()


def _fixture_f2_mixed():
    """StringsByLength inserted 16/12/14 so the CSV's ascending sort (12/14/16)
    visibly diverges from the JSON's insertion order."""
    summary = calcs.ProjectSummary(
        panel_count=168, inverter_count=3, string_count=12,
        total_dc_kwp=84.672, total_ac_kw=66.000, dc_ac_ratio=1.283,
        total_trunk_length=425.75, total_branch_length=1240.33,
        critical_findings=1, warning_findings=2, info_findings=5)
    summary.strings_by_length[16] = 3
    summary.strings_by_length[12] = 5
    summary.strings_by_length[14] = 4
    summary.largest_cable_sizes = ["10 AWG x8", "8 AWG x3", "6 AWG x1"]
    return summary


def _fixture_f3_null():
    """A null cable size in the middle: EMPTY in the CSV, JSON null in the JSON."""
    summary = calcs.ProjectSummary(
        panel_count=42, inverter_count=1, string_count=3,
        total_dc_kwp=15.120, total_ac_kw=12.500, dc_ac_ratio=1.210,
        total_trunk_length=50.00, total_branch_length=120.00,
        critical_findings=0, warning_findings=1, info_findings=2)
    summary.strings_by_length[14] = 3
    summary.largest_cable_sizes = ["4 AWG x2", None, "2 AWG x1"]
    return summary


PROJECTSUMMARY_FIXTURES = (
    ("f1", _fixture_f1_empty),
    ("f2", _fixture_f2_mixed),
    ("f3", _fixture_f3_null),
)


# --------------------------------------------------------------------------- #
# LEAFSHADELIMITDEMO -- ShadeLimitAngleCalculator + BacktrackingCalculator
# --------------------------------------------------------------------------- #
SHADELIMIT_CROSS_AXIS_M = 2.1
# Block A: external anchors derivable from trig identities, not from the
# implementation formula. (name, gcr, tiltDeg, expectedSlaDeg)
SHADELIMIT_ANCHORS = (
    ("Anchor_GCR_0.5_Tilt_60", 0.5, 60.0, 30.0),
    ("Anchor_GCR_InvRoot2_Tilt_45", 1.0 / calcs.math.sqrt(2.0), 45.0, 45.0),
    ("Anchor_GCR_Root3Over2_Tilt_30", calcs.math.sqrt(3.0) / 2.0, 30.0, 60.0),
)
# Block B: the forward-inverse round-trip grid, held strictly inside the domain
# where MaxGcrForSla stays below 1.
SHADELIMIT_SLA_TARGETS = (10.0, 20.0, 30.0, 40.0, 50.0)
SHADELIMIT_MAX_TILTS = (30.0, 45.0, 60.0)
# Block C: AutoCalculate at Boulder (40 N, -105 W) solstice solar noon.
SHADELIMIT_LAT_DEG = 40.0
SHADELIMIT_LON_DEG = -105.0
SHADELIMIT_MAX_TILT_DEG = 60.0
SHADELIMIT_AUTOCALCS = (
    ("AutoCalc_Boulder_WinterNoon_2025", calcs.utc(2025, 12, 21, 19, 0, 0)),
    ("AutoCalc_Boulder_SummerNoon_2025", calcs.utc(2025, 6, 21, 19, 0, 0)),
)


# --------------------------------------------------------------------------- #
# LEAFTORQUESHADEDEMO -- TorqueTubeShadeCalculator.ComputeRearSelfShadeFraction
# (label, radiusM, topGapM, botGapM, crossAxisM, tiltDeg)
# --------------------------------------------------------------------------- #
TORQUESHADE_SCENARIOS = (
    # Zero short-circuit: R = 0 and both gaps 0 returns 0 at any tilt.
    ("Z1", 0.00, 0.00, 0.00, 2.0, 0.0),
    ("Z2", 0.00, 0.00, 0.00, 2.0, 45.0),
    ("Z3", 0.00, 0.00, 0.00, 2.0, 89.0),
    # Tilt = 0, where sin(0) masks the gap contribution.
    ("T0R", 0.08, 0.00, 0.00, 2.0, 0.0),
    ("T0G", 0.00, 0.10, 0.10, 2.0, 0.0),
    ("T0B", 0.08, 0.05, 0.05, 2.0, 0.0),
    # Monotonicity in positive tilt.
    ("M30", 0.04, 0.10, 0.10, 2.0, 30.0),
    ("M60", 0.04, 0.10, 0.10, 2.0, 60.0),
    ("M89", 0.04, 0.10, 0.10, 2.0, 89.0),
    # Symmetry in the sign of tilt, and the exact sin = 1 case.
    ("P30", 0.08, 0.05, 0.05, 2.0, 30.0),
    ("N30", 0.08, 0.05, 0.05, 2.0, -30.0),
    ("P89", 0.08, 0.05, 0.05, 2.0, 89.0),
    ("N89", 0.08, 0.05, 0.05, 2.0, -89.0),
    ("P90", 0.08, 0.05, 0.05, 2.0, 90.0),
    # Gap commutativity.
    ("GA", 0.05, 0.10, 0.02, 2.0, 45.0),
    ("GB", 0.05, 0.02, 0.10, 2.0, 45.0),
    # CrossAxisM inverse-proportional scaling.
    ("C1", 0.08, 0.05, 0.05, 1.0, 45.0),
    ("C2", 0.08, 0.05, 0.05, 2.0, 45.0),
    ("C3", 0.08, 0.05, 0.05, 3.0, 45.0),
    # Clamp to 1.0.
    ("XR", 5.00, 0.00, 0.00, 2.0, 0.0),
    ("XG", 0.08, 5.00, 5.00, 2.0, 90.0),
    ("XE", 0.50, 1.00, 1.00, 2.0, 30.0),
    # Just under the clamp.
    ("BC", 0.50, 0.50, 0.50, 2.0, 89.9),
    # CrossAxisM = 0 guard.
    ("XZ", 0.08, 0.05, 0.05, 0.0, 45.0),
)


# --------------------------------------------------------------------------- #
# Builders. Each returns the exact TEXT the plugin writes for one file.
# --------------------------------------------------------------------------- #
def build_snakeorder():
    """SnakeOrderDemoCommand.AddProbe, including its reference-equality flag.

    `Unchanged` is `object.ReferenceEquals(input, output)`, which SnakeOrder makes
    true only by RETURNING ITS ARGUMENT for a null or single-element list; the
    port returns the same list object, so `is` carries the same fact.
    """
    probes = []
    for name, panels in SNAKEORDER_SCENARIOS:
        built = None if panels is None else [calcs.TradePanel(*panel) for panel in panels]
        output = calcs.snake_order(built)
        probes.append({
            "Name": name,
            "Tag": name,
            "InputIsNull": built is None,
            "InputPanels": None if built is None else [
                {"Handle": panel.handle, "X": panel.x, "Y": panel.y,
                 "Row": panel.row, "Col": panel.col} for panel in built],
            "OutputHandles": None if output is None else [panel.handle for panel in output],
            "Unchanged": output is built,
        })
    # Newtonsoft's Formatting.Indented: two spaces, CRLF, no trailing newline.
    return json.dumps(probes, indent=2, ensure_ascii=False,
                      allow_nan=False).replace("\n", CRLF)


def build_projectsummary():
    """Six files from three fixtures: one CSV and one JSON each."""
    files = {}
    for label, factory in PROJECTSUMMARY_FIXTURES:
        summary = factory()
        files["leafprojectsummary_demo_" + label + ".csv"] = \
            calcs.project_summary_to_csv(summary)
        files["leafprojectsummary_demo_" + label + ".json"] = \
            calcs.project_summary_to_json(summary)
    return files


def build_shadelimit():
    """ShadeLimitAngleDemoCommand's three blocks, in its own format strings.

    The commented preamble is part of the file the validator reads, so it is
    reproduced exactly; `# cross_axis_m={0}` renders the cross axis through C#'s
    default double format, which is what `format_roundtrip` gives for 2.1.
    """
    lines = [
        "# LEAFSHADELIMITDEMO Q2 Shade Limit Angle forward/inverse live-smoke",
        "# cross_axis_m=" + calcs.format_roundtrip(SHADELIMIT_CROSS_AXIS_M)
        + " (matches Q10/loop-11 fixture)",
        "# block=A: external anchors (Lorenzo 2011 Eq.6 + trig identity)",
        "# block=B: forward-inverse round-trip grid",
        "# block=C: AutoCalculate Boulder solstice anchors",
        "block,label,gcr_in,tilt_deg,sla_target_deg,max_gcr,sla_reconstructed_deg,"
        "min_pitch_m,sun_elev_deg,sun_az_deg,is_daytime",
    ]
    fixed = calcs.format_fixed

    for name, gcr, tilt_deg, expected_sla_deg in SHADELIMIT_ANCHORS:
        sla_forward = calcs.compute_shade_limit_angle(gcr, tilt_deg)
        max_gcr_round = calcs.max_gcr_for_sla(expected_sla_deg, tilt_deg)
        sla_recon = calcs.compute_shade_limit_angle(max_gcr_round, tilt_deg)
        min_pitch = calcs.min_pitch_for_sla(expected_sla_deg, SHADELIMIT_CROSS_AXIS_M,
                                            tilt_deg)
        lines.append("A,%s,%s,%s,%s,%s,%s,%s,,," % (
            name, fixed(gcr, 12), fixed(tilt_deg, 6), fixed(sla_forward, 12),
            fixed(max_gcr_round, 12), fixed(sla_recon, 12), fixed(min_pitch, 9)))

    for tilt_deg in SHADELIMIT_MAX_TILTS:
        for sla_target in SHADELIMIT_SLA_TARGETS:
            max_gcr = calcs.max_gcr_for_sla(sla_target, tilt_deg)
            sla_recon = calcs.compute_shade_limit_angle(max_gcr, tilt_deg)
            min_pitch = calcs.min_pitch_for_sla(sla_target, SHADELIMIT_CROSS_AXIS_M,
                                                tilt_deg)
            lines.append("B,RoundTrip_SLA%s_Tilt%s,,%s,%s,%s,%s,%s,,," % (
                fixed(sla_target, 0), fixed(tilt_deg, 0), fixed(tilt_deg, 6),
                fixed(sla_target, 12), fixed(max_gcr, 12), fixed(sla_recon, 12),
                fixed(min_pitch, 9)))

    for name, design_utc in SHADELIMIT_AUTOCALCS:
        result = calcs.shade_limit_auto_calculate(
            SHADELIMIT_LAT_DEG, SHADELIMIT_LON_DEG, design_utc,
            SHADELIMIT_CROSS_AXIS_M, SHADELIMIT_MAX_TILT_DEG)
        # The command substitutes NaN for an infinite pitch rather than printing
        # C#'s "Infinity", so an unreachable pitch still renders as a number-ish
        # token the validator can recognise.
        min_pitch = (calcs.math.nan if calcs.math.isinf(result.min_pitch_m)
                     else result.min_pitch_m)
        lines.append("C,%s,,60.000000,%s,%s,,%s,%s,%s,%d" % (
            name, fixed(result.sun_elevation_deg, 12), fixed(result.max_gcr, 12),
            fixed(min_pitch, 9), fixed(result.sun_elevation_deg, 6),
            fixed(result.sun_azimuth_deg, 6), 1 if result.is_daytime else 0))

    return "".join(line + CRLF for line in lines)


def build_torqueshade():
    """TorqueShadeDemoCommand's 24 scenarios, every column in C#'s "R" format."""
    lines = ["Label,RadiusM,TopGapM,BotGapM,CrossAxisM,TiltDeg,Fraction"]
    for label, radius, top_gap, bot_gap, cross_axis, tilt_deg in TORQUESHADE_SCENARIOS:
        fraction = calcs.rear_self_shade_fraction(cross_axis, radius, top_gap,
                                                  bot_gap, tilt_deg)
        lines.append(",".join((
            label,
            calcs.format_roundtrip(radius), calcs.format_roundtrip(top_gap),
            calcs.format_roundtrip(bot_gap), calcs.format_roundtrip(cross_axis),
            calcs.format_roundtrip(tilt_deg), calcs.format_roundtrip(fraction))))
    return "".join(line + CRLF for line in lines)


DEMOS = {
    "leafsnakeorder": lambda: {"leafsnakeorder_probes.json": build_snakeorder()},
    "leafprojectsummary": build_projectsummary,
    "leafshadelimit": lambda: {"leafshadelimit_demo.csv": build_shadelimit()},
    "leaftorqueshade": lambda: {"leaftorqueshade_demo.csv": build_torqueshade()},
}

# Every file a demo writes, in the order the command writes them.
DEMO_FILES = {
    "leafsnakeorder": ("leafsnakeorder_probes.json",),
    "leafprojectsummary": tuple(
        "leafprojectsummary_demo_%s.%s" % (label, extension)
        for label, _ in PROJECTSUMMARY_FIXTURES for extension in ("csv", "json")),
    "leafshadelimit": ("leafshadelimit_demo.csv",),
    "leaftorqueshade": ("leaftorqueshade_demo.csv",),
}

# The capability each FILE proves, for the probe-evidence normalizer's caller.
FILE_CAPABILITIES = {
    "leafsnakeorder_probes.json": "panel-snake-order",
    "leafshadelimit_demo.csv": "shade-limit-angle",
    "leaftorqueshade_demo.csv": "torque-tube-rear-shade",
}
# The probe-type key solar_probe_evidence.py reads to project a file into rows.
FILE_PROBE_TYPES = {
    "leafsnakeorder_probes.json": "panel-snake-order",
    "leafshadelimit_demo.csv": "shade-limit-angle",
    "leaftorqueshade_demo.csv": "torque-tube-rear-shade",
}
for _label, _ in PROJECTSUMMARY_FIXTURES:
    FILE_CAPABILITIES["leafprojectsummary_demo_%s.csv" % _label] = \
        "project-summary-export-csv-json"
    FILE_CAPABILITIES["leafprojectsummary_demo_%s.json" % _label] = \
        "project-summary-export-csv-json"
    FILE_PROBE_TYPES["leafprojectsummary_demo_%s.csv" % _label] = "project-summary-csv"
    FILE_PROBE_TYPES["leafprojectsummary_demo_%s.json" % _label] = "project-summary-json"
del _label

# Which demo owns a file, so a caller holding one file name can rebuild it.
FILE_DEMOS = {name: demo for demo, names in DEMO_FILES.items() for name in names}


def build(demo):
    """Run one DEMO's scenario list and return {file name: exact file text}."""
    if demo not in DEMOS:
        raise ValueError("unknown demo: " + str(demo))
    files = DEMOS[demo]()
    if tuple(files) != DEMO_FILES[demo]:
        raise ValueError("demo wrote unexpected files: " + str(sorted(files)))
    return files


def file_names(demo):
    if demo not in DEMO_FILES:
        raise ValueError("unknown demo: " + str(demo))
    return DEMO_FILES[demo]


def write(demo, out_dir):
    """Write one demo's files exactly, and return their paths in file order.

    The bytes are written through `write_bytes`, never through text mode: the
    line endings are already the plugin's CRLF and a second translation on a
    Windows host would double every one of them.
    """
    out_dir = Path(out_dir)
    paths = []
    for name, text in build(demo).items():
        path = out_dir / name
        path.write_bytes(text.encode("utf-8"))
        paths.append(path)
    return paths


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--demo", action="append", choices=sorted(DEMOS), default=None)
    parser.add_argument("--all", action="store_true", help="run every demo")
    args = parser.parse_args(argv)
    demos = sorted(DEMOS) if args.all else (args.demo or [])
    if not demos:
        print("solar-probe-calcs-probes: name --demo at least once, or pass --all",
              file=sys.stderr)
        return 2
    try:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        for demo in demos:
            for path in write(demo, args.out_dir):
                print(path)
    except (OSError, ValueError, TypeError) as exc:
        print("solar-probe-calcs-probes: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
