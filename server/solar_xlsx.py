#!/usr/bin/env python3
"""A small, bounded XLSX reader and writer built on the standard library alone.

The repo declares no spreadsheet dependency and this slice does not add one, so
`zipfile` plus `xml.etree.ElementTree` are the whole toolkit. The scope is the
one thing joint identity contract v5 rule E5 compares: CELL VALUES, per sheet,
in sheet order. Styling, column widths, panes, formulas, merged ranges and
number formats are NOT read and NOT written, because rule E5 does not compare
them; a workbook this module writes is a minimal valid one, not a copy of the
plugin's EPPlus output.

The one shape both halves speak:

    workbook = [ Sheet(name, [ Row(index, [cell, ...]), ... ]), ... ]

`index` is the sheet's own 1-based row number, which rule E5 makes part of the
row's identity, so it is carried explicitly rather than implied by position: a
sheet whose first populated row is row 4 must not read back as row 1. Cells are
dense from column A to the last populated column of that row, with `None` for a
gap, so a missing cell can never shift its neighbours one column left. A row
with no populated cell at all is not returned (rule E5: non-empty rows).

Cell values are `str`, `int`, `float`, `bool` or `None`. A number arrives as an
`int` when it is written without a fraction and as a `float` otherwise, which is
what makes `read(write(x)) == [normalize_cell(c) for c in x]` hold: see
`normalize_cell`, the one place that equivalence is defined.

HARDENING. Every read is bounded before it allocates: the archive's byte length,
its entry count, each entry's DECLARED and ACTUAL uncompressed size (a deflate
bomb declares a small size and then expands, so both are checked), the total
uncompressed budget, the sheet count, the shared-string count and the cells per
sheet. Entry names are resolved inside the package and an absolute or `..` path
is refused rather than normalized. Anything malformed raises `XlsxError`; no
read path returns a partial workbook.

No network, no dependencies outside the standard library.
"""

from __future__ import annotations

from collections import namedtuple
import importlib.util
import io
from pathlib import Path
import re
import xml.etree.ElementTree as ElementTree
import zipfile

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"

# Bounds. Each one fails the read closed rather than trimming; a probe workbook
# in this repo is a few kilobytes, so every ceiling here is orders of magnitude
# above the real traffic and exists only to stop a hostile archive.
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_ENTRIES = 64
MAX_ENTRY_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_SHEETS = 32
MAX_SHARED_STRINGS = 100_000
MAX_CELLS_PER_SHEET = 50_000
MAX_ROW_INDEX = 1_048_576
MAX_COLUMN_INDEX = 16_384
MAX_CELL_TEXT = 16_384


class XlsxError(ValueError):
    """A workbook this module refuses to read or write. Never a partial result."""


