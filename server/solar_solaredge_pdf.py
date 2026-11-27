"""Page-one primitives of a SolarEdge stringing report, read the way the plugin's PdfPig does.

The plugin's ``SolarEdgePdfConverter`` (Branch2025) takes three feeds from PdfPig 0.1.13:

A. ``page.Operations``: the RG stroke colour and the m/l/c/v/y operands of the PAGE content
   stream, in stream order, Y flipped by the page height and nothing else. No CTM is applied,
   q/Q never restores the colour, and ``re``/``h``/fill colours are ignored.
B. ``ExperimentalAccess.Paths``: every painted path with IsFilled/IsStroked, its bounding
   rectangle in user space (CTM applied) and its fill/stroke colour. Only DeviceRGB colours
   parse; anything else is reported as ``None`` and the converter treats it as black.
C. ``page.Letters``: Value, Location (the baseline origin, text rise included) and Width.

All three come from one pdfminer.six pass through a custom interpreter and device.

Contract: bounded and fail closed. The file size, page count, content bytes, operator count and
per-feed item counts are capped, and anything malformed or outside the proven subset (a rotated
or offset page, an unknown colour space, a vertical font, an operator with bad operands) raises
``SolarEdgePdfError`` rather than returning a partial parse. One linear pass; no allocation beyond
the recorded primitives.
Measured 2026-09-24 on the 1 MB fixture (213k operators, 25k paths, 8.6k letters): 3 to 5 s on
the dev host, all of it pdfminer tokenizing; budget 10 s per report.

Bounding rectangle rule (matched against the golden): the hull of every point a subpath's
segments carry, Bezier control points included, which is what PdfPig's
``PdfSubpath.GetBoundingRectangle`` reports. Letter width rule: the length of the advance vector
``(w / 1000 * Tfs * Th, 0)`` pushed through the text rendering matrix, which is PdfPig's
``Letter.Width`` for an unrotated glyph.
"""
from __future__ import annotations

import io
import itertools
import math
import os
import zlib
from dataclasses import dataclass, field

from pdfminer.pdfdevice import PDFTextDevice
from pdfminer.cmapdb import CMapParser, FileUnicodeMap
from pdfminer.pdfdocument import PDFDocument, PDFStreamParser
from pdfminer.pdffont import PDFCIDFont, PDFUnicodeNotDefined
from pdfminer.pdfinterp import PDFContentParser, PDFPageInterpreter, PDFResourceManager
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfparser import PDFParser
from pdfminer.pdftypes import PDFObjRef, PDFStream, resolve1, stream_value
from pdfminer.psparser import LIT, PSEOF, PSKeyword, PSLiteral, keyword_name, literal_name
from pdfminer.utils import MATRIX_IDENTITY, apply_matrix_pt, decode_text, mult_matrix


MAX_PDF_BYTES = 64 * 1024 * 1024
MAX_PAGES = 1000
MAX_ANNOTATIONS = 10_000
MAX_CONTENT_BYTES = 256 * 1024 * 1024
MAX_OPERATORS = 5_000_000
MAX_PATHS = 1_000_000
MAX_SEGMENTS = 4_000_000
MAX_LETTERS = 1_000_000
MAX_FORM_DEPTH = 8
MAX_OBJECT_STREAM_BYTES = 8 * 1024 * 1024
MAX_OBJECT_STREAM_OBJECTS = 8192
MAX_CMAP_BYTES = 2 * 1024 * 1024
MAX_CMAP_ENTRIES = 65536
MAX_CMAP_REPLACEMENT = 256
MAX_TEXT_CHARACTERS = 1_000_000
MAX_COPY_TEXT_BYTES = 1_000_000
MAX_LAYER_GROUPS = 4096
MAX_LAYER_DEPTH = 32
MAX_LAYER_STEPS = 100_000

BLACK = (0.0, 0.0, 0.0)


class SolarEdgePdfError(ValueError):
    """The PDF is malformed, over a bound, or outside the subset whose parity is proven."""

    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def _layer_value(value):
    # Resolve only an indirection chain here. Expression recursion is separately bounded.
    seen = set()
    while isinstance(value, PDFObjRef):
        if value.objid in seen or len(seen) >= MAX_LAYER_DEPTH:
            raise SolarEdgePdfError("BAD_OPTIONAL_CONTENT", "layer reference cycle or depth")
        seen.add(value.objid)
        value = value.resolve()
    return value


def _layer_dict(value):
    value = _layer_value(value)
    if not isinstance(value, dict):
        raise SolarEdgePdfError("BAD_OPTIONAL_CONTENT", "layer dictionary")
    return value


def _layer_list(value, limit=MAX_LAYER_GROUPS):
    value = _layer_value(value)
    if not isinstance(value, list):
        raise SolarEdgePdfError("BAD_OPTIONAL_CONTENT", "layer array")
    if len(value) > limit:
        raise SolarEdgePdfError("PDF_RESOURCE_LIMIT", "layer array")
    return value


