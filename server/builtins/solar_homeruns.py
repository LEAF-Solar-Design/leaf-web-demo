"""Required W1 terminal routing candidate for the existing mutation broker."""
import copy
import hashlib
import math
import re
import uuid

from solar_design_graph import GraphValidationError, _bounded_json, entities
from solar_joint_homerun_routing import MAX_KEEP_OUTS
from solar_saved_aisle_routing import route_saved_aisles
from solar_sizing_client import advance, checked_graph
from solar_solve_results import HOMERUN_KINDS
from solar_wiring_client import licensed_preview, local_routes

_KEEP_OUT_LAYER = re.compile(r"(?<![A-Z0-9])keep[ _-]*outs?(?![A-Z0-9])", re.IGNORECASE)


def _replace_homeruns(graph, routes):
    """Replace only the homerun subset of graph["routes"] with `routes`; return pathways dropped.

    Feeders, trenches and any other non-homerun route stay verbatim, in their order, after the
    regenerated leads. A regenerated lead whose prior lead (same from_ref and route_kind) named a
    pathway keeps that pathway_ref only when its new points equal the prior points; otherwise it
    carries pathway_ref null and counts as dropped. A lead whose prior had no pathway_ref key gets
    none, so W1 graphs stay byte for byte. One pass over routes; mutates graph and routes.
    """
    prior = {}
    for route in graph["routes"]:
        if route["route_kind"] in HOMERUN_KINDS:
            prior.setdefault((route["from_ref"], route["route_kind"]), route)
    dropped = 0
    for route in routes:
        old = prior.get((route["from_ref"], route["route_kind"]))
        if old is None or "pathway_ref" not in old:
            continue
        if old["pathway_ref"] is None or old["points"] == route["points"]:
            route["pathway_ref"] = old["pathway_ref"]
        else:
            route["pathway_ref"] = None
            dropped += 1
    graph["routes"] = routes + [route for route in graph["routes"]
                                if route["route_kind"] not in HOMERUN_KINDS]
    return dropped


def _validate_request(params):
    _bounded_json(params)
    if (type(params) is not dict
            or set(params) - {"expected_rev", "cancel", "routing_constraints", "saved_aisle_layer"}
            or type(params.get("cancel", False)) is not bool
            or ("routing_constraints" in params and type(params["routing_constraints"]) is not dict)):
        raise GraphValidationError("INVALID_ROUTING_REQUEST")
    if "saved_aisle_layer" in params:
        layer = params["saved_aisle_layer"]
        if (type(layer) is not str or not layer.strip() or len(layer) > 128
                or "routing_constraints" in params):
            raise GraphValidationError("INVALID_ROUTING_REQUEST", "saved_aisle_layer")


def _routing_constraints(intake, graph, requested):
    if type(intake) is not dict:
        return requested
    saved = []
    scale = graph["project"]["units"]["meters_per_unit"]
    for polyline in intake.get("polylines", []):
        if type(polyline) is not dict:
            continue
        layer = polyline.get("layer")
        if (polyline.get("space") == "paper" or type(layer) is not str
                or not _KEEP_OUT_LAYER.search(layer)):
            continue
        points = polyline.get("pts")
        bulges = polyline.get("bulges", [])
        invalid = GraphValidationError("INVALID_ROUTING_CONSTRAINTS", "drawing.keep_outs")
        if (polyline.get("closed") is not True or polyline.get("width")
                or type(bulges) is not list or any(bulges)
                or type(points) is not list or len(points) < 3
                or any(type(point) is not list or len(point) < 2 or any(
                    type(value) not in (int, float) or abs(value) > 1e15 or not math.isfinite(value)
                    for value in point[:2]) for point in points)):
            raise invalid
        # The router accepts planar boxes. The envelope of a straight outline
        # keeps every segment outside the drawn area, including rotated outlines.
        x_coords, y_coords = zip(*(point[:2] for point in points))
        saved.append({"min_xy": [min(x_coords) * scale, min(y_coords) * scale],
                      "max_xy": [max(x_coords) * scale, max(y_coords) * scale]})
        if len(saved) > MAX_KEEP_OUTS:
            raise invalid
    if not saved:
        return requested
    constraints = {"keep_outs": [], "aisles": []} if requested is None else requested
    if (set(constraints) != {"keep_outs", "aisles"}
            or any(type(items) is not list for items in constraints.values())):
        raise GraphValidationError("INVALID_ROUTING_CONSTRAINTS", "routing_constraints")
    names = {item["name"] for items in constraints.values() for item in items
             if type(item) is dict and type(item.get("name")) is str}
    ordinal = 0
    for box in saved:
        ordinal += 1
        while f"Drawing keepout {ordinal}" in names:
            ordinal += 1
        box["name"] = f"Drawing keepout {ordinal}"
    return {**constraints, "keep_outs": [*constraints["keep_outs"], *saved]}


