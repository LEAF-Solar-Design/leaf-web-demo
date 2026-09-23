"""Studio's PVcase solve (contract G33): a literal port of the plugin's LEAFPVCASESOLVE.

Ported from Branch2025 (paths relative to its root; line numbers at the S64 base):
  LeafPvcaseSolveCommand.cs:41-126        the command flow (read, build inputs, solve, write back)
  LeafPvcaseSolveCommand.cs:129-143       BuildHandleAssignmentMap
  LeafPvcaseSolveCommand.cs:146-170       ApplyAssignmentsToMatrix
  CombinerAutoCmd.cs:4041-4132            BuildPanelGroupInputsWithDiagnostics (the solver's inputs)
  LeafSolarDesign.Core/PvcaseSolver.cs:55 DefaultPanelsPerString = 12
  LeafSolarDesign.Core/PvcaseSolver.cs:72-156   PvcaseSolver.Solve
  LeafSolarDesign.Core/PvcaseSolver.cs:158-174  NearestL2

The flow, exactly as the plugin runs it:
  1. the panel groups in the order ReadAllPanelGroupData returns them (the intake lists them in that
     order); none -> "no panel groups found", nothing is written (cs:57-64);
  2. BuildPanelGroupInputsWithDiagnostics: per group, every non-null cell with Code != 0 becomes a
     panel {handle Id, centre (X, Y), RowIndex r, ColIndex c}; a group with no surviving panel is
     dropped with a reason; no group survives -> "no usable panels", nothing is written (cs:66-75);
  3. the L2 snapshot (cs:80-91): every registered L2 inverter's number and insertion point. The G33
     intake carries none (the capture registers no L2), so the snapshot is empty: the zero L2 case;
  4. panels per string = the StringLength setting, or DefaultPanelsPerString when it is < 1 (cs:94-95);
  5. Solve: per group in input order, the panels ordered by (RowIndex, ColIndex) (a stable sort, as
     LINQ OrderBy.ThenBy is), cut into consecutive runs of panels-per-string; each run goes to the L2
     nearest its centroid (the first strictly nearer one in list order) or to L2 1 when no L2 carries
     a number > 0 (the zero L2 case), and takes that L2's next string number, counted ACROSS groups;
  6. BuildHandleAssignmentMap: handle -> (L2, string), ordinal ignore-case, a later panel wins;
  7. ApplyAssignmentsToMatrix: every non-null cell with a non-empty Id found in the map gets
     InverterId and StringInputNumber (Code is not consulted here), and nothing else changes.

Not ported, because nothing in the solve reads it: telemetry, the rejection-reason texts beyond their
use as diagnostics, and the batch save (cs:104), which persists the rows this module returns. The
group's row angle and panel size are carried onto the inputs as the plugin carries them (cs:4059-4061,
cs:4104-4105, cs:4127) and PvcaseSolver never reads them. The builder's matrixJson, Rows, panelDef and
panelGroupJson null checks (cs:4054-4057) have no intake form: the plugin adapter refuses such a group.

Intake (the G33 pvcase intake as the plugin adapter writes it; exactly these keys, anything else
refuses):
  units              a drawing length unit ("in" for the rooftop fixture)
  panels_per_string  int: the plugin's StringLength setting at solve time (cs:94)
  panel_groups       [{handle, installation, panel_size {height_across_row, width_along_row},
                     row_angle_rad, sequences, rows}] in the plugin's read order. sequences is the
                     matrix's Sequences (a list of int); rows is the matrix rows, each a list of null
                     (a null or Code 0 cell, whose place keeps the column index) or a panel
                     {code, id, inverter_id, seq, string_input_number, x, y}. A null matrix row is [].

Pure and deterministic: no I/O, no clock, no randomness; fails closed on a malformed intake with a
named PvcaseInputError. Linear in the number of cells plus one sort per group.
"""
from __future__ import annotations

import copy
import math
import re
import sys

