"""Design presets and the drawing's L1/L2 mode (sf-w2-design-presets-l2-mode).

The plugin's SyncFromGlobalSettings always overwrites the drawing's UseL2Collectors from
Settings.Default (DrawingPropertiesJson.cs:608-623), so a LEAFPROFILE Swap to a preset of the other
mode changes the drawing's mode. The graph contract ties the inverters to that mode: in L2 mode every
inverter carries an equipment_type (EQUIPMENT_TYPE_REQUIRED), in L1 mode none does (L2_MODE_REQUIRED),
and an untyped inverter is never an L2. So the flag alone cannot change (solar-settings' own boolean
edit is refused, last row). The frozen policy (server/solar_preset_sync.py): a preset whose mode
equals the drawing's changes nothing; entering L2 mode types every inverter as an unconnected string
inverter (equipment_type "string_inverter", l2_ref null) in the same commit, and the graph is
validated; leaving L2 mode removes equipment_type and l2_ref, and is refused with
DESIGN_PRESET_L2_EQUIPMENT_PRESENT, before anything is written, when any inverter is a combiner box or
a central inverter or names an L2. The mode is read back from the graph before every commit.

Covered: the constants against the graph contract and the preset kernel; entering and leaving L2
mode on the populated W1 fixture with the changed entities, validities and digests measured; the
electrical bridge reading the typed inverter as the same device; every refusal with the input
untouched; matching modes commit and change no entity; the read back into Create and List; Swap
both ways and the delete of the active preset; a duplicate inverter number refused by validation;
the mode with the installation design on a drawing with no frame; the refusal order; solar-settings
left as it was; the rail both ways. Inputs are authored here or are committed evidence, so every
count and digest is exact.
"""
import copy
import json
from pathlib import Path

import pytest

import solar_electrical_state_bridge as bridge
import solar_local_graph
import solar_local_read
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

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = ROOT / "docs/parity/evidence/ground/generate/profile-settings.json"
SCHEMA = ROOT / "contract/solar-design-graph.v1.schema.json"
TOOL = "solar-design-presets"
LIST = "solar-design-presets-list"
PRESENT = "DESIGN_PRESET_L2_EQUIPMENT_PRESENT"
NOT_CURRENT = "SOLAR_OUTPUT_NOT_CURRENT"
VALID = {"state": "valid", "reasons": []}
STALE = {"state": "stale", "reasons": ["settings_changed"]}
# The W1 fixture's layer names, so a preset that carries them changes no other drawing setting and
# only the mode rule is exercised.
FIXTURE_LAYERS = {"StringLayer": "Strings", "HomeRunLayer": "Homeruns", "PanelGroupLayer": "Groups"}
INVERTER_ID = "leaf:inverter:00000000-0000-4000-8000-000000000001"
# Measured with python -B (solar_sizing_client.digest, canonical sha256).
ENTER_DIGEST = "1e011e914267d2812d71777a52f58bd737dd3eca1e24fad1605fe5c18d884094"
LEAVE_DIGEST = "d444a9198717901e993a09f79065442ad80d1e663454053fe31d5bdb2b9b1c00"
SWAP_STEPS = [
    (1, True, "string_inverter", ["settings", "route", "schedule", "inverter"],
     "fb3f3686e9a63a8761505307f0cfb41e588dc330bd22422d5f579b1e8d17f227"),
    (2, False, None, ["settings", "inverter"],
     "688480891fe1a5067ad9fc2c451e0ff5b151200e7d6e147d9e95218289048fc8"),
    (3, True, "string_inverter", ["settings", "inverter"],
     "3e5ddd5ca78c903d83eab4129b33e386a6f2d9e1e99cb916e963c998e7ab0030"),
]
DELETE_STEP = (1, True, "string_inverter", ["settings", "route", "schedule", "inverter"],
               "9331289e7772e0d533354de0cb9b22e5ae30214bc19f504fa599cc84828d5c2b")
