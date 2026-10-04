"""Frozen current-assignment export and electrical boundary cases."""
import copy
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import solar_design_graph as sdg
import solar_pvcase_graph as pvg
import solar_pvcase_outputs as pvo
from test_solar_pvcase_graph import converted, refused, membership_variant, GRAPH_CODES

OUTPUT_COLUMNS = (
    "panel_handle", "parity_l2_number", "parity_string_number", "group_handle",
    "frame_ref", "panel_ref", "string_ref", "row", "col", "centre_m", "inverter_ref",
)


def solved(kind="roof"):
    graph, envelope = converted(kind)
    return pvg.solve_graph(graph, envelope)["graph"], envelope


def reverse_keys(value):
    if type(value) is dict:
        return {k: reverse_keys(v) for k, v in reversed(list(value.items()))}
    if type(value) is list:
        return [reverse_keys(v) for v in value]
    return value


def test_pvo_raw_columns():
    graph, envelope = solved()
    rows = json.loads(pvo.assignment_export(graph, envelope))["rows"]
    oracle = pvg.raw.pvcase_solve(envelope["intake"])
    expected = [row for group in oracle["panel_groups"] for row in pvg.raw.panel_assignments(group)]
    assert len(rows) == 2345 and [row[:3] for row in rows] == expected
    assert rows[0][:3] == ["8891", 1, 1]


def test_pvo_roof_bytes():
    graph, envelope = solved()
    data = pvo.assignment_export(graph, envelope)
    assert len(data) == 475691
    assert hashlib.sha256(data).hexdigest() == "5ad8c994799081dd33dabc25dd50772feeb946f2f3e3caf56219400c51cf209a"
    assert pvo.COLUMNS == OUTPUT_COLUMNS
    assert json.loads(data)["columns"] == list(OUTPUT_COLUMNS)
    assert data.endswith(b"\n") and not data.endswith(b"\n\n")
    assert b"\r" not in data and not data.startswith(b"\xef\xbb\xbf")
    assert pvg.canonical_json(json.loads(data)) + b"\n" == data


def test_pvo_current_geometry(monkeypatch):
    graph, envelope = solved()
    old_route = copy.deepcopy(graph["strings"][0]["route"])
    graph["panels"][0]["centre"] = [6.0, 8.0]
    def forbidden(*args, **kwargs):
        raise AssertionError("export must never solve")
    monkeypatch.setattr(pvg.raw, "solve", forbidden)
    rows = json.loads(pvo.assignment_export(graph, envelope))["rows"]
    assert rows[0][9] == [6.0, 8.0]
    assert graph["strings"][0]["route"] == old_route and old_route[0] != [6.0, 8.0]


def test_pvo_requires_solve():
    graph, envelope = converted()
    refused("PVG_INVALID_RESULT", lambda: pvo.assignment_export(graph, envelope))
    graph, envelope = solved("ground")
    for mutate in (
        lambda g: g["extra"].pop("pvcase_solve"),
        lambda g: g["strings"][0]["extra"].pop("pvcase"),
        lambda g: g["strings"][0]["extra"]["pvcase"].update(parity_l2_number=2),
        lambda g: g["strings"][0]["extra"]["pvcase"].update(parity_string_number=0),
        lambda g: g["extra"]["pvcase_solve"].update(written=1),
    ):
        changed = copy.deepcopy(graph)
        mutate(changed)
        refused("PVG_INVALID_RESULT", lambda: pvo.assignment_export(changed, envelope))
    changed = copy.deepcopy(graph)
    changed["frames"][0]["sequences"][0]["ordered_panel_refs"].reverse()
    refused("FRAME_SEQUENCE_MISMATCH", lambda: pvo.assignment_export(changed, envelope))


def test_pvo_membership_refusal():
    graph, envelope = solved("ground")
    for kind, code in (("remove", "FRAME_MEMBERSHIP_MISMATCH"),
                       ("add", "FRAME_MEMBERSHIP_MISMATCH"), ("reorder", "MATRIX_CELL_MISMATCH")):
        changed = membership_variant(graph, kind)
        before = copy.deepcopy((changed, envelope))
        refused(code, lambda: pvo.assignment_export(changed, envelope))
        assert (changed, envelope) == before


def test_pvo_electrical_boundary():
    graph, envelope = solved()
    settings = copy.deepcopy(graph["settings"])
    exported = json.loads(pvo.assignment_export(graph, envelope))
    assert set(exported) == {"schema", "columns", "rows", "electrical_sizing"}
    assert exported["electrical_sizing"] == "not-evaluated"
    assert exported["columns"] == list(OUTPUT_COLUMNS)
    assert all(row[-1] is None for row in exported["rows"])
    assert not any(word in column for word in ("wire", "voltage", "power", "collector", "cable", "sizing")
                   for column in exported["columns"])
    assert graph["settings"] == settings
    for mutate in (
        lambda g: g["frames"][0]["panel_assignments"][0].update(string_input_number=1),
        lambda g: g["frames"][0]["matrix"][0][0].update(string_input_number=1),
        lambda g: g["strings"][0].update(inverter_ref="leaf:inverter:00000000-0000-4000-8000-000000000099"),
    ):
        changed = copy.deepcopy(graph)
        mutate(changed)
        before = copy.deepcopy(changed)
        refused("MATRIX_INPUT_MISMATCH", lambda: pvo.assignment_export(changed, envelope))
        assert changed == before


def test_pvo_determinism_isolation():
    graph, envelope = solved()
    before = copy.deepcopy((graph, envelope))
    first = pvo.assignment_export(graph, envelope)
    assert first == pvo.assignment_export(graph, envelope)
    assert first == pvo.assignment_export(reverse_keys(graph), reverse_keys(envelope))
    assert (graph, envelope) == before


def test_pvo_codes_and_byte_limit(monkeypatch):
    assert pvo.CODES == GRAPH_CODES + ("FRAME_SEQUENCE_MISMATCH", "MATRIX_INPUT_MISMATCH", "PVG_BYTE_LIMIT")
    graph, envelope = solved()
    before = copy.deepcopy((graph, envelope))
    # Lower only the output ceiling: the much larger graph must still pass its own admission.
    class OutputCeiling:
        MAX_BYTES = 475690
    monkeypatch.setattr(pvo, "contract", OutputCeiling)
    refused("PVG_BYTE_LIMIT", lambda: pvo.assignment_export(graph, envelope))
    assert (graph, envelope) == before
