"""Export currency on compact Ground frames: require_current_export and upstream_basis give a compact
graph, its partial compaction and its expansion the same answer (sf-w3-compact-export-currency)."""
from __future__ import annotations

from copy import deepcopy
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
from test_solar_ground_graph_codec import b18, canon_sha, ground_graph, slot_ids  # noqa: E402,F401
import solar_design_graph as sdg  # noqa: E402
import solar_ground_graph_codec as codec  # noqa: E402
import solar_solve_results as ssr  # noqa: E402

NOT_CURRENT = "SOLAR_OUTPUT_NOT_CURRENT"
W1_BASIS = "b013a3c9353bea41c2f9118c5b26bead06496673b6d0ca005f99735722b5dc44"
TWO_BASIS = "857e6f32a8c9dda1cad4171395d64f0b87d970cb6b1f34315d5034dc595745e4"
TWO_SHA = "2ce9e60fa18eae63b66f87cd2a1da918a0aa84b9cb8c619d094e89a1d19d09a6"
SITE_BASIS = "00fdef6bd8cd25c383b02227d85bef819be0e34de46aac66b8963a5fa2427c6d"
STALE = {"state": "stale", "reasons": ["upstream_corrected"]}


def _builtin(name):
    spec = importlib.util.spec_from_file_location(name, SERVER / "builtins" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verdict(value):
    """'current' or the refusal code; any other exception fails the row."""
    sdg._reset_validation_caches()
    try:
        assert ssr.require_current_export(value) == value
    except sdg.GraphValidationError as error:
        return error.code
    return "current"


def _strings(g, frames):
    """Fourteen 21-slot strings per named compact frame (every slot assigned), numbered from 1."""
    n = len(g["strings"])
    for k in frames:
        ids = slot_ids(g, k, 294)
        for i in range(14):
            refs = ids[21 * i:21 * (i + 1)]
            n += 1
            g["strings"].append(entity(
                "string", n, circuit_tag=f"S{n}", circuit_kind="String", ordered_panel_refs=list(refs),
                module_count=21, from_ref=refs[0], to_ref=refs[-1], tag_text_ref=None, wire_gauge="10 AWG",
                length_ft=10, route=[[0, 0], [1, 0]], inverter_ref=None))
    return g


def two(w1, site):
    """Two compact b18 trackers (frames A and B, 294 slots each), every slot on one of 28 strings."""
    return _strings(ground_graph(w1, site, 2), (0, 1))


def forms(g):
    """(compact, mixed, expanded): mixed keeps only frame B compact, expanded keeps none."""
    expanded = codec.expand_graph(g)
    mixed = codec.compact_graph(expanded, [g["frames"][1]["id"]])
    assert canon_sha(codec.expand_graph(mixed)) == canon_sha(expanded)
    return g, mixed, expanded


def _stamp(g, value, frame=0):
    g["frames"][frame]["extra"]["solve"] = {"upstream_sha256": value}
    return g


def _move_slot(g, frame, col, dx):
    """Re-encode frame `frame`'s block with slot `col`'s x centre moved by dx (ids and order kept)."""
    block = g["frames"][frame]["ground_slots"]
    table = codec.decode_slots(block)
    centres = [[table.centres[2 * i], table.centres[2 * i + 1]] for i in range(len(table.ids))]
    centres[col][0] += dx
    g["frames"][frame]["ground_slots"] = codec.encode_slots(list(table.ids), centres, table.angle, table.panel)
    return g


def case(name, w1, site):
    """The graph a table row starts from, in compact form (rooftop rows have no compact form)."""
    if name.startswith("rooftop"):
        g = deepcopy(w1)
        if name == "rooftop-stamped":
            _stamp(g, ssr.upstream_basis(g))
        elif name == "rooftop-stale-panel":
            g["panels"][0]["validity"] = deepcopy(STALE)
        return g
    g = two(w1, site)
    if name == "compact-stale-template-a":
        g["frames"][0]["ground_slots"]["panel"]["validity"] = deepcopy(STALE)
    elif name == "compact-stale-template-b":
        g["frames"][1]["ground_slots"]["panel"]["validity"] = deepcopy(STALE)
    elif name == "compact-invalid-template-b":
        g["frames"][1]["ground_slots"]["panel"]["validity"] = {"state": "invalid", "reasons": ["EQUIPMENT_X"]}
    elif name == "compact-template-valid-extra-field":
        g["frames"][1]["ground_slots"]["panel"]["validity"] = {"state": "valid", "reasons": [], "note": 1}
    elif name == "stamp-from-compact":
        _stamp(g, ssr.upstream_basis(g))
    elif name == "stamp-from-mixed":
        _stamp(g, ssr.upstream_basis(forms(g)[1]))
    elif name == "stamp-from-expansion":
        _stamp(g, ssr.upstream_basis(codec.expand_graph(g)))
    elif name == "stamp-on-compact-frame-b":
        _stamp(g, ssr.upstream_basis(g), frame=1)
    elif name == "stamp-then-slot-moved-b":
        _move_slot(_stamp(g, ssr.upstream_basis(g)), 1, 5, 1.0)
    elif name == "stamp-then-slot-moved-a":
        _move_slot(_stamp(g, ssr.upstream_basis(g), frame=1), 0, 293, -0.25)
    elif name == "compact-unassigned-slot":
        last = g["strings"][-1]
        last["ordered_panel_refs"] = last["ordered_panel_refs"][:20]
        last.update(module_count=20, to_ref=last["ordered_panel_refs"][-1])
    elif name == "stamp-foreign-digest":
        _stamp(g, "0" * 64)
    return g


TABLE = [
    ("rooftop-fresh", "current"),
    ("rooftop-stamped", "current"),
    ("rooftop-stale-panel", NOT_CURRENT),
    ("compact-fresh", "current"),
    ("compact-stale-template-a", NOT_CURRENT),
    ("compact-stale-template-b", NOT_CURRENT),
    ("compact-invalid-template-b", NOT_CURRENT),
    ("compact-template-valid-extra-field", "current"),
    ("compact-unassigned-slot", NOT_CURRENT),
    ("stamp-from-compact", "current"),
    ("stamp-from-mixed", "current"),
    ("stamp-from-expansion", "current"),
    ("stamp-on-compact-frame-b", "current"),
    ("stamp-then-slot-moved-b", NOT_CURRENT),
    ("stamp-then-slot-moved-a", NOT_CURRENT),
    ("stamp-foreign-digest", NOT_CURRENT),
]


@pytest.mark.parametrize("name,expected", TABLE, ids=[name for name, _ in TABLE])
def test_ground_export_currency_compact_equals_expansion(graph, b18, name, expected):
    g = case(name, graph, b18)
    if name.startswith("rooftop"):
        assert codec.expand_graph(g) == g
        candidates = (g,)
    else:
        candidates = forms(g)
    assert [verdict(c) for c in candidates] == [expected] * len(candidates)
    bases = {ssr.upstream_basis(c) for c in candidates}
    assert len(bases) == 1


def test_ground_export_currency_basis_digests(graph, b18):
    g = two(graph, b18)
    compact, mixed, expanded = forms(g)
    assert ssr.upstream_basis(graph) == W1_BASIS
    assert ssr.upstream_basis(compact) == ssr.upstream_basis(mixed) == ssr.upstream_basis(expanded) == TWO_BASIS
    assert canon_sha(compact) == TWO_SHA
    moved = _move_slot(deepcopy(compact), 1, 5, 1.0)
    assert ssr.upstream_basis(moved) == ssr.upstream_basis(codec.expand_graph(moved)) != TWO_BASIS


@pytest.mark.parametrize("solve,on_two,on_w1", [
    (None, NOT_CURRENT, NOT_CURRENT), ("x", NOT_CURRENT, NOT_CURRENT), ([], NOT_CURRENT, NOT_CURRENT),
    (5, NOT_CURRENT, NOT_CURRENT), (True, NOT_CURRENT, NOT_CURRENT), ({}, "current", "current"),
    ({"upstream_sha256": None}, NOT_CURRENT, NOT_CURRENT),
    ({"upstream_sha256": TWO_BASIS.upper()}, NOT_CURRENT, NOT_CURRENT),
    ({"upstream_sha256": TWO_BASIS}, "current", NOT_CURRENT),
    ({"upstream_sha256": W1_BASIS}, NOT_CURRENT, "current"),
], ids=["none", "string", "list", "int", "bool", "empty", "digest-none", "digest-upper", "digest-two", "digest-w1"])
def test_ground_export_currency_solve_record_shapes(graph, b18, solve, on_two, on_w1):
    g = two(graph, b18)
    g["frames"][0]["extra"]["solve"] = deepcopy(solve)
    assert [verdict(c) for c in forms(g)] == [on_two] * 3
    w1 = deepcopy(graph)
    w1["frames"][0]["extra"]["solve"] = deepcopy(solve)
    assert verdict(w1) == on_w1


def site(w1, b18):
    """The full b18 site: 237 compact trackers, 69,678 slots, every slot on one of 3,318 strings."""
    return _strings(ground_graph(w1, b18, 237), range(237))


def test_ground_export_currency_full_site_without_expansion(graph, b18, monkeypatch):
    g = site(graph, b18)
    sdg._reset_validation_caches()
    with pytest.raises(sdg.GraphValidationError) as error:
        codec.expand_graph(g)
    assert error.value.code == "GRAPH_LIMIT_EXCEEDED"

    def never(*args, **kwargs):
        raise AssertionError("export currency must not expand")

    monkeypatch.setattr(codec, "expand_graph", never)
    assert ssr.upstream_basis(g) == SITE_BASIS
    assert verdict(g) == "current"
    stamped = _stamp(deepcopy(g), SITE_BASIS, frame=236)
    assert verdict(stamped) == "current"
    moved = _move_slot(deepcopy(stamped), 0, 0, 0.5)
    assert ssr.upstream_basis(moved) != SITE_BASIS
    assert verdict(moved) == NOT_CURRENT
    template = deepcopy(stamped)
    template["frames"][236]["ground_slots"]["panel"]["validity"] = deepcopy(STALE)
    assert verdict(template) == NOT_CURRENT


def test_ground_export_currency_unstamped_graph_skips_the_basis(graph, b18, monkeypatch):
    def never(*args, **kwargs):
        raise AssertionError("no frame carries a solve digest")

    g = site(graph, b18)
    monkeypatch.setattr(ssr, "_upstream_basis", never)
    assert verdict(g) == "current"
    assert verdict(deepcopy(graph)) == "current"
    sdg._reset_validation_caches()
    with pytest.raises(AssertionError):
        ssr.require_current_export(_stamp(deepcopy(g), SITE_BASIS))


def test_ground_export_currency_decodes_slots_once(graph, b18, monkeypatch):
    calls = []
    real = codec.decode_graph_slots

    def counted(value):
        calls.append(1)
        return real(value)

    monkeypatch.setattr(codec, "decode_graph_slots", counted)
    g = _stamp(two(graph, b18), TWO_BASIS)
    assert verdict(g) == "current"
    # One decode inside validate_graph, one for coverage and the basis together.
    assert len(calls) == 2


@pytest.mark.parametrize("name", ["solar_cable_export", "solar_electrical_schedules"])
def test_ground_export_currency_export_tools_refuse_a_stale_template(graph, b18, name):
    tool = _builtin(name)
    g = two(graph, b18)
    sdg._reset_validation_caches()
    fresh = tool.run(deepcopy(g), {})
    assert fresh is not None
    for form in forms(g):
        stale = deepcopy(form)
        target = stale["frames"][1]
        if "ground_slots" in target:
            target["ground_slots"]["panel"]["validity"] = deepcopy(STALE)
        else:
            for panel in stale["panels"]:
                if panel["frame_ref"] == target["id"]:
                    panel["validity"] = deepcopy(STALE)
        sdg._reset_validation_caches()
        with pytest.raises(sdg.GraphValidationError) as error:
            tool.run(stale, {})
        assert error.value.code == NOT_CURRENT
