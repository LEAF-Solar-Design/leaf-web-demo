"""da/intake_parse.py — families-text -> Intake JSON (§1) parser.

Copied verbatim (o2w / _cross / parse) from the proven extractor
  C:/Users/ehaug/OneDrive/Documents/GitHub/utility-estimation/extracts/dwg_intake.py
so the DA Activity's LISP output is parsed IDENTICALLY to the local extractor.
The Activity runs the same LISP block (see da/lisp.py) headless on APS and
emits the same "families text"; this module turns that text into Intake JSON.
"""
from __future__ import annotations

import math
import sys


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def o2w(p, n):
    """OCS point -> WCS via the arbitrary-axis algorithm (pure python)."""
    nx, ny, nz = n
    if abs(nx) < 1 / 64.0 and abs(ny) < 1 / 64.0:
        ax = _cross((0.0, 1.0, 0.0), n)
    else:
        ax = _cross((0.0, 0.0, 1.0), n)
    al = (ax[0] ** 2 + ax[1] ** 2 + ax[2] ** 2) ** 0.5
    ax = (ax[0] / al, ax[1] / al, ax[2] / al)
    ay = _cross(n, ax)
    return (p[0] * ax[0] + p[1] * ay[0] + p[2] * n[0],
            p[0] * ax[1] + p[1] * ay[1] + p[2] * n[1],
            p[0] * ax[2] + p[1] * ay[2] + p[2] * n[2])


def parse(families_txt, dwg):
    """Parse a families text FILE PATH into Intake JSON (§1)."""
    out = {"dwg": dwg, "layers": [], "polylines": [], "inserts": [],
           "faces3d": [], "blockdefs": {}, "geodata": [], "images": [],
           "imageNames": []}
    cur_bd = None
    cur_pl = None

    def close_pl():
        nonlocal cur_pl
        if cur_pl is not None and len(cur_pl["pts"]) >= 2:
            out["polylines"].append(cur_pl)
        cur_pl = None

    with open(families_txt, errors="replace") as f:
        lines = f.readlines()
    out = _parse_lines(lines, out, close_pl, cur_bd, cur_pl)
    return out


def parse_text(families_text, dwg):
    """Parse families text CONTENT (str) into Intake JSON (§1).

    Used by the DA client after downloading the WorkItem's result file.
    """
    out = {"dwg": dwg, "layers": [], "polylines": [], "inserts": [],
           "faces3d": [], "blockdefs": {}, "geodata": [], "images": [],
           "imageNames": []}
    cur_bd = None
    cur_pl = None

    def close_pl():
        nonlocal cur_pl
        if cur_pl is not None and len(cur_pl["pts"]) >= 2:
            out["polylines"].append(cur_pl)
        cur_pl = None

    return _parse_lines(families_text.splitlines(), out, close_pl, cur_bd, cur_pl)


_MTEXT_FORMAT_CODES = ("\\P", "\\p", "\\f", "\\F", "\\H", "\\W", "\\C", "\\c", "\\Q", "\\T", "\\A", "\\L", "\\l", "\\O", "\\o", "\\K", "\\k", "\\S")


def _strip_mtext(s):
    """Drop MTEXT inline formatting ({\\fArial|b0;...} groups keep their text; \\P is a break).
    Mirrors server/dxf_intake._strip_mtext so both extractors emit the same label text."""
    out = []
    j = 0
    L = len(s)
    while j < L:
        c = s[j]
        if c == "\\" and j + 1 < L:
            code = s[j:j + 2]
            if code == "\\P":
                out.append(" ")
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


def _block_point(value, places=3, width=3):
    point = [round(float(v), places) for v in value.split(",")]
    if len(point) != width or not all(math.isfinite(v) for v in point):
        raise ValueError("malformed block point")
    return point


def _block_name(value):
    # Decode only the catalogue framing escapes, with percent LAST so an
    # original literal "%7C" (encoded as "%257C") stays a literal "%7C".
    return value.replace("%7C", "|").replace("%0D", "\r").replace("%0A", "\n").replace("%25", "%")