class _DefaultLayers:
    """Document-local default view and bounded membership evaluation."""

    def __init__(self, properties):
        self.states = {}
        self.memberships = {}
        self.steps = 0
        if properties is None:
            return
        props = _layer_dict(properties)
        default = _layer_dict(props.get("D"))
        base = _layer_value(default.get("BaseState", LIT("ON")))
        if base not in (LIT("ON"), LIT("OFF")) or _layer_value(default.get("AS", [])) != []:
            raise SolarEdgePdfError("BAD_OPTIONAL_CONTENT", "unsupported default view settings")
        for group in _layer_list(props.get("OCGs")):
            if (not isinstance(group, PDFObjRef)
                    or _layer_dict(group).get("Type") is not LIT("OCG")
                    or group.objid in self.states):
                raise SolarEdgePdfError("BAD_OPTIONAL_CONTENT", "invalid group registration")
            self.states[group.objid] = base is LIT("ON")
        overrides = set()
        for enabled, key in ((True, "ON"), (False, "OFF")):
            for group in _layer_list(default.get(key, [])):
                self._group_state(group)
                if group.objid in overrides:
                    raise SolarEdgePdfError("BAD_OPTIONAL_CONTENT", "duplicate group override")
                overrides.add(group.objid)
                self.states[group.objid] = enabled

    def _group_state(self, group):
        if not isinstance(group, PDFObjRef) or group.objid not in self.states:
            raise SolarEdgePdfError("BAD_OPTIONAL_CONTENT", "unknown layer group")
        return self.states[group.objid]

    def _visit(self, amount=1):
        self.steps += amount
        if self.steps > MAX_LAYER_STEPS:
            raise SolarEdgePdfError("PDF_RESOURCE_LIMIT", "layer evaluation")

    def _expression(self, value, depth=0):
        self._visit()
        if depth >= MAX_LAYER_DEPTH:
            raise SolarEdgePdfError("PDF_RESOURCE_LIMIT", "layer expression depth")
        if depth and isinstance(value, PDFObjRef) and value.objid in self.states:
            return self._group_state(value)
        expression = _layer_list(value)
        if not expression:
            raise SolarEdgePdfError("BAD_OPTIONAL_CONTENT", "empty layer expression")
        op, *entries = expression
        if op is LIT("Not") and len(entries) == 1:
            return not self._expression(entries[0], depth + 1)
        if op not in (LIT("And"), LIT("Or")) or len(entries) < 2:
            raise SolarEdgePdfError("BAD_OPTIONAL_CONTENT", "invalid layer expression")
        # Validate every operand, even when the first resolves the Boolean result.
        states = [self._expression(entry, depth + 1) for entry in entries]
        return all(states) if op is LIT("And") else any(states)

    def visible(self, value):
        if isinstance(value, PDFObjRef):
            if value.objid in self.states:
                return self._group_state(value)
            if value.objid in self.memberships:
                return self.memberships[value.objid]
        self._visit()
        membership = _layer_dict(value)
        if _layer_value(membership.get("Type", LIT("OCMD"))) is not LIT("OCMD"):
            raise SolarEdgePdfError("BAD_OPTIONAL_CONTENT", "invalid layer membership")
        if "VE" in membership:
            result = self._expression(membership["VE"])
        else:
            groups = membership.get("OCGs")
            values = _layer_value(groups)
            entries = _layer_list(values) if isinstance(values, list) else [groups]
            policy = _layer_value(membership.get("P", LIT("AnyOn")))
            if not entries or policy not in (LIT("AnyOn"), LIT("AllOn"), LIT("AnyOff"), LIT("AllOff")):
                raise SolarEdgePdfError("BAD_OPTIONAL_CONTENT", "invalid layer membership policy")
            self._visit(len(entries))
            states = [self._group_state(group) for group in entries]
            if policy in (LIT("AnyOff"), LIT("AllOff")):
                states = [not state for state in states]
            result = all(states) if policy in (LIT("AllOn"), LIT("AllOff")) else any(states)
        if isinstance(value, PDFObjRef):
            self.memberships[value.objid] = result
        return result


def _bounded_auxiliary_data(stream, limit):
    """Decode only the proven plain/Flate subset, stopping before expansion exceeds limit."""
    if stream.data is not None:
        data = stream.data
    else:
        if stream.decipher is not None:
            raise SolarEdgePdfError("AUXILIARY_FILTER_UNSUPPORTED", "encrypted stream")
        filters = stream.get_filters()
        raw = stream.rawdata or b""
        if not filters:
            data = raw
        elif len(filters) == 1 and literal_name(filters[0][0]) in ("FlateDecode", "Fl") and not filters[0][1]:
            decoder = zlib.decompressobj()
            data = decoder.decompress(raw, limit + 1)
            if len(data) > limit or decoder.unconsumed_tail:
                raise SolarEdgePdfError("RESOURCE_LIMIT", "auxiliary decoded bytes")
            if not decoder.eof or decoder.unused_data.strip(b"\x00\t\n\x0c\r "):
                raise SolarEdgePdfError("MALFORMED_PDF", "auxiliary Flate stream")
        else:
            raise SolarEdgePdfError("AUXILIARY_FILTER_UNSUPPORTED")
    if len(data) > limit:
        raise SolarEdgePdfError("RESOURCE_LIMIT", "auxiliary decoded bytes")
    stream.data = data
    stream.rawdata = None
    return data


class _BoundedPDFDocument(PDFDocument):
    """Bound compressed-object decoding and index cardinality per document."""

    def __init__(self, *args, **kwargs):
        self.object_stream_bytes = 0
        self.object_stream_objects = 0
        super().__init__(*args, **kwargs)

    def _get_objects(self, stream):
        count, first = stream.get("N"), stream.get("First")
        if type(count) is not int or count < 1 or type(first) is not int or first < 0:
            raise SolarEdgePdfError("MALFORMED_PDF", "object stream header")
        if self.object_stream_objects + count > MAX_OBJECT_STREAM_OBJECTS:
            raise SolarEdgePdfError("RESOURCE_LIMIT", "object stream object count")
        data = _bounded_auxiliary_data(stream, MAX_OBJECT_STREAM_BYTES - self.object_stream_bytes)
        self.object_stream_bytes += len(data)
        self.object_stream_objects += count
        if first > len(data):
            raise SolarEdgePdfError("MALFORMED_PDF", "object stream offset")
        tokens = data[:first].split(None, count * 2)
        if len(tokens) != count * 2 or any(len(token) > 12 or not token.isdigit() for token in tokens):
            raise SolarEdgePdfError("MALFORMED_PDF", "object stream index")
        header = [int(token) for token in tokens]
        ids, offsets = header[::2], header[1::2]
        if len(set(ids)) != count or any(value <= 0 for value in ids) or offsets[0] != 0:
            raise SolarEdgePdfError("MALFORMED_PDF", "object stream index")
        ends = offsets[1:] + [len(data) - first]
        objects = list(header)
        for start, end in zip(offsets, ends):
            if start >= end or end > len(data) - first:
                raise SolarEdgePdfError("MALFORMED_PDF", "object stream object bounds")
            parser = PDFStreamParser(data[first + start:first + end])
            parser.set_document(self)
            try:
                _, obj = parser.nextobject()
                try:
                    parser.nextobject()
                except PSEOF:
                    objects.append(obj)
                else:
                    raise SolarEdgePdfError("MALFORMED_PDF", "extra object stream object")
            except PSEOF as exc:
                raise SolarEdgePdfError("MALFORMED_PDF", "missing object stream object") from exc
        return objects, count


@dataclass
class PagePrimitives:
    """Everything the converter reads from page one, in PdfPig's order and units.

    lines:   (sx, sy, ex, ey, (r, g, b))                  Y flipped, raw operands
    curves:  (sx, sy, c1x, c1y, c2x, c2y, ex, ey, (r, g, b))  Y flipped, raw operands
    letters: (value, x, y, width)                          user space, Y not flipped
    paths:   (filled, stroked, (left, bottom, right, top), fill_rgb|None, stroke_rgb|None)
    """

    page_width: float
    page_height: float
    lines: list = field(default_factory=list)
    curves: list = field(default_factory=list)
    letters: list = field(default_factory=list)
    paths: list = field(default_factory=list)


