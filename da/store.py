"""da/store.py — per-tenant, per-drawing, immutable-versioned DWG drawing store.

Replaces the transient throwaway-object model in client.py with a PERSISTENT,
versioned store on APS OSS. APS OSS has no native versioning and no atomic
compare-and-swap, so versioning is encoded in the object-KEY scheme plus a
`manifest.json` object we maintain per drawing:

    tenants/{tenant_id}/drawings/{drawing_id}/v/{version:08d}.dwg   # immutable DWG bytes
    tenants/{tenant_id}/drawings/{drawing_id}/manifest.json         # version index + checkout lock

Immutable versions ARE the undo guarantee: we NEVER PUT over an existing
`.../v/NNNNNNNN.dwg` key. `undo` just repoints the manifest `head` pointer at the
parent version (the object is never deleted, so redo stays possible).

DWG is an unmergeable binary blob, so the write model is single-writer-checkout +
immutable versions, NOT git-merge. Concurrent edits serialize on a checkout lock
stored in the manifest.

LIMITATION (documented, see STORE.md): OSS manifest writes are NON-ATOMIC — this
is a best-effort lock, not a true compare-and-swap. Production promotes the version
index + lock to Postgres for atomic CAS. That hardening is out of scope here.

Backends: all store primitives take a `backend` so tests inject an in-memory
backend (ZERO network) and the live path injects `OSSBackend` (delegates to
da/client.py OSS helpers).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # da/ sibling imports (highest precedence)
# The shared F13 tenant/opaque-id validator lives in server/; APPEND it (lowest
# precedence) so da/ modules still win any name clash while `tenant_id_validator`
# (unique to server/) still resolves in both the da- and server-rooted test runs.
_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server")
if _SERVER_DIR not in sys.path:
    sys.path.append(_SERVER_DIR)
import client  # noqa: E402  (OSSBackend delegates to client's OSS helpers)
import requests  # noqa: E402  (only OSSBackend.exists references requests.HTTPError)
import tenant_id_validator as _tid  # noqa: E402  (the ONE shared reject-don't-collapse rule)

_platform_db = None


def authority_mode() -> str:
    """Return the explicit mutable drawing authority.

    Legacy blob manifests remain the default. PostgreSQL never silently falls
    back because two ECS tasks must make the same mutation decision.
    """
    mode = os.environ.get("LEAF_DRAWING_STORE", "legacy").strip().lower()
    if mode not in {"legacy", "postgres"}:
        raise RuntimeError("LEAF_DRAWING_STORE must be 'legacy' or 'postgres'")
    return mode


def _db():
    global _platform_db
    if _platform_db is not None:
        return _platform_db
    if "leaf_platform" not in sys.modules:
        package_dir = Path(__file__).resolve().parent.parent / "platform"
        spec = importlib.util.spec_from_file_location(
            "leaf_platform", package_dir / "__init__.py",
            submodule_search_locations=[str(package_dir)],
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("platform package could not be loaded")
        module = importlib.util.module_from_spec(spec)
        sys.modules["leaf_platform"] = module
        spec.loader.exec_module(module)
    import leaf_platform.db as db  # noqa: PLC0415
    _platform_db = db
    return db

# --------------------------------------------------------------------------- #
# Key scheme
# --------------------------------------------------------------------------- #
# The canonical key contract every stored version key must satisfy. The id segments
# admit the same charset as the shared validator (`[a-z0-9_-]`, underscores included
# for real Auth0 tenant ids like `org_acme_solar`).
VERSION_KEY_RE = re.compile(r"^tenants/[a-z0-9_-]+/drawings/[a-z0-9_-]+/v/\d{8}\.dwg$")


class CheckoutDenied(Exception):
    """A write was attempted by someone who is not the single-writer lock holder.

    Deliberately NOT a ValueError. Every caller of the write primitives already
    maps `(KeyError, ValueError)` to a 400 BAD_PARAMS ("you asked for a drawing
    or version that does not exist"), and this is a different answer: the request
    is well-formed and the drawing exists, but the caller lacks authority over it.
    A distinct type is what lets server/write_loop.py surface it as 403 FORBIDDEN,
    matching the DELETE .../checkout route, which has always answered 403 when a
    non-holder tries to release a lock (server/routers/drawings.py).
    """


def sanitize_id(raw: str) -> str:
    """Validate an id against the ONE shared tenant/opaque-id rule and return it
    UNCHANGED — REJECT-don't-collapse (server/tenant_id_validator, security-audit F13).

    Was: collapsed to `[a-z0-9-]`, which let DISTINCT ids collide on a single store
    key — e.g. `acme_corp` and `acme-corp`, or `Acme Corp` and `acme-corp`, all
    folded to `acme-corp`. Now anything not already canonical is REJECTED, so no two
    distinct tenants/drawings can ever share a key. A UUIDv7 drawing id is already
    canonical and passes through unchanged.
    """
    return _tid.validate_tenant_id(raw, kind="id")


def new_drawing_id() -> str:
    """Generate a UUIDv7 (time-ordered) string. Lowercase hex + hyphens => `[a-z0-9-]`."""
    ms = int(time.time() * 1000)
    b = bytearray(ms.to_bytes(6, "big") + os.urandom(10))
    b[6] = (b[6] & 0x0F) | 0x70  # version 7
    b[8] = (b[8] & 0x3F) | 0x80  # RFC 4122 variant
    return str(uuid.UUID(bytes=bytes(b)))


def drawing_version_key(tenant_id: str, drawing_id: str, version) -> str:
    """Deterministic immutable object key for a specific version's DWG bytes.

    Matches VERSION_KEY_RE. NEVER write over an existing one (immutability = undo).
    """
    v = int(version)
    if v < 1:
        raise ValueError(f"version must be >= 1, got {version!r}")
    if v > 99999999:
        raise ValueError(f"version {v} overflows the 8-digit key field")
    key = f"tenants/{sanitize_id(tenant_id)}/drawings/{sanitize_id(drawing_id)}/v/{v:08d}.dwg"
    assert VERSION_KEY_RE.match(key), key  # invariant guard
    return key


def manifest_key(tenant_id: str, drawing_id: str) -> str:
    """Deterministic object key for the drawing's version index + checkout lock."""
    return f"tenants/{sanitize_id(tenant_id)}/drawings/{sanitize_id(drawing_id)}/manifest.json"


# --------------------------------------------------------------------------- #
# Storage backend abstraction (so tests run fully offline)
# --------------------------------------------------------------------------- #
class StorageBackend:
    """Minimal blob interface: get/put/exists over opaque string keys."""

    def get(self, key: str) -> bytes:
        raise NotImplementedError

    def put(self, key: str, data: bytes) -> None:
        raise NotImplementedError

    def exists(self, key: str) -> bool:
        raise NotImplementedError

    def put_if_absent_or_verify(self, key: str, data: bytes) -> None:
        """Publish immutable bytes without replacing a matching winner."""
        payload = bytes(data)
        if not self.exists(key):
            self.put(key, payload)
        existing = self.get(key)
        if existing != payload:
            raise ValueError("immutable object already exists with different content")


class InMemoryBackend(StorageBackend):
    """In-memory dict backend for tests — makes ZERO network calls."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    def get(self, key: str) -> bytes:
        if key not in self._blobs:
            raise KeyError(key)
        return self._blobs[key]

    def put(self, key: str, data: bytes) -> None:
        self._blobs[key] = bytes(data)

    def exists(self, key: str) -> bool:
        return key in self._blobs

    def put_if_absent_or_verify(self, key: str, data: bytes) -> None:
        payload = bytes(data)
        winner = self._blobs.setdefault(key, payload)
        if winner != payload:
            raise ValueError("immutable object already exists with different content")

    # test convenience
    def keys(self) -> list[str]:
        return sorted(self._blobs)


class OSSBackend(StorageBackend):
    """Live backend delegating to da/client.py's OSS helpers (LIVE calls)."""

    def get(self, key: str) -> bytes:
        return client.download_object(key)

    def put(self, key: str, data: bytes) -> None:
        # client.upload_object takes a local path (signed-S3 3-step), so stage bytes.
        import tempfile
        fd, tmp = tempfile.mkstemp(suffix=".blob")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            client.upload_object(tmp, key)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def exists(self, key: str) -> bool:
        # A signed-download request 404s for a missing object; anything else re-raises.
        try:
            client.signed_download_url(key)
            return True
        except requests.HTTPError as e:
            resp = getattr(e, "response", None)
            if resp is not None and resp.status_code == 404:
                return False
            raise


class FilesystemBackend(StorageBackend):
    """Local-filesystem backend: blobs are files under `root_dir`, one file per
    object key (the key's `/`-segments become directories). Makes ZERO network /
    APS calls — this is the backend the demo bootstrap ("demo" drawing) uses at
    APS_LIVE=0 so the write loop persists real, durable, undo-able versions on
    disk without touching OSS.

    Object keys are the store's own deterministic, already-sanitized keys
    (`tenants/.../v/00000001.dwg`, `.../manifest.json`); a normpath + prefix
    check refuses any key that would escape `root_dir`.
    """

    def __init__(self, root_dir: str) -> None:
        self.root = os.path.abspath(root_dir)

    def _path(self, key: str) -> str:
        p = os.path.normpath(os.path.join(self.root, key))
        root = self.root if self.root.endswith(os.sep) else self.root + os.sep
        if p != self.root and not p.startswith(root):
            raise ValueError(f"object key {key!r} escapes store root")
        return p

    def get(self, key: str) -> bytes:
        try:
            with open(self._path(key), "rb") as fh:
                return fh.read()
        except FileNotFoundError as exc:
            raise KeyError(key) from exc

    def put(self, key: str, data: bytes) -> None:
        p = self._path(key)
        parent = os.path.dirname(p)
        os.makedirs(parent, exist_ok=True)
        handle, tmp = tempfile.mkstemp(prefix=".leaf-put-", dir=parent)
        try:
            with os.fdopen(handle, "wb") as fh:
                fh.write(bytes(data))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, p)
            if hasattr(os, "O_DIRECTORY"):
                directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            try:
                os.remove(tmp)
            except FileNotFoundError:
                pass

    def put_if_absent_or_verify(self, key: str, data: bytes) -> None:
        """Durably create an immutable object, or verify the atomic winner."""
        p = self._path(key)
        parent = os.path.dirname(p)
        os.makedirs(parent, exist_ok=True)
        payload = bytes(data)
        handle, tmp = tempfile.mkstemp(prefix=".leaf-immutable-", dir=parent)
        created = False
        try:
            with os.fdopen(handle, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            try:
                os.link(tmp, p)
                created = True
            except FileExistsError:
                pass
            with open(p, "rb") as winner:
                existing = winner.read()
            if existing != payload:
                raise ValueError(
                    "immutable object already exists with different content")
            if created and hasattr(os, "O_DIRECTORY"):
                directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            try:
                os.remove(tmp)
            except FileNotFoundError:
                pass

    def exists(self, key: str) -> bool:
        return os.path.exists(self._path(key))


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read(local_path: str) -> bytes:
    with open(local_path, "rb") as fh:
        return fh.read()


def _pg_manifest(tenant_id: str, drawing_id: str) -> dict:
    db = _db()
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT head, latest, checkout_holder, checkout_acquired_at,
                   checkout_expires_at, checkout_fence
            FROM drawing_store_manifests
            WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
            """,
            {"tenant": tenant_id, "drawing": drawing_id},
        )
        manifest_row = cur.fetchone()
        if manifest_row is None or manifest_row["head"] is None:
            raise KeyError(manifest_key(tenant_id, drawing_id))
        cur.execute(
            """
            SELECT version, parent_version, created_at, byte_count,
                   content_sha256, workitem_id, tool, note
            FROM drawing_store_versions
            WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
              AND state = 'ready'
            ORDER BY version
            """,
            {"tenant": tenant_id, "drawing": drawing_id},
        )
        rows = cur.fetchall()
    checkout = None
    if manifest_row["checkout_holder"] is not None:
        checkout = {
            "holder": manifest_row["checkout_holder"],
            "acquired": manifest_row["checkout_acquired_at"].isoformat(),
            "expires": manifest_row["checkout_expires_at"].isoformat(),
            "fence": int(manifest_row["checkout_fence"]),
        }
    ready_latest = max((int(row["version"]) for row in rows), default=0)
    return {
        "schema": 1,
        "tenant_id": tenant_id,
        "drawing_id": drawing_id,
        "head": int(manifest_row["head"]),
        "latest": ready_latest,
        "versions": [{
            "v": int(row["version"]),
            "parent": (int(row["parent_version"])
                       if row["parent_version"] is not None else None),
            "created": row["created_at"].isoformat(),
            "bytes": int(row["byte_count"]),
            "sha256": row["content_sha256"],
            "workitem_id": row["workitem_id"],
            "tool": row["tool"],
            "note": row["note"],
        } for row in rows],
        "checkout": checkout,
    }


def load_manifest(backend: StorageBackend, tenant_id: str, drawing_id: str) -> dict:
    if authority_mode() == "postgres":
        return _pg_manifest(sanitize_id(tenant_id), sanitize_id(drawing_id))
    raw = backend.get(manifest_key(tenant_id, drawing_id))
    return json.loads(raw.decode("utf-8"))


def save_manifest(backend: StorageBackend, tenant_id: str, drawing_id: str, manifest: dict) -> None:
    # NOTE: non-atomic on OSS (best-effort). Production promotes this to Postgres CAS.
    backend.put(manifest_key(tenant_id, drawing_id),
                json.dumps(manifest, separators=(",", ":")).encode("utf-8"))


def _new_manifest(tenant_id: str, drawing_id: str) -> dict:
    return {
        "schema": 1,
        "tenant_id": tenant_id,
        "drawing_id": drawing_id,
        "head": 1,
        "latest": 1,
        "versions": [],
        "checkout": None,
    }


def _pg_mark_version_state(
    tenant_id: str, drawing_id: str, version: int, state: str,
) -> None:
    db = _db()
    with db.connection() as conn:
        conn.execute(
            """
            UPDATE drawing_store_versions
            SET state = %(state)s
            WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
              AND version = %(version)s AND state = 'reserved'
            """,
            {
                "state": state, "tenant": tenant_id,
                "drawing": drawing_id, "version": version,
            },
        )


def _put_or_verify_blob(
    backend: StorageBackend, key: str, data: bytes, digest: str,
) -> None:
    """Publish one immutable blob, or adopt an exact crash-window artifact."""
    backend.put_if_absent_or_verify(key, data)
    existing = backend.get(key)
    if len(existing) != len(data) or _sha256(existing) != digest:
        raise ValueError(
            f"reserved immutable version key {key} has mismatched content")


def _locked_upload_guard(conn, guard: dict) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM drawing_upload_attempts
        WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
          AND attempt = %(attempt)s AND status = 'extracting'
          AND extraction_owner = %(owner)s
          AND extraction_fence = %(fence)s
          AND extraction_expires_at > clock_timestamp()
        FOR UPDATE
        """,
        guard,
    ).fetchone()
    return row is not None


