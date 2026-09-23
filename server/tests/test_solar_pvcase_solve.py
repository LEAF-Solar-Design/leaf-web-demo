"""Studio's PVcase solve against the plugin source it ports (contract G33).

Covered: DefaultPanelsPerString and the StringLength fallback, the solver's (row, column) stable
ordering, the chunking with a short last string, string numbers counted per L2 ACROSS groups, the
zero L2 case (every string on L2 1), the nearest-L2 pick by centroid with its first-wins tie and
its number > 0 filter, the input builder (Code 0 and null cells, empty rows, the Sequences
derivation, the zero-panels rejection with its reason), the ignore-case last-wins handle map, the
write-back (id-keyed, Code not consulted, only inverter_id and string_input_number change), the
command's three outcomes and message, the committed rooftop intake's printed totals, and the
intake's fail-closed validation. Every other input is authored in this file.
"""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMMITTED_INTAKE = ROOT / "docs" / "parity" / "evidence" / "rooftop" / "pvcase" / "intake.json"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pv = _load("solar_pvcase_solve", ROOT / "server" / "solar_pvcase_solve.py")


def cell(ident, code=1, x=0.0, y=0.0, seq=0, inv=-1, sin=0, **extra):
    return dict({"code": code, "id": ident, "seq": seq, "inverter_id": inv, "string_input_number": sin,
                 "x": x, "y": y}, **extra)


def group(handle, rows, sequences=(), installation="Roof", row_angle_rad=0.0):
    return {"handle": handle, "installation": installation,
            "panel_size": {"height_across_row": 38.5, "width_along_row": 77.0},
            "row_angle_rad": row_angle_rad, "sequences": list(sequences), "rows": rows}


def intake(groups, per_string=3):
    return {"units": "in", "panels_per_string": per_string, "panel_groups": groups}


def panel(handle, row, col, x=0.0, y=0.0):
    return pv.PanelInput(handle, x, y, 77.0, 38.5, 0, 0, row, col)


def assignments(outcome, handle):
    (g,) = [g for g in outcome["panel_groups"] if g["handle"] == handle]
    return pv.panel_assignments(g)


# ------------------------------------------------------------ panels per string --

def test_default_panels_per_string_is_the_core_constant():
    assert pv.DEFAULT_PANELS_PER_STRING == 12


@pytest.mark.parametrize("setting,expected", [(0, 12), (-4, 12), (1, 1), (28, 28)])
def test_string_length_setting_below_one_falls_back_to_the_default(setting, expected):
    assert pv.effective_panels_per_string(setting) == expected


def test_zero_string_length_setting_solves_in_twelves():
    rows = [[cell(f"{i:X}") for i in range(1, 26)]]
    out = pv.pvcase_solve(intake([group("A1", rows)], per_string=0))
    got = assignments(out, "A1")
    assert [s for _, _, s in got] == [1] * 12 + [2] * 12 + [3]
    assert out["strings_created"] == 3


def test_solver_clamps_a_direct_length_below_one_to_one():
    grp = {"group_id": "g", "panels": [panel("1", 0, 0), panel("2", 0, 1)]}
    result = pv.solve([grp], [], 0)
    assert [p.string_number for p in grp["panels"]] == [1, 2]
    assert result["strings_created"] == 2


# ---------------------------------------------------------------------- solver --

def test_solver_orders_by_row_then_column_stably():
    a, b, c, d = panel("a", 1, 0), panel("b", 0, 1), panel("c", 0, 0), panel("d", 0, 1)
    grp = {"group_id": "g", "panels": [a, b, c, d]}
    pv.solve([grp], [], 2)
    # ordered c(0,0) b(0,1) d(0,1) a(1,0): b before d because the sort is stable
    assert [(p.handle, p.string_number) for p in (c, b, d, a)] == [("c", 1), ("b", 1), ("d", 2), ("a", 2)]


def test_solver_short_last_string_and_counts():
    grp = {"group_id": "g", "panels": [panel(str(i), 0, i) for i in range(7)]}
    result = pv.solve([grp], [], 3)
    assert [p.string_number for p in grp["panels"]] == [1, 1, 1, 2, 2, 2, 3]
    assert result == {"panels_assigned": 7, "strings_created": 3, "strings_per_l2": {1: 3}}


