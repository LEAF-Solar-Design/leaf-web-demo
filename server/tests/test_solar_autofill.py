"""Offline checks for the auto-fill port and its revert: the DP, the chain, the snake.

The first half touches no graph, no network and no builtin: server/solar_autofill.py
is a pure port of the plugin's OptimalPlanSolver and SnakePanelSelector, so it is
tested as one. The captured case is COMPUTED from the committed rooftop intake
(the same kernel, the same four grouping parameters, the same 28 removals the
licensed run made), never asserted from a stored answer: the only thing taken from
the capture is its INPUT (which panels the removal took) and its OUTCOME (one panel,
8201, from the 71-panel group to the 137-panel group).

The second half covers server/builtins/solar_autofill.py's revert_corrections, which
is where Studio deliberately DIVERGES from the plugin: AutoFillRevert keys its
snapshot by block handle and AutoFill rebuilds every group it changes under a new
one, so the plugin's revert skips exactly the groups that moved and leaves them at
70 and 138. Studio reverts by the PLAN, so the round trip puts 8201 back in the
71-panel group and every other group back byte for byte. Those cases build a real
graph, seeded here, offline.
"""
import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest

import solar_panel_group_kernel as kernel
from solar_autofill import (
    AutofillError, autofill, estimate_panel_dimensions, find_nearest_feasible,
    is_feasible_count, realize_trades, select_snake_panels, _read_groups,
)
from solar_design_graph import GraphValidationError, new_id, validate_graph
from solar_graph_seed import new_empty_graph

SERVER = Path(__file__).resolve().parents[1]
ROOT = Path(__file__).resolve().parents[2]
INTAKE = ROOT / "data" / "rooftop_unsplit.intake.json"
# The four grouping parameters the captured drawing carries (its own settings read,
# receipts/w2-autofill-20260923/bthost-autofill-after-reopen.jsonl).
LAYER_CONTAINS = "Panels"
BRANCH_MAX_OFFSET = 120.0
ALIGNMENT_TOLERANCE = 12.0
# mSettings.NumPanelsInSequence at the captured AUTOFILL: 71 is infeasible at 14
# (the feasible counts are k strings of 12 to 14), which is why the run had work.
STRING_LENGTH = 14
# receipts/w2-autofill-20260923/removed-28-from-9CC7.json, in the capture's own order:
# the panels REMOVEPANEL took out of the 99-panel group, leaving it at 71.
REMOVED_28 = ["81E0", "81DF", "81DE", "81DD", "81DC", "81DB", "81DA", "81D9", "81D8",
              "81D7", "81D6", "81D5", "81D4", "81D3", "81D2", "81D1", "81D0", "81CF",
              "81CE", "81CD", "81CC", "81CB", "81CA", "81C9", "81C8", "81C7", "81C6",
              "81C5"]
# The two groups the licensed AUTOFILL touched, by the neutral id their members give
# them after the removal: the 71-panel donor and the 137-panel receiver.
DONOR = "group:81E1"
RECEIVER = "group:8228"
MOVED_PANEL = "8201"
# "Correction: OptimalSolver: Group 6 -> Group 8 (1 panels, d=1027, direct)".
REPORTED_DISTANCE = 1027

PITCH_X, PITCH_Y = 79.0, 40.0
COLUMNS = 10


