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
        visible = handle_set(inventory.get("visible_handles"))
        if visible is None:
            return "INCOMPLETE"
        if not visible.issubset(live):
            return "FAIL"
        expected = [visible, visible]
    return "PASS" if (expected == sets[2:] and after-before == additions
                      and before-after == removals) else "FAIL"