DEFAULT_PANELS_PER_STRING = 12  # PvcaseSolver.cs:55
INT32_MIN, INT32_MAX = -2 ** 31, 2 ** 31 - 1
MAX_GROUPS = 10_000
MAX_CELLS = 500_000       # every cell of every row, null cells included
MAX_SEQUENCE_TOTAL = 500_000
LENGTH_UNITS = ("mm", "cm", "m", "in", "ft")
INSTALLATIONS = ("Roof", "Ground")   # InstallationDesign.cs:18-19
INTAKE_KEYS = {"units", "panels_per_string", "panel_groups"}
GROUP_KEYS = {"handle", "installation", "panel_size", "row_angle_rad", "sequences", "rows"}
PANEL_SIZE_KEYS = {"height_across_row", "width_along_row"}
PANEL_INT_FIELDS = ("code", "seq", "inverter_id", "string_input_number")
PANEL_FLOAT_FIELDS = ("x", "y")
PANEL_KEYS = set(PANEL_INT_FIELDS) | set(PANEL_FLOAT_FIELDS) | {"id"}
_HANDLE = re.compile(r"[0-9A-Fa-f]{1,16}")

SOLVED = "solved"
NO_PANEL_GROUPS = "no-panel-groups"
NO_USABLE_PANELS = "no-usable-panels"


class PvcaseInputError(ValueError):
    """A malformed intake: a named refusal, nothing is solved."""


# ------------------------------------------------------------------ intake --

def _int32(value, what):
    if isinstance(value, bool) or not isinstance(value, int) or not INT32_MIN <= value <= INT32_MAX:
        raise PvcaseInputError(f"{what} must be a 32-bit integer")
    return value


def _finite(value, what):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PvcaseInputError(f"{what} must be a finite number")
    return float(value)


def _list(value, what):
    if not isinstance(value, list):
        raise PvcaseInputError(f"{what} must be a list")
    return value


def _handle(value, what):
    if not isinstance(value, str) or not _HANDLE.fullmatch(value):
        raise PvcaseInputError(f"{what} must be a drawing handle (hex)")
    return value


def handle_order(handle):
    """Ascending drawing-handle value, the G33 row order."""
    return int(handle, 16)


def _panel(cell, where, seen):
    if cell is None:
        return None
    if not isinstance(cell, dict) or set(cell) != PANEL_KEYS:
        raise PvcaseInputError(f"{where} must be null or an object of exactly {sorted(PANEL_KEYS)}")
    out = {"id": _handle(cell["id"], f"{where}.id")}
    key = handle_order(out["id"])
    if key in seen:
        raise PvcaseInputError(f"panel {out['id']} sits in two cells")
    seen.add(key)
    for field in PANEL_INT_FIELDS:
        out[field] = _int32(cell[field], f"{where}.{field}")
    for field in PANEL_FLOAT_FIELDS:
        out[field] = _finite(cell[field], f"{where}.{field}")
    return out