def test_string_numbers_continue_across_groups_on_one_l2():
    g1 = {"group_id": "g1", "panels": [panel(str(i), 0, i) for i in range(4)]}
    g2 = {"group_id": "g2", "panels": [panel("x" + str(i), 0, i) for i in range(2)]}
    pv.solve([g1, g2], [], 3)
    assert [p.string_number for p in g1["panels"] + g2["panels"]] == [1, 1, 1, 2, 3, 3]
    assert {p.l2_number for p in g1["panels"] + g2["panels"]} == {1}


def test_zero_l2_case_puts_every_string_on_l2_one():
    grp = {"group_id": "g", "panels": [panel(str(i), 0, i, x=1000.0 * i) for i in range(4)]}
    result = pv.solve([grp], None, 2)
    assert [p.l2_number for p in grp["panels"]] == [1, 1, 1, 1]
    assert result["strings_per_l2"] == {1: 2}


def test_l2_without_a_positive_number_is_not_a_target():
    grp = {"group_id": "g", "panels": [panel("1", 0, 0, x=5.0)]}
    pv.solve([grp], [{"number": 0, "x": 5.0, "y": 0.0}, {"number": -2, "x": 5.0, "y": 0.0}], 1)
    assert grp["panels"][0].l2_number == 1


def test_nearest_l2_by_string_centroid_numbers_per_l2():
    # strings of 2: centroids (1, 0), (101, 0), (3, 0)
    pts = [(0, 0), (2, 0), (100, 0), (102, 0), (2, 0), (4, 0)]
    grp = {"group_id": "g", "panels": [panel(str(i), 0, i, x=float(x), y=float(y)) for i, (x, y) in enumerate(pts)]}
    l2s = [{"number": 7, "x": 0.0, "y": 0.0}, {"number": 3, "x": 100.0, "y": 0.0}]
    result = pv.solve([grp], l2s, 2)
    assert [(p.l2_number, p.string_number) for p in grp["panels"]] == [(7, 1), (7, 1), (3, 1), (3, 1), (7, 2), (7, 2)]
    assert result["strings_per_l2"] == {7: 2, 3: 1}


def test_nearest_l2_tie_keeps_the_first_in_list_order():
    grp = {"group_id": "g", "panels": [panel("1", 0, 0, x=0.0)]}
    pv.solve([grp], [{"number": 4, "x": -1.0, "y": 0.0}, {"number": 2, "x": 1.0, "y": 0.0}], 1)
    assert grp["panels"][0].l2_number == 4


def test_solver_with_no_groups_assigns_nothing():
    assert pv.solve(None, [], 3) == {"panels_assigned": 0, "strings_created": 0, "strings_per_l2": {}}


# ----------------------------------------------------------------- input builder --

def test_builder_drops_code_zero_and_null_cells_keeping_column_indices():
    state = pv.validate_intake(intake([group("A1", [[cell("1"), None, cell("3", code=0), cell("2")], []])]))
    inputs, reasons = pv.build_panel_group_inputs(state["panel_groups"])
    assert reasons == []
    assert [(p.handle, p.row_index, p.col_index) for p in inputs[0]["panels"]] == [("1", 0, 0), ("2", 0, 3)]


def test_builder_carries_the_group_geometry_the_solver_never_reads():
    state = pv.validate_intake(intake([group("A1", [[cell("1")]], row_angle_rad=0.5)]))
    inputs, _ = pv.build_panel_group_inputs(state["panel_groups"])
    (p,) = inputs[0]["panels"]
    assert (inputs[0]["row_angle_rad"], p.width_along_row, p.height_across_row) == (0.5, 77.0, 38.5)


def test_builder_carries_existing_assignments_and_the_sequences_derivation():
    rows = [[cell("1", seq=1), cell("2", seq=3), cell("3", inv=5, sin=9, seq=1), cell("4", seq=99)]]
    state = pv.validate_intake(intake([group("A1", rows, sequences=[2, 2])]))
    inputs, _ = pv.build_panel_group_inputs(state["panel_groups"])
    assert [(p.l2_number, p.string_number) for p in inputs[0]["panels"]] == [(0, 1), (0, 2), (5, 9), (0, 0)]


def test_builder_rejects_a_group_with_no_surviving_panel_by_name():
    groups = [group("B5", [[cell("9", code=0), None], []]), group("B6", [[cell("3")]]), group("B7", [])]
    inputs, reasons = pv.build_panel_group_inputs(pv.validate_intake(intake(groups))["panel_groups"])
    assert [g["group_id"] for g in inputs] == ["B6"]
    assert reasons == ["[B5] zero panels survived (raw=1, code0=1)", "[B7] zero panels survived (raw=0, code0=0)"]


