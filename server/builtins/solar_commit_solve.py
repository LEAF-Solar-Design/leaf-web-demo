"""Commit a broker-validated cloud candidate through the existing write owner.

A split frame's candidate carries one proposal per piece ("proposals") and
commits through accept_split_candidate: restitch, plugin string cut, the same
graph mutation.
"""
from solar_solve_results import accept_candidate, accept_split_candidate


def commit_solve(graph, params, *, candidate):
    from solar_design_graph import GraphValidationError, _bounded_json
    from solar_sizing_client import checked_graph

    _bounded_json(params)
    if (type(params) is not dict or set(params) - {"expected_rev", "cancel"}
            or type(params.get("cancel", False)) is not bool):
        raise GraphValidationError("INVALID_COMMIT_REQUEST")
    if params.get("cancel", False):
        return checked_graph(graph, params.get("expected_rev"))
    if type(candidate) is dict and "proposals" in candidate:
        return accept_split_candidate(graph, candidate, expected_rev=params.get("expected_rev"))
    return accept_candidate(graph, candidate, expected_rev=params.get("expected_rev"))


def run(intake, params):
    # Candidate identity and provenance come from the broker's durable job.
    raise RuntimeError("solar-commit-solve requires the cloud mutation broker")
