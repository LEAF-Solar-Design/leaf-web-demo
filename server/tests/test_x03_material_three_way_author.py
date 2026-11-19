"""Compare local branch merges with hand-computed, stable-ID expected records."""
import json
from uuid import uuid4

import pytest

from test_integration_tool_service import rig
import project_reference_materials as originals
import tool_loader
import tools_fallback
from cad_versions import CadVersionError
from test_x03_material_revision_author import local


CASES = [
    ("Merge the two imported JSON edits from their common ancestor.",
     [{"id": "R", "dims": {"x": 1, "y": 2}}],
     [{"id": "R", "dims": {"x": 4, "y": 2}}],
     [{"id": "R", "dims": {"x": 1, "y": 5}}],
     [{"id": "R", "dims": {"x": 4, "y": 5}}], []),
    ("Reconcile both branches of my material records and show edit/delete conflicts.",
     [{"id": 9, "address": "A"}, {"id": 4}],
     [{"id": 4}, {"id": 9, "address": "B"}],
     [{"id": 4}, {"id": 8}],
     None, [(9, "", "edit_delete")]),
    ("Combine the two versions of my project material and flag competing changes.",
     [{"id": "Q", "a/b": {"~rate": 0}}],
     [{"id": "Q", "a/b": {"~rate": True}}],
     [{"id": "Q", "a/b": {"~rate": 1}}],
     None, [("Q", "/a~1b/~0rate", "divergent_edit")]),
]


def store_pin(scope, materials, rows, version):
    org, project, actor = scope
    mid = str(uuid4())
    raw = json.dumps({"table": rows}).encode()
    stored = originals.prepare_and_store(org, project, "records.json", "application/json", raw)
    materials[mid] = dict(stored, org_id=org, project_id=project, material_id=mid)
    source = {"kind": "project_material", "material_id": mid,
              "byte_length": len(raw), "sha256": stored["sha256"]}
    context = {"schema": "leaf.project-material-context.v1", "source": source, "row_count": len(rows),
               "selection": {"records_pointer": "/table"}, "envelope": {"table": {
                   "schema": "leaf.selected-records-reference.v1", "row_count": len(rows),
                   "records_pointer": "/table"}}}
    manifest = json.dumps({"material_id": mid, "sha256": stored["sha256"]},
                          sort_keys=True, separators=(",", ":")).encode()
    return (version, mid, context), manifest


@pytest.mark.parametrize("ask,base,left,right,expected,issues", CASES)
def test_three_way_merge(local, ask, base, left, right, expected, issues):
    pkg, source, _ = tools_fallback.author_tool(ask)
    assert pkg["engine_op"] == "material_three_way_merge"
    from authored_material_three_way import bind_three_way
    scope, ledger, materials = local
    ancestor, ancestor_bytes = store_pin(scope, materials, base, 1)
    branches = []
    for artifact, rows in (("l", left), ("r", right)):
        tip, manifest = store_pin(scope, materials, rows, 2)
        ledger.record_upload(*scope[:2], artifact, ancestor_bytes)
        ledger.record_derive(*scope[:2], artifact, 1, manifest)
        branches.append((artifact, tip))
    bound = bind_three_way({}, ledger, *scope, ancestor, branches)
    status, payload = tool_loader._run_source_in_sandbox(source, "combine.py", bound, {})
    assert status == "ok", payload
    report, _ = payload
    assert report["merged_records"] == expected
    assert [(c["id"], c["path"], c["kind"]) for c in report["conflicts"]] == issues
    assert report["ancestor"]["material_id"] == ancestor[1]
    assert [len(ledger.list_versions(*scope[:2], name)) for name, _ in branches] == [2, 2]


def test_unrelated_ancestor_fails(local):
    from authored_material_three_way import bind_three_way
    scope, ledger, materials = local
    ancestor, manifest = store_pin(scope, materials, [], 1)
    other, other_manifest = store_pin(scope, materials, [], 1)
    branches = []
    for artifact, root in (("left", manifest), ("right", other_manifest)):
        tip, data = store_pin(scope, materials, [], 2)
        ledger.record_upload(*scope[:2], artifact, root)
        ledger.record_derive(*scope[:2], artifact, 1, data)
        branches.append((artifact, tip))
    with pytest.raises(CadVersionError):
        bind_three_way({}, ledger, *scope, ancestor, branches)
    with pytest.raises(ValueError, match="ancestor"):
        bind_three_way({}, ledger, *scope, None, branches)
