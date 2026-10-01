"""The string tools on slot panels of a compact Ground frame: every tool equals the same call on the
graph's expansion, decided without expanding, and every rooftop result is byte-identical to the base."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))
from test_w1_design_graph import app_id, entity, graph  # noqa: E402,F401
from test_solar_ground_graph_codec import b18, canon, canon_sha, ground_graph, slot_ids, strung  # noqa: E402,F401
import solar_artifacts  # noqa: E402
import solar_design_graph as sdg  # noqa: E402
import solar_ground_graph_codec as codec  # noqa: E402
import solar_solve_results as ssr  # noqa: E402

MISSING = "leaf:panel:00000000-0000-4000-8000-0000000000aa"
TOOLS = ("add", "multi_add", "midpoint", "delete", "flip", "swap", "conductors", "rebuild", "data")
MODULES = {"add": "solar_string_add", "multi_add": "solar_string_multi_add",
           "midpoint": "solar_string_midpoint", "delete": "solar_string_delete",
           "flip": "solar_string_flip", "swap": "solar_string_swap",
           "conductors": "solar_string_conductors", "rebuild": "solar_string_rebuild",
           "data": "solar_string_data"}
# Canonical result digests on the rooftop fixture, measured at the base 86573930 (byte-identical after).
ROOF = {
    "add": "e271d495b5b030c4b68051f2c76f5637c4be3c4a966a811a4496d14395e5a614",
    "multi_add": "f88a39445c1703f611ef8bfa9c60e4886e50cbaaaedd0b5d4ce16863f06f2df5",
    "midpoint": "8a737773e3fa430ed161bbe8c55504cfbc66b8fb9b365d18c3590a7ad320ca77",
    "delete": "57d2d8c6631e17aad7a87555f3d4d47eb68b9f4a73bf9882d05422b53ecf939c",
    "flip": "59270f0fef7c5b0cbda03505473f3ef87cec59242814cde1c8cf6dc9262d9dc3",
    "swap": "033b860b0ce4c3ce30be978969ebce68417fd788202d18a45d08e4820935641a",
    "conductors": "59cc1a7a1a3f6c3800b0f9df87227a486a0a84b87298c46c0292025bb12f2040",
    "rebuild": "5d4756f4d560961cf395e8d0991ea550a235239019389763a86b04faa9dba33a",
    "data": "d0523bde4586bf52ecbf0f5c4f65ec988cd108f80667e88ac8cbb1bb92f22400",
}
# Compact site results: digest, (frames, panels) whose rev and provenance differ on the expansion,
# unassigned slot count and string count; measured on the prototype.
SITE = {
    "add": {"digest": "c88a906d153ac6e5b4e59572696bf0492f81fbd7a4137c7c3af99a6a73b8a998",
            "strings": 4, "touched": [1, 5], "unassigned": 574},
    "multi_add": {"digest": "78c2cb37b81b40584c281b073c6e611b81ef58b0823824f57722fc0e8cae509f",
                  "strings": 6, "touched": [1, 12], "unassigned": 567},
    "midpoint": {"digest": "8c9b22ba6cdff121a6a5e4c9aa29c37edb9e0b4704fcfd97cbec27a6946e4986",
                 "strings": 4, "touched": [1, 3], "unassigned": 576},
    "delete": {"digest": "142ffd4be08a4830d350ec0f3dc327876c90501c3aeff331c367d65d9af8f1b4",
               "strings": 2, "touched": [2, 4], "unassigned": 583},
    "flip": {"digest": "89b9481c7cf50b5821beafce211161c79d488724c1212f497affdc85c9764c6c",
             "strings": 3, "touched": [2, 4], "unassigned": 579},
    "swap": {"digest": "afd37d0d2c647a91883a8051fd5595eac9e48ea318b8a50abdc0761dfe41a508",
             "strings": 3, "touched": [2, 0], "unassigned": 579},
    "conductors": {"digest": "8679f518f44be9f38fc59100e5ef2017201326d2961819a4cb7510d14cff41f0",
                   "strings": 3, "touched": [0, 0], "unassigned": 579},
    "rebuild": {"digest": "b5bf8b17a7c06722c44b826b7eaa91f22aefd77d4e6d41c196d8f37e2fb0cbc9"},
    "data": {"digest": "04391aa011432292e8367a2ac95a027ba2786cfafacd391dbc3bae8265b060f9"},
}
VIEWS_SHA = "8823df153c7f81bffbac0a7d045005b89addb33aafb9ad65112a944e00b111ab"
REFUSAL_CODES = {
    "add-bad-block": "INVALID_GROUND_SLOTS",
    "add-missing": "MISSING_PANEL",
    "add-wired": "PANEL_ALREADY_ASSIGNED",
    "data-bad-block": "INVALID_GROUND_SLOTS",
    "midpoint-bad-block": "INVALID_GROUND_SLOTS",
    "midpoint-missing": "MISSING_PANEL",
    "midpoint-two-frames": "DIFFERENT_PANEL_GROUPS",
    "midpoint-wired": "PANEL_ALREADY_ASSIGNED",
    "multi-add-missing": "MISSING_PANEL",
    "multi-add-wired": "PANEL_ALREADY_ASSIGNED",
}
B18 = {
    "add_canon_len": 6837123,
    "add_module_count": 21,
    "add_rev": 1,
    "add_unassigned": 69636,
    "data": "dad65df45c0d87ca9209c85dcf50aa334b2ec3a94a62b54337af22c6e198dd14",
    "data_summary": {"grouped_strings": 1, "groups": 237, "lines": 1201, "selected_strings": 1, "status": "written"},
    "mid_refs": 3,
    "mid_unassigned": 69654,
}


class FixedDatetime:
    @staticmethod
    def now(tz=None):
        return datetime(2026, 10, 1, tzinfo=timezone.utc)


def _builtin(name):
    spec = importlib.util.spec_from_file_location(name, SERVER / "builtins" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pinned(tool, monkeypatch):
    """A fresh load of the tool with string ids app_id("string", 901), 902, ... and one timestamp."""
    module = _builtin(MODULES[tool])
    counter = [901]

    def fake_new_id(kind):
        value = app_id(kind, counter[0])
        counter[0] += 1
        return value

    if hasattr(module, "new_id"):
        monkeypatch.setattr(module, "new_id", fake_new_id)
    if hasattr(module, "datetime"):
        monkeypatch.setattr(module, "datetime", FixedDatetime)
    return module


def lsha(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")).hexdigest()


def _code(fn, *args, **kwargs):
    sdg._reset_validation_caches()
    with pytest.raises(sdg.GraphValidationError) as error:
        fn(*args, **kwargs)
    return error.value.code


def _run(module, g, params):
    sdg._reset_validation_caches()
    return module.run(g, deepcopy(params))


def _digest(tool, result):
    """One sha256 per result kind: a graph canonically, a report as JSON, an artifact by its bytes."""
    if isinstance(result, solar_artifacts.ArtifactOutput):
        return lsha({"summary": result.summary, "media_type": result.media_type,
                     "filename": result.filename, "content": sha256(result.content).hexdigest()})
    if tool == "rebuild":
        return lsha(result)
    return canon_sha(result)


def _stripped(g):
    """The graph with rev and provenance removed from every frame and panel (kept elsewhere)."""
    out = deepcopy(g)
    for key in ("frames", "panels"):
        for item in out[key]:
            item.pop("rev")
            item.pop("provenance")
    return out


def _touched(compact_expanded, reference):
    """(frames, panels) ids whose rev or provenance differ between the two expanded graphs."""
    counts = []
    for key in ("frames", "panels"):
        mine = {item["id"]: item for item in compact_expanded[key]}
        counts.append(sum(1 for item in reference[key]
                          if (item["rev"], item["provenance"]) != (mine[item["id"]]["rev"],
                                                                   mine[item["id"]]["provenance"])))
    return counts


def roof(w1):
    """The W1 rooftop graph without its schedule, plus frame 2: one row of eight unwired panels
    2.5 m apart; sized 6."""
    g = deepcopy(w1)
    g["settings"]["panels_in_sequence"] = 6
    g["schedules"] = []
    frame_id = app_id("frame", 2)
    panels = [entity("panel", 10 + col, frame_ref=frame_id, matrix_cell={"row": 0, "col": col},
                     centre=[2.5 * col, 10.0], angle=0, assignment={"string_ref": None, "seq": None})
              for col in range(8)]
    cells = [{"code": "panel", "panel_ref": p["id"], "seq": None, "inverter_id": None,
              "string_input_number": None, "x": p["centre"][0], "y": p["centre"][1], "angle": 0}
             for p in panels]
    records = [{"panel_ref": p["id"], "string_ref": None, "seq": None, "inverter_id": None,
                "string_input_number": None} for p in panels]
    g["frames"].append(entity(
        "frame", 2, name="Roof row", insertion_point=[0, 10, 0], installation_design="Roof",
        panel_refs=[p["id"] for p in panels], module_rows=1, module_columns=8, module_slots=8,
        module_power_watts=400, module_width_along_row=1, module_height_across_row=2,
        electrical_zone_ref=None, matrix=[cells], sequences=[], panel_assignments=records))
    g["panels"].extend(panels)
    return g


def site(w1, sites):
    """strung (two compact b18 trackers, strings S1..S3) sized 30."""
    g = strung(w1, sites)
    g["settings"]["panels_in_sequence"] = 30
    return g


def roof_params(tool, g):
    row = [app_id("panel", 10 + col) for col in range(8)]
    s1, s2 = app_id("string", 1), app_id("string", 2)
    return {
        "add": {"operation": "add-string", "expected_rev": 0, "ordered_panel_refs": row[0:3]},
        "multi_add": {"operation": "add-strings", "expected_rev": 0, "string_length": 4,
                      "ordered_panel_refs": row[0:8]},
        "midpoint": {"operation": "add-midpoint-string", "expected_rev": 0,
                     "start_panel_ref": row[0], "end_panel_ref": row[4]},
        "delete": {"operation": "delete-strings", "expected_rev": 0, "string_refs": [s2]},
        "flip": {"operation": "flip-string", "expected_rev": 0, "string_ref": s1},
        "swap": {"operation": "swap-strings", "expected_rev": 0, "string_refs": [s1, s2]},
        "conductors": {"operation": "set-conductors", "expected_rev": 0,
                       "assignments": [{"string_ref": s1, "wire_gauge": "8 AWG"}]},
        "rebuild": {},
        "data": {},
    }[tool]


def site_params(tool, g):
    b = slot_ids(g, 1, 294)
    s = [app_id("string", n) for n in (1, 2, 3)]
    return {
        "add": {"operation": "add-string", "expected_rev": 0, "ordered_panel_refs": b[20:25]},
        "multi_add": {"operation": "add-strings", "expected_rev": 0, "string_length": 5,
                      "ordered_panel_refs": b[30:42]},
        "midpoint": {"operation": "add-midpoint-string", "expected_rev": 0,
                     "start_panel_ref": b[50], "end_panel_ref": b[56]},
        "delete": {"operation": "delete-strings", "expected_rev": 0, "string_refs": [s[1]]},
        "flip": {"operation": "flip-string", "expected_rev": 0, "string_ref": s[1]},
        "swap": {"operation": "swap-strings", "expected_rev": 0, "string_refs": [s[0], s[2]]},
        "conductors": {"operation": "set-conductors", "expected_rev": 0,
                       "assignments": [{"string_ref": s[2], "wire_gauge": "8 AWG"}]},
        "rebuild": {},
        "data": {},
    }[tool]


@pytest.mark.parametrize("tool", TOOLS)
def test_ground_compact_string_tools_rooftop_is_unchanged(graph, tool, monkeypatch):
    g = roof(graph)
    sdg._reset_validation_caches()
    assert sdg.validate_graph(deepcopy(g)) == g
    before = deepcopy(g)
    monkeypatch.setitem(sys.modules, "solar_ground_graph_codec", None)
    result = _run(_pinned(tool, monkeypatch), g, roof_params(tool, g))
    assert g == before
    assert _digest(tool, result) == ROOF[tool]


def test_ground_compact_string_tools_slot_views_follow_expansion(graph, b18):
    tool = _builtin("solar_string_add")
    g = site(graph, b18)
    views = tool.panel_views(g)
    expanded = codec.expand_graph(g)
    assert [view["id"] for view in views] == [panel["id"] for panel in expanded["panels"]]
    assert tuple(view["id"] for view in views) == ssr.slot_panel_ids(g)
    assert all(view == {key: panel[key] for key in ("id", "frame_ref", "centre", "angle")}
               for view, panel in zip(views, expanded["panels"]))
    assert lsha(views) == VIEWS_SHA
    assert tool.slot_tables(graph) == {}
    assert tool.panel_views(graph)[0] is graph["panels"][0]


@pytest.mark.parametrize("tool", TOOLS)
def test_ground_compact_string_tools_compact_matches_expansion(graph, b18, tool, monkeypatch):
    g = site(graph, b18)
    before = deepcopy(g)
    params = site_params(tool, g)
    result = _run(_pinned(tool, monkeypatch), g, params)
    assert g == before
    reference = _run(_pinned(tool, monkeypatch), codec.expand_graph(g), params)
    entry = {"digest": _digest(tool, result)}
    if isinstance(result, solar_artifacts.ArtifactOutput):
        assert (result.summary, result.content) == (reference.summary, reference.content)
    elif tool == "rebuild":
        assert result == reference
    else:
        assert result["rev"] == 1
        sdg._reset_validation_caches()
        assert sdg.validate_graph(deepcopy(result)) == result
        expanded = codec.expand_graph(result)
        assert canon(_stripped(expanded)) == canon(_stripped(reference))
        entry["touched"] = _touched(expanded, reference)
        entry["unassigned"] = len(result["extra"]["solve_coverage"]["unassigned_panel_refs"])
        entry["strings"] = len(result["strings"])
    assert entry == SITE[tool]


def _corrupt(g):
    block = g["frames"][0]["ground_slots"]
    block["panel_ids"] = "!" + block["panel_ids"][1:]
    return g


REFUSALS = [
    ("add-missing", "add", lambda a, b: {"operation": "add-string", "expected_rev": 0,
                                         "ordered_panel_refs": [b[20], MISSING]}, False),
    ("add-wired", "add", lambda a, b: {"operation": "add-string", "expected_rev": 0,
                                       "ordered_panel_refs": [b[20], a[0]]}, False),
    ("multi-add-missing", "multi_add", lambda a, b: {"operation": "add-strings", "expected_rev": 0,
                                                     "string_length": 2,
                                                     "ordered_panel_refs": [b[20], MISSING]}, False),
    ("multi-add-wired", "multi_add", lambda a, b: {"operation": "add-strings", "expected_rev": 0,
                                                   "string_length": 2,
                                                   "ordered_panel_refs": [b[20], a[0]]}, False),
    ("midpoint-missing", "midpoint", lambda a, b: {"operation": "add-midpoint-string", "expected_rev": 0,
                                                   "start_panel_ref": MISSING, "end_panel_ref": b[56]}, False),
    ("midpoint-two-frames", "midpoint", lambda a, b: {"operation": "add-midpoint-string", "expected_rev": 0,
                                                      "start_panel_ref": a[100], "end_panel_ref": b[100]}, False),
    ("midpoint-wired", "midpoint", lambda a, b: {"operation": "add-midpoint-string", "expected_rev": 0,
                                                 "start_panel_ref": b[8], "end_panel_ref": b[14]}, False),
    ("add-bad-block", "add", lambda a, b: {"operation": "add-string", "expected_rev": 0,
                                           "ordered_panel_refs": [b[20]]}, True),
    ("midpoint-bad-block", "midpoint", lambda a, b: {"operation": "add-midpoint-string", "expected_rev": 0,
                                                     "start_panel_ref": b[50], "end_panel_ref": b[56]}, True),
    ("data-bad-block", "data", lambda a, b: {}, True),
]


@pytest.mark.parametrize("name,tool,request_of,corrupt", REFUSALS, ids=[row[0] for row in REFUSALS])
def test_ground_compact_string_tools_refusals_leave_the_graph(graph, b18, name, tool, request_of, corrupt):
    g = site(graph, b18)
    a, b = slot_ids(g, 0, 294), slot_ids(g, 1, 294)
    if corrupt:
        _corrupt(g)
    before = deepcopy(g)
    code = _code(_builtin(MODULES[tool]).run, g, request_of(a, b))
    assert g == before
    assert code == REFUSAL_CODES[name]


def _never_expand(*args, **kwargs):
    raise AssertionError("a compact string tool must not expand")


def test_ground_compact_string_tools_b18_site(graph, b18, monkeypatch):
    g = ground_graph(graph, b18, 237)
    g["settings"]["panels_in_sequence"] = 30
    ids = slot_ids(g, 0, 294)
    g["strings"].append(entity(
        "string", 1, circuit_tag="S1", circuit_kind="String", ordered_panel_refs=ids[0:21], module_count=21,
        from_ref=ids[0], to_ref=ids[20], tag_text_ref=None, wire_gauge="10 AWG", length_ft=10,
        route=[[0, 0], [1, 0]], inverter_ref=None))
    monkeypatch.setattr(codec, "expand_graph", _never_expand)
    added = _run(_pinned("add", monkeypatch), g, {"operation": "add-string", "expected_rev": 0,
                                                 "ordered_panel_refs": ids[21:42]})
    mid = _run(_pinned("midpoint", monkeypatch), g, {"operation": "add-midpoint-string", "expected_rev": 0,
                                                     "start_panel_ref": ids[50], "end_panel_ref": ids[56]})
    data = _run(_builtin("solar_string_data"), g, {})
    entry = {"add_rev": added["rev"], "add_module_count": added["strings"][1]["module_count"],
             "add_unassigned": len(added["extra"]["solve_coverage"]["unassigned_panel_refs"]),
             "add_canon_len": len(canon(added)),
             "mid_refs": len(mid["strings"][1]["ordered_panel_refs"]),
             "mid_unassigned": len(mid["extra"]["solve_coverage"]["unassigned_panel_refs"]),
             "data": _digest("data", data), "data_summary": data.summary}
    assert entry == B18
    assert g["strings"][0]["module_count"] == 21

