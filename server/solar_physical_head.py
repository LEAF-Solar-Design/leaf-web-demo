"""The current Ground physical state of a drawing, found by a bounded search and advanced by
optimistic publication of a child that names its parent.

A drawing's physical states form one append-only head log under its prefix:
`physical/head-NNNN.json`, entry n naming a state artifact and the preceding log entry's
state (its chain parent). An entry is created with the store's one atomic
primitive, `put_if_absent_or_verify` (os.link on the filesystem backend, dict.setdefault in
memory), so of two writers that build on the same head exactly one creates entry n and the
other is refused PHYSICAL_HEAD_CONFLICT. A backend that inherits the base class's
exists-then-put (the APS OSS backend) cannot make that promise and publication refuses it.

The log is per drawing. With version binding enabled a read follows the reference on the
selected drawing version; an unbound version follows the legacy adoption policy. Resolution adds one manifest
read and at most one object read (64 MiB for intake, 16 MiB for bundles), none on a memo hit.
Entries are contiguous from 0 (entry n is written only after entry n-1 was read), so
the head is found by binary search in at most 13 log reads, its predecessor included. The log
holds at most 4,096 entries. Every read and write fails closed with a named, payload-free code.
Entry parents link the immutable log; view parents name the state the document replaced.
Version-bound publication may leave an orphan after a refused version commit; readers
follow only the selected version's reference, and an identical retry reuses the orphan.
The state bytes are the physical-state module's own.
"""
import hashlib
import json
import re

import store
import solar_artifacts
import solar_physical_state as ps
import write_loop
from leaf_cloud_client import canonical_bytes
from solar_design_graph import GraphValidationError
from solar_graph_context import resolve_graph_context

ENTRY_SCHEMA = "leaf.solar-physical-head-entry.v1"
HEAD_SCHEMA = "leaf.solar-physical-head.v1"
PUBLISH_SCHEMA = "leaf.solar-physical-head-publish.v1"
MAX_LOG_ENTRIES = 4096
MAX_PHYSICAL_VERSIONS = MAX_LOG_ENTRIES
MAX_ENTRY_BYTES = 1024
MAX_PROJECT_ID_CHARS = 100
ENTRY_KEYS = frozenset({"schema", "index", "project_id", "parent", "state", "content_sha256"})
CODES = frozenset({
    "PHYSICAL_HEAD_CONFLICT", "PHYSICAL_HEAD_LOG_FULL", "PHYSICAL_HEAD_CORRUPT",
    "PHYSICAL_HEAD_PROJECT_MISMATCH", "PHYSICAL_HEAD_PROJECT_ID_INVALID", "PHYSICAL_HEAD_ID_INVALID",
    "PHYSICAL_HEAD_WRITES_DRAINED", "PHYSICAL_HEAD_STORE_UNAVAILABLE", "PHYSICAL_HEAD_STORE_UNSAFE",
    "PHYSICAL_HEAD_CHECKOUT_REQUIRED",
})
_HEX64 = re.compile(r"[0-9a-f]{64}")


class PhysicalHeadError(ps.PhysicalStateError):
    """A named, payload-free refusal; a PhysicalStateError, so one except clause covers both."""


def _hex(value):
    return type(value) is str and _HEX64.fullmatch(value) is not None


def _project_id(value):
    if type(value) is not str or not 1 <= len(value) <= MAX_PROJECT_ID_CHARS:
        raise PhysicalHeadError("PHYSICAL_HEAD_PROJECT_ID_INVALID")
    return value


def _log_prefix(tenant_id, drawing_id):
    try:
        return store.drawing_prefix(tenant_id, drawing_id) + "/physical/"
    except (ValueError, TypeError):
        raise PhysicalHeadError("PHYSICAL_HEAD_ID_INVALID") from None


def entry_key(tenant_id, drawing_id, index):
    """The object key of head-log entry `index` (0 <= index < MAX_LOG_ENTRIES)."""
    if type(index) is not int or not 0 <= index < MAX_LOG_ENTRIES:
        raise PhysicalHeadError("PHYSICAL_HEAD_ID_INVALID")
    return _log_prefix(tenant_id, drawing_id) + f"head-{index:04d}.json"


