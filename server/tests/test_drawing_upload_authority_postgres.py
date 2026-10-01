"""Focused PostgreSQL drawing and upload authority tests.

The selector and legacy checks always run. Fleet race tests require an explicit
DATABASE_URL and skip cleanly otherwise.
"""
from __future__ import annotations

import os
import hashlib
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import guest_uploads
import store
import write_loop


def test_authority_selectors_are_legacy_by_default_and_reject_typos(monkeypatch):
    monkeypatch.delenv("LEAF_DRAWING_STORE", raising=False)
    monkeypatch.delenv("LEAF_UPLOAD_STORE", raising=False)
    assert store.authority_mode() == "legacy"
    assert guest_uploads.upload_store_mode() == "legacy"

    monkeypatch.setenv("LEAF_DRAWING_STORE", "typo")
    with pytest.raises(RuntimeError, match="LEAF_DRAWING_STORE"):
        store.authority_mode()

    monkeypatch.setenv("LEAF_DRAWING_STORE", "legacy")
    monkeypatch.setenv("LEAF_UPLOAD_STORE", "postgres")
    with pytest.raises(RuntimeError, match="requires LEAF_DRAWING_STORE"):
        guest_uploads.upload_store_mode()


def test_legacy_manifest_and_checkout_contract_is_unchanged(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_DRAWING_STORE", "legacy")
    backend = store.InMemoryBackend()
    source = tmp_path / "drawing.dwg"
    source.write_bytes(b"v1")
    store.ingest_drawing(backend, "tenant-a", str(source), drawing_id="drawing-a")
    assert store.acquire_checkout(
        backend, "tenant-a", "drawing-a", "holder-a", 30)
    assert not store.acquire_checkout(
        backend, "tenant-a", "drawing-a", "holder-b", 30)
    manifest = store.load_manifest(backend, "tenant-a", "drawing-a")
    assert manifest["head"] == manifest["latest"] == 1
    assert manifest["checkout"]["holder"] == "holder-a"


def test_filesystem_immutable_publish_never_replaces_concurrent_winner(tmp_path):
    backend = store.FilesystemBackend(str(tmp_path / "store"))
    key = "tenants/t/drawings/d/v/00000001.intake.json"
    barrier = threading.Barrier(2)
    successes, conflicts = [], []

    def publish(payload):
        barrier.wait(timeout=5)
        try:
            backend.put_if_absent_or_verify(key, payload)
            successes.append(payload)
        except ValueError:
            conflicts.append(payload)

    threads = [
        threading.Thread(target=publish, args=(payload,))
        for payload in (b"first", b"second")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert len(successes) == 1
    assert len(conflicts) == 1
    assert backend.get(key) == successes[0]
    assert not list((tmp_path / "store").rglob(".leaf-immutable-*"))


def test_migration_declares_fences_cas_and_receipts():
    sql = (
        Path(__file__).resolve().parents[2]
        / "platform" / "migrations" / "0016_drawing_upload_authority.sql"
    ).read_text(encoding="utf-8")
    for required in (
        "drawing_store_manifests",
        "drawing_store_versions",
        "UNIQUE (object_key)",
        "extraction_fence",
        "purge_fence",
        "checkout_fence",
        "reservation_token",
        "drawing_purge_receipts",
    ):
        assert required in sql


requires_database = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="PostgreSQL race test requires explicit DATABASE_URL",
)


@pytest.fixture
def postgres_authority(monkeypatch):
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("PostgreSQL race test requires explicit DATABASE_URL")
    db = store._db()
    db.apply_migration()
    monkeypatch.setenv("LEAF_DRAWING_STORE", "postgres")
    monkeypatch.setenv("LEAF_UPLOAD_STORE", "postgres")
    yield db
    db.reset_pool()
    store._platform_db = None


def _temp_blob(data: bytes) -> str:
    handle, name = tempfile.mkstemp(suffix=".dwg")
    with os.fdopen(handle, "wb") as stream:
        stream.write(data)
    return name


@pytest.fixture
def drawing_reconciler():
    import importlib.util
    path = Path(__file__).resolve().parents[2] / "scripts" / "reconcile_drawing_authority.py"
    spec = importlib.util.spec_from_file_location("drawing_reconciler", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def drained_drawing(tmp_path):
    import json
    token = uuid.uuid4().hex
    tenant, drawing = f"backfill-{token}", f"drawing-{token}"
    root = tmp_path / "legacy"
    versions = []
    blobs = {}
    for number in (1, 2):
        data = f"immutable drawing {number}".encode()
        key = store.drawing_version_key(tenant, drawing, number)
        path = root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        blobs[key] = data
        versions.append({
            "v": number, "parent": number - 1 if number > 1 else None,
            "created": f"2026-09-01T12:00:0{number}+00:00",
            "ready_at": f"2026-09-01T12:01:0{number}+00:00",
            "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "workitem_id": f"workitem-{number}", "tool": "fixture-tool",
            "note": "initial ingest" if number == 1 else "authored change",
            "source_ref": "a" * 64 if number == 2 else None,
        })
    manifest = {
        "schema": 1, "tenant_id": tenant, "drawing_id": drawing,
        "head": 1, "latest": 2, "checkout": None, "checkout_fence": 17,
        "created_at": "2026-09-01T12:00:01+00:00",
        "updated_at": "2026-09-01T12:02:00+00:00", "versions": versions,
    }
    path = root / store.manifest_key(tenant, drawing)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return root, tenant, drawing, manifest, blobs


def _reconcile_fixture(reconciler, fixture, database, mode="backfill", **kwargs):
    root, tenant, drawing, _manifest, _blobs = fixture
    return reconciler.reconcile(
        mode=mode, source_dir=root, tenant_id=tenant, drawing_id=drawing,
        database=database, **kwargs,
    )


@requires_database
def test_drawing_backfill_repeat_reverse_reimport_preserves_metadata_and_blobs(
    postgres_authority, drawing_reconciler, drained_drawing, tmp_path,
):
    import json
    root, tenant, drawing, manifest, blobs = drained_drawing
    first = _reconcile_fixture(drawing_reconciler, drained_drawing, postgres_authority)
    assert first["inserted"] == {"manifests": 1, "versions": 2}
    second = _reconcile_fixture(drawing_reconciler, drained_drawing, postgres_authority)
    assert second["inserted_total"] == 0
    assert second["parity"]
    assert _reconcile_fixture(drawing_reconciler, drained_drawing, postgres_authority, "parity")["parity"]
    output = tmp_path / "reverse"
    _reconcile_fixture(drawing_reconciler, drained_drawing, postgres_authority,
                       "reverse", output_dir=output)
    exported = json.loads((output / store.manifest_key(tenant, drawing)).read_text())
    expected = drawing_reconciler.validate_manifest(
        manifest, tenant_id=tenant, drawing_id=drawing, blob_dir=root,
    )
    assert exported == expected
    assert list(output.rglob("*.dwg")) == []
    with postgres_authority.transaction() as conn:
        conn.execute("DELETE FROM drawing_store_manifests WHERE tenant_id = %s AND drawing_id = %s",
                     (tenant, drawing))
    restored = drawing_reconciler.reconcile(
        mode="backfill", source_dir=output, blob_dir=root, tenant_id=tenant,
        drawing_id=drawing, database=postgres_authority,
    )
    assert restored["inserted_total"] == 3
    assert drawing_reconciler.reconcile(
        mode="parity", source_dir=output, blob_dir=root, tenant_id=tenant,
        drawing_id=drawing, database=postgres_authority,
    )["parity"]
    for key, original in blobs.items():
        assert (root / key).read_bytes() == original
        assert hashlib.sha256((root / key).read_bytes()).digest() == hashlib.sha256(original).digest()


@requires_database
def test_drawing_backfill_conflict_rolls_back_and_does_not_overwrite(
    postgres_authority, drawing_reconciler, drained_drawing,
):
    import json
    root, tenant, drawing, manifest, _blobs = drained_drawing
    _reconcile_fixture(drawing_reconciler, drained_drawing, postgres_authority)
    manifest["versions"][1]["note"] = "conflicting note"
    (root / store.manifest_key(tenant, drawing)).write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="version conflict"):
        _reconcile_fixture(drawing_reconciler, drained_drawing, postgres_authority)
    assert not _reconcile_fixture(drawing_reconciler, drained_drawing, postgres_authority, "parity")["parity"]
    with postgres_authority.cursor() as cur:
        rows = cur.execute(
            "SELECT note FROM drawing_store_versions WHERE tenant_id = %s AND drawing_id = %s ORDER BY version",
            (tenant, drawing),
        ).fetchall()
    assert [row["note"] for row in rows] == ["initial ingest", "authored change"]


@requires_database
def test_drawing_backfill_accepts_real_legacy_manifest_and_defaults_reverse_to_sibling(
    postgres_authority, drawing_reconciler, monkeypatch, tmp_path,
):
    import json
    token = uuid.uuid4().hex
    tenant, drawing = f"legacy-{token}", f"drawing-{token}"
    root = tmp_path / "legacy"
    backend = store.FilesystemBackend(str(root))
    original = tmp_path / "drawing.dwg"
    original.write_bytes(b"real legacy ingest")
    monkeypatch.setenv("LEAF_DRAWING_STORE", "legacy")
    store.ingest_drawing(backend, tenant, str(original), drawing_id=drawing)
    source_bytes = (root / store.manifest_key(tenant, drawing)).read_bytes()
    args = dict(source_dir=root, tenant_id=tenant, drawing_id=drawing, database=postgres_authority)
    assert drawing_reconciler.reconcile(mode="backfill", **args)["inserted_total"] == 2
    assert drawing_reconciler.reconcile(mode="backfill", **args)["inserted_total"] == 0
    exported = drawing_reconciler.reconcile(mode="reverse", **args)
    output = root.with_name("legacy-drawing-reverse")
    assert exported["output_dir"] == str(output.resolve())
    manifest = json.loads((output / store.manifest_key(tenant, drawing)).read_text())
    assert manifest["checkout_fence"] == 0
    assert manifest["versions"][0]["source_ref"] is None
    assert (root / store.manifest_key(tenant, drawing)).read_bytes() == source_bytes
    assert backend.get(store.drawing_version_key(tenant, drawing, 1)) == b"real legacy ingest"
    assert os.environ["LEAF_DRAWING_STORE"] == "legacy"


@requires_database
@pytest.mark.parametrize("defect", ["blob", "intake"])
def test_drawing_reverse_rejects_corruption_and_unsupported_proof_without_output(
    postgres_authority, drawing_reconciler, drained_drawing, tmp_path, defect,
):
    root, tenant, drawing, _manifest, _blobs = drained_drawing
    _reconcile_fixture(drawing_reconciler, drained_drawing, postgres_authority)
    if defect == "blob":
        (root / store.drawing_version_key(tenant, drawing, 2)).write_bytes(b"corrupted fixture")
    else:
        with postgres_authority.transaction() as conn:
            conn.execute(
                "UPDATE drawing_store_versions SET intake_ref='unsupported-proof', intake_sha256=%s "
                "WHERE tenant_id=%s AND drawing_id=%s AND version=2",
                ("b" * 64, tenant, drawing),
            )
    output = tmp_path / "reverse"
    with pytest.raises(ValueError, match="hash/size|unsupported"):
        _reconcile_fixture(drawing_reconciler, drained_drawing, postgres_authority,
                           "reverse", output_dir=output)
    assert not output.exists()


@requires_database
def test_drawing_backfill_late_unique_conflict_commits_no_partial_drawing(
    postgres_authority, drawing_reconciler, drained_drawing,
):
    root, tenant, drawing, manifest, blobs = drained_drawing
    other = f"other-{uuid.uuid4().hex}"
    # A different scoped row occupying the second immutable key causes a real
    # SQL failure after the manifest and first version have been inserted.
    with postgres_authority.transaction() as conn:
        conn.execute("INSERT INTO drawing_store_manifests (tenant_id,drawing_id,head,latest) VALUES (%s,%s,1,1)",
                     (other, drawing))
        entry = manifest["versions"][1]
        conn.execute(
            "INSERT INTO drawing_store_versions (tenant_id,drawing_id,version,object_key,byte_count,content_sha256,state) "
            "VALUES (%s,%s,1,%s,%s,%s,'ready')",
            (other, drawing, store.drawing_version_key(tenant, drawing, 2), entry["bytes"], entry["sha256"]),
        )
    import psycopg
    with pytest.raises(psycopg.errors.UniqueViolation):
        _reconcile_fixture(drawing_reconciler, drained_drawing, postgres_authority)
    with postgres_authority.cursor() as cur:
        assert cur.execute("SELECT 1 FROM drawing_store_manifests WHERE tenant_id = %s AND drawing_id = %s",
                           (tenant, drawing)).fetchone() is None
        assert cur.execute("SELECT 1 FROM drawing_store_versions WHERE tenant_id = %s AND drawing_id = %s",
                           (tenant, drawing)).fetchone() is None
        assert cur.execute("SELECT 1 FROM drawing_store_versions WHERE tenant_id = %s AND drawing_id = %s",
                           (other, drawing)).fetchone() is not None
    assert all((root / key).read_bytes() == data for key, data in blobs.items())


@requires_database
@pytest.mark.parametrize("defect", [
    "unsupported", "scope", "checkout", "reservation", "size", "hash", "parent", "source_ref", "timestamp", "missing_blob",
])
def test_drawing_backfill_invalid_input_fails_before_mutation(
    postgres_authority, drawing_reconciler, drained_drawing, defect,
):
    import json
    root, tenant, drawing, manifest, blobs = drained_drawing
    if defect == "unsupported":
        manifest["extra"] = {"silently_lost": True}
    elif defect == "scope":
        manifest["tenant_id"] = "another-tenant"
    elif defect == "checkout":
        manifest["checkout"] = {"holder": "writer", "expires": "2099-01-01T00:00:00Z"}
    elif defect == "reservation":
        manifest["versions"][1]["reservation_token"] = "live-owner"
    elif defect == "size":
        manifest["versions"][1]["bytes"] += 1
    elif defect == "hash":
        manifest["versions"][1]["sha256"] = "0" * 64
    elif defect == "parent":
        manifest["versions"][1]["parent"] = 2
    elif defect == "source_ref":
        manifest["versions"][1]["source_ref"] = {"unexpected": "object"}
    elif defect == "timestamp":
        manifest["versions"][1]["created"] = "2026-09-01T12:00:00"
    elif defect == "missing_blob":
        (root / store.drawing_version_key(tenant, drawing, 2)).unlink()
    (root / store.manifest_key(tenant, drawing)).write_text(json.dumps(manifest))
    with pytest.raises((ValueError, FileNotFoundError)):
        _reconcile_fixture(drawing_reconciler, drained_drawing, postgres_authority)
    with postgres_authority.cursor() as cur:
        assert cur.execute("SELECT 1 FROM drawing_store_manifests WHERE tenant_id = %s AND drawing_id = %s",
                           (tenant, drawing)).fetchone() is None
    for key, data in blobs.items():
        if (root / key).exists():
            assert (root / key).read_bytes() == data


@requires_database
@pytest.mark.parametrize("lease", ["checkout", "reservation"])
def test_drawing_reverse_and_parity_fail_closed_on_database_leases(
    postgres_authority, drawing_reconciler, drained_drawing, tmp_path, lease,
):
    root, tenant, drawing, _manifest, _blobs = drained_drawing
    _reconcile_fixture(drawing_reconciler, drained_drawing, postgres_authority)
    with postgres_authority.transaction() as conn:
        if lease == "checkout":
            conn.execute(
                "UPDATE drawing_store_manifests SET checkout_holder='writer', checkout_acquired_at=NOW(), "
                "checkout_expires_at=NOW()+INTERVAL '1 hour' WHERE tenant_id=%s AND drawing_id=%s",
                (tenant, drawing),
            )
        else:
            conn.execute(
                "UPDATE drawing_store_versions SET reservation_token='writer', "
                "reservation_expires_at=NOW()+INTERVAL '1 hour' WHERE tenant_id=%s AND drawing_id=%s AND version=2",
                (tenant, drawing),
            )
    output = tmp_path / "reverse"
    for mode in ("backfill", "parity", "reverse"):
        with pytest.raises(ValueError, match="drained"):
            _reconcile_fixture(drawing_reconciler, drained_drawing, postgres_authority, mode, output_dir=output)
    assert not output.exists()


@requires_database
def test_drawing_reverse_rejects_source_output_overlap(
    postgres_authority, drawing_reconciler, drained_drawing,
):
    root, _tenant, _drawing, _manifest, _blobs = drained_drawing
    with pytest.raises(ValueError, match="separate output tree"):
        _reconcile_fixture(drawing_reconciler, drained_drawing, postgres_authority,
                           "reverse", output_dir=root / "exports")


def test_live_write_manifest_probe_uses_selected_authority(monkeypatch):
    tenant, drawing = "authority-tenant", "authority-drawing"
    backend = store.InMemoryBackend()
    source = b"AC1032" + b"\x00" * 32
    backend.put(store.drawing_version_key(tenant, drawing, 1), source)
    write_loop.publish_intake_cache(
        backend, tenant, drawing, 1, source,
        {"dwg": drawing, "layers": ["Panels"], "polylines": [{
            "handle": "A", "layer": "Panels", "closed": True,
            "pts": [[0, 0, 0], [1, 0, 0], [1, 1, 0]], "xdata": None,
        }]},
    )
    manifest = {
        "schema": 1, "tenant_id": tenant, "drawing_id": drawing,
        "head": 1, "latest": 1, "versions": [{"v": 1}], "checkout": None,
    }
    marker_reads = []
    monkeypatch.setattr(store, "load_manifest", lambda *_args: manifest)
    monkeypatch.setattr(store, "authorize_checkout", lambda *_args: None)

    def read_marker(_backend, marker_tenant, marker_drawing):
        marker_reads.append((marker_tenant, marker_drawing))
        return {"status": "ready", "source_ext": ".dwg"}

    monkeypatch.setattr(guest_uploads, "read_marker", read_marker)

    class _ReachedDa:
        def ephemeral_input_key(self, *_args, **_kwargs):
            raise RuntimeError("authority-aware guards passed")

    env, status = write_loop.run_write_live(
        {"name": "authority-probe"}, {"drawing_id": drawing}, tenant,
        backend=backend, da=_ReachedDa(), t0=time.perf_counter(),
        run_tool_dynamic_fn=lambda *_args, **_kwargs: {
            "ok": True, "result": {"mutations": {"removed": ["A"]}},
        },
    )

    assert status == 502
    assert env["error"]["message"] == "live drawing mutation failed"
    assert "authority-aware guards passed" not in str(env)
    assert marker_reads == [(tenant, drawing), (tenant, drawing)]


def test_live_write_marker_probe_uses_selected_authority(monkeypatch):
    tenant, drawing = "marker-tenant", "marker-drawing"
    backend = store.InMemoryBackend()
    backend.put(
        store.manifest_key(tenant, drawing),
        b'{"schema":1,"tenant_id":"marker-tenant",'
        b'"drawing_id":"marker-drawing","head":1,"latest":1,'
        b'"versions":[],"checkout":null}',
    )
    monkeypatch.setattr(
        guest_uploads, "read_marker",
        lambda *_args: {"status": "ready", "source_ext": ".dxf"},
    )

    class _NeverCalledDa:
        def __getattr__(self, name):
            raise AssertionError(f"da.{name} must not be reached")

    env, status = write_loop.run_write_live(
        {"name": "authority-probe"}, {"drawing_id": drawing}, tenant,
        backend=backend, da=_NeverCalledDa(), t0=time.perf_counter(),
    )

    assert status == 400
    assert "DWG source" in env["error"]["message"]


class _LiveWriteDa:
    def __init__(self, source: bytes):
        self.source = source
        self.staged = {}
        self.submissions = 0

    def ephemeral_input_key(self, name, tenant_id, ts):
        return f"t/{tenant_id}/in/{ts}_{name}"

    def upload_scratch_object(self, local_path, key):
        self.staged[key] = Path(local_path).read_bytes()

    def scratch_signed_download_url(self, key):
        assert self.staged[key] == self.source or self.staged[key].startswith(
            b"LEAF_MUTATION_PLAN|1\n")
        return "https://aps.test/input"

    def scratch_signed_upload_url(self, key):
        return "upload-key", "https://aps.test/output"

    def activity_qualified(self, name):
        return f"owner.{name}+prod"

    def submit_workitem(self, *_args, **_kwargs):
        self.submissions += 1
        return {"id": "wi-postgres-authority", "status": "success"}

    def finalize_scratch_upload(self, _object_key, upload_key):
        assert upload_key == "upload-key"

    def download_scratch_object(self, object_key):
        if object_key.endswith(".txt"):
            return b"LAYER|Updated\n"
        return self.source + b"-updated"

    def delete_scratch_object(self, _object_key):
        pass

    def extract(self, local_path):
        raise AssertionError("live writes must not launch a second extraction")

    def _engine_seconds(self, _status):
        return 1.0


@requires_database
def test_live_write_uses_postgres_manifest_with_filesystem_blobs(
    postgres_authority, monkeypatch, tmp_path,
):
    token = uuid.uuid4().hex
    tenant, drawing = f"tenant-{token}", f"drawing-{token}"

    class UploadFinalizationBackend(store.InMemoryBackend):
        def put(self, key, data):
            if key == store.manifest_key(tenant, drawing):
                return
            super().put(key, data)

    backend = UploadFinalizationBackend()
    source = b"AC1032" + b"\x00" * 64
    initial = tmp_path / "initial.dwg"
    initial.write_bytes(source)
    store.ingest_drawing(backend, tenant, str(initial), drawing_id=drawing)
    write_loop.publish_intake_cache(
        backend, tenant, drawing, 1, source,
        {"dwg": drawing, "layers": ["Panels"], "polylines": [{
            "handle": "A", "layer": "Panels", "closed": True,
            "pts": [[0, 0, 0], [1, 0, 0], [1, 1, 0]], "xdata": None,
        }]},
    )
    assert store.acquire_checkout(backend, tenant, drawing, "session-a", 600)
    assert not backend.exists(store.manifest_key(tenant, drawing))
    assert not backend.exists(write_loop.upload_marker_key(tenant, drawing))

    marker_reads = []

    def read_marker(_backend, marker_tenant, marker_drawing):
        marker_reads.append((marker_tenant, marker_drawing))
        return {"status": "ready", "source_ext": ".dwg"}

    monkeypatch.setattr(guest_uploads, "read_marker", read_marker)
    da = _LiveWriteDa(source)
    env, status = write_loop.run_write_live(
        {"name": "postgres-live-write"}, {"drawing_id": drawing}, tenant,
        backend=backend, da=da, t0=time.perf_counter(), holder="session-a",
        run_tool_dynamic_fn=lambda *_args, **_kwargs: {
            "ok": True, "result": {"mutations": {"removed": ["A"]}},
        },
    )

    assert status == 200
    assert env["result"]["new_version"] == {
        "drawing_id": drawing, "version": 2, "parent": 1,
    }
    assert marker_reads
    assert set(marker_reads) == {(tenant, drawing)}
    assert da.submissions == 1
    assert store.load_manifest(backend, tenant, drawing)["head"] == 2


@requires_database
def test_live_write_unknown_postgres_drawing_fails_before_marker_or_da(
    postgres_authority, monkeypatch,
):
    token = uuid.uuid4().hex
    tenant, drawing = f"tenant-{token}", f"drawing-{token}"
    backend = store.InMemoryBackend()

    def marker_must_not_be_read(*_args):
        raise AssertionError("missing drawing must fail before upload marker lookup")

    class _NeverCalledDa:
        def __getattr__(self, name):
            raise AssertionError(f"da.{name} must not be reached")

    monkeypatch.setattr(guest_uploads, "read_marker", marker_must_not_be_read)
    env, status = write_loop.run_write_live(
        {"name": "postgres-live-write"}, {"drawing_id": drawing}, tenant,
        backend=backend, da=_NeverCalledDa(), t0=time.perf_counter(),
        holder="session-a",
    )

    assert status == 400
    assert env["error"]["error_code"] == "BAD_PARAMS"
    assert "drawing not in store" in env["error"]["message"]


@requires_database
def test_live_write_reads_postgres_dxf_marker_before_da(
    postgres_authority, monkeypatch, tmp_path,
):
    token = uuid.uuid4().hex
    tenant, drawing = f"tenant-{token}", f"drawing-{token}"
    backend = store.InMemoryBackend()
    initial = tmp_path / "initial.dwg"
    initial.write_bytes(b"AC1032" + b"\x00" * 32)
    store.ingest_drawing(backend, tenant, str(initial), drawing_id=drawing)

    monkeypatch.setattr(
        guest_uploads, "read_marker",
        lambda *_args: {"status": "ready", "source_ext": ".dxf"},
    )

    class _NeverCalledDa:
        def __getattr__(self, name):
            raise AssertionError(f"da.{name} must not be reached")

    env, status = write_loop.run_write_live(
        {"name": "postgres-live-write"}, {"drawing_id": drawing}, tenant,
        backend=backend, da=_NeverCalledDa(), t0=time.perf_counter(),
        holder="session-a",
    )

    assert status == 400
    assert "DWG source" in env["error"]["message"]


@requires_database
def test_pending_upload_row_blocks_demo_bootstrap(postgres_authority):
    token = uuid.uuid4().hex
    tenant, drawing = f"tenant-{token}", f"drawing-{token}"
    backend = store.InMemoryBackend()
    marker = guest_uploads.new_marker(
        filename="drawing.dxf", data=b"dxf", tenant_kind="account",
        source_ext=".dxf",
    )
    guest_uploads.write_marker(backend, tenant, drawing, marker)

    with pytest.raises(ValueError, match="refusing the demo-intake bootstrap"):
        write_loop.ensure_demo_drawing(backend, tenant, drawing)

    with postgres_authority.cursor() as cur:
        manifest = cur.execute(
            """
            SELECT 1 FROM drawing_store_manifests
            WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
            """,
            {"tenant": tenant, "drawing": drawing},
        ).fetchone()
        upload = cur.execute(
            """
            SELECT status FROM drawing_upload_attempts
            WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
            """,
            {"tenant": tenant, "drawing": drawing},
        ).fetchone()

    assert manifest is None
    assert upload["status"] == "extracting"
    assert backend.keys() == [
        write_loop.upload_marker_key(tenant, drawing)
    ]


@requires_database
def test_two_writers_reserve_unique_versions_and_one_head_wins(
    postgres_authority,
):
    token = uuid.uuid4().hex
    tenant, drawing = f"tenant-{token}", f"drawing-{token}"
    blob_barrier = threading.Barrier(2)

    class RacingBackend(store.InMemoryBackend):
        def put(self, key, data):
            if key.endswith(".dwg") and not key.endswith("00000001.dwg"):
                blob_barrier.wait(timeout=10)
            super().put(key, data)

    backend = RacingBackend()
    source = _temp_blob(b"v1")
    try:
        store.ingest_drawing(backend, tenant, source, drawing_id=drawing)
    finally:
        os.remove(source)
    assert store.acquire_checkout(
        backend, tenant, drawing, "writer", 60)

    barrier = threading.Barrier(2)
    successes, failures = [], []
    lock = threading.Lock()

    def writer(number: int):
        path = _temp_blob(f"v{number}".encode())
        try:
            barrier.wait(timeout=10)
            version = store.put_drawing(
                backend, tenant, drawing, path, 1, {"tool": "race"})
            with lock:
                successes.append(version)
        except ValueError as exc:
            with lock:
                failures.append(str(exc))
        finally:
            os.remove(path)

    threads = [threading.Thread(target=writer, args=(n,)) for n in (2, 3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert all(not thread.is_alive() for thread in threads)
    assert len(successes) == 1
    assert len(failures) == 1
    with postgres_authority.cursor() as cur:
        cur.execute(
            """
            SELECT version, state FROM drawing_store_versions
            WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
            ORDER BY version
            """,
            {"tenant": tenant, "drawing": drawing},
        )
        rows = cur.fetchall()
    assert [int(row["version"]) for row in rows] == [1, 2, 3]
    assert sum(row["state"] == "ready" for row in rows) == 2
    assert sum(row["state"] == "orphaned" for row in rows) == 1


@requires_database
def test_two_tasks_share_one_checkout_and_one_extraction_lease(
    postgres_authority, monkeypatch,
):
    token = uuid.uuid4().hex
    tenant, drawing = f"tenant-{token}", f"drawing-{token}"
    backend = store.InMemoryBackend()
    source = _temp_blob(b"v1")
    try:
        store.ingest_drawing(backend, tenant, source, drawing_id=drawing)
    finally:
        os.remove(source)

    barrier = threading.Barrier(2)
    results = []

    def checkout(holder: str):
        barrier.wait(timeout=10)
        results.append(
            store.acquire_checkout(
                backend, tenant, drawing, holder, 60))

    threads = [
        threading.Thread(target=checkout, args=(holder,))
        for holder in ("holder-a", "holder-b")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert sorted(results) == [False, True]

    marker = guest_uploads.new_marker(
        filename="drawing.dxf", data=b"dxf", tenant_kind="account",
        source_ext=".dxf")
    guest_uploads.write_marker(backend, tenant, drawing, marker)
    claims = []
    barrier = threading.Barrier(2)

    def claim():
        barrier.wait(timeout=10)
        claims.append(guest_uploads._claim_extraction(tenant, drawing))

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert sum(item is not None for item in claims) == 1


@requires_database
def test_fenced_intake_publication_persists_digest_proof(
    postgres_authority,
):
    import hashlib

    token = uuid.uuid4().hex
    tenant, drawing = f"tenant-{token}", f"drawing-{token}"
    backend = store.InMemoryBackend()
    marker = guest_uploads.new_marker(
        filename="drawing.dxf", data=b"dxf", tenant_kind="account",
        source_ext=".dxf")
    guest_uploads.write_marker(backend, tenant, drawing, marker)
    claimed, owner, fence = guest_uploads._claim_extraction(tenant, drawing)
    source = _temp_blob(b"stored-version")
    try:
        store.ingest_drawing(
            backend, tenant, source, drawing_id=drawing,
            authority_guard={
                "attempt": claimed["attempt"], "owner": owner, "fence": fence,
            })
    finally:
        os.remove(source)

    intake_data = b'{"layers":["Panels"],"polylines":[]}'
    intake_ref = write_loop.intake_cache_key(tenant, drawing, 1)
    digest = guest_uploads._put_intake_cache_fenced(
        backend, tenant, drawing, intake_ref, intake_data,
        str(claimed["attempt"]), owner, fence)
    assert digest == hashlib.sha256(intake_data).hexdigest()
    with postgres_authority.cursor() as cur:
        cur.execute(
            "SELECT intake_ref, intake_sha256 FROM drawing_store_versions "
            "WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s "
            "AND version = 1",
            {"tenant": tenant, "drawing": drawing},
        )
        proof = cur.fetchone()
    assert proof == {"intake_ref": intake_ref, "intake_sha256": digest}


@requires_database
def test_ready_upload_finalize_is_atomic_and_cache_failure_stays_invisible(
    postgres_authority,
):
    token = uuid.uuid4().hex
    tenant, drawing = f"tenant-{token}", f"drawing-{token}"
    intake_ref = write_loop.intake_cache_key(tenant, drawing, 1)

    class FailingCacheBackend(store.InMemoryBackend):
        fail_cache = True
        fail_sentinel = False

        def put_if_absent_or_verify(self, key, data):
            if self.fail_cache and key == intake_ref:
                raise OSError("cache write failed")
            super().put_if_absent_or_verify(key, data)

        def put(self, key, data):
            if self.fail_sentinel and key == write_loop.upload_marker_key(
                tenant, drawing
            ):
                raise RuntimeError("compatibility sentinel failed")
            super().put(key, data)

    backend = FailingCacheBackend()
    marker = guest_uploads.new_marker(
        filename="drawing.dxf", data=b"dxf", tenant_kind="account",
        source_ext=".dxf")
    guest_uploads.write_marker(backend, tenant, drawing, marker)
    claimed, owner, fence = guest_uploads._claim_extraction(tenant, drawing)
    source = _temp_blob(b"stored-version")
    try:
        store.ingest_drawing(
            backend, tenant, source, drawing_id=drawing,
            authority_guard={
                "attempt": claimed["attempt"],
                "owner": owner,
                "fence": fence,
                "defer_ready": True,
            },
        )
    finally:
        os.remove(source)

    intake_data = b'{"layers":[],"polylines":[]}'
    ready = dict(
        claimed,
        status="ready",
        extracted_version=1,
        intake_ref=intake_ref,
        intake_sha256=hashlib.sha256(intake_data).hexdigest(),
    )
    with pytest.raises(OSError, match="cache write failed"):
        guest_uploads._finalize_pg_ready_attempt(
            backend, tenant, drawing, ready, owner, fence,
            intake_ref, intake_data,
        )
    with postgres_authority.cursor() as cur:
        cur.execute(
            """
            SELECT m.head, v.state, u.status
            FROM drawing_store_manifests m
            JOIN drawing_store_versions v
              ON v.tenant_id = m.tenant_id AND v.drawing_id = m.drawing_id
             AND v.version = 1
            JOIN drawing_upload_attempts u
              ON u.tenant_id = m.tenant_id AND u.drawing_id = m.drawing_id
            WHERE m.tenant_id = %(tenant)s AND m.drawing_id = %(drawing)s
            """,
            {"tenant": tenant, "drawing": drawing},
        )
        failed = cur.fetchone()
    assert failed == {"head": None, "state": "reserved", "status": "extracting"}

    backend.fail_cache = False
    backend.fail_sentinel = True
    digest = guest_uploads._finalize_pg_ready_attempt(
        backend, tenant, drawing, ready, owner, fence,
        intake_ref, intake_data,
    )
    with postgres_authority.cursor() as cur:
        cur.execute(
            """
            SELECT m.head, v.state, v.intake_ref, v.intake_sha256, u.status
            FROM drawing_store_manifests m
            JOIN drawing_store_versions v
              ON v.tenant_id = m.tenant_id AND v.drawing_id = m.drawing_id
             AND v.version = 1
            JOIN drawing_upload_attempts u
              ON u.tenant_id = m.tenant_id AND u.drawing_id = m.drawing_id
            WHERE m.tenant_id = %(tenant)s AND m.drawing_id = %(drawing)s
            """,
            {"tenant": tenant, "drawing": drawing},
        )
        committed = cur.fetchone()
    assert committed == {
        "head": 1,
        "state": "ready",
        "intake_ref": intake_ref,
        "intake_sha256": digest,
        "status": "ready",
    }


@requires_database
def test_run_extraction_retries_same_attempt_after_cache_publish_failure(
    postgres_authority, monkeypatch, tmp_path,
):
    import deps

    token = uuid.uuid4().hex
    tenant, drawing = f"tenant-{token}", f"drawing-{token}"
    intake_ref = write_loop.intake_cache_key(tenant, drawing, 1)

    class FailingCacheBackend(store.InMemoryBackend):
        fail_cache = True

        def put_if_absent_or_verify(self, key, data):
            if self.fail_cache and key == intake_ref:
                raise OSError("cache write failed")
            super().put_if_absent_or_verify(key, data)

    backend = FailingCacheBackend()
    marker = guest_uploads.new_marker(
        filename="drawing.dwg", data=b"DWG", tenant_kind="account",
        source_ext=".dwg")
    guest_uploads.write_marker(backend, tenant, drawing, marker)
    monkeypatch.setenv("LEAF_UPLOADS_DIR", str(tmp_path / "uploads"))
    staged = guest_uploads.staged_path(tenant, drawing, ".dwg")
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"DWG")
    monkeypatch.setattr(
        write_loop, "upload_backend_for_tenant", lambda _tenant: backend)
    monkeypatch.setattr(deps, "APS_LIVE", True)
    attempts = []

    def broker_extract(_tenant, _drawing, attempt):
        attempts.append(attempt)
        return {"layers": [], "polylines": []}

    monkeypatch.setattr(guest_uploads, "_extract_via_broker", broker_extract)
    guest_uploads.run_extraction(tenant, drawing, ".dwg")

    after_failure = guest_uploads.read_marker(backend, tenant, drawing)
    assert after_failure["status"] == "extracting"
    assert after_failure["attempt"] == marker["attempt"]
    with postgres_authority.cursor() as cur:
        cur.execute(
            """
            SELECT m.head, v.state, u.status, u.extraction_owner
            FROM drawing_store_manifests m
            JOIN drawing_store_versions v
              ON v.tenant_id = m.tenant_id AND v.drawing_id = m.drawing_id
             AND v.version = 1
            JOIN drawing_upload_attempts u
              ON u.tenant_id = m.tenant_id AND u.drawing_id = m.drawing_id
            WHERE m.tenant_id = %(tenant)s AND m.drawing_id = %(drawing)s
            """,
            {"tenant": tenant, "drawing": drawing},
        )
        failed = cur.fetchone()
    assert failed == {
        "head": None,
        "state": "reserved",
        "status": "extracting",
        "extraction_owner": None,
    }

    backend.fail_cache = False
    guest_uploads.run_extraction(tenant, drawing, ".dwg")
    ready = guest_uploads.read_marker(backend, tenant, drawing)
    assert ready["status"] == "ready"
    assert ready["attempt"] == marker["attempt"]
    assert attempts == [marker["attempt"], marker["attempt"]]


@requires_database
def test_stale_finalizer_cannot_replace_new_fence_cache_winner(
    postgres_authority, tmp_path,
):
    token = uuid.uuid4().hex
    tenant, drawing = f"tenant-{token}", f"drawing-{token}"
    intake_ref = write_loop.intake_cache_key(tenant, drawing, 1)
    stale_checked = threading.Event()
    resume_stale = threading.Event()

    class PausingBackend(store.FilesystemBackend):
        def put_if_absent_or_verify(self, key, data):
            if (
                key == intake_ref
                and threading.current_thread().name == "stale-finalizer"
            ):
                stale_checked.set()
                assert resume_stale.wait(10)
            return super().put_if_absent_or_verify(key, data)

    backend = PausingBackend(str(tmp_path / "store"))
    marker = guest_uploads.new_marker(
        filename="drawing.dwg", data=b"DWG", tenant_kind="account",
        source_ext=".dwg")
    guest_uploads.write_marker(backend, tenant, drawing, marker)
    claimed, old_owner, old_fence = guest_uploads._claim_extraction(
        tenant, drawing)
    source = _temp_blob(b"stored-version")
    try:
        store.ingest_drawing(
            backend, tenant, source, drawing_id=drawing,
            authority_guard={
                "attempt": claimed["attempt"],
                "owner": old_owner,
                "fence": old_fence,
                "defer_ready": True,
            },
        )
    finally:
        os.remove(source)

    stale_data = b'{"layers":["stale"],"polylines":[]}'
    fresh_data = b'{"layers":["fresh"],"polylines":[]}'

    def ready_marker(data):
        return dict(
            claimed,
            status="ready",
            extracted_version=1,
            intake_ref=intake_ref,
            intake_sha256=hashlib.sha256(data).hexdigest(),
        )

    stale_errors = []

    def stale_finalize():
        try:
            guest_uploads._finalize_pg_ready_attempt(
                backend, tenant, drawing, ready_marker(stale_data),
                old_owner, old_fence, intake_ref, stale_data,
            )
        except Exception as exc:  # expected immutable conflict
            stale_errors.append(exc)

    stale_thread = threading.Thread(
        target=stale_finalize, name="stale-finalizer")
    stale_thread.start()
    assert stale_checked.wait(10)
    with postgres_authority.connection() as conn:
        conn.execute(
            """
            UPDATE drawing_upload_attempts
            SET extraction_expires_at = NOW() - INTERVAL '1 second'
            WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
            """,
            {"tenant": tenant, "drawing": drawing},
        )
    current, new_owner, new_fence = guest_uploads._claim_extraction(
        tenant, drawing)
    fresh_digest = guest_uploads._finalize_pg_ready_attempt(
        backend, tenant, drawing, ready_marker(fresh_data),
        new_owner, new_fence, intake_ref, fresh_data,
    )
    resume_stale.set()
    stale_thread.join(10)

    assert stale_errors and isinstance(stale_errors[0], ValueError)
    assert backend.get(intake_ref) == fresh_data
    with postgres_authority.cursor() as cur:
        cur.execute(
            """
            SELECT v.intake_sha256, u.marker->>'intake_sha256' AS marker_sha
            FROM drawing_store_versions v
            JOIN drawing_upload_attempts u
              ON u.tenant_id = v.tenant_id AND u.drawing_id = v.drawing_id
            WHERE v.tenant_id = %(tenant)s AND v.drawing_id = %(drawing)s
              AND v.version = 1
            """,
            {"tenant": tenant, "drawing": drawing},
        )
        proof = cur.fetchone()
    assert proof == {"intake_sha256": fresh_digest, "marker_sha": fresh_digest}


@requires_database
def test_stale_extraction_completion_is_fenced(postgres_authority):
    token = uuid.uuid4().hex
    tenant, drawing = f"tenant-{token}", f"drawing-{token}"
    backend = store.InMemoryBackend()
    marker = guest_uploads.new_marker(
        filename="drawing.dxf", data=b"dxf", tenant_kind="account",
        source_ext=".dxf")
    guest_uploads.write_marker(backend, tenant, drawing, marker)
    first_marker, first_owner, first_fence = guest_uploads._claim_extraction(
        tenant, drawing)
    with postgres_authority.connection() as conn:
        conn.execute(
            """
            UPDATE drawing_upload_attempts
            SET extraction_expires_at = NOW() - INTERVAL '1 second'
            WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
            """,
            {"tenant": tenant, "drawing": drawing},
        )
    second_marker, second_owner, second_fence = guest_uploads._claim_extraction(
        tenant, drawing)
    stale_cache_key = write_loop.intake_cache_key(tenant, drawing, 1)
    with pytest.raises(RuntimeError, match="lease is stale"):
        guest_uploads._put_intake_cache_fenced(
            backend, tenant, drawing, stale_cache_key, b"stale",
            str(first_marker["attempt"]), first_owner, first_fence)
    assert not backend.exists(stale_cache_key)
    stale = dict(first_marker, status="failed")
    current = dict(second_marker, status="ready", extracted_version=1)
    assert not guest_uploads._finish_pg_attempt(
        tenant, drawing, stale, first_owner, first_fence)
    assert guest_uploads._finish_pg_attempt(
        tenant, drawing, current, second_owner, second_fence)
    assert guest_uploads.read_marker(
        backend, tenant, drawing)["status"] == "ready"


@requires_database
def test_two_purge_workers_emit_one_deletion_receipt(
    postgres_authority, monkeypatch, tmp_path,
):
    token = uuid.uuid4().hex
    tenant, drawing = f"guest-{token}", f"drawing-{token}"
    guest_root = tmp_path / "guest"
    uploads_root = tmp_path / "uploads"
    monkeypatch.setenv("LEAF_GUEST_STORE_DIR", str(guest_root))
    monkeypatch.setenv("LEAF_UPLOADS_DIR", str(uploads_root))
    backend = store.FilesystemBackend(str(guest_root))
    marker = guest_uploads.new_marker(
        filename="drawing.dxf", data=b"dxf", tenant_kind="guest",
        source_ext=".dxf")
    now = datetime.now(timezone.utc)
    marker["retention_expires_at"] = (
        now - timedelta(seconds=1)).isoformat()
    guest_uploads.write_marker(backend, tenant, drawing, marker)
    staged = guest_uploads.staged_path(tenant, drawing, ".dxf")
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"dxf")

    barrier = threading.Barrier(2)
    results = []

    def purge():
        barrier.wait(timeout=10)
        results.append(guest_uploads.purge_expired(now))

    threads = [threading.Thread(target=purge) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert all(not thread.is_alive() for thread in threads)
    target_purges = [
        item for result in results for item in result["purged"]
        if item == {"tenant_id": tenant, "drawing_id": drawing}
    ]
    assert len(target_purges) == 1
    assert guest_uploads.read_marker(backend, tenant, drawing) is None
    with postgres_authority.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) AS count FROM drawing_purge_receipts
            WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
              AND status = 'deleted'
            """,
            {"tenant": tenant, "drawing": drawing},
        )
        assert int(cur.fetchone()["count"]) == 1
    replacement = guest_uploads.new_marker(
        filename="drawing.dxf", data=b"dxf", tenant_kind="guest",
        source_ext=".dxf")
    guest_uploads.write_marker(backend, tenant, drawing, replacement)
    assert guest_uploads.read_marker(
        backend, tenant, drawing)["attempt"] == replacement["attempt"]


@requires_database
def test_initial_ingest_adopts_exact_expired_reservation(
    postgres_authority, tmp_path,
):
    token = uuid.uuid4().hex
    tenant, drawing = f"tenant-{token}", f"drawing-{token}"
    data = b"crash-window-v1"
    digest = __import__("hashlib").sha256(data).hexdigest()
    backend = store.InMemoryBackend()
    key = store.drawing_version_key(tenant, drawing, 1)
    backend.put(key, data)
    with postgres_authority.connection() as conn:
        conn.execute(
            """
            INSERT INTO drawing_store_manifests
              (tenant_id, drawing_id, head, latest)
            VALUES (%(tenant)s, %(drawing)s, NULL, 1)
            """,
            {"tenant": tenant, "drawing": drawing},
        )
        conn.execute(
            """
            INSERT INTO drawing_store_versions
              (tenant_id, drawing_id, version, object_key, byte_count,
               content_sha256, note, state, reservation_token,
               reservation_expires_at)
            VALUES
              (%(tenant)s, %(drawing)s, 1, %(key)s, %(bytes)s, %(sha)s,
               'initial ingest', 'reserved', 'dead-worker',
               NOW() - INTERVAL '1 second')
            """,
            {
                "tenant": tenant, "drawing": drawing, "key": key,
                "bytes": len(data), "sha": digest,
            },
        )
    source = tmp_path / "exact.dwg"
    source.write_bytes(data)
    assert store.ingest_drawing(
        backend, tenant, str(source), drawing_id=drawing)["version"] == 1
    assert store.load_manifest(backend, tenant, drawing)["head"] == 1

    other = f"drawing-mismatch-{token}"
    other_key = store.drawing_version_key(tenant, other, 1)
    backend.put(other_key, b"old")
    with postgres_authority.connection() as conn:
        conn.execute(
            """
            INSERT INTO drawing_store_manifests
              (tenant_id, drawing_id, head, latest)
            VALUES (%(tenant)s, %(drawing)s, NULL, 1)
            """,
            {"tenant": tenant, "drawing": other},
        )
        conn.execute(
            """
            INSERT INTO drawing_store_versions
              (tenant_id, drawing_id, version, object_key, byte_count,
               content_sha256, note, state, reservation_token,
               reservation_expires_at)
            VALUES
              (%(tenant)s, %(drawing)s, 1, %(key)s, 3, %(sha)s,
               'initial ingest', 'reserved', 'dead-worker',
               NOW() - INTERVAL '1 second')
            """,
            {
                "tenant": tenant, "drawing": other, "key": other_key,
                "sha": __import__("hashlib").sha256(b"old").hexdigest(),
            },
        )
    with pytest.raises(ValueError, match="does not match content"):
        store.ingest_drawing(
            backend, tenant, str(source), drawing_id=other)


@requires_database
def test_reacquired_checkout_fences_expired_writer(
    postgres_authority, tmp_path,
):
    token = uuid.uuid4().hex
    tenant, drawing = f"tenant-{token}", f"drawing-{token}"
    entered, release = threading.Event(), threading.Event()

    class DelayedBackend(store.InMemoryBackend):
        def put(self, key, data):
            if key.endswith("00000002.dwg"):
                entered.set()
                assert release.wait(timeout=10)
            super().put(key, data)

    backend = DelayedBackend()
    initial = tmp_path / "initial.dwg"
    initial.write_bytes(b"v1")
    store.ingest_drawing(backend, tenant, str(initial), drawing_id=drawing)
    assert store.acquire_checkout(
        backend, tenant, drawing, "old-holder", 60)
    update = tmp_path / "update.dwg"
    update.write_bytes(b"v2")
    errors = []

    def publish():
        try:
            store.put_drawing(
                backend, tenant, drawing, str(update), 1, {"tool": "race"})
        except ValueError as exc:
            errors.append(str(exc))

    thread = threading.Thread(target=publish)
    thread.start()
    assert entered.wait(timeout=10)
    with postgres_authority.connection() as conn:
        conn.execute(
            """
            UPDATE drawing_store_manifests
            SET checkout_expires_at = NOW() - INTERVAL '1 second'
            WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
            """,
            {"tenant": tenant, "drawing": drawing},
        )
    assert store.acquire_checkout(
        backend, tenant, drawing, "new-holder", 60)
    release.set()
    thread.join(timeout=20)
    assert errors and "head changed" in errors[0]
    manifest = store.load_manifest(backend, tenant, drawing)
    assert manifest["head"] == 1
    assert manifest["checkout"]["holder"] == "new-holder"
    assert manifest["checkout"]["fence"] == 2


@requires_database
def test_failed_retry_cleanup_is_attempt_cas_safe(
    postgres_authority, monkeypatch, tmp_path,
):
    token = uuid.uuid4().hex
    tenant, drawing = f"guest-{token}", f"drawing-{token}"
    guest_root = tmp_path / "guest"
    monkeypatch.setenv("LEAF_GUEST_STORE_DIR", str(guest_root))
    backend = store.FilesystemBackend(str(guest_root))
    marker = guest_uploads.new_marker(
        filename="drawing.dxf", data=b"dxf", tenant_kind="guest",
        source_ext=".dxf")
    marker["status"] = "failed"
    guest_uploads.write_marker(backend, tenant, drawing, marker)
    drawing_dir = guest_uploads.guest_drawing_dir(tenant, drawing)
    (drawing_dir / "v").mkdir(parents=True, exist_ok=True)
    (drawing_dir / "v" / "residue").write_bytes(b"x")

    entered, release = threading.Event(), threading.Event()
    original = guest_uploads._wipe_failed_attempt_files

    def delayed(tenant_arg, drawing_arg):
        entered.set()
        assert release.wait(timeout=10)
        return original(tenant_arg, drawing_arg)

    monkeypatch.setattr(
        guest_uploads, "_wipe_failed_attempt_files", delayed)
    winner = []

    def clean():
        winner.append(guest_uploads.wipe_failed_attempt_residue(
            tenant, drawing, marker["attempt"]))

    thread = threading.Thread(target=clean)
    thread.start()
    assert entered.wait(timeout=10)
    assert not guest_uploads.wipe_failed_attempt_residue(
        tenant, drawing, marker["attempt"])
    release.set()
    thread.join(timeout=20)
    assert winner == [True]

    replacement = guest_uploads.new_marker(
        filename="drawing.dxf", data=b"dxf", tenant_kind="guest",
        source_ext=".dxf")
    guest_uploads.write_marker(backend, tenant, drawing, replacement)
    assert not guest_uploads.wipe_failed_attempt_residue(
        tenant, drawing, marker["attempt"])
    assert guest_uploads.read_marker(
        backend, tenant, drawing)["attempt"] == replacement["attempt"]


@requires_database
def test_expired_extractor_cannot_resurrect_after_purge(
    postgres_authority, monkeypatch, tmp_path,
):
    token = uuid.uuid4().hex
    tenant, drawing = f"guest-{token}", f"drawing-{token}"
    guest_root = tmp_path / "guest"
    uploads_root = tmp_path / "uploads"
    monkeypatch.setenv("LEAF_GUEST_STORE_DIR", str(guest_root))
    monkeypatch.setenv("LEAF_UPLOADS_DIR", str(uploads_root))
    entered = threading.Event()

    class SlowBackend(store.FilesystemBackend):
        def put_if_absent_or_verify(self, key, data):
            if key.endswith("00000001.dwg"):
                entered.set()
                time.sleep(0.35)
            super().put_if_absent_or_verify(key, data)

    backend = SlowBackend(str(guest_root))
    marker = guest_uploads.new_marker(
        filename="drawing.dxf", data=b"dxf", tenant_kind="guest",
        source_ext=".dxf")
    now = datetime.now(timezone.utc)
    marker["retention_expires_at"] = (
        now - timedelta(seconds=1)).isoformat()
    guest_uploads.write_marker(backend, tenant, drawing, marker)
    claimed, owner, fence = guest_uploads._claim_extraction(tenant, drawing)
    with postgres_authority.connection() as conn:
        conn.execute(
            """
            UPDATE drawing_upload_attempts
            SET extraction_expires_at =
              clock_timestamp() + INTERVAL '150 milliseconds'
            WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
            """,
            {"tenant": tenant, "drawing": drawing},
        )
    source = tmp_path / "intake.json"
    source.write_bytes(b'{"layers":[],"polylines":[]}')
    errors = []

    def ingest():
        try:
            store.ingest_drawing(
                backend, tenant, str(source), drawing_id=drawing,
                authority_guard={
                    "attempt": claimed["attempt"],
                    "owner": owner,
                    "fence": fence,
                })
        except RuntimeError as exc:
            errors.append(str(exc))

    ingest_thread = threading.Thread(target=ingest)
    ingest_thread.start()
    assert entered.wait(timeout=10)
    purge_result = {}

    def purge():
        purge_result.update(guest_uploads.purge_expired(
            datetime.now(timezone.utc)))

    purge_thread = threading.Thread(target=purge)
    purge_thread.start()
    ingest_thread.join(timeout=20)
    purge_thread.join(timeout=20)
    assert errors
    assert {"tenant_id": tenant, "drawing_id": drawing} in purge_result["purged"]
    assert not guest_uploads.guest_drawing_dir(tenant, drawing).exists()
    with postgres_authority.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) AS count FROM drawing_store_manifests
            WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
            """,
            {"tenant": tenant, "drawing": drawing},
        )
        assert int(cur.fetchone()["count"]) == 0


# --------------------------------------------------------------------------- #
# Single-writer AUTHORIZATION under the PostgreSQL authority.
#
# _pg_put always required an ACTIVE checkout, but it read the holder and fence
# out of the manifest row and then required those same values back at finalize.
# That proves the lock did not change between reserve and finalize — which is
# equally true when the writer is a bystander. The caller's own identity now has
# to match, and a caller-supplied fence makes the token a real fencing token
# rather than a churn detector.
# --------------------------------------------------------------------------- #
@requires_database
def test_pg_write_refused_for_a_session_that_does_not_hold_the_checkout(
    postgres_authority, tmp_path,
):
    token = uuid.uuid4().hex
    tenant, drawing = f"tenant-{token}", f"drawing-{token}"
    backend = store.InMemoryBackend()
    initial = tmp_path / "initial.dwg"
    initial.write_bytes(b"v1")
    store.ingest_drawing(backend, tenant, str(initial), drawing_id=drawing)
    update = tmp_path / "update.dwg"
    update.write_bytes(b"v2")

    assert store.acquire_checkout(backend, tenant, drawing, "sess-a", 600)
    with pytest.raises(store.CheckoutDenied, match="sess-a"):
        store.put_drawing(backend, tenant, drawing, str(update), 1,
                          {"tool": "intruder"}, holder="sess-b")
    # nothing published: head is untouched and no version was left 'ready'
    manifest = store.load_manifest(backend, tenant, drawing)
    assert manifest["head"] == 1 and manifest["latest"] == 1

    # the real holder still publishes normally
    assert store.put_drawing(backend, tenant, drawing, str(update), 1,
                             {"tool": "owner"}, holder="sess-a") == 2


@requires_database
def test_pg_preflight_refuses_everything_the_commit_would_refuse(
    postgres_authority, tmp_path,
):
    """sol-critic r3 MAJOR. authorize_checkout is the pre-flight run_write_live
    uses BEFORE submitting a paid APS WorkItem, so it has to refuse everything
    the commit refuses or the saving is imaginary. Under postgres the commit also
    requires a LIVE checkout (_pg_put), which the shared legacy predicate does
    not — it treats no lock and an expired lock as free. Pre-flighting only the
    holder rule let an unlocked live write pass here, buy the WorkItem, and fail
    at publish: precisely the bill the pre-flight exists to avoid."""
    token = uuid.uuid4().hex
    tenant, drawing = f"tenant-{token}", f"drawing-{token}"
    backend = store.InMemoryBackend()
    initial = tmp_path / "initial.dwg"
    initial.write_bytes(b"v1")
    store.ingest_drawing(backend, tenant, str(initial), drawing_id=drawing)

    # (a) NO lock: commit raises ValueError, so the pre-flight must too
    with pytest.raises(ValueError, match="active checkout is required"):
        store.authorize_checkout(backend, tenant, drawing, "sess-a")

    # (b) EXPIRED lock: same answer, not "free" as the legacy rule would say
    assert store.acquire_checkout(backend, tenant, drawing, "sess-a", 600)
    with postgres_authority.connection() as conn:
        conn.execute(
            """
            UPDATE drawing_store_manifests
            SET checkout_expires_at = NOW() - INTERVAL '1 second'
            WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
            """,
            {"tenant": tenant, "drawing": drawing},
        )
    with pytest.raises(ValueError, match="active checkout is required"):
        store.authorize_checkout(backend, tenant, drawing, "sess-a")

    # (c) LIVE lock: the holder passes, a non-holder is denied — same as commit
    assert store.acquire_checkout(backend, tenant, drawing, "sess-a", 600)
    store.authorize_checkout(backend, tenant, drawing, "sess-a")
    with pytest.raises(store.CheckoutDenied):
        store.authorize_checkout(backend, tenant, drawing, "sess-b")
    with pytest.raises(store.CheckoutDenied, match="names no session"):
        store.authorize_checkout(backend, tenant, drawing, store.ANONYMOUS_HOLDER)

    # (d) holder and fence are INDEPENDENT claims. _pg_put checks any supplied
    # fence regardless of holder, so the pre-flight must too — otherwise a caller
    # naming no session but presenting a stale fence passes here and is refused
    # only after the WorkItem is paid for.
    current = int(store.load_manifest(backend, tenant, drawing)["checkout"]["fence"])
    store.authorize_checkout(backend, tenant, drawing, None, fence=current)
    with pytest.raises(store.CheckoutDenied, match="stale"):
        store.authorize_checkout(backend, tenant, drawing, None, fence=current - 1)
    with pytest.raises(store.CheckoutDenied, match="stale"):
        store.authorize_checkout(backend, tenant, drawing, "sess-a", fence=current - 1)


@requires_database
def test_pg_write_refused_against_a_persisted_anonymous_sentinel_lock(
    postgres_authority, tmp_path,
):
    """sol-critic r2 BLOCKER, postgres half. `checkout_holder` is free text
    (platform/migrations/0016), and acquire_checkout's refusal only guards NEW
    acquisitions, so a row already carrying the sentinel — written under an
    earlier release or restored — would compare EQUAL to the anonymous caller.
    Seeded with a direct UPDATE, which is the path the acquire-time refusal
    cannot see."""
    token = uuid.uuid4().hex
    tenant, drawing = f"tenant-{token}", f"drawing-{token}"
    backend = store.InMemoryBackend()
    initial = tmp_path / "initial.dwg"
    initial.write_bytes(b"v1")
    store.ingest_drawing(backend, tenant, str(initial), drawing_id=drawing)
    update = tmp_path / "update.dwg"
    update.write_bytes(b"v2")

    with postgres_authority.connection() as conn:
        conn.execute(
            """
            UPDATE drawing_store_manifests
            SET checkout_holder = %(holder)s,
                checkout_expires_at = NOW() + INTERVAL '600 seconds',
                checkout_acquired_at = NOW()
            WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
            """,
            {"holder": store.ANONYMOUS_HOLDER, "tenant": tenant, "drawing": drawing},
        )

    with pytest.raises(store.CheckoutDenied, match="names no session"):
        store.put_drawing(backend, tenant, drawing, str(update), 1,
                          {"tool": "anon"}, holder=store.ANONYMOUS_HOLDER)
    assert store.load_manifest(backend, tenant, drawing)["head"] == 1


@requires_database
def test_pg_write_refused_for_a_stale_fence_from_the_same_holder(
    postgres_authority, tmp_path,
):
    """The fencing property a row-read fence cannot provide: the holder id is
    unchanged, but this writer's lease lapsed and was re-acquired, so the
    generation it believes it holds is stale and its write must not land."""
    token = uuid.uuid4().hex
    tenant, drawing = f"tenant-{token}", f"drawing-{token}"
    backend = store.InMemoryBackend()
    initial = tmp_path / "initial.dwg"
    initial.write_bytes(b"v1")
    store.ingest_drawing(backend, tenant, str(initial), drawing_id=drawing)
    update = tmp_path / "update.dwg"
    update.write_bytes(b"v2")

    assert store.acquire_checkout(backend, tenant, drawing, "sess-a", 600)
    stale_fence = int(store.load_manifest(backend, tenant, drawing)["checkout"]["fence"])
    # re-acquire bumps the generation (acquire_checkout: checkout_fence + 1)
    assert store.acquire_checkout(backend, tenant, drawing, "sess-a", 600)
    current_fence = int(store.load_manifest(backend, tenant, drawing)["checkout"]["fence"])
    assert current_fence == stale_fence + 1

    with pytest.raises(store.CheckoutDenied, match="stale"):
        store.put_drawing(backend, tenant, drawing, str(update), 1,
                          {"tool": "resumed"}, holder="sess-a", fence=stale_fence)
    assert store.load_manifest(backend, tenant, drawing)["head"] == 1

    # the CURRENT generation is accepted
    assert store.put_drawing(backend, tenant, drawing, str(update), 1,
                             {"tool": "resumed"}, holder="sess-a",
                             fence=current_fence) == 2
