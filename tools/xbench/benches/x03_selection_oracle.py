"""Compare revision-bound selections using hexadecimal DXF handles."""
import re


def handle_set(items):
    if not isinstance(items, list) or not all(
            isinstance(h, str) and re.fullmatch(r"[0-9a-fA-F]{1,16}", h)
            and int(h, 16) for h in items):
        return None
    return {int(h, 16) for h in items}


def selection_quality(saved, reverted, exports, added, removed, *, inventory=None, requested_scope=None):
    if not isinstance(exports, (list, tuple)) or len(exports) != 2:
        return "INCOMPLETE"
    rows = [saved, reverted, *exports]
    for row in rows:
        if not isinstance(row, dict) or not all(isinstance(row.get(k), str) and row[k]
                for k in ("drawing", "version", "rule_revision")):
            return "INCOMPLETE"
    sets = [handle_set(row.get("handles")) for row in rows]
    additions, removals = handle_set(added), handle_set(removed)
    if None in sets or additions is None or removals is None:
        return "FAIL"
    if len({(r["drawing"], r["version"]) for r in rows}) != 1:
        return "FAIL"
    if any(a["rule_revision"] != b["rule_revision"] for a, b in zip(rows[:2], exports)):
        return "FAIL"
    if not isinstance(inventory, dict) or not inventory.get("drawing") or not inventory.get("version"):
        return "INCOMPLETE"
    if (inventory["drawing"], inventory["version"]) != (saved["drawing"], saved["version"]):
        return "FAIL"
    live = handle_set(inventory.get("live_handles"))
    if live is None:
        return "INCOMPLETE"
    deleted = handle_set(inventory.get("deleted_handles", []))
    if deleted is None:
        return "INCOMPLETE"
    if live & deleted:
        return "FAIL"
    if any(not handles.issubset(live) for handles in sets):
        return "FAIL"
    before, after = sets[:2]
    if requested_scope not in ("selected-only", "visible-only", "full-drawing"):
        return "INCOMPLETE"
    if any(not row.get("scope") for row in exports):
        return "INCOMPLETE"
    if any(row["scope"] != requested_scope for row in exports):
        return "FAIL"
    expected = sets[:2]
    if requested_scope == "full-drawing":
        expected = [live, live]
    elif requested_scope == "visible-only":
        entities, layers = inventory.get("entities"), inventory.get("layers")
        if not isinstance(entities, list) or not isinstance(layers, dict):
            return "INCOMPLETE"
        visible, seen = set(), set()
        for entity in entities:
            if not isinstance(entity, dict) or type(entity.get("visible")) is not bool:
                return "INCOMPLETE"
            handles = handle_set([entity.get("handle")])
            layer_name = entity.get("layer")
            layer = layers.get(layer_name) if isinstance(layer_name, str) else None
            if handles is None or not isinstance(layer, dict) or any(
                    type(layer.get(k)) is not bool for k in ("on", "frozen")):
                return "INCOMPLETE"
            if seen & handles or not handles <= live:
                return "FAIL"
            seen.update(handles)
            if entity["visible"] and layer["on"] and not layer["frozen"]:
                visible.update(handles)
        if seen != live:
            return "INCOMPLETE"
        if "visible_handles" in inventory and handle_set(inventory["visible_handles"]) != visible:
            return "FAIL"
        expected = [visible, visible]
    return "PASS" if (expected == sets[2:] and after-before == additions
                      and before-after == removals) else "FAIL"


def snapshot_roundtrip_quality(saved, exported, expected_handles):
    """Compare a revision's exported entity snapshots keyed by normalized handle."""
    import json
    expected = handle_set(expected_handles)
    if expected is None or not all(isinstance(s, dict) for s in (saved, exported)):
        return "INCOMPLETE"
    for key in ("drawing", "version"):
        if not all(isinstance(s.get(key), str) and s[key] for s in (saved, exported)):
            return "INCOMPLETE"
        if saved[key] != exported[key]:
            return "FAIL"
    snapshots = []
    for record in (saved, exported):
        if not isinstance(record.get("entities"), list):
            return "INCOMPLETE"
        entities = {}
        for entity in record["entities"]:
            if not isinstance(entity, dict):
                return "INCOMPLETE"
            handles = handle_set([entity.get("handle")])
            if (not handles or not isinstance(entity.get("geometry"), dict) or not entity["geometry"]
                    or not isinstance(entity.get("layer"), str) or not entity["layer"]
                    or not isinstance(entity.get("attributes"), dict)):
                return "INCOMPLETE"
            handle = next(iter(handles))
            if handle in entities or handle not in expected:
                return "FAIL"
            try:
                entities[handle] = json.dumps([entity[k] for k in ("geometry", "layer", "attributes")],
                                              sort_keys=True, allow_nan=False)
            except (ValueError, TypeError):
                return "INCOMPLETE"
        if entities.keys() != expected:
            return "INCOMPLETE"
        snapshots.append(entities)
    return "PASS" if snapshots[0] == snapshots[1] else "FAIL"


def framed_snapshot_quality(saved, exported, expected_handles, transforms):
    """Resolve planar points and ordered paths with declared local-to-world affine matrices."""
    from fractions import Fraction
    from benches.x03_quality import finite_measure
    if not isinstance(transforms, dict):
        return "INCOMPLETE"
    normalized = []
    for snapshot in (saved, exported):
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("frame"), str):
            return "INCOMPLETE"
        matrix = transforms.get(snapshot["frame"])
        if not isinstance(matrix, (tuple, list)) or len(matrix) != 6 or None in matrix:
            return "INCOMPLETE"
        if not all(map(finite_measure, matrix)):
            return "FAIL"
        a, b, c, d, tx, ty = (Fraction(str(v)) for v in matrix)
        if a*d == b*c:
            return "FAIL"
        entities = []
        if not isinstance(snapshot.get("entities"), list):
            return "INCOMPLETE"
        for entity in snapshot["entities"]:
            geom = entity.get("geometry") if isinstance(entity, dict) else None
            if not isinstance(geom, dict) or set(geom) not in ({"point"}, {"vertices"}):
                return "INCOMPLETE"
            kind, coords = next(iter(geom.items()))
            points = [coords] if kind == "point" else coords
            if not isinstance(points, (list, tuple)) or not points:
                return "INCOMPLETE"
            world = []
            for point in points:
                if not isinstance(point, (list, tuple)) or len(point) != 2 or not all(map(finite_measure, point)):
                    return "INCOMPLETE"
                x, y = (Fraction(str(v)) for v in point)
                world.append([str(a*x + b*y + tx), str(c*x + d*y + ty)])
            entities.append(dict(entity, geometry={kind: world}))
        normalized.append(dict(snapshot, entities=entities))
    return snapshot_roundtrip_quality(*normalized, expected_handles)
