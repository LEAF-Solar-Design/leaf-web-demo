#!/usr/bin/env python3
"""Studio's copies of five licensed KML and LandXML DEMO fixtures, and their probe files.

Each LEAF*DEMO command named below runs a FIXED fixture through one plugin
engine and writes files to %TEMP%. This module holds Studio's own copy of those
fixtures and runs them through server/solar_geo_formats.py, emitting each file in
the plugin's exact bytes: UTF-8 with or without the byte order mark
Encoding.UTF8 writes, CRLF line breaks, and the plugin's number rendering.

Fixtures ported from, read 2026-09-23 at C:/tmp/solar-parity/wt-b25-s17:

  * Terrain/KmlExportCommand.cs:207-298       -> leafkmlexport_demo.kml
  * Terrain/KmlExportFmtDemoCommand.cs:76-338 -> twelve leafkmlexportfmt_demo_F*.xml
                                                 and leafkmlexportfmt_demo.csv
  * Terrain/KmlImportDemoCommand.cs:55-110    -> leafkmlimport_demo.csv
  * Terrain/LandXmlExportDemoCommand.cs:56-120 -> leaflandxml_fixed.xml and
                                                 leaflandxml_delaunay.xml
  * Terrain/LandXmlImportDemoCommand.cs:47-196 -> leafimportlandxml_demo.csv

Contract: the fixture is the FIXTURE (joint identity contract v5 rule E1), so it
is written here as literal inputs and NEVER read from the plugin's output. Every
number and every rendered string in a probe file is COMPUTED by
server/solar_geo_formats.py; nothing is copied from the licensed capture. The
two importers read Studio's OWN documents: LEAFKMLIMPORTDEMO re-parses the KML
Studio's exporter wrote (from --out-dir when LEAFKMLEXPORTDEMO ran first, which
--all guarantees), and LEAFIMPORTLANDXMLDEMO parses the four XML fixtures below.

Two inputs that are not geometry, stated because each one appears in a file:

  * LANDXML_UTC_NOW. TerrainExporter.ToLandXml stamps DateTime.UtcNow into the
    LandXML header (TerrainExporter.cs:121-123), so the clock is an INPUT to that
    engine exactly as the grid is. The scenario pins it to the instant the
    licensed run read, so both sides ran the same case.
  * The path in LEAFKMLIMPORTDEMO's `#` line names the file the importer read.
    Studio writes the path it actually read; the line is preamble, which the
    probe-evidence reader skips as metadata.

No network, no dependencies outside the standard library.

Usage:
    python scripts/solar_geo_formats_probes.py --out-dir <dir> --all
    python scripts/solar_geo_formats_probes.py --out-dir <dir> --demo leafkmlexport
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


engine = _load("solar_geo_formats", ROOT / "server" / "solar_geo_formats.py")

C = engine.KmlCoordinate
PP = engine.KmlProjectedPoint
POLY = engine.KmlPolygon

# --------------------------------------------------------------------------- #
# LEAFKMLEXPORTDEMO (KmlExportCommand.cs:246-282): a 100 m square boundary and
# two 10 x 1 m tracker rows, anchored at 40 N, 105 W.
# --------------------------------------------------------------------------- #
KML_EXPORT_CENTER = (40.0, -105.0)
KML_EXPORT_DOCUMENT = "LEAFKMLEXPORTDEMO"
KML_EXPORT_POLYGONS = (
    ("Site Boundary", (PP(-50.0, -50.0), PP(50.0, -50.0), PP(50.0, 50.0), PP(-50.0, 50.0))),
    ("Tracker Row 1", (PP(-5.0, 19.5), PP(5.0, 19.5), PP(5.0, 20.5), PP(-5.0, 20.5))),
    ("Tracker Row 2", (PP(-5.0, -20.5), PP(5.0, -20.5), PP(5.0, -19.5), PP(-5.0, -19.5))),
)


def _triangle(lon0, lat0, alt=(0.0, 0.0, 0.0)):
    """The fmt demo's three-vertex ring (lon0,lat0) (lon0+1,lat0) (lon0,lat0+1)."""
    return [C(lon0, lat0, alt[0]), C(lon0 + 1.0, lat0, alt[1]), C(lon0, lat0 + 1.0, alt[2])]


