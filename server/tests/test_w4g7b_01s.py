"""W4g-7b: block catalogue and INSERT intake/DXF round-trip case table."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "da"))

import dxf_intake
import intake_dxf
import intake_parse
import write_loop
from mutation_plan import validate_mutations


def _pt(values):
    return ",".join(str(v) for v in values)


def _inspection_text(intake):
    """The families-text an APS extraction would emit for `intake`: a small
    table-driven writer over LAYER/IN/BK/BKE records, kept in step with
    `_fixture` so the DXF and inspection fixtures never drift apart."""
    lines = [f"LAYER|{layer}" for layer in intake["layers"]]
    for ins in intake.get("inserts", []):
        lines.append("|".join([
            "IN", ins["name"], ins["layer"], _pt([ins["x"], ins["y"], ins["z"]]),
            str(ins["rot"]), _pt(ins["nrm"]), _pt(ins["scale"]), ins["handle"],
        ]))
    for name, block in intake.get("blocks", {}).items():
        lines.append("|".join([
            "BK", name, _pt(block["base"]), str(block["count"]),
            "1" if block["complete"] else "0",
        ]))
        for child in block["children"]:
            kind = child["kind"]
            if kind == "LINE":
                body = "|".join([_pt(child["pts"][0]), _pt(child["pts"][1])])
            elif kind == "CIRCLE":
                body = "|".join([_pt(child["c"]), str(child["r"]), _pt(child["nrm"])])
            elif kind == "ARC":
                body = "|".join([_pt(child["c"]), str(child["r"]),
                                  str(child["start_deg"]), str(child["end_deg"]),
                                  _pt(child["nrm"])])
            else:
                raise ValueError(f"the test writer does not cover block child kind {kind!r}")
            lines.append(f'BKE|{name}|{kind}|{body}|{child["layer"]}')
    return "\n".join(lines) + "\n"


def _fixture(sx=2.0):
    return {
        "dwg": "upload.dxf", "layers": ["0"], "polylines": [],
        "blocks": {
            "Fixture": {"base": [1.0, 2.0, 0.0], "count": 2, "complete": True,
                        "children": [
                            {"kind": "LINE", "layer": "0",
                             "pts": [[1.0, 2.0, 0.0], [4.0, 2.0, 0.0]]},
                            {"kind": "CIRCLE", "layer": "0", "c": [3.0, 4.0, 0.0], "r": 2.0,
                             "nrm": [0.0, 0.0, 1.0]},
                        ]},
        },
        "inserts": [{"name": "Fixture", "layer": "0", "x": 10.0, "y": 20.0, "z": 0.0,
                     "rot": 90.0, "nrm": [0.0, 0.0, 1.0], "scale": [sx, 3.0, 1.0],
                     "handle": "A1"}],
    }


def _dxf(*pairs):
    return ("\n".join(str(value) for pair in pairs for value in pair) + "\n").encode()


def _block_dxf(children, name="Fixture", flags=0):
    return _dxf(
        (0, "SECTION"), (2, "BLOCKS"),
        (0, "BLOCK"), (2, name), (10, 1), (20, 2), (30, 0), (70, flags),
        *children, (0, "ENDBLK"), (0, "ENDSEC"), (0, "EOF"),
    )


def _records(data):
    rows = []
    for code, value in dxf_intake._group_pairs(data.decode()):
        if code == 0:
            rows.append((value, {}))
        elif rows:
            rows[-1][1][code] = value
    return rows


@pytest.mark.parametrize("sx", [2.0, -2.0])
def test_case_table_round_trip_preserves_block_base_and_insert_transform(sx):
    intake = _fixture(sx)
    before = copy.deepcopy(intake)
    data = intake_dxf.intake_to_dxf(intake)
    assert dxf_intake.parse_dxf_bytes(data) == intake
    assert intake == before
    block = next(row for kind, row in _records(data) if kind == "BLOCK" and row[2] == "Fixture")
    assert block[10] == "1.0" and block[20] == "2.0" and block[30] == "0.0"
    assert block[70] == "0"


def test_hand_written_dxf_and_inspection_fixture_pair_match_field_for_field():
    fixture = _fixture()
    data = intake_dxf.intake_to_dxf(fixture)
    actual = dxf_intake.parse_dxf_bytes(data)
    inspected = intake_parse.parse_text(_inspection_text(fixture), "upload.dxf")
    assert actual == fixture
    assert actual["blocks"] == inspected["blocks"]
    assert actual["inserts"] == inspected["inserts"]
    assert len(actual["inserts"]) == 1
    assert actual["blocks"]["Fixture"]["count"] == 2


def test_block_record_owners_and_all_new_handles_are_distinct():
    intake = _fixture()
    intake["polylines"] = [
        {"layer": "0", "handle": "FF0", "closed": False, "xdata": None,
         "pts": [[0, 0, 0], [1, 1, 0]]},
        {"layer": "0", "handle": "L1", "closed": False, "xdata": None,
         "pts": [[2, 2, 0], [3, 3, 0]]},
    ]
    rows = _records(intake_dxf.intake_to_dxf(intake))
    handles = [row[5] for _, row in rows if 5 in row]
    assert len(handles) == len(set(handles))
    table = next(row for kind, row in rows if kind == "TABLE" and row[2] == "BLOCK_RECORD")
    records = {row[2]: row for kind, row in rows if kind == "BLOCK_RECORD"}
    assert set(records) == {"*Model_Space", "*Paper_Space", "Fixture"}
    entity_max = max(int(row[5], 16) for kind, row in rows if kind in ("INSERT", "LWPOLYLINE"))
    assert int(table[5], 16) > entity_max
    assert all(row[330] == table[5] and int(row[5], 16) > entity_max for row in records.values())
    for kind, row in rows:
        if kind == "BLOCK":
            assert row[330] == records[row[2]][5]
    assert sum(kind == "ENDBLK" for kind, _ in rows) == 3


def test_sixty_one_children_keep_the_total_and_only_the_first_sixty():
    child = [(0, "LINE"), (10, 1), (20, 2), (11, 4), (21, 2)]
    parsed = dxf_intake.parse_dxf_bytes(_block_dxf(child * 61))
    block = parsed["blocks"]["Fixture"]
    assert block["count"] == 61 and block["complete"] is False
    assert len(block["children"]) == 60
    inspected = intake_parse.parse_text(
        "BK|Fixture|1,2,0|61|0\n" + "BKE|Fixture|LINE|1,2,0|4,2,0|0\n" * 60, "x")
    assert inspected["blocks"] == parsed["blocks"]


@pytest.mark.parametrize("kind", ["INSERT", "ATTDEF", "HATCH"])
def test_unsupported_child_is_listed_and_marks_the_definition_incomplete(kind):
    parsed = dxf_intake.parse_dxf_bytes(_block_dxf([(0, kind), (2, "Nested")]))
    assert parsed["blocks"]["Fixture"] == {
        "base": [1.0, 2.0, 0.0], "count": 1, "complete": False,
        "children": [{"kind": "OTHER", "type": kind, "layer": ""}],
    }
    inspected = intake_parse.parse_text(
        f"BK|Fixture|1,2,0|1|0\nBKE|Fixture|OTHER|{kind}|", "x")
    assert inspected["blocks"] == parsed["blocks"]


@pytest.mark.parametrize("name,flags", [
    ("*Model_Space", 0), ("*Paper_Space", 0), ("*Paper_Space0", 0),
    ("*D1", 1), ("*U2", 1), ("Anonymous", 1),
])
def test_space_and_anonymous_definitions_are_skipped(name, flags):
    assert dxf_intake.parse_dxf_bytes(_block_dxf([], name, flags))["blocks"] == {}


def test_catalogue_is_capped_at_two_hundred_with_the_true_total():
    blocks = []
    for i in range(203):
        blocks.extend([(0, "BLOCK"), (2, f"B{i}"), (70, 0), (0, "ENDBLK")])
    parsed = dxf_intake.parse_dxf_bytes(_dxf(
        (0, "SECTION"), (2, "BLOCKS"), *blocks, (0, "ENDSEC"), (0, "EOF")))
    assert len(parsed["blocks"]) == 200
    assert list(parsed["blocks"])[-1] == "B199"
    assert parsed["blocksCapped"] == 203


def test_all_supported_block_child_geometries_use_the_inspection_precision():
    text = (
        "BK|Shapes|1.12345,2.23456,0|3|1\n"
        "BKE|Shapes|LWPOLYLINE|1|0,1,0|3.12345|1.12345,2.23456;4,5;|0\n"
        "BKE|Shapes|ARC|3.12345,4,0|2.12345|30.12345678|120.12345678|0,1,0|0\n"
        "BKE|Shapes|TEXT|1.12345,2,3|2.12345|45.12345678|hello world|0\n"
    )
    blocks = intake_parse.parse_text(text, "upload.dxf")["blocks"]
    intake = {"dwg": "upload.dxf", "layers": ["0"], "polylines": [], "blocks": blocks}
    assert dxf_intake.parse_dxf_bytes(intake_dxf.intake_to_dxf(intake)) == intake
    lw, arc, label = blocks["Shapes"]["children"]
    assert lw["pts"] == [[1.123, 2.235], [4.0, 5.0]]
    assert lw["elev"] == 3.123 and lw["nrm"] == [0.0, 1.0, 0.0]
    assert arc["r"] == 2.123 and arc["start_deg"] == 30.123457
    assert arc["nrm"] == [0.0, 1.0, 0.0]
    assert label["height"] == 2.123 and label["rot"] == 45.123457


def test_insert_defaults_and_tilted_position_match_the_in_parser():
    parsed = dxf_intake.parse_dxf_bytes(_dxf(
        (0, "SECTION"), (2, "ENTITIES"), (0, "INSERT"), (2, "Fixture"),
        (5, "a1"), (10, 1), (20, 2), (30, 3), (210, 0), (220, 1), (230, 0),
        (0, "ENDSEC"), (0, "EOF")))
    inspected = intake_parse.parse_text("IN|Fixture|0|1,2,3|0|0,1,0|1,1,1|a1", "upload.dxf")
    assert parsed["inserts"] == inspected["inserts"]
    assert dxf_intake.parse_dxf_bytes(intake_dxf.intake_to_dxf(parsed)) == parsed


def test_emitter_refuses_an_unresolved_insert_block_reference():
    intake = _fixture()
    intake["blocks"] = {}
    intake["inserts"][0]["name"] = "Missing"
    with pytest.raises(intake_dxf.IntakeDxfError, match="unresolved block reference Missing"):
        intake_dxf.intake_to_dxf(intake)


def test_verify_accepts_unchanged_insert_rotation_in_radians_and_rejects_a_real_rotation():
    base = _fixture()
    base["blocks"] = {}
    base["inserts"][0]["rot"] = 1.5708
    unchanged = copy.deepcopy(base)
    write_loop.verify_live_mutation_effects(base, unchanged, {})
    rotated = copy.deepcopy(base)
    rotated["inserts"][0]["rot"] = 90.0
    with pytest.raises(ValueError, match="INSERT"):
        write_loop.verify_live_mutation_effects(base, rotated, {})


def test_no_blocks_dxf_has_the_legacy_intake_bytes():
    data = _dxf(
        (0, "SECTION"), (2, "ENTITIES"), (0, "LINE"), (5, "A"), (8, "0"),
        (10, 1), (20, 2), (11, 4), (21, 2), (0, "ENDSEC"), (0, "EOF"))
    expected = (
        '{"dwg": "upload.dxf", "layers": ["0"], "polylines": [{"layer": "0", '
        '"closed": false, "pts": [[1.0, 2.0, 0.0], [4.0, 2.0, 0.0]], '
        '"xdata": null, "handle": "A"}]}'
    ).encode()
    assert json.dumps(dxf_intake.parse_dxf_bytes(data)).encode() == expected


def test_intake_without_blocks_or_inserts_has_the_legacy_dxf_bytes():
    intake = {"layers": ["0"], "polylines": [{
        "layer": "0", "closed": False, "pts": [[1, 2, 0], [4, 2, 0]], "handle": "A"}]}
    expected = _dxf(
        (0, "SECTION"), (2, "TABLES"), (0, "TABLE"), (2, "LAYER"), (70, 1),
        (0, "LAYER"), (100, "AcDbSymbolTableRecord"), (100, "AcDbLayerTableRecord"),
        (2, "0"), (70, 0), (62, 7), (6, "Continuous"), (0, "ENDTAB"), (0, "ENDSEC"),
        (0, "SECTION"), (2, "ENTITIES"), (0, "LWPOLYLINE"), (5, "A"),
        (100, "AcDbEntity"), (8, "0"), (100, "AcDbPolyline"), (90, 2), (70, 0),
        (38, "0.0"), (10, "1.0"), (20, "2.0"), (10, "4.0"), (20, "2.0"),
        (0, "ENDSEC"), (0, "EOF"))
    assert intake_dxf.intake_to_dxf(intake) == expected


def _v2_case():
    base = _fixture()
    base["polylines"] = [{
        "layer": "0", "closed": False, "pts": [[1, 2, 0], [4, 2, 0]],
        "handle": "B1", "xdata": None,
    }]
    plan = validate_mutations(base, {
        "set_points": [{"handle": "B1", "pts": [[2, 3, 0], [5, 3, 0]], "closed": False}],
    }, allow_transforms=False)
    actual = write_loop.apply_mutations(base, plan)
    return base, actual, plan


def test_v2_effects_verify_with_unchanged_blocks_and_inserts():
    base, actual, plan = _v2_case()
    write_loop.verify_live_mutation_effects(base, actual, plan)
    extra = copy.deepcopy(base["inserts"][0])
    extra["handle"] = "A2"
    base["inserts"].append(extra)
    actual["inserts"].insert(0, copy.deepcopy(extra))
    write_loop.verify_live_mutation_effects(base, actual, plan)


@pytest.mark.parametrize("change", ["position", "scale", "missing", "duplicate", "block"])
def test_v2_verifier_rejects_changes_to_the_unchanged_insert_or_block(change):
    base, actual, plan = _v2_case()
    if change == "position":
        actual["inserts"][0]["x"] += 1
    elif change == "scale":
        actual["inserts"][0]["scale"][0] = -2
    elif change == "missing":
        actual["inserts"] = []
    elif change == "duplicate":
        actual["inserts"].append(copy.deepcopy(actual["inserts"][0]))
    else:
        actual["blocks"]["Fixture"]["base"][0] += 1
    with pytest.raises(ValueError, match="INSERT|block"):
        write_loop.verify_live_mutation_effects(base, actual, plan)


@pytest.mark.parametrize("change", ["name", "coordinate", "duplicate_handle"])
def test_new_dxf_records_retain_the_synthesizers_validation_boundary(change):
    intake = _fixture()
    if change == "name":
        intake["inserts"][0]["name"] = "bad\n0\nLINE"
    elif change == "coordinate":
        intake["blocks"]["Fixture"]["base"][0] = float("nan")
    elif change == "duplicate_handle":
        intake["inserts"].append(copy.deepcopy(intake["inserts"][0]))
    with pytest.raises(intake_dxf.IntakeDxfError):
        intake_dxf.intake_to_dxf(intake)


def test_incomplete_catalogue_emits_only_the_supported_children_it_carries():
    intake = _fixture()
    block = intake["blocks"]["Fixture"]
    block["complete"] = False
    block["count"] = 3
    block["children"].append({"kind": "OTHER", "type": "HATCH", "layer": ""})
    parsed = dxf_intake.parse_dxf_bytes(intake_dxf.intake_to_dxf(intake))
    assert parsed == _fixture()
