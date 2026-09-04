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
from typing import Any, Callable, Optional

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


class ImmutableConflict(ValueError):
    """put_if_absent_or_verify found a DIFFERENT winner already at the key.

    A distinct subclass because a bare ValueError is ambiguous on the live
    backend: OSS credential parsing or response decoding can raise ValueError
    for reasons that are transport problems, not conflicts, and a caller that
    must fail closed on REAL conflicts (write_loop's proof mint) needs to
    tell the two apart without string matching."""


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

    The regex below is an inline LITERAL restatement of
    tenant_id_validator.TENANT_ID_PATTERN, deliberately: a literal fullmatch is a
    taint barrier static analysis can prove (a pattern behind a variable or a
    helper call earns no barrier credit — measured on PR #843's CodeQL run).
    server/tests/test_codeql_barrier_literals.py pins this literal equal to the
    ONE shared canonical-id rule so the two can never drift.
    """
    m = re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", raw) if isinstance(raw, str) else None
    if m is None:
        _tid.validate_tenant_id(raw, kind="id")  # raises the canonical ValueError
        raise ValueError(f"invalid id {raw!r}")  # unreachable belt: fail closed
    return str(m.group(0))


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
    key = f"{drawing_prefix(tenant_id, drawing_id)}/v/{v:08d}.dwg"
    assert VERSION_KEY_RE.match(key), key  # invariant guard
    return key


def drawing_prefix(tenant_id: str, drawing_id: str) -> str:
    """Object-key prefix holding EVERYTHING that belongs to one drawing.

    Every per-drawing key is built from it -- `manifest_key` and
    `drawing_version_key` here, and the guest upload's marker, which the store
    does not name but does have to notice. `checkout_lock_key` is the deliberate
    exception and lives OUTSIDE this prefix; see its own docstring for why that
    is the whole point.

    Having one prefix is what lets `StorageBackend.drawing_object_keys` ask "is
    anything left under this drawing" without the store knowing what any
    particular file is for.
    """
    return f"tenants/{sanitize_id(tenant_id)}/drawings/{sanitize_id(drawing_id)}"


def manifest_key(tenant_id: str, drawing_id: str) -> str:
    """Deterministic object key for the drawing's version index + checkout lock."""
    return f"{drawing_prefix(tenant_id, drawing_id)}/manifest.json"


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

    ONE FILE PER DRAWING, and deliberately not a bounded pool of shared files.
    A shared pool was tried and reverted: because `put_drawing` and
    `ingest_drawing` write the version blob inside the guard (a durable,
    `fsync`ed write), any two drawings sharing a lock file would serialize across
    that write, so one tenant's slow DWG could push an unrelated tenant's
    checkout past `_CHECKOUT_LOCK_TIMEOUT_S` and FAIL its request rather than
    merely delay it. Trading unrelated-tenant availability for inodes is the
    wrong way round. Per drawing, contention is only ever between callers of the
    same drawing, which is the contention that has to exist.

    NAMED BY DIGEST, not by the ids. The prefix is never cleaned, so a raw name
    would leave a tenant id and drawing id readable in a filename long after the
    drawing itself was purged. The digest is stable, so it still identifies the
    drawing to every process, without carrying the ids around.

    RETIRED ONLY FROM INSIDE THE SECTION, BY A CALLER THAT HAS PROVED THE
    DRAWING IS GONE. The rule is one rule and every caller obeys the same three
    steps: hold this lock, establish that the drawing does not exist, then remove
    the file as the LAST act before leaving. What differs is only how each one
    came to be holding a file for a drawing that is not there.
    `server/guest_uploads.py::purge_expired` is the one that DELETES the drawing,
    and now takes THIS lock (`legacy_purge_guard`) rather than only its own
    `drawing_lock`. `_mark_failed` enters with `must_exist=False`, so nothing
    refused it for a missing drawing and merely OPENING the file created one --
    if the drawing is already purged, it retires what it just minted rather than
    leaving a file behind for a drawing no sweep will ever walk again. The
    `not creating` re-check in `_legacy_checkout_guard` covers a writer that lost
    the race to a purge and re-minted the file behind it. And `ingest_drawing`
    retires the file it opened when its own write FAILS -- the drawing it was
    bringing into existence never arrived, so no sweep will ever walk that
    directory.

    WHAT "GONE" MEANS IS THE CALLER'S TO PROVE, and a missing manifest is not
    that proof. A guest upload is alive on `upload.state.json` with no manifest
    at all, so `_mark_failed` requires its marker to be gone AS WELL, and
    `ingest_drawing` requires the drawing's whole key prefix to be empty apart
    from a version-1 blob. That is why this is a per-caller
    obligation and not something the guard does for everyone: the guard knows
    which flags a section carries, never who is still waiting on the drawing.

    The proof they all rely on is the exclusive hold, plus `_HeldCheckoutLock.reclaim`
    refusing if the path no longer names the inode it holds. A caller that was
    parked on the retired inode is caught by the identity check in
    `FilesystemBackend.cross_process_lock` and sent back around to the live file,
    so it never runs on a lock that excludes nobody. Unlinking this file from
    anywhere else -- outside the section, or without that proof -- reintroduces
    exactly the two-callers-one-section defect the paragraph above describes.

    THE NAMING IS PART OF THE CROSS-PROCESS PROTOCOL. Two processes agree on a
    drawing's lock only by deriving the same path, so this function cannot be
    changed under a rolling deploy: mixed versions would take different files and
    both enter the section, which is the very defect the guard exists to prevent.
    Any change here needs a full drain. `test_the_lock_key_mapping_is_pinned`
    pins the mapping against literals so a change cannot be made by accident.
    """
    ident = f"{sanitize_id(tenant_id)}/{sanitize_id(drawing_id)}"
    digest = hashlib.sha256(ident.encode("utf-8")).hexdigest()
    # Two levels so one directory does not collect every drawing's lock file.
    return f"checkout-locks/{digest[:2]}/{digest}.lock"


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


# Budget for ONE guarded section to hold the drawing. Usually that is a manifest
# load, an edit and a save; for `put_drawing` and `ingest_drawing` it also
# includes the durable write of the version blob, which is why this is seconds
# rather than milliseconds. A holder that has not finished inside this is stuck
# rather than busy, and the caller is better served by an error than by a request
# that never returns. Waiters are only ever contending for the SAME drawing (see
# `checkout_lock_key`), so a timeout here names real contention, never a
# coincidence of hashing.
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


# Can this platform unlink a file THIS process holds open? POSIX can (the name
# goes, the inode lives on for every open descriptor); Windows cannot, because
# CPython opens without FILE_SHARE_DELETE, so the unlink fails while ANY process
# has a handle. The two facts drive the same reclaim rule from opposite ends and
# both are safe: see `_HeldCheckoutLock.reclaim`.
_CAN_UNLINK_OPEN_FILE = os.name != "nt"


def _file_identity(handle) -> tuple:
    """The (device, inode) an OPEN descriptor actually refers to."""
    st = os.fstat(handle.fileno())
    return (st.st_dev, st.st_ino)


def _path_identity(path: str) -> tuple | None:
    """The (device, inode) the NAME currently resolves to, or None if it is gone."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _holds_the_live_file(handle, path: str) -> bool:
    """Is the locked descriptor still the file this PATH names?

    A lock's identity is its inode, not its name. Once anything may retire a lock
    file, "I locked the descriptor I opened" stops being enough: a caller parked
    on an inode that was unlinked while it waited wins that lock the moment the
    holder lets go, while the next caller opens the freshly created file and wins
    a different lock, and both then believe they own the drawing. Comparing the
    descriptor against the name is what separates those two cases, and it is
    checked AFTER the lock is taken, because before it the answer can change.

    Both fields matter. `st_ino` alone can repeat across devices, and the store
    root can be a mount of its own.
    """
    return _path_identity(path) == _file_identity(handle)


class _HeldCheckoutLock:
    """A live, VALIDATED hold on one drawing's checkout lock file.

    Handed to the body of `cross_process_lock` so the one caller that is allowed
    to retire the file -- the one that has just deleted the drawing -- can do it
    without reaching around the lock to find the path.
    """

    def __init__(self, path: str, handle) -> None:
        self.path = path
        self.handle = handle
        self.identity = _file_identity(handle)
        # Set when `reclaim` could not unlink in place; see below.
        self.reclaim_on_release = False

    def reclaim(self) -> bool:
        """Retire this lock file. The LAST act inside the section, never earlier.

        Safe on POSIX because the unlink happens while this caller holds the lock
        EXCLUSIVELY, so no second caller is inside the section to be robbed of
        its file, and any caller parked on the retired inode is turned away by
        the identity check in `cross_process_lock` and sent to the live file.
        Nothing may follow it here: the moment the name is gone this caller is
        out of the section, because a later caller can now create a new file and
        legitimately take a different lock.

        Safe on Windows by a different mechanism with the same effect. There an
        open handle blocks the unlink outright, including this caller's own, so
        the removal is deferred to the release below -- and it then succeeds only
        when NO process holds the file, which is precisely "provably unheld".

        Returns whether the file is gone (or provably will be). Advisory: the
        caller's receipt is about the DRAWING, and a lock file left behind is a
        stale inode, not a broken promise.
        """
        if not _holds_the_live_file(self.handle, self.path):
            # Someone re-created the drawing and this name is now THEIR lock.
            # Removing it would hand two callers two different files.
            return False
        if not _CAN_UNLINK_OPEN_FILE:
            self.reclaim_on_release = True
            return True
        try:
            os.remove(self.path)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True

    def _release_reclaim(self) -> None:
        """The Windows half: unlink after the handle is closed.

        Re-checks identity first so a file re-created in between is left alone,
        and tolerates the refusal that means another process still has it open,
        which is the case where leaving it is the CORRECT outcome.
        """
        if _path_identity(self.path) != self.identity:
            return
        try:
            os.remove(self.path)
        except OSError:
            pass


class _NoFileCheckoutLock:
    """The held-lock token for a backend whose lock has no file behind it.

    `reclaim` reports True because there is genuinely nothing left to reclaim,
    not because the request was dropped: `InMemoryBackend` keeps its blobs on one
    interpreter's heap, so a purged drawing leaves no lock file anywhere. Given
    as a real object rather than None so the purge calls the same method on every
    backend and cannot grow a "does this backend have one" branch.
    """

    reclaim_on_release = False

    def reclaim(self) -> bool:
        return True


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

    def drawing_object_keys(self, tenant_id: str, drawing_id: str) -> set | None:
        """Every object key currently under ONE drawing, or None if this backend
        cannot enumerate them.

        `None` does NOT mean "empty". It means "I cannot tell", and the one
        caller of this (`ingest_drawing`'s failure reclaim) must read it as
        "something is there" — the answer that forbids the removal. Defaulting
        to None rather than to an empty set is what keeps a backend added later
        from inheriting a proof it cannot make.

        It exists because "no manifest" is NOT "no drawing". A guest upload is
        alive on `upload.state.json` with no manifest at all, and the store
        cannot ask `server/guest_uploads.py` about it — the store never imports
        that module, which is what keeps the two lock orders from inverting
        (`test_the_store_never_reaches_back_into_the_upload_module`). So the
        question is asked in the store's own vocabulary instead: is there any
        object left under this drawing? A marker answers yes without the store
        having to know what a marker is.
        """
        return None

    def put_if_absent_or_verify(self, key: str, data: bytes) -> None:
        """Publish immutable bytes without replacing a matching winner."""
        payload = bytes(data)
        if not self.exists(key):
            self.put(key, payload)
        existing = self.get(key)
        if existing != payload:
            raise ImmutableConflict("immutable object already exists with different content")


class InMemoryBackend(StorageBackend):
    """In-memory dict backend for tests — makes ZERO network calls."""

    # A dict on one interpreter's heap. No second process can reach these blobs
    # at all, so a checkout read-modify-write here is already excluded from
    # every other process and the no-op below is the CORRECT implementation
    # rather than a missing one.
    cross_process_checkout_safe = True

    @contextlib.contextmanager
    def cross_process_lock(self, key: str):
        yield _NoFileCheckoutLock()

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
            raise ImmutableConflict("immutable object already exists with different content")

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

        VALIDATED, because the file can now be retired. Taking the lock is only
        half the question; the other half is whether the descriptor holding it is
        still the file the path names. A caller that was waiting when the file
        was retired wakes up owning an inode with no name, which excludes nobody,
        so it is sent back around to open and lock the live file instead. See
        `_holds_the_live_file`. Retirement is rare (one purged drawing), so the
        retry costs nothing on the ordinary path.
        """
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        deadline = time.monotonic() + _CHECKOUT_LOCK_TIMEOUT_S
        while True:
            # "a+b" creates on first use and never truncates, so an existing lock
            # file is opened, not replaced -- replacing it would hand this caller
            # a different inode from the one the current holder owns.
            handle = open(path, "a+b")
            held = None
            try:
                while not _try_os_lock(handle):
                    if time.monotonic() >= deadline:
                        raise CheckoutLockTimeout(
                            f"checkout lock held by another process past "
                            f"{_CHECKOUT_LOCK_TIMEOUT_S}s: {path}")
                    time.sleep(0.01)
                if not _holds_the_live_file(handle, path):
                    # Retired while we waited. Drop it and take the live one; the
                    # same deadline applies, so this cannot spin without a limit.
                    if time.monotonic() >= deadline:
                        raise CheckoutLockTimeout(
                            f"checkout lock file kept being retired past "
                            f"{_CHECKOUT_LOCK_TIMEOUT_S}s: {path}")
                    continue
                # Closing the handle releases the lock on EVERY exit path, so the
                # body needs no unlock of its own.
                held = _HeldCheckoutLock(path, handle)
                yield held
                return
            finally:
                handle.close()
                if held is not None and held.reclaim_on_release:
                    held._release_reclaim()

    def _path(self, key: str) -> str:
        # The containment barrier every filesystem sink in this backend goes
        # through. Written in the ONE shape static analysis proves (normalize,
        # then a bare prefix check that raises): a compound condition here
        # earns no barrier credit. Also strictly tighter than before: a key
        # that normalizes to the root itself (empty, ".") is now rejected —
        # no caller builds one, and a bare root is never a valid object.
        root = self.root if self.root.endswith(os.sep) else self.root + os.sep
        p = os.path.normpath(os.path.join(root, key))
        if not p.startswith(root):
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
                raise ImmutableConflict(
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

    def drawing_object_keys(self, tenant_id: str, drawing_id: str) -> set | None:
        """Walk the drawing's prefix, separating ABSENCE from a FAILED SCAN.

        The separation is the whole job. The caller removes a lock file only
        when this comes back with nothing in it that names an owner, so every
        way this can report FEWER keys than are really there -- above all, a
        bare `set()` from a scan that never looked -- is a way to retire a LIVE
        drawing's file. Over-reporting is harmless; under-reporting is not. The two
        standard-library defaults both do exactly that: `os.path.isdir` answers
        False for a directory it could not stat, and `os.walk` swallows the
        `scandir` error and simply yields nothing unless it is given `onerror`.
        Either turns a transient permission or I/O fault into a confident,
        wrong "there is nothing here".

        So absence is only ever `FileNotFoundError`, every other `OSError`
        becomes None ("cannot tell"), and the walk is told to raise.
        """
        root = self._path(drawing_prefix(tenant_id, drawing_id))

        def _reraise(exc: OSError) -> None:
            raise exc

        found = set()
        try:
            os.stat(root)  # absence must be PROVED, not inferred from a failure
        except FileNotFoundError:
            return set()
        except OSError:
            return None
        try:
            for dirpath, _dirs, files in os.walk(root, onerror=_reraise):
                for name in files:
                    rel = os.path.relpath(os.path.join(dirpath, name), self.root)
                    found.add(rel.replace(os.sep, "/"))
        except OSError:
            return None
        return found


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
                   content_sha256, workitem_id, tool, note, source_ref
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
            "source_ref": row["source_ref"],
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
               byte_count, content_sha256, workitem_id, tool, note,
               source_ref, state)
            VALUES
              (%(tenant)s, %(drawing)s, %(version)s, %(parent)s, %(key)s,
               %(bytes)s, %(sha)s, %(workitem)s, %(tool)s, %(note)s,
               %(source_ref)s, 'reserved')
            """,
            {
                "tenant": tenant_id, "drawing": drawing_id,
                "version": version, "parent": expected, "key": key,
                "bytes": len(data), "sha": digest,
                "workitem": meta.get("workitem_id"), "tool": meta.get("tool"),
                "note": meta.get("note"),
                "source_ref": meta.get("source_ref"),
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
                   authority_guard: Optional[dict] = None,
                   precondition: Callable[[], bool] | None = None) -> dict:
    """PUT version 1 of a new drawing + write its initial manifest.

    Returns {"drawing_id": <id>, "version": 1}. Refuses to clobber an existing
    drawing (use put_drawing to append versions).

    `precondition` is the CREATING writer's answer to the resurrect-after-purge
    problem. Every other legacy writer proves the drawing still exists by loading
    its manifest inside the checkout guard; this one cannot, because an absent
    drawing is its normal case, so a caller whose work can be cancelled out from
    under it (an extraction whose upload was purged mid-flight) passes a callable
    that re-reads its own evidence. It runs INSIDE the guard, which is the whole
    point — checked outside, it is a read with an unbounded wait after it — and a
    False raises `DrawingVanished` before anything is written. Legacy authority
    only: the postgres path already settles the same question with row-level
    authority through `authority_guard`, so supplying both is refused rather than
    silently ignoring one.
    """
    tid = sanitize_id(tenant_id)
    did = sanitize_id(drawing_id) if drawing_id else new_drawing_id()
    if authority_mode() == "postgres":
        if precondition is not None:
            raise ValueError(
                "precondition is a legacy-authority mechanism; the postgres "
                "authority uses authority_guard")
        return _pg_ingest(
            backend, tid, did, _read(local_path), authority_guard)

    # Guarded, and it is the WORST of the legacy writers to leave out. The
    # "already exists" check below and the save at the end are a read and a
    # write with nothing holding the record between them, so two ingests of one
    # drawing id both pass the check and both write. Worse, what they write is a
    # FRESH `_new_manifest`: not a stale copy of the checkout fields but no
    # checkout and no `checkout_fence` at all. An ingest that passed the check
    # before a concurrent acquire therefore erases the lease AND resets the
    # generation counter, so `_next_fence` starts over from 1 and the next
    # acquire reissues a generation an outstanding capability still carries.
    # Read the payload BEFORE taking the lock: it does not depend on anything the
    # guard protects, and reading a large DWG inside the section would hold the
    # drawing for the duration for no reason.
    data = _read(local_path)
    # `creating=True`: this is the one writer whose drawing is SUPPOSED to be
    # absent, so it is the one that may bring a lock file into existence.
    # Derived before the section so the failure handler below can name it even
    # when the very first check raised. Pure function of the ids, no I/O.
    vkey = drawing_version_key(tid, did, 1)
    with _legacy_checkout_guard(backend, tid, did, creating=True,
                                precondition=precondition) as held:
        try:
            if backend.exists(manifest_key(tid, did)):
                raise ValueError(f"drawing already exists: {tid}/{did} (use put_drawing to add versions)")

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
        except BaseException:
            # THIS caller's share of the retirement rule (`checkout_lock_key`),
            # obeyed the same way `purge_expired` and `_mark_failed` obey it:
            # from its own body, holding the lock, as the last act before
            # leaving. Entering the guard OPENED the lock file, which created it,
            # and this is the one writer allowed in for a drawing that does not
            # exist yet. If the write above fails -- an I/O error on the version
            # blob or the manifest, not a race -- the drawing never comes to
            # exist, so no purge sweep will ever walk its directory and the file
            # would sit there forever.
            #
            # THE PROOF IS NOT "NO MANIFEST", and two review rounds were spent
            # learning it. A guest upload is alive on `upload.state.json` with no
            # manifest at all: `run_extraction` calls this function on exactly
            # that drawing, so a failure here routinely leaves a LIVE upload
            # whose `_mark_failed`, retry and purge all still want this file.
            # Retiring it there is churn at best and, for a waiter, a lap around
            # `cross_process_lock`'s identity check it did not need to run.
            #
            # So the question is asked in the store's own vocabulary: is there
            # ANY object left under this drawing? A marker answers yes without
            # the store having to know what a marker is, which is what keeps it
            # from importing `guest_uploads` and inverting the lock order.
            #
            # The v1 blob is discounted, and NOT on the grounds that this call
            # wrote it -- on the immutability path above it was written by an
            # earlier one, and either way it is the same orphan. The grounds are
            # that a version-1 blob with no manifest over it is residue of an
            # ingest that never completed: no manifest lists it, so nothing
            # reads it and no writer is waiting on it. It is the ONLY key
            # discounted; anything else still under the prefix is taken as an
            # owner, which is the conservative direction.
            #
            # `None` from `drawing_object_keys` means the backend cannot tell,
            # and cannot-tell forbids the removal.
            #
            # There is deliberately no separate `exists(manifest_key(...))` here.
            # The manifest lives UNDER this prefix, so an existing drawing is
            # already a non-empty answer and a second check could only ever agree
            # -- a mutation test confirmed it never changes the outcome. One
            # question, asked once, is what keeps the two from drifting apart.
            # NOTHING IN HERE MAY RAISE over the failure that brought us. That
            # error is the one worth reporting, and a secondary one would replace
            # it -- turning "the disk is full" into whatever the cleanup tripped
            # over. So the removal is inside the guarded block too, not just the
            # question that decides it: `reclaim` swallows the `OSError` from its
            # own `os.remove`, but it reaches `_holds_the_live_file` first, and
            # the `os.fstat` in there is unguarded. Unsure, at either step, means
            # LEAVE the file -- the side that costs only an inode.
            try:
                remaining = backend.drawing_object_keys(tid, did)
                if remaining is not None and not (remaining - {vkey}):
                    held.reclaim()
            except Exception:
                pass
            raise


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
                holder: str | None = None, fence: int | None = None,
                require_parent_is_head: bool = False) -> int:
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
    # Read the payload BEFORE taking the lock; see `ingest_drawing`. The blob
    # WRITE has to stay inside, because its key depends on the version number the
    # guarded manifest read produces.
    data = _read(local_path)
    with _legacy_checkout_guard(backend, tid, did):
        m = load_manifest(backend, tid, did)
        _authorize_checkout_view(m.get("checkout"), holder, fence)

        # Opt-in compare-and-set for callers whose CONTRACT is "parent = the
        # current head" (restore): inside this guard the manifest is fresh, so
        # a head that moved since the caller read it is refused here instead
        # of silently branching the DAG. Default off — the write/undo/redo
        # family branches deliberately, and the PostgreSQL authority enforces
        # its own parent discipline under its row lock.
        if require_parent_is_head:
            declared_parent = int(parent_version) if parent_version is not None else None
            if declared_parent != int(m["head"]):
                raise ValueError(
                    f"stale parent {declared_parent}: head is now {int(m['head'])}")

        new_v = int(m["latest"]) + 1
        vkey = drawing_version_key(tid, did, new_v)
        if backend.exists(vkey):  # immutability guard (monotonic latest => never true in practice)
            raise ValueError(f"refuse to overwrite immutable version key {vkey}")

        backend.put(vkey, data)

        meta = meta or {}
        parent = int(parent_version) if parent_version is not None else None
        m["versions"].append({
            "v": new_v, "parent": parent, "created": _now_iso(),
            "bytes": len(data), "sha256": _sha256(data),
            "workitem_id": meta.get("workitem_id"), "tool": meta.get("tool"),
            "note": meta.get("note"),
            # Authored-tool provenance (`leaf.tool-source.v1` receipt digest)
            # when the writer had one. Stored verbatim and validated on the
            # way OUT (server/routers/drawings.py `_source_ref`), so a drifted
            # value can never be dressed as provenance by a reader.
            "source_ref": meta.get("source_ref"),
        })
        m["head"] = new_v
        m["latest"] = new_v
        save_manifest(backend, tid, did, m)
        return new_v


