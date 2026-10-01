"""Add ONE midpoint-connection string between two picked panels: the Studio counterpart of LEAFSTRINGMID.

Plugin semantics, read from the licensed source (Branch2025 Commands.cs:4642-4865 and
LeafSolarDesign.Core/StringMidpointPlacer.cs): LEAFSTRINGMID asks for two endpoint panels,
refuses identical picks and picks on different layers (the layer is the panel group), takes
the first pick's extents diagonal, gathers the same-layer panels a crossing window padded six
diagonals around the picks touches, links centroids within 1.5 diagonals, walks the shortest
path, and commits in one transaction one polyline through the path's centroids on the string
layer, one "MID" text at the path's floor(N / 2) panel (height 0.4 diagonals), and XData
tag_position = that index under the `LEAF` RegApp on the polyline.

The path, label index, label text and label height come from the frozen port
server/solar_batch2_simple.string_midpoint_rows (receipt m1, w4-m1). This module maps the
design graph to that kernel's intake and commits the polyline as one new circuit:

* the panel group is the frame: both endpoints must carry the same frame_ref, and only that
  frame's panels are candidates, exactly as the plugin filters its window by the picked layer;
* the kernel runs in DRAWING units (graph centres are compute metres, divided by
  meters_per_unit), so the returned mid_string row is the plugin's row; the committed circuit
  keeps graph conventions (route = the path panels' centres in metres, length_ft);
* the extents are the axis-aligned box of the first pick's module (its frame's module width
  along the row and height across the row, turned by the panel's angle), as AutoCAD's
  GeometricExtents of a rotated block are;
* the tag is stored in the circuit's extra.midpoint (the v1 string entity has an open extra
  and no text entity kind, so tag_text_ref stays null); no schema change;
* declared divergences: the plugin draws over panels another string already holds and never
  bounds the length; a design graph holds a panel in at most one circuit, so this refuses
  PANEL_ALREADY_ASSIGNED, and the drawing's own committed sizing bounds the circuit as
  SINGLESTRING's counterpart does (STRING_LENGTH_NOT_SIZED, STRING_TOO_LONG).

Fails closed: every check runs before the private copy is written; the caller's graph and
params are never mutated; a kernel refusal never escapes as a generic error. Bounded: one pass
over the panels to build the intake, the kernel's band and bucketed adjacency, one pass over
the strings for the wired set.
"""
import copy
from datetime import datetime, timezone
import importlib.util
import math
from pathlib import Path

import solar_batch2_simple as kernel
from solar_design_graph import GraphValidationError, _bounded_json, new_id
from solar_sizing_client import checked_graph
from solar_solve_results import finish_mutation, invalidate_dependents, sync_assignments

TOOL = "solar-string-midpoint"
INVALID = "INVALID_STRING_MIDPOINT_REQUEST"
MAX_REF = 128
MAX_REV = 2147483647
REQUEST_KEYS = frozenset({"expected_rev", "start_panel_ref", "end_panel_ref"})
TAG_RULE = "leafstringmid-floor-half"
# The kernel's own refusal texts (solar_batch2_simple.string_midpoint_path) mapped to codes.
KERNEL_REFUSALS = {
    "no adjacency path between the endpoints": "MIDPOINT_NO_PATH",
    "the path is too short for a midpoint string": "MIDPOINT_PATH_TOO_SHORT",
}


