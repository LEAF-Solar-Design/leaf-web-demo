"""Studio's string assignment and colouring engines against the plugin source they port (contract G35).

Covered: the circuit parser, the MPPT letter and the tag text; the geometric fallback rows and the
seed-outward walk; AssignStrings' pattern plan (collectors by X, the nearest seed, capacity, string
numbers from one past the largest, the per-collector colour from the session counter, each string's
device, input, label and colour, the one drawing-properties save), its dialogs (Cancel, Manual, Excess
Capacity No, not enough capacity) and its refusals (an unknown device number, electrical zones, a
string without its annotations); a collector that already holds strings keeps its block colour; and
LEAFCOLORSTRINGS (the colour per inverter number, colour 7 unassigned, a string without its
annotations left alone, the printed count). States are synthetic and authored here.
"""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name, path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


st = _load("solar_inverter_state", ROOT / "server" / "solar_inverter_state.py")
ss = _load("solar_inverter_strings", ROOT / "server" / "solar_inverter_strings.py")

HOST = {"UseL2Collectors": True, "UseCombinerBox": False, "UsePatternStringAssignment": True,
        "L2NumMppt": 2, "L2StringsPerMppt": 2, "SessionColorCounter": 1, "AutoTagHeight": 12.5,
        "DeviceNumbers": [{"position": [0.04, -0.03], "number": 1}]}
FORMS = {"assign_strings_to_central_inverters": "OK", "assign_strings_to_central_inverters_mode": "Auto",
         "excess_capacity": "Yes"}


def device(x, y, number=None, colour=256, role="inverter"):
    row = {"number": None, "role": role, "position": st.coordinate(x, y), "scale": 2.0,
           "rotation": st.angle(0.0), "placement": None, "hardware": None,
           "_detail": {"type_key": "A", "is_l2": role != "combiner", "box_input_count": 0, "colour": colour}}
    if number is not None:
        row["_number"] = number
    return row


def unassigned(handle):
    return {"string": handle, "device": 0, "input": 24, "label": "-", "colour": 254,
            "_detail": {"circuit": "-", "string_number": 0}}


def make_state(xs=(0, 10, 20, 30, 40, 50), devices=None, settings=None, rows=None, annotated=True):
    handles = [f"{0x1000 + i:X}" for i in range(len(xs))]
    devices = [device(0.0, 0.0), device(100.0, 0.0, number=2)] if devices is None else devices
    base = {"InstallationDesign": "Roof", "HomerunRouting": copy.deepcopy(st.HOMERUN_ROUTING_DEFAULT)}
    base.update(settings or {})
    return st.validate_state({
        "format": st.STATE_FORMAT, "source": {"dump_sha256": "0" * 64, "reopened": True},
        "rows": {"device": [copy.deepcopy(d) for d in devices],
                 "string-assignment": rows if rows is not None else [unassigned(h) for h in handles],
                 "cable": [], "schedule": [], "lbd": []},
        "setting": base,
        "geometry": {"strings": [{"string": h, "vertices": [[float(x), 20.0], [float(x), 25.0]],
                                  "start": [float(x), 20.0] if annotated else None, "end": [float(x), 25.0],
                                  "panels": ["93A6"]}
                                 for h, x in zip(handles, xs)],
                     "panel_groups": []}})


def by_string(state):
    return {r["string"]: r for r in state["rows"]["string-assignment"]}


# ----------------------------------------------------------------- parsing --

def test_the_circuit_parser():
    assert ss.parse_circuit("+22/1d") == 1
    assert ss.parse_circuit("A1/4b") == 4
    assert ss.parse_circuit("+33/12a") == 12
    assert ss.parse_circuit("-") == -1
    assert ss.parse_circuit("+3/") == -1
    assert ss.parse_circuit("+3/99999999999a") == -1
    assert ss.parse_circuit(None) == -1


def test_the_mppt_letter_and_the_tag():
    assert ss.mppt_letter(22, 6, 6) == "d"
    assert ss.mppt_letter(37, 6, 6) == "a"          # the next inverter's first MPPT
    assert ss.mppt_letter(5, 0, 6) == "a"           # incomplete topology
    assert ss.tag_text(1, 22, 6, 6) == "+22/1d"
    assert ss.tag_text(7, 173, 6, 6) == "+173/7e"


