"""Read the panel-footprint evidence for saved rooftop feeder routes."""

from solar_design_graph import GraphValidationError
from solar_feeder_audit import audit_feeders


def run(graph, params, *, source_intake=None):
    if type(params) is not dict or params:
        raise GraphValidationError("INVALID_FEEDER_AUDIT_REQUEST")
    return {"schema": "leaf.solar-feeder-audit.v1", **audit_feeders(graph, source_intake)}
