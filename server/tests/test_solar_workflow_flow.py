"""Studio's Rooftop workflow flow engine against the plugin's flow source (contract G34).

Covered: every Rooftop step's CanAdvance and HasDrawingPrerequisites, true and false
(ZonesStepPanel with PaletteZoneAdvanceGate rule by rule and in rule order, PanelGroupsStepPanel
with PanelGroupStringLengthGate, SolveStepPanel's string polylines and its prerequisite's scan
count with the persisted string-number fallback, the Equipment and Combiners L1 and L1/L2 modes
read from the host's per-user setting and never from the drawing's copy (G34b), Homeruns, and the action-only Export and Project Summary); the .NET Trim and OrdinalIgnoreCase
rules the zone names go through; the controller's two status layers (in-memory Steps[i].Status,
the per-user file's StepStatuses): AdvanceStep, GoBack and NavigateToStep Stale marking read from
memory, TestJumpToStep and BTHost's palette_set_step, the restore clamp, and the
ValidateDrawingState reopen cascade read from the file (first failing Complete step and everything
after it reset, Stale steps skipped); the Zones activation's string-length sync and the refusal
when the per-user setting is unknown but read; the committed plugin-adapter intakes of the three
G34a fixture states (docs/parity/evidence/rooftop/flow), their per-step can_advance and the G34a
event script, including the observed Back that marks steps 2..6 Stale although the file never held
them Complete; and every intake refusal.
"""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
INTAKES = ROOT / "docs" / "parity" / "evidence" / "rooftop" / "flow"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


flow = _load("solar_workflow_flow", ROOT / "server" / "solar_workflow_flow.py")

C, S, N = flow.COMPLETE, flow.STALE, flow.NOT_STARTED
T, F = True, False


def strings(polylines=0, paths=0, persisted=0):
    return {"polylines": polylines, "paths": paths, "persisted": persisted}


def missing(electrical=0, elevation=0):
    return {"electrical": electrical, "elevation": elevation}


# Every gate open: one sized electrical zone, a confirmed drawing length, groups, strings, L1
# equipment and homeruns.
READY = {
    "flow": "rooftop",
    "electrical_zones": [{"name": "Zone A", "panels": ["1A2", "1A3"], "panels_in_sequence": 12}],
    "elevation_zones": [],
    "missing_zone_panels": missing(),
    "panels_in_sequence": 12,
    "global_string_sizing_confirmed": True,
    "panel_groups": 3,
    "strings": strings(5, 5, 5),
    "inverters": 2,
    "l2_collectors": 0,
    "combiners": 2,
    "host_use_l2_collectors": False,
    "use_l2_collectors": False,
    "homeruns": 4,
}
EMPTY = dict({key: 0 for key in flow.COUNT_FIELDS}, flow="rooftop", electrical_zones=[], elevation_zones=[],
             missing_zone_panels=missing(), panels_in_sequence=0, global_string_sizing_confirmed=False,
             strings=strings(), host_use_l2_collectors=False, use_l2_collectors=False)

# G34b: the capture host's per-user UseL2Collectors was true for every G34a capture.
CAPTURE_HOST_USE_L2 = True


def intake(name):
    """A committed plugin-adapter intake (G34a fixture state) carrying G34b's host input: the
    committed file's own value once the adapter has regenerated it, else the capture host's."""
    raw = json.loads((INTAKES / f"{name}.json").read_text(encoding="utf-8"))
    raw.setdefault("host_use_l2_collectors", CAPTURE_HOST_USE_L2)
    return raw


UNSPLIT, ZSPLIT, FULL_RUN = intake("unsplit"), intake("zsplit"), intake("full_run")


def sd(statuses):
    """A status list as the per-user file's StepStatuses (absent = not-started, 1/2 values)."""
    return {str(i): {C: 1, S: 2}[s] for i, s in enumerate(statuses) if s != N}


def facts(base=READY, **over):
    raw = copy.deepcopy(base)
    raw.update(copy.deepcopy(over))
    return raw


def ez(name="Zone A", panels=("1A2",), pis=12):
    return {"name": name, "panels": list(panels), "panels_in_sequence": pis}


def lz(name="Roof 1", panels=("1A2",)):
    return {"name": name, "panels": list(panels)}


