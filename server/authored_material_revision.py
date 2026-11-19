"""Compare two host-selected JSON record sets against immutable revision references."""
import json
import re

from authored_material_selection import bind_material_selection


def matches(description):
    source = re.search(r"\b(?:json|imports?|imported|materials?)\b", description, re.I)
    revisions = re.search(r"\b(?:revisions?|versions?|reordered|before and after)\b", description, re.I)
    comparison = re.search(r"\b(?:compare|comparisons?|diff|differences?|change[ds]?|moved|edited)\b",
                           description, re.I)
    return bool(source and revisions and comparison)


def _field_report(description):
    return bool(re.search(r"\b(?:fields?|properties|keys?|values?|null|missing|absent|boolean|numeric)\b",
                          description, re.I))


def bind_material_revisions(intake, ledger, org, project, actor, artifact_id, prior, current, *,
                            include_row_values=False, description="", exact_numbers=True):
    """The host supplies a selected ledger and separately authorized material IDs."""
    include_row_values = include_row_values or _field_report(description)
    snapshots = []
    for version, mid, selection in (prior, current):
        if type(version) is not int or version < 1:
            raise ValueError("Invalid material revision.")
        bound = bind_material_selection({}, org, project, actor, mid, selection,
                                        include_row_digests=True, include_row_values=include_row_values,
                                        exact_numbers=exact_numbers)
        verified = bound["checker_material_selection"]
        # A digest alone cannot distinguish two materials containing identical bytes.
        revision_ref = {"material_id": str(mid), "sha256": verified["sha256"]}
        manifest = json.dumps(revision_ref, separators=(",", ":"), sort_keys=True).encode()
        receipt = ledger.verify_read(org, project, artifact_id, version, manifest)
        if snapshots and receipt.parent_version != prior[0]:
            raise ValueError("Current material revision must directly reference the selected parent.")
        ids = verified["record_ids"]
        if None in ids or len(set(ids)) != len(ids):
            raise ValueError("Selected records need unique IDs in each revision.")
        snapshots.append({"version": version, **revision_ref, "records_pointer": verified["records_pointer"],
                          "record_ids": ids, "record_sha256": verified["record_sha256"]})
        if include_row_values:
            snapshots[-1]["record_values"] = verified["record_values"]
            if "record_exact_numbers" in verified:
                snapshots[-1]["record_exact_numbers"] = verified["record_exact_numbers"]
    return {**intake, "linked_material_selections": snapshots}


def build_body(description):
    if _field_report(description):
        return _FIELDS + _BODY.replace("__FIELD_DETAILS__", "add_field_changes(snapshots, changes)")
    return _BODY.replace("__FIELD_DETAILS__", "")


_FIELDS = """
import json


def add_field_changes(snapshots, changes):
    missing = object()
    rows = []
    exact = []
    for snap in snapshots:
        values = snap.get("record_values")
        if type(values) is not list or len(values) != len(snap["record_ids"]):
            raise ValueError("Verified record values are required for a field comparison.")
        rows.append(dict(zip(snap["record_ids"], values)))
        exact.append(dict(zip(snap["record_ids"], snap.get("record_exact_numbers", []))))

    def state(base, path, value, numbers):
        item = {"pointer": base["pointer"] + path if base is not None else None,
                "present": value is not missing}
        if value is not missing:
            item["value"] = value
            if path in numbers:
                item["value_type"] = "decimal"
            elif type(value) in (dict, list):
                descendants = sorted(p[len(path):] for p in numbers if p.startswith(path + "/"))
                if descendants:
                    item["decimal_pointers"] = descendants
        return item

    for change in changes:
        fields = change["fields"] = []
        identity = change["id"]
        old_numbers, new_numbers = (set(side.get(identity, ())) for side in exact)
        # Nested lists use JSON Pointer indices. Only the selected records use stable IDs.
        pending = [("", rows[0].get(identity, missing), rows[1].get(identity, missing))]
        while pending:
            path, left, right = pending.pop()
            if type(left) is dict and type(right) is dict:
                for key in reversed(sorted(left.keys() | right.keys())):
                    part = key.replace("~", "~0").replace("/", "~1")
                    pending.append((path + "/" + part, left.get(key, missing), right.get(key, missing)))
            elif type(left) is list and type(right) is list:
                for i in reversed(range(max(len(left), len(right)))):
                    pending.append((path + "/" + str(i), left[i] if i < len(left) else missing,
                                    right[i] if i < len(right) else missing))
            elif (left is missing or right is missing or (path in old_numbers) != (path in new_numbers)
                  or json.dumps(left, sort_keys=True) != json.dumps(right, sort_keys=True)):
                fields.append({"before": state(change["before"], path, left, old_numbers),
                               "after": state(change["after"], path, right, new_numbers)})
"""


_BODY = """
def run(intake, params):
    snapshots = intake.get("linked_material_selections")
    if not snapshots or len(snapshots) != 2:
        raise ValueError("Select two linked material revisions with verified records.")
    sides = []
    report = {}
    for name, snapshot in zip(("before", "after"), snapshots):
        report[name] = {key: snapshot[key] for key in ("version", "material_id", "sha256", "records_pointer")}
        sides.append({identity: {"pointer": snapshot["records_pointer"] + "/" + str(index),
                                 "sha256": snapshot["record_sha256"][index]}
                      for index, identity in enumerate(snapshot["record_ids"])})
    previous, current = sides
    changes = []
    for identity in dict.fromkeys(list(previous) + list(current)):
        left, right = previous.get(identity), current.get(identity)
        if left == right:
            continue
        if left is None:
            kind = "added"
        elif right is None:
            kind = "removed"
        elif left["sha256"] != right["sha256"]:
            kind = "modified"
        else:
            kind = "moved"
        present_in_both = left is not None and right is not None
        changes.append({"id": identity, "kind": kind, "before": left, "after": right,
                        "moved": present_in_both and left["pointer"] != right["pointer"],
                        "edited": present_in_both and left["sha256"] != right["sha256"]})
    __FIELD_DETAILS__
    report["changes"] = changes
    return report, None
"""