def _num(value):
    """A PDF numeric operand as float; anything else is malformed."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SolarEdgePdfError("BAD_OPERAND", repr(value)[:80])
    number = float(value)
    if not math.isfinite(number):
        raise SolarEdgePdfError("BAD_OPERAND", repr(value)[:80])
    return number


def _ordered(p, q):
    return (p, q) if p <= q else (q, p)


def _bezier_value(p1, p2, p3, p4, t):
    u = 1 - t
    return (u * u * u) * p1 + (3 * u * u * t) * p2 + (3 * u * t * t) * p3 + (t * t * t) * p4


def _bezier_extent(p1, p2, p3, p4):
    """One axis of PdfPig's BezierCurve.GetBoundingRectangle: the end points widened by the
    roots of the derivative that fall in [0, 1]. A zero leading coefficient yields no root,
    as PdfPig's division by zero does (its linear root is never examined)."""
    lo, hi = _ordered(p1, p4)
    three_da = 3 * (p2 - p1)
    six_db = 6 * (p3 - p2)
    three_dc = 3 * (p4 - p3)
    qa = three_da - six_db + three_dc
    qb = six_db - three_da - three_da
    qc = three_da
    disc = qb * qb - 4 * qa * qc
    if disc < 0 or qa == 0:
        return lo, hi
    root = math.sqrt(disc)
    divisor = 2 * qa
    for t in ((-qb + root) / divisor, (-qb - root) / divisor):
        if 0 <= t <= 1:
            value = _bezier_value(p1, p2, p3, p4, t)
            if value < lo:
                lo = value
            if value > hi:
                hi = value
    return lo, hi


MAX_FONT_BYTES = 32 * 1024 * 1024


class _BoundedUnicodeMap(FileUnicodeMap):
    def __init__(self, manager):
        super().__init__()
        self.manager = manager

    def add_cid2unichr(self, cid, code):
        # Check the encoded value before FileUnicodeMap allocates its Unicode string.
        if ((isinstance(code, bytes) and len(code) > MAX_CMAP_REPLACEMENT * 2)
                or (isinstance(code, PSLiteral) and len(str(code.name)) > MAX_CMAP_REPLACEMENT)):
            raise SolarEdgePdfError("RESOURCE_LIMIT", "CMap replacement")
        self.manager.cmap_entries += 1
        if self.manager.cmap_entries > MAX_CMAP_ENTRIES:
            raise SolarEdgePdfError("RESOURCE_LIMIT", "CMap entries")
        super().add_cid2unichr(cid, code)
        self.manager.cmap_characters += len(self.cid2unichr.get(cid, ""))
        if self.manager.cmap_characters > MAX_TEXT_CHARACTERS:
            raise SolarEdgePdfError("RESOURCE_LIMIT", "CMap characters")


class _BoundedCMapParser(CMapParser):
    def do_keyword(self, pos, token):
        if token is self.KEYWORD_USECMAP:
            raise SolarEdgePdfError("CMAP_INHERITANCE_UNSUPPORTED")
        return super().do_keyword(pos, token)