def gate(**over):
    return flow.zone_advance_gate(flow.validate_facts(facts(**over)))


# ------------------------------------------------------------ zones gate --

def test_zone_gate_ready_with_a_sized_electrical_zone():
    assert gate() == "Ready"


@pytest.mark.parametrize("pis, confirmed, expected", [
    (12, True, "Ready"),
    (0, True, "MissingGlobalStringSizing"),
    (12, False, "MissingGlobalStringSizing"),
    (-1, True, "MissingGlobalStringSizing"),
])
def test_zone_gate_without_electrical_zones_needs_a_confirmed_drawing_length(pis, confirmed, expected):
    assert gate(electrical_zones=[], panels_in_sequence=pis, global_string_sizing_confirmed=confirmed) == expected


@pytest.mark.parametrize("zones, missing_electrical, expected", [
    ([ez(name="")], 0, "EmptyElectricalZoneName"),
    ([ez(name=" \t　")], 0, "EmptyElectricalZoneName"),
    ([ez(name=None)], 0, "EmptyElectricalZoneName"),
    ([ez(name="Zone A"), ez(name=" zone a ")], 0, "DuplicateElectricalZoneName"),
    ([ez(panels=())], 0, "EmptyElectricalZonePanels"),
    ([ez()], 1, "MissingElectricalZonePanelHandles"),
    ([ez(pis=0)], 0, "MissingElectricalZoneSizing"),
    ([ez(pis=-3)], 0, "MissingElectricalZoneSizing"),
])
def test_zone_gate_electrical_rules(zones, missing_electrical, expected):
    assert gate(electrical_zones=zones, missing_zone_panels=missing(missing_electrical)) == expected


def test_zone_gate_rules_run_in_source_order():
    # A zone with no panels AND no sizing reports the panels rule first (:89 before :93), and a
    # nameless zone beside a duplicate pair reports the name rule first (:85 before :87).
    assert gate(electrical_zones=[ez(panels=(), pis=0)], missing_zone_panels=missing(3)) \
        == "EmptyElectricalZonePanels"
    assert gate(electrical_zones=[ez(name="A"), ez(name="a"), ez(name="")]) == "EmptyElectricalZoneName"
    # The electrical rules run before the elevation ones (:82 before :97).
    assert gate(electrical_zones=[ez(pis=0)], elevation_zones=[lz(name="")]) == "MissingElectricalZoneSizing"


def test_electrical_zones_bypass_the_global_sizing_rule():
    assert gate(panels_in_sequence=0, global_string_sizing_confirmed=False) == "Ready"


@pytest.mark.parametrize("zones, missing_elevation, expected", [
    ([lz()], 0, "Ready"),
    ([lz(name="  ")], 0, "EmptyElevationZoneName"),
    ([lz(name=None)], 0, "EmptyElevationZoneName"),
    ([lz(name="Roof"), lz(name="ROOF")], 0, "DuplicateElevationZoneName"),
    ([lz(panels=())], 0, "EmptyElevationZonePanels"),
    ([lz()], 2, "MissingElevationZonePanelHandles"),
])
def test_zone_gate_elevation_rules(zones, missing_elevation, expected):
    assert gate(elevation_zones=zones, missing_zone_panels=missing(0, missing_elevation)) == expected


def test_zone_names_trim_like_dotnet_not_like_python():
    # U+001C is whitespace to Python's str.strip but not to .NET's char.IsWhiteSpace.
    assert gate(electrical_zones=[ez(name="\x1c")]) == "Ready"
    assert gate(electrical_zones=[ez(name="\x85\xa0")]) == "EmptyElectricalZoneName"


def test_zone_names_compare_ordinal_ignore_case_per_char():
    # U+00DF upper-cases to two chars in Python; OrdinalIgnoreCase keeps it, so no duplicate.
    assert gate(electrical_zones=[ez(name="straße"), ez(name="STRASSE")]) == "Ready"
    assert gate(electrical_zones=[ez(name="été"), ez(name="ÉTÉ")]) == "DuplicateElectricalZoneName"


# ------------------------------------------------- panel-group length gate --

