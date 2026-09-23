"""Studio's dialog-batch engines against the plugin (contract G30).

Covered: the .NET number text the dialogs round-trip through ("F1", "0.##", "0.###",
double.TryParse) and Newtonsoft's JSON text (shortest round-trip doubles with ".0", string
escapes, compact and indented layout); the Shading objects form accepted untouched and
PlaceTree's circle, restriction ring and label, equal to the e1 capture; the project-area
manager's Add Area then OK on a drawing without areas, equal to the e4 capture's record byte for
byte, plus the grid round trip on stored areas; and the pile-template manager's "+" then OK on
the committed BEFORE store, whose written file must hash to the captured AFTER store (8,985
bytes, CRLF, no BOM), plus the store's read (Newtonsoft's append-to-default buckets), Normalize,
naming and selection rules. Every refusal path fails closed.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
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


dialogs = _load("solar_ground_dialogs", ROOT / "server" / "solar_ground_dialogs.py")
BEFORE_STORE = ROOT / "docs" / "parity" / "evidence" / "ground" / "terrain" / "pile-templates-before.json"
# The e6 capture's AFTER store, as the plugin wrote it: 8,985 bytes, CRLF, no BOM, no final newline.
AFTER_STORE_BYTES = 8985
AFTER_STORE_SHA256 = "0b8ba49ec4feac4598c8adf6cf612884a5eafcbe68f50e56ced5c5d1f4431a11"
# The e4 capture's project-area record, decoded from the reopened drawing.
CAPTURED_AREA_RECORD = ('{"Version":1,"Areas":[{"Name":"Area 1","SubAreaId":"","BoundaryHandle":null,'
                        '"FramePresetName":"TinyTest","PitchOverrideM":0.0,"ExclusionZoneHandles":[],'
                        '"CachedBounds":null}]}')
DEFAULT_BUCKETS = [0.9144000000000001, 1.2192, 1.524, 1.8288000000000002, 2.1336]


def before_text():
    return BEFORE_STORE.read_bytes().decode("utf-8")


# --------------------------------------------------------- .NET number text --

@pytest.mark.parametrize("value, decimals, text", [
    (8.0, 1, "8.0"), (3.0, 1, "3.0"), (0.25, 1, "0.3"), (2.5, 0, "3"), (-0.25, 1, "-0.3"), (1.005, 2, "1.00"),
])
def test_fixed_format_rounds_the_exact_value_half_away_from_zero(value, decimals, text):
    assert dialogs.format_fixed(value, decimals) == text


@pytest.mark.parametrize("value, decimals, text", [
    (4.5353051145682635, 3, "4.535"), (9.83, 3, "9.83"), (1.0, 3, "1"), (0.0, 2, "0"), (4.0, 2, "4"),
    (0.2, 2, "0.2"), (1.0005, 3, "1.001"), (4.456, 2, "4.46"), (2.1336, 3, "2.134"),
])
def test_custom_format_rounds_fifteen_digits_then_drops_trailing_zeros(value, decimals, text):
    assert dialogs.format_custom_decimals(value, decimals) == text


def test_custom_format_refuses_a_magnitude_it_does_not_port():
    with pytest.raises(dialogs.DialogsInputError):
        dialogs.format_custom_decimals(1e16, 3)


@pytest.mark.parametrize("value, text", [
    (1.0, "1.0"), (0.0, "0.0"), (-0.0, "-0.0"), (0.9144000000000001, "0.9144000000000001"), (4.535, "4.535"),
    (100.0, "100.0"), (1e14, "100000000000000.0"), (1e15, "1E+15"), (1.2345678901234567e20, "1.2345678901234567E+20"),
    (0.0001, "0.0001"), (1e-05, "1E-05"), (-2.5e-07, "-2.5E-07"),
])
def test_newtonsoft_double_is_shortest_round_trip_with_a_decimal_place(value, text):
    assert dialogs.newtonsoft_double(value) == text


def test_newtonsoft_string_escapes():
    assert dialogs.newtonsoft_string('a"b\\c\n\t\x01 é') == '"a\\"b\\\\c\\n\\t\\u0001\\u2028é"'


def test_newtonsoft_layout_compact_and_indented():
    value = {"a": [], "b": {}, "c": [1.0, {"d": None}], "e": True}
    assert dialogs.newtonsoft_json(value) == '{"a":[],"b":{},"c":[1.0,{"d":null}],"e":true}'
    assert dialogs.newtonsoft_json(value, indented=True) == (
        '{\r\n  "a": [],\r\n  "b": {},\r\n  "c": [\r\n    1.0,\r\n    {\r\n      "d": null\r\n    }\r\n  ],'
        '\r\n  "e": true\r\n}')


@pytest.mark.parametrize("text, value", [
    (" 4.535 ", 4.535), ("1e3", 1000.0), ("-.5", -0.5), ("+2.", 2.0), ("abc", None), ("", None), ("1,5", None),
    ("0x10", None),
])
def test_parse_double_is_invariant_float_style(text, value):
    assert dialogs.parse_double(text) == value


def test_parse_double_refuses_the_symbol_spellings():
    for text in ("Infinity", "NaN"):
        with pytest.raises(dialogs.DialogsInputError):
            dialogs.parse_double(text)


# ---------------------------------------------------------- shading object --

def test_the_form_accepts_its_declared_defaults_unchanged():
    accepted = dialogs.shading_form_accept()
    assert accepted == dialogs.shading_defaults()
    assert accepted == {"Kind": "Tree", "TreeTopDiameter": 4.0, "TreeTrunkHeight": 3.0, "TreeTotalHeight": 8.0,
                        "TreeRestrictionOffset": 1.0, "StationLength": 5.0, "StationWidth": 3.0,
                        "StationHeight": 3.0, "StationRestrictionOffset": 1.0, "FenceHeight": 2.0,
                        "FenceWidth": 0.2, "VegetationHeight": 12.0}


def test_the_form_round_trip_keeps_two_places():
    initial = dict(dialogs.shading_defaults(), TreeTopDiameter=4.456, FenceWidth=0.125)
    accepted = dialogs.shading_form_accept(initial)
    assert accepted["TreeTopDiameter"] == 4.46 and accepted["FenceWidth"] == 0.13


def test_form_values_carry_the_g30a_snake_case_keys_in_load_order():
    values = dialogs.shading_form_values(dialogs.shading_form_accept())
    assert list(values) == ["tree_top_diameter", "tree_trunk_height", "tree_total_height",
                            "tree_restriction_offset", "station_length", "station_width", "station_height",
                            "station_restriction_offset", "fence_height", "fence_width", "vegetation_height"]
    assert values["tree_top_diameter"] == 4.0 and values["vegetation_height"] == 12.0
    assert [f for f, _ in dialogs.SHADING_FORM_KEYS] == list(dialogs.SHADING_FIELDS)
    with pytest.raises(dialogs.DialogsInputError):
        dialogs.shading_form_values({"Kind": "Tree"})


def test_place_tree_matches_the_e1_capture():
    ents = dialogs.place_tree(dialogs.shading_form_accept(), (250, 150))
    assert ents == [
        {"type": "circle", "layer": "LEAF-PVCASE-SHADING-TREE", "center": (250.0, 150.0), "radius": 2.0},
        {"type": "circle", "layer": "LEAF-PVCASE-SHADING-RESTRICTION", "center": (250.0, 150.0), "radius": 3.0},
        {"type": "text", "layer": "LEAF-PVCASE-SHADING-TREE", "position": (250.0, 147.5), "height": 0.5,
         "text": "Tree h=8.0 trunk=3.0"},
    ]


def test_no_restriction_ring_without_a_positive_offset():
    ents = dialogs.place_tree(dict(dialogs.shading_defaults(), TreeRestrictionOffset=0.0), (0, 0))
    assert [e["type"] for e in ents] == ["circle", "text"]


def test_the_label_uses_fixed_one_place():
    ents = dialogs.place_tree(dict(dialogs.shading_defaults(), TreeTotalHeight=7.25, TreeTrunkHeight=2.04), (0, 0))
    assert ents[-1]["text"] == "Tree h=7.3 trunk=2.0"


def test_only_the_tree_placement_is_ported():
    with pytest.raises(dialogs.DialogsInputError, match="only the Tree"):
        dialogs.place_tree(dict(dialogs.shading_defaults(), Kind="Station"), (0, 0))


@pytest.mark.parametrize("params, center", [
    ({"Kind": "Tree"}, (0, 0)),
    (dict(dialogs.shading_defaults(), TreeTopDiameter=math.nan), (0, 0)),
    (dialogs.shading_defaults(), (0,)),
])
def test_shading_inputs_fail_closed(params, center):
    with pytest.raises(dialogs.DialogsInputError):
        dialogs.place_tree(params, center)


# ----------------------------------------------------------- project areas --

def test_add_then_ok_on_a_drawing_without_areas_writes_the_e4_record():
    areas, text = dialogs.project_areas_add_then_ok(None, "TinyTest")
    assert text == CAPTURED_AREA_RECORD
    assert [a["Name"] for a in areas] == ["Area 1"]


def test_a_second_add_numbers_the_new_area_and_round_trips_the_first():
    _, text = dialogs.project_areas_add_then_ok(CAPTURED_AREA_RECORD, "TinyTest")
    assert text == ('{"Version":1,"Areas":[{"Name":"Area 1","SubAreaId":"","BoundaryHandle":null,'
                    '"FramePresetName":"TinyTest","PitchOverrideM":0.0,"ExclusionZoneHandles":[],"CachedBounds":null},'
                    '{"Name":"Area 2","SubAreaId":"","BoundaryHandle":null,"FramePresetName":"TinyTest",'
                    '"PitchOverrideM":0.0,"ExclusionZoneHandles":[],"CachedBounds":null}]}')


def test_the_grid_round_trip_rounds_pitch_and_fills_blanks():
    stored = ('{"Version":1,"Areas":[{"Name":" ","SubAreaId":null,"BoundaryHandle":"00a1","FramePresetName":null,'
              '"PitchOverrideM":1.23456,"ExclusionZoneHandles":["b2","FF"],'
              '"CachedBounds":{"MinX":0.0,"MinY":1.0,"MaxX":2.5,"MaxY":3.0}}]}')
    _, text = dialogs.project_areas_add_then_ok(stored, "Beta", preset_names=["Alpha", "Beta"],
                                                live_handles=("A1", "B2"))
    assert text == ('{"Version":1,"Areas":[{"Name":"Area","SubAreaId":"","BoundaryHandle":"A1",'
                    '"FramePresetName":"Alpha","PitchOverrideM":1.235,"ExclusionZoneHandles":["B2"],'
                    '"CachedBounds":{"MinX":0.0,"MinY":1.0,"MaxX":2.5,"MaxY":3.0}},'
                    '{"Name":"Area 2","SubAreaId":"","BoundaryHandle":null,"FramePresetName":"Beta",'
                    '"PitchOverrideM":0.0,"ExclusionZoneHandles":[],"CachedBounds":null}]}')


def test_no_active_preset_binds_the_default_name():
    areas, _ = dialogs.project_areas_add_then_ok(None, None)
    assert areas[0]["FramePresetName"] == "Default"


def test_pvcase_area_import_is_refused():
    with pytest.raises(dialogs.DialogsInputError, match="not ported"):
        dialogs.project_areas_add_then_ok(None, "TinyTest", pvcase_area_boundaries=1)


def test_record_property_names_match_case_insensitively():
    areas = dialogs.read_area_record('{"version":1,"areas":[{"name":"X","pitchoverridem":2}]}')
    assert areas[0]["Name"] == "X" and areas[0]["PitchOverrideM"] == 2.0


@pytest.mark.parametrize("record", [
    "not json", '{"Areas": 5}', '{"Areas":[{"PitchOverrideM":"2"}]}', '{"Areas":[{"Name":NaN}]}',
])
def test_a_record_this_port_cannot_read_fails_closed(record):
    with pytest.raises(dialogs.DialogsInputError):
        dialogs.read_area_record(record)


# ---------------------------------------------------------- pile templates --

def test_plus_then_ok_on_the_before_store_writes_the_e6_after_store_byte_for_byte():
    _, text = dialogs.pile_templates_add_then_ok(before_text())
    data = text.encode("utf-8")
    assert len(data) == AFTER_STORE_BYTES
    assert hashlib.sha256(data).hexdigest() == AFTER_STORE_SHA256


def test_the_after_store_shape():
    store, text = dialogs.pile_templates_add_then_ok(before_text())
    assert text.startswith("{\r\n") and not text.endswith("\n") and "﻿" not in text
    doc = json.loads(text)
    assert list(doc) == ["Templates", "ActiveTemplate"] and doc["ActiveTemplate"] == "New"
    assert list(doc["Templates"]) == ["Default", "Full", "New"]
    # Each load appends to the five default buckets (Newtonsoft populates the existing list).
    assert len(doc["Templates"]["Default"]["RevealBucketBoundariesM"]) == 280
    assert doc["Templates"]["Full"]["RevealBucketBoundariesM"] == DEFAULT_BUCKETS * 2
    # The editor's "0.###" round trip on the selected template.
    assert doc["Templates"]["Full"]["VerticalDistancesM"] == [4.536, 4.536, 4.535]
    new = {"Name": "New", "AreEqualMargins": False, "ShouldPlacePilesAtJoints": False, "IsMirrorFromMiddle": False,
           "DistributionType": 2, "HorizontalDistancesM": [1.0, 1.0, 1.0], "VerticalDistancesM": [1.0, 1.0],
           "MiddleDistribution": 0.0, "SelectedMiddlePole": False, "HorizontalPoleCount": 2,
           "VerticalPoleCount": 1, "PlacementMode": "Grid", "StationAxis": "LocalX",
           "ReverseStationStart": False, "Stations": [], "PileDiameterM": 0.0, "PileRevealM": 0.0,
           "PileEmbedmentM": 0.0, "MinPileLengthM": 0.0, "MaxPileLengthM": 0.0,
           "RevealBucketBoundariesM": DEFAULT_BUCKETS, "PilesPerFrame": 2}
    assert doc["Templates"]["New"] == new and list(doc["Templates"]["New"]) == list(new)
    assert [t["Name"] for t in store.list()] == ["Default", "Full", "New"] and store.active == "New"


def test_reading_appends_to_the_default_buckets():
    store = dialogs.PileTemplateStore('{"Templates":{"A":{"Name":"A","RevealBucketBoundariesM":[9.0]}},'
                                      '"ActiveTemplate":"A"}')
    assert store.get("a")["RevealBucketBoundariesM"] == DEFAULT_BUCKETS + [9.0]
    assert store.get_active()["Name"] == "A" and store.contains("Full")


def test_a_missing_store_file_is_the_default_full_template():
    store = dialogs.PileTemplateStore(None)
    [full] = store.list()
    assert (full["Name"], full["HorizontalPoleCount"], full["VerticalPoleCount"]) == ("Full", 2, 1)
    assert full["HorizontalDistancesM"] == [1.0, 1.0, 1.0] and full["VerticalDistancesM"] == [1.0, 1.0]
    assert full["RevealBucketBoundariesM"] == DEFAULT_BUCKETS and store.active == "Full"
    text = store.file_text()
    assert '"PilesPerFrame": 2' in text and text.endswith('"ActiveTemplate": "Full"\r\n}')


def test_normalize_orders_stations_and_clamps():
    store = dialogs.PileTemplateStore(
        '{"Templates":{"S":{"Name":" S ","PlacementMode":"axisstations","PileDiameterM":-1,'
        '"Stations":[{"OffsetM":3,"Kind":"drive"},{"OffsetM":1,"Label":null}]},'
        '"G":{"Name":"G","PlacementMode":1,"HorizontalPoleCount":0}}}')
    s, g = store.get("S"), store.get("G")
    assert s["Name"] == "S" and [st["OffsetM"] for st in s["Stations"]] == [1.0, 3.0]
    assert [st["Label"] for st in s["Stations"]] == ["", ""] and s["Stations"][1]["Kind"] == "Drive"
    assert s["PlacementMode"] == "AxisStations" and s["PileDiameterM"] == 0.0 and dialogs.piles_per_frame(s) == 2
    assert g["PlacementMode"] == "Grid" and g["HorizontalPoleCount"] == 1


def test_a_taken_name_gets_a_numbered_suffix_and_becomes_active():
    store, text = dialogs.pile_templates_add_then_ok(
        '{"Templates":{"New":{"Name":"New"},"Full":{"Name":"Full"}},"ActiveTemplate":"New"}')
    doc = json.loads(text)
    assert list(doc["Templates"]) == ["Full", "New", "New 2"] and doc["ActiveTemplate"] == "New 2"


def test_only_the_selected_template_goes_through_the_editor_round_trip():
    _, text = dialogs.pile_templates_add_then_ok(
        '{"Templates":{"Full":{"Name":"Full","HorizontalDistancesM":[1.23456]},'
        '"Other":{"Name":"Other","HorizontalDistancesM":[2.34567]}},"ActiveTemplate":"Full"}')
    doc = json.loads(text)
    assert doc["Templates"]["Full"]["HorizontalDistancesM"] == [1.235]
    assert doc["Templates"]["Other"]["HorizontalDistancesM"] == [2.34567]


def test_a_blank_name_takes_its_key_and_names_merge_case_insensitively():
    store = dialogs.PileTemplateStore('{"Templates":{"k1":{"Name":null},"x":{"Name":"FULL"}}}')
    assert [t["Name"] for t in store.list()] == ["FULL", "k1"]
    assert store.get_active()["Name"] == "FULL"
    assert list(json.loads(store.file_text())["Templates"]) == ["FULL", "k1"]


@pytest.mark.parametrize("text", [
    '{"Templates":{"A":{"DistributionType":"2"}}}', '{"Templates":{"A":{"HorizontalPoleCount":2.0}}}',
    '{"Templates":{"A":{"PlacementMode":"Diagonal"}}}', '{"Templates":[1]}',
    '{"Templates":{"A":{"AreEqualMargins":1}}}', "{'Templates': {}}",
])
def test_a_store_this_port_cannot_read_fails_closed(text):
    with pytest.raises(dialogs.DialogsInputError):
        dialogs.PileTemplateStore(text)


def test_piles_per_frame_wraps_like_a_csharp_int():
    assert dialogs.piles_per_frame(dialogs.new_template(HorizontalPoleCount=3, VerticalPoleCount=4)) == 12
    assert dialogs.piles_per_frame(dialogs.new_template(HorizontalPoleCount=65536, VerticalPoleCount=65536)) == 0
