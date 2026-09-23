"""Offline checks for the standard-library XLSX reader and writer.

Four layers, all hermetic and none of them skipping:

  1. Cell values: the number spellings C# and EPPlus produce, the column
     reference algebra, and `normalize_cell`, which is where the round-trip
     contract is DEFINED rather than assumed.
  2. The round trip: `read(write(x))` equals `x` normalized, for every cell type
     the probe workbooks carry and for the awkward ones they do not (a sparse
     row, a gap between rows, an empty string, a string needing XML escaping).
  3. The licensed captures, committed at
     docs/parity/evidence/probes/demo-probes-20260923 and written by EPPlus, not
     by this module: the reader must resolve their shared-string table, keep
     their sheet order, and hand back the exact cell values the plugin wrote. A
     reader that only reads its own writer's output proves nothing.
  4. The bounds and the refusals. Every malformed workbook here is one this
     module must REFUSE rather than answer partially: not a zip, a missing part,
     bad XML, an out-of-range shared-string index, an error cell, rows out of
     order, a cell whose reference contradicts its row, a duplicate reference, a
     zip with too many entries, and a deflate bomb whose entry expands past the
     size it declared.

Styling is never asserted because rule E5 does not compare it: this module does
not read fonts, widths or number formats and does not write them.
"""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


xlsx = _load("solar_xlsx", ROOT / "server" / "solar_xlsx.py")

# The licensed capture is committed in this repo, so every runner carries it.
REFERENCE_DIR = ROOT / "docs/parity/evidence/probes/demo-probes-20260923"
LICENSED_WORKBOOKS = ("leafbomxlsx_demo.xlsx", "leafharness_bom_demo.xlsx",
                      "leafbomxlsxempty_demo_f1.xlsx", "leafbomxlsxempty_demo_f2.xlsx")
SEVEN_TABS = ("Overview", "By Area", "Modules", "Cable Tray", "Layout",
              "Electrical", "Piling")


def rows_of(sheets, name):
    """Every row of one named sheet, as {row index: cells}."""
    for sheet in sheets:
        if sheet.name == name:
            return {row.index: row.cells for row in sheet.rows}
    raise AssertionError("workbook has no sheet " + name)


# --------------------------------------------------------------------------- #
# Cell values
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("index,letters", [
    (1, "A"), (2, "B"), (26, "Z"), (27, "AA"), (28, "AB"), (52, "AZ"),
    (53, "BA"), (702, "ZZ"), (703, "AAA"), (16384, "XFD"),
])
def test_column_references_round_trip(index, letters):
    assert xlsx.column_letters(index) == letters
    assert xlsx.column_index(letters) == index


@pytest.mark.parametrize("bad", [0, -1, xlsx.MAX_COLUMN_INDEX + 1])
def test_column_index_out_of_range_is_refused(bad):
    with pytest.raises(xlsx.XlsxError):
        xlsx.column_letters(bad)


@pytest.mark.parametrize("letters", ["", "a", "A1", "ABCD", "A B"])
def test_malformed_column_letters_are_refused(letters):
    with pytest.raises(xlsx.XlsxError):
        xlsx.column_index(letters)


@pytest.mark.parametrize("value,text", [
    (18.0, "18"), (0.0, "0"), (1.2, "1.2"), (4.5, "4.5"), (60.0, "60"),
    (-0.0, "-0"), (0.001, "0.001"), (3, "3"), (-7, "-7"), (2 ** 40, "1099511627776"),
])
def test_numbers_render_the_way_the_plugin_writes_them(value, text):
    """A double with no fraction prints without one, exactly as C# does."""
    assert xlsx.format_number(value) == text


@pytest.mark.parametrize("text,value", [
    ("18", 18), ("-0", 0), ("0", 0), ("1.2", 1.2), ("4.5", 4.5),
    ("  7  ", 7), ("1e2", 100.0), ("-3.5", -3.5),
])
def test_numbers_parse_to_the_type_their_spelling_implies(text, value):
    parsed = xlsx.parse_number(text)
    assert parsed == value
    assert type(parsed) is type(value)


@pytest.mark.parametrize("text", ["", "abc", "NaN", "Infinity", "-Infinity",
                                  "0x10", "1e400", "1,000"])
