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
Polylines may carry finite ``bulges``: per-vertex values emit nonzero groups
only for planar polylines; other list lengths are inspection flags and emit none.

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
# Mirrors server/mutation_plan.py's LINEWEIGHTS (the DXF group-370 enumeration).
_LINEWEIGHTS = frozenset({-3, -2, -1, 0, 5, 9, 13, 15, 18, 20, 25, 30, 35,
                          40, 50, 53, 60, 70, 80, 90, 100, 106, 120, 140, 158, 200, 211})


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


def _entity_property_groups(properties: Dict[str, Any], handle: Any, where: str) -> List[str]:
    """62 / 6 / 370 / 420 DXF group pairs for one entity, from
    `intake["properties"][handle]` (the EP shape); only fields the record
    actually carries are emitted, so an entity without one is left ByLayer/
    absent, same as before this record existed. w4g-7b-03s-d D2: when the
    record carries a non-null `rgb` ([r, g, b], each 0..255), 420 (true
    colour) IS written back, immediately after 62 (AutoCAD writes 62 then
    420) so the reader's own 420 -> rgb mapping round-trips; the write
    contract itself only ever sets an ACI (server/mutation_plan.py
    `set_color`), never rgb."""
    record = properties.get(handle)
    if not isinstance(record, dict):
        return []
    out: List[str] = []
    if "aci" in record:
        aci = record["aci"]
        if isinstance(aci, bool) or not isinstance(aci, int) or not 0 <= aci <= 256:
            _fail(f"{where}: properties.aci must be an integer in 0..256")
        out += ["62", str(aci)]
    if record.get("rgb") is not None:
        rgb = record["rgb"]
        if (not isinstance(rgb, (list, tuple)) or len(rgb) != 3
                or any(isinstance(c, bool) or not isinstance(c, int) or not 0 <= c <= 255
                       for c in rgb)):
            _fail(f"{where}: properties.rgb must be [r, g, b] each an integer in 0..255")
        packed = (rgb[0] << 16) | (rgb[1] << 8) | rgb[2]
        out += ["420", str(packed)]
    if "linetype" in record:
        name = record["linetype"]
        if (not isinstance(name, str) or not name or len(name) > 255
                or _CONTROL_RE.search(name)):
            _fail(f"{where}: properties.linetype is not a safe linetype name")
        out += ["6", name]
    if "lineweight" in record:
        weight = record["lineweight"]
        if isinstance(weight, bool) or not isinstance(weight, int) or weight not in _LINEWEIGHTS:
            _fail(f"{where}: properties.lineweight is not a valid enumeration value")
        out += ["370", str(weight)]
    return out


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
    dimensions = intake.get("dimensions", [])
    dimstyles_in = intake.get("dimstyles", [])
    mleaders = intake.get("mleaders", [])
    mlstyles_in = intake.get("mlstyles", [])
    if not isinstance(mleaders, list) or not isinstance(mlstyles_in, list):
        _fail("mleaders and mlstyles must be lists")
    if not isinstance(layers_in, list) or len(layers_in) > MAX_LAYERS:
        _fail(f"layers must be a list of at most {MAX_LAYERS}")
    if not isinstance(polylines, list) or not isinstance(texts, list):
        _fail("polylines and texts must be lists")
    if not isinstance(circles, list) or not isinstance(arcs, list):
        _fail("circles and arcs must be lists")
    if not isinstance(inserts, list):
        _fail("inserts must be a list")
    if not isinstance(dimensions, list) or not isinstance(dimstyles_in, list):
        _fail("dimensions and dimstyles must be lists")
    if (len(polylines) + len(texts) + len(circles) + len(arcs) + len(inserts) + len(dimensions) + len(mleaders)
            > MAX_ENTITIES):
        _fail(f"more than {MAX_ENTITIES} entities")
    properties = intake.get("properties", {})
    if not isinstance(properties, dict):
        _fail("properties must be an object")

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

    # DIMSTYLE table: emitted only when the intake actually names a dimstyle
    # or carries a dimension (byte-identical output otherwise), and always
    # carries "Standard" so an added dimension's default style resolves.
    dimstyle_order: List[str] = []
    dimstyle_seen: set = set()
    for k, name in enumerate(dimstyles_in):
        if (not isinstance(name, str) or not name or len(name) > MAX_LAYER_CHARS
                or _CONTROL_RE.search(name)):
            _fail(f"dimstyles[{k}]: not a safe dimstyle name")
        if name not in dimstyle_seen:
            dimstyle_seen.add(name)
            dimstyle_order.append(name)
    emit_dimstyle_table = bool(dimstyle_order) or bool(dimensions)
    if emit_dimstyle_table and "Standard" not in dimstyle_seen:
        dimstyle_order = ["Standard"] + dimstyle_order

    # Pass 1: validate every entity and settle handles. Real hex handles are
    # kept (uppercased, the DXF norm) and must be unique; synthetic ones get
    # fresh handles above the highest real one.
    real: set = set()
    highest = 0xFF
    kinds: List[tuple] = []
    # Parallel to `kinds` (one entry per append, "text" rows included as an
    # empty list): the 62/6/370 group pairs for that entity, looked up here
    # by the intake's OWN declared handle (before synthesis), never the
    # settled/uppercased one, matching how `properties` is keyed everywhere
    # else in this codebase.
    kind_properties: List[List[str]] = []
    kind_sources: List[Dict[str, Any]] = []
    total_points = 0
    mlstyles = {}
    for k, style in enumerate(mlstyles_in):
        where = f"mlstyles[{k}]"
        if not isinstance(style, dict):
            _fail(f"{where}: not an object")
        name = _layer_name(style.get("name"), where)
        if name.casefold() in {n.casefold() for n in mlstyles}:
            _fail(f"{where}: duplicate style")
        segments = style.get("segments")
        if isinstance(segments, bool) or not isinstance(segments, int):
            _fail(f"{where}: segments must be an integer")
        mlstyles[name] = {"name": name, "textstyle": _layer_name(style.get("textstyle"), where),
                          "segments": segments,
                          **{field: _number(style.get(field), where)
                             for field in ("height", "arrow", "dogleg", "gap")}}
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
        bulges = None
        if "bulges" in poly:
            values = poly["bulges"]
            if not isinstance(values, list):
                _fail(f"{where}.bulges: must be a list of finite numbers")
            bulges = []
            for j, value in enumerate(values):
                if (isinstance(value, bool) or not isinstance(value, (int, float))):
                    _fail(f"{where}.bulges[{j}]: must be a finite number")
                try:
                    value = float(value)
                except OverflowError:
                    _fail(f"{where}.bulges[{j}]: must be a finite number")
                if not math.isfinite(value):
                    _fail(f"{where}.bulges[{j}]: must be a finite number")
                bulges.append(value)
            if len(bulges) != len(coords):
                bulges = None
        handle = poly.get("handle")
        h = _real_handle(handle, where, real)
        if h is not None:
            highest = max(highest, int(h, 16))
        note_layer(layer)
        kinds.append(("poly", layer, closed, coords, bulges, h))
        kind_sources.append(poly)
        kind_properties.append(_entity_property_groups(properties, handle, where))
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
        kind_sources.append(tx)
        kind_properties.append([])  # TEXT carries no colour/linetype/lineweight round trip
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
            kind_sources.append(ent)
            kind_properties.append(_entity_property_groups(properties, ent.get("handle"), where))

    for k, ent in enumerate(inserts):
        where = f"inserts[{k}]"
        if not isinstance(ent, dict):
            _fail(f"{where}: not an object")
        name = _layer_name(ent.get("name"), where)
        raw_blocks = intake.get("blocks", {})
        known = raw_blocks if isinstance(raw_blocks, dict) else {}
        if name.startswith("*") or name not in known:
            _fail(f"{where}: unresolved block reference {name}")
        layer = _layer_name(ent.get("layer"), where)
        point = [_number(ent.get(axis), f"{where}.{axis}") for axis in ("x", "y", "z")]
        normal = _vector(ent.get("nrm", [0, 0, 1]), f"{where}.nrm")
        if not any(normal):
            _fail(f"{where}: nrm must not be the zero vector")
        scale = _vector(ent.get("scale", [1, 1, 1]), f"{where}.scale")
        rotation = _number(ent.get("rot", 0), f"{where}.rot")
        degrees = math.degrees(rotation)
        # Recover whole degrees when their radians have the exact same
        # six-decimal reading (for example 1.570796 represents 90 degrees).
        whole_degrees = round(degrees)
        rotation = (float(whole_degrees)
                    if rotation == round(math.radians(whole_degrees), 6)
                    else round(degrees, 6))
        h = _real_handle(ent.get("handle"), where, real)
        if h is not None:
            highest = max(highest, int(h, 16))
            h = ent["handle"]
        note_layer(layer)
        kinds.append(("insert", layer, name, point, normal, scale, rotation, h))
        kind_sources.append(ent)
        kind_properties.append(_entity_property_groups(properties, ent.get("handle"), where))
    for k, ent in enumerate(dimensions):
        where = f"dimensions[{k}]"
        if not isinstance(ent, dict):
            _fail(f"{where}: not an object")
        dimtype = ent.get("type")
        if dimtype not in ("LINEAR", "ALIGNED"):
            _fail(f"{where}: type must be LINEAR or ALIGNED")
        p1 = _vector(ent.get("p1"), f"{where}.p1")
        p2 = _vector(ent.get("p2"), f"{where}.p2")
        dimline = _vector(ent.get("dimline"), f"{where}.dimline")
        rotation = _number(ent.get("rotation_deg", 0), f"{where}.rotation_deg")
        style = ent.get("style")
        if (not isinstance(style, str) or not style or len(style) > MAX_LAYER_CHARS
                or _CONTROL_RE.search(style)):
            _fail(f"{where}: style is not a safe dimstyle name")
        normal = _vector(ent.get("nrm", [0.0, 0.0, 1.0]), f"{where}.nrm")
        if not any(normal):
            _fail(f"{where}: nrm must not be the zero vector")
        measurement = _number(ent.get("measurement"), f"{where}.measurement")
        # A legacy record predates the layer field; emit it on "0" like before.
        layer = _layer_name(ent.get("layer", "0"), where)
        h = _real_handle(ent.get("handle"), where, real)
        if h is not None:
            highest = max(highest, int(h, 16))
        note_layer(layer)
        kinds.append(("dim", dimtype, layer, p1, p2, dimline, rotation, style, normal, measurement, h))
        kind_sources.append(ent)
        kind_properties.append([])  # DIMENSION carries no colour/linetype/lineweight round trip

    for k, ent in enumerate(mleaders):
        where = f"mleaders[{k}]"
        if not isinstance(ent, dict):
            _fail(f"{where}: not an object")
        layer = _layer_name(ent.get("layer"), where)
        style = _layer_name(ent.get("style"), where)
        if style not in mlstyles:
            _fail(f"{where}: unresolved mleader style")
        pts = ent.get("pts")
        if not isinstance(pts, list) or not 2 <= len(pts) <= MAX_POINTS_PER_ENTITY:
            _fail(f"{where}: invalid point count")
        total_points += len(pts)
        if total_points > MAX_POINTS:
            _fail(f"more than {MAX_POINTS} points in total")
        record = {"layer": layer, "style": style,
                  "textstyle": _layer_name(ent.get("textstyle"), where),
                  "pts": [_vector(p, where) for p in pts],
                  **{field: _vector(ent.get(field), where)
                     for field in ("landing", "dogleg_dir", "textpt")},
                  **{field: _number(ent.get(field), where)
                     for field in ("height", "arrow", "dogleg")}}
        if record["pts"][-1] != record["landing"]:
            _fail(f"{where}: last point must be the landing")
        attachment = ent.get("attachment")
        if isinstance(attachment, bool) or not isinstance(attachment, int):
            _fail(f"{where}: attachment must be an integer")
        text = ent.get("text")
        if not isinstance(text, str) or _CONTROL_RE.search(text):
            _fail(f"{where}: text must be a string without control characters")
        record.update(attachment=attachment, text=text)
        h = _real_handle(ent.get("handle"), where, real)
        if h is not None:
            highest = max(highest, int(h, 16))
        note_layer(layer)
        kinds.append(("mleader", record, h))
        kind_sources.append(ent)
        kind_properties.append([])

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
    textstyle_handles = {}
    mlstyle_handles = {}
    ml_linetype = None
    if mlstyles or mleaders:
        ml_linetype = fresh_handle()
        textstyle_handles = {name: fresh_handle() for name in dict.fromkeys(
            ["Standard"] + [s["textstyle"] for s in mlstyles.values()] +
            [row[1]["textstyle"] for row in kinds if row[0] == "mleader"])}
        mlstyle_handles = {name: fresh_handle() for name in mlstyles}

    # Pass 2: emit. One flat list of lines, joined once.
    out: List[str] = [
        "0", "SECTION", "2", "TABLES",
        "0", "TABLE", "2", "LAYER", "70", str(len(layer_order)),
    ]
    for name in layer_order:
        out += ["0", "LAYER", "100", "AcDbSymbolTableRecord", "100", "AcDbLayerTableRecord",
                "2", name, "70", "0", "62", "7", "6", "Continuous"]
    out += ["0", "ENDTAB"]
    if emit_dimstyle_table:
        out += ["0", "TABLE", "2", "DIMSTYLE", "70", str(len(dimstyle_order))]
        for name in dimstyle_order:
            out += ["0", "DIMSTYLE", "105", fresh_handle(), "100", "AcDbSymbolTableRecord",
                    "100", "AcDbDimStyleTableRecord", "2", name, "70", "0"]
        out += ["0", "ENDTAB"]
    if textstyle_handles:
        out += ["0", "TABLE", "2", "LTYPE", "70", "1", "0", "LTYPE", "5", ml_linetype,
                "100", "AcDbSymbolTableRecord", "100", "AcDbLinetypeTableRecord",
                "2", "ByLayer", "70", "0", "3", "", "72", "65", "73", "0",
                "40", "0.0", "0", "ENDTAB"]
        out += ["0", "TABLE", "2", "STYLE", "70", str(len(textstyle_handles))]
        for name, handle in textstyle_handles.items():
            out += ["0", "STYLE", "5", handle, "100", "AcDbSymbolTableRecord",
                    "100", "AcDbTextStyleTableRecord", "2", name, "70", "0",
                    "40", "0.0", "41", "1.0", "50", "0.0", "71", "0",
                    "42", "0.2", "3", "arial.ttf", "4", ""]
        out += ["0", "ENDTAB"]
    block_records = {}
    if blocks is not None or mleaders:
        table_handle = fresh_handle()
        block_records = {name: fresh_handle() for name in ("*Model_Space", "*Paper_Space", *(blocks or {}))}
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
    handle_map = {}
    for idx, row in enumerate(kinds):
        h = row[-1]
        if h is None:
            h = format(next_handle, "X")
            next_handle += 1
        # 62 / 6 / 370 (colour / linetype / lineweight) are emitted at the end
        # of each entity's own group list — group-code ORDER within an entity
        # is not significant to a real DXF reader or to dxf_intake.parse_dxf_
        # bytes, which scans every code up to the next 0-group regardless of
        # position, so appending here avoids threading the insert point
        # through every entity kind's group-list construction below.
        props = kind_properties[idx]
        entity_offset = len(out)
        if row[0] == "poly":
            _, layer, closed, coords, bulges, _ = row
            z0 = coords[0][2]
            planar = all(c[2] == z0 for c in coords)
            if planar:
                out += ["0", "LWPOLYLINE", "5", h, "100", "AcDbEntity", "8", layer,
                        "100", "AcDbPolyline", "90", str(len(coords)),
                        "70", "1" if closed else "0", "38", _num(z0)]
                for j, (x, y, _z) in enumerate(coords):
                    out += ["10", _num(x), "20", _num(y)]
                    if bulges is not None and bulges[j] != 0:
                        out += ["42", _num(bulges[j])]
            else:
                # A polyline whose vertices differ in z is a classic 3D
                # POLYLINE (flag 8) with per-vertex z; the parser keeps each z.
                out += ["0", "POLYLINE", "5", h, "100", "AcDbEntity", "8", layer,
                        "100", "AcDb3dPolyline", "66", "1",
                        "70", str(8 | (1 if closed else 0))]
                # 3D polylines cannot carry arcs.
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
        elif row[0] == "mleader":
            out += _emit_mleader(row[1], h, block_records["*Model_Space"],
                                 mlstyle_handles, textstyle_handles, mlstyles, ml_linetype)
        elif row[0] == "dim":
            _, dimtype, layer, p1, p2, dimline, rotation, style, normal, measurement, _ = row
            tilted = normal != [0.0, 0.0, 1.0]
            # F3: groups 13/14/10 are WCS per the DXF spec (only 11/12/16 are
            # OCS), so p1/p2/dimline are written exactly as given, with no
            # arbitrary-axis transform.
            flags = 33 if dimtype == "ALIGNED" else 32
            # F4: group 11 (text middle point), OCS like 12/16; this planar
            # contract puts the text on the dimension line, so it is always
            # the canonical dimline point. Group 2 (block name) stays
            # omitted by design: this contract never synthesizes the
            # anonymous block AutoCAD normally attaches to a DIMENSION.
            text_mid = _wcs_to_ocs(dimline, normal) if tilted else tuple(dimline)
            out += ["0", "DIMENSION", "5", h, "100", "AcDbEntity", "8", layer,
                    "100", "AcDbDimension", *_point_groups(dimline), *_point_groups(text_mid, 11),
                    "70", str(flags), "3", style, "42", _num(measurement)]
            if tilted:
                out += ["210", _num(normal[0]), "220", _num(normal[1]), "230", _num(normal[2])]
            out += ["100", "AcDbAlignedDimension", *_point_groups(p1, 13), *_point_groups(p2, 14)]
            if dimtype == "LINEAR":
                out += ["50", _num(rotation), "100", "AcDbRotatedDimension"]
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
        source = kind_sources[idx]
        if source.get("space") == "paper":
            space_pairs = ["67", "1"]
            if "layout" in source:
                space_pairs += ["410", _layer_name(source["layout"], "entity layout")]
            # Place common entity metadata before its kind-specific subclass,
            # including for a POLYLINE whose following records are its vertices.
            entity_start = out.index("AcDbEntity", entity_offset)
            out[entity_start + 1:entity_start + 1] = space_pairs
        out += props
        handle_map[str(source.get("handle", "")).upper()] = h
    out += ["0", "ENDSEC"]
    if "groups" in intake or mlstyles:
        groups = intake.get("groups", [])
        if not isinstance(groups, list):
            _fail("groups must be a list")
        root, dictionary = fresh_handle(), fresh_handle()
        group_handles = [fresh_handle() for _ in groups]
        out += ["0", "SECTION", "2", "OBJECTS", "0", "DICTIONARY", "5", root,
                "330", "0", "100", "AcDbDictionary", "281", "1"]
        if mlstyles:
            ml_dictionary = fresh_handle()
            out += ["3", "ACAD_MLEADERSTYLE", "350", ml_dictionary]
        if "groups" in intake:
            out += ["3", "ACAD_GROUP", "350", dictionary,
                    "0", "DICTIONARY", "5", dictionary, "330", root,
                    "100", "AcDbDictionary", "281", "1"]
        names = set()
        for group, handle in zip(groups, group_handles):
            name = group.get("name")
            if (not isinstance(name, str) or not 1 <= len(name) <= 255
                    or name.upper() != name.upper().strip()
                    or any(c in '<>/\\\\":;?*|,=`' for c in name)
                    or any(not 0x20 <= ord(c) <= 0x7E for c in name)
                    or name.casefold() in names):
                _fail("groups require safe unique names")
            names.add(name.casefold())
            out += ["3", name, "350", handle]
        for group, handle in zip(groups, group_handles):
            out += ["0", "GROUP", "5", handle, "330", dictionary,
                    "100", "AcDbGroup", "300", "", "70", str(group.get("flags", 0)),
                    "71", str(group.get("selectable", 1))]
            for member in group["members"]:
                target = handle_map.get(str(member).upper())
                if target is None:
                    _fail(f"group member {member!r} must name an emitted entity")
                out += ["340", target]
        if mlstyles:
            out += ["0", "DICTIONARY", "5", ml_dictionary, "102", "{ACAD_REACTORS",
                    "330", root, "102", "}", "330", root,
                    "100", "AcDbDictionary", "280", "0", "281", "1"]
            for name, handle in mlstyle_handles.items():
                out += ["3", name, "350", handle]
            for name, style in mlstyles.items():
                out += _emit_mlstyle(style, mlstyle_handles[name], ml_dictionary,
                                     textstyle_handles[style["textstyle"]], ml_linetype,
                                     [row[-1] for row in kinds
                                      if row[0] == "mleader" and row[1]["style"] == name])
        out += ["0", "ENDSEC"]
    out += ["0", "EOF"]
    return ("\n".join(out) + "\n").encode("utf-8")