def _block_child(kind, body, layer):
    child = {"kind": kind, "layer": layer}
    if kind == "LINE":
        a, b = body
        child["pts"] = [_block_point(a), _block_point(b)]
    elif kind == "LWPOLYLINE":
        closed, normal, elevation, points = body
        child.update(closed=bool(int(closed) & 1), nrm=_block_point(normal, 6),
                     elev=round(float(elevation), 3),
                     pts=[_block_point(p, width=2) for p in points.split(";") if p])
        if len(child["pts"]) < 2:
            raise ValueError("LWPOLYLINE needs at least two points")
    elif kind in ("CIRCLE", "ARC"):
        expected = 5 if kind == "ARC" else 3
        if len(body) != expected:
            raise ValueError(f"{kind} body must have {expected} fields")
        child.update(c=_block_point(body[0]), r=round(float(body[1]), 3),
                     nrm=_block_point(body[-1], 6))
        if not math.isfinite(child["r"]) or child["r"] <= 0:
            raise ValueError(f"{kind} radius must be positive")
        if kind == "ARC":
            child.update(start_deg=round(float(body[2]), 6),
                         end_deg=round(float(body[3]), 6))
    elif kind == "TEXT":
        point, height, rotation, value = body
        child.update(pt=_block_point(point), height=round(float(height), 3),
                     rot=round(float(rotation), 6), text=" ".join(value.split())[:512])
    elif kind == "OTHER":
        child["type"], = body
    else:
        raise ValueError("unknown block child kind")
    if "nrm" in child and not any(child["nrm"]):
        raise ValueError("block child normal must not be the zero vector")
    return child


