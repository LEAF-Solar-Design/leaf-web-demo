"""List drawing-derived module candidates and count an explicit selection.

Candidates are not design-role claims. The user selects the intended set from
the listing; the normal job records that selection with the result.
"""
from __future__ import annotations

import json
import math

MAX_EXACT_COUNT = 2**53 - 1


def _module_total(weights):
    total = sum(weights)
    if total > MAX_EXACT_COUNT:
        raise ValueError("Module count exceeds the JavaScript safe integer limit; select fewer sets or split the drawing.")
    return total


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
        bulges = entity.get("bulges", [])
        if not isinstance(bulges, (list, tuple)) or any(not math.isfinite(v) or v != 0 for v in bulges):
            return False
        thickness = entity.get("thickness", 0)
        if not math.isfinite(thickness) or thickness != 0:
            return False
        normal = entity.get("normal", [0, 0, 1])
        if len(normal) != 3 or not all(math.isfinite(v) for v in normal) or not any(normal):
            return False
        if any(len(p) < 2 or any(not math.isfinite(v) for v in p) for p in points):
            return False
        xyz = [(p[0], p[1], p[2] if len(p) > 2 else 0) for p in points]
        local3 = [tuple(p[j] - xyz[0][j] for j in range(3)) for p in xyz]
        a, b = local3[1:3]
        normal = (a[1]*b[2] - a[2]*b[1], a[2]*b[0] - a[0]*b[2], a[0]*b[1] - a[1]*b[0])
        magnitude = math.hypot(*normal)
        if not math.isfinite(magnitude) or magnitude == 0:
            return False
        scale = max(abs(v) for p in local3 for v in p)
        # Floating roundoff allowance only, relative to the local extent.
        distance = math.fsum((local3[3][j] / scale) * (normal[j] / magnitude) for j in range(3))
        if not math.isfinite(distance) or abs(distance) > 64 * math.ulp(1.0):
            return False
        drop = max(range(3), key=lambda j: abs(normal[j]))
        points = [tuple(p[j] for j in range(3) if j != drop) for p in xyz]
        if len({(p[0], p[1]) for p in points}) != 4:
            return False
        turns = []
        for i in range(4):
            a, b, c = points[i - 1], points[i], points[(i + 1) % 4]
            turn = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
            if not math.isfinite(turn) or turn == 0:
                return False
            turns.append(turn > 0)
        if len(set(turns)) != 1:
            return False
        origin_x, origin_y = points[0][:2]
        local = [(p[0] - origin_x, p[1] - origin_y) for p in points]
        area = sum(local[i][0] * local[(i + 1) % 4][1]
                   - local[(i + 1) % 4][0] * local[i][1] for i in range(4))
        return math.isfinite(area) and abs(area) > 0
    except (TypeError, ValueError, IndexError, OverflowError):
        return False


def run(intake, params):
    if not isinstance(intake, dict):
        raise ValueError("Saved intake must be an object; re-import the drawing.")
    for key in ("inserts", "polylines"):
        if key in intake and not isinstance(intake[key], list):
            raise ValueError(f"Saved intake {key} must be an array; re-import the drawing.")
        for row in intake.get(key, []):
            if not isinstance(row, dict) or (key == "polylines" and not isinstance(row.get("pts"), list)):
                raise ValueError(f"Saved intake {key} has an incompatible candidate row; re-import the drawing.")
            for field in ("layer", "name", "handle"):
                if field in row and not isinstance(row[field], str):
                    raise ValueError(f"Saved intake {key} candidate {field} must be a string; re-import the drawing.")
    groups = {}
    multiplicities = {}
    for entity_type, key in (("INSERT", "inserts"), ("LWPOLYLINE", "polylines")):
        for index, entity in enumerate(intake.get(key) or []):
            if not _model(entity) or (key == "polylines" and not _four_corners(entity)):
                continue
            layer = entity.get("layer") or "0"
            block = (entity.get("name") or "") if key == "inserts" else ""
            identity = (entity_type, layer, block)
            group = groups.setdefault(identity, {"entity_type": entity_type,
                "layer": layer, "block": block, "count": 0, "handles": [], "_keys": set()})
            handle = str(entity.get("handle") or "").strip().upper().removeprefix("0X")
            if handle and all(c in "0123456789ABCDEF" for c in handle):
                handle = handle.lstrip("0") or "0"
            entity_key = ("handle", handle) if handle else ("row", key, index)
            dimensions = (entity.get("rows", 1), entity.get("columns", 1)) if key == "inserts" else (1, 1)
            if any(type(value) is not int or not 1 <= value <= 65535 for value in dimensions):
                raise ValueError(f"INSERT {handle or index} has invalid array multiplicity; rows and columns must be positive UInt16 integers.")
            if entity_key in multiplicities and multiplicities[entity_key] != dimensions:
                raise ValueError(f"INSERT {handle or index} has conflicting array multiplicity; re-import the drawing.")
            multiplicities[entity_key] = dimensions
            if entity_key not in group["_keys"]:
                group["_keys"].add(entity_key)
                group["count"] = _module_total((group["count"], dimensions[0] * dimensions[1]))
                if handle:
                    group["handles"].append(handle)
    sets = []
    for identity, group in sorted(groups.items()):
        group["set_id"] = json.dumps(identity, ensure_ascii=True, separators=(",", ":"))
        sets.append(group)
    params = params or {}
    selected_ids = list(dict.fromkeys(params.get("set_ids") or
        ([params["set_id"]] if params.get("set_id") else [])))
    selected_sets = [s for s in sets if s["set_id"] in selected_ids]
    if len(selected_sets) != len(selected_ids):
        raise ValueError("Selected module set is absent from this drawing; list the sets again.")
    selected = selected_sets[0] if len(selected_sets) == 1 else None
    def public_set(group):
        return {k: v for k, v in group.items() if k not in ("handles", "_keys")}

    return ({"sets": [public_set(s) for s in sets],
             "selection_required": not selected_sets,
             "selected_sets": [public_set(s) for s in selected_sets],
             "selected_set": public_set(selected) if selected else None,
             "count": _module_total(multiplicities[key][0] * multiplicities[key][1]
                          for key in set().union(*(s["_keys"] for s in selected_sets))) if selected_sets else None},
            {"highlight_handles": list(dict.fromkeys(h for s in selected_sets for h in s["handles"]))}
            if selected_sets else None)
