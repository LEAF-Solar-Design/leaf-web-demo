"""Add ONE string over named panels, in the named order: the Studio counterpart of SingleString.

Plugin semantics, read from the licensed source (Branch2025 Commands.cs:4309 and
BranchCmd.cs:17074 AddSingleString): SINGLESTRING is a synchronous modal command
that asks for panels, drops the ones GetPanelToStringMap already reports as wired,
sets the installation design first, takes the panel definition from the FIRST
selected panel, and commits one polyline, its two endpoint blocks and its circuit
MText inside one transaction.

* the SELECTION ORDER is the command's output, not a set: the polyline runs through
  the panels in the order they were picked, so nothing here is ever sorted;
* it is LOCAL. No cloud call, no solver and no inverter, so the new circuit carries
  inverter_ref null, exactly like the circuits around it on a drawing whose
  inverterNumber is null;
* it is ADDITIVE and scoped to the selection. Every existing circuit, every group
  and every electrical zone keeps every field it had; the only panels that move are
  the ones named, and they move from unassigned to this one circuit;
* the plugin FILTERS an already-wired panel out of its own selection, so a caller
  that reaches this module with one has made an error the drawing cannot express:
  a panel belongs to at most one circuit, and refusing beats silently dropping it.

Captured on licensed AutoCAD 2025 on 2026-09-22 (receipts/w2-string-single-add-20260922),
driven by handle on a copy of the post-delete drawing: SEVEN of the fourteen panels
freed by the string delete, selected in order, took the drawing from 172 to 173
strings with length distribution 7x1, 12x6, 13x65, 14x101 and left the other seven
panels unassigned. A seven-panel circuit is shorter than anything the solver cuts on
that drawing (12 to 14), which is what makes the receipt prove the ADD rather than
re-prove the solve.

Fails closed: every check runs before any mutation, the add runs on the private copy
checked_graph returns, and the committed sizing the drawing already carries bounds
the new circuit rather than an invented constant. Bounded: one pass per structure,
one dict of panels built once, no rescan per named panel.
"""
import copy
from datetime import datetime, timezone
import math

from solar_design_graph import GraphValidationError, _bounded_json, new_id
from solar_sizing_client import checked_graph
from solar_solve_results import finish_mutation, invalidate_dependents, sync_assignments

TOOL = "solar-string-add"
# A request bound only: the circuit's real cap is the length the drawing was sized
# with, read from the graph by max_string_length below.
MAX_MEMBERS = 4096
# Schema bound for settings.string_number (contract/solar-design-graph.v1.schema.json).
MAX_STRING_NUMBER = 1000000
# Metres per foot: graph geometry is in compute metres, length_ft is feet, the same
# conversion the solve commit records for a solved circuit.
METRES_PER_FOOT = 0.3048


def max_string_length(graph):
    """The longest circuit the graph's own committed sizing allows.

    Global sizing writes the plugin's recommendation to the settings; zone sizing
    writes it per zone and leaves the settings at zero. The bound here is the
    LOOSEST committed length, because one selection may cross zones and a per-zone
    rule belongs to the sizing capability that owns it. A graph that never committed
    a length cannot bound a circuit at all, so it refuses instead of guessing.
    """
    lengths = [graph["settings"]["panels_in_sequence"]]
    lengths.extend(zone["panels_in_sequence"] for zone in graph["electrical_zones"])
    longest = max((length for length in lengths if type(length) is int), default=0)
    if longest < 1:
        raise GraphValidationError("STRING_LENGTH_NOT_SIZED")
    return longest


def slot_tables(graph):
    """{frame id: SlotTable} for every compact Ground frame (codec leaf.solar-ground-slots.v1), frames
    in graph order. A graph with no ground_slots block returns {} without importing the codec.
    Decoding fails closed with the codec's own refusal; no slot is expanded."""
    if not any("ground_slots" in frame for frame in graph["frames"]):
        return {}
    import solar_ground_graph_codec as codec
    return codec.decode_graph_slots(graph)


