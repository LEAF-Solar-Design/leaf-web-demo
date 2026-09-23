"""Apply auto-fill's corrections to group membership: the Studio counterpart of AUTOFILL.

Measured plugin semantics (Branch2025 Commands.cs:448-476, a modal command that
scans every panel group and rebalances membership, calling
BranchCmd.AutoFillAllPanelGroups(), BranchCmd.cs:3606), captured on licensed
AutoCAD 2025 on 2026-09-23 (receipts/w2-autofill-20260923) on a copy of the
plugin-grouped rooftop drawing whose 99-panel group a REMOVEPANEL had just cut to
71, an infeasible count at string length 14:

    (command "AUTOFILL")

"Auto-fill complete: 2 groups modified, 1 panels moved." and
"Correction: OptimalSolver: Group 6 -> Group 8 (1 panels, d=1027, direct)". The
LOCAL OptimalPlanSolver ran, no cloud call, every SectionWorkflow section
skipped. After save and reopen dbmod 0: the 71-panel group had lost exactly one
panel, handle 8201, and the 137-panel group had gained it (70 and 138). Every
other group was byte-identical.

* auto-fill MOVES membership and nothing else. A moved panel keeps its geometry,
  its electrical zone and its string membership; only frame membership changes,
  the same two fields panel-remove and panel-add move;
* a correction is one LOCAL trade between two groups. A chain routed through an
  intermediate group arrives here as several corrections in the solver's own
  order, and they are applied in that order, so an intermediate group can hand on
  a panel it has just received;
* the receiving group's matrix reuses the slots the donation emptied before it
  grows, and it grows only by whole rows, so every panel already placed keeps its
  own (row, col), exactly as panel-add does;
* a group is never emptied. The groups builtin commits no group with fewer than
  two panels, so a correction that would take a group below that is refused and
  panel-group-delete stays the way to remove one. This is a deliberate divergence
  from the solver, whose DP may choose a target of 0 (OptimalPlanSolver.cs:555);
  on the captured run no group drains.

The plan itself is server/solar_autofill.py, the pure port of OptimalPlanSolver.
This builtin only applies it.

Fails closed: every correction is validated against a simulated membership BEFORE
any mutation, so a plan that goes wrong on its third hop moves nothing at all; the
mutation runs on the private copy checked_graph returns; and advance() revalidates
the whole graph. Bounded: one dict of panels, one map of inverter inputs and one
membership map built once for the whole plan, at most one pass over a frame's
matrix per correction, and the receiving frame's sequences are rebuilt only for
the circuits the moved panels actually carry.
"""
from solar_design_graph import GraphValidationError, _bounded_json
from solar_sizing_client import advance, checked_graph

TOOL = "solar-autofill"
MAX_CORRECTIONS = 4096
MAX_MOVED = 4096
# The largest membership the groups builtin will commit (builtins/solar_panel_add.py:48).
MAX_MEMBERS = 4096
# The smallest membership the groups builtin will commit; a donation never takes a
# group below it (builtins/solar_panel_remove.py, "GROUP_WOULD_BE_EMPTY").
MIN_MEMBERS = 2
# An emptied matrix cell, the shape the groups builtin commits for a slot with no
# panel in it, identical to builtins/solar_panel_remove.py:37 and
# builtins/solar_panel_add.py:51. The geometry fields are the placeholder the
# frame already uses.
EMPTY_CELL = {"code": "empty", "panel_ref": None, "seq": None, "inverter_id": None,
              "string_input_number": None, "x": 0.0, "y": 0.0, "angle": 0.0}


def _valid_request(params):
    """True for exactly {expected_rev, corrections}, no coercion anywhere."""
    if type(params) is not dict or set(params) != {"expected_rev", "corrections"}:
        return False
    corrections = params["corrections"]
    if type(params["expected_rev"]) is not int or type(corrections) is not list:
        return False
    if not 1 <= len(corrections) <= MAX_CORRECTIONS:
        return False
    for correction in corrections:
        if type(correction) is not dict or set(correction) != {"from_ref", "to_ref", "panel_refs"}:
            return False
        refs = correction["panel_refs"]
        if (type(correction["from_ref"]) is not str or not correction["from_ref"]
                or type(correction["to_ref"]) is not str or not correction["to_ref"]
                or correction["from_ref"] == correction["to_ref"]):
            return False
        if (type(refs) is not list or not 1 <= len(refs) <= MAX_MOVED
                or not all(type(ref) is str and ref for ref in refs)
                or len(set(refs)) != len(refs)):
            return False
    return True


