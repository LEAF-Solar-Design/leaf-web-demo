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

import contextlib
import errno
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
import threading
import time
import uuid
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

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


# RESERVED holder id for a PRODUCT write whose caller named no session.
#
# The route used to default an absent holder to the tenant id, on the theory that
# a tenant-shaped id never matches a `sess-` lock. That is only true when the lock
# was taken by a session. POST .../checkout defaults ITS holder to the tenant id
# too, so a drawing locked with the documented empty body is held by the tenant id
# — and an unnamed writer then MATCHED it and published under someone else's
# lease. The tenant id was serving as both the "nobody named themselves" sentinel
# and a real holder value; conflating the two is what reopened the hole this
# module exists to close.
#
# Guarded on BOTH sides, because either alone is insufficient:
#
#   * acquire_checkout REFUSES this id, so no NEW lock can be taken as it. Needed
#     because `holder` is caller-supplied there — otherwise a caller could take
#     the lock as the sentinel and every unnamed write would match it.
#   * the write check refuses this id UNCONDITIONALLY against an active lock,
#     without comparing it to the stored holder. Needed because the acquire-time
#     refusal cannot see state that already exists: a lock taken as the sentinel
#     under an earlier release, restored from a backup, or written straight into
#     a manifest is persisted and would compare EQUAL. The reserved id is a
#     statement about the CALLER ("named nobody"), so it is never a valid answer
#     to "who owns this lock", whatever the stored value says.
#
# So an unnamed product write is refused against ANY active lock, whatever shape
# its holder has, and publishes normally on an unlocked drawing. Distinct from
# `holder=None`, which means "not a product write at all" (ingest, the offline
# harness, this module's own tests) and skips the check entirely.
ANONYMOUS_HOLDER = "anonymous:unnamed-writer"


class CheckoutParamError(ValueError):
    """Bad INPUT to a checkout request: a reserved holder id, or a non-positive
    TTL. Narrow on purpose.

    Subclasses ValueError so existing callers that map `(KeyError, ValueError)`
    to 400 keep working unchanged. It exists so the route can catch THIS instead
    of bare ValueError: `acquire_checkout` also decodes the stored manifest, and
    a corrupt one raises JSONDecodeError — itself a ValueError — which a broad
    catch would report to the caller as "your request was malformed" when the
    truth is that the stored object is damaged and nothing the caller sends will
    help.
    """


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


def checkout_lock_key(tenant_id: str, drawing_id: str) -> str:
    """Object key of the drawing's cross-process checkout lock file.

    NOT the manifest. `save_manifest` replaces the manifest's inode on every
    save, and a lock held on a replaced inode is one no later caller can reach,
    which is a lock that excludes nobody.

    NOT inside the drawing's own directory either, which is the harder
    constraint and the one that decides where this lives. The lock's identity IS
    its inode, so the file has to outlive everything that clears a drawing.
    `server/guest_uploads.py` wipes a failed upload attempt by deleting every
    child of the drawing directory except `upload.state.json`
    (`_wipe_failed_attempt_files`) and leaves the DRAWING itself alive. A lock
    file in there would be unlinked from under a live holder: on POSIX the
    holder keeps the unlinked inode and its lock, the next caller creates a
    fresh inode and locks that instead, and both then run inside the section
    this exists to keep to one. The guest purge (`purge_expired`) removes the
    whole drawing directory for the same reason of being a directory cleaner.

    So the lock lives under its own top-level prefix that no drawing-directory
    walker descends into, and is created once and never unlinked.
    """
    return (f"checkout-locks/{sanitize_id(tenant_id)}/"
            f"{sanitize_id(drawing_id)}.checkout-lock")


# --------------------------------------------------------------------------- #
# OS-level advisory locking (the cross-process half of the legacy checkout)
# --------------------------------------------------------------------------- #
try:  # POSIX (the production deployment)
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None
try:  # Windows (the development host)
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None


# Budget for ONE checkout read-modify-write to hold the drawing. The section is
# a manifest load, an edit, and a save, so a holder that has not finished inside
# this is stuck rather than busy, and the caller is better served by an error
# than by a request that never returns.
_CHECKOUT_LOCK_TIMEOUT_S = 30.0


class CrossProcessLockUnavailable(RuntimeError):
    """This backend has no OS-level lock to take, so it cannot serialize a
    read-modify-write against a second process."""


