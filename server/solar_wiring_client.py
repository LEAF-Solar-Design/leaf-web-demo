"""Local W1 circuit routing and the licensed broker boundary.

Coordinates in the solar graph are compute meters. Drawing units are resolved
at intake, not applied again here. The broker supplies a licensed preview
callback; the owner publishes its DWG and this candidate using apply_graph_version.
No callback or credential is accepted in user parameters.
"""
from __future__ import annotations

import copy
import math
from datetime import datetime, timezone

from leaf_cloud_grants import CloudError
from solar_design_graph import GraphValidationError, _bounded_json, new_id
from solar_sizing_client import digest


def point(value):
    if (type(value) is not list or len(value) not in (2, 3)
            or any(type(n) not in (int, float) or not math.isfinite(n)
                   or abs(n) > 1e12 for n in value)):
        raise GraphValidationError("INVALID_ROUTE_POINT")
    return [float(n) for n in value] + ([0.0] if len(value) == 2 else [])


def entity(graph, kind, tool, **fields):
    return {"id": new_id(kind), "kind": kind, "rev": graph["rev"],
            "provenance": {"created_by": tool,
                           "created_at": datetime.now(timezone.utc).isoformat(),
                           "last_writer": tool, "source_rev": graph["rev"],
                           "source_hash": graph["source_hash"]},
            "validity": {"state": "valid", "reasons": []}, "extra": {}, **fields}


def local_routes(graph):
    """Produce both terminal leads for every assigned string, in circuit order.

    The conductor is the explicit string conductor choice, never a guessed size.
    Straight terminal leads are the frozen topology's local routing policy.
    """
    panels = {p["id"]: p for p in graph["panels"]}
    inverters = {i["id"]: i for i in graph["inverters"]}
    if not graph["strings"] or len(graph["strings"]) > 4096:
        raise GraphValidationError("ROUTING_TOPOLOGY_REQUIRED")
    routes = []
    for string in graph["strings"]:
        refs = string["ordered_panel_refs"]
        inverter = inverters.get(string["inverter_ref"])
        gauge = string["wire_gauge"]
        if (not refs or inverter is None or type(gauge) is not str
                or not gauge.strip() or len(gauge) > 128
                or string["validity"]["state"] != "valid"
                or inverter["validity"]["state"] != "valid"):
            raise GraphValidationError("ROUTING_TOPOLOGY_REQUIRED")
        assignment = next((a for a in inverter["input_assignments"]
                           if a["string_ref"] == string["id"]), None)
        if assignment is None:
            raise GraphValidationError("INVERTER_ASSIGNMENT_MISMATCH")
        for kind, terminal in (("start homerun", refs[0]), ("end homerun", refs[-1])):
            panel = panels.get(terminal)
            if panel is None or panel["validity"]["state"] != "valid":
                raise GraphValidationError("ROUTING_TOPOLOGY_REQUIRED")
            points = [point(panel["centre"]), point(inverter["position"])]
            length = math.dist(*points)
            if length <= 0:
                raise GraphValidationError("DEGENERATE_ROUTE")
            routes.append(entity(
                graph, "route", "solar-homeruns", route_kind=kind, points=points,
                from_ref=string["id"], to_ref=inverter["id"], wire_gauge=gauge,
                length_ft=length / .3048, point_units="m", length_units="ft",
                extra={"terminal_panel_ref": terminal,
                       "mppt_letter": assignment["mppt_letter"],
                       "input_number": assignment["input_number"],
                       "layer": graph["settings"]["home_run_layer"],
                       "routing_policy": "direct-terminal-lead-v1"}))
    return routes


def check_service_status(status):
    """Broker HTTP status classification, independent of geometric failure."""
    if status == 503:
        raise CloudError("cloud_service_unavailable", 503)
    if status != 200:
        raise CloudError("cloud_upstream_failure", 502)


def licensed_preview(before, candidate, changed, licensed_write):
    """Require exact semantic readback from an isolated licensed DWG preview.

    Adapter input: source graph, candidate graph, changed ids, timeout.
    Reply: candidate_sha256, entities (re-extracted changed entities), and
    application_to_handle. The broker retains DWG bytes for the existing commit
    transaction. This function never publishes a drawing or a graph by itself.
    """
    if licensed_write is None:
        raise GraphValidationError("LICENSED_WRITE_REQUIRED")
    expected = digest(candidate)
    reply = licensed_write(source_graph=copy.deepcopy(before), graph=copy.deepcopy(candidate),
                           changed_ids=[e["id"] for e in changed], timeout=50)
    _bounded_json(reply)
    if (type(reply) is not dict
            or set(reply) != {"candidate_sha256", "entities", "application_to_handle"}
            or reply["candidate_sha256"] != expected
            or reply["entities"] != changed):
        raise GraphValidationError("INVALID_LICENSED_READBACK")
    mapping = reply["application_to_handle"]
    if (type(mapping) is not dict or set(mapping) != {e["id"] for e in changed}
            or any(type(v) is not str or not 1 <= len(v) <= 16
                   or any(c not in "0123456789abcdefABCDEF" for c in v)
                   or int(v, 16) == 0 for v in mapping.values())
            or len({v.upper() for v in mapping.values()}) != len(mapping)):
        raise GraphValidationError("INVALID_LICENSED_READBACK")
    return {"candidate_sha256": expected, "application_to_handle": copy.deepcopy(mapping)}