class _ResourceManager(PDFResourceManager):
    """Keeps each CID font's descendant dictionary, which pdfminer discards, for its hmtx."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cmap_entries = 0
        self.cmap_characters = 0
        self.cmap_bytes = 0
        self.checked_cmaps = set()

    def get_font(self, objid, spec):
        if isinstance(spec, dict) and spec.get("ToUnicode") is not None:
            cmap = resolve1(spec["ToUnicode"])
            if isinstance(cmap, PDFStream) and id(cmap) not in self.checked_cmaps:
                if cmap.get("UseCMap") is not None:
                    raise SolarEdgePdfError("CMAP_INHERITANCE_UNSUPPORTED")
                data = _bounded_auxiliary_data(cmap, MAX_CMAP_BYTES - self.cmap_bytes)
                self.cmap_bytes += len(data)
                _BoundedCMapParser(_BoundedUnicodeMap(self), io.BytesIO(data)).run()
                self.checked_cmaps.add(id(cmap))
        # A Type0 spec recurses here with its descendant, which is the dictionary we keep.
        font = super().get_font(objid, spec)
        if isinstance(font, PDFCIDFont) and not hasattr(font, "leaf_cid_spec"):
            font.leaf_cid_spec = spec if isinstance(spec, dict) else {}
        return font


def _cid_truetype_metrics(font):
    """(units_per_em, advances, cid_to_gid bytes|None) of an embedded CIDFontType2, or False.

    Bounded: the font program and the CIDToGIDMap are size-capped and every table offset is
    checked against the program length; a malformed program fails closed."""
    spec = getattr(font, "leaf_cid_spec", None) or {}
    if resolve1(spec.get("Subtype")) is None or literal_name(resolve1(spec.get("Subtype"))) != "CIDFontType2":
        return False
    descriptor = resolve1(spec.get("FontDescriptor"))
    if not isinstance(descriptor, dict) or descriptor.get("FontFile2") is None:
        return False
    data = stream_value(descriptor["FontFile2"]).get_data()
    if len(data) > MAX_FONT_BYTES or len(data) < 12:
        raise SolarEdgePdfError("BAD_FONT_PROGRAM", str(len(data)))
    num_tables = int.from_bytes(data[4:6], "big")
    if 12 + 16 * num_tables > len(data):
        raise SolarEdgePdfError("BAD_FONT_PROGRAM", "table directory")
    tables = {}
    for i in range(num_tables):
        rec = data[12 + 16 * i:28 + 16 * i]
        offset = int.from_bytes(rec[8:12], "big")
        length = int.from_bytes(rec[12:16], "big")
        if offset + length > len(data):
            raise SolarEdgePdfError("BAD_FONT_PROGRAM", "table bounds")
        tables[rec[:4]] = (offset, length)
    if not all(t in tables for t in (b"head", b"hhea", b"hmtx")):
        return False
    head, hhea, hmtx = tables[b"head"], tables[b"hhea"], tables[b"hmtx"]
    if head[1] < 20 or hhea[1] < 36:
        raise SolarEdgePdfError("BAD_FONT_PROGRAM", "head/hhea")
    units_per_em = int.from_bytes(data[head[0] + 18:head[0] + 20], "big")
    count = int.from_bytes(data[hhea[0] + 34:hhea[0] + 36], "big")
    if units_per_em == 0 or 4 * count > hmtx[1]:
        raise SolarEdgePdfError("BAD_FONT_PROGRAM", "hmtx")
    base = hmtx[0]
    advances = [int.from_bytes(data[base + 4 * i:base + 4 * i + 2], "big") for i in range(count)]
    mapping = resolve1(spec.get("CIDToGIDMap"))
    if mapping is None or (isinstance(mapping, PSLiteral) and literal_name(mapping) == "Identity"):
        cid_to_gid = None
    elif isinstance(mapping, PDFStream):
        cid_to_gid = mapping.get_data()
        if len(cid_to_gid) > 2 * 65536 * 2:
            raise SolarEdgePdfError("BAD_FONT_PROGRAM", "CIDToGIDMap size")
    else:
        raise SolarEdgePdfError("BAD_FONT_PROGRAM", "CIDToGIDMap")
    return units_per_em, advances, cid_to_gid


class _PrimitiveDevice(PDFTextDevice):
    """Collects painted paths (feed B) and letters (feed C)."""

    def __init__(self, rsrcmgr, out):
        super().__init__(rsrcmgr)
        self.out = out
        self.segments = 0
        self.paint_colors = None  # set by the interpreter before each paint
        self.visible = True
        self._cid_metrics = {}
        self.text_characters = 0

    def paint_path(self, graphicstate, stroke, fill, evenodd, path):
        paths = self.out.paths
        if len(paths) >= MAX_PATHS:
            raise SolarEdgePdfError("TOO_MANY_PATHS", str(MAX_PATHS))
        self.segments += len(path)
        if self.segments > MAX_SEGMENTS:
            raise SolarEdgePdfError("TOO_MANY_SEGMENTS", str(MAX_SEGMENTS))
        if not self.visible:
            return
        a, b, c, d, e, f = self.ctm
        min_x = min_y = math.inf
        max_x = max_y = -math.inf
        cur = start = None
        for segment in path:
            op = segment[0]
            if op == "m":
                cur = start = (a * segment[1] + c * segment[2] + e,
                               b * segment[1] + d * segment[2] + f)
                continue
            if op == "h":
                cur = start
                continue
            if cur is None:
                raise SolarEdgePdfError("SEGMENT_WITHOUT_MOVE", op)
            pts = [(a * segment[k] + c * segment[k + 1] + e, b * segment[k] + d * segment[k + 1] + f)
                   for k in range(1, len(segment), 2)]
            if op == "l":
                lo_x, hi_x = _ordered(cur[0], pts[0][0])
                lo_y, hi_y = _ordered(cur[1], pts[0][1])
                end = pts[0]
            else:
                if op == "c":
                    c1, c2, end = pts
                elif op == "v":
                    c1, (c2, end) = cur, pts
                else:  # "y"
                    c1, end = pts
                    c2 = end
                lo_x, hi_x = _bezier_extent(cur[0], c1[0], c2[0], end[0])
                lo_y, hi_y = _bezier_extent(cur[1], c1[1], c2[1], end[1])
            if lo_x < min_x:
                min_x = lo_x
            if hi_x > max_x:
                max_x = hi_x
            if lo_y < min_y:
                min_y = lo_y
            if hi_y > max_y:
                max_y = hi_y
            cur = end
        bbox = None if min_x == math.inf else (min_x, min_y, max_x, max_y)
        fill_rgb, stroke_rgb = self.paint_colors
        paths.append((bool(fill), bool(stroke), bbox,
                      fill_rgb if fill else None, stroke_rgb if stroke else None))

    def render_char(self, matrix, font, fontsize, scaling, rise, cid, ncs, graphicstate):
        letters = self.out.letters
        if len(letters) >= MAX_LETTERS:
            raise SolarEdgePdfError("TOO_MANY_LETTERS", str(MAX_LETTERS))
        if font.is_vertical():
            raise SolarEdgePdfError("VERTICAL_FONT_UNSUPPORTED")
        adv = self._glyph_width(font, cid) * fontsize * scaling
        try:
            value = font.to_unichr(cid)
        except PDFUnicodeNotDefined:
            value = f"(cid:{cid})"
        if self.text_characters + len(value) > MAX_TEXT_CHARACTERS:
            raise SolarEdgePdfError("RESOURCE_LIMIT", "extracted text characters")
        self.text_characters += len(value)
        if not self.visible:
            return adv
        a, b, c, d, e, f = matrix
        x = c * rise + e
        y = d * rise + f
        # PdfPig: Width of the rectangle (0, 0)-(w, 0) pushed through the rendering matrix.
        width = math.hypot(a * adv + x - x, b * adv + y - y)
        letters.append((value, x, y, width))
        return adv

    def _glyph_width(self, font, cid):
        """Advance in text space per unit font size. PdfPig takes a CIDFontType2's advance from
        its embedded TrueType hmtx (through CIDToGIDMap), and a simple font's from /Widths."""
        if isinstance(font, PDFCIDFont):
            metrics = self._cid_metrics.get(id(font))
            if metrics is None:
                metrics = _cid_truetype_metrics(font)
                self._cid_metrics[id(font)] = metrics
            if metrics is not False:
                units_per_em, advances, cid_to_gid = metrics
                if cid_to_gid is None:
                    gid = cid
                elif 0 <= cid < len(cid_to_gid) // 2:
                    gid = (cid_to_gid[2 * cid] << 8) | cid_to_gid[2 * cid + 1]
                else:
                    gid = None
                if gid is not None and advances:
                    advance = advances[gid] if gid < len(advances) else advances[-1]
                    return advance / units_per_em
        return font.char_width(cid)


_DEVICE_SPACES = {"DeviceGray": 1, "DeviceRGB": 3, "DeviceCMYK": 4}
_INITIAL = {"DeviceGray": (0.0,), "DeviceRGB": (0.0, 0.0, 0.0), "DeviceCMYK": (0.0, 0.0, 0.0, 1.0)}
_ARG_COUNTS = {"m": 2, "l": 2, "c": 6, "v": 4, "y": 4, "re": 4, "RG": 3, "rg": 3, "G": 1, "g": 1,
               "K": 4, "k": 4, "CS": 1, "cs": 1, "cm": 6, "Tf": 2, "Td": 2, "TD": 2, "Tm": 6,
               "Tc": 1, "Tw": 1, "Tz": 1, "TL": 1, "Ts": 1, "Tr": 1, "Tj": 1, "TJ": 1, "'": 1,
               '"': 3, "Do": 1, "w": 1, "gs": 1}
