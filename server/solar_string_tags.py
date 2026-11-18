"""Prepare deterministic, nominally separated string TEXT; publication belongs to the caller."""
from functools import lru_cache
import importlib.util
import math
from pathlib import Path
import sys

from solar_design_graph import GraphValidationError
from solar_sizing_client import checked_graph

MAX_STRINGS = 10000
MAX_COORDINATE = 1e12
MAX_CELL_INDEX = 10**9
_ILLEGAL_LAYER = set('<>/\\":;?*|=')


@lru_cache(maxsize=1)
def _single_add():
    path = Path(__file__).resolve().parent / "builtins" / "solar_string_add.py"
    spec = importlib.util.spec_from_file_location("_solar_string_tags_single", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _positive(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and value > 0
    except OverflowError:
        return False


def _ascii(value):
    return (type(value) is str and 1 <= len(value) <= 128
            and all(32 <= ord(char) <= 126 for char in value))


def _reserve(successors, column, row):
    """Reserve the first free row >= row, compressing the occupied successor path."""
    trail = []
    while (column, row) in successors:
        trail.append(row)
        row = successors[column, row]
    for occupied in trail:
        successors[column, occupied] = row + 1
    successors[column, row] = row + 1
    return row


def _coordinate(value, meters_per_unit, path):
    if type(value) not in (int, float):
        raise GraphValidationError("INVALID_STRING_TAG_POSITION", path)
    try:
        result = value / meters_per_unit
    except (OverflowError, ZeroDivisionError):
        raise GraphValidationError("STRING_TAG_COORDINATE_LIMIT", path) from None
    if (not math.isfinite(result) or abs(result) > MAX_COORDINATE
            or (value != 0 and abs(result) < sys.float_info.min)):
        raise GraphValidationError("STRING_TAG_COORDINATE_LIMIT", path)
    return result


def prepare_tags(graph, request):
    """Return only the proposed v1 payload, without changing graph or request."""
    required = {"expected_rev", "paper_mm_per_drawing_unit"}
    allowed = required | {"tag_layer", "paper_height_mm"}
    if (type(request) is not dict or not required <= request.keys()
            or not request.keys() <= allowed or type(request["expected_rev"]) is not int):
        raise GraphValidationError("INVALID_STRING_TAGS_REQUEST")
    factor = request["paper_mm_per_drawing_unit"]
    paper_height = request.get("paper_height_mm", 2.0)
    if not _positive(factor) or not _positive(paper_height):
        raise GraphValidationError("INVALID_STRING_TAG_SCALE")
    if paper_height < 1.8:
        raise GraphValidationError("STRING_TAG_HEIGHT_TOO_SMALL")
    layer = request.get("tag_layer", "LEAF_STRING_TAGS")
    if not _ascii(layer) or layer == "0" or any(c in _ILLEGAL_LAYER for c in layer):
        raise GraphValidationError("INVALID_STRING_TAG_LAYER")
    try:
        height = paper_height / factor
        plotted_height = height * factor
    except (OverflowError, ZeroDivisionError):
        raise GraphValidationError("STRING_TAG_SCALE_LIMIT") from None
    if (not _positive(height) or height < sys.float_info.min * 4
            or height > MAX_COORDINATE or not _positive(plotted_height)
            or (plotted_height < 1.8 and not math.isclose(plotted_height, 1.8, rel_tol=1e-15))):
        raise GraphValidationError("STRING_TAG_SCALE_LIMIT")
    before = checked_graph(graph, request["expected_rev"])
    meters_per_unit = before["project"]["units"]["meters_per_unit"]
    if not _positive(meters_per_unit):
        raise GraphValidationError("INVALID_STRING_TAG_UNITS")
    strings = before["strings"]
    if not strings:
        raise GraphValidationError("STRING_TAGS_REQUIRE_STRINGS")
    if len(strings) > MAX_STRINGS:
        raise GraphValidationError("STRING_TAGS_LIMIT_EXCEEDED")
    anchors = []
    wanted = []
    for index, string in enumerate(strings):
        path = f"strings[{index}]"
        if not _ascii(string["circuit_tag"]):
            raise GraphValidationError("INVALID_STRING_TAG_LABEL", path)
        route = string["route"]
        if route:
            anchors.append((route[len(route) // 2], None, path))
        else:
            refs = string["ordered_panel_refs"]
            if not refs:
                raise GraphValidationError("STRING_TAG_POSITION_REQUIRED", path)
            ref = refs[len(refs) // 2]
            wanted.append(ref)
            anchors.append((None, ref, path))
    panels = {}
    if wanted:
        single = _single_add()
        panels = single.find_panels(before, wanted, single.slot_tables(before))
    margin = height * 0.25
    cell_width = 1.2 * max(len(s["circuit_tag"]) for s in strings) * height + 2 * margin
    cell_height = height + 2 * margin
    if not _positive(cell_width) or cell_width > MAX_COORDINATE or cell_height > MAX_COORDINATE:
        raise GraphValidationError("STRING_TAG_SCALE_LIMIT")
    successors = {}
    tags = []
    for string, (point, ref, path) in sorted(zip(strings, anchors), key=lambda pair: pair[0]["id"]):
        if point is None:
            point = panels.get(ref, {}).get("centre")
        if type(point) is not list or len(point) not in (2, 3):
            raise GraphValidationError("STRING_TAG_POSITION_REQUIRED", path)
        x, y = (_coordinate(value, meters_per_unit, path) for value in point[:2])
        ratios = (x / cell_width, y / cell_height)
        if any(not math.isfinite(ratio) or abs(ratio) > MAX_CELL_INDEX for ratio in ratios):
            raise GraphValidationError("STRING_TAG_COORDINATE_LIMIT", path)
        # Floor snapping is defined for negative anchors too. Reservation only moves upward.
        column, row = (math.floor(ratio) for ratio in ratios)
        row = _reserve(successors, column, row)
        if abs(row) > MAX_CELL_INDEX:
            raise GraphValidationError("STRING_TAG_COORDINATE_LIMIT", path)
        x, y = column * cell_width + margin, row * cell_height + margin
        width = 1.2 * len(string["circuit_tag"]) * height
        if any(not math.isfinite(v) or abs(v) > MAX_COORDINATE
               for v in (x, y, x + width, y + height)):
            raise GraphValidationError("STRING_TAG_COORDINATE_LIMIT", path)
        tags.append({"string_ref": string["id"], "text": string["circuit_tag"],
                     "position": [x, y, 0], "height": height, "rotation_degrees": 0})
    return {"schema": "leaf.solar-string-tags.v1", "tag_layer": layer,
            "paper_mm_per_drawing_unit": factor, "tags": tags}
