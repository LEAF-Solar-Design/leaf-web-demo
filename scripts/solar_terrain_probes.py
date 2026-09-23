#!/usr/bin/env python3
"""Studio's copies of six licensed terrain DEMO scenario lists, and their probe files.

Each LEAF*DEMO command named below runs a FIXED fixture through one plugin
engine and writes a CSV to %TEMP%. This module holds Studio's own copy of those
fixtures and runs them through server/solar_terrain.py, emitting each file in
the plugin's exact bytes: its encoding (ASCII, UTF-8 with or without a byte
order mark), its `#` preamble, CRLF after every line and no quoting.

Fixtures ported from, read 2026-09-23 at C:/tmp/solar-parity/wt-b25-s17:

  * Terrain/HeatmapDemoCommand.cs:84-206            -> leafheatmap_demo.csv
  * Terrain/HorizonProfileCommand.cs:289-356        -> leafhorizon_demo.csv
      (LEAFHORIZONDEMOCSV at its default azimuth step of 10 degrees, the value
      an accoreconsole run accepts at the prompt)
  * Terrain/InPlaneIrradianceDemoCommand.cs:100-165 -> leafpoa_demo.csv
  * Terrain/GradingPadDemoCommand.cs:76-133         -> leafgradingpad_demo.csv
  * Terrain/CrossSectionDemoCommand.cs:78-133       -> leafcrosssection_demo.csv
  * Terrain/CapacityIterationDemoCommand.cs:54-83   -> leafcapacity_demo.csv

Contract: the fixture is the FIXTURE (joint identity contract v5 rule E1), so it
is written here as literal inputs and NEVER read from the plugin's output. Every
number and every rendered string in a probe file is COMPUTED by
server/solar_terrain.py; nothing is copied from the licensed capture.

No network, no dependencies outside the standard library.

Usage:
    python scripts/solar_terrain_probes.py --out-dir <dir> --all
    python scripts/solar_terrain_probes.py --out-dir <dir> --demo leafheatmap
"""

from __future__ import annotations

import argparse
import importlib.util
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


engine = _load("solar_terrain", ROOT / "server" / "solar_terrain.py")


# --------------------------------------------------------------------------- #
# LEAFHEATMAPDEMO (HeatmapDemoCommand.cs:84-206). Every fixture sits on
# [0,100] x [0,100] with the proposed floor at 1.0 m, and each node stores
# existingZ = 1.0 + target, so a uniform fixture's delta is (1.0 + t) - 1.0.
# --------------------------------------------------------------------------- #
HEATMAP_EXTENT = (0.0, 100.0, 0.0, 100.0)
HEATMAP_PROPOSED_ELEV_M = 1.0
HEATMAP_UNIFORM = (
    ("F1_AT_GRADE_ZERO", 0.000),
    ("F2_NEUTRAL_BAND_UNDER", 0.049),
    ("F3_NEUTRAL_BAND_OVER", 0.050),
    ("F4_LIGHT_CUT", 0.300),
    ("F5_LIGHT_FILL", -0.300),
    ("F6_MID_CUT", 1.500),
    ("F7_MID_FILL", -1.500),
    ("F8_SAT_CUT", 3.000),
    ("F9_SAT_FILL", -3.000),
    ("F10_OVERSAT_CUT", 5.000),
)
# F11 (cs:176-181), row-major existing elevations on a 3x3 grid.
HEATMAP_MIXED = ("F11_3X3_MIXED", 3, 3, (1.00, 1.30, 0.70,
                                          0.97, 1.00, 4.00,
                                          6.00, 0.70, 1.03))

# --------------------------------------------------------------------------- #
# LEAFHORIZONDEMOCSV (HorizonProfileCommand.cs:289-356): a 41x41 zero grid on
# [-200,200]^2 with one 5 m node at row 30, col 20, i.e. (0, +100); observer at
# (0, 0) with no eye height; ray step 1 du; azimuth step 10 degrees.
# --------------------------------------------------------------------------- #
HORIZON_GRID = (41, 41, -200.0, 200.0, -200.0, 200.0)
HORIZON_PEAK = (30, 20, 5.0)
HORIZON_AZIMUTH_STEP_DEG = 10.0
HORIZON_RAY_STEP_DU = 1.0

