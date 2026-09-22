"""Delete named strings from a solved drawing: the Studio counterpart of DeleteString.

Plugin semantics, read from the licensed source (Branch2025 Commands.cs:2125 and
BranchCmd.cs:11053): DeleteString is a synchronous modal command that asks for
string polylines with `GetStrings(false, null)` and then, inside ONE transaction,
erases four entities per selected cable, the string polyline, its from-block, its
to-block and its circuit MText. A cable whose polyline, from, to or text handle is
null or already erased is skipped with a message and the rest still commit.

* the command is SCOPED to the selection: exactly the named cables go, and every
  cable the selection did not name is left byte-identical;
* it writes NO membership bookkeeping. The panels the deleted cable ran through
  are never touched, so they stay in the drawing, in their group and in their
  electrical zone, and simply stop being wired;
* selecting every cable is allowed, so deleting all of them is allowed here too:
  the result is a drawing with no strings and all of its panels.

The committed state is read back by the same read_combiner_string_assignments op
the solve receipt uses, so the graph must say the same thing the drawing does: the
deleted circuits stop appearing, every surviving circuit is unchanged, the panel
set is unchanged, and the solve coverage the strings evidence requires
(scripts/solar_studio_evidence.py:232) is recomputed so the freed panels are
reported as unassigned rather than silently still wired.

Fails closed: every check runs before any mutation, the delete runs on a private
copy, and a surviving reference to a deleted string anywhere in the graph refuses
the whole delete rather than leaving it half applied. Bounded: one pass per
structure, no rescan per string, and the residual scan walks the graph once with
an explicit node cap.
"""
import copy

from solar_design_graph import MAX_NODES, GraphValidationError, _bounded_json
from solar_sizing_client import checked_graph
from solar_solve_results import finish_mutation, invalidate_dependents, sync_assignments

TOOL = "solar-string-delete"
MAX_DELETED = 4096


def _references(value, removed):
    """True when any string anywhere under value names a deleted circuit.

    One bounded iterative pass, keys included: a deleted id used as a mapping key
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
    """True for exactly {expected_rev, string_refs}, no coercion anywhere."""
    if type(params) is not dict or set(params) != {"expected_rev", "string_refs"}:
        return False
    refs = params["string_refs"]
    return (type(params["expected_rev"]) is int and type(refs) is list
            and 1 <= len(refs) <= MAX_DELETED
            and all(type(ref) is str and ref for ref in refs)
            and len(set(refs)) == len(refs))


def delete_strings(graph, params):
    """Drop exactly the named strings; every panel and every other string survives."""
    _bounded_json(params)
    if not _valid_request(params):
        raise GraphValidationError("INVALID_STRING_DELETE_REQUEST")
    before = checked_graph(graph, params["expected_rev"])
    removed = set(params["string_refs"])
    if not removed <= {string["id"] for string in before["strings"]}:
        # A circuit the drawing does not hold: the command deletes a SELECTION of
        # the cables that are there, never an id the caller invented.
        raise GraphValidationError("MISSING_STRING")
    # The panels the deleted cables ran through, named before anything moves.
    freed = sorted({ref for string in before["strings"] if string["id"] in removed
                    for ref in string["ordered_panel_refs"]})
    result = copy.deepcopy(before)
    result["strings"] = [string for string in result["strings"] if string["id"] not in removed]
    for inverter in result["inverters"]:
        # An input assignment naming an erased cable is a dangling reference, and
        # the solve commit drops one the same way when it replaces a frame's strings.
        inverter["input_assignments"] = [assignment for assignment in inverter["input_assignments"]
                                         if assignment["string_ref"] not in removed]
    # Panel assignments, frame sequences, frame panel_assignments and matrix cells are
    # redundant views of string membership: one pass rebuilds all of them from the
    # strings that remain, so a freed panel is unwired everywhere at once.
    sync_assignments(result)
    if _references(result, removed):
        # Half a delete is worse than none: something still names an erased cable.
        raise GraphValidationError("STRING_REFERENCE_NOT_CLEARED")
    invalidate_dependents(before, result, sorted(removed))
    # finish_mutation recomputes extra.solve_coverage, so the freed panels are
    # reported as unassigned instead of the stale coverage the solve committed.
    after = finish_mutation(before, result, TOOL)
    return {"graph": after, "deleted": sorted(removed), "remaining": len(after["strings"]),
            "panels_freed": freed}


OPERATIONS = {"delete-strings": delete_strings}


def run(intake, params):
    _bounded_json(params)
    # The operation is named as a string or the request is refused: an unhashable
    # value must never reach the lookup.
    if (type(params) is not dict or type(params.get("operation")) is not str
            or params["operation"] not in OPERATIONS):
        raise GraphValidationError("INVALID_STRING_DELETE_REQUEST")
    request = {key: value for key, value in params.items() if key != "operation"}
    return OPERATIONS[params["operation"]](intake, request)["graph"]