class CheckoutLockTimeout(RuntimeError):
    """The checkout lock stayed held past its budget.

    Raised rather than reported as a refused acquire. "Someone else holds the
    checkout" and "this host could not find out" are different answers, and
    returning None for the second would name a lease holder that may not exist.
    """


class CrossProcessCheckoutLockMissing(RuntimeWarning):
    """Emitted per legacy checkout served by a backend that cannot exclude a
    second OS process, so the degradation is visible in logs and assertable in
    tests instead of silent."""


# The errnos that mean "another holder has it". Anything else is a lock that
# waiting cannot win — a bad descriptor, exhausted lock resources, a filesystem
# with no lock support — and is raised straight away, because retrying it for
# the whole budget and then reporting contention would name a holder that does
# not exist.
_LOCK_CONTENDED = frozenset(
    code for code in (
        getattr(errno, "EACCES", None),
        getattr(errno, "EAGAIN", None),
        getattr(errno, "EWOULDBLOCK", None),
        getattr(errno, "EDEADLK", None),
        getattr(errno, "EDEADLOCK", None),
    ) if code is not None
)


def _try_os_lock(handle) -> bool:
    """Take an exclusive OS lock on an open file WITHOUT blocking.

    Non-blocking on both platforms on purpose. The blocking primitives disagree:
    POSIX `LOCK_EX` waits forever, while Windows `LK_LOCK` gives up after about
    ten seconds and raises. Polling a non-blocking attempt against one deadline
    makes the dev host and the production host fail the same way at the same
    point, which is the only version of this that can be tested on either.
    """
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif msvcrt is not None:
            # Locks a byte range from the CURRENT position, so anchor it: every
            # caller must contend for the same byte 0 or they exclude nobody.
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover - no such host is supported
            raise CrossProcessLockUnavailable(
                "neither fcntl nor msvcrt is available on this interpreter")
        return True
    except OSError as exc:
        if exc.errno in _LOCK_CONTENDED:
            return False
        raise


# --------------------------------------------------------------------------- #
# Storage backend abstraction (so tests run fully offline)
# --------------------------------------------------------------------------- #
class StorageBackend:
    """Minimal blob interface: get/put/exists over opaque string keys."""

    # Is a checkout read-modify-write on this backend safe against a SECOND OS
    # process? A declared, inspectable property of the backend rather than a
    # thing the caller infers, because the answer changes what a successful
    # acquire actually promises. False here so a backend added later has to
    # answer the question deliberately instead of inheriting a guarantee it
    # cannot keep.
    cross_process_checkout_safe = False

    def cross_process_lock(self, key: str):
        """Return a context manager holding an exclusive OS lock for `key`.

        Raises rather than handing back a no-op: a lock that excludes nobody but
        reports success is worse than no lock, because the caller then believes
        the read-modify-write it wraps is serialized.
        """
        raise CrossProcessLockUnavailable(
            f"{type(self).__name__} cannot take an OS-level lock")

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

    # A dict on one interpreter's heap. No second process can reach these blobs
    # at all, so a checkout read-modify-write here is already excluded from
    # every other process and the no-op below is the CORRECT implementation
    # rather than a missing one.
    cross_process_checkout_safe = True

    @contextlib.contextmanager
    def cross_process_lock(self, key: str):
        yield

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

    # Object storage has no file descriptor to lock and no compare-and-swap, so
    # there is nothing here that could exclude a second process. Stated
    # explicitly rather than inherited so the gap is greppable, and left as a
    # REFUSAL (the inherited `cross_process_lock` raises) rather than a no-op:
    # a lock object that grants everyone would make the legacy OSS path look
    # fixed while leaving it exactly as it is. The caller reads this flag,
    # warns, and proceeds with the in-process lock alone. The real answer for a
    # multi-replica deployment is the postgres authority.
    cross_process_checkout_safe = False

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

    # One shared filesystem, so a sidecar lockfile beside the manifest gives a
    # real OS-level exclusion between processes. See `cross_process_lock`.
    cross_process_checkout_safe = True

    def __init__(self, root_dir: str) -> None:
        self.root = os.path.abspath(root_dir)

    @contextlib.contextmanager
    def cross_process_lock(self, key: str):
        """Hold an exclusive OS lock on the sidecar file at `key`.

        Advisory locking on an open descriptor, not a lock directory with a
        staleness heuristic. That choice is the point: the kernel drops the lock
        when the descriptor closes, which covers a normal exit, an exception in
        the body, and the holder being killed. So a crashed process cannot wedge
        the drawing, and nothing here ever has to guess whether a live holder is
        dead. Guessing would be unsound at any threshold, since evicting a
        merely-slow holder puts two writers inside the section this exists to
        keep to one.

        Bounded, so a wedged holder surfaces as a timeout rather than a request
        that never returns.
        """
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        deadline = time.monotonic() + _CHECKOUT_LOCK_TIMEOUT_S
        # "a+b" creates on first use and never truncates, so an existing lock
        # file is opened, not replaced -- replacing it would hand this caller a
        # different inode from the one the current holder owns.
        with open(path, "a+b") as handle:
            while not _try_os_lock(handle):
                if time.monotonic() >= deadline:
                    raise CheckoutLockTimeout(
                        f"checkout lock held by another process past "
                        f"{_CHECKOUT_LOCK_TIMEOUT_S}s: {path}")
                time.sleep(0.01)
            # Closing the handle releases the lock on EVERY exit path, so the
            # body needs no unlock of its own.
            yield

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
        _authorize_checkout_row(row, holder, fence, action="publish a version")
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


