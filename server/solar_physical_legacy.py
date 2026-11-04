"""Legacy physical adoption: no clock, no network, no write on any read path.

One write-once blob per drawing is written only by a version-bound publication.
All failures use payload-free codes and fail closed. Legacy readers follow ancestry
from the state's source version, never pinning state to the first version read.
"""
import json

import write_loop
import store
import solar_physical_state as ps
import solar_physical_head as ph
from leaf_cloud_client import canonical_bytes

FREEZE_SCHEMA = "leaf.solar-physical-legacy-freeze.v1"
MAX_RECORD_BYTES = 1024
SWITCH_ENV = "LEAF_SOLAR_PHYSICAL_VERSIONED"
CODES = frozenset({
    "PHYSICAL_LEGACY_CORRUPT", "PHYSICAL_LEGACY_PROJECT_MISMATCH",
    "PHYSICAL_LEGACY_STORE_UNAVAILABLE", "PHYSICAL_LEGACY_STORE_UNSAFE",
    "PHYSICAL_LEGACY_ID_INVALID", "PHYSICAL_LEGACY_WRITES_DRAINED", "PHYSICAL_LEGACY_FROZEN",
})
_KEYS = frozenset({"schema", "tenant_id", "drawing_id", "project_id", "legacy_last_index",
                   "state", "content_sha256", "source_version"})


class PhysicalLegacyError(ps.PhysicalStateError):
    """A named, payload-free legacy refusal."""


def versioned_mode():
    return store.physical_versioned_enabled()


def freeze_key(tenant_id, drawing_id):
    try:
        return store.drawing_prefix(tenant_id, drawing_id) + "/physical-legacy/freeze.json"
    except (ValueError, TypeError):
        raise PhysicalLegacyError("PHYSICAL_LEGACY_ID_INVALID") from None


def _read_freeze(backend, tenant_id, drawing_id):
    key = freeze_key(tenant_id, drawing_id)
    try:
        raw = backend.get(key)
    except KeyError:
        return None
    except (OSError, RuntimeError, ValueError) as exc:
        if getattr(getattr(exc, "response", None), "status_code", None) == 404:
            return None
        raise PhysicalLegacyError("PHYSICAL_LEGACY_STORE_UNAVAILABLE") from None
    try:
        if type(raw) is not bytes or not raw or len(raw) > MAX_RECORD_BYTES:
            raise ValueError()
        record = json.loads(raw.decode("utf-8", errors="strict"))
        if (type(record) is not dict or set(record) != _KEYS
                or record["schema"] != FREEZE_SCHEMA
                or record["tenant_id"] != tenant_id or record["drawing_id"] != drawing_id
                or type(record["legacy_last_index"]) is not int
                or not -1 <= record["legacy_last_index"] < ph.MAX_LOG_ENTRIES):
            raise ValueError()
        if record["legacy_last_index"] == -1:
            if any(record[k] is not None for k in
                   ("project_id", "state", "content_sha256", "source_version")):
                raise ValueError()
        elif (type(record["project_id"]) is not str
              or not 1 <= len(record["project_id"]) <= ph.MAX_PROJECT_ID_CHARS
              or not ph._hex(record["state"]) or not ph._hex(record["content_sha256"])
              or type(record["source_version"]) is not int or record["source_version"] < 1):
            raise ValueError()
        if canonical_bytes(record) != raw:
            raise ValueError()
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise PhysicalLegacyError("PHYSICAL_LEGACY_CORRUPT") from None
    return record


def _read_for_head(backend, tenant_id, drawing_id):
    try:
        return _read_freeze(backend, tenant_id, drawing_id)
    except PhysicalLegacyError as exc:
        code = {"PHYSICAL_LEGACY_STORE_UNAVAILABLE": "PHYSICAL_HEAD_STORE_UNAVAILABLE",
                "PHYSICAL_LEGACY_ID_INVALID": "PHYSICAL_HEAD_ID_INVALID"}.get(
                    exc.code, "PHYSICAL_HEAD_CORRUPT")
        raise ph.PhysicalHeadError(code) from None


def _check_project(record, project_id):
    if record["legacy_last_index"] >= 0 and record["project_id"] != project_id:
        raise PhysicalLegacyError("PHYSICAL_LEGACY_PROJECT_MISMATCH")
    return record