BARE_BOTH_DIGEST = "cac136888e662dea82881d117889c33dae213b6cd23c6357a565ca8b4812d473"
BARE_MODE_DIGEST = "80e69e5c45013ef3c246f1e0bc44df932bcaf72a587723ac7ead7c04936ac33c"


def commit_builtin():
    return solar_local_graph._load_builtin(TOOL)


def list_builtin():
    return solar_local_read._load_builtin(LIST)


def kernel():
    return commit_builtin().preset_store.profiles


def settings_of(mode, design="Roof", **patch):
    """The committed snapshot's current settings with UseL2Collectors `mode`, InstallationDesign
    `design` and the fixture's three layer names."""
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    return dict(kernel().current_settings_from_preset(snapshot), **FIXTURE_LAYERS,
                InstallationDesign=design, UseL2Collectors=mode, **patch)


def commit(value, params):
    return commit_builtin().run(copy.deepcopy(value), dict(params, expected_rev=value["rev"]))


def create(value, name, mode, design="Roof"):
    return commit(value, {"subcommand": "Create", "name": name, "current_settings": settings_of(mode, design)})


def refusal(value, params):
    before = copy.deepcopy(value)
    with pytest.raises(GraphValidationError) as caught:
        commit_builtin().run(value, dict(params, expected_rev=value["rev"]))
    assert value == before                                            # nothing written on refusal
    return caught.value.code


def listed(value):
    return list_builtin().run(copy.deepcopy(value), {})


def store_of(value):
    return value["extra"]["design_profiles"]


def with_store(value, modes, active, current_mode):
    """`value` with a preset store written through the kernel and the store module directly: one
    preset per mode in order (P0 prefix A, P1 prefix B, ...), each the snapshot's settings with that
    UseL2Collectors, `active` the active prefix, the store's current settings carrying
    `current_mode`. The Create path is proven in test_solar_tool_design_presets.py."""
    value = copy.deepcopy(value)
    builtin = commit_builtin()
    manager = kernel().DesignProfileManager()
    for index, _ in enumerate(modes):
        manager.create_profile(f"P{index}", False, settings_of(False), created_utc=builtin._UNRECORDED)
    for profile, mode in zip(manager.profiles, modes):
        profile.settings["UseL2Collectors"] = mode
    manager.active_prefix = active
    builtin.preset_store.save(value, settings_of(current_mode), manager)
    return validate_graph(value)


def preset_modes(value):
    return [(p["Prefix"], p["Settings"]["UseL2Collectors"]) for p in store_of(value)["record"]["Profiles"]]


def changed_kinds(monkeypatch):
    """Capture the kinds of the entities each commit hands to advance, in order."""
    builtin = commit_builtin()
    original = builtin.advance
    seen = []

    def capture(result, changed, tool):
        seen.append([entity["kind"] for entity in changed])
        return original(result, changed, tool)

    monkeypatch.setattr(builtin, "advance", capture)
    return seen


def export_code(value):
    try:
        require_current_export(value)
    except GraphValidationError as error:
        return error.code
    return "current"


def bare(value):
    """The W1 fixture with no frame and no inverter: no zone, panel, string, inverter or route. Its
    one schedule stays, a derived output the change must stale."""
    value = copy.deepcopy(value)
    for key in ("electrical_zones", "frames", "panels", "strings", "inverters", "routes"):
        value[key] = []
    return validate_graph(value)


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


# ------------------------------------------------------------------------- constants --

def test_presets_l2_mode_constants():
    assert solar_preset_sync.L2_EQUIPMENT_PRESENT == PRESENT
    assert solar_preset_sync.L1_ENTRY_TYPE == "string_inverter"
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert solar_preset_sync.L1_ENTRY_TYPE in schema["$defs"]["inverter"]["properties"]["equipment_type"]["enum"]
    assert solar_preset_sync.ALWAYS[-1] == ("use_l2_collectors", "UseL2Collectors")
    assert ("UseL2Collectors", "b") in kernel().PRESET_FIELDS