def resolve_version_entry(backend: StorageBackend, tenant_id: str, drawing_id: str,
                          version="head") -> tuple[int, str, dict]:
    """Resolve `version` to (version_int, object_key, manifest_row) in ONE manifest read.

    The row is a copy of the manifest's own entry for that version, so a caller
    that needs its metadata (restore reads `source_ref`) does not load the
    manifest a second time to find what this call already proved is there.
    """
    tid = sanitize_id(tenant_id)
    did = sanitize_id(drawing_id)
    m = load_manifest(backend, tid, did)

    if isinstance(version, str) and version == "head":
        v = int(m["head"])
    elif isinstance(version, str) and version == "latest":
        v = int(m["latest"])
    else:
        v = int(version)

    entry = next((e for e in m["versions"] if int(e["v"]) == v), None)
    if entry is None:
        known = sorted(int(e["v"]) for e in m["versions"])
        raise ValueError(f"version {v} not in manifest for {tid}/{did} (known={known})")
    return v, drawing_version_key(tid, did, v), dict(entry)


def resolve_version(backend: StorageBackend, tenant_id: str, drawing_id: str,
                    version="head") -> tuple[int, str]:
    """Resolve `version` (an int, "head", or "latest") to (version_int, object_key)."""
    v, key, _entry = resolve_version_entry(backend, tenant_id, drawing_id, version)
    return v, key


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

    Every caller outside this module asks THROUGH here, so the "expired means
    free" rule is read from one place rather than re-implemented against a
    timestamp string. Two of them:

      * the mutating drawing routes, which need proof of ownership for an active
        lock and nothing for an absent or expired one;
      * `_checkout_view` on GET /versions, which publishes the lock record only
        while it is live. That read used to test presence instead, which is how a
        lease that had ended 13 hours earlier was still reported as held.
    """
    return _is_active(co, datetime.now(timezone.utc))


_LEGACY_CHECKOUT_LOCKS: dict[str, threading.Lock] = {}
_LEGACY_CHECKOUT_LOCKS_GUARD = threading.Lock()


@contextlib.contextmanager
def _legacy_checkout_lock(tid: str, did: str):
    """The per-drawing, per-PROCESS half of the checkout guard.

    Kept as the outer layer of `_legacy_checkout_guard` because it is far
    cheaper than a syscall: threads of one app process settle here and never
    reach the filesystem. It excludes only threads. The cross-process half is
    the backend's OS lock.

    REFERENCE COUNTED, so the map does not grow for the life of the process.
    Round 6 caught that: with all six writers routed through here, a process
    handling a long tail of distinct drawings accumulated one `Lock` per drawing
    it had ever touched and never dropped one, so the heap grew with churn.

    The entry is removed when the last interested caller leaves, and the count is
    maintained under `_LEGACY_CHECKOUT_LOCKS_GUARD`, which is what makes the
    removal safe. Evicting on "the lock looks unheld" instead would be a race:
    callers take the lock AFTER this returns, so an entry can legitimately be
    unheld while a caller is on its way to it, and dropping it there would hand
    the next caller a DIFFERENT `Lock` for the same drawing and let both into the
    section.
    """
    key = f"{tid}/{did}"
    with _LEGACY_CHECKOUT_LOCKS_GUARD:
        entry = _LEGACY_CHECKOUT_LOCKS.get(key)
        if entry is None:
            entry = [threading.Lock(), 0]
            _LEGACY_CHECKOUT_LOCKS[key] = entry
        entry[1] += 1
        lock = entry[0]
    try:
        yield lock
    finally:
        with _LEGACY_CHECKOUT_LOCKS_GUARD:
            entry = _LEGACY_CHECKOUT_LOCKS.get(key)
            if entry is not None:
                entry[1] -= 1
                if entry[1] <= 0:
                    del _LEGACY_CHECKOUT_LOCKS[key]


class DrawingVanished(RuntimeError):
    """The drawing a writer was about to create stopped being its drawing while
    it waited for the checkout lock.

    Its own class, not a `ValueError`, because the callers that pass a
    `precondition` treat "it was deleted underneath me" as a clean abort and
    every other ingest failure as an error to record. Recording this one would
    itself write to the drawing that was just deleted.
    """


@contextlib.contextmanager
def _legacy_checkout_guard(backend: StorageBackend, tid: str, did: str, *,
                           creating: bool = False,
                           precondition: Callable[[], bool] | None = None):
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
    generation a SECOND time — the same bypass by a different route.
    `ingest_drawing` is worse again: it writes a fresh `_new_manifest`, so it
    erases the lease and drops `checkout_fence` entirely, resetting the
    generation counter to its start. Guarding only the checkout calls would
    leave all of those open, so all six writers run here.
    `test_no_legacy_manifest_writer_escapes_the_guard` holds the line at the
    source level, because the failure mode is a seventh writer added later.

    Two layers close the window, because one process is two different problems.
    The threading lock settles the app's own threads without a syscall. Under it
    an OS advisory lock on a separate lock file settles a SECOND PROCESS reading
    the same store directory, which the threading lock cannot see: a per-process
    dict is not shared state, so two replicas each hold their own uncontended
    lock and both walk into the section.

    NOT REENTRANT. The threading half is a plain `Lock`, so a guarded function
    calling another guarded function would wait out `_CHECKOUT_LOCK_TIMEOUT_S`
    and then raise. None of the six writers listed above calls another; keep it
    that way, `ingest_drawing` included.

    WHAT THIS DOES NOT DO. It is not a distributed lock and must not grow into
    one. It reaches exactly as far as the OS lock does, which is one shared
    filesystem — so replicas on one host, or on a shared volume, and nothing
    beyond that. A backend with no descriptor to lock (`OSSBackend`) cannot be
    covered at all; rather than pretend otherwise this warns
    `CrossProcessCheckoutLockMissing` and runs with the threading lock alone,
    which is the behaviour that was previously silent and unlabelled.

    IT DOES COORDINATE WITH THE GUEST-UPLOAD PURGE, which is what closes the
    resurrect-after-purge hole. `server/guest_uploads.py::purge_expired` used to
    delete a whole drawing directory under its own `drawing_lock` alone, a lock
    this module cannot see; `FilesystemBackend.put` recreates missing parents, so
    a save already in flight put the drawing back AFTER the purge had appended a
    "deleted" line to `purge.log.jsonl`, making the receipt a lie. The purge now
    enters this same guard through `legacy_purge_guard` and holds it across the
    deletion and the receipt.

    THE LOCK IS WHAT CLOSES IT, for the five writers that load the manifest. Each
    one's read and save are both inside this section, so a purge holding the same
    lock can no longer land between them; and a writer that arrives after the
    deletion fails its `load_manifest`. The re-checks below are not what makes
    that true — a mutation test was what settled it — and the comments there say
    so. `ingest_drawing` is the exception: it CREATES, so it has no manifest to
    load and nothing about a missing drawing looks wrong to it. That one carries
    a `precondition`, evaluated inside the lock, and it is genuinely the only
    thing standing between a purged upload and an extraction that finishes late.

    RESOURCES IT HOLDS, and exactly how far each is bounded. The in-process lock
    map is reference counted and drops an entry when its last caller leaves, so it
    is bounded by CONCURRENT callers rather than by drawings ever seen. The lock
    FILES track the drawings that currently exist: a missing drawing is refused
    before the file is created, so no caller can mint lock files for ids that
    were never drawings, and a purged drawing's file is retired
    (`_HeldCheckoutLock.reclaim`) as the last act inside this section, where
    holding the lock exclusively is the proof that no live holder is being robbed
    of it. Every path that skips the refusal retires its own file instead: the
    purge after it deletes the drawing, `must_exist=False` when its caller finds
    the drawing already gone, the re-check below for a writer that lost the
    race, and `ingest_drawing` when its own write fails and leaves no drawing
    behind. Retiring it from anywhere else — under the drawing's own lock, or by
    sweeping files whose lock can be taken — puts two callers in here at once,
    which is why the file is not in the drawing's own directory to begin with.

    THE FAILED INGEST IS CLOSED BY ITS OWN CALLER, which is the last of these.
    An ingest that takes the lock and then FAILS to write (a disk error between
    the version blob and the manifest) leaves a drawing that never came to
    exist, so nothing would ever walk its directory again. `ingest_drawing`
    re-reads the drawing's key prefix on the way out of an abnormal exit and
    retires the file when nothing is left under it but a version-1 blob.

    IT IS DELIBERATELY NOT DONE HERE, and the reason is what `creating` actually
    means. `creating` says only "do not refuse a missing drawing"; it does not
    say "a missing manifest means a missing drawing", and nothing about a
    section's flags says whether ANYONE is left to serve. Three entries set it —
    `ingest_drawing`, `legacy_drawing_guard(must_exist=False)` (which serves
    `_mark_failed`) and `legacy_purge_guard` — and neither of the last two can
    read an absent manifest as an absent drawing. `_mark_failed`'s upload is
    alive on `upload.state.json` before any manifest exists, and a purge that
    has deleted the manifest but not finished has a drawing that still needs
    finishing. A blanket reclaim on abnormal exit would retire both their files.
    So the rule stays where it has always been: with the caller that can prove
    its own drawing has nothing left to serve. `ingest_drawing` proves it by
    finding the drawing's whole key prefix empty apart from a version-1 blob,
    not merely by finding no manifest.

    The documented answer above that boundary is unchanged and is postgres: it
    reads `FOR UPDATE` and increments in one statement, so it needs none of
    this. What this removes is the narrower, previously-standing caveat that the
    legacy path was safe only for a SINGLE app process.
    """
    with _legacy_checkout_lock(tid, did) as thread_lock:
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
                if precondition is not None and not precondition():
                    raise DrawingVanished(f"{tid}/{did} vanished before the write")
                # No lock file exists on this path, so there is nothing for a
                # deleter to reclaim and the token says so honestly.
                yield _NoFileCheckoutLock()
                return
            # Refuse a MISSING drawing before the lock file exists. Taking the
            # lock creates the file, and nothing reclaims it, so without this an
            # authenticated caller could mint an empty lock file per made-up
            # drawing id and grow the prefix without ever creating a drawing.
            # Every writer but `ingest_drawing` needs the manifest to be there
            # already and would raise this exact `KeyError` from `load_manifest`
            # a moment later, so this only moves the refusal earlier. Racing a
            # concurrent ingest can turn into a not-found for a drawing that
            # appears immediately afterwards, which was already possible and
            # costs nothing, because this path never writes.
            if not creating and not backend.exists(manifest_key(tid, did)):
                raise KeyError(manifest_key(tid, did))
            with backend.cross_process_lock(checkout_lock_key(tid, did)) as held:
                # RE-CHECK, now that nothing else can be mid-delete. The check
                # above ran before the wait, so a purge that started after it and
                # finished before the lock was granted is invisible to it.
                if not creating and not backend.exists(manifest_key(tid, did)):
                    # What stops this caller RESURRECTING the drawing is not this
                    # line. It is the shared lock -- which keeps the purge out
                    # from a writer's manifest read until its save -- plus the
                    # `load_manifest` every one of these writers does as its
                    # first act inside the guard, which raises this same
                    # `KeyError` a moment later. Stated plainly because the
                    # earlier wording here claimed otherwise and a mutation test
                    # showed the claim was false: deleting this check leaves the
                    # drawing just as un-resurrectable.
                    #
                    # What is only possible HERE is retiring the lock FILE.
                    # Opening the lock re-created it a moment ago, for a drawing
                    # the purge has already finished with and will never look at
                    # again, so letting the refusal come from `load_manifest`
                    # instead would leave one empty file behind every writer that
                    # lost this race -- the same unbounded growth the purge's own
                    # reclaim exists to stop. Holding the lock exclusively is the
                    # same proof the purge relies on.
                    held.reclaim()
                    raise KeyError(manifest_key(tid, did))
                # The CREATING writer cannot use the check above — an absent
                # drawing is its normal case — so it brings its own proof that
                # the work it is about to commit is still wanted. Evaluated here
                # rather than by the caller for the same reason: outside the lock
                # it is a read with a wait after it.
                if precondition is not None and not precondition():
                    if not backend.exists(manifest_key(tid, did)):
                        held.reclaim()
                    raise DrawingVanished(f"{tid}/{did} vanished before the write")
                yield held
        finally:
            thread_lock.release()


