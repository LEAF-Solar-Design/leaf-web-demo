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


@pytest.mark.parametrize("unit,units_per_metre", [(4, 1000), (5, 100), (6, 1)])
def test_reexport_preserves_insertion_units_and_native_coordinates(unit, units_per_metre):
    points = [[0, 0], [units_per_metre, 0], [units_per_metre, units_per_metre / 2]]
    source = intake_to_dxf({"polylines": [
        {"handle": "A", "layer": "0", "pts": points, "closed": True},
    ]}).replace(b"1\nAC1015\n", f"1\nAC1015\n9\n$INSUNITS\n70\n{unit}\n".encode(), 1)
    parsed = parse_dxf_bytes(source)
    assert parsed["insunits"] == unit
    raw = intake_to_dxf(parsed)
    assert _sections(raw)["HEADER"][-2:] == [("9", "$INSUNITS"), ("70", str(unit))]
    reopened = parse_dxf_bytes(raw)
    assert reopened["insunits"] == unit
    assert reopened["polylines"] == parsed["polylines"]


def test_reexport_distinguishes_unitless_and_absent_insertion_units():
    assert "insunits" not in parse_dxf_bytes(intake_to_dxf({}))
    assert parse_dxf_bytes(intake_to_dxf({"insunits": 0}))["insunits"] == 0


def test_reexport_rejects_conflicting_insertion_units():
    source = b"0\nSECTION\n2\nHEADER\n9\n$INSUNITS\n70\n4\n9\n$INSUNITS\n70\n6\n0\nENDSEC\n0\nEOF\n"
    parsed = parse_dxf_bytes(source)
    assert "insunits_error" in parsed
    with pytest.raises(intake_dxf.IntakeDxfError, match="insertion units"):
        intake_to_dxf(parsed)


@pytest.mark.parametrize("unit", [-1, 25, True, "4", 4.0, None])
def test_reexport_rejects_invalid_insertion_unit_code(unit):
    with pytest.raises(intake_dxf.IntakeDxfError, match="INSUNITS"):
        intake_to_dxf({"insunits": unit})


def _assert_pointers_resolve(raw):
    sections = _sections(raw)
    records = [record for name, pairs in sections.items() if name != "HEADER"
               for record in _records(pairs)]
    handles = {v.upper() for record in records for c, v in record if c in ("5", "105")}
    for pairs in sections.values():
        for code, value in pairs:
            if (330 <= int(code) <= 369 or 390 <= int(code) <= 399) and value != "0":
                assert value.upper() in handles, (code, value)


def _round_trip_with_resolved_pointers(intake):
    raw = intake_to_dxf(intake)
    _assert_pointers_resolve(raw)
    return parse_dxf_bytes(raw, source_name="x.dxf")


@pytest.mark.parametrize("parent,middle,override", [
    ("Roof", "Branch", "Override"),
    ("Circuit-7", "Circuit-8", "Circuit-9"),
    ("South Roof", "East Rack", "Manual Override"),
])
def test_reexport_preserves_nested_insert_paths_and_arrays(parent, middle, override):
    def insert(handle, name, layer, columns, invisible=False):
        return [(0, "INSERT"), (5, handle), (2, name), (8, layer),
                (10, 5), (20, 10), (30, 2), (41, -2), (42, 3), (50, 90),
                (210, 0), (220, 1), (230, 0), (70, columns), (71, 2), (44, 4), (45, 7),
                (60, int(invisible))]

    def block(name, *records):
        return [(0, "BLOCK"), (2, name), *[pair for record in records for pair in record],
                (0, "ENDBLK")]

    pairs = [(0, "SECTION"), (2, "BLOCKS"),
             *block("Module", [(0, "POINT"), (8, "0"), (10, 0), (20, 0)]),
             *block("Tray", insert("A1", "Module", "0", 2),
                    insert("A2", "Module", override, 3, invisible=True)),
             *block("Rack", insert("B1", "Tray", "0", 1),
                    insert("B2", "Tray", middle, 1)),
             (0, "ENDSEC"), (0, "SECTION"), (2, "ENTITIES"),
             *insert("C1", "Rack", parent, 2), (0, "ENDSEC"), (0, "EOF")]
    parsed = parse_dxf_bytes("".join(f"{code}\n{value}\n" for code, value in pairs).encode())
    assert not parsed.get("parse_errors")
    raw = intake_to_dxf(parsed)
    reopened = parse_dxf_bytes(raw)
    assert reopened["inserts"] == parsed["inserts"]
    assert reopened["blocks"]["Tray"]["nested_inserts"][1]["invisible"] is True
    for name in ("Rack", "Tray"):
        assert reopened["blocks"][name]["nested_inserts"] == parsed["blocks"][name]["nested_inserts"]
    sections = _sections(raw)
    owners = {_get(row, 2): _get(row, 5) for row in _records(sections["TABLES"])
              if row[0] == ("0", "BLOCK_RECORD")}
    nested = {_get(row, 5): _get(row, 330) for row in _records(sections["BLOCKS"])
              if row[0] == ("0", "INSERT")}
    assert nested == {"A1": owners["Tray"], "A2": owners["Tray"],
                      "B1": owners["Rack"], "B2": owners["Rack"]}
    _assert_pointers_resolve(raw)


