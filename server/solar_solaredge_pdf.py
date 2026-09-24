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
from dataclasses import dataclass, field

from pdfminer.pdfdevice import PDFTextDevice
from pdfminer.pdfdocument import PDFDocument
from pdfminer.pdffont import PDFCIDFont, PDFUnicodeNotDefined
from pdfminer.pdfinterp import PDFContentParser, PDFPageInterpreter, PDFResourceManager
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfparser import PDFParser
from pdfminer.pdftypes import PDFStream, resolve1, stream_value
from pdfminer.psparser import PSEOF, PSKeyword, PSLiteral, keyword_name, literal_name


MAX_PDF_BYTES = 64 * 1024 * 1024
MAX_PAGES = 1000
MAX_CONTENT_BYTES = 256 * 1024 * 1024
MAX_OPERATORS = 5_000_000
MAX_PATHS = 1_000_000
MAX_SEGMENTS = 4_000_000
MAX_LETTERS = 1_000_000
MAX_FORM_DEPTH = 8

BLACK = (0.0, 0.0, 0.0)


class SolarEdgePdfError(ValueError):
    """The PDF is malformed, over a bound, or outside the subset whose parity is proven."""

    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


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


class _ResourceManager(PDFResourceManager):
    """Keeps each CID font's descendant dictionary, which pdfminer discards, for its hmtx."""

    def get_font(self, objid, spec):
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
        self._cid_metrics = {}

    def paint_path(self, graphicstate, stroke, fill, evenodd, path):
        paths = self.out.paths
        if len(paths) >= MAX_PATHS:
            raise SolarEdgePdfError("TOO_MANY_PATHS", str(MAX_PATHS))
        self.segments += len(path)
        if self.segments > MAX_SEGMENTS:
            raise SolarEdgePdfError("TOO_MANY_SEGMENTS", str(MAX_SEGMENTS))
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


class _PrimitiveInterpreter(PDFPageInterpreter):
    """pdfminer interpreter that also records feed A and tracks colour exactly.

    pdfminer's graphic-state copy drops the colour spaces across q/Q, so colour is tracked here
    with its own q/Q stack: (stroke_space, stroke_components, fill_space, fill_components).
    """

    def __init__(self, rsrcmgr, device, out, counter, depth=0):
        super().__init__(rsrcmgr, device)
        self.out = out
        self.counter = counter
        self.depth = depth
        self.color = ("DeviceGray", (0.0,), "DeviceGray", (0.0,))
        self.color_stack = []
        # Feed A state: never reset by q/Q, S or BT, exactly as the converter keeps it.
        self.raw_stroke = BLACK
        self.raw_point = None

    def dup(self):
        if self.depth + 1 > MAX_FORM_DEPTH:
            raise SolarEdgePdfError("FORM_TOO_DEEP", str(MAX_FORM_DEPTH))
        child = _PrimitiveInterpreter(self.rsrcmgr, self.device, self.out, self.counter,
                                      self.depth + 1)
        child.color = self.color
        return child

    # ---- execution with bounds and strict operands ----
    def execute(self, streams):
        total = 0
        for stream in streams:
            if not isinstance(stream, PDFStream):
                raise SolarEdgePdfError("BAD_CONTENT_STREAM")
            total += len(stream.get_data())
            if total > MAX_CONTENT_BYTES:
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
            expected = _ARG_COUNTS.get(name)
            if expected is not None and len(self.argstack) < expected:
                raise SolarEdgePdfError("MISSING_OPERANDS", name)
            method = "do_%s" % name.replace("*", "_a").replace('"', "_w").replace("'", "_q")
            func = getattr(self, method, None)
            if func is None:
                self.argstack = []
                continue
            nargs = func.__code__.co_argcount - 1
            if nargs:
                args = self.pop(nargs)
                if len(args) != nargs:
                    raise SolarEdgePdfError("MISSING_OPERANDS", name)
                if name in _PAINT_OPS:
                    self._set_paint_colors()
                func(*args)
            else:
                if name in _PAINT_OPS:
                    self._set_paint_colors()
                func()
            self.argstack = []

    # ---- colour tracking (feed B) ----
    def _set_paint_colors(self):
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
        if self.depth == 0:
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
    def _flip(self, y):
        return self.out.page_height - y

    def do_m(self, x, y):
        xf, yf = _num(x), _num(y)
        self.curpath.append(("m", xf, yf))
        if self.depth == 0:
            self.raw_point = (xf, self._flip(yf))

    def do_l(self, x, y):
        xf, yf = _num(x), _num(y)
        self.curpath.append(("l", xf, yf))
        if self.depth == 0 and self.raw_point is not None:
            end = (xf, self._flip(yf))
            self.out.lines.append((self.raw_point[0], self.raw_point[1], end[0], end[1],
                                   self.raw_stroke))
            self.raw_point = end

    def do_c(self, x1, y1, x2, y2, x3, y3):
        v = (_num(x1), _num(y1), _num(x2), _num(y2), _num(x3), _num(y3))
        self.curpath.append(("c",) + v)
        if self.depth == 0 and self.raw_point is not None:
            flip = self._flip
            end = (v[4], flip(v[5]))
            self.out.curves.append((self.raw_point[0], self.raw_point[1], v[0], flip(v[1]),
                                    v[2], flip(v[3]), end[0], end[1], self.raw_stroke))
            self.raw_point = end

    def do_v(self, x2, y2, x3, y3):
        v = (_num(x2), _num(y2), _num(x3), _num(y3))
        self.curpath.append(("v",) + v)
        if self.depth == 0 and self.raw_point is not None:
            flip = self._flip
            start = self.raw_point
            end = (v[2], flip(v[3]))
            self.out.curves.append((start[0], start[1], start[0], start[1], v[0], flip(v[1]),
                                    end[0], end[1], self.raw_stroke))
            self.raw_point = end

    def do_y(self, x1, y1, x3, y3):
        v = (_num(x1), _num(y1), _num(x3), _num(y3))
        self.curpath.append(("y",) + v)
        if self.depth == 0 and self.raw_point is not None:
            flip = self._flip
            end = (v[2], flip(v[3]))
            self.out.curves.append((self.raw_point[0], self.raw_point[1], v[0], flip(v[1]),
                                    end[0], end[1], end[0], end[1], self.raw_stroke))
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
        super().do_TJ(seq)

    def do_Do(self, xobjid):
        saved = self.device.ctm
        super().do_Do(xobjid)
        self.device.set_ctm(saved)


