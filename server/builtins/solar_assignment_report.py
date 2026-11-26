"""Read saved string assignments or preview a version-bound AssignStrings plan.

Candidate assignments are tentative, including when the atomic edit would refuse.
The read adapter supplies immutable source and output digests and limits output
bytes. No string identifiers are added to the payload-free exception envelope.
"""
import solar_local_graph
from solar_equipment_rebalance import minimum_direct_moves
from solar_design_graph import GraphValidationError, _bounded_json, require_revision


SAVED_REQUEST_KEYS = frozenset({"mode", "expected_rev", "accept_excess_capacity", "suggest_minimum_moves"})


def _saved_state(graph, targets):
    strings = {string["id"]: string for string in graph["strings"]}
    assignments = [
        {**item, "inverter_ref": inverter["id"], "circuit_tag": strings[item["string_ref"]]["circuit_tag"]}
        for inverter in graph["inverters"] for item in inverter["input_assignments"]
    ]
    collectors = []
    for inverter in sorted(targets, key=lambda item: item["id"]):
        if inverter.get("l1_assignments"):
            continue
        total = int(inverter["total_dc_inputs"])
        occupied = len(inverter["input_assignments"])
        collectors.append({"inverter_ref": inverter["id"], "total_dc_inputs": total,
                           "occupied_inputs": occupied, "spare_inputs": total - occupied})
    return {
        "assignments": sorted(assignments, key=lambda item: item["string_ref"]),
        "unassigned_string_refs": sorted(ref for ref, string in strings.items() if string["inverter_ref"] is None),
        "collector_capacity": collectors,
    }


def run(graph, params):
    assignment = solar_local_graph._load_builtin("solar-assign-strings")
    if type(params) is dict and params.get("mode") == "saved":
        _bounded_json(params)
        if (set(params) - SAVED_REQUEST_KEYS or type(params.get("expected_rev")) is not int
                or type(params.get("accept_excess_capacity", False)) is not bool
                or type(params.get("suggest_minimum_moves", False)) is not bool):
            raise GraphValidationError("INVALID_STRING_ASSIGNMENT_REQUEST")
        before = require_revision(graph, params["expected_rev"])
        targets = assignment._targets(before)
        if not targets:
            raise GraphValidationError("NO_STRING_COLLECTORS")
        result = {
            "schema": "leaf.solar-assignment-report.v1",
            "expected_rev": before["rev"],
            "status": "saved",
            "committed": False,
            "saved_state": _saved_state(before, targets),
            "electrical_suitability": {"status": "unknown", "reason": "mppt_voltage_current_limits_not_verified"},
        }
        if params.get("suggest_minimum_moves", False):
            result["rebalance"] = minimum_direct_moves(before)
        return result
    before, binding, state, rows, assigned, remaining, reason, _ = assignment._assignment_plan(graph, params)
    if reason == "STRING_ASSIGNMENT_MAPPING_FAILED":
        assignment._refuse(reason)
    candidate = (assignment._assignment_candidate(before, binding, state, rows, assigned)
                 if assigned else before)
    strings = {string["id"]: string for string in candidate["strings"]}
    inputs = {item["string_ref"]: item for inverter in candidate["inverters"]
              for item in inverter["input_assignments"]}
    assigned_ids = sorted(binding["strings"][handle]["id"] for handle in assigned)
    remaining_ids = sorted(binding["strings"][handle]["id"] for handle in remaining)
    return {
        "schema": "leaf.solar-assignment-report.v1",
        "expected_rev": before["rev"],
        "status": "partial" if assigned and remaining else "complete" if assigned else "unassigned",
        "committed": False,
        "commit_refusal_code": reason,
        "saved_state": _saved_state(before, assignment._targets(before)),
        "eligible_string_refs": sorted(assigned_ids + remaining_ids),
        "candidate_assignments": [
            {"string_ref": ref, "inverter_ref": strings[ref]["inverter_ref"],
             "circuit_tag": strings[ref]["circuit_tag"], "mppt_letter": inputs[ref]["mppt_letter"],
             "input_number": inputs[ref]["input_number"]} for ref in assigned_ids],
        "remaining_string_refs": remaining_ids,
        "counts": {"eligible": len(assigned_ids) + len(remaining_ids),
                   "candidate_assigned": len(assigned_ids), "remaining": len(remaining_ids)},
    }


def input_readiness(graph):
    assignment = solar_local_graph._load_builtin("solar-assign-strings")
    if assignment._targets(graph):
        return {"input_ready": True, "input_reason": None}
    return assignment.input_readiness(graph)