def _refuse_unless_owner(current_holder: Any, current_fence: Any,
                         holder: str | None, fence: int | None,
                         action: str) -> None:
    """The ownership comparison itself, over an ACTIVE lock's (holder, fence).

    Shared by the two shapes an active lock arrives in — a `load_manifest`
    checkout dict (`_authorize_checkout_view`) and a `FOR UPDATE` manifest row
    (`_authorize_checkout_row`) — so the postgres and legacy authorities cannot
    drift on what "you are not the holder" means. `action` only names the
    attempted mutation in the message ("publish a version", "undo", "redo").

    `holder` and `fence` are INDEPENDENT claims and each is checked only when
    made. A caller that names no session but presents a stale generation still
    has that generation checked — skipping it would let the pre-flight permit
    what the commit refuses, which is the one thing these checks must never do.
    """
    if holder is not None:
        # An anonymous writer NEVER mutates under an active lock, whoever holds
        # it. Checked before the equality below rather than relying on the
        # acquire-time refusal, because that refusal only guards NEW acquisitions:
        # a lock taken as the sentinel under an earlier release, or restored from
        # a backup, or written straight into a manifest, is already persisted and
        # would compare EQUAL to the anonymous caller and let it through. The
        # reserved id is a statement about the CALLER ("named nobody"), so it can
        # never be a valid answer to "who owns this lock", regardless of what the
        # stored value happens to say.
        if str(holder) == ANONYMOUS_HOLDER:
            raise CheckoutDenied(
                f"drawing is checked out by {current_holder!r}; "
                f"a writer that names no session may not {action}")
        if str(current_holder) != str(holder):
            raise CheckoutDenied(
                f"drawing is checked out by {current_holder!r}; "
                f"{holder!r} may not {action}")
    # A caller-supplied fence is what makes this a fencing token rather than a
    # churn detector: a writer whose lease lapsed and was re-acquired holds a
    # stale generation and is refused here even when the holder id matches,
    # which is exactly the resumed-after-a-pause writer the fence exists for.
    if fence is not None and current_fence is not None \
            and int(current_fence) != int(fence):
        raise CheckoutDenied(
            f"checkout fence {int(fence)} is stale "
            f"(current generation {int(current_fence)}); "
            f"re-acquire the checkout first")


def _authorize_checkout_row(row: Any, holder: str | None, fence: int | None,
                            action: str) -> None:
    """`_refuse_unless_owner` over a `FOR UPDATE`-locked manifest row.

    Callers have already SELECTed `checkout_holder`, `checkout_fence` and
    `checkout_expires_at` under the row lock, so nothing can change between this
    check and the write it guards. An absent or expired lock is permitted — the
    same rule the legacy path applies, and the store's rule everywhere else
    (`acquire_checkout` re-grants an expired lock to anyone).

    Only a caller claiming NEITHER a holder nor a fence skips the check, matching
    `_authorize_checkout_view`: the two claims are independent, and returning
    early on `holder is None` alone would drop a stale-generation claim that the
    postgres commit path has always honoured.
    """
    if holder is None and fence is None:
        return
    if (row["checkout_holder"] is None
            or row["checkout_expires_at"] <= datetime.now(timezone.utc)):
        return
    _refuse_unless_owner(row["checkout_holder"], row["checkout_fence"],
                         holder, fence, action)


