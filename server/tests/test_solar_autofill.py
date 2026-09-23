"""Offline checks for the auto-fill port: the DP, the chain routing, the snake, the capture.

Nothing here touches a graph, a network or a builtin: server/solar_autofill.py is a
pure port of the plugin's OptimalPlanSolver and SnakePanelSelector, so it is tested
as one. The captured case at the end is COMPUTED from the committed rooftop intake
(the same kernel, the same four grouping parameters, the same 28 removals the
licensed run made), never asserted from a stored answer: the only thing taken from
the capture is its INPUT (which panels the removal took) and its OUTCOME (one panel,
8201, from the 71-panel group to the 137-panel group).
"""
import json
from copy import deepcopy
from pathlib import Path

import pytest

import solar_panel_group_kernel as kernel
from solar_autofill import (
    AutofillError, autofill, estimate_panel_dimensions, find_nearest_feasible,
    is_feasible_count, realize_trades, select_snake_panels, _read_groups,
)

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