def _free_slots(frame, count):
    """Exactly `count` (row, col) slots for the arriving panels, growing by whole rows.

    The same allocation panel-add makes (builtins/solar_panel_add.py:65-90): row-major
    over the cells the frame already has, so a slot the donation emptied is reused
    before the matrix grows at all, and growth appends WHOLE ROWS at the bottom so
    every placed panel keeps its own (row, col). One bounded pass: the scan stops at
    `count` free cells and the growth loop runs at most ceil(count / module_columns)
    times.
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


def _checked_plan(frames, corrections):
    """Refuse the whole plan before anything moves, by replaying it on a membership map.

    The replay is the only way to judge a CHAIN: the second hop's donor is a group
    the first hop has already changed, so checking each correction against the
    graph as it stands would reject a legal plan and accept an illegal one.
    """
    membership = {frame_id: set(frame["panel_refs"]) for frame_id, frame in frames.items()}
    for correction in corrections:
        source = frames.get(correction["from_ref"])
        target = frames.get(correction["to_ref"])
        if source is None or target is None:
            raise GraphValidationError("MISSING_FRAME")
        moved = set(correction["panel_refs"])
        held = membership[source["id"]]
        if not moved <= held:
            # A panel the donating group does not hold, including one held by a
            # DIFFERENT group: a correction is scoped to the pair it names.
            raise GraphValidationError("PANEL_NOT_IN_GROUP")
        if len(held) - len(moved) < MIN_MEMBERS:
            raise GraphValidationError("GROUP_WOULD_BE_EMPTY")
        if len(membership[target["id"]]) + len(moved) > MAX_MEMBERS:
            raise GraphValidationError("GROUP_TOO_LARGE")
        held -= moved
        membership[target["id"]] |= moved


def _donate(frame, moved):
    """Drop `moved` from one frame: membership, matrix cells, assignments, sequences."""
    frame["panel_refs"] = [ref for ref in frame["panel_refs"] if ref not in moved]
    # The matrix keeps its dimensions, so module_rows, module_columns and
    # module_slots stay true and every remaining panel keeps its own cell.
    for row in frame["matrix"]:
        for column, cell in enumerate(row):
            if cell["panel_ref"] in moved:
                row[column] = dict(EMPTY_CELL)
    frame["panel_assignments"] = [assignment for assignment in frame["panel_assignments"]
                                  if assignment["panel_ref"] not in moved]
    sequences = []
    for sequence in frame["sequences"]:
        ordered = [ref for ref in sequence["ordered_panel_refs"] if ref not in moved]
        if ordered:
            sequences.append({**sequence, "ordered_panel_refs": ordered})
    frame["sequences"] = sequences


def _receive(frame, arriving, inputs, strings):
    """Place `arriving` panels in one frame, in the order the correction names them."""
    slots = _free_slots(frame, len(arriving))
    circuits = set()
    for panel, (row_number, column) in zip(arriving, slots):
        assignment = panel["assignment"]
        inverter_id, input_number = inputs.get(assignment["string_ref"], (None, None))
        frame["matrix"][row_number][column] = {
            "code": "panel", "panel_ref": panel["id"], "seq": assignment["seq"],
            "inverter_id": inverter_id, "string_input_number": input_number,
            "x": panel["centre"][0], "y": panel["centre"][1], "angle": panel["angle"]}
        panel["frame_ref"] = frame["id"]
        panel["matrix_cell"] = {"row": row_number, "col": column}
        frame["panel_refs"].append(panel["id"])
        frame["panel_assignments"].append({
            "panel_ref": panel["id"], "string_ref": assignment["string_ref"],
            "seq": assignment["seq"], "inverter_id": inverter_id,
            "string_input_number": input_number})
        if assignment["string_ref"] is not None:
            circuits.add(assignment["string_ref"])
    if not circuits:
        return
    # A frame sequence is a view of the CIRCUIT restricted to the frame's members,
    # so a moved panel takes the place the circuit gives it, not the one the
    # correction did. Only the circuits the moved panels carry are rebuilt.
    members = set(frame["panel_refs"])
    sequences = {sequence["string_ref"]: sequence for sequence in frame["sequences"]}
    for string_id in circuits:
        string = strings.get(string_id)
        if string is None:
            raise GraphValidationError("MISSING_STRING")
        ordered = [ref for ref in string["ordered_panel_refs"] if ref in members]
        sequence = sequences.get(string_id)
        if sequence is None:
            frame["sequences"].append({"string_ref": string_id, "ordered_panel_refs": ordered})
        else:
            sequence["ordered_panel_refs"] = ordered


def apply_corrections(graph, params):
    """Move exactly the panels each correction names, in the solver's own order."""
    _bounded_json(params)
    if not _valid_request(params):
        raise GraphValidationError("INVALID_AUTOFILL_REQUEST")
    result = checked_graph(graph, params["expected_rev"])
    frames = {frame["id"]: frame for frame in result["frames"]}
    panels = {panel["id"]: panel for panel in result["panels"]}
    corrections = params["corrections"]
    _checked_plan(frames, corrections)
    for correction in corrections:
        for ref in correction["panel_refs"]:
            panel = panels.get(ref)
            if panel is None:
                raise GraphValidationError("MISSING_PANEL")
    # Every refusal has run: from here the private copy is mutated.
    inputs = {assignment["string_ref"]: (inverter["id"], assignment["input_number"])
              for inverter in result["inverters"] for assignment in inverter["input_assignments"]}
    strings = {string["id"]: string for string in result["strings"]}
    changed = {}
    for correction in corrections:
        source = frames[correction["from_ref"]]
        target = frames[correction["to_ref"]]
        arriving = [panels[ref] for ref in correction["panel_refs"]]
        moved = set(correction["panel_refs"])
        for panel in arriving:
            if panel["frame_ref"] != source["id"]:
                raise GraphValidationError("FRAME_MEMBERSHIP_MISMATCH")
            panel["frame_ref"], panel["matrix_cell"] = None, None
        _donate(source, moved)
        _receive(target, arriving, inputs, strings)
        for entity in (source, target, *arriving):
            changed[entity["id"]] = entity
    moved_total = sum(len(correction["panel_refs"]) for correction in corrections)
    return {"graph": advance(result, list(changed.values()), TOOL),
            "corrections": len(corrections), "panels_moved": moved_total,
            "groups_modified": sorted({correction[key] for correction in corrections
                                       for key in ("from_ref", "to_ref")})}


OPERATIONS = {"apply-corrections": apply_corrections}


def run(intake, params):
    _bounded_json(params)
    # The operation is named as a string or the request is refused: an unhashable
    # value must never reach the lookup.
    if (type(params) is not dict or type(params.get("operation")) is not str
            or params["operation"] not in OPERATIONS):
        raise GraphValidationError("INVALID_AUTOFILL_REQUEST")
    request = {key: value for key, value in params.items() if key != "operation"}
    return OPERATIONS[params["operation"]](intake, request)["graph"]
