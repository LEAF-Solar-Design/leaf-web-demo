"""The string tools decode a compact Ground frame's rows once per process, not once per seam, and
build a slot view only for the slots a call names: the slot-row memo in solar_ground_graph_codec and
the touched-only lookups in the string builtins. Every result is byte-identical to the base."""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import sys

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))
from test_w1_design_graph import app_id, entity, graph  # noqa: E402,F401
from test_solar_ground_graph_codec import (  # noqa: E402,F401
    SMALL_IDS, b18, canon, ground_graph, slot_ids, small_block)
from test_solar_ground_compact_string_tools import (  # noqa: E402
    MISSING, MODULES, _builtin, _code, _digest, _pinned, _run, _stripped)
import solar_design_graph as sdg  # noqa: E402
import solar_ground_graph_codec as codec  # noqa: E402

TOOLS = ("add", "multi_add", "midpoint", "data")
# Canonical result digests on the full b18 site (237 compact trackers, 69,678 slots, strings S1 =
# slots 0..20 and S2 = slots 60..80 of tracker 0, sized 30), measured at the base 3293e8cd; the change
# must leave every one byte-identical.
FULL = {
    "add": "5c847c12c1e709a89c73931f2fa6e107893d928a132cc0e42b9ee0bcea8a7317",
    "multi_add": "34ff75df7d901b2f0bc7147ab54b4df290c3ffbb2a3d17b75752d4c39f73908b",
    "midpoint": "bd454364551c723f046f71441fa56402436209d942a40bd253c3594d96c10451",
    "data": "896e63e1f0c534d214124f1aeb24fd100c97165fa4a1017f81b68517a574d2c8",
}
# Per tool on the full site, after the change: slot views built (cold run), and slot_tables calls.
FULL_VIEWS = {"add": 42, "multi_add": 84, "midpoint": 299, "data": 0}
# Row strings decoded by codec._rows on the full site: two per block (ids, centres) on a cold memo,
# none on a warm one. The base decodes every block at every seam: 4,266 (nine seams) for the three
# mutating tools and 474 (one seam) for string data, cold and warm alike.
FULL_ROWS_COLD = 474
# Canonical result digests on the mixed graph (tracker 0 stored, trackers 1 and 2 compact), measured
# at the base 3293e8cd.
MIXED = {
    "add": "598978a8f9b4405fc4285effdff3fd5351d75da429dc61276e02643382aefaa6",
    "multi_add": "eb592df26855492eb40d35c9e20843d955b7eea2d9adba3408a713f5008fbdcc",
    "midpoint": "5d4d435204ff055a5335bb52eda171897e95061f81eaca20d1263baf69320b34",
    "data": "73ee2eaca0614d37be6c59aa195b01c7b8feffd51226f45bc2d9489843ed9b13",
}
MIXED_REFUSALS = {
    "add-missing": "MISSING_PANEL",
    "add-wired-stored": "PANEL_ALREADY_ASSIGNED",
    "add-wired-slot": "PANEL_ALREADY_ASSIGNED",
    "midpoint-stored-to-slot": "DIFFERENT_PANEL_GROUPS",
    "midpoint-missing": "MISSING_PANEL",
}


@pytest.fixture(autouse=True)
def _cold_memo():
    """Every row starts and ends on an empty slot-row memo (absent at the base: nothing to reset)."""
    reset = getattr(codec, "reset_slot_memo", None)
    if reset is not None:
        reset()
    yield
    if reset is not None:
        reset()


def _never(*args, **kwargs):
    raise AssertionError("must not be called")


def _counting(fn, box):
    def wrapped(*args, **kwargs):
        box[0] += 1
        return fn(*args, **kwargs)
    return wrapped


def _seam(module):
    """The module that holds the shared lookups for a tool: add itself, the others' `single`."""
    return getattr(module, "single", module)


def full_site(w1, sites):
    g = ground_graph(w1, sites, 237)
    g["settings"]["panels_in_sequence"] = 30
    ids = slot_ids(g, 0, 294)
    for n, (a, b) in enumerate([(0, 21), (60, 81)], 1):
        g["strings"].append(entity(
            "string", n, circuit_tag=f"S{n}", circuit_kind="String", ordered_panel_refs=ids[a:b],
            module_count=b - a, from_ref=ids[a], to_ref=ids[b - 1], tag_text_ref=None,
            wire_gauge="10 AWG", length_ft=10, route=[[0, 0], [1, 0]], inverter_ref=None))
    return g


