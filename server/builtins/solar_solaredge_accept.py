"""Accept a stored SolarEdge report's tracking into the design graph (import-solaredge-pdf, piece 3).

The adapter resolves the report server side (trusted input solaredge_report): the stored report
artifact the request names, bound to the exact version and graph this commit runs against. This
builtin validates every association against the graph and writes only
graph["extra"]["solaredge_import"] (server/solar_solaredge_tracking.py), then publishes the next
revision. No entity changes: no string, inverter, device, route, schedule or Solve state is created,
so no design output goes stale. Pure: linear in the report size, no I/O, no clock.
"""
import solar_solaredge_tracking as tracking
from solar_design_graph import GraphValidationError, require_revision
from solar_sizing_client import advance

TOOL = "solar-solaredge-accept"


def run(graph, params, *, solaredge_report=None):
    tracking.validate_request(params)
    result = require_revision(graph, params["expected_rev"])
    if solaredge_report is None:
        raise GraphValidationError(tracking.REPORT_REQUIRED)
    result["extra"][tracking.STORE_KEY] = tracking.import_record(result, params, solaredge_report)
    return advance(result, [], TOOL)
