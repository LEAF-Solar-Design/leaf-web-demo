"""Design presets applied to the drawing: the plugin's SyncFromGlobalSettings rule on the graph.

A LEAFPROFILE Swap rewrites the plugin's Settings.Default (ProjectPreset.ApplyToSettings), and the
plugin carries Settings.Default into the drawing's own properties through
DrawingPropertiesJson.SyncFromGlobalSettings: the three layer names always, the string length and
MPPT topology only where the drawing still holds 0. Studio applies that rule to graph["settings"]
after every solar-design-presets commit (server/solar_preset_sync.py) through the solar-settings
rule, and reads the three layer names back from the graph before one.

Covered: the rule table; the parity chain's one settings write (f1) and nothing after it; a Swap
applying the target's layers and keeping the outgoing preset's; a solar-settings edit captured
into the outgoing preset and restored on the round trip; the <= 0 seeds; the delete of the active
preset; supplied settings that match the graph change nothing (sizing kept) and ones that differ
invalidate sizing exactly as solar-settings does; the fields never synced (panel layer filter, L2
flag, optimizer ratio, counters, cable fields); the graph layer name the store cannot hold; List
reading the graph's layer names; purity; the rail. The installation design rule has its own file,
test_solar_tool_design_presets_installation.py.
"""
import copy
import json
from pathlib import Path

import pytest

import product_capability_availability as availability
import solar_local_graph
import solar_local_read
from solar_design_graph import GraphValidationError, entities, validate_graph
from solar_sizing_client import digest, require_sizing, sizing_basis
from solar_solve_results import upstream_basis
from test_w1_design_graph import graph  # noqa: F401
from test_w1_local_graph_adapter import held
from test_w1_sizing_groups import confirm, passing, service, sizing_params  # noqa: F401
from test_w1_solve_commit import seed
from test_solar_w2_registration import dispatch, head_graph, latest

SNAPSHOT = Path(__file__).resolve().parents[2] / "docs/parity/evidence/ground/generate/profile-settings.json"
TOOL = "solar-design-presets"
LIST = "solar-design-presets-list"
LAYERS = ("string_layer", "home_run_layer", "panel_group_layer")
SEEDED_KEYS = ("panels_in_sequence", "num_mppt", "strings_per_mppt")
NEVER = ("panel_layer_contains", "use_l2_collectors", "optimizer_ratio", "panel_group_number",
         "string_number", "inverter_number", "mppt_letter")
# The fixture graph's settings before any preset commit (test_w1_design_graph.graph), measured.
FIXTURE_LAYERS = {"string_layer": "Strings", "home_run_layer": "Homeruns", "panel_group_layer": "Groups"}
# The committed snapshot's layer names (docs/parity/evidence/ground/generate/profile-settings.json).
SNAPSHOT_LAYERS = {"string_layer": "String", "home_run_layer": "HomeRun", "panel_group_layer": "Panel Group"}
BETA_LAYERS = {"StringLayer": "B-String", "HomeRunLayer": "B-Home", "PanelGroupLayer": "B-Groups"}
GRAPH_BETA = {"string_layer": "B-String", "home_run_layer": "B-Home", "panel_group_layer": "B-Groups"}
# canonical sha256 (solar_sizing_client.digest) of graph["settings"] after f1 on the fixture graph.
F1_SETTINGS_SHA256 = "e260816996efd904294ccb9883a990fed60de7750889a5b71a5bccaf8e16e9c4"
# canonical sha256 of the whole graph after test_design_presets_apply_solar_settings_edit_round_trip.
# Moved with sf-w2-design-presets-installation: snapshot_current() carries InstallationDesign "Roof".
ROUND_TRIP_GRAPH_SHA256 = "0d6ee64f30aca99268ae041e51dfeb8dac40241fbd889724eb1071143b4accf1"


def commit_builtin():
    return solar_local_graph._load_builtin(TOOL)


def settings_builtin():
    return solar_local_graph._load_builtin("solar-settings")


def list_builtin():
    return solar_local_read._load_builtin(LIST)


def kernel():
    return commit_builtin().preset_store.profiles


def snapshot_current():
    """The committed snapshot's current settings on this rooftop fixture: InstallationDesign "Roof",
    since the snapshot's own "Ground" is refused on a drawing with Roof frames
    (sf-w2-design-presets-installation, test_solar_tool_design_presets_installation.py)."""
    return dict(kernel().current_settings_from_preset(json.loads(SNAPSHOT.read_text(encoding="utf-8"))),
                InstallationDesign="Roof")


