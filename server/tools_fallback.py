"""
Pure-python tool logic + tool authoring for the Leaf web demo (Lane D).

This module is the APS_LIVE=0 execution path. It computes tool results directly
from the Intake JSON (contract section 1) and returns the (result, overlay) parts
of the Result envelope (contract section 3).

Lane B owns engine/selfcheck.py. When that module is importable and exposes a
compatible entry point, app.py prefers it. This file is the always-works fallback
so the demo runs before Lane B lands, and it also backs authored tools.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

DEFAULT_LAYER = "Panels"


# --------------------------------------------------------------------------- #
# geometry helpers
# --------------------------------------------------------------------------- #
def _shoelace_area(pts: List[List[float]]) -> float:
    """Absolute polygon area (2D, ignores z) via the shoelace formula."""
    n = len(pts)
    if n < 3:
        return 0.0
    a = 0.0
    for i in range(n):
        x1, y1 = pts[i][0], pts[i][1]
        x2, y2 = pts[(i + 1) % n][0], pts[(i + 1) % n][1]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


def _centroid(pts: List[List[float]]) -> Tuple[float, float]:
    if not pts:
        return (0.0, 0.0)
    sx = sum(p[0] for p in pts)
    sy = sum(p[1] for p in pts)
    return (sx / len(pts), sy / len(pts))


def _all_entities_by_layer(intake: Dict[str, Any]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for key in ("polylines", "inserts", "faces3d"):
        for ent in intake.get(key) or []:
            layer = ent.get("layer", "0")
            counts[layer] = counts.get(layer, 0) + 1
    return counts


def _bounds(polylines: List[Dict[str, Any]]) -> Tuple[float, float, float, float]:
    xs: List[float] = []
    ys: List[float] = []
    for p in polylines:
        for pt in p.get("pts", []):
            xs.append(pt[0])
            ys.append(pt[1])
    if not xs:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs), min(ys), max(xs), max(ys))


# --------------------------------------------------------------------------- #
# tool op implementations. Each returns (result_dict, overlay_or_none).
# --------------------------------------------------------------------------- #
def op_count_by_layer(intake: Dict[str, Any], params: Dict[str, Any]) -> Tuple[dict, dict | None]:
    layer = params.get("layer")
    counts = _all_entities_by_layer(intake)
    if layer:
        counts = {layer: counts.get(layer, 0)}
    return ({"counts": counts, "total": sum(counts.values())}, None)


def op_measure_area_by_layer(intake: Dict[str, Any], params: Dict[str, Any]) -> Tuple[dict, dict | None]:
    layer = params.get("layer", DEFAULT_LAYER)
    polys = [p for p in (intake.get("polylines") or []) if p.get("layer") == layer]
    areas = [_shoelace_area(p.get("pts", [])) for p in polys if p.get("closed")]
    total = sum(areas)
    count = len(areas)
    return (
        {
            "layer": layer,
            "count": count,
            "total_area": round(total, 3),
            "avg_area": round(total / count, 3) if count else 0.0,
            "units": "drawing",
        },
        None,
    )


def op_highlight_near_edge(intake: Dict[str, Any], params: Dict[str, Any]) -> Tuple[dict, dict | None]:
    """Highlight closed polylines on a layer whose centroid is within `margin`
    drawing units of the overall bounding-box edge of that layer."""
    layer = params.get("layer", DEFAULT_LAYER)
    polys = [p for p in (intake.get("polylines") or []) if p.get("layer") == layer]
    minx, miny, maxx, maxy = _bounds(polys)
    # default margin = 5% of the smaller bbox dimension
    span = min(maxx - minx, maxy - miny) or 1.0
    margin = float(params.get("margin", round(span * 0.05, 3)))

    handles: List[str] = []
    markers: List[dict] = []
    for p in polys:
        cx, cy = _centroid(p.get("pts", []))
        d_edge = min(cx - minx, maxx - cx, cy - miny, maxy - cy)
        if d_edge <= margin:
            h = p.get("handle")
            if h is not None:
                handles.append(str(h))
            markers.append({"pt": [round(cx, 3), round(cy, 3)], "label": "edge"})

    result = {
        "layer": layer,
        "margin": margin,
        "matched": len(handles),
        "total_on_layer": len(polys),
        "bounds": {"minx": minx, "miny": miny, "maxx": maxx, "maxy": maxy},
    }
    overlay = {"highlight_handles": handles, "markers": markers[:500]}
    return (result, overlay)


# canonical op name -> handler
OPS = {
    "count_by_layer": op_count_by_layer,
    "measure_area_by_layer": op_measure_area_by_layer,
    "highlight_near_edge": op_highlight_near_edge,
}

# tolerant aliases so authored / engine op ids resolve to a handler
OP_ALIASES = {
    "count": "count_by_layer",
    "select_count": "count_by_layer",
    "measure": "measure_area_by_layer",
    "measure_panel_area": "measure_area_by_layer",
    "select_measure": "measure_area_by_layer",
    "highlight": "highlight_near_edge",
    "highlight_panels_near_edge": "highlight_near_edge",
    "select_highlight": "highlight_near_edge",
}


def resolve_op(engine_op: str) -> str | None:
    if engine_op in OPS:
        return engine_op
    return OP_ALIASES.get(engine_op)


def run_op(engine_op: str, intake: Dict[str, Any], params: Dict[str, Any]) -> Tuple[dict, dict | None]:
    canonical = resolve_op(engine_op)
    if canonical is None:
        raise KeyError(f"unknown engine_op: {engine_op!r}")
    return OPS[canonical](intake, params or {})


# --------------------------------------------------------------------------- #
# built-in registry (used when engine/registry.json is absent)
# --------------------------------------------------------------------------- #
def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


DEFAULT_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "count-by-layer",
        "version": "1.0.0",
        "description": "Counts entities per layer.",
        "kind": "script",
        "engine_op": "count_by_layer",
        "params": {
            "type": "object",
            "properties": {"layer": {"type": "string", "description": "optional: restrict to one layer"}},
            "required": [],
        },
        "returns": {"type": "object"},
        "capabilities": ["drawing.read"],
        "provenance": {"author": "user", "created": "2026-07-17T00:00:00+00:00"},
    },
    {
        "name": "measure-panel-area",
        "version": "1.0.0",
        "description": "Measures total and average closed-polyline area on a layer.",
        "kind": "script",
        "engine_op": "measure_area_by_layer",
        "params": {
            "type": "object",
            "properties": {"layer": {"type": "string", "default": DEFAULT_LAYER}},
            "required": [],
        },
        "returns": {"type": "object"},
        "capabilities": ["drawing.read"],
        "provenance": {"author": "user", "created": "2026-07-17T00:00:00+00:00"},
    },
    {
        "name": "highlight-panels-near-edge",
        "version": "1.0.0",
        "description": "Highlights panels whose centroid is within a margin of the layout edge.",
        "kind": "script",
        "engine_op": "highlight_near_edge",
        "params": {
            "type": "object",
            "properties": {
                "layer": {"type": "string", "default": DEFAULT_LAYER},
                "margin": {"type": "number", "description": "drawing units; default 5% of span"},
            },
            "required": [],
        },
        "returns": {"type": "object"},
        "capabilities": ["drawing.read"],
        "provenance": {"author": "user", "created": "2026-07-17T00:00:00+00:00"},
    },
]


# --------------------------------------------------------------------------- #
# tool authoring — constrained family:
#   "select entities on layer L and count/measure/highlight"
# --------------------------------------------------------------------------- #
_OP_KEYWORDS = [
    ("highlight", "highlight_near_edge", ["highlight", "near edge", "edge", "flag", "mark"]),
    ("measure", "measure_area_by_layer", ["measure", "area", "size", "square", "sq"]),
    ("count", "count_by_layer", ["count", "how many", "number of", "tally"]),
]


def _detect_op(desc: str) -> Tuple[str, str]:
    d = desc.lower()
    for verb, engine_op, kws in _OP_KEYWORDS:
        if any(k in d for k in kws):
            return verb, engine_op
    # default to count (safest read-only)
    return "count", "count_by_layer"


def _detect_layer(desc: str) -> str:
    d = desc
    # "layer Panels", "layer 'Panel Groups'", "on the Panels layer"
    m = re.search(r"layer\s+['\"]?([A-Za-z0-9 _\-]+?)['\"]?(?:\s+(?:and|to|,|\.)|$)", d, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r"on\s+(?:the\s+)?['\"]?([A-Za-z0-9_\-]+)['\"]?\s+layer", d, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # named-entity fallback: "panels" -> Panels
    if re.search(r"\bpanel groups?\b", d, re.IGNORECASE):
        return "Panel Groups"
    if re.search(r"\bpanels?\b", d, re.IGNORECASE):
        return "Panels"
    return DEFAULT_LAYER


def _slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s or "authored-tool"


_CODE_TEMPLATE = '''"""Authored tool: {name}
Generated from description: {description!r}
Family: select entities on layer {layer!r} and {verb}.
"""

def run(intake, params):
    layer = params.get("layer", {layer!r})
    entities = [e for e in intake.get("polylines", []) if e.get("layer") == layer]
    # engine_op = {engine_op!r}  ->  server dispatches to the {verb} primitive
    return {{"layer": layer, "selected": len(entities), "op": {engine_op!r}}}
'''


def author_tool(description: str) -> Tuple[dict, str, str]:
    """Parse a natural-language description into a tool package (contract section 2),
    generated code, and a human-readable preview. Templated (no LLM) for the
    constrained select-by-layer + count/measure/highlight family."""
    description = (description or "").strip()
    verb, engine_op = _detect_op(description)
    layer = _detect_layer(description)

    name = _slugify(f"{verb}-{layer}")
    default_params = {"layer": layer}
    if verb == "highlight":
        cap = ["drawing.read"]
    else:
        cap = ["drawing.read"]

    tool = {
        "name": name,
        "version": "1.0.0",
        "description": (description or f"{verb} entities on layer {layer}")[:200],
        "kind": "script",
        "engine_op": engine_op,
        "params": {
            "type": "object",
            "properties": {"layer": {"type": "string", "default": layer}},
            "required": [],
        },
        "returns": {"type": "object"},
        "capabilities": cap,
        "provenance": {"author": "agent", "created": _iso_now()},
        # non-contract hint the server uses to run the tool with the right default layer
        "default_params": default_params,
    }

    code = _CODE_TEMPLATE.format(
        name=name, description=description, layer=layer, verb=verb, engine_op=engine_op
    )
    preview = (
        f"Tool '{name}': selects entities on layer '{layer}' and runs '{verb}' "
        f"(engine_op={engine_op}). Read-only. Runnable now against the cached demo drawing."
    )
    return tool, code, preview
