"""Design presets and the drawing's installation design (sf-w2-design-presets-installation).

The plugin's SyncFromGlobalSettings always overwrites the drawing's InstallationDesign from
Settings.Default (DrawingPropertiesJson.cs:611), so a LEAFPROFILE Swap to a preset of the other
design changes the drawing's design. In the plugin each panel group keeps its own design, so a
drawing may hold both; the graph may not: every frame carries the project's design
(INSTALLATION_DESIGN_MISMATCH), a Ground frame must carry a tracker and a Roof frame must not, so no
frame converts. The frozen policy (server/solar_preset_sync.py): a preset whose resolved design
equals the drawing's changes nothing; on a drawing with no frame it sets
project.installation_design through the project-change rule (server/solar_project.py
set_installation_design); on a drawing with any frame the commit is refused with
DESIGN_PRESET_INSTALLATION_POPULATED and nothing is written. The design is read back from the graph
before every commit, like the three layer names.

Covered: the constants against the graph contract; the plugin's text resolution; every refusal on a
populated Roof and a populated Ground drawing, with the input untouched; matching designs commit and
change no entity; a drawing with no frame takes the preset's design both ways through Create, Swap
and the delete of the active preset, with the changed entities and digests measured; the read back
into Create and List; the refusal order; set_installation_design's own guards; apply_project_changes
unchanged by the extraction of its rule; the rail. Inputs are authored here or are committed
evidence, so every count and digest is exact.
"""
import copy
import json
from pathlib import Path

import pytest

import solar_local_graph
import solar_local_read
import solar_preset_sync
import solar_project
from solar_design_graph import GraphValidationError, entities, validate_graph
from solar_sizing_client import digest
from test_solar_ground_graph import ground_of
from test_solar_w2_registration import dispatch, head_graph, latest
from test_w1_design_graph import graph  # noqa: F401
from test_w1_local_graph_adapter import held
from test_w1_solve_commit import seed

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = ROOT / "docs/parity/evidence/ground/generate/profile-settings.json"
SCHEMA = ROOT / "contract/solar-design-graph.v1.schema.json"
TOOL = "solar-design-presets"
LIST = "solar-design-presets-list"
POPULATED = "DESIGN_PRESET_INSTALLATION_POPULATED"
# The W1 fixture's layer names, so a preset that carries them changes no drawing setting and only
# the design rule is exercised.
FIXTURE_LAYERS = {"StringLayer": "Strings", "HomeRunLayer": "Homeruns", "PanelGroupLayer": "Groups"}
SCHEDULE_ID = "leaf:schedule:00000000-0000-4000-8000-000000000001"
# Measured with python -B (solar_sizing_client.digest, canonical sha256) on the bare fixture.
BARE_CREATE_STEPS = [
    (1, "Roof", 0, 0, 0, "7da2dddf2eb69e52b64296d7b33048cd0c4a5a2336542d43d4a17d5a0b08e799"),
    (2, "Ground", 2, 2, 2, "761120de1a524fec84715f92f709e99d2b6846f2c249fd2462f2b240b1803f76"),
    (3, "Roof", 3, 2, 2, "8315f79fc33ca74b7a2e7df9ec740bc832378dd5d1d05888daa2d41058a1775e"),
]
BARE_SWAP_STEPS = [
    (1, "Ground", "f023fb2035957d77dff5e879ff705cea0859deb6a6e26fe0a00a00bf36c5f9b8"),
    (2, "Roof", "7b943dbe6b264d9ac7b9fb11dd5d58ac46d6b6c83398769dd98691a9e9dc78ea"),
    (3, "Ground", "f0f9cb219e4af1a7231e1d14bc3ce5a3adbf4e61870ae86800ac55b4da85064f"),
]
BARE_DELETE_DIGEST = "39a21315356d971e6d4212d9ad0f4781878221837c36df77cc516f53304bd3b8"
PROJECT_CHANGES_DIGEST = "eaa8ce23f2b9d1cc1a7dd8c3f4681cacb38841cb9598b85dd3bce8d19492b6e0"


def commit_builtin():
    return solar_local_graph._load_builtin(TOOL)


def list_builtin():
    return solar_local_read._load_builtin(LIST)


def kernel():
    return commit_builtin().preset_store.profiles


def settings_of(design, **patch):
    """The committed snapshot's current settings with InstallationDesign `design` (any text) and
    the fixture's three layer names."""
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    return dict(kernel().current_settings_from_preset(snapshot), **FIXTURE_LAYERS,
                InstallationDesign=design, **patch)


def commit(value, params):
    return commit_builtin().run(copy.deepcopy(value), dict(params, expected_rev=value["rev"]))


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


def preset(value, prefix):
    return next(p["Settings"] for p in store_of(value)["record"]["Profiles"] if p["Prefix"] == prefix)