def commit(value, params):
    return commit_builtin().run(copy.deepcopy(value), dict(params, expected_rev=value["rev"]))


def create(value, name, **extra):
    return commit(value, dict({"subcommand": "Create", "name": name}, **extra))


def swap(value, prefix, **extra):
    return commit(value, dict({"subcommand": "Swap", "prefix": prefix}, **extra))


def delete(value, prefix):
    return commit(value, {"subcommand": "Delete", "prefix": prefix})


def edit(value, changes):
    return settings_builtin().run(copy.deepcopy(value), {"expected_rev": value["rev"], "changes": changes})


def listed(value):
    return list_builtin().run(copy.deepcopy(value), {})


def layers(value):
    return {key: value["settings"][key] for key in LAYERS}


def store_of(value):
    return value["extra"]["design_profiles"]


def preset(value, prefix):
    return next(p["Settings"] for p in store_of(value)["record"]["Profiles"] if p["Prefix"] == prefix)


def beta_settings(**patch):
    return dict(snapshot_current(), **BETA_LAYERS, **patch)


def two_presets(graph):
    """Alpha and Beta over the snapshot, then the B layers set through solar-settings while Beta is
    active: Beta active, the graph on the B layers, Alpha still on the snapshot's."""
    value = create(graph, "Alpha", current_settings=snapshot_current())
    value = create(value, "Beta")
    return edit(value, GRAPH_BETA)


def refusal(value, params):
    with pytest.raises(GraphValidationError) as caught:
        commit_builtin().run(copy.deepcopy(value), dict(params, expected_rev=value["rev"]))
    return caught.value.code


def located(value):
    value["project"].update(zip_code="44224", latitude=41.0, longitude=-81.4)
    return value


# ---------------------------------------------------------------------------- the rule --

def test_design_presets_apply_rule_table():
    sync = commit_builtin().preset_sync
    assert sync.ALWAYS == (("string_layer", "StringLayer"), ("home_run_layer", "HomeRunLayer"),
                           ("panel_group_layer", "PanelGroupLayer"))
    assert sync.SEEDED == (("panels_in_sequence", "NumPanelsInSequence", False),
                           ("strings_per_mppt", "StringsPerMppt", True), ("num_mppt", "NumMppt", True))
    assert sync.GRAPH_SETTINGS_OUT_OF_RANGE == "DESIGN_PRESET_GRAPH_SETTINGS_OUT_OF_RANGE"
    fields = set(kernel().SETTINGS_FIELD_NAMES)
    assert {field for _, field in sync.ALWAYS} | {field for _, field, _ in sync.SEEDED} <= fields
    assert not {field for _, field in sync.ALWAYS} & set(kernel().CABLE_FIELDS)


def test_design_presets_apply_f1_writes_the_snapshot_layers(graph):
    assert layers(graph) == FIXTURE_LAYERS
    after = create(graph, "Alpha", current_settings=snapshot_current())
    settings, before = after["settings"], graph["settings"]
    assert layers(after) == SNAPSHOT_LAYERS
    # The graph already holds a string length and topology, so nothing is seeded.
    assert {k: settings[k] for k in SEEDED_KEYS} == {k: before[k] for k in SEEDED_KEYS} == \
        {"panels_in_sequence": 2, "num_mppt": 1, "strings_per_mppt": 2}
    assert {k: settings[k] for k in NEVER} == {k: before[k] for k in NEVER}
    assert after["project"] == graph["project"]                       # installation_design stays "Roof"
    assert before["global_string_sizing_confirmed"] is True
    assert settings["global_string_sizing_confirmed"] is False
    assert (settings["rev"], settings["provenance"]["last_writer"], settings["provenance"]["tool_id"],
            settings["provenance"]["source_rev"]) == (1, TOOL, TOOL, 0)
    # The layer change stales the homerun and the schedule (solar_settings_invalidation.py) and
    # nothing else.
    moved = ("settings", "route", "schedule")
    assert [e for e in entities(after) if e["kind"] not in moved] == \
        [e for e in entities(graph) if e["kind"] not in moved]
    for item in after["routes"] + after["schedules"]:
        assert item["validity"] == {"state": "stale", "reasons": ["settings_changed"]}
        assert (item["rev"], item["provenance"]["last_writer"], item["provenance"]["tool_id"],
                item["provenance"]["source_rev"]) == (1, TOOL, TOOL, 0)
    expected_outputs = copy.deepcopy(graph["routes"] + graph["schedules"])
    for item in expected_outputs:
        item["validity"] = {"state": "stale", "reasons": ["settings_changed"]}
        item["rev"] = 1
        item["provenance"].update(last_writer=TOOL, tool_id=TOOL, source_rev=0)
    actual_outputs = after["routes"] + after["schedules"]
    assert len(actual_outputs) == len(expected_outputs)
    for actual, expected in zip(actual_outputs, expected_outputs):
        assert json.dumps(actual, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                          allow_nan=False) == \
            json.dumps(expected, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                       allow_nan=False)
    assert digest(settings) == F1_SETTINGS_SHA256


