"""A drawing-settings change stales the outputs built under the old settings.

solar-settings and a design preset commit (through SyncFromGlobalSettings) both change
graph["settings"]. On a graph whose frames carry no solve digest the export guard
(solar_solve_results.require_current_export) could not see such a change, so it still called the
homeruns and the schedule current. The rule (server/solar_settings_invalidation.py): a change to
any editable setting except string_number stales every homerun route and every schedule with the
reason "settings_changed", and nothing else.

Covered: the rule table; the gap on the fixture graph (a routed graph with no solve digest); every
material field; the controls that change no validity (string_number, an equal value, cancel);
a numeric representation change; an entity already stale; a project change in the same request;
feeders, trenches, strings and inverters left valid on the typed topology fixture; agreement with
the dependency index; the electrical schedules export and its readiness hook, on the fixture and on
the recorded i7 design (2345 panels, 173 strings, 22 inverters, 346 homerun legs); preset Create
and the seed rule through the same rule; recovery after sizing evidence is restored; one
published revision and the reopened head on the rail, for both writers; purity.
"""
import copy
from collections import Counter

import pytest

import solar_local_graph
import solar_local_read
import solar_settings_invalidation as invalidation
from solar_dependencies import affected_entities
from solar_design_graph import GraphValidationError, entities
from solar_sizing_client import digest, require_sizing, sizing_basis
from solar_solve_results import HOMERUN_KINDS, SETTINGS_FIELDS, require_current_export
from test_solar_tool_electrical_schedules import i7  # noqa: F401
from test_solar_w2_registration import dispatch, head_graph, latest
from test_w1_design_graph import graph  # noqa: F401
from test_w1_equipment import case, equipment, licensed  # noqa: F401
from test_w1_local_graph_adapter import held
from test_w1_sizing_groups import confirm, passing, service, sizing_params  # noqa: F401
from test_w1_solve_commit import seed
import test_solar_ground_route_kinds as route_kinds
from test_solar_ground_topology import topology_of

STALE = {"state": "stale", "reasons": ["settings_changed"]}
VALID = {"state": "valid", "reasons": []}
NOT_CURRENT = "SOLAR_OUTPUT_NOT_CURRENT"
MATERIAL = ("panel_layer_contains", "panel_group_layer", "string_layer", "home_run_layer",
            "panels_in_sequence", "num_mppt", "strings_per_mppt", "optimizer_ratio",
            "use_l2_collectors", "panel_group_number", "inverter_number", "mppt_letter")