_PAINT_OPS = {"S", "s", "f", "F", "f*", "B", "B*", "b", "b*"}


@dataclass(frozen=True)
class CopyTextMismatch:
    page: int
    visible_id: str
    copy_text: str
    x: float
    y: float


@dataclass
class _ReplacementSpan:
    start: int
    text: str
    nonpainting: bool = False


class _CopyTextAudit:
    def __init__(self, approved_ids):
        self.approved_ids = frozenset(approved_ids)
        self.findings = []
        self.page = 0
        self.bytes_read = 0

    def decode(self, value):
        self.bytes_read += len(value)
        if self.bytes_read > MAX_COPY_TEXT_BYTES:
            raise SolarEdgePdfError("PDF_RESOURCE_LIMIT", "replacement text bytes")
        try:
            if value.startswith(b"\xfe\xff"):
                return value[2:].decode("utf-16-be")
            if value.startswith(b"\xef\xbb\xbf"):
                return value[3:].decode("utf-8")
            return decode_text(value)
        except UnicodeDecodeError as exc:
            raise SolarEdgePdfError("BAD_ACTUAL_TEXT", "invalid text encoding") from exc

    def finish(self, span, letters):
        if span.nonpainting or span.start == len(letters):
            return
        visible_id = "".join(letter[0] for letter in letters[span.start:])
        if visible_id in self.approved_ids and visible_id != span.text:
            _, x, y, _ = letters[span.start]
            self.findings.append(CopyTextMismatch(self.page, visible_id, span.text, x, y))