def panel_views(graph, tables=None):
    """Every panel in expansion order: the stored panels themselves, then, frames in graph order
    and slots in slot order, one fresh read-only view {"id", "frame_ref", "centre", "angle"} per
    compact slot panel carrying the values codec.expand_graph gives that panel. Nothing is stored
    for a slot panel; linear in the panel count."""
    views = list(graph["panels"])
    if tables is None:
        tables = slot_tables(graph)
    for frame in graph["frames"]:
        table = tables.get(frame["id"])
        if table is None:
            continue
        xy = table.centres
        views.extend({"id": panel_id, "frame_ref": frame["id"], "centre": [xy[2 * col], xy[2 * col + 1]],
                      "angle": table.angle} for col, panel_id in enumerate(table.ids))
    return views


def slot_view(frame_id, table, col):
    """The read-only view panel_views gives slot `col` of a compact frame: a fresh dict."""
    xy = table.centres
    return {"id": table.ids[col], "frame_ref": frame_id, "centre": [xy[2 * col], xy[2 * col + 1]],
            "angle": table.angle}


def find_panels(graph, refs, tables):
    """{ref: panel} for exactly the named refs that exist, equal to the same lookup in
    {p["id"]: p for p in panel_views(graph, tables)} without building a view per slot: a stored
    panel is the graph's own dict, a slot panel a fresh slot_view, and a ref that is neither is
    absent. One pass over the stored panels and one allocation-free scan of the slot ids; a view
    is built only for a named slot, so the work beyond the scan is linear in the refs."""
    wanted = set(refs)
    found = {panel["id"]: panel for panel in graph["panels"] if panel["id"] in wanted}
    for frame in graph["frames"]:
        table = tables.get(frame["id"])
        if table is None:
            continue
        for col, panel_id in enumerate(table.ids):
            if panel_id in wanted:
                found[panel_id] = slot_view(frame["id"], table, col)
    return found


def input_readiness(graph):
    """Whether a string can be added here: the graph holds panel rows, or a converted Ground frame
    holds a compact slot block (the schema requires at least one slot). Reads frame keys only: no
    slot is decoded. Never raises on a valid graph."""
    if graph["panels"] or any("ground_slots" in frame for frame in graph["frames"]):
        return {"input_ready": True, "input_reason": None}
    return {"input_ready": False, "input_reason": "panels_required"}


def _valid_request(params):
    """True for exactly {expected_rev, ordered_panel_refs}, no coercion anywhere."""
    if type(params) is not dict or set(params) != {"expected_rev", "ordered_panel_refs"}:
        return False
    refs = params["ordered_panel_refs"]
    return (type(params["expected_rev"]) is int and type(refs) is list
            and 1 <= len(refs) <= MAX_MEMBERS
            and all(type(ref) is str and ref for ref in refs))