# One new value per field on the fixture graph (settings: "Panels", "Groups", "Strings", "Homeruns",
# 2, 1, 2, 1, false, 2, 2, "A"), and the canonical sha256 of the graph after that one edit.
FIELD_EDITS = {
    "panel_layer_contains": ("PV", "cb98c1ca9b1e4485a1e23133f3e6a949a15dfcf74d521b89e554736f40c13fca"),
    "panel_group_layer": ("G2", "8cfa1f7ed0ee282a3ef310af1d2e39e5bdd5ed86fa7e648f6852f78340682d17"),
    "string_layer": ("S2", "9816232024305cc5db410403fa4923fbe9f9caf0b8ed827942de2b4ccd69879a"),
    "home_run_layer": ("H2", "62ec0bb265f096d8ce79f6b20265b5a2bd0a4499ded4823685de37c16cdcdd64"),
    "panels_in_sequence": (3, "62c25a4225ce98bf159db6ec043bb0496e82623807351a532cc8a645f085700c"),
    "num_mppt": (2, "be24d1c48beaf59671c6041db3137f43a2db870ffd6ce45ecc690a613e3f70bd"),
    "strings_per_mppt": (3, "7114763cd70712c263ee4a704e6d7beedb1963e1bb7b58baab554992c71852cd"),
    "optimizer_ratio": (2, "e5e761d8a13d7bb5a4e5960058572b501aa67f1858ad035b9e46797dbff45f7e"),
    "panel_group_number": (5, "bc906e193e6f78bb40ac4d115c6d13ef73032637e44599f85915fe991871e033"),
    "inverter_number": (5, "6abe0b8cf42132978e1121eb5f441fd73359c2f207e315647ace8f1e8d09621f"),
    "mppt_letter": ("B", "35ca633c4e426bfa3cbcd1d9bb55197b504829c4b9e0aae0e8fb118817d6d85c"),
}
STRING_NUMBER_SHA256 = "2a560acd24aa197222bfe645e18290dcb77ffa72cafb885d915f549a9ec12170"
EQUAL_VALUE_SHA256 = "fabe3701a3f73109ac57776834b9f5e70e2365f65b980c102a6d16e8c4f211f0"
TOPOLOGY_SHA256 = "8c792048449e139cc13e575e7016ff49648bfaa23c407bc2b562b8d7a5b4adf1"
TOPOLOGY_EDIT_SHA256 = "38943b127883e29e584e63b1f6f027de846a3d654e2d39cba9d2a6b0736b6b9d"
I7_SHA256 = "0a030c62567ab225705f6c003dc1058936500d3b45b8efca16103d67fc9acf31"
I7_EDIT_SHA256 = "7f1e049e53e43db5ffb72652c009bac725e569def16f0116a3618b5226ca58c8"
SNAPSHOT_LAYERS = {"StringLayer": "String", "HomeRunLayer": "HomeRun", "PanelGroupLayer": "Panel Group"}
FIXTURE_LAYERS = {"StringLayer": "Strings", "HomeRunLayer": "Homeruns", "PanelGroupLayer": "Groups"}


def settings_builtin():
    return solar_local_graph._load_builtin("solar-settings")


def presets_builtin():
    return solar_local_graph._load_builtin("solar-design-presets")


def schedules_builtin():
    return solar_local_read._load_builtin("solar-electrical-schedules")


def homeruns_builtin():
    return solar_local_graph._load_builtin("solar-homeruns")


def edit(value, changes, **extra):
    return settings_builtin().run(copy.deepcopy(value),
                                  dict({"expected_rev": value["rev"], "changes": changes}, **extra))


def snapshot_current():
    """The committed parity snapshot's current settings (test_solar_tool_design_presets_apply)."""
    from test_solar_tool_design_presets_apply import snapshot_current as current
    return current()


def preset(value, params):
    return presets_builtin().run(copy.deepcopy(value), dict(params, expected_rev=value["rev"]))


def validity(value):
    return [(e["kind"], e.get("route_kind"), e["validity"]) for e in entities(value)
            if e["kind"] in ("string", "inverter", "route", "schedule")]


def export_code(value):
    try:
        require_current_export(value)
    except GraphValidationError as error:
        return error.code
    return "current"


def moved(before, after):
    old = {e["id"]: e for e in entities(before)}
    return [(e["kind"], e["validity"]) for e in entities(after) if e != old[e["id"]]]


# ---------------------------------------------------------------------------- the rule --

def test_settings_invalidation_rule_table():
    assert invalidation.REASON == "settings_changed"
    assert invalidation.MATERIAL_SETTINGS == MATERIAL
    assert set(MATERIAL) == SETTINGS_FIELDS - {"string_number"}
    assert set(MATERIAL) == settings_builtin().EDITABLE - {"string_number"}
    assert HOMERUN_KINDS == ("start homerun", "end homerun")