def _pg_ingest_guarded(
    backend: StorageBackend, tenant_id: str, drawing_id: str, data: bytes,
    guard: dict,
) -> dict:
    """Ingest v1 while holding the upload authority row through publication."""
    db = _db()
    vkey = drawing_version_key(tenant_id, drawing_id, 1)
    digest = _sha256(data)
    params = {
        "tenant": tenant_id, "drawing": drawing_id, "key": vkey,
        "bytes": len(data), "sha": digest, "attempt": str(guard["attempt"]),
        "owner": str(guard["owner"]), "fence": int(guard["fence"]),
    }

    def operation(conn):
        if not _locked_upload_guard(conn, params):
            raise RuntimeError("upload extraction lease is stale")
        manifest = conn.execute(
            """
            SELECT head FROM drawing_store_manifests
            WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
            FOR UPDATE
            """,
            params,
        ).fetchone()
        if manifest is not None and manifest["head"] is not None:
            ready = conn.execute(
                """
                SELECT byte_count, content_sha256, object_key
                FROM drawing_store_versions
                WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
                  AND version = 1 AND state = 'ready'
                """,
                params,
            ).fetchone()
            if (
                ready is None or int(ready["byte_count"]) != len(data)
                or ready["content_sha256"] != digest
                or ready["object_key"] != vkey
            ):
                raise ValueError(
                    f"drawing already exists with different content: "
                    f"{tenant_id}/{drawing_id}")
            return
        if manifest is None:
            conn.execute(
                """
                INSERT INTO drawing_store_manifests
                  (tenant_id, drawing_id, head, latest)
                VALUES (%(tenant)s, %(drawing)s, NULL, 1)
                """,
                params,
            )
            conn.execute(
                """
                INSERT INTO drawing_store_versions
                  (tenant_id, drawing_id, version, parent_version, object_key,
                   byte_count, content_sha256, note, state, reservation_token,
                   reservation_expires_at)
                VALUES
                  (%(tenant)s, %(drawing)s, 1, NULL, %(key)s, %(bytes)s,
                   %(sha)s, 'initial ingest', 'reserved', %(attempt)s,
                   clock_timestamp() + INTERVAL '15 minutes')
                """,
                params,
            )
        else:
            reserved = conn.execute(
                """
                SELECT byte_count, content_sha256, object_key, state
                FROM drawing_store_versions
                WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
                  AND version = 1
                FOR UPDATE
                """,
                params,
            ).fetchone()
            if (
                reserved is None or reserved["state"] != "reserved"
                or int(reserved["byte_count"]) != len(data)
                or reserved["content_sha256"] != digest
                or reserved["object_key"] != vkey
            ):
                raise ValueError("initial drawing reservation does not match content")
            conn.execute(
                """
                UPDATE drawing_store_versions
                SET reservation_token = %(attempt)s,
                    reservation_expires_at =
                      clock_timestamp() + INTERVAL '15 minutes'
                WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
                  AND version = 1
                """,
                params,
            )
        # clock_timestamp() is intentional. NOW() is fixed at transaction start.
        if not _locked_upload_guard(conn, params):
            raise RuntimeError("upload extraction lease expired during ingest")
        if guard.get("defer_ready"):
            # The upload owner will publish the intake proof, ready version,
            # manifest head, and terminal upload marker in one later database
            # transaction. Blob bytes may exist first, but remain unreachable.
            return
        _put_or_verify_blob(backend, vkey, data, digest)
        updated = conn.execute(
            """
            UPDATE drawing_store_manifests m
            SET head = 1, updated_at = NOW()
            WHERE m.tenant_id = %(tenant)s AND m.drawing_id = %(drawing)s
              AND m.head IS NULL AND EXISTS (
                SELECT 1 FROM drawing_upload_attempts u
                WHERE u.tenant_id = m.tenant_id
                  AND u.drawing_id = m.drawing_id
                  AND u.attempt = %(attempt)s
                  AND u.status = 'extracting'
                  AND u.extraction_owner = %(owner)s
                  AND u.extraction_fence = %(fence)s
                  AND u.extraction_expires_at > clock_timestamp()
              )
            RETURNING m.tenant_id
            """,
            params,
        ).fetchone()
        if updated is None:
            raise RuntimeError("upload authority changed before manifest publish")
        conn.execute(
            """
            UPDATE drawing_store_versions
            SET state = 'ready', ready_at = NOW(),
                reservation_token = NULL, reservation_expires_at = NULL
            WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
              AND version = 1 AND state = 'reserved'
            """,
            params,
        )
        backend.put(
            manifest_key(tenant_id, drawing_id),
            b'{"authority":"postgres"}',
        )
        if not _locked_upload_guard(conn, params):
            raise RuntimeError(
                "upload extraction lease expired during manifest publication")

    db.run_transaction(operation, isolation="serializable")
    if guard.get("defer_ready"):
        _put_or_verify_blob(backend, vkey, data, digest)
    return {"drawing_id": drawing_id, "version": 1}