@pytest.mark.parametrize("issue,match", [
    ("incomplete", "nested INSERT data is incomplete"),
    ("unresolved", "unresolved block reference"),
    ("duplicate", "duplicate handle A"),
    ("bad-count", "columns must be an integer"),
])
def test_reexport_rejects_unusable_nested_insert_data(issue, match):
    intake = _small(with_blocks=True)
    fixture = intake["blocks"]["Fixture"]
    reference = {"name": "Leaf", "handle": "B", "layer": "0", "x": 0, "y": 0, "z": 0}
    intake["blocks"]["Leaf"] = {"base": [0, 0, 0], "children": []}
    fixture["nested_inserts"] = [reference]
    if issue == "incomplete":
        fixture["nested_inserts_incomplete"] = True
    elif issue == "unresolved":
        reference["name"] = "Missing"
    elif issue == "duplicate":
        reference["handle"] = "a"
    elif issue == "bad-count":
        reference["columns"] = 32768
    with pytest.raises(intake_dxf.IntakeDxfError, match=match):
        intake_to_dxf(intake)


def test_exhausted_handle_space_is_rejected():
    intake = {"layers": ["0"], "polylines": [
        {"layer": "0", "closed": True, "pts": [[0, 0, 0], [1, 0, 0], [1, 1, 0]],
         "xdata": None, "handle": "F" * 32}]}
    with pytest.raises(intake_dxf.IntakeDxfError, match="handle space"):
        intake_to_dxf(intake)


def test_large_handle_with_available_space_is_preserved():
    handle = "F" * 31
    intake = {"layers": ["0"], "polylines": [
        {"layer": "0", "closed": True, "pts": [[0, 0, 0], [1, 0, 0], [1, 1, 0]],
         "xdata": None, "handle": handle}]}
    raw = intake_to_dxf(intake)
    sections = _sections(raw)
    polyline, = _records(sections["ENTITIES"])
    assert _get(polyline, 5) == handle
    assert parse_dxf_bytes(raw)["polylines"][0]["handle"] == handle
    for pairs in sections.values():
        for code, value in pairs:
            if code in ("5", "105"):
                assert 1 <= len(value) <= 32
                assert int(value, 16) < 16**32
    assert 1 <= len(_get(sections["HEADER"], 5)) <= 32


def test_case_distinct_layers_keep_legacy_order():
    intake = {"layers": ["Roof", "roof"], "polylines": [
        {"layer": "Roof", "closed": True, "pts": [[0, 0], [1, 0], [1, 1]]}]}
    back = _round_trip_with_resolved_pointers(intake)
    assert back["layers"] == ["Roof", "roof"]
    assert back["polylines"][0]["layer"] == "Roof"


def test_default_dimstyle_merges_in_legacy_position():
    back = _round_trip_with_resolved_pointers({"dimstyles": ["Foo", "Standard"]})
    assert back["dimstyles"] == ["Foo", "Standard"]


def test_case_distinct_dimstyles_survive():
    back = _round_trip_with_resolved_pointers({"dimstyles": ["standard"]})
    assert back["dimstyles"] == ["Standard", "standard"]


def test_case_distinct_textstyles_keep_mleader_style_pointers():
    intake = {"mlstyles": [
        {"name": name, "textstyle": textstyle, "segments": 1,
         "height": 1, "arrow": 1, "dogleg": 1, "gap": 1}
        for name, textstyle in (("M", "Standard"), ("N", "standard"))]}
    back = _round_trip_with_resolved_pointers(intake)
    assert [(s["name"], s["textstyle"]) for s in back["mlstyles"]] == [
        ("M", "Standard"), ("N", "standard")]