def _authorize_checkout_view(co: dict | None, holder: str | None,
                             fence: int | None = None,
                             action: str = "publish a version") -> None:
    """The single-writer rule over a NORMALIZED checkout dict ({holder, expires,
    fence?}) — the shape both authorities hand back from `load_manifest`.

    Raises CheckoutDenied when an ACTIVE lock is held by someone other than
    `holder`, or carries a generation other than `fence`. Two cases stay
    deliberately permitted, because they are the behaviour the product has
    always had and neither lets a caller write under another session's lease:

      * no lock at all, and
      * an EXPIRED lock (the store's rule everywhere else is that an expired lock
        is free — acquire_checkout re-grants it to anyone).

    A `fence` is checked only when the lock actually carries one. Locks taken
    before `acquire_checkout` stamped a generation do not: that branch used to
    write holder/acquired/expires and nothing else, so for a lock still held from
    before this module fenced the legacy authority the holder comparison IS the
    whole guarantee. Every lock acquired since carries one.
    """
    # `holder` and `fence` are INDEPENDENT claims, and each is checked only when
    # made. Returning early on `holder is None` skipped the fence too, so a caller
    # naming no session but presenting a STALE fence passed here and was refused
    # at the commit — `_pg_put` checks any supplied fence regardless of holder.
    # That is the pre-flight promising something the commit does not honour, which
    # is the one thing this function must never do.
    if holder is None and fence is None:
        return
    if not _is_active(co, datetime.now(timezone.utc)):
        return
    _refuse_unless_owner(co.get("holder"), co.get("fence"), holder, fence, action)


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

    It must therefore refuse everything the commit would refuse, or the promise is
    empty. Under the POSTGRES authority the commit ALSO requires a live checkout
    (`_pg_put`), which the shared legacy predicate does not — it treats no lock
    and an expired lock as free. Pre-flighting only the holder rule there let an
    unlocked live write pass here, buy an APS WorkItem, and fail at publish: the
    exact bill this function exists to avoid. So that requirement is mirrored
    below, raising the same ValueError the commit raises, and the two authorities
    keep their genuinely different rules rather than being forced to agree.
    """
    tid = sanitize_id(tenant_id)
    did = sanitize_id(drawing_id)
    if authority_mode() == "postgres":
        # Mirrors _pg_put's precondition, and like it applies whether or not the
        # caller named itself.
        m = load_manifest(backend, tid, did)
        if not _is_active(m.get("checkout"), datetime.now(timezone.utc)):
            raise ValueError("an active checkout is required to publish a version")
        _authorize_checkout_view(m.get("checkout"), holder, fence)
        return
    # Same independence as the predicate: a caller naming no session but claiming
    # a fence still has that claim checked, or the pre-flight permits what the
    # commit refuses. Only a caller claiming NEITHER skips the read.
    if holder is None and fence is None:
        return
    m = load_manifest(backend, tid, did)
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

    SCOPE, stated plainly so the guarantee is not overread. `holder` is a caller-
    supplied label bound only to the tenant, and GET /versions publishes the
    current holder, so a MALICIOUS same-tenant caller can read it and present it.
    Against that caller this is a coordination lock, not an authorization
    boundary — the same property the POST/DELETE .../checkout routes have always
    had, and this check does not weaken it. What it does close is the accidental
    cross-session write: no caller publishes under a lock it has not at least
    named, and an unnamed caller (`ANONYMOUS_HOLDER`) publishes under no lock at
    all. Making it a true boundary needs an opaque server-issued checkout
    capability that never travels in a readable field; tracked separately.

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
    # Under the SAME guard as acquire/release, because this is a load, edit,
    # save of the same one manifest document and `save_manifest` writes the
    # WHOLE document back. Publishing a version therefore rewrites `checkout`
    # and `checkout_fence` to whatever this caller happened to read, so a commit
    # whose load precedes a concurrent acquire erases the new lease and restores
    # the older generation. The next acquire then hands out that generation a
    # SECOND time, and two capabilities verify against one lease -- the same
    # ownership bypass the guard on acquire exists to prevent, reached through a
    # different door. Two concurrent commits also lose a version from
    # `m["versions"]` the same way.
    with _legacy_checkout_guard(backend, tid, did):
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


def undo(backend: StorageBackend, tenant_id: str, drawing_id: str, *,
         holder: str | None = None, fence: int | None = None) -> int:
    """Repoint head to the current head's parent (no object deletion => redo-able).

    `latest` is left unchanged so the undone version's object still resolves.

    SINGLE-WRITER AUTHORIZATION (`holder`, `fence`): identical to `put_drawing`'s,
    and for the same reason — moving head is a MUTATION of the drawing every other
    session reads, so it is not a lesser act than publishing a version. Until this
    existed, undo/redo were the way around the write check: a caller refused at
    `put_drawing` could still walk the same drawing's head backwards under someone
    else's lease. An absent or expired lock permits the move (the store's rule
    everywhere else); an ACTIVE lock requires the caller to own it. Both default
    to None, which skips the check entirely, so ingest paths, the offline harness
    and this module's own tests are unchanged.
    """
    tid = sanitize_id(tenant_id)
    did = sanitize_id(drawing_id)
    if authority_mode() == "postgres":
        db = _db()

        def operation(conn):
            row = conn.execute(
                """
                SELECT m.head, m.checkout_holder, m.checkout_fence,
                       m.checkout_expires_at, v.parent_version
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
            _authorize_checkout_row(row, holder, fence, action="undo")
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
    # Guarded for the reason spelled out in `put_drawing`: moving head is a
    # whole-manifest rewrite, so it carries `checkout` and `checkout_fence` back
    # with it and can erase a lease acquired since this load.
    with _legacy_checkout_guard(backend, tid, did):
        m = load_manifest(backend, tid, did)
        _authorize_checkout_view(m.get("checkout"), holder, fence, action="undo")

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