def with_store(value, designs, active, current_design):
    """`value` with a preset store written through the kernel and the store module directly: one
    preset per design in order (A, B, ...), each the snapshot's settings with that
    InstallationDesign, `active` the active prefix, the store's current settings carrying
    `current_design`. The Create path is proven in test_solar_tool_design_presets.py; a kernel
    Create saves the active preset's state first, so mixed designs are set on the records."""
    value = copy.deepcopy(value)
    builtin = commit_builtin()
    manager = kernel().DesignProfileManager()
    for index, _ in enumerate(designs):
        manager.create_profile(f"P{index}", False, settings_of("Roof"), created_utc=builtin._UNRECORDED)
    for profile, design in zip(manager.profiles, designs):
        profile.settings["InstallationDesign"] = design
    manager.active_prefix = active
    builtin.preset_store.save(value, settings_of(current_design), manager)
    return validate_graph(value)


def bare(value):
    """The W1 fixture with no frame: no zone, panel, string, inverter or route. Its one schedule
    stays, a derived output the design change must stale. Measured: validate_graph accepts it."""
    value = copy.deepcopy(value)
    for key in ("electrical_zones", "frames", "panels", "strings", "inverters", "routes"):
        value[key] = []
    return validate_graph(value)


def changed_ids(monkeypatch):
    """Capture the entity ids each commit hands to advance."""
    builtin = commit_builtin()
    original = builtin.advance
    seen = []

    def capture(result, changed, tool):
        seen.append([entity["id"] for entity in changed])
        return original(result, changed, tool)

    monkeypatch.setattr(builtin, "advance", capture)
    return seen


# ------------------------------------------------------------------------- constants --

def test_presets_installation_constants():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert solar_preset_sync.INSTALLATION_POPULATED == POPULATED
    assert solar_project.INSTALLATION_DESIGNS == ("Roof", "Ground")
    assert set(solar_project.INSTALLATION_DESIGNS) == \
        set(schema["$defs"]["project"]["properties"]["installation_design"]["enum"]) == \
        set(schema["$defs"]["frame"]["properties"]["installation_design"]["enum"])


@pytest.mark.parametrize("text, design", [
    ("Roof", "Roof"), ("Ground", "Ground"), ("roof", "Ground"), ("ROOF", "Ground"), ("", "Ground"),
    (" Roof", "Ground"), ("Roof ", "Ground"), ('"' * 256, "Ground"), ("Tracker", "Ground"),
])
def test_presets_installation_resolution(text, design):
    # BranchCmdCore.SetInstallationDesign: anything but exactly "Ground" or "Roof" is "Ground";
    # BranchSettings.ApplyProjectPreset: every value but "Roof" is Ground.
    assert solar_preset_sync.installation_design(text) == design


# ------------------------------------------------------------------ populated drawings --

def roof(graph):
    return copy.deepcopy(graph)


CASES = [
    ("roof-create-ground", roof, None, {"subcommand": "Create", "name": "Alpha",
                                         "current_settings": ("Ground",)}),
    ("roof-create-lowercase", roof, None, {"subcommand": "Create", "name": "Alpha",
                                            "current_settings": ("roof",)}),
    ("roof-create-empty-text", roof, None, {"subcommand": "Create", "name": "Alpha",
                                             "current_settings": ("",)}),
    ("roof-swap-ground", roof, (("Roof", "Ground"), "A", "Roof"), {"subcommand": "Swap", "prefix": "B"}),
    ("roof-delete-active", roof, (("Roof", "Ground"), "A", "Roof"), {"subcommand": "Delete", "prefix": "A"}),
    ("roof-create-over-ground-store", roof, (("Roof",), "A", "Roof"),
     {"subcommand": "Create", "name": "Beta", "current_settings": ("Ground",)}),
    ("ground-create-roof", ground_of, None, {"subcommand": "Create", "name": "Alpha",
                                              "current_settings": ("Roof",)}),
    ("ground-swap-roof", ground_of, (("Ground", "Roof"), "A", "Ground"), {"subcommand": "Swap", "prefix": "B"}),
]


def build(graph, shape, stocked, params):
    value = shape(graph)
    if stocked is not None:
        value = with_store(value, *stocked)
    params = dict(params)
    if "current_settings" in params:
        params["current_settings"] = settings_of(params["current_settings"][0])
    return value, params


@pytest.mark.parametrize("case, shape, stocked, params", CASES, ids=[row[0] for row in CASES])
def test_presets_installation_populated_refusals(graph, case, shape, stocked, params):
    value, params = build(graph, shape, stocked, params)
    assert value["frames"]
    assert refusal(value, params) == POPULATED


