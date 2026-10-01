"""StringData (GetStringData, StringHomeRunCmd.cs:153-296) over the W1 design graph, as a read.

The plugin writes StringData.json: every panel group, sorted by name, each with
the selected strings whose two ends lie in it. Studio maps frames to panel
groups (graph order), each frame owning its panel_refs, so an end is located by
the panel at that end (the kernel's panel-owner path; frames carry no outline
polylines). A string's start marker sits at route[0] and its end marker at
route[-1], converted from compute metres to drawing units; a string with no
route has no markers and, as in the plugin, no file is written. The file's
handles are graph ids: frame id, string id, from_ref, to_ref.

Position surrogates (upper-case hex) carry identity into the pure kernel and a
strict inverse carries it back. Fails closed; linear in panels plus selection.
"""
import importlib.util
import json
import math
from pathlib import Path

import solar_artifacts
import solar_rooftop_chain as chain
from solar_design_graph import GraphValidationError, _bounded_json
from solar_sizing_client import units_resolved

TOOL = "solar-string-data"
MAX_REF = 128
MAX_SELECTED = 10000
FILENAME = "StringData.json"
MEDIA_TYPE = "application/json"


def _load_single_add():
    path = Path(__file__).resolve().with_name("solar_string_add.py")
    spec = importlib.util.spec_from_file_location("_solar_string_data_single", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


single = _load_single_add()


def _refs(value):
    return (type(value) is list and 1 <= len(value) <= MAX_SELECTED
            and all(type(ref) is str and 1 <= len(ref) <= MAX_REF for ref in value)
            and len(set(value)) == len(value))


def render(document):
    """The kernel's StringData.json text: two-space indent, CRLF line ends, no trailing newline."""
    return json.dumps(document, indent=2, ensure_ascii=False).replace("\n", "\r\n")


def _point(point, scale):
    converted = [point[0] / scale, point[1] / scale]
    if not all(math.isfinite(coordinate) for coordinate in converted):
        raise GraphValidationError("STRING_EDIT_BOUNDS_EXCEEDED")
    return converted


def run(graph, params):
    _bounded_json(params)
    if (type(params) is not dict or not set(params) <= {"string_refs"}
            or ("string_refs" in params and not _refs(params["string_refs"]))):
        raise GraphValidationError("INVALID_STRING_DATA_REQUEST")
    if not units_resolved(graph):
        raise GraphValidationError("UNRESOLVED_UNITS")
    by_id = {s["id"]: s for s in graph["strings"]}
    if "string_refs" in params:
        if any(ref not in by_id for ref in params["string_refs"]):
            raise GraphValidationError("MISSING_STRING")
        selected = [by_id[ref] for ref in params["string_refs"]]
    else:
        selected = list(graph["strings"])
        if len(selected) > MAX_SELECTED:
            raise GraphValidationError("STRING_EDIT_BOUNDS_EXCEEDED")
    scale = graph["project"]["units"]["meters_per_unit"]
    tables = single.slot_tables(graph)
    panel_sur = {p["id"]: format(i + 1, "X") for i, p in enumerate(single.panel_views(graph, tables))}
    frames = graph["frames"]
    try:
        groups = [{"handle": format(k + 1, "X"), "name": f["name"],
                   "panels": [panel_sur[ref] for ref in (tables[f["id"]].ids if f["id"] in tables
                                                         else f["panel_refs"])]}
                  for k, f in enumerate(frames)]
        sent = []
        for i, s in enumerate(selected):
            item = {"handle": format(i + 1, "X"),
                    "panels": [panel_sur[ref] for ref in s["ordered_panel_refs"]], "label": {}}
            if s["route"]:
                item["start"] = {"handle": item["handle"], "at": _point(s["route"][0], scale)}
                item["end"] = {"handle": item["handle"], "at": _point(s["route"][-1], scale)}
            sent.append(item)
    except KeyError:
        raise GraphValidationError("STRING_EDIT_MAPPING_FAILED") from None
    try:
        text = chain.string_data(groups, sent)
    except chain.RooftopBoundsError:
        raise GraphValidationError("STRING_EDIT_BOUNDS_EXCEEDED") from None
    except chain.RooftopInputError:
        raise GraphValidationError("STRING_EDIT_MAPPING_FAILED") from None
    counts = {"selected_strings": len(selected), "groups": len(frames)}
    if text is None:
        if not selected:
            reason = "no-strings"
        elif any(not s["route"] for s in selected):
            reason = "missing-end-markers"
        elif not frames:
            reason = "no-groups"
        else:
            raise GraphValidationError("STRING_EDIT_MAPPING_FAILED")
        return {"status": "no-file", "reason": reason, **counts}
    frame_of = {g["handle"]: (f, g["name"]) for g, f in zip(groups, frames)}
    string_of = {item["handle"]: s for item, s in zip(sent, selected)}
    try:
        data = json.loads(text)
    except ValueError:
        raise GraphValidationError("STRING_EDIT_MAPPING_FAILED") from None
    if type(data) is not dict or set(data) != {"groups"} or type(data["groups"]) is not list \
            or len(data["groups"]) != len(frames):
        raise GraphValidationError("STRING_EDIT_MAPPING_FAILED")
    seen_groups, seen_strings, mapped, grouped = set(), set(), [], 0
    for group in data["groups"]:
        if (type(group) is not dict or set(group) != {"handle", "name", "strings"}
                or type(group["handle"]) is not str or group["handle"] not in frame_of or group["handle"] in seen_groups
                or group["name"] != frame_of[group["handle"]][1]
                or type(group["strings"]) is not list):
            raise GraphValidationError("STRING_EDIT_MAPPING_FAILED")
        seen_groups.add(group["handle"])
        rows = []
        for row in group["strings"]:
            ends = ("startPoint", "endPoint")
            if (type(row) is not dict or set(row) != {"handle", *ends}
                    or type(row["handle"]) is not str or row["handle"] not in string_of or row["handle"] in seen_strings
                    or any(type(row[end]) is not dict or set(row[end]) != {"handle", "coordinate"}
                           or row[end]["handle"] != row["handle"]
                           or type(row[end]["coordinate"]) is not str for end in ends)):
                raise GraphValidationError("STRING_EDIT_MAPPING_FAILED")
            seen_strings.add(row["handle"])
            s = string_of[row["handle"]]
            rows.append({"handle": s["id"],
                         "startPoint": {"handle": s["from_ref"], "coordinate": row["startPoint"]["coordinate"]},
                         "endPoint": {"handle": s["to_ref"], "coordinate": row["endPoint"]["coordinate"]}})
        grouped += len(rows)
        mapped.append({"handle": frame_of[group["handle"]][0]["id"], "name": group["name"], "strings": rows})
    out = render({"groups": mapped})
    summary = {"status": "written", **counts, "grouped_strings": grouped,
               "lines": out.count("\r\n") + 1}
    return solar_artifacts.ArtifactOutput(summary, MEDIA_TYPE, FILENAME, out.encode("utf-8"))