def test_settings_invalidation_material_changes_and_dependents(graph):
    after = copy.deepcopy(graph["settings"])
    assert invalidation.material_changes(graph["settings"], after) == ()
    after.update(string_number=9, global_string_sizing_confirmed=False)
    assert invalidation.material_changes(graph["settings"], after) == ()
    after.update(mppt_letter="B", num_mppt=2)
    assert invalidation.material_changes(graph["settings"], after) == ("num_mppt", "mppt_letter")
    after.update(num_mppt=1, optimizer_ratio=1.0)
    assert invalidation.material_changes(graph["settings"], after) == ("optimizer_ratio", "mppt_letter")
    assert [e["kind"] for e in invalidation.settings_dependents(graph)] == ["route", "schedule"]
    value = copy.deepcopy(graph)
    assert invalidation.invalidate_settings_dependents(value, graph["settings"]) == []
    assert value == graph
    value["settings"]["num_mppt"] = 2
    changed = invalidation.invalidate_settings_dependents(value, graph["settings"])
    assert [e["id"] for e in changed] == [graph["routes"][0]["id"], graph["schedules"][0]["id"]]
    assert all(e["validity"] == STALE for e in changed)
    assert invalidation.invalidate_settings_dependents(value, graph["settings"]) == []


# ------------------------------------------------------------ solar-settings, the fixture --

def test_settings_invalidation_edit_stales_homeruns_and_schedule(graph):
    assert export_code(graph) == "current"
    assert all(v == VALID for _, _, v in validity(graph))
    before = copy.deepcopy(graph)
    after = edit(graph, {"panels_in_sequence": 3})
    assert graph == before
    assert (after["rev"], after["parent_rev"]) == (1, 0)
    route, schedule = after["routes"][0], after["schedules"][0]
    assert route["route_kind"] == "start homerun"
    assert route["validity"] == schedule["validity"] == STALE
    for item in (route, schedule):
        assert item["rev"] == 1
        assert (item["provenance"]["last_writer"], item["provenance"]["tool_id"],
                item["provenance"]["source_rev"]) == ("solar-settings", "solar-settings", 0)
    for key in ("project", "strings", "inverters", "panels", "frames", "electrical_zones"):
        assert after[key] == graph[key]
    assert export_code(after) == NOT_CURRENT
    assert digest(after) == FIELD_EDITS["panels_in_sequence"][1]


@pytest.mark.parametrize("field", sorted(FIELD_EDITS))
def test_settings_invalidation_every_material_field(graph, field):
    value, sha256 = FIELD_EDITS[field]
    after = edit(graph, {field: value})
    assert moved(graph, after) == [("settings", VALID), ("route", STALE), ("schedule", STALE)]
    assert export_code(after) == NOT_CURRENT
    assert digest(after) == sha256


@pytest.mark.parametrize("entry", ("builtin", "rail"))
def test_settings_invalidation_l2_flag_is_refused_and_writes_nothing(
        graph, tmp_path, monkeypatch, entry):
    # Leaving L2 mode with a combiner box and a central inverter is refused before anything is
    # written (the design-preset rule, sf-w2-settings-l2-collectors); entering it now types the
    # inverters (test_solar_tool_settings_l2_mode.py).
    typed = topology_of(graph)
    before = copy.deepcopy(typed)
    backend, _ = seed(tmp_path, monkeypatch, typed)
    version, head = latest(backend), copy.deepcopy(head_graph(backend))
    with pytest.raises(GraphValidationError) as caught:
        if entry == "builtin":
            settings_builtin().run(typed, {"expected_rev": typed["rev"],
                                           "changes": {"use_l2_collectors": False}})
        else:
            with held(backend) as fence:
                dispatch(backend, fence, "solar-settings",
                         {"expected_rev": typed["rev"], "changes": {"use_l2_collectors": False}})
    assert caught.value.code == "DESIGN_PRESET_L2_EQUIPMENT_PRESENT"
    assert latest(backend) == version
    assert head_graph(backend) == head
    assert typed == before


