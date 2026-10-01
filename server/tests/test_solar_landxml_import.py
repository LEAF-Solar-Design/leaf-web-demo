"""sf-w4-landxml-import: the typed LandXML terrain intake (server/solar_landxml_import.py) and the
two seams it adds to server/solar_geo_formats.py (parse_xml's per-caller depth bound and kept
attributes, and survey_points). Every expected value below was measured by running the module
against the FilesystemBackend the physical-state tests seed. The positive fixture is the
licensed LEAFLANDXMLDEMO capture committed at docs/parity/evidence/probes/demo-probes-20260923,
read with its line endings normalized to CRLF (the bytes the plugin wrote), so its digest does
not depend on the checkout."""
import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
ROOT = SERVER.parent
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import solar_geo_formats as geo  # noqa: E402
import solar_ground_terrain as terrain  # noqa: E402
import solar_landxml_import as lx  # noqa: E402
import solar_physical_head as ph  # noqa: E402
import solar_physical_state as ps  # noqa: E402
import store  # noqa: E402
import write_loop  # noqa: E402
from test_solar_physical_state import DRAWING, GENERATE_SHA, PREFIX, PROJECT, TENANT, TINY  # noqa: E402
from test_w1_design_graph import graph  # noqa: E402,F401
from test_w1_solve_commit import seed, seed_graphless  # noqa: E402

CAPTURE = ROOT / "docs/parity/evidence/probes/demo-probes-20260923/leaflandxml_fixed.xml"
REAL = CAPTURE.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
REAL_SHA = "9846f488c6780af2185fdf62741865fbc7a75077488449f6741f0aac77f370b5"
METRIC = '<Metric linearUnit="meter" areaUnit="squareMeter" volumeUnit="cubicMeter"/>'
FOOT = '<Imperial linearUnit="foot" areaUnit="squareFoot" volumeUnit="cubicYard"/>'
SURVEY_FOOT = '<Imperial linearUnit="USSurveyFoot" areaUnit="squareFoot" volumeUnit="cubicYard"/>'
# northing easting elevation, as LandXML writes a <P>
FOUR = ["0 0 10", "0 10 11", "10 0 12", "10 10 13"]


def canonical(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def landxml(points=FOUR, units=METRIC, crs="", extra=""):
    pnts = "".join('<P id="%d">%s</P>' % (index + 1, text) for index, text in enumerate(points))
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<LandXML xmlns="http://www.landxml.org/schema/LandXML-1.2" version="1.2">'
            + crs + ("<Units>" + units + "</Units>" if units is not None else "")
            + '<Surfaces><Surface name="S"><Definition surfType="TIN"><Pnts>' + pnts
            + "</Pnts></Definition></Surface></Surfaces>" + extra + "</LandXML>").encode("utf-8")


