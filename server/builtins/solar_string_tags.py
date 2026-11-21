"""Trusted, pure source-intake annotation producer; never accepts a caller graph."""
from solar_design_graph import GraphValidationError, validate_graph
from solar_sizing_client import digest
from solar_string_tags import prepare_tags


def stored_graph(intake):
    if type(intake) is not dict:
        raise GraphValidationError("GRAPH_NOT_EMBEDDED")
    if "solar_physical_layout" in intake:
        raise GraphValidationError("STRING_TAGS_NATIVE_PARENT_UNSUPPORTED")
    if type(intake.get("solar_design_graph")) is not dict:
        raise GraphValidationError("GRAPH_NOT_EMBEDDED")
    graph = validate_graph(intake["solar_design_graph"])
    if intake.get("solar_design_graph_sha256") != digest(graph):
        raise GraphValidationError("GRAPH_DIGEST_MISMATCH")
    return graph


def run(intake, params):
    plan = prepare_tags(stored_graph(intake), params)
    return {"tag_layer": plan["tag_layer"], "tag_count": len(plan["tags"]),
            "string_tag_plan": plan}, None