def test_design_presets_apply_chain_writes_settings_once(graph):
    value = create(graph, "Alpha", current_settings=snapshot_current())
    first = copy.deepcopy(value["settings"])
    for params in ({"subcommand": "Create", "name": "Beta"}, {"subcommand": "Swap", "prefix": "A"},
                   {"subcommand": "Delete", "prefix": "B"}):
        value = commit(value, params)
        assert value["settings"] == first                            # f2, f3, f5 change no setting
    assert value["rev"] == 4


def test_design_presets_apply_is_the_solar_settings_rule(graph):
    before = create(graph, "Alpha", current_settings=snapshot_current())
    through_presets = create(before, "Beta", current_settings=beta_settings())
    through_settings = edit(before, {"string_layer": "B-String", "home_run_layer": "B-Home",
                                     "panel_group_layer": "B-Groups"})

    def without_writer(value):
        settings = copy.deepcopy(value["settings"])
        settings["provenance"].pop("last_writer")
        settings["provenance"].pop("tool_id")
        return settings

    assert without_writer(through_presets) == without_writer(through_settings)


# ------------------------------------------------------------------------------ swap --

def test_design_presets_apply_swap_applies_the_target_layers(graph):
    value = two_presets(graph)
    assert layers(value) == GRAPH_BETA
    alpha_layers = {"StringLayer": "String", "HomeRunLayer": "HomeRun", "PanelGroupLayer": "Panel Group"}
    assert {k: preset(value, "A")[k] for k in BETA_LAYERS} == alpha_layers
    after = swap(value, "A")
    assert store_of(after)["record"]["ActivePrefix"] == "A"
    assert layers(after) == SNAPSHOT_LAYERS
    assert {k: preset(after, "B")[k] for k in BETA_LAYERS} == BETA_LAYERS      # captured from the graph
    assert after["settings"]["rev"] == after["rev"]
    assert layers(swap(after, "B")) == GRAPH_BETA


def test_design_presets_apply_swap_keeps_the_hardware_of_a_sized_drawing(graph, passing, service):
    value = create(graph, "Alpha", current_settings=snapshot_current())
    value = create(value, "Beta")
    value = confirm(located(value), sizing_params(value, passing))["graph"]
    evidence = require_sizing(value)
    after = swap(value, "A", current_settings=beta_settings(NumMppt=7, StringsPerMppt=3,
                                                             NumPanelsInSequence=20))
    assert require_sizing(after) == evidence
    assert layers(after) == SNAPSHOT_LAYERS
    assert layers(swap(after, "B")) == GRAPH_BETA
    # Verified sizing leaves positive hardware values, so SyncFromGlobalSettings seeds none.
    assert all(value["settings"][k] > 0 for k in SEEDED_KEYS)
    assert {k: after["settings"][k] for k in SEEDED_KEYS} == \
        {k: value["settings"][k] for k in SEEDED_KEYS}
    assert (preset(after, "B")["NumMppt"], preset(after, "B")["StringsPerMppt"],
            preset(after, "B")["NumPanelsInSequence"]) == (7, 3, 20)
    assert store_of(after)["current_settings"]["NumMppt"] == 12          # Alpha's, in the store only


def test_design_presets_apply_solar_settings_edit_round_trip(graph):
    value = two_presets(graph)
    value = swap(value, "A")
    value = edit(value, {"string_layer": "Edited"})
    assert store_of(value)["current_settings"]["StringLayer"] == "String"    # the store is stale here
    assert listed(value)["current_settings"]["StringLayer"] == "Edited"      # List reads the graph
    value = swap(value, "B")
    assert preset(value, "A")["StringLayer"] == "Edited"                    # captured from the graph
    assert layers(value)["string_layer"] == "B-String"
    value = swap(value, "A")
    assert layers(value) == {"string_layer": "Edited", "home_run_layer": "HomeRun",
                             "panel_group_layer": "Panel Group"}
    assert value["rev"] == 7
    assert digest(value) == ROUND_TRIP_GRAPH_SHA256


