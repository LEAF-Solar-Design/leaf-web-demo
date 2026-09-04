"""Intake JSON -> minimal ASCII DXF: the browser engine's reach into a version
whose stored payload is intake (every version on an APS_LIVE=0 deployment, and
a browser-edited version whose full-fidelity sidecar is gone or unbound).

This is the exact inverse of ``dxf_intake.parse_dxf_bytes`` over the intake
subset (layers, polylines, texts), and that inverse is PINNED by
tests/test_intake_dxf.py: parsing the emitted bytes with the intake's own
source name reproduces ``layers`` and ``polylines`` exactly. What the subset
cannot carry (xdata, inserts, faces, images) is not invented here; the DWG
plan leg keeps those by handle on the real drawing.

Hardened and bounded, fail-closed: every field is validated BEFORE a byte is
emitted; a malformed intake raises ``IntakeDxfError`` and nothing is returned.
Control characters (a newline in a layer name would break the DXF pair
grammar, i.e. inject entities) are refused, not escaped. Allocation is one
list of lines joined once; no quadratic pass over entities or layers.

Handles: an intake handle that is a DXF handle (1..32 hex digits) is emitted
as group 5 verbatim, so the engine and the write contract see the drawing's
own identities. A synthetic handle (the parser's ``L<n>`` for a DXF that
carried none) is replaced by a fresh hex handle above every real one, unique
by construction; identity does not matter on that leg because such a version
can only ever be re-saved as a whole DXF (the sidecar leg), never through the
by-handle plan. Duplicate real handles are refused.
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, List

from dxf_intake import _TEXT_MAX_CHARS as MAX_TEXT_CHARS

# Bounds shared with the write contract (server/mutation_plan.py) so a document
# the plan could never describe is refused at the same size here.
MAX_ENTITIES = 200_000
MAX_POINTS_PER_ENTITY = 10_000
MAX_POINTS = 1_000_000
MAX_LAYERS = 10_000
MAX_LAYER_CHARS = 255
MAX_COORDINATE = 1_000_000_000.0
# The engine's TEXT needs a height; the intake carries none (it keeps the
# words, not the typography), so every synthesized text gets this one.
TEXT_HEIGHT = "2.5"

_HANDLE_RE = re.compile(r"^[0-9A-Fa-f]{1,32}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class IntakeDxfError(ValueError):
    """The intake is not something this synthesizer can honestly emit."""


def _fail(msg: str) -> None:
    raise IntakeDxfError(msg)


def _layer_name(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_LAYER_CHARS:
        _fail(f"{where}: layer must be a non-empty string of at most {MAX_LAYER_CHARS} chars")
    if _CONTROL_RE.search(value):
        _fail(f"{where}: layer name carries a control character")
    return value


def _number(value: Any, where: str) -> float:
    # bool is an int subclass; a True coordinate is a bug, not a 1.0.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{where}: coordinate is not a number")
    f = float(value)
    if not math.isfinite(f) or abs(f) > MAX_COORDINATE:
        _fail(f"{where}: coordinate is not finite or exceeds {MAX_COORDINATE:g}")
    return f


def _num(f: float) -> str:
    # repr is the shortest string that parses back to the SAME float, which
    # is what makes the round-trip pin exact; a fixed-decimal format would
    # round a 7-decimal coordinate and break it.
    return repr(f)


def _text_value(value: Any, where: str) -> str:
    if not isinstance(value, str):
        _fail(f"{where}: text is not a string")
    if _CONTROL_RE.search(value):
        _fail(f"{where}: text carries a control character")
    # The parser normalizes whitespace and caps the value; emitting the same
    # normal form keeps parse(emit(x)) == x for parser-produced intakes.
    return " ".join(value.split())[:MAX_TEXT_CHARS]


def intake_to_dxf(intake: Dict[str, Any]) -> bytes:
    """Emit ASCII DXF bytes for the intake subset. Raises IntakeDxfError."""
    if not isinstance(intake, dict):
        _fail("intake is not an object")
    layers_in = intake.get("layers", [])
    polylines = intake.get("polylines", [])
    texts = intake.get("texts", [])
    if not isinstance(layers_in, list) or len(layers_in) > MAX_LAYERS:
        _fail(f"layers must be a list of at most {MAX_LAYERS}")
    if not isinstance(polylines, list) or not isinstance(texts, list):
        _fail("polylines and texts must be lists")
    if len(polylines) + len(texts) > MAX_ENTITIES:
        _fail(f"more than {MAX_ENTITIES} entities")

    # Layer order is part of the intake shape (first seen). The table lists
    # the intake's layers in order, then any entity layer it forgot, so the
    # parser's first-seen order over the emitted file equals the input's.
    layer_order: List[str] = []
    layer_seen: set = set()
    for k, name in enumerate(layers_in):
        name = _layer_name(name, f"layers[{k}]")
        if name in layer_seen:
            _fail(f"layers[{k}]: duplicate layer {name!r}")
        layer_seen.add(name)
        layer_order.append(name)

    def note_layer(name: str) -> None:
        if name not in layer_seen:
            layer_seen.add(name)
            layer_order.append(name)

    # Pass 1: validate every entity and settle handles. Real hex handles are
    # kept (uppercased, the DXF norm) and must be unique; synthetic ones get
    # fresh handles above the highest real one.
    real: set = set()
    highest = 0xFF
    kinds: List[tuple] = []
    total_points = 0
    for k, poly in enumerate(polylines):
        where = f"polylines[{k}]"
        if not isinstance(poly, dict):
            _fail(f"{where}: not an object")
        layer = _layer_name(poly.get("layer"), where)
        closed = poly.get("closed", False)
        if not isinstance(closed, bool):
            _fail(f"{where}: closed must be a boolean")
        pts = poly.get("pts")
        if not isinstance(pts, list) or len(pts) < 2:
            _fail(f"{where}: pts must hold at least two points")
        if len(pts) > MAX_POINTS_PER_ENTITY:
            _fail(f"{where}: more than {MAX_POINTS_PER_ENTITY} points")
        total_points += len(pts)
        if total_points > MAX_POINTS:
            _fail(f"more than {MAX_POINTS} points in total")
        coords: List[tuple] = []
        for j, pt in enumerate(pts):
            if not isinstance(pt, (list, tuple)) or len(pt) not in (2, 3):
                _fail(f"{where}.pts[{j}]: a point is [x, y] or [x, y, z]")
            x = _number(pt[0], f"{where}.pts[{j}]")
            y = _number(pt[1], f"{where}.pts[{j}]")
            z = _number(pt[2], f"{where}.pts[{j}]") if len(pt) == 3 else 0.0
            coords.append((x, y, z))
        handle = poly.get("handle")
        h = _real_handle(handle, where, real)
        if h is not None:
            highest = max(highest, int(h, 16))
        note_layer(layer)
        kinds.append(("poly", layer, closed, coords, h))
    for k, tx in enumerate(texts):
        where = f"texts[{k}]"
        if not isinstance(tx, dict):
            _fail(f"{where}: not an object")
        kind = tx.get("kind", "TEXT")
        if kind not in ("TEXT", "MTEXT"):
            _fail(f"{where}: kind must be TEXT or MTEXT")
        layer = _layer_name(tx.get("layer"), where)
        pt = tx.get("pt")
        if not isinstance(pt, (list, tuple)) or len(pt) < 2:
            _fail(f"{where}: pt must be [x, y]")
        x = _number(pt[0], f"{where}.pt")
        y = _number(pt[1], f"{where}.pt")
        value = _text_value(tx.get("text"), where)
        if not value:
            # The parser drops an empty text on the way in; dropping it on the
            # way out keeps both legs consistent instead of inventing a glyph.
            continue
        h = _real_handle(tx.get("handle"), where, real)
        if h is not None:
            highest = max(highest, int(h, 16))
        note_layer(layer)
        kinds.append(("text", layer, kind, (x, y), value, h))

    # Pass 2: emit. One flat list of lines, joined once.
    out: List[str] = [
        "0", "SECTION", "2", "TABLES",
        "0", "TABLE", "2", "LAYER", "70", str(len(layer_order)),
    ]
    for name in layer_order:
        out += ["0", "LAYER", "100", "AcDbSymbolTableRecord", "100", "AcDbLayerTableRecord",
                "2", name, "70", "0", "62", "7", "6", "Continuous"]
    out += ["0", "ENDTAB", "0", "ENDSEC", "0", "SECTION", "2", "ENTITIES"]
    next_handle = highest + 1
    for row in kinds:
        h = row[-1]
        if h is None:
            h = format(next_handle, "X")
            next_handle += 1
        if row[0] == "poly":
            _, layer, closed, coords, _ = row
            z0 = coords[0][2]
            planar = all(c[2] == z0 for c in coords)
            if planar:
                out += ["0", "LWPOLYLINE", "5", h, "100", "AcDbEntity", "8", layer,
                        "100", "AcDbPolyline", "90", str(len(coords)),
                        "70", "1" if closed else "0", "38", _num(z0)]
                for x, y, _z in coords:
                    out += ["10", _num(x), "20", _num(y)]
            else:
                # A polyline whose vertices differ in z is a classic 3D
                # POLYLINE (flag 8) with per-vertex z; the parser keeps each z.
                out += ["0", "POLYLINE", "5", h, "100", "AcDbEntity", "8", layer,
                        "100", "AcDb3dPolyline", "66", "1",
                        "70", str(8 | (1 if closed else 0))]
                for x, y, z in coords:
                    out += ["0", "VERTEX", "100", "AcDbEntity", "8", layer,
                            "100", "AcDbVertex", "100", "AcDb3dPolylineVertex",
                            "10", _num(x), "20", _num(y), "30", _num(z), "70", "32"]
                out += ["0", "SEQEND", "100", "AcDbEntity", "8", layer]
        else:
            _, layer, kind, (x, y), value, _ = row
            if kind == "TEXT":
                out += ["0", "TEXT", "5", h, "100", "AcDbEntity", "8", layer,
                        "100", "AcDbText", "10", _num(x), "20", _num(y), "30", "0.0",
                        "40", TEXT_HEIGHT, "1", value, "100", "AcDbText"]
            else:
                out += ["0", "MTEXT", "5", h, "100", "AcDbEntity", "8", layer,
                        "100", "AcDbMText", "10", _num(x), "20", _num(y), "30", "0.0",
                        "40", TEXT_HEIGHT, "1", value]
    out += ["0", "ENDSEC", "0", "EOF"]
    return ("\n".join(out) + "\n").encode("utf-8")


def _real_handle(handle: Any, where: str, real: set):
    """A DXF handle (hex) uppercased and registered unique, or None for a
    synthetic/absent one. Anything that is neither a string nor None is a
    malformed intake."""
    if handle is None or handle == "":
        return None
    if not isinstance(handle, str):
        _fail(f"{where}: handle is not a string")
    if not _HANDLE_RE.match(handle):
        if len(handle) > 64 or _CONTROL_RE.search(handle):
            _fail(f"{where}: handle is malformed")
        return None  # synthetic (the parser's L<n>), replaced in pass 2
    h = handle.upper()
    if h in real:
        _fail(f"{where}: duplicate handle {h}")
    real.add(h)
    return h