@pytest.mark.parametrize("groups, has_zones, pis, confirmed, setting, expected", [
    (0, False, 0, False, 12, "Ready"),
    (3, True, 0, False, 5, "Ready"),
    (3, False, 0, True, 12, "MissingDrawingStringSizing"),
    (3, False, 12, False, 12, "MissingDrawingStringSizing"),
    (3, False, 12, False, None, "MissingDrawingStringSizing"),
    (3, False, 12, True, 10, "StringLengthChangedAfterPanelGroups"),
    (3, False, 12, True, 0, "Ready"),
    (3, False, 12, True, 12, "Ready"),
])
def test_panel_group_string_length_gate(groups, has_zones, pis, confirmed, setting, expected):
    assert flow.panel_group_string_length_gate(groups, has_zones, pis, confirmed, setting) == expected


def test_panel_group_gate_refuses_an_unknown_setting_it_would_read():
    with pytest.raises(flow.FlowInputError):
        flow.panel_group_string_length_gate(3, False, 12, True, None)


# ------------------------------------------------------------ step gates --

NO_ZONES = {"electrical_zones": []}


@pytest.mark.parametrize("index, over, setting, expected", [
    (0, {}, 12, True),
    (0, {**NO_ZONES, "global_string_sizing_confirmed": False}, 12, False),
    (1, {}, 12, True),
    (1, {"panel_groups": 0}, 12, False),
    (1, {**NO_ZONES}, 10, False),
    (1, {**NO_ZONES}, 12, True),
    (1, {**NO_ZONES, "global_string_sizing_confirmed": False}, 12, False),
    (2, {}, 12, True),
    (2, {"strings": strings(0, 9, 9)}, 12, False),
    (3, {}, 12, True),
    (3, {"inverters": 0}, 12, False),
    (3, {"inverters": 0, "l2_collectors": 1}, 12, True),
    (3, {"host_use_l2_collectors": True, "inverters": 5}, 12, False),
    (3, {"host_use_l2_collectors": True, "l2_collectors": 1}, 12, True),
    (3, {"use_l2_collectors": True, "inverters": 5}, 12, True),        # the drawing copy is not read
    (4, {"combiners": 0}, 12, True),
    (4, {"host_use_l2_collectors": True, "combiners": 0, "l2_collectors": 2}, 12, False),
    (4, {"host_use_l2_collectors": True, "combiners": 1}, 12, True),
    (4, {"use_l2_collectors": True, "combiners": 0}, 12, True),        # the drawing copy is not read
    (5, {}, 12, True),
    (5, {"homeruns": 0}, 12, False),
    (6, {"_empty": True}, None, True),
    (7, {"_empty": True}, None, True),
])
def test_step_can_advance(index, over, setting, expected):
    over = dict(over)
    base = EMPTY if over.pop("_empty", False) else READY
    assert flow.step_can_advance(index, flow.validate_facts(facts(base, **over)), setting) is expected


@pytest.mark.parametrize("index, over, expected", [
    (0, {}, True),
    (0, {**NO_ZONES}, False),
    (0, {"electrical_zones": [ez(panels=()), ez(name="B", panels=())]}, False),
    (0, {"electrical_zones": [ez(panels=()), ez(name="B")]}, True),
    (1, {}, True),
    (1, {"panel_groups": 0}, False),
    (2, {}, True),
    (2, {"strings": strings(0, 0, 1)}, True),
    (2, {"strings": strings(0, 3, 0)}, True),
    (2, {"strings": strings(0, 0, 0)}, False),
    (3, {}, True),
    (3, {"inverters": 0, "l2_collectors": 1}, True),
    (3, {"inverters": 0}, False),
    (4, {"combiners": 0}, True),
    (4, {"host_use_l2_collectors": True, "combiners": 0, "l2_collectors": 1}, False),
    (4, {"host_use_l2_collectors": True}, True),
    (4, {"use_l2_collectors": True, "combiners": 0}, True),            # the drawing copy is not read
    (5, {}, True),
    (5, {"homeruns": 0}, False),
    (6, {"_empty": True}, True),
    (7, {"_empty": True}, True),
])
def test_step_has_prerequisites(index, over, expected):
    over = dict(over)
    base = EMPTY if over.pop("_empty", False) else READY
    assert flow.step_has_prerequisites(index, flow.validate_facts(facts(base, **over))) is expected


