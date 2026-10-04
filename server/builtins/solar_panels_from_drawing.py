"""Create graph panels from the drawing's stored, kernel-recognised outlines."""
import hashlib
import math
import re
from collections.abc import Mapping
from uuid import UUID

import solar_panel_group_kernel as kernel
from solar_design_graph import GraphValidationError, _bounded_json
from solar_sizing_client import advance, checked_graph

TOOL = "solar-panels-from-drawing"
MAX_PANELS = 5000


def input_readiness(graph):
    if graph["panels"]:
        return {"input_ready": False, "input_reason": "panels_already_present"}
    layer_filter = graph["settings"].get("panel_layer_contains")
    if not isinstance(layer_filter, str) or not layer_filter.strip():
        return {"input_ready": False, "input_reason": "panel_layer_filter_required"}
    return {"input_ready": True, "input_reason": None}


def run(graph, params, *, source_intake=None):
    _bounded_json(params)
    if (type(params) is not dict or set(params) - {"expected_rev", "cancel"}
            or type(params.get("cancel", False)) is not bool):
        raise GraphValidationError("INVALID_PANEL_IMPORT_REQUEST")
    graph = checked_graph(graph, params.get("expected_rev"))
    if params.get("cancel", False):
        return graph
    if graph["panels"]:
        raise GraphValidationError("PANELS_ALREADY_PRESENT")
    layer_filter = graph["settings"].get("panel_layer_contains")
    if not isinstance(layer_filter, str) or not layer_filter.strip():
        raise GraphValidationError("PANEL_LAYER_FILTER_REQUIRED")
    if source_intake is None:
        raise GraphValidationError("SOURCE_INTAKE_REQUIRED")
    if (not isinstance(source_intake, dict)
            or "polylines" in source_intake and not isinstance(source_intake["polylines"], list)
            or "inserts" in source_intake and not isinstance(source_intake["inserts"], list)):
        raise GraphValidationError("INVALID_SOURCE_INTAKE")
    try:
        recognised = kernel.panels_from_intake(
            source_intake, layer_contains=layer_filter,
            installation_design=graph["project"]["installation_design"])
    except kernel.PanelGroupKernelError as exc:
        code = ("AMBIGUOUS_PANEL_HANDLE" if str(exc).startswith("duplicate panel handle ")
                else "INVALID_SOURCE_INTAKE")
        raise GraphValidationError(code) from None
    if not recognised:
        raise GraphValidationError("NO_PANELS_RECOGNISED")
    if len(recognised) > MAX_PANELS:
        raise GraphValidationError("PANEL_LIMIT_EXCEEDED")

    by_handle = {}
    for kind in ("polylines", "inserts"):
        for entity in source_intake.get(kind, []):
            if isinstance(entity, Mapping) and isinstance(entity.get("handle"), str):
                by_handle.setdefault(entity["handle"].upper(), []).append((kind, entity))
    panels, seen = [], set()
    for selected in recognised:
        handle = selected["handle"]
        if not isinstance(handle, str) or re.fullmatch(r"[0-9A-Fa-f]{1,32}", handle) is None:
            raise GraphValidationError("INVALID_PANEL_HANDLE")
        matches = by_handle.get(handle.upper(), [])
        if len(matches) != 1:
            raise GraphValidationError("AMBIGUOUS_PANEL_HANDLE")
        normalized = handle.upper().lstrip("0") or "0"
        if normalized in seen:
            raise GraphValidationError("AMBIGUOUS_PANEL_HANDLE")
        seen.add(normalized)
        kind, polyline = matches[0]
        if kind == "inserts":
            try:
                polyline = kernel.panel_outline_from_insert(polyline, source_intake.get("blocks"))
            except kernel.PanelGroupKernelError:
                raise GraphValidationError("INVALID_SOURCE_INTAKE") from None
        points = [(float(q[0]), float(q[1])) for q in polyline["pts"]]
        if (len(points) >= 4 and kernel.v_close(points[0][0], points[-1][0])
                and kernel.v_close(points[0][1], points[-1][1])):
            points.pop()
        if len(points) != 4:
            raise GraphValidationError("UNSUPPORTED_PANEL_OUTLINE")
        raw = hashlib.sha256((graph["source_hash"] + ":panel:" + normalized).encode()).digest()[:16]
        panels.append({
            "id": "leaf:panel:" + str(UUID(bytes=raw, version=4)), "kind": "panel",
            "rev": graph["rev"], "extra": {}, "validity": {"state": "valid", "reasons": []},
            "provenance": {
                "created_by": TOOL, "created_at": graph["project"]["provenance"]["created_at"],
                "last_writer": TOOL, "source_rev": graph["rev"],
                "source_hash": graph["source_hash"], "source_handle": polyline["handle"],
            },
            "frame_ref": None, "matrix_cell": None,
            "centre": [sum(p[i] for p in points) / 4 for i in (0, 1)],
            "angle": math.degrees(math.atan2(points[1][1] - points[0][1],
                                            points[1][0] - points[0][0])),
            "assignment": {"string_ref": None, "seq": None},
        })
    graph["panels"].extend(panels)
    return advance(graph, panels, TOOL)