def full_params(tool, g):
    ids = slot_ids(g, 0, 294)
    return {
        "add": {"operation": "add-string", "expected_rev": 0, "ordered_panel_refs": ids[21:42]},
        "multi_add": {"operation": "add-strings", "expected_rev": 0, "string_length": 21,
                      "ordered_panel_refs": ids[100:142]},
        "midpoint": {"operation": "add-midpoint-string", "expected_rev": 0,
                     "start_panel_ref": ids[50], "end_panel_ref": ids[56]},
        "data": {},
    }[tool]


def mixed(w1, sites):
    """Three b18 trackers: tracker 0's 294 slot panels STORED in graph["panels"], trackers 1 and 2
    compact. S1 = stored 0..2, S2 = tracker 2 slots 0..1. Sized 30."""
    g = ground_graph(w1, sites, 3)
    s, t2 = slot_ids(g, 0, 294), slot_ids(g, 2, 294)
    for n, refs in ((1, s[0:3]), (2, t2[0:2])):
        g["strings"].append(entity(
            "string", n, circuit_tag=f"S{n}", circuit_kind="String", ordered_panel_refs=list(refs),
            module_count=len(refs), from_ref=refs[0], to_ref=refs[-1], tag_text_ref=None,
            wire_gauge="10 AWG", length_ft=10, route=[[0, 0], [1, 0]], inverter_ref=None))
    g = codec.compact_graph(codec.expand_graph(g), [frame["id"] for frame in g["frames"][1:]])
    g["settings"]["panels_in_sequence"] = 30
    return g


def mixed_params(tool, g):
    s, t1, t2 = (slot_ids(g, k, 294) for k in range(3))
    return {
        "add": {"operation": "add-string", "expected_rev": 0,
                "ordered_panel_refs": [s[10], s[11], t2[20], t2[21]]},
        "multi_add": {"operation": "add-strings", "expected_rev": 0, "string_length": 4,
                      "ordered_panel_refs": t1[30:36] + s[40:42]},
        "midpoint": {"operation": "add-midpoint-string", "expected_rev": 0,
                     "start_panel_ref": t2[50], "end_panel_ref": t2[56]},
        "data": {},
    }[tool]


@pytest.mark.parametrize("tool", TOOLS)
def test_string_decode_memo_full_site_decodes_each_block_once(graph, b18, tool, monkeypatch):
    g = full_site(graph, b18)
    before = deepcopy(g)
    monkeypatch.setattr(codec, "expand_graph", _never)
    rows, views, tables = [0], [0], [0]
    monkeypatch.setattr(codec, "_rows", _counting(codec._rows, rows))
    module = _pinned(tool, monkeypatch)
    seam = _seam(module)
    monkeypatch.setattr(seam, "panel_views", _never)
    monkeypatch.setattr(seam, "slot_view", _counting(seam.slot_view, views))
    monkeypatch.setattr(seam, "slot_tables", _counting(seam.slot_tables, tables))
    cold = _run(module, g, full_params(tool, g))
    assert (rows[0], views[0], tables[0]) == (FULL_ROWS_COLD, FULL_VIEWS[tool], 1)
    assert _digest(tool, cold) == FULL[tool]
    rows[0] = 0
    warm = _run(_pinned(tool, monkeypatch), g, full_params(tool, g))
    assert rows[0] == 0
    assert _digest(tool, warm) == FULL[tool]
    assert g == before


@pytest.mark.parametrize("tool", TOOLS)
def test_string_decode_memo_mixed_matches_expansion(graph, b18, tool, monkeypatch):
    g = mixed(graph, b18)
    before = deepcopy(g)
    params = mixed_params(tool, g)
    result = _run(_pinned(tool, monkeypatch), g, params)
    assert g == before
    reference = _run(_pinned(tool, monkeypatch), codec.expand_graph(g), params)
    if tool == "data":
        assert (result.summary, result.content) == (reference.summary, reference.content)
    else:
        assert result["rev"] == 1
        assert canon(_stripped(codec.expand_graph(result))) == canon(_stripped(reference))
    assert _digest(tool, result) == MIXED[tool]