def test_non_numeric_cell_text_is_refused(text):
    with pytest.raises(xlsx.XlsxError):
        xlsx.parse_number(text)


@pytest.mark.parametrize("value,normalized", [
    (18.0, 18), (-0.0, 0), (1.2, 1.2), (3, 3), (True, True), (False, False),
    ("x", "x"), ("", ""), (None, None),
])
def test_normalize_cell_is_the_round_trip_contract(value, normalized):
    result = xlsx.normalize_cell(value)
    assert result == normalized and type(result) is type(normalized)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"),
                                   b"bytes", [1], {"a": 1}])
def test_unsupported_cell_values_are_refused(value):
    with pytest.raises(xlsx.XlsxError):
        xlsx.normalize_cell(value)


# --------------------------------------------------------------------------- #
# The round trip
# --------------------------------------------------------------------------- #
ROUND_TRIP = [
    ("Overview", [(1, ["Metric", "Value", "Unit"]),
                  (2, ["Total rows (sections)", 3, "ea"]),
                  (3, ["Total tube length", 60.0, "m"]),
                  (4, ["DC nameplate capacity", 18.0, "kWp"])]),
    ("By Area", [(1, ["Category", "Description"]),
                 (2, ["Module", "PV Module — 2.000 m × 1.000 m portrait"])]),
    ("Cable Tray", [(1, ["EndOfRow", 10, 4.5, 3, 13.5])]),
    ("Odd shapes", [(1, [True, False, "", "a & b < c > d \" e"]),
                    (4, ["gap above", None, "after a hole"]),
                    (9, [1.2])]),
]


def test_write_then_read_returns_the_normalized_cells():
    sheets = xlsx.read_workbook(xlsx.write_workbook(ROUND_TRIP))
    assert [sheet.name for sheet in sheets] == [name for name, _ in ROUND_TRIP]
    for sheet, (_, rows) in zip(sheets, ROUND_TRIP):
        expected = [(index, [xlsx.normalize_cell(cell) for cell in cells])
                    for index, cells in rows]
        assert [(row.index, row.cells) for row in sheet.rows] == expected


def test_a_row_index_survives_a_gap():
    """Rule E5 keys a row by its SHEET row number, so 4 must not read back as 2."""
    sheets = xlsx.read_workbook(xlsx.write_workbook(ROUND_TRIP))
    assert sorted(rows_of(sheets, "Odd shapes")) == [1, 4, 9]


def test_a_trailing_empty_cell_is_dropped_and_an_interior_one_is_kept():
    """A gap must never shift its neighbours left, and a tail is not a column."""
    sheets = xlsx.read_workbook(xlsx.write_workbook(
        [("S", [(1, ["a", None, "c", None, None])])]))
    assert rows_of(sheets, "S")[1] == ["a", None, "c"]


def test_a_wholly_empty_row_is_not_a_row():
    sheets = xlsx.read_workbook(xlsx.write_workbook(
        [("S", [(1, ["a"]), (2, [None, None]), (3, ["c"])])]))
    assert sorted(rows_of(sheets, "S")) == [1, 3]


def test_the_writer_is_deterministic():
    """Same workbook, same bytes: a fixed zip timestamp, no incidental ordering."""
    assert xlsx.write_workbook(ROUND_TRIP) == xlsx.write_workbook(ROUND_TRIP)


def test_read_workbook_file_reads_what_write_workbook_wrote(tmp_path):
    path = tmp_path / "book.xlsx"
    path.write_bytes(xlsx.write_workbook(ROUND_TRIP))
    assert xlsx.read_workbook_file(path) == xlsx.read_workbook(path.read_bytes())


def test_read_workbook_file_bounds_the_read(tmp_path):
    path = tmp_path / "book.xlsx"
    path.write_bytes(xlsx.write_workbook(ROUND_TRIP))
    with pytest.raises(xlsx.XlsxError):
        xlsx.read_workbook_file(path, max_bytes=16)


