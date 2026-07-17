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


# canonical op name -> handler.
# COMPAT LOOKUP ONLY: since the dynamic tool loader landed, the authoritative
# dispatch is `server/tool_loader.run_tool_dynamic`, which runs the tool FILE a
# registry entry references. OPS/OP_ALIASES/run_op are retained purely as the
# shared primitives that `server/builtins/*.py` delegate to (byte-identical
# built-in results) — they are NEVER consulted to run an authored tool.
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
        "entry": "builtins/count_by_layer.py",
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
        "entry": "builtins/measure_area_by_layer.py",
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
        "entry": "builtins/highlight_near_edge.py",
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
    # list_layers is a family OUTSIDE the original 3-op set; it must win first so
    # "list the layer names" does not silently collapse into count_by_layer.
    ("list", "list_layers", ["list", "enumerate", "layer name", "layer names",
                             "which layers", "what layers", "all layers",
                             "distinct layer", "layer list", "names of the layer"]),
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


# A header docstring (formatted) + a REAL, self-contained run() body (never
# formatted — the bodies contain dict literals). The caller persists code to
# server/authored/<name>.py; the dynamic loader runs THAT FILE (no re-dispatch
# to a pre-coded primitive). `__LAYER_DEFAULT__` is injected verbatim.
_HEADER_TEMPLATE = '''"""Authored tool: {name}
Generated (templated, no LLM) from description: {description}
engine_op: {engine_op}  (kind: script; the FILE below IS the tool — it runs
locally with zero LLM against the extracted Intake JSON).
"""
'''

_BODY_LIST_LAYERS = """

def run(intake, params):
    params = params or {}
    prefix = params.get("prefix")
    names = set()
    for key in ("polylines", "inserts", "faces3d"):
        for ent in intake.get(key) or []:
            names.add(ent.get("layer", "0"))
    ordered = sorted(names)
    if prefix:
        ordered = [n for n in ordered if n.startswith(prefix)]
    return ({"layers": ordered, "count": len(ordered)}, None)
"""

_BODY_COUNT = """

def run(intake, params):
    params = params or {}
    layer = params.get("layer", __LAYER_DEFAULT__)
    counts = {}
    for key in ("polylines", "inserts", "faces3d"):
        for ent in intake.get(key) or []:
            lay = ent.get("layer", "0")
            if layer and lay != layer:
                continue
            counts[lay] = counts.get(lay, 0) + 1
    return ({"layer": layer, "counts": counts, "total": sum(counts.values())}, None)
"""

_BODY_MEASURE = """

def _shoelace(pts):
    n = len(pts)
    if n < 3:
        return 0.0
    a = 0.0
    for i in range(n):
        x1, y1 = pts[i][0], pts[i][1]
        x2, y2 = pts[(i + 1) % n][0], pts[(i + 1) % n][1]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


def run(intake, params):
    params = params or {}
    layer = params.get("layer", __LAYER_DEFAULT__)
    polys = [p for p in (intake.get("polylines") or []) if p.get("layer") == layer]
    areas = [_shoelace(p.get("pts", [])) for p in polys if p.get("closed")]
    total = sum(areas)
    count = len(areas)
    return ({
        "layer": layer,
        "count": count,
        "total_area": round(total, 3),
        "avg_area": round(total / count, 3) if count else 0.0,
        "units": "drawing",
    }, None)
"""