MIXED_REQUESTS = [
    ("add-missing", "add", lambda s, t1, t2: {"operation": "add-string", "expected_rev": 0,
                                              "ordered_panel_refs": [s[10], t2[20], MISSING]}),
    ("add-wired-stored", "add", lambda s, t1, t2: {"operation": "add-string", "expected_rev": 0,
                                                   "ordered_panel_refs": [t2[20], s[1]]}),
    ("add-wired-slot", "add", lambda s, t1, t2: {"operation": "add-string", "expected_rev": 0,
                                                 "ordered_panel_refs": [s[10], t2[1]]}),
    ("midpoint-stored-to-slot", "midpoint", lambda s, t1, t2: {
        "operation": "add-midpoint-string", "expected_rev": 0, "start_panel_ref": s[50],
        "end_panel_ref": t1[50]}),
    ("midpoint-missing", "midpoint", lambda s, t1, t2: {
        "operation": "add-midpoint-string", "expected_rev": 0, "start_panel_ref": t2[50],
        "end_panel_ref": MISSING}),
]


@pytest.mark.parametrize("name,tool,request_of", MIXED_REQUESTS, ids=[row[0] for row in MIXED_REQUESTS])
def test_string_decode_memo_mixed_refusals(graph, b18, name, tool, request_of):
    g = mixed(graph, b18)
    before = deepcopy(g)
    request = request_of(*(slot_ids(g, k, 294) for k in range(3)))
    code = _code(_builtin(MODULES[tool]).run, g, request)
    assert g == before
    assert code == MIXED_REFUSALS[name]
    assert _code(_builtin(MODULES[tool]).run, codec.expand_graph(g), request) == code


def test_string_decode_memo_find_panels_equals_the_view_lookup(graph, b18):
    tool = _builtin("solar_string_add")
    g = mixed(graph, b18)
    s, t1, t2 = (slot_ids(g, k, 294) for k in range(3))
    tables = tool.slot_tables(g)
    views = {view["id"]: view for view in tool.panel_views(g, tables)}
    refs = [s[0], s[293], t1[0], t1[293], t2[147], MISSING]
    found = tool.find_panels(g, refs, tables)
    assert found == {ref: views[ref] for ref in refs if ref in views}
    assert list(found) == [s[0], s[293], t1[0], t1[293], t2[147]]
    assert found[s[0]] is g["panels"][0] and found[s[293]] is g["panels"][293]
    found[t1[0]]["centre"][0] = 1e9
    assert tool.find_panels(g, [t1[0]], tables)[t1[0]] == views[t1[0]]
    assert tool.find_panels(g, [], tables) == {}
    assert tool.find_panels(graph, [graph["panels"][0]["id"]], tool.slot_tables(graph)) == {
        graph["panels"][0]["id"]: graph["panels"][0]}


@pytest.mark.parametrize("frame", [0, 1, 2], ids=["stored", "compact-first", "compact-second"])
def test_string_decode_memo_frame_views_follow_panel_views(graph, b18, frame):
    tool = _builtin("solar_string_midpoint")
    g = mixed(graph, b18)
    tables = tool.single.slot_tables(g)
    frame_ref = g["frames"][frame]["id"]
    expected = [(index, view) for index, view in enumerate(tool.single.panel_views(g, tables))
                if view["frame_ref"] == frame_ref]
    got = tool._frame_views(g, frame_ref, tables)
    assert got == expected
    assert [index for index, _ in got] == list(range(294 * frame, 294 * frame + 294))
    assert all((a is b) == (frame == 0) for (_, a), (_, b) in zip(got, expected))


def _block_variant(**changes):
    block = small_block()
    block.update(changes)
    return block


def test_string_decode_memo_hit_keeps_each_blocks_own_angle_and_template(monkeypatch):
    rows = [0]
    monkeypatch.setattr(codec, "_rows", _counting(codec._rows, rows))
    first = small_block()
    other = _block_variant(angle=0.25, panel={**first["panel"], "rev": 7})
    a = codec.decode_slots(first)
    b = codec.decode_slots(other)
    assert rows[0] == 2
    assert b.ids is a.ids and b.centres is a.centres and b.ids == tuple(SMALL_IDS)
    assert (b.angle, a.angle) == (0.25, first["angle"])
    assert b.panel is other["panel"] and a.panel is first["panel"]


HIT_REFUSALS = [
    ("angle-nan", {"angle": math.nan}, "INVALID_GROUND_SLOTS"),
    ("template-extra-key", {"panel": {**small_block()["panel"], "more": 1}}, "INVALID_GROUND_SLOTS"),
    ("codec-name", {"codec": "leaf.solar-ground-slots.v2"}, "INVALID_GROUND_SLOTS"),
    ("template-rev-negative", {"panel": {**small_block()["panel"], "rev": -1}}, "INVALID_GROUND_SLOTS"),
]


