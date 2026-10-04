"""Frozen local graph parity cases; source data is only a membership witness."""
import copy
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import solar_design_graph as sdg
import solar_pvcase_conversion as conversion
import solar_pvcase_graph as pvg
import solar_pvcase_outputs as outputs
import solar_pvcase_solve as raw
import solar_solve_results as results
from test_solar_pvcase_conversion import seed, ground_envelope, canonical
from test_w1_design_graph import graph  # noqa: F401

GRAPH_CODES = (
    "PVG_INVALID_TARGET", "PVG_INVALID_SOURCE", "PVG_TARGET_CONTEXT",
    "FRAME_MEMBERSHIP_MISMATCH", "MATRIX_CELL_MISMATCH", "PVG_TARGET_NOT_EMPTY",
    "PVG_INVALID_INTAKE", "PVG_GEOMETRY_RANGE", "PVG_INVALID_RESULT",
)
ROOT = Path(__file__).resolve().parents[2]


def converted(kind="ground"):
    if kind == "roof":
        envelope = {"schema": conversion.INPUT_SCHEMA, "intake": json.loads(
            (ROOT / "docs/parity/evidence/rooftop/pvcase/intake.json").read_text(encoding="utf-8"))}
        value = seed("in", "Roof")
    elif kind == "twelves":
        envelope = {"schema": conversion.INPUT_SCHEMA, "intake": {
            "units": "in", "panels_per_string": 0, "panel_groups": [{
                "handle": "A1", "installation": "Roof", "row_angle_rad": 0.0,
                "sequences": [], "panel_size": {"height_across_row": 38.5, "width_along_row": 77.0},
                "rows": [[{"code": 1, "id": f"{i:X}", "seq": 0, "inverter_id": -1,
                           "string_input_number": 0, "x": 0.0, "y": 0.0}
                          for i in range(1, 26)]]}]}}
        value = seed("in", "Roof")
    else:
        envelope, value = ground_envelope(), seed()
    return conversion.convert(value, envelope, source_artifact_id="b" * 64,
                              source_sha256=hashlib.sha256(canonical(envelope)).hexdigest()), envelope


def refused(code, operation):
    with pytest.raises(sdg.GraphValidationError) as caught:
        operation()
    error = caught.value
    assert error.code == code and error.path == "<root>"
    assert str(error) == code + ": <root>" and error.__cause__ is None


def membership_variant(value, kind):
    """Coherent graph edits which ordinary validation cannot identify as source drift."""
    value = copy.deepcopy(value)
    frame = value["frames"][0]
    if kind == "remove":
        removed = value["panels"].pop(0)
        ref = removed["id"]
        frame["panel_refs"].remove(ref)
        frame["panel_assignments"] = [a for a in frame["panel_assignments"] if a["panel_ref"] != ref]
        r, c = removed["matrix_cell"]["row"], removed["matrix_cell"]["col"]
        frame["matrix"][r][c] = conversion._empty_cell()
        for string in value["strings"]:
            if ref in string["ordered_panel_refs"]:
                string["ordered_panel_refs"].remove(ref)
                string["from_ref"] = string["ordered_panel_refs"][0]
                string["to_ref"] = string["ordered_panel_refs"][-1]
    elif kind == "add":
        panel = copy.deepcopy(value["panels"][0])
        panel["id"] = "leaf:panel:00000000-0000-4000-8000-000000000099"
        panel["provenance"]["source_handle"] = "FF"
        panel["provenance"]["pvcase"].update(row=0, col=1)
        panel["matrix_cell"] = {"row": 0, "col": 1}
        panel["assignment"] = {"string_ref": None, "seq": None}
        value["panels"].append(panel)
        frame["panel_refs"].append(panel["id"])
        frame["panel_assignments"].append(conversion._assignment(panel))
        frame["matrix"][0][1] = conversion._occupied(panel)
        if value["strings"]:
            value["strings"][0]["ordered_panel_refs"].append(panel["id"])
            value["strings"][0]["to_ref"] = panel["id"]
    else:
        frame["matrix"][0][0], frame["matrix"][1][0] = frame["matrix"][1][0], frame["matrix"][0][0]
        for panel in value["panels"]:
            row = 1 - panel["matrix_cell"]["row"]
            panel["matrix_cell"]["row"] = row
            panel["provenance"]["pvcase"]["row"] = row
    results.sync_assignments(value)
    assert sdg.validate_graph(value) == value
    return value