def redo(backend: StorageBackend, tenant_id: str, drawing_id: str, *,
         holder: str | None = None, fence: int | None = None) -> int:
    """Re-advance head one step toward `latest` — the inverse of `undo`.

    `undo` repointed head at its parent WITHOUT deleting any object, so the
    forward chain (head -> ... -> latest via parent linkage) is still intact.
    `redo` walks from `latest` back along `parent` pointers until it finds the
    version whose parent IS the current head; that version is head's immediate
    child on the path to latest, and head is repointed onto it. Stepping one
    version at a time makes repeated redo mirror repeated undo.

    Raises ValueError when head is already `latest` (nothing to redo) or the
    forward chain is broken (no child of head leads to latest).

    `holder`/`fence` carry the same single-writer authorization as `undo` — see
    that docstring; the two are one surface and a check on only one of them is
    no check at all.
    """
    tid = sanitize_id(tenant_id)
    did = sanitize_id(drawing_id)
    if authority_mode() == "postgres":
        db = _db()

        def operation(conn):
            manifest = conn.execute(
                """
                SELECT head, checkout_holder, checkout_fence,
                       checkout_expires_at
                FROM drawing_store_manifests
                WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
                FOR UPDATE
                """,
                {"tenant": tid, "drawing": did},
            ).fetchone()
            if manifest is None or manifest["head"] is None:
                raise KeyError(manifest_key(tid, did))
            _authorize_checkout_row(manifest, holder, fence, action="redo")
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
    # Guarded for the reason spelled out in `put_drawing`.
    with _legacy_checkout_guard(backend, tid, did):
        m = load_manifest(backend, tid, did)
        _authorize_checkout_view(m.get("checkout"), holder, fence, action="redo")

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


def checkout_active(co: dict | None) -> bool:
    """Is this manifest `checkout` a LIVE lease right now?

    The one place outside this module that asks — the mutating drawing routes,
    which need proof of ownership for an active lock and nothing for an absent or
    expired one — so the "expired means free" rule is read from here rather than
    re-implemented against a timestamp string.
    """
    return _is_active(co, datetime.now(timezone.utc))


_LEGACY_CHECKOUT_LOCKS: dict[str, threading.Lock] = {}
_LEGACY_CHECKOUT_LOCKS_GUARD = threading.Lock()


def _legacy_checkout_lock(tid: str, did: str) -> threading.Lock:
    """The per-drawing, per-PROCESS half of the checkout guard.

    Kept as the outer layer of `_legacy_checkout_guard` because it is far
    cheaper than a syscall: threads of one app process settle here and never
    reach the filesystem. It excludes only threads. The cross-process half is
    the backend's OS lock.
    """
    key = f"{tid}/{did}"
    with _LEGACY_CHECKOUT_LOCKS_GUARD:
        lock = _LEGACY_CHECKOUT_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LEGACY_CHECKOUT_LOCKS[key] = lock
        return lock