@pytest.mark.parametrize(("blocks", "children", "message"), [
    (201, 1, "block database was capped"),
    (1, 61, "truncated block children"),
    (2, 62, "truncated block children"),
])
def test_reexport_refuses_truncated_block_data(blocks, children, message):
    intake = parse_dxf_bytes(_block_capture_dxf(blocks, children))
    with pytest.raises(intake_dxf.IntakeDxfError, match=message):
        intake_to_dxf(intake)


def _block_capture_dxf(blocks, children, *, nested_tail=False):
    pairs = [(0, "SECTION"), (2, "BLOCKS")]
    for index in range(blocks):
        pairs.extend([(0, "BLOCK"), (2, f"Panel{index}")])
        for child in range(children if index == 0 else 1):
            pairs.extend([(0, "POINT"), (8, "0"), (10, child), (20, 0)])
        if nested_tail and index == 0:
            pairs.extend([(0, "INSERT"), (5, "A1"), (2, "Panel1"), (10, 60), (20, 0)])
        pairs.append((0, "ENDBLK"))
    pairs.extend([(0, "ENDSEC"), (0, "SECTION"), (2, "ENTITIES"),
                  (0, "INSERT"), (5, "A2"), (2, "Panel0"), (10, 0), (20, 0),
                  (0, "ENDSEC"), (0, "EOF")])
    return "".join(f"{code}\n{value}\n" for code, value in pairs).encode()


@pytest.mark.parametrize("nested_tail", [False, True])
def test_reexport_retains_complete_block_capture_at_limits(nested_tail):
    intake = parse_dxf_bytes(_block_capture_dxf(200, 60, nested_tail=nested_tail))
    reopened = parse_dxf_bytes(intake_to_dxf(intake))
    assert len(reopened["blocks"]) == 200
    assert "blocksCapped" not in reopened
    assert reopened["blocks"]["Panel0"] == intake["blocks"]["Panel0"]


def _nested_outline_dxf(vertex_groups, outline_groups=()):
    pairs = [(0, "SECTION"), (2, "BLOCKS"), (0, "BLOCK"), (2, "Module"),
             (0, "LWPOLYLINE"), (90, 4), (70, 1), *outline_groups, *vertex_groups,
             (0, "ENDBLK"), (0, "BLOCK"), (2, "Panel"), (0, "INSERT"), (5, "A1"),
             (2, "Module"), (10, 1), (20, 2), (0, "ENDBLK"), (0, "ENDSEC"),
             (0, "SECTION"), (2, "ENTITIES"), (0, "INSERT"), (5, "A2"), (2, "Panel"),
             (10, 4), (20, 5), (0, "ENDSEC"), (0, "EOF")]
    return "".join(f"{code}\n{value}\n" for code, value in pairs).encode()


@pytest.mark.parametrize(("bulges", "thickness"), [
    ([0.5, 0, 0, 0], 0),
    ([0, -0.25, 0, 0], 0),
    ([0, 0, 0, 0], 1.25),
    ([0, 0, -0.625, 0.125], -0.03125),
    ([0, 0, 0, 0], 0),
])
def test_nested_block_polyline_preserves_edge_geometry(bulges, thickness):
    vertices = []
    for (x, y), bulge in zip([(0, 0), (3, 0), (3, 2), (0, 2)], bulges, strict=True):
        vertices.extend([(10, x), (20, y)])
        if bulge:
            vertices.append((42, bulge))
    drawing = parse_dxf_bytes(_nested_outline_dxf(vertices, [(39, thickness)]))
    exported = intake_to_dxf(drawing)
    reopened = parse_dxf_bytes(exported)
    assert reopened["blocks"]["Panel"]["nested_inserts"] == drawing["blocks"]["Panel"]["nested_inserts"]
    for intake in (drawing, reopened):
        outline, = intake["blocks"]["Module"]["children"]
        assert outline.get("bulges", [0] * 4) == bulges
        assert outline.get("thickness", 0) == thickness
        assert ("bulges" in outline) == any(bulges)
        assert ("thickness" in outline) == bool(thickness)
    outline, = (record for record in _records(_sections(exported)["BLOCKS"])
                if record[0] == ("0", "LWPOLYLINE"))
    assert [float(value) for code, value in outline if code == "42"] == [b for b in bulges if b]
    assert [float(value) for code, value in outline if code == "39"] == ([thickness] if thickness else [])


@pytest.mark.parametrize("invalid_groups", [
    [(39, "nan")], [(42, "nan")], [(42, 1), (42, -1)],
])
def test_nested_block_polyline_refuses_malformed_geometry(invalid_groups):
    vertices = [(10, 0), (20, 0), *invalid_groups, (10, 3), (20, 2)]
    drawing = parse_dxf_bytes(_nested_outline_dxf(vertices))
    with pytest.raises(intake_dxf.IntakeDxfError, match="truncated block children"):
        intake_to_dxf(drawing)


