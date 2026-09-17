"""Required W1 terminal routing candidate for the existing mutation broker."""
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
    raise RuntimeError("solar-homeruns requires the licensed mutation broker")