@contextlib.contextmanager
def _legacy_checkout_guard(backend: StorageBackend, tid: str, did: str):
    """Serialize EVERY legacy read-modify-write of one drawing's manifest.

    The legacy manifest is a load, edit, save with nothing holding the record in
    between (see the module note on non-atomic writes). Two concurrent acquires
    of a FREE drawing therefore both read generation N, both compute N+1, and
    the second save wins — leaving ONE persisted lease that two callers were
    each told they had taken. Both then hold a capability that verifies against
    it, because the capability binds the generation and the generations are
    equal, which is the ownership bypass the fence exists to prevent. A release
    racing an acquire loses the same update in the other direction and lets a
    generation repeat.

    EVERY writer, not just acquire and release. `save_manifest` writes the whole
    document, so `put_drawing`, `undo` and `redo` each carry `checkout` and
    `checkout_fence` back with them from whenever they loaded. A commit that
    loaded before a concurrent acquire will, on save, erase the new lease and
    restore the older generation, and the next acquire then issues that
    generation a SECOND time — the same bypass by a different route. Guarding
    only the checkout calls would leave that route open, so all five run here.

    Two layers close the window, because one process is two different problems.
    The threading lock settles the app's own threads without a syscall. Under it
    an OS advisory lock on a separate lock file settles a SECOND PROCESS reading
    the same store directory, which the threading lock cannot see: a per-process
    dict is not shared state, so two replicas each hold their own uncontended
    lock and both walk into the section.

    NOT REENTRANT. The threading half is a plain `Lock`, so a guarded function
    calling another guarded function would wait out `_CHECKOUT_LOCK_TIMEOUT_S`
    and then raise. None of the five call each other; keep it that way.

    WHAT THIS DOES NOT DO. It is not a distributed lock and must not grow into
    one. It reaches exactly as far as the OS lock does, which is one shared
    filesystem — so replicas on one host, or on a shared volume, and nothing
    beyond that. A backend with no descriptor to lock (`OSSBackend`) cannot be
    covered at all; rather than pretend otherwise this warns
    `CrossProcessCheckoutLockMissing` and runs with the threading lock alone,
    which is the behaviour that was previously silent and unlabelled.

    It also does not coordinate with the guest-upload purge, which deletes a
    whole drawing directory on its own per-drawing lock. `FilesystemBackend.put`
    recreates missing parents, so a manifest save that was already in flight can
    put the drawing back after the purge recorded it deleted. That is unchanged
    by this guard — every legacy writer, including the pre-existing acquire, has
    always been able to do it — and closing it means giving the purge and the
    store one shared lock, which is tracked separately.

    The documented answer above that boundary is unchanged and is postgres: it
    reads `FOR UPDATE` and increments in one statement, so it needs none of
    this. What this removes is the narrower, previously-standing caveat that the
    legacy path was safe only for a SINGLE app process.
    """
    thread_lock = _legacy_checkout_lock(tid, did)
    # Bounded, so a queue of in-process callers cannot wait without a limit
    # before the cross-process deadline below even starts counting.
    if not thread_lock.acquire(timeout=_CHECKOUT_LOCK_TIMEOUT_S):
        raise CheckoutLockTimeout(
            f"in-process checkout lock busy past {_CHECKOUT_LOCK_TIMEOUT_S}s: "
            f"{tid}/{did}")
    try:
        if not backend.cross_process_checkout_safe:
            warnings.warn(
                f"{type(backend).__name__} cannot take an OS-level checkout "
                f"lock, so the {tid}/{did} checkout is serialized within this "
                f"process only; a second replica can still lose an update. Use "
                f"the postgres authority for a multi-replica deployment.",
                CrossProcessCheckoutLockMissing, stacklevel=3)
            yield
            return
        with backend.cross_process_lock(checkout_lock_key(tid, did)):
            yield
    finally:
        thread_lock.release()


