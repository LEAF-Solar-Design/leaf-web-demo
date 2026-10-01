"""Compact Ground frames are first-class v1 graphs without expanding their slots."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import importlib
import json
from pathlib import Path
import sys

import pytest
from jsonschema import Draft202012Validator

SERVER = Path(__file__).resolve().parents[1]
ROOT = SERVER.parent
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))
from test_w1_design_graph import app_id, entity, graph  # noqa: E402,F401
from test_solar_ground_graph_codec import VALID, b18, canon, canon_sha, ground_graph, slot_ids, strung  # noqa: E402,F401
import solar_design_graph as sdg  # noqa: E402
import solar_dependencies as dep  # noqa: E402
import solar_ground_graph_codec as codec  # noqa: E402

MISSING = "leaf:panel:00000000-0000-4000-8000-0000000000aa"


def _validate(g):
    sdg._reset_validation_caches()
    return sdg.validate_graph(g)


def _refused(g, code):
    sdg._reset_validation_caches()
    with pytest.raises(sdg.GraphValidationError) as error:
        sdg.validate_graph(g)
    assert error.value.code == code


def _never_expand(*args, **kwargs):
    raise AssertionError("compact validation must not expand")


def _loose_panel(w1, site, panel_id):
    panel = deepcopy(codec.expand_graph(ground_graph(w1, site, 1))["panels"][0])
    panel.update(frame_ref=None, matrix_cell=None)
    panel["id"] = panel_id
    return panel


def test_ground_compact_graph_schema_declares_the_block():
    schema = sdg.load_schema()
    Draft202012Validator.check_schema(schema)
    assert schema["$defs"]["frame"]["properties"]["ground_slots"] == {"$ref": "#/$defs/ground_slots"}
    block = schema["$defs"]["ground_slots"]
    assert block["additionalProperties"] is False
    assert block["required"] == ["codec", "count", "panel_ids", "centres", "angle", "panel"]
    props = block["properties"]
    assert props["codec"]["const"] == codec.CODEC
    assert props["count"]["maximum"] == codec.MAX_FRAME_SLOTS == 10000
    for key in ("panel_ids", "centres"):
        assert props[key]["maxLength"] == codec.row_string_length(codec.MAX_FRAME_SLOTS) == 213336
        assert props[key]["minLength"] == codec.row_string_length(1) == 24
    assert props["panel"]["additionalProperties"] is False
    assert props["panel"]["required"] == ["rev", "provenance", "validity", "extra"]
    assert props["panel"]["properties"]["rev"] == {"type": "integer", "minimum": 0, "maximum": codec.MAX_REV}
    assert props["count"] == {"type": "integer", "minimum": 1, "maximum": codec.MAX_FRAME_SLOTS}


@pytest.mark.parametrize("name,schema_accepts,code", [
    ("rev-integral-float", True, "INVALID_GROUND_SLOTS"),
    ("count-integral-float", True, "INVALID_GROUND_SLOTS"),
    ("module-rows-integral-float", True, "INVALID_GROUND_SLOTS"),
    ("rev-fraction", False, "INVALID_GRAPH_SCHEMA"),
    ("rev-above-maximum", False, "INVALID_GRAPH_SCHEMA"),
    ("rev-bool", False, "INVALID_GRAPH_SCHEMA"),
], ids=["rev-integral-float", "count-integral-float", "module-rows-integral-float", "rev-fraction", "rev-above-maximum", "rev-bool"])
def test_ground_compact_graph_integer_spelling(graph, b18, name, schema_accepts, code):
    g = ground_graph(graph, b18, 20)
    block = g["frames"][0]["ground_slots"]
    if name == "count-integral-float":
        block["count"] = float(block["count"])
    elif name == "module-rows-integral-float":
        frame = g["frames"][0]
        frame["module_rows"] = float(frame["module_rows"])
    else:
        block["panel"]["rev"] = {
            "rev-integral-float": 0.0,
            "rev-fraction": 0.5,
            "rev-above-maximum": codec.MAX_REV + 1,
            "rev-bool": True,
        }[name]
    assert Draft202012Validator(sdg.load_schema()).is_valid(g) is schema_accepts
    _refused(g, code)


def test_ground_compact_graph_template_depth_counts_compact_nesting(graph, b18):
    g = ground_graph(graph, b18, 1)
    extra = {}
    for _ in range(28):
        extra = {"x": extra}
    g["frames"][0]["ground_slots"]["panel"]["extra"] = extra
    assert Draft202012Validator(sdg.load_schema()).is_valid(g)
    _refused(g, "GRAPH_LIMIT_EXCEEDED")
    sdg._reset_validation_caches()
    with pytest.raises(sdg.GraphValidationError) as error:
        codec.expand_graph(g)
    assert error.value.code == "GRAPH_LIMIT_EXCEEDED"


def test_ground_compact_graph_b18_site_validates(graph, b18, monkeypatch):
    g = ground_graph(graph, b18, 237)
    assert canon_sha(g) == "a7e92f0cedec74b369d232918e12cf4bcd25e98e92409726648295c311b27a0b"
    assert len(canon(g)) == 3350782
    monkeypatch.setattr(codec, "expand_graph", _never_expand)
    result = _validate(g)
    assert result == g and result is not g
    result["frames"][0]["ground_slots"]["panel"]["extra"]["note"] = "isolated"
    assert g["frames"][0]["ground_slots"]["panel"]["extra"] == {}
    assert len(sdg.serialize_graph(g).encode("utf-8")) == 3350782
    assert sdg.deserialize_graph(sdg.serialize_graph(g)) == g


def test_ground_compact_graph_twenty_trackers_validate(graph, b18):
    g = ground_graph(graph, b18, 20)
    assert canon_sha(g) == "a59a275e4d04098182922013d1b574e33a4deb14fe2e955a6e684dc33fd028ea"
    assert _validate(g) == g


@pytest.mark.parametrize("with_strings,digest", [
    (False, "da0365699a167f319128e57639f5700e5e473c3280e660471a47de740f70682d"),
    (True, "3193a2336c11b718e5ad3989524b50421fd671e63b0bb2045e1cdd4acdaf3648"),
], ids=["unstrung", "strung"])
def test_ground_compact_graph_agrees_with_expansion(graph, b18, with_strings, digest):
    g = strung(graph, b18) if with_strings else ground_graph(graph, b18, 2)
    assert canon_sha(g) == digest
    assert _validate(g) == g
    expanded = codec.expand_graph(g)
    assert _validate(expanded) == expanded


ROWS = [
    ("missing-panel", "MISSING_PANEL", "MISSING_PANEL"),
    ("duplicate-membership", "DUPLICATE_PANEL_MEMBERSHIP", "DUPLICATE_PANEL_MEMBERSHIP"),
    ("slot-id-is-explicit-panel", "DUPLICATE_APPLICATION_ID", "DUPLICATE_APPLICATION_ID"),
    ("template-future-rev", "FUTURE_ENTITY_REVISION", "FUTURE_ENTITY_REVISION"),
    ("template-future-source-rev", "FUTURE_ENTITY_REVISION", "FUTURE_ENTITY_REVISION"),
    ("template-provenance-key", "INVALID_GRAPH_SCHEMA", "INVALID_GRAPH_SCHEMA"),
    ("template-unknown-key", "INVALID_GRAPH_SCHEMA", None),
    ("block-unknown-key", "INVALID_GRAPH_SCHEMA", None),
    ("frame-count-disagrees", "INVALID_GROUND_SLOTS", None),
    ("roof-frame-block", "INVALID_GROUND_SLOTS", None),
    ("zone-missing-panel", "MISSING_PANEL", "MISSING_PANEL"),
    ("non-base64-row", "INVALID_GROUND_SLOTS", None),
    ("ids-repeated-across-frames", "DUPLICATE_APPLICATION_ID", None),
    ("derived-list-filled", "INVALID_GROUND_SLOTS", None),
    ("count-over-frame-bound", "INVALID_GRAPH_SCHEMA", None),
    ("count-disagrees-with-rows", "INVALID_GROUND_SLOTS", None),
    ("unknown-codec", "INVALID_GRAPH_SCHEMA", None),
    ("angle-not-a-number", "INVALID_GRAPH_SCHEMA", None),
    ("ids-over-max-length", "INVALID_GRAPH_SCHEMA", None),
    ("tracker-slots-disagree", "INVALID_GROUND_SLOTS", None),
    ("input-over-capacity", "INVERTER_CAPACITY_EXCEEDED", "INVERTER_CAPACITY_EXCEEDED"),
    ("string-count-mismatch", "STRING_COUNT_MISMATCH", "STRING_COUNT_MISMATCH"),
]


def _mutate(g, name, w1, site):
    frame = g["frames"][0]
    block = frame["ground_slots"]
    a = slot_ids(g, 0, 294)
    if name == "missing-panel":
        g["strings"][2]["ordered_panel_refs"][0] = MISSING
    elif name == "duplicate-membership":
        g["strings"][2]["ordered_panel_refs"][0] = a[0]
    elif name == "slot-id-is-explicit-panel":
        g["panels"].append(_loose_panel(w1, site, a[0]))
    elif name == "template-future-rev":
        block["panel"]["rev"] = 1
    elif name == "template-future-source-rev":
        block["panel"]["provenance"]["source_rev"] = 1
    elif name == "template-provenance-key":
        del block["panel"]["provenance"]["created_by"]
    elif name == "template-unknown-key":
        block["panel"]["note"] = 1
    elif name == "block-unknown-key":
        block["future"] = 1
    elif name == "frame-count-disagrees":
        frame.update(module_slots=293, module_columns=293)
        frame["tracker"]["module_slots"] = 293
    elif name == "roof-frame-block":
        g["project"]["installation_design"] = "Roof"
        for item in g["frames"]:
            item["installation_design"] = "Roof"
            del item["tracker"]
    elif name == "zone-missing-panel":
        zone = deepcopy(w1["electrical_zones"][0])
        zone["panel_refs"] = [MISSING]
        g["electrical_zones"] = [zone]
    elif name == "non-base64-row":
        block["panel_ids"] = "!" + block["panel_ids"][1:]
    elif name == "ids-repeated-across-frames":
        g["frames"][1]["ground_slots"]["panel_ids"] = block["panel_ids"]
    elif name == "derived-list-filled":
        frame["panel_refs"] = [a[0]]
    elif name == "count-over-frame-bound":
        block["count"] = 10001
    elif name == "count-disagrees-with-rows":
        block["count"] = 293
    elif name == "unknown-codec":
        block["codec"] = "leaf.solar-ground-slots.v2"
    elif name == "angle-not-a-number":
        block["angle"] = "x"
    elif name == "ids-over-max-length":
        block["panel_ids"] = "A" * 213340
    elif name == "tracker-slots-disagree":
        frame["tracker"]["module_slots"] = 293
    elif name == "input-over-capacity":
        g["inverters"][0]["input_assignments"][0]["input_number"] = 5
    elif name == "string-count-mismatch":
        g["strings"][0]["module_count"] = 4
    else:
        raise AssertionError(name)


@pytest.mark.parametrize("name,code,expanded_code", ROWS, ids=[row[0] for row in ROWS])
def test_ground_compact_graph_refusals(graph, b18, name, code, expanded_code):
    g = deepcopy(strung(graph, b18))
    _mutate(g, name, graph, b18)
    snapshot = deepcopy(g)
    sdg._reset_validation_caches()
    for _ in range(2):
        with pytest.raises(sdg.GraphValidationError) as error:
            sdg.validate_graph(g)
        assert error.value.code == code
        assert g == snapshot
    if expanded_code is not None:
        _refused(codec.expand_graph(g), expanded_code)
        assert g == snapshot


def test_ground_compact_graph_zone_may_name_slot_panels(graph, b18):
    g = strung(graph, b18)
    zone = deepcopy(graph["electrical_zones"][0])
    zone["panel_refs"] = [slot_ids(g, 0, 294)[0], slot_ids(g, 1, 294)[1]]
    g["electrical_zones"] = [zone]
    assert _validate(g) == g
    expanded = codec.expand_graph(g)
    assert _validate(expanded) == expanded


def test_ground_compact_graph_combined_panel_bound(graph, b18, monkeypatch):
    g = strung(graph, b18)
    loose = _loose_panel(graph, b18, "leaf:panel:00000000-0000-4000-8000-0000000000bb")
    original_bound = codec.MAX_GRAPH_SLOTS
    monkeypatch.setattr(codec, "MAX_GRAPH_SLOTS", 588)
    assert _validate(g) == g
    more = deepcopy(g)
    more["panels"].append(loose)
    _refused(more, "GRAPH_LIMIT_EXCEEDED")
    monkeypatch.setattr(codec, "MAX_GRAPH_SLOTS", original_bound)
    assert _validate(more) == more
    monkeypatch.setattr(codec, "MAX_GRAPH_SLOTS", 587)
    _refused(g, "GRAPH_LIMIT_EXCEEDED")


def test_ground_compact_graph_plain_graph_never_imports_the_codec(graph, b18, monkeypatch):
    expanded = codec.expand_graph(ground_graph(graph, b18, 1))
    monkeypatch.setitem(sys.modules, "solar_ground_graph_codec", None)
    assert _validate(graph) == graph
    sdg._reset_validation_caches()
    assert dep.dependency_index(graph)
    assert _validate(expanded) == expanded


def test_ground_compact_graph_dependency_index_matches_expansion(graph, b18):
    s = strung(graph, b18)
    sdg._reset_validation_caches()
    index = dep.dependency_index(s)
    sdg._reset_validation_caches()
    assert index == dep.dependency_index(codec.expand_graph(s))
    assert len(index) == 596
    assert sum(map(len, index.values())) == 1788
    data = json.dumps({k: sorted(v) for k, v in index.items()}, sort_keys=True, separators=(",", ":"))
    assert sha256(data.encode()).hexdigest() == "6e74ba189cb928f0416d22f18e712a7649357027e91ac48862fdacbb594e51b6"


def test_ground_compact_graph_affected_entities_reach_slots_and_strings(graph, b18):
    s = strung(graph, b18)
    frame_id = s["frames"][0]["id"]
    panel_id = slot_ids(s, 1, 294)[10]
    sdg._reset_validation_caches()
    frame_result = dep.affected_entities(s, s, [frame_id])
    assert len(frame_result) == 298
    assert sha256(json.dumps(frame_result, separators=(",", ":")).encode()).hexdigest() == \
        "4c66d2fffdcada736d3a8fa7b3816634054c508ec29ac3bdbdac2672db84e76e"
    panel_result = dep.affected_entities(s, s, [panel_id])
    assert panel_result == sorted([panel_id, app_id("string", 3)])
    expanded = codec.expand_graph(s)
    sdg._reset_validation_caches()
    assert frame_result == dep.affected_entities(expanded, expanded, [frame_id])
    assert panel_result == dep.affected_entities(expanded, expanded, [panel_id])


def test_ground_compact_graph_b18_dependency_index(graph, b18, monkeypatch):
    g = ground_graph(graph, b18, 237)
    monkeypatch.setattr(codec, "expand_graph", _never_expand)
    sdg._reset_validation_caches()
    index = dep.dependency_index(g)
    assert len(index) == 69917
    assert sum(map(len, index.values())) == 209509
    assert len(dep.affected_entities(g, g, [g["frames"][5]["id"]])) == 295


def test_ground_compact_graph_package_context(graph, b18):
    if str(ROOT) not in sys.path:
        sys.path.append(str(ROOT))
    pkg = importlib.import_module("server.solar_design_graph")
    pkg_dep = importlib.import_module("server.solar_dependencies")
    s = strung(graph, b18)
    pkg._reset_validation_caches()
    assert pkg.validate_graph(s) == s
    pkg._reset_validation_caches()
    assert len(pkg_dep.dependency_index(s)) == 596
    _mutate(s, "non-base64-row", graph, b18)
    pkg._reset_validation_caches()
    with pytest.raises(pkg.GraphValidationError) as error:
        pkg.validate_graph(s)
    assert error.value.code == "INVALID_GROUND_SLOTS"