_BODY_HIGHLIGHT = """

def _centroid(pts):
    if not pts:
        return (0.0, 0.0)
    sx = sum(p[0] for p in pts)
    sy = sum(p[1] for p in pts)
    return (sx / len(pts), sy / len(pts))


def run(intake, params):
    params = params or {}
    layer = params.get("layer", __LAYER_DEFAULT__)
    polys = [p for p in (intake.get("polylines") or []) if p.get("layer") == layer]
    xs = [pt[0] for p in polys for pt in p.get("pts", [])]
    ys = [pt[1] for p in polys for pt in p.get("pts", [])]
    if not xs:
        return ({"layer": layer, "margin": 0.0, "matched": 0, "total_on_layer": 0},
                {"highlight_handles": [], "markers": []})
    minx, miny, maxx, maxy = min(xs), min(ys), max(xs), max(ys)
    span = min(maxx - minx, maxy - miny) or 1.0
    margin = float(params.get("margin", round(span * 0.05, 3)))
    handles = []
    markers = []
    for p in polys:
        cx, cy = _centroid(p.get("pts", []))
        d_edge = min(cx - minx, maxx - cx, cy - miny, maxy - cy)
        if d_edge <= margin:
            h = p.get("handle")
            if h is not None:
                handles.append(str(h))
            markers.append({"pt": [round(cx, 3), round(cy, 3)], "label": "edge"})
    result = {"layer": layer, "margin": margin, "matched": len(handles),
              "total_on_layer": len(polys)}
    overlay = {"highlight_handles": handles, "markers": markers[:500]}
    return (result, overlay)
"""

_BODIES = {
    "list_layers": _BODY_LIST_LAYERS,
    "count_by_layer": _BODY_COUNT,
    "measure_area_by_layer": _BODY_MEASURE,
    "highlight_near_edge": _BODY_HIGHLIGHT,
}


def _params_schema(engine_op: str, layer: str) -> Dict[str, Any]:
    if engine_op == "list_layers":
        return {
            "type": "object",
            "properties": {"prefix": {"type": "string",
                                      "description": "optional: only layers starting with this prefix"}},
            "required": [],
        }
    if engine_op == "highlight_near_edge":
        return {
            "type": "object",
            "properties": {"layer": {"type": "string", "default": layer},
                           "margin": {"type": "number",
                                      "description": "drawing units; default 5% of span"}},
            "required": [],
        }
    return {
        "type": "object",
        "properties": {"layer": {"type": "string", "default": layer}},
        "required": [],
    }


def author_tool(description: str) -> Tuple[dict, str, str]:
    """Parse a natural-language description into (tool package §2, generated code,
    preview). Templated (no LLM). The returned `code` is a REAL, self-contained
    run(intake, params) body — the caller persists it under server/authored/ and
    the dynamic loader runs THAT FILE, not a hardcoded primitive. Families:
    list_layers | count | measure | highlight (all read-only, deterministic)."""
    description = (description or "").strip()
    verb, engine_op = _detect_op(description)

    if engine_op == "list_layers":
        layer = ""
        name = _slugify(description) or "list-layers"
        default_params: Dict[str, Any] = {}
        body = _BODIES["list_layers"]
    else:
        layer = _detect_layer(description)
        name = _slugify(f"{verb}-{layer}")
        default_params = {"layer": layer}
        body = _BODIES[engine_op].replace("__LAYER_DEFAULT__", repr(layer))

    header = _HEADER_TEMPLATE.format(
        name=name, description=(description.replace('"', "'") or "(none)"), engine_op=engine_op)
    code = header + body

    tool = {
        "name": name,
        "version": "1.0.0",
        "description": (description or f"{verb} on layer {layer}")[:200],
        "kind": "script",
        "engine_op": engine_op,
        "params": _params_schema(engine_op, layer),
        "returns": {"type": "object"},
        "capabilities": ["drawing.read"],
        "provenance": {"author": "agent", "created": _iso_now()},
        # non-contract hint the server uses to run the tool with the right defaults
        "default_params": default_params,
    }

    if engine_op == "list_layers":
        preview = (f"Tool '{name}': lists the distinct layer names in the drawing "
                   f"(engine_op={engine_op}). Read-only; runs its own persisted file.")
    else:
        preview = (f"Tool '{name}': selects entities on layer '{layer}' and runs '{verb}' "
                   f"(engine_op={engine_op}). Read-only; runs its own persisted file.")
    return tool, code, preview