@pytest.mark.parametrize("sheets", [
    [],
    [("A", [(1, ["x"])]), ("A", [(1, ["y"])])],
    [("", [(1, ["x"])])],
    [("A", [(0, ["x"])])],
    [("A", [(2, ["x"]), (1, ["y"])])],
    [("A", [(2, ["x"]), (2, ["y"])])],
    [("A", [(1.5, ["x"])])],
    [("A", [(True, ["x"])])],
    [("A", ["not a pair"])],
    ["not a pair"],
    [("A", [(1, [float("nan")])])],
    [("A", [(1, ["\x00"])])],
])
def test_malformed_workbooks_are_refused_by_the_writer(sheets):
    with pytest.raises(xlsx.XlsxError):
        xlsx.write_workbook(sheets)


def test_too_many_sheets_is_refused():
    with pytest.raises(xlsx.XlsxError):
        xlsx.write_workbook([("S%d" % i, [(1, ["x"])])
                             for i in range(xlsx.MAX_SHEETS + 1)])


# --------------------------------------------------------------------------- #
# The licensed captures, written by EPPlus and not by this module
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", LICENSED_WORKBOOKS)
def test_licensed_workbook_keeps_its_seven_tabs_in_order(name):
    sheets = xlsx.read_workbook_file(REFERENCE_DIR / name)
    assert tuple(sheet.name for sheet in sheets) == SEVEN_TABS


def test_licensed_shared_strings_and_numbers_read_as_the_plugin_wrote_them():
    """EPPlus stores every string in a shared table; the reader must resolve it."""
    sheets = xlsx.read_workbook_file(REFERENCE_DIR / "leafbomxlsx_demo.xlsx")
    overview = rows_of(sheets, "Overview")
    assert overview[1] == ["Metric", "Value", "Unit"]
    assert overview[2] == ["Total rows (sections)", 3, "ea"]
    assert overview[4] == ["Total tube length", 60, "m"]
    assert overview[7] == ["DC nameplate capacity", 18, "kWp"]
    by_area = rows_of(sheets, "By Area")
    assert by_area[2] == ["Module", "PV Module — 2.000 m × 1.000 m portrait",
                          "ea", 30]
    piling = rows_of(sheets, "Piling")
    # A fractional double keeps its fraction where an integral one loses it.
    assert piling[2] == [1, 0, 0, 0, 0, 1.2]
    assert len(piling) == 16


def test_licensed_cable_tray_holds_the_aggregated_harness_rows():
    sheets = xlsx.read_workbook_file(REFERENCE_DIR / "leafharness_bom_demo.xlsx")
    tray = rows_of(sheets, "Cable Tray")
    assert tray[1] == ["HarnessType", "CableGaugeAwg", "DropLengthM",
                       "Count", "TotalLengthM"]
    assert tray[2] == ["EndOfRow", 10, 0, 3, 0]
    assert tray[12] == ["Motor", 6, 4.5, 1, 4.5]
    assert len(tray) == 12


def test_licensed_empty_fixtures_differ_only_where_the_null_branch_does():
    """F1 passes null lists and F2 empty ones; Cable Tray is the only difference."""
    f1 = xlsx.read_workbook_file(REFERENCE_DIR / "leafbomxlsxempty_demo_f1.xlsx")
    f2 = xlsx.read_workbook_file(REFERENCE_DIR / "leafbomxlsxempty_demo_f2.xlsx")
    differing = [a.name for a, b in zip(f1, f2) if a != b]
    assert differing == ["Cable Tray"]
    assert rows_of(f1, "Cable Tray")[1] == [
        "Cable / harness detail will be populated when Q27 (Harness Manager) "
        "ships. Placeholder tab for parity."]
    assert rows_of(f2, "Cable Tray")[1][0] == "HarnessType"
    assert len(rows_of(f2, "Cable Tray")) == 1


@pytest.mark.parametrize("name", LICENSED_WORKBOOKS)
def test_this_writer_reproduces_the_licensed_cells(name):
    """Read a licensed workbook, write it back, read it again: same cells.

    The BYTES differ (EPPlus writes styles and a shared-string table and this
    module writes neither), and rule E5 compares values, not storage.
    """
    original = xlsx.read_workbook_file(REFERENCE_DIR / name)
    assert xlsx.read_workbook(xlsx.write_workbook(original)) == original