# ----------------------------------------------------------------------------- seeds --

def zeroed(graph):
    value = copy.deepcopy(graph)
    value["settings"].update(panels_in_sequence=0, num_mppt=0, strings_per_mppt=0)
    return value


def test_design_presets_apply_seeds_a_zero_drawing(graph):
    after = create(zeroed(graph), "Alpha", current_settings=snapshot_current())
    assert {k: after["settings"][k] for k in SEEDED_KEYS} == \
        {"panels_in_sequence": 14, "num_mppt": 12, "strings_per_mppt": 2}


def test_design_presets_apply_seed_rules_for_a_zero_preset(graph):
    supplied = dict(snapshot_current(), NumPanelsInSequence=0, NumMppt=0, StringsPerMppt=0)
    after = create(zeroed(graph), "Alpha", current_settings=supplied)
    # NumMppt and StringsPerMppt seed only when > 0; NumPanelsInSequence seeds as it is, so 0 stays 0.
    assert {k: after["settings"][k] for k in SEEDED_KEYS} == \
        {"panels_in_sequence": 0, "num_mppt": 0, "strings_per_mppt": 0}
    assert layers(after) == SNAPSHOT_LAYERS


def test_design_presets_apply_zero_preset_with_matching_layers_keeps_settings(graph, monkeypatch):
    value = zeroed(graph)
    value["settings"].update(string_layer="L1", home_run_layer="L2", panel_group_layer="L3")
    supplied = dict(snapshot_current(), NumPanelsInSequence=0, NumMppt=0, StringsPerMppt=0,
                    StringLayer="L1", HomeRunLayer="L2", PanelGroupLayer="L3")
    before = json.dumps(value["settings"], sort_keys=True, separators=(",", ":"), allow_nan=False)
    assert value["settings"]["global_string_sizing_confirmed"] is True
    builtin = commit_builtin()
    original_advance = builtin.advance
    changed_sets = []

    def capture_advance(result, changed, tool):
        changed_sets.append(copy.deepcopy(changed))
        return original_advance(result, changed, tool)

    monkeypatch.setattr(builtin, "advance", capture_advance)
    for params in ({"subcommand": "Create", "name": "Alpha", "current_settings": supplied},
                   {"subcommand": "Create", "name": "Beta"},
                   {"subcommand": "Swap", "prefix": "A"}):
        value = commit(value, params)
        assert json.dumps(value["settings"], sort_keys=True, separators=(",", ":"),
                          allow_nan=False) == before
        assert value["settings"]["global_string_sizing_confirmed"] is True
    assert changed_sets == [[], [], []]


def test_design_presets_apply_seeds_once(graph):
    value = create(zeroed(graph), "Alpha", current_settings=snapshot_current())
    value = create(value, "Beta", current_settings=beta_settings(NumMppt=7, StringsPerMppt=3,
                                                                 NumPanelsInSequence=20))
    # The drawing now holds 14, 12 and 2 (seeded by Alpha), so Beta's values seed nothing.
    assert {k: value["settings"][k] for k in SEEDED_KEYS} == \
        {"panels_in_sequence": 14, "num_mppt": 12, "strings_per_mppt": 2}


# ---------------------------------------------------------------------------- delete --

def test_design_presets_apply_delete_active_applies_the_first_remaining(graph):
    value = two_presets(graph)                                   # Beta active, B layers on the graph
    after = delete(value, "B")
    assert store_of(after)["record"]["ActivePrefix"] == "A"
    assert layers(after) == SNAPSHOT_LAYERS
    assert after["settings"]["rev"] == after["rev"]


def test_design_presets_apply_delete_inactive_changes_no_setting(graph):
    value = swap(two_presets(graph), "A")
    after = delete(value, "B")
    assert after["settings"] == value["settings"]
    assert after["rev"] == value["rev"] + 1


# ------------------------------------------------------------------- sizing evidence --