def test_settings_invalidation_string_number_equal_value_and_cancel_stale_nothing(graph):
    counter = edit(graph, {"string_number": 9})
    assert moved(graph, counter) == [("settings", VALID)]
    assert export_code(counter) == "current"
    assert digest(counter) == STRING_NUMBER_SHA256
    equal = edit(graph, {"panels_in_sequence": 2})
    # Sizing confirmation is still cleared (the settings entity moves), but no output goes stale.
    assert moved(graph, equal) == [("settings", VALID)]
    assert equal["settings"]["global_string_sizing_confirmed"] is False
    assert equal["routes"] == graph["routes"] and equal["schedules"] == graph["schedules"]
    assert export_code(equal) == "current"
    assert digest(equal) == EQUAL_VALUE_SHA256
    cancelled = edit(graph, {"panels_in_sequence": 3}, cancel=True)
    assert cancelled == graph


def test_settings_invalidation_numeric_representation_is_material(graph):
    assert type(graph["settings"]["optimizer_ratio"]) is int
    same = edit(graph, {"optimizer_ratio": 1})
    assert same["routes"] == graph["routes"] and same["schedules"] == graph["schedules"]
    floated = edit(same, {"optimizer_ratio": 1.0})
    assert floated["routes"][0]["validity"] == floated["schedules"][0]["validity"] == STALE


def test_settings_invalidation_keeps_an_earlier_stale_reason(graph):
    graph["schedules"][0]["validity"] = {"state": "stale", "reasons": ["ROUTES_CHANGED"]}
    after = edit(graph, {"num_mppt": 2})
    assert after["schedules"][0] == graph["schedules"][0]              # rev 0, reason kept
    assert after["routes"][0]["validity"] == STALE


def test_settings_invalidation_with_a_project_change(graph):
    material = settings_builtin().run(copy.deepcopy(graph), {
        "expected_rev": 0, "changes": {"num_mppt": 2}, "project_changes": {"zip_code": "37601"}})
    # The project rule runs first and stales every derived entity; its reason stays.
    assert [v for _, _, v in validity(material)] == \
        [{"state": "stale", "reasons": ["project_changed"]}] * 5
    same_project = settings_builtin().run(copy.deepcopy(graph), {
        "expected_rev": 0, "changes": {"num_mppt": 2},
        "project_changes": {"name": graph["project"]["name"]}})
    assert validity(same_project) == [
        ("string", None, VALID), ("string", None, VALID), ("inverter", None, VALID),
        ("route", "start homerun", STALE), ("schedule", None, STALE)]


def test_settings_invalidation_is_pure(graph):
    before = copy.deepcopy(graph)
    first = settings_builtin().run(graph, {"expected_rev": graph["rev"], "changes": {"num_mppt": 2}})
    assert graph == before
    second = settings_builtin().run(graph, {"expected_rev": graph["rev"], "changes": {"num_mppt": 2}})
    assert graph == before
    assert first == second and first is not second


# ------------------------------------------------ what never goes stale, and the index --

def test_settings_invalidation_leaves_feeders_trenches_strings_and_inverters(graph):
    typed = route_kinds.build(graph, "topology", route_kinds._composite)
    assert digest(typed) == TOPOLOGY_SHA256
    assert typed["settings"]["use_l2_collectors"] is True
    assert export_code(typed) == "current"
    after = edit(typed, {"panels_in_sequence": 3})
    assert validity(after) == [
        ("string", None, VALID), ("string", None, VALID), ("inverter", None, VALID),
        ("inverter", None, VALID), ("route", "start homerun", STALE), ("route", "feeder", VALID),
        ("route", "trench", VALID), ("schedule", None, STALE)]
    assert export_code(after) == NOT_CURRENT
    assert digest(after) == TOPOLOGY_EDIT_SHA256
    # Leaving L2 mode with an L2 device on the graph is refused before anything is written.
    with pytest.raises(GraphValidationError) as caught:
        edit(typed, {"use_l2_collectors": False})
    assert caught.value.code == "DESIGN_PRESET_L2_EQUIPMENT_PRESENT"