def test_pvg_roof_totals():
    value, envelope = converted("roof")
    solved = pvg.solve_graph(value, envelope)
    d, g = solved["diagnostics"], solved["graph"]
    assert tuple(d[k] for k in ("panels_assigned", "strings_created", "written", "l2_count")) == (2345, 88, 2345, 0)
    assert d["status"] == "solved" and len(g["frames"]) == 11
    assert len(g["panels"]) == 2345 and len(g["strings"]) == 88 and not g["inverters"]
    assert sdg.validate_graph(g) == g


def test_pvg_roof_group_numbers():
    value, envelope = converted("roof")
    g = pvg.solve_graph(value, envelope)["graph"]
    by_id = {s["id"]: s for s in g["strings"]}
    first, final = [], []
    for frame in g["frames"]:
        numbers = [by_id[s["string_ref"]]["extra"]["pvcase"]["parity_string_number"] for s in frame["sequences"]]
        first.append(numbers[0])
        final.append(numbers[-1])
        for record in frame["panel_assignments"] + [c for row in frame["matrix"] for c in row]:
            assert record["inverter_id"] is None and record["string_input_number"] is None
    assert first == [1, 8, 13, 18, 23, 31, 35, 43, 47, 51, 69]
    assert final == [7, 12, 17, 22, 30, 34, 42, 46, 50, 68, 88]
    assert all(s["extra"]["pvcase"]["parity_l2_number"] == 1 and s["inverter_ref"] is None for s in g["strings"])


def test_pvg_default_twelves():
    value, envelope = converted("twelves")
    g = pvg.solve_graph(value, envelope)["graph"]
    strings = {s["id"]: s for s in g["strings"]}
    assert [strings[p["assignment"]["string_ref"]]["extra"]["pvcase"]["parity_string_number"]
            for p in g["panels"]] == [1] * 12 + [2] * 12 + [3]
    assert [s["module_count"] for s in g["strings"]] == [12, 12, 1]


def test_pvg_current_geometry(monkeypatch):
    value, envelope = converted("twelves")
    value["panels"][0]["centre"] = [3.0, 4.0]
    assert value["frames"][0]["matrix"][0][0]["x"] == 0.0
    value["frames"][0]["module_width_along_row"] = 9.0
    value["panels"][0]["angle"] = 90.0
    original, calls = raw.solve, []
    def spy(groups, l2, length):
        calls.append((groups[0]["panels"][0].x, groups[0]["panels"][0].y,
                      groups[0]["panels"][0].width_along_row, groups[0]["row_angle_rad"]))
        return original(groups, l2, length)
    monkeypatch.setattr(raw, "solve", spy)
    g = pvg.solve_graph(value, envelope)["graph"]
    assert calls == [(3.0, 4.0, 9.0, pytest.approx(1.5707963267948966))]
    assert g["strings"][0]["route"][0] == [3.0, 4.0]
    assert g["strings"][0]["length_ft"] == 16.404199475065617


def test_pvg_deterministic_identity():
    value, envelope = converted("roof")
    a = pvg.solve_graph(value, envelope)["graph"]
    b = pvg.solve_graph(value, envelope)["graph"]
    assert a == b and outputs.assignment_export(a, envelope) == outputs.assignment_export(b, envelope)
    assert a["strings"][0]["id"] == "leaf:string:048a715f-5705-4548-aa9d-a09bf1b01a59"


def test_pvg_isolation():
    value, envelope = converted("twelves")
    value["future"] = {"keep": [1, 2]}
    value["panels"][0]["matrix_cell"]["future"] = True
    before = copy.deepcopy((value, envelope))
    projection = pvg.project_graph(value, envelope)
    solved = pvg.solve_graph(value, envelope)["graph"]
    assert (value, envelope) == before
    assert solved["rev"] == value["rev"] and solved["parent_rev"] == value["parent_rev"]
    assert solved["future"] == value["future"] and solved["settings"] == value["settings"]
    assert [p["provenance"] for p in solved["panels"]] == [p["provenance"] for p in value["panels"]]
    projection["panel_groups"][0]["panels"][0].x = 999
    solved["strings"][0]["route"][0][0] = 999
    solved["future"]["keep"].append(3)
    assert (value, envelope) == before


