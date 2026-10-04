"""Publish unassigned parity strings using current geometry and the immutable G33 witness."""
import solar_pvcase_graph
from solar_design_graph import GraphValidationError
from solar_local_graph import validate_pvcase_request, check_pvcase_source, pvcase_conversion_marker
from solar_sizing_client import checked_graph
from solar_solve_results import finish_mutation

TOOL = "solar-pvcase-solve"
INVALID = "INVALID_PVCASE_SOLVE_REQUEST"


def input_readiness(graph):
    if not pvcase_conversion_marker(graph):
        return {"input_ready": False, "input_reason": "pvcase_conversion_required"}
    if any(graph[key] for key in ("strings", "inverters", "routes", "schedules")):
        return {"input_ready": False, "input_reason": "pvcase_target_in_use"}
    return {"input_ready": True, "input_reason": None}


def run(graph, params, *, pvcase_source=None):
    revision = validate_pvcase_request(TOOL, params)
    try:
        before = checked_graph(graph, revision)
    except (ValueError, TypeError, LookupError, AttributeError, ArithmeticError, RecursionError) as error:
        code = (error.code if isinstance(error, GraphValidationError)
                and error.code in {"STALE_GRAPH_REVISION", "UNRESOLVED_UNITS"} else "PVG_INVALID_TARGET")
        raise GraphValidationError(code, "<root>") from None
    readiness = input_readiness(before)
    if not readiness["input_ready"]:
        raise GraphValidationError(readiness["input_reason"].upper(), "<root>") from None
    check_pvcase_source(before, params, pvcase_source)
    result = solar_pvcase_graph.solve_graph(before, pvcase_source["envelope"])["graph"]
    try:
        return finish_mutation(before, result, TOOL)
    except (ValueError, TypeError, LookupError, AttributeError, ArithmeticError, RecursionError):
        raise GraphValidationError("PVG_INVALID_RESULT", "<root>") from None
