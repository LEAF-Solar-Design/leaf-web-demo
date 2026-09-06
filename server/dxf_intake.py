"""
Minimal, honest ASCII-DXF -> intake parser (guest uploads, the DEFAULT DXF path).

Extracts ONLY what it can literally read from the user's own bytes —
LWPOLYLINE / POLYLINE entities and layer names — into the exact intake shape
`data/rooftop_demo.intake.json` established:

    {"dwg": <source name>, "layers": [...],
     "polylines": [{"layer", "closed", "pts": [[x, y, z], ...],
                    "xdata": null, "handle"}]}

HONESTY: nothing is invented. No entities -> an intake with zero polylines
(honest and renderable as such). A binary DXF raises — nothing reads it here.

This is the DEFAULT DXF extractor and the ONLY one the local (APS_LIVE=0) demo
has: it shows a REAL end-to-end guest flow on the user's own DXF without
fabricating geometry, cheaply and instantly. It is intentionally minimal
(LWPOLYLINE/POLYLINE + layers). For full fidelity (INSERT/3DFACE/geo/xdata)
a live deployment can instead route DXF to the DXF-correct APS Activity by
setting LEAF_GUEST_DXF_EXTRACT=aps (see guest_uploads.run_extraction and
da.client.EXTRACT_DXF_ACTIVITY); that path costs a paid APS run. DWG always
extracts through APS.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BINARY_SENTINEL = b"AutoCAD Binary DXF"


class DxfParseError(ValueError):
    """The file is not something this minimal parser can honestly read."""


def parse_dxf_file(path: Path, *, source_name: str = "") -> Dict[str, Any]:
    raw = Path(path).read_bytes()
    return parse_dxf_bytes(raw, source_name=source_name or Path(path).name)


def parse_dxf_bytes(raw: bytes, *, source_name: str = "upload.dxf") -> Dict[str, Any]:
    if raw.startswith(BINARY_SENTINEL):
        raise DxfParseError(
            "binary DXF is not supported; re-save this drawing as ASCII DXF "
            "or upload the DWG instead")
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception as exc:  # pragma: no cover - decode with replace cannot raise
        raise DxfParseError(f"undecodable DXF: {exc}") from exc

    pairs = _group_pairs(text)
    layers: List[str] = []
    # Membership set beside the ordered list. `layers` stays a list because
    # first-seen ORDER is part of the intake shape; the set only answers
    # "seen already?" in O(1). A plain `x not in layers` scan was quadratic in
    # the number of unique layers, and a guest can pick that number: ~36 bytes
    # per LAYER entry means ~728k layers fit inside LEAF_UPLOAD_MAX_BYTES,
    # which measured out to over an hour of pegged CPU on ONE unauthenticated
    # upload. Cheap to write, and it was the live-traffic blocker for routing
    # DXF here (see guest_uploads.run_extraction).
    seen_layers: set[str] = set()
    polylines: List[Dict[str, Any]] = []
    texts: List[Dict[str, Any]] = []
    circles: List[Dict[str, Any]] = []
    arcs: List[Dict[str, Any]] = []
    handle_seq = 0

    i = 0
    n = len(pairs)
    section: Optional[str] = None
    while i < n:
        code, value = pairs[i]
        if code == 0 and value == "SECTION" and i + 1 < n and pairs[i + 1][0] == 2:
            section = pairs[i + 1][1].upper()
            i += 2
            continue
        if code == 0 and value == "ENDSEC":
            section = None
            i += 1
            continue
        if section == "TABLES" and code == 0 and value == "LAYER":
            # the next code-2 before the next code-0 names the layer
            j = i + 1
            while j < n and pairs[j][0] != 0:
                if pairs[j][0] == 2 and pairs[j][1] not in seen_layers:
                    seen_layers.add(pairs[j][1])
                    layers.append(pairs[j][1])
                j += 1
            i = j
            continue
        if section == "ENTITIES" and code == 0 and value == "LWPOLYLINE":
            entity, i = _parse_lwpolyline(pairs, i + 1)
            handle_seq += 1
            _finish_entity(entity, handle_seq, layers, seen_layers, polylines)
            continue
        if section == "ENTITIES" and code == 0 and value == "POLYLINE":
            entity, i = _parse_polyline(pairs, i + 1)
            handle_seq += 1
            _finish_entity(entity, handle_seq, layers, seen_layers, polylines)
            continue
        if section == "ENTITIES" and code == 0 and value == "LINE":
            # A LINE is a 2-point open polyline to the viewer and to every tool: no new
            # intake field, the frozen §1 shape renders it as-is.
            entity, i = _parse_line(pairs, i + 1)
            handle_seq += 1
            _finish_entity(entity, handle_seq, layers, seen_layers, polylines)
            continue
        if section == "ENTITIES" and code == 0 and value in ("CIRCLE", "ARC"):
            # W4g-3: the kinds the browser engine draws besides lines and
            # polylines, as ADDITIVE §1 fields `circles` / `arcs` (a viewer
            # or tool that does not know them ignores them). Centre in WCS,
            # radius, the normal, and for an arc its start/end in degrees.
            entity, i = _parse_circle_or_arc(pairs, i + 1, value)
            handle_seq += 1
            if entity is not None:
                if not entity["handle"]:
                    entity["handle"] = f"L{handle_seq:X}"
                if entity["layer"] not in seen_layers:
                    seen_layers.add(entity["layer"])
                    layers.append(entity["layer"])
                (circles if value == "CIRCLE" else arcs).append(entity)
            continue
        if section == "ENTITIES" and code == 0 and value in ("TEXT", "MTEXT"):
            entity, i = _parse_text(pairs, i + 1, value)
            handle_seq += 1
            if entity["text"]:
                if not entity["handle"]:
                    entity["handle"] = f"L{handle_seq:X}"
                if entity["layer"] not in seen_layers:
                    seen_layers.add(entity["layer"])
                    layers.append(entity["layer"])
                texts.append(entity)
            continue
        i += 1

    out: Dict[str, Any] = {"dwg": source_name, "layers": layers, "polylines": polylines}
    if texts:
        # ADDITIVE §1 field (frontend ignores unknown keys): drawing labels for tools
        # that classify views by the text inside a frame.
        out["texts"] = texts
    if circles:
        out["circles"] = circles
    if arcs:
        out["arcs"] = arcs
    return out


def _ocs_to_wcs(point: List[float], normal: List[float]) -> List[float]:
    """AutoCAD's arbitrary-axis algorithm (the same one da/intake_parse.o2w
    applies to the extractor's OCS output), so a tilted circle's centre lands
    where AutoCAD puts it. A +z normal is the identity."""
    nx, ny, nz = normal
    unit = (nx * nx + ny * ny + nz * nz) ** 0.5 or 1.0
    nx, ny, nz = nx / unit, ny / unit, nz / unit
    if abs(nx) < 1 / 64.0 and abs(ny) < 1 / 64.0:
        ax = (nz, 0.0, -nx)  # (0,1,0) x n
    else:
        ax = (-ny, nx, 0.0)  # (0,0,1) x n
    length = (ax[0] ** 2 + ax[1] ** 2 + ax[2] ** 2) ** 0.5 or 1.0
    ax = (ax[0] / length, ax[1] / length, ax[2] / length)
    ay = (ny * ax[2] - nz * ax[1], nz * ax[0] - nx * ax[2], nx * ax[1] - ny * ax[0])
    x, y, z = point
    return [x * ax[0] + y * ay[0] + z * nx,
            x * ax[1] + y * ay[1] + z * ny,
            x * ax[2] + y * ay[2] + z * nz]


def _parse_circle_or_arc(pairs: List[Tuple[int, str]], i: int, kind: str):
    """CIRCLE / ARC: layer=8, handle=5, centre (10, 20, 30) in OCS, radius=40,
    normal (210, 220, 230, default +z), ARC start=50 / end=51 in DEGREES (the
    DXF file convention; entget's radians never reach a file). A radius that
    is not positive carries no geometry and is dropped, like a 1-point
    polyline."""
    layer = "0"
    handle = ""
    c = [0.0, 0.0, 0.0]
    normal = [0.0, 0.0, 1.0]
    radius = 0.0
    start = 0.0
    end = 360.0 if kind == "ARC" else 0.0
    n = len(pairs)
    while i < n and pairs[i][0] != 0:
        code, value = pairs[i]
        if code == 8:
            layer = value or "0"
        elif code == 5:
            handle = value
        elif code == 10:
            c[0] = _float(value)
        elif code == 20:
            c[1] = _float(value)
        elif code == 30:
            c[2] = _float(value)
        elif code == 40:
            radius = _float(value)
        elif code == 50:
            start = _float(value)
        elif code == 51:
            end = _float(value)
        elif code == 210:
            normal[0] = _float(value)
        elif code == 220:
            normal[1] = _float(value)
        elif code == 230:
            normal[2] = _float(value)
        i += 1
    if not radius > 0.0:
        return None, i
    centre = _ocs_to_wcs(c, normal) if normal != [0.0, 0.0, 1.0] else c
    entity: Dict[str, Any] = {"layer": layer, "c": centre, "r": radius,
                              "nrm": normal, "handle": handle}
    if kind == "ARC":
        entity["start_deg"] = start
        entity["end_deg"] = end
    return entity, i


def _finish_entity(entity: Dict[str, Any], seq: int, layers: List[str],
                   seen_layers: set, polylines: List[Dict[str, Any]]) -> None:
    if len(entity["pts"]) < 2:
        return  # a 0/1-point polyline carries no geometry worth claiming
    if not entity.get("handle"):
        entity["handle"] = f"L{seq:X}"  # synthetic-but-labeled: DXF handle absent
    if entity["layer"] not in seen_layers:
        seen_layers.add(entity["layer"])
        layers.append(entity["layer"])
    polylines.append(entity)


def _group_pairs(text: str) -> List[Tuple[int, str]]:
    """DXF is (code line, value line) pairs. Tolerates \r\n and blank tail."""
    lines = text.splitlines()
    pairs: List[Tuple[int, str]] = []
    for k in range(0, len(lines) - 1, 2):
        code_raw = lines[k].strip()
        try:
            code = int(code_raw)
        except ValueError:
            # Not a group-code line where one belongs: the pairing is broken;
            # resync by scanning forward one line. (Cheap tolerance for files
            # with a stray blank line — real writers do not produce these, but
            # a hand-edited demo file might.)
            continue
        pairs.append((code, lines[k + 1].strip()))
    if not pairs:
        raise DxfParseError("no DXF group-code pairs found")
    return pairs


def _parse_lwpolyline(pairs: List[Tuple[int, str]], i: int):
    """LWPOLYLINE: layer=8, handle=5, flags=70 (bit 1 = closed), elevation=38,
    vertices as repeated (10=x, 20=y)."""
    layer = "0"
    handle = ""
    closed = False
    elevation = 0.0
    xs: List[float] = []
    ys: List[float] = []
    n = len(pairs)
    while i < n and pairs[i][0] != 0:
        code, value = pairs[i]
        if code == 8:
            layer = value or "0"
        elif code == 5:
            handle = value
        elif code == 70:
            closed = bool(_int(value) & 1)
        elif code == 38:
            elevation = _float(value)
        elif code == 10:
            xs.append(_float(value))
        elif code == 20:
            ys.append(_float(value))
        i += 1
    pts = [[x, y, elevation] for x, y in zip(xs, ys)]
    return {"layer": layer, "closed": closed, "pts": pts,
            "xdata": None, "handle": handle}, i


def _parse_polyline(pairs: List[Tuple[int, str]], i: int):
    """Classic POLYLINE ... VERTEX* ... SEQEND: flags=70 on POLYLINE, vertices
    carry (10, 20, 30)."""
    layer = "0"
    handle = ""
    closed = False
    pts: List[List[float]] = []
    n = len(pairs)
    while i < n and pairs[i][0] != 0:
        code, value = pairs[i]
        if code == 8:
            layer = value or "0"
        elif code == 5:
            handle = value
        elif code == 70:
            closed = bool(_int(value) & 1)
        i += 1
    while i < n:
        code, value = pairs[i]
        if code == 0 and value == "VERTEX":
            x = y = z = 0.0
            i += 1
            while i < n and pairs[i][0] != 0:
                c, v = pairs[i]
                if c == 10:
                    x = _float(v)
                elif c == 20:
                    y = _float(v)
                elif c == 30:
                    z = _float(v)
                i += 1
            pts.append([x, y, z])
            continue
        if code == 0 and value == "SEQEND":
            i += 1
            while i < n and pairs[i][0] != 0:
                i += 1
            break
        break  # any other entity start ends this POLYLINE (missing SEQEND)
    return {"layer": layer, "closed": closed, "pts": pts,
            "xdata": None, "handle": handle}, i


def _parse_line(pairs: List[Tuple[int, str]], i: int):
    """LINE: layer=8, handle=5, start (10, 20, 30), end (11, 21, 31). Emitted in the
    polyline shape (closed=False, two pts) so nothing downstream learns a new type."""
    layer = "0"
    handle = ""
    a = [0.0, 0.0, 0.0]
    b = [0.0, 0.0, 0.0]
    n = len(pairs)
    while i < n and pairs[i][0] != 0:
        code, value = pairs[i]
        if code == 8:
            layer = value or "0"
        elif code == 5:
            handle = value
        elif code == 10:
            a[0] = _float(value)
        elif code == 20:
            a[1] = _float(value)
        elif code == 30:
            a[2] = _float(value)
        elif code == 11:
            b[0] = _float(value)
        elif code == 21:
            b[1] = _float(value)
        elif code == 31:
            b[2] = _float(value)
        i += 1
    return {"layer": layer, "closed": False, "pts": [a, b], "xdata": None, "handle": handle}, i


_MTEXT_FORMAT_CODES = ("\\p", "\\f", "\\F", "\\H", "\\W", "\\C", "\\c", "\\Q", "\\T", "\\A", "\\S")
_MTEXT_TOGGLE_CODES = ("\\L", "\\l", "\\O", "\\o", "\\K", "\\k")
_TEXT_MAX_CHARS = 512


def _parse_text(pairs: List[Tuple[int, str]], i: int, kind: str):
    """TEXT / MTEXT: layer=8, handle=5, insertion (10, 20), value=1 (MTEXT may continue
    in 3-codes). MTEXT inline formatting codes are stripped to plain words; the value is
    capped so a hostile file cannot inflate the intake."""
    layer = "0"
    handle = ""
    x = y = 0.0
    parts: List[str] = []
    n = len(pairs)
    while i < n and pairs[i][0] != 0:
        code, value = pairs[i]
        if code == 8:
            layer = value or "0"
        elif code == 5:
            handle = value
        elif code == 10:
            x = _float(value)
        elif code == 20:
            y = _float(value)
        elif code == 3:
            parts.append(value)
        elif code == 1:
            parts.append(value)
        i += 1
    text = "".join(parts)
    if kind == "MTEXT":
        text = _strip_mtext(text)
    text = " ".join(text.split())[:_TEXT_MAX_CHARS]
    return {"kind": kind, "layer": layer, "pt": [x, y], "text": text, "handle": handle}, i


def _strip_mtext(s: str) -> str:
    """Drop MTEXT formatting: {\\fArial|b0;...} groups keep their text, \\P is a line break."""
    out: List[str] = []
    j = 0
    L = len(s)
    while j < L:
        c = s[j]
        if c == "\\" and j + 1 < L:
            code = s[j:j + 2]
            if code in ("\\P", "\\~"):
                out.append(" ")
                j += 2
                continue
            if code in _MTEXT_TOGGLE_CODES:
                j += 2
                continue
            if code in ("\\\\", "\\{", "\\}"):
                out.append(s[j + 1])
                j += 2
                continue
            if code in _MTEXT_FORMAT_CODES:
                k = s.find(";", j)
                j = (k + 1) if k >= 0 else L
                continue
            j += 2
            continue
        if c in "{}":
            j += 1
            continue
        out.append(c)
        j += 1
    return "".join(out)


def _int(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return 0


def _float(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        return 0.0