def test_design_presets_apply_matching_settings_keep_sizing(graph, passing, service):
    sized = confirm(located(graph), sizing_params(graph, passing))["graph"]
    evidence = require_sizing(sized)
    supplied = dict(snapshot_current(), StringLayer="Strings", HomeRunLayer="Homeruns",
                    PanelGroupLayer="Groups")
    after = commit(sized, {"subcommand": "Create", "name": "Alpha", "current_settings": supplied})
    assert after["settings"] == sized["settings"]
    assert entities(after) == entities(sized)
    assert upstream_basis(after) == upstream_basis(sized)
    assert require_sizing(after) == evidence
    assert availability.w1_graph_readiness(after) == availability.w1_graph_readiness(sized)


def test_design_presets_apply_differing_settings_drop_sizing(graph, passing, service):
    sized = confirm(located(graph), sizing_params(graph, passing))["graph"]
    assert "string_sizing" in sized["settings"]["extra"]
    assert sized["settings"]["global_string_sizing_confirmed"] is True
    after = commit(sized, {"subcommand": "Create", "name": "Alpha", "current_settings": snapshot_current()})
    assert layers(after) == SNAPSHOT_LAYERS
    assert "string_sizing" not in after["settings"]["extra"]
    assert after["settings"]["global_string_sizing_confirmed"] is False
    assert upstream_basis(after) != upstream_basis(sized)
    assert sizing_basis(after) == sizing_basis(sized)                # sizing inputs are unchanged
    through_settings = edit(sized, {"string_layer": "String", "home_run_layer": "HomeRun",
                                    "panel_group_layer": "Panel Group"})
    assert availability.w1_graph_readiness(after) == availability.w1_graph_readiness(through_settings)


# ------------------------------------------------------------------------ never synced --

def test_design_presets_apply_never_synced_fields(graph):
    value = create(graph, "Alpha", current_settings=snapshot_current())
    value = create(value, "Beta")
    other = beta_settings(ModuleLayer="Other panels", UseL2Collectors=False, OptimizerRatio="2:1",
                          InstallationDesign="Roof", UseOptimizers=True)
    value = swap(value, "A", current_settings=other)                 # Beta keeps `other`
    after = swap(value, "B")
    assert layers(after) == GRAPH_BETA
    for step in (value, after):
        assert {k: step["settings"][k] for k in NEVER} == {k: graph["settings"][k] for k in NEVER}
        assert step["project"] == graph["project"]                   # installation_design stays "Roof"
    current = store_of(after)["current_settings"]
    assert (current["ModuleLayer"], current["UseL2Collectors"], current["OptimizerRatio"],
            current["InstallationDesign"]) == ("Other panels", False, "2:1", "Roof")


def test_design_presets_apply_cable_fields_stay_in_the_store(graph):
    alpha = dict(snapshot_current(), **{f: "A-" + f for f in kernel().CABLE_FIELDS})
    alpha["CableMaterial"] = "Aluminum"
    beta = dict(alpha, **{f: "B-" + f for f in kernel().CABLE_FIELDS})
    beta["CableMaterial"] = "Copper"
    value = create(graph, "Alpha", current_settings=alpha)
    value = create(value, "Beta")
    before = copy.deepcopy(value["settings"])
    after = swap(value, "A", current_settings=beta)
    for field in kernel().CABLE_FIELDS:
        assert alpha[field] != beta[field]
        assert preset(after, "B")[field] == beta[field]
        assert store_of(after)["current_settings"][field] == alpha[field]
    assert after["settings"] == before                               # no cable field on the graph
    restored = swap(after, "B")
    for field in kernel().CABLE_FIELDS:
        assert preset(restored, "A")[field] == alpha[field]
        assert store_of(restored)["current_settings"][field] == beta[field]
    assert restored["settings"] == before


# --------------------------------------------------------------------------- refusals --

def long_layer(value, size=257):
    value = copy.deepcopy(value)
    value["settings"]["string_layer"] = "x" * size
    return value


def test_design_presets_apply_graph_layer_the_store_cannot_hold(graph):
    value = long_layer(create(graph, "Alpha", current_settings=snapshot_current()))
    assert refusal(value, {"subcommand": "Create", "name": "Beta"}) == \
        "DESIGN_PRESET_GRAPH_SETTINGS_OUT_OF_RANGE"
    assert listed(value)["current_settings"]["StringLayer"] == "x" * 257
    # Supplied settings are the drawing's settings at that moment: they are written through.
    after = create(value, "Beta", current_settings=snapshot_current())
    assert layers(after) == SNAPSHOT_LAYERS
    # 256 characters is within the store's bound.
    held_value = long_layer(create(graph, "Alpha", current_settings=snapshot_current()), 256)
    assert preset(create(held_value, "Beta"), "B")["StringLayer"] == "x" * 256