class _PrimitiveInterpreter(PDFPageInterpreter):
    """pdfminer interpreter that also records feed A and tracks colour exactly.

    pdfminer's graphic-state copy drops the colour spaces across q/Q, so colour is tracked here
    with its own q/Q stack: (stroke_space, stroke_components, fill_space, fill_components).
    """

    def __init__(self, rsrcmgr, device, out, counter, layers, depth=0, *, appearance=False,
                 copy_audit=None):
        super().__init__(rsrcmgr, device)
        self.out = out
        self.counter = counter
        self.depth = depth
        self.appearance = appearance
        self.layers = layers
        self.marked_content = []
        self.copy_audit = copy_audit
        self.replacement_span = None
        self.raw_pending = ([], []) if layers.states else None
        self.records_raw = depth == 0 or (appearance and depth == 1) or bool(layers.states)
        self.color = ("DeviceGray", (0.0,), "DeviceGray", (0.0,))
        self.color_stack = []
        # Feed A state: never reset by q/Q, S or BT, exactly as the converter keeps it.
        self.raw_stroke = BLACK
        self.raw_point = None

    def dup(self):
        if self.depth + 1 > MAX_FORM_DEPTH:
            raise SolarEdgePdfError("FORM_TOO_DEEP", str(MAX_FORM_DEPTH))
        child = _PrimitiveInterpreter(self.rsrcmgr, self.device, self.out, self.counter,
                                      self.layers, self.depth + 1, appearance=self.appearance,
                                      copy_audit=self.copy_audit)
        child.color = self.color
        child.replacement_span = self.replacement_span
        return child

    # ---- execution with bounds and strict operands ----
    def execute(self, streams):
        streams = [resolve1(stream) for stream in streams]
        total = 0
        for stream in streams:
            if not isinstance(stream, PDFStream):
                raise SolarEdgePdfError("BAD_CONTENT_STREAM")
            total += len(stream.get_data())
            if total > MAX_CONTENT_BYTES:
                raise SolarEdgePdfError("CONTENT_TOO_LARGE", str(MAX_CONTENT_BYTES))
        self.counter[1] += total
        if self.counter[1] > MAX_CONTENT_BYTES:
            raise SolarEdgePdfError("CONTENT_TOO_LARGE", str(MAX_CONTENT_BYTES))
        try:
            parser = PDFContentParser(streams)
        except PSEOF:
            return
        while True:
            try:
                _, obj = parser.nextobject()
            except PSEOF:
                break
            if not isinstance(obj, PSKeyword):
                self.push(obj)
                continue
            self.counter[0] += 1
            if self.counter[0] > MAX_OPERATORS:
                raise SolarEdgePdfError("TOO_MANY_OPERATORS", str(MAX_OPERATORS))
            name = keyword_name(obj)
            if self.appearance and name in ("W", "W*"):
                raise SolarEdgePdfError("BAD_STAMP_APPEARANCE", "explicit clipping path")
            expected = _ARG_COUNTS.get(name)
            if expected is not None and len(self.argstack) < expected:
                raise SolarEdgePdfError("MISSING_OPERANDS", name)
            method = "do_%s" % name.replace("*", "_a").replace('"', "_w").replace("'", "_q")
            func = getattr(self, method, None)
            if func is None:
                self.argstack = []
                continue
            nargs = func.__code__.co_argcount - 1
            args = self.pop(nargs) if nargs else []
            if len(args) != nargs:
                raise SolarEdgePdfError("MISSING_OPERANDS", name)
            if name in _PAINT_OPS:
                self._set_paint_colors()
            func(*args)
            if self.raw_pending is not None and (name in _PAINT_OPS or name == "n"):
                self._finish_raw_path(name)
            self.argstack = []
        if self.marked_content:
            raise SolarEdgePdfError("BAD_OPTIONAL_CONTENT", "unclosed marked content")

    @property
    def visible(self):
        return not self.marked_content or self.marked_content[-1][0]

    def _begin_marked_content(self, visible, properties=None):
        if len(self.marked_content) >= MAX_LAYER_DEPTH:
            raise SolarEdgePdfError("PDF_RESOURCE_LIMIT", "marked content depth")
        visible = self.visible and visible
        previous = self.replacement_span
        if self.copy_audit is not None and visible and previous is None:
            self.replacement_span = self._open_replacement_span(properties)
        self.marked_content.append((visible, previous))

    def _open_replacement_span(self, properties):
        if isinstance(properties, PSLiteral):
            named = _layer_value(self.resources.get("Properties"))
            properties = named.get(literal_name(properties)) if isinstance(named, dict) else None
        properties = _layer_value(properties)
        if not isinstance(properties, dict) or "ActualText" not in properties:
            return None
        replacement = _layer_value(properties["ActualText"])
        if not isinstance(replacement, bytes):
            raise SolarEdgePdfError("BAD_ACTUAL_TEXT", "replacement must be a PDF string")
        return _ReplacementSpan(len(self.out.letters), self.copy_audit.decode(replacement))

    def do_BMC(self, tag):
        self._begin_marked_content(True)

    def do_BDC(self, tag, properties):
        if tag is LIT("OC"):
            if isinstance(properties, PSLiteral):
                properties = _layer_dict(self.resources.get("Properties")).get(literal_name(properties))
            self._begin_marked_content(self.layers.visible(properties), properties)
        else:
            self._begin_marked_content(True, properties)

    def do_EMC(self):
        if not self.marked_content:
            raise SolarEdgePdfError("BAD_OPTIONAL_CONTENT", "unbalanced marked content")
        _, previous = self.marked_content.pop()
        span = self.replacement_span
        if span is not previous:
            self.copy_audit.finish(span, self.out.letters)
        self.replacement_span = previous

    # ---- colour tracking (feed B) ----
    def _set_paint_colors(self):
        self.device.visible = self.visible
        s_space, s_comp, f_space, f_comp = self.color
        self.device.paint_colors = (f_comp if f_space == "DeviceRGB" else None,
                                    s_comp if s_space == "DeviceRGB" else None)

    def do_q(self):
        super().do_q()
        self.color_stack.append(self.color)

    def do_Q(self):
        super().do_Q()
        if self.color_stack:
            self.color = self.color_stack.pop()

    def do_RG(self, r, g, b):
        rgb = (_num(r), _num(g), _num(b))
        self.color = ("DeviceRGB", rgb, self.color[2], self.color[3])
        if self.records_raw:
            self.raw_stroke = rgb

    def do_rg(self, r, g, b):
        self.color = (self.color[0], self.color[1], "DeviceRGB", (_num(r), _num(g), _num(b)))

    def do_G(self, gray):
        self.color = ("DeviceGray", (_num(gray),), self.color[2], self.color[3])

    def do_g(self, gray):
        self.color = (self.color[0], self.color[1], "DeviceGray", (_num(gray),))

    def do_K(self, c, m, y, k):
        self.color = ("DeviceCMYK", (_num(c), _num(m), _num(y), _num(k)), self.color[2],
                      self.color[3])

    def do_k(self, c, m, y, k):
        self.color = (self.color[0], self.color[1], "DeviceCMYK",
                      (_num(c), _num(m), _num(y), _num(k)))

    def _space(self, name):
        key = literal_name(name) if isinstance(name, PSLiteral) else None
        if key in _DEVICE_SPACES:
            return key
        space = self.csmap.get(key) if key is not None else None
        if space is None or space.name not in _DEVICE_SPACES:
            raise SolarEdgePdfError("COLORSPACE_UNSUPPORTED", str(key))
        return space.name

    def do_CS(self, name):
        space = self._space(name)
        self.color = (space, _INITIAL[space], self.color[2], self.color[3])

    def do_cs(self, name):
        space = self._space(name)
        self.color = (self.color[0], self.color[1], space, _INITIAL[space])

    def _components(self, space):
        n = _DEVICE_SPACES[space]
        if len(self.argstack) < n:
            raise SolarEdgePdfError("MISSING_OPERANDS", "SC")
        return tuple(_num(v) for v in self.argstack[-n:])

    def do_SC(self):
        self.color = (self.color[0], self._components(self.color[0]), self.color[2],
                      self.color[3])

    def do_SCN(self):
        self.do_SC()

    def do_sc(self):
        self.color = (self.color[0], self.color[1], self.color[2],
                      self._components(self.color[2]))

    def do_scn(self):
        self.do_sc()

    # ---- path construction: pdfminer path (feed B) plus the raw feed A ----
    def _record_raw(self, feed, record):
        target = self.out.lines if feed == 0 else self.out.curves
        if self.raw_pending is not None:
            target = self.raw_pending[feed]
            if len(target) >= MAX_SEGMENTS:
                raise SolarEdgePdfError("PDF_RESOURCE_LIMIT", "unpainted layer geometry")
        target.append(record)

    def _finish_raw_path(self, paint):
        lines, curves = self.raw_pending
        if paint != "n" and self.visible:
            self.out.lines.extend(lines)
            self.out.curves.extend(curves)
        lines.clear()
        curves.clear()
        self.raw_point = None

    def _raw_xy(self, x, y):
        # Layered layouts may paint wiring inside translated forms. Place it
        # on the page so the visible wire and its panel share coordinates.
        if self.appearance or self.raw_pending is not None:
            x, y = apply_matrix_pt(self.ctm, (x, y))
        return x, self.out.page_height - y

    def do_m(self, x, y):
        xf, yf = _num(x), _num(y)
        self.curpath.append(("m", xf, yf))
        if self.records_raw:
            self.raw_point = self._raw_xy(xf, yf)

    def do_l(self, x, y):
        xf, yf = _num(x), _num(y)
        self.curpath.append(("l", xf, yf))
        if self.raw_point is not None:
            end = self._raw_xy(xf, yf)
            self._record_raw(0, (*self.raw_point, *end, self.raw_stroke))
            self.raw_point = end

    def do_c(self, x1, y1, x2, y2, x3, y3):
        v = (_num(x1), _num(y1), _num(x2), _num(y2), _num(x3), _num(y3))
        self.curpath.append(("c",) + v)
        if self.raw_point is not None:
            end = self._raw_xy(*v[4:6])
            self._record_raw(1, (*self.raw_point, *self._raw_xy(*v[0:2]),
                                 *self._raw_xy(*v[2:4]), *end, self.raw_stroke))
            self.raw_point = end

    def do_v(self, x2, y2, x3, y3):
        v = (_num(x2), _num(y2), _num(x3), _num(y3))
        self.curpath.append(("v",) + v)
        if self.raw_point is not None:
            start = self.raw_point
            end = self._raw_xy(*v[2:4])
            self._record_raw(1, (*start, *start, *self._raw_xy(*v[0:2]), *end, self.raw_stroke))
            self.raw_point = end

    def do_y(self, x1, y1, x3, y3):
        v = (_num(x1), _num(y1), _num(x3), _num(y3))
        self.curpath.append(("y",) + v)
        if self.raw_point is not None:
            end = self._raw_xy(*v[2:4])
            self._record_raw(1, (*self.raw_point, *self._raw_xy(*v[0:2]), *end, *end, self.raw_stroke))
            self.raw_point = end

    def do_re(self, x, y, w, h):
        xf, yf, wf, hf = _num(x), _num(y), _num(w), _num(h)
        self.curpath.extend((("m", xf, yf), ("l", xf + wf, yf), ("l", xf + wf, yf + hf),
                             ("l", xf, yf + hf), ("h",)))

    def do_cm(self, a1, b1, c1, d1, e1, f1):
        for value in (a1, b1, c1, d1, e1, f1):
            _num(value)
        super().do_cm(a1, b1, c1, d1, e1, f1)

    def do_Tf(self, fontid, fontsize):
        key = literal_name(fontid) if isinstance(fontid, PSLiteral) else None
        if key not in self.fontmap:
            raise SolarEdgePdfError("UNDEFINED_FONT", str(key))
        _num(fontsize)
        super().do_Tf(fontid, fontsize)

    def do_TJ(self, seq):
        if self.textstate.font is None:
            raise SolarEdgePdfError("TEXT_WITHOUT_FONT")
        if not isinstance(seq, list) or not all(
                isinstance(o, bytes) or (isinstance(o, (int, float)) and not isinstance(o, bool))
                for o in seq):
            raise SolarEdgePdfError("BAD_TEXT_OPERAND")
        self.device.visible = self.visible
        first_letter = len(self.out.letters)
        super().do_TJ(seq)
        if (self.replacement_span is not None and self.textstate.render in (3, 7)
                and len(self.out.letters) > first_letter):
            self.replacement_span.nonpainting = True

    def do_Do(self, xobjid):
        if not self.visible:
            return
        reference = self.xobjmap.get(literal_name(xobjid))
        if reference is not None:
            xobj = stream_value(reference)
            if "OC" in xobj.attrs and not self.layers.visible(xobj.get("OC")):
                return
        saved = self.device.ctm
        try:
            super().do_Do(xobjid)
        finally:
            self.device.set_ctm(saved)


