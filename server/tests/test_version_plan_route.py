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


def _seed_unchanged_dwg(tmp_path):
    import dxf_intake

    return saves._seed_dwg_backed(
        tmp_path, intake=dxf_intake.parse_dxf_bytes(saves.EDITED_DXF))


def _expected(backend, drawing, mutations):
    import mutation_plan
    import write_loop

    _, base = write_loop.read_intake(backend, saves.TENANT, drawing, 1)
    canonical = mutation_plan.validate_mutations(
        base, mutations, allow_transforms=True, allow_xdata=False, reject_noop=False)
    digest = write_loop.version_content_sha256(backend, saves.TENANT, drawing, 1)
    assert digest is not None
    return mutation_plan.emit_plan(canonical, base_sha256=digest, base_intake=base)


def _assert_plan(client, backend, drawing, expected):
    import store
    import write_loop

    response = _get(client, drawing)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "drawing_id": drawing, "version": 2, "parent_version": 1,
        "plan_text": expected.decode("utf-8"),
        "plan_sha256": hashlib.sha256(expected).hexdigest(),
        "result_sha256": (hashlib.sha256(saves.EDITED_DXF).hexdigest()
                          if backend.exists(write_loop.edited_source_key(
                              saves.TENANT, drawing, 2)) else None),
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


def test_plan_on_edited_version_uses_parent_content_digest(client, tmp_path):
    saves._seed_dwg_backed(tmp_path)
    parent_digest = hashlib.sha256(saves.EDITED_DXF).hexdigest()
    response = client.post(
        f"/api/drawings/{saves.DWG_DRAWING}/versions/edited",
        headers={"X-Tenant-Id": saves.TENANT},
        files={"file": ("edited.dxf", io.BytesIO(saves.EDITED_DXF), "application/dxf")},
        data={"parent_version": "1", "source_digest": parent_digest},
    )
    assert response.status_code == 201, response.text
    edited = saves.EDITED_DXF.replace(b"8\nRoof\n", b"8\nMoved\n")
    result_digest = hashlib.sha256(edited).hexdigest()
    assert parent_digest != result_digest
    response = saves._post(
        client, saves.DWG_DRAWING,
        {"set_layer": [{"handle": "A", "layer": "Moved"}]},
        parent=2, data=edited)
    assert response.status_code == 201, response.text
    assert response.json()["commit"] == "dxf-sidecar"
    response = _get(client, version=3)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["plan_text"].splitlines()[1] == f"BASE_SHA256|{parent_digest}"
    assert body["parent_version"] == 2
    assert body["result_sha256"] == result_digest