def test_pvg_ground_holes():
    value, envelope = converted()
    projection = pvg.project_graph(value, canonical(envelope))
    assert [(p.row_index, p.col_index) for p in projection["panel_groups"][0]["panels"]] == [(0, 0), (1, 0)]
    g = pvg.solve_graph(value, envelope)["graph"]
    assert len(g["frames"]) == 1 and len(g["panels"]) == 2
    assert [s["module_count"] for s in g["strings"]] == [2]
    assert g["frames"][0]["matrix"][0][1] == value["frames"][0]["matrix"][0][1]
    assert "tracker" not in g["frames"][0] and not g["inverters"]


def test_pvg_removed_membership():
    value, envelope = converted()
    changed = membership_variant(value, "remove")
    before = copy.deepcopy(changed)
    refused("FRAME_MEMBERSHIP_MISMATCH", lambda: pvg.solve_graph(changed, envelope))
    assert changed == before


def test_pvg_added_membership():
    value, envelope = converted()
    changed = membership_variant(value, "add")
    refused("FRAME_MEMBERSHIP_MISMATCH", lambda: pvg.project_graph(changed, envelope))


def test_pvg_reordered_membership():
    value, envelope = converted()
    changed = membership_variant(value, "reorder")
    refused("MATRIX_CELL_MISMATCH", lambda: pvg.solve_graph(changed, envelope))


def test_pvg_source_and_context():
    value, envelope = converted()
    for key in ("normalized_intake_sha256", "source_artifact_id", "source_sha256"):
        changed = copy.deepcopy(value)
        changed["extra"]["pvcase"][key] = "c" * 64
        refused("PVG_INVALID_SOURCE", lambda: pvg.project_graph(changed, envelope))
    changed = copy.deepcopy(value)
    changed["panels"][0]["provenance"]["source_handle"] = "EE"
    refused("PVG_INVALID_SOURCE", lambda: pvg.project_graph(changed, envelope))
    for mutate in (
        lambda g: g["project"]["units"].update(compute_units="ft"),
        lambda g: g["project"]["units"].update(crs="EPSG:4326"),
        lambda g: g["project"]["units"]["wcs_to_ucs"].__setitem__(12, 1),
        lambda g: g["project"]["units"].update(drawing_units="ft"),
        lambda g: g["panels"][0].update(centre=[1, 2, 3]),
    ):
        changed = copy.deepcopy(value)
        mutate(changed)
        refused("PVG_TARGET_CONTEXT", lambda: pvg.project_graph(changed, envelope))
    changed = copy.deepcopy(value)
    changed["project"].update(installation_design="Roof")
    # Graph validation refuses a Roof design carrying frames before any context check.
    refused("PVG_INVALID_TARGET", lambda: pvg.project_graph(changed, envelope))
    changed = copy.deepcopy(value)
    changed["extra"]["pvcase"]["panels_per_string"] = True
    refused("PVG_INVALID_INTAKE", lambda: pvg.project_graph(changed, envelope))
    refused("PVG_INVALID_SOURCE", lambda: pvg.project_graph(value, {}))


def test_pvg_invalid_graph(monkeypatch):
    value, envelope = converted()
    for malformed in ({}, None, {**value, "graph_schema_version": 2}):
        before = copy.deepcopy(malformed)
        refused("PVG_INVALID_TARGET", lambda: pvg.project_graph(malformed, envelope))
        assert malformed == before
    monkeypatch.setattr(sdg, "MAX_NODES", 10)
    before = copy.deepcopy(value)
    refused("PVG_INVALID_TARGET", lambda: pvg.solve_graph(value, envelope))
    assert value == before