def test_panel_ids_parse_only_the_three_part_form():
    assert ss.parse_panel_id("93A6") is None
    assert ss.parse_panel_id("P:a5de:3") == ("A5DE", 3)
    assert ss.parse_panel_id("P:a5de:-1") is None


# ------------------------------------------------------------ plan helpers --

def test_geometric_rows_split_on_the_running_mean():
    cables = [{"cx": float(x), "cy": float(y)} for x, y in ((0, 0), (10, 0.5), (0, 100), (10, 100.4))]
    rows = ss.geometric_rows(cables)
    assert [r["key"] for r in rows] == ["GEOROW:1", "GEOROW:2"]
    assert [[(c["cx"], c["cy"]) for c in r["strings"]] for r in rows] == \
        [[(0.0, 0.0), (10.0, 0.5)], [(0.0, 100.0), (10.0, 100.4)]]


def test_seed_outward_walks_forward_then_back():
    items = list("abcdefg")
    assert ss.seed_outward(items, items[3], 5) == ["d", "e", "c", "f", "b"]
    assert ss.seed_outward(items, items[6], 3) == ["g", "f", "e"]
    assert ss.seed_outward(items, items[0], 0) == []


# ------------------------------------------------------------------ assign --

def test_pattern_assignment_fills_collectors_by_x_from_the_nearest_seed():
    before = make_state()
    after, lines = ss.assign_strings(before, HOST, FORMS)
    rows = by_string(after)
    # Collector 1 at x 0 takes the four strings from x 0 outward, numbers 1 to 4, two per MPPT.
    assert [(rows[h]["device"], rows[h]["input"], rows[h]["label"], rows[h]["colour"])
            for h in ("1000", "1001", "1002", "1003")] == \
        [(1, 1, "+1/1a", 2), (1, 1, "+2/1a", 2), (1, 2, "+3/1b", 2), (1, 2, "+4/1b", 2)]
    # Collector 2 at x 100 seeds on x 50 and walks back: numbers 5 and 6, its MPPT a.
    assert [(rows[h]["device"], rows[h]["label"], rows[h]["colour"]) for h in ("1005", "1004")] == \
        [(2, "+5/2a", 5), (2, "+6/2a", 5)]
    assert rows["1000"]["_detail"]["circuit"] == "+1/1a" and rows["1000"]["_detail"]["strings_on_inverter"] == 4
    colours = [d["_detail"]["colour"] for d in after["rows"]["device"]]
    assert sorted(colours) == [2, 5]
    assert lines[-1] == "Pattern assignment complete. 6 strings assigned to 2 collectors in 1 seconds."
    # The one drawing-properties save: the auto tag height and one appended default catalog.
    assert after["setting"]["TagHeight"] == 12.5
    assert after["setting"]["HomerunRouting"]["CableCatalog"] == st.DEFAULT_CATALOG * 2
    assert before["rows"]["string-assignment"][0]["label"] == "-"      # the input is untouched


def test_the_step_delta_is_every_string_and_the_saved_settings():
    before = make_state()
    after, lines = ss.assign_strings(before, HOST, FORMS)
    rows, settings = st.step_rows("i2", before, after, lines)
    kinds = [r["type"] for r in rows]
    assert kinds.count("string-assignment") == 6 and kinds.count("device") == 0
    assert [s["fields"]["name"] for s in settings] == ["HomerunRouting", "TagHeight"]
    first = next(r for r in rows if r["type"] == "string-assignment")
    assert first["change"] == "changed" and first["string"] == "1000" and "_detail" not in first


def test_numbering_continues_past_the_largest_label():
    state = make_state()
    state["rows"]["string-assignment"][0]["_detail"]["string_number"] = 40
    after, _ = ss.assign_strings(state, HOST, FORMS)
    assert by_string(after)["1000"]["label"] == "+41/1a"


def test_a_collector_holding_strings_keeps_its_block_colour():
    rows = [unassigned(f"{0x1000 + i:X}") for i in range(6)]
    rows[5].update({"device": 2, "input": 1, "label": "+1/2a", "colour": 3,
                    "_detail": {"circuit": "+1/2a", "string_number": 1}})
    state = make_state(devices=[device(0.0, 0.0), device(100.0, 0.0, number=2, colour=3)], rows=rows)
    after, _ = ss.assign_strings(state, HOST, FORMS)
    assigned = by_string(after)
    assert assigned["1004"]["device"] == 2 and assigned["1004"]["colour"] == 3
    assert assigned["1000"]["colour"] == 2          # collector 1 still takes the counter's colour