def _load_single_add():
    path = Path(__file__).resolve().with_name("solar_string_add.py")
    spec = importlib.util.spec_from_file_location("_solar_string_midpoint_single", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


single = _load_single_add()


def _valid_request(request):
    return (type(request) is dict and set(request) == REQUEST_KEYS
            and type(request["expected_rev"]) is int and 0 <= request["expected_rev"] <= MAX_REV
            and all(type(request[key]) is str and 1 <= len(request[key]) <= MAX_REF
                    for key in ("start_panel_ref", "end_panel_ref")))


def extents(frame, panel):
    """(ex, ey) in metres: the axis-aligned box of the panel's module turned by its angle."""
    width, height = float(frame["module_width_along_row"]), float(frame["module_height_across_row"])
    turn = math.radians(panel["angle"])
    cos, sin = abs(math.cos(turn)), abs(math.sin(turn))
    return width * cos + height * sin, width * sin + height * cos


def midpoint_intake(graph, start_ref, end_ref):
    """(intake, surrogate -> panel id, diagonal in metres) for the kernel, in drawing units.

    Surrogates are the panel's position in graph["panels"] as eight hex digits, so the
    kernel's handle-order tie break is graph order.
    """
    views = single.panel_views(graph)
    panels = {panel["id"]: panel for panel in views}
    if start_ref not in panels or end_ref not in panels:
        raise GraphValidationError("MISSING_PANEL")
    start, end = panels[start_ref], panels[end_ref]
    frame_ref = start["frame_ref"]
    frame = next((f for f in graph["frames"] if f["id"] == frame_ref), None) if frame_ref else None
    if frame is None:
        raise GraphValidationError("PANEL_GROUP_REQUIRED")
    if end["frame_ref"] != frame_ref:
        raise GraphValidationError("DIFFERENT_PANEL_GROUPS")
    scale = graph["project"]["units"]["meters_per_unit"]
    ex, ey = extents(frame, start)
    diagonal = math.hypot(ex, ey)
    names, rows = {}, []
    for index, panel in enumerate(views):
        if panel["frame_ref"] != frame_ref:
            continue
        name = format(index, "08X")
        names[name] = panel["id"]
        rows.append({"handle": name, "x": panel["centre"][0] / scale, "y": panel["centre"][1] / scale})
    surrogate = {panel_id: name for name, panel_id in names.items()}
    intake = {"panels": rows, "half_extents": [ex / 2 / scale, ey / 2 / scale], "diagonal": diagonal / scale}
    return intake, surrogate[start_ref], surrogate[end_ref], names, diagonal


def _new_string(graph, path, tag_index, label_height_m):
    """The circuit LEAFSTRINGMID commits, on the private copy only, after every refusal ran."""
    panels = {panel["id"]: panel for panel in single.panel_views(graph)}
    tags = {string["circuit_tag"] for string in graph["strings"]}
    number = graph["settings"]["string_number"]
    if type(number) is float and number.is_integer():
        number = int(number)
    if type(number) is not int:
        raise GraphValidationError("STRING_NUMBER_INVALID")
    while f"S{number}" in tags:
        number += 1
    if number >= single.MAX_STRING_NUMBER:
        raise GraphValidationError("STRING_NUMBER_EXHAUSTED")
    graph["settings"]["string_number"] = number + 1
    points = [copy.deepcopy(panels[ref]["centre"]) for ref in path]
    length_m = sum(math.dist(a + [0] * (3 - len(a)), b + [0] * (3 - len(b)))
                   for a, b in zip(points, points[1:]))
    return {
        "id": new_id("string"), "kind": "string", "rev": graph["rev"],
        "validity": {"state": "valid", "reasons": []},
        "provenance": {"created_by": TOOL, "created_at": datetime.now(timezone.utc).isoformat(),
                       "last_writer": TOOL, "source_rev": graph["rev"],
                       "source_hash": graph["source_hash"],
                       "catalog_versions": copy.deepcopy(graph["catalog_versions"])},
        "extra": {
            "polarity": {"source": "derived",
                         "rule": "ordered-selection-first-negative-last-positive",
                         "negative_panel_ref": path[0], "positive_panel_ref": path[-1],
                         "source_rev": graph["rev"]},
            "length_provenance": {"source": "derived", "rule": "panel-centre-path",
                                  "point_units": "m", "length_units": "ft",
                                  "includes_home_runs": False},
            "midpoint": {"source": "derived", "rule": TAG_RULE, "tag_index": tag_index,
                         "tag_panel_ref": path[tag_index], "label_text": kernel.MID_LABEL,
                         "label_height_m": label_height_m, "source_rev": graph["rev"]},
        },
        "circuit_tag": f"S{number}", "circuit_kind": "String",
        "ordered_panel_refs": list(path), "module_count": len(path),
        "from_ref": path[0], "to_ref": path[-1], "tag_text_ref": None, "wire_gauge": "",
        "length_ft": length_m / single.METRES_PER_FOOT, "route": points,
        "inverter_ref": None,
    }


def add_midpoint_string(graph, request):
    """Create exactly one circuit along LEAFSTRINGMID's shortest path between two panels."""
    _bounded_json(request)
    if not _valid_request(request):
        raise GraphValidationError(INVALID)
    start_ref, end_ref = request["start_panel_ref"], request["end_panel_ref"]
    if start_ref == end_ref:
        raise GraphValidationError("MIDPOINT_ENDPOINTS_IDENTICAL")
    before = checked_graph(graph, request["expected_rev"])
    longest = single.max_string_length(before)
    try:
        intake, start, end, names, diagonal_m = midpoint_intake(before, start_ref, end_ref)
        (_, fields), = kernel.string_midpoint_rows(intake, start, end)
    except kernel.BatchTwoError as exc:
        raise GraphValidationError(KERNEL_REFUSALS.get(str(exc), "STRING_MIDPOINT_MAPPING_FAILED")) from None
    except GraphValidationError:
        raise
    except (OverflowError, ArithmeticError, ValueError):
        raise GraphValidationError("STRING_MIDPOINT_GEOMETRY_OUT_OF_RANGE") from None
    path = [names[name] for name in fields["panels"]]
    if len(path) > longest:
        raise GraphValidationError("STRING_TOO_LONG")
    wired = {ref for string in before["strings"] for ref in string["ordered_panel_refs"]}
    if not wired.isdisjoint(path):
        raise GraphValidationError("PANEL_ALREADY_ASSIGNED")
    result = copy.deepcopy(before)
    label_height_m = diagonal_m * kernel.MID_LABEL_HEIGHT_DIAGONALS
    string = _new_string(result, path, fields["label_index"], label_height_m)
    result["strings"].append(string)
    sync_assignments(result)
    path_refs = set(path)
    original_frames = {frame["id"]: frame for frame in before["frames"]}
    result["frames"] = [
        copy.deepcopy(original_frames[frame["id"]])
        if path_refs.isdisjoint(frame["panel_refs"]) else frame
        for frame in result["frames"]
    ]
    invalidate_dependents(before, result, [string["id"], *path], solved_ids={string["id"]})
    after = finish_mutation(before, result, TOOL)
    mid_string = dict(fields, panels=list(path))
    return {"graph": after, "string_ref": string["id"], "circuit_tag": string["circuit_tag"],
            "ordered_panel_refs": list(path), "mid_string": mid_string}


OPERATIONS = {"add-midpoint-string": add_midpoint_string}


def run(graph, params):
    _bounded_json(params)
    if (type(params) is not dict or type(params.get("operation")) is not str
            or params["operation"] not in OPERATIONS):
        raise GraphValidationError(INVALID)
    request = {key: value for key, value in params.items() if key != "operation"}
    return OPERATIONS[params["operation"]](graph, request)["graph"]