def entry_bytes(index, project_id, parent, state, content_sha256):
    """The one byte form of a head-log entry: canonical (sorted, compact) JSON."""
    return canonical_bytes({"schema": ENTRY_SCHEMA, "index": index, "project_id": project_id,
                            "parent": parent, "state": state, "content_sha256": content_sha256})


class _Log:
    """Reads of one drawing's head log; each entry is fetched at most once per call."""

    __slots__ = ("backend", "tenant_id", "drawing_id", "raw")

    def __init__(self, backend, tenant_id, drawing_id):
        self.backend = backend
        self.tenant_id = tenant_id
        self.drawing_id = drawing_id
        self.raw = {}

    def get(self, index):
        """The entry's bytes, or None when absent."""
        if index not in self.raw:
            key = entry_key(self.tenant_id, self.drawing_id, index)
            try:
                self.raw[index] = self.backend.get(key)
            except KeyError:
                self.raw[index] = None
            except (OSError, RuntimeError, ValueError) as exc:
                # A 404 response is absence, as in store.OSSBackend.exists; anything else is a fault.
                if getattr(getattr(exc, "response", None), "status_code", None) != 404:
                    raise PhysicalHeadError("PHYSICAL_HEAD_STORE_UNAVAILABLE") from None
                self.raw[index] = None
        return self.raw[index]

    def last(self):
        """The highest present index (-1 for an empty log): binary search over the contiguous
        prefix, at most 13 reads for 4,096 slots."""
        lo, hi = -1, MAX_LOG_ENTRIES
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if self.get(mid) is None:
                hi = mid
            else:
                lo = mid
        return lo

    def entry(self, index):
        """Entry `index` parsed and checked against its own position; fails closed."""
        raw = self.get(index)
        try:
            if type(raw) is not bytes or not raw or len(raw) > MAX_ENTRY_BYTES:
                raise ValueError()
            entry = json.loads(raw.decode("utf-8", errors="strict"))
            if (type(entry) is not dict or set(entry) != ENTRY_KEYS or entry["schema"] != ENTRY_SCHEMA
                    or type(entry["index"]) is not int or entry["index"] != index
                    or type(entry["project_id"]) is not str
                    or not 1 <= len(entry["project_id"]) <= MAX_PROJECT_ID_CHARS
                    or not (entry["parent"] is None or _hex(entry["parent"]))
                    or not _hex(entry["state"]) or not _hex(entry["content_sha256"])
                    or canonical_bytes(entry) != raw):
                raise ValueError()
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise PhysicalHeadError("PHYSICAL_HEAD_CORRUPT") from None
        return entry


def _reference(meta, drawing_id):
    ref = {key: meta[key] for key in ("artifact_id", "media_type", "filename", "byte_length",
                                      "content_sha256", "source_version")}
    ref.update(schema=solar_artifacts.REF_SCHEMA,
               download=f"/api/drawings/{drawing_id}/artifacts/{meta['artifact_id']}")
    return ref


def _head(backend, tenant_id, drawing_id, project_id):
    """(view, content) of the current head, or (None, None) for an empty log."""
    log = _Log(backend, tenant_id, drawing_id)
    index = log.last()
    if index < 0:
        return None, None
    return _view_at(log, index, project_id)