def _count_pages(doc):
    return sum(1 for _ in itertools.islice(PDFPage.create_pages(doc), MAX_PAGES + 1))


def extract_primitives(pdf_bytes):
    """Read page one of ``pdf_bytes`` into :class:`PagePrimitives`. Fails closed."""
    if not isinstance(pdf_bytes, (bytes, bytearray)):
        raise SolarEdgePdfError("NOT_BYTES")
    if len(pdf_bytes) > MAX_PDF_BYTES:
        raise SolarEdgePdfError("PDF_TOO_LARGE", str(MAX_PDF_BYTES))
    if not bytes(pdf_bytes[:1024]).lstrip().startswith(b"%PDF-"):
        raise SolarEdgePdfError("NOT_A_PDF")
    try:
        doc = PDFDocument(PDFParser(io.BytesIO(bytes(pdf_bytes))))
        pages = _count_pages(doc)
        if pages == 0:
            raise SolarEdgePdfError("NO_PAGES")
        if pages > MAX_PAGES:
            raise SolarEdgePdfError("TOO_MANY_PAGES", str(MAX_PAGES))
        page = next(PDFPage.create_pages(doc))
        x0, y0, x1, y1 = (_num(v) for v in page.mediabox)
        crop = tuple(_num(v) for v in page.cropbox)
        if (x0, y0) != (0.0, 0.0) or crop != (x0, y0, x1, y1):
            raise SolarEdgePdfError("PAGE_BOX_UNSUPPORTED", f"{page.mediabox} {page.cropbox}")
        if page.rotate % 360 != 0:
            raise SolarEdgePdfError("PAGE_ROTATION_UNSUPPORTED", str(page.rotate))
        out = PagePrimitives(page_width=x1 - x0, page_height=y1 - y0)
        rsrcmgr = _ResourceManager(caching=True)
        device = _PrimitiveDevice(rsrcmgr, out)
        interpreter = _PrimitiveInterpreter(rsrcmgr, device, out, [0])
        interpreter.render_contents(resolve1(page.resources) or {}, page.contents,
                                    ctm=(1, 0, 0, 1, 0, 0))
    except SolarEdgePdfError:
        raise
    except (RecursionError, MemoryError) as exc:
        raise SolarEdgePdfError("PDF_RESOURCE_LIMIT", type(exc).__name__) from exc
    except Exception as exc:  # pdfminer raises many unrelated types on malformed input
        raise SolarEdgePdfError("MALFORMED_PDF", f"{type(exc).__name__}: {exc}"[:200]) from exc
    return out


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
