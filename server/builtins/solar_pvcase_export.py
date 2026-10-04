"""Export current G33 assignments without solving or requiring electrical sizing."""
import solar_artifacts
import solar_pvcase_outputs
from solar_design_graph import GraphValidationError
from solar_local_graph import validate_pvcase_request, check_pvcase_source, pvcase_conversion_marker
from solar_sizing_client import checked_graph

TOOL = "solar-pvcase-export"
INVALID = "INVALID_PVCASE_EXPORT_REQUEST"


def input_readiness(graph):
    if not pvcase_conversion_marker(graph):
        return {"input_ready": False, "input_reason": "pvcase_conversion_required"}
    marker = graph["extra"].get("pvcase_solve")
    if not isinstance(marker, dict) or marker.get("schema") != "leaf.pvcase-g33-solve.v1" or not graph["strings"]:
        return {"input_ready": False, "input_reason": "pvcase_solve_required"}
    if any(graph[key] for key in ("inverters", "routes", "schedules")):
        return {"input_ready": False, "input_reason": "pvcase_target_in_use"}
    return {"input_ready": True, "input_reason": None}


def run(graph, params, *, pvcase_source=None):
    validate_pvcase_request(TOOL, params)
    try:
        before = checked_graph(graph, graph.get("rev") if isinstance(graph, dict) else None)
    except (ValueError, TypeError, LookupError, AttributeError, ArithmeticError, RecursionError) as error:
        preserved = {"STALE_GRAPH_REVISION", "UNRESOLVED_UNITS", "FRAME_MEMBERSHIP_MISMATCH",
                     "MATRIX_CELL_MISMATCH", "FRAME_SEQUENCE_MISMATCH", "MATRIX_INPUT_MISMATCH"}
        original = error.code if isinstance(error, GraphValidationError) else None
        code = ("MATRIX_INPUT_MISMATCH" if original == "INVERTER_ASSIGNMENT_MISMATCH"
                else original if original in preserved else "PVG_INVALID_TARGET")
        raise GraphValidationError(code, "<root>") from None
    readiness = input_readiness(before)
    if not readiness["input_ready"]:
        raise GraphValidationError(readiness["input_reason"].upper(), "<root>") from None
    check_pvcase_source(before, params, pvcase_source)
    return solar_artifacts.ArtifactOutput(
        {"schema": "leaf.pvcase-g33-export.v1", "panels": len(before["panels"]),
         "strings": len(before["strings"]), "electrical_sizing": "not-evaluated",
         "source": {"artifact_id": pvcase_source["meta"]["artifact_id"],
                    "content_sha256": pvcase_source["meta"]["content_sha256"]}},
        "application/json", "PVcaseAssignments.json",
        solar_pvcase_outputs.assignment_export(before, pvcase_source["envelope"]))
