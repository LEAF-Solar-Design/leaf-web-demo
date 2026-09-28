"""Required W1 terminal routing candidate for the existing mutation broker."""
import copy
import hashlib
import uuid

from solar_design_graph import GraphValidationError, _bounded_json
from solar_sizing_client import advance, checked_graph
from solar_wiring_client import licensed_preview, local_routes


def create_homeruns(intake, params, *, licensed_write=None):
    _bounded_json(params)
    if (type(params) is not dict or set(params) - {"expected_rev", "cancel"}
            or type(params.get("cancel", False)) is not bool):
        raise GraphValidationError("INVALID_ROUTING_REQUEST")
    graph = checked_graph(intake, params.get("expected_rev"))
    if params.get("cancel", False):
        return {"graph": graph, "cancelled": True}
    routes = local_routes(graph)
    # Replace the required terminal leads and invalidate their derived tables.
    graph["routes"] = routes
    changed = list(routes)
    for schedule in graph["schedules"]:
        schedule["validity"] = {"state": "stale", "reasons": ["ROUTES_CHANGED"]}
        changed.append(schedule)
    candidate = advance(graph, changed, "solar-homeruns")
    receipt = licensed_preview(intake, candidate, changed, licensed_write)
    return {"graph": candidate, "receipt": receipt, "cancelled": False}


def run(intake, params):
    _bounded_json(params)
    if (type(params) is not dict or set(params) - {"expected_rev", "cancel"}
            or type(params.get("cancel", False)) is not bool):
        raise GraphValidationError("INVALID_ROUTING_REQUEST")
    graph = checked_graph(intake, params.get("expected_rev"))
    if params.get("cancel", False):
        return graph

    import product_capability_availability as availability

    try:
        readiness = availability.w1_graph_readiness(copy.deepcopy(graph))["solar-homeruns"]
    except (KeyError, ValueError, TypeError):
        raise GraphValidationError("PERSISTED_GRAPH_UNAVAILABLE") from None
    if not readiness["input_ready"]:
        raise GraphValidationError(readiness["input_reason"].upper())

    routes = local_routes(graph)
    prior = {}
    for route in graph["routes"]:
        prior.setdefault((route["from_ref"], route["route_kind"]), route)
    strings = {string["id"]: string for string in graph["strings"]}
    for route in routes:
        key = (route["from_ref"], route["route_kind"])
        if key in prior:
            route["id"] = prior[key]["id"]
            route["provenance"]["created_at"] = prior[key]["provenance"]["created_at"]
        else:
            identity = hashlib.sha256(
                ("solar-homeruns|" + key[0] + "|" + key[1]).encode("utf-8")).digest()
            route["id"] = "leaf:route:" + str(uuid.UUID(bytes=identity[:16], version=4))
            route["provenance"]["created_at"] = strings[key[0]]["provenance"]["created_at"]
    graph["routes"] = routes
    schedules = graph["schedules"]
    for schedule in schedules:
        schedule["validity"] = {"state": "stale", "reasons": ["ROUTES_CHANGED"]}
    return advance(graph, routes + schedules, "solar-homeruns")