@contextlib.contextmanager
def legacy_drawing_guard(backend: StorageBackend, tid: str, did: str, *,
                         must_exist: bool = True):
    """Public entry into one drawing's checkout guard for a NON-manifest writer.

    The manifest writers reach this guard through their own functions. What this
    is for is the OTHER writes that land in a drawing's directory — an upload's
    intake cache, its state marker — because `FilesystemBackend.put` recreates
    missing parents, so any one of them can rebuild a directory the purge has
    just deleted and reported. Serializing only the manifest would leave the
    receipt false by a different file.

    `must_exist=True` refuses a drawing that is already gone, with the same
    `KeyError` the manifest writers raise, which is what a caller finishing work
    on a purged drawing needs. It also retires the lock file on that refusal, so
    a loser of the race leaves nothing behind.

    `must_exist=False` is for a caller whose drawing may legitimately not exist
    yet (a failure recorded before the ingest ever created one). It carries two
    obligations, because skipping the existence check is what makes both
    possible. It must bring its own evidence and re-read it INSIDE the section,
    since outside it the answer goes stale while it waits. And when that evidence
    says the drawing is gone for good, it must `reclaim()` the yielded lock
    before returning: entering here OPENED the lock file, which created it, and
    for a drawing that no longer exists nothing else will ever come back for it.

    NOT REENTRANT, like the guard beneath it: never call this from inside
    another guarded section for the same drawing.
    """
    with _legacy_checkout_guard(backend, tid, did, creating=not must_exist) as held:
        yield held


