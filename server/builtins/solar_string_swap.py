"""StringSwap/SwapStrings (BranchCmd.cs:10954-11049, :11340-11423).

Exchange whole circuit identities while panels, markers, handles and geometry
stay put. Each string uses local upper-case hex position surrogates for panels;
handles 1/2 and label slots 1/2 identify the identity donors, never provenance.
Mapping is linear in the strings' lengths with no scan of all panels.

The graph takes each donor's circuit and inverter, redirects inverter input
entries in place, rebuilds membership views, and stales only directly dependent
routes and schedules (including schedules of changed inverters). String and
inverter validity remain untouched. Fails closed: every check precedes any
mutation, and all mutation is confined to a private graph copy.
The stored equipment requests follow the swapped inputs, so assignment intent and inverter inputs agree after a swap.
"""
import copy

import solar_rooftop_chain as chain
from solar_design_graph import GraphValidationError, _bounded_json
from solar_sizing_client import checked_graph
from solar_solve_results import finish_mutation, sync_assignments

TOOL = "solar-string-swap"
MAX_REF = 128
REASON = "string_swapped"


def _invalidate(after, edited, changed_inverters):
    sources = set(edited) | changed_inverters
    for route in after["routes"]:
        if route["from_ref"] in edited or route["to_ref"] in edited:
            route["validity"] = {"state": "stale", "reasons": [REASON]}
            sources.add(route["id"])
    for schedule in after["schedules"]:
        if not sources.isdisjoint(schedule["source_refs"]):
            schedule["validity"] = {"state": "stale", "reasons": [REASON]}


def swap_strings(graph, params):
    _bounded_json(params)
    if (type(params) is not dict or set(params) != {"expected_rev", "string_refs"}
            or type(params["expected_rev"]) is not int
            or type(params["string_refs"]) is not list
            or len(params["string_refs"]) != 2
            or not all(type(ref) is str and 1 <= len(ref) <= MAX_REF
                       for ref in params["string_refs"])):
        raise GraphValidationError("INVALID_STRING_SWAP_REQUEST")
    before = checked_graph(graph, params["expected_rev"])
    refs = params["string_refs"]
    if refs[0] == refs[1]:
        raise GraphValidationError("SAME_STRING_TWICE")
    originals = {s["id"]: s for s in before["strings"] if s["id"] in refs}
    if len(originals) != 2:
        raise GraphValidationError("MISSING_STRING")
    inputs = [{"handle": str(slot),
               "panels": [format(i + 1, "X")
                          for i in range(len(originals[ref]["ordered_panel_refs"]))],
               "label": {"slot": slot}, "circuit": originals[ref]["circuit_tag"]}
              for slot, ref in enumerate(refs, 1)]
    try:
        results = chain.string_swap(*copy.deepcopy(inputs))
    except chain.RooftopBoundsError:
        raise GraphValidationError("STRING_EDIT_BOUNDS_EXCEEDED") from None
    except chain.RooftopInputError:
        raise GraphValidationError("STRING_EDIT_MAPPING_FAILED") from None
    if (not isinstance(results, (list, tuple)) or len(results) != 2
            or any(type(result) is not dict
                   or set(result) != {"handle", "panels", "label", "circuit"}
                   or result["handle"] != source["handle"]
                   or result["panels"] != source["panels"]
                   or type(result["label"]) is not dict
                   or set(result["label"]) != {"slot"}
                   or type(result["label"]["slot"]) is not int
                   or result["label"]["slot"] not in (1, 2)
                   or type(result["circuit"]) is not str
                   for result, source in zip(results, inputs))
            or {result["label"]["slot"] for result in results} != {1, 2}):
        raise GraphValidationError("STRING_EDIT_MAPPING_FAILED")
    after = copy.deepcopy(before)
    strings = {s["id"]: s for s in after["strings"] if s["id"] in refs}
    receivers = {}
    for ref, result in zip(refs, results):
        donor = refs[result["label"]["slot"] - 1]
        strings[ref]["circuit_tag"] = result["circuit"]
        strings[ref]["inverter_ref"] = originals[donor]["inverter_ref"]
        receivers[donor] = ref
    changed_inverters = set()
    for inverter in after["inverters"]:
        for assignment in inverter["input_assignments"]:
            donor = assignment["string_ref"]
            if donor in receivers and receivers[donor] != donor:
                assignment["string_ref"] = receivers[donor]
                changed_inverters.add(inverter["id"])
    equipment = after["extra"].get("equipment")
    if type(equipment) is dict and type(equipment.get("assignment_requests")) is list:
        for request in equipment["assignment_requests"]:
            donor = request.get("string_ref") if type(request) is dict else None
            if type(donor) is str and donor in receivers and receivers[donor] != donor:
                request["string_ref"] = receivers[donor]
    sync_assignments(after)
    _invalidate(after, set(refs), changed_inverters)
    after = finish_mutation(before, after, TOOL)
    return {"graph": after, "string_refs": list(refs)}


OPERATIONS = {"swap-strings": swap_strings}


def run(intake, params):
    _bounded_json(params)
    if (type(params) is not dict or type(params.get("operation")) is not str
            or params["operation"] not in OPERATIONS):
        raise GraphValidationError("INVALID_STRING_SWAP_REQUEST")
    request = {key: value for key, value in params.items() if key != "operation"}
    return OPERATIONS[params["operation"]](intake, request)["graph"]
