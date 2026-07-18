"""Built-in drawing.write tool: delete-marked-panel. The file IS the tool.

MOCK write semantics (APS_LIVE=0, CONTRACT-ADDENDUM §11): a write tool's
`run(intake, params) -> (result, overlay)` does NOT itself mutate the store — it
DECLARES its edit as `result["mutations"] = {"added": [<intake entities>],
"removed": [<handles>]}`. The execution chain (server/write_loop.py) applies
those mutations to the CURRENT version's intake and persists a NEW immutable
version via da/store.put_drawing. That keeps the tool a pure function of
(intake, params) and makes the version/undo bookkeeping the chain's job.

This tool removes ONE polyline (by `handle`, default = the first polyline on
layer `layer`, default "Panels") and adds ONE marker polyline on a probe layer —
the exact shape of the proven live LeafWriteProbe Activity (delete one polyline +
add a probe polyline on LEAF_WRITE_PROBE), so the mock and live representations
stay symmetric.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

PROBE_LAYER = "LEAF_WRITE_PROBE"


def _first_handle_on_layer(intake: Dict[str, Any], layer: str) -> Optional[str]:
    for p in intake.get("polylines") or []:
        if p.get("layer") == layer and p.get("handle") is not None:
            return str(p["handle"])
    # fall back to the first polyline with a handle on ANY layer
    for p in intake.get("polylines") or []:
        if p.get("handle") is not None:
            return str(p["handle"])
    return None


def _marker_entity(near: Optional[Dict[str, Any]], deleted_handle: Optional[str]) -> Dict[str, Any]:
    """A small closed marker polyline placed near the deleted panel (or origin).

    Uses the §1 intake polyline shape so it round-trips through the frontend
    renderer exactly like any extracted entity."""
    if near and near.get("pts"):
        pts = near["pts"]
        cx = sum(pt[0] for pt in pts) / len(pts)
        cy = sum(pt[1] for pt in pts) / len(pts)
        z = pts[0][2] if len(pts[0]) > 2 else 0.0
    else:
        cx, cy, z = 0.0, 0.0, 0.0
    d = 6.0  # marker half-size (drawing units / inches)
    ring = [[cx - d, cy - d, z], [cx + d, cy - d, z],
            [cx + d, cy + d, z], [cx - d, cy + d, z]]
    handle = f"LEAFMARK-{deleted_handle or 'X'}"
    return {"layer": PROBE_LAYER, "closed": True, "pts": ring,
            "xdata": {"LEAFWRITE": [f"marks deletion of {deleted_handle}"]},
            "handle": handle}


def run(intake: Dict[str, Any], params: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    params = params or {}
    layer = params.get("layer", "Panels")
    handle = params.get("handle")
    if handle is None:
        handle = _first_handle_on_layer(intake, layer)
    else:
        handle = str(handle)

    target = None
    for p in intake.get("polylines") or []:
        if p.get("handle") is not None and str(p["handle"]) == handle:
            target = p
            break

    marker = _marker_entity(target, handle)
    removed = [handle] if handle is not None else []

    result: Dict[str, Any] = {
        "deleted_handle": handle,
        "deleted_layer": (target or {}).get("layer") if target else None,
        "added_marker_handle": marker["handle"],
        "marker_layer": PROBE_LAYER,
        # The execution chain (write_loop) consumes THIS to build the new version.
        "mutations": {"added": [marker], "removed": removed},
    }
    overlay: Dict[str, Any] = {
        "highlight_handles": [handle] if handle is not None else [],
        "markers": [{"pt": [round(marker["pts"][0][0] + 6.0, 3),
                            round(marker["pts"][0][1] + 6.0, 3)],
                     "label": "deleted"}],
    }
    return result, overlay