@pytest.mark.parametrize("name,changes,code", HIT_REFUSALS, ids=[row[0] for row in HIT_REFUSALS])
def test_string_decode_memo_hit_still_checks_the_block(name, changes, code, monkeypatch):
    codec.decode_slots(small_block())
    rows = [0]
    monkeypatch.setattr(codec, "_rows", _counting(codec._rows, rows))
    with pytest.raises(sdg.GraphValidationError) as error:
        codec.decode_slots(_block_variant(**changes))
    assert (error.value.code, rows[0]) == (code, 0)


def _ids_text(ids):
    raw = b"".join(bytes.fromhex(ref.split(":")[2].replace("-", "")) for ref in ids)
    return codec.base64.b64encode(raw).decode("ascii")


MISS_REFUSALS = [
    ("uuid-version", {"panel_ids": _ids_text([SMALL_IDS[0].replace("-4000-", "-5000-")] + SMALL_IDS[1:])},
     "INVALID_GROUND_SLOTS"),
    ("repeated-id", {"panel_ids": _ids_text([SMALL_IDS[0], SMALL_IDS[0], SMALL_IDS[2]])},
     "DUPLICATE_APPLICATION_ID"),
    ("infinite-centre", {"centres": codec.base64.b64encode(codec.struct.pack(
        "<6d", 0.0, 0.0, math.inf, 0.0, 1.0, 1.0)).decode("ascii")}, "INVALID_GROUND_SLOTS"),
    ("not-base64", {"panel_ids": "!" + small_block()["panel_ids"][1:]}, "INVALID_GROUND_SLOTS"),
]


@pytest.mark.parametrize("name,changes,code", MISS_REFUSALS, ids=[row[0] for row in MISS_REFUSALS])
def test_string_decode_memo_never_stores_a_refusal(name, changes, code, monkeypatch):
    rows = [0]
    monkeypatch.setattr(codec, "_rows", _counting(codec._rows, rows))
    block = _block_variant(**changes)
    for attempt in (1, 2):
        with pytest.raises(sdg.GraphValidationError) as error:
            codec.decode_slots(block)
        assert error.value.code == code
        assert rows[0] >= attempt
    assert (len(codec._ROWS_MEMO), codec._ROWS_MEMO_SLOTS) == (0, 0)


def test_string_decode_memo_is_bounded_least_recently_used(graph, b18, monkeypatch):
    g = ground_graph(graph, b18, 3)
    blocks = [frame["ground_slots"] for frame in g["frames"]]
    monkeypatch.setattr(codec, "MEMO_MAX_SLOTS", 2 * 294)
    rows = [0]
    monkeypatch.setattr(codec, "_rows", _counting(codec._rows, rows))
    for k in (0, 1, 0, 2):
        codec.decode_slots(blocks[k])
    assert rows[0] == 6
    assert codec._ROWS_MEMO_SLOTS == 588 == sum(key[0] for key in codec._ROWS_MEMO)
    codec.decode_slots(blocks[0])
    codec.decode_slots(blocks[2])
    assert rows[0] == 6
    codec.decode_slots(blocks[1])
    assert rows[0] == 8
    assert codec._ROWS_MEMO_SLOTS == 588 and len(codec._ROWS_MEMO) == 2


def test_string_decode_memo_key_is_the_text_not_the_object(monkeypatch):
    rows = [0]
    monkeypatch.setattr(codec, "_rows", _counting(codec._rows, rows))
    block = small_block()
    copied = json.loads(json.dumps(block))
    assert copied["panel_ids"] is not block["panel_ids"]
    assert codec.decode_slots(copied) == codec.decode_slots(block)._replace(panel=copied["panel"])
    assert rows[0] == 2


def test_string_decode_memo_reset_empties_the_memo(monkeypatch):
    rows = [0]
    monkeypatch.setattr(codec, "_rows", _counting(codec._rows, rows))
    codec.decode_slots(small_block())
    codec.reset_slot_memo()
    assert (len(codec._ROWS_MEMO), codec._ROWS_MEMO_SLOTS) == (0, 0)
    codec.decode_slots(small_block())
    assert rows[0] == 4


def test_string_decode_memo_hit_still_refuses_repeated_ids_across_frames(graph, b18):
    g = ground_graph(graph, b18, 2)
    codec.decode_graph_slots(g)
    first, second = (frame["ground_slots"] for frame in g["frames"])
    assert first["count"] == second["count"]
    second.update(panel_ids=first["panel_ids"], centres=first["centres"])
    with pytest.raises(sdg.GraphValidationError) as error:
        codec.decode_graph_slots(g)
    assert error.value.code == "DUPLICATE_APPLICATION_ID"
