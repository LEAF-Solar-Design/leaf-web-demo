"""Delete every panel group: the Studio counterpart of PanelGroupDeleteAll.

Measured plugin semantics (Branch2025 Commands.cs:608-633, a synchronous modal
command that checks authentication and the hardware config and then calls
BranchCmd.DeleteAllPanelGroups()), captured on licensed AutoCAD 2025 on
2026-09-22 (receipts/w2-group-delete-20260922) against a copy of the drawing the
plugin itself grouped: 11 panel groups before, "Deleted 11 panel group(s)"
printed, 0 groups after, and dbmod 0 with 0 groups after save and reopen.

* the command is drawing-wide and ALL-OR-NOTHING: every group goes, never a
  selection and never a subset;
* the panels survive untouched, and so do the electrical zones and their
  membership: only the groups and the panels' membership IN a group are removed;
* a drawing with no groups is a no-op that says so and leaves dbmod alone, so
  this leaves the graph revision alone too.

Fails closed: the delete runs on the private copy checked_graph returns, and a
surviving reference to a removed group refuses the whole delete rather than
leaving the graph half-deleted. Bounded: validation before any mutation, and one
single pass over the graph for the residual scan, never a rescan per frame.
"""
from solar_design_graph import MAX_NODES, GraphValidationError, _bounded_json
from solar_sizing_client import advance, checked_graph

TOOL = "solar-panel-group-delete"
# A v1 electrical zone carries no link to a group (the frame names its zone, not
# the other way round). These are the names such a link would take; clearing
# them here keeps the delete all-or-nothing if one is ever added, and the
# residual scan below refuses anything this list does not reach.
ZONE_GROUP_LINK_FIELDS = ("frame_ref", "frame_refs", "group_ref", "group_refs")


def _clear_zone_links(zone, removed):
    """Drop a zone's link to a removed group, scalar or list; True when one went."""
    cleared = False
    for field in ZONE_GROUP_LINK_FIELDS:
        value = zone.get(field)
        if type(value) is str and value in removed:
            zone[field] = None
            cleared = True
        elif type(value) is list and any(ref in removed for ref in value):
            zone[field] = [ref for ref in value if ref not in removed]
            cleared = True
    return cleared


def _references(value, removed):
    """True when any string anywhere under value names a removed group.

    One bounded iterative pass, keys included: a removed id used as a mapping key
    is as much a dangling reference as one used as a value.
    """
    stack = [value]
    nodes = 0
    while stack:
        item = stack.pop()
        nodes += 1
        if nodes > MAX_NODES:
            raise GraphValidationError("GRAPH_LIMIT_EXCEEDED")
        if type(item) is dict:
            stack.extend(item)
            stack.extend(item.values())
        elif type(item) is list:
            stack.extend(item)
        elif type(item) is str and item in removed:
            return True
    return False


def delete_all_groups(graph, params):
    """Remove every panel group and every reference to one; panels and zones survive."""
    _bounded_json(params)
    # The request carries one field and nothing else: the command takes no
    # selection, so a caller that names one is refused rather than interpreted.
    if (type(params) is not dict or set(params) != {"expected_rev"}
            or type(params["expected_rev"]) is not int):
        raise GraphValidationError("INVALID_GROUP_DELETE_REQUEST")
    result = checked_graph(graph, params["expected_rev"])
    removed = {frame["id"] for frame in result["frames"]}
    if not removed:
        # An ungrouped drawing: the plugin reports zero and writes nothing, so the
        # revision does not move and the caller's graph comes back unchanged.
        return {"graph": result, "deleted": 0, "panels_cleared": [], "no_op": True}
    changed = []
    for panel in result["panels"]:
        if panel["frame_ref"] is None:
            continue
        if panel["frame_ref"] not in removed:
            raise GraphValidationError("FRAME_MEMBERSHIP_MISMATCH")
        panel["frame_ref"], panel["matrix_cell"] = None, None
        changed.append(panel)
    cleared = [panel["id"] for panel in changed]
    for zone in result["electrical_zones"]:
        if _clear_zone_links(zone, removed):
            changed.append(zone)
    result["frames"] = []
    if _references(result, removed):
        # Half a delete is worse than none: something still names a removed group.
        raise GraphValidationError("GROUP_REFERENCE_NOT_CLEARED")
    return {"graph": advance(result, changed, TOOL), "deleted": len(removed),
            "panels_cleared": cleared, "no_op": False}


OPERATIONS = {"delete-all": delete_all_groups}


def run(intake, params):
    _bounded_json(params)
    # The operation is named as a string or the request is refused: an unhashable
    # value must never reach the lookup.
    if (type(params) is not dict or type(params.get("operation")) is not str
            or params["operation"] not in OPERATIONS):
        raise GraphValidationError("INVALID_GROUP_DELETE_REQUEST")
    request = {key: value for key, value in params.items() if key != "operation"}
    return OPERATIONS[params["operation"]](intake, request)["graph"]