@pytest.mark.parametrize("index", [-1, 8, True, "1"])
def test_step_index_is_refused_out_of_range(index):
    with pytest.raises(flow.FlowInputError):
        flow.step_can_advance(index, flow.validate_facts(facts()), 12)


# ------------------------------------------------------------ controller --

def test_open_starts_at_the_zones_step_with_nothing_started():
    f = flow.RooftopFlow.open(facts())
    assert f.snapshot() == {"current_index": 0, "statuses": [N] * 8}


def test_open_syncs_the_string_length_setting_from_a_confirmed_drawing():
    raw = facts(**NO_ZONES)
    assert flow.step_can_advance(1, flow.validate_facts(raw), 10) is False
    f = flow.RooftopFlow.open(raw, 10)
    assert f.settings_string_length == 12
    assert f.can_advance(1) is True
    assert flow.RooftopFlow.open(raw).settings_string_length == 12


def test_open_keeps_the_setting_when_the_drawing_length_is_unconfirmed():
    assert flow.RooftopFlow.open(facts(global_string_sizing_confirmed=False), 10).settings_string_length == 10
    assert flow.RooftopFlow.open(facts(global_string_sizing_confirmed=False)).settings_string_length is None


def test_zones_activation_resyncs_the_setting():
    f = flow.RooftopFlow.open(facts())
    assert f.advance()
    f.settings_string_length = 5
    assert f.go_back()
    assert f.settings_string_length == 12


def test_restore_past_the_zones_step_refuses_an_unknown_setting_the_gate_reads():
    f = flow.RooftopFlow.restore(facts(**NO_ZONES), 1, {}, None)
    assert f.snapshot() == {"current_index": 1, "statuses": [N] * 8}
    with pytest.raises(flow.FlowInputError):
        f.can_advance()
    assert flow.RooftopFlow.restore(facts(**NO_ZONES), 1, {}, 12).can_advance() is True
    assert flow.RooftopFlow.restore(facts(**NO_ZONES), 1, {}, 10).can_advance() is False


@pytest.mark.parametrize("setting", ["12", 12.0, True, 2 ** 31])
def test_a_malformed_setting_is_refused(setting):
    with pytest.raises(flow.FlowInputError):
        flow.RooftopFlow.open(facts(), setting)


def test_advance_marks_the_step_left_complete_and_moves():
    f = flow.RooftopFlow.open(facts())
    assert f.advance() is True
    assert f.snapshot() == {"current_index": 1, "statuses": [C] + [N] * 7}


def test_advance_is_blocked_by_the_current_gate():
    f = flow.RooftopFlow.open(facts(panel_groups=0))
    assert f.advance() is True
    assert f.advance() is False
    assert f.snapshot() == {"current_index": 1, "statuses": [C] + [N] * 7}


def test_advance_stops_at_the_last_step():
    f = flow.RooftopFlow.open(facts())
    assert [f.advance() for _ in range(8)] == [True] * 7 + [False]
    assert f.snapshot() == {"current_index": 7, "statuses": [C] * 7 + [N]}


def test_advance_marks_a_stale_step_complete_again():
    f = flow.RooftopFlow.open(facts())
    f.advance(), f.advance(), f.advance()
    f.go_back(), f.go_back()
    assert f.statuses()[:3] == [C, C, S]
    assert f.advance()
    assert f.snapshot() == {"current_index": 2, "statuses": [C, C, S] + [N] * 5}
    assert f.advance()
    assert f.snapshot() == {"current_index": 3, "statuses": [C, C, C] + [N] * 5}


def test_go_back_marks_complete_steps_from_the_current_one_on_stale():
    f = flow.RooftopFlow.open(facts())
    f.advance(), f.advance(), f.advance()
    assert f.go_back() is True           # the step left (3) never completed: nothing turns stale
    assert f.snapshot() == {"current_index": 2, "statuses": [C, C, C] + [N] * 5}
    assert f.go_back() is True           # the step left (2) was complete: it turns stale
    assert f.snapshot() == {"current_index": 1, "statuses": [C, C, S] + [N] * 5}


