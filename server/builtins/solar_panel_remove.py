"""Remove named panels from one panel group: the Studio counterpart of RemovePanel.

Measured plugin semantics (Branch2025 Commands.cs:634-659, a synchronous modal
command that asks for a panel GROUP and then for the panels to remove from it,
calling BranchCmd.PanelGroupRemoveAdditionalPanels()), captured on licensed
AutoCAD 2025 on 2026-09-22 (receipts/w2-panel-remove-20260922) on a copy of the
drawing the plugin itself grouped, driven by handle:

    (command "REMOVEPANEL" (handent "9C93") <selection set holding panel 93E8> "")

"Removing 1 panels from Group 1", "Successfully removed 1 panels from Group 1",
"555 panels remain in the group"; after save and reopen dbmod 0, still 11 groups,
one fewer panel in the largest group (556 -> 555) and handle 93E8 in NO group.

* the command is SCOPED to one group and one selection: only the named panels'
  membership in that group changes, never another group and never the panel;
* the panel itself survives in the drawing with its geometry, its electrical
  zone and its string membership untouched: only frame membership goes;
* the group stays NON-EMPTY. The plugin's own "N panels remain in the group"
  wording is a report about a group that still exists, and the group schema the
  groups builtin commits never holds fewer than two panels, so emptying a group
  is refused here and PanelGroupDeleteAll remains the way to remove one.

Fails closed: every check runs before any mutation, the removal runs on the
private copy checked_graph returns, and a surviving reference to a removed panel
anywhere under the frame refuses the whole removal rather than leaving the frame
half-updated. Bounded: one pass per structure, no rescan per panel, and the
residual scan walks the frame once with an explicit node cap.
"""
from solar_design_graph import MAX_NODES, GraphValidationError, _bounded_json
from solar_sizing_client import advance, checked_graph

TOOL = "solar-panel-remove"
MAX_REMOVED = 4096
# An emptied matrix cell, the shape the groups builtin commits for a slot with no
# panel in it. The geometry fields are the placeholder the frame already uses.
EMPTY_CELL = {"code": "empty", "panel_ref": None, "seq": None, "inverter_id": None,
              "string_input_number": None, "x": 0.0, "y": 0.0, "angle": 0.0}


def _references(value, removed):
    """True when any string anywhere under value names a removed panel.

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


def _valid_request(params):
    """True for exactly {expected_rev, frame_ref, panel_refs}, no coercion anywhere."""
    if type(params) is not dict or set(params) != {"expected_rev", "frame_ref", "panel_refs"}:
        return False
    refs = params["panel_refs"]
    return (type(params["expected_rev"]) is int and type(params["frame_ref"]) is str
            and params["frame_ref"] and type(refs) is list and 1 <= len(refs) <= MAX_REMOVED
            and all(type(ref) is str for ref in refs) and len(set(refs)) == len(refs))


def remove_panels(graph, params):
    """Drop exactly the named panels from one group; the panels and every other group survive."""
    _bounded_json(params)
    if not _valid_request(params):
        raise GraphValidationError("INVALID_PANEL_REMOVE_REQUEST")
    result = checked_graph(graph, params["expected_rev"])
    frame = next((f for f in result["frames"] if f["id"] == params["frame_ref"]), None)
    if frame is None:
        raise GraphValidationError("MISSING_FRAME")
    removed = set(params["panel_refs"])
    members = set(frame["panel_refs"])
    if not removed <= members:
        # A panel the group does not hold, including one held by a DIFFERENT group:
        # the command is scoped to the group the caller named.
        raise GraphValidationError("PANEL_NOT_IN_GROUP")
    if removed == members:
        raise GraphValidationError("GROUP_WOULD_BE_EMPTY")
    panels = {panel["id"]: panel for panel in result["panels"]}
    changed = []
    for ref in params["panel_refs"]:
        panel = panels.get(ref)
        if panel is None or panel["frame_ref"] != frame["id"]:
            raise GraphValidationError("FRAME_MEMBERSHIP_MISMATCH")
        # Geometry, electrical zone and string assignment are the drawing's, not the
        # group's: only these two fields carry membership, so only these two move.
        panel["frame_ref"], panel["matrix_cell"] = None, None
        changed.append(panel)
    frame["panel_refs"] = [ref for ref in frame["panel_refs"] if ref not in removed]
    # The matrix keeps its dimensions, so module_rows, module_columns and
    # module_slots stay true and every remaining panel keeps its own cell: the
    # vacated slots simply become empty, exactly as a group built with a gap has.
    for row in frame["matrix"]:
        for column, cell in enumerate(row):
            if cell["panel_ref"] in removed:
                row[column] = dict(EMPTY_CELL)
    frame["panel_assignments"] = [assignment for assignment in frame["panel_assignments"]
                                  if assignment["panel_ref"] not in removed]
    sequences = []
    for sequence in frame["sequences"]:
        ordered = [ref for ref in sequence["ordered_panel_refs"] if ref not in removed]
        if ordered:
            sequences.append({**sequence, "ordered_panel_refs": ordered})
    frame["sequences"] = sequences
    if _references(frame, removed):
        # Half a removal is worse than none: something in the frame still names a
        # panel that is no longer one of its members.
        raise GraphValidationError("PANEL_REFERENCE_NOT_CLEARED")
    changed.append(frame)
    return {"graph": advance(result, changed, TOOL), "frame_ref": frame["id"],
            "removed": list(params["panel_refs"]), "remaining": len(frame["panel_refs"])}


OPERATIONS = {"remove-panels": remove_panels}


def run(intake, params):
    _bounded_json(params)
    # The operation is named as a string or the request is refused: an unhashable
    # value must never reach the lookup.
    if (type(params) is not dict or type(params.get("operation")) is not str
            or params["operation"] not in OPERATIONS):
        raise GraphValidationError("INVALID_PANEL_REMOVE_REQUEST")
    request = {key: value for key, value in params.items() if key != "operation"}
    return OPERATIONS[params["operation"]](intake, request)["graph"]
