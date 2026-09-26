"""Typed W1 circuit schedule for local and licensed persistence."""
import math
import uuid

from solar_design_graph import GraphValidationError, _bounded_json
from solar_sizing_client import advance, checked_graph, digest
from solar_wiring_client import entity, licensed_preview, local_routes, point


def _schedule(intake, params):
    _bounded_json(params)
    if (type(params) is not dict
            or set(params) - {"expected_rev", "insertion_point", "cancel"}
            or type(params.get("cancel", False)) is not bool):
        raise GraphValidationError("INVALID_SCHEDULE_REQUEST")
    graph = checked_graph(intake, params.get("expected_rev"))
    if params.get("cancel", False):
        return graph, None
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
    return graph, schedule


def create_schedule(intake, params, *, licensed_write=None):
    graph, schedule = _schedule(intake, params)
    if schedule is None:
        return {"graph": graph, "cancelled": True}
    graph["schedules"].append(schedule)
    candidate = advance(graph, [schedule], "solar-schedule")
    receipt = licensed_preview(intake, candidate, [schedule], licensed_write)
    return {"graph": candidate, "receipt": receipt, "cancelled": False}


def run(graph, params):
    graph, schedule = _schedule(graph, params)
    if schedule is None:
        return graph
    identity = digest({"tool": "solar-schedule", "source_graph_sha256": digest(graph),
                       "insertion_point": schedule["insertion_point"]})
    schedule["id"] = "leaf:schedule:" + str(
        uuid.UUID(bytes=bytes.fromhex(identity)[:16], version=4))
    lead = next(route for route in graph["routes"]
                if route["from_ref"] == graph["strings"][0]["id"]
                and route["route_kind"] == "start homerun")
    schedule["provenance"]["created_at"] = lead["provenance"]["created_at"]
    graph["schedules"].append(schedule)
    return advance(graph, [schedule], "solar-schedule")