def _count_pages(doc):
    return sum(1 for _ in itertools.islice(PDFPage.create_pages(doc), MAX_PAGES + 1))


def _appearance_numbers(value, size):
    values = resolve1(value)
    if not isinstance(values, (list, tuple)) or len(values) != size:
        raise SolarEdgePdfError("BAD_STAMP_APPEARANCE", "invalid rectangle or matrix")
    return tuple(_num(resolve1(item)) for item in values)


def _stamp_placement(annot, appearance):
    left, bottom, right, top = _appearance_numbers(annot.get("Rect"), 4)
    left, right = _ordered(left, right)
    bottom, top = _ordered(bottom, top)
    x0, y0, x1, y1 = _appearance_numbers(appearance.get("BBox"), 4)
    matrix = _appearance_numbers(appearance.get("Matrix", MATRIX_IDENTITY), 6)
    a, b, c, d, _, _ = matrix
    if x0 == x1 or y0 == y1 or not ((a and d and not (b or c)) or (b and c and not (a or d))):
        raise SolarEdgePdfError("BAD_STAMP_APPEARANCE", "unsupported form bounds or orientation")
    corners = [apply_matrix_pt(matrix, (x, y)) for x in (x0, x1) for y in (y0, y1)]
    xs, ys = zip(*corners)
    width, height = max(xs) - min(xs), max(ys) - min(ys)
    if not (0 < width < math.inf and 0 < height < math.inf and right > left and top > bottom):
        raise SolarEdgePdfError("BAD_STAMP_APPEARANCE", "empty rectangle or bounds")
    sx, sy = (right - left) / width, (top - bottom) / height
    fit = (sx, 0, 0, sy, left - min(xs) * sx, bottom - min(ys) * sy)
    placement = mult_matrix(matrix, fit)
    if not all(math.isfinite(value) for value in placement):
        raise SolarEdgePdfError("BAD_STAMP_APPEARANCE", "nonfinite placement")
    return placement, (left, bottom, right, top)


def _check_stamp_bounds(out, bounds, starts):
    left, bottom, right, top = bounds
    left, bottom = max(0, left), max(0, bottom)
    right, top = min(out.page_width, right), min(out.page_height, top)

    def inside(x, y):
        return left <= x <= right and bottom <= y <= top

    paths, letters, lines, curves = starts
    for i in range(paths, len(out.paths)):
        bbox = out.paths[i][2]
        if bbox is not None and not (inside(*bbox[:2]) and inside(*bbox[2:])):
            raise SolarEdgePdfError("BAD_STAMP_APPEARANCE", "clipped path")
    for i in range(letters, len(out.letters)):
        _, x, y, _ = out.letters[i]
        if not inside(x, y):
            raise SolarEdgePdfError("BAD_STAMP_APPEARANCE", "clipped text origin")
    for records, start in ((out.lines, lines), (out.curves, curves)):
        for i in range(start, len(records)):
            points = records[i][:-1]
            if any(not inside(points[j], out.page_height - points[j + 1])
                   for j in range(0, len(points), 2)):
                raise SolarEdgePdfError("BAD_STAMP_APPEARANCE", "clipped wire")


def _render_printable_stamps(page, resources, interpreter):
    annotations = resolve1(page.annots)
    if not isinstance(annotations, list):
        return
    interpreter.counter[2] += len(annotations)
    if interpreter.counter[2] > MAX_ANNOTATIONS:
        raise SolarEdgePdfError("RESOURCE_LIMIT", "annotation count")
    for reference in annotations:
        annot = resolve1(reference)
        if not isinstance(annot, dict) or resolve1(annot.get("Subtype")) is not LIT("Stamp"):
            continue
        flags = resolve1(annot.get("F", 0))
        if type(flags) is not int or not flags & 4 or flags & 2:  # Print, Hidden
            continue
        if "OC" in annot and not interpreter.layers.visible(annot["OC"]):
            continue
        appearances = resolve1(annot.get("AP"))
        if not isinstance(appearances, dict):
            continue
        normal = resolve1(appearances.get("N"))
        if isinstance(normal, dict):
            state = resolve1(annot.get("AS"))
            normal = resolve1(normal.get(literal_name(state))) if isinstance(state, PSLiteral) else None
        if normal is None:
            continue
        if not isinstance(normal, PDFStream) or resolve1(normal.get("Subtype")) is not LIT("Form"):
            raise SolarEdgePdfError("BAD_STAMP_APPEARANCE", "normal appearance is not a form")
        if "OC" in normal.attrs and not interpreter.layers.visible(normal.get("OC")):
            continue
        form_resources = resolve1(normal.get("Resources", resources))
        if not isinstance(form_resources, dict):
            raise SolarEdgePdfError("BAD_STAMP_APPEARANCE", "invalid resources")
        # Annotation graphics state starts independently of the page and other stamps.
        out = interpreter.out
        starts = (len(out.paths), len(out.letters), len(out.lines), len(out.curves))
        placement, bounds = _stamp_placement(annot, normal)
        stamp = _PrimitiveInterpreter(interpreter.rsrcmgr, interpreter.device, out,
                                      interpreter.counter, interpreter.layers, depth=1, appearance=True)
        stamp.render_contents(form_resources, [normal], ctm=placement)
        _check_stamp_bounds(out, bounds, starts)