@pytest.mark.parametrize("extra", [
    [(0, "POINT"), (10, 1), (20, 2)] * 60,
    [(0, "LWPOLYLINE"), (10, 0), (20, 0), (42, "nan"), (10, 1), (20, 1)],
    [(0, "LWPOLYLINE"), (10, "bad"), (20, 0), (42, "nan")],
])
def test_legacy_block_geometry_drop_does_not_hide_another_omission(extra):
    pairs = [(0, "SECTION"), (2, "BLOCKS"), (0, "BLOCK"), (2, "Module"),
             (0, "LINE"), (10, 0), (20, 0), *extra,
             (0, "ENDBLK"), (0, "ENDSEC"), (0, "EOF")]
    drawing = parse_dxf_bytes("".join(f"{code}\n{value}\n" for code, value in pairs).encode())
    assert drawing["blocks"]["Module"]["dropped_legacy_geometry"] == 1
    with pytest.raises(intake_dxf.IntakeDxfError, match="truncated block children"):
        intake_to_dxf(drawing)


@pytest.mark.parametrize("name,reference,font,bigfont,width,oblique,label_kind", [
    ("ModuleNotes", "ModuleNotes", "txt.shx", "", 1.0, 0, "TEXT"),
    ("PanelLabels", "panellabels", "simplex.shx", "bigfont.shx", 0.85, 12, "MTEXT"),
    ("String Labels", "String Labels", "arial.ttf", "", 1.25, -15, "TEXT"),
])
def test_reexport_retains_only_used_text_styles(name, reference, font, bigfont, width, oblique, label_kind):
    style = [(70, 0), (40, 2), (41, width), (50, oblique), (71, 0), (42, 2), (3, font), (4, bigfont)]
    pairs = [(0, "SECTION"), (2, "TABLES"), (0, "TABLE"), (2, "STYLE"), (70, 2),
             (0, "STYLE"), (2, name), *style, (0, "STYLE"), (2, "UnusedLabels"), (3, "unused.shx"),
             (0, "ENDTAB"), (0, "ENDSEC"),
             (0, "SECTION"), (2, "BLOCKS"), (0, "BLOCK"), (2, "Module"),
             (0, "TEXT"), (5, "A0"), (1, "Module label"), (7, reference),
             (10, 0), (20, 0), (40, 2), (0, "ENDBLK"),
             (0, "BLOCK"), (2, "Rack"), (0, "INSERT"), (5, "A1"), (2, "Module"),
             (10, 1), (20, 2), (0, "ENDBLK"), (0, "ENDSEC"),
             (0, "SECTION"), (2, "ENTITIES"), (0, "INSERT"), (5, "A2"), (2, "Rack"),
             (10, 4), (20, 5), (0, label_kind), (5, "A3"), (1, "Drawing label"),
             (7, reference), (10, 2), (20, 3), (40, 2), (0, "ENDSEC"), (0, "EOF")]
    source = parse_dxf_bytes("".join(f"{code}\n{value}\n" for code, value in pairs).encode())
    raw = intake_to_dxf(source)
    reopened = parse_dxf_bytes(raw)
    for drawing in (source, reopened):
        assert drawing["inserts"][0]["handle"] == "A2"
        assert drawing["blocks"]["Rack"]["nested_inserts"][0]["handle"] == "A1"
        assert drawing["blocks"]["Module"]["children"][0]["style"] == reference
        assert drawing["texts"][0]["style"] == reference
        assert set(drawing["text_styles"]) == {name.casefold()}
    assert reopened["text_styles"] == source["text_styles"]
    exported = [record for record in _records(_sections(raw)["TABLES"])
                if record[0] == ("0", "STYLE")]
    assert {_get(record, 2) for record in exported} == {"Standard", name}
    custom, = [record for record in exported if _get(record, 2) == name]
    assert custom[6:] == [(str(code), str(value)) for code, value in style]
    _assert_pointers_resolve(raw)


@pytest.mark.parametrize("catalogue,error", [
    ({}, "missing STYLE definition"),
    ({"label": {"name": "Other"}}, "STYLE name does not match"),
    ({"label": {"name": "Label", "width": "nan"}}, "invalid width"),
])
def test_reexport_refuses_unresolved_or_invalid_text_style(catalogue, error):
    drawing = _small(with_blocks=True)
    drawing["texts"] = [{"layer": "0", "pt": [0, 1], "text": "Label", "style": "Label"}]
    drawing["text_styles"] = catalogue
    with pytest.raises(intake_dxf.IntakeDxfError, match=error):
        intake_to_dxf(drawing)


