"""FlipString (BranchCmd.cs:18570-18769) over the W1 design graph.

The plugin trades marker positions and the record's from/to handles, reverses
panel order, and leaves the label and circuit untouched. Position surrogates
(upper-case hex 1..N) let the pure kernel operate without mistaking graph ids or
shared provenance handles for CAD handles. The inverse mapping is local, linear
in the string's length, with no scan of all panels.

On the graph, reverse the route, trade the ends and any complete polarity pair,
rebuild membership views, and stale only directly dependent routes and schedules.
Length, circuit, inverter and string validity stay unchanged. Fails closed:
every check precedes any mutation, which happens only on a private graph copy.
"""
import copy

import solar_rooftop_chain as chain
from solar_design_graph import GraphValidationError, _bounded_json
from solar_sizing_client import checked_graph
from solar_solve_results import finish_mutation, sync_assignments

TOOL = "solar-string-flip"
MAX_REF = 128
REASON = "string_flipped"


def _invalidate(after, edited):
    sources = set(edited)
    for route in after["routes"]:
        if route["from_ref"] in edited or route["to_ref"] in edited:
            route["validity"] = {"state": "stale", "reasons": [REASON]}
            sources.add(route["id"])
    for schedule in after["schedules"]:
        if not sources.isdisjoint(schedule["source_refs"]):
            schedule["validity"] = {"state": "stale", "reasons": [REASON]}


def flip_string(graph, params):
    _bounded_json(params)
    if (type(params) is not dict or set(params) != {"expected_rev", "string_ref"}
            or type(params["expected_rev"]) is not int
            or type(params["string_ref"]) is not str
            or not 1 <= len(params["string_ref"]) <= MAX_REF):
        raise GraphValidationError("INVALID_STRING_FLIP_REQUEST")
    before = checked_graph(graph, params["expected_rev"])
    ref = params["string_ref"]
    original = next((s for s in before["strings"] if s["id"] == ref), None)
    if original is None:
        raise GraphValidationError("MISSING_STRING")
    refs = original["ordered_panel_refs"]
    surrogates = [format(i + 1, "X") for i in range(len(refs))]
    inverse = dict(zip(surrogates, refs))
    try:
        result = chain.string_flip({"handle": "1", "panels": list(surrogates), "label": {}})
    except chain.RooftopBoundsError:
        raise GraphValidationError("STRING_EDIT_BOUNDS_EXCEEDED") from None
    except chain.RooftopInputError:
        raise GraphValidationError("STRING_EDIT_MAPPING_FAILED") from None
    if (type(result) is not dict or set(result) != {"handle", "panels", "label"}
            or result["handle"] != "1" or result["label"] != {}
            or type(result["panels"]) is not list
            or not all(type(panel) is str for panel in result["panels"])
            or len(result["panels"]) != len(surrogates)
            or set(result["panels"]) != set(surrogates)):
        raise GraphValidationError("STRING_EDIT_MAPPING_FAILED")
    after = copy.deepcopy(before)
    string = next(s for s in after["strings"] if s["id"] == ref)
    string["ordered_panel_refs"] = [inverse[panel] for panel in result["panels"]]
    string["from_ref"], string["to_ref"] = string["to_ref"], string["from_ref"]
    string["route"] = string["route"][::-1]
    polarity = string["extra"].get("polarity")
    if (isinstance(polarity, dict)
            and {"negative_panel_ref", "positive_panel_ref"} <= set(polarity)):
        polarity["negative_panel_ref"], polarity["positive_panel_ref"] = (
            polarity["positive_panel_ref"], polarity["negative_panel_ref"])
    sync_assignments(after)
    _invalidate(after, {ref})
    after = finish_mutation(before, after, TOOL)
    return {"graph": after, "string_ref": ref,
            "ordered_panel_refs": list(string["ordered_panel_refs"])}


OPERATIONS = {"flip-string": flip_string}


def run(intake, params):
    _bounded_json(params)
    if (type(params) is not dict or type(params.get("operation")) is not str
            or params["operation"] not in OPERATIONS):
        raise GraphValidationError("INVALID_STRING_FLIP_REQUEST")
    request = {key: value for key, value in params.items() if key != "operation"}
    return OPERATIONS[params["operation"]](intake, request)["graph"]
