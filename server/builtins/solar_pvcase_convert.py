"""Convert an immutable admitted G33 source into the empty shared Solar graph."""
import solar_pvcase_conversion
from solar_design_graph import COLLECTIONS, GraphValidationError
from solar_local_graph import validate_pvcase_request, check_pvcase_source
from solar_sizing_client import checked_graph
from solar_solve_results import finish_mutation

TOOL = "solar-pvcase-convert"
INVALID = "INVALID_PVCASE_CONVERT_REQUEST"


def input_readiness(graph):
    extra = graph.get("extra")
    if any(graph[key] for key in COLLECTIONS) or (isinstance(extra, dict) and "pvcase" in extra):
        return {"input_ready": False, "input_reason": "pvcase_empty_target_required"}
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
        raise GraphValidationError("PVCASE_EMPTY_TARGET_REQUIRED", "<root>") from None
    check_pvcase_source(before, params, pvcase_source, converting=True)
    result = solar_pvcase_conversion.convert(
        before, pvcase_source["envelope"],
        source_artifact_id=pvcase_source["meta"]["artifact_id"],
        source_sha256=pvcase_source["meta"]["content_sha256"])
    try:
        return finish_mutation(before, result, TOOL)
    except (ValueError, TypeError, LookupError, AttributeError, ArithmeticError, RecursionError):
        raise GraphValidationError("PVG_INVALID_RESULT", "<root>") from None
