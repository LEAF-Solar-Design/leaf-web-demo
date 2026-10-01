"""Typed LandXML terrain intake: one uploaded LandXML file becomes a stored source and the
drawing's current Ground physical state, with its units, axes and CRS explicit.

The plugin's LEAFIMPORTLANDXML (Terrain/LandXmlImportCommand.cs) reads every <P> as
"northing easting elevation" (TerrainImporter.ParseLandXmlPoints, its silent skip of a
short, non-numeric or non-finite entry included), places easting on drawing X and northing
on drawing Y in the file's own numbers, resamples them onto a grid by inverse distance
weighting (LandXmlImporter.ResampleToGrid) and multiplies the grid's elevations by the
Meters or Feet answer to a prompt. Studio keeps that arithmetic and replaces the prompts
with typed facts:

  * units come from the file's <Units> (Metric meter, Imperial foot, Imperial
    USSurveyFoot), never from a guess; anything else is refused. Horizontal coordinates
    are scaled into the caller's drawing units (m or ft) and elevations into metres. When
    the file's unit IS the drawing unit the horizontal scale is exactly 1.0, so the grid
    is the plugin's grid bit for bit;
  * the CRS is declared by the caller ("none" or EPSG:n) and, when the file carries a
    <CoordinateSystem epsgCode>, must equal it; a CoordinateSystem without an EPSG code
    is refused rather than assumed;
  * the point axes are frozen: <P>1 2 3</P> is northing 1, easting 2, elevation 3, so
    drawing X 2.0, drawing Y 1.0, Z 3.0 (before unit scaling).

Bounds come before work: the byte length before decoding, the nesting depth and element
count while expat streams, the declared point count before any value is read, the
coordinate magnitude before resampling, and the resample's point-node work before the
inverse distance pass. The source bytes are stored as an immutable artifact; the grid is
published as a child of the drawing's current physical head (server/solar_physical_head.py),
carrying every other key of that state unchanged, so a concurrent writer is refused rather
than overwritten. Re-importing the same file onto its own result writes nothing.

Every refusal is a named, payload-free LANDXML_* code; a refusal from the physical state or
head store passes through with its own PHYSICAL_* code (LandXmlImportError is a
PhysicalStateError, so one except clause covers both). No clock, network, environment read
or graph write lives here.
"""
import hashlib
import json
import re

import solar_artifacts
import solar_geo_formats as geo
import solar_ground_terrain as terrain
import solar_physical_head as ph
import solar_physical_state as ps
import write_loop
from solar_design_graph import GraphValidationError
from solar_graph_context import resolve_graph_context

RESULT_SCHEMA = "leaf.solar-landxml-import.v1"
SOURCE_TOOL = "solar-landxml-source"
SOURCE_MEDIA_TYPE = "application/xml"
SOURCE_FILENAME = "landxml-source.xml"
CAPABILITY = "landxml-import"
POINT_ORDER = "northing-easting-elevation"
MAX_LANDXML_BYTES = 16_777_216          # the artifact store's own limit
MAX_LANDXML_DEPTH = 32                  # a TIN point sits at depth 6 (LandXML/Surfaces/.../P)
MAX_LANDXML_POINTS = 250_000            # <P> elements, counted before any value is read
MAX_ABS_COORDINATE = 1e9                # source units, on every accepted point
MAX_IDW_OPERATIONS = 20_000_000         # rows * cols * points; measured about 3 s
DEFAULT_TARGET_CELLS = 30               # LandXmlImportCommand.cs DefaultTargetCells
MIN_TARGET_CELLS = 2                    # its prompt's LowerLimit
MAX_TARGET_CELLS = 200                  # its prompt's UpperLimit
MAX_PROJECT_ID_CHARS = 100
# (units element, linearUnit) -> metres per source unit. Closed: every other unit is refused.
LINEAR_UNITS = {
    ("Metric", "meter"): ("meter", 1.0),
    ("Imperial", "foot"): ("foot", 0.3048),
    ("Imperial", "USSurveyFoot"): ("USSurveyFoot", 1200.0 / 3937.0),
}
# An elevationUnit attribute, when present, must name the linear unit (closed aliases).
ELEVATION_ALIASES = {"meter": frozenset({"meter"}), "foot": frozenset({"foot", "feet"}),
                     "USSurveyFoot": frozenset({"USSurveyFoot"})}
