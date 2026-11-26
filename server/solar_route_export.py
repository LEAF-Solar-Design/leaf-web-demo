"""Build a schedule and CAD route overlay from the same validated solar graph."""

import io
import json
import zipfile

from intake_dxf import IntakeDxfError, intake_to_dxf
from solar_artifacts import ROUTE_BUNDLE_FILES, ROUTE_BUNDLE_MEDIA_TYPE


FILENAME = "CableRoutes.zip"
MEDIA_TYPE = ROUTE_BUNDLE_MEDIA_TYPE
INSUNITS = {"in": 1, "ft": 2, "mm": 4, "cm": 5, "m": 6, "km": 7, "yd": 10}
LAYERS = {
    "start homerun": "LEAF-HOMERUN-START",
    "end homerun": "LEAF-HOMERUN-END",
    "feeder": "LEAF-FEEDER",
}


def build(graph, workbook):
    """Return the archive bytes and number of geometry routes with no Homeruns row."""
    # The route bridge projects non-trench cables in graph order. The workbook
    # carries that position through sorting even when the printed circuits tie.
    routes = (route for route in graph["routes"] if route["route_kind"] != "trench")
    units = graph["project"]["units"]
    drawing_units = units["drawing_units"]
    meters_per_unit = units["meters_per_unit"]
    rows = workbook["route_rows"]
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
    insertion_units = f"9\n$INSUNITS\n70\n{INSUNITS[drawing_units]}\n".encode("ascii")
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
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in (
            (drawing_name, overlay),
            (workbook_name, workbook["bytes"]),
            (index_name, json.dumps(manifest, indent=2, allow_nan=False).encode("utf-8")),
        ):
            archive.writestr(zipfile.ZipInfo(name), content, compress_type=zipfile.ZIP_DEFLATED)
    return output.getvalue(), missing