# --------------------------------------------------------------------------- #
# LEAFPOADEMO (InPlaneIrradianceDemoCommand.cs:101-127): (id, zenith, solar
# azimuth, tilt, surface azimuth, DNI, DHI, albedo, day of year).
# --------------------------------------------------------------------------- #
POA_FIXTURES = (
    ("F1_CLEAR_NOON", 30.0, 180.0, 30.0, 180.0, 900.0, 100.0, 0.20, 81),
    ("F2_OVERCAST", 30.0, 180.0, 30.0, 180.0, 0.0, 300.0, 0.20, 81),
    ("F3_LOW_SUN", 80.0, 270.0, 30.0, 180.0, 400.0, 150.0, 0.20, 172),
    ("F4_STEEP_TILT", 30.0, 180.0, 75.0, 180.0, 900.0, 100.0, 0.20, 81),
    ("F5_PARTLY", 45.0, 200.0, 30.0, 180.0, 500.0, 200.0, 0.20, 172),
    ("F6_DHI_ZERO", 30.0, 180.0, 30.0, 180.0, 900.0, 0.0, 0.20, 81),
)

# --------------------------------------------------------------------------- #
# LEAFGRADINGPADDEMO (GradingPadDemoCommand.cs:78-133): z = 0.05 x on a 31x31
# grid over [0,30]^2, pad [10,10]-[20,20] at 1.5 m, slope 2H:1V.
# --------------------------------------------------------------------------- #
GRADING_GRID = (31, 31, 0.0, 30.0, 0.0, 30.0)
GRADING_SLOPE_X_PER_M = 0.05
GRADING_PAD = (10.0, 10.0, 20.0, 20.0)
GRADING_PAD_ELEV_M = 1.5
GRADING_SLOPE_RATIO_H = 2.0

# --------------------------------------------------------------------------- #
# LEAFCROSSSECTIONDEMO (CrossSectionDemoCommand.cs:79-133): a 41x41 separable
# sinusoid, 1 m amplitude, 200 m wavelength, on [-200,200]^2, cut from
# (-190,-150) to (190,170) at 100 samples.
# --------------------------------------------------------------------------- #
CROSS_GRID = (41, 41, -200.0, 200.0, -200.0, 200.0)
CROSS_AMPLITUDE_M = 1.0
CROSS_WAVELENGTH_M = 200.0
CROSS_START = (-190.0, -150.0)
CROSS_END = (190.0, 170.0)
CROSS_SAMPLE_COUNT = 100

# --------------------------------------------------------------------------- #
# LEAFCAPACITYITERATEDEMO (CapacityIterationDemoCommand.cs:55-83): a 100 x 50 m
# rectangle, a 1.0 x 2.1 m 600 Wp module with a 0.02 m gap and no overhang or
# corridors, GCR 0.30..0.50 in 5 steps x tilt 20..50 in 2 steps, cap 100.
# --------------------------------------------------------------------------- #
CAPACITY_BOUNDARY = ((0.0, 0.0), (100.0, 0.0), (100.0, 50.0), (0.0, 50.0))
CAPACITY_GCR = (0.30, 0.50, 5)
CAPACITY_TILT = (20.0, 50.0, 2)
CAPACITY_MAX_SCENARIOS = 100


def capacity_module():
    return engine.TrackerModuleSpec(along_axis_m=1.000, cross_axis_m=2.100, gap_m=0.020,
                                    rail_overhang_m=0.0, corridor_gap_m=0.0,
                                    secondary_corridor_gap_m=0.0, pmax_w=600.0)


def _grid(shape, elevation_at, meters_per_unit=1.0):
    """A rows x cols grid over its extent, elevations from (row, col, x, y)."""
    rows, cols, x_min, x_max, y_min, y_max = shape
    dx = (x_max - x_min) / (cols - 1)
    dy = (y_max - y_min) / (rows - 1)
    elevations = [elevation_at(r, c, x_min + c * dx, y_min + r * dy)
                  for r in range(rows) for c in range(cols)]
    return engine.TerrainGridInterpolator(elevations, rows, cols, x_min, x_max, y_min, y_max,
                                          meters_per_unit)