def _view_at(log, index, project_id):
    """Read and verify an exact entry and its predecessor, without discovering the tip."""
    backend, tenant_id, drawing_id = log.backend, log.tenant_id, log.drawing_id
    entry = log.entry(index)
    if index == 0:
        if entry["parent"] is not None:
            raise PhysicalHeadError("PHYSICAL_HEAD_CORRUPT")
    elif log.get(index - 1) is None or log.entry(index - 1)["state"] != entry["parent"]:
        raise PhysicalHeadError("PHYSICAL_HEAD_CORRUPT")
    if project_id is not None and entry["project_id"] != project_id:
        raise PhysicalHeadError("PHYSICAL_HEAD_PROJECT_MISMATCH")
    try:
        meta, content = solar_artifacts.read_artifact(backend, tenant_id, drawing_id, entry["state"])
    except GraphValidationError as exc:
        if exc.code == "ARTIFACT_STORE_UNAVAILABLE":
            raise PhysicalHeadError("PHYSICAL_HEAD_STORE_UNAVAILABLE") from None
        raise PhysicalHeadError("PHYSICAL_HEAD_CORRUPT") from None
    if (meta["tool"] != ps.TOOL or meta["media_type"] != ps.MEDIA_TYPE or meta["filename"] != ps.FILENAME
            or meta["project_id"] != entry["project_id"]
            or meta["request_sha256"] != meta["content_sha256"]
            or meta["content_sha256"] != entry["content_sha256"]):
        raise PhysicalHeadError("PHYSICAL_HEAD_CORRUPT")
    view = {"schema": HEAD_SCHEMA, "drawing_id": drawing_id, "project_id": entry["project_id"],
            "index": index, "parent": entry["parent"], "state": _reference(meta, drawing_id)}
    return view, content


def _version_head(backend, tenant_id, drawing_id, project_id, version):
    if version != "head" and (type(version) is not int or version < 1):
        raise PhysicalHeadError("PHYSICAL_HEAD_ID_INVALID")
    if not store.physical_versioned_enabled():
        return _head(backend, tenant_id, drawing_id, project_id)
    try:
        v, ref = store.read_version_physical(
            backend, tenant_id, drawing_id, version, project_id=project_id)
    except store.PhysicalReferenceScopeMismatch:
        raise PhysicalHeadError("PHYSICAL_HEAD_PROJECT_MISMATCH") from None
    except (store.PhysicalReferenceInvalid, KeyError):
        raise PhysicalHeadError("PHYSICAL_HEAD_CORRUPT") from None
    except ValueError:
        raise PhysicalHeadError("PHYSICAL_HEAD_ID_INVALID") from None
    except (OSError, RuntimeError):
        raise PhysicalHeadError("PHYSICAL_HEAD_STORE_UNAVAILABLE") from None
    if ref is None:
        if v is None:
            return _head(backend, tenant_id, drawing_id, project_id)
        import solar_physical_legacy
        return solar_physical_legacy.legacy_view(
            backend, tenant_id, drawing_id, v, project_id=project_id)
    view, content = _view_at(_Log(backend, tenant_id, drawing_id), ref["head_index"], project_id)
    if (view["state"]["artifact_id"] != ref["state_artifact_id"]
            or view["state"]["content_sha256"] != ref["state_content_sha256"]):
        raise PhysicalHeadError("PHYSICAL_HEAD_CORRUPT")
    return view, content


def physical_head(backend, tenant_id, drawing_id, *, project_id, version="head"):
    """The selected version's physical view. Bound reads use two log entries and two
    artifact gets; resolution uses one manifest and at most one bounded object (memoized).
    Unbound reads retain the at-most-13-read tip search."""
    _project_id(project_id)
    if not store.physical_versioned_enabled():
        # Preserve the legacy metadata-only view, including its decode boundary.
        return _version_head(backend, tenant_id, drawing_id, project_id, version)[0]
    return load_physical_head(backend, tenant_id, drawing_id, project_id=project_id, version=version)[0]


def load_physical_head(backend, tenant_id, drawing_id, *, project_id, version="head"):
    """(view, document) following the selected version; same bounded read budget as
    physical_head. Decode the already verified content without another artifact read."""
    _project_id(project_id)
    view, content = _version_head(backend, tenant_id, drawing_id, project_id, version)
    return _document(view, content)


def load_physical_entry(backend, tenant_id, drawing_id, index, *, project_id):
    """Read one named immutable entry, failing closed when it is absent or corrupt."""
    _project_id(project_id)
    return _document(*_view_at(_Log(backend, tenant_id, drawing_id), index, project_id))


def _document(view, content):
    if view is None:
        return None, None
    try:
        document = ps.decode_document(content)
    except ps.PhysicalStateError:
        raise PhysicalHeadError("PHYSICAL_HEAD_CORRUPT") from None
    return _document_view(view, document), document


