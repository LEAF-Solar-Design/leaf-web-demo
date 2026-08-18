"""
The fabrication-trap guards (§19): an uploaded/guest drawing id must NEVER
fall through to write_loop.ensure_demo_drawing's cached rooftop_demo
bootstrap. Real extraction or an honest 404 — nothing else. Also regression-
proves the pre-§19 bootstrap behavior for ordinary tenants is untouched.

Run:  cd server && python -m pytest tests/test_guest_fail_closed.py -q
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
import broker_client
import guest_uploads
import write_loop

SERVER_DIR = Path(__file__).resolve().parent.parent
ROOFTOP = json.loads((SERVER_DIR.parent / "data" / "rooftop_demo.intake.json")
                     .read_text(encoding="utf-8"))


def test_upload_extract_event_key_is_attempt_bound():
    first = broker_client.extract_event_key(
        "tenant-a", "drawing-a", upload=True, attempt="attempt-1")
    replay = broker_client.extract_event_key(
        "tenant-a", "drawing-a", upload=True, attempt="attempt-1")
    replacement = broker_client.extract_event_key(
        "tenant-a", "drawing-a", upload=True, attempt="attempt-2")

    assert first == replay
    assert first != replacement
    with pytest.raises(ValueError, match="require an attempt"):
        broker_client.extract_event_key("tenant-a", "drawing-a", upload=True)


def test_non_upload_extract_event_key_keeps_legacy_digest():
    assert broker_client.extract_event_key("tenant-a", "drawing-a") == (
        "extract:cb0acff9bdde9af6b2a44562d8f8f12e1e679add31c0ce5afe69d6f50899a725"
    )


def test_shared_fence_blocks_mock_write_at_commit(monkeypatch, tmp_path):
    import store

    backend = store.InMemoryBackend()
    tenant, drawing = "tenant-a", "drawing-a"
    payload = json.dumps({"layers": [], "polylines": []}).encode()
    backend.put(store.drawing_version_key(tenant, drawing, 1), payload)
    backend.put(
        store.manifest_key(tenant, drawing),
        json.dumps({
            "schema": 1,
            "tenant_id": tenant,
            "drawing_id": drawing,
            "head": 1,
            "latest": 1,
            "versions": [{
                "v": 1, "parent": None, "created": "now",
                "bytes": len(payload), "sha256": "source",
                "workitem_id": None, "tool": None, "note": None,
            }],
            "checkout": None,
        }).encode(),
    )
    fence = tmp_path / "drawing-mutations"
    fence.write_text("0\n", encoding="utf-8")
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "1")
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_FENCE_FILE", str(fence))

    env, status = write_loop.run_write_mock(
        {"name": "writer", "version": "1.0.0"},
        {"drawing_id": drawing},
        tenant,
        backend=backend,
        t0=0.0,
        run_tool_dynamic_fn=lambda *_args, **_kwargs: {
            "ok": True,
            "result": {"mutations": {"added": [], "removed": []}},
        },
    )

    assert status == 503
    assert env["error"]["retryable"] is True
    assert store.load_manifest(backend, tenant, drawing)["latest"] == 1


def test_shared_fence_blocks_extraction_commit_without_marker_write(
    monkeypatch, tmp_path
):
    from contextlib import contextmanager

    import dxf_intake
    import store

    backend = store.InMemoryBackend()
    tenant, drawing = "tenant-a", "drawing-a"
    marker = guest_uploads.new_marker(
        filename="drawing.dxf",
        data=b"DXF",
        tenant_kind="account",
        source_ext=".dxf",
    )
    guest_uploads.write_marker(backend, tenant, drawing, marker)
    before = {key: backend.get(key) for key in backend.keys()}

    uploads = tmp_path / "uploads"
    monkeypatch.setenv("LEAF_UPLOADS_DIR", str(uploads))
    staged = guest_uploads.staged_path(tenant, drawing, ".dxf")
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"DXF")
    monkeypatch.setattr(
        write_loop, "upload_backend_for_tenant", lambda _tenant: backend
    )
    monkeypatch.setattr(guest_uploads, "upload_store_mode", lambda: "legacy")
    monkeypatch.setattr(write_loop, "drawing_mutations_enabled", lambda: True)
    monkeypatch.setattr(
        dxf_intake,
        "parse_dxf_file",
        lambda *_args, **_kwargs: {"layers": [], "polylines": []},
    )

    @contextmanager
    def drained_commit():
        yield False

    monkeypatch.setattr(
        write_loop, "drawing_mutation_commit_guard", drained_commit
    )

    guest_uploads.run_extraction(tenant, drawing, ".dxf")

    assert {key: backend.get(key) for key in backend.keys()} == before
    assert guest_uploads.read_marker(backend, tenant, drawing) == marker


@pytest.mark.skipif(os.name != "posix", reason="fcntl is a Linux deployment contract")
def test_exclusive_fence_transition_waits_for_shared_commit(monkeypatch, tmp_path):
    import fcntl
    import threading

    fence = tmp_path / "drawing-mutations"
    fence.write_text("1\n", encoding="utf-8")
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "1")
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_FENCE_FILE", str(fence))
    commit_entered = threading.Event()
    release_commit = threading.Event()
    exclusive_acquired = threading.Event()

    def commit():
        with write_loop.drawing_mutation_commit_guard() as enabled:
            assert enabled
            commit_entered.set()
            assert release_commit.wait(5)

    def drain():
        assert commit_entered.wait(5)
        with open(f"{fence}.lock", "a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            exclusive_acquired.set()
            temp = fence.with_suffix(".tmp")
            with open(temp, "w", encoding="utf-8") as output:
                output.write("0\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, fence)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    commit_thread = threading.Thread(target=commit)
    drain_thread = threading.Thread(target=drain)
    commit_thread.start()
    drain_thread.start()
    assert commit_entered.wait(5)
    assert not exclusive_acquired.wait(0.1)
    release_commit.set()
    commit_thread.join(5)
    drain_thread.join(5)
    assert exclusive_acquired.is_set()
    assert not write_loop.drawing_mutations_enabled()


def test_scratch_cleanup_failure_logs_only_key_digest(caplog):
    raw_key = "t/tenant-secret/in/private-file.dwg"

    class _Da:
        @staticmethod
        def delete_scratch_object(_key):
            raise OSError("cleanup unavailable")

    with caplog.at_level("WARNING", logger=write_loop.LOGGER.name):
        write_loop._cleanup_scratch_objects(_Da(), [raw_key])

    assert "APS scratch cleanup failed" in caplog.text
    assert raw_key not in caplog.text
    assert len(caplog.records[0].scratch_key_sha256) == 64
    assert caplog.records[0].error_type == "OSError"


def test_missing_scratch_delete_api_is_observable_without_key(caplog):
    raw_key = "t/tenant-secret/in/private-file.dwg"

    with caplog.at_level("WARNING", logger=write_loop.LOGGER.name):
        write_loop._cleanup_scratch_objects(object(), [raw_key])

    assert "APS scratch cleanup unavailable" in caplog.text
    assert raw_key not in caplog.text
    assert caplog.records[0].error_type == "DeleteMethodUnavailable"


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_GUEST_STORE_DIR", str(tmp_path / "guest"))
    monkeypatch.setenv("LEAF_UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path / "store"))
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    guest_uploads._reset_rate_state()
    return TestClient(app_module.app)


def _in_memory_backend():
    import store
    return store.InMemoryBackend()


def test_ensure_demo_drawing_guest_guard_raises_and_writes_nothing():
    backend = _in_memory_backend()
    with pytest.raises(KeyError):
        write_loop.ensure_demo_drawing(backend, "guest-abc123", "u-deadbeef")
    assert backend.keys() == []  # not one byte was bootstrapped


def test_ensure_demo_drawing_marker_guard_raises():
    backend = _in_memory_backend()
    backend.put(write_loop.upload_marker_key("acme-solar", "u-cafe"),
                b'{"status": "extracting"}')
    with pytest.raises(ValueError) as exc:
        write_loop.ensure_demo_drawing(backend, "acme-solar", "u-cafe")
    assert "upload-status" in str(exc.value)
    # the marker is the ONLY key; no manifest / no demo v1 appeared
    assert all("manifest" not in k and "/v/" not in k for k in backend.keys())


def test_guest_unknown_drawing_404_no_bootstrap(client, tmp_path):
    r = client.get("/api/drawings/some-drawing/intake",
                   headers={"X-Tenant-Id": "guest-abc123"})
    assert r.status_code == 404
    # and the guest store stayed empty on disk
    guest_root = Path(write_loop.guest_store_dir())
    assert not (guest_root / "tenants" / "guest-abc123").exists()


def test_pending_upload_intake_404_with_honest_message(client, monkeypatch):
    # Suppress extraction entirely: the marker stays "extracting".
    monkeypatch.setattr(guest_uploads, "start_extraction_thread",
                        lambda *a, **k: None)
    dxf = b"0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n"
    r = client.post("/api/drawings/upload",
                    files={"file": ("f.dxf", io.BytesIO(dxf))})
    assert r.status_code == 202
    tenant, did = r.json()["tenant_id"], r.json()["drawing_id"]

    i = client.get(f"/api/drawings/{did}/intake", headers={"X-Tenant-Id": tenant})
    assert i.status_code == 404

    s = client.get(f"/api/drawings/{did}/upload-status",
                   headers={"X-Tenant-Id": tenant})
    assert s.status_code == 200
    assert s.json()["status"] == "extracting"


def test_pending_upload_versions_and_checkout_fail_closed(client, monkeypatch):
    monkeypatch.setattr(guest_uploads, "start_extraction_thread",
                        lambda *a, **k: None)
    dxf = b"0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n"
    r = client.post("/api/drawings/upload",
                    files={"file": ("f.dxf", io.BytesIO(dxf))})
    tenant, did = r.json()["tenant_id"], r.json()["drawing_id"]
    assert client.get(f"/api/drawings/{did}/versions",
                      headers={"X-Tenant-Id": tenant}).status_code == 404
    assert client.post(f"/api/drawings/{did}/checkout",
                       headers={"X-Tenant-Id": tenant}).status_code == 404


def test_account_demo_bootstrap_regression_byte_identical(client):
    """Ordinary tenants keep the pre-§19 behavior: any first-seen slug-safe
    drawing id bootstraps v1 from the cached intake, byte-identical."""
    r = client.get("/api/drawings/demo/intake",
                   headers={"X-Tenant-Id": "acme-regression"})
    assert r.status_code == 200
    assert r.json()["intake"] == ROOFTOP
    r2 = client.get("/api/drawings/any-fresh-id/intake",
                    headers={"X-Tenant-Id": "acme-regression"})
    assert r2.status_code == 200
    assert r2.json()["intake"] == ROOFTOP


def test_guest_cannot_reach_demo_bootstrap_even_for_wellknown_id(client):
    r = client.get("/api/drawings/demo/intake",
                   headers={"X-Tenant-Id": "guest-abc123"})
    assert r.status_code == 404


def test_live_write_refuses_non_dwg_uploaded_source():
    """Round-3 MAJOR: a live write signs the v1 blob as HostDwg — for a DXF
    upload that blob is the extracted intake, so the write path must refuse
    honestly instead of handing APS a mislabeled file."""
    import store

    class _NeverCalledDa:
        def __getattr__(self, name):  # any DA use would mean the guard failed
            raise AssertionError(f"da.{name} must not be reached")

    backend = store.InMemoryBackend()
    backend.put(store.manifest_key("acme-solar", "u-dxf1"),
                json.dumps({"schema": 1, "tenant_id": "acme-solar",
                            "drawing_id": "u-dxf1", "head": 1, "latest": 1,
                            "versions": [], "checkout": None}).encode())
    backend.put(write_loop.upload_marker_key("acme-solar", "u-dxf1"),
                json.dumps({"status": "ready", "source_ext": ".dxf"}).encode())
    import time
    env, status = write_loop.run_write_live(
        {"name": "delete-marked-panel"}, {"drawing_id": "u-dxf1"},
        "acme-solar", backend=backend, da=_NeverCalledDa(), t0=time.perf_counter())
    assert status == 400
    assert "DWG source" in env["error"]["message"]


def test_storage_cutover_gate_blocks_all_app_drawing_mutations(client, monkeypatch):
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "0")
    headers = {"X-Tenant-Id": "account-a"}
    assert client.post("/api/drawings/any/undo", headers=headers).status_code == 503
    assert client.post("/api/drawings/any/redo", headers=headers).status_code == 503
    assert client.post(
        "/api/drawings/any/checkout", headers=headers, json={}
    ).status_code == 503
    assert client.delete(
        "/api/drawings/any/checkout", headers=headers
    ).status_code == 503

    platform_api = (
        Path(__file__).resolve().parents[2] / "platform" / "api.py"
    ).read_text(encoding="utf-8")
    platform_fence = (
        Path(__file__).resolve().parents[2] / "platform" / "mutation_fence.py"
    ).read_text(encoding="utf-8")
    assert "with drawing_mutation_commit_guard() as commit_enabled" in platform_api
    assert 'os.environ.get("LEAF_DRAWING_MUTATIONS_ENABLED", "1")' in platform_fence


def test_purge_extraction_race_cannot_resurrect(client, monkeypatch, tmp_path):
    """Round-3 MAJOR: an extraction that finishes AFTER the purge deleted its
    drawing must not resurrect anything — the marker re-check under the shared
    store lock aborts the ingest, and the deletion receipt stays true."""
    import time
    import guest_uploads as gu
    monkeypatch.setattr(gu, "start_extraction_thread", lambda *a, **k: None)
    monkeypatch.setenv("LEAF_GUEST_RETENTION_HOURS", "0.00002")
    import io
    dxf = b"0\nSECTION\n2\nENTITIES\n0\nLWPOLYLINE\n5\nAB\n8\nL1\n70\n1\n" \
          b"10\n1.0\n20\n2.0\n10\n3.0\n20\n4.0\n0\nENDSEC\n0\nEOF\n"
    r = client.post("/api/drawings/upload",
                    files={"file": ("f.dxf", io.BytesIO(dxf))})
    tenant, did = r.json()["tenant_id"], r.json()["drawing_id"]
    time.sleep(0.2)
    assert gu.purge_expired()["count"] == 1  # deleted + receipted while "extracting"

    gu.run_extraction(tenant, did, ".dxf")  # the slow extraction lands NOW

    guest_root = Path(write_loop.guest_store_dir())
    assert not (guest_root / "tenants" / tenant).exists(), \
        "nothing may be recreated after the deletion receipt"
    assert client.get(f"/api/drawings/{did}/upload-status",
                      headers={"X-Tenant-Id": tenant}).status_code == 404
    assert client.get(f"/api/drawings/{did}/intake",
                      headers={"X-Tenant-Id": tenant}).status_code == 404


def test_live_write_dwg_marker_without_source_ext_uses_filename():
    """Round-4 MINOR (compat): markers persisted before source_ext existed
    must not brick live writes — the guard falls back to the recorded
    filename's extension. A .dwg filename passes the guard; a .dxf filename
    is still refused."""
    import store
    import time

    def _marker_backend(filename):
        backend = store.InMemoryBackend()
        backend.put(store.manifest_key("acme-solar", "u-old1"),
                    json.dumps({"schema": 1, "tenant_id": "acme-solar",
                                "drawing_id": "u-old1", "head": 1, "latest": 1,
                                "versions": [], "checkout": None}).encode())
        backend.put(write_loop.upload_marker_key("acme-solar", "u-old1"),
                    json.dumps({"status": "ready", "filename": filename}).encode())
        return backend

    class _NeverCalledDa:
        def __getattr__(self, name):
            raise AssertionError(f"da.{name} must not be reached")

    # .dxf filename, no source_ext: refused at the guard, DA never touched.
    env, status = write_loop.run_write_live(
        {"name": "delete-marked-panel"}, {"drawing_id": "u-old1"},
        "acme-solar", backend=_marker_backend("roof plan.dxf"),
        da=_NeverCalledDa(), t0=time.perf_counter())
    assert status == 400
    assert "DWG source" in env["error"]["message"]

    # .dwg filename, no source_ext: PASSES the guard (the next failure, if
    # any, is version resolution — not the DWG-source refusal).
    class _SentinelDa:
        def signed_download_url(self, key):
            raise RuntimeError("guard-passed-reached-DA")

    env, status = write_loop.run_write_live(
        {"name": "delete-marked-panel"}, {"drawing_id": "u-old1"},
        "acme-solar", backend=_marker_backend("roof plan.dwg"),
        da=_SentinelDa(), t0=time.perf_counter())
    assert not (status == 400 and "DWG source" in (env.get("error") or {}).get("message", ""))


def test_live_write_stages_filesystem_blob_in_broker_owned_aps_scratch():
    """An EFS-backed DWG is copied to APS scratch before its URL is signed."""
    import store
    import time

    backend = store.InMemoryBackend()
    tenant, drawing = "acme-solar", "u-live1"
    raw = b"AC1032" + b"\x00" * 64
    vkey = store.drawing_version_key(tenant, drawing, 1)
    backend.put(vkey, raw)
    backend.put(
        store.manifest_key(tenant, drawing),
        json.dumps({
            "schema": 1,
            "tenant_id": tenant,
            "drawing_id": drawing,
            "head": 1,
            "latest": 1,
            "versions": [{
                "v": 1, "parent": None, "created": "2026-07-24T00:00:00Z",
                "bytes": len(raw), "sha256": "source", "workitem_id": None,
                "tool": None, "note": "upload",
            }],
            "checkout": None,
        }).encode(),
    )
    backend.put(
        write_loop.upload_marker_key(tenant, drawing),
        json.dumps({"status": "ready", "source_ext": ".dwg"}).encode(),
    )

    class DA:
        def __init__(self):
            self.staged = {}
            self.deleted = []

        def ephemeral_input_key(self, name, tenant_id, ts):
            return f"t/{tenant_id}/in/{ts}_{name}"

        def upload_scratch_object(self, local_path, key):
            self.staged[key] = Path(local_path).read_bytes()

        def scratch_signed_download_url(self, key):
            assert self.staged[key] == raw
            return "https://aps.test/input"

        def scratch_signed_upload_url(self, key):
            assert key.startswith(f"t/{tenant}/out/")
            return "upload-key", "https://aps.test/output"

        def activity_qualified(self, name):
            return f"owner.{name}+prod"

        def submit_workitem(self, *_args, **_kwargs):
            return {"id": "wi-1", "status": "success"}

        def finalize_scratch_upload(self, object_key, upload_key):
            assert upload_key == "upload-key"

        def download_scratch_object(self, object_key):
            return raw + b"updated"

        def delete_scratch_object(self, object_key):
            self.deleted.append(object_key)

        def extract(self, local_path):
            assert Path(local_path).read_bytes() == raw + b"updated"
            return {"layers": ["Updated"], "polylines": []}

        def _engine_seconds(self, status):
            return 1.0

    da = DA()
    env, status = write_loop.run_write_live(
        {"name": "delete-marked-panel"},
        {"drawing_id": drawing},
        tenant,
        backend=backend,
        da=da,
        t0=time.perf_counter(),
    )

    assert status == 200
    assert env["result"]["new_version"]["version"] == 2
    assert backend.get(store.drawing_version_key(tenant, drawing, 2)) == raw + b"updated"
    assert len(da.deleted) == 2
    assert all(key.startswith(f"t/{tenant}/") for key in da.deleted)


def test_live_write_scratch_keys_are_unique_within_one_second(monkeypatch):
    """Concurrent tenant writes cannot share APS input or output objects."""
    import store
    import time

    monkeypatch.setattr(write_loop.time, "time", lambda: 1_800_000_000)
    nonces = iter(("nonce-a", "nonce-b"))
    monkeypatch.setattr(
        write_loop.secrets, "token_hex", lambda _size: next(nonces),
    )

    def run(tenant):
        backend = store.InMemoryBackend()
        drawing = "demo"
        raw = (tenant + "-dwg").encode()
        backend.put(store.drawing_version_key(tenant, drawing, 1), raw)
        backend.put(
            store.manifest_key(tenant, drawing),
            json.dumps({
                "schema": 1, "tenant_id": tenant, "drawing_id": drawing,
                "head": 1, "latest": 1,
                "versions": [{
                    "v": 1, "parent": None, "created": "now",
                    "bytes": len(raw), "sha256": "source",
                    "workitem_id": None, "tool": None, "note": None,
                }],
                "checkout": None,
            }).encode(),
        )

        class DA:
            def __init__(self):
                self.inputs = []
                self.outputs = []

            def upload_object(self, path, key):
                self.inputs.append(key)

            def signed_download_url(self, key):
                return "https://aps.test/input"

            def signed_upload_url(self, key):
                self.outputs.append(key)
                return "upload", "https://aps.test/output"

            def activity_qualified(self, name):
                return name

            def submit_workitem(self, *args, **kwargs):
                return {"id": tenant, "status": "success"}

            def finalize_upload(self, *args):
                pass

            def download_object(self, key):
                return raw + b"-out"

            def extract(self, path):
                return {"layers": [], "polylines": []}

            def _engine_seconds(self, status):
                return 0

        da = DA()
        env, status = write_loop.run_write_live(
            {"name": "write"}, {"drawing_id": drawing}, tenant,
            backend=backend, da=da, t0=time.perf_counter(),
        )
        assert status == 200, env
        return da.inputs[0], da.outputs[0]

    first = run("tenant-a")
    second = run("tenant-b")
    assert first != second
    assert first[0].startswith("t/tenant-a/in/")
    assert first[1].startswith("t/tenant-a/out/")
    assert second[0].startswith("t/tenant-b/in/")
    assert second[1].startswith("t/tenant-b/out/")


def test_purge_landing_mid_extraction_cannot_resurrect(client, monkeypatch):
    """Round-4 MINOR: the round-3 race test only proved the ENTRY check
    (purge fully before extraction). This one interleaves the purge INSIDE
    extraction — after the initial marker read, before ingest — so it
    exercises the locked re-check itself: the ingest tail must see the
    vanished marker and abort, leaving the deletion receipt true."""
    import time
    import guest_uploads as gu
    import dxf_intake

    monkeypatch.setattr(gu, "start_extraction_thread", lambda *a, **k: None)
    monkeypatch.setenv("LEAF_GUEST_RETENTION_HOURS", "0.00002")
    dxf = b"0\nSECTION\n2\nENTITIES\n0\nLWPOLYLINE\n5\nAB\n8\nL1\n70\n1\n" \
          b"10\n1.0\n20\n2.0\n10\n3.0\n20\n4.0\n0\nENDSEC\n0\nEOF\n"
    r = client.post("/api/drawings/upload",
                    files={"file": ("f.dxf", io.BytesIO(dxf))})
    tenant, did = r.json()["tenant_id"], r.json()["drawing_id"]
    time.sleep(0.2)  # let the retention window lapse

    receipts = {}
    real_parse = dxf_intake.parse_dxf_file

    def parse_then_purge(*args, **kwargs):
        intake = real_parse(*args, **kwargs)
        # The sweep lands NOW: extraction already passed its entry check and
        # holds real intake in hand — only the locked re-check can stop it.
        receipts["purge"] = gu.purge_expired()
        return intake

    monkeypatch.setattr(dxf_intake, "parse_dxf_file", parse_then_purge)
    gu.run_extraction(tenant, did, ".dxf")

    assert receipts["purge"]["count"] == 1  # deleted + receipted mid-flight
    guest_root = Path(write_loop.guest_store_dir())
    assert not (guest_root / "tenants" / tenant).exists(), \
        "nothing may be recreated after the deletion receipt"
    assert client.get(f"/api/drawings/{did}/upload-status",
                      headers={"X-Tenant-Id": tenant}).status_code == 404
    assert client.get(f"/api/drawings/{did}/intake",
                      headers={"X-Tenant-Id": tenant}).status_code == 404