def _parse_lines(lines, out, close_pl, cur_bd, cur_pl):
    # cur_bd / cur_pl are closed over via the enclosing scope trick; re-bind locally
    state = {"bd": cur_bd, "pl": cur_pl}

    def close():
        if state["pl"] is not None and len(state["pl"]["pts"]) >= 2:
            out["polylines"].append(state["pl"])
        state["pl"] = None

    for line in lines:
        line = line.rstrip("\r\n")
        if not line or "|" not in line:
            continue
        tag, rest = line.split("|", 1)
        if tag not in ("PV", "PX", "PXS"):
            close()
        try:
            if tag == "LAYER":
                out["layers"].append(rest)
            elif tag == "PL":
                layn, cl, el, nrm, hnd = rest.split("|")
                state["pl"] = {"layer": layn, "closed": bool(int(cl) & 1),
                               "_el": float(el),
                               "_nrm": tuple(float(v) for v in nrm.split(",")),
                               "pts": [], "xdata": [], "handle": hnd}
            elif tag == "PV" and state["pl"] is not None:
                x, y = (float(v) for v in rest.split(","))
                w = o2w((x, y, state["pl"]["_el"]), state["pl"]["_nrm"])
                state["pl"]["pts"].append([round(v, 3) for v in w])
            elif tag == "PX" and state["pl"] is not None:
                state["pl"]["xdata"].append({"app": rest, "strings": []})
            elif tag == "PXS" and state["pl"] is not None and state["pl"]["xdata"]:
                state["pl"]["xdata"][-1]["strings"].append(rest)
            elif tag == "LN":
                # LINE as a 2-point open polyline: the frozen §1 shape, no new field.
                layn, p1, p2, hnd = rest.split("|")
                a = [round(float(v), 3) for v in p1.split(",")]
                b = [round(float(v), 3) for v in p2.split(",")]
                out["polylines"].append({"layer": layn, "closed": False, "pts": [a, b],
                                         "xdata": None, "handle": hnd})
            elif tag == "CI":
                # W4g-3: CIRCLE with its handle (ADDITIVE §1 field `circles`);
                # the centre comes in OCS with the normal, like PL vertices.
                layn, cp, r, nrm, hnd = rest.split("|")
                n = tuple(float(v) for v in nrm.split(","))
                w = o2w(tuple(float(v) for v in cp.split(",")), n)
                out.setdefault("circles", []).append({
                    "layer": layn, "c": [round(v, 3) for v in w], "r": round(float(r), 3),
                    "nrm": [round(v, 6) for v in n], "handle": hnd})
            elif tag == "AR":
                # W4g-3: ARC (ADDITIVE §1 field `arcs`); angles arrive in degrees.
                layn, cp, r, a1, a2, nrm, hnd = rest.split("|")
                n = tuple(float(v) for v in nrm.split(","))
                w = o2w(tuple(float(v) for v in cp.split(",")), n)
                out.setdefault("arcs", []).append({
                    "layer": layn, "c": [round(v, 3) for v in w], "r": round(float(r), 3),
                    "start_deg": round(float(a1), 6), "end_deg": round(float(a2), 6),
                    "nrm": [round(v, 6) for v in n], "handle": hnd})
            elif tag == "EP":
                hnd, aci, rgb, linetype, lineweight = rest.split("|")
                color = None if rgb == "~" else [int(v) for v in rgb.split(",")]
                if (not hnd or any(v not in "0123456789abcdefABCDEF" for v in hnd)
                        or not linetype or (color is not None and (
                            len(color) != 3 or any(v < 0 or v > 255 for v in color)))):
                    raise ValueError("malformed entity properties")
                properties = {"aci": int(aci), "rgb": color,
                              "linetype": linetype, "lineweight": int(lineweight)}
                out.setdefault("properties", {})[hnd] = properties
            elif tag == "DM":
                kind, p1, p2, dimline, rotation, style, nrm, measurement, hnd = rest.split("|")
                points = [[round(float(v), 3) for v in p.split(",")]
                          for p in (p1, p2, dimline)]
                n = tuple(float(v) for v in nrm.split(","))
                if any(len(p) != 3 for p in points) or len(n) != 3:
                    raise ValueError("malformed dimension point or normal")
                o2w((0.0, 0.0, 0.0), n)
                if not hnd or any(v not in "0123456789abcdefABCDEF" for v in hnd):
                    raise ValueError("malformed dimension handle")
                normal = [round(v, 6) for v in n]
                if not any(normal):
                    raise ValueError("dimension normal rounds to the zero vector")
                dimension = {
                    "type": kind, "p1": points[0], "p2": points[1], "dimline": points[2],
                    "rotation_deg": round(float(rotation), 6), "style": style,
                    "nrm": normal, "measurement": round(float(measurement), 3), "handle": hnd}
                out.setdefault("dimensions", []).append(dimension)
            elif tag == "TX":
                # TEXT/MTEXT label (ADDITIVE §1 field `texts`; the value had "|" replaced
                # by a space in the LISP and is capped at 512 chars there).
                et, layn, ip, hnd, tx = rest.split("|", 4)
                x, y = (float(v) for v in ip.split(","))
                tx = " ".join(_strip_mtext(tx).split()) if et == "MTEXT" else " ".join(tx.split())
                if tx:
                    out.setdefault("texts", []).append({"kind": et, "layer": layn,
                                                        "pt": [round(x, 3), round(y, 3)],
                                                        "text": tx, "handle": hnd})
            elif tag == "IN":
                nm, layn, ip, rot, nrm, scl, hnd = rest.split("|")
                n = tuple(float(v) for v in nrm.split(","))
                p = tuple(float(v) for v in ip.split(","))
                w = o2w(p, n)
                out["inserts"].append({
                    "name": nm, "layer": layn,
                    "x": round(w[0], 3), "y": round(w[1], 3), "z": round(w[2], 3),
                    "rot": float(rot), "nrm": [round(v, 6) for v in n],
                    "scale": [float(v) for v in scl.split(",")], "handle": hnd})
            elif tag == "F3":
                parts = rest.split("|")
                layn = parts[0]
                cs = [[float(v) for v in p.split(",")] for p in parts[1:5]]
                out["faces3d"].append({"layer": layn, "pts": cs})
            elif tag == "BD":
                state["bd"] = rest
                out["blockdefs"][state["bd"]] = []
            elif tag == "BDE" and state["bd"]:
                et, pts = rest.split("|", 1)
                out["blockdefs"][state["bd"]].append({
                    "type": et,
                    "pts": [[float(v) for v in p.split(",")] for p in pts.split(";") if p]})
            elif tag == "BK":
                name, base, count, complete = rest.split("|")
                name = _block_name(name)
                count = int(count)
                if count < 0 or complete not in ("0", "1"):
                    raise ValueError("malformed block count or completeness")
                out.setdefault("blocks", {})[name] = {
                    "base": _block_point(base), "count": count,
                    "complete": complete == "1" and count <= 60, "children": []}
            elif tag == "BKE":
                fields = rest.split("|")
                name = _block_name(fields[0])
                block = out.setdefault("blocks", {}).setdefault(name, {
                    "base": [0.0, 0.0, 0.0], "count": 0,
                    "complete": False, "children": []})
                try:
                    kind, *body, layer = fields[1:]
                    child = _block_child(kind, body, _block_name(layer))
                except Exception:
                    block["complete"] = False
                    raise
                if kind == "OTHER":
                    block["complete"] = False
                if len(block["children"]) < 60:
                    block["children"].append(child)
                else:
                    block["complete"] = False
            elif tag == "BKCAP":
                out["blocksCapped"] = int(rest)
            elif tag == "GEO":
                out["geodata"].append(rest)
            elif tag == "IMG":
                out["images"].append(rest)
            elif tag == "IMGNAME":
                out["imageNames"].append(rest)
        except Exception as e:
            out.setdefault("parseErrors", []).append(f"{tag}: {e}")
    close()
    for block in out.get("blocks", {}).values():
        if block["count"] <= 60 and len(block["children"]) < block["count"]:
            block["complete"] = False
    # strip parser-internal fields
    for pl in out["polylines"]:
        pl.pop("_el", None)
        pl.pop("_nrm", None)
        if not pl["xdata"]:
            pl["xdata"] = None
    if not out["geodata"]:
        out["geodata"] = ["none"]
    if out.get("parseErrors"):
        print(f"WARN: intake_parse dropped {len(out['parseErrors'])} lines", file=sys.stderr)
    return out
