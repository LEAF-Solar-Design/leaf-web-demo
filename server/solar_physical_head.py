"""The current Ground physical state of a drawing, found by a bounded search and advanced by
optimistic publication of a child that names its parent.

A drawing's physical states form one append-only head log under its prefix:
`physical/head-NNNN.json`, entry n naming the state artifact that became current at step n
and the state it replaced (its parent). An entry is created with the store's one atomic
primitive, `put_if_absent_or_verify` (os.link on the filesystem backend, dict.setdefault in
memory), so of two writers that build on the same head exactly one creates entry n and the
other is refused PHYSICAL_HEAD_CONFLICT. A backend that inherits the base class's
exists-then-put (the APS OSS backend) cannot make that promise and publication refuses it.

The log is per drawing, not per drawing version: a graph commit, undo or redo of the drawing
leaves the physical head where it is, and the head reports the revision its state was written
against. Entries are contiguous from 0 (entry n is written only after entry n-1 was read), so
the head is found by binary search in at most 13 log reads, its predecessor included. The log
holds at most 4,096 entries. Every read and write fails closed with a named, payload-free code.
No clock, network, graph write or re-encoding of the state lives here: the state bytes are the
physical-state module's own.
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

ENTRY_SCHEMA = "leaf.solar-physical-head-entry.v1"
HEAD_SCHEMA = "leaf.solar-physical-head.v1"
PUBLISH_SCHEMA = "leaf.solar-physical-head-publish.v1"
MAX_LOG_ENTRIES = 4096
MAX_ENTRY_BYTES = 1024
MAX_PROJECT_ID_CHARS = 100
ENTRY_KEYS = frozenset({"schema", "index", "project_id", "parent", "state", "content_sha256"})
CODES = frozenset({
    "PHYSICAL_HEAD_CONFLICT", "PHYSICAL_HEAD_LOG_FULL", "PHYSICAL_HEAD_CORRUPT",
    "PHYSICAL_HEAD_PROJECT_MISMATCH", "PHYSICAL_HEAD_PROJECT_ID_INVALID", "PHYSICAL_HEAD_ID_INVALID",
    "PHYSICAL_HEAD_WRITES_DRAINED", "PHYSICAL_HEAD_STORE_UNAVAILABLE", "PHYSICAL_HEAD_STORE_UNSAFE",
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


def physical_head(backend, tenant_id, drawing_id, *, project_id):
    """The drawing's current physical head (None when no state was ever published): at most 13
    log reads (the search and the head's predecessor) plus the head artifact's metadata and
    content (the store verifies its digest)."""
    _project_id(project_id)
    return _head(backend, tenant_id, drawing_id, project_id)[0]


def load_physical_head(backend, tenant_id, drawing_id, *, project_id):
    """(view, document) of the current head, or (None, None); the document is decoded from the
    bytes the head read already verified, so the artifact is read once."""
    _project_id(project_id)
    view, content = _head(backend, tenant_id, drawing_id, project_id)
    if view is None:
        return None, None
    try:
        document = ps.decode_document(content)
    except ps.PhysicalStateError:
        raise PhysicalHeadError("PHYSICAL_HEAD_CORRUPT") from None
    return view, document


def _atomic_backend(backend):
    """False for a backend that inherits the base class's exists-then-put publication (APS OSS),
    which lets two writers both believe they created the same entry."""
    inherited = store.StorageBackend.put_if_absent_or_verify
    return getattr(type(backend), "put_if_absent_or_verify", None) is not inherited


def publish_physical_state(backend, tenant_id, drawing_id, document, *, project_id=None):
    """Store `document` and make it the drawing's physical head, provided its parent is the
    current head's state (None for the first state). Retrying the head's own document returns
    that head with created False; any other stale parent, or a writer that loses the race for
    the next entry, is refused PHYSICAL_HEAD_CONFLICT. A crash after the state is stored and
    before its entry is created leaves an unreferenced artifact and an unchanged head; the
    retry reuses the artifact at the same drawing revision."""
    if write_loop.drawing_mutations_refusal() is not None:
        raise PhysicalHeadError("PHYSICAL_HEAD_WRITES_DRAINED")
    if project_id is not None:
        _project_id(project_id)
    if not _atomic_backend(backend):
        raise PhysicalHeadError("PHYSICAL_HEAD_STORE_UNSAFE")
    data = ps.encode_document(document)
    content_sha256 = hashlib.sha256(data).hexdigest()
    parent = document["parent"]
    head, _ = _head(backend, tenant_id, drawing_id, project_id)
    if head is not None and head["parent"] == parent and head["state"]["content_sha256"] == content_sha256:
        return {"schema": PUBLISH_SCHEMA, "created": False, "head": head}
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
    head = {"schema": HEAD_SCHEMA, "drawing_id": drawing_id, "project_id": stored["project_id"],
            "index": index, "parent": parent, "state": ref}
    return {"schema": PUBLISH_SCHEMA, "created": True, "head": head}