def _routes(graph, source, params):
    constraints = _routing_constraints(source, graph, params.get("routing_constraints"))
    if "saved_aisle_layer" not in params:
        return local_routes(graph, constraints)
    return route_saved_aisles(local_routes(graph), source, params["saved_aisle_layer"],
                             graph["project"]["units"]["meters_per_unit"], constraints)


def create_homeruns(intake, params, *, licensed_write=None):
    _validate_request(params)
    graph = checked_graph(intake, params.get("expected_rev"))
    if params.get("cancel", False):
        return {"graph": graph, "cancelled": True}
    routes = _routes(graph, intake, params)
    # Replace only the terminal leads (feeders and trenches stay) and invalidate derived tables.
    dropped = _replace_homeruns(graph, routes)
    changed = list(routes)
    for schedule in graph["schedules"]:
        schedule["validity"] = {"state": "stale", "reasons": ["ROUTES_CHANGED"]}
        changed.append(schedule)
    candidate = advance(graph, changed, "solar-homeruns")
    receipt = licensed_preview(intake, candidate, changed, licensed_write)
    receipt["pathway_refs_dropped"] = dropped
    return {"graph": candidate, "receipt": receipt, "cancelled": False}


def run(intake, params, *, source_intake=None):
    _validate_request(params)
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

    routes = _routes(graph, source_intake, params)
    prior = {}
    for route in graph["routes"]:
        if route["route_kind"] in HOMERUN_KINDS:
            prior.setdefault((route["from_ref"], route["route_kind"]), route)
    strings = {string["id"]: string for string in graph["strings"]}
    # Reserve every surviving identity, including prior leads reused later in circuit order.
    # Only the entities the validator enumerates; extension keys it ignores are not graph ids.
    held_ids = {item["id"] for item in entities(graph)
                if item.get("kind") != "route" or item["route_kind"] not in HOMERUN_KINDS}
    held_ids.update(old["id"] for old in prior.values())
    for route in routes:
        key = (route["from_ref"], route["route_kind"])
        if key in prior:
            route["id"] = prior[key]["id"]
            route["provenance"]["created_at"] = prior[key]["provenance"]["created_at"]
        else:
            seed = "solar-homeruns|" + key[0] + "|" + key[1]
            # At most len(held_ids) candidates can collide; try one more to find a free id.
            for counter in range(len(held_ids) + 1):
                identity = hashlib.sha256(
                    (seed if counter == 0 else seed + "|" + str(counter)).encode("utf-8")).digest()
                candidate = "leaf:route:" + str(uuid.UUID(bytes=identity[:16], version=4))
                if candidate not in held_ids:
                    route["id"] = candidate
                    held_ids.add(candidate)
                    break
            else:
                raise GraphValidationError("INVALID_ROUTING_REQUEST")
            route["provenance"]["created_at"] = strings[key[0]]["provenance"]["created_at"]
    # The local receipt schema is the adapter's; the dropped count is visible as pathway_ref null.
    _replace_homeruns(graph, routes)
    schedules = graph["schedules"]
    for schedule in schedules:
        schedule["validity"] = {"state": "stale", "reasons": ["ROUTES_CHANGED"]}
    return advance(graph, routes + schedules, "solar-homeruns")
