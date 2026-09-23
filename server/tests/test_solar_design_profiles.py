"""Studio's design-profile port against the plugin source (contract G32).

Covered: NextPrefix (first free letter, the gap after a delete, the "P" fallback past Z);
LEAFPROFILE Create (the trimmed name, the case-insensitive duplicate refusal, the adoption prompt
only for the first profile with its Yes default, the settings copy under the profile's name);
Swap (two profiles needed, the active prefix is no keyword, the outgoing state saved and the
target's settings restored, layers frozen and thawed); Delete (the No default, a non-active and
an active delete, the last profile); List; the record's shape, version, key order and creation
time format; load_record's empty and corrupt paths; FromCurrentSettings' and ApplyToSettings'
null fallbacks and cable-sizing rule; layer resolution and adoption renames; the input bounds.
Every expected value is hand-computed from the cited plugin lines or the captured f1..f5 records.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
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


dp = _load("solar_design_profiles", ROOT / "server" / "solar_design_profiles.py")
SNAPSHOT_PATH = ROOT / "docs" / "parity" / "evidence" / "ground" / "generate" / "profile-settings.json"
T0 = datetime(2026, 9, 23, 20, 43, 30, 295306, tzinfo=timezone.utc)


def snapshot():
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def settings():
    return dp.current_settings_from_preset(snapshot())


def run(mgr, s, *answers, **kw):
    kw.setdefault("created_utc", T0)
    return dp.leafprofile(mgr, s, list(answers), **kw)


def chain():
    """f1 to f3 of the G32 scenario."""
    mgr, s = dp.DesignProfileManager(), settings()
    run(mgr, s, "Create", "Alpha", "No")
    run(mgr, s, "Create", "Beta")
    run(mgr, s, "Swap", "A")
    return mgr, s


def pairs(mgr):
    return [(p.name, p.prefix) for p in mgr.profiles]


# ------------------------------------------------------------ settings --

def test_committed_snapshot_is_a_full_preset_named_alpha():
    snap = snapshot()
    assert list(snap) == list(dp.PRESET_FIELD_NAMES)
    assert dp.validate_preset(snap) is snap
    assert snap["Name"] == "Alpha" and snap["IsBuiltIn"] is False
    assert len(dp.PRESET_FIELDS) == 62 and len(dp.SETTINGS_FIELD_NAMES) == 60


def test_from_current_settings_round_trips_the_snapshot():
    assert dp.preset_from_current_settings(settings(), "Alpha") == snapshot()
    beta = dp.preset_from_current_settings(settings(), "Beta")
    assert list(beta) == list(dp.PRESET_FIELD_NAMES)
    assert beta["Name"] == "Beta" and {k: v for k, v in beta.items() if k != "Name"} == \
        {k: v for k, v in snapshot().items() if k != "Name"}


def test_from_current_settings_null_fallbacks():
    s = settings()
    for field in ("TagFirst", "TagSecond", "TagThird", "TagDelim1", "HomeRunLayer", "OptimizerRatio",
                  "CableTray", "StringLayer"):
        s[field] = None
    preset = dp.preset_from_current_settings(s, "X")
    assert (preset["TagFirst"], preset["TagSecond"], preset["TagThird"], preset["TagDelim1"]) == \
        ("Inv. #", "String #", "MPPT #", ".")
    assert (preset["HomeRunLayer"], preset["OptimizerRatio"], preset["CableTray"]) == ("HomeRun", "1:1", "")
    assert preset["StringLayer"] is None                  # no `??` on StringLayer (ProjectPreset.cs:322)


def test_apply_to_settings_skips_cable_fields_without_a_material():
    s = settings()
    s["CableTray"] = "kept"
    preset = dp.preset_from_current_settings(settings(), "P")
    preset["NumMppt"], preset["CableTray"], preset["TagFirst"] = 3, "tray", None
    dp.apply_to_settings(preset, s)
    assert s["NumMppt"] == 3 and s["CableTray"] == "kept" and s["TagFirst"] is None
    preset["CableMaterial"] = "Cu"
    dp.apply_to_settings(preset, s)
    assert (s["CableMaterial"], s["CableTray"]) == ("Cu", "tray")


@pytest.mark.parametrize("field,value", [("NumMppt", 2.5), ("NumMppt", True), ("Vmp", float("nan")),
                                         ("UseCombinerBox", 1), ("ModuleLayer", 7)])
def test_validate_preset_refuses_a_wrong_kind(field, value):
    bad = snapshot()
    bad[field] = value
    with pytest.raises(dp.ProfileInputError):
        dp.validate_preset(bad)


def test_validate_preset_refuses_a_missing_or_extra_field():
    bad = snapshot()
    del bad["NumPanels"]
    with pytest.raises(dp.ProfileInputError):
        dp.validate_preset(bad)
    with pytest.raises(dp.ProfileInputError):
        dp.validate_preset(dict(snapshot(), Extra=1))
    with pytest.raises(dp.ProfileInputError):
        dp.validate_settings(dict(settings(), Name="x"))


def test_canonical_settings_text_is_sorted_and_compact():
    text = dp.canonical_settings_text(snapshot())
    assert text.startswith('{"AlignmentTolerance":12.0,"AmbientTemperature":"",')
    assert '"TagHeight":15.387500000000001' in text and '"MinTemp":-2.700000047683716' in text
    assert " " not in text.replace("Inv. #", "").replace("String #", "").replace("MPPT #", "") \
        .replace("Sungrow SG-HX SG250HX", "").replace("Panel Group", "").replace("TMEIC NINJA-5.05", "")


# ------------------------------------------------------------ prefixes --

def test_next_prefix_fills_the_first_free_letter():
    mgr, s = dp.DesignProfileManager(), settings()
    for name in ("One", "Two", "Three"):
        run(mgr, s, "Create", name, "No")
    assert [p.prefix for p in mgr.profiles] == ["A", "B", "C"]
    run(mgr, s, "Delete", "B", "Yes")
    assert mgr.next_prefix() == "B"
    run(mgr, s, "Create", "Four")
    assert pairs(mgr) == [("One", "A"), ("Three", "C"), ("Four", "B")]


def test_next_prefix_past_z_is_p_and_the_count_plus_one():
    mgr = dp.DesignProfileManager()
    mgr.profiles = [dp.DesignProfile(str(i), chr(ord("A") + i), None, None) for i in range(26)]
    assert mgr.next_prefix() == "P27"


# --------------------------------------------------------------- create --

def test_f1_create_first_profile_adopt_no():
    mgr, s = dp.DesignProfileManager(), settings()
    layers = {"String": {"frozen": False, "entities": 3}}
    out = run(mgr, s, "Create", "Alpha", "No", layers=layers)
    assert out["consumed"] == 3 and out["saved"] is True
    assert out["messages"] == ["Profile 'Alpha' created (prefix: A).", "  Active profile is now: Alpha"]
    assert pairs(mgr) == [("Alpha", "A")] and mgr.active_prefix == "A"
    assert mgr.profiles[0].settings == snapshot()
    assert list(layers) == ["String"]                    # adopt No renames nothing


def test_create_adoption_defaults_to_yes_and_renames_layers():
    mgr, s = dp.DesignProfileManager(), settings()
    layers = {"string": {"frozen": False, "entities": 2}, "HomeRun": {"frozen": False, "entities": 1},
              "HOMERUN-DEBUG": {"frozen": False, "entities": 0}, "Other": {"frozen": False, "entities": 5}}
    out = run(mgr, s, "Create", "Alpha", layers=layers)   # Enter at the adoption prompt: Yes
    assert out["consumed"] == 2
    # "string" matches "String" case-insensitively (:364); HomeRun is the configured home-run layer
    # (:377); HOMERUN-DEBUG is scoped but not in the adoption list (DesignProfile.cs:41-51).
    assert sorted(layers) == ["A_HomeRun", "A_String", "HOMERUN-DEBUG", "Other"]


def test_create_second_profile_asks_no_adoption_and_becomes_active():
    mgr, s = dp.DesignProfileManager(), settings()
    run(mgr, s, "Create", "Alpha", "No")
    out = run(mgr, s, "Create", "Beta")
    assert out["consumed"] == 2
    assert pairs(mgr) == [("Alpha", "A"), ("Beta", "B")] and mgr.active_prefix == "B"
    assert mgr.profiles[1].settings["Name"] == "Beta"
    assert mgr.profiles[0].settings == snapshot()          # the outgoing profile's state re-saved


def test_create_refuses_a_duplicate_name_case_insensitively():
    mgr, s = dp.DesignProfileManager(), settings()
    run(mgr, s, "Create", "Alpha", "No")
    out = run(mgr, s, "Create", "  ALPHA  ")
    assert out["messages"] == ["Profile 'ALPHA' already exists."] and out["saved"] is False
    assert pairs(mgr) == [("Alpha", "A")]


def test_create_with_a_blank_name_does_nothing():
    mgr, s = dp.DesignProfileManager(), settings()
    assert run(mgr, s, "Create", "   ")["messages"] == []
    assert run(mgr, s, "Create")["messages"] == [] and mgr.profiles == []


# ----------------------------------------------------------------- swap --

def test_swap_needs_two_profiles():
    mgr, s = dp.DesignProfileManager(), settings()
    run(mgr, s, "Create", "Alpha", "No")
    out = run(mgr, s, "Swap", "A")
    assert out["messages"] == ["Need at least 2 profiles to swap. Use LEAFPROFILE Create first."]
    assert out["consumed"] == 1


def test_f3_swap_saves_the_outgoing_state_and_restores_the_target():
    mgr, s = dp.DesignProfileManager(), settings()
    run(mgr, s, "Create", "Alpha", "No")
    run(mgr, s, "Create", "Beta")
    s["NumMppt"] = 7                                      # a setting changed while Beta is active
    layers = {"A_String": {"frozen": True, "entities": 1}, "B_String": {"frozen": False, "entities": 1}}
    out = run(mgr, s, "Swap", "a", layers=layers)
    assert out["messages"] == ["Swapped to profile 'Alpha' (prefix: A)."]
    assert mgr.active_prefix == "A" and s["NumMppt"] == 12
    assert mgr.profiles[1].settings["NumMppt"] == 7       # SaveActiveProfileState (:207)
    assert layers == {"A_String": {"frozen": False, "entities": 1}, "B_String": {"frozen": True, "entities": 1}}


def test_swap_offers_no_keyword_for_the_active_profile():
    mgr, s = dp.DesignProfileManager(), settings()
    run(mgr, s, "Create", "Alpha", "No")
    run(mgr, s, "Create", "Beta")
    with pytest.raises(dp.ProfileInputError):
        run(mgr, s, "Swap", "B")
    with pytest.raises(dp.ProfileInputError):
        mgr.swap_to("Z", s)


def test_swap_to_restores_the_drawing_state():
    mgr, s = dp.DesignProfileManager(), settings()
    props = dict(dp.drawing_state_from(None), StringNumber=9)
    run(mgr, s, "Create", "Alpha", "No", drawing_props=props)
    props["StringNumber"] = 4
    run(mgr, s, "Create", "Beta", drawing_props=props)
    props["StringNumber"] = 5
    run(mgr, s, "Swap", "A", drawing_props=props)
    assert props["StringNumber"] == 4                     # Alpha was re-saved when Beta was created
    assert mgr.profiles[1].drawing_state["StringNumber"] == 5


# --------------------------------------------------------------- delete --

def test_delete_defaults_to_no():
    mgr, s = chain()
    assert run(mgr, s, "Delete", "B")["messages"] == ["Deletion cancelled."]
    assert run(mgr, s, "Delete", "B", "No")["messages"] == ["Deletion cancelled."]
    assert pairs(mgr) == [("Alpha", "A"), ("Beta", "B")]


def test_f5_delete_a_non_active_profile_keeps_the_active_one():
    mgr, s = chain()
    layers = {"B_String": {"frozen": True, "entities": 4}, "A_String": {"frozen": False, "entities": 2}}
    out = run(mgr, s, "Delete", "B", "Yes", layers=layers)
    assert out["messages"] == ["Profile 'Beta' deleted.", "  Active profile is now: Alpha"]
    assert pairs(mgr) == [("Alpha", "A")] and mgr.active_prefix == "A"
    assert list(layers) == ["A_String"]


def test_delete_the_active_profile_hands_over_to_the_first():
    mgr, s = chain()
    run(mgr, s, "Swap", "B")
    mgr.profiles[0].settings["NumMppt"] = 5
    erased = mgr.delete_profile("B", s, layers={"b_x": {"frozen": False, "entities": 6}})
    assert erased == {"layers": ["b_x"], "entities": 6}
    assert mgr.active_prefix == "A" and s["NumMppt"] == 5
    out = run(mgr, s, "Delete", "A", "Yes")
    assert out["messages"] == ["Profile 'Alpha' deleted.", "  No profiles remaining."]
    assert mgr.active_prefix is None and mgr.profiles == []
    assert run(mgr, s, "Delete")["messages"] == ["No profiles to delete."]


# ----------------------------------------------------------------- list --

def test_f4_list_prints_every_profile_and_marks_the_active_one():
    mgr, s = chain()
    before = mgr.record_json()
    out = run(mgr, s, "List")
    assert out["listed"] == [("A", "Alpha"), ("B", "Beta")] and out["saved"] is False
    assert out["messages"][:4] == ["", "  Design Profiles:", "  " + "─" * 37, "  [A] Alpha ◄ ACTIVE"]
    assert out["messages"][4:8] == ["      Module: JA_Solar_JAM72D40-595/MB",
                                    "      Inverter: Sungrow SG-HX SG250HX", "      String length: 14",
                                    "  [B] Beta"]
    assert mgr.record_json() == before


def test_list_without_profiles():
    out = run(dp.DesignProfileManager(), settings(), "List")
    assert out["messages"] == ["No design profiles configured. Use LEAFPROFILE Create to start."]
    assert out["listed"] == []


# --------------------------------------------------------------- record --

def test_record_shape_matches_the_captured_f2_record():
    mgr, s = dp.DesignProfileManager(), settings()
    run(mgr, s, "Create", "Alpha", "No")
    run(mgr, s, "Create", "Beta")
    rec = json.loads(mgr.record_json())
    assert list(rec) == ["Version", "ActivePrefix", "Profiles"]
    assert rec["Version"] == 1 and rec["ActivePrefix"] == "B"
    assert [list(p) for p in rec["Profiles"]] == \
        [["Name", "Prefix", "Settings", "CreatedUtc", "DrawingState", "ReOptResults"]] * 2
    assert rec["Profiles"][0]["CreatedUtc"] == "2026-09-23T20:43:30.295306Z"
    assert rec["Profiles"][0]["DrawingState"] == {
        "StringNumber": 1, "InverterNumber": 1, "MPPTLetter": "a", "PanelGroupNumber": 1, "PanelGroupColour": 0,
        "ElevationZones": [], "ElectricalZones": [], "L1ToL2Assignments": {}, "L1ToL2InputAssignments": {},
        "InverterTypes": {}, "InverterTypeAssignments": {}}
    assert rec["Profiles"][1]["ReOptResults"] is None
    assert mgr.record_json().startswith('{"Version":1,"ActivePrefix":"B","Profiles":[{"Name":"Alpha","Prefix":"A",'
                                        '"Settings":{"Name":"Alpha","IsBuiltIn":false,"InstallationDesign":"Ground",')


def test_created_utc_format():
    assert dp.format_created_utc(datetime(2026, 9, 23, 20, 43, 40, 654260, tzinfo=timezone.utc)) == \
        "2026-09-23T20:43:40.65426Z"
    assert dp.format_created_utc(datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)) == "2026-01-02T03:04:05Z"
    with pytest.raises(dp.ProfileInputError):
        dp.format_created_utc(datetime(2026, 1, 2))


def test_load_record_round_trip_and_the_empty_and_corrupt_paths():
    mgr, _ = chain()
    loaded = dp.load_record(mgr.record_json())
    assert loaded.record() == mgr.record() and loaded.corrupt is False
    empty = dp.load_record(None)
    assert (empty.active_prefix, empty.profiles, empty.corrupt) == (None, [], False)
    for text in ("{not json", "[1]", '{"Profiles":[{"Name":1,"Prefix":"A"}]}'):
        bad = dp.load_record(text)
        assert (bad.profiles, bad.corrupt) == ([], True)
    missing = dp.load_record('{"Version":1,"ActivePrefix":"A"}')
    assert (missing.active_prefix, missing.profiles, missing.corrupt) == ("A", [], False)


def test_rename_profile_renames_its_settings_too():
    mgr, _ = chain()
    mgr.rename_profile("b", "Gamma")
    assert mgr.profiles[1].name == "Gamma" and mgr.profiles[1].settings["Name"] == "Gamma"
    with pytest.raises(dp.ProfileInputError):
        mgr.rename_profile("Q", "x")


def test_scenario_f1_to_f5_matches_the_captured_records():
    mgr, s = dp.DesignProfileManager(), settings()
    seen = []
    for answers in (("Create", "Alpha", "No"), ("Create", "Beta"), ("Swap", "A"), ("List",),
                    ("Delete", "B", "Yes")):
        run(mgr, s, *answers)
        seen.append((mgr.active_prefix, pairs(mgr)))
    ab = [("Alpha", "A"), ("Beta", "B")]
    assert seen == [("A", [("Alpha", "A")]), ("B", ab), ("A", ab), ("A", ab), ("A", [("Alpha", "A")])]
    assert s == settings()


# --------------------------------------------------------------- layers --

def test_layer_resolution():
    s = settings()
    assert dp.resolve_layer_name(None, "String", s) == "String"
    assert dp.resolve_layer_name("A", "string", s) == "A_string"
    assert dp.resolve_layer_name("A", "HomeRun", s) == "A_HomeRun"
    assert dp.resolve_layer_name("A", "Other", s) == "Other"
    assert dp.is_layer_in_active_profile(None, "Other") is True
    assert dp.is_layer_in_active_profile("A", "a_String") is True
    assert dp.is_layer_in_active_profile("A", "B_String") is False


# --------------------------------------------------------------- bounds --

def test_input_bounds():
    mgr, s = dp.DesignProfileManager(), settings()
    with pytest.raises(dp.ProfileInputError):
        run(mgr, s, "Rename")
    with pytest.raises(dp.ProfileInputError):
        run(mgr, s, *(["List"] * (dp.MAX_ANSWERS + 1)))
    with pytest.raises(dp.ProfileInputError):
        run(mgr, s, "Create", "x" * (dp.MAX_NAME_CHARS + 1))
    with pytest.raises(dp.ProfileInputError):
        dp.leafprofile(mgr, dict(s, NumMppt="12"), ["List"])
    with pytest.raises(dp.ProfileInputError):
        run(mgr, s, "List", layers={"A_x": {"frozen": "no"}})
    mgr.profiles = [dp.DesignProfile(str(i), f"P{i}", None, None) for i in range(dp.MAX_PROFILES)]
    with pytest.raises(dp.ProfileInputError):
        mgr.create_profile("One more", False, s, created_utc=T0)
    assert dp.load_record(copy.deepcopy("x" * (dp.MAX_RECORD_CHARS + 1))).corrupt is True


def test_keyword_abbreviations():
    mgr, s = dp.DesignProfileManager(), settings()
    assert run(mgr, s, "c", "Alpha", "n")["subcommand"] == "Create"
    assert mgr.profiles and list(mgr.profiles[0].settings) == list(dp.PRESET_FIELD_NAMES)
    assert run(mgr, s, "LIST")["subcommand"] == "List"
    assert run(mgr, s)["subcommand"] is None              # Enter at the first prompt cancels
