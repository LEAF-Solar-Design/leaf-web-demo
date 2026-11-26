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
import unicodedata
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
def op_count_by_layer(intake: Dict[str, Any], params: Dict[str, Any]) -> Tuple[dict, dict | None] | dict:
    layer = params.get("layer")
    counts = _all_entities_by_layer(intake)
    if layer:
        layers = intake.get("layers") or []
        if layer not in counts and not any(
                layer == (row.get("name") if isinstance(row, dict) else row)
                for row in layers):
            from envelopes import ErrorCode, err_envelope
            name = "".join(c for c in layer if not unicodedata.category(c).startswith("C"))[:200]
            body = err_envelope(ErrorCode.BAD_PARAMS,
                f'Layer "{name}" is not in this drawing.', False, tool="count-by-layer",
                version="1.0.0")
            body["error"].update(reason_code="LAYER_NOT_FOUND",
                next_action="Choose a layer from the drawing's layer list.")
            return body
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


def run_op(engine_op: str, intake: Dict[str, Any], params: Dict[str, Any]) -> Tuple[dict, dict | None] | dict:
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


# Quoted names remain literal, even when the layer is named "active".
_QUOTED_LAYER = re.compile(
    r"""(?:\blayer\s+(?P<q>['"])(?P<after>[^'"\r\n]{1,255})(?P=q)|(?P<p>['"])(?P<before>[^'"\r\n]{1,255})(?P=p)\s+layer\b)""",
    re.IGNORECASE,
)
_RELATIVE_LAYER = re.compile(r"\b(?:active|current)\s+layer\b", re.IGNORECASE)

_PANEL_COUNT_TARGET = re.compile(
    r"\b(?:count(?:s|ing)?|tally|how many|number of)\s+(?:the\s+)?(?:nested\s+)?(?:(?:pv|solar)\s+)?(?:panels?|modules?)\b",
    re.IGNORECASE,
)

_CURRENT_LAYER_BINDING = """    layer = params.get("layer") or intake.get("active_layer")
    if not layer:
        raise ValueError("The current layer is unavailable. Supply a layer or use a drawing with a saved current layer.")
"""


def _detect_layer(desc: str) -> str:
    d = desc
    quoted = _QUOTED_LAYER.search(desc)
    if quoted:
        return quoted.group("before") or quoted.group("after")
    # "layer Panels", "layer 'Panel Groups'", "on the Panels layer"
    # Bounded capture ({1,64}), not `+?`: the space inside the class overlaps
    # the `\s+` that follows, which is polynomial backtracking territory on a
    # crafted description. 64 chars covers any real layer name; a longer one
    # falls through to the later heuristics instead of blowing the matcher up.
    m = re.search(r"layer\s+['\"]?([A-Za-z0-9 _\-]{1,64}?)['\"]?(?:\s+(?:and|to|,|\.)|$)", d, re.IGNORECASE)
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
    # Bounded: the slug becomes a filename and a registry key; an unbounded
    # description must not mint an unbounded (or >255-byte) name.
    s = s[:64].rstrip("-")
    return s or "authored-tool"


# A header docstring (formatted) + a REAL, self-contained run() body (never
# formatted — the bodies contain dict literals). The caller persists code under the
# AUTHORING TENANT's own directory, server/authored/<sha256(tenant_id)[:32]>/<name>.py
# (never a flat authored/<name>.py, which every tenant would share); the dynamic
# loader runs THAT FILE (no re-dispatch to a pre-coded primitive).
# The generator supplies the literal or runtime layer binding for each layer operation.
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
__LAYER_BINDING__
    counts = {}
    for key in ("polylines", "inserts", "faces3d"):
        for ent in intake.get(key) or []:
            lay = ent.get("layer", "0")
            if layer and lay != layer:
                continue
            counts[lay] = counts.get(lay, 0) + 1
    return ({"layer": layer, "counts": counts, "total": sum(counts.values())}, None)
"""

# Direct panel outlines on a declared panel layer, or named module INSERTs.
_BODY_COUNT_PANELS = """
import re

def is_module_name(name):
    words = " ".join(re.findall(r"[a-z0-9]+", name.casefold()))
    return re.fullmatch(r"(?:(?:pv|solar|photovoltaic) )?(?:panels?|modules?)", words) is not None


def run(intake, params):
    params = params or {}
