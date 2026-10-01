"""solar-settings and the drawing's L1/L2 mode (sf-w2-settings-l2-collectors).

The graph contract ties the inverters to settings.use_l2_collectors: in L2 mode every inverter
carries an equipment_type (EQUIPMENT_TYPE_REQUIRED), in L1 mode none does (L2_MODE_REQUIRED). Before
this record solar-settings' own boolean edit of the mode was refused on any drawing with an
inverter. Now it takes the design-preset rule (server/solar_preset_sync.py retype_inverters and
leaves_l2_blocked): a boolean that differs from the drawing's mode retypes every inverter in the same
commit (entering L2 mode, an unconnected string inverter; leaving it, untyped), and leaving L2 mode
with a combiner box, a central inverter or an inverter naming an L2 is refused with
DESIGN_PRESET_L2_EQUIPMENT_PRESENT before anything is written. Every other request, including an
equal mode and a value that is not a boolean, is byte-identical to main.

Covered: the rule is the preset module's; entering and leaving on the populated W1 fixture with the
changed entities, validities and digests measured; the same inverters as a preset Create; two
inverters both ways; every refusal with the input untouched; leaving with string inverters only; a
duplicate inverter number refused by validation; the mode with another setting and with a project
change; every other request against a table digest measured on main; the refusal order; the preset
List reading the mode back; the rail both ways. Inputs are authored here or are committed evidence,
so every count and digest is exact.
"""
import copy
import hashlib
import json

import pytest

import solar_local_graph
import solar_preset_sync
from solar_design_graph import GraphValidationError, entities, validate_graph
from solar_sizing_client import digest
from solar_solve_results import require_current_export
from test_solar_w2_registration import dispatch, head_graph, latest
from test_w1_design_graph import app_id, graph  # noqa: F401
from test_w1_local_graph_adapter import held
from test_w1_solve_commit import seed
import test_solar_ground_route_kinds as route_kinds
import test_solar_ground_topology as topology
import test_solar_tool_design_presets_l2_mode as presets_l2

TOOL = "solar-settings"
PRESENT = "DESIGN_PRESET_L2_EQUIPMENT_PRESENT"
NOT_CURRENT = "SOLAR_OUTPUT_NOT_CURRENT"
VALID = {"state": "valid", "reasons": []}
STALE = {"state": "stale", "reasons": ["settings_changed"]}
INVERTER_ID = "leaf:inverter:00000000-0000-4000-8000-000000000001"
# Measured with python -B (solar_sizing_client.digest, canonical sha256).
ENTER_DIGEST = "24acf04e7aee51dcb3a55d259eb5c0ce52fc2b4286a4e081fcd62960b396ac4f"
LEAVE_DIGEST = "8fee837fbd6d52660bc16832c322879b8ea0063347a0fd6e6609a5087cfa58b7"
TWO_ENTER_DIGEST = "20fd73536c6a2ecaecd4d4ada837473a9118f40b5c7532534df68f0a5fb1fc38"
TWO_LEAVE_DIGEST = "8206b6ddd92a16dd882a7d64b8d7c895d3335d43f78ce6a241ed945b5f8c4648"
STRING_ONLY_LEAVE_DIGEST = "8d1e77aaad8715780663e326a36672e4f03ee7aace931d7dbd75aa834f26980f"
WITH_NUM_MPPT_DIGEST = "cd23c7a38b04e49e337aa2aed7f0a5dbed36fc7bf8e596572436bbc65730cac8"
WITH_PROJECT_DIGEST = "c9c28a4ef2406ab922bb4c7b354849f3e350311b07271ba2a007ec2f3ea6c296"
# The canonical sha256 of the outcome table below, measured on main 86573930 (before this record)
# and unchanged after it: 208 rows, every request that is not a boolean mode flip.
MAIN_TABLE_ROWS = 208
MAIN_TABLE_SHA256 = "a92bf5445191267175eb4427adc6ab0f78040f300649fbb371c4e51e9e4b8967"


def builtin():
    return solar_local_graph._load_builtin(TOOL)