def test_pvg_existing_engineering(graph):
    value, envelope = converted()
    solved = pvg.solve_graph(value, envelope)["graph"]
    refused("PVG_TARGET_NOT_EMPTY", lambda: pvg.solve_graph(solved, envelope))
    for key in ("inverters", "routes", "schedules"):
        changed = copy.deepcopy(value)
        item = copy.deepcopy(graph[key][0])
        if key == "inverters":
            item["input_assignments"] = []
        elif key == "routes":
            item.update(from_ref=None, to_ref=None)
        else:
            item["source_refs"] = []
        changed[key].append(item)
        assert sdg.validate_graph(changed) == changed
        before = copy.deepcopy(changed)
        refused("PVG_TARGET_NOT_EMPTY", lambda: pvg.solve_graph(changed, envelope))
        assert changed == before


def test_pvg_geometry_range():
    value, envelope = converted()
    value["panels"][0]["centre"] = [1.7e308, 0.0]
    value["panels"][1]["centre"] = [-1.7e308, 0.0]
    assert sdg.validate_graph(value) == value
    before = copy.deepcopy(value)
    refused("PVG_GEOMETRY_RANGE", lambda: pvg.solve_graph(value, envelope))
    assert value == before


def test_pvg_kernel_and_result_boundary(monkeypatch):
    value, envelope = converted("twelves")
    original, calls = raw.solve, []
    def spy(groups, equipment, length):
        calls.append((equipment, length))
        return original(groups, equipment, length)
    monkeypatch.setattr(raw, "solve", spy)
    pvg.solve_graph(value, envelope)
    assert calls == [([], 12)]
    def bad_count(groups, equipment, length):
        result = original(groups, equipment, length)
        result["panels_assigned"] -= 1
        return result
    def bad_assignment(groups, equipment, length):
        result = original(groups, equipment, length)
        groups[0]["panels"][0].l2_number = 2
        return result
    def throwing(*args):
        raise ValueError("private payload")
    for kernel in (bad_count, bad_assignment, throwing):
        monkeypatch.setattr(raw, "solve", kernel)
        refused("PVG_INVALID_RESULT", lambda: pvg.solve_graph(value, envelope))
    monkeypatch.setattr(raw, "solve", original)
    validate = sdg.validate_graph
    def reject_final(g):
        if g.get("strings"):
            raise sdg.GraphValidationError("private payload")
        return validate(g)
    monkeypatch.setattr(sdg, "validate_graph", reject_final)
    refused("PVG_INVALID_RESULT", lambda: pvg.solve_graph(value, envelope))


def test_pvg_refusal_tuple():
    assert pvg.CODES == GRAPH_CODES
    value, envelope = converted()
    before = copy.deepcopy((value, envelope))
    refused("PVG_INVALID_SOURCE", lambda: pvg.solve_graph(value, b"private payload"))
    refused("PVG_INVALID_TARGET", lambda: pvg.project_graph({"private payload": 1}, envelope))
    assert (value, envelope) == before

def test_pvg_mixed_dimension_centres():
    value, envelope = converted()
    planar = copy.deepcopy(value)
    planar["panels"][0]["centre"] = [0.0, 0.0]
    planar["panels"][1]["centre"] = [3.0, 4.0]
    value["panels"][0]["centre"] = [0.0, 0.0, 0.0]
    value["panels"][1]["centre"] = [3.0, 4.0]
    assert sdg.validate_graph(value) == value
    before = copy.deepcopy(value)
    mixed = pvg.solve_graph(value, envelope)["graph"]["strings"]
    flat = pvg.solve_graph(planar, envelope)["graph"]["strings"]
    assert [s["length_ft"] for s in mixed] == [s["length_ft"] for s in flat] == [16.404199475065617]
    assert mixed[0]["route"] == [[0.0, 0.0, 0.0], [3.0, 4.0]]
    assert value == before


def test_pvg_arithmetic_kernel_error(monkeypatch):
    value, envelope = converted()
    before = copy.deepcopy((value, envelope))
    for error in (ZeroDivisionError, FloatingPointError):
        def kernel(*args, _error=error):
            raise _error("PRIVATE_DATA")
        monkeypatch.setattr(raw, "solve", kernel)
        refused("PVG_INVALID_RESULT", lambda: pvg.solve_graph(value, envelope))
    assert (value, envelope) == before
