"""Committed browser plans are exact, tenant-bound, integrity-checked artifacts."""
from __future__ import annotations

import hashlib
import io
import json
import time

import pytest

import test_save_plan_version as saves
from test_save_plan_version import client as save_client  # noqa: F401


@pytest.fixture
def client(save_client, monkeypatch):
    # Use the real admission/commit gates, with explicit hermetic configuration.
    monkeypatch.setenv("LEAF_DRAWING_STORE", "legacy")
    monkeypatch.setenv("LEAF_BLOB_STORE", "filesystem")
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "1")
    monkeypatch.delenv("LEAF_DRAWING_MUTATIONS_FENCE_FILE", raising=False)
    return save_client


def _get(client, drawing=saves.DWG_DRAWING, version=2, tenant=saves.TENANT):
    return client.get(f"/api/drawings/{drawing}/versions/{version}/plan",
                      headers={"X-Tenant-Id": tenant})


def _expected(backend, drawing, mutations):
    import mutation_plan
    import store
    import write_loop

    _, key = store.resolve_version(backend, saves.TENANT, drawing, 1)
    source = backend.get(key)
    _, base = write_loop.read_intake(backend, saves.TENANT, drawing, 1)
    canonical = mutation_plan.validate_mutations(
        base, mutations, allow_transforms=True, allow_xdata=False, reject_noop=False)
    digest = hashlib.sha256(
        source if source.startswith(write_loop.DWG_MAGIC) else saves.EDITED_DXF).hexdigest()
    return mutation_plan.emit_plan(canonical, base_sha256=digest, base_intake=base)


def _assert_plan(client, backend, drawing, expected):
    import store

    response = _get(client, drawing)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "drawing_id": drawing, "version": 2, "parent_version": 1,
        "plan_text": expected.decode("utf-8"),
        "plan_sha256": hashlib.sha256(expected).hexdigest(),
        "error": None, "degraded_mode": False,
    }
    assert expected.startswith(b"LEAF_MUTATION_PLAN|")
    assert backend.get(store.version_plan_key(saves.TENANT, drawing, 2)) == expected
    entry = store.load_manifest(backend, saves.TENANT, drawing)["versions"][-1]
    assert entry["parent_version"] == 1
    assert entry["plan_sha256"] == hashlib.sha256(expected).hexdigest()
    history = client.get(f"/api/drawings/{drawing}/versions",
                         headers={"X-Tenant-Id": saves.TENANT}).json()
    assert history["versions"][-1]["plan_sha256"] == entry["plan_sha256"]


def test_mock_plan_round_trips_exact_emitted_bytes(client, tmp_path):
    backend = saves._seed_dwg_backed(tmp_path)
    mutations = {"set_layer": [{"handle": "A", "layer": "Moved"}]}
    expected = _expected(backend, saves.DWG_DRAWING, mutations)
    response = saves._post(client, saves.DWG_DRAWING, mutations)
    assert response.status_code == 201, response.text
    assert response.json()["commit"] == "dwg-plan"
    _assert_plan(client, backend, saves.DWG_DRAWING, expected)


@pytest.mark.parametrize("dwg_backed,mutations", [
    (False, {"added": [{"handle": "n1", "kind": "LINE", "layer": "New",
                        "pts": [[1, 2], [4, 6]]}]}),
    (False, {}),
    (True, {}),
])
def test_sidecar_including_no_operations_stores_emitted_plan(
        client, tmp_path, dwg_backed, mutations):
    import store

    if dwg_backed:
        backend = saves._seed_dwg_backed(tmp_path)
        drawing = saves.DWG_DRAWING
    else:
        drawing = saves.DEMO
        assert saves._head(client, drawing) == 1
        backend = store.FilesystemBackend(str(tmp_path / "drawings"))
    expected = _expected(backend, drawing, mutations)
    response = saves._post(client, drawing, mutations)
    assert response.status_code == 201, response.text
    assert response.json()["commit"] == "dxf-sidecar"
    assert response.json()["plan_sha256"] is None  # Existing save response stays unchanged.
    _assert_plan(client, backend, drawing, expected)


