"""Intake JSON -> minimal ASCII DXF: the browser engine's reach into a version
whose stored payload is intake (every version on an APS_LIVE=0 deployment, and
a browser-edited version whose full-fidelity sidecar is gone or unbound).

This is the exact inverse of ``dxf_intake.parse_dxf_bytes`` over the intake
subset (layers, polylines, texts), and that inverse is PINNED by
tests/test_intake_dxf.py: parsing the emitted bytes with the intake's own
source name reproduces ``layers`` and ``polylines`` exactly. What the subset
cannot carry (xdata, faces, images) is not invented here; the DWG
plan leg keeps those by handle on the real drawing.
Complete bounded block catalogues and INSERTs also round-trip here. For an
incomplete catalogue, only the supported children actually captured are emitted.

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
    circles = intake.get("circles", [])
    arcs = intake.get("arcs", [])
    inserts = intake.get("inserts", [])
    if not isinstance(layers_in, list) or len(layers_in) > MAX_LAYERS:
        _fail(f"layers must be a list of at most {MAX_LAYERS}")
    if not isinstance(polylines, list) or not isinstance(texts, list):
        _fail("polylines and texts must be lists")
    if not isinstance(circles, list) or not isinstance(arcs, list):
        _fail("circles and arcs must be lists")
    if not isinstance(inserts, list):
        _fail("inserts must be a list")
    if len(polylines) + len(texts) + len(circles) + len(arcs) + len(inserts) > MAX_ENTITIES:
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
    # W4g-3: circles and arcs (ADDITIVE fields, the browser engine's kinds).
    # The centre is WCS in the intake; a tilted normal (dxf_intake keeps it)
    # puts the centre back into that OCS for the file.
    for field, rows in (("circles", circles), ("arcs", arcs)):
        for k, ent in enumerate(rows):
            where = f"{field}[{k}]"
            if not isinstance(ent, dict):
                _fail(f"{where}: not an object")
            layer = _layer_name(ent.get("layer"), where)
            centre = ent.get("c")
            if not isinstance(centre, (list, tuple)) or len(centre) not in (2, 3):
                _fail(f"{where}: c must be [x, y] or [x, y, z]")
            cx = _number(centre[0], f"{where}.c")
            cy = _number(centre[1], f"{where}.c")
            cz = _number(centre[2], f"{where}.c") if len(centre) == 3 else 0.0
            radius = _number(ent.get("r"), f"{where}.r")
            if not radius > 0.0:
                _fail(f"{where}: r must be positive")
            normal = ent.get("nrm", [0.0, 0.0, 1.0])
            if not isinstance(normal, (list, tuple)) or len(normal) != 3:
                _fail(f"{where}: nrm must be [nx, ny, nz]")
            normal = [_number(v, f"{where}.nrm") for v in normal]
            if not (normal[0] ** 2 + normal[1] ** 2 + normal[2] ** 2) > 0.0:
                _fail(f"{where}: nrm must not be the zero vector")
            angles = ()
            if field == "arcs":
                start = _number(ent.get("start_deg"), f"{where}.start_deg")
                end = _number(ent.get("end_deg"), f"{where}.end_deg")
                angles = (start, end)
            h = _real_handle(ent.get("handle"), where, real)
            if h is not None:
                highest = max(highest, int(h, 16))
            note_layer(layer)
            kinds.append(("round", layer, field, (cx, cy, cz), radius, normal, angles, h))

    for k, ent in enumerate(inserts):
        where = f"inserts[{k}]"
        if not isinstance(ent, dict):
            _fail(f"{where}: not an object")
        name = _layer_name(ent.get("name"), where)
        if "blocks" in intake:
            raw_blocks = intake["blocks"]
            known = raw_blocks if isinstance(raw_blocks, dict) else {}
            if not name.startswith("*") and name not in known:
                _fail(f"{where}: unresolved block reference {name}")
        layer = _layer_name(ent.get("layer"), where)
        point = [_number(ent.get(axis), f"{where}.{axis}") for axis in ("x", "y", "z")]
        normal = _vector(ent.get("nrm", [0, 0, 1]), f"{where}.nrm")
        if not any(normal):
            _fail(f"{where}: nrm must not be the zero vector")
        scale = _vector(ent.get("scale", [1, 1, 1]), f"{where}.scale")
        rotation = _number(ent.get("rot", 0), f"{where}.rot")
        h = _real_handle(ent.get("handle"), where, real)
        if h is not None:
            highest = max(highest, int(h, 16))
            h = ent["handle"]
        note_layer(layer)
        kinds.append(("insert", layer, name, point, normal, scale, rotation, h))
    blocks = _validated_blocks(intake["blocks"], note_layer) if "blocks" in intake else None
    if blocks is not None:
        total_points += sum(len(child.get("pts", [])) for block in blocks.values()
                            for child in block["children"])
        if total_points > MAX_POINTS:
            _fail(f"more than {MAX_POINTS} points in total")

    next_handle = highest + 1

    def fresh_handle():
        nonlocal next_handle
        h = format(next_handle, "X")
        next_handle += 1
        return h

    # Settle synthetic entity handles before table/definition handles, so
    # BLOCK_RECORD identities are above every model-space entity identity.
    kinds = [row[:-1] + (row[-1] if row[-1] is not None else fresh_handle(),) for row in kinds]

    # Pass 2: emit. One flat list of lines, joined once.
    out: List[str] = [
        "0", "SECTION", "2", "TABLES",
        "0", "TABLE", "2", "LAYER", "70", str(len(layer_order)),
    ]
    for name in layer_order:
        out += ["0", "LAYER", "100", "AcDbSymbolTableRecord", "100", "AcDbLayerTableRecord",
                "2", name, "70", "0", "62", "7", "6", "Continuous"]
    out += ["0", "ENDTAB"]
    block_records = {}
    if blocks is not None:
        table_handle = fresh_handle()
        block_records = {name: fresh_handle() for name in ("*Model_Space", "*Paper_Space", *blocks)}
        out += ["0", "TABLE", "2", "BLOCK_RECORD", "5", table_handle, "330", "0",
                "100", "AcDbSymbolTable", "70", str(len(block_records))]
        for name, h in block_records.items():
            out += ["0", "BLOCK_RECORD", "5", h, "330", table_handle,
                    "100", "AcDbSymbolTableRecord", "100", "AcDbBlockTableRecord",
                    "2", name, "70", "0"]
        out += ["0", "ENDTAB"]
    out += ["0", "ENDSEC"]
    if blocks is not None:
        out += ["0", "SECTION", "2", "BLOCKS"]
        for name, owner in block_records.items():
            block = blocks.get(name, {"base": [0.0, 0.0, 0.0], "children": []})
            out += ["0", "BLOCK", "5", fresh_handle(), "330", owner,
                    "100", "AcDbEntity", "8", "0", "100", "AcDbBlockBegin",
                    "2", name, "70", "0", *_point_groups(block["base"]), "3", name, "1", ""]
            for child in block["children"]:
                out += _emit_block_child(child, fresh_handle(), owner)
            out += ["0", "ENDBLK", "5", fresh_handle(), "330", owner,
                    "100", "AcDbEntity", "8", "0", "100", "AcDbBlockEnd"]
        out += ["0", "ENDSEC"]
    out += ["0", "SECTION", "2", "ENTITIES"]
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
        elif row[0] == "insert":
            _, layer, name, point, normal, scale, rotation, _ = row
            point = _wcs_to_ocs(point, normal)
            out += ["0", "INSERT", "5", h]
            if block_records:
                out += ["330", block_records["*Model_Space"]]
            out += ["100", "AcDbEntity", "8", layer, "100", "AcDbBlockReference", "2", name,
                    *_point_groups(point), "41", _num(scale[0]), "42", _num(scale[1]),
                    "43", _num(scale[2]), "50", _num(rotation), *_point_groups(normal, 210)]
        elif row[0] == "round":
            _, layer, field, (cx, cy, cz), radius, normal, angles, _ = row
            tilted = normal != [0.0, 0.0, 1.0]
            ox, oy, oz = _wcs_to_ocs((cx, cy, cz), normal) if tilted else (cx, cy, cz)
            out += ["0", "CIRCLE" if field == "circles" else "ARC", "5", h,
                    "100", "AcDbEntity", "8", layer, "100", "AcDbCircle",
                    "10", _num(ox), "20", _num(oy), "30", _num(oz), "40", _num(radius)]
            if tilted:
                out += ["210", _num(normal[0]), "220", _num(normal[1]), "230", _num(normal[2])]
            if field == "arcs":
                out += ["100", "AcDbArc", "50", _num(angles[0]), "51", _num(angles[1])]
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


def _vector(value, where, width=3):
    if not isinstance(value, (list, tuple)) or len(value) != width:
        _fail(f"{where}: expected {width} coordinates")
    return [_number(v, where) for v in value]


def _validated_blocks(blocks, note_layer):
    if not isinstance(blocks, dict) or len(blocks) > 200:
        _fail("blocks must be an object of at most 200 definitions")
    result = {}
    for name, block in blocks.items():
        _layer_name(name, "block name")
        if name.startswith("*"):
            _fail("reserved or anonymous block name")
        if not isinstance(block, dict):
            _fail(f"blocks[{name}]: not an object")
        base = _vector(block.get("base"), f"blocks[{name}].base")
        children = block.get("children")
        if not isinstance(children, list) or len(children) > 60:
            _fail(f"blocks[{name}]: children must be a list of at most 60")
        rows = []
        for k, child in enumerate(children):
            where = f"blocks[{name}].children[{k}]"
            if not isinstance(child, dict):
                _fail(f"{where}: not an object")
            kind = child.get("kind")
            if kind == "OTHER":
                # The catalogue names this type but carries no geometry for it.
                continue
            layer = _layer_name(child.get("layer"), where)
            row = {"kind": kind, "layer": layer}
            if kind in ("LINE", "LWPOLYLINE"):
                points = child.get("pts")
                if not isinstance(points, list) or not 2 <= len(points) <= MAX_POINTS_PER_ENTITY:
                    _fail(f"{where}: invalid point count")
                if kind == "LINE" and len(points) != 2:
                    _fail(f"{where}: LINE must have two points")
                row["pts"] = [_vector(p, where, 3 if kind == "LINE" else 2) for p in points]
                if kind == "LWPOLYLINE":
                    if not isinstance(child.get("closed"), bool):
                        _fail(f"{where}: closed must be a boolean")
                    row.update(closed=child["closed"], nrm=_vector(child.get("nrm"), where),
                               elev=_number(child.get("elev"), where))
                    if not any(row["nrm"]):
                        _fail(f"{where}: nrm must not be the zero vector")
            elif kind in ("CIRCLE", "ARC"):
                row.update(c=_vector(child.get("c"), where), r=_number(child.get("r"), where),
                           nrm=_vector(child.get("nrm"), where))
                if row["r"] <= 0:
                    _fail(f"{where}: r must be positive")
                if not any(row["nrm"]):
                    _fail(f"{where}: nrm must not be the zero vector")
                if kind == "ARC":
                    row.update(start_deg=_number(child.get("start_deg"), where),
                               end_deg=_number(child.get("end_deg"), where))
            elif kind == "TEXT":
                row.update(pt=_vector(child.get("pt"), where),
                           height=_number(child.get("height"), where),
                           rot=_number(child.get("rot"), where),
                           text=_text_value(child.get("text"), where).replace("|", " "))
            else:
                _fail(f"{where}: unsupported block child kind")
            note_layer(layer)
            rows.append(row)
        result[name] = {"base": base, "children": rows}
    return result


def _point_groups(point, code=10):
    return [part for j, v in enumerate(point) for part in (str(code + 10 * j), _num(v))]


def _emit_block_child(child, handle, owner):
    kind = child["kind"]
    out = ["0", kind, "5", handle, "330", owner, "100", "AcDbEntity", "8", child["layer"]]
    if kind == "LINE":
        out += ["100", "AcDbLine", *_point_groups(child["pts"][0]), *_point_groups(child["pts"][1], 11)]
    elif kind == "LWPOLYLINE":
        out += ["100", "AcDbPolyline", "90", str(len(child["pts"])),
                "70", "1" if child["closed"] else "0", "38", _num(child["elev"])]
        for point in child["pts"]:
            out += _point_groups(point)
        out += _point_groups(child["nrm"], 210)
    elif kind in ("CIRCLE", "ARC"):
        out += ["100", "AcDbCircle", *_point_groups(child["c"]), "40", _num(child["r"])]
        out += _point_groups(child["nrm"], 210)
        if kind == "ARC":
            out += ["100", "AcDbArc", "50", _num(child["start_deg"]), "51", _num(child["end_deg"])]
    else:
        out += ["100", "AcDbText", *_point_groups(child["pt"]), "40", _num(child["height"]),
                "50", _num(child["rot"]), "1", child["text"], "100", "AcDbText"]
    return out


def _wcs_to_ocs(point, normal):
    """The inverse of dxf_intake._ocs_to_wcs: project a WCS point onto the
    arbitrary-axis frame of `normal` (unit-normalized here)."""
    nx, ny, nz = normal
    length = (nx * nx + ny * ny + nz * nz) ** 0.5
    nx, ny, nz = nx / length, ny / length, nz / length
    if abs(nx) < 1 / 64.0 and abs(ny) < 1 / 64.0:
        ax = (nz, 0.0, -nx)  # (0,1,0) x n
    else:
        ax = (-ny, nx, 0.0)  # (0,0,1) x n
    al = (ax[0] ** 2 + ax[1] ** 2 + ax[2] ** 2) ** 0.5 or 1.0
    ax = (ax[0] / al, ax[1] / al, ax[2] / al)
    ay = (ny * ax[2] - nz * ax[1], nz * ax[0] - nx * ax[2], nx * ax[1] - ny * ax[0])
    x, y, z = point
    return (x * ax[0] + y * ax[1] + z * ax[2],
            x * ay[0] + y * ay[1] + z * ay[2],
            x * nx + y * ny + z * nz)


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
