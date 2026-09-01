"""Built-in: timber-cutlist-preflight. The file IS the tool. Local, zero APS cost.

Shows the user what the timber-cutlist AppBundle will read BEFORE the paid run: which
layers follow ``Material_W x H`` (counted, coloured by material) and which do not (grey),
the view frames it detects (closed polylines with a classifiable label inside) and the
segment counts per layer and per view. Mirrors CutLists.Core (LayerSpec.TryParse,
ViewDetector.Classify/Detect, MaterialPalette) and is cross-checked against the engine
on the six-views fixture in tests/test_timber_cutlist_preflight.py.

Reads the intake (§1) only: `polylines` (LINE arrives as a 2-point open polyline) and
the additive `texts` array. Bounded: overlay polylines are capped; a hostile intake with
a million tiny polylines yields a truncated overlay and a warning, never a hang.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

_LAYER_RX = re.compile(r"^\s*(?P<mat>.+?)\s*[_\s]\s*(?P<w>\d+(?:[.,]\d+)?)\s*[xX×]\s*(?P<h>\d+(?:[.,]\d+)?)\s*$")
# Same order as CutLists.Core.Extract.ViewDetector.Keywords (first match wins).
_KEYWORDS: List[Tuple[str, str]] = [
    ("ROOF PLAN", "RoofPlan"), ("TAGPLAN", "RoofPlan"),
    ("NORTH", "ElevationNorth"), ("NORD", "ElevationNorth"),
    ("EAST", "ElevationEast"), ("ØST", "ElevationEast"), ("OST", "ElevationEast"),
    ("SOUTH", "ElevationSouth"), ("SYD", "ElevationSouth"),
    ("WEST", "ElevationWest"), ("VEST", "ElevationWest"),
    ("SECTION", "Section"), ("SNIT", "Section"),
    ("PLAN", "Plan"), ("GRUNDPLAN", "Plan"), ("PLANTEGNING", "Plan"),
]
_MAX_OVERLAY_POLYLINES = 20_000
_MAX_LAYER_NAME = 200


def parse_layer(name: str) -> Optional[Dict[str, Any]]:
    """Material_W x H -> {material, w, h, key}; None when the layer is not rule-coded."""
    if not name or len(name) > _MAX_LAYER_NAME:
        return None
    m = _LAYER_RX.match(name)
    if not m:
        return None
    try:
        w = float(m.group("w").replace(",", "."))
        h = float(m.group("h").replace(",", "."))
    except ValueError:
        return None
    if w <= 0 or h <= 0:
        return None
    mat = m.group("mat").strip().rstrip("_ ")
    if not mat:
        return None

    def fmt(v: float) -> str:
        return str(int(v)) if v == int(v) else (f"{v:.2f}").rstrip("0").rstrip(".")

    return {"material": mat, "w": w, "h": h, "key": f"{mat.upper()}_{fmt(w)}X{fmt(h)}"}


def classify(label: str) -> str:
    up = (label or "").upper()
    for word, kind in _KEYWORDS:
        if word in up:
            return kind
    return "Unknown"


def material_hex(material: Optional[str]) -> str:
    """Mirror of CutLists.Core.Report.MaterialPalette."""
    if material is None:
        return "#a0a0a0"
    m = material.upper()
    table = [("RAFTER", "#a03c14"), ("JOIST", "#c8781e"), ("BEAM", "#782828"), ("CLADDING", "#1e5aa0"),
             ("RIDGE", "#5a145a"), ("PLATE", "#146e5a"), ("WOOD", "#8b5a2b")]
    for kw, hx in table:
        if kw in m:
            return hx
    return "#006400"


def _bbox(pts):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _area(pts) -> float:
    a = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i][0], pts[i][1]
        x2, y2 = pts[(i + 1) % n][0], pts[(i + 1) % n][1]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


def _contains(pts, bbox, x: float, y: float) -> bool:
    if x < bbox[0] or x > bbox[2] or y < bbox[1] or y > bbox[3]:
        return False
    inside = False
    n = len(pts)
    j = n - 1
    for i in range(n):
        xi, yi = pts[i][0], pts[i][1]
        xj, yj = pts[j][0], pts[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def run(intake: Dict[str, Any], params: Dict[str, Any]):
    polylines = [p for p in (intake.get("polylines") or []) if isinstance(p, dict) and len(p.get("pts") or []) >= 2]
    texts = [t for t in (intake.get("texts") or []) if isinstance(t, dict) and t.get("text")]
    warnings: List[str] = []

    # 1) frames: per classifiable label, the smallest closed polyline (>=4 pts) containing it
    closed = []
    for p in polylines:
        pts = p["pts"]
        if p.get("closed") and len(pts) >= 4:
            closed.append((p, _bbox(pts), _area(pts)))
    views = []
    used = set()
    for t in texts:
        kind = classify(t["text"])
        if kind == "Unknown":
            continue
        x, y = float(t["pt"][0]), float(t["pt"][1])
        best = None
        for (p, bb, area) in closed:
            if id(p) in used or not _contains(p["pts"], bb, x, y):
                continue
            if best is None or area < best[2]:
                best = (p, bb, area)
        if best is None:
            continue
        used.add(id(best[0]))
        views.append({"kind": kind, "label": t["text"], "handle": best[0].get("handle"), "bbox": best[1], "segments": 0})
    order = {"Plan": 0, "RoofPlan": 1, "ElevationNorth": 2, "ElevationEast": 3, "ElevationSouth": 4, "ElevationWest": 5, "Section": 6}
    views.sort(key=lambda v: (order.get(v["kind"], 9), -(v["bbox"][2] - v["bbox"][0]) * (v["bbox"][3] - v["bbox"][1])))
    frame_handles = {v["handle"] for v in views if v.get("handle")}

    # 2) segments per layer, assigned to the frame containing their midpoint
    layers: Dict[str, Dict[str, Any]] = {}
    overlay_lines: List[Dict[str, Any]] = []
    truncated = False
    for p in polylines:
        if p.get("handle") and p["handle"] in frame_handles:
            continue
        name = str(p.get("layer") or "0")
        rec = layers.get(name)
        if rec is None:
            rec = layers[name] = {"spec": parse_layer(name), "segments": 0}
        pts = p["pts"]
        n = len(pts)
        edges = n if p.get("closed") and n >= 3 else n - 1
        for i in range(edges):
            a, b = pts[i], pts[(i + 1) % n]
            rec["segments"] += 1
            mx, my = (a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0
            for v in views:
                bb = v["bbox"]
                if bb[0] <= mx <= bb[2] and bb[1] <= my <= bb[3]:
                    v["segments"] += 1
                    break
            if rec["spec"] is not None:
                if len(overlay_lines) < _MAX_OVERLAY_POLYLINES:
                    overlay_lines.append({"pts": [[a[0], a[1]], [b[0], b[1]]],
                                          "color": material_hex(rec["spec"]["material"])})
                else:
                    truncated = True

    rows = []
    counted_segments = 0
    for name, rec in sorted(layers.items(), key=lambda kv: (kv[1]["spec"] is None, -kv[1]["segments"], kv[0])):
        spec = rec["spec"]
        rows.append([name, spec["key"] if spec else "not Material_W x H", rec["segments"], "yes" if spec else "no"])
        if spec:
            counted_segments += rec["segments"]
        else:
            warnings.append(f"layer '{name}' does not follow Material_W x H: {rec['segments']} segment(s) not counted")
    if counted_segments == 0:
        warnings.append("no rule-coded layers found; the cut list would be empty")
    if not views:
        warnings.append("no labelled view frames found; the whole drawing is treated as one view")
    if truncated:
        warnings.append(f"overlay truncated at {_MAX_OVERLAY_POLYLINES} segments")

    markers = [{"pt": [(v["bbox"][0] + v["bbox"][2]) / 2.0, v["bbox"][3]], "label": f"{v['label']} ({v['kind']})"} for v in views]
    frame_lines = []
    for v in views:
        x0, y0, x1, y1 = v["bbox"]
        frame_lines.append({"pts": [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]], "color": "#3c3c3c"})
    result = {
        "table": {"columns": ["Layer", "Read as", "Segments", "Counted"], "rows": rows},
        "views": [{"kind": v["kind"], "label": v["label"], "segments": v["segments"]} for v in views],
        "counted_segments": counted_segments,
        "view_count": len(views),
        "warnings": warnings,
        "next": "Run timber-cutlist to generate the cut list (CSV and PDF) from these layers.",
    }
    overlay = {"markers": markers, "polylines": frame_lines + overlay_lines,
               "highlight_handles": [v["handle"] for v in views if v.get("handle")]}
    return result, overlay