def _pg_ingest(
    backend: StorageBackend, tenant_id: str, drawing_id: str, data: bytes,
    guard: Optional[dict] = None,
) -> dict:
    if guard is not None:
        return _pg_ingest_guarded(
            backend, tenant_id, drawing_id, data, guard)
    db = _db()
    vkey = drawing_version_key(tenant_id, drawing_id, 1)
    digest = _sha256(data)
    token = uuid.uuid4().hex
    params = {
        "tenant": tenant_id, "drawing": drawing_id, "key": vkey,
        "bytes": len(data), "sha": digest, "token": token,
    }

    def reserve(conn):
        manifest = conn.execute(
            """
            SELECT head FROM drawing_store_manifests
            WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
            FOR UPDATE
            """,
            params,
        ).fetchone()
        if manifest is None:
            conn.execute(
                """
                INSERT INTO drawing_store_manifests
                  (tenant_id, drawing_id, head, latest)
                VALUES (%(tenant)s, %(drawing)s, NULL, 1)
                """,
                params,
            )
            conn.execute(
                """
                INSERT INTO drawing_store_versions
                  (tenant_id, drawing_id, version, parent_version, object_key,
                   byte_count, content_sha256, note, state, reservation_token,
                   reservation_expires_at)
                VALUES
                  (%(tenant)s, %(drawing)s, 1, NULL, %(key)s, %(bytes)s,
                   %(sha)s, 'initial ingest', 'reserved', %(token)s,
                   clock_timestamp() + INTERVAL '1 minute')
                """,
                params,
            )
            return
        if manifest["head"] is not None:
            raise ValueError(
                f"drawing already exists: {tenant_id}/{drawing_id} "
                "(use put_drawing to add versions)")
        row = conn.execute(
            """
            SELECT byte_count, content_sha256, object_key, state,
                   reservation_expires_at
            FROM drawing_store_versions
            WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
              AND version = 1
            FOR UPDATE
            """,
            params,
        ).fetchone()
        if (
            row is None or row["state"] != "reserved"
            or int(row["byte_count"]) != len(data)
            or row["content_sha256"] != digest or row["object_key"] != vkey
        ):
            raise ValueError("initial drawing reservation does not match content")
        if row["reservation_expires_at"] > datetime.now(timezone.utc):
            raise RuntimeError("initial drawing reservation is still active")
        conn.execute(
            """
            UPDATE drawing_store_versions
            SET reservation_token = %(token)s,
                reservation_expires_at =
                  clock_timestamp() + INTERVAL '1 minute'
            WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
              AND version = 1
            """,
            params,
        )

    db.run_transaction(reserve, isolation="serializable")
    _put_or_verify_blob(backend, vkey, data, digest)

    def finalize(conn):
        updated = conn.execute(
            """
            UPDATE drawing_store_manifests m
            SET head = 1, updated_at = NOW()
            WHERE m.tenant_id = %(tenant)s AND m.drawing_id = %(drawing)s
              AND m.head IS NULL AND EXISTS (
                SELECT 1 FROM drawing_store_versions v
                WHERE v.tenant_id = m.tenant_id
                  AND v.drawing_id = m.drawing_id AND v.version = 1
                  AND v.state = 'reserved'
                  AND v.reservation_token = %(token)s
                  AND v.reservation_expires_at > clock_timestamp()
              )
            RETURNING m.tenant_id
            """,
            params,
        ).fetchone()
        if updated is None:
            raise RuntimeError("initial drawing reservation was reclaimed")
        conn.execute(
            """
            UPDATE drawing_store_versions
            SET state = 'ready', ready_at = NOW(),
                reservation_token = NULL, reservation_expires_at = NULL
            WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
              AND version = 1 AND reservation_token = %(token)s
            """,
            params,
        )

    db.run_transaction(finalize, isolation="serializable")
    backend.put(manifest_key(tenant_id, drawing_id), b'{"authority":"postgres"}')
    return {"drawing_id": drawing_id, "version": 1}


