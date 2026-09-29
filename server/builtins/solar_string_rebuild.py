"""STRINGREBUILD with ALL (BranchCmd.cs:18464-18557) over the W1 design graph, as a read.

The plugin re-reads each string polyline's vertices and re-associates every
vertex with the nearest panel centre, then prints "Rebuilt panel associations
for N string(s)". A Studio string carries no separate polyline: its route is
derived from its panel centres (rule panel-centre-path), so its ordered panel
refs ARE its association and the kernel leaves them standing. The tool reports
the count and changes nothing; it never publishes a drawing version.

Position surrogates (upper-case hex 1..N, per string for panels) let the pure
kernel run without mistaking graph ids for CAD handles. Fails closed: the
request must be empty and the kernel's answer must be exactly the association
it was given. Linear in the total panel count.
"""
import solar_rooftop_chain as chain
from solar_design_graph import GraphValidationError, _bounded_json

TOOL = "solar-string-rebuild"


def run(graph, params):
    _bounded_json(params)
    if type(params) is not dict or params:
        raise GraphValidationError("INVALID_STRING_REBUILD_REQUEST")
    sent = [{"handle": format(i + 1, "X"),
             "panels": [format(j + 1, "X") for j in range(len(s["ordered_panel_refs"]))],
             "label": {}} for i, s in enumerate(graph["strings"])]
    try:
        rebuilt, count = chain.string_rebuild(sent)
    except chain.RooftopBoundsError:
        raise GraphValidationError("STRING_EDIT_BOUNDS_EXCEEDED") from None
    except chain.RooftopInputError:
        raise GraphValidationError("STRING_EDIT_MAPPING_FAILED") from None
    if type(count) is not int or count != len(sent) or rebuilt != sent:
        raise GraphValidationError("STRING_EDIT_MAPPING_FAILED")
    return {"status": "rebuilt", "rebuilt_strings": count,
            "message": f"Rebuilt panel associations for {count} string(s)"}
