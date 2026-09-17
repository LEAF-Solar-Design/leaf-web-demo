"""Typed W1 circuit schedule for licensed persistence and versioned reopen."""
import math

from solar_design_graph import GraphValidationError, _bounded_json
from solar_sizing_client import advance, checked_graph
from solar_wiring_client import entity, licensed_preview, local_routes, point


def create_schedule(intake, params, *, licensed_write=None):
    _bounded_json(params)
    if (type(params) is not dict
            or set(params) - {"expected_rev", "insertion_point", "cancel"}
            or type(params.get("cancel", False)) is not bool):
        raise GraphValidationError("INVALID_SCHEDULE_REQUEST")
    graph = checked_graph(intake, params.get("expected_rev"))
    if params.get("cancel", False):
        return {"graph": graph, "cancelled": True}
    insertion = point(params.get("insertion_point"))
    expected = local_routes(graph)
    actual = {(r["from_ref"], r["route_kind"]): r for r in graph["routes"]}
    if len(actual) != len(expected) or len(actual) != len(graph["routes"]):
        raise GraphValidationError("COMPLETE_ROUTING_REQUIRED")
    for route in expected:
        found = actual.get((route["from_ref"], route["route_kind"]))
        if (found is None or found["validity"]["state"] != "valid"
                or any(found[key] != route[key] for key in
                       ("to_ref", "points", "wire_gauge", "point_units", "length_units"))
                or not math.isclose(found["length_ft"], route["length_ft"], rel_tol=1e-9)
                or any(found["extra"].get(key) != route["extra"][key] for key in
                       ("terminal_panel_ref", "mppt_letter", "input_number"))):
            raise GraphValidationError("COMPLETE_ROUTING_REQUIRED")
    rows, sources = [], []
    for string in graph["strings"]:
        leads = [actual[(string["id"], kind)] for kind in ("start homerun", "end homerun")]
        rows.append([string["circuit_tag"], string["module_count"], string["wire_gauge"],
                     leads[0]["length_ft"], leads[1]["length_ft"],
                     sum(r["length_ft"] for r in leads)])
        sources.extend([string["id"], string["inverter_ref"],
                        *string["ordered_panel_refs"], *[r["id"] for r in leads]])
    schedule = entity(
        graph, "schedule", "solar-schedule", rows=rows,
        headers=["Circuit", "Modules", "Conductor", "Start homerun", "End homerun", "Total wire"],
        insertion_point=insertion, layer="LEAF-SCHEDULES", source_rev=graph["rev"],
        source_refs=list(dict.fromkeys(sources)),
        column_units=[None, "count", None, "ft", "ft", "ft"],
        extra={"column_types": ["string", "integer", "string", "number", "number", "number"],
               "row_source_refs": [s["id"] for s in graph["strings"]]})
    graph["schedules"].append(schedule)
    candidate = advance(graph, [schedule], "solar-schedule")
    receipt = licensed_preview(intake, candidate, [schedule], licensed_write)
    return {"graph": candidate, "receipt": receipt, "cancelled": False}


def run(intake, params):
    raise RuntimeError("solar-schedule requires the licensed mutation broker")
