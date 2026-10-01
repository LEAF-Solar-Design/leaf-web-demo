"""Split selected panels using the MULTISTRING combination.

Plugin: BranchCmd.cs:17197 AddMultiString calls :17333
SolveManualStringAsync(useSingleString: false), refuses the sentinel at :17396
("Invalid Panel Count"), and flattens Sequences at :17446.
Declared divergence: no solver call; the caller's order is the walk and the
combination decides the cut. The receipt's rows flattened in order reproduce the
plugin's commit. Longer chunks come first, with a new_id("string") per chunk;
S-number tags advance from settings, skipping existing tags.

Fails closed: every check before any mutation of the caller's state. One pass
over panels and strings per commit in the builder, no rescan per string; the
caller's graph and params are never mutated. All strings commit in one revision.
"""
import copy
from datetime import datetime, timezone
import importlib.util
import math
from pathlib import Path

import solar_string_combo as combo
from solar_design_graph import GraphValidationError, _bounded_json, new_id
from solar_sizing_client import checked_graph
from solar_solve_results import finish_mutation, invalidate_dependents, sync_assignments

TOOL = "solar-string-multi-add"
MAX_REF = 128
# One AutoCAD selection set; also bounds the strings created.
MAX_MEMBERS = 900


def _load_single_add():
    path = Path(__file__).resolve().with_name("solar_string_add.py")
    spec = importlib.util.spec_from_file_location("_solar_string_multi_add_single", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


single = _load_single_add()


def _valid_request(params):
    if type(params) is not dict or set(params) != {
            "expected_rev", "string_length", "ordered_panel_refs"}:
        return False
    refs = params["ordered_panel_refs"]
    return (type(params["expected_rev"]) is int
            and 0 <= params["expected_rev"] <= 2147483647
            and type(params["string_length"]) is int
            and 1 <= params["string_length"] <= combo.MAX_TARGET
            and type(refs) is list and 1 <= len(refs) <= MAX_MEMBERS
            and all(type(ref) is str and 1 <= len(ref) <= MAX_REF for ref in refs))


def _sizes(string_length, count):
    try:
        seq = combo.sequences(string_length, count)
    except combo.StringComboError:
        raise GraphValidationError("INVALID_STRING_MULTI_ADD_REQUEST") from None
    if combo.is_sentinel([seq[:2], seq[2:]]):
        raise GraphValidationError("MULTI_ADD_INFEASIBLE_COUNT")
    sizes = [seq[0]] * seq[2] + [seq[1]] * seq[3]
    if any(size < 1 or size > string_length for size in sizes) or sum(sizes) != count:
        raise GraphValidationError("MULTI_ADD_INFEASIBLE_COUNT")
    return sizes


def _new_strings(graph, chunks):
    """Build and append circuits on the private copy, sharing maps and timestamp."""
    panels = {panel["id"]: panel for panel in single.panel_views(graph)}
    tags = {string["circuit_tag"] for string in graph["strings"]}
    number = int(graph["settings"]["string_number"])
    stamp = datetime.now(timezone.utc).isoformat()
    strings = []
    for refs in chunks:
        while f"S{number}" in tags:
            number += 1
        if number >= single.MAX_STRING_NUMBER:
            raise GraphValidationError("STRING_NUMBER_EXHAUSTED")
        tag = f"S{number}"
        tags.add(tag)
        number += 1
        points = [copy.deepcopy(panels[ref]["centre"]) for ref in refs]
        length_m = sum(math.dist(a + [0] * (3 - len(a)), b + [0] * (3 - len(b)))
                       for a, b in zip(points, points[1:]))
        string = {
            "id": new_id("string"), "kind": "string", "rev": graph["rev"],
            "validity": {"state": "valid", "reasons": []},
            "provenance": {"created_by": TOOL, "created_at": stamp,
                           "last_writer": TOOL, "source_rev": graph["rev"],
                           "source_hash": graph["source_hash"],
                           "catalog_versions": copy.deepcopy(graph["catalog_versions"])},
            "extra": {
                "polarity": {"source": "derived",
                             "rule": "ordered-selection-first-negative-last-positive",
                             "negative_panel_ref": refs[0], "positive_panel_ref": refs[-1],
                             "source_rev": graph["rev"]},
                "length_provenance": {"source": "derived", "rule": "panel-centre-path",
                                      "point_units": "m", "length_units": "ft",
                                      "includes_home_runs": False},
            },
            "circuit_tag": tag, "circuit_kind": "String",
            "ordered_panel_refs": list(refs), "module_count": len(refs),
            "from_ref": refs[0], "to_ref": refs[-1], "tag_text_ref": None, "wire_gauge": "",
            "length_ft": length_m / single.METRES_PER_FOOT, "route": points,
            "inverter_ref": None,
        }
        graph["strings"].append(string)
        strings.append(string)
    while f"S{number}" in tags:
        number += 1
    if number > single.MAX_STRING_NUMBER:
        raise GraphValidationError("STRING_NUMBER_EXHAUSTED")
    graph["settings"]["string_number"] = number
    return strings


def add_strings(graph, params):
    _bounded_json(params)
    if not _valid_request(params):
        raise GraphValidationError("INVALID_STRING_MULTI_ADD_REQUEST")
    refs = params["ordered_panel_refs"]
    if len(set(refs)) != len(refs):
        raise GraphValidationError("DUPLICATE_PANEL_MEMBERSHIP")
    before = checked_graph(graph, params["expected_rev"])
    if params["string_length"] > single.max_string_length(before):
        raise GraphValidationError("STRING_TOO_LONG")
    panels = {panel["id"] for panel in single.panel_views(before)}
    if not set(refs) <= panels:
        raise GraphValidationError("MISSING_PANEL")
    wired = {ref for string in before["strings"] for ref in string["ordered_panel_refs"]}
    if not wired.isdisjoint(refs):
        raise GraphValidationError("PANEL_ALREADY_ASSIGNED")
    sizes = _sizes(params["string_length"], len(refs))
    try:
        seq = combo.sequences(params["string_length"], len(refs))
    except combo.StringComboError:
        raise GraphValidationError("INVALID_STRING_MULTI_ADD_REQUEST") from None
    chunks = []
    offset = 0
    for size in sizes:
        chunks.append(refs[offset:offset + size])
        offset += size
    result = copy.deepcopy(before)
    strings = _new_strings(result, chunks)
    new_ids = [string["id"] for string in strings]
    sync_assignments(result)
    invalidate_dependents(before, result, [*new_ids, *refs], solved_ids=set(new_ids))
    after = finish_mutation(before, result, TOOL)
    return {"graph": after, "string_refs": new_ids,
            "circuit_tags": [string["circuit_tag"] for string in strings],
            "sequences": seq, "ordered_panel_refs": chunks, "total": len(after["strings"])}


OPERATIONS = {"add-strings": add_strings}


def run(intake, params):
    _bounded_json(params)
    if (type(params) is not dict or type(params.get("operation")) is not str
            or params["operation"] not in OPERATIONS):
        raise GraphValidationError("INVALID_STRING_MULTI_ADD_REQUEST")
    request = {key: value for key, value in params.items() if key != "operation"}
    return OPERATIONS[params["operation"]](intake, request)["graph"]