@contextlib.contextmanager
def legacy_purge_guard(backend: StorageBackend, tid: str, did: str):
    """The DELETER's entry into a drawing's checkout guard.

    One shared lock is the whole point. The guest purge holding only its own
    `drawing_lock` and the store holding only this one meant the two never
    excluded each other across processes, so the purge could delete a drawing
    directory and write its receipt while a legacy writer sat between its
    manifest read and its save, and `FilesystemBackend.put` then recreated the
    directory behind the receipt. Taking the SAME lock the writers take is what
    makes the receipt true, and it costs the purge nothing it should not already
    be paying: it is deleting the drawing, so serializing against the drawing's
    own writers is the correct behaviour.

    `creating=True`, because a deleter's drawing may already be half gone (a
    previous sweep that failed after the directory but before the receipt), and
    refusing on a missing manifest would strand exactly the case that most needs
    finishing.

    Yields the held lock so the caller can `reclaim()` the lock FILE once it has
    confirmed the drawing is gone and written its receipt. It is the caller's
    call and not automatic, because a purge that FAILED to delete leaves a live
    drawing whose writers still need that file.

    ORDERING. Callers take their own per-drawing lock first and this second
    (`purge_expired` -> `drawing_lock` -> here, matching `run_extraction` ->
    `drawing_lock` -> `ingest_drawing` -> the guard). Nothing in this module ever
    takes a `guest_uploads` lock, so the order cannot invert and there is no
    AB-BA cycle; keep it that way, and `test_the_store_never_reaches_back_into_
    the_upload_module` holds the line.
    """
    with _legacy_checkout_guard(backend, tid, did, creating=True) as held:
        yield held


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
