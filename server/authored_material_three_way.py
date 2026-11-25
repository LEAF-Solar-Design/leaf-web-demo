"""Preview a merge between JSON selections with verified material ancestry."""
import re

from authored_material_revision import bind_material_revisions


def matches(description):
    return bool(re.search(r"\b(?:merge|combine|reconcile)\b", description, re.I)
                and re.search(r"\b(?:materials?|imports?|imported|json)\b", description, re.I)
                and re.search(r"\b(?:two|both|branches|three.way|ancestor)\b", description, re.I))


def bind_three_way(intake, ledger, org, project, actor, ancestor, branches):
    """Verify both direct parent links to the exact same scoped material ID and bytes."""
    if not ancestor or len(branches) != 2 or branches[0][0] == branches[1][0]:
        raise ValueError("Select a common ancestor and two distinct material branches.")
    snapshots = []
    for artifact, tip in branches:
        pair = bind_material_revisions({}, ledger, org, project, actor, artifact, ancestor, tip,
                                       include_row_values=True, exact_numbers=False)["linked_material_selections"]
        base, child = pair
        if child["records_pointer"] != base["records_pointer"]:
            raise ValueError("Select corresponding record arrays at the same JSON pointer.")
        if not snapshots:
            snapshots.append(base)
        snapshots.append({**child, "branch": artifact})
    return {**intake, "three_way_materials": snapshots}


def build_body(description):
    return _BODY


_BODY = """
import json


def run(intake, params):
    snapshots = intake.get("three_way_materials")
    if not snapshots or len(snapshots) != 3:
        raise ValueError("Select a verified ancestor and both descendant materials.")
    missing = object()
    conflicts = []

    def equal(a, b):
        if a is missing or b is missing:
            return a is b
        return json.dumps(a, sort_keys=True, allow_nan=False) == json.dumps(b, sort_keys=True, allow_nan=False)

    def merge(identity, path, base, left, right):
        if equal(left, right) or equal(base, right):
            return left
        if equal(base, left):
            return right
        if all(type(side) is dict for side in (base, left, right)):
            result = {}
            for key in dict.fromkeys(list(base) + list(left) + list(right)):
                child = path + "/" + key.replace("~", "~0").replace("/", "~1")
                value = merge(identity, child, base.get(key, missing),
                              left.get(key, missing), right.get(key, missing))
                if value is not missing:
                    result[key] = value
            return result
        kind = "edit_delete" if left is missing or right is missing else "divergent_edit"
        conflicts.append({"id": identity, "path": path, "kind": kind})
        return missing

    maps = [{row["id"]: row for row in snap["record_values"]} for snap in snapshots]
    merged = []
    # Stable-ID selection order is base, followed by new left and right IDs.
    # Arrays inside a record are atomic values.
    for identity in dict.fromkeys(list(maps[0]) + list(maps[1]) + list(maps[2])):
        row = merge(identity, "", *(rows.get(identity, missing) for rows in maps))
        if row is not missing:
            merged.append(row)
    pins = [{key: value for key, value in snap.items()
             if key in ("branch", "version", "material_id", "records_pointer", "sha256")}
            for snap in snapshots]
    return {"ancestor": pins[0], "branches": pins[1:], "conflicts": conflicts,
            "merged_records": None if conflicts else merged}, None
"""
