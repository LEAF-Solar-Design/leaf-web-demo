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
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # sibling imports
import client  # noqa: E402  (OSSBackend delegates to client's OSS helpers)
import requests  # noqa: E402  (only OSSBackend.exists references requests.HTTPError)

# --------------------------------------------------------------------------- #
# Key scheme
# --------------------------------------------------------------------------- #
_ID_DISALLOWED = re.compile(r"[^a-z0-9-]+")
_ID_COLLAPSE = re.compile(r"-+")

# The canonical key contract every stored version key must satisfy.
VERSION_KEY_RE = re.compile(r"^tenants/[a-z0-9-]+/drawings/[a-z0-9-]+/v/\d{8}\.dwg$")


def sanitize_id(raw: str) -> str:
    """Lowercase and reduce an id to `[a-z0-9-]`, collapsing runs of '-'.

    Guarantees the result is non-empty and safe as a single OSS key path segment.
    (drawing_id is ideally a UuidV7 to match cadwalk-studio `UuidV7`; a uuid string
    is already `[a-z0-9-]`, so it passes through unchanged.)
    """
    s = _ID_COLLAPSE.sub("-", _ID_DISALLOWED.sub("-", str(raw).strip().lower())).strip("-")
    if not s:
        raise ValueError(f"id {raw!r} sanitizes to empty (needs at least one [a-z0-9] char)")
    return s


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
        os.makedirs(os.path.dirname(p), exist_ok=True)
        # atomic-ish write (tmp in the same dir, then replace) so a crash mid-write
        # never leaves a half-written immutable version or manifest behind.
        tmp = p + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(bytes(data))
        os.replace(tmp, p)

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


def load_manifest(backend: StorageBackend, tenant_id: str, drawing_id: str) -> dict:
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


# --------------------------------------------------------------------------- #
# Version primitives
# --------------------------------------------------------------------------- #
def ingest_drawing(backend: StorageBackend, tenant_id: str, local_path: str,
                   drawing_id: str | None = None) -> dict:
    """PUT version 1 of a new drawing + write its initial manifest.

    Returns {"drawing_id": <id>, "version": 1}. Refuses to clobber an existing
    drawing (use put_drawing to append versions).
    """
    tid = sanitize_id(tenant_id)
    did = sanitize_id(drawing_id) if drawing_id else new_drawing_id()

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


def put_drawing(backend: StorageBackend, tenant_id: str, drawing_id: str, local_path: str,
                parent_version, meta: dict | None = None) -> int:
    """Append a NEW immutable version (v = latest+1, parent = parent_version) and
    advance head + latest. This is the primitive the DWG write path calls.

    Returns the new integer version. A write from a non-latest head branches the
    DAG (parent linkage is enough; we keep the model simple).
    """
    tid = sanitize_id(tenant_id)
    did = sanitize_id(drawing_id)
    m = load_manifest(backend, tid, did)

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