def _group(group, where, budget, seen_panels):
    if not isinstance(group, dict) or set(group) != GROUP_KEYS:
        raise PvcaseInputError(f"{where} must be an object of exactly {sorted(GROUP_KEYS)}")
    if group["installation"] not in INSTALLATIONS:
        raise PvcaseInputError(f"{where}.installation must be one of {list(INSTALLATIONS)}")
    size = group["panel_size"]
    if not isinstance(size, dict) or set(size) != PANEL_SIZE_KEYS:
        raise PvcaseInputError(f"{where}.panel_size must be an object of exactly {sorted(PANEL_SIZE_KEYS)}")
    panel_size = {}
    for key in sorted(PANEL_SIZE_KEYS):
        panel_size[key] = _finite(size[key], f"{where}.panel_size.{key}")
        if panel_size[key] <= 0:
            raise PvcaseInputError(f"{where}.panel_size.{key} must be positive")
    sequences = [_int32(s, f"{where}.sequences[{i}]")
                 for i, s in enumerate(_list(group["sequences"], f"{where}.sequences"))]
    if sum(max(0, s) for s in sequences) > MAX_SEQUENCE_TOTAL:
        raise PvcaseInputError(f"{where}.sequences cover more than {MAX_SEQUENCE_TOTAL} positions")
    rows = []
    for r, row in enumerate(_list(group["rows"], f"{where}.rows")):
        budget[0] -= 1
        if budget[0] < 0:
            raise PvcaseInputError(f"the rows hold more than {MAX_CELLS} cells")
        cells = []
        for c, cell in enumerate(_list(row, f"{where}.rows[{r}]")):
            budget[0] -= 1
            if budget[0] < 0:
                raise PvcaseInputError(f"the rows hold more than {MAX_CELLS} cells")
            cells.append(_panel(cell, f"{where}.rows[{r}][{c}]", seen_panels))
        rows.append(cells)
    return {"handle": _handle(group["handle"], f"{where}.handle"), "installation": group["installation"],
            "panel_size": panel_size, "row_angle_rad": _finite(group["row_angle_rad"], f"{where}.row_angle_rad"),
            "sequences": sequences, "rows": rows}


def validate_intake(intake):
    """The G33 intake, fail closed. Returns a normalized deep copy (numbers as the plugin types them);
    the caller's object is never mutated. No l2_inverters key: the capture registers no L2."""
    if not isinstance(intake, dict) or set(intake) != INTAKE_KEYS:
        raise PvcaseInputError(f"intake keys must be exactly {sorted(INTAKE_KEYS)}")
    if intake["units"] not in LENGTH_UNITS:
        raise PvcaseInputError(f"intake units must be one of {list(LENGTH_UNITS)}")
    per_string = _int32(intake["panels_per_string"], "panels_per_string")
    groups, seen, seen_panels, budget = [], set(), set(), [MAX_CELLS]
    for g, group in enumerate(_list(intake["panel_groups"], "panel_groups")):
        if g >= MAX_GROUPS:
            raise PvcaseInputError(f"panel_groups holds more than {MAX_GROUPS} groups")
        out = _group(group, f"panel_groups[{g}]", budget, seen_panels)
        if handle_order(out["handle"]) in seen:
            raise PvcaseInputError(f"panel group {out['handle']} is listed twice")
        seen.add(handle_order(out["handle"]))
        groups.append(out)
    return {"units": intake["units"], "panels_per_string": per_string, "panel_groups": groups}


# --------------------------------------------------------------- the solver --

class PanelInput:
    """PanelInputXY (the width and height are carried, PvcaseSolver never reads them)."""
    __slots__ = ("handle", "x", "y", "width_along_row", "height_across_row", "l2_number", "string_number",
                 "row_index", "col_index")

    def __init__(self, handle, x, y, width_along_row, height_across_row, l2_number, string_number,
                 row_index, col_index):
        self.handle = handle
        self.x = x
        self.y = y
        self.width_along_row = width_along_row
        self.height_across_row = height_across_row
        self.l2_number = l2_number
        self.string_number = string_number
        self.row_index = row_index
        self.col_index = col_index