def panels(prefix, count, origin):
    ox, oy = origin
    return [{"id": f"{prefix}-{i}", "x": ox + (i % COLUMNS) * PITCH_X,
             "y": oy + (i // COLUMNS) * PITCH_Y, "angle": 0.0} for i in range(count)]


def group(group_id, count, origin, zone="|"):
    return {"id": group_id, "zone": zone, "panels": panels(group_id, count, origin)}


# Three groups 600 in apart along Y: close enough that single-linkage bbox clustering
# puts all three in ONE cluster, so the DP solves them together.
def three_groups(order):
    built = {"a": group("a", 137, (0.0, 0.0)), "b": group("b", 134, (0.0, 600.0)),
             "c": group("c", 71, (0.0, 1200.0))}
    return [built[name] for name in order]


def line_groups(count, size, spacing):
    return [group(f"g{i}", size, (i * spacing, 0.0)) for i in range(count)]


def captured_groups():
    """The committed rooftop intake, grouped, cut by the captured 28, in scan order.

    The group the removal rebuilt is scanned LAST: REMOVEPANEL replaces the group's
    block reference, and AutoCAD hands a select-all back in database order, so the
    new block sorts after every original one (receipts/w2-autofill-20260923:
    the 71-panel group's block was 9D2F, above every original 9C93..9CFB).
    Members stay in the kernel's own order, which is ascending handle.
    """
    intake = json.loads(INTAKE.read_text(encoding="utf-8"))
    selected = kernel.panels_from_intake(intake, layer_contains=LAYER_CONTAINS,
                                         installation_design="Roof")
    geometry = {panel["handle"].upper(): panel for panel in selected}
    grouped = kernel.group_panels(selected, branch_max_offset=BRANCH_MAX_OFFSET,
                                  alignment_tolerance=ALIGNMENT_TOLERANCE,
                                  installation_design="Roof")
    removed = {handle.upper() for handle in REMOVED_28}
    untouched, cut = [], None
    for source in grouped:
        members = [handle.upper() for handle in source["members"] if handle.upper() not in removed]
        entry = {"id": "group:" + min(members, key=lambda h: int(h, 16)), "zone": "|",
                 "panels": [{"id": handle, "x": geometry[handle]["centre"][0],
                             "y": geometry[handle]["centre"][1],
                             "angle": geometry[handle]["angle"]} for handle in members]}
        if len(members) == len(source["members"]):
            untouched.append(entry)
        else:
            cut = entry
    assert cut is not None, "the captured removal must cut exactly one group"
    return untouched + [cut]


@pytest.fixture(scope="module")
def captured():
    return captured_groups(), autofill(captured_groups(), STRING_LENGTH)


@pytest.mark.parametrize("n,total,expected", [
    (14, 0, False), (14, -1, False), (0, 10, False), (14, 11, False), (14, 12, True),
    (14, 14, True), (14, 15, False), (14, 23, False), (14, 24, True), (14, 70, True),
    (14, 71, False), (14, 72, True),
])
def test_a_count_is_feasible_when_it_splits_into_strings(n, total, expected):
    """IsFeasibleCount: some k >= 1 with k*(n-2) <= total <= k*n, and 71 at n=14 is the hole."""
    assert is_feasible_count(n, total) is expected


@pytest.mark.parametrize("n,count,expected", [
    (14, 70, 70), (14, 71, 70), (14, 11, 12), (14, 15, 14), (14, 23, 24),
])
def test_the_nearest_feasible_count_prefers_the_lower_one(n, count, expected):
    """FindNearestFeasible searches outward and takes the lower value on a tie."""
    assert find_nearest_feasible(n, count) == expected


def test_the_last_group_in_scan_order_donates_and_the_first_receives():
    """The DP keeps the FIRST target reaching a cost and backtracks from the LAST group."""
    plan = autofill(three_groups(("a", "b", "c")), STRING_LENGTH)
    assert plan["feasible"] is True and plan["valid"] is True
    assert plan["counts"] == {"a": 138, "b": 134, "c": 70}
    assert plan["total_moved"] == 1
    assert [(c["from"], c["to"], len(c["panels"])) for c in plan["corrections"]] == [("c", "a", 1)]


def test_reversing_the_scan_order_reverses_the_donation():
    """Same three groups, same geometry, order swapped: the tie goes the other way."""
    plan = autofill(three_groups(("c", "b", "a")), STRING_LENGTH)
    assert plan["counts"] == {"a": 136, "b": 134, "c": 72}
    assert [(c["from"], c["to"], len(c["panels"])) for c in plan["corrections"]] == [("a", "c", 1)]


def test_a_drawing_whose_groups_are_already_feasible_moves_nothing():
    plan = autofill([group("a", 137, (0.0, 0.0)), group("b", 134, (0.0, 600.0))], STRING_LENGTH)
    assert plan["corrections"] == [] and plan["total_moved"] == 0
    assert plan["counts"] == {"a": 137, "b": 134}


def test_a_zone_boundary_stops_a_trade_the_geometry_would_allow():
    """Panels cannot cross a zone, so an infeasible lone zone stays infeasible."""
    plan = autofill([group("a", 137, (0.0, 0.0), zone="A|"),
                     group("c", 71, (0.0, 600.0), zone="B|")], STRING_LENGTH)
    assert plan["feasible"] is False and plan["valid"] is False
    assert plan["total_moved"] == 0
    assert plan["counts"] == {"a": 137, "c": 71}
    assert any("infeasible after solver" in violation for violation in plan["violations"])


def test_an_unpartitionable_zone_reports_itself_instead_of_guessing():
    plan = autofill([group("c", 71, (0.0, 0.0))], STRING_LENGTH)
    assert plan["feasible"] is False
    assert any("no feasible partition" in violation for violation in plan["violations"])
    assert plan["counts"] == {"c": 71}


def test_a_far_donor_reaches_its_receiver_through_the_groups_between_them():
    """Chain routing: K=8 leaves no direct edge across twelve groups, so each hop is local."""
    built = _read_groups(line_groups(12, 20, 1000.0))
    width, height = estimate_panel_dimensions(built)
    targets = {g.id: g.count for g in built}
    targets["g0"] -= 1
    targets["g11"] += 1
    corrections = realize_trades(built, targets, width, height)
    assert len(corrections) >= 2
    assert all(len(c["panels"]) == 1 for c in corrections)
    assert corrections[0]["from"] == "g0" and corrections[-1]["to"] == "g11"
    assert all(c["chain"].startswith("hop ") for c in corrections)
    # Every hop is between two groups that are adjacent in the line, and every group
    # in the middle of the chain nets zero.
    steps = [(int(c["from"][1:]), int(c["to"][1:])) for c in corrections]
    assert all(steps[i][1] == steps[i + 1][0] for i in range(len(steps) - 1))
    counts = {g.id: g.count for g in built}
    assert counts["g0"] == 19 and counts["g11"] == 21
    assert all(counts[f"g{i}"] == 20 for i in range(1, 11))


def test_the_snake_starts_at_the_jumper_on_the_receiver_facing_edge():
    donor = _read_groups([group("d", 16, (0.0, 0.0))])[0]
    receiver = _read_groups([group("r", 16, (COLUMNS * PITCH_X * 2, 0.0))])[0]
    one = select_snake_panels(donor.panels, receiver.panels, 1, PITCH_X, PITCH_Y)
    assert len(one) == 1
    assert one[0].x == max(p.x for p in donor.panels)
    five = select_snake_panels(donor.panels, receiver.panels, 5, PITCH_X, PITCH_Y)
    assert len(five) == 5
    assert five[0].id == one[0].id
    assert len({p.id for p in five}) == 5
    assert {p.id for p in five} <= {p.id for p in donor.panels}


def test_the_snake_never_returns_more_panels_than_the_donor_holds():
    donor = _read_groups([group("d", 16, (0.0, 0.0))])[0]
    receiver = _read_groups([group("r", 16, (COLUMNS * PITCH_X * 2, 0.0))])[0]
    picked = select_snake_panels(donor.panels, receiver.panels, 99, PITCH_X, PITCH_Y)
    assert len(picked) == 16 and len({p.id for p in picked}) == 16
    # The donor's own coordinates survive the walk untouched.
    assert [(p.x, p.y) for p in donor.panels] == [
        (panel["x"], panel["y"]) for panel in group("d", 16, (0.0, 0.0))["panels"]]


def test_the_port_never_mutates_the_caller_s_groups():
    supplied = three_groups(("a", "b", "c"))
    before = deepcopy(supplied)
    autofill(supplied, STRING_LENGTH)
    assert supplied == before


def test_the_captured_rooftop_case_moves_exactly_panel_8201(captured):
    """The 2026-09-23 capture: 1 panel moved, 71-panel group to 137-panel group."""
    supplied, plan = captured
    assert [len(entry["panels"]) for entry in supplied] == [137, 134, 123, 104, 111, 174, 71]
    assert plan["feasible"] is True and plan["valid"] is True
    assert plan["total_moved"] == 1
    assert len(plan["corrections"]) == 1
    correction = plan["corrections"][0]
    assert correction["panels"] == [MOVED_PANEL]
    assert (correction["from"], correction["to"]) == (DONOR, RECEIVER)
    assert correction["chain"] == "direct"
    # The plugin printed d=1027, measured between the two centroids after the move.
    assert REPORTED_DISTANCE - 1 <= correction["distance"] <= REPORTED_DISTANCE + 1


def test_the_captured_case_leaves_every_other_group_exactly_as_it_was(captured):
    supplied, plan = captured
    before = {entry["id"]: len(entry["panels"]) for entry in supplied}
    after = plan["counts"]
    assert after[DONOR] == before[DONOR] - 1 == 70
    assert after[RECEIVER] == before[RECEIVER] + 1 == 138
    assert {name: after[name] for name in after if name not in (DONOR, RECEIVER)} == {
        name: count for name, count in before.items() if name not in (DONOR, RECEIVER)}
    assert MOVED_PANEL in plan["membership"][RECEIVER]
    assert MOVED_PANEL not in plan["membership"][DONOR]
    assert all(is_feasible_count(STRING_LENGTH, count) for count in after.values())


CREATED_AT = "2026-09-22T00:00:00+00:00"
UNITS = {"drawing_units": "in", "wcs_to_ucs": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
         "elevation_datum": "unrecorded", "crs": ""}


def builtin(name):
    spec = importlib.util.spec_from_file_location(name, SERVER / "builtins" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


applier = builtin("solar_autofill")


def _provenance():
    return {"created_by": "test", "created_at": CREATED_AT, "last_writer": "test",
            "source_rev": 0, "source_hash": "a" * 64}


def _panel(source):
    return {"id": new_id("panel"), "kind": "panel", "rev": 0, "extra": {},
            "validity": {"state": "valid", "reasons": []}, "provenance": _provenance(),
            "frame_ref": None, "matrix_cell": None,
            "centre": [source["x"], source["y"]], "angle": source["angle"],
            "assignment": {"string_ref": None, "seq": None}}


def _frame(name, members):
    """One group over a single full row, the densest shape the schema allows."""
    cells = [{"code": "panel", "panel_ref": panel["id"], "seq": None, "inverter_id": None,
              "string_input_number": None, "x": panel["centre"][0], "y": panel["centre"][1],
              "angle": panel["angle"]} for panel in members]
    frame = {
        "id": new_id("frame"), "kind": "frame", "rev": 0, "extra": {},
        "validity": {"state": "valid", "reasons": []}, "provenance": _provenance(),
        "name": name, "insertion_point": members[0]["centre"][:], "installation_design": "Roof",
        "panel_refs": [panel["id"] for panel in members], "module_rows": 1,
        "module_columns": len(cells), "module_slots": len(cells), "module_power_watts": 0,
        "module_width_along_row": 77.0, "module_height_across_row": 38.5,
        "electrical_zone_ref": None, "sequences": [], "matrix": [cells],
        "panel_assignments": [{"panel_ref": panel["id"], "string_ref": None, "seq": None,
                               "inverter_id": None, "string_input_number": None}
                              for panel in members],
    }
    for column, panel in enumerate(members):
        panel["frame_ref"] = frame["id"]
        panel["matrix_cell"] = {"row": 0, "col": column}
    return frame


def committed(entries):
    """A committed graph whose frames are exactly the port's `entries`, one row each.

    Returns ``(graph, panel ids by the port's panel id, frame ids by the port's group
    id)``. The builtin speaks graph ids and the plan speaks the port's, so the two
    namespaces are mapped ONCE here rather than scanned per assertion.
    """
    graph = new_empty_graph(tenant_id="studio-autofill-revert", drawing_id="w2-" + "a" * 16,
                            source_hash="a" * 64, units=deepcopy(UNITS), created_at=CREATED_AT)
    panel_ids, frame_ids = {}, {}
    for entry in entries:
        members = []
        for source in entry["panels"]:
            panel = _panel(source)
            panel_ids[source["id"]] = panel["id"]
            graph["panels"].append(panel)
            members.append(panel)
        frame = _frame(entry["id"].replace(":", "-"), members)
        frame_ids[entry["id"]] = frame["id"]
        graph["frames"].append(frame)
    return validate_graph(graph), panel_ids, frame_ids


def membership(graph, panel_ids, frame_ids):
    """Group membership back in the PORT's own names, for comparison across a move."""
    panels = {value: key for key, value in panel_ids.items()}
    frames = {value: key for key, value in frame_ids.items()}
    return {frames[frame["id"]]: frozenset(panels[ref] for ref in frame["panel_refs"])
            for frame in graph["frames"]}


def as_request(graph, corrections, panel_ids, frame_ids):
    """The builtin's {expected_rev, corrections} for a plan written in the port's names."""
    return {"expected_rev": graph["rev"],
            "corrections": [{"from_ref": frame_ids[correction["from"]],
                             "to_ref": frame_ids[correction["to"]],
                             "panel_refs": [panel_ids[ref] for ref in correction["panels"]]}
                            for correction in corrections]}


@pytest.fixture(scope="module")
def captured_graph(captured):
    """The captured rooftop groups as a committed graph, with the port's own plan."""
    supplied, plan = captured
    graph, panel_ids, frame_ids = committed(supplied)
    return graph, plan, panel_ids, frame_ids


def small_graph():
    """Three small groups: enough for a chain, small enough to seed per test."""
    return committed([group("a", 6, (0.0, 0.0)), group("b", 5, (0.0, 600.0)),
                      group("c", 4, (0.0, 1200.0))])


CHAIN = [{"from": "a", "to": "b", "panels": ["a-0", "a-1"]},
         {"from": "b", "to": "c", "panels": ["a-0", "b-0"]}]


def test_reverting_the_captured_auto_fill_puts_8201_back_in_the_71_panel_group(captured_graph):
    """The round trip the plugin's own revert fails: 70 and 138 go back to 71 and 137."""
    graph, plan, panel_ids, frame_ids = captured_graph
    before = membership(graph, panel_ids, frame_ids)
    assert len(before[DONOR]) == 71 and len(before[RECEIVER]) == 137
    plan_request = as_request(graph, plan["corrections"], panel_ids, frame_ids)
    filled = applier.apply_corrections(graph, plan_request)["graph"]
    moved = membership(filled, panel_ids, frame_ids)
    assert MOVED_PANEL in moved[RECEIVER] and MOVED_PANEL not in moved[DONOR]
    assert len(moved[DONOR]) == 70 and len(moved[RECEIVER]) == 138

    reverted = applier.revert_corrections(
        filled, {**plan_request, "expected_rev": filled["rev"]})["graph"]
    after = membership(reverted, panel_ids, frame_ids)
    assert MOVED_PANEL in after[DONOR] and MOVED_PANEL not in after[RECEIVER]
    assert len(after[DONOR]) == 71 and len(after[RECEIVER]) == 137
    # Every group, not just the two the correction named.
    assert after == before
    assert validate_graph(reverted) == reverted
    assert reverted["rev"] == filled["rev"] + 1 == graph["rev"] + 2


def test_the_revert_restores_every_panel_s_own_frame_and_matrix_cell(captured_graph):
    """Membership is not enough: the moved panel goes back into the slot it emptied."""
    graph, plan, panel_ids, frame_ids = captured_graph
    plan_request = as_request(graph, plan["corrections"], panel_ids, frame_ids)
    filled = applier.apply_corrections(graph, plan_request)["graph"]
    reverted = applier.revert_corrections(
        filled, {**plan_request, "expected_rev": filled["rev"]})["graph"]
    donor = frame_ids[DONOR]

    def occupied(state):
        frame = next(entry for entry in state["frames"] if entry["id"] == donor)
        return {cell["panel_ref"]: (row, column)
                for row, line in enumerate(frame["matrix"])
                for column, cell in enumerate(line) if cell["panel_ref"] is not None}

    # The donation emptied one slot and the revert reuses that same slot, so the
    # donor's matrix comes back identical rather than merely the right size.
    assert occupied(reverted) == occupied(graph) and len(occupied(graph)) == 71
    placed = {panel["id"]: (panel["frame_ref"], panel["matrix_cell"])
              for panel in reverted["panels"]}
    started = {panel["id"]: (panel["frame_ref"], panel["matrix_cell"])
               for panel in graph["panels"]}
    assert placed[panel_ids[MOVED_PANEL]] == started[panel_ids[MOVED_PANEL]]
    assert placed == started


def test_a_chain_reverts_hop_by_hop_backwards():
    """The second hop hands its panels back FIRST, so the first hop finds them."""
    graph, panel_ids, frame_ids = small_graph()
    before = membership(graph, panel_ids, frame_ids)
    chain = as_request(graph, CHAIN, panel_ids, frame_ids)
    filled = applier.apply_corrections(graph, chain)["graph"]
    moved = membership(filled, panel_ids, frame_ids)
    assert moved["a"] == before["a"] - {"a-0", "a-1"}
    assert moved["c"] == before["c"] | {"a-0", "b-0"}
    result = applier.revert_corrections(filled, {**chain, "expected_rev": filled["rev"]})
    assert membership(result["graph"], panel_ids, frame_ids) == before
    assert result["corrections"] == 2 and result["panels_moved"] == 4


def test_a_graph_that_was_never_auto_filled_is_refused_before_anything_moves():
    graph, panel_ids, frame_ids = small_graph()
    untouched = deepcopy(graph)
    with pytest.raises(GraphValidationError) as raised:
        applier.revert_corrections(graph, as_request(graph, CHAIN, panel_ids, frame_ids))
    assert raised.value.code == "PANEL_NOT_IN_GROUP"
    assert graph == untouched


def test_reverting_twice_is_refused_because_the_second_graph_no_longer_matches():
    graph, panel_ids, frame_ids = small_graph()
    chain = as_request(graph, CHAIN, panel_ids, frame_ids)
    filled = applier.apply_corrections(graph, chain)["graph"]
    reverted = applier.revert_corrections(filled, {**chain, "expected_rev": filled["rev"]})["graph"]
    untouched = deepcopy(reverted)
    with pytest.raises(GraphValidationError) as raised:
        applier.revert_corrections(reverted, {**chain, "expected_rev": reverted["rev"]})
    assert raised.value.code == "PANEL_NOT_IN_GROUP"
    assert reverted == untouched


def test_a_correction_naming_a_panel_another_group_holds_is_refused():
    """Scoped to the pair it names: c never received b-1, so the revert moves nothing."""
    graph, panel_ids, frame_ids = small_graph()
    plan = [{"from": "a", "to": "c", "panels": ["b-1"]}]
    untouched = deepcopy(graph)
    with pytest.raises(GraphValidationError) as raised:
        applier.revert_corrections(graph, as_request(graph, plan, panel_ids, frame_ids))
    assert raised.value.code == "PANEL_NOT_IN_GROUP"
    assert graph == untouched


def test_the_revert_refuses_a_stale_revision_and_an_unknown_group():
    graph, panel_ids, frame_ids = small_graph()
    chain = as_request(graph, CHAIN, panel_ids, frame_ids)
    with pytest.raises(GraphValidationError) as stale:
        applier.revert_corrections(graph, {**chain, "expected_rev": graph["rev"] + 1})
    assert stale.value.code == "STALE_GRAPH_REVISION"
    missing = as_request(graph, [{"from": "a", "to": "b", "panels": ["a-0"]}], panel_ids, frame_ids)
    # The inverse gives the panels BACK to this group, so an id no frame carries is
    # caught before any membership is read.
    missing["corrections"][0]["from_ref"] = new_id("frame")
    with pytest.raises(GraphValidationError) as absent:
        applier.revert_corrections(graph, missing)
    assert absent.value.code == "MISSING_FRAME"


@pytest.mark.parametrize("params", [
    {"expected_rev": 0, "corrections": []},
    {"expected_rev": 0},
    {"expected_rev": 0, "corrections": [], "extra": 1},
    {"expected_rev": "0", "corrections": [{"from_ref": "a", "to_ref": "b", "panel_refs": ["p"]}]},
    {"expected_rev": 0, "corrections": [{"from_ref": "a", "to_ref": "a", "panel_refs": ["p"]}]},
    {"expected_rev": 0, "corrections": [{"from_ref": "a", "to_ref": "b", "panel_refs": ["p", "p"]}]},
    {"expected_rev": 0, "corrections": [{"from_ref": "a", "to_ref": "b", "panel_refs": []}]},
])
def test_a_malformed_revert_request_is_refused_as_an_apply_would_be(params):
    """An empty correction list is refused exactly as apply_corrections refuses it."""
    graph, _, _ = small_graph()
    untouched = deepcopy(graph)
    with pytest.raises(GraphValidationError) as raised:
        applier.revert_corrections(graph, params)
    assert raised.value.code == "INVALID_AUTOFILL_REQUEST"
    assert graph == untouched


def test_the_builtin_exposes_the_revert_as_its_own_operation():
    graph, panel_ids, frame_ids = small_graph()
    chain = as_request(graph, CHAIN, panel_ids, frame_ids)
    filled = applier.run(graph, {"operation": "apply-corrections", **chain})
    reverted = applier.run(filled, {"operation": "revert-corrections",
                                    **chain, "expected_rev": filled["rev"]})
    assert membership(reverted, panel_ids, frame_ids) == membership(graph, panel_ids, frame_ids)


@pytest.mark.parametrize("groups,length,message", [
    ({"id": "a"}, STRING_LENGTH, "groups must be a sequence"),
    ([{"zone": "|", "panels": []}], STRING_LENGTH, "group id must be"),
    ([group("a", 2, (0.0, 0.0)), {"id": "a", "zone": "|", "panels": []}],
     STRING_LENGTH, "group ids must be unique"),
    ([group("a", 2, (0.0, 0.0)),
      {"id": "b", "zone": "|", "panels": [{"id": "a-0", "x": 0.0, "y": 0.0, "angle": 0.0}]}],
     STRING_LENGTH, "only one group"),
    ([{"id": "a", "zone": "|", "panels": [{"id": "p", "x": float("nan"), "y": 0.0, "angle": 0.0}]}],
     STRING_LENGTH, "must be a finite number"),
    ([group("a", 2, (0.0, 0.0))], 0, "panels per string"),
])
def test_a_malformed_plan_is_refused_before_anything_is_computed(groups, length, message):
    with pytest.raises(AutofillError) as raised:
        autofill(groups, length)
    assert message in str(raised.value)
