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


def run(intake, params, *, candidate=None):
    from hashlib import sha256
    from uuid import UUID
    from solar_design_graph import validate_graph
    from solar_proposal_candidate import canonical

    if candidate is None:
        raise RuntimeError("solar-commit-solve requires a broker-validated candidate")
    result = commit_solve(intake, params, candidate=candidate)
    if params.get("cancel", False):
        return result
    # The pure kernel allocates fresh UUIDs. Give only newly created strings
    # stable identities at the dispatcher boundary so proof and delivery replay
    # can rerun acceptance without modifying the kernel or global UUID state.
    previous = {string["id"] for string in intake["strings"]}
    basis = sha256(canonical(candidate).encode("utf-8")).hexdigest()
    replacements = {
        string["id"]: "leaf:string:" + str(UUID(bytes=sha256(
            ("leaf.solar-commit-solve.v1:" + basis + ":" + str(index)).encode("utf-8")
        ).digest()[:16], version=4))
        for index, string in enumerate(result["strings"]) if string["id"] not in previous
    }

    def replace(value):
        if type(value) is dict:
            return {key: replace(item) for key, item in value.items()}
        if type(value) is list:
            return [replace(item) for item in value]
        return replacements.get(value, value) if type(value) is str else value

    return validate_graph(replace(result))
