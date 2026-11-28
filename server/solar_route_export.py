"""Build a schedule and CAD route overlay from the same validated solar graph."""

import io
import json
import zipfile

from intake_dxf import MAX_POINTS, MAX_POINTS_PER_ENTITY, IntakeDxfError, intake_to_dxf
from solar_artifacts import MAX_ARTIFACT_BYTES, ROUTE_BUNDLE_FILES, ROUTE_BUNDLE_MEDIA_TYPE
from solar_design_graph import GraphValidationError


FILENAME = "CableRoutes.zip"
MEDIA_TYPE = ROUTE_BUNDLE_MEDIA_TYPE
INSUNITS = {"in": 1, "ft": 2, "mm": 4, "cm": 5, "m": 6, "km": 7, "yd": 10}
# A planar vertex has two DXF groups, each with at least one digit of value.
MIN_DXF_POINT_BYTES = len(b"10\n0\n20\n0\n")
LAYERS = {
    "start homerun": "LEAF-HOMERUN-START",
    "end homerun": "LEAF-HOMERUN-END",
    "feeder": "LEAF-FEEDER",
}


def build(graph, workbook):
    """Return the archive bytes and number of geometry routes with no Homeruns row."""
    # The route bridge projects non-trench cables in graph order. The workbook
    # carries that position through sorting even when the printed circuits tie.
    routes = [route for route in graph["routes"] if route["route_kind"] != "trench"]
    units = graph["project"]["units"]
    drawing_units = units["drawing_units"]
    insunits = INSUNITS.get(drawing_units)
    if insunits is None:
        raise GraphValidationError("UNRESOLVED_UNITS")
    meters_per_unit = units["meters_per_unit"]
    rows = workbook["route_rows"]
    if len(routes) != workbook["route_count"] or any(not 0 <= position < len(routes) for position in rows):
        raise GraphValidationError("SCHEDULES_MAPPING_FAILED")
    workbook_bytes = workbook["bytes"]
    point_counts = [len(route["points"]) for route in routes]
    total_points = sum(point_counts)
    if (total_points > MAX_POINTS or any(count > MAX_POINTS_PER_ENTITY for count in point_counts)
            or len(workbook_bytes) + total_points * MIN_DXF_POINT_BYTES > MAX_ARTIFACT_BYTES):
        raise GraphValidationError("ARTIFACT_TOO_LARGE")
    polylines = []
    index = []
    for position, route in enumerate(routes):
        kind = route["route_kind"]
        layer = LAYERS[kind]
        handle = format(position + 0x100, "X")
        polylines.append({
            "layer": layer,
            "handle": handle,
            "pts": [[x / meters_per_unit, y / meters_per_unit] for x, y in route["points"]],
        })
        index.append({
            "route_id": route["id"],
            "kind": kind,
            "from_ref": route["from_ref"],
            "to_ref": route["to_ref"],
            "dxf_layer": layer,
            "dxf_handle": handle,
            "homeruns_row": rows.get(position),
        })
    missing = sum(item["homeruns_row"] is None for item in index)
    overlay = intake_to_dxf({"polylines": polylines})
    # The intake writer emits a canonical HEADER but does not emit INSUNITS.
    handseed = b"9\n$HANDSEED\n"
    if handseed not in overlay:
        raise IntakeDxfError("Route drawing header has no handle seed")
    insertion_units = f"9\n$INSUNITS\n70\n{insunits}\n".encode("ascii")
    overlay = overlay.replace(handseed, insertion_units + handseed, 1)
    drawing_name, workbook_name, index_name = ROUTE_BUNDLE_FILES
    manifest = {
        "schema": "leaf.solar-route-export.v1",
        "drawing_units": drawing_units,
        "meters_per_unit": meters_per_unit,
        "drawing": drawing_name,
        "workbook": workbook_name,
        "sheet": "Homeruns",
        "missing_schedule_rows": missing,
        "routes": index,
    }
    index_bytes = json.dumps(manifest, indent=2, allow_nan=False).encode("utf-8")
    if len(overlay) + len(workbook_bytes) + len(index_bytes) > MAX_ARTIFACT_BYTES:
        raise GraphValidationError("ARTIFACT_TOO_LARGE")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in (
            (drawing_name, overlay),
            (workbook_name, workbook_bytes),
            (index_name, index_bytes),
        ):
            archive.writestr(zipfile.ZipInfo(name), content, compress_type=zipfile.ZIP_DEFLATED)
    content = output.getvalue()
    if len(content) > MAX_ARTIFACT_BYTES:
        raise GraphValidationError("ARTIFACT_TOO_LARGE")
    return content, missing