def edit(value, changes, **extra):
    return builtin().run(copy.deepcopy(value), dict({"expected_rev": value["rev"], "changes": changes}, **extra))


def refusal(value, params):
    before = copy.deepcopy(value)
    with pytest.raises(GraphValidationError) as caught:
        builtin().run(value, params)
    assert value == before                                            # nothing written on refusal
    return caught.value.code


def changed_kinds(monkeypatch):
    """Capture the kinds of the entities each commit hands to advance, in order."""
    module = builtin()
    original = module.advance
    seen = []

    def capture(result, changed, tool):
        seen.append([entity["kind"] for entity in changed])
        return original(result, changed, tool)

    monkeypatch.setattr(module, "advance", capture)
    return seen


def export_code(value):
    try:
        require_current_export(value)
    except GraphValidationError as error:
        return error.code
    return "current"


def without(entity, *keys):
    return {key: value for key, value in entity.items() if key not in keys}


def central_only(w1):
    """L1/L2 mode with one central inverter and nothing else."""
    value = topology.bare(w1)
    value["inverters"].append(topology.central(2, 1, []))
    return value


def combiner_only(w1):
    """L1/L2 mode with one combiner box that feeds no L2."""
    value = topology.bare(w1)
    value["inverters"].append(topology.combiner(3, 1, None))
    return value


def composite(w1):
    """The typed topology with a feeder and a trench (test_solar_ground_route_kinds._composite)."""
    return route_kinds.build(w1, "topology", route_kinds._composite)


def string_only_l2(w1):
    """W1 in L1/L2 mode with its inverter an unconnected string inverter (L1 mode can hold it)."""
    value = copy.deepcopy(w1)
    value["settings"]["use_l2_collectors"] = True
    value["inverters"][0].update(equipment_type="string_inverter", l2_ref=None)
    return validate_graph(value)


def l1_empty(w1):
    """L1 mode with no electrical content."""
    value = topology.bare(w1)
    value["settings"]["use_l2_collectors"] = False
    return value


def two_inverters(w1):
    """W1 plus a copy of its inverter as inverter 7, number 7, with no strings."""
    value = copy.deepcopy(w1)
    second = copy.deepcopy(value["inverters"][0])
    second.update(id=app_id("inverter", 7), number=7, input_assignments=[])
    value["inverters"].append(second)
    return validate_graph(value)


# ---------------------------------------------------------------------------- the rule --

def test_settings_l2_mode_rule_is_the_preset_rule():
    module = builtin()
    assert "use_l2_collectors" in module.EDITABLE
    assert module.retype_inverters is solar_preset_sync.retype_inverters
    assert module.leaves_l2_blocked is solar_preset_sync.leaves_l2_blocked
    assert module.L2_EQUIPMENT_PRESENT == solar_preset_sync.L2_EQUIPMENT_PRESENT == PRESENT


# ------------------------------------------------------------ entering and leaving --

def test_settings_l2_mode_enter_types_every_inverter(graph, monkeypatch):
    seen = changed_kinds(monkeypatch)
    before_input = copy.deepcopy(graph)
    assert graph["settings"]["use_l2_collectors"] is False
    assert "equipment_type" not in graph["inverters"][0]
    after = edit(graph, {"use_l2_collectors": True})
    assert graph == before_input
    assert seen == [["settings", "inverter", "route", "schedule"]]
    assert (after["rev"], digest(after)) == (1, ENTER_DIGEST)
    assert after["settings"]["use_l2_collectors"] is True
    assert after["settings"]["global_string_sizing_confirmed"] is False
    before, typed = graph["inverters"][0], after["inverters"][0]
    assert sorted(k for k in set(before) | set(typed) if before.get(k, "-") != typed.get(k, "-")) == \
        ["equipment_type", "l2_ref", "provenance", "rev"]
    assert (typed["equipment_type"], typed["l2_ref"], typed["is_l2"], typed["validity"], typed["rev"]) == \
        ("string_inverter", None, False, VALID, 1)
    assert typed["provenance"]["tool_id"] == TOOL
    assert typed["input_assignments"] == before["input_assignments"]
    assert [(e["kind"], e["validity"]) for e in entities(after) if e["validity"] != VALID] == \
        [("route", STALE), ("schedule", STALE)]
    assert export_code(after) == NOT_CURRENT
    assert validate_graph(copy.deepcopy(after)) == after


