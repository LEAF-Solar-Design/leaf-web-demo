"""List drawing-derived module candidates and count an explicit selection.

Candidates are not design-role claims. The user selects the intended set from
the listing; the normal job records that selection with the result.
"""
from __future__ import annotations

import json
import math


def _model(entity):
    return entity.get("space") != "paper" and entity.get("layout", "Model") in ("", "Model")


def _four_corners(entity):
    if not entity.get("closed"):
        return False
    points = entity.get("pts") or []
    if len(points) == 5 and points[0] == points[-1]:
        points = points[:-1]
    if len(points) != 4:
        return False
    try:
        if any(not math.isfinite(float(v)) for p in points for v in p[:2]):
            return False
        return abs(sum(points[i][0] * points[(i + 1) % 4][1]
                       - points[(i + 1) % 4][0] * points[i][1]
                       for i in range(4))) > 0
    except (TypeError, ValueError, IndexError):
        return False


def run(intake, params):
    groups = {}
    for entity_type, key in (("INSERT", "inserts"), ("LWPOLYLINE", "polylines")):
        for entity in intake.get(key) or []:
            if not _model(entity) or (key == "polylines" and not _four_corners(entity)):
                continue
            layer = entity.get("layer") or "0"
            block = (entity.get("name") or "") if key == "inserts" else ""
            identity = (entity_type, layer, block)
            group = groups.setdefault(identity, {"entity_type": entity_type,
                "layer": layer, "block": block, "count": 0, "handles": []})
            group["count"] += 1
            if entity.get("handle"):
                group["handles"].append(str(entity["handle"]))
    sets = []
    for identity, group in sorted(groups.items()):
        group["set_id"] = json.dumps(identity, ensure_ascii=True, separators=(",", ":"))
        sets.append(group)
    selected_id = (params or {}).get("set_id")
    selected = next((s for s in sets if s["set_id"] == selected_id), None)
    if selected_id and selected is None:
        raise ValueError("Selected module set is absent from this drawing; list the sets again.")
    return ({"sets": [{k: v for k, v in s.items() if k != "handles"} for s in sets],
             "selection_required": selected is None,
             "selected_set": {k: v for k, v in selected.items() if k != "handles"} if selected else None,
             "count": selected["count"] if selected else None},
            {"highlight_handles": selected["handles"]} if selected else None)