@pytest.mark.parametrize("live", [False, True])
def test_dwg_parent_digest_and_result_content(client, tmp_path, monkeypatch, live):
    import deps
    import store

    backend = saves._seed_dwg_backed(tmp_path, intake=saves._live_base_intake())
    _, key = store.resolve_version(backend, saves.TENANT, saves.DWG_DRAWING, 1)
    parent_digest = hashlib.sha256(backend.get(key)).hexdigest()
    monkeypatch.setattr(deps, "APS_LIVE", live)
    response = saves._post(
        client, saves.DWG_DRAWING,
        saves._live_mutations())
    assert response.status_code == 201, response.text
    assert response.json()["commit"] == ("dxf-sidecar" if live else "dwg-plan")
    response = _get(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["plan_text"].splitlines()[1] == f"BASE_SHA256|{parent_digest}"
    assert body["parent_version"] == 1
    assert body["result_sha256"] == (
        hashlib.sha256(saves.EDITED_DXF).hexdigest() if live else None)


def test_mock_plan_round_trips_exact_emitted_bytes(client, tmp_path):
    backend = saves._seed_dwg_backed(tmp_path)
    mutations = {"set_layer": [{"handle": "A", "layer": "Moved"}]}
    expected = _expected(backend, saves.DWG_DRAWING, mutations)
    response = saves._post(client, saves.DWG_DRAWING, mutations)
    assert response.status_code == 201, response.text
    assert response.json()["commit"] == "dwg-plan"
    _assert_plan(client, backend, saves.DWG_DRAWING, expected)


@pytest.mark.parametrize("live", [False, True])
def test_noop_set_layer_is_rejected_only_on_plan_leg(client, tmp_path, monkeypatch, live):
    import deps
    import store
    import write_loop

    backend = _seed_unchanged_dwg(tmp_path)
    before = store.load_manifest(backend, saves.TENANT, saves.DWG_DRAWING)
    monkeypatch.setattr(deps, "APS_LIVE", live)
    mutations = {"set_layer": [{"handle": "A", "layer": "Roof"}]}
    response = saves._post(client, saves.DWG_DRAWING, mutations)
    if live:
        assert response.status_code == 201, response.text
        assert response.json()["commit"] == "dxf-sidecar"
        _assert_plan(client, backend, saves.DWG_DRAWING,
                     _expected(backend, saves.DWG_DRAWING, mutations))
    else:
        assert response.status_code == 422, response.text
        assert "set_layer 'A' is a no-op" in response.text
        assert store.load_manifest(backend, saves.TENANT, saves.DWG_DRAWING) == before
        assert not backend.exists(store.drawing_version_key(saves.TENANT, saves.DWG_DRAWING, 2))
        assert not backend.exists(store.version_plan_key(saves.TENANT, saves.DWG_DRAWING, 2))
        assert not backend.exists(write_loop.edited_source_key(saves.TENANT, saves.DWG_DRAWING, 2))


def test_parent_without_content_digest_commits_without_plan(client, tmp_path):
    import store
    import write_loop

    drawing = saves.DEMO
    assert saves._head(client, drawing) == 1
    backend = store.FilesystemBackend(str(tmp_path / "drawings"))
    # Match the uploaded geometry exactly; only the missing digest disqualifies it.
    _, data, _ = write_loop.read_dxf(backend, saves.TENANT, drawing, 1)
    response = saves._post(client, drawing, {}, data=data)
    assert response.status_code == 201, response.text
    assert response.json()["commit"] == "dxf-sidecar"
    response = _get(client, drawing=drawing)
    assert response.status_code == 404
    assert response.json()["error"]["error_code"] == "no_plan"


@pytest.mark.parametrize("mutations", [{}, {"set_layer": [{"handle": "A", "layer": "Moved"}]}])
def test_sidecar_changed_geometry_without_faithful_plan_commits(client, tmp_path, monkeypatch, mutations):
    import deps
    import write_loop

    backend = _seed_unchanged_dwg(tmp_path)
    edited = saves.EDITED_DXF.replace(b"10\n50\n", b"10\n60\n")
    if mutations:
        edited = edited.replace(b"8\nRoof\n", b"8\nMoved\n")
    # A nonempty partial plan takes the gated sidecar leg too.
    monkeypatch.setattr(deps, "APS_LIVE", True)
    response = saves._post(client, saves.DWG_DRAWING, mutations, data=edited)
    assert response.status_code == 201, response.text
    assert response.json()["commit"] == "dxf-sidecar"
    _, intake = write_loop.read_intake(backend, saves.TENANT, saves.DWG_DRAWING, 2)
    assert intake["polylines"][0]["pts"][1][0] == 60
    assert backend.get(write_loop.edited_source_key(saves.TENANT, saves.DWG_DRAWING, 2)) == edited
    response = _get(client)
    assert response.status_code == 404
    assert response.json()["error"]["error_code"] == "no_plan"


@pytest.mark.parametrize("failure", ["missing-cache", "cache-read", "proof-read", "digest-read", "comparison"])
def test_sidecar_unavailable_replay_metadata_still_commits(client, tmp_path, monkeypatch, failure):
    import store
    import write_loop

    backend = _seed_unchanged_dwg(tmp_path)
    if failure == "missing-cache":
        backend.delete(write_loop.intake_cache_key(saves.TENANT, saves.DWG_DRAWING, 1))
    else:
        def fail(*args, **kwargs):
            if failure == "proof-read":
                raise write_loop.ProofStateUnreadable("unavailable proof")
            raise OSError("unavailable replay metadata")

        target = ("version_content_sha256" if failure == "digest-read" else
                  "verify_live_mutation_effects" if failure == "comparison" else "read_intake")
        monkeypatch.setattr(write_loop, target, fail)
    response = saves._post(client, saves.DWG_DRAWING, {})
    assert response.status_code == 201, response.text
    assert response.json()["commit"] == "dxf-sidecar"
    assert store.load_manifest(backend, saves.TENANT, saves.DWG_DRAWING)["head"] == 2
    response = _get(client)
    assert response.status_code == 404
    assert response.json()["error"]["error_code"] == "no_plan"


def test_unchanged_sidecar_stores_empty_plan(client, tmp_path):
    backend = _seed_unchanged_dwg(tmp_path)
    expected = _expected(backend, saves.DWG_DRAWING, {})
    response = saves._post(client, saves.DWG_DRAWING, {})
    assert response.status_code == 201, response.text
    assert response.json()["commit"] == "dxf-sidecar"
    assert response.json()["plan_sha256"] is None
    _assert_plan(client, backend, saves.DWG_DRAWING, expected)


def test_sidecar_is_published_before_plan_becomes_visible(client, tmp_path, monkeypatch):
    import store
    import write_loop

    backend = _seed_unchanged_dwg(tmp_path)
    expected = _expected(backend, saves.DWG_DRAWING, {})
    real_publish = write_loop.publish_edited_source
    published = []

    def publish(*args, **kwargs):
        assert _get(client).status_code == 404
        assert not backend.exists(store.version_plan_key(saves.TENANT, saves.DWG_DRAWING, 2))
        real_publish(*args, **kwargs)
        assert _get(client).status_code == 404
        published.append(True)

    monkeypatch.setattr(write_loop, "publish_edited_source", publish)
    response = saves._post(client, saves.DWG_DRAWING, {})
    assert response.status_code == 201, response.text
    assert response.json()["source_stored"] is True
    assert published == [True]
    _assert_plan(client, backend, saves.DWG_DRAWING, expected)


@pytest.mark.parametrize("partial_write", [False, True])
def test_failed_sidecar_publication_commits_without_plan(client, tmp_path, monkeypatch, partial_write):
    import store
    import write_loop

    real_publish = write_loop.publish_edited_source

    def fail(*args, **kwargs):
        if partial_write:
            real_publish(*args, **kwargs)
        raise OSError("sidecar publication failed")

    monkeypatch.setattr(write_loop, "publish_edited_source", fail)
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    _seed_unchanged_dwg(baseline_dir)
    real_put = write_loop._put_bytes_version

    def without_plan(*args, **kwargs):
        kwargs["mutation_plan_bytes"] = None
        return real_put(*args, **kwargs)

    # Main commits the intake and reports the nonfatal source-storage failure.
    with monkeypatch.context() as baseline:
        baseline.setenv("LEAF_STORE_DIR", str(baseline_dir / "drawings"))
        baseline.setattr(write_loop, "_put_bytes_version", without_plan)
        expected = saves._post(client, saves.DWG_DRAWING, {})
    assert expected.status_code == 201, expected.text
    assert expected.json()["source_stored"] is False

    backend = _seed_unchanged_dwg(tmp_path)
    response = saves._post(client, saves.DWG_DRAWING, {})
    assert response.status_code == expected.status_code, response.text
    assert response.json() == expected.json()
    entry = store.resolve_version_entry(backend, saves.TENANT, saves.DWG_DRAWING, 2)[2]
    assert "plan_sha256" not in entry
    assert "parent_version" not in entry
    assert not backend.exists(store.version_plan_key(saves.TENANT, saves.DWG_DRAWING, 2))
    plan = _get(client)
    assert plan.status_code == 404
    assert plan.json()["error"]["error_code"] == "no_plan"


def test_gated_live_sidecar_still_stores_plan(client, tmp_path, monkeypatch):
    import deps

    backend = saves._seed_dwg_backed(tmp_path, intake=saves._live_base_intake())
    monkeypatch.setattr(deps, "APS_LIVE", True)
    mutations = saves._live_mutations()
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
    _seed_unchanged_dwg(tmp_path)
    assert saves._post(client, saves.DWG_DRAWING, {}).status_code == 201
    assert _get(client, tenant="other-tenant").status_code == 404
    assert _get(client).status_code == 200


def test_deleted_filesystem_plan_is_no_plan(client, tmp_path):
    import store

    backend = _seed_unchanged_dwg(tmp_path)
    assert saves._post(client, saves.DWG_DRAWING, {}).status_code == 201
    entry = store.resolve_version_entry(backend, saves.TENANT, saves.DWG_DRAWING, 2)[2]
    assert entry["plan_sha256"]
    key = store.version_plan_key(saves.TENANT, saves.DWG_DRAWING, 2)
    path = tmp_path / "drawings" / key
    assert path.is_file()
    path.unlink()
    response = _get(client)
    assert response.status_code == 404, response.text
    assert response.json()["error"]["error_code"] == "no_plan"
    assert store.resolve_version_entry(backend, saves.TENANT, saves.DWG_DRAWING, 2)[2] == entry


@pytest.mark.parametrize("failure", ["key", "file", "http404", "http403", "http500", "oserror"])
def test_plan_read_errors_only_missing_objects_are_no_plan(client, tmp_path, monkeypatch, failure):
    import requests
    import store

    _seed_unchanged_dwg(tmp_path)
    assert saves._post(client, saves.DWG_DRAWING, {}).status_code == 201
    real_get = store.FilesystemBackend.get
    plan_key = store.version_plan_key(saves.TENANT, saves.DWG_DRAWING, 2)

    def get(self, key):
        if key != plan_key:
            return real_get(self, key)
        if failure.startswith("http"):
            response = requests.Response()
            response.status_code = int(failure[4:])
            raise requests.HTTPError("plan read failed", response=response)
        error_type = {"key": KeyError, "file": FileNotFoundError, "oserror": OSError}[failure]
        raise error_type("plan read failed")

    monkeypatch.setattr(store.FilesystemBackend, "get", get)
    response = _get(client)
    if failure in {"key", "file", "http404"}:
        assert response.status_code == 404, response.text
        assert response.json()["error"]["error_code"] == "no_plan"
    else:
        assert response.status_code == 500, response.text


def test_tampered_plan_never_returns_text(client, tmp_path):
    import store

    backend = _seed_unchanged_dwg(tmp_path)
    assert saves._post(client, saves.DWG_DRAWING, {}).status_code == 201
    backend.put(store.version_plan_key(saves.TENANT, saves.DWG_DRAWING, 2), b"tampered-secret")
    response = _get(client)
    assert response.status_code == 500
    assert response.json()["error"]["error_code"] == "plan_integrity"
    assert "plan_text" not in response.json()
    assert "tampered-secret" not in response.text


def test_read_plan_works_with_mutations_disabled(client, tmp_path, monkeypatch):
    _seed_unchanged_dwg(tmp_path)
    assert saves._post(client, saves.DWG_DRAWING, {}).status_code == 201
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "0")
    assert _get(client).status_code == 200
    assert saves._post(client, saves.DWG_DRAWING, {}).status_code == 503
    assert _get(client, version=3).status_code == 404