def _document_view(view, document):
    return dict(view, parent=document["parent"])


def _atomic_backend(backend):
    """False for a backend that inherits the base class's exists-then-put publication (APS OSS),
    which lets two writers both believe they created the same entry."""
    inherited = store.StorageBackend.put_if_absent_or_verify
    return getattr(type(backend), "put_if_absent_or_verify", None) is not inherited


def publish_physical_state(backend, tenant_id, drawing_id, document, *, project_id=None,
                           holder=None, fence=None):
    """Store `document` and make it the drawing's physical head, provided its parent is the
    current head's state (None for the first state). Retrying the head's own document returns
    that head with created False; any other stale parent, or a writer that loses the race for
    the next entry, is refused PHYSICAL_HEAD_CONFLICT. A crash after the state is stored and
    before its entry is created leaves an unreferenced artifact and an unchanged head; the
    retry reuses the artifact at the same drawing revision. New publications read the freeze
    before and after appending; a racing freeze that excludes the entry leaves it stranded
    in the immutable log and refuses publication. An idempotent retry reads the freeze
    once (and the intent once when no freeze exists), accepting only an included entry."""
    if write_loop.drawing_mutations_refusal() is not None:
        raise PhysicalHeadError("PHYSICAL_HEAD_WRITES_DRAINED")
    if project_id is not None:
        _project_id(project_id)
    if not _atomic_backend(backend):
        raise PhysicalHeadError("PHYSICAL_HEAD_STORE_UNSAFE")
    import solar_physical_legacy
    versioned = solar_physical_legacy.versioned_mode()
    if versioned:
        _project_id(project_id)
    paired = holder is not None or fence is not None
    if ((versioned or paired) and (type(holder) is not str or not 1 <= len(holder) <= 200
            or holder == store.ANONYMOUS_HOLDER or type(fence) is not int or fence < 1)):
        raise PhysicalHeadError("PHYSICAL_HEAD_CHECKOUT_REQUIRED")
    try:
        if versioned and not store.checkout_active(
                store.load_manifest(backend, tenant_id, drawing_id)["checkout"]):
            raise PhysicalHeadError("PHYSICAL_HEAD_CHECKOUT_REQUIRED")
        if versioned or paired:
            store.authorize_checkout(backend, tenant_id, drawing_id, holder, fence)
    except store.CheckoutDenied:
        raise PhysicalHeadError("PHYSICAL_HEAD_CHECKOUT_REQUIRED") from None
    except KeyError:
        raise PhysicalHeadError("PHYSICAL_HEAD_ID_INVALID") from None
    except PhysicalHeadError:
        raise
    except ValueError as exc:
        if str(exc).startswith("an active checkout is required"):
            raise PhysicalHeadError("PHYSICAL_HEAD_CHECKOUT_REQUIRED") from None
        raise PhysicalHeadError("PHYSICAL_HEAD_ID_INVALID") from None
    except (OSError, RuntimeError):
        raise PhysicalHeadError("PHYSICAL_HEAD_STORE_UNAVAILABLE") from None
    if versioned:
        return _publish_version(backend, tenant_id, drawing_id, document,
                                project_id=project_id, holder=holder, fence=fence)
    data = ps.encode_document(document)
    content_sha256 = hashlib.sha256(data).hexdigest()
    parent = document["parent"]
    head, _ = _head(backend, tenant_id, drawing_id, project_id)
    frozen_index = solar_physical_legacy.resolved_last_index(backend, tenant_id, drawing_id)
    if head is not None and head["parent"] == parent and head["state"]["content_sha256"] == content_sha256:
        if frozen_index is None or head["index"] <= frozen_index:
            return {"schema": PUBLISH_SCHEMA, "created": False, "head": _document_view(head, document)}
    # A stranded tip may hide an included retry; compare with the frozen entry too.
    if frozen_index is not None and frozen_index >= 0:
        included, _ = _view_at(_Log(backend, tenant_id, drawing_id), frozen_index, project_id)
        if included["parent"] == parent and included["state"]["content_sha256"] == content_sha256:
            return {"schema": PUBLISH_SCHEMA, "created": False, "head": _document_view(included, document)}
    if frozen_index is not None:
        raise solar_physical_legacy.PhysicalLegacyError("PHYSICAL_LEGACY_FROZEN")
    if parent != (None if head is None else head["state"]["artifact_id"]):
        raise PhysicalHeadError("PHYSICAL_HEAD_CONFLICT")
    index = 0 if head is None else head["index"] + 1
    if index >= MAX_LOG_ENTRIES:
        raise PhysicalHeadError("PHYSICAL_HEAD_LOG_FULL")
    stored = ps.store_physical_state(backend, tenant_id, drawing_id, document, project_id=project_id)
    ref = stored["state"]
    raw = entry_bytes(index, stored["project_id"], parent, ref["artifact_id"], ref["content_sha256"])
    try:
        backend.put_if_absent_or_verify(entry_key(tenant_id, drawing_id, index), raw)
    except store.ImmutableConflict:
        raise PhysicalHeadError("PHYSICAL_HEAD_CONFLICT") from None
    except (OSError, RuntimeError, ValueError):
        raise PhysicalHeadError("PHYSICAL_HEAD_STORE_UNAVAILABLE") from None
    frozen_index = solar_physical_legacy.resolved_last_index(backend, tenant_id, drawing_id)
    if frozen_index is not None and frozen_index < index:
        raise solar_physical_legacy.PhysicalLegacyError("PHYSICAL_LEGACY_FROZEN")
    head = {"schema": HEAD_SCHEMA, "drawing_id": drawing_id, "project_id": stored["project_id"],
            "index": index, "parent": parent, "state": ref}
    return {"schema": PUBLISH_SCHEMA, "created": True, "head": _document_view(head, document)}