def test_go_back_marks_later_in_memory_complete_steps_stale_too():
    # Step 6's object is Complete in memory only (the file never held it): NavigateToStep(3)
    # from 5 does not reach it (:884), GoBack from 3 does (:991), and writes Stale to the file.
    f = flow.RooftopFlow.open(facts())
    f.test_jump_to_step(5)
    f.step_statuses[6] = C
    f.navigate_to_step(3)
    assert f.snapshot() == {"current_index": 3, "statuses": [C, C, C, C, S, N, N, N]}
    assert f.go_back()
    assert f.snapshot() == {"current_index": 2, "statuses": [C, C, C, S, S, N, S, N]}
    assert f.step_statuses == [C, C, C, S, S, N, S, N]


def test_navigate_to_step_reads_the_in_memory_layer_not_the_file():
    f = flow.RooftopFlow.open(facts())
    f.test_jump_to_step(4)
    f.state_statuses.pop(2)                  # the file forgot step 2; its object is still Complete
    assert f.navigate_to_step(1)
    assert f.snapshot() == {"current_index": 1, "statuses": [C, C, S, S, N, N, N, N]}


def test_g34a_back_marks_steps_the_file_never_held_complete_stale():
    # The observed plugin behaviour, on the committed zones-fixture intake: the per-step capture
    # (palette_set_step 0..7) leaves steps 0..6 Complete in memory; palette_set_step 1 rewrites
    # the FILE to {0: complete}; one advance completes step 1; the Back from step 2 then turns
    # 2..6 Stale in the file (GoBack reads Steps[i].Status, PaletteController.cs:993) although the
    # file never held them Complete.
    f = flow.RooftopFlow.open(facts(ZSPLIT))
    flow.capture_steps(f)
    assert f.step_statuses == [C] * 7 + [N]
    f.palette_set_step(1)
    assert f.snapshot() == {"current_index": 1, "statuses": [C] + [N] * 7}
    assert f.step_statuses == [C] * 7 + [N]
    assert f.advance() is True
    assert f.advance() is False
    assert f.go_back() is True
    assert f.snapshot() == {"current_index": 1, "statuses": [C, C, S, S, S, S, S, N]}


def test_a_fresh_open_clears_both_layers():
    f = flow.RooftopFlow.open(facts(ZSPLIT))
    flow.capture_steps(f)
    g = flow.RooftopFlow.open(facts(ZSPLIT))
    g.palette_set_step(1)
    g.advance()
    assert g.go_back()
    assert g.snapshot() == {"current_index": 1, "statuses": [C, C] + [N] * 6}


@pytest.mark.parametrize("index, expected", [(3, 3), (99, 7), (-4, 0)])
def test_palette_set_step_clamps_and_rewrites_only_the_file(index, expected):
    f = flow.RooftopFlow.open(facts())
    f.test_jump_to_step(7)
    assert f.palette_set_step(index) == expected
    assert f.snapshot() == {"current_index": expected, "statuses": [C] * expected + [N] * (8 - expected)}
    assert f.step_statuses == [C] * 7 + [N]
    with pytest.raises(flow.FlowInputError):
        f.palette_set_step("1")


def test_go_back_at_the_first_step_is_a_no_op():
    f = flow.RooftopFlow.open(facts())
    assert f.go_back() is False
    assert f.snapshot() == {"current_index": 0, "statuses": [N] * 8}


def test_navigate_to_step_marks_the_steps_between_stale():
    f = flow.RooftopFlow.open(facts())
    for _ in range(5):
        f.advance()
    assert f.navigate_to_step(1) is True
    assert f.snapshot() == {"current_index": 1, "statuses": [C, C, S, S, S, N, N, N]}


def test_navigate_to_step_refuses_forward_and_out_of_range():
    f = flow.RooftopFlow.open(facts())
    f.advance(), f.advance()
    before = f.snapshot()
    assert f.navigate_to_step(3) is False
    assert f.navigate_to_step(-1) is False
    assert f.navigate_to_step(8) is False
    assert f.snapshot() == before
    with pytest.raises(flow.FlowInputError):
        f.navigate_to_step("1")


def test_navigate_to_the_current_step_changes_nothing():
    f = flow.RooftopFlow.open(facts())
    f.advance(), f.advance()
    before = f.snapshot()
    assert f.navigate_to_step(2) is True
    assert f.snapshot() == before