def test_gated_live_sidecar_still_stores_plan(client, tmp_path, monkeypatch):
    import deps

    backend = saves._seed_dwg_backed(tmp_path)
    monkeypatch.setattr(deps, "APS_LIVE", True)
    mutations = {"set_layer": [{"handle": "A", "layer": "Moved"}]}
    expected = _expected(backend, saves.DWG_DRAWING, mutations)
    response = saves._post(client, saves.DWG_DRAWING, mutations)
    assert response.status_code == 201, response.text
    assert response.json()["commit"] == "dxf-sidecar"
    _assert_plan(client, backend, saves.DWG_DRAWING, expected)


@pytest.mark.parametrize("mutations", [
    pytest.param({"removed": ["AB"]}, id="unknown-handle"),
    pytest.param({"set_points": [{
        "handle": "A", "closed": True,
        "pts": [[0, 0, 0], [2, 0, 0], [2, 2, 1], [0, 2, 0]],
    }]}, id="unlowerable-points"),
])
def test_sidecar_without_emittable_plan_still_commits(client, tmp_path, monkeypatch, mutations):
    import deps
    import store

    backend = saves._seed_dwg_backed(tmp_path)
    monkeypatch.setattr(deps, "APS_LIVE", True)
    response = saves._post(client, saves.DWG_DRAWING, mutations)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["commit"] == "dxf-sidecar"
    assert body["commit_note"] == (
        "live plan commit is gated off (LEAF_PLAN_LIVE_LEG); the DXF carries this save")
    assert body["new_version"] == {
        "drawing_id": saves.DWG_DRAWING, "version": 2, "parent": 1}
    assert body["head"] == 2
    assert body["plan_sha256"] is None
    assert body["source_stored"] is True
    dxf = client.get(f"/api/drawings/{saves.DWG_DRAWING}/dxf?version=2",
                     headers={"X-Tenant-Id": saves.TENANT})
    assert dxf.status_code == 200
    assert dxf.content == saves.EDITED_DXF
    response = _get(client)
    assert response.status_code == 404
    assert response.json()["error"]["error_code"] == "no_plan"
    assert not backend.exists(store.version_plan_key(saves.TENANT, saves.DWG_DRAWING, 2))
    entry = store.resolve_version_entry(backend, saves.TENANT, saves.DWG_DRAWING, 2)[2]
    assert "plan_sha256" not in entry
    assert "parent_version" not in entry


@pytest.mark.parametrize("at_commit", [False, True])
def test_stale_parent_writes_no_plan(client, tmp_path, monkeypatch, at_commit):
    import store
    import write_loop

    backend = saves._seed_dwg_backed(tmp_path)
    if at_commit:
        real = write_loop._put_bytes_version

        def stale(*args, **kwargs):
            kwargs["parent_version"] += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(write_loop, "_put_bytes_version", stale)
    response = saves._post(client, saves.DWG_DRAWING, {}, parent=1 if at_commit else 7)
    assert response.status_code == 409, response.text
    assert _get(client).status_code == 404
    assert not backend.exists(store.version_plan_key(saves.TENANT, saves.DWG_DRAWING, 2))
    assert store.load_manifest(backend, saves.TENANT, saves.DWG_DRAWING)["latest"] == 1


def test_uploaded_and_edited_dxf_versions_have_no_plan(client, tmp_path):
    saves._seed_dwg_backed(tmp_path)
    response = _get(client, version=1)
    assert response.status_code == 404
    assert response.json()["error"]["error_code"] == "no_plan"
    response = client.post(
        f"/api/drawings/{saves.DWG_DRAWING}/versions/edited",
        headers={"X-Tenant-Id": saves.TENANT},
        files={"file": ("edited.dxf", io.BytesIO(saves.EDITED_DXF), "application/dxf")},
        data={"parent_version": "1",
              "source_digest": hashlib.sha256(saves.EDITED_DXF).hexdigest()},
    )
    assert response.status_code == 201, response.text
    response = _get(client)
    assert response.status_code == 404
    assert response.json()["error"]["error_code"] == "no_plan"


@pytest.mark.parametrize("version", ["0", "-1", "oops", "1.5",
                                    pytest.param("9" * 5000, id="overlong")])
def test_invalid_version_is_400(client, version):
    assert _get(client, version=version).status_code == 400


@pytest.mark.parametrize("drawing", ["Bad-ID", "bad.id"])
def test_malformed_drawing_id_is_400(client, drawing):
    assert _get(client, drawing=drawing).status_code == 400


def test_unknown_drawing_is_404_without_writing(client, tmp_path):
    assert _get(client, drawing="unknown").status_code == 404
    assert not (tmp_path / "drawings").exists()