def _ml_pairs(pairs):
    """Expand entget point groups to file triples without changing group order."""
    out = []
    for code, value in pairs:
        if isinstance(value, (list, tuple)):
            out += _point_groups(value, code)
        else:
            out += [str(code), str(value)]
    return out


def _emit_mleader(e, handle, owner, styles, textstyles, catalogue, linetype):
    """probe6.txt MULTILEADER sequence, including the null leader-line handles."""
    gap = catalogue[e["style"]]["gap"]
    base = [_number(p + d * e["dogleg"], "mleader content base")
            for p, d in zip(e["landing"], e["dogleg_dir"])]
    textstyle = textstyles[e["textstyle"]]
    out = _ml_pairs([
        (0, "MULTILEADER"), (330, owner), (5, handle), (100, "AcDbEntity"),
        (67, 0), (410, "Model"), (8, e["layer"]), (100, "AcDbMLeader"), (270, 2),
        (300, "CONTEXT_DATA{"), (40, 1.0), (10, base), (41, e["height"]),
        (140, e["arrow"]), (145, gap), (174, 1), (175, 1), (176, 0), (177, 0),
        (290, 1), (304, e["text"]), (11, [0.0, 0.0, 1.0]), (340, textstyle),
        (12, e["textpt"]), (13, [1.0, 0.0, 0.0]), (42, 0.0), (43, 0.0),
        (44, 0.0), (45, 1.0), (170, 1), (90, -1073741824),
        (171, e["attachment"]), (172, 5), (91, -1073741824), (141, 0.0),
        (92, 0), (291, 0), (292, 0), (173, 0), (293, 0), (142, 0.0),
        (143, 0.0), (294, 0), (295, 0), (296, 0), (110, [0.0, 0.0, 0.0]),
        (111, [1.0, 0.0, 0.0]), (112, [0.0, 1.0, 0.0]), (297, 0),
        (302, "LEADER{"), (290, 1), (291, 1), (10, e["landing"]),
        (11, e["dogleg_dir"]), (90, 0), (40, e["dogleg"]), (304, "LEADER_LINE{")])
    for vertex in e["pts"][:-1]:
        out += _point_groups(vertex)
    out += _ml_pairs([
        (91, 0), (170, 1), (92, -1056964608), (340, "0"), (171, -2),
        (40, 0.0), (341, "0"), (93, 0), (305, "}"), (271, 0), (303, "}"),
        (272, 9), (273, 9), (301, "}"), (340, styles[e["style"]]), (90, 279552),
        (170, 1), (91, -1056964608), (341, linetype), (171, -2), (290, 1),
        (291, 1), (41, e["dogleg"]), (42, e["arrow"]), (172, 2), (343, textstyle),
        (173, 1), (95, 1), (174, 1), (175, 0), (92, -1056964608), (292, 0),
        (93, -1056964608), (10, [1.0, 1.0, 1.0]), (43, 0.0), (176, 0),
        (293, 0), (294, 0), (178, 0), (179, 1), (45, 1.0), (271, 0),
        (272, 9), (273, 9), (295, 0)])
    return out


