"""Compare per-string cable lengths across two verified saved drawing versions."""
from decimal import Decimal

import solar_electrical_route_bridge as bridge
import solar_inverter_outputs as kernel
from solar_design_graph import GraphValidationError
from solar_project_graph import resolve_project_graph_context
from solar_sizing_client import units_resolved
from solar_solve_results import require_current_export


def _circuits(graph):
    if not units_resolved(graph):
        raise GraphValidationError("UNRESOLVED_UNITS")
    graph = require_current_export(graph)
    if any(inverter["is_solaredge"] for inverter in graph["inverters"]):
        raise GraphValidationError("SCHEDULES_OPTIMIZERS_UNSUPPORTED")

    try:
        state, binding = bridge.state_from_graph(graph)
    except bridge.ElectricalBridgeError as error:
        raise GraphValidationError(error.code) from None

    # Match Cable Export's drawing-units-to-inches conversion and geometric
    # measurement. Stored length_ft and circuit labels aren't join keys.
    scale = graph["project"]["units"]["meters_per_unit"] / 0.0254
    strings = {string["id"]: string for string in graph["strings"]}
    routes = {
        (route["from_ref"], bridge.SEGMENT_OF[route["route_kind"]]): route
        for route in graph["routes"] if route["route_kind"] in bridge.SEGMENT_OF
    }

    def inches(vertices):
        return kernel.polyline_length([(x * scale, y * scale) for x, y in kernel._points(vertices)])

    try:
        lengths = {
            line["string"]: {None: inches(line["vertices"])}
            for line in state["geometry"]["strings"]
        }
        for row in state["rows"]["cable"]:
            if row["cable_kind"] == "dc-homerun":
                lengths[row["from"]][row["segment"]] = inches(row["vertices"])
    except kernel.InverterOutputError:
        raise GraphValidationError("SCHEDULES_KERNEL_REFUSED") from None

    circuits = {}
    for handle, identity in binding["strings"].items():
        string_id = identity["id"]
        string = strings[string_id]
        parts = lengths[handle]
        legs = [(segment, routes[string_id, segment]) for segment in sorted(parts.keys() - {None})]
        length_inches = parts.get("end", 0) + parts.get("start", 0) + parts[None]
        circuits[string_id] = {
            "label": string["circuit_tag"],
            "length_ft": kernel.cs_fixed(length_inches / 12, 2),
            "route_ids": {segment: route["id"] for segment, route in legs},
            "geometry": (string["route"], [(segment, route["to_ref"], route["points"]) for segment, route in legs]),
        }
    return circuits


def compare_saved_circuit_lengths(before, after):
    """Return changed string circuits for a chronological, adjacent saved version pair."""
    prior = resolve_project_graph_context(before)
    current = resolve_project_graph_context(after)
    if (prior.organization_id, prior.project_id, prior.drawing_id) != (
            current.organization_id, current.project_id, current.drawing_id):
        raise GraphValidationError("CABLE_COMPARISON_SCOPE_MISMATCH")

    previous_graph, next_graph = prior.graph, current.graph
    if previous_graph is None or next_graph is None:
        raise GraphValidationError("CABLE_COMPARISON_GRAPH_REQUIRED")
    if previous_graph["project"]["id"] != next_graph["project"]["id"]:
        raise GraphValidationError("CABLE_COMPARISON_SCOPE_MISMATCH")

    previous_version, next_version = before.binding.version, after.binding.version
    provenance = next_version.provenance
    if (next_version.seq != previous_version.seq + 1
            or not isinstance(provenance, dict)
            or provenance.get("parent_version_id") != str(previous_version.version_id)
            or (prior.graph_sha256 != current.graph_sha256
                and (next_graph["parent_rev"] != previous_graph["rev"]
                     or next_graph["rev"] != previous_graph["rev"] + 1))):
        raise GraphValidationError("CABLE_COMPARISON_PAIR_NOT_ADJACENT")

    old, new = _circuits(previous_graph), _circuits(next_graph)
    changed = []
    for string_id in sorted(old.keys() | new.keys()):
        left, right = old.get(string_id), new.get(string_id)
        if left == right:
            continue
        both = left is not None and right is not None
        changed.append({
            "string_id": string_id,
            "before": {k: v for k, v in left.items() if k != "geometry"} if left is not None else None,
            "after": {k: v for k, v in right.items() if k != "geometry"} if right is not None else None,
            "geometry_changed": left["geometry"] != right["geometry"] if both else None,
            "delta_ft": str(Decimal(right["length_ft"]) - Decimal(left["length_ft"])) if both else None,
        })
    return {
        "before_version_id": str(previous_version.version_id),
        "after_version_id": str(next_version.version_id),
        "changed_circuits": changed,
    }