# --------------------------------------------------------------------------- #
# Bounds and refusals on the read path
# --------------------------------------------------------------------------- #
def rewritten(name, changes):
    """The licensed workbook with named parts replaced, as bytes."""
    source = zipfile.ZipFile(io.BytesIO((REFERENCE_DIR / name).read_bytes()))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as out:
        for info in source.infolist():
            if info.filename in changes and changes[info.filename] is None:
                continue
            out.writestr(info.filename, changes.get(info.filename, source.read(info.filename)))
    return buffer.getvalue()


SHEET = ('<?xml version="1.0"?><worksheet xmlns="%s"><sheetData>%%s</sheetData></worksheet>'
         % xlsx.MAIN_NS)


@pytest.mark.parametrize("body", [
    '<row r="2"><c r="A2"><v>1</v></c></row><row r="1"><c r="A1"><v>2</v></c></row>',
    '<row r="1"><c r="A2"><v>1</v></c></row>',
    '<row r="1"><c r="A1"><v>1</v></c><c r="A1"><v>2</v></c></row>',
    '<row r="1"><c r="A1" t="e"><v>#DIV/0!</v></c></row>',
    '<row r="1"><c r="A1" t="s"><v>9999</v></c></row>',
    '<row r="1"><c r="A1" t="s"><v>-1</v></c></row>',
    '<row r="1"><c r="A1" t="zz"><v>1</v></c></row>',
    '<row r="0"><c r="A0"><v>1</v></c></row>',
    '<row r="1"><c r="A1"><v>not a number</v></c></row>',
])
def test_malformed_sheets_are_refused(body):
    data = rewritten("leafbomxlsx_demo.xlsx",
                     {"xl/worksheets/sheet1.xml": (SHEET % body).encode("utf-8")})
    with pytest.raises(xlsx.XlsxError):
        xlsx.read_workbook(data)


@pytest.mark.parametrize("changes", [
    {"xl/workbook.xml": None},
    {"xl/_rels/workbook.xml.rels": None},
    {"xl/worksheets/sheet1.xml": None},
    {"xl/workbook.xml": b"<not xml"},
    {"xl/workbook.xml": ('<?xml version="1.0"?><workbook xmlns="%s"/>'
                         % xlsx.MAIN_NS).encode("utf-8")},
    {"xl/_rels/workbook.xml.rels": ('<?xml version="1.0"?><Relationships xmlns="%s"/>'
                                    % xlsx.PKG_REL_NS).encode("utf-8")},
])
def test_malformed_packages_are_refused(changes):
    with pytest.raises(xlsx.XlsxError):
        xlsx.read_workbook(rewritten("leafbomxlsx_demo.xlsx", changes))


@pytest.mark.parametrize("data", [b"", b"not a zip at all", "a string", 17, None])
def test_a_non_archive_is_refused(data):
    with pytest.raises(xlsx.XlsxError):
        xlsx.read_workbook(data)


def test_an_oversized_archive_is_refused():
    with pytest.raises(xlsx.XlsxError):
        xlsx.read_workbook(b"x" * (xlsx.MAX_ARCHIVE_BYTES + 1))


def test_too_many_entries_is_refused():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as out:
        for index in range(xlsx.MAX_ENTRIES + 1):
            out.writestr("part%d.xml" % index, b"<x/>")
    with pytest.raises(xlsx.XlsxError):
        xlsx.read_workbook(buffer.getvalue())


def test_an_oversized_entry_is_refused_before_it_is_read():
    """A highly compressible entry: 8 MB of zeros is a few hundred bytes on disk,
    so the refusal must come from the DECLARED size, before any decompression."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as out:
        out.writestr("xl/workbook.xml", b"\0" * (xlsx.MAX_ENTRY_BYTES + 1))
    payload = buffer.getvalue()
    assert len(payload) < 100_000, "the bomb must be small on disk to be a bomb"
    with pytest.raises(xlsx.XlsxError):
        xlsx.read_workbook(payload)


def test_a_relationship_escaping_the_package_is_refused():
    rels = ('<?xml version="1.0"?><Relationships xmlns="%s">'
            '<Relationship Id="rId1" Type="t" Target="../../etc/passwd"/>'
            '</Relationships>' % xlsx.PKG_REL_NS).encode("utf-8")
    with pytest.raises(xlsx.XlsxError):
        xlsx.read_workbook(rewritten("leafbomxlsx_demo.xlsx",
                                     {"xl/_rels/workbook.xml.rels": rels}))
