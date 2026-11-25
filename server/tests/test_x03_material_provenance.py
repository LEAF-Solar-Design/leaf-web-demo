"""Offline material storage and Studio discovery, with a recorded Ops response shape."""
from copy import deepcopy
from hashlib import sha256
import json
from uuid import uuid4

import pytest

from test_integration_tool_service import FIELD, REF, read, rig, service
import project_reference_materials as references


@pytest.mark.parametrize("ask,parts,pointer,expected_ids", [
    ("Which panel records did the checker use from the north roof?",
     ("roofs", 0, "panels"), "/roofs/0/panels", ["P1", "P3"]),
    ("Which beam IDs came from the framing/steel section of my import?",
     ("framing/steel", "beams"), "/framing~1steel/beams", ["B7"]),
    ("Which defects came from the second visit in the as~built log?",
     ("as~built", 1, "defects"), "/as~0built/1/defects", ["D2", "D4", "D9"]),
])
def test_material_provenance(rig, tmp_path, monkeypatch, ask, parts, pointer, expected_ids):
    import store
    import write_loop

    document = {"roofs": [{"panels": [{"id": "P1"}, {"id": "P3"}]}, {"panels": [{"id": "P8"}]}],
                "framing/steel": {"beams": [{"id": "B7"}]},
                "as~built": [{"defects": [{"id": "D1"}]},
                             {"defects": [{"id": "D2"}, {"id": "D4"}, {"id": "D9"}]}]}
    raw = json.dumps(document, indent=2).encode()
    backend = store.FilesystemBackend(str(tmp_path))
    monkeypatch.setattr(write_loop, "upload_backend_for_tenant", lambda tenant: backend)
    scope = (rig.auth.tenant_id, rig.auth.project_id)
    material = references.prepare_and_store(*scope, "inspection.json", "application/json", raw)
    material_id = str(uuid4())
    envelope = json.loads(references.read_content(*scope, material))
    parent = envelope
    for part in parts[:-1]:
        parent = parent[part]
    rows = parent[parts[-1]]
    assert [row["id"] for row in rows] == expected_ids, ask
    parent[parts[-1]] = {"schema": "leaf.selected-records-reference.v1",
                          "records_pointer": pointer, "row_count": len(rows)}
    context = {"schema": "leaf.project-material-context.v1",
               "source": {"kind": "project_material", "material_id": material_id,
                          "sha256": material["sha256"], "byte_length": material["byte_size"]},
               "selection": {"records_pointer": pointer}, "row_count": len(rows), "envelope": envelope}
    rig.response = {"records": [{"reference": REF, "title": "Inspection", "fields": [FIELD],
                                 "lastAttempt": {"outcome": "success", "row_count": len(rows)},
                                 "freshness": read()["freshness"], "material_context": context}], "nextCursor": None}
    result = rig.svc.execute("datasets", {}, rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id)
    projected = result["result"]["datasets"][0]["material_context"]
    assert projected["source"] == {"kind": "project_material", "material_id": material_id,
                                   "sha256": sha256(raw).hexdigest(), "byte_length": len(raw)}
    assert projected["selection"] == {"records_pointer": pointer}
    assert projected["row_count"] == len(expected_ids)
    assert projected["envelope"] == envelope

    wrong = deepcopy(context)
    wrong["selection"]["records_pointer"] = "/roofs/1/panels"
    with pytest.raises(service.ReadFailure, match="invalid_response"):
        service.validate_material_context(wrong)
    # Return equal-length, different bytes at the advertised digest's storage key.
    digest = sha256(raw.replace(b"P1", b"P2")).hexdigest()
    backend.put_if_absent_or_verify(references.object_key(*scope, digest), raw)
    with pytest.raises(references.ReferenceStoreUnavailable, match="integrity"):
        references.read_content(*scope, {**material, "sha256": digest})