def _next_fence(m: dict) -> int:
    """The next legacy lock generation — monotonic ACROSS release.

    Kept at the MANIFEST level (`checkout_fence`) rather than inside the
    `checkout` dict, because `release_checkout` clears that dict: a counter
    living there would restart at 1 on the next acquire, and a REPEATED
    generation is exactly what would let a checkout capability minted for an
    earlier lease verify against a later one. The postgres authority already
    behaves this way — `checkout_fence` is a manifest column that release leaves
    untouched — so this only brings the legacy authority to the same guarantee.

    Falls back to the generation on an in-flight lock so a manifest written
    before this counter existed keeps counting up rather than restarting.
    """
    prior = m.get("checkout_fence")
    if prior is None:
        prior = (m.get("checkout") or {}).get("fence")
    try:
        return int(prior) + 1
    except (TypeError, ValueError):
        return 1


def acquire_checkout(backend: StorageBackend, tenant_id: str, drawing_id: str,
                     holder: str, ttl_s: float, *,
                     expected_fence: int | None = None,
                     strict_owner: bool = False) -> bool:
    """Try to take the single-writer lock. Returns True if acquired/refreshed.

    Thin bool view of `acquire_checkout_fence`, kept because every caller that
    only needs "did I get it" reads better this way. A caller that will MINT a
    capability must use `acquire_checkout_fence` instead: it needs the
    generation this call stamped, and re-reading the manifest to find one is a
    different question with a different answer (see that function's note).
    """
    return acquire_checkout_fence(
        backend, tenant_id, drawing_id, holder, ttl_s,
        expected_fence=expected_fence, strict_owner=strict_owner) is not None


def acquire_checkout_fence(backend: StorageBackend, tenant_id: str, drawing_id: str,
                           holder: str, ttl_s: float, *,
                           expected_fence: int | None = None,
                           strict_owner: bool = False) -> int | None:
    """Take the single-writer lock and return the generation THIS call stamped,
    or None if the lock was not granted.

    WHY THE GENERATION IS RETURNED RATHER THAN RE-READ. A checkout capability is
    bound to a lock generation, and the mint has to use the generation this
    acquire wrote. Re-loading the manifest afterwards asks "what generation is
    current now", which is not the same question: the lease taken here can lapse
    between the write and the re-read (the TTL is caller-supplied and only has
    to be positive), another session can acquire in that gap, and the re-read
    then returns THAT session's generation. Minting against it hands this caller
    a validly-signed capability for someone else's lease — the exact ownership
    bypass the capability exists to prevent, since verification recomputes the
    tag with the PRESENTER's own subject and so cannot tell the two apart.

    Returning it from inside the same transaction (postgres) / the same
    load-modify-save (legacy) removes the gap: there is no second read to race.

    Every successful acquire stamps a NEW generation into `checkout.fence`, so no
    two leases of one drawing ever share a generation.

    Two rules for an ACTIVE lock, selected by `strict_owner`:

      * ``strict_owner=False`` (default, unchanged): a lock held by ANOTHER
        holder blocks (False); the SAME holder re-acquiring refreshes. Holder
        equality is the whole test — a coordination rule, not an authorization
        one, because `holder` is caller-supplied and GET /versions publishes it,
        so anyone who can read it can present it.
      * ``strict_owner=True``: holder equality is NOT consulted. An active lock
        is refreshed only when `expected_fence` equals its current generation,
        and refused outright when `expected_fence` is None. The route passes a
        fence only after verifying an opaque server-issued capability
        (`server/checkout_capability.py`), so this is what makes taking over a
        LIVE lease require proof of that lease instead of knowledge of a readable
        label. Without it, session B could read A's holder from GET /versions,
        re-acquire as A (a "refresh"), and be handed a capability of its own.

    An EXPIRED lock (expires <= now) is free under both rules and is re-granted
    to anyone — the store's rule everywhere else, and what keeps a forgotten lock
    from wedging a drawing forever.

    `ANONYMOUS_HOLDER` is REFUSED. `holder` is caller-supplied on this route, so
    without this a caller could take the lock AS the unnamed-writer sentinel and
    every unnamed write would then match it — turning the fail-closed default
    back into a fail-open one. Nothing legitimate asks for that id.
    """
    if str(holder) == ANONYMOUS_HOLDER:
        raise CheckoutParamError(
            f"{ANONYMOUS_HOLDER!r} is reserved and cannot hold a checkout")
    tid = sanitize_id(tenant_id)
    did = sanitize_id(drawing_id)
    if authority_mode() == "postgres":
        if float(ttl_s) <= 0:
            raise CheckoutParamError("checkout ttl must be positive")
        db = _db()

        def operation(conn):
            row = conn.execute(
                """
                SELECT checkout_holder, checkout_expires_at, checkout_fence
                FROM drawing_store_manifests
                WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
                FOR UPDATE
                """,
                {"tenant": tid, "drawing": did},
            ).fetchone()
            if row is None:
                raise KeyError(manifest_key(tid, did))
            active = (
                row["checkout_holder"] is not None
                and row["checkout_expires_at"] > datetime.now(timezone.utc)
            )
            if active and strict_owner:
                # Compare-and-swap on the generation, under the same FOR UPDATE
                # row lock the UPDATE below runs in, so a lease cannot be
                # re-acquired between the check and the write.
                if expected_fence is None:
                    return None
                if int(row["checkout_fence"]) != int(expected_fence):
                    return None
            elif active and row["checkout_holder"] != holder:
                return None
            # RETURNING, so the generation comes back from the same statement
            # that wrote it, inside the same row lock. Reading it afterwards
            # would reintroduce the race this function exists to close.
            written = conn.execute(
                """
                UPDATE drawing_store_manifests
                SET checkout_holder = %(holder)s,
                    checkout_fence = checkout_fence + 1,
                    checkout_acquired_at = NOW(),
                    checkout_expires_at =
                      NOW() + (%(ttl)s * INTERVAL '1 second'),
                    updated_at = NOW()
                WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
                RETURNING checkout_fence
                """,
                {
                    "holder": str(holder), "ttl": float(ttl_s),
                    "tenant": tid, "drawing": did,
                },
            ).fetchone()
            return int(written["checkout_fence"])

        return db.run_transaction(operation, isolation="serializable")
    with _legacy_checkout_guard(backend, tid, did):
        m = load_manifest(backend, tid, did)
        now = datetime.now(timezone.utc)
        co = m.get("checkout")

        if _is_active(co, now):
            if strict_owner:
                if expected_fence is None:
                    return None
                if int((co or {}).get("fence") or 0) != int(expected_fence):
                    return None
            elif co.get("holder") != holder:
                return None

        fence = _next_fence(m)
        m["checkout_fence"] = fence      # monotonic; survives release (see _next_fence)
        m["checkout"] = {
            "holder": holder,
            "acquired": now.isoformat(),
            "expires": (now + timedelta(seconds=float(ttl_s))).isoformat(),
            "fence": fence,
        }
        save_manifest(backend, tid, did, m)
        return fence