def test_settings_l2_mode_leave_restores_the_untyped_inverter(graph, monkeypatch):
    entered = edit(graph, {"use_l2_collectors": True})
    seen = changed_kinds(monkeypatch)
    after = edit(entered, {"use_l2_collectors": False})
    assert seen == [["settings", "inverter"]]                         # route and schedule already stale
    assert (after["rev"], digest(after)) == (2, LEAVE_DIGEST)
    assert after["settings"]["use_l2_collectors"] is False
    before, untyped = graph["inverters"][0], after["inverters"][0]
    assert sorted(k for k in set(before) | set(untyped) if before.get(k, "-") != untyped.get(k, "-")) == \
        ["provenance", "rev"]
    assert export_code(after) == NOT_CURRENT


def test_settings_l2_mode_same_inverters_as_a_preset_create(graph):
    by_settings = edit(graph, {"use_l2_collectors": True})
    by_preset = presets_l2.create(graph, "Alpha", True)
    assert by_preset["settings"]["use_l2_collectors"] is by_settings["settings"]["use_l2_collectors"] is True
    for kind in ("inverters", "routes", "schedules"):
        assert [without(e, "provenance") for e in by_settings[kind]] == \
            [without(e, "provenance") for e in by_preset[kind]]
    assert (by_settings["inverters"][0]["provenance"]["tool_id"],
            by_preset["inverters"][0]["provenance"]["tool_id"]) == (TOOL, "solar-design-presets")


def test_settings_l2_mode_two_inverters_round_trip(graph, monkeypatch):
    value = two_inverters(graph)
    seen = changed_kinds(monkeypatch)
    entered = edit(value, {"use_l2_collectors": True})
    assert seen[-1] == ["settings", "inverter", "inverter", "route", "schedule"]
    assert digest(entered) == TWO_ENTER_DIGEST
    assert [(i["equipment_type"], i["l2_ref"], i["is_l2"]) for i in entered["inverters"]] == \
        [("string_inverter", None, False)] * 2
    after = edit(entered, {"use_l2_collectors": False})
    assert seen[-1] == ["settings", "inverter", "inverter"]
    assert digest(after) == TWO_LEAVE_DIGEST
    assert [without(e, "provenance", "rev") for e in after["inverters"]] == \
        [without(e, "provenance", "rev") for e in value["inverters"]]


# --------------------------------------------------------------------- refusals --

REFUSALS = [
    ("topology", topology.topology_of, {"changes": {"use_l2_collectors": False}}),
    ("central-only", central_only, {"changes": {"use_l2_collectors": False}}),
    ("combiner-only", combiner_only, {"changes": {"use_l2_collectors": False}}),
    ("feeder-and-trench", composite, {"changes": {"use_l2_collectors": False}}),
    ("topology-with-num-mppt", topology.topology_of, {"changes": {"use_l2_collectors": False, "num_mppt": 2}}),
    ("topology-with-project-change", topology.topology_of,
     {"changes": {"use_l2_collectors": False}, "project_changes": {"zip_code": "37601"}}),
]


@pytest.mark.parametrize("case", REFUSALS, ids=[case[0] for case in REFUSALS])
def test_settings_l2_mode_leaving_refused(graph, case):
    _, build, params = case
    value = build(graph)
    assert value["settings"]["use_l2_collectors"] is True
    assert refusal(value, dict(params, expected_rev=value["rev"])) == PRESENT


def test_settings_l2_mode_leave_allowed_with_string_inverters_only(graph, monkeypatch):
    value = string_only_l2(graph)
    seen = changed_kinds(monkeypatch)
    after = edit(value, {"use_l2_collectors": False})
    assert seen == [["settings", "inverter", "route", "schedule"]]
    assert (after["rev"], digest(after)) == (1, STRING_ONLY_LEAVE_DIGEST)
    assert "equipment_type" not in after["inverters"][0] and "l2_ref" not in after["inverters"][0]