def build_panel_group_inputs(groups):
    """CombinerAutoCmd.BuildPanelGroupInputsWithDiagnostics (cs:4041-4132): ([{group_id, row_angle_rad,
    panels}], rejection reasons). `groups` are validated intake groups."""
    reasons, result = [], []
    for g, pg in enumerate(groups):
        ident = pg["handle"] or ("idx" + str(g))                                   # cs:4052
        # cs:4059-4061: the panel definition's dimensions for the group's installation, which the
        # adapter resolved into panel_size.
        width_along_row = pg["panel_size"]["width_along_row"]
        height_across_row = pg["panel_size"]["height_across_row"]
        panels, raw_examined, filtered_code0 = [], 0, 0
        # Seq -> string number from Sequences (cs:4071-4084); Solve overwrites the result, the
        # plugin still builds it.
        seq_to_string = {}
        if pg["sequences"]:
            cursor = 1
            for s, length in enumerate(pg["sequences"]):
                for _ in range(length):
                    seq_to_string[cursor] = s + 1
                    cursor += 1
        for r, row in enumerate(pg["rows"]):                                       # cs:4086
            for c, pj in enumerate(row):                                           # cs:4090
                if pj is None:
                    continue
                raw_examined += 1
                if pj["code"] == 0:                                                # cs:4095
                    filtered_code0 += 1
                    continue
                l2_num = pj["inverter_id"] if pj["inverter_id"] > 0 else 0         # cs:4099
                str_num = pj["string_input_number"] if pj["string_input_number"] > 0 else 0
                if str_num == 0 and pj["seq"] > 0 and pj["seq"] in seq_to_string:  # cs:4101
                    str_num = seq_to_string[pj["seq"]]
                panels.append(PanelInput(pj["id"], pj["x"], pj["y"], width_along_row, height_across_row,
                                         l2_num, str_num, r, c))
        if not panels:
            reasons.append("[" + ident + "] zero panels survived (raw=" + str(raw_examined) +
                           ", code0=" + str(filtered_code0) + ")")                  # cs:4119
            continue
        result.append({"group_id": pg["handle"] or ("pg" + str(g)),               # cs:4126
                       "row_angle_rad": pg["row_angle_rad"], "panels": panels})
    return result, reasons


def _nearest_l2(x, y, l2_list):
    """PvcaseSolver.NearestL2 (cs:158-174): the first L2 strictly nearer than every earlier one."""
    best_dist_sq = sys.float_info.max
    best_number = l2_list[0]["number"]
    for l2 in l2_list:
        dx = x - l2["x"]
        dy = y - l2["y"]
        d = dx * dx + dy * dy
        if d < best_dist_sq:
            best_dist_sq = d
            best_number = l2["number"]
    return best_number


def solve(panel_groups, l2_inverters, panels_per_string=DEFAULT_PANELS_PER_STRING):
    """PvcaseSolver.Solve (cs:72-156). `l2_inverters` is the snapshot [{number, x, y}] (empty for the
    G33 intake). Mutates the panels' l2_number and string_number in place and returns
    {panels_assigned, strings_created, strings_per_l2}."""
    strings_per_l2 = {}
    result = {"panels_assigned": 0, "strings_created": 0, "strings_per_l2": strings_per_l2}
    if panel_groups is None:
        return result
    if panels_per_string < 1:
        panels_per_string = 1                                                      # cs:80
    l2_list = [l2 for l2 in (l2_inverters or ()) if l2["number"] > 0]             # cs:83-88
    have_l2_positions = len(l2_list) > 0
    next_string_by_l2 = {}                                                         # counted across groups
    for group in panel_groups:
        if group is None or group["panels"] is None:
            continue
        # OrderBy(RowIndex).ThenBy(ColIndex) is a stable sort (cs:103-107).
        ordered = sorted((p for p in group["panels"] if p is not None), key=lambda p: (p.row_index, p.col_index))
        for start in range(0, len(ordered), panels_per_string):                   # cs:109
            end = min(start + panels_per_string, len(ordered))
            sum_x = sum_y = 0.0
            for i in range(start, end):                                           # cs:116-120, in order
                sum_x += ordered[i].x
                sum_y += ordered[i].y
            n = end - start
            l2_number = _nearest_l2(sum_x / n, sum_y / n, l2_list) if have_l2_positions else 1   # cs:124-126
            string_number = next_string_by_l2.get(l2_number, 0) + 1              # cs:135-139
            next_string_by_l2[l2_number] = string_number
            for i in range(start, end):
                ordered[i].l2_number = l2_number
                ordered[i].string_number = string_number
                result["panels_assigned"] += 1
            result["strings_created"] += 1
            strings_per_l2[l2_number] = strings_per_l2.get(l2_number, 0) + 1
    return result