def extract_primitives(pdf_bytes):
    """Read page one of the report for callers using the original extractor."""
    return next(_iter_primitives(pdf_bytes, first_only=True))


def iter_page_primitives(pdf_bytes):
    """Read each physical page independently with shared report limits."""
    return _iter_primitives(pdf_bytes, first_only=False)


def audit_copy_text(pdf_bytes, approved_ids):
    """Find exact approved glyph IDs with differing PDF ActualText in page content.

    Coordinates are the first glyph's baseline in PDF user space. This follows the
    default optional-content layers and includes form invocations. Each span must
    contain one complete ID. Annotation appearances and OCR are outside the audit.
    """
    audit = _CopyTextAudit(approved_ids)
    for _ in _iter_primitives(pdf_bytes, first_only=False, copy_audit=audit):
        pass
    return audit.findings


def _iter_primitives(pdf_bytes, *, first_only, copy_audit=None):
    if not isinstance(pdf_bytes, (bytes, bytearray)):
        raise SolarEdgePdfError("NOT_BYTES")
    if len(pdf_bytes) > MAX_PDF_BYTES:
        raise SolarEdgePdfError("PDF_TOO_LARGE", str(MAX_PDF_BYTES))
    if not bytes(pdf_bytes[:1024]).lstrip().startswith(b"%PDF-"):
        raise SolarEdgePdfError("NOT_A_PDF")
    try:
        doc = _BoundedPDFDocument(PDFParser(io.BytesIO(bytes(pdf_bytes))))
        pages = _count_pages(doc)
        if pages == 0:
            raise SolarEdgePdfError("NO_PAGES")
        if pages > MAX_PAGES:
            raise SolarEdgePdfError("TOO_MANY_PAGES", str(MAX_PAGES))
        layers = _DefaultLayers(doc.catalog.get("OCProperties"))
        page_objects = PDFPage.create_pages(doc)
        if first_only:
            page_objects = itertools.islice(page_objects, 1)
        rsrcmgr = _ResourceManager(caching=True)
        counter = [0, 0, 0]  # operators, decoded content bytes, annotation entries
        paths = segments = letters = text_characters = 0
        for page_number, page in enumerate(page_objects, start=1):
            # Raw-coordinate parity is proven only for default units. Never silently scale labels.
            unit = page.attrs.get("UserUnit", 1)
            if type(unit) not in (int, float) or unit != 1:
                raise SolarEdgePdfError("PAGE_UNIT_UNSUPPORTED")
            x0, y0, x1, y1 = (_num(v) for v in page.mediabox)
            crop = tuple(_num(v) for v in page.cropbox)
            if (x0, y0) != (0.0, 0.0) or crop != (x0, y0, x1, y1):
                raise SolarEdgePdfError("PAGE_BOX_UNSUPPORTED", f"{page.mediabox} {page.cropbox}")
            if page.rotate % 360 != 0:
                raise SolarEdgePdfError("PAGE_ROTATION_UNSUPPORTED", str(page.rotate))
            out = PagePrimitives(page_width=x1 - x0, page_height=y1 - y0)
            device = _PrimitiveDevice(rsrcmgr, out)
            if copy_audit is not None:
                copy_audit.page = page_number
            interpreter = _PrimitiveInterpreter(rsrcmgr, device, out, counter, layers,
                                                copy_audit=copy_audit)
            resources = resolve1(page.resources) or {}
            interpreter.render_contents(resources, page.contents, ctm=MATRIX_IDENTITY)
            _render_printable_stamps(page, resources, interpreter)
            paths += len(out.paths)
            segments += device.segments
            letters += len(out.letters)
            text_characters += device.text_characters
            if (paths > MAX_PATHS or segments > MAX_SEGMENTS or letters > MAX_LETTERS
                    or text_characters > MAX_TEXT_CHARACTERS):
                raise SolarEdgePdfError("PDF_RESOURCE_LIMIT", "report primitives")
            yield out
    except SolarEdgePdfError:
        raise
    except (RecursionError, MemoryError) as exc:
        raise SolarEdgePdfError("PDF_RESOURCE_LIMIT", type(exc).__name__) from exc
    except Exception as exc:  # pdfminer raises many unrelated types on malformed input
        raise SolarEdgePdfError("MALFORMED_PDF", f"{type(exc).__name__}: {exc}"[:200]) from exc


def extract_primitives_from_file(path):
    """Size-checked read of a PDF file, then :func:`extract_primitives`."""
    size = os.path.getsize(path)
    if size > MAX_PDF_BYTES:
        raise SolarEdgePdfError("PDF_TOO_LARGE", str(MAX_PDF_BYTES))
    with open(path, "rb") as handle:
        data = handle.read(MAX_PDF_BYTES + 1)
    return extract_primitives(data)


def primitives_to_golden(prims):
    """The S1 sections in the golden dump's JSON shape (colours as [r, g, b] or None)."""
    def rgb(color):
        return None if color is None else list(color)
    return {
        "page_width": prims.page_width,
        "page_height": prims.page_height,
        "lines": [{"start": [ln[0], ln[1]], "end": [ln[2], ln[3]], "stroke": list(ln[4])}
                  for ln in prims.lines],
        "curves": [{"start": [c[0], c[1]], "control1": [c[2], c[3]], "control2": [c[4], c[5]],
                    "end": [c[6], c[7]], "stroke": list(c[8])} for c in prims.curves],
        "letters": [{"value": t[0], "x": t[1], "y": t[2], "width": t[3]} for t in prims.letters],
        "paths": [{"filled": p[0], "stroked": p[1],
                   "bbox": None if p[2] is None else list(p[2]),
                   "fill": rgb(p[3]), "stroke": rgb(p[4])} for p in prims.paths],
    }
