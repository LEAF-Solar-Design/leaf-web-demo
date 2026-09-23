#!/usr/bin/env python3
"""Studio's copies of four licensed DEMO scenario lists, and their probe files.

Each LEAF*DEMO command named below runs a FIXED fixture through one plugin
engine and writes a file to %TEMP%. This module holds Studio's own copy of those
fixtures and runs them through server/solar_harness_bom.py, emitting files in
the plugin's shape: the CSV byte for byte (pipe-separated, ASCII, CRLF, trailing
newline) and the workbooks through server/solar_xlsx.py, whose cell VALUES are
what rule E5 compares.

Fixtures ported from, read 2026-09-22 at C:/tmp/solar-parity/wt-b25-s17:

  * Terrain/HarnessPlanDemoCommand.cs:112-222   -> leafharnessplan_demo.csv
  * Terrain/HarnessBomDemoCommand.cs:74-140     -> leafharness_bom_demo.xlsx
  * Terrain/BomXlsxDemoCommand.cs:61-106        -> leafbomxlsx_demo.xlsx
  * Terrain/BomXlsxEmptyDemoCommand.cs:85-150   -> leafbomxlsxempty_demo_f1.xlsx
                                                   leafbomxlsxempty_demo_f2.xlsx

Contract: the fixture is the FIXTURE (joint identity contract v5 rule E1), so it
is written here as literal inputs and NEVER read from the plugin's output. Every
number, every rendered string and every aggregation in a probe file is COMPUTED
by server/solar_harness_bom.py; nothing is copied from the licensed capture.

The unit of output is a FILE, not a demo, because the EMPTY demo writes two of
them: `file_names(demo)` returns every file a demo writes and the capability and
probe-type maps below are keyed by file name.

No network, no dependencies outside the standard library.

Usage:
    python scripts/solar_harness_bom_probes.py --out-dir <dir> --all
    python scripts/solar_harness_bom_probes.py --out-dir <dir> --demo leafharnessplan
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


engine = _load("solar_harness_bom", ROOT / "server" / "solar_harness_bom.py")
xlsx = _load("solar_xlsx", ROOT / "server" / "solar_xlsx.py")


def _string_input(harness_type, module_count, pitch_m, tap_m, amps, is_tracker):
    return engine.HarnessStringInput(
        module_count=module_count, module_pitch_m=pitch_m,
        trunk_tap_from_start_m=tap_m, string_max_amps=amps,
        is_tracker_row=is_tracker, requested_type=harness_type)


# --------------------------------------------------------------------------- #
# LEAFHARNESSPLANDEMO -- HarnessCalculator.Plan
# (HarnessPlanDemoCommand.BuildScenarios, cs:112-222)
#   S0..S9  the ampacity ladder swept on both sides of every boundary
#   A1..A4  EndOfRow drop shapes, the last on a fixed-tilt row
#   B1..B2  ParallelTrunk, B2 mirroring A2 so the delegation is provable
#   C1..C4  Motor drop-to-mid, including the interior-minimum tap
#   E1..E3  the three refusals: parallel and motor on fixed-tilt, over-ladder amps
# --------------------------------------------------------------------------- #
HARNESSPLAN_SWEEP_AMPS = (0.0, 30.0, 30.001, 55.0, 55.001,
                          75.0, 75.001, 95.0, 95.001, 130.0)

HARNESSPLAN_SCENARIOS = tuple(
    ("S%d" % index, _string_input(engine.END_OF_ROW, 1, 1.0, 0.0, amps, True))
    for index, amps in enumerate(HARNESSPLAN_SWEEP_AMPS)
) + (
    ("A1", _string_input(engine.END_OF_ROW, 10, 1.0, 0.0, 10.0, True)),
    ("A2", _string_input(engine.END_OF_ROW, 10, 1.0, 3.0, 20.0, True)),
    ("A3", _string_input(engine.END_OF_ROW, 10, 1.5, 4.5, 30.0, True)),
    ("A4", _string_input(engine.END_OF_ROW, 8, 2.0, 7.0, 40.0, False)),
    ("B1", _string_input(engine.PARALLEL_TRUNK, 10, 1.0, 4.5, 40.0, True)),
    ("B2", _string_input(engine.PARALLEL_TRUNK, 10, 1.0, 3.0, 20.0, True)),
    ("C1", _string_input(engine.MOTOR, 10, 1.0, 0.0, 60.0, True)),
    ("C2", _string_input(engine.MOTOR, 10, 1.0, 4.5, 75.0, True)),
    ("C3", _string_input(engine.MOTOR, 8, 2.0, 7.0, 75.001, True)),
    ("C4", _string_input(engine.MOTOR, 10, 1.0, 0.0, 120.0, True)),
    ("E1", _string_input(engine.PARALLEL_TRUNK, 10, 1.0, 4.5, 40.0, False)),
    ("E2", _string_input(engine.MOTOR, 10, 1.0, 0.0, 60.0, False)),
    ("E3", _string_input(engine.END_OF_ROW, 10, 1.0, 0.0, 150.0, True)),
)

# --------------------------------------------------------------------------- #
# The shared three-row SAT fixture. BomXlsxDemoCommand (cs:61-95) and
# HarnessBomDemoCommand (cs:74-106) build the same rows and module: 3 N-S rows
# pitched 5 m apart in X, 20 m long, 10 slots each, 1.0 x 2.0 m 600 Wp module,
# piles at 5 m c/c.
# --------------------------------------------------------------------------- #
BOM_ROW_COUNT = 3
BOM_ROW_LENGTH_M = 20.0
BOM_ROW_PITCH_M = 5.0
BOM_MODULES_PER_ROW = 10
BOM_PILE_SPACING_M = 5.0


def _sat_rows(count, length_m, pitch_m, slots):
    return [engine.TrackerRow(axis_start=(index * pitch_m, 0.0),
                              axis_end=(index * pitch_m, length_m),
                              module_slots=slots, length_meters=length_m,
                              rail_overhang_m=0.0, row_index=index)
            for index in range(count)]


def _bom_module(pmax_w):
    return engine.TrackerModuleSpec(along_axis_m=1.0, cross_axis_m=2.0,
                                    gap_m=0.02, pmax_w=pmax_w)


# HarnessBomDemoCommand's harness list (cs:108-130): one EndOfRow per row at
# 20 A -> 10 AWG, plus one Motor at 60 A -> 6 AWG on the middle row.
HARNESSBOM_HARNESS_INPUTS = tuple(
    [(engine.END_OF_ROW, 20.0)] * BOM_ROW_COUNT + [(engine.MOTOR, 60.0)])

# BomXlsxEmptyDemoCommand's fixture (cs:90-113): ONE 20 m row, 10 slots, and
# PmaxW=0 so Compute skips the DC Capacity line and Electrical falls through to
# its placeholder. F1 passes null for both lists, F2 passes empty ones, and the
# Cable Tray tab is the only place those two differ.
EMPTY_FIXTURES = (("f1", None, None), ("f2", (), ()))


# --------------------------------------------------------------------------- #
# Builders. Each returns the exact CONTENT the plugin writes for one file: a
# str for the CSV, a workbook (name, rows) list for an XLSX.
# --------------------------------------------------------------------------- #
def build_harnessplan():
    return {"leafharnessplan_demo.csv": engine.harness_plan_csv(HARNESSPLAN_SCENARIOS)}


def build_harnessbom():
    rows = _sat_rows(BOM_ROW_COUNT, BOM_ROW_LENGTH_M, BOM_ROW_PITCH_M, BOM_MODULES_PER_ROW)
    bom = engine.compute_bom(rows, _bom_module(600.0), BOM_PILE_SPACING_M)
    piles = engine.generate_pile_coordinates(rows, BOM_PILE_SPACING_M, terrain=None)
    harnesses = [engine.plan(_string_input(harness_type, 10, 1.0, 0.0, amps, True))
                 for harness_type, amps in HARNESSBOM_HARNESS_INPUTS]
    return {"leafharness_bom_demo.xlsx":
            engine.build_bom_workbook(bom, rows, piles, harnesses)}


def build_bomxlsx():
    rows = _sat_rows(BOM_ROW_COUNT, BOM_ROW_LENGTH_M, BOM_ROW_PITCH_M, BOM_MODULES_PER_ROW)
    bom = engine.compute_bom(rows, _bom_module(600.0), BOM_PILE_SPACING_M)
    # The 4-arg overload: harnesses stays null, so Cable Tray keeps its placeholder.
    piles = engine.generate_pile_coordinates(rows, BOM_PILE_SPACING_M, terrain=None,
                                             pile_height_above_grade_m=1.2)
    return {"leafbomxlsx_demo.xlsx": engine.build_bom_workbook(bom, rows, piles)}


def build_bomxlsxempty():
    rows = _sat_rows(1, BOM_ROW_LENGTH_M, BOM_ROW_PITCH_M, BOM_MODULES_PER_ROW)
    bom = engine.compute_bom(rows, _bom_module(0.0), BOM_PILE_SPACING_M)
    files = {}
    for label, piles, harnesses in EMPTY_FIXTURES:
        files["leafbomxlsxempty_demo_%s.xlsx" % label] = \
            engine.build_bom_workbook(bom, rows, piles, harnesses)
    return files


DEMOS = {
    "leafharnessplan": build_harnessplan,
    "leafharnessbom": build_harnessbom,
    "leafbomxlsx": build_bomxlsx,
    "leafbomxlsxempty": build_bomxlsxempty,
}

# Every file a demo writes, in the order the command writes them.
DEMO_FILES = {
    "leafharnessplan": ("leafharnessplan_demo.csv",),
    "leafharnessbom": ("leafharness_bom_demo.xlsx",),
    "leafbomxlsx": ("leafbomxlsx_demo.xlsx",),
    "leafbomxlsxempty": ("leafbomxlsxempty_demo_f1.xlsx", "leafbomxlsxempty_demo_f2.xlsx"),
}

# The capability each FILE proves, for the probe-evidence normalizer's caller.
FILE_CAPABILITIES = {
    "leafharnessplan_demo.csv": "harness-cable-plan",
    "leafharness_bom_demo.xlsx": "harness-bom-export",
    "leafbomxlsx_demo.xlsx": "tracker-bom-xlsx-export",
    "leafbomxlsxempty_demo_f1.xlsx": "tracker-bom-xlsx-export",
    "leafbomxlsxempty_demo_f2.xlsx": "tracker-bom-xlsx-export",
}
# The probe-type key solar_probe_evidence.py reads to project a file into rows.
FILE_PROBE_TYPES = {
    "leafharnessplan_demo.csv": "harness-cable-plan",
    "leafharness_bom_demo.xlsx": "harness-bom-xlsx",
    "leafbomxlsx_demo.xlsx": "tracker-bom-xlsx",
    "leafbomxlsxempty_demo_f1.xlsx": "tracker-bom-xlsx",
    "leafbomxlsxempty_demo_f2.xlsx": "tracker-bom-xlsx",
}

# Which demo owns a file, so a caller holding one file name can rebuild it.
FILE_DEMOS = {name: demo for demo, names in DEMO_FILES.items() for name in names}


def build(demo):
    """Run one DEMO's fixture and return {file name: CSV text or workbook}."""
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


def file_bytes(name, content):
    """The exact bytes of one probe file.

    The CSV is ASCII because the plugin writes it with `Encoding.ASCII`, and its
    CRLFs are already in the text, so it is encoded here and written through
    `write_bytes`: text mode on a Windows host would translate them a second
    time and double every line ending.
    """
    if name.endswith(".csv"):
        return content.encode("ascii")
    return xlsx.write_workbook(content)


def write(demo, out_dir):
    """Write one demo's files exactly, and return their paths in file order."""
    out_dir = Path(out_dir)
    paths = []
    for name, content in build(demo).items():
        path = out_dir / name
        path.write_bytes(file_bytes(name, content))
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
        print("solar-harness-bom-probes: name --demo at least once, or pass --all",
              file=sys.stderr)
        return 2
    try:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        for demo in demos:
            for path in write(demo, args.out_dir):
                print(path)
    except (OSError, ValueError, TypeError) as exc:
        print("solar-harness-bom-probes: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