def test_test_jump_to_step_marks_the_prior_steps_complete_unless_asked_not_to():
    f = flow.RooftopFlow.open(facts())
    assert f.test_jump_to_step(4) is True
    assert f.snapshot() == {"current_index": 4, "statuses": [C] * 4 + [N] * 4}
    g = flow.RooftopFlow.open(facts())
    assert g.test_jump_to_step(4, mark_prior_complete=False) is True
    assert g.snapshot() == {"current_index": 4, "statuses": [N] * 8}
    assert g.test_jump_to_step(8) is False


def test_validate_resets_the_first_failing_complete_step_and_everything_after():
    f = flow.RooftopFlow.restore(facts(panel_groups=0), 5, sd([C, C, C, C, C, N, N, N]), 12)
    assert f.snapshot() == {"current_index": 1, "statuses": [C] + [N] * 7}
    assert f.step_statuses == [C] + [N] * 7


def test_validate_skips_stale_steps():
    f = flow.RooftopFlow.restore(facts(panel_groups=0), 3, sd([C, S, C, N, N, N, N, N]), 12)
    assert f.snapshot() == {"current_index": 3, "statuses": [C, S, C, N, N, N, N, N]}


def test_validate_returns_none_when_every_complete_step_still_holds():
    f = flow.RooftopFlow.restore(facts(), 4, sd([C, C, C, C, N, N, N, N]), 12)
    assert f.validate_drawing_state() is None
    assert f.snapshot() == {"current_index": 4, "statuses": [C, C, C, C, N, N, N, N]}


def test_validate_reads_the_file_layer_not_the_in_memory_one():
    # A step Complete only in memory is not checked (:1028 reads State.StepStatuses).
    f = flow.RooftopFlow.open(facts(panel_groups=0))
    f.step_statuses[1] = C
    assert f.reopen() is None
    assert f.snapshot() == {"current_index": 0, "statuses": [N] * 8}


def test_restore_takes_the_statuses_into_both_layers():
    f = flow.RooftopFlow.restore(facts(), 2, {"0": 1, "1": 2, 2: "complete", "9": 1, "3": 0}, 12)
    assert f.snapshot() == {"current_index": 2, "statuses": [C, S, C] + [N] * 5}
    assert f.step_statuses == [C, S, C] + [N] * 5


def test_reopen_with_changed_drawing_facts_cascades_back():
    f = flow.RooftopFlow.open(facts())
    f.advance(), f.advance(), f.advance(), f.advance()
    assert f.reopen(facts(strings=strings())) == 2
    assert f.snapshot() == {"current_index": 2, "statuses": [C, C] + [N] * 6}


def test_reopen_keeps_the_solve_step_on_the_persisted_string_number():
    # The scan finds no string path but the next string number says strings were solved
    # (PaletteDrawingCounts.cs:92-93): the Solve prerequisite holds, no cascade.
    f = flow.RooftopFlow.open(facts())
    f.advance(), f.advance(), f.advance()
    assert f.reopen(facts(strings=strings(0, 0, 4))) is None
    assert f.snapshot() == {"current_index": 3, "statuses": [C, C, C] + [N] * 5}


def test_reopen_on_the_global_sizing_path_resets_to_the_zones_step():
    # Zones advances on a confirmed drawing length without electrical zones, but its
    # HasDrawingPrerequisites needs an electrical zone with panels (ZonesStepPanel.cs:56-73).
    f = flow.RooftopFlow.open(facts(**NO_ZONES))
    f.advance(), f.advance()
    assert f.reopen() == 0
    assert f.snapshot() == {"current_index": 0, "statuses": [N] * 8}


def test_restore_clamps_the_persisted_index():
    assert flow.RooftopFlow.restore(facts(), 99, {}, 12).current_index == 7
    assert flow.RooftopFlow.restore(facts(), -5, {}, 12).current_index == 0


@pytest.mark.parametrize("statuses", [[N] * 8, "not-started", {"0": 3}, {"0": "done"}, {"x": 1},
                                      {1.0: 1}, {"0": True}, {str(i): 1 for i in range(33)}])
