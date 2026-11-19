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


def bind_material_revisions(intake, ledger, org, project, actor, artifact_id, prior, current, *,
                            include_row_values=False):
    """The host supplies a selected ledger and separately authorized material IDs."""
    snapshots = []
    for version, mid, selection in (prior, current):
        if type(version) is not int or version < 1:
            raise ValueError("Invalid material revision.")
        bound = bind_material_selection({}, org, project, actor, mid, selection,
                                        include_row_digests=True, include_row_values=include_row_values)
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
    return {**intake, "linked_material_selections": snapshots}


def build_body(description):
    return _BODY


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
    report["changes"] = changes
    return report, None
"""
