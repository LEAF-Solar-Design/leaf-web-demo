"""Focused PostgreSQL drawing and upload authority tests.

The selector and legacy checks always run. Fleet race tests require an explicit
DATABASE_URL and skip cleanly otherwise.
"""
from __future__ import annotations

import os
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
        def put(self, key, data):
            if key.endswith("00000001.dwg"):
                entered.set()
                time.sleep(0.35)
            super().put(key, data)

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
