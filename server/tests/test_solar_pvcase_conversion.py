"""Frozen G33 conversion cases: geometry, evidence, isolation and admission budgets."""
import copy
import hashlib
import json
import math
from pathlib import Path
import sys

import pytest
from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import solar_design_graph as sdg
import solar_pvcase_conversion as pvg
import solar_pvcase_solve as solver
from solar_graph_seed import new_empty_graph
from test_w1_design_graph import graph  # noqa: F401; unchanged W1 fixture

ROOT = Path(__file__).resolve().parents[2]
ROOF_HASH = "364eeaca982cc3eaed58628c1d6c81ac6640ff47ba70e2fb2142991f46f27a41"
GROUND_HASH = "d0ab9cc5c5560c14ce9cf07c31589aac8622ae83bd17c939d4e63cc42a511774"
ROOF_ROWS = [
    ("A646", 174, 15, 13, 195), ("A63B", 123, 21, 13, 273),
    ("A631", 134, 16, 13, 208), ("A627", 137, 10, 15, 150),
    ("A61D", 207, 15, 32, 480), ("A612", 99, 13, 10, 130),
    ("A608", 213, 21, 22, 462), ("A5FE", 104, 18, 7, 126),
    ("A5F4", 111, 9, 15, 135), ("A5EA", 487, 30, 88, 2640),
    ("A5DE", 556, 31, 160, 4960),
]


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def cell(handle="001", *, code=1, x=1, y=2):
    return {"code": code, "id": handle, "inverter_id": 7, "seq": 9,
            "string_input_number": 3, "x": x, "y": y}


def ground_envelope():
    return {"schema": pvg.INPUT_SCHEMA, "intake": {
        "units": "m", "panels_per_string": -4, "panel_groups": [{
            "handle": "00a", "installation": "Ground",
            "panel_size": {"height_across_row": 2, "width_along_row": 1},
            "row_angle_rad": 0, "sequences": [2, 0, -1],
            "rows": [[cell(), None, {"code": 0, "id": "002", "inverter_id": 8,
                                      "seq": 4, "string_input_number": 5, "x": 5, "y": 2}],
                     [{"code": -1, "id": "003", "inverter_id": -1, "seq": -2,
                       "string_input_number": 0, "x": 1, "y": 4}], []]}]}}


def seed(unit="m", design="Ground"):
    value = new_empty_graph(
        tenant_id="fixture-tenant", drawing_id="solar", source_hash="a" * 64,
        created_at="2026-09-26T00:00:00Z",
        units={"drawing_units": unit, "wcs_to_ucs": pvg.IDENTITY[:],
               "elevation_datum": "unknown", "crs": None})
    value["project"]["installation_design"] = design
    return sdg.validate_graph(value)


def convert(value=None, envelope=None, **binding):
    envelope = ground_envelope() if envelope is None else envelope
    value = seed() if value is None else value
    return pvg.convert(value, envelope, source_artifact_id=binding.get("source_artifact_id", "b" * 64),
                       source_sha256=binding.get("source_sha256", hashlib.sha256(
                           envelope if type(envelope) is bytes else canonical(envelope)).hexdigest()))


def refused(code, operation):
    with pytest.raises(sdg.GraphValidationError) as caught:
        operation()
    assert caught.value.code == code
    assert caught.value.path == "<root>"
    assert str(caught.value) == code + ": <root>"
    assert caught.value.__cause__ is None


def walk(value):
    # Independent reproduction of the graph walk, not the converter's accounting helper.
    nodes = size = depth = 0
    stack = [(value, 0)]
    while stack:
        item, level = stack.pop()
        nodes += 1
        depth = max(depth, level)
        if type(item) is dict:
            stack.extend((k, level + 1) for k in item)
            stack.extend((v, level + 1) for v in item.values())
        elif type(item) is list:
            stack.extend((v, level + 1) for v in item)
        elif type(item) is str:
            size += len(item.encode("utf-8"))
        elif type(item) in (int, float):
            size += 24
        else:
            size += 5
    return nodes, size, len(canonical(value)), depth