@pytest.fixture
def backend(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    return backend


def run(backend, data=REAL, drawing_units="m", crs="none", **kw):
    return lx.import_landxml_terrain(backend, TENANT, DRAWING, data, drawing_units=drawing_units,
                                     crs=crs, **kw)


def keys(backend):
    return set(backend.drawing_object_keys(TENANT, DRAWING))


def refused(code, fn, *args, **kw):
    with pytest.raises(ps.PhysicalStateError) as exc:
        fn(*args, **kw)
    assert exc.value.code == code
    return exc.value


def plugin_grid(data, target_cells=30, elevation_scale=1.0):
    """LEAFIMPORTLANDXML's own arithmetic: ParsePoints, ResampleToGrid, then elevScale."""
    grid = geo.resample_to_grid(geo.parse_points(data.decode("utf-8")), target_cells)
    return {"elevations": [value * elevation_scale for value in grid.elevations], "rows": grid.rows,
            "cols": grid.cols, "x_min": grid.x_min, "x_max": grid.x_max, "y_min": grid.y_min,
            "y_max": grid.y_max}


# ------------------------------------------------------------------ contract --

def test_landxml_import_constants():
    assert (lx.RESULT_SCHEMA, lx.SOURCE_TOOL, lx.SOURCE_MEDIA_TYPE, lx.SOURCE_FILENAME, lx.CAPABILITY) == (
        "leaf.solar-landxml-import.v1", "solar-landxml-source", "application/xml", "landxml-source.xml",
        "landxml-import")
    assert lx.POINT_ORDER == "northing-easting-elevation"
    assert (lx.MAX_LANDXML_BYTES, lx.MAX_LANDXML_DEPTH, lx.MAX_LANDXML_POINTS, lx.MAX_ABS_COORDINATE,
            lx.MAX_IDW_OPERATIONS) == (16_777_216, 32, 250_000, 1e9, 20_000_000)
    assert (lx.DEFAULT_TARGET_CELLS, lx.MIN_TARGET_CELLS, lx.MAX_TARGET_CELLS) == (30, 2, 200)
    assert lx.LINEAR_UNITS == {("Metric", "meter"): ("meter", 1.0), ("Imperial", "foot"): ("foot", 0.3048),
                               ("Imperial", "USSurveyFoot"): ("USSurveyFoot", 1200.0 / 3937.0)}
    assert len(lx.CODES) == 28 and not lx.CODES & (ps.CODES | ph.CODES)
    error = lx.LandXmlImportError("LANDXML_EMPTY")
    assert isinstance(error, ps.PhysicalStateError) and error.code == str(error) == "LANDXML_EMPTY"


def test_landxml_import_codes_closed():
    source = (SERVER / "solar_landxml_import.py").read_text(encoding="utf-8")
    assert set(re.findall(r'"(LANDXML_[A-Z_]+)"', source)) == lx.CODES


# ----------------------------------------------------- the geo-formats seams --

def test_landxml_import_parse_xml_defaults_unchanged():
    root = geo.parse_xml(REAL.decode("utf-8"))
    assert root.local_name == "LandXML" and root.attributes is None
    assert all(element.attributes is None for element in root.descendants())


def test_landxml_import_parse_xml_keeps_requested_attributes():
    text = '<a xmlns:q="urn:q"><M x="1" q:y="2"/><N z="3"/><M/></a>'
    root = geo.parse_xml(text, attributes_of=frozenset({"M"}))
    first, other, second = root.children()
    assert (first.attributes, other.attributes, second.attributes) == ({"x": "1"}, None, {})


def test_landxml_import_parse_xml_depth_bound():
    ok = "<a>" * 5 + "</a>" * 5
    assert geo.parse_xml(ok, max_depth=5).local_name == "a"
    with pytest.raises(geo.UnsafeXmlError, match="exceeds depth 4"):
        geo.parse_xml(ok, max_depth=4)
    for bad in (0, geo.MAX_XML_DEPTH + 1, True, 5.0):
        with pytest.raises(ValueError):
            geo.parse_xml(ok, max_depth=bad)
    with pytest.raises(TypeError):
        geo.parse_xml(ok, attributes_of={"a"})


@pytest.mark.parametrize("text,accepted", [
    ("<a><P/><P/></a>", True),
    ("<a><P/><P/><P/></a>", False),
])
def test_landxml_import_parser_count_limit(text, accepted):
    if accepted:
        assert geo.parse_xml(text, count_limits={"P": 2}).local_name == "a"
    else:
        with pytest.raises(geo.XmlCountError) as exc:
            geo.parse_xml(text, count_limits={"P": 2})
        assert exc.value.local_name == "P"


def test_landxml_import_parser_stops_at_the_limit(monkeypatch):
    constructions = []

    class CountingElement(geo.XmlElement):
        def __init__(self, local_name, attributes=None):
            constructions.append(local_name)
            super().__init__(local_name, attributes)

    monkeypatch.setattr(geo, "XmlElement", CountingElement)
    with pytest.raises(geo.XmlCountError):
        geo.parse_xml("<a>" + "<P/>" * 10 + "</a>", count_limits={"P": 3})
    assert constructions == ["a", "P", "P", "P"]


@pytest.mark.parametrize("count_limits,error,match", [
    ({"P": 0}, ValueError, "count_limits values must be from 1 to"),
    ({"P": geo.MAX_XML_ELEMENTS + 1}, ValueError, "count_limits values must be from 1 to"),
    ({"P": True}, TypeError, "count_limits values must be int, not bool"),
    (["P"], TypeError, "count_limits must be a dict"),
])
def test_landxml_import_parser_count_limits_validated(count_limits, error, match):
    with pytest.raises(error, match=match):
        geo.parse_xml("<a/>", count_limits=count_limits)


def test_landxml_import_discarded_attributes_unbounded():
    attributes = " ".join('a%d="x"' % index for index in range(1000))
    root = geo.parse_xml("<a " + attributes + "/>")
    assert root.local_name == "a" and root.attributes is None


def test_landxml_import_survey_points_is_the_plugin_reader():
    text = REAL.decode("utf-8")
    assert geo.survey_points(geo.parse_xml(text)) == geo.parse_landxml_points(text)
    skips = landxml(["1 2 3", "", "4 5", "a 6 7", "8 9 NaN", " 10\t11\n12 13 "]).decode("utf-8")
    assert geo.parse_landxml_points(skips) == [geo.SurveyPoint(2.0, 1.0, 3.0), geo.SurveyPoint(11.0, 10.0, 12.0)]
    assert geo.survey_points(geo.parse_xml(skips)) == geo.parse_landxml_points(skips)


# ---------------------------------------------------------------- inspection --

def test_landxml_import_inspect_licensed_capture():
    source = lx.inspect_landxml(REAL)
    assert {key: source[key] for key in source if key != "points"} == {
        "byte_length": 45305, "sha256": REAL_SHA, "linear_unit": "meter", "meters_per_source_unit": 1.0,
        "file_crs": None, "declared_points": 441}
    assert len(source["points"]) == 441
    assert source["points"][:2] == [(-100.0, -100.0, -0.0), (-90.0, -100.0, 0.618)]
    assert source["points"] == [(p.x, p.y, p.z) for p in geo.parse_landxml_points(REAL.decode("utf-8"))]


def test_landxml_import_axes_frozen():
    source = lx.inspect_landxml(landxml(["1 2 3", "4 5 6", "7 8 9"]))
    assert source["points"] == [(2.0, 1.0, 3.0), (5.0, 4.0, 6.0), (8.0, 7.0, 9.0)]


def test_landxml_import_skips_are_counted():
    source = lx.inspect_landxml(landxml(FOUR + ["", "1 2", "x 1 2", "1 2 inf"]))
    assert (source["declared_points"], len(source["points"])) == (8, 4)


@pytest.mark.parametrize("case,data,code", [
    ("empty", b"", "LANDXML_EMPTY"),
    ("text", "<LandXML/>", "LANDXML_EMPTY"),
    ("none", None, "LANDXML_EMPTY"),
    ("utf8", b"<LandXML>\xff</LandXML>", "LANDXML_ENCODING_INVALID"),
    ("doctype", b'<!DOCTYPE LandXML [<!ENTITY a "b">]><LandXML/>', "LANDXML_UNSAFE"),
    ("depth", b"<LandXML>" + b"<a>" * 32 + b"</a>" * 32 + b"</LandXML>", "LANDXML_UNSAFE"),
    ("malformed", b"<LandXML><Units></LandXML>", "LANDXML_MALFORMED"),
    ("root", landxml().replace(b"<LandXML", b"<kml").replace(b"</LandXML>", b"</kml>"), "LANDXML_NOT_LANDXML"),
    ("no units", landxml(units=None), "LANDXML_UNITS_MISSING"),
    ("empty units", landxml(units=""), "LANDXML_UNITS_MISSING"),
    ("millimeter", landxml(units='<Metric linearUnit="millimeter"/>'), "LANDXML_UNITS_UNSUPPORTED"),
    ("metric foot", landxml(units='<Metric linearUnit="foot"/>'), "LANDXML_UNITS_UNSUPPORTED"),
    ("no linear", landxml(units="<Metric/>"), "LANDXML_UNITS_UNSUPPORTED"),
    ("two systems", landxml(units=METRIC + FOOT), "LANDXML_UNITS_UNSUPPORTED"),
    ("two units", landxml(crs="<Units>" + METRIC + "</Units>"), "LANDXML_UNITS_UNSUPPORTED"),
    ("elevation", landxml(units='<Metric linearUnit="meter" elevationUnit="feet"/>'),
     "LANDXML_UNITS_UNSUPPORTED"),
    ("crs no epsg", landxml(crs='<CoordinateSystem name="local"/>'), "LANDXML_CRS_UNSUPPORTED"),
    ("crs bad epsg", landxml(crs='<CoordinateSystem epsgCode="EPSG:2229"/>'), "LANDXML_CRS_UNSUPPORTED"),
    ("two crs", landxml(crs='<CoordinateSystem epsgCode="2229"/><CoordinateSystem epsgCode="2229"/>'),
     "LANDXML_CRS_UNSUPPORTED"),
    ("two points", landxml(FOUR[:2]), "LANDXML_TOO_FEW_POINTS"),
    ("skipped to two", landxml(FOUR[:2] + ["1 2"]), "LANDXML_TOO_FEW_POINTS"),
    ("far", landxml(FOUR + ["0 1000000001 0"]), "LANDXML_COORDINATE_OUT_OF_RANGE"),
    ("deep", landxml(FOUR + ["0 0 -1e10"]), "LANDXML_COORDINATE_OUT_OF_RANGE"),
])
def test_landxml_import_inspect_refusals(case, data, code):
    refused(code, lx.inspect_landxml, data)


def test_landxml_import_inspect_bounds():
    refused("LANDXML_TOO_LARGE", lx.inspect_landxml, b"<" + b" " * lx.MAX_LANDXML_BYTES)
    nested = b"<LandXML>" + b"<a>" * 30 + b"</a>" * 30 + b"</LandXML>"
    refused("LANDXML_UNITS_MISSING", lx.inspect_landxml, nested)
    assert lx.inspect_landxml(landxml(FOUR + ["0 1000000000 -1000000000"]))["points"][-1] == (1e9, 0.0, -1e9)
    ok = lx.inspect_landxml(landxml(units='<Imperial linearUnit="foot" elevationUnit="feet"/>'))
    assert ok["linear_unit"] == "foot"


def test_landxml_import_point_count_bound(monkeypatch):
    monkeypatch.setattr(lx, "MAX_LANDXML_POINTS", 4)
    assert lx.inspect_landxml(landxml(FOUR))["declared_points"] == 4
    refused("LANDXML_TOO_MANY_POINTS", lx.inspect_landxml, landxml(FOUR + [""]))


def test_landxml_import_point_bound_stops_the_parser(monkeypatch):
    def walked(*args, **kwargs):
        raise AssertionError("point bound must stop parsing before any tree walk")

    monkeypatch.setattr(lx, "MAX_LANDXML_POINTS", 4)
    monkeypatch.setattr(lx.geo, "survey_points", walked)
    monkeypatch.setattr(lx.geo, "count_local_names", walked)
    refused("LANDXML_TOO_MANY_POINTS", lx.inspect_landxml, landxml(FOUR + [""]))


@pytest.mark.parametrize("attribute_count", [geo.MAX_KEPT_ATTRIBUTES, geo.MAX_KEPT_ATTRIBUTES + 1])
def test_landxml_import_kept_attribute_cap(attribute_count):
    extras = " ".join('a%d="x"' % index for index in range(attribute_count - 1))
    data = landxml(units='<Metric linearUnit="meter" ' + extras + "/>")
    if attribute_count == geo.MAX_KEPT_ATTRIBUTES:
        assert lx.inspect_landxml(data)["linear_unit"] == "meter"
    else:
        refused("LANDXML_UNSAFE", lx.inspect_landxml, data)


@pytest.mark.parametrize("attribute_count", [geo.MAX_KEPT_ATTRIBUTES, geo.MAX_KEPT_ATTRIBUTES + 1])
def test_landxml_import_kept_attribute_cap_refuses_before_copy(monkeypatch, attribute_count):
    copies = []

    class CountingAttributes(dict):
        def items(self):
            copies.append(True)
            return super().items()

    real_parser_create = geo.expat.ParserCreate

    class ParserWrapper:
        def __init__(self, parser):
            object.__setattr__(self, "parser", parser)

        def __getattr__(self, name):
            return getattr(self.parser, name)

        def __setattr__(self, name, handler):
            if name == "StartElementHandler":
                setattr(self.parser, name, lambda name, attrs: handler(name, CountingAttributes(attrs)))
            else:
                setattr(self.parser, name, handler)

    def parser_create(*args, **kwargs):
        return ParserWrapper(real_parser_create(*args, **kwargs))

    monkeypatch.setattr(geo.expat, "ParserCreate", parser_create)
    attributes = " ".join('a%d="x"' % index for index in range(attribute_count))
    text = "<Metric " + attributes + "/>"
    if attribute_count == geo.MAX_KEPT_ATTRIBUTES:
        root = geo.parse_xml(text, attributes_of=frozenset({"Metric"}))
        assert len(root.attributes) == geo.MAX_KEPT_ATTRIBUTES
        assert copies == [True]
    else:
        with pytest.raises(geo.UnsafeXmlError):
            geo.parse_xml(text, attributes_of=frozenset({"Metric"}))
        assert copies == []


def test_landxml_import_file_crs():
    data = landxml(crs='<CoordinateSystem name="CA V" epsgCode="2229"/>', units=SURVEY_FOOT)
    source = lx.inspect_landxml(data)
    assert (source["file_crs"], source["linear_unit"], source["meters_per_source_unit"]) == (
        "EPSG:2229", "USSurveyFoot", 0.3048006096012192)


# ---------------------------------------------------------------------- grid --

def test_landxml_import_grid_is_the_plugin_grid():
    grid = lx.terrain_grid(lx.inspect_landxml(REAL)["points"], horizontal_scale=1.0, elevation_scale=1.0,
                           target_cells=30)
    assert grid == plugin_grid(REAL)
    assert list(grid) == ["elevations", "rows", "cols", "x_min", "x_max", "y_min", "y_max"]
    assert (grid["rows"], grid["cols"], grid["x_min"], grid["x_max"], grid["y_min"], grid["y_max"]) == (
        30, 30, -100.0, 100.0, -100.0, 100.0)
    assert canonical(grid) == "1c78f602025556918336710801265c08a50a9e9098b48d2bda4a669bc9927452"
    assert terrain.neutral_grid(grid) == grid


def test_landxml_import_resample_bound():
    """600 points on a 25 x 24 lattice: 186 cells is 178 x 186 x 600 = 19,864,800 point-node pairs (the
    largest under the bound, about 3 s), 187 cells is 179 x 187 x 600 = 20,083,800 and is refused."""
    points = lx.inspect_landxml(landxml(["%d %d 1" % (i // 25, i % 25) for i in range(600)]))["points"]
    grid = lx.terrain_grid(points, horizontal_scale=1.0, elevation_scale=1.0, target_cells=186)
    assert (grid["rows"], grid["cols"]) == (178, 186)
    refused("LANDXML_RESAMPLE_TOO_LARGE", lx.terrain_grid, points, horizontal_scale=1.0, elevation_scale=1.0,
            target_cells=187)


# -------------------------------------------------------------- end to end --

def test_landxml_import_first(backend):
    before = keys(backend)
    result = run(backend)
    source, state = result["source"], result["head"]["state"]
    assert result == {
        "schema": "leaf.solar-landxml-import.v1", "created": True, "drawing_id": DRAWING, "project_id": PROJECT,
        "source": source,
        "interpretation": {"point_order": "northing-easting-elevation", "drawing_x": "easting",
                           "drawing_y": "northing", "linear_unit": "meter", "meters_per_source_unit": 1.0,
                           "drawing_units": "m", "meters_per_drawing_unit": 1.0, "horizontal_scale": 1.0,
                           "elevation_scale": 1.0, "crs": "none", "crs_source": "declared",
                           "elevation_datum": "unrecorded"},
        "points": {"declared": 441, "accepted": 441, "skipped": 0},
        "grid": {"rows": 30, "cols": 30, "target_cells": 30, "x_min": -100.0, "x_max": 100.0,
                 "y_min": -100.0, "y_max": 100.0},
        "head": {"schema": ph.HEAD_SCHEMA, "drawing_id": DRAWING, "project_id": PROJECT, "index": 0,
                 "parent": None, "state": state}}
    assert (source["content_sha256"], source["byte_length"], source["media_type"], source["filename"],
            source["source_version"]) == (REAL_SHA, 45305, "application/xml", "landxml-source.xml", 1)
    assert source["artifact_id"] == MEASURED_SOURCE_ID
    assert (state["artifact_id"], state["content_sha256"], state["byte_length"]) == MEASURED_STATE
    assert canonical(result) == MEASURED_FIRST_RESULT
    assert keys(backend) - before == {
        PREFIX + "artifacts/" + source["artifact_id"] + ".json",
        PREFIX + "artifacts/blobs/" + REAL_SHA + ".bin",
        PREFIX + "artifacts/" + state["artifact_id"] + ".json",
        PREFIX + "artifacts/blobs/" + state["content_sha256"] + ".bin",
        PREFIX + "physical/head-0000.json"}


MEASURED_SOURCE_ID = "d6923bdcbbc5c7e02b2cfc486ff981d716adc4b7d05e344c04939bb4eef33cce"
MEASURED_STATE = ("56f3892ae8228c968f19b0a7f1a3fb65ae49be60d279382fb69e21ed4a4bcbc8",
                  "0de2afeb7ae692538ce33fd011b043438cd65cc426fd1cead1933eb4046d22f0", 18283)
MEASURED_FIRST_RESULT = "49c4b726eed64d3600ec3d1234a8ac7d4138b67741f11f9a421c6460c720b1cd"


def test_landxml_import_reopens_unchanged(backend):
    result = run(backend)
    head, document, grid = lx.load_terrain(backend, TENANT, DRAWING, project_id=PROJECT)
    assert head == result["head"] and head == ph.physical_head(backend, TENANT, DRAWING, project_id=PROJECT)
    assert grid == plugin_grid(REAL) and repr(grid) == repr(plugin_grid(REAL))
    assert document == {"schema": ps.DOCUMENT_SCHEMA, "units": {"drawing_units": "m", "meters_per_unit": 1.0},
                        "frame": dict(ps.DEFAULT_FRAME), "source": {"kind": "ground-intake", "sha256": REAL_SHA},
                        "capability": "landxml-import", "parent": None, "state": {"grid": grid}}
    meta, content = lx.load_landxml_source(backend, TENANT, DRAWING, result["source"]["artifact_id"],
                                           project_id=PROJECT)
    assert content == REAL and meta["tool"] == "solar-landxml-source"
    assert lx.load_terrain(backend, TENANT, DRAWING, project_id=PROJECT)[2] == grid


def test_landxml_import_empty_drawing_reads_none(backend):
    assert lx.load_terrain(backend, TENANT, DRAWING, project_id=PROJECT) == (None, None, None)
    refused("LANDXML_PROJECT_ID_INVALID", lx.load_terrain, backend, TENANT, DRAWING, project_id="")


def test_landxml_import_same_file_twice_writes_nothing(backend):
    first = run(backend)
    before = keys(backend)
    again = run(backend)
    assert again == dict(first, created=False)
    assert keys(backend) == before


def test_landxml_import_new_cells_is_a_child(backend):
    first = run(backend)
    second = run(backend, target_cells=10)
    assert (second["created"], second["head"]["index"], second["head"]["parent"]) == (
        True, 1, first["head"]["state"]["artifact_id"])
    assert (second["grid"]["rows"], second["grid"]["cols"]) == (10, 10)
    assert lx.load_terrain(backend, TENANT, DRAWING, project_id=PROJECT)[2] == plugin_grid(REAL, 10)


def test_landxml_import_keeps_the_rest_of_the_head(backend):
    tiny = ps.physical_document(TINY, drawing_units="m", source_sha256=GENERATE_SHA, capability="frame-generate")
    prior = ph.publish_physical_state(backend, TENANT, DRAWING, tiny)
    result = run(backend)
    assert result["head"]["parent"] == prior["head"]["state"]["artifact_id"]
    _, document, grid = lx.load_terrain(backend, TENANT, DRAWING, project_id=PROJECT)
    assert list(document["state"]) == list(TINY)
    assert {k: v for k, v in document["state"].items() if k != "grid"} == {
        k: v for k, v in tiny["state"].items() if k != "grid"}
    assert repr(document["state"]["frames"]) == repr(tiny["state"]["frames"])
    assert grid == plugin_grid(REAL) and document["frame"] == tiny["frame"]


def test_landxml_import_head_units_and_crs_must_agree(backend):
    feet = ps.physical_document(TINY, drawing_units="ft", source_sha256=GENERATE_SHA, capability="frame-generate")
    ph.publish_physical_state(backend, TENANT, DRAWING, feet)
    before = keys(backend)
    refused("LANDXML_UNITS_MISMATCH", run, backend)
    refused("LANDXML_CRS_MISMATCH", run, backend, drawing_units="ft", crs="EPSG:2229")
    assert keys(backend) == before
    assert run(backend, drawing_units="ft")["head"]["index"] == 1


@pytest.mark.parametrize("units,drawing_units,scales", [
    (FOOT, "m", (0.3048, 0.3048)),
    (FOOT, "ft", (1.0, 0.3048)),
    (METRIC, "ft", (1.0 / 0.3048, 1.0)),
    (SURVEY_FOOT, "ft", (1200.0 / 3937.0 / 0.3048, 1200.0 / 3937.0)),
])
def test_landxml_import_units(backend, units, drawing_units, scales):
    data = landxml(units=units)
    result = run(backend, data, drawing_units=drawing_units)
    interpretation = result["interpretation"]
    assert (interpretation["horizontal_scale"], interpretation["elevation_scale"]) == scales
    grid = lx.load_terrain(backend, TENANT, DRAWING, project_id=PROJECT)[2]
    assert grid == lx.terrain_grid(lx.inspect_landxml(data)["points"], horizontal_scale=scales[0],
                                   elevation_scale=scales[1], target_cells=30)
    assert (grid["x_max"], grid["y_max"]) == (10.0 * scales[0], 10.0 * scales[0])


def test_landxml_import_feet_file_is_the_plugin_feet_answer(backend):
    data = landxml(units=FOOT)
    run(backend, data, drawing_units="ft")
    assert lx.load_terrain(backend, TENANT, DRAWING, project_id=PROJECT)[2] == plugin_grid(data, 30, 0.3048)


def test_landxml_import_crs(backend):
    data = landxml(crs='<CoordinateSystem epsgCode="2229"/>')
    refused("LANDXML_CRS_MISMATCH", run, backend, data)
    refused("LANDXML_CRS_MISMATCH", run, backend, data, crs="EPSG:4326")
    result = run(backend, data, crs="EPSG:2229")
    assert (result["interpretation"]["crs"], result["interpretation"]["crs_source"]) == ("EPSG:2229", "file")
    assert lx.load_terrain(backend, TENANT, DRAWING, project_id=PROJECT)[1]["frame"]["crs"] == "EPSG:2229"


def test_landxml_import_declared_crs(backend):
    result = run(backend, crs="EPSG:32611")
    assert (result["interpretation"]["crs"], result["interpretation"]["crs_source"]) == ("EPSG:32611", "declared")


@pytest.mark.parametrize("kw,code", [
    ({"drawing_units": "in"}, "LANDXML_DRAWING_UNITS_INVALID"),
    ({"drawing_units": None}, "LANDXML_DRAWING_UNITS_INVALID"),
    ({"crs": "EPSG:0"}, "LANDXML_CRS_INVALID"),
    ({"crs": "epsg:4326"}, "LANDXML_CRS_INVALID"),
    ({"crs": "WGS84"}, "LANDXML_CRS_INVALID"),
    ({"crs": 4326}, "LANDXML_CRS_INVALID"),
    ({"target_cells": 1}, "LANDXML_TARGET_CELLS_INVALID"),
    ({"target_cells": 201}, "LANDXML_TARGET_CELLS_INVALID"),
    ({"target_cells": True}, "LANDXML_TARGET_CELLS_INVALID"),
    ({"target_cells": 30.0}, "LANDXML_TARGET_CELLS_INVALID"),
    ({"project_id": "p" * 101}, "LANDXML_PROJECT_ID_INVALID"),
    ({"project_id": ""}, "LANDXML_PROJECT_ID_INVALID"),
    ({"data": b""}, "LANDXML_EMPTY"),
])
def test_landxml_import_parameter_refusals(backend, kw, code):
    before = keys(backend)
    refused(code, run, backend, **kw)
    assert keys(backend) == before


def test_landxml_import_context(backend, tmp_path, monkeypatch):
    refused("LANDXML_PROJECT_MISMATCH", run, backend, project_id="leaf:project:other")
    refused("LANDXML_DRAWING_NOT_FOUND", lx.import_landxml_terrain, backend, TENANT, "nosuch", REAL,
            drawing_units="m", crs="none")
    (tmp_path / "graphless").mkdir()
    graphless, _ = seed_graphless(tmp_path / "graphless", monkeypatch)
    refused("LANDXML_GRAPH_REQUIRED", run, graphless)
    assert not [key for key in graphless.drawing_object_keys(TENANT, DRAWING) if "/artifacts/" in key]


def test_landxml_import_drained(backend, monkeypatch):
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "0")
    before = keys(backend)
    refused("LANDXML_WRITES_DRAINED", run, backend)
    assert keys(backend) == before


def test_landxml_import_concurrent_writer_conflicts(backend, monkeypatch):
    real_publish = ph.publish_physical_state

    def racing(backend_, tenant, drawing, document, **kw):
        rival = ps.physical_document(TINY, drawing_units="m", source_sha256=GENERATE_SHA,
                                     capability="frame-generate")
        real_publish(backend_, tenant, drawing, rival)
        return real_publish(backend_, tenant, drawing, document, **kw)

    monkeypatch.setattr(lx.ph, "publish_physical_state", racing)
    refused("PHYSICAL_HEAD_CONFLICT", run, backend)
    head = ph.physical_head(backend, TENANT, DRAWING, project_id=PROJECT)
    assert head["index"] == 0 and head["state"]["artifact_id"] != MEASURED_STATE[0]


def test_landxml_import_unsafe_store_passes_through(backend):
    class Inherits(store.StorageBackend):
        def __init__(self, inner):
            self.inner = inner

        def get(self, key):
            return self.inner.get(key)

        def put(self, key, data):
            return self.inner.put(key, data)

        def exists(self, key):
            return self.inner.exists(key)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    refused("PHYSICAL_HEAD_STORE_UNSAFE", run, Inherits(backend))
    assert not [key for key in keys(backend) if "/physical/" in key]


@pytest.mark.parametrize("field", ["tool", "media_type", "filename"])
def test_landxml_import_source_loader_checks_each_field(backend, monkeypatch, field):
    result = run(backend)
    artifact_id = result["source"]["artifact_id"]
    meta, content = lx.load_landxml_source(backend, TENANT, DRAWING, artifact_id, project_id=PROJECT)
    changed_meta = dict(meta)
    changed_meta[field] = "x"
    monkeypatch.setattr(lx.solar_artifacts, "read_artifact", lambda *args, **kwargs: (changed_meta, content))
    refused("LANDXML_SOURCE_KIND_MISMATCH", lx.load_landxml_source, backend, TENANT, DRAWING,
            artifact_id, project_id=PROJECT)


def test_landxml_import_source_loader_refusals(backend):
    result = run(backend)
    state_id = result["head"]["state"]["artifact_id"]
    load = lx.load_landxml_source
    refused("LANDXML_SOURCE_KIND_MISMATCH", load, backend, TENANT, DRAWING, state_id, project_id=PROJECT)
    refused("LANDXML_SOURCE_NOT_FOUND", load, backend, TENANT, DRAWING, "0" * 64, project_id=PROJECT)
    refused("LANDXML_SOURCE_ID_INVALID", load, backend, TENANT, DRAWING, "zz", project_id=PROJECT)
    refused("LANDXML_PROJECT_MISMATCH", load, backend, TENANT, DRAWING, result["source"]["artifact_id"],
            project_id="leaf:project:other")
    refused("LANDXML_PROJECT_ID_INVALID", load, backend, TENANT, DRAWING, result["source"]["artifact_id"],
            project_id=None)
