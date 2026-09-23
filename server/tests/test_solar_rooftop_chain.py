"""Studio's rooftop-chain engines against the plugin source they port (contract G27).

Covered: the flip (panel order, the markers trading positions and the record's ends trading),
the swap (the whole label record, the label text and the circuit, the same-string refusal),
FrameGroupOps (find, delete, every rename outcome), the frame-group create, list and select
commands, the export-settings prompt (defaults, the G22 answers, orientation truncation, the
negative-value rule), the "0.00" coordinate format, the StringData.json text byte for byte
(group order, empty groups, a string across two groups, a missing marker, outline containment),
the G29 outline assignment (the plugin's quadrant-angle PointIsInside on edges, diagonals, notches
and multiple windings, first outline in group order, a group's several outlines),
the rebuild's nearest-panel search and count, the setting canonicalization, and the bounds.
Every input is authored in this file.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


chain = _load("solar_rooftop_chain", ROOT / "server" / "solar_rooftop_chain.py")


def string(handle, panels, label=None, **extra):
    return dict({"handle": handle, "panels": list(panels), "label": dict(label or {"inverter": 1})}, **extra)


def fg(name, handles, ticks=5, color=0):
    return {"Name": name, "FrameHandles": list(handles), "ColorIndex": color, "LastModifiedTicks": ticks}


# ------------------------------------------------------------------ handles --

@pytest.mark.parametrize("raw, neutral", [("a67a", "A67A"), ("00A1", "A1"), ("0", "0"), ("000", "0")])
def test_neutral_handle(raw, neutral):
    assert chain.neutral_handle(raw) == neutral


@pytest.mark.parametrize("raw", ["", "G1", "A 1", None, 12, "1" * 17])
def test_neutral_handle_refuses(raw):
    with pytest.raises(chain.RooftopInputError):
        chain.neutral_handle(raw)


# --------------------------------------------------------------------- flip --

def test_flip_reverses_panels_and_trades_the_ends():
    s = string("a67a", ["1", "2", "3"], start={"handle": "b1", "at": [0, 0]},
               end={"handle": "b2", "at": [10, 0]})
    out = chain.string_flip(s)
    assert out["panels"] == ["3", "2", "1"]
    # BranchCmd.cs:18726-18727 then :18745-18747: the old end marker is the record's start end,
    # standing at the old start position; the old start marker stands at the old end.
    assert out["start"] == {"handle": "B2", "at": [0.0, 0.0]}
    assert out["end"] == {"handle": "B1", "at": [10.0, 0.0]}
    assert out["label"] == s["label"]
    assert s["panels"] == ["1", "2", "3"]   # the input is not mutated


def test_flip_twice_is_the_identity():
    s = string("A1", ["1", "2", "3", "4"], start={"handle": "B1", "at": [0, 0]},
               end={"handle": "B2", "at": [3, 1]})
    assert chain.string_flip(chain.string_flip(s)) == chain.validate_string(s)


def test_flip_without_markers_reverses_panels_only():
    assert chain.string_flip(string("A1", ["5", "6"]))["panels"] == ["6", "5"]


def test_flip_refuses_one_marker():
    with pytest.raises(chain.RooftopInputError):
        chain.string_flip(string("A1", ["5"], start={"handle": "B1", "at": [0, 0]}))


# --------------------------------------------------------------------- swap --

def test_swap_exchanges_the_whole_label_record_and_circuit():
    a = string("A912", ["1", "2"], {"mppt_count": 2, "strings_on_inverter": 3}, circuit="1/1a", label_text="1/1a")
    b = string("A8FE", ["3"], {"mppt_count": 2, "strings_on_inverter": 7}, circuit="2/1b", label_text="2/1b")
    a2, b2 = chain.string_swap(a, b)
    assert a2["label"] == {"mppt_count": 2, "strings_on_inverter": 7}
    assert b2["label"] == {"mppt_count": 2, "strings_on_inverter": 3}
    assert (a2["circuit"], b2["circuit"]) == ("2/1b", "1/1a")
    assert (a2["label_text"], b2["label_text"]) == ("2/1b", "1/1a")
    assert a2["panels"] == ["1", "2"] and b2["panels"] == ["3"]


def test_swap_refuses_the_same_string_twice():
    with pytest.raises(chain.RooftopInputError, match="same string"):
        chain.string_swap(string("A1", ["1"]), string("a1", ["1"]))


def test_swap_refuses_a_half_recorded_circuit():
    with pytest.raises(chain.RooftopInputError):
        chain.string_swap(string("A1", ["1"], circuit="x"), string("A2", ["2"]))


@pytest.mark.parametrize("label", [{"id": 1}, {"kind": 1}, {"Bad": 1}, {"x": 1.5}, {"x": True}, [1]])
def test_label_refuses_non_neutral_records(label):
    with pytest.raises(chain.RooftopInputError):
        chain.validate_label(label)


# -------------------------------------------------------------- frame groups --

def test_find_by_name_is_case_insensitive_and_skips_nulls():
    groups = [None, fg("Alpha", ["A"]), fg("alpha", ["B"])]
    assert chain.find_by_name(groups, "ALPHA") == 1
    assert chain.find_by_name(groups, "  ") is None
    assert chain.exists_by_name(groups, "beta") is False


def test_delete_removes_the_first_match_only():
    groups, removed = chain.delete_by_name([fg("X", ["1"]), fg("x", ["2"])], "x")
    assert removed["FrameHandles"] == ["1"]
    assert [g["Name"] for g in groups] == ["x"]


def test_delete_not_found_and_blank_leave_the_list():
    groups = [fg("X", ["1"])]
    assert chain.delete_by_name(groups, "Y") == (chain.validate_frame_groups(groups), None)
    assert chain.delete_by_name(groups, " ")[1] is None


@pytest.mark.parametrize("old, new, outcome", [
    ("FGA", "FGB", chain.RENAMED),
    ("FGA", " ", chain.INVALID_NEW_NAME),
    ("", "FGB", chain.NOT_FOUND),
    ("FGA", "fga", chain.NO_OP_SAME_NAME),
    ("ZZZ", "zzz", chain.NOT_FOUND),
    ("FGA", "other", chain.NEW_NAME_ALREADY_EXISTS),
    ("missing", "FGC", chain.NOT_FOUND),
])
def test_rename_outcomes(old, new, outcome):
    groups = [fg("FGA", ["A646"]), fg("Other", ["A1"])]
    result, after = chain.rename_by_name(groups, old, new, 99)
    assert result == outcome
    if outcome == chain.RENAMED:
        assert after[0]["Name"] == "FGB" and after[0]["LastModifiedTicks"] == 99
    else:
        assert after == chain.validate_frame_groups(groups)


def test_rename_trims_the_new_name():
    _, after = chain.rename_by_name([fg("A", ["1"])], "A", "  B  ", 1)
    assert after[0]["Name"] == "B"


def test_create_appends_the_picked_panel_groups_in_pick_order():
    outcome, groups = chain.frame_group_create([], " FGA ", ["a646", "A63B", "A631", "A646", "FFFF"],
                                               ["A631", "A63B", "A646"], 7)
    assert outcome == "created"
    assert groups == [{"Name": "FGA", "FrameHandles": ["A646", "A63B", "A631"], "ColorIndex": 0,
                       "LastModifiedTicks": 7}]


@pytest.mark.parametrize("name, picked, outcome", [
    ("fga", ["A1"], "duplicate"), ("  ", ["A1"], "cancelled"), ("New", ["FFFF"], "cancelled"), ("New", [], "cancelled")])
def test_create_refusals_change_nothing(name, picked, outcome):
    groups = [fg("FGA", ["A1"])]
    result, after = chain.frame_group_create(groups, name, picked, ["A1"], 7)
    assert result == outcome and after == chain.validate_frame_groups(groups)


def test_list_reports_count_and_frames_in_listed_order():
    assert chain.frame_group_list([fg("B", ["1", "2", "3"]), None, fg("A", [])]) == {
        "count": 3, "groups": [{"name": "B", "frames": 3}, {"name": "(unnamed)", "frames": 0},
                               {"name": "A", "frames": 0}]}
    assert chain.frame_group_list(None) == {"count": 0, "groups": []}


def test_select_resolves_and_skips_stale_handles():
    groups = [fg("FGB", ["A646", "A63B", "DEAD", "zz"])]
    out = chain.frame_group_select(groups, " fgb ", ["A63B", "A646"])
    assert out == {"status": "selected", "handles": ["A646", "A63B"], "stale": 2}


@pytest.mark.parametrize("groups, name, status", [
    ([], "FGB", "no-groups"), ([fg("FGB", ["1"])], " ", "cancelled"), ([fg("FGB", ["1"])], "X", "missing"),
    ([fg("FGB", [])], "FGB", "empty"), ([fg("FGB", ["DEAD"])], "FGB", "stale")])
def test_select_outcomes(groups, name, status):
    out = chain.frame_group_select(groups, name, ["1"])
    assert out["status"] == status and out["handles"] == []


def test_frame_group_setting_shape_is_refused_when_malformed():
    with pytest.raises(chain.RooftopInputError):
        chain.validate_frame_groups([{"Name": "A"}])
    with pytest.raises(chain.RooftopInputError):
        chain.validate_frame_groups([{"Name": "A", "FrameHandles": [], "Extra": 1}])


def test_frame_groups_without_clock_zeroes_ticks_only():
    out = chain.frame_groups_without_clock([fg("A", ["1"], ticks=638_000_000_000_000_000, color=3)])
    assert out == [{"Name": "A", "FrameHandles": ["1"], "ColorIndex": 3, "LastModifiedTicks": 0}]
    assert chain.canonical_setting_text(out) == \
        '[{"ColorIndex":3,"FrameHandles":["1"],"LastModifiedTicks":0,"Name":"A"}]'


def test_dotnet_ticks_at_the_epoch():
    assert chain.dotnet_utc_ticks(0) == 621_355_968_000_000_000
    assert chain.dotnet_utc_ticks(1.5) == 621_355_968_015_000_000


# ------------------------------------------------------------ export settings --

def test_export_settings_absent_reads_the_declared_defaults():
    assert chain.load_export_settings(None) == {
        "module_width": 0.992, "module_height": 1.640, "module_x_spacing": 0.02, "module_y_spacing": 0.02,
        "orientation": 0, "tilt": 15.0, "azimuth": 0.0, "maintenance_margin": 2.0,
        "manufacturer": "generic", "product": "generic"}


def test_export_settings_take_the_g22_answers():
    out = chain.export_settings_prompt(None, ["1.134", "2.278", "0.025", "0.03", "1", "12.5", "185", "0.6",
                                              "ACME", "P440"])
    assert out == {"module_width": 1.134, "module_height": 2.278, "module_x_spacing": 0.025,
                   "module_y_spacing": 0.03, "orientation": 1, "tilt": 12.5, "azimuth": 185.0,
                   "maintenance_margin": 0.6, "manufacturer": "ACME", "product": "P440"}


def test_export_settings_empty_answers_keep_the_current_values():
    current = chain.export_settings_prompt(None, ["1", "2", "0", "0", "1", "-5", "-90", "0", "M", "P"])
    assert chain.export_settings_prompt(current, [""] * 10) == current
    assert current["tilt"] == -5.0 and current["azimuth"] == -90.0


@pytest.mark.parametrize("answer, orientation", [("1", 1), ("1.9", 1), ("2", 0), ("0", 0), ("0.99", 0)])
def test_export_settings_orientation_truncates_then_only_one_is_portrait(answer, orientation):
    answers = [""] * 10
    answers[4] = answer
    assert chain.export_settings_prompt(None, answers)["orientation"] == orientation


@pytest.mark.parametrize("index, answer", [(0, "-1"), (3, "-0.1"), (7, "-2"), (1, "abc"), (5, "nan")])
def test_export_settings_refuse_what_the_prompt_rejects(index, answer):
    answers = [""] * 10
    answers[index] = answer
    with pytest.raises(chain.RooftopInputError):
        chain.export_settings_prompt(None, answers)


def test_export_settings_take_exactly_ten_answers():
    with pytest.raises(chain.RooftopInputError):
        chain.export_settings_prompt(None, ["1"] * 9)


# -------------------------------------------------------------- string data --

@pytest.mark.parametrize("value, text", [
    (15993.47, "15993.47"), (1.005, "1.01"), (2.675, "2.68"), (0.125, "0.13"), (-0.001, "0.00"),
    (-1.235, "-1.24"), (3, "3.00"), (1e11 + 0.004, "100000000000.00")])
def test_coordinate_format(value, text):
    assert chain.format_coordinate(value) == text


def test_coordinate_format_is_bounded():
    with pytest.raises(chain.RooftopBoundsError):
        chain.format_coordinate(1e13)


EXPECTED_STRING_DATA = "\r\n".join([
    "{",
    '  "groups": [',
    "    {",
    '      "handle": "B10",',
    '      "name": "Group 10",',
    '      "strings": []',
    "    },",
    "    {",
    '      "handle": "B2",',
    '      "name": "Group 2",',
    '      "strings": [',
    "        {",
    '          "handle": "A1",',
    '          "startPoint": {',
    '            "handle": "A2",',
    '            "coordinate": "1.01,2.00"',
    "          },",
    '          "endPoint": {',
    '            "handle": "A3",',
    '            "coordinate": "3.00,4.46"',
    "          }",
    "        }",
    "      ]",
    "    },",
    "    {",
    '      "handle": "B3",',
    '      "name": "group 3",',
    '      "strings": []',
    "    }",
    "  ]",
    "}"])


def _groups():
    return [{"handle": "B2", "name": "Group 2", "panels": ["10", "11"]},
            {"handle": "B10", "name": "Group 10", "panels": ["12", "13"]},
            {"handle": "B3", "name": "group 3", "panels": []}]


def test_string_data_text_byte_for_byte():
    s1 = string("A1", ["10", "11"], start={"handle": "A2", "at": [1.005, 2]},
                end={"handle": "A3", "at": [3.0, 4.456]})
    s2 = string("A4", ["11", "12"], start={"handle": "A5", "at": [0, 0]}, end={"handle": "A6", "at": [1, 1]})
    assert chain.string_data(_groups(), [s1, s2]) == EXPECTED_STRING_DATA


def test_string_data_keeps_selection_order_within_a_group():
    s1 = string("A9", ["10"], start={"handle": "C1", "at": [0, 0]}, end={"handle": "C2", "at": [0, 0]})
    s2 = string("A1", ["11"], start={"handle": "C3", "at": [0, 0]}, end={"handle": "C4", "at": [0, 0]})
    data = json.loads(chain.string_data(_groups(), [s1, s2]))
    assert [s["handle"] for s in data["groups"][1]["strings"]] == ["A9", "A1"]


def test_string_data_writes_nothing_when_a_marker_is_missing():
    assert chain.string_data(_groups(), [string("A1", ["10"])]) is None
    assert chain.string_data(_groups(), []) is None
    assert chain.string_data([], [string("A1", ["10"], start={"handle": "C1", "at": [0, 0]},
                                         end={"handle": "C2", "at": [0, 0]})]) is None


def test_string_data_locates_ends_by_outline_when_groups_carry_them():
    groups = [{"handle": "D1", "name": "Left", "outlines": [[[0, 0], [10, 0], [10, 10], [0, 10]]]},
              {"handle": "D2", "name": "Right", "outlines": [[[20, 0], [30, 0], [30, 10], [20, 10]]]}]
    inside = string("A1", ["99"], start={"handle": "C1", "at": [21, 1]}, end={"handle": "C2", "at": [29, 9]})
    across = string("A2", ["98"], start={"handle": "C3", "at": [1, 1]}, end={"handle": "C4", "at": [25, 5]})
    data = json.loads(chain.string_data(groups, [inside, across]))
    assert [(g["name"], [s["handle"] for s in g["strings"]]) for g in data["groups"]] == \
        [("Left", []), ("Right", ["A1"])]


def test_string_data_group_names_sort_like_the_culture_compare():
    names = ["b", "Group-B", "GroupA", "a", "B", "group 1"]
    groups = [{"handle": f"{i + 1:X}", "name": n, "panels": []} for i, n in enumerate(names)]
    s = string("A1", ["1"], start={"handle": "C1", "at": [0, 0]}, end={"handle": "C2", "at": [0, 0]})
    data = json.loads(chain.string_data(groups, [s]))
    assert [g["name"] for g in data["groups"]] == ["a", "b", "B", "group 1", "GroupA", "Group-B"]


def test_string_data_lists_groups_without_outlines_with_no_strings():
    # StringHomeRunCmd.cs:186-195, :243-246: a group with no outline polyline holds no end.
    s = string("A1", ["1"], start={"handle": "C1", "at": [0, 0]}, end={"handle": "C2", "at": [0, 0]})
    data = json.loads(chain.string_data([{"handle": "D2", "name": "y"}, {"handle": "D1", "name": "x"}], [s]))
    assert data == {"groups": [{"handle": "D1", "name": "x", "strings": []},
                               {"handle": "D2", "name": "y", "strings": []}]}


SQUARE = [[0, 0], [10, 0], [10, 10], [0, 10]]


def _outline_groups():
    # The _groups() scenario rebuilt on G29 outlines: B2's outline holds both of A1's ends, B10's
    # holds only A4's end, B3 carries no outline polyline.
    return [{"handle": "B2", "name": "Group 2", "outlines": [[[0.5, 1.5], [5, 1.5], [5, 5], [0.5, 5]]]},
            {"handle": "B10", "name": "Group 10", "outlines": [[[0.9, 0.9], [1.1, 0.9], [1.1, 1.1], [0.9, 1.1]]]},
            {"handle": "B3", "name": "group 3"}]


def test_string_data_text_byte_for_byte_from_outlines():
    s1 = string("A1", ["10", "11"], start={"handle": "A2", "at": [1.005, 2]},
                end={"handle": "A3", "at": [3.0, 4.456]})
    s2 = string("A4", ["11", "12"], start={"handle": "A5", "at": [0, 0]}, end={"handle": "A6", "at": [1, 1]})
    assert chain.string_data(_outline_groups(), [s1, s2]) == EXPECTED_STRING_DATA


@pytest.mark.parametrize("point,inside", [
    ((5, 5), True), ((0, 5), True), ((5, 0), True), ((0, 0), True),     # left and bottom edges hold
    ((10, 5), False), ((5, 10), False), ((10, 10), False),               # right and top edges do not
    ((-1, 5), False), ((11, 5), False), ((5, -1), False), ((5, 11), False)])
def test_point_inside_is_the_plugins_quadrant_test_on_the_edges(point, inside):
    # PointInPoly.GetQuadrant (PolylineExtensions.cs:363-368) compares with strict >, so the
    # low edges fall inside and the high edges outside.
    assert chain._point_inside(point[0], point[1], [tuple(p) for p in SQUARE]) is inside


def test_point_inside_diagonal_edge_takes_the_intercept_branch():
    # A +-2 quadrant step runs X_intercept (:375-380, :390-396); a point on the hypotenuse is out.
    tri = [(0.0, 0.0), (10.0, 0.0), (0.0, 10.0)]
    assert chain._point_inside(2, 2, tri) and not chain._point_inside(5, 5, tri)
    assert not chain._point_inside(6, 6, tri)


def test_point_inside_concave_notch_is_outside():
    u_shape = [(0.0, 0.0), (30.0, 0.0), (30.0, 30.0), (20.0, 30.0), (20.0, 10.0), (10.0, 10.0),
               (10.0, 30.0), (0.0, 30.0)]
    assert chain._point_inside(5, 25, u_shape) and chain._point_inside(25, 25, u_shape)
    assert not chain._point_inside(15, 25, u_shape) and chain._point_inside(15, 5, u_shape)


def test_point_inside_needs_a_winding_of_exactly_one():
    # (angle == +4) || (angle == -4) (:447): a doubly or triply wound outline holds nothing, where
    # an even-odd test would call the triple winding inside.
    once = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    assert chain._point_inside(5, 5, once) and chain._point_inside(5, 5, once[::-1])
    assert not chain._point_inside(5, 5, once * 2)
    assert not chain._point_inside(5, 5, once * 3)


def test_point_inside_empty_and_degenerate_outlines_hold_nothing():
    assert not chain._point_inside(0, 0, [])
    assert not chain._point_inside(0, 0, [(0.0, 0.0)])
    assert not chain._point_inside(1, 0, [(0.0, 0.0), (2.0, 0.0)])


def test_outline_box_only_skips_what_the_quadrant_test_rejects():
    import random
    rng = random.Random(29)
    for _ in range(300):
        poly = [(rng.uniform(-5, 5), rng.uniform(-5, 5)) for _ in range(rng.randint(1, 9))]
        x0, y0, x1, y1 = chain._outline_box(poly)
        for _ in range(20):
            x, y = rng.uniform(-8, 8), rng.uniform(-8, 8)
            if not (x0 <= x <= x1 and y0 <= y <= y1):
                assert not chain._point_inside(x, y, poly)
        for x, y in poly:   # vertices sit on the box, never pruned
            assert x0 <= x <= x1 and y0 <= y <= y1


def test_string_data_first_outline_in_group_order_wins():
    # GetStringPanelGroupData breaks on the first dictionary outline that holds the end (:327-331);
    # the dictionary is filled group by group in drawing order (:186-195), not by name.
    groups = [{"handle": "E1", "name": "Zed", "outlines": [SQUARE]},
              {"handle": "E2", "name": "Alpha", "outlines": [SQUARE]}]
    s = string("A1", ["1"], start={"handle": "C1", "at": [1, 1]}, end={"handle": "C2", "at": [9, 9]})
    data = json.loads(chain.string_data(groups, [s]))
    assert [(g["name"], [x["handle"] for x in g["strings"]]) for g in data["groups"]] == \
        [("Alpha", []), ("Zed", ["A1"])]


def test_string_data_two_outlines_of_one_group_are_one_group():
    # Both ends resolve to the same blockObjectId (:220-222) though they lie in different outlines.
    groups = [{"handle": "F1", "name": "Split", "outlines": [SQUARE, [[20, 0], [30, 0], [30, 10], [20, 10]]]},
              {"handle": "F2", "name": "Other"}]
    s = string("A1", ["1"], start={"handle": "C1", "at": [5, 5]}, end={"handle": "C2", "at": [25, 5]})
    miss = string("A2", ["2"], start={"handle": "C3", "at": [5, 5]}, end={"handle": "C4", "at": [15, 5]})
    data = json.loads(chain.string_data(groups, [s, miss]))
    assert data["groups"] == [
        {"handle": "F2", "name": "Other", "strings": []},
        {"handle": "F1", "name": "Split", "strings": [
            {"handle": "A1", "startPoint": {"handle": "C1", "coordinate": "5.00,5.00"},
             "endPoint": {"handle": "C2", "coordinate": "25.00,5.00"}}]}]


def test_string_data_outlines_ignore_panel_membership():
    # With outlines the plugin never looks at panels: a string whose panels are the group's but
    # whose markers sit outside every outline joins no group.
    groups = [{"handle": "1A1", "name": "G", "panels": ["1", "2"], "outlines": [SQUARE]}]
    s = string("A1", ["1", "2"], start={"handle": "C1", "at": [50, 50]}, end={"handle": "C2", "at": [60, 60]})
    data = json.loads(chain.string_data(groups, [s]))
    assert data == {"groups": [{"handle": "1A1", "name": "G", "strings": []}]}


def test_panel_group_outlines_accept_any_polyline_and_are_bounded(monkeypatch):
    # PanelGroupData.cs:73-82 keeps every polyline of the block, whatever its vertex count.
    (g,) = chain.validate_panel_groups([{"handle": "A1", "name": "n", "outlines": [[], [[0, 0], [1, 1]]]}])
    assert g["outlines"] == [[], [(0.0, 0.0), (1.0, 1.0)]]
    monkeypatch.setattr(chain, "MAX_OUTLINE_VERTICES_TOTAL", 5)
    with pytest.raises(chain.RooftopBoundsError):
        chain.validate_panel_groups([{"handle": "A1", "name": "n", "outlines": [SQUARE]},
                                     {"handle": "A2", "name": "m", "outlines": [SQUARE]}])
    with pytest.raises(chain.RooftopInputError):
        chain.validate_panel_groups([{"handle": "A1", "name": "n", "outlines": [[[0, "x"]]]}])


def test_marker_takes_handle_and_at_only():
    with pytest.raises(chain.RooftopInputError):
        chain.validate_string(string("A1", ["1"], start={"handle": "C1", "point": [0, 0]},
                                     end={"handle": "C2", "point": [0, 0]}))


# ------------------------------------------------------ the committed intake --

INTAKE_PATH = ROOT / "docs" / "parity" / "evidence" / "rooftop" / "chain" / "intake.json"
LABEL_FIELDS = {"color_counter", "inverter_number", "num_mppt", "string_number", "strings_on_inverter",
                "strings_per_mppt", "terminal_number"}


@pytest.fixture(scope="module")
def intake():
    return json.loads(INTAKE_PATH.read_text(encoding="utf-8"))


def test_committed_intake_runs_every_engine(intake):
    strings = {s["handle"]: chain.validate_string(s) for s in intake["strings"]}
    assert len(strings) == len(intake["strings"])
    assert all(set(s["label"]) == LABEL_FIELDS for s in strings.values())
    # G29: every group carries its outlines, each a closed ring of [x, y] points.
    for raw in intake["panel_groups"]:
        assert set(raw) == {"handle", "name", "outlines"} and raw["outlines"], raw["handle"]
        assert all(len(o) >= 3 and all(len(p) == 2 for p in o) for o in raw["outlines"]), raw["handle"]
    groups = chain.validate_panel_groups(intake["panel_groups"])
    assert all(set(g) == {"handle", "name", "outlines"} and g["outlines"] for g in groups)

    # c1: the flip reverses A67A's panels and trades its markers' positions.
    before = strings["A67A"]
    flipped = chain.string_flip(before)
    assert flipped["panels"] == before["panels"][::-1]
    assert flipped["start"] == {"handle": before["end"]["handle"], "at": before["start"]["at"]}
    assert flipped["end"] == {"handle": before["start"]["handle"], "at": before["end"]["at"]}

    # c2: the swap trades the two label records whole.
    a, b = chain.string_swap(strings["A912"], strings["A902"])
    assert (a["label"], b["label"]) == (strings["A902"]["label"], strings["A912"]["label"])

    # c3 to c7 over the committed FrameGroups setting.
    settings = intake["settings"]
    group_handles = [g["handle"] for g in groups]
    outcome, fgs = chain.frame_group_create(settings["FrameGroups"], "FGA", ["A646", "A63B", "A631"],
                                            group_handles, 1)
    assert outcome == "created" and fgs[-1]["FrameHandles"] == ["A646", "A63B", "A631"]
    outcome, fgs = chain.rename_by_name(fgs, "FGA", "FGB", 2)
    assert outcome == chain.RENAMED
    assert chain.frame_group_list(fgs) == {"count": 1, "groups": [{"name": "FGB", "frames": 3}]}
    selected = chain.frame_group_select(fgs, "FGB", group_handles)
    assert selected["status"] == "selected" and sorted(selected["handles"]) == ["A631", "A63B", "A646"]
    fgs, removed = chain.delete_by_name(fgs, "FGB")
    assert removed is not None and fgs == []

    # c9: the stored record is absent (null), so the prompts start from the declared defaults.
    assert chain.load_export_settings(settings["export-settings"]) == chain.EXPORT_SETTINGS_DEFAULTS
    saved = chain.export_settings_prompt(settings["export-settings"],
                                         ["1.134", "2.278", "0.025", "0.03", "1", "12.5", "185", "0.6", "ACME", "P440"])
    assert saved["orientation"] == 1 and saved["product"] == "P440"

    # c10: every group is listed, sorted by name; both strings' ends lie in Group 11's outlines.
    text = chain.string_data(intake["panel_groups"], [strings["A912"], strings["A90E"]])
    data = json.loads(text)
    assert [g["name"] for g in data["groups"]][:4] == ["Group 1", "Group 10", "Group 11", "Group 2"]
    assert len(data["groups"]) == len(groups) and "\r\n" in text
    assert [(g["handle"], [s["handle"] for s in g["strings"]]) for g in data["groups"] if g["strings"]] == \
        [("A646", ["A912", "A90E"])]
    assert data["groups"][2]["strings"] == [
        {"handle": "A912", "startPoint": {"handle": "A913", "coordinate": "15993.47,3179.76"},
         "endPoint": {"handle": "A914", "coordinate": "16920.47,3179.76"}},
        {"handle": "A90E", "startPoint": {"handle": "A90F", "coordinate": "15993.47,3237.56"},
         "endPoint": {"handle": "A910", "coordinate": "16920.47,3237.56"}}]

    # c11: every string counts once and, with no vertices, keeps its association.
    rebuilt, count = chain.string_rebuild(list(strings.values()))
    assert count == len(strings) and [s["panels"] for s in rebuilt] == [s["panels"] for s in strings.values()]


# ------------------------------------------------------------------ rebuild --

PANELS = [("10", [0, 0]), ("11", [5, 0]), ("12", [5.9, 0])]


def test_rebuild_takes_the_nearest_panel_inside_the_tolerance():
    s = string("A1", ["99"], vertices=[[0.2, 0], [5.5, 0], [20, 0]])
    rebuilt, count = chain.string_rebuild([s], PANELS)
    assert rebuilt[0]["panels"] == ["10", "12"] and count == 1


def test_rebuild_tolerance_is_strict():
    rebuilt, _ = chain.string_rebuild([string("A1", ["99"], vertices=[[1.0, 0]])], [("10", [0, 0])])
    assert rebuilt[0]["panels"] == []


def test_rebuild_tie_keeps_the_first_found():
    rebuilt, _ = chain.string_rebuild([string("A1", [], vertices=[[0.5, 0.5]])],
                                      [("20", [0.5, 0.0]), ("21", [0.5, 1.0])])
    assert rebuilt[0]["panels"] == ["20"]


def test_rebuild_without_vertices_keeps_the_association_and_counts_every_string():
    strings = [string("A1", ["1", "2"]), string("A2", ["3"])]
    rebuilt, count = chain.string_rebuild(strings)
    assert [s["panels"] for s in rebuilt] == [["1", "2"], ["3"]] and count == 2


def test_rebuild_over_vertices_needs_panel_centres():
    with pytest.raises(chain.RooftopInputError):
        chain.string_rebuild([string("A1", [], vertices=[[0, 0]])])


def test_panel_index_matches_a_full_scan():
    points = [(f"{i + 1:X}", [(i * 7919) % 97 / 3.0, (i * 104729) % 89 / 3.0]) for i in range(300)]
    where = {h: xy for h, xy in points}
    index = chain.build_panel_index(points)
    found = 0
    for k in range(200):
        p = [(k * 31) % 97 / 3.0 + 0.1, (k * 17) % 89 / 3.0 - 0.2]
        best_d = min((((x - p[0]) ** 2 + (y - p[1]) ** 2) ** 0.5 for _, (x, y) in points), default=None)
        got = chain.find_panel_near_point(p, index)
        if best_d is None or best_d >= 1.0:
            assert got is None
        else:
            gx, gy = where[got]
            assert ((gx - p[0]) ** 2 + (gy - p[1]) ** 2) ** 0.5 == pytest.approx(best_d, abs=1e-12)
            found += 1
    assert found > 0


# ------------------------------------------------------------------- bounds --

def test_strings_past_the_bound_are_refused():
    with pytest.raises(chain.RooftopBoundsError):
        chain.string_rebuild([string("A1", ["1"])] * (chain.MAX_STRINGS + 1))


def test_a_string_with_too_many_panels_is_refused():
    with pytest.raises(chain.RooftopBoundsError):
        chain.validate_string(string("A1", ["1"] * (chain.MAX_PANELS_PER_STRING + 1)))


def test_unknown_string_keys_are_refused():
    with pytest.raises(chain.RooftopInputError):
        chain.validate_string(dict(string("A1", ["1"]), colour=3))