def test_another_tenant_cannot_read_plan(client, tmp_path):
    saves._seed_dwg_backed(tmp_path)
    assert saves._post(client, saves.DWG_DRAWING, {}).status_code == 201
    assert _get(client, tenant="other-tenant").status_code == 404
    assert _get(client).status_code == 200


def test_tampered_plan_never_returns_text(client, tmp_path):
    import store

    backend = saves._seed_dwg_backed(tmp_path)
    assert saves._post(client, saves.DWG_DRAWING, {}).status_code == 201
    backend.put(store.version_plan_key(saves.TENANT, saves.DWG_DRAWING, 2), b"tampered-secret")
    response = _get(client)
    assert response.status_code == 500
    assert response.json()["error"]["error_code"] == "plan_integrity"
    assert "plan_text" not in response.json()
    assert "tampered-secret" not in response.text


def test_read_plan_works_with_mutations_disabled(client, tmp_path, monkeypatch):
    saves._seed_dwg_backed(tmp_path)
    assert saves._post(client, saves.DWG_DRAWING, {}).status_code == 201
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "0")
    assert _get(client).status_code == 200
    assert saves._post(client, saves.DWG_DRAWING, {}).status_code == 503
    assert _get(client, version=3).status_code == 404


@pytest.mark.parametrize("failure", ["manifest", "plan"])
def test_failed_publication_leaves_no_plan(client, tmp_path, monkeypatch, failure):
    import store

    backend = saves._seed_dwg_backed(tmp_path)
    real = store.FilesystemBackend.put

    def fail(self, key, data):
        if ((failure == "manifest" and key.endswith("manifest.json"))
                or (failure == "plan" and key.endswith(".plan.txt"))):
            raise OSError("publication failed")
        return real(self, key, data)

    monkeypatch.setattr(store.FilesystemBackend, "put", fail)
    if failure == "plan":
        # Exercise a partial object write followed by an error, through the
        # immutable publication seam used by the real store.
        def fail_plan(self, key, data):
            real(self, key, data)
            raise OSError("plan publication failed")

        monkeypatch.setattr(store.FilesystemBackend, "put_if_absent_or_verify", fail_plan)
    response = saves._post(client, saves.DWG_DRAWING, {})
    assert response.status_code == 500, response.text
    assert _get(client).status_code == 404
    assert not backend.exists(store.version_plan_key(saves.TENANT, saves.DWG_DRAWING, 2))


@pytest.mark.parametrize("tamper", [False, True])
def test_live_commit_stores_only_the_plan_submitted_with_the_job(client, tmp_path, tamper):
    import mutation_plan
    import store
    import test_live_mutation_plan as live
    import write_loop

    backend = live._store(tmp_path)
    plan = live._data_plan(backend)
    if tamper:
        plan["plan_sha256"] = "0" * 64
    da = live.FakeDa(live._actual_success())
    envelope, status = write_loop.run_data_plan_live(
        plan, "tenant", backend=backend, da=da, t0=time.perf_counter())
    if tamper:
        assert status == 422, envelope
        assert da.submissions == []
        assert not backend.exists(store.version_plan_key("tenant", "drawing", 2))
        return
    assert status == 200, envelope
    stored = backend.get(store.version_plan_key("tenant", "drawing", 2))
    canonical = mutation_plan.validate_mutations(
        live._base(), plan["mutations"], allow_transforms=True, allow_xdata=False)
    _, key = store.resolve_version(backend, "tenant", "drawing", 1)
    assert stored == mutation_plan.emit_plan(
        canonical, base_sha256=hashlib.sha256(backend.get(key)).hexdigest(),
        base_intake=live._base())
    assert stored in da.staged.values()
    assert hashlib.sha256(stored).hexdigest() == plan["plan_sha256"]
    response = _get(client, drawing="drawing", tenant="tenant")
    assert response.status_code == 200, response.text
    assert response.json()["parent_version"] == 1
    assert response.json()["plan_text"].encode() == stored


def test_postgres_plan_metadata_preserves_public_note():
    import store

    metadata = {"note": "live browser edit plan", "parent_version": 7,
                "plan_sha256": "a" * 64}
    entry = store._decode_plan_note({"v": 8, "note": store._PLAN_NOTE_PREFIX + json.dumps(metadata)})
    assert entry == {"v": 8, **metadata}
