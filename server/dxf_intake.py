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
        i += 1

    return {"dwg": source_name, "layers": layers, "polylines": polylines}


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