def test_settings_l2_mode_duplicate_numbers_refused_by_validation(graph):
    # L1 mode does not check the numbers of untyped inverters; typing them does.
    value = copy.deepcopy(graph)
    second = copy.deepcopy(value["inverters"][0])
    second.update(id=app_id("inverter", 7), input_assignments=[])
    value["inverters"].append(second)
    assert validate_graph(value) == value
    assert refusal(value, {"expected_rev": 0, "changes": {"use_l2_collectors": True}}) == \
        "DUPLICATE_EQUIPMENT_NUMBER"


def test_settings_l2_mode_with_other_changes(graph, monkeypatch):
    seen = changed_kinds(monkeypatch)
    with_setting = edit(graph, {"use_l2_collectors": True, "num_mppt": 2})
    assert seen[-1] == ["settings", "inverter", "route", "schedule"]
    assert (with_setting["settings"]["num_mppt"], digest(with_setting)) == (2, WITH_NUM_MPPT_DIGEST)
    with_project = edit(graph, {"use_l2_collectors": True}, project_changes={"zip_code": "37601"})
    assert seen[-1] == ["project", "settings", "zone-el", "string", "string", "inverter", "route", "schedule"]
    assert with_project["inverters"][0]["equipment_type"] == "string_inverter"
    assert digest(with_project) == WITH_PROJECT_DIGEST


# ------------------------------------------------------------ everything else is main --

EDITS = (("panel_layer_contains", "PV"), ("panel_group_layer", "G2"), ("string_layer", "S2"),
         ("home_run_layer", "H2"), ("panels_in_sequence", 3), ("num_mppt", 2), ("strings_per_mppt", 3),
         ("optimizer_ratio", 2), ("panel_group_number", 5), ("inverter_number", 5), ("mppt_letter", "B"),
         ("string_number", 9))
MODES = (True, False, 1, 0, "yes", None, 1.0)
FIXTURES = (("w1", lambda g: g), ("topology", topology.topology_of), ("central_only", central_only),
            ("combiner_only", combiner_only), ("composite", composite), ("string_only_l2", string_only_l2),
            ("l1_empty", l1_empty), ("l2_empty", topology.bare))


def outcome(value, params, seen):
    """(rev, digest) or the refusal code, plus the kinds handed to advance; the input must not move."""
    before = copy.deepcopy(value)
    seen.clear()
    try:
        after = builtin().run(value, params)
        result = {"rev": after["rev"], "digest": digest(after)}
    except GraphValidationError as error:
        result = {"code": error.code}
    assert value == before
    result["kinds"] = seen[-1] if seen else None
    return result


def main_table(w1, seen):
    """Every request in the cross product that is not a boolean mode flip, keyed by its name."""
    table = {}
    for name, build in FIXTURES:
        fixture = build(w1)
        rev, mode_now = fixture["rev"], fixture["settings"]["use_l2_collectors"]

        def flip(mode):
            return type(mode) is bool and mode != mode_now

        def run(key, params):
            table[f"{name}|{key}"] = outcome(copy.deepcopy(fixture), dict(params, expected_rev=params.get(
                "expected_rev", rev)), seen)

        for mode in MODES:
            if not flip(mode):
                run(f"use_l2_collectors={json.dumps(mode)}", {"changes": {"use_l2_collectors": mode}})
        for key, value in EDITS:
            run(f"{key}={json.dumps(value)}", {"changes": {key: value}})
        for mode in (True, False):
            if not flip(mode):
                run(f"mode={mode}+num_mppt=2", {"changes": {"use_l2_collectors": mode, "num_mppt": 2}})
                run(f"mode={mode}+zip", {"changes": {"use_l2_collectors": mode},
                                         "project_changes": {"zip_code": "37601"}})
            run(f"mode={mode}|stale_rev", {"changes": {"use_l2_collectors": mode}, "expected_rev": rev + 1})
            run(f"mode={mode}|bogus", {"changes": {"use_l2_collectors": mode, "bogus": 1}})
            cancelled = builtin().run(copy.deepcopy(fixture), {"expected_rev": rev, "cancel": True,
                                                              "changes": {"use_l2_collectors": mode}})
            table[f"{name}|mode={mode}|cancel"] = {"equal": cancelled == fixture}
    return table