def test_restore_refuses_malformed_statuses(statuses):
    with pytest.raises(flow.FlowInputError):
        flow.RooftopFlow.restore(facts(), 0, statuses, 12)


def test_restore_refuses_a_malformed_index():
    with pytest.raises(flow.FlowInputError):
        flow.RooftopFlow.restore(facts(), "1", {}, 12)


# ------------------------------------------------------- G34 on the intakes --

def test_flow_steps_lists_every_rooftop_step_in_order_with_g34a_fields_only():
    rows = flow.flow_steps(facts(homeruns=0))
    assert [r["step"] for r in rows] == ["zones", "panel-groups", "solve", "inverters", "combiners", "homeruns",
                                         "export", "reopt"]
    assert [r["index"] for r in rows] == list(range(8))
    assert {r["flow"] for r in rows} == {"rooftop"}
    assert [r["can_advance"] for r in rows] == [True] * 5 + [False, True, True]
    assert {frozenset(r) for r in rows} == {frozenset({"flow", "index", "step", "can_advance"})}


@pytest.mark.parametrize("name", ["unsplit", "zsplit", "full_run"])
def test_the_committed_intakes_are_accepted_with_the_host_input(name):
    assert flow.validate_facts(intake(name)) == intake(name)


# The literal port on the committed drawings with the host's setting off: the Combiners step
# passes through (CombinerStepPanel.cs:41).
@pytest.mark.parametrize("state, expected", [
    (UNSPLIT, [F, F, F, F, T, F, T, T]),
    (ZSPLIT, [F, T, F, F, T, F, T, T]),
    (FULL_RUN, [F, F, T, F, T, F, T, T]),
])
def test_flow_steps_on_the_committed_intakes_with_the_host_setting_off(state, expected):
    assert [r["can_advance"] for r in flow.flow_steps(facts(state, host_use_l2_collectors=False))] == expected


# With the capture host's setting on (G34b; the Combiners step then needs a combiner, :42), the same
# drawings give G34a's observed per-step can_advance exactly, whatever the drawing's own copy says.
@pytest.mark.parametrize("drawing_copy", [False, True])
@pytest.mark.parametrize("state, expected", [
    (UNSPLIT, [F, F, F, F, F, F, T, T]),
    (ZSPLIT, [F, T, F, F, F, F, T, T]),
    (FULL_RUN, [F, F, T, F, F, F, T, T]),
])
def test_flow_steps_with_the_host_l1_l2_setting_on_match_g34a(state, expected, drawing_copy):
    raw = facts(state, host_use_l2_collectors=True, use_l2_collectors=drawing_copy)
    assert [r["can_advance"] for r in flow.flow_steps(raw)] == expected


G34A_EVENTS = [
    {"event": "jump", "current_index": 1, "statuses": [C] + [N] * 7},
    {"event": "advance", "advanced": True, "current_index": 2, "statuses": [C, C] + [N] * 6},
    {"event": "advance", "advanced": False, "current_index": 2, "statuses": [C, C] + [N] * 6},
    {"event": "back", "current_index": 1, "statuses": [C, C, S, S, S, S, S, N]},
    {"event": "reopen", "current_index": 1, "statuses": [C, C, S, S, S, S, S, N]},
    {"event": "reopen", "current_index": 0, "statuses": [N] * 8},
]


@pytest.mark.parametrize("l2", [False, True])
def test_flow_events_g34a_script_on_the_committed_zones_intake(l2):
    # Jump to 1, one advance, refused at Solve, the Back that stales 2..6, the zones reopen that
    # keeps every prerequisite, and the unsplit reopen whose Zones prerequisite fails (no zones).
    events = flow.flow_events(facts(ZSPLIT, host_use_l2_collectors=l2), facts(UNSPLIT, host_use_l2_collectors=l2))
    assert events == G34A_EVENTS