UNITS_ELEMENTS = frozenset({"Metric", "Imperial"})
_ATTRIBUTES_OF = frozenset({"Metric", "Imperial", "CoordinateSystem"})
_EPSG = re.compile(r"EPSG:[1-9][0-9]{0,5}")
_EPSG_CODE = re.compile(r"[1-9][0-9]{0,5}")
GRID_KEYS = ("elevations", "rows", "cols") + terrain.GRID_BOUNDS
CODES = frozenset({
    "LANDXML_WRITES_DRAINED", "LANDXML_PROJECT_ID_INVALID", "LANDXML_DRAWING_UNITS_INVALID",
    "LANDXML_CRS_INVALID", "LANDXML_TARGET_CELLS_INVALID", "LANDXML_EMPTY", "LANDXML_TOO_LARGE",
    "LANDXML_ENCODING_INVALID", "LANDXML_UNSAFE", "LANDXML_MALFORMED", "LANDXML_NOT_LANDXML",
    "LANDXML_UNITS_MISSING", "LANDXML_UNITS_UNSUPPORTED", "LANDXML_CRS_UNSUPPORTED",
    "LANDXML_CRS_MISMATCH", "LANDXML_UNITS_MISMATCH", "LANDXML_TOO_MANY_POINTS",
    "LANDXML_TOO_FEW_POINTS", "LANDXML_COORDINATE_OUT_OF_RANGE", "LANDXML_RESAMPLE_TOO_LARGE",
    "LANDXML_DRAWING_NOT_FOUND", "LANDXML_GRAPH_REQUIRED", "LANDXML_PROJECT_MISMATCH",
    "LANDXML_STORE_UNAVAILABLE", "LANDXML_SOURCE_NOT_FOUND", "LANDXML_SOURCE_ID_INVALID",
    "LANDXML_SOURCE_CORRUPT", "LANDXML_SOURCE_KIND_MISMATCH",
})
_ARTIFACT_CODES = {
    "ARTIFACT_WRITES_DRAINED": "LANDXML_WRITES_DRAINED",
    "ARTIFACT_STORE_UNAVAILABLE": "LANDXML_STORE_UNAVAILABLE",
    "ARTIFACT_NOT_FOUND": "LANDXML_SOURCE_NOT_FOUND",
    "ARTIFACT_ID_INVALID": "LANDXML_SOURCE_ID_INVALID",
    "ARTIFACT_TOO_LARGE": "LANDXML_TOO_LARGE",
}


class LandXmlImportError(ps.PhysicalStateError):
    """A named, payload-free refusal; a PhysicalStateError, so one except clause covers the
    intake's codes and the physical state and head codes that pass through it."""


def _artifact_refusal(code):
    return _ARTIFACT_CODES.get(code, "LANDXML_SOURCE_CORRUPT")


def _project_id(value):
    if type(value) is not str or not 1 <= len(value) <= MAX_PROJECT_ID_CHARS:
        raise LandXmlImportError("LANDXML_PROJECT_ID_INVALID")
    return value


def _units(root):
    """(linear unit name, metres per source unit) from the root's one <Units> child."""
    units = [child for child in root.children() if child.local_name == "Units"]
    if not units:
        raise LandXmlImportError("LANDXML_UNITS_MISSING")
    if len(units) > 1:
        raise LandXmlImportError("LANDXML_UNITS_UNSUPPORTED")
    systems = [child for child in units[0].children() if child.local_name in UNITS_ELEMENTS]
    if not systems:
        raise LandXmlImportError("LANDXML_UNITS_MISSING")
    if len(systems) > 1:
        raise LandXmlImportError("LANDXML_UNITS_UNSUPPORTED")
    system = systems[0]
    attributes = system.attributes or {}
    found = LINEAR_UNITS.get((system.local_name, attributes.get("linearUnit")))
    if found is None:
        raise LandXmlImportError("LANDXML_UNITS_UNSUPPORTED")
    elevation = attributes.get("elevationUnit")
    if elevation is not None and elevation not in ELEVATION_ALIASES[found[0]]:
        raise LandXmlImportError("LANDXML_UNITS_UNSUPPORTED")
    return found


def _file_crs(root):
    """"EPSG:n" from the root's <CoordinateSystem epsgCode>, or None when it has none."""
    systems = [child for child in root.children() if child.local_name == "CoordinateSystem"]
    if not systems:
        return None
    if len(systems) > 1:
        raise LandXmlImportError("LANDXML_CRS_UNSUPPORTED")
    code = (systems[0].attributes or {}).get("epsgCode")
    if type(code) is not str or not _EPSG_CODE.fullmatch(code):
        raise LandXmlImportError("LANDXML_CRS_UNSUPPORTED")
    return "EPSG:" + code


