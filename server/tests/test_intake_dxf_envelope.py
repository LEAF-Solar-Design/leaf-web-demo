"""DXF1: document structure and legacy identities, independent of byte pins."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from dxf_intake import parse_dxf_bytes
import intake_dxf
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


def _replace_objects(raw, objects):
    sections = _sections(raw)
    sections["OBJECTS"] = [pair for record in objects for pair in record]
    pairs = []
    for name, body in sections.items():
        pairs.extend([("0", "SECTION"), ("2", name), *body, ("0", "ENDSEC")])
    pairs.append(("0", "EOF"))
    return ("\n".join(value for pair in pairs for value in pair) + "\n").encode()


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
    assert set(entries) == {"ACAD_GROUP", "ACAD_PLOTSTYLENAME", "LEAF_INTAKE_ENVELOPE"}
    marker, = [r for r in objects if r[0] == ("0", "XRECORD")]
    assert marker == [("0", "XRECORD"), ("5", entries["LEAF_INTAKE_ENVELOPE"]),
                      ("330", _get(root, 5)), ("100", "AcDbXrecord"), ("280", "1"),
                      ("1", "dimstyle-standard"),
                      *([] if case == "blocks-groups" else [("1", "groups"), ("1", "blocks")])]
    assert int(_get(marker, 5), 16) < seed
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


@pytest.mark.parametrize("explicit", [(), ("dimstyles",), ("groups",), ("blocks",),
                                    ("dimstyles", "groups"), ("dimstyles", "blocks"),
                                    ("groups", "blocks"), ("dimstyles", "groups", "blocks")])
def test_provenance_preserves_explicit_catalogues_and_empty_fields(explicit):
    intake = _small()
    supplied = {"dimstyles": ["Standard"], "groups": [], "blocks": {}}
    intake.update({key: supplied[key] for key in explicit})
    raw = intake_to_dxf(intake)
    back = parse_dxf_bytes(raw)
    for key, value in supplied.items():
        if key in explicit:
            assert back[key] == value
        else:
            assert key not in back
    sections = _sections(raw)
    objects = _records(sections["OBJECTS"])
    root, = [r for r in objects if r[0] == ("0", "DICTIONARY") and _get(r, 330) == "0"]
    entry, = [i for i, pair in enumerate(root) if pair == ("3", "LEAF_INTAKE_ENVELOPE")]
    assert root[entry + 1][0] == "350"
    marker, = [r for r in objects if r[0] == ("0", "XRECORD")]
    assert _get(marker, 5) == root[entry + 1][1]
    assert _get(marker, 330) == _get(root, 5)
    assert int(_get(marker, 5), 16) < int(_get(sections["HEADER"], 5), 16)
    assert [value for code, value in marker if code == "1"] == [
        item for key, item in (("dimstyles", "dimstyle-standard"), ("groups", "groups"),
                              ("blocks", "blocks")) if key not in explicit]


def test_unmarked_reference_envelope_is_not_filtered():
    raw = intake_to_dxf(_small())
    objects = _records(_sections(raw)["OBJECTS"])
    root = next(r for r in objects if r[0] == ("0", "DICTIONARY") and _get(r, 330) == "0")
    entry = root.index(("3", "LEAF_INTAKE_ENVELOPE"))
    marker_handle = root[entry + 1][1]
    del root[entry:entry + 2]
    objects = [r for r in objects if _get(r, 5) != marker_handle]
    back = parse_dxf_bytes(_replace_objects(raw, objects))
    assert back["dimstyles"] == ["Standard"]
    assert back["groups"] == []
    assert back["blocks"] == {}


@pytest.mark.parametrize("items, removed", [
    (["unknown", "groups"], {"groups"}),
    (["unknown"] * 8 + ["dimstyle-standard", "groups", "blocks"], set()),
    (["unknown"] * 7 + ["groups", "blocks"], {"groups"}),
])
def test_provenance_ignores_unknown_items_and_reads_at_most_eight(items, removed):
    raw = intake_to_dxf(_small())
    objects = _records(_sections(raw)["OBJECTS"])
    marker, = [r for r in objects if r[0] == ("0", "XRECORD")]
    marker[:] = [(c, v) for c, v in marker if c != "1"] + [("1", item) for item in items]
    back = parse_dxf_bytes(_replace_objects(raw, objects))
    for key, value in (("dimstyles", ["Standard"]), ("groups", []), ("blocks", {})):
        if key in removed:
            assert key not in back
        else:
            assert back[key] == value


@pytest.mark.parametrize("invalid", ["type", "owner", "subclass"])
def test_invalid_provenance_does_not_filter(invalid):
    raw = intake_to_dxf(_small())
    objects = _records(_sections(raw)["OBJECTS"])
    marker, = [r for r in objects if r[0] == ("0", "XRECORD")]
    code, value = {"type": ("0", "ACDBPLACEHOLDER"), "owner": ("330", "0"),
                   "subclass": ("100", "Invalid")}[invalid]
    marker[:] = [(c, value if c == code else v) for c, v in marker]
    back = parse_dxf_bytes(_replace_objects(raw, objects))
    assert back["dimstyles"] == ["Standard"]
    assert back["groups"] == []
    assert back["blocks"] == {}


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


def test_mleader_standard_textstyle_and_bylayer_fields_survive():
    intake = _small()
    intake["mlstyles"] = [{"name": "Standard", "textstyle": "Standard", "segments": 1,
                           "height": 0.18, "arrow": 0.18, "dogleg": 0.36, "gap": 0.09}]
    intake["mleaders"] = [{"handle": "B", "layer": "0", "style": "Standard",
        "textstyle": "Standard", "pts": [[0, 0, 0], [5, 4, 0]],
        "landing": [5, 4, 0], "dogleg_dir": [1, 0, 0], "textpt": [5.45, 4.09, 0],
        "height": 0.18, "arrow": 0.18, "dogleg": 0.36, "attachment": 1, "text": "Valve"}]
    sections = _sections(intake_to_dxf(intake))
    records = _records(sections["TABLES"])
    style, = [r for r in records if r[0] == ("0", "STYLE") and ("2", "Standard") in r]
    assert style[6:] == [("70", "0"), ("40", "0.0"), ("41", "1.0"), ("50", "0.0"),
                         ("71", "0"), ("42", "0.2"), ("3", "arial.ttf"), ("4", "")]
    leader, = [r for r in _records(sections["ENTITIES"]) if r[0] == ("0", "MULTILEADER")]
    assert ("340", _get(style, 5)) in leader
    ltype, = [r for r in records if r[0] == ("0", "LTYPE") and ("2", "ByLayer") in r]
    assert ltype[6:] == [("70", "0"), ("3", ""), ("72", "65"), ("73", "0"), ("40", "0.0")]


def test_dimension_standard_keeps_pre_envelope_fields_and_gains_defaults(monkeypatch):
    intake = _small()
    intake["dimensions"] = [{"handle": "B", "type": "LINEAR", "layer": "0",
        "p1": [0, 0, 0], "p2": [3, 4, 0], "dimline": [1.5, 6, 0],
        "style": "Standard", "measurement": 3.0}]
    before = []
    envelope = intake_dxf._r2000_envelope

    def capture(lines, *args):
        before.extend(_records(_sections(("\n".join(lines) + "\n").encode())["TABLES"]))
        return envelope(lines, *args)

    monkeypatch.setattr(intake_dxf, "_r2000_envelope", capture)
    records = _records(_sections(intake_to_dxf(intake))["TABLES"])
    old, = [r for r in before if r[0] == ("0", "DIMSTYLE") and ("2", "Standard") in r]
    new, = [r for r in records if r[0] == ("0", "DIMSTYLE") and ("2", "Standard") in r]
    assert _get(new, 105) == _get(old, 105)
    assert new[3:6] == old[2:5]
    assert new[6:7] == old[5:] == [("70", "0")]
    assert new[7:9] == [("3", ""), ("4", "")]
    assert _get(new, 143) == "0.03937007874"
    assert len([c for c, _ in new if c == "70"]) == 1
    assert _get(new, 330) == _get(next(r for r in records
        if r[0] == ("0", "TABLE") and ("2", "DIMSTYLE") in r), 5)


@pytest.mark.parametrize("kind,symbol", [
    ("VPORT", "*Active"), ("LTYPE", "ByBlock"), ("LTYPE", "ByLayer"),
    ("LTYPE", "Continuous"), ("STYLE", "Standard"), ("APPID", "ACAD"),
    ("DIMSTYLE", "Standard"), ("BLOCK_RECORD", "*Model_Space"),
    ("BLOCK_RECORD", "*Paper_Space"),
])
def test_default_symbol_merge_keeps_custom_fields_order_and_repeats(kind, symbol):
    # Exercise records the subset writer does not currently emit, too.
    fields = [("70", "7"), ("3", "custom"), ("49", "1.25"), ("49", "-0.5")]
    lines = ["0", "SECTION", "2", "TABLES", "0", "TABLE", "2", kind,
             "70", "1", "0", kind, "105" if kind == "DIMSTYLE" else "5", "A",
             "2", symbol.swapcase(), *[value for pair in fields for value in pair],
             "0", "ENDTAB", "0", "ENDSEC", "0", "SECTION", "2", "ENTITIES",
             "0", "ENDSEC", "0", "EOF"]
    handles = iter(format(n, "X") for n in range(0x100, 0x200))
    result = intake_dxf._r2000_envelope(lines, lambda: next(handles), lambda: "200", {}, False)
    records = _records(_sections(("\n".join(result) + "\n").encode())["TABLES"])
    record, = [r for r in records if r[0] == ("0", kind) and ("2", symbol.swapcase()) in r]
    assert _get(record, 105 if kind == "DIMSTYLE" else 5) == "A"
    assert record[6:10] == fields
    assert not {c for c, _ in fields} & {c for c, _ in record[10:]}
    assert sum(c == "100" for c, _ in record) == 2


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
