"""Add named panels to one panel group: the Studio counterpart of AddPanel.

Measured plugin semantics (Branch2025 Commands.cs:418, a synchronous modal
command that asks for a panel GROUP, then for a mode, then for the panels,
calling BranchCmd.PanelGroupAddAdditionalPanels()), captured on licensed AutoCAD
2025 on 2026-09-22 (receipts/w2-panel-add-20260922) on a copy of the drawing the
panel-remove capture left behind, driven by handle:

    (command "ADDPANEL" (handent "9CC7") "" <selection set holding panel 93E8> "")

The empty string takes the default Claim, which CLAIMS AN EXISTING PANEL rather
than placing a new one. "Directly adding 1 unassociated panels to group",
"Successfully added 1 panels to Group 6"; after save and reopen dbmod 0, still 11
groups, one more panel grouped (2344 -> 2345) and handle 93E8 at the END of the
group that received it (99 -> 100).

* the command is SCOPED to one group and one selection: only the named panels'
  membership in that group changes, never another group and never the panel;
* it CLAIMS, so the panel it takes is an unassociated one. A panel that already
  belongs to SOME group is refused rather than silently moved: the plugin's own
  wording ("unassociated panels") and its Claim default both say so, and moving
  one would be two mutations under one name;
* membership is EXPLICIT, never geometric. The group that receives a panel is
  the one the caller named, so a panel may join a group the grouping kernel
  would never have put it in. Re-deriving groups is panel-group-create's job;
* the panel keeps everything that is the drawing's rather than the group's: its
  geometry, its electrical zone and its string membership. A panel that is wired
  brings its circuit's sequence into the receiving frame, in the CIRCUIT's order,
  because a frame sequence is a view of the string and not of the selection;
* the group's electrical zone is the zone capability's state, not this one's, so
  a claimed panel joins the group without being pulled into its zone.

Fails closed: every check runs before any mutation, the add runs on the private
copy checked_graph returns, the matrix grows only by whole rows so every panel
already placed keeps its own (row, col), and advance() revalidates the whole
graph, so a half-consistent frame refuses instead of committing. Bounded: one
dict of panels and one map of inverter inputs built once, at most one pass over
the matrix for free slots, and the frame's sequences are rebuilt only for the
circuits the claimed panels actually carry.
"""
from solar_design_graph import GraphValidationError, _bounded_json
from solar_sizing_client import advance, checked_graph

TOOL = "solar-panel-add"
MAX_ADDED = 4096
# The largest membership the groups builtin will commit; a claim never grows a
# group past the size a group could have been created with.
MAX_MEMBERS = 4096
# An empty matrix cell, the shape the groups builtin commits for a slot with no
# panel in it. The geometry fields are the placeholder the frame already uses.
EMPTY_CELL = {"code": "empty", "panel_ref": None, "seq": None, "inverter_id": None,
              "string_input_number": None, "x": 0.0, "y": 0.0, "angle": 0.0}


def _valid_request(params):
    """True for exactly {expected_rev, frame_ref, panel_refs}, no coercion anywhere."""
    if type(params) is not dict or set(params) != {"expected_rev", "frame_ref", "panel_refs"}:
        return False
    refs = params["panel_refs"]
    return (type(params["expected_rev"]) is int and type(params["frame_ref"]) is str
            and params["frame_ref"] and type(refs) is list and 1 <= len(refs) <= MAX_ADDED
            and all(type(ref) is str and ref for ref in refs) and len(set(refs)) == len(refs))


def _free_slots(frame, count):
    """Exactly `count` (row, col) slots for the claimed panels, growing by whole rows.

    Row-major over the cells the frame already has, so a slot a removal emptied is
    reused before the matrix grows at all. Growth appends WHOLE ROWS at the bottom,
    which keeps the matrix rectangular and leaves every placed panel's own
    (row, col) untouched. One bounded pass: the scan stops at `count` free cells and
    the growth loop runs at most ceil(count / module_columns) times.
    """
    columns = frame["module_columns"]
    if type(columns) is not int or columns < 1:
        raise GraphValidationError("INVALID_MATRIX_DIMENSIONS")
    free = []
    for row_number, row in enumerate(frame["matrix"]):
        for column, cell in enumerate(row):
            if cell["panel_ref"] is None:
                free.append((row_number, column))
                if len(free) == count:
                    return free
    while len(free) < count:
        row_number = len(frame["matrix"])
        frame["matrix"].append([dict(EMPTY_CELL) for _ in range(columns)])
        free.extend((row_number, column) for column in range(columns))
    frame["module_rows"] = len(frame["matrix"])
    frame["module_slots"] = frame["module_rows"] * columns
    return free[:count]