def _emit_mlstyle(s, handle, owner, textstyle, linetype, reactors):
    """probe6.txt MLEADERSTYLE defaults with the reported catalogue values."""
    return _ml_pairs([
        (0, "MLEADERSTYLE"), (5, handle), (102, "{ACAD_REACTORS"), (330, owner),
        *((330, entity) for entity in reactors),
        (102, "}"), (330, owner), (100, "AcDbMLeaderStyle"), (179, 2), (170, 2),
        (171, 1), (172, 0), (90, 2), (40, 0.0), (41, 0.0), (173, s["segments"]),
        (91, -1056964608), (340, linetype), (92, -2), (290, 1), (42, s["gap"]),
        (291, 1), (43, s["dogleg"]), (3, s["name"]), (341, "0"), (44, s["arrow"]),
        (300, ""), (342, textstyle), (174, 1), (178, 1), (175, 1), (176, 0),
        (93, -1056964608), (45, s["height"]), (292, 0), (297, 0), (46, 0.18),
        (343, "0"), (94, -1056964608), (47, 1.0), (49, 1.0), (140, 1.0),
        (293, 1), (141, 0.0), (294, 1), (177, 0), (142, 1.0), (295, 0),
        (296, 0), (143, 0.125), (271, 0), (272, 9), (273, 9), (298, 0)])


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
            if "properties" in child:
                _entity_property_groups({"child": child["properties"]}, "child", where)
                row["properties"] = child["properties"]
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
    out += _entity_property_groups({handle: child.get("properties", {})}, handle, "block child")
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