# --------------------------------------------------------------------------- #
# LEAFKMLEXPORTFMTDEMO (KmlExportFmtDemoCommand.cs:88-312), in command order.
# --------------------------------------------------------------------------- #
KML_FMT_FIXTURES = (
    ("kml", "F1_ALT_ZERO_OMIT", "F1Doc", [POLY("F1Poly", _triangle(10.0, 40.0))]),
    ("kml", "F2_ALT_NONZERO", "F2Doc",
     [POLY("F2Poly", _triangle(10.0, 40.0, (100.0, 100.0, 100.0)))]),
    ("kml", "F3_MIXED_ALT", "F3Doc",
     [POLY("F3Poly", _triangle(10.0, 40.0, (0.0, 50.0, 0.0)))]),
    ("kml", "F4_EXPLICIT_CLOSED", "F4Doc",
     [POLY("F4Poly", _triangle(10.0, 40.0) + [C(10.0, 40.0, 0.0)])]),
    ("kml", "F5_IMPLICIT_OPEN", "F5Doc", [POLY("F5Poly", _triangle(10.0, 40.0))]),
    ("projected", "F6_PROJECTED_ROUNDTRIP", 40.0, -105.0, "F6Doc",
     [("F6Poly", (PP(0.0, 0.0), PP(200.0, 0.0), PP(0.0, 100.0)))]),
    ("kml", "F7_NO_DOC_NAME", "", [POLY("F7Poly", _triangle(10.0, 40.0))]),
    ("kml", "F8_NO_PM_NAME", "F8Doc", [POLY("", _triangle(10.0, 40.0))]),
    ("kml", "F9_NULL_POLYGON_SKIP", "F9Doc",
     [POLY("F9PolyA", _triangle(10.0, 40.0)), None, POLY("F9PolyB", _triangle(20.0, 50.0))]),
    ("kml", "F10_G17_PRECISION", "F10Doc",
     [POLY("F10Poly", [C(-123.45678901234567, 40.0, 0.0), C(11.0, 40.0, 0.0),
                       C(10.0, 41.0, 0.0)])]),
    ("kml", "F11_IS_CLOSED_EPS", "F11Doc",
     [POLY("F11Poly", _triangle(10.0, 40.0) + [C(10.0, 40.0, 1e-15)])]),
    ("kml", "F12_UTF8_DECL", "F12Doc", [POLY("F12Poly", _triangle(10.0, 40.0))]),
    ("pole", "F13_POLE_GUARD", 90.0, 0.0, "F13Doc",
     [("F13Poly", (PP(0.0, 0.0), PP(10.0, 0.0), PP(0.0, 10.0)))]),
)

# --------------------------------------------------------------------------- #
# LEAFLANDXMLDEMO (LandXmlExportDemoCommand.cs:66-120): a 21 x 21 sinusoid on
# [-100, 100]^2, z = 2 sin(2 pi x / 200) cos(2 pi y / 200), both triangulations.
# --------------------------------------------------------------------------- #
LANDXML_GRID = dict(rows=21, cols=21, x_min=-100.0, x_max=100.0, y_min=-100.0,
                    y_max=100.0, amplitude_m=2.0, wavelength_m=200.0)
LANDXML_SURFACE = "LEAFLANDXMLDEMO"
# The DateTime.UtcNow the licensed run read; see the module docstring.
LANDXML_UTC_NOW = datetime(2026, 9, 23, 5, 41, 53, tzinfo=timezone.utc)

# --------------------------------------------------------------------------- #
# LEAFIMPORTLANDXMLDEMO (LandXmlImportDemoCommand.cs:47-127): four documents,
# tokens N E Z.
# --------------------------------------------------------------------------- #
_LANDXML_DOC = """<?xml version="1.0" encoding="UTF-8"?>
<LandXML{ns}>
  <Surfaces>
    <Surface name="{name}">
      <Definition surfType="TIN">
        <Pnts>
{points}
        </Pnts>
      </Definition>
    </Surface>
  </Surfaces>
</LandXML>"""


def _landxml_fixture(name, points, namespace=""):
    body = "\n".join('          <P id="%d">%s</P>' % (index, text)
                     for index, text in enumerate(points, start=1))
    ns = ' xmlns="%s"' % namespace if namespace else ""
    return _LANDXML_DOC.format(ns=ns, name=name, points=body)


LANDXML_IMPORT_FIXTURES = (
    ("F1_BASIC_NO_NS", _landxml_fixture("demo", ("0 0 100", "10 0 101", "10 10 102",
                                                 "0 10 103"))),
    ("F2_WITH_NAMESPACE", _landxml_fixture("demo", ("0 0 100", "10 0 101", "10 10 102",
                                                    "0 10 103"),
                                           "http://www.landxml.org/schema/LandXML-1.2")),
    ("F3_BAD_LINES", _landxml_fixture("bad", ("2 1 50", "", "1 2", "foo bar baz", "5 4 60"))),
    ("F4_ORDER", _landxml_fixture("order", ("0 0 0", "5 0 5", "5 5 10", "0 5 15",
                                            "2.5 2.5 20"))),
)


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def build_kml_export():
    lat, lon = KML_EXPORT_CENTER
    return engine.kml_export_demo(KML_EXPORT_POLYGONS, lat, lon, KML_EXPORT_DOCUMENT)


def build_kml_export_fmt():
    return engine.kml_export_fmt_demo(KML_FMT_FIXTURES)


def build_kml_import(kml_text=None, kml_path=None):
    """Re-parse Studio's own exported KML; `kml_path` labels the file that was read."""
    if kml_text is None:
        kml_text = build_kml_export()[engine.KML_EXPORT_DEMO_FILE]
    label = kml_path if kml_path is not None else engine.KML_EXPORT_DEMO_FILE
    return {engine.KML_IMPORT_DEMO_FILE: engine.kml_import_demo_csv(kml_text, label)}