def add_panels(graph, params):
    """Claim exactly the named panels into one group; every other group is untouched."""
    _bounded_json(params)
    if not _valid_request(params):
        raise GraphValidationError("INVALID_PANEL_ADD_REQUEST")
    result = checked_graph(graph, params["expected_rev"])
    frame = next((f for f in result["frames"] if f["id"] == params["frame_ref"]), None)
    if frame is None:
        raise GraphValidationError("MISSING_FRAME")
    panels = {panel["id"]: panel for panel in result["panels"]}
    members = set(frame["panel_refs"])
    claimed = []
    for ref in params["panel_refs"]:
        panel = panels.get(ref)
        if panel is None:
            raise GraphValidationError("MISSING_PANEL")
        if panel["frame_ref"] is not None or ref in members:
            # Claim takes an unassociated panel. One that is already in a group,
            # this one included, is refused rather than moved.
            raise GraphValidationError("PANEL_ALREADY_IN_GROUP")
        claimed.append(panel)
    if len(members) + len(claimed) > MAX_MEMBERS:
        raise GraphValidationError("GROUP_TOO_LARGE")
    # Every refusal has run: from here the private copy is mutated.
    inputs = {assignment["string_ref"]: (inverter["id"], assignment["input_number"])
              for inverter in result["inverters"] for assignment in inverter["input_assignments"]}
    slots = _free_slots(frame, len(claimed))
    # A set, not a list: membership is tested once per claimed panel.
    circuits = set()
    for panel, (row_number, column) in zip(claimed, slots):
        assignment = panel["assignment"]
        inverter_id, input_number = inputs.get(assignment["string_ref"], (None, None))
        frame["matrix"][row_number][column] = {
            "code": "panel", "panel_ref": panel["id"], "seq": assignment["seq"],
            "inverter_id": inverter_id, "string_input_number": input_number,
            "x": panel["centre"][0], "y": panel["centre"][1], "angle": panel["angle"]}
        # Geometry, electrical zone and string assignment are the drawing's, not the
        # group's: only these two fields carry membership, so only these two move.
        panel["frame_ref"] = frame["id"]
        panel["matrix_cell"] = {"row": row_number, "col": column}
        frame["panel_refs"].append(panel["id"])
        frame["panel_assignments"].append({
            "panel_ref": panel["id"], "string_ref": assignment["string_ref"],
            "seq": assignment["seq"], "inverter_id": inverter_id,
            "string_input_number": input_number})
        if assignment["string_ref"] is not None:
            circuits.add(assignment["string_ref"])
    if circuits:
        # A frame sequence is a view of the CIRCUIT restricted to the frame's members,
        # so a claimed panel takes the place the circuit gives it, not the one the
        # selection did. Only the circuits the claimed panels carry are rebuilt.
        members = set(frame["panel_refs"])
        sequences = {sequence["string_ref"]: sequence for sequence in frame["sequences"]}
        for string in result["strings"]:
            if string["id"] not in circuits:
                continue
            ordered = [ref for ref in string["ordered_panel_refs"] if ref in members]
            sequence = sequences.get(string["id"])
            if sequence is None:
                frame["sequences"].append({"string_ref": string["id"],
                                           "ordered_panel_refs": ordered})
            else:
                sequence["ordered_panel_refs"] = ordered
    return {"graph": advance(result, [*claimed, frame], TOOL), "frame_ref": frame["id"],
            "added": list(params["panel_refs"]), "total": len(frame["panel_refs"])}


OPERATIONS = {"add-panels": add_panels}


def run(intake, params):
    _bounded_json(params)
    # The operation is named as a string or the request is refused: an unhashable
    # value must never reach the lookup.
    if (type(params) is not dict or type(params.get("operation")) is not str
            or params["operation"] not in OPERATIONS):
        raise GraphValidationError("INVALID_PANEL_ADD_REQUEST")
    request = {key: value for key, value in params.items() if key != "operation"}
    return OPERATIONS[params["operation"]](intake, request)["graph"]