def test_settings_invalidation_agrees_with_the_dependency_index(graph):
    typed = route_kinds.build(graph, "topology", route_kinds._composite)
    for value in (graph, typed):
        after = edit(value, {"home_run_layer": "H2"})
        affected = set(affected_entities(value, after, [value["settings"]["id"]]))
        expected = {e["id"] for e in entities(after) if e["id"] in affected and (
            e["kind"] == "schedule" or (e["kind"] == "route" and e["route_kind"] in HOMERUN_KINDS))}
        assert {e["id"] for e in invalidation.settings_dependents(after)} == expected
        assert {e["id"] for e in entities(after) if e["validity"] == STALE} == expected


# --------------------------------------------------------- the export and its readiness --

def test_settings_invalidation_electrical_schedules_refuse_after_an_edit(graph):
    hook = schedules_builtin().input_readiness
    assert hook(copy.deepcopy(graph)) == {"input_ready": True, "input_reason": None}
    after = edit(graph, {"home_run_layer": "H2"})
    assert hook(copy.deepcopy(after)) == {"input_ready": False,
                                          "input_reason": "solar_output_not_current"}
    with pytest.raises(GraphValidationError) as caught:
        schedules_builtin().run(copy.deepcopy(after), {})
    assert caught.value.code == NOT_CURRENT


def test_settings_invalidation_recorded_i7_design(i7):
    assert digest(i7) == I7_SHA256
    assert export_code(i7) == "current"
    assert schedules_builtin().input_readiness(copy.deepcopy(i7))["input_ready"] is True
    assert i7["settings"]["home_run_layer"] == "Homeruns"
    after = edit(i7, {"home_run_layer": "HR-2"})
    counts = Counter((e["kind"], e["validity"]["state"]) for e in entities(after))
    assert counts == {("project", "valid"): 1, ("settings", "valid"): 1, ("panel", "valid"): 2345,
                      ("string", "valid"): 173, ("inverter", "valid"): 22, ("route", "stale"): 346}
    assert all(r["route_kind"] in HOMERUN_KINDS and r["validity"] == STALE for r in after["routes"])
    assert (after["rev"], digest(after)) == (2, I7_EDIT_SHA256)
    assert export_code(after) == NOT_CURRENT
    with pytest.raises(GraphValidationError) as caught:
        schedules_builtin().run(copy.deepcopy(after), {})
    assert caught.value.code == NOT_CURRENT


# ------------------------------------------------------------------- preset apply --

def test_settings_invalidation_preset_create_goes_through_the_rule(graph):
    sync = presets_builtin().preset_sync
    value = copy.deepcopy(graph)
    changed = sync.sync(value, dict(snapshot_current()))
    assert [e["kind"] for e in changed] == ["settings", "route", "schedule"]
    assert changed[0] is value["settings"]
    assert value["routes"][0]["validity"] == value["schedules"][0]["validity"] == STALE
    agreeing = copy.deepcopy(graph)
    assert sync.sync(agreeing, dict(snapshot_current(), **FIXTURE_LAYERS)) == []
    assert agreeing == graph
    after = preset(graph, {"subcommand": "Create", "name": "Alpha", "current_settings": snapshot_current()})
    for item in (after["routes"][0], after["schedules"][0]):
        assert item["validity"] == STALE
        assert (item["rev"], item["provenance"]["last_writer"], item["provenance"]["tool_id"],
                item["provenance"]["source_rev"]) == (1, "solar-design-presets", "solar-design-presets", 0)
    assert export_code(after) == NOT_CURRENT
    beta = preset(after, {"subcommand": "Create", "name": "Beta"})        # changes no setting
    assert beta["routes"] == after["routes"] and beta["schedules"] == after["schedules"]