def _pg_put(
    backend: StorageBackend, tenant_id: str, drawing_id: str, data: bytes,
    parent_version: Optional[int], meta: dict, *,
    holder: Optional[str] = None, fence: Optional[int] = None,
) -> int:
    db = _db()
    digest = _sha256(data)

    def reserve(conn):
        row = conn.execute(
            """
            SELECT head, latest, checkout_holder, checkout_fence,
                   checkout_expires_at
            FROM drawing_store_manifests
            WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
            FOR UPDATE
            """,
            {"tenant": tenant_id, "drawing": drawing_id},
        ).fetchone()
        if row is None or row["head"] is None:
            raise KeyError(manifest_key(tenant_id, drawing_id))
        if (
            row["checkout_holder"] is None
            or row["checkout_expires_at"] <= datetime.now(timezone.utc)
        ):
            raise ValueError("an active checkout is required to publish a version")
        # The lock is live — but WHOSE? Until this check, the answer was "nobody
        # asked": the caller's identity never reached the store, so any request
        # on this tenant published under whatever lease happened to be open. The
        # comparison happens under the same FOR UPDATE row lock that the fenced
        # finalize below re-checks, so the holder cannot change underneath it.
        if holder is not None and str(row["checkout_holder"]) != str(holder):
            raise CheckoutDenied(
                f"drawing is checked out by {row['checkout_holder']!r}; "
                f"{holder!r} may not publish a version")
        # A caller-supplied fence is what makes this a fencing token rather than a
        # churn detector: a writer whose lease lapsed and was re-acquired holds a
        # stale generation and is refused here even when the holder id matches,
        # which is exactly the resumed-after-a-pause writer the fence exists for.
        if fence is not None and int(row["checkout_fence"]) != int(fence):
            raise CheckoutDenied(
                f"checkout fence {int(fence)} is stale "
                f"(current generation {int(row['checkout_fence'])}); "
                f"re-acquire the checkout before publishing")
        expected = int(parent_version) if parent_version is not None else None
        if int(row["head"]) != expected:
            raise ValueError(
                f"stale drawing head: expected {expected}, current {row['head']}")
        version = int(row["latest"]) + 1
        key = drawing_version_key(tenant_id, drawing_id, version)
        conn.execute(
            """
            UPDATE drawing_store_manifests
            SET latest = %(version)s, updated_at = NOW()
            WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
            """,
            {
                "tenant": tenant_id, "drawing": drawing_id,
                "version": version,
            },
        )
        conn.execute(
            """
            INSERT INTO drawing_store_versions
              (tenant_id, drawing_id, version, parent_version, object_key,
               byte_count, content_sha256, workitem_id, tool, note, state)
            VALUES
              (%(tenant)s, %(drawing)s, %(version)s, %(parent)s, %(key)s,
               %(bytes)s, %(sha)s, %(workitem)s, %(tool)s, %(note)s,
               'reserved')
            """,
            {
                "tenant": tenant_id, "drawing": drawing_id,
                "version": version, "parent": expected, "key": key,
                "bytes": len(data), "sha": digest,
                "workitem": meta.get("workitem_id"), "tool": meta.get("tool"),
                "note": meta.get("note"),
            },
        )
        # Carried to finalize as the lock this version was reserved against. When
        # the caller named itself these are its OWN verified holder/generation
        # (checked just above under this row lock), so the fenced UPDATE below now
        # proves "the caller still owns the lock", not merely "the lock did not
        # change" — the latter was true even when the writer was a bystander.
        return (
            version, key, expected, str(row["checkout_holder"]),
            int(row["checkout_fence"]),
        )

    version, vkey, expected, checkout_holder, checkout_fence = db.run_transaction(
        reserve, isolation="serializable")
    try:
        if backend.exists(vkey):
            raise ValueError(f"refuse to overwrite immutable version key {vkey}")
        backend.put(vkey, data)
    except Exception:
        _pg_mark_version_state(tenant_id, drawing_id, version, "orphaned")
        raise

    def finalize(conn):
        updated = conn.execute(
            """
            UPDATE drawing_store_manifests
            SET head = %(version)s, updated_at = NOW()
            WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
              AND head = %(expected)s
              AND checkout_holder = %(checkout_holder)s
              AND checkout_fence = %(checkout_fence)s
              AND checkout_expires_at > clock_timestamp()
            RETURNING tenant_id
            """,
            {
                "tenant": tenant_id, "drawing": drawing_id,
                "version": version, "expected": expected,
                "checkout_holder": checkout_holder,
                "checkout_fence": checkout_fence,
            },
        ).fetchone()
        if updated is None:
            conn.execute(
                """
                UPDATE drawing_store_versions SET state = 'orphaned'
                WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
                  AND version = %(version)s AND state = 'reserved'
                """,
                {
                    "tenant": tenant_id, "drawing": drawing_id,
                    "version": version,
                },
            )
            return False
        conn.execute(
            """
            UPDATE drawing_store_versions SET state = 'ready', ready_at = NOW()
            WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
              AND version = %(version)s AND state = 'reserved'
            """,
            {
                "tenant": tenant_id, "drawing": drawing_id,
                "version": version,
            },
        )
        return True

    if not db.run_transaction(finalize, isolation="serializable"):
        raise ValueError("drawing head changed while the immutable version was stored")
    return version