def _ignore_case_key(text):
    """StringComparer.OrdinalIgnoreCase: each character's simple upper case, one for one."""
    return "".join(ch.upper() if len(ch.upper()) == 1 else ch for ch in text)


def build_handle_assignment_map(group_inputs):
    """LeafPvcaseSolveCommand.BuildHandleAssignmentMap (cs:129-143): handle -> (L2, string),
    compared ignoring case; a later panel with the same handle wins."""
    by_handle = {}
    for grp in group_inputs:
        if grp is None or grp["panels"] is None:
            continue
        for p in grp["panels"]:
            if p is None or not p.handle:
                continue
            by_handle[_ignore_case_key(p.handle)] = (p.l2_number, p.string_number)
    return by_handle


def apply_assignments_to_matrix(groups, by_handle):
    """LeafPvcaseSolveCommand.ApplyAssignmentsToMatrix (cs:146-170), in place on the groups' rows:
    inverter_id and string_input_number of every cell whose id is in the map. Returns the count
    written."""
    written = 0
    for pg in groups:
        for row in pg["rows"]:
            for pj in row:
                if pj is None or not pj["id"]:
                    continue
                asg = by_handle.get(_ignore_case_key(pj["id"]))
                if asg is not None:
                    pj["inverter_id"], pj["string_input_number"] = asg
                    written += 1
    return written


def effective_panels_per_string(setting):
    """cs:94-95: the StringLength setting, or DefaultPanelsPerString when it is below 1."""
    return setting if setting >= 1 else DEFAULT_PANELS_PER_STRING


def pvcase_solve(intake):
    """LEAFPVCASESOLVE over an intake (cs:41-126). Returns {status, panel_groups (the groups after
    the command, the intake's order), panels_assigned, strings_created, l2_count, written,
    rejection_reasons, message}. The intake is not mutated; a refusal leaves the rows as read."""
    state = validate_intake(intake)
    groups = copy.deepcopy(state["panel_groups"])
    l2_snapshots = []                                                              # cs:80-91, zero L2
    out = {"status": None, "panel_groups": groups, "panels_assigned": 0, "strings_created": 0,
           "l2_count": len(l2_snapshots), "written": 0, "rejection_reasons": [], "message": None}
    if not groups:                                                                 # cs:57-64
        out["status"] = NO_PANEL_GROUPS
        out["message"] = ("LEAFPVCASESOLVE: no panel groups found in the drawing. "
                          "Create and Solve panel groups first.")
        return out
    inputs, reasons = build_panel_group_inputs(groups)                             # cs:66-68
    out["rejection_reasons"] = reasons
    if not inputs:                                                                 # cs:69-75
        out["status"] = NO_USABLE_PANELS
        out["message"] = "LEAFPVCASESOLVE: no usable panels in the panel groups."
        return out
    per_string = effective_panels_per_string(state["panels_per_string"])          # cs:94-95
    result = solve(inputs, l2_snapshots, per_string)                               # cs:97
    by_handle = build_handle_assignment_map(inputs)                                # cs:100
    written = apply_assignments_to_matrix(groups, by_handle)                       # cs:101
    out.update(status=SOLVED, panels_assigned=result["panels_assigned"], strings_created=result["strings_created"],
               written=written)
    out["message"] = (f"LEAFPVCASESOLVE: solved {result['panels_assigned']} panel(s) into "
                      f"{result['strings_created']} string(s) across {out['l2_count']} L2 inverter(s); "
                      f"{written} panel assignment(s) written back to the drawing.")   # cs:106-109
    return out


def panel_assignments(group):
    """G33: a group's [panel id, inverter id, string input number] for every panel (Code != 0) in row
    and cell order."""
    return [[pj["id"], pj["inverter_id"], pj["string_input_number"]]
            for row in group["rows"] for pj in row if pj is not None and pj["code"] != 0]
