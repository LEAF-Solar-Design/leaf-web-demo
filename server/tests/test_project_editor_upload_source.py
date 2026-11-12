"""Original account DXF survives staging cleanup before project adoption."""
import hashlib
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import guest_uploads
import write_loop
from test_guest_uploads import client, DXF_BYTES, _upload
from test_project_drawing_editor import private_pg


def source_key(tenant, drawing):
    return guest_uploads.original_dxf_key(tenant, drawing, hashlib.sha256(DXF_BYTES).hexdigest())


def test_account_original_is_immutable_before_ready_and_survives_staging_cleanup(client, monkeypatch):
    tenant = "account-original-source"
    observed = []
    original = guest_uploads._preserve_original_dxf

    def preserve(backend, tenant_id, drawing_id, marker, raw):
        current = guest_uploads.read_marker(backend, tenant_id, drawing_id)
        observed.append(current["status"])
        assert current["status"] == "extracting"
        return original(backend, tenant_id, drawing_id, marker, raw)

    monkeypatch.setattr(guest_uploads, "_preserve_original_dxf", preserve)
    uploaded = _upload(client, headers={"X-Tenant-Id": tenant})
    assert uploaded.status_code == 202
    drawing = uploaded.json()["drawing_id"]
    backend = write_loop.upload_backend_for_tenant(tenant)
    marker = guest_uploads.read_marker(backend, tenant, drawing)
    assert marker["status"] == "ready" and observed == ["extracting"]
    key = source_key(tenant, drawing)
    assert backend.get(key) == DXF_BYTES
    guest_uploads.staged_path(tenant, drawing, ".dxf").unlink()
    assert backend.get(key) == DXF_BYTES
    with pytest.raises(ValueError):
        original(backend, tenant, drawing, marker, b"different")
    assert backend.get(key) == DXF_BYTES


def test_source_storage_failure_cannot_publish_ready(client, monkeypatch):
    def unavailable(*_args):
        raise ValueError("immutable original storage refused")
    monkeypatch.setattr(guest_uploads, "_preserve_original_dxf", unavailable)
    response = _upload(client, headers={"X-Tenant-Id": "account-source-refusal"})
    assert response.status_code == 202
    drawing = response.json()["drawing_id"]
    backend = write_loop.upload_backend_for_tenant("account-source-refusal")
    assert guest_uploads.read_marker(backend, "account-source-refusal", drawing)["status"] == "failed"


def test_pg_upload_original_is_preserved_under_attempt_fence_before_ready(private_pg, client, monkeypatch):
    monkeypatch.setenv("LEAF_UPLOAD_STORE", "postgres")
    monkeypatch.setenv("LEAF_DRAWING_STORE", "postgres")
    tenant = str(uuid4())
    response = _upload(client, headers={"X-Tenant-Id": tenant})
    assert response.status_code == 202, response.text
    drawing = response.json()["drawing_id"]
    backend = write_loop.upload_backend_for_tenant(tenant)
    status = guest_uploads.read_marker(backend, tenant, drawing)
    assert status["status"] == "ready", status.get("error", {}).get("error_code")
    assert backend.get(source_key(tenant, drawing)) == DXF_BYTES
    guest_uploads.staged_path(tenant, drawing, ".dxf").unlink()
    assert backend.get(source_key(tenant, drawing)) == DXF_BYTES