__LAYER_BINDING__
    total = 0
    if is_module_name(layer):
        total += sum(1 for poly in intake.get("polylines", [])
                     if poly.get("layer", "0") == layer and poly.get("closed") and poly.get("pts"))
    blocks = {name.casefold(): block for name, block in (intake.get("blocks") or {}).items()}
    paths = []
    stack = [(insert, "0", [], (), 1) for insert in reversed(intake.get("inserts", []))]
    visited = 0
    while stack:
        insert, parent_layer, parent_path, ancestors, copies = stack.pop()
        visited += 1
        if visited > 100000:
            raise ValueError("Block traversal exceeds the supported instance-path limit.")
        name = insert.get("name", "").casefold()
        if not name:
            raise ValueError("Missing block name for panel count.")
        own_layer = insert.get("layer", "0")
        effective_layer = parent_layer if own_layer == "0" else own_layer
        rows, columns = insert.get("rows", 1), insert.get("columns", 1)
        if type(rows) is not int or type(columns) is not int or min(rows, columns) < 1:
            raise ValueError("Invalid module array size.")
        copies *= rows * columns
        path = parent_path + [insert.get("handle", "")]
        if is_module_name(name):
            if effective_layer.casefold() == layer.casefold():
                total += copies
                paths.append({"path": path, "multiplicity": copies})
            continue
        block = blocks.get(name)
        if block is None and not __REQUIRE_NESTED__:
            continue  # Older flat intakes omit definitions for unrelated symbols.
        if block is None or block.get("nested_inserts_incomplete"):
            raise ValueError("Incomplete block catalogue for panel count.")
        if "nested_inserts" not in block and block.get("count", 0) > len(block.get("children", [])) and __REQUIRE_NESTED__:
            raise ValueError("Incomplete block catalogue for panel count.")
        if name in ancestors:
            raise ValueError("Cyclic block reference in panel count.")
        children = block.get("nested_inserts", [])
        if any(child.get("type") == "INSERT" for child in block.get("children", [])) and not children and __REQUIRE_NESTED__:
            raise ValueError("Missing nested block reference evidence.")
        stack.extend((child, effective_layer, path, ancestors + (name,), copies)
                     for child in reversed(children))
    return ({"layer": layer, "counts": {layer: total}, "total": total, "instance_paths": paths}, None)
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
__LAYER_BINDING__
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
__LAYER_BINDING__
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


def _params_schema(engine_op: str, layer: str | None) -> Dict[str, Any]:
    if engine_op == "list_layers":
        return {
            "type": "object",
            "properties": {"prefix": {"type": "string",
                                      "description": "optional: only layers starting with this prefix"}},
            "required": [],
        }
    layer_schema = ({"type": "string", "default": layer} if layer is not None else
                    {"type": "string", "description": "Override the current drawing layer"})
    if engine_op == "highlight_near_edge":
        return {
            "type": "object",
            "properties": {"layer": layer_schema,
                           "margin": {"type": "number",
                                      "description": "drawing units; default 5% of span"}},
            "required": [],
        }
    return {
        "type": "object",
        "properties": {"layer": layer_schema},
        "required": [],
    }