def test_case_distinct_block_records_keep_definition_owners():
    intake = {"blocks": {name: {"base": [0, 0, 0], "children": []} for name in ("B", "b")}}
    back = _round_trip_with_resolved_pointers(intake)
    assert list(back["blocks"]) == ["B", "b"]
    assert back["blocks"] == {
        name: {"base": [0, 0, 0], "count": 0, "complete": True, "children": []}
        for name in ("B", "b")}


@pytest.mark.parametrize("asset", ["sample", "rooftop"])
def test_asset_pointers_resolve(asset):
    raw = ((ROOT / "web/public/sample.dxf").read_bytes() if asset == "sample" else
           intake_to_dxf(json.loads((ROOT / "data/rooftop_unsplit.intake.json").read_bytes())))
    _assert_pointers_resolve(raw)


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
    back = parse_dxf_bytes(raw, include_measurement_evidence=False)
    assert back["layers"] == intake["layers"]
    assert "dimstyles" not in back
    if case == "blocks-groups":
        user_block = next(r for r in blocks if r[0] == ("0", "BLOCK") and ("2", "Fixture") in r)
        assert _get(user_block, 5) == "109"
        child = next(r for r in blocks if r[0] == ("0", "LINE"))
        assert _get(child, 5) == "10A"
        # The default measurement view adds a handle-bound child path.
        block = dict(parse_dxf_bytes(raw)["blocks"]["Fixture"])
        path, = block.pop("beam_paths")
        expected_child = intake["blocks"]["Fixture"]["children"][0]
        assert path["pts"] == expected_child["pts"]
        assert path["handle"] and path["layer"] == expected_child["layer"] and path["closed"] is False
        assert {"Fixture": block} == intake["blocks"]
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


@pytest.mark.parametrize("kind,symbol", [
    ("VPORT", "*Active"), ("LTYPE", "ByBlock"), ("LTYPE", "ByLayer"),
    ("LTYPE", "Continuous"), ("STYLE", "Standard"), ("APPID", "ACAD"),
    ("DIMSTYLE", "Standard"), ("BLOCK_RECORD", "*Model_Space"),
    ("BLOCK_RECORD", "*Paper_Space"),
])
def test_default_merge_prefers_exact_name_without_moving_case_variants(kind, symbol):
    handle_code = "105" if kind == "DIMSTYLE" else "5"
    legacy = [(symbol.swapcase(), "A"), (symbol, "B"), (symbol, "C")]
    pairs = [("0", "SECTION"), ("2", "TABLES"), ("0", "TABLE"),
             ("2", kind), ("70", "3")]
    for name, handle in legacy:
        pairs.extend([("0", kind), (handle_code, handle), ("2", name),
                      ("100", "AcDbSymbolTableRecord"), ("100", "LegacyRecord"),
                      ("70", "7")])
    pairs.extend([("0", "ENDTAB"), ("0", "ENDSEC"), ("0", "SECTION"),
                  ("2", "ENTITIES"), ("0", "LINE"), ("5", "D"),
                  ("100", "AcDbEntity"), ("8", "0"), ("0", "ENDSEC"), ("0", "EOF")])
    handles = iter(format(n, "X") for n in range(0x100, 0x200))
    result = intake_dxf._r2000_envelope(
        [value for pair in pairs for value in pair], lambda: next(handles), lambda: "200", {}, False)
    raw = ("\n".join(result) + "\n").encode()
    _assert_pointers_resolve(raw)
    sections = _sections(raw)
    records = _records(sections["TABLES"])
    kept = [r for r in records if r[0] == ("0", kind) and _get(r, handle_code) in ("A", "B", "C")]
    assert [(_get(r, 2), _get(r, handle_code)) for r in kept] == legacy
    # Only the first exact match gets the default subclass and fields.
    assert ("100", "LegacyRecord") in kept[0]
    assert ("100", "LegacyRecord") not in kept[1]
    assert ("100", "LegacyRecord") in kept[2]
    if kind == "BLOCK_RECORD":
        blocks = _records(sections["BLOCKS"])
        begin = next(r for r in blocks if r[0] == ("0", "BLOCK") and ("2", symbol) in r)
        assert _get(begin, 330) == "B"
        if symbol == "*Model_Space":
            assert _get(_records(sections["ENTITIES"])[0], 330) == "B"


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