# ------------------------------------------------------------ entering and leaving --

def test_presets_l2_mode_enter_types_every_inverter(graph, monkeypatch):
    seen = changed_kinds(monkeypatch)
    assert graph["settings"]["use_l2_collectors"] is False
    assert "equipment_type" not in graph["inverters"][0]
    after = create(graph, "Alpha", True)
    assert seen == [["settings", "route", "schedule", "inverter"]]
    assert (after["rev"], digest(after)) == (1, ENTER_DIGEST)
    assert after["settings"]["use_l2_collectors"] is True
    assert after["settings"]["global_string_sizing_confirmed"] is False
    before, typed = graph["inverters"][0], after["inverters"][0]
    assert sorted(k for k in set(before) | set(typed) if before.get(k, "-") != typed.get(k, "-")) == \
        ["equipment_type", "l2_ref", "provenance", "rev"]
    assert (typed["equipment_type"], typed["l2_ref"], typed["is_l2"], typed["validity"], typed["rev"]) == \
        ("string_inverter", None, False, VALID, 1)
    assert typed["input_assignments"] == before["input_assignments"]
    assert [(e["kind"], e["validity"]) for e in entities(after) if e["validity"] != VALID] == \
        [("route", STALE), ("schedule", STALE)]
    assert export_code(after) == NOT_CURRENT
    assert store_of(after)["current_settings"]["UseL2Collectors"] is True
    assert validate_graph(after) == after


def test_presets_l2_mode_leave_restores_the_untyped_inverter(graph, monkeypatch):
    entered = create(graph, "Alpha", True)
    seen = changed_kinds(monkeypatch)
    after = create(entered, "Beta", False)
    assert seen == [["settings", "inverter"]]                         # route and schedule already stale
    assert (after["rev"], digest(after)) == (2, LEAVE_DIGEST)
    assert after["settings"]["use_l2_collectors"] is False
    before, untyped = graph["inverters"][0], after["inverters"][0]
    assert sorted(k for k in set(before) | set(untyped) if before.get(k, "-") != untyped.get(k, "-")) == \
        ["provenance", "rev"]
    assert export_code(after) == NOT_CURRENT
    # Supplied settings are the drawing's state at that moment: the kernel's Create saved them into
    # the then active preset A first (CreateProfile -> SaveActiveProfileState).
    assert preset_modes(after) == [("A", False), ("B", False)]


def test_presets_l2_mode_two_inverters_round_trip(graph):
    value = copy.deepcopy(graph)
    second = copy.deepcopy(value["inverters"][0])
    second.update(id=app_id("inverter", 7), number=7, input_assignments=[])
    value["inverters"].append(second)
    entered = create(value, "Alpha", True)
    assert len(entered["inverters"]) == 2
    for inverter in entered["inverters"]:
        assert inverter["equipment_type"] == "string_inverter"
        assert inverter["l2_ref"] is None
        assert inverter["is_l2"] is False
    assert (entered["inverters"][1]["id"], entered["inverters"][1]["number"],
            entered["inverters"][1]["input_assignments"]) == (second["id"], second["number"], [])
    assert validate_graph(entered) == entered
    after = create(entered, "Beta", False)
    assert len(after["inverters"]) == 2
    assert all("equipment_type" not in inverter for inverter in after["inverters"])
    assert (after["inverters"][1]["id"], after["inverters"][1]["number"],
            after["inverters"][1]["input_assignments"]) == (second["id"], second["number"], [])
    assert validate_graph(after) == after


def test_presets_l2_mode_bridge_reads_the_same_device(graph):
    # The electrical bridge reads an untyped inverter and a "string_inverter" as the same device row
    # (only a combiner box carries a box input count), so typing changes no device.
    entered = create(graph, "Alpha", True)
    state_l1, _ = bridge.state_from_graph(graph)
    state_l2, _ = bridge.state_from_graph(entered)
    assert state_l1["rows"]["device"] == state_l2["rows"]["device"]
    assert (state_l1["setting"]["UseL2Collectors"], state_l2["setting"]["UseL2Collectors"]) == (False, True)


