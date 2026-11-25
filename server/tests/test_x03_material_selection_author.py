"""Trace host-selected JSON records through offline author dispatch and sandbox execution."""
from copy import deepcopy
from hashlib import sha256
import json
from uuid import uuid4

import pytest

from test_integration_tool_service import FIELD, REF, read, rig
import project_reference_materials as originals
import tool_loader
import tools_fallback


GENERAL = "Tell me exactly which records in this imported project JSON my drawing checker used."
CASES = [
    ("Which panel records did the checker use from the north roof?", "/roofs/0/panels", ["P1", "P3"]),
    ("Which beam IDs came from the framing/steel section of my import?", "/framing~1steel/beams", ["B7"]),
    ("Which defects came from the second visit in the as~built log?", "/as~0built/1/defects", ["D2", "D4", "D9"]),
]
DOCUMENT = {"roofs": [{"panels": [{"id": "P1"}, {"id": "P3"}]}, {"panels": [{"id": "P8"}]}],
            "framing/steel": {"beams": [{"id": "B7"}]},
            "as~built": [{"defects": [{"id": "D1"}]}, {"defects": [{"id": "D2"}, {"id": "D4"}, {"id": "D9"}]}]}


@pytest.fixture
def material(tmp_path, monkeypatch, rig):
    from leaf_platform import project_reference_materials as metadata
    import store
    import write_loop

    scope = (rig.auth.tenant_id, rig.auth.project_id, str(uuid4()))
    mid = str(uuid4())
    raw = json.dumps(DOCUMENT, indent=2).encode()
    backend = store.FilesystemBackend(str(tmp_path))
    monkeypatch.setattr(write_loop, "upload_backend_for_tenant", lambda _: backend)
    info = originals.prepare_and_store(*scope[:2], "inspection.json", "application/json", raw)
    row = dict(info, material_id=mid, org_id=scope[0], project_id=scope[1])

    def get_material(org, project, actor, material_id):
        assert (org, project, actor, material_id) == (*scope, mid)
        return row

    monkeypatch.setattr(metadata, "get_material", get_material)
    return scope, mid, raw, backend


def context(mid, raw, pointer, count):
    envelope = deepcopy(DOCUMENT)
    marker = {"schema": "leaf.selected-records-reference.v1", "records_pointer": pointer, "row_count": count}
    if pointer == CASES[0][1]:
        envelope["roofs"][0]["panels"] = marker
    elif pointer == CASES[1][1]:
        envelope["framing/steel"]["beams"] = marker
    else:
        envelope["as~built"][1]["defects"] = marker
    return {"schema": "leaf.project-material-context.v1",
            "source": {"kind": "project_material", "material_id": mid, "sha256": sha256(raw).hexdigest(),
                       "byte_length": len(raw)},
            "selection": {"records_pointer": pointer}, "row_count": count, "envelope": envelope}


def execute(ask, intake, params=None):
    package, source, _ = tools_fallback.author_tool(ask)
    assert package["engine_op"] == "material_selection_provenance"
    return tool_loader._run_source_in_sandbox(source, "selected_records.py", intake, params or {})


@pytest.mark.parametrize("ask,pointer,ids", CASES)
def test_author_reports_verified_checker_records(rig, material, ask, pointer, ids):
    # Dispatch must recognize the actual wording before binding any service data.
    assert tools_fallback.author_tool(ask)[0]["engine_op"] == "material_selection_provenance"
    import authored_material_selection as author

    scope, mid, raw, _ = material
    selected = context(mid, raw, pointer, len(ids))
    rig.response = {"records": [{"reference": REF, "title": "Inspection", "fields": [FIELD],
                     "lastAttempt": {"outcome": "success", "row_count": len(ids)},
                     "freshness": read()["freshness"], "material_context": selected}], "nextCursor": None}
    result = rig.svc.execute("datasets", {}, rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id)
    advertised = result["result"]["datasets"][0]["material_context"]
    intake = author.bind_material_selection({}, *scope, mid, advertised)
    for phrase in ([ask, GENERAL] if pointer == CASES[0][1] else [ask]):
        status, payload = execute(phrase, intake)
        assert status == "ok", payload
        report, _ = payload
        assert report == {"material_id": mid, "sha256": sha256(raw).hexdigest(), "byte_length": len(raw),
                          "records_pointer": pointer, "row_count": len(ids),
                          "records": [{"pointer": f"{pointer}/{i}", "id": item} for i, item in enumerate(ids)]}


@pytest.mark.parametrize("damage", ["bytes", "pointer", "count", "envelope", "identity"])
def test_reject_tampered_material_and_selections(material, damage):
    import authored_material_selection as author
    from integration_tool_service import ReadFailure

    scope, mid, raw, backend = material
    _, pointer, ids = CASES[0]
    selected = context(mid, raw, pointer, len(ids))
    if damage == "bytes":
        backend.put(originals.object_key(*scope[:2], sha256(raw).hexdigest()), raw.replace(b"P1", b"P2"))
    elif damage == "pointer":
        selected["selection"]["records_pointer"] = "/roofs/1/panels"
    elif damage == "count":
        selected = context(mid, raw, pointer, 1)
    elif damage == "envelope":
        selected["envelope"]["roofs"][1] = {"panels": [{"id": "forged"}]}
    else:
        selected["source"]["material_id"] = str(uuid4())
    with pytest.raises((ValueError, originals.ReferenceStoreUnavailable, ReadFailure)):
        author.bind_material_selection({}, *scope, mid, selected)


def test_editable_report_cannot_replace_host_selection():
    status, payload = execute(GENERAL, {}, {"material_selection": {"records": []}})
    assert status == "tool_error" and "selection" in str(payload).lower(), payload