def author_tool(description: str) -> Tuple[dict, str, str]:
    """Parse a natural-language description into (tool package §2, generated code,
    preview). Templated (no LLM). The returned `code` is a REAL, self-contained
    run(intake, params) body — the caller persists it under server/authored/ and
    the dynamic loader runs THAT FILE, not a hardcoded primitive. Families:
    list_layers | count | measure | highlight (all read-only, deterministic)."""
    description = (description or "").strip()
    import authored_material_selection
    import authored_action_discovery
    import authored_viewport_panels
    import authored_dynamic_visibility
    import authored_clipped_panels
    import authored_joined_spline
    import authored_recipe_retry
    import authored_action_revocation
    import authored_batch_recipe
    import authored_material_three_way
    import authored_material_patch
    import authored_material_revision
    import authored_panel_regions
    import authored_role_action
    import authored_recipe_edit
    import authored_dataset_discovery
    import authored_multibundle
    import authored_aisle_detour
    import authored_revision_panel_moves
    import authored_spatial_clearance
    import authored_beam_openings
    import authored_beam_lengths
    import authored_route_crossings

    verb, engine_op = _detect_op(description)
    drawing_body = None
    drawing_params = {"type": "object", "properties": {}, "required": []}
    if authored_revision_panel_moves.matches(description):
        verb, engine_op = "compare", "revision_panel_moves"
        drawing_body = authored_revision_panel_moves.build_body(description)
        drawing_params = authored_revision_panel_moves.PARAMS
        summary = "compares host-bound parent and current DXFs using the supplied floor base elevations"
    elif authored_recipe_retry.matches(description):
        verb, engine_op = "report", "recipe_save_recovery"
        drawing_body = authored_recipe_retry.build_body(description)
        summary = "checks the pinned successor after an unconfirmed recipe save"
    elif authored_action_revocation.matches(description):
        verb, engine_op = "select", "guarded_action_selection"
        drawing_body = authored_action_revocation.build_body(description)
        summary = "selects a pinned local action subject to its current grant"
    elif authored_joined_spline.matches(description):
        verb, engine_op = "measure", "joined_spline_beams"
        drawing_body = authored_joined_spline.build_body(description)
        summary = "measures joined spline beams and checks endpoint continuity"
    elif authored_batch_recipe.matches(description):
        verb, engine_op = "edit", "batch_recipe_edit"
        drawing_body = authored_batch_recipe.build_body(description)
        summary = "edits selected tables and reports each revision result"
    elif authored_material_patch.matches(description):
        verb, engine_op = "edit", "material_array_patch"
        drawing_body = authored_material_patch.build_body(description)
        summary = "applies ordered edits to a verified material selection"
    elif authored_material_three_way.matches(description):
        verb, engine_op = "merge", "material_three_way_merge"
        drawing_body = authored_material_three_way.build_body(description)
        summary = "previews a merge of two material revisions against their verified common ancestor"
    elif authored_material_revision.matches(description):
        verb, engine_op = "compare", "material_revision_diff"
        drawing_body = authored_material_revision.build_body(description)
        summary = "compares verified records across pinned import revisions"
    elif authored_viewport_panels.revision_comparison_requested(description):
        verb, engine_op = "compare", "viewport_panel_revision_comparison"
        drawing_body = authored_viewport_panels.build_body(description)
        drawing_params = authored_viewport_panels.parameter_schema(description)
        summary = "compares visible panel placements in two selected viewports across the verified parent revision"
    elif authored_viewport_panels.matches(description):
        if authored_viewport_panels.comparison_requested(description):
            verb, engine_op = "compare", "viewport_panel_comparison"
            summary = "compares panel placements in two selected viewports"
        else:
            verb, engine_op = "count", "viewport_layer_panels"
            summary = "counts model-space panel placements by viewport layer visibility"
        drawing_body = authored_viewport_panels.build_body(description)
        drawing_params = authored_viewport_panels.parameter_schema(description)
    elif authored_viewport_panels.camera_diagnostics_requested(description):
        verb, engine_op = "report", "viewport_camera_diagnostics"
        summary = "reports unresolved camera geometry by paper-space viewport"
        drawing_body = authored_viewport_panels.CAMERA_DIAGNOSTICS
        drawing_params = {"type": "object", "properties": {}}
    elif authored_panel_regions.matches(description):
        verb, engine_op = "measure", authored_panel_regions.operation(description)
        drawing_body = authored_panel_regions.build_body(description)
        summary = "evaluates visible panels against nested drawing boundaries"
    elif authored_role_action.matches(description):
        verb, engine_op = "compare", "role_action_comparison"
        drawing_body = authored_role_action.build_body(description)
        summary = "compares the authorized actions visible to project roles"
    elif authored_dynamic_visibility.matches(description):
        verb, engine_op = "count", "count_visible_state_panels"
        drawing_body = authored_dynamic_visibility.build_body(description)
        summary = "counts panel instances in resolved block visibility states"
    elif authored_clipped_panels.matches(description):
        verb, engine_op = "count", "count_clipped_panels"
        drawing_body = authored_clipped_panels.build_body(description)
        summary = "counts panel insertion points inside the effective clipping region"
    elif authored_material_selection.matches(description):
        verb, engine_op = "report", "material_selection_provenance"
        drawing_body = authored_material_selection.build_body(description)
        summary = "reports verified JSON pointers and digests for the checker selection"
    elif authored_action_discovery.matches(description):
        verb, engine_op = "list", "project_action_discovery"
        drawing_body = authored_action_discovery.build_body(description)
        summary = "lists authorized version-pinned actions for this project"
    elif authored_recipe_edit.matches(description):
        verb, engine_op = "edit", "inspection_recipe_edit"
        drawing_body = authored_recipe_edit.build_body(description)
        summary = "prepares the requested edit against the inspected recipe revision"
    elif authored_dataset_discovery.matches(description):
        verb, engine_op = "list", "project_dataset_discovery"
        drawing_body = authored_dataset_discovery.build_body(description)
        summary = "reports project dataset freshness from a turn-authorized catalog"
    elif authored_multibundle.matches(description):
        verb, engine_op = "route", "multibundle"
        drawing_body = authored_multibundle.build_body(description)
        summary = "assigns cable bundles on Bundle load N layers to routes on Tray cap N layers within each tray's capacity"
    elif authored_aisle_detour.matches(description):
        verb, engine_op = "route", "aisle_detour"
        drawing_body = authored_aisle_detour.build_body(description)
        summary = "finds legal cable detours along the drawn aisles"
    elif authored_spatial_clearance.matches(description):
        verb, engine_op = "check", "spatial_route_clearance"
        drawing_body = authored_spatial_clearance.build_body(description)
        summary = "checks vertical separation above and below panel footprints in drawing units"
        drawing_params = {"type": "object", "properties": {"clearance": {
            "type": "number", "exclusiveMinimum": 0,
            "description": "Required vertical separation in drawing units"}}, "required": [] if authored_spatial_clearance.measures_minimum(description) else ["clearance"]}
    elif authored_beam_openings.matches(description):
        verb, engine_op = "measure", "installed_steel_by_level"
        drawing_body = authored_beam_openings.build_body(description)
        summary = "ranks floor deductions from model-space Openings marks: OPENING <instance path or unique beam handle> <start> <end>, in world drawing units"
    elif authored_beam_lengths.matches(description):
        verb, engine_op = "measure", "beam_lengths_by_layer"
        drawing_body = authored_beam_lengths.build_body(description)
        summary = "measures model-space beam centerlines by layer, including supported block references"
    elif authored_route_crossings.is_route_crossing_request(description):
        verb, engine_op = "check", "check_route_crossings"
        drawing_body = authored_route_crossings.ROUTE_CROSSINGS_BODY
        summary = "checks cable routes against array and aisle boundaries"

    relative_layer = drawing_body is None and engine_op != "list_layers" and _QUOTED_LAYER.search(description) is None and bool(_RELATIVE_LAYER.search(description))
    if drawing_body is not None:
        layer = ""
        name = engine_op.replace("_", "-")
        default_params = {}
        body = drawing_body
    elif engine_op == "list_layers":
        layer = ""
        name = _slugify(description) or "list-layers"
        default_params: Dict[str, Any] = {}
        body = _BODIES["list_layers"]
    else:
        if relative_layer:
            layer = None
            name = _slugify(f"{verb}-current-layer")
            default_params = {}
            binding = _CURRENT_LAYER_BINDING.rstrip()
        else:
            layer = _detect_layer(description)
            name = _slugify(f"{verb}-{layer}")
            default_params = {"layer": layer}
            binding = f'    layer = params.get("layer", {layer!r})'
        body = _BODIES[engine_op]
        if engine_op == "count_by_layer" and _PANEL_COUNT_TARGET.search(description):
            requires_nested = bool(re.search(r"\b(?:nested|inside|repeated|references)\b", description, re.I))
            body = _BODY_COUNT_PANELS.replace("__REQUIRE_NESTED__", str(requires_nested))
            if relative_layer:
                name = "count-panels-current-layer"
        body = body.replace("__LAYER_BINDING__", binding)

    header = _HEADER_TEMPLATE.format(
        name=name, description=(description.replace('"', "'") or "(none)"), engine_op=engine_op)
    code = header + body

    tool = {
        "name": name,
        "version": "1.0.0",
        "description": (description or f"{verb} on layer {layer}")[:200],
        "kind": "script",
        "engine_op": engine_op,
        "params": drawing_params if drawing_body is not None else _params_schema(engine_op, layer),
        "returns": {"type": "object"},
        "capabilities": ["drawing.read"],
        "provenance": {"author": "agent", "created": _iso_now()},
        # non-contract hint the server uses to run the tool with the right defaults
        "default_params": default_params,
    }

    if drawing_body is not None:
        preview = f"Tool '{name}' {summary}."
    elif relative_layer:
        preview = f"Tool '{name}': runs '{verb}' on the drawing's current layer at execution time."
    elif engine_op == "list_layers":
        preview = (f"Tool '{name}': lists the distinct layer names in the drawing "
                   f"(engine_op={engine_op}). Read-only; runs its own persisted file.")
    else:
        preview = (f"Tool '{name}': selects entities on layer '{layer}' and runs '{verb}' "
                   f"(engine_op={engine_op}). Read-only; runs its own persisted file.")
    return tool, code, preview