# --------------------------------------------------------------------- refusals --

REFUSALS = [
    ("topology-create", topology.topology_of, (True, False), "A", {"subcommand": "Create", "name": "Beta"}),
    ("central-only-create", central_only, (True, False), "A", {"subcommand": "Create", "name": "Beta"}),
    ("combiner-only-create", combiner_only, (True, False), "A", {"subcommand": "Create", "name": "Beta"}),
    ("feeder-and-trench-create", composite, (True, False), "A", {"subcommand": "Create", "name": "Beta"}),
    ("topology-swap", topology.topology_of, (True, False), "A", {"subcommand": "Swap", "prefix": "B"}),
    ("topology-delete-active", topology.topology_of, (True, False), "A", {"subcommand": "Delete", "prefix": "A"}),
]


@pytest.mark.parametrize("case", REFUSALS, ids=[case[0] for case in REFUSALS])
def test_presets_l2_mode_leaving_refused(graph, case):
    _, build, modes, active, params = case
    value = with_store(build(graph), modes, active, True)
    assert value["settings"]["use_l2_collectors"] is True
    if params["subcommand"] == "Create":
        params = dict(params, current_settings=settings_of(False))
    assert refusal(value, params) == PRESENT


def test_presets_l2_mode_matching_mode_changes_nothing(graph, monkeypatch):
    seen = changed_kinds(monkeypatch)
    l1 = create(graph, "Alpha", False)
    typed = topology.topology_of(graph)
    l2 = create(typed, "Alpha", True)
    assert seen == [[], []]
    assert entities(l1) == entities(graph) and entities(l2) == entities(typed)


def test_presets_l2_mode_read_back(graph, monkeypatch):
    # The store says L1 while the drawing is in L2 mode (a store written before this rule).
    value = with_store(topology.topology_of(graph), (False, False), "A", False)
    assert listed(value)["current_settings"]["UseL2Collectors"] is True
    seen = changed_kinds(monkeypatch)
    after = commit(value, {"subcommand": "Create", "name": "Gamma"})
    assert seen == [[]]
    assert entities(after) == entities(value)
    # The kernel's Create saved the read-back state into the then active preset A.
    assert preset_modes(after) == [("A", True), ("B", False), ("C", True)]
    assert store_of(after)["current_settings"]["UseL2Collectors"] is True


def test_presets_l2_mode_swap_both_ways(graph, monkeypatch):
    value = with_store(graph, (False, True), "A", False)
    seen = changed_kinds(monkeypatch)
    steps = []
    for prefix in ("B", "A", "B"):
        value = commit(value, {"subcommand": "Swap", "prefix": prefix})
        steps.append((value["rev"], value["settings"]["use_l2_collectors"],
                      value["inverters"][0].get("equipment_type"), seen[-1], digest(value)))
    assert steps == SWAP_STEPS
    assert preset_modes(value) == [("A", False), ("B", True)]


def test_presets_l2_mode_delete_active(graph, monkeypatch):
    value = with_store(graph, (False, True), "A", False)
    seen = changed_kinds(monkeypatch)
    after = commit(value, {"subcommand": "Delete", "prefix": "A"})
    assert (after["rev"], after["settings"]["use_l2_collectors"], after["inverters"][0].get("equipment_type"),
            seen[-1], digest(after)) == DELETE_STEP
    assert store_of(after)["record"]["ActivePrefix"] == "B"


def test_presets_l2_mode_duplicate_numbers_refused_by_validation(graph):
    # L1 mode does not check the numbers of untyped inverters; typing them does.
    value = copy.deepcopy(graph)
    second = copy.deepcopy(value["inverters"][0])
    second.update(id=app_id("inverter", 7), input_assignments=[])
    value["inverters"].append(second)
    assert validate_graph(value) == value
    params = {"subcommand": "Create", "name": "Alpha", "current_settings": settings_of(True)}
    assert refusal(value, params) == "DUPLICATE_EQUIPMENT_NUMBER"