def inspect_landxml(data):
    """The typed reading of one LandXML upload, or a LANDXML_* refusal. Pure: no store.

    Returns {"byte_length", "sha256", "linear_unit", "meters_per_source_unit", "file_crs",
    "declared_points", "points"} where points are (easting, northing, elevation) tuples in
    source units, in document order, after the plugin's silent skip.
    """
    if type(data) is not bytes or not data:
        raise LandXmlImportError("LANDXML_EMPTY")
    if len(data) > MAX_LANDXML_BYTES:
        raise LandXmlImportError("LANDXML_TOO_LARGE")
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise LandXmlImportError("LANDXML_ENCODING_INVALID") from None
    try:
        root = geo.parse_xml(text, max_depth=MAX_LANDXML_DEPTH, attributes_of=_ATTRIBUTES_OF,
                             count_limits={"P": MAX_LANDXML_POINTS})
    except geo.XmlCountError:
        raise LandXmlImportError("LANDXML_TOO_MANY_POINTS") from None
    except geo.UnsafeXmlError:
        raise LandXmlImportError("LANDXML_UNSAFE") from None
    except geo.GeoFormatError:
        raise LandXmlImportError("LANDXML_MALFORMED") from None
    if root.local_name != "LandXML":
        raise LandXmlImportError("LANDXML_NOT_LANDXML")
    linear_unit, meters_per_source_unit = _units(root)
    file_crs = _file_crs(root)
    declared = geo.count_local_names(root, "P")
    points = [(p.x, p.y, p.z) for p in geo.survey_points(root)]
    if len(points) < 3:
        raise LandXmlImportError("LANDXML_TOO_FEW_POINTS")
    for point in points:
        if any(abs(value) > MAX_ABS_COORDINATE for value in point):
            raise LandXmlImportError("LANDXML_COORDINATE_OUT_OF_RANGE")
    return {"byte_length": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "linear_unit": linear_unit, "meters_per_source_unit": meters_per_source_unit,
            "file_crs": file_crs, "declared_points": declared, "points": points}


def terrain_grid(points, *, horizontal_scale, elevation_scale, target_cells):
    """The neutral grid (server/solar_ground_terrain.neutral_grid's shape): bounds in drawing
    units, elevations in metres. Horizontal coordinates are scaled before the resample and
    elevations after it, exactly where the plugin applies its elevation answer."""
    scaled = [(x * horizontal_scale, y * horizontal_scale, z) for x, y, z in points]
    try:
        grid = terrain.resample_to_grid(scaled, target_cells, max_idw_operations=MAX_IDW_OPERATIONS)
    except terrain.TerrainBoundsError:
        raise LandXmlImportError("LANDXML_RESAMPLE_TOO_LARGE") from None
    grid = {key: grid[key] for key in GRID_KEYS}
    grid["elevations"] = [value * elevation_scale for value in grid["elevations"]]
    return grid


def _context(backend, tenant_id, drawing_id, project_id):
    try:
        return resolve_graph_context(backend, tenant_id, drawing_id, "head", project_id=project_id)
    except GraphValidationError as exc:
        code = {"PROJECT_MISMATCH": "LANDXML_PROJECT_MISMATCH",
                "GRAPH_CONTEXT_UNAVAILABLE": "LANDXML_DRAWING_NOT_FOUND"}.get(
                    exc.code, "LANDXML_GRAPH_REQUIRED")
        raise LandXmlImportError(code) from None
    except (OSError, RuntimeError):
        raise LandXmlImportError("LANDXML_STORE_UNAVAILABLE") from None