def test_the_dialog_answers():
    state = make_state()
    same, lines = ss.assign_strings(state, HOST, dict(FORMS, assign_strings_to_central_inverters="Cancel"))
    assert same == state and lines == []
    same, lines = ss.assign_strings(state, HOST, dict(FORMS, excess_capacity="No"))
    assert same["rows"] == state["rows"] and lines == ["Auto-assignment cancelled."]
    with pytest.raises(ss.InverterStringNotPortedError, match="manual"):
        ss.assign_strings(state, HOST, dict(FORMS, assign_strings_to_central_inverters_mode="Manual"))
    with pytest.raises(ss.InverterStringNotPortedError, match="cloud"):
        ss.assign_strings(state, dict(HOST, UsePatternStringAssignment=False), FORMS)


def test_not_enough_capacity_assigns_nothing_and_exact_capacity_skips_the_dialog():
    short = make_state(xs=tuple(range(0, 90, 10)))
    same, lines = ss.assign_strings(short, HOST, FORMS)
    assert same["rows"] == short["rows"] and lines == []
    exact = make_state(xs=tuple(range(0, 80, 10)))
    after, lines = ss.assign_strings(exact, HOST, {k: v for k, v in FORMS.items() if k != "excess_capacity"})
    assert lines[0] == "8 unassigned strings, 8 available slots across 2 central inverter(s)."
    assert all(r["device"] in (1, 2) for r in after["rows"]["string-assignment"])


def test_refusals():
    with pytest.raises(ss.InverterStringError, match="number is unknown"):
        ss.assign_strings(make_state(), dict(HOST, DeviceNumbers=[]), FORMS)
    with pytest.raises(ss.InverterStringNotPortedError, match="electrical zones"):
        ss.assign_strings(make_state(settings={"ElectricalZones": [{"Name": "Z"}]}), HOST, FORMS)
    with pytest.raises(ss.InverterStringNotPortedError, match="hydration"):
        ss.assign_strings(make_state(annotated=False), HOST, FORMS)
    with pytest.raises(ss.InverterStringError, match="host inputs"):
        ss.assign_strings(make_state(), dict(HOST, Surprise=1), FORMS)
    with pytest.raises(ss.InverterStringError, match="OK or Cancel"):
        ss.assign_strings(make_state(), HOST, {})


# ------------------------------------------------------------------- colour --

def test_colour_strings_by_inverter_number():
    assigned, _ = ss.assign_strings(make_state(), HOST, FORMS)
    after, lines = ss.color_strings(assigned, HOST)
    rows = by_string(after)
    assert {rows[h]["colour"] for h in ("1000", "1001", "1002", "1003")} == {1}    # untyped[(1 - 1) % 6]
    assert {rows[h]["colour"] for h in ("1004", "1005")} == {2}                    # untyped[(2 - 1) % 6]
    assert lines == ["LEAFCOLORSTRINGS: recoloured 6 of 6 strings across 2 inverter(s); "
                     "drew 0 in-block module overlay(s)."]
    assert after["setting"] == assigned["setting"]                               # no settings save


def test_colour_strings_unassigned_and_unannotated():
    state = make_state(xs=(0, 10))
    state["geometry"]["strings"][1]["start"] = None
    after, lines = ss.color_strings(state, {})
    rows = by_string(after)
    assert rows["1000"]["colour"] == 7 and rows["1001"]["colour"] == 254
    assert lines[0].startswith("LEAFCOLORSTRINGS: recoloured 1 of 2 strings across 0 inverter(s)")


def test_colour_strings_follow_the_type_family():
    assigned, _ = ss.assign_strings(make_state(), HOST, FORMS)
    assigned["setting"]["InverterTypeAssignments"] = {"2": "B"}
    assigned["setting"]["InverterTypes"] = {"B": {"TypeKey": "B"}}
    after, _ = ss.color_strings(assigned, HOST)
    assert by_string(after)["1004"]["colour"] == 4          # (5, 4, 3)[(2 - 1) % 3]


def test_no_strings_prints_the_layer():
    state = make_state(xs=())
    after, lines = ss.color_strings(state, {})
    assert lines == ["LEAFCOLORSTRINGS: no strings found on layer String."]
    assert json.loads(st.canonical(st.publish(after))) == json.loads(st.canonical(st.publish(state)))