# --------------------------------------------------------------------------- #
# Builders. Each returns {file name: the exact text the plugin writes}.
# --------------------------------------------------------------------------- #
def build_heatmap():
    fixtures = []
    x_min, x_max, y_min, y_max = HEATMAP_EXTENT
    for name, target in HEATMAP_UNIFORM:
        existing = HEATMAP_PROPOSED_ELEV_M + target
        grid = engine.TerrainGridInterpolator([existing] * 4, 2, 2, x_min, x_max, y_min, y_max)
        fixtures.append((name, 2, 2, engine.compute_cells(grid, HEATMAP_PROPOSED_ELEV_M)))
    name, rows, cols, elevations = HEATMAP_MIXED
    grid = engine.TerrainGridInterpolator(elevations, rows, cols, x_min, x_max, y_min, y_max)
    fixtures.append((name, rows, cols, engine.compute_cells(grid, HEATMAP_PROPOSED_ELEV_M)))
    return {"leafheatmap_demo.csv": engine.heatmap_demo_csv(fixtures)}


def build_horizon():
    peak_row, peak_col, peak_m = HORIZON_PEAK
    grid = _grid(HORIZON_GRID,
                 lambda r, c, x, y: peak_m if (r, c) == (peak_row, peak_col) else 0.0)
    profile = engine.generate_horizon_profile(
        grid, 0.0, 0.0, observer_height_m=0.0, azimuth_step_deg=HORIZON_AZIMUTH_STEP_DEG,
        ray_step_du=HORIZON_RAY_STEP_DU)
    return {"leafhorizon_demo.csv": engine.horizon_csv(profile, 0.0, 0.0)}


def build_poa():
    rows = []
    for fixture_id, zen, sun_az, tilt, surf_az, dni, dhi, albedo, doy in POA_FIXTURES:
        # PoaFixture.Ghi (cs:88-89): the physically consistent GHI.
        ghi = dni * math.cos(zen * math.pi / 180.0) + dhi
        rows.append((fixture_id, "Isotropic", engine.compute_isotropic(
            dni, dhi, ghi, zen, sun_az, tilt, surf_az, albedo)))
        rows.append((fixture_id, "HDKR", engine.compute_hdkr(
            dni, dhi, ghi, zen, sun_az, tilt, surf_az, doy, albedo)))
        rows.append((fixture_id, "Perez", engine.compute_perez(
            dni, dhi, ghi, zen, sun_az, tilt, surf_az, doy, albedo)))
    return {"leafpoa_demo.csv": engine.poa_demo_csv(rows)}


def build_gradingpad():
    terrain = _grid(GRADING_GRID, lambda r, c, x, y: GRADING_SLOPE_X_PER_M * x)
    pad = engine.PadRectangle(*GRADING_PAD)
    # cs:123-127: the four mid-edge samples and their mean as the uniform reference.
    z_s = engine._or_zero(terrain.interpolate_z((pad.min_x + pad.max_x) * 0.5, pad.min_y))
    z_n = engine._or_zero(terrain.interpolate_z((pad.min_x + pad.max_x) * 0.5, pad.max_y))
    z_w = engine._or_zero(terrain.interpolate_z(pad.min_x, (pad.min_y + pad.max_y) * 0.5))
    z_e = engine._or_zero(terrain.interpolate_z(pad.max_x, (pad.min_y + pad.max_y) * 0.5))
    uniform_ref = 0.25 * (z_s + z_n + z_w + z_e)
    uniform = engine.compute_embankment_toes(pad, GRADING_PAD_ELEV_M, uniform_ref,
                                             GRADING_SLOPE_RATIO_H, meters_per_unit=1.0)
    per_edge = engine.compute_embankment_toes_per_edge(pad, GRADING_PAD_ELEV_M, terrain,
                                                       GRADING_SLOPE_RATIO_H,
                                                       meters_per_unit=1.0)
    return {"leafgradingpad_demo.csv": engine.grading_pad_demo_csv(
        (z_s, z_n, z_w, z_e), uniform_ref, uniform, per_edge)}


