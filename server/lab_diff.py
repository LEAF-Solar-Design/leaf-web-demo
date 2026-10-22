"""Deterministic structural differences using JSON Pointer paths."""
from lab_canonical import canonical_json


def structural_diff(before, after):
    changes = []

    def visit(left, right, path):
        if canonical_json(left) == canonical_json(right):
            return
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(left.keys() | right.keys(), key=lambda key: key.encode("utf-16-be")):
                child = path + "/" + key.replace("~", "~0").replace("/", "~1")
                if key not in right:
                    changes.append({"op": "remove", "path": child, "before": left[key]})
                elif key not in left:
                    changes.append({"op": "add", "path": child, "after": right[key]})
                else:
                    visit(left[key], right[key], child)
        elif isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
            for index, (old, new) in enumerate(zip(left, right)):
                visit(old, new, path + "/" + str(index))
        else:
            changes.append({"op": "replace", "path": path, "before": left, "after": right})

    visit(before, after, "")
    return changes