# --------------------------------------------------------------------------- #
# Version primitives
# --------------------------------------------------------------------------- #
def ingest_drawing(backend: StorageBackend, tenant_id: str, local_path: str,
                   drawing_id: str | None = None, *,
                   authority_guard: Optional[dict] = None) -> dict:
    """PUT version 1 of a new drawing + write its initial manifest.

    Returns {"drawing_id": <id>, "version": 1}. Refuses to clobber an existing
    drawing (use put_drawing to append versions).
    """
    tid = sanitize_id(tenant_id)
    did = sanitize_id(drawing_id) if drawing_id else new_drawing_id()
    if authority_mode() == "postgres":
        return _pg_ingest(
            backend, tid, did, _read(local_path), authority_guard)

    if backend.exists(manifest_key(tid, did)):
        raise ValueError(f"drawing already exists: {tid}/{did} (use put_drawing to add versions)")

    data = _read(local_path)
    vkey = drawing_version_key(tid, did, 1)
    if backend.exists(vkey):  # immutability guard
        raise ValueError(f"refuse to overwrite immutable version key {vkey}")
    backend.put(vkey, data)

    m = _new_manifest(tid, did)
    m["versions"].append({
        "v": 1, "parent": None, "created": _now_iso(),
        "bytes": len(data), "sha256": _sha256(data),
        "workitem_id": None, "tool": None, "note": "initial ingest",
    })
    save_manifest(backend, tid, did, m)
    return {"drawing_id": did, "version": 1}