def trap_materializer(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("expanded result must not be allocated on refusal")
    monkeypatch.setattr(pvg, "_materialize", forbidden)


def capture_preflight(monkeypatch):
    original = pvg._preflight
    measures = []
    def capture(*args):
        measurement = original(*args)
        measures.append(measurement)
        return measurement
    monkeypatch.setattr(pvg, "_preflight", capture)
    return measures


def nested(depth):
    result = None
    for _ in range(depth):
        result = [result]
    return result


def test_pvg_envelope_accepts_bytes_and_object():
    envelope = ground_envelope()
    before = copy.deepcopy(envelope)
    obj = pvg.validate_envelope(envelope)
    raw = pvg.validate_envelope(canonical(envelope))
    assert obj == raw == {"schema": pvg.INPUT_SCHEMA,
                          "intake": solver.validate_intake(envelope["intake"])}
    assert obj is not envelope and obj["intake"] is not envelope["intake"]
    group = obj["intake"]["panel_groups"][0]
    assert type(group["row_angle_rad"]) is float
    assert type(group["rows"][0][0]["x"]) is float
    assert group["handle"] == "00a" and group["rows"][0][0]["id"] == "001"
    group["rows"][0][0]["x"] = 123
    assert raw != obj and envelope == before
    envelope["intake"]["panel_groups"][0]["rows"][0][0]["x"] = 10 ** 30
    assert pvg.validate_envelope(envelope)["intake"] == solver.validate_intake(envelope["intake"])


def test_pvg_envelope_refuses_bad_json():
    for raw in (b"\xff", b"{", b'{"schema":1,"schema":2}', b"NaN", b"Infinity",
                b"-Infinity", b"1e999", b"-1e999", b"9" * 5000, b'"\\ud800"'):
        refused("PVG_INVALID_JSON", lambda: pvg.validate_envelope(raw))
    cycle = {}
    cycle["self"] = cycle
    for value in (cycle, {"x": set()}, {1: None}, {"x": object()}, {"x": float("nan")},
                  {"x": float("inf")}, bytearray(b"{}")):
        refused("PVG_INVALID_JSON", lambda: pvg.validate_envelope(value))


def test_pvg_envelope_refuses_keys_and_versions():
    for value in (None, [], "intake.json", {}, {"schema": pvg.INPUT_SCHEMA},
                  {**ground_envelope(), "extra": 1}):
        refused("PVG_ENVELOPE_FIELDS", lambda: pvg.validate_envelope(value))
    for version in ("leaf.pvcase-g33.v2", 1, None, True, {}, []):
        value = ground_envelope()
        value["schema"] = version
        refused("PVG_ENVELOPE_SCHEMA", lambda: pvg.validate_envelope(value))


def test_pvg_reuses_nested_validator(monkeypatch):
    original = solver.validate_intake
    calls = []
    def spy(value):
        calls.append(value)
        return original(value)
    monkeypatch.setattr(solver, "validate_intake", spy)
    envelope = ground_envelope()
    assert pvg.validate_envelope(envelope)["intake"] == original(envelope["intake"])
    assert calls == [envelope["intake"]]
    mutations = [
        lambda i: i.update(version=1), lambda i: i.update(units="yd"),
        lambda i: i.update(panels_per_string=True),
        lambda i: i.update(panels_per_string=2147483648),
        lambda i: i["panel_groups"][0].update(handle="A\n"),
        lambda i: i["panel_groups"][0].update(rows=[None]),
        lambda i: i["panel_groups"][0].update(sequences=[500001]),
        lambda i: i["panel_groups"][0]["panel_size"].update(width_along_row=0),
        lambda i: i["panel_groups"][0]["panel_size"].update(height_across_row=True),
        lambda i: i["panel_groups"][0]["rows"][0][0].update(seq=False),
        lambda i: i["panel_groups"][0]["rows"][0][0].update(x=True),
        lambda i: i["panel_groups"][0]["rows"][0][0].update(extra=1),
        lambda i: i["panel_groups"][0]["rows"][0][2].update(id="0001"),
        lambda i: i["panel_groups"].append(copy.deepcopy(i["panel_groups"][0])),
    ]
    for mutate in mutations:
        value = ground_envelope()
        mutate(value["intake"])
        before = copy.deepcopy(value)
        refused("PVG_INVALID_INTAKE", lambda: pvg.validate_envelope(value))
        assert value == before
    for error in (TypeError("private"), OverflowError("private")):
        def broken(_):
            raise error
        monkeypatch.setattr(solver, "validate_intake", broken)
        refused("PVG_INVALID_INTAKE", lambda: pvg.validate_envelope(envelope))


def test_pvg_envelope_limits(monkeypatch):
    def forbidden(_):
        raise AssertionError("normalization must follow admission")
    monkeypatch.setattr(solver, "validate_intake", forbidden)
    refused("PVG_INPUT_BYTES_EXCEEDED", lambda: pvg.validate_envelope(b" " * (16777216 + 1)))
    refused("PVG_DEPTH_LIMIT", lambda: pvg.validate_envelope(nested(34)))
    refused("PVG_DEPTH_LIMIT", lambda: pvg.validate_envelope(b"[" * 34 + b"0" + b"]" * 34))
    refused("PVG_LIST_LIMIT", lambda: pvg.validate_envelope([None] * 100001))
    with monkeypatch.context() as patch:
        patch.setattr(pvg, "MAX_NODES", 79)  # ground_envelope() holds 80 nodes.
        refused("PVG_NODE_LIMIT", lambda: pvg.validate_envelope(ground_envelope()))
    with monkeypatch.context() as patch:
        patch.setattr(pvg, "MAX_NODES", 80)
        pvg._guard(ground_envelope())
    # The two budgets bracket the fixture's exact count, so either comparison drifting by
    # one refuses 80 or admits 80 at 79.
    assert walk(ground_envelope())[0] == 80
    # Brackets and escaped quotes inside JSON strings are not structural nesting.
    refused("PVG_ENVELOPE_FIELDS", lambda: pvg.validate_envelope(canonical({"text": '["' * 100})))


def test_pvg_committed_roof():
    intake = json.loads((ROOT / "docs/parity/evidence/rooftop/pvcase/intake.json").read_text(encoding="utf-8"))
    envelope = {"schema": pvg.INPUT_SCHEMA, "intake": intake}
    before = copy.deepcopy(envelope)
    result = convert(seed("in", "Roof"), envelope)
    assert len(result["frames"]) == 11 and len(result["panels"]) == 2345
    assert [(f["provenance"]["source_handle"], len(f["panel_refs"]), f["module_rows"],
             f["module_columns"], f["module_slots"]) for f in result["frames"]] == ROOF_ROWS
    assert sum(f["module_rows"] for f in result["frames"]) == 199
    assert sum(f["module_slots"] for f in result["frames"]) == 9759
    assert sum(f["module_slots"] - sum(f["provenance"]["pvcase"]["source_row_lengths"])
               for f in result["frames"]) == 0
    assert result["strings"] == result["inverters"] == []
    assert result["extra"]["pvcase"]["normalized_intake_sha256"] == ROOF_HASH
    assert walk(result) == (347941, 3878729, 3314989, 6)
    assert sdg.validate_graph(result) == result
    assert all("captured_matrix" not in f and "tracker" not in f and "ground_slots" not in f
               for f in result["frames"])
    assert envelope == before


def test_pvg_authored_ground():
    value = seed()
    result = convert(value)
    assert len(result["frames"]) == 1 and len(result["panels"]) == 2
    frame = result["frames"][0]
    assert (frame["module_rows"], frame["module_columns"], frame["module_slots"]) == (3, 3, 9)
    assert frame["provenance"]["pvcase"]["source_row_lengths"] == [3, 1, 0]
    assert frame["module_slots"] - sum(frame["provenance"]["pvcase"]["source_row_lengths"]) == 5
    assert frame["id"] == "leaf:frame:ebdcc318-84a4-4741-955a-1c3750bfb580"
    assert result["panels"][0]["id"] == "leaf:panel:e2bbcf8b-7c09-4a33-bab0-3a2c1fe23096"
    assert [p["centre"] for p in result["panels"]] == [[1.0, 2.0], [1.0, 4.0]]
    assert [p["matrix_cell"] for p in result["panels"]] == [{"row": 0, "col": 0}, {"row": 1, "col": 0}]
    assert frame["matrix"][0][0]["panel_ref"] == result["panels"][0]["id"]
    assert frame["matrix"][1][0]["panel_ref"] == result["panels"][1]["id"]
    assert sum(c["code"] == "empty" for row in frame["matrix"] for c in row) == 7
    assert all(c == {"code": "empty", "panel_ref": None, "seq": None,
                     "inverter_id": None, "string_input_number": None,
                     "x": 0.0, "y": 0.0, "angle": 0.0}
               for row in frame["matrix"] for c in row if c["code"] == "empty")
    assert frame["name"] == "PVcase A" and frame["captured_matrix"] == "pvcase-g33"
    assert "tracker" not in frame and "ground_slots" not in frame
    assert result["extra"]["pvcase"]["normalized_intake_sha256"] == GROUND_HASH
    assert result["extra"]["pvcase"]["panels_per_string"] == -4
    assert walk(result) == (603, 7456, 6465, 6)
    assert result["project"] == value["project"] and result["settings"] == value["settings"]
    assert result["rev"] == 0 and result["parent_rev"] is None
    assert all(p["rev"] == p["provenance"]["source_rev"] == 0 for p in result["panels"] + [frame])
    assert sdg.validate_graph(result) == result


def test_pvg_all_units_and_degrees():
    table = [
        ("mm", [0.01, -0.02], 0.077, 0.0385),
        ("cm", [0.1, -0.2], 0.77, 0.385),
        ("m", [10.0, -20.0], 77.0, 38.5),
        ("in", [0.254, -0.508], 1.9558, 0.9779),
        ("ft", [3.048, -6.096], 23.4696, 11.7348),
    ]
    for unit, centre, width, height in table:
        envelope = ground_envelope()
        envelope["intake"]["units"] = unit
        group = envelope["intake"]["panel_groups"][0]
        group.update(installation="Roof", row_angle_rad=0.5, rows=[[cell(x=10, y=-20)]])
        group["panel_size"] = {"height_across_row": 38.5, "width_along_row": 77}
        result = convert(seed(unit, "Roof"), envelope)
        frame, panel = result["frames"][0], result["panels"][0]
        assert panel["centre"] == frame["insertion_point"] == centre
        assert frame["module_width_along_row"] == width
        assert frame["module_height_across_row"] == height
        assert panel["angle"] == frame["matrix"][0][0]["angle"] == 28.64788975654116
        assert all(type(x) is float for x in panel["centre"])
        assert type(frame["module_width_along_row"]) is float
        u, v = math.cos(0.5), math.sin(0.5)
        corners = [(centre[0] + a * width / 2 * u - b * height / 2 * v,
                    centre[1] + a * width / 2 * v + b * height / 2 * u)
                   for a, b in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
        assert math.dist(corners[0], corners[1]) == pytest.approx(width)
        assert math.dist(corners[1], corners[2]) == pytest.approx(height)
        assert "corners" not in panel and "elevation" not in panel


def test_pvg_handle_normalization():
    first = convert()
    envelope = ground_envelope()
    group = envelope["intake"]["panel_groups"][0]
    group["handle"] = "A"
    for row in group["rows"]:
        for c in row:
            if c is not None:
                c["id"] = c["id"].upper().lstrip("0") or "0"
    second = convert(envelope=envelope)
    assert first["extra"]["pvcase"]["normalized_intake_sha256"] == second["extra"]["pvcase"]["normalized_intake_sha256"] == GROUND_HASH
    for collection in ("frames", "panels"):
        assert [e["id"] for e in first[collection]] == [e["id"] for e in second[collection]]
    assert first["frames"][0]["provenance"]["source_handle"] == "00a"
    assert second["frames"][0]["provenance"]["source_handle"] == "A"
    assert first["panels"][0]["provenance"]["source_handle"] == "001"
    assert second["panels"][0]["provenance"]["source_handle"] == "1"


def test_pvg_identity_namespaces():
    first = convert()
    def ids(value):
        return [e["id"] for e in value["frames"] + value["panels"]]
    value = seed()
    value["source_hash"] = "c" * 64
    assert ids(convert(value)) != ids(first)
    value = seed()
    value.update(rev=1, parent_rev=0)
    revised = convert(value)
    assert ids(revised) == ids(first)
    assert all(e["rev"] == e["provenance"]["source_rev"] == 1 for e in revised["frames"] + revised["panels"])
    envelope = ground_envelope()
    envelope["intake"]["panel_groups"][0]["rows"][0][0]["x"] = 2
    assert ids(convert(envelope=envelope)) != ids(first)
    rebound = convert(source_artifact_id="c" * 64, source_sha256="d" * 64)
    assert ids(rebound) == ids(first)
    assert rebound["extra"]["pvcase"]["source_artifact_id"] == "c" * 64
    assert rebound["extra"]["pvcase"]["source_sha256"] == "d" * 64
    assert ids(convert(envelope=json.dumps(ground_envelope(), indent=2).encode())) == ids(first)


def test_pvg_unknown_fields_and_isolation():
    value, envelope = seed(), ground_envelope()
    value["future"] = {"keep": [1, None, False]}
    value["project"]["extra"]["future"] = {"keep": ["x"]}
    value["opaque_stores"] = {"external": {"payload_ref": "opaque:fixture", "sha256": "e" * 64, "byte_length": 7}}
    value["orphaned_xdata"] = [{"payload_ref": "opaque:orphan", "sha256": "f" * 64, "byte_length": 9}]
    value["extra"]["other"] = {"list": [1]}
    before_value, before_envelope = copy.deepcopy(value), copy.deepcopy(envelope)
    result = convert(value, envelope)
    for key in ("project", "settings", "future", "opaque_stores", "orphaned_xdata"):
        assert result[key] == value[key]
    assert result["extra"]["other"] == value["extra"]["other"]
    result["future"]["keep"].append("changed")
    result["project"]["extra"]["future"]["keep"].append("changed")
    result["extra"]["other"]["list"].append(2)
    result["frames"][0]["provenance"]["pvcase"]["source_sequences"].append(3)
    result["frames"][0]["matrix"][2][0]["x"] = 9
    assert result["frames"][0]["matrix"][2][1]["x"] == 0.0
    assert value == before_value and envelope == before_envelope


def test_pvg_source_assignments_are_evidence():
    result = convert()
    frame = result["frames"][0]
    expected = [
        {"group_index": 0, "row": 0, "col": 0, "code": 1, "seq": 9, "inverter_id": 7, "string_input_number": 3},
        {"group_index": 0, "row": 1, "col": 0, "code": -1, "seq": -2, "inverter_id": -1, "string_input_number": 0},
    ]
    assert [p["provenance"]["pvcase"] for p in result["panels"]] == expected
    assert frame["provenance"]["pvcase"]["source_sequences"] == [2, 0, -1]
    assert frame["sequences"] == [] and frame["electrical_zone_ref"] is None
    assert frame["module_power_watts"] == 0 and type(frame["module_power_watts"]) is int
    for p in result["panels"]:
        assert p["assignment"] == {"string_ref": None, "seq": None}
    for c in [c for row in frame["matrix"] for c in row] + frame["panel_assignments"]:
        assert all(c[k] is None for k in ("seq", "inverter_id", "string_input_number"))
    for c in frame["panel_assignments"]:
        assert c["string_ref"] is None
    assert result["strings"] == result["inverters"] == result["electrical_zones"] == []
    assert result["settings"]["global_string_sizing_confirmed"] is False


def test_pvg_skips_unusable_groups():
    envelope = ground_envelope()
    skipped = copy.deepcopy(envelope["intake"]["panel_groups"][0])
    skipped.update(handle="B", rows=[[], [None]])
    envelope["intake"]["panel_groups"].insert(0, skipped)
    result = convert(envelope=envelope)
    assert result["extra"]["pvcase"]["skipped_groups"] == [
        {"group_index": 0, "source_handle": "B", "reason": "no-usable-panels"}]
    assert result["frames"][0]["provenance"]["pvcase"]["group_index"] == 1
    assert all(p["provenance"]["pvcase"]["group_index"] == 1 for p in result["panels"])


def test_pvg_no_panel_groups():
    envelope = ground_envelope()
    envelope["intake"]["panel_groups"] = []
    refused("PVG_NO_PANEL_GROUPS", lambda: convert(envelope=envelope))


def test_pvg_no_usable_panels():
    for rows in ([], [[], [None]], [[cell(code=0)]]):
        envelope = ground_envelope()
        envelope["intake"]["panel_groups"][0]["rows"] = rows
        refused("PVG_NO_USABLE_PANELS", lambda: convert(envelope=envelope))


def test_pvg_invalid_target():
    for mutate in (lambda g: g.update(graph_schema_version=2), lambda g: g.pop("project"),
                   lambda g: g["project"].pop("units"),
                   lambda g: g["project"].update(units=None),
                   lambda g: g["settings"].update(optimizer_ratio=-1)):
        value, envelope = seed(), ground_envelope()
        mutate(value)
        before_value, before_envelope = copy.deepcopy(value), copy.deepcopy(envelope)
        refused("PVG_INVALID_TARGET", lambda: convert(value, envelope))
        assert value == before_value and envelope == before_envelope


def test_pvg_nonempty_target(graph):
    for collection in sdg.COLLECTIONS:
        value = seed("in", "Roof")
        item = copy.deepcopy(graph[collection][0])
        if collection == "frames":
            item.update(panel_refs=[], module_rows=0, module_columns=0, module_slots=0,
                        electrical_zone_ref=None, matrix=[], sequences=[], panel_assignments=[])
        elif collection == "panels":
            item.update(frame_ref=None, matrix_cell=None, assignment={"string_ref": None, "seq": None})
        elif collection == "electrical_zones":
            item["panel_refs"] = []
        elif collection == "strings":
            item.update(ordered_panel_refs=[], module_count=0, from_ref=None, to_ref=None, inverter_ref=None)
        elif collection == "inverters":
            item["input_assignments"] = []
        elif collection == "schedules":
            item["source_refs"] = []
        value[collection] = [item]
        assert sdg.validate_graph(value) == value
        envelope = ground_envelope()
        envelope["intake"]["units"] = "in"
        envelope["intake"]["panel_groups"][0]["installation"] = "Roof"
        before = copy.deepcopy(value)
        refused("PVG_TARGET_NOT_EMPTY", lambda: convert(value, envelope))
        assert value == before
    for existing in (None, {}, {"old": "binding"}):
        value = seed()
        value["extra"]["pvcase"] = existing
        refused("PVG_TARGET_NOT_EMPTY", lambda: convert(value))


def test_pvg_target_context():
    mutations = [
        lambda g: g["project"].update(installation_design="Roof"),
        lambda g: g["project"]["units"].update(drawing_units="ft"),
        lambda g: g["project"]["units"].update(meters_per_unit=0.0254),
        lambda g: g["project"]["units"].update(compute_units="ft"),
        lambda g: g["project"]["units"]["wcs_to_ucs"].__setitem__(12, 1),
        lambda g: g["project"]["units"].update(crs="EPSG:4326"),
    ]
    for mutate in mutations:
        value, envelope = seed(), ground_envelope()
        mutate(value)
        before = copy.deepcopy(value)
        refused("PVG_TARGET_CONTEXT", lambda: convert(value, envelope))
        assert value == before
    envelope = ground_envelope()
    other = copy.deepcopy(envelope["intake"]["panel_groups"][0])
    other.update(handle="B", installation="Roof", rows=[[]])
    envelope["intake"]["panel_groups"].append(other)
    refused("PVG_TARGET_CONTEXT", lambda: convert(envelope=envelope))


def test_pvg_units_defects_are_invalid_targets():
    # Only a known non-metric compute unit on an otherwise valid target is a context
    # conflict; every structural units defect, and a units conflict beside another
    # defect, is an invalid target. The caller's graph is never written.
    def ft_and_bad_settings(g):
        g["project"]["units"]["compute_units"] = "ft"
        g["settings"]["optimizer_ratio"] = -1
    invalid = [
        lambda g: g["project"]["units"].pop("wcs_to_ucs"),
        lambda g: g["project"].update(units={}),
        lambda g: g["project"]["units"].update(wcs_to_ucs="bad"),
        lambda g: g["project"]["units"].pop("source"),
        lambda g: g["project"]["units"].pop("compute_units"),
        lambda g: g["project"]["units"].update(compute_units=5),
        lambda g: g["project"]["units"].update(compute_units="furlong"),
        ft_and_bad_settings,
    ]
    for mutate in invalid:
        value, envelope = seed(), ground_envelope()
        mutate(value)
        before = copy.deepcopy(value)
        refused("PVG_INVALID_TARGET", lambda: convert(value, envelope))
        assert value == before
    for unit in ("mm", "ft", "km", "yd"):
        value = seed()
        value["project"]["units"]["compute_units"] = unit
        before = copy.deepcopy(value)
        refused("PVG_TARGET_CONTEXT", lambda: convert(value))
        assert value == before


@pytest.mark.parametrize("case", ["frames-panels", "copies", "future-panels", "future-extra"])
def test_pvg_aliased_target(case, monkeypatch):
    # A target may share one container between keys. The conversion appends to
    # independent output collections, preflight's accounting equals the result, and
    # every preserved value keeps the content it was given.
    baseline = convert()
    value = seed()
    if case == "frames-panels":
        value["frames"] = value["panels"]
    elif case == "copies":
        value["extra"]["copies"] = [value["panels"]] * 5000
    elif case == "future-panels":
        value["future"] = value["panels"]
    else:
        value["future"] = value["extra"]
    before = copy.deepcopy(value)
    measures = capture_preflight(monkeypatch)
    result = convert(value)
    nodes, size, canonical_bytes, depth = walk(result)
    assert (measures[-1].nodes, measures[-1].size, measures[-1].canonical_bytes,
            measures[-1].depth) == (nodes, size, canonical_bytes, depth)
    assert result["panels"] == baseline["panels"] and result["frames"] == baseline["frames"]
    if case == "copies":
        assert len(result["extra"]["copies"]) == 5000
        assert all(item == [] for item in result["extra"]["copies"])
    elif case == "future-panels":
        assert result["future"] == []
    elif case == "future-extra":
        assert result["future"] == before["extra"] and "pvcase" not in result["future"]
    assert value == before


def test_pvg_source_reference():
    for field in ("source_artifact_id", "source_sha256"):
        for bad in (None, 1, b"b" * 64, "B" * 64, "g" * 64, "b" * 63, "b" * 65, "b" * 64 + "\n"):
            value, envelope = seed(), ground_envelope()
            before_value, before_envelope = copy.deepcopy(value), copy.deepcopy(envelope)
            refused("PVG_INVALID_SOURCE", lambda: convert(value, envelope, **{field: bad}))
            assert value == before_value and envelope == before_envelope


def test_pvg_geometry_range(monkeypatch):
    trap_materializer(monkeypatch)
    variants = [
        ("m", "angle", 1e308), ("m", "rectangle", 1.7e308),
        ("mm", "width", 5e-324), ("mm", "height", 5e-324),
    ]
    for unit, kind, number in variants:
        envelope = ground_envelope()
        envelope["intake"]["units"] = unit
        group = envelope["intake"]["panel_groups"][0]
        if kind == "angle":
            group["row_angle_rad"] = number
        elif kind == "rectangle":
            group["panel_size"]["width_along_row"] = number
            group["rows"][0][0]["x"] = number
        else:
            group["panel_size"]["width_along_row" if kind == "width" else "height_across_row"] = number
        assert solver.validate_intake(envelope["intake"])
        refused("PVG_GEOMETRY_RANGE", lambda: convert(seed(unit), envelope))


def test_pvg_ragged_preallocation(monkeypatch):
    envelope = ground_envelope()
    rows = [[cell()] + [None] * 999] + [[] for _ in range(999)]
    envelope["intake"]["panel_groups"][0]["rows"] = rows
    admitted = solver.validate_intake(envelope["intake"])
    source_rows = admitted["panel_groups"][0]["rows"]
    assert len(source_rows) == 1000 and max(map(len, source_rows)) == 1000
    assert sum(map(len, source_rows)) == 1000
    assert len(source_rows) + sum(map(len, source_rows)) == 2000
    assert 1000 * 1000 == 1000000 and 1000000 - 1000 == 999000
    assert 17 * 1000000 == 17000000
    trap_materializer(monkeypatch)
    refused("PVG_NODE_LIMIT", lambda: convert(envelope=envelope))


def test_pvg_matrix_limit(monkeypatch):
    trap_materializer(monkeypatch)
    for rows in ([[cell()]] + [[] for _ in range(10000)],
                 [[cell()] + [None] * 10000],
                 [[cell()] + [None] * 999] + [[] for _ in range(1000)]):
        envelope = ground_envelope()
        envelope["intake"]["panel_groups"][0]["rows"] = rows
        assert solver.validate_intake(envelope["intake"])
        refused("PVG_MATRIX_LIMIT", lambda: convert(envelope=envelope))


def test_pvg_node_preflight(monkeypatch):
    measurements = capture_preflight(monkeypatch)
    result = convert()
    assert measurements[0].nodes == walk(result)[0] == 603
    trap_materializer(monkeypatch)
    monkeypatch.setattr(pvg, "MAX_NODES", 602)
    refused("PVG_NODE_LIMIT", lambda: convert())


def test_pvg_byte_preflight(monkeypatch):
    measurements = capture_preflight(monkeypatch)
    result = convert()
    assert measurements[0].size == walk(result)[1] == 7456
    assert measurements[0].canonical_bytes == walk(result)[2] == 6465
    with monkeypatch.context() as patch:
        trap_materializer(patch)
        patch.setattr(pvg, "MAX_BYTES", 7455)
        refused("PVG_BYTE_LIMIT", lambda: convert())
    value = seed()
    value["extra"]["escaped"] = "\x00" * 2000
    result = convert(value)
    measurement = measurements[-1]
    assert measurement.canonical_bytes == len(canonical(result))
    assert measurement.canonical_bytes > measurement.size
    limit = measurement.canonical_bytes - 1
    assert max(walk(value)[1:3]) < limit
    with monkeypatch.context() as patch:
        trap_materializer(patch)
        patch.setattr(pvg, "MAX_BYTES", limit)
        refused("PVG_BYTE_LIMIT", lambda: convert(value))


def test_pvg_depth_preflight(monkeypatch):
    measurements = capture_preflight(monkeypatch)
    result = convert()
    assert measurements[0].depth == walk(result)[3] == 6
    value = seed()
    value["extra"]["deep"] = nested(30)
    assert walk(value)[3] == 32
    assert walk(convert(value))[3] == 32
    with monkeypatch.context() as patch:
        trap_materializer(patch)
        value["extra"]["deep"] = nested(31)
        refused("PVG_DEPTH_LIMIT", lambda: convert(value))
    # Lower only the prospective depth ceiling after input and target admission.
    original = pvg._preflight
    def low_depth(*args):
        monkeypatch.setattr(pvg, "MAX_DEPTH", 5)
        return original(*args)
    monkeypatch.setattr(pvg, "_preflight", low_depth)
    trap_materializer(monkeypatch)
    refused("PVG_DEPTH_LIMIT", lambda: convert())


def test_pvg_list_preflight(monkeypatch):
    envelope = ground_envelope()
    envelope["intake"]["panel_groups"][0]["sequences"] = [0] * 100001
    assert solver.validate_intake(envelope["intake"])
    trap_materializer(monkeypatch)
    refused("PVG_LIST_LIMIT", lambda: convert(envelope=envelope))
    original = pvg._preflight
    def low_list(*args):
        monkeypatch.setattr(pvg, "MAX_LIST", 1)
        return original(*args)
    monkeypatch.setattr(pvg, "_preflight", low_list)
    refused("PVG_LIST_LIMIT", lambda: convert())


def test_pvg_ground_marker_contract():
    result = convert()
    assert sdg.validate_graph(result) == result
    for mutate in (
        lambda f: f.update(captured_matrix="other"),
        lambda f: f.update(installation_design="Roof"),
        lambda f: f.update(tracker=None),
        lambda f: f.update(ground_slots=None),
        lambda f: f["provenance"].pop("pvcase"),
        lambda f: f["provenance"]["pvcase"].pop("source_sha256"),
    ):
        value = copy.deepcopy(result)
        mutate(value["frames"][0])
        refused("INVALID_GRAPH_SCHEMA", lambda: sdg.validate_graph(value))
    # Provenance is historical; admitted later geometry edits do not rewrite it.
    edited = copy.deepcopy(result)
    edited["frames"][0]["module_width_along_row"] = 3.0
    assert sdg.validate_graph(edited) == edited


def test_pvg_unmarked_ground_still_refuses():
    value = convert()
    del value["frames"][0]["captured_matrix"]
    refused("INVALID_GRAPH_SCHEMA", lambda: sdg.validate_graph(value))


def test_pvg_w1_bytes_unchanged(graph):
    assert sdg.validate_graph(graph) == graph
    assert hashlib.sha256(canonical(graph)).hexdigest() == "67ab25efcda14a69da65a4184cf5d362ac19a85dcf266f722a9528f5881737c3"
    payload = sdg.serialize_graph(graph).encode("utf-8")
    assert len(payload) == 11244
    assert hashlib.sha256(payload).hexdigest() == "1fd29adc97543a32e739c14bcad1257284b6fcf15eb08d9d3a1407df0b9c84fc"


def test_pvg_schema_matches_envelope():
    schema = json.loads((ROOT / "contract/leaf-pvcase-g33.v1.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    assert validator.is_valid(ground_envelope())
    for unit in ("mm", "cm", "m", "in", "ft"):
        value = ground_envelope()
        value["intake"]["units"] = unit
        value["intake"]["panels_per_string"] = -2147483648
        assert validator.is_valid(value)
    for mutate in (
        lambda e: e.update(extra=1), lambda e: e.pop("schema"),
        lambda e: e.update(schema="leaf.pvcase-g33.v2"),
        lambda e: e["intake"].update(version=1),
        lambda e: e["intake"].update(panels_per_string=True),
        lambda e: e["intake"].update(panels_per_string=2147483648),
        lambda e: e["intake"].update(units="yd"),
        lambda e: e["intake"]["panel_groups"][0].update(rows=[None]),
        lambda e: e["intake"]["panel_groups"][0].update(installation="Tracker"),
        lambda e: e["intake"]["panel_groups"][0].update(handle="A\n"),
        lambda e: e["intake"]["panel_groups"][0].update(extra=1),
        lambda e: e["intake"]["panel_groups"][0]["panel_size"].update(width_along_row=0),
        lambda e: e["intake"]["panel_groups"][0]["rows"][0][0].update(code=2147483648),
        lambda e: e["intake"]["panel_groups"][0]["rows"][0][0].update(extra=1),
    ):
        value = ground_envelope()
        mutate(value)
        assert not validator.is_valid(value)
    # Cross-field numeric identity remains the existing Python validator's job.
    value = ground_envelope()
    value["intake"]["panel_groups"][0]["rows"][0][2]["id"] = "1"
    assert validator.is_valid(value)
    refused("PVG_INVALID_INTAKE", lambda: pvg.validate_envelope(value))


def test_pvg_result_refusal_is_atomic(monkeypatch):
    value, envelope = seed(), ground_envelope()
    before_value, before_envelope = copy.deepcopy(value), copy.deepcopy(envelope)
    original = pvg._materialize
    calls = []
    def malformed(*args):
        calls.append(True)
        result = original(*args)
        result["panels"][0]["frame_ref"] = None
        return result
    monkeypatch.setattr(pvg, "_materialize", malformed)
    refused("PVG_INVALID_RESULT", lambda: convert(value, envelope))
    assert calls == [True]
    assert value == before_value and envelope == before_envelope