# ----------------------------------------------------------------- write-back --

def test_handle_map_ignores_case_and_a_later_panel_wins():
    g1 = {"group_id": "g1", "panels": [panel("a1", 0, 0)]}
    g2 = {"group_id": "g2", "panels": [panel("A1", 0, 0), panel("", 0, 1)]}
    g1["panels"][0].l2_number, g1["panels"][0].string_number = 1, 1
    g2["panels"][0].l2_number, g2["panels"][0].string_number = 1, 2
    by_handle = pv.build_handle_assignment_map([g1, g2])
    assert by_handle == {"A1": (1, 2)}


def test_write_back_is_keyed_by_id_ignoring_case_and_code():
    groups = [group("C1", [[cell("aa", code=0), cell("BB"), None, cell("cc")]])]
    groups = pv.validate_intake(intake(groups))["panel_groups"]
    written = pv.apply_assignments_to_matrix(groups, {"AA": (3, 4), "CC": (5, 6)})
    cells = groups[0]["rows"][0]
    assert written == 2
    assert [(c["inverter_id"], c["string_input_number"]) for c in cells if c] == [(3, 4), (-1, 0), (5, 6)]


def test_solve_changes_only_inverter_id_and_string_input_number():
    rows = [[cell("10", x=1.5, y=2.5, seq=4), cell("11", code=2, x=3.0)], [cell("12", code=0)]]
    source = intake([group("D1", rows)], per_string=1)
    frozen = copy.deepcopy(source)
    out = pv.pvcase_solve(source)
    assert source == frozen  # the intake is never mutated
    before = pv.validate_intake(frozen)["panel_groups"][0]
    after = out["panel_groups"][0]
    assert {k: v for k, v in before.items() if k != "rows"} == {k: v for k, v in after.items() if k != "rows"}
    changed = {k for rb, ra in zip(before["rows"], after["rows"]) for cb, ca in zip(rb, ra)
               for k in cb if cb[k] != ca[k]}
    assert changed == {"inverter_id", "string_input_number"}
    assert after["rows"][1][0]["inverter_id"] == -1


# --------------------------------------------------------------------- command --

def test_command_solves_writes_and_reports():
    groups = [group("E2", [[cell("1"), cell("2")], [cell("3")]]), group("E1", [[cell("4"), cell("5")]])]
    out = pv.pvcase_solve(intake(groups, per_string=2))
    assert out["status"] == pv.SOLVED
    assert assignments(out, "E2") == [["1", 1, 1], ["2", 1, 1], ["3", 1, 2]]
    assert assignments(out, "E1") == [["4", 1, 3], ["5", 1, 3]]
    assert out["message"] == ("LEAFPVCASESOLVE: solved 5 panel(s) into 3 string(s) across 0 L2 inverter(s); "
                              "5 panel assignment(s) written back to the drawing.")


def test_no_panel_groups_writes_nothing():
    out = pv.pvcase_solve(intake([]))
    assert out["status"] == pv.NO_PANEL_GROUPS and out["written"] == 0
    assert out["message"] == ("LEAFPVCASESOLVE: no panel groups found in the drawing. "
                              "Create and Solve panel groups first.")


def test_no_usable_panels_writes_nothing():
    source = intake([group("F1", [[cell("1", code=0)]]), group("F2", [])])
    out = pv.pvcase_solve(source)
    assert out["status"] == pv.NO_USABLE_PANELS and out["written"] == 0
    assert out["message"] == "LEAFPVCASESOLVE: no usable panels in the panel groups."
    assert out["rejection_reasons"] == ["[F1] zero panels survived (raw=1, code0=1)",
                                        "[F2] zero panels survived (raw=0, code0=0)"]


def test_panel_assignments_list_panels_in_row_order():
    g = pv.validate_intake(intake([group("9B1", [[cell("2"), None], [], [cell("1"), cell("3", code=0)]])]))
    assert pv.panel_assignments(g["panel_groups"][0]) == [["2", -1, 0], ["1", -1, 0]]
    assert pv.panel_assignments(group("9B2", [])) == []