def release_checkout(backend: StorageBackend, tenant_id: str, drawing_id: str,
                     holder: str | None = None, *,
                     expected_fence: int | None = None) -> bool:
    """Release the lock. Returns True if cleared.

    Two ways to name yourself, matching `acquire_checkout`:

      * `holder` alone — only that holder may release an ACTIVE lock (an expired
        lock is releasable by anyone). Caller-supplied and readable, so this is
        coordination, not authorization.
      * `expected_fence` — the lease GENERATION the caller proved it owns, via
        the opaque capability the route verified. When given it REPLACES the
        holder comparison: an active lock is cleared only if its generation still
        matches, so a release cannot land on a lease that was re-acquired between
        the capability check and this call.
    """
    tid = sanitize_id(tenant_id)
    did = sanitize_id(drawing_id)
    if authority_mode() == "postgres":
        db = _db()

        def operation(conn):
            row = conn.execute(
                """
                SELECT checkout_holder, checkout_expires_at, checkout_fence
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
            active = row["checkout_expires_at"] > datetime.now(timezone.utc)
            if active and expected_fence is not None:
                if int(row["checkout_fence"]) != int(expected_fence):
                    return False
            elif (
                active
                and expected_fence is None
                and holder is not None
                and row["checkout_holder"] != holder
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
    with _legacy_checkout_guard(backend, tid, did):
        m = load_manifest(backend, tid, did)
        co = m.get("checkout")
        if not co:
            return False

        if _is_active(co, datetime.now(timezone.utc)):
            # refuse to steal an ACTIVE lock: by proven generation when the caller
            # has one, else by the caller-supplied holder label.
            if expected_fence is not None:
                if int(co.get("fence") or 0) != int(expected_fence):
                    return False
            elif holder is not None and co.get("holder") != holder:
                return False

        m["checkout"] = None
        save_manifest(backend, tid, did, m)
        return True