def test_presets_installation_matching_design_changes_nothing(graph, monkeypatch):
    seen = changed_ids(monkeypatch)
    after = commit(graph, {"subcommand": "Create", "name": "Alpha", "current_settings": settings_of("Roof")})
    ground = ground_of(graph)
    on_ground = commit(ground, {"subcommand": "Create", "name": "Alpha", "current_settings": settings_of("")})
    assert seen == [[], []]
    assert entities(after) == entities(graph)
    assert entities(on_ground) == entities(ground)
    # Supplied text is stored as given; the drawing's design is what List reports.
    assert store_of(on_ground)["current_settings"]["InstallationDesign"] == ""
    assert listed(on_ground)["current_settings"]["InstallationDesign"] == "Ground"


def test_presets_installation_swap_saves_the_supplied_outgoing_state(graph):
    # Supplied settings on a Swap are the outgoing preset's state; the applied preset is the target.
    value = with_store(graph, ("Roof", "Roof"), "A", "Roof")
    after = commit(value, {"subcommand": "Swap", "prefix": "B", "current_settings": settings_of("Ground")})
    assert after["project"] == value["project"]
    assert preset(after, "A")["InstallationDesign"] == "Ground"
    assert store_of(after)["current_settings"]["InstallationDesign"] == "Roof"


# ------------------------------------------------------------------- the bare drawing --

def test_presets_installation_bare_fixture_shape(graph):
    value = bare(graph)
    assert value["project"]["installation_design"] == "Roof"
    assert [entity["kind"] for entity in entities(value)] == ["project", "settings", "schedule"]
    assert value["schedules"][0]["id"] == SCHEDULE_ID
    assert value["settings"]["global_string_sizing_confirmed"] is True


def test_presets_installation_bare_create_both_ways(graph, monkeypatch):
    seen = changed_ids(monkeypatch)
    value = bare(graph)
    steps = []
    for name, design in (("Alpha", "Roof"), ("Beta", "Ground"), ("Gamma", "Roof")):
        value = commit(value, {"subcommand": "Create", "name": name, "current_settings": settings_of(design)})
        assert validate_graph(value) == value
        steps.append((value["rev"], value["project"]["installation_design"], value["project"]["rev"],
                      value["settings"]["rev"], value["schedules"][0]["rev"], digest(value)))
    assert steps == BARE_CREATE_STEPS
    project_id, settings_id = value["project"]["id"], value["settings"]["id"]
    # Alpha matches; Beta flips (project, settings and the schedule); Gamma flips back (project only:
    # the settings already hold the reset values and the schedule keeps its first cause).
    assert seen == [[], [project_id, settings_id, SCHEDULE_ID], [project_id]]
    settings = value["settings"]
    assert settings["global_string_sizing_confirmed"] is False
    assert settings["voc_cold"] == solar_project.SEED_VOC_COLD
    assert value["schedules"][0]["validity"] == {"state": "stale", "reasons": ["project_changed"]}
    assert value["project"]["provenance"]["last_writer"] == TOOL
    assert value["project"]["validity"] == bare(graph)["project"]["validity"]
    # Create saves the active preset's state first (the plugin's CreateProfile), so Beta's supplied
    # Ground was captured into Alpha and Gamma's Roof into Beta.
    assert [preset(value, p)["InstallationDesign"] for p in ("A", "B", "C")] == ["Ground", "Roof", "Roof"]


def test_presets_installation_create_recomputes_project_validity(graph):
    value = bare(graph)
    value["project"]["validity"] = {"state": "unknown", "reasons": ["project_zip_required"]}
    assert validate_graph(value) == value
    after = commit(value, {"subcommand": "Create", "name": "Alpha",
                           "current_settings": settings_of("Ground")})
    assert after["project"]["validity"] == {"state": "valid", "reasons": []}
    assert after["project"]["installation_design"] == "Ground"
    assert digest(after) == "1b8a92a0ef9da44f4094056762bd9c85f52bf45510d6b5847b47de9d8dbcb392"


def test_presets_installation_bare_swap_both_ways(graph):
    value = with_store(bare(graph), ("Roof", "Ground"), "A", "Roof")
    steps = []
    for prefix in ("B", "A", "B"):
        value = commit(value, {"subcommand": "Swap", "prefix": prefix})
        steps.append((value["rev"], value["project"]["installation_design"], digest(value)))
    assert steps == BARE_SWAP_STEPS
    # Each outgoing preset is captured with the drawing's design read back from the graph.
    assert (preset(value, "A")["InstallationDesign"], preset(value, "B")["InstallationDesign"]) == \
        ("Roof", "Ground")


def test_presets_installation_bare_delete_active(graph):
    value = with_store(bare(graph), ("Roof", "Ground"), "A", "Roof")
    after = commit(value, {"subcommand": "Delete", "prefix": "A"})
    assert store_of(after)["record"]["ActivePrefix"] == "B"
    assert after["project"]["installation_design"] == "Ground"
    assert digest(after) == BARE_DELETE_DIGEST


