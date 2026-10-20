"""DXF1: document structure and legacy identities, independent of byte pins."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from dxf_intake import parse_dxf_bytes
from intake_dxf import intake_to_dxf


def _small(with_blocks=False):
    intake = {"layers": ["0", "dEfPoInTs"], "polylines": [
        {"handle": "A", "layer": "0", "pts": [[1, 2, 0], [4, 2, 0]], "closed": False},
        {"handle": "L1", "layer": "dEfPoInTs", "pts": [[2, 3, 0], [5, 3, 0]], "closed": False}]}
    if with_blocks:
        intake["blocks"] = {"Fixture": {"base": [1, 2, 0], "count": 1, "complete": True,
            "children": [{"kind": "LINE", "layer": "0", "pts": [[0, 0, 0], [1, 1, 0]]}]}}
        intake["groups"] = [{"name": "RACK", "flags": 0, "selectable": 1, "members": ["A", "L1"]}]
    return intake


def _sections(raw):
    lines = raw.decode().splitlines()
    assert len(lines) % 2 == 0
    pairs = list(zip(lines[::2], lines[1::2]))
    assert pairs[-1] == ("0", "EOF")
    sections = {}
    i = 0
    while i < len(pairs) - 1:
        assert pairs[i] == ("0", "SECTION")
        assert pairs[i + 1][0] == "2"
        name = pairs[i + 1][1]
        assert name not in sections
        end = pairs.index(("0", "ENDSEC"), i + 2)
        sections[name] = pairs[i + 2:end]
        i = end + 1
    return sections


def _records(pairs):
    records = []
    for code, value in pairs:
        if code == "0":
            records.append([])
        records[-1].append((code, value))
    return records


def _get(record, code):
    return next(v for c, v in record if c == str(code))


@pytest.mark.parametrize("case", ["sample", "small", "blocks-groups"])
def test_r2000_document_envelope_and_legacy_handles(case):
    intake = (json.loads((ROOT / "web/public/sample.intake.json").read_text())
              if case == "sample" else _small(case == "blocks-groups"))
    raw = intake_to_dxf(intake)
    sections = _sections(raw)
    assert list(sections) == ["HEADER", "TABLES", "BLOCKS", "ENTITIES", "OBJECTS"]
    assert sections["HEADER"][:3] == [("9", "$ACADVER"), ("1", "AC1015"), ("9", "$HANDSEED")]
    assert len(sections["HEADER"]) == 4
    seed = int(_get(sections["HEADER"], 5), 16)
    records = {name: _records(pairs) for name, pairs in sections.items() if name != "HEADER"}
    handles = [v.upper() for rows in records.values() for row in rows for c, v in row if c in ("5", "105")]
    assert len(handles) == len(set(handles))
    assert seed > max(int(h, 16) for h in handles)
    tables = {}
    for record in records["TABLES"]:
        kind = _get(record, 0)
        if kind == "TABLE":
            name = _get(record, 2)
            assert name not in tables
            tables[name] = (record, [])
            assert _get(record, 5)
            assert _get(record, 330) == "0"
            assert ("100", "AcDbSymbolTable") in record
        elif kind == "ENDTAB":
            assert int(_get(tables[name][0], 70)) == len(tables[name][1])
        else:
            assert kind == name
            assert _get(record, 330) == _get(tables[name][0], 5)
            assert _get(record, 105 if name == "DIMSTYLE" else 5)
            assert sum(c == "100" for c, _ in record) == 2
            assert ("100", "AcDbSymbolTableRecord") in record
            tables[name][1].append(record)
    assert list(tables) == ["VPORT", "LTYPE", "LAYER", "STYLE", "VIEW", "UCS", "APPID", "DIMSTYLE", "BLOCK_RECORD"]
    for _, rows in tables.values():
        names = [_get(r, 2).casefold() for r in rows]
        assert len(names) == len(set(names))
    assert [_get(r, 2) for r in tables["LTYPE"][1]] == ["ByBlock", "ByLayer", "Continuous"]
    assert _get(tables["VPORT"][1][0], 2) == "*Active"
    assert _get(tables["STYLE"][1][0], 3) == "txt"
    assert _get(tables["DIMSTYLE"][1][0], 143) == "0.03937007874"
    assert ("100", "AcDbDimStyleTable") in tables["DIMSTYLE"][0]
    assert _get(tables["APPID"][1][0], 2) == "ACAD"
    assert tables["VIEW"][1] == tables["UCS"][1] == []
    block_records = {_get(r, 2): _get(r, 5) for r in tables["BLOCK_RECORD"][1]}
    assert set(block_records) == {"*Model_Space", "*Paper_Space"} | ({"Fixture"} if case == "blocks-groups" else set())
    blocks = records["BLOCKS"]
    for index, name in enumerate(("*Model_Space", "*Paper_Space")):
        begin, end = blocks[index * 2:index * 2 + 2]
        assert _get(begin, 0) == "BLOCK" and _get(end, 0) == "ENDBLK"
        assert _get(begin, 2) == _get(begin, 3) == name
        assert _get(begin, 330) == _get(end, 330) == block_records[name]
        assert [(c, v) for c, v in begin if c in ("10", "20", "30")] == [("10", "0.0"), ("20", "0.0"), ("30", "0.0")]
        assert (("67", "1") in begin) == (("67", "1") in end) == (index == 1)
    for record in records["ENTITIES"]:
        assert record[1:3] == [("5", _get(record, 5)), ("330", block_records["*Model_Space"])]
    expected = ([p["handle"].upper() for p in intake["polylines"]] if case == "sample" else ["A", "100"])
    assert [_get(r, 5) for r in records["ENTITIES"]] == expected
    objects = records["OBJECTS"]
    root = next(r for r in objects if r[0] == ("0", "DICTIONARY") and _get(r, 330) == "0")
    entries = {root[i][1]: root[i + 1][1] for i in range(len(root) - 1) if root[i][0] == "3"}
    assert set(entries) == {"ACAD_GROUP", "ACAD_PLOTSTYLENAME"}
    normal, = [r for r in objects if r[0] == ("0", "ACDBPLACEHOLDER")]
    plot, = [r for r in objects if r[0] == ("0", "ACDBDICTIONARYWDFLT")]
    assert plot == [("0", "ACDBDICTIONARYWDFLT"), ("5", entries["ACAD_PLOTSTYLENAME"]),
                    ("330", _get(root, 5)), ("100", "AcDbDictionary"), ("281", "1"),
                    ("3", "Normal"), ("350", _get(normal, 5)),
                    ("100", "AcDbDictionaryWithDefault"), ("340", _get(normal, 5))]
    assert normal == [("0", "ACDBPLACEHOLDER"), ("5", _get(normal, 5)), ("330", _get(plot, 5))]
    group_dict = next(r for r in objects if ("5", entries["ACAD_GROUP"]) in r)
    assert _get(group_dict, 330) == _get(root, 5)
    assert [_get(r, 2) for r in tables["LAYER"][1]] == intake["layers"]
    for layer in tables["LAYER"][1]:
        assert layer[-2:] == [("370", "-3"), ("390", _get(normal, 5))]
        if _get(layer, 2).casefold() == "defpoints":
            assert layer[-3] == ("290", "0")
    back = parse_dxf_bytes(raw)
    assert back["layers"] == intake["layers"]
    assert "dimstyles" not in back
    if case == "blocks-groups":
        user_block = next(r for r in blocks if r[0] == ("0", "BLOCK") and ("2", "Fixture") in r)
        assert _get(user_block, 5) == "109"
        child = next(r for r in blocks if r[0] == ("0", "LINE"))
        assert _get(child, 5) == "10A"
        assert back["blocks"] == intake["blocks"]
        assert back["groups"] == [{"handle": "10E", "owner": "10D", "name": "RACK",
                                   "flags": 0, "selectable": 1, "members": ["A", "100"]}]
    else:
        assert "blocks" not in back and "groups" not in back


def test_paper_space_entity_owner():
    intake = _small()
    intake["polylines"][0].update(space="paper", layout="Layout1")
    sections = _sections(intake_to_dxf(intake))
    paper = next(r for r in _records(sections["TABLES"])
                 if r[0] == ("0", "BLOCK_RECORD") and ("2", "*Paper_Space") in r)
    entity = _records(sections["ENTITIES"])[0]
    assert entity[1:3] == [("5", "A"), ("330", _get(paper, 5))]
    assert ("67", "1") in entity and ("410", "Layout1") in entity


def test_existing_symbol_and_user_block_handles_survive_table_merge():
    intake = _small(True)
    intake["dimstyles"] = ["Standard", "Roof"]
    intake["mlstyles"] = [{"name": "Standard", "textstyle": "Notes", "segments": 1,
                           "height": 0.18, "arrow": 0.18, "dogleg": 0.36, "gap": 0.09}]
    sections = _sections(intake_to_dxf(intake))
    records = _records(sections["TABLES"])
    for kind, expected in (
            ("LTYPE", {"ByLayer": "101"}),
            ("STYLE", {"Standard": "102", "Notes": "103"}),
            ("DIMSTYLE", {"Standard": "105", "Roof": "106"}),
            ("BLOCK_RECORD", {"*Model_Space": "108", "*Paper_Space": "109", "Fixture": "10A"})):
        actual = {_get(r, 2): _get(r, 105 if kind == "DIMSTYLE" else 5)
                  for r in records if r[0] == ("0", kind)}
        assert {name: actual[name] for name in expected} == expected
    notes = next(r for r in records if r[0] == ("0", "STYLE") and ("2", "Notes") in r)
    assert _get(notes, 3) == "arial.ttf"
    style, = [r for r in _records(sections["OBJECTS"]) if r[0] == ("0", "MLEADERSTYLE")]
    assert _get(style, 5) == "104"
    assert _get(style, 342) == "103" and _get(style, 340) == "101"
    child = next(r for r in _records(sections["BLOCKS"]) if r[0] == ("0", "LINE"))
    assert _get(child, 5) == "110"


def test_sample_ezdxf_recovery_has_no_audit_errors(tmp_path):
    pytest.importorskip("ezdxf")
    from ezdxf import recover

    intake = json.loads((ROOT / "web/public/sample.intake.json").read_text())
    path = tmp_path / "sample.dxf"
    path.write_bytes(intake_to_dxf(intake))
    document, auditor = recover.readfile(path)
    assert document.dxfversion == "AC1015"
    assert auditor.errors == []
    assert len(document.modelspace()) == 2345