def test_settings_invalidation_preset_seed_alone_is_material(graph):
    zeroed = copy.deepcopy(graph)
    zeroed["settings"].update(panels_in_sequence=0, num_mppt=0, strings_per_mppt=0)
    after = preset(zeroed, {"subcommand": "Create", "name": "Alpha",
                            "current_settings": dict(snapshot_current(), **FIXTURE_LAYERS)})
    assert {k: after["settings"][k] for k in ("panels_in_sequence", "num_mppt", "strings_per_mppt")} == \
        {"panels_in_sequence": 14, "num_mppt": 12, "strings_per_mppt": 2}
    assert after["routes"][0]["validity"] == after["schedules"][0]["validity"] == STALE


def test_settings_invalidation_preset_matching_settings_stale_nothing(graph):
    after = preset(graph, {"subcommand": "Create", "name": "Alpha",
                           "current_settings": dict(snapshot_current(), **FIXTURE_LAYERS)})
    assert after["routes"] == graph["routes"] and after["schedules"] == graph["schedules"]
    assert export_code(after) == "current"


# ------------------------------------------------------------------------ recovery --

def test_settings_invalidation_homeruns_recover_after_sizing_evidence_is_restored(case):
    source, params, intake = case
    assigned = equipment.assign_equipment(source, params, drawing_intake=intake,
                                          licensed_equipment=licensed)["graph"]
    routed = homeruns_builtin().run(assigned, {"expected_rev": assigned["rev"]})
    assert [r["validity"] for r in routed["routes"]] == [VALID] * 4
    assert {r["extra"]["layer"] for r in routed["routes"]} == {"Homeruns"}
    assert require_sizing(routed)
    edited = edit(routed, {"home_run_layer": "HR-2"})
    assert [r["validity"] for r in edited["routes"]] == [STALE] * 4
    with pytest.raises(GraphValidationError) as caught:
        homeruns_builtin().run(copy.deepcopy(edited), {"expected_rev": edited["rev"]})
    assert caught.value.code == "EQUIPMENT_ASSIGNMENT_REQUIRED"         # sizing was cleared
    # The edit changed no sizing input. Restore the fixture's sizing evidence directly before
    # rebuilding the homeruns.
    assert sizing_basis(edited) == sizing_basis(routed)
    resized = copy.deepcopy(edited)
    resized["settings"]["global_string_sizing_confirmed"] = True
    resized["settings"]["extra"]["string_sizing"] = copy.deepcopy(
        routed["settings"]["extra"]["string_sizing"])
    assert require_sizing(resized)
    rerouted = homeruns_builtin().run(resized, {"expected_rev": resized["rev"]})
    assert [r["validity"] for r in rerouted["routes"]] == [VALID] * 4
    assert {r["extra"]["layer"] for r in rerouted["routes"]} == {"HR-2"}
    assert [r["id"] for r in rerouted["routes"]] == [r["id"] for r in routed["routes"]]


# ---------------------------------------------------------------------------- rail --

def test_settings_invalidation_settings_edit_on_the_rail(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    with held(backend) as fence:
        result = dispatch(backend, fence, "solar-settings",
                          {"expected_rev": 0, "changes": {"home_run_layer": "H2"}})
        assert result["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
        assert latest(backend) == 2
    head = head_graph(backend)
    assert head["rev"] == 1
    assert head["routes"][0]["validity"] == head["schedules"][0]["validity"] == STALE
    assert result["graph_sha256"] == digest(head) == FIELD_EDITS["home_run_layer"][1]
    assert export_code(head) == NOT_CURRENT


def test_settings_invalidation_preset_create_on_the_rail(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    with held(backend) as fence:
        result = dispatch(backend, fence, "solar-design-presets",
                          {"expected_rev": 0, "subcommand": "Create", "name": "Alpha",
                           "current_settings": snapshot_current()})
        assert result["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
        assert latest(backend) == 2
    head = head_graph(backend)
    assert head["routes"][0]["validity"] == head["schedules"][0]["validity"] == STALE
    assert head["routes"][0]["provenance"]["last_writer"] == "solar-design-presets"
    assert result["graph_sha256"] == digest(head)