def _authorize_checkout_view(co: dict | None, holder: str | None,
                             fence: int | None = None) -> None:
    """The single-writer rule over a NORMALIZED checkout dict ({holder, expires,
    fence?}) — the shape both authorities hand back from `load_manifest`.

    Raises CheckoutDenied when an ACTIVE lock is held by someone other than
    `holder`. Called only when the caller names itself (`holder is not None`).
    Two cases stay deliberately permitted, because they are the behaviour the
    product has always had and neither lets a caller write under another
    session's lease:

      * no lock at all, and
      * an EXPIRED lock (the store's rule everywhere else is that an expired lock
        is free — acquire_checkout re-grants it to anyone).

    A `fence` is checked only when the lock actually carries one. Legacy locks do
    not: acquire_checkout's legacy branch writes holder/acquired/expires and no
    generation, so under that authority the holder comparison IS the whole
    guarantee. The asymmetry is inherent to the legacy backend, which STORE.md
    already documents as best-effort (non-atomic manifest writes); it is not
    introduced by this check.
    """
    if holder is None:
        return
    if not _is_active(co, datetime.now(timezone.utc)):
        return
    if str(co.get("holder")) != str(holder):
        raise CheckoutDenied(
            f"drawing is checked out by {co.get('holder')!r}; "
            f"{holder!r} may not publish a version")
    current = co.get("fence")
    if fence is not None and current is not None and int(current) != int(fence):
        raise CheckoutDenied(
            f"checkout fence {int(fence)} is stale "
            f"(current generation {int(current)}); "
            f"re-acquire the checkout before publishing")


def authorize_checkout(backend: StorageBackend, tenant_id: str, drawing_id: str,
                       holder: str | None, fence: int | None = None) -> None:
    """PRE-FLIGHT single-writer check: raise CheckoutDenied if `holder` may not
    publish to this drawing right now. Read-only; no side effects.

    put_drawing performs this same check at COMMIT time, and that is the
    authoritative one (it runs under the row lock, so nothing can change between
    the check and the write). This exists because the live path spends real money
    before it reaches the commit: run_write_live submits an APS WorkItem, waits
    for it, and only then publishes. Refusing an unauthorized writer here means
    it is refused BEFORE the engine bill, instead of after.
    """
    if holder is None:
        return
    m = load_manifest(backend, sanitize_id(tenant_id), sanitize_id(drawing_id))
    _authorize_checkout_view(m.get("checkout"), holder, fence)


