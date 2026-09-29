"""Set explicit per-string conductor choices in one local graph transaction.

Branch2025's HomerunRoutingConfig.cs selects catalog gauges by route length,
OptiHomerunCmd.cs uses that selection for routing, and
StringHomerunExportForm.cs sizes cables per circuit for export. This Studio
producer records the drafter's per-string choice; it never claims automatic
sizing, ampacity certification or voltage-drop compliance.

Validate the complete batch before changing a private copy. Only routes touching
strings whose gauge changed, and schedules naming those strings or routes, become stale.
The generic dependency closure also traverses shared inverters and is too broad.
"""
import copy

from solar_design_graph import GraphValidationError, _bounded_json
from solar_nec import WIRE_LABELS, WIRE_UNITS
from solar_sizing_client import checked_graph
from solar_solve_results import finish_mutation

TOOL = "solar-string-conductors"
WIRE_GAUGES = frozenset(f"{label} {unit}" for label, unit in zip(WIRE_LABELS, WIRE_UNITS))
MAX_ASSIGNMENTS = 4096


def run(intake, params):
    _bounded_json(params)
    required = {"operation", "expected_rev", "assignments"}
    if (type(params) is not dict or not required <= set(params)
            or set(params) - required - {"drawing_id", "cancel"}
            or params["operation"] != "set-conductors"
            or type(params["expected_rev"]) is not int
            or not 0 <= params["expected_rev"] <= 2147483647
            or ("drawing_id" in params and (type(params["drawing_id"]) is not str
                                             or len(params["drawing_id"]) > 128))
            or ("cancel" in params and type(params["cancel"]) is not bool)):
        raise GraphValidationError("INVALID_CONDUCTOR_REQUEST")
    before = checked_graph(intake, params["expected_rev"])
    if params.get("cancel") is True:
        return before

    assignments = params["assignments"]
    # Each phase covers the whole batch, so an earlier missing reference cannot
    # hide a malformed item or unsupported gauge later in the request.
    if (type(assignments) is not list or not 1 <= len(assignments) <= MAX_ASSIGNMENTS
            or any(type(item) is not dict or set(item) != {"string_ref", "wire_gauge"}
                   or type(item["string_ref"]) is not str
                   or not 1 <= len(item["string_ref"]) <= 128
                   or type(item["wire_gauge"]) is not str for item in assignments)):
        raise GraphValidationError("INVALID_CONDUCTOR_REQUEST")
    if any(item["wire_gauge"] not in WIRE_GAUGES for item in assignments):
        raise GraphValidationError("INVALID_WIRE_GAUGE")
    choices = {item["string_ref"]: item["wire_gauge"] for item in assignments}
    if len(choices) != len(assignments):
        raise GraphValidationError("DUPLICATE_STRING_ASSIGNMENT")
    if not choices.keys() <= {string["id"] for string in before["strings"]}:
        raise GraphValidationError("MISSING_STRING")

    after = copy.deepcopy(before)
    changed_strings = set()
    for string in after["strings"]:
        if string["id"] in choices:
            gauge = choices[string["id"]]
            if string["wire_gauge"] != gauge:
                changed_strings.add(string["id"])
            string["wire_gauge"] = gauge
            string["provenance"]["last_writer"] = TOOL
    changed_routes = set()
    for route in after["routes"]:
        if route["from_ref"] in changed_strings or route["to_ref"] in changed_strings:
            route["validity"] = {"state": "stale", "reasons": ["conductor_changed"]}
            changed_routes.add(route["id"])
    changed_sources = changed_strings | changed_routes
    for schedule in after["schedules"]:
        if changed_sources.intersection(schedule["source_refs"]):
            schedule["validity"] = {"state": "stale", "reasons": ["conductor_changed"]}
    return finish_mutation(before, after, TOOL)