def test_committed_rooftop_intake_gives_the_printed_totals():
    # The G33 capture: eleven groups at StringLength 28, no L2 registered; the plugin printed
    # "solved 2345 panel(s) into 88 string(s) across 0 L2 inverter(s); 2345 panel assignment(s) ...".
    source = json.loads(COMMITTED_INTAKE.read_text(encoding="utf-8"))
    assert set(source) == {"units", "panels_per_string", "panel_groups"} and source["panels_per_string"] == 28
    out = pv.pvcase_solve(source)
    assert out["status"] == pv.SOLVED and out["rejection_reasons"] == []
    assert (out["panels_assigned"], out["strings_created"], out["written"], out["l2_count"]) == (2345, 88, 2345, 0)
    assert out["message"] == ("LEAFPVCASESOLVE: solved 2345 panel(s) into 88 string(s) across 0 L2 inverter(s); "
                              "2345 panel assignment(s) written back to the drawing.")
    firsts = [assignments(out, g["handle"])[0][1:] for g in out["panel_groups"]]
    lasts = [assignments(out, g["handle"])[-1][1:] for g in out["panel_groups"]]
    assert [s for _, s in firsts] == [1, 8, 13, 18, 23, 31, 35, 43, 47, 51, 69]
    assert [s for _, s in lasts] == [7, 12, 17, 22, 30, 34, 42, 46, 50, 68, 88]
    assert {l2 for g in out["panel_groups"] for _, l2, _ in pv.panel_assignments(g)} == {1}


# --------------------------------------------------------------------- intake --

def _bad(mutate):
    source = intake([group("9D1", [[cell("1")]])])
    pv.validate_intake(source)  # the base intake is well formed: each case fails on its own mutation
    mutate(source)
    return source


@pytest.mark.parametrize("mutate", [
    lambda s: s.pop("units"),
    lambda s: s.update(extra=1),
    lambda s: s.update(l2_inverters=[]),
    lambda s: s.update(units="yd"),
    lambda s: s.update(panels_per_string=True),
    lambda s: s.update(panels_per_string=2.0),
    lambda s: s.update(panels_per_string=2 ** 31),
    lambda s: s.update(panel_groups={}),
    lambda s: s["panel_groups"].append(group("9d1", [])),
    lambda s: s["panel_groups"].append(group("9D2", [[cell("1")]])),
    lambda s: s["panel_groups"][0].update(handle="G-1"),
    lambda s: s["panel_groups"][0].update(name="Group 1"),
    lambda s: s["panel_groups"][0].pop("installation"),
    lambda s: s["panel_groups"][0].update(installation="Carport"),
    lambda s: s["panel_groups"][0].update(matrix={"Rows": []}),
    lambda s: s["panel_groups"][0].update(row_angle_rad=float("nan")),
    lambda s: s["panel_groups"][0]["panel_size"].update(width_along_row=0.0),
    lambda s: s["panel_groups"][0]["panel_size"].update(depth=1.0),
    lambda s: s["panel_groups"][0].update(sequences=[1, "2"]),
    lambda s: s["panel_groups"][0].update(sequences=[600_000]),
    lambda s: s["panel_groups"][0].update(rows={}),
    lambda s: s["panel_groups"][0]["rows"].append(None),
    lambda s: s["panel_groups"][0]["rows"][0].append([1]),
    lambda s: s["panel_groups"][0]["rows"][0].append(cell("2", Angle=0.0)),
    lambda s: s["panel_groups"][0]["rows"][0].append({"id": "2", "code": 1}),
    lambda s: s["panel_groups"][0]["rows"][0].append(cell(5)),
    lambda s: s["panel_groups"][0]["rows"][0].append(cell("")),
    lambda s: s["panel_groups"][0]["rows"][0].append(cell("0001")),
    lambda s: s["panel_groups"][0]["rows"][0].append(cell("2", code=1.0)),
    lambda s: s["panel_groups"][0]["rows"][0].append(cell("2", x=float("inf"))),
])
def test_malformed_intake_is_refused(mutate):
    with pytest.raises(pv.PvcaseInputError):
        pv.pvcase_solve(_bad(mutate))


def test_cell_bound_is_enforced(monkeypatch):
    monkeypatch.setattr(pv, "MAX_CELLS", 3)
    with pytest.raises(pv.PvcaseInputError, match="more than 3 cells"):
        pv.validate_intake(intake([group("9E1", [[cell("1"), cell("2"), cell("3")]])]))