# ---------------------------------------------------------------------- the read back --

def test_presets_installation_read_back(graph):
    # The store's current settings say Ground on a Roof drawing: what main left after a Ground
    # snapshot was supplied to the Roof fixture before this rule.
    value = with_store(graph, ("Roof",), "A", "Ground")
    assert store_of(value)["current_settings"]["InstallationDesign"] == "Ground"
    stored = store_of(value)["current_settings"]
    assert solar_preset_sync.effective_current(value, stored)["InstallationDesign"] == "Roof"
    assert listed(value)["current_settings"]["InstallationDesign"] == "Roof"
    after = commit(value, {"subcommand": "Create", "name": "Gamma"})
    assert after["project"] == value["project"]
    assert preset(after, "A")["InstallationDesign"] == "Roof"
    assert preset(after, "B")["InstallationDesign"] == "Roof"
    assert store_of(after)["current_settings"]["InstallationDesign"] == "Roof"


# ---------------------------------------------------------------------- refusal order --

def test_presets_installation_refusal_order(graph):
    value = with_store(graph, ("Roof",), "A", "Roof")
    ground = settings_of("Ground")
    with pytest.raises(GraphValidationError) as caught:
        commit_builtin().run(copy.deepcopy(value), {"expected_rev": value["rev"] + 1, "subcommand": "Create",
                                                    "name": "Beta", "current_settings": ground})
    assert caught.value.code == "STALE_GRAPH_REVISION"
    assert refusal(value, {"subcommand": "Create", "name": "Beta",
                           "current_settings": settings_of("Ground", NumMppt=-1)}) == \
        "DESIGN_PRESET_SETTINGS_OUT_OF_RANGE"
    assert refusal(value, {"subcommand": "Create", "name": " p0 ", "current_settings": ground}) == \
        "DESIGN_PRESET_NAME_EXISTS"
    assert refusal(value, {"subcommand": "Swap", "prefix": "A", "current_settings": ground}) == \
        "DESIGN_PRESET_SWAP_NEEDS_TWO"
    assert refusal(value, {"subcommand": "Create", "name": "Beta", "current_settings": ground}) == POPULATED


# ------------------------------------------------------------- set_installation_design --

def test_presets_installation_set_design_guards(graph):
    value = copy.deepcopy(graph)
    for design in ("roof", "", None, 1):
        with pytest.raises(GraphValidationError) as caught:
            solar_project.set_installation_design(value, design)
        assert caught.value.code == "INVALID_PROJECT_REQUEST"
    with pytest.raises(GraphValidationError) as caught:
        solar_project.set_installation_design(value, "Ground")
    assert caught.value.code == "INSTALLATION_DESIGN_MISMATCH"
    assert solar_project.set_installation_design(value, "Roof") is False
    assert value == graph
    empty = bare(graph)
    assert solar_project.set_installation_design(empty, "Ground") is True
    assert empty["project"]["installation_design"] == "Ground"
    assert validate_graph(empty) == empty


@pytest.mark.parametrize("change", [
    {"installation_design": "Ground"},
    {"installation_design": "Roof"},
    {"zip_code": "44224", "installation_design": "Ground"},
])
def test_presets_installation_project_changes_refuse_installation_design(graph, change):
    g = bare(graph)
    before = digest(g)
    with pytest.raises(GraphValidationError) as caught:
        solar_local_graph._load_builtin("solar-settings").run(
            copy.deepcopy(g), {"expected_rev": 0, "project_changes": change})
    assert caught.value.code == "INVALID_PROJECT_REQUEST"
    assert digest(g) == before


def test_presets_installation_project_changes_unchanged(graph):
    # The project-change rule moved into invalidate_project_dependents; a solar-settings project edit
    # is byte-identical to the base (digest measured on cf876b03 before the change).
    after = solar_local_graph._load_builtin("solar-settings").run(
        copy.deepcopy(graph), {"expected_rev": 0, "project_changes": {"zip_code": "44224"}})
    assert digest(after) == PROJECT_CHANGES_DIGEST


# --------------------------------------------------------------------------- the rail --

def test_presets_installation_on_the_rail(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    with held(backend) as fence:
        with pytest.raises(GraphValidationError, match=POPULATED):
            dispatch(backend, fence, TOOL, {"expected_rev": 0, "subcommand": "Create", "name": "Alpha",
                                           "current_settings": settings_of("Ground")})
        assert latest(backend) == 1
        first = dispatch(backend, fence, TOOL, {"expected_rev": 0, "subcommand": "Create", "name": "Alpha",
                                               "current_settings": settings_of("Roof")})
        assert first["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
    head = head_graph(backend)
    assert head["project"]["installation_design"] == "Roof"
    assert store_of(head)["current_settings"]["InstallationDesign"] == "Roof"