def import_landxml_terrain(backend, tenant_id, drawing_id, data, *, drawing_units, crs,
                           target_cells=DEFAULT_TARGET_CELLS, project_id=None):
    """Store the LandXML source and publish its terrain grid as the drawing's physical head.

    drawing_units is "m" or "ft"; crs is "none" or "EPSG:n"; target_cells is 2 to 200.
    Returns the RESULT_SCHEMA dict; created is False when the current head is already this
    file's terrain with the same units, CRS and grid (nothing is published)."""
    if write_loop.drawing_mutations_refusal() is not None:
        raise LandXmlImportError("LANDXML_WRITES_DRAINED")
    if project_id is not None:
        _project_id(project_id)
    if type(drawing_units) is not str or drawing_units not in ps.UNITS:
        raise LandXmlImportError("LANDXML_DRAWING_UNITS_INVALID")
    if type(crs) is not str or not (crs == "none" or _EPSG.fullmatch(crs)):
        raise LandXmlImportError("LANDXML_CRS_INVALID")
    if (type(target_cells) is not int
            or not MIN_TARGET_CELLS <= target_cells <= MAX_TARGET_CELLS):
        raise LandXmlImportError("LANDXML_TARGET_CELLS_INVALID")
    source = inspect_landxml(data)
    if source["file_crs"] is not None and source["file_crs"] != crs:
        raise LandXmlImportError("LANDXML_CRS_MISMATCH")
    meters_per_drawing_unit = ps.UNITS[drawing_units]
    horizontal_scale = source["meters_per_source_unit"] / meters_per_drawing_unit
    elevation_scale = source["meters_per_source_unit"]
    grid = terrain_grid(source["points"], horizontal_scale=horizontal_scale,
                        elevation_scale=elevation_scale, target_cells=target_cells)

    context = _context(backend, tenant_id, drawing_id, project_id)
    project = context["project_id"]
    head, head_document = ph.load_physical_head(backend, tenant_id, drawing_id, project_id=project)
    if head is None:
        frame = json.loads(json.dumps(ps.DEFAULT_FRAME))
        frame["crs"] = crs
        state = {"grid": grid}
        parent = None
    else:
        if head_document["units"]["drawing_units"] != drawing_units:
            raise LandXmlImportError("LANDXML_UNITS_MISMATCH")
        if head_document["frame"]["crs"] != crs:
            raise LandXmlImportError("LANDXML_CRS_MISMATCH")
        frame = head_document["frame"]
        state = dict(head_document["state"])
        state["grid"] = grid
        parent = head["state"]["artifact_id"]
    document = ps.physical_document(state, drawing_units=drawing_units, source_sha256=source["sha256"],
                                    capability=CAPABILITY, parent=parent, frame=frame)

    sink = solar_artifacts.ArtifactSink(backend, tenant_id, drawing_id, context, SOURCE_TOOL,
                                        source["sha256"], False)
    try:
        source_ref = sink.finish(sink.prepare(solar_artifacts.ArtifactOutput(
            {"capability": CAPABILITY}, SOURCE_MEDIA_TYPE, SOURCE_FILENAME, data)))
    except GraphValidationError as exc:
        raise LandXmlImportError(_artifact_refusal(exc.code)) from None

    if (head is not None and head_document["capability"] == CAPABILITY
            and head_document["source"]["sha256"] == source["sha256"]
            and head_document["state"].get("grid") == grid):
        created, head_view = False, head
    else:
        published = ph.publish_physical_state(backend, tenant_id, drawing_id, document, project_id=project)
        created, head_view = published["created"], published["head"]
    return {
        "schema": RESULT_SCHEMA, "created": created, "drawing_id": drawing_id, "project_id": project,
        "source": source_ref,
        "interpretation": {
            "point_order": POINT_ORDER, "drawing_x": "easting", "drawing_y": "northing",
            "linear_unit": source["linear_unit"],
            "meters_per_source_unit": source["meters_per_source_unit"],
            "drawing_units": drawing_units, "meters_per_drawing_unit": meters_per_drawing_unit,
            "horizontal_scale": horizontal_scale, "elevation_scale": elevation_scale,
            "crs": crs, "crs_source": "declared" if source["file_crs"] is None else "file",
            "elevation_datum": frame["elevation_datum"],
        },
        "points": {"declared": source["declared_points"], "accepted": len(source["points"]),
                   "skipped": source["declared_points"] - len(source["points"])},
        "grid": {"rows": grid["rows"], "cols": grid["cols"], "target_cells": target_cells,
                 **{key: grid[key] for key in terrain.GRID_BOUNDS}},
        "head": head_view,
    }


def load_terrain(backend, tenant_id, drawing_id, *, project_id):
    """(head view, document, grid) of the drawing's current physical head, the grid exactly as
    stored (None when the head carries none); (None, None, None) for a drawing with no head."""
    _project_id(project_id)
    head, document = ph.load_physical_head(backend, tenant_id, drawing_id, project_id=project_id)
    if head is None:
        return None, None, None
    return head, document, document["state"].get("grid")


def load_landxml_source(backend, tenant_id, drawing_id, artifact_id, *, project_id):
    """(meta, bytes) of a stored LandXML source of this tenant, drawing and project."""
    _project_id(project_id)
    try:
        meta, content = solar_artifacts.read_artifact(backend, tenant_id, drawing_id, artifact_id)
    except GraphValidationError as exc:
        raise LandXmlImportError(_artifact_refusal(exc.code)) from None
    if (meta["tool"] != SOURCE_TOOL or meta["media_type"] != SOURCE_MEDIA_TYPE
            or meta["filename"] != SOURCE_FILENAME):
        raise LandXmlImportError("LANDXML_SOURCE_KIND_MISMATCH")
    if meta["project_id"] != project_id:
        raise LandXmlImportError("LANDXML_PROJECT_MISMATCH")
    if meta["request_sha256"] != meta["content_sha256"]:
        raise LandXmlImportError("LANDXML_SOURCE_CORRUPT")
    return meta, content
