"""Compare revision-bound selections using hexadecimal DXF handles."""
import re


def handle_set(items):
    if not isinstance(items, list) or not all(
            isinstance(h, str) and re.fullmatch(r"[0-9a-fA-F]{1,16}", h)
            and int(h, 16) for h in items):
        return None
    return {int(h, 16) for h in items}


def selection_quality(saved, reverted, exports, added, removed):
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
    before, after = sets[:2]
    return "PASS" if (sets[:2] == sets[2:] and after-before == additions
                      and before-after == removals) else "FAIL"