def put_drawing(backend: StorageBackend, tenant_id: str, drawing_id: str, local_path: str,
                parent_version, meta: dict | None = None, *,
                holder: str | None = None, fence: int | None = None) -> int:
    """Append a NEW immutable version (v = latest+1, parent = parent_version) and
    advance head + latest. This is the primitive the DWG write path calls.

    Returns the new integer version. A write from a non-latest head branches the
    DAG (parent linkage is enough; we keep the model simple).

    SINGLE-WRITER AUTHORIZATION (`holder`, `fence`). The lock only ever proved
    that A checkout was live, never that the CALLER owned it, so session B could
    publish a version under session A's lease. `holder` is the caller's own
    identity (the same id it sent to POST/DELETE .../checkout); when given, the
    write is refused with `CheckoutDenied` unless that identity owns the active
    lock. `fence` is the lock generation the caller believes it holds
    (`checkout.fence`, surfaced by GET /versions): supplying it makes this a real
    fencing token, because a caller whose lease lapsed and was re-acquired — even
    by ITSELF, under the same holder id — carries a stale generation and is
    refused. Postgres authority only; see `_authorize_legacy_checkout`.

    Both default to None, which preserves the pre-existing behaviour exactly:
    ingest paths, the offline harness and the store's own tests publish without
    naming a session. The product write path always names one — the authorization
    that matters to a tenant is applied at POST /api/run (server/routers/jobs.py)
    and carried down to here, so it cannot be skipped by reaching the store
    through a different route.
    """
    tid = sanitize_id(tenant_id)
    did = sanitize_id(drawing_id)
    if authority_mode() == "postgres":
        return _pg_put(
            backend, tid, did, _read(local_path),
            int(parent_version) if parent_version is not None else None,
            meta or {}, holder=holder, fence=fence,
        )
    m = load_manifest(backend, tid, did)
    _authorize_checkout_view(m.get("checkout"), holder, fence)

    new_v = int(m["latest"]) + 1
    vkey = drawing_version_key(tid, did, new_v)
    if backend.exists(vkey):  # immutability guard (monotonic latest => never true in practice)
        raise ValueError(f"refuse to overwrite immutable version key {vkey}")

    data = _read(local_path)
    backend.put(vkey, data)

    meta = meta or {}
    parent = int(parent_version) if parent_version is not None else None
    m["versions"].append({
        "v": new_v, "parent": parent, "created": _now_iso(),
        "bytes": len(data), "sha256": _sha256(data),
        "workitem_id": meta.get("workitem_id"), "tool": meta.get("tool"),
        "note": meta.get("note"),
    })
    m["head"] = new_v
    m["latest"] = new_v
    save_manifest(backend, tid, did, m)
    return new_v


def resolve_version(backend: StorageBackend, tenant_id: str, drawing_id: str,
                    version="head") -> tuple[int, str]:
    """Resolve `version` (an int, "head", or "latest") to (version_int, object_key)."""
    tid = sanitize_id(tenant_id)
    did = sanitize_id(drawing_id)
    m = load_manifest(backend, tid, did)

    if isinstance(version, str) and version == "head":
        v = int(m["head"])
    elif isinstance(version, str) and version == "latest":
        v = int(m["latest"])
    else:
        v = int(version)

    known = {int(e["v"]) for e in m["versions"]}
    if v not in known:
        raise ValueError(f"version {v} not in manifest for {tid}/{did} (known={sorted(known)})")
    return v, drawing_version_key(tid, did, v)


def undo(backend: StorageBackend, tenant_id: str, drawing_id: str) -> int:
    """Repoint head to the current head's parent (no object deletion => redo-able).

    `latest` is left unchanged so the undone version's object still resolves.
    """
    tid = sanitize_id(tenant_id)
    did = sanitize_id(drawing_id)
    if authority_mode() == "postgres":
        db = _db()

        def operation(conn):
            row = conn.execute(
                """
                SELECT m.head, v.parent_version
                FROM drawing_store_manifests m
                JOIN drawing_store_versions v
                  ON v.tenant_id = m.tenant_id
                 AND v.drawing_id = m.drawing_id
                 AND v.version = m.head AND v.state = 'ready'
                WHERE m.tenant_id = %(tenant)s AND m.drawing_id = %(drawing)s
                FOR UPDATE OF m
                """,
                {"tenant": tid, "drawing": did},
            ).fetchone()
            if row is None:
                raise KeyError(manifest_key(tid, did))
            if row["parent_version"] is None:
                raise ValueError("nothing to undo: head is the root version")
            parent = int(row["parent_version"])
            updated = conn.execute(
                """
                UPDATE drawing_store_manifests
                SET head = %(parent)s, updated_at = NOW()
                WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
                  AND head = %(old_head)s
                RETURNING head
                """,
                {
                    "parent": parent, "tenant": tid, "drawing": did,
                    "old_head": int(row["head"]),
                },
            ).fetchone()
            if updated is None:
                raise ValueError("drawing head changed during undo")
            return parent

        return db.run_transaction(operation, isolation="serializable")
    m = load_manifest(backend, tid, did)

    cur = int(m["head"])
    entry = next((e for e in m["versions"] if int(e["v"]) == cur), None)
    if entry is None:
        raise ValueError(f"head version {cur} missing from manifest {tid}/{did}")
    parent = entry["parent"]
    if parent is None:
        raise ValueError("nothing to undo: head is the root version")

    m["head"] = int(parent)
    save_manifest(backend, tid, did, m)
    return int(parent)