def _load_probe_calcs():
    """Load server/solar_probe_calcs.py by path so the import works from any cwd.

    The C# number formatters already live there (and, under them, in
    server/solar_nec.py). Re-deriving "R" here is exactly how two renderings of
    the same double drift apart, so this module borrows instead.
    """
    path = Path(__file__).resolve().with_name("solar_probe_calcs.py")
    spec = importlib.util.spec_from_file_location("solar_probe_calcs", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


calcs = _load_probe_calcs()

Sheet = namedtuple("Sheet", ("name", "rows"))
Row = namedtuple("Row", ("index", "cells"))

_INTEGER = re.compile(r"[+-]?[0-9]+\Z")
_CELL_REF = re.compile(r"([A-Z]{1,3})([0-9]{1,7})\Z")


# --------------------------------------------------------------------------- #
# Cell values
# --------------------------------------------------------------------------- #
def column_index(letters):
    """1-based column number for a cell reference's letters ("A" -> 1, "AA" -> 27)."""
    index = 0
    for char in letters:
        if not ("A" <= char <= "Z"):
            raise XlsxError("invalid column reference: " + letters)
        index = index * 26 + (ord(char) - 64)
    if not 1 <= index <= MAX_COLUMN_INDEX:
        raise XlsxError("column reference out of range: " + letters)
    return index


def column_letters(index):
    """The inverse of `column_index`, for the writer's cell references."""
    if not 1 <= index <= MAX_COLUMN_INDEX:
        raise XlsxError("column index out of range: %r" % (index,))
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def format_number(value):
    """Render a number the way the cell's `<v>` holds it: C# "R" for a double."""
    if type(value) is int:
        return "%d" % value
    return calcs.format_roundtrip(float(value))


def parse_number(text):
    """The value a `<v>` holds: an `int` with no fraction, a finite `float` else.

    This is the ONLY numeric parse in the module, so the reader cannot disagree
    with `normalize_cell` about which spelling yields which Python type.
    """
    text = text.strip()
    if _INTEGER.match(text):
        value = int(text)
        if abs(value) > 10 ** 100:
            raise XlsxError("numeric cell out of range: " + text)
        return value
    try:
        value = float(text)
    except ValueError:
        raise XlsxError("cell is not a number: " + text[:64])
    if value != value or value in (float("inf"), float("-inf")):
        raise XlsxError("cell is not a finite number: " + text[:64])
    if abs(value) > 1e100:
        raise XlsxError("numeric cell out of range: " + text[:64])
    return value


def normalize_cell(value):
    """The value `read(write(value))` returns, which is the round-trip contract.

    A float with no fractional part renders without one ("18", never "18.0"),
    exactly as C# and EPPlus render it, so it reads back as an `int`. Negative
    zero renders "-0" and reads back as the integer 0, which is the one case
    where the round trip is not the identity and is recorded here rather than
    discovered later. Everything else is returned unchanged.
    """
    if value is None or type(value) is bool or isinstance(value, str):
        return value
    if type(value) is int:
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise XlsxError("cell value must be finite")
        if value.is_integer() and abs(value) < 2 ** 53:
            return int(value)
        return value
    raise XlsxError("unsupported cell type: " + type(value).__name__)


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #
def _resolve(target, base="xl/"):
    """A relationship target as a package path, refusing anything outside it."""
    if not isinstance(target, str) or not target:
        raise XlsxError("relationship has no target")
    path = target[1:] if target.startswith("/") else base + target
    parts = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise XlsxError("relationship target escapes the package: " + target)
        parts.append(part)
    if not parts:
        raise XlsxError("relationship target is empty: " + target)
    return "/".join(parts)


def _open(data):
    """Open the archive under every byte and entry bound before reading a part."""
    if not isinstance(data, (bytes, bytearray)):
        raise XlsxError("workbook must be bytes")
    if len(data) > MAX_ARCHIVE_BYTES:
        raise XlsxError("workbook exceeds byte limit")
    try:
        archive = zipfile.ZipFile(io.BytesIO(bytes(data)))
    except (zipfile.BadZipFile, OSError, ValueError):
        raise XlsxError("workbook is not a readable zip archive")
    infos = archive.infolist()
    if len(infos) > MAX_ENTRIES:
        raise XlsxError("workbook holds too many entries")
    declared = 0
    for info in infos:
        if info.file_size > MAX_ENTRY_BYTES:
            raise XlsxError("workbook entry exceeds byte limit: " + info.filename)
        declared += info.file_size
    if declared > MAX_TOTAL_BYTES:
        raise XlsxError("workbook exceeds its uncompressed budget")
    return archive


def _part(archive, name, required=True):
    """One archive entry's bytes, re-checked against the bound it declared.

    A deflate bomb declares a small uncompressed size and then expands past it,
    so the ACTUAL read is capped too and a part that overruns is refused.
    """
    try:
        info = archive.getinfo(name)
    except KeyError:
        if required:
            raise XlsxError("workbook is missing " + name)
        return None
    with archive.open(info) as handle:
        raw = handle.read(MAX_ENTRY_BYTES + 1)
    if len(raw) > MAX_ENTRY_BYTES:
        raise XlsxError("workbook entry exceeds byte limit: " + name)
    return raw


def _xml(raw, name):
    try:
        return ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        raise XlsxError("workbook part is not well-formed XML: " + name)


def _text(element):
    """A shared string's text: every `<t>` under it, in document order."""
    parts = []
    total = 0
    for node in element.iter("{%s}t" % MAIN_NS):
        piece = node.text or ""
        total += len(piece)
        if total > MAX_CELL_TEXT:
            raise XlsxError("shared string exceeds length limit")
        parts.append(piece)
    return "".join(parts)


def _shared_strings(archive):
    raw = _part(archive, "xl/sharedStrings.xml", required=False)
    if raw is None:
        return []
    root = _xml(raw, "xl/sharedStrings.xml")
    strings = []
    for item in root.findall("{%s}si" % MAIN_NS):
        if len(strings) >= MAX_SHARED_STRINGS:
            raise XlsxError("workbook holds too many shared strings")
        strings.append(_text(item))
    return strings


def _relationships(archive):
    root = _xml(_part(archive, "xl/_rels/workbook.xml.rels"), "xl/_rels/workbook.xml.rels")
    targets = {}
    for relationship in root.findall("{%s}Relationship" % PKG_REL_NS):
        identifier = relationship.get("Id")
        if not identifier or identifier in targets:
            raise XlsxError("workbook relationships are malformed")
        targets[identifier] = relationship.get("Target")
    return targets


def _cell_value(cell, strings):
    """One `<c>`'s value, resolved per its `t` attribute. `None` when empty."""
    kind = cell.get("t")
    if kind == "inlineStr":
        inline = cell.find("{%s}is" % MAIN_NS)
        return _text(inline) if inline is not None else None
    if kind == "e":
        raise XlsxError("workbook holds an error cell")
    value = cell.find("{%s}v" % MAIN_NS)
    if value is None or value.text is None:
        return None
    text = value.text
    if kind == "s":
        index = parse_number(text)
        if type(index) is not int or not 0 <= index < len(strings):
            raise XlsxError("shared-string index out of range: " + text[:32])
        return strings[index]
    if kind == "str":
        if len(text) > MAX_CELL_TEXT:
            raise XlsxError("cell text exceeds length limit")
        return text
    if kind == "b":
        return text.strip() not in ("0", "false", "FALSE", "")
    if kind in (None, "n"):
        return parse_number(text)
    raise XlsxError("unsupported cell type: " + str(kind))


def _sheet_rows(root, strings, name):
    """Every non-empty row of one sheet, dense from column A (rule E5)."""
    data = root.find("{%s}sheetData" % MAIN_NS)
    rows = []
    cells_seen = 0
    previous = 0
    for ordinal, row in enumerate(data.findall("{%s}row" % MAIN_NS) if data is not None else [],
                                  start=1):
        declared = row.get("r")
        index = parse_number(declared) if declared is not None else ordinal
        if type(index) is not int or not 1 <= index <= MAX_ROW_INDEX:
            raise XlsxError("row index out of range on sheet " + name)
        if index <= previous:
            raise XlsxError("rows are out of order on sheet " + name)
        previous = index
        by_column = {}
        for column_ordinal, cell in enumerate(row.findall("{%s}c" % MAIN_NS), start=1):
            cells_seen += 1
            if cells_seen > MAX_CELLS_PER_SHEET:
                raise XlsxError("sheet exceeds its cell limit: " + name)
            reference = cell.get("r")
            if reference is None:
                column = column_ordinal
            else:
                match = _CELL_REF.match(reference)
                if match is None or parse_number(match.group(2)) != index:
                    raise XlsxError("cell reference does not match its row on sheet " + name)
                column = column_index(match.group(1))
            if column in by_column:
                raise XlsxError("duplicate cell reference on sheet " + name)
            by_column[column] = _cell_value(cell, strings)
        populated = [column for column, value in by_column.items() if value is not None]
        if not populated:
            continue
        width = max(populated)
        rows.append(Row(index, [by_column.get(column) for column in range(1, width + 1)]))
    return rows


def read_workbook(data):
    """Every sheet's non-empty rows, in workbook order. Fails closed, never partial."""
    archive = _open(data)
    strings = _shared_strings(archive)
    targets = _relationships(archive)
    root = _xml(_part(archive, "xl/workbook.xml"), "xl/workbook.xml")
    container = root.find("{%s}sheets" % MAIN_NS)
    if container is None:
        raise XlsxError("workbook names no sheets")
    entries = container.findall("{%s}sheet" % MAIN_NS)
    if not entries:
        raise XlsxError("workbook names no sheets")
    if len(entries) > MAX_SHEETS:
        raise XlsxError("workbook holds too many sheets")
    sheets = []
    seen = set()
    for entry in entries:
        name = entry.get("name")
        if not name or len(name) > 255:
            raise XlsxError("sheet has no usable name")
        if name in seen:
            raise XlsxError("workbook repeats a sheet name: " + name)
        seen.add(name)
        identifier = entry.get("{%s}id" % REL_NS)
        if identifier not in targets:
            raise XlsxError("sheet has no resolvable relationship: " + name)
        part = _resolve(targets[identifier])
        sheets.append(Sheet(name, _sheet_rows(_xml(_part(archive, part), part), strings, name)))
    return sheets


def read_workbook_file(path, *, max_bytes=MAX_ARCHIVE_BYTES):
    """`read_workbook` over a file, bounding the read before it allocates."""
    path = Path(path)
    size = path.stat().st_size
    if size > max_bytes:
        raise XlsxError("workbook exceeds byte limit")
    return read_workbook(path.read_bytes())


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #
_ESCAPES = (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"), ('"', "&quot;"))
# Neither XML 1.0 nor a worksheet cell can carry a control character; a value
# holding one is refused rather than silently stripped into a different string.
_FORBIDDEN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_DECLARATION = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
# One fixed timestamp so the same workbook is the same bytes on every run.
_ZIP_DATE = (1980, 1, 1, 0, 0, 0)


def _escape(text):
    if _FORBIDDEN.search(text):
        raise XlsxError("cell text holds a control character")
    for char, entity in _ESCAPES:
        text = text.replace(char, entity)
    return text


def _sheet_xml(rows):
    parts = [_DECLARATION,
             '<worksheet xmlns="%s" xmlns:r="%s"><sheetData>' % (MAIN_NS, REL_NS)]
    for index, cells in rows:
        parts.append('<row r="%d">' % index)
        for column, value in enumerate(cells, start=1):
            if value is None:
                continue
            reference = column_letters(column) + str(index)
            if type(value) is bool:
                parts.append('<c r="%s" t="b"><v>%d</v></c>' % (reference, 1 if value else 0))
            elif isinstance(value, str):
                parts.append('<c r="%s" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>'
                             % (reference, _escape(value)))
            else:
                parts.append('<c r="%s"><v>%s</v></c>' % (reference, format_number(value)))
        parts.append("</row>")
    parts.append("</sheetData></worksheet>")
    return "".join(parts).encode("utf-8")


def _workbook_xml(names):
    parts = [_DECLARATION, '<workbook xmlns="%s" xmlns:r="%s"><sheets>' % (MAIN_NS, REL_NS)]
    for ordinal, name in enumerate(names, start=1):
        parts.append('<sheet name="%s" sheetId="%d" r:id="rId%d"/>'
                     % (_escape(name), ordinal, ordinal))
    parts.append("</sheets></workbook>")
    return "".join(parts).encode("utf-8")


def _rels_xml(count):
    parts = [_DECLARATION, '<Relationships xmlns="%s">' % PKG_REL_NS]
    for ordinal in range(1, count + 1):
        parts.append('<Relationship Id="rId%d" Type="%s/worksheet" '
                     'Target="worksheets/sheet%d.xml"/>' % (ordinal, REL_NS, ordinal))
    parts.append("</Relationships>")
    return "".join(parts).encode("utf-8")


def _content_types_xml(count):
    sheet_type = ("application/vnd.openxmlformats-officedocument."
                  "spreadsheetml.worksheet+xml")
    parts = [_DECLARATION, '<Types xmlns="%s">' % CONTENT_TYPES_NS,
             '<Default Extension="rels" ContentType="application/vnd.openxmlformats-'
             'package.relationships+xml"/>',
             '<Default Extension="xml" ContentType="application/xml"/>',
             '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
             'openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>']
    for ordinal in range(1, count + 1):
        parts.append('<Override PartName="/xl/worksheets/sheet%d.xml" ContentType="%s"/>'
                     % (ordinal, sheet_type))
    parts.append("</Types>")
    return "".join(parts).encode("utf-8")


_ROOT_RELS = (_DECLARATION + '<Relationships xmlns="%s"><Relationship Id="rId1" '
              'Type="%s/officeDocument" Target="xl/workbook.xml"/></Relationships>'
              % (PKG_REL_NS, REL_NS)).encode("utf-8")


def write_workbook(sheets):
    """The bytes of a minimal valid workbook carrying exactly these cell values.

    `sheets` is the reader's own shape: (name, [(row index, [cell, ...]), ...]).
    Strings are written inline rather than through a shared-string table, which
    is valid OOXML and keeps the writer free of a second index to keep honest;
    rule E5 compares values, not the storage the file chose for them.
    """
    prepared = []
    for entry in sheets:
        try:
            name, rows = entry
        except (TypeError, ValueError):
            raise XlsxError("sheet must be a (name, rows) pair")
        if not isinstance(name, str) or not name or len(name) > 255:
            raise XlsxError("sheet has no usable name")
        if any(name == other for other, _ in prepared):
            raise XlsxError("workbook repeats a sheet name: " + name)
        built = []
        previous = 0
        cells_seen = 0
        for row in rows:
            try:
                index, cells = row
            except (TypeError, ValueError):
                raise XlsxError("row must be an (index, cells) pair")
            if type(index) is not int or type(index) is bool or not 1 <= index <= MAX_ROW_INDEX:
                raise XlsxError("row index out of range on sheet " + name)
            if index <= previous:
                raise XlsxError("rows are out of order on sheet " + name)
            previous = index
            values = [normalize_cell(cell) for cell in cells]
            cells_seen += len(values)
            if cells_seen > MAX_CELLS_PER_SHEET:
                raise XlsxError("sheet exceeds its cell limit: " + name)
            if len(values) > MAX_COLUMN_INDEX:
                raise XlsxError("row exceeds the column limit on sheet " + name)
            for value in values:
                if isinstance(value, str) and len(value) > MAX_CELL_TEXT:
                    raise XlsxError("cell text exceeds length limit")
            built.append((index, values))
        prepared.append((name, built))
    if not prepared:
        raise XlsxError("workbook names no sheets")
    if len(prepared) > MAX_SHEETS:
        raise XlsxError("workbook holds too many sheets")

    buffer = io.BytesIO()
    count = len(prepared)
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        def put(name, payload):
            info = zipfile.ZipInfo(name, date_time=_ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, payload)

        put("[Content_Types].xml", _content_types_xml(count))
        put("_rels/.rels", _ROOT_RELS)
        put("xl/workbook.xml", _workbook_xml([name for name, _ in prepared]))
        put("xl/_rels/workbook.xml.rels", _rels_xml(count))
        for ordinal, (_, rows) in enumerate(prepared, start=1):
            put("xl/worksheets/sheet%d.xml" % ordinal, _sheet_xml(rows))
    payload = buffer.getvalue()
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise XlsxError("workbook exceeds byte limit")
    return payload
