"""Explain a version-bound AssignStrings plan without committing a graph.

Candidate assignments are tentative, including when the atomic edit would refuse.
The read adapter supplies immutable source and output digests and limits output
bytes. No string identifiers are added to the payload-free exception envelope.
"""
import solar_local_graph


def run(graph, params):
    assignment = solar_local_graph._load_builtin("solar-assign-strings")
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
    return solar_local_graph._load_builtin("solar-assign-strings").input_readiness(graph)
