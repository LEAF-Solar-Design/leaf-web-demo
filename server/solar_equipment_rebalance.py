"""Find minimum direct moves from overloaded inverters to spare saved inputs."""

from collections import Counter
from itertools import combinations, islice, product

from solar_design_graph import GraphValidationError, validate_graph
from solar_equipment import FIELDS, equipment_candidate

_POWER_EXCEEDED = "EQUIPMENT_POWER_EXCEEDED"
_MAX_STRINGS = 24
_MAX_INVERTERS = 16
_MAX_PANELS = 2048
_MAX_PROBES = 64


def _candidate(graph, configs, requests):
    return equipment_candidate(graph, {
        "expected_rev": graph["rev"], "equipment": configs, "assignments": requests,
    })


def _slots(inverter, limit):
    occupied = {(item["mppt_letter"], item["input_number"])
                for item in inverter["input_assignments"]}
    return list(islice(((letter, number) for letter, count in sorted(inverter["mppt_inputs"].items())
            for number in range(count) if (letter, number) not in occupied), limit))


def _assignments(graph):
    return {item["string_ref"]: (inverter["id"], item["mppt_letter"], item["input_number"])
            for inverter in graph["inverters"] for item in inverter["input_assignments"]}


def _ready(graph):
    return all(item["validity"]["state"] == "valid"
               for item in graph["inverters"] + graph["strings"])


def minimum_direct_moves(graph):
    """Use saved equipment and canonical validation for each tentative assignment.

    Search complete move counts in ascending order. A direct move goes from an
    overloaded inverter to an initially healthy inverter. The result applies
    to this policy only. Swaps, cascading moves and equipment changes are out
    of scope. A limit hit never yields a feasibility or minimum claim.
    The probe count includes incomplete sender selections and attempted
    destination combinations. Structural limits apply before probing.
    """
    source = validate_graph(graph)
    receipt = {"source_rev": source["rev"], "status": "unavailable", "minimum_moves": None,
               "moves": [], "probes_examined": 0, "probe_limit": _MAX_PROBES}
    strings = source["strings"]
    panel_count = len(source["panels"]) + sum(
        frame["ground_slots"]["count"] for frame in source["frames"] if "ground_slots" in frame)
    if (len(strings) > _MAX_STRINGS or panel_count > _MAX_PANELS
            or len(source["inverters"]) > _MAX_INVERTERS
            or sum(item["module_count"] for item in strings) > _MAX_PANELS):
        return {**receipt, "status": "search_limit"}
    if (not strings or not source["inverters"]
            or any(item["is_l2"] or item.get("l1_assignments") for item in source["inverters"])):
        return receipt
    configs = [{key: item.get(key) for key in FIELDS} for item in source["inverters"]]
    try:
        requests = source["extra"]["equipment"]["assignment_requests"]
        baseline = _candidate(source, configs, requests)
    except (KeyError, GraphValidationError):
        return receipt
    if (_assignments(baseline) != _assignments(source)
            or any(old["inverter_ref"] != new["inverter_ref"]
                   for old, new in zip(strings, baseline["strings"]))):
        return receipt
    if _ready(baseline):
        return {**receipt, "status": "already_balanced", "minimum_moves": 0}
    equipment = baseline["inverters"]
    if any(item["validity"]["state"] != "valid"
           and set(item["validity"]["reasons"]) != {_POWER_EXCEEDED}
           for item in equipment + baseline["strings"]):
        return receipt
    senders = {item["id"] for item in equipment if _POWER_EXCEEDED in item["validity"]["reasons"]}
    candidates = sorted((i for i, item in enumerate(requests) if item["inverter_ref"] in senders),
                        key=lambda i: requests[i]["string_ref"])
    free = {item["id"]: _slots(item, len(candidates)) for item in equipment if item["validity"]["state"] == "valid"}
    receivers = sorted(ref for ref, slots in free.items() if slots)
    tags = {item["id"]: item["circuit_tag"] for item in strings}

    for count in range(len(senders), min(len(candidates), sum(map(len, free.values()))) + 1):
        for indices in combinations(candidates, count):
            if {requests[i]["inverter_ref"] for i in indices} != senders:
                if receipt["probes_examined"] == _MAX_PROBES:
                    return {**receipt, "status": "search_limit"}
                receipt["probes_examined"] += 1
                continue
            for destinations in product(receivers, repeat=count):
                if receipt["probes_examined"] == _MAX_PROBES:
                    return {**receipt, "status": "search_limit"}
                receipt["probes_examined"] += 1
                next_slot = Counter()
                replacements = {}
                for index, receiver in zip(indices, destinations):
                    if next_slot[receiver] == len(free[receiver]):
                        break
                    letter, number = free[receiver][next_slot[receiver]]
                    next_slot[receiver] += 1
                    replacements[index] = {**requests[index], "inverter_ref": receiver,
                                           "mppt_letter": letter, "input_number": number}
                if len(replacements) != count:
                    continue
                trial = [replacements.get(i, item) for i, item in enumerate(requests)]
                if _ready(_candidate(source, configs, trial)):
                    moves = [{"string_ref": requests[i]["string_ref"],
                              "circuit_tag": tags[requests[i]["string_ref"]],
                              "from_inverter_ref": requests[i]["inverter_ref"],
                              "to_inverter_ref": row["inverter_ref"],
                              "mppt_letter": row["mppt_letter"], "input_number": row["input_number"]}
                             for i, row in replacements.items()]
                    return {**receipt, "status": "proposed", "minimum_moves": count, "moves": moves}
    return {**receipt, "status": "no_direct_solution"}
