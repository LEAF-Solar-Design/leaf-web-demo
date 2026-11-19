"""Compare independently stored JSON selections across an actual scoped version ledger."""
from hashlib import sha256
import json
from uuid import uuid4

import pytest

from test_integration_tool_service import rig
from cad_versions import CadVersionStore
import project_reference_materials as originals

import tool_loader
import tools_fallback

CASES = [
    ("Compare the records my checker used across two revisions of an imported JSON file.",
     "roof/panels", [{"id": "B"}, {"id": "A"}], [{"id": "A"}, {"id": "C"}],
     [("moved", "A", 1, 0), ("removed", "B", 0, None), ("added", "C", None, 1)]),
    ("Show what changed in the beam records between versions of my project material.",
     "steel~beams", [{"id": 3, "m": 4}, {"id": 5}], [{"id": 3, "m": 7}, {"id": 5}],
     [("modified", 3, 0, 0)]),
    ("Diff the imported defect IDs and their source rows from the previous revision to the current one.",
     "defects", [{"id": "old"}], [{"id": "new"}],
     [("removed", "old", 0, None), ("added", "new", None, 0)]),
]


def digest(data):
    return sha256(data).hexdigest()


def pin_selection(scope, version, key, records, ledger, materials):
    mid = str(uuid4())
    doc = {key: records}
    raw = json.dumps(doc, indent=2).encode()
    org, project, actor = scope
    stored = originals.prepare_and_store(org, project, "inspection.json", "application/json", raw)
    materials[mid] = dict(stored, org_id=org, project_id=project, material_id=mid)
    manifest = json.dumps({"material_id": mid, "sha256": digest(raw)}, sort_keys=True, separators=(",", ":")).encode()
    if version == 1:
        ledger.record_upload(org, project, "inspection", manifest)
    else:
        ledger.record_derive(org, project, "inspection", version-1, manifest)
    pointer = "/" + key.replace("~", "~0").replace("/", "~1")
    context = {"schema": "leaf.project-material-context.v1", "source": {
        "kind": "project_material", "material_id": mid, "sha256": digest(raw), "byte_length": len(raw)},
        "selection": {"records_pointer": pointer}, "row_count": len(records),
        "envelope": {key: {"schema": "leaf.selected-records-reference.v1", "records_pointer": pointer,
                           "row_count": len(records)}}}
    return (version, mid, context), raw


@pytest.fixture
def local(rig, monkeypatch, tmp_path):
    import store
    import write_loop
    from leaf_platform import project_reference_materials as metadata
    backend = store.FilesystemBackend(str(tmp_path))
    monkeypatch.setattr(write_loop, "upload_backend_for_tenant", lambda _: backend)
    materials = {}
    scope = rig.auth.tenant_id, rig.auth.project_id, str(uuid4())
    def lookup(org, project, actor, mid):
        assert (org, project, actor) == scope
        return materials.get(mid)
    monkeypatch.setattr(metadata, "get_material", lookup)
    return scope, CadVersionStore(), materials


@pytest.mark.parametrize("ask,key,before,after,expected", CASES)
def test_authored_material_revision(local, ask, key, before, after, expected):
    package, source, _ = tools_fallback.author_tool(ask)
    assert package["engine_op"] == "material_revision_diff"
    from authored_material_revision import bind_material_revisions
    scope, ledger, materials = local
    previous, old_bytes = pin_selection(scope, 1, key, before, ledger, materials)
    current, new_bytes = pin_selection(scope, 2, key, after, ledger, materials)
    intake = bind_material_revisions({}, ledger, *scope, "inspection", previous, current)
    status, payload = tool_loader._run_source_in_sandbox(source, "material_diff.py", intake, {})
    assert status == "ok", payload
    report, _ = payload
    pointer = previous[-1]["selection"]["records_pointer"]
    for field, (version, mid, _), raw in (("before", previous, old_bytes), ("after", current, new_bytes)):
        assert report[field] == {"version": version, "material_id": mid, "sha256": digest(raw),
                                 "records_pointer": pointer}
    changes = {r["id"]: r for r in report["changes"]}
    assert set(changes) == {r[1] for r in expected}
    for kind, identity, i, j in expected:
        row = changes[identity]
        assert row["kind"] == kind
        for side, rows, index in (("before", before, i), ("after", after, j)):
            if index is None:
                assert row[side] is None
            else:
                canonical = json.dumps(rows[index], separators=(",", ":"), sort_keys=True).encode()
                assert row[side] == {"pointer": f"{pointer}/{index}", "sha256": digest(canonical)}


def test_refuses_unlinked_material_versions(local):
    from authored_material_revision import bind_material_revisions
    scope, ledger, materials = local
    prior, _ = pin_selection(scope, 1, "rows", [{"id": 1}], ledger, materials)
    pin_selection(scope, 2, "rows", [{"id": 2}], ledger, materials)
    current, _ = pin_selection(scope, 3, "rows", [{"id": 3}], ledger, materials)
    with pytest.raises(ValueError, match="parent"):
        bind_material_revisions({}, ledger, *scope, "inspection", prior, current)



def test_material_pin_distinguishes_identical_bytes_under_different_ids(local):
    from authored_material_revision import bind_material_revisions
    from cad_versions import DigestMismatch

    scope, ledger, materials = local
    prior, _ = pin_selection(scope, 1, "rows", [{"id": 1}], ledger, materials)
    current, _ = pin_selection(scope, 2, "rows", [{"id": 1}], ledger, materials)
    substituted = (current[0], prior[1], prior[2])
    with pytest.raises(DigestMismatch):
        bind_material_revisions({}, ledger, *scope, "inspection", prior, substituted)