def test_settings_l2_mode_everything_else_is_main(graph, monkeypatch):
    seen = changed_kinds(monkeypatch)
    table = main_table(graph, seen)
    assert len(table) == MAIN_TABLE_ROWS
    text = json.dumps(table, sort_keys=True, separators=(",", ":"), allow_nan=False)
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == MAIN_TABLE_SHA256
    # Spot rows, readable: an equal mode, a value that is not a boolean, an empty drawing.
    assert table["w1|use_l2_collectors=false"]["kinds"] == ["settings"]
    assert table["topology|use_l2_collectors=true"]["kinds"] == ["settings"]
    assert table["w1|use_l2_collectors=\"yes\""]["code"] == "INVALID_GRAPH_SCHEMA"
    assert table["topology|mode=False|stale_rev"]["code"] == "STALE_GRAPH_REVISION"


def test_settings_l2_mode_empty_drawings_move_only_the_flag(graph):
    for build, mode in ((l1_empty, True), (topology.bare, False)):
        value = build(graph)
        after = edit(value, {"use_l2_collectors": mode})
        assert after["settings"]["use_l2_collectors"] is mode
        assert after["inverters"] == []


# ----------------------------------------------------------------- order and reads --

def test_settings_l2_mode_refusal_order(graph):
    value = topology.topology_of(graph)
    leave = {"expected_rev": value["rev"], "changes": {"use_l2_collectors": False}}
    assert refusal(value, dict(leave, unknown=1)) == "INVALID_SETTINGS_REQUEST"
    assert refusal(value, dict(leave, changes={"use_l2_collectors": False, "bogus": 1})) == \
        "INVALID_SETTINGS_REQUEST"
    assert refusal(value, dict(leave, expected_rev=value["rev"] + 1)) == "STALE_GRAPH_REVISION"
    assert builtin().run(copy.deepcopy(value), dict(leave, cancel=True)) == value
    assert refusal(value, leave) == PRESENT


def test_settings_l2_mode_presets_read_the_mode_back(graph, monkeypatch):
    value = presets_l2.with_store(graph, (False,), "A", False)
    entered = edit(value, {"use_l2_collectors": True})
    assert presets_l2.listed(entered)["current_settings"]["UseL2Collectors"] is True
    seen = presets_l2.changed_kinds(monkeypatch)
    after = presets_l2.commit(entered, {"subcommand": "Create", "name": "Beta"})
    assert seen == [[]]                                               # the read-back mode matches
    assert entities(after) == entities(entered)
    assert presets_l2.preset_modes(after) == [("A", True), ("B", True)]


# ------------------------------------------------------------------------ the rail --

def test_settings_l2_mode_on_the_rail_enter(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    with held(backend) as fence:
        first = dispatch(backend, fence, TOOL, {"expected_rev": 0, "changes": {"use_l2_collectors": True}})
    assert first["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
    head = head_graph(backend)
    assert head["settings"]["use_l2_collectors"] is True
    assert (head["inverters"][0]["id"], head["inverters"][0]["equipment_type"]) == (INVERTER_ID, "string_inverter")
    assert digest(head) == ENTER_DIGEST


def test_settings_l2_mode_on_the_rail_refused(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, topology.topology_of(graph))
    head = copy.deepcopy(head_graph(backend))
    with held(backend) as fence:
        with pytest.raises(GraphValidationError, match=PRESENT):
            dispatch(backend, fence, TOOL, {"expected_rev": 0, "changes": {"use_l2_collectors": False}})
    assert latest(backend) == 1
    assert head_graph(backend) == head