def test_failed_manifest_publication_leaves_no_plan(client, tmp_path, monkeypatch):
    import store

    backend = _seed_unchanged_dwg(tmp_path)
    real = store.FilesystemBackend.put

    def fail(self, key, data):
        if key.endswith("manifest.json"):
            raise OSError("publication failed")
        return real(self, key, data)

    monkeypatch.setattr(store.FilesystemBackend, "put", fail)
    response = saves._post(client, saves.DWG_DRAWING, {})
    assert response.status_code == 500, response.text
    assert _get(client).status_code == 404
    assert not backend.exists(store.version_plan_key(saves.TENANT, saves.DWG_DRAWING, 2))


@pytest.mark.parametrize("cleanup_fails", [False, True])
@pytest.mark.parametrize("error_kind", ["oserror", "conflict"])
def test_plan_write_failure_commits_like_no_plan(
        client, tmp_path, monkeypatch, caplog, cleanup_fails, error_kind):
    import store
    import write_loop

    error_type = OSError if error_kind == "oserror" else store.ImmutableConflict
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    _seed_unchanged_dwg(baseline_dir)
    real_publish = write_loop._put_bytes_version

    def without_plan(*args, **kwargs):
        kwargs["mutation_plan_bytes"] = None
        return real_publish(*args, **kwargs)

    with monkeypatch.context() as baseline:
        baseline.setenv("LEAF_STORE_DIR", str(baseline_dir / "drawings"))
        baseline.setattr(write_loop, "_put_bytes_version", without_plan)
        expected = saves._post(client, saves.DWG_DRAWING, {})
    assert expected.status_code == 201, expected.text

    backend = _seed_unchanged_dwg(tmp_path)
    real_put = store.FilesystemBackend.put
    real_delete = store.FilesystemBackend.delete

    def fail_plan(self, key, data):
        real_put(self, key, data)
        raise error_type("private-plan-text")

    def delete(self, key):
        if cleanup_fails and key.endswith(".plan.txt"):
            raise OSError("cleanup failed")
        return real_delete(self, key)

    monkeypatch.setattr(store.FilesystemBackend, "put_if_absent_or_verify", fail_plan)
    monkeypatch.setattr(store.FilesystemBackend, "delete", delete)
    response = saves._post(client, saves.DWG_DRAWING, {})
    assert response.status_code == expected.status_code, response.text
    assert response.json() == expected.json()
    entry = store.resolve_version_entry(backend, saves.TENANT, saves.DWG_DRAWING, 2)[2]
    assert "plan_sha256" not in entry
    assert "parent_version" not in entry
    assert backend.exists(store.drawing_version_key(saves.TENANT, saves.DWG_DRAWING, 2))
    plan = _get(client)
    assert plan.status_code == 404
    assert plan.json()["error"]["error_code"] == "no_plan"
    assert backend.exists(store.version_plan_key(saves.TENANT, saves.DWG_DRAWING, 2)) == cleanup_fails
    warnings = [r.getMessage() for r in caplog.records if r.name == store.__name__]
    assert warnings == [f"Plan write failed drawing={saves.DWG_DRAWING} version=2 "
                        f"exception={error_type.__name__}"]