def _new_string(graph, refs, tables):
    """The circuit SINGLESTRING commits: one polyline through the panels, in order.

    Called on the private copy only, after every refusal has already run; `tables` are the slot
    tables of the graph it was copied from (the copy carries the same blocks).
    """
    panels = find_panels(graph, refs, tables)
    tags = {string["circuit_tag"] for string in graph["strings"]}
    number = graph["settings"]["string_number"]
    # Bounded by the number of circuits the drawing already holds: the loop can only
    # step over a tag that exists.
    while f"S{number}" in tags:
        number += 1
    if number >= MAX_STRING_NUMBER:
        raise GraphValidationError("STRING_NUMBER_EXHAUSTED")
    graph["settings"]["string_number"] = number + 1
    points = [copy.deepcopy(panels[ref]["centre"]) for ref in refs]
    length_m = sum(math.dist(a + [0] * (3 - len(a)), b + [0] * (3 - len(b)))
                   for a, b in zip(points, points[1:]))
    return {
        "id": new_id("string"), "kind": "string", "rev": graph["rev"],
        "validity": {"state": "valid", "reasons": []},
        "provenance": {"created_by": TOOL, "created_at": datetime.now(timezone.utc).isoformat(),
                       "last_writer": TOOL, "source_rev": graph["rev"],
                       "source_hash": graph["source_hash"],
                       "catalog_versions": copy.deepcopy(graph["catalog_versions"])},
        "extra": {
            # The polyline runs from the first selected panel to the last, so the
            # first module is the negative terminal and the last the positive one.
            # Derived from the selection, never from a solver response: this command
            # never calls one.
            "polarity": {"source": "derived",
                         "rule": "ordered-selection-first-negative-last-positive",
                         "negative_panel_ref": refs[0], "positive_panel_ref": refs[-1],
                         "source_rev": graph["rev"]},
            "length_provenance": {"source": "derived", "rule": "panel-centre-path",
                                  "point_units": "m", "length_units": "ft",
                                  "includes_home_runs": False},
        },
        "circuit_tag": f"S{number}", "circuit_kind": "String",
        "ordered_panel_refs": list(refs), "module_count": len(refs),
        "from_ref": refs[0], "to_ref": refs[-1], "tag_text_ref": None, "wire_gauge": "",
        "length_ft": length_m / METRES_PER_FOOT, "route": points,
        # SINGLESTRING is local: no inverter is chosen, so the circuit is committed
        # unassigned exactly as the drawing's own strings are.
        "inverter_ref": None,
    }


def add_string(graph, params):
    """Create exactly one circuit over the named panels, in the named order."""
    _bounded_json(params)
    if not _valid_request(params):
        raise GraphValidationError("INVALID_STRING_ADD_REQUEST")
    refs = params["ordered_panel_refs"]
    if len(set(refs)) != len(refs):
        # One panel cannot be two modules of one circuit, and an AutoCAD selection
        # set cannot hold the same entity twice either.
        raise GraphValidationError("DUPLICATE_PANEL_MEMBERSHIP")
    before = checked_graph(graph, params["expected_rev"])
    if len(refs) > max_string_length(before):
        # The drawing's own sized length, not a constant: a longer circuit is one the
        # cold-Voc guard never cleared.
        raise GraphValidationError("STRING_TOO_LONG")
    tables = slot_tables(before)
    if len(find_panels(before, refs, tables)) != len(refs):
        raise GraphValidationError("MISSING_PANEL")
    wired = {ref for string in before["strings"] for ref in string["ordered_panel_refs"]}
    if not wired.isdisjoint(refs):
        # The plugin filters an already-wired panel out of its own selection, so
        # reaching one here is a caller error, never a drawing state.
        raise GraphValidationError("PANEL_ALREADY_ASSIGNED")
    result = copy.deepcopy(before)
    string = _new_string(result, refs, tables)
    result["strings"].append(string)
    # Panel assignments, frame sequences, frame panel_assignments and matrix cells are
    # redundant views of string membership: one pass rebuilds all of them, so the newly
    # wired panels carry this circuit and its sequence everywhere at once.
    sync_assignments(result)
    # The new circuit is the solved-for entity here, so it stays valid; anything else
    # that derives from these panels (a route, a schedule) goes stale.
    invalidate_dependents(before, result, [string["id"], *refs], solved_ids={string["id"]})
    # finish_mutation recomputes extra.solve_coverage, so the wired panels stop being
    # reported as unassigned instead of carrying the stale coverage the delete left.
    after = finish_mutation(before, result, TOOL)
    return {"graph": after, "string_ref": string["id"], "circuit_tag": string["circuit_tag"],
            "ordered_panel_refs": list(refs), "total": len(after["strings"])}


OPERATIONS = {"add-string": add_string}


def run(intake, params):
    _bounded_json(params)
    # The operation is named as a string or the request is refused: an unhashable
    # value must never reach the lookup.
    if (type(params) is not dict or type(params.get("operation")) is not str
            or params["operation"] not in OPERATIONS):
        raise GraphValidationError("INVALID_STRING_ADD_REQUEST")
    request = {key: value for key, value in params.items() if key != "operation"}
    return OPERATIONS[params["operation"]](intake, request)["graph"]