def test_design_presets_apply_refusal_order(graph):
    value = long_layer(create(graph, "Alpha", current_settings=snapshot_current()))
    stale = dict({"subcommand": "Create", "name": "Beta"}, expected_rev=value["rev"] + 1)
    with pytest.raises(GraphValidationError) as caught:
        commit_builtin().run(copy.deepcopy(value), stale)
    assert caught.value.code == "STALE_GRAPH_REVISION"
    broken = copy.deepcopy(value)
    broken["extra"]["design_profiles"]["schema"] = "leaf.solar-design-presets.v2"
    assert refusal(broken, {"subcommand": "Create", "name": "Beta"}) == "DESIGN_PRESETS_STORE_INVALID"
    assert refusal(value, {"subcommand": "Create", "name": "Alpha"}) == \
        "DESIGN_PRESET_GRAPH_SETTINGS_OUT_OF_RANGE"                   # before DESIGN_PRESET_NAME_EXISTS


def test_design_presets_readiness_with_an_out_of_range_graph_layer(graph):
    value = two_presets(graph)
    value["settings"]["home_run_layer"] = "x" * 257
    before = copy.deepcopy(value)
    assert validate_graph(value) == value
    readiness = availability.w1_graph_readiness(value)
    assert readiness[TOOL]["input_ready"] is True
    assert readiness[LIST]["input_ready"] is True
    assert listed(value)["current_settings"]["HomeRunLayer"] == "x" * 257
    assert value == before
    requests = ({"subcommand": "Create", "name": "Gamma"},
                {"subcommand": "Swap", "prefix": "A"},
                {"subcommand": "Delete", "prefix": "A"})
    for params in requests:
        with pytest.raises(GraphValidationError) as caught:
            commit_builtin().run(value, dict(params, expected_rev=value["rev"]))
        assert caught.value.code == "DESIGN_PRESET_GRAPH_SETTINGS_OUT_OF_RANGE"
        assert value == before
    supplied = snapshot_current()
    assert all(len(item) <= 256 for item in supplied.values() if isinstance(item, str))
    for params in requests:
        after = commit_builtin().run(value, dict(params, expected_rev=value["rev"],
                                                 current_settings=supplied))
        assert after["rev"] == value["rev"] + 1
        assert validate_graph(after) == after
        assert value == before


def test_design_presets_apply_is_pure(graph):
    value = two_presets(graph)
    before = copy.deepcopy(value)
    first = commit_builtin().run(value, {"expected_rev": value["rev"], "subcommand": "Swap", "prefix": "A"})
    second = commit_builtin().run(value, {"expected_rev": value["rev"], "subcommand": "Swap", "prefix": "A"})
    assert value == before
    assert first == second and first is not second
    first["settings"]["string_layer"] = "mutated"
    assert second["settings"]["string_layer"] == "String"


# ------------------------------------------------------------------------------- rail --

def test_design_presets_apply_on_the_rail(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    with held(backend) as fence:
        first = dispatch(backend, fence, TOOL, {"expected_rev": 0, "subcommand": "Create", "name": "Alpha",
                                               "current_settings": snapshot_current()})
        assert first["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}

        def run(params, version, job):
            return solar_local_graph.run_local_graph_commit(
                backend, "fixture-tenant", TOOL, dict(params, drawing_id="solar"), drawing_id="solar",
                source_version=version, holder="fixture-owner", fence=fence, job_id=job)

        run({"expected_rev": 1, "subcommand": "Create", "name": "Beta"}, 2, "apply-beta")
        run({"expected_rev": 2, "subcommand": "Swap", "prefix": "A", "current_settings": beta_settings()},
            3, "apply-swap-a")
        fourth = run({"expected_rev": 3, "subcommand": "Swap", "prefix": "B"}, 4, "apply-swap-b")
        assert fourth["new_version"] == {"drawing_id": "solar", "version": 5, "parent": 4}
        assert latest(backend) == 5
    head = head_graph(backend)
    assert layers(head) == GRAPH_BETA
    assert (head["rev"], head["settings"]["rev"]) == (4, 4)
    assert store_of(head)["record"]["ActivePrefix"] == "B"