def freeze_legacy(backend, tenant_id, drawing_id, *, project_id):
    if type(project_id) is not str or not 1 <= len(project_id) <= ph.MAX_PROJECT_ID_CHARS:
        raise PhysicalLegacyError("PHYSICAL_LEGACY_ID_INVALID")
    key = freeze_key(tenant_id, drawing_id)
    if write_loop.drawing_mutations_refusal() is not None:
        raise PhysicalLegacyError("PHYSICAL_LEGACY_WRITES_DRAINED")
    if not ph._atomic_backend(backend):
        raise PhysicalLegacyError("PHYSICAL_LEGACY_STORE_UNSAFE")
    record = _read_freeze(backend, tenant_id, drawing_id)
    if record is not None:
        return _check_project(record, project_id)
    try:
        view, _ = ph._head(backend, tenant_id, drawing_id, project_id)
    except ph.PhysicalHeadError as exc:
        code = {"PHYSICAL_HEAD_PROJECT_MISMATCH": "PHYSICAL_LEGACY_PROJECT_MISMATCH",
                "PHYSICAL_HEAD_STORE_UNAVAILABLE": "PHYSICAL_LEGACY_STORE_UNAVAILABLE",
                "PHYSICAL_HEAD_ID_INVALID": "PHYSICAL_LEGACY_ID_INVALID"}.get(
                    exc.code, "PHYSICAL_LEGACY_CORRUPT")
        raise PhysicalLegacyError(code) from None
    record = {"schema": FREEZE_SCHEMA, "tenant_id": tenant_id, "drawing_id": drawing_id,
              "project_id": None if view is None else view["project_id"],
              "legacy_last_index": -1 if view is None else view["index"],
              "state": None if view is None else view["state"]["artifact_id"],
              "content_sha256": None if view is None else view["state"]["content_sha256"],
              "source_version": None if view is None else view["state"]["source_version"]}
    raw = canonical_bytes(record)
    if len(raw) > MAX_RECORD_BYTES:
        raise PhysicalLegacyError("PHYSICAL_LEGACY_CORRUPT")
    try:
        backend.put_if_absent_or_verify(key, raw)
    except store.ImmutableConflict:
        winner = _read_freeze(backend, tenant_id, drawing_id)
        if winner is None:
            raise PhysicalLegacyError("PHYSICAL_LEGACY_CORRUPT") from None
        return _check_project(winner, project_id)
    except (OSError, RuntimeError, ValueError):
        raise PhysicalLegacyError("PHYSICAL_LEGACY_STORE_UNAVAILABLE") from None
    return record


def refuse_if_frozen(backend, tenant_id, drawing_id):
    if _read_freeze(backend, tenant_id, drawing_id) is not None:
        raise PhysicalLegacyError("PHYSICAL_LEGACY_FROZEN")


def _descends(manifest, v, s):
    try:
        rows = manifest["versions"]
        if type(rows) is not list or type(v) is not int or v < 1 or type(s) is not int or s < 1:
            raise ValueError()
        parents = {}
        for row in rows:
            if (type(row) is not dict or type(row["v"]) is not int or row["v"] < 1
                    or row["v"] in parents or (row["parent"] is not None
                    and (type(row["parent"]) is not int or row["parent"] < 1))):
                raise ValueError()
            parents[int(row["v"])] = row["parent"]
        for _ in range(len(rows) + 1):
            if v is None:
                return False
            if v not in parents:
                raise ValueError()
            if v == s:
                return True
            v = parents[v]
        raise ValueError()
    except (KeyError, ValueError, TypeError, UnicodeError, RecursionError):
        raise ph.PhysicalHeadError("PHYSICAL_HEAD_CORRUPT") from None


def _manifest(backend, tenant_id, drawing_id):
    try:
        return store.load_manifest(backend, tenant_id, drawing_id)
    except (KeyError, ValueError, TypeError, UnicodeError):
        raise ph.PhysicalHeadError("PHYSICAL_HEAD_CORRUPT") from None
    except (OSError, RuntimeError) as exc:
        code = ("PHYSICAL_HEAD_CORRUPT" if
                getattr(getattr(exc, "response", None), "status_code", None) == 404
                else "PHYSICAL_HEAD_STORE_UNAVAILABLE")
        raise ph.PhysicalHeadError(code) from None


def legacy_view(backend, tenant_id, drawing_id, v, *, project_id):
    freeze = _read_for_head(backend, tenant_id, drawing_id)
    if freeze is not None:
        index = freeze["legacy_last_index"]
        if index < 0:
            return None, None
        if freeze["project_id"] != project_id:
            raise ph.PhysicalHeadError("PHYSICAL_HEAD_PROJECT_MISMATCH")
        source = freeze["source_version"]
    else:
        log = ph._Log(backend, tenant_id, drawing_id)
        index = log.last()
        if index < 0:
            return None, None
        view, content = ph._view_at(log, index, project_id)
        source = view["state"]["source_version"]
    if not _descends(_manifest(backend, tenant_id, drawing_id), v, source):
        return None, None
    if freeze is not None:
        view, content = ph._view_at(ph._Log(backend, tenant_id, drawing_id), index, project_id)
        if (view["state"]["artifact_id"] != freeze["state"]
                or view["state"]["content_sha256"] != freeze["content_sha256"]
                or view["state"]["source_version"] != freeze["source_version"]):
            raise ph.PhysicalHeadError("PHYSICAL_HEAD_CORRUPT")
    return view, content


def legacy_reference(backend, tenant_id, drawing_id, v, *, project_id):
    view, _ = legacy_view(backend, tenant_id, drawing_id, v, project_id=project_id)
    if view is None:
        return None
    return store.check_physical_reference({
        "head_index": view["index"], "state_artifact_id": view["state"]["artifact_id"],
        "state_content_sha256": view["state"]["content_sha256"]})


def legacy_status(backend, tenant_id, drawing_id):
    freeze = _read_freeze(backend, tenant_id, drawing_id)
    last = ph._Log(backend, tenant_id, drawing_id).last()
    stranded = freeze is not None and last > freeze["legacy_last_index"]
    if stranded:
        manifest = _manifest(backend, tenant_id, drawing_id)
        for row in manifest["versions"]:
            _, ref = store.read_version_physical(backend, tenant_id, drawing_id, row["v"])
            if ref is not None and ref["head_index"] > freeze["legacy_last_index"]:
                stranded = False
                break
    return {"freeze": freeze, "log_last_index": last, "stranded_after_freeze": stranded}