def build_crosssection():
    k = 2.0 * math.pi / CROSS_WAVELENGTH_M
    terrain = _grid(CROSS_GRID,
                    lambda r, c, x, y: CROSS_AMPLITUDE_M * math.sin(k * x) * math.cos(k * y))
    profile = engine.sample_line(terrain, CROSS_START, CROSS_END, CROSS_SAMPLE_COUNT)
    return {"leafcrosssection_demo.csv": engine.cross_section_demo_csv(
        profile, CROSS_AMPLITUDE_M, k)}


def build_capacity():
    results = engine.enumerate_capacity(
        CAPACITY_BOUNDARY, capacity_module(), engine.CapacityRange(*CAPACITY_GCR),
        engine.CapacityRange(*CAPACITY_TILT), CAPACITY_MAX_SCENARIOS)
    return {"leafcapacity_demo.csv": engine.capacity_demo_csv(results)}


DEMOS = {
    "leafheatmap": build_heatmap,
    "leafhorizon": build_horizon,
    "leafpoa": build_poa,
    "leafgradingpad": build_gradingpad,
    "leafcrosssection": build_crosssection,
    "leafcapacity": build_capacity,
}

# Every file a demo writes, in the order the command writes them.
DEMO_FILES = {
    "leafheatmap": ("leafheatmap_demo.csv",),
    "leafhorizon": ("leafhorizon_demo.csv",),
    "leafpoa": ("leafpoa_demo.csv",),
    "leafgradingpad": ("leafgradingpad_demo.csv",),
    "leafcrosssection": ("leafcrosssection_demo.csv",),
    "leafcapacity": ("leafcapacity_demo.csv",),
}

# The capability each FILE proves, for the probe-evidence normalizer's caller.
FILE_CAPABILITIES = {
    "leafheatmap_demo.csv": "cut-fill-heatmap",
    "leafhorizon_demo.csv": "horizon-profile",
    "leafpoa_demo.csv": "plane-of-array-irradiance",
    "leafgradingpad_demo.csv": "grading-pad-design",
    "leafcrosssection_demo.csv": "terrain-cross-section",
    "leafcapacity_demo.csv": "capacity-iteration-sweep",
}
# The probe-type key solar_probe_evidence.py reads to project a file into rows.
FILE_PROBE_TYPES = {
    "leafheatmap_demo.csv": "cut-fill-heatmap",
    "leafhorizon_demo.csv": "horizon-profile",
    "leafpoa_demo.csv": "plane-of-array-irradiance",
    "leafgradingpad_demo.csv": "grading-pad-design",
    "leafcrosssection_demo.csv": "terrain-cross-section",
    "leafcapacity_demo.csv": "capacity-iteration-sweep",
}
# The encoding each command passes to File.WriteAllText. Encoding.UTF8's byte
# order mark is already the first character of the text the engine renders.
FILE_ENCODINGS = {
    "leafheatmap_demo.csv": "ascii",
    "leafhorizon_demo.csv": "utf-8",
    "leafpoa_demo.csv": "utf-8",
    "leafgradingpad_demo.csv": "utf-8",
    "leafcrosssection_demo.csv": "utf-8",
    "leafcapacity_demo.csv": "utf-8",
}

# Which demo owns a file, so a caller holding one file name can rebuild it.
FILE_DEMOS = {name: demo for demo, names in DEMO_FILES.items() for name in names}


def build(demo):
    """Run one DEMO's fixture and return {file name: CSV text}."""
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

    The CRLFs and any byte order mark are already in the text, so it is encoded
    here and written through `write_bytes`: text mode on a Windows host would
    translate the line endings a second time and double every one of them.
    """
    if name not in FILE_ENCODINGS:
        raise ValueError("unknown probe file: " + str(name))
    return content.encode(FILE_ENCODINGS[name])


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
        print("solar-terrain-probes: name --demo at least once, or pass --all",
              file=sys.stderr)
        return 2
    try:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        for demo in demos:
            for path in write(demo, args.out_dir):
                print(path)
    except (OSError, ValueError, TypeError) as exc:
        print("solar-terrain-probes: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