def redo(backend: StorageBackend, tenant_id: str, drawing_id: str) -> int:
    """Re-advance head one step toward `latest` — the inverse of `undo`.

    `undo` repointed head at its parent WITHOUT deleting any object, so the
    forward chain (head -> ... -> latest via parent linkage) is still intact.
    `redo` walks from `latest` back along `parent` pointers until it finds the
    version whose parent IS the current head; that version is head's immediate
    child on the path to latest, and head is repointed onto it. Stepping one
    version at a time makes repeated redo mirror repeated undo.

    Raises ValueError when head is already `latest` (nothing to redo) or the
    forward chain is broken (no child of head leads to latest).
    """
    tid = sanitize_id(tenant_id)
    did = sanitize_id(drawing_id)
    if authority_mode() == "postgres":
        db = _db()

        def operation(conn):
            manifest = conn.execute(
                """
                SELECT head FROM drawing_store_manifests
                WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
                FOR UPDATE
                """,
                {"tenant": tid, "drawing": did},
            ).fetchone()
            if manifest is None or manifest["head"] is None:
                raise KeyError(manifest_key(tid, did))
            head = int(manifest["head"])
            latest_row = conn.execute(
                """
                SELECT MAX(version) AS latest
                FROM drawing_store_versions
                WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
                  AND state = 'ready'
                """,
                {"tenant": tid, "drawing": did},
            ).fetchone()
            latest = int(latest_row["latest"])
            if head == latest:
                raise ValueError(
                    "nothing to redo: head is already the latest version")
            rows = conn.execute(
                """
                SELECT version, parent_version
                FROM drawing_store_versions
                WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
                  AND state = 'ready'
                """,
                {"tenant": tid, "drawing": did},
            ).fetchall()
            parent_of = {
                int(row["version"]): (
                    int(row["parent_version"])
                    if row["parent_version"] is not None else None)
                for row in rows
            }
            cur, target, seen = latest, None, set()
            while cur is not None and cur not in seen:
                seen.add(cur)
                if parent_of.get(cur) == head:
                    target = cur
                    break
                cur = parent_of.get(cur)
            if target is None:
                raise ValueError(
                    f"nothing to redo: no child of head {head} "
                    f"leads to latest {latest}")
            conn.execute(
                """
                UPDATE drawing_store_manifests
                SET head = %(target)s, updated_at = NOW()
                WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
                  AND head = %(head)s
                """,
                {
                    "target": target, "tenant": tid,
                    "drawing": did, "head": head,
                },
            )
            return target

        return db.run_transaction(operation, isolation="serializable")
    m = load_manifest(backend, tid, did)

    head = int(m["head"])
    latest = int(m["latest"])
    if head == latest:
        raise ValueError("nothing to redo: head is already the latest version")

    parent_of = {int(e["v"]): (int(e["parent"]) if e["parent"] is not None else None)
                 for e in m["versions"]}
    cur = latest
    target = None
    seen = set()
    while cur is not None and cur not in seen:
        seen.add(cur)
        if parent_of.get(cur) == head:
            target = cur
            break
        cur = parent_of.get(cur)
    if target is None:
        raise ValueError(f"nothing to redo: no child of head {head} leads to latest {latest}")

    m["head"] = target
    save_manifest(backend, tid, did, m)
    return target


# --------------------------------------------------------------------------- #
# Single-writer checkout lock (best-effort; non-atomic on OSS)
# --------------------------------------------------------------------------- #
def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _is_active(co: dict | None, now: datetime) -> bool:
    if not co:
        return False
    try:
        return _parse_iso(co["expires"]) > now
    except Exception:
        return False  # malformed lock => treat as free


def acquire_checkout(backend: StorageBackend, tenant_id: str, drawing_id: str,
                     holder: str, ttl_s: float) -> bool:
    """Try to take the single-writer lock. Returns True if acquired/refreshed.

    A lock held by ANOTHER holder and still within TTL blocks (False). An expired
    lock (expires < now) is treated as free. The same holder re-acquiring refreshes.
    """
    tid = sanitize_id(tenant_id)
    did = sanitize_id(drawing_id)
    if authority_mode() == "postgres":
        if float(ttl_s) <= 0:
            raise ValueError("checkout ttl must be positive")
        db = _db()

        def operation(conn):
            row = conn.execute(
                """
                SELECT checkout_holder, checkout_expires_at
                FROM drawing_store_manifests
                WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
                FOR UPDATE
                """,
                {"tenant": tid, "drawing": did},
            ).fetchone()
            if row is None:
                raise KeyError(manifest_key(tid, did))
            if (
                row["checkout_holder"] is not None
                and row["checkout_expires_at"] > datetime.now(timezone.utc)
                and row["checkout_holder"] != holder
            ):
                return False
            conn.execute(
                """
                UPDATE drawing_store_manifests
                SET checkout_holder = %(holder)s,
                    checkout_fence = checkout_fence + 1,
                    checkout_acquired_at = NOW(),
                    checkout_expires_at =
                      NOW() + (%(ttl)s * INTERVAL '1 second'),
                    updated_at = NOW()
                WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
                """,
                {
                    "holder": str(holder), "ttl": float(ttl_s),
                    "tenant": tid, "drawing": did,
                },
            )
            return True

        return db.run_transaction(operation, isolation="serializable")
    m = load_manifest(backend, tid, did)
    now = datetime.now(timezone.utc)
    co = m.get("checkout")

    if _is_active(co, now) and co.get("holder") != holder:
        return False

    m["checkout"] = {
        "holder": holder,
        "acquired": now.isoformat(),
        "expires": (now + timedelta(seconds=float(ttl_s))).isoformat(),
    }
    save_manifest(backend, tid, did, m)
    return True


def release_checkout(backend: StorageBackend, tenant_id: str, drawing_id: str,
                     holder: str | None = None) -> bool:
    """Release the lock. If `holder` is given, only that holder may release an
    ACTIVE lock (an expired lock is releasable by anyone). Returns True if cleared.
    """
    tid = sanitize_id(tenant_id)
    did = sanitize_id(drawing_id)
    if authority_mode() == "postgres":
        db = _db()

        def operation(conn):
            row = conn.execute(
                """
                SELECT checkout_holder, checkout_expires_at
                FROM drawing_store_manifests
                WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
                FOR UPDATE
                """,
                {"tenant": tid, "drawing": did},
            ).fetchone()
            if row is None:
                raise KeyError(manifest_key(tid, did))
            if row["checkout_holder"] is None:
                return False
            if (
                holder is not None
                and row["checkout_holder"] != holder
                and row["checkout_expires_at"] > datetime.now(timezone.utc)
            ):
                return False
            conn.execute(
                """
                UPDATE drawing_store_manifests
                SET checkout_holder = NULL, checkout_acquired_at = NULL,
                    checkout_expires_at = NULL, updated_at = NOW()
                WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
                """,
                {"tenant": tid, "drawing": did},
            )
            return True

        return db.run_transaction(operation, isolation="serializable")
    m = load_manifest(backend, tid, did)
    co = m.get("checkout")
    if not co:
        return False

    if holder is not None and co.get("holder") != holder:
        if _is_active(co, datetime.now(timezone.utc)):
            return False  # refuse to steal an active lock from another holder

    m["checkout"] = None
    save_manifest(backend, tid, did, m)
    return True