def test_presets_l2_mode_with_the_design_on_a_bare_drawing(graph, monkeypatch):
    value = bare(graph)
    assert [e["kind"] for e in entities(value)] == ["project", "settings", "schedule"]
    seen = changed_kinds(monkeypatch)
    both = create(value, "Alpha", True, "Ground")
    assert seen[-1] == ["project", "settings", "schedule"]
    assert (both["project"]["installation_design"], both["settings"]["use_l2_collectors"]) == ("Ground", True)
    # The settings rule runs first; the project rule keeps that first cause.
    assert both["schedules"][0]["validity"] == STALE
    assert (both["rev"], digest(both)) == (1, BARE_BOTH_DIGEST)
    mode = create(value, "Alpha", True)
    assert seen[-1] == ["settings", "schedule"]
    assert mode["project"] == value["project"]
    assert (mode["rev"], digest(mode)) == (1, BARE_MODE_DIGEST)


def test_presets_l2_mode_refusal_order(graph):
    value = with_store(topology.topology_of(graph), (True,), "A", True)
    leave = {"subcommand": "Create", "name": "Beta", "current_settings": settings_of(False)}
    with pytest.raises(GraphValidationError) as caught:
        commit_builtin().run(copy.deepcopy(value), dict(leave, expected_rev=value["rev"] + 1))
    assert caught.value.code == "STALE_GRAPH_REVISION"
    assert refusal(value, dict(leave, current_settings=settings_of(False, NumMppt=-1))) == \
        "DESIGN_PRESET_SETTINGS_OUT_OF_RANGE"
    assert refusal(value, dict(leave, name=" p0 ")) == "DESIGN_PRESET_NAME_EXISTS"
    # The installation design is checked first, then the mode, both after the kernel.
    assert refusal(value, dict(leave, current_settings=settings_of(False, "Ground"))) == \
        "DESIGN_PRESET_INSTALLATION_POPULATED"
    assert refusal(value, leave) == PRESENT


def test_presets_l2_mode_solar_settings_unchanged(graph):
    # A boolean assignment alone cannot implement the mode: solar-settings still refuses it.
    with pytest.raises(GraphValidationError) as caught:
        solar_local_graph._load_builtin("solar-settings").run(
            copy.deepcopy(graph), {"expected_rev": graph["rev"], "changes": {"use_l2_collectors": True}})
    assert caught.value.code == "EQUIPMENT_TYPE_REQUIRED"


# ------------------------------------------------------------------------ the rail --

def test_presets_l2_mode_on_the_rail_enter(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    with held(backend) as fence:
        first = dispatch(backend, fence, TOOL, {"expected_rev": 0, "subcommand": "Create", "name": "Alpha",
                                               "current_settings": settings_of(True)})
    assert first["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
    head = head_graph(backend)
    assert head["settings"]["use_l2_collectors"] is True
    assert (head["inverters"][0]["id"], head["inverters"][0]["equipment_type"]) == (INVERTER_ID, "string_inverter")
    assert digest(head) == ENTER_DIGEST


def test_presets_l2_mode_on_the_rail_refused(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, topology.topology_of(graph))
    with held(backend) as fence:
        with pytest.raises(GraphValidationError, match=PRESENT):
            dispatch(backend, fence, TOOL, {"expected_rev": 0, "subcommand": "Create", "name": "Alpha",
                                           "current_settings": settings_of(False)})
        assert latest(backend) == 1
        first = dispatch(backend, fence, TOOL, {"expected_rev": 0, "subcommand": "Create", "name": "Alpha",
                                               "current_settings": settings_of(True)})
    assert first["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
    head = head_graph(backend)
    assert [i.get("equipment_type") for i in head["inverters"]] == ["combiner_box", "central_inverter"]