def landxml_interpolator():
    grid = LANDXML_GRID
    elevations = engine.sinusoid_grid(grid["rows"], grid["cols"], grid["x_min"], grid["x_max"],
                                      grid["y_min"], grid["y_max"], grid["amplitude_m"],
                                      grid["wavelength_m"])
    return engine.TerrainGridInterpolator(elevations, grid["rows"], grid["cols"],
                                          grid["x_min"], grid["x_max"], grid["y_min"],
                                          grid["y_max"], meters_per_unit=1.0)


def build_landxml(utc_now=None):
    return engine.landxml_demo(landxml_interpolator(), LANDXML_SURFACE,
                               LANDXML_UTC_NOW if utc_now is None else utc_now)


def build_landxml_import():
    return {engine.LANDXML_IMPORT_DEMO_FILE:
            engine.landxml_import_demo_csv(LANDXML_IMPORT_FIXTURES)}


DEMOS = {
    "leafimportlandxml": build_landxml_import,
    "leafkmlexport": build_kml_export,
    "leafkmlexportfmt": build_kml_export_fmt,
    "leafkmlimport": build_kml_import,
    "leaflandxml": build_landxml,
}

_FMT_IDS = tuple(fixture[1] for fixture in KML_FMT_FIXTURES if fixture[0] != "pole")
KML_FMT_XML_FILES = tuple(engine.KML_FMT_FILE_PREFIX + fixture_id + ".xml"
                          for fixture_id in _FMT_IDS)

# Every file a demo writes, in the order the command writes them.
DEMO_FILES = {
    "leafimportlandxml": (engine.LANDXML_IMPORT_DEMO_FILE,),
    "leafkmlexport": (engine.KML_EXPORT_DEMO_FILE,),
    "leafkmlexportfmt": KML_FMT_XML_FILES + (engine.KML_FMT_MANIFEST_FILE,),
    "leafkmlimport": (engine.KML_IMPORT_DEMO_FILE,),
    "leaflandxml": (engine.LANDXML_FIXED_FILE, engine.LANDXML_DELAUNAY_FILE),
}

# The capability each FILE proves, for the probe-evidence normalizer's caller.
FILE_CAPABILITIES = {engine.KML_EXPORT_DEMO_FILE: "kml-export",
                     engine.KML_FMT_MANIFEST_FILE: "kml-export",
                     engine.KML_IMPORT_DEMO_FILE: "kml-import",
                     engine.LANDXML_FIXED_FILE: "landxml-export",
                     engine.LANDXML_DELAUNAY_FILE: "landxml-export",
                     engine.LANDXML_IMPORT_DEMO_FILE: "landxml-import"}
FILE_CAPABILITIES.update({name: "kml-export" for name in KML_FMT_XML_FILES})

# The probe-type key solar_probe_evidence.py reads to project a file into rows.
FILE_PROBE_TYPES = {engine.KML_EXPORT_DEMO_FILE: "kml-document",
                    engine.KML_FMT_MANIFEST_FILE: "kml-export-format-manifest",
                    engine.KML_IMPORT_DEMO_FILE: "kml-import-vertices",
                    engine.LANDXML_FIXED_FILE: "landxml-surface",
                    engine.LANDXML_DELAUNAY_FILE: "landxml-surface",
                    engine.LANDXML_IMPORT_DEMO_FILE: "landxml-import-points"}
FILE_PROBE_TYPES.update({name: "kml-document" for name in KML_FMT_XML_FILES})

# Every file is UTF-8; the byte order mark, where the command passes
# Encoding.UTF8, is already the first character of the text the engine renders.
FILE_ENCODINGS = {name: "utf-8" for name in FILE_PROBE_TYPES}

# Which demo owns a file, so a caller holding one file name can rebuild it.
FILE_DEMOS = {name: demo for demo, names in DEMO_FILES.items() for name in names}


def build(demo):
    """Run one DEMO's fixture and return {file name: text}."""
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
    """Write one demo's files exactly, and return their paths in file order.

    LEAFKMLIMPORTDEMO reads the KML LEAFKMLEXPORTDEMO left in `out_dir` when it
    is there, a true write-then-read round trip like the plugin's, and otherwise
    re-parses the same document computed in process.
    """
    out_dir = Path(out_dir)
    if demo == "leafkmlimport":
        kml_path = out_dir / engine.KML_EXPORT_DEMO_FILE
        if kml_path.is_file():
            files = build_kml_import(kml_path.read_bytes().decode("utf-8-sig"), str(kml_path))
        else:
            files = build_kml_import()
        if tuple(files) != DEMO_FILES[demo]:
            raise ValueError("demo wrote unexpected files: " + str(sorted(files)))
    else:
        files = build(demo)
    paths = []
    for name, content in files.items():
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
        print("solar-geo-formats-probes: name --demo at least once, or pass --all",
              file=sys.stderr)
        return 2
    try:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        for demo in demos:
            for path in write(demo, args.out_dir):
                print(path)
    except (OSError, ValueError, TypeError) as exc:
        print("solar-geo-formats-probes: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
