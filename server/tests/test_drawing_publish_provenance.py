"""Edited-version provenance distinguishes AutoCAD Publish from browser edits."""
from __future__ import annotations

import hashlib
import io

import pytest

import test_save_edited_version as saves
from test_save_edited_version import client as save_client  # noqa: F401


@pytest.fixture
def client(save_client, monkeypatch):
    monkeypatch.setenv("LEAF_DRAWING_STORE", "legacy")
    monkeypatch.setenv("LEAF_BLOB_STORE", "filesystem")
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "1")
    monkeypatch.delenv("LEAF_DRAWING_MUTATIONS_FENCE_FILE", raising=False)
    return save_client


def _history(client):
    response = client.get(f"/api/drawings/{saves.DRAWING}/versions",
                          headers={"X-Tenant-Id": saves.TENANT})
    assert response.status_code == 200, response.text
    return response.json()


def _publish(client, parent, origin):
    fields = {"parent_version": str(parent),
              "source_digest": hashlib.sha256(saves.EDITED_DXF).hexdigest()}
    if origin is not None:
        fields["origin"] = origin
    return client.post(
        f"/api/drawings/{saves.DRAWING}/versions/edited",
        headers={"X-Tenant-Id": saves.TENANT},
        files={"file": ("edited.dxf", io.BytesIO(saves.EDITED_DXF), "application/dxf")},
        data=fields,
    )


@pytest.mark.parametrize("origin,tool,source", [
    ("autocad", "autocad-publish", "autocad"),
    (None, "cad-edit-surface", "cad_edit"),
])
def test_edited_version_records_origin_provenance(client, monkeypatch, origin, tool, source):
    import write_loop

    committed_meta = []
    put_bytes_version = write_loop._put_bytes_version

    def capture_meta(*args, **kwargs):
        committed_meta.append(dict(kwargs["meta"]))
        return put_bytes_version(*args, **kwargs)

    monkeypatch.setattr(write_loop, "_put_bytes_version", capture_meta)

    before = _history(client)
    response = _publish(client, before["head"], origin)
    assert response.status_code == 201, response.text
    receipt = response.json()
    history = _history(client)
    assert history["head"] == before["head"] + 1 == receipt["head"]
    row = next(row for row in history["versions"] if row["v"] == receipt["head"])
    assert row["tool"] == tool

    assert row["parent"] == before["head"]
    # The store's manifest projection omits source and digest metadata. Capture
    # that metadata at the persistence boundary while still doing the real write.
    assert committed_meta == [{
        "tool": tool, "source": source,
        "source_sha256": receipt["source_sha256"],
        "intake_sha256": receipt["intake_sha256"],
        "note": "browser edit via the isolated engine worker",
    }]


@pytest.mark.parametrize("origin", ["browser", "AutoCAD", "autocad "])
def test_invalid_origin_is_bad_params_and_writes_nothing(client, tmp_path, origin):
    before = _history(client)
    files_before = {path.relative_to(tmp_path): path.read_bytes()
                    for path in tmp_path.rglob("*") if path.is_file()}
    response = _publish(client, before["head"], origin)
    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["error_code"] == "BAD_PARAMS"
    assert error["message"] == "origin must be autocad when present"
    assert {path.relative_to(tmp_path): path.read_bytes()
            for path in tmp_path.rglob("*") if path.is_file()} == files_before
    assert _history(client) == before