def test_flow_events_advance_refused_at_the_last_step():
    # The first advancing step of the unsplit state (L1/L2 on) is Export (6): one advance, then the
    # last step refuses (:952); the Back from 7 finds step 7 never Complete; the reopen cascades from
    # step 0.
    unsplit = facts(UNSPLIT, host_use_l2_collectors=True)
    events = flow.flow_events(unsplit, copy.deepcopy(unsplit))
    assert events == [
        {"event": "jump", "current_index": 6, "statuses": [C] * 6 + [N, N]},
        {"event": "advance", "advanced": True, "current_index": 7, "statuses": [C] * 7 + [N]},
        {"event": "advance", "advanced": False, "current_index": 7, "statuses": [C] * 7 + [N]},
        {"event": "back", "current_index": 6, "statuses": [C] * 7 + [N]},
        {"event": "reopen", "current_index": 0, "statuses": [N] * 8},
        {"event": "reopen", "current_index": 0, "statuses": [N] * 8},
    ]


def test_flow_events_reopen_on_a_state_that_keeps_every_prerequisite():
    events = flow.flow_events(facts(), facts())
    assert [e["event"] for e in events] == ["jump"] + ["advance"] * 8 + ["back", "reopen", "reopen"]
    assert [e.get("advanced") for e in events[1:9]] == [True] * 7 + [False]
    assert events[-1] == {"event": "reopen", "current_index": 6, "statuses": [C] * 7 + [N]}


def test_flow_events_refuses_a_malformed_reopen_intake():
    with pytest.raises(flow.FlowInputError):
        flow.flow_events(facts(ZSPLIT), [])


# ---------------------------------------------------------------- intake --

def _without(key):
    raw = facts()
    del raw[key]
    return raw


@pytest.mark.parametrize("raw", [
    [],
    _without("homeruns"),
    _without("flow"),
    _without("host_use_l2_collectors"),
    facts(extra=1),
    facts(flow="ground"),
    facts(panel_groups=True),
    facts(panel_groups=-1),
    facts(panel_groups="3"),
    facts(combiners=2 ** 31),
    facts(panels_in_sequence=2 ** 31),
    facts(panels_in_sequence=1.0),
    facts(use_l2_collectors=1),
    facts(host_use_l2_collectors=1),
    facts(global_string_sizing_confirmed=None),
    facts(missing_zone_panels={"electrical": 0}),
    facts(missing_zone_panels=missing(-1)),
    facts(missing_zone_panels=[0, 0]),
    facts(strings={"polylines": 0, "paths": 0}),
    facts(strings=dict(strings(), persisted=True)),
    facts(strings=dict(strings(), extra=0)),
    facts(electrical_zones={}),
    facts(electrical_zones=[None]),
    facts(electrical_zones=[dict(ez(), color=3)]),
    facts(electrical_zones=[{"name": "A", "panels": []}]),
    facts(elevation_zones=[{"name": "A", "panels": [], "panels_in_sequence": 1}]),
    facts(electrical_zones=[ez(name=7)]),
    facts(electrical_zones=[ez(name="x" * 1025)]),
    facts(electrical_zones=[ez(panels=("1A", 2))]),
    facts(electrical_zones=[dict(ez(), panels="1A")]),
    facts(electrical_zones=[dict(ez(), panels=None)]),
    facts(electrical_zones=[ez(panels=("x" * 1025,))]),
    facts(electrical_zones=[ez(pis="12")]),
    facts(electrical_zones=[ez()] * (flow.MAX_ZONES + 1)),
])
def test_validate_facts_refuses_malformed_intake(raw):
    with pytest.raises(flow.FlowInputError):
        flow.validate_facts(raw)


def test_validate_facts_bounds_the_panel_references(monkeypatch):
    monkeypatch.setattr(flow, "MAX_ZONE_PANELS", 3)
    flow.validate_facts(facts(electrical_zones=[ez(panels=("1", "2"))], elevation_zones=[lz(panels=("3",))]))
    with pytest.raises(flow.FlowInputError):
        flow.validate_facts(facts(electrical_zones=[ez(panels=("1", "2"))], elevation_zones=[lz(panels=("3", "4"))]))


def test_validate_facts_copies_the_intake():
    raw = facts()
    got = flow.validate_facts(raw)
    raw["electrical_zones"][0]["panels"].append("FFF")
    raw["strings"]["polylines"] = 0
    raw["panel_groups"] = 0
    assert got["electrical_zones"][0]["panels"] == ["1A2", "1A3"]
    assert got["strings"]["polylines"] == 5
    assert got["panel_groups"] == 3
