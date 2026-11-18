"""Compare saved, reverted and exported selection receipts on one drawing."""
def selection_quality(saved, reverted, exports, added, removed):
    if len(exports) != 2:
        return "INCOMPLETE"
    receipts = [saved, reverted, *exports]
    for row in receipts:
        if not row or any(not row.get(k) for k in ("drawing", "version", "rule_revision")):
            return "INCOMPLETE"
        if not isinstance(row.get("handles"), list):
            return "INCOMPLETE"
    if len({(row["drawing"], row["version"]) for row in receipts}) != 1:
        return "FAIL"
    for original, exported in zip((saved, reverted), exports):
        if (original["rule_revision"] != exported["rule_revision"]
                or set(original["handles"]) != set(exported["handles"])):
            return "FAIL"
    before, after = set(saved["handles"]), set(reverted["handles"])
    return "PASS" if after-before == set(added) and before-after == set(removed) else "FAIL"