@pytest.mark.parametrize("cleanup_fails", [False, True])
@pytest.mark.parametrize("error_kind", [None, "oserror", "conflict"])
@pytest.mark.parametrize("sidecar_ready", [False, True])
def test_postgres_plan_write_failure_is_optional(
        monkeypatch, caplog, cleanup_fails, error_kind, sidecar_ready):
    import store
    from datetime import timedelta

    error_type = (None if error_kind is None else
                  OSError if error_kind == "oserror" else store.ImmutableConflict)
    backend = store.InMemoryBackend()
    row = {"head": 1, "latest": 1, "checkout_holder": "writer", "checkout_fence": 1,
           "checkout_expires_at": store.datetime.now(store.timezone.utc) + timedelta(minutes=5)}
    version_row = {}

    class Connection:
        def execute(self, sql, params):
            if "SELECT head" in sql:
                self.result = row
            elif "INSERT INTO drawing_store_versions" in sql:
                version_row.update(params, state="reserved")
            elif "SET head =" in sql:
                row["head"] = params["version"]
                self.result = {"tenant_id": "tenant"}
            elif "SET state = 'ready'" in sql:
                version_row.update(params, state="ready")
            elif "SET latest =" in sql:
                row["latest"] = params["version"]
            else:
                raise AssertionError(sql)
            return self

        def fetchone(self):
            return self.result

    class Database:
        def run_transaction(self, callback, **kwargs):
            return callback(Connection())

    monkeypatch.setattr(store, "_db", lambda: Database())
    plan = b"LEAF_MUTATION_PLAN|private-plan-text"
    key = store.version_plan_key("tenant", "drawing", 2)

    def fail_plan(key, data):
        backend.put(key, data)
        raise error_type("private-plan-text")

    def fail_delete(key):
        raise OSError("cleanup failed")

    if error_type is not None:
        monkeypatch.setattr(backend, "put_if_absent_or_verify", fail_plan)
    if cleanup_fails:
        monkeypatch.setattr(backend, "delete", fail_delete)

    def prepare(version):
        assert version == 2
        assert version_row["state"] == "reserved"
        assert row["head"] == 1
        assert not backend.exists(key)
        return sidecar_ready

    version = store._pg_put(
        backend, "tenant", "drawing", b"version bytes", 1,
        {"note": "public note", "plan_sha256": hashlib.sha256(plan).hexdigest()},
        holder="writer", fence=1, mutation_plan_bytes=plan,
        prepare_mutation_plan=prepare)
    assert version == row["head"] == row["latest"] == 2
    assert version_row["state"] == "ready"
    assert backend.get(store.drawing_version_key("tenant", "drawing", 2)) == b"version bytes"
    entry = store._decode_plan_note({"v": 2, "note": version_row["note"]})
    if error_type is None and sidecar_ready:
        assert entry["plan_sha256"] == hashlib.sha256(plan).hexdigest()
        assert entry["parent_version"] == 1
        assert backend.get(key) == plan
    else:
        assert entry == {"v": 2, "note": "public note"}
        assert backend.exists(key) == (cleanup_fails and sidecar_ready)
        warnings = [r.getMessage() for r in caplog.records if r.name == store.__name__]
        assert warnings == ([f"Plan write failed drawing=drawing version=2 exception={error_type.__name__}"]
                            if sidecar_ready else [])


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