def _publish_version(backend, tenant_id, drawing_id, document, *, project_id, holder, fence):
    import solar_physical_legacy as legacy
    data = ps.encode_document(document)
    digest = hashlib.sha256(data).hexdigest()
    # Validate scope before creating even a freeze intent on an unbound drawing.
    try:
        resolve_graph_context(backend, tenant_id, drawing_id, "head", project_id=project_id)
    except GraphValidationError as exc:
        code = ("PHYSICAL_HEAD_PROJECT_MISMATCH" if exc.code == "PROJECT_MISMATCH"
                else "PHYSICAL_HEAD_CORRUPT")
        raise PhysicalHeadError(code) from None
    except (OSError, RuntimeError):
        raise PhysicalHeadError("PHYSICAL_HEAD_STORE_UNAVAILABLE") from None
    try:
        legacy.freeze_legacy(backend, tenant_id, drawing_id, project_id=project_id)
    except legacy.PhysicalLegacyError as exc:
        code = {"PHYSICAL_LEGACY_WRITES_DRAINED": "PHYSICAL_HEAD_WRITES_DRAINED",
                "PHYSICAL_LEGACY_STORE_UNAVAILABLE": "PHYSICAL_HEAD_STORE_UNAVAILABLE",
                "PHYSICAL_LEGACY_STORE_UNSAFE": "PHYSICAL_HEAD_STORE_UNSAFE",
                "PHYSICAL_LEGACY_ID_INVALID": "PHYSICAL_HEAD_ID_INVALID",
                "PHYSICAL_LEGACY_PROJECT_MISMATCH": "PHYSICAL_HEAD_PROJECT_MISMATCH"}.get(
                    exc.code, "PHYSICAL_HEAD_CORRUPT")
        raise PhysicalHeadError(code) from None

    def current():
        version, _ = store.read_version_physical(
            backend, tenant_id, drawing_id, project_id=project_id)
        view, doc = load_physical_head(backend, tenant_id, drawing_id,
                                      project_id=project_id, version=version)
        return version, view, doc

    def retry(view, doc):
        return (doc is not None and doc["parent"] == document["parent"]
                and view["state"]["content_sha256"] == digest)

    def result(view, version, parent_version, created):
        return {"schema": PUBLISH_SCHEMA, "created": created, "head": view,
                "drawing_version": {"version": version, "parent_version": parent_version,
                                    "created": created}}

    def reread():
        version, view, doc = current()
        if retry(view, doc):
            return result(view, version, version, False)
        raise PhysicalHeadError("PHYSICAL_HEAD_CONFLICT")

    try:
        version, view, doc = current()
        if retry(view, doc):
            return result(view, version, version, False)
        if document["parent"] != (None if view is None else view["state"]["artifact_id"]):
            raise PhysicalHeadError("PHYSICAL_HEAD_CONFLICT")
        stored = ps.store_physical_state(backend, tenant_id, drawing_id, document, project_id=project_id)
        ref = stored["state"]
        log = _Log(backend, tenant_id, drawing_id)
        last = log.last()
        tip = None if last < 0 else log.entry(last)
        if tip is not None and tip["state"] == ref["artifact_id"] and tip["content_sha256"] == digest:
            index = last
        else:
            index = last + 1
            if index >= MAX_LOG_ENTRIES:
                raise PhysicalHeadError("PHYSICAL_HEAD_LOG_FULL")
            raw = entry_bytes(index, project_id, None if tip is None else tip["state"],
                              ref["artifact_id"], digest)
            try:
                backend.put_if_absent_or_verify(entry_key(tenant_id, drawing_id, index), raw)
            except store.ImmutableConflict:
                raise PhysicalHeadError("PHYSICAL_HEAD_CONFLICT") from None
            except (OSError, RuntimeError, ValueError):
                raise PhysicalHeadError("PHYSICAL_HEAD_STORE_UNAVAILABLE") from None
        manifest = store.load_manifest(backend, tenant_id, drawing_id)
        latest = manifest["latest"]
        if sum(row.get("tool") == "solar-physical" for row in manifest["versions"]) >= MAX_PHYSICAL_VERSIONS:
            raise PhysicalHeadError("PHYSICAL_HEAD_LOG_FULL")
        apply_id = "phys_" + hashlib.sha256(
            f"{tenant_id}/{drawing_id}/{version}/{latest}/{digest}".encode()).hexdigest()
        try:
            v, fresh = write_loop.commit_physical_version(
                backend, tenant_id, drawing_id, project_id=project_id, parent_version=version,
                physical={"head_index": index, "state_artifact_id": ref["artifact_id"],
                          "state_content_sha256": digest}, holder=holder, fence=fence, apply_id=apply_id)
        except (store.PhysicalReferenceScopeMismatch, store.PhysicalReferenceInvalid, PhysicalHeadError):
            raise
        except ValueError as exc:
            message = str(exc)
            if message.startswith(("stale parent ", "stale drawing head:")):
                return reread()
            if message.startswith("drawing mutations are disabled"):
                raise PhysicalHeadError("PHYSICAL_HEAD_WRITES_DRAINED") from None
            if message.startswith("an active checkout is required"):
                raise PhysicalHeadError("PHYSICAL_HEAD_CHECKOUT_REQUIRED") from None
            if message.startswith("apply id was reused"):
                raise PhysicalHeadError("PHYSICAL_HEAD_CONFLICT") from None
            raise PhysicalHeadError("PHYSICAL_HEAD_CORRUPT") from None
        if not fresh:
            return reread()
        view = {"schema": HEAD_SCHEMA, "drawing_id": drawing_id, "project_id": project_id,
                "index": index, "parent": document["parent"], "state": ref}
        return result(_document_view(view, document), v, version, True)
    except store.CheckoutDenied:
        raise PhysicalHeadError("PHYSICAL_HEAD_CHECKOUT_REQUIRED") from None
    except store.PhysicalReferenceScopeMismatch:
        raise PhysicalHeadError("PHYSICAL_HEAD_PROJECT_MISMATCH") from None
    except store.PhysicalReferenceInvalid:
        raise PhysicalHeadError("PHYSICAL_HEAD_CORRUPT") from None
    except (OSError, RuntimeError):
        raise PhysicalHeadError("PHYSICAL_HEAD_STORE_UNAVAILABLE") from None
