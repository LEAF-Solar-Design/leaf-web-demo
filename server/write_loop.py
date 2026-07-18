"""
Write loop (M2): wire the proven drawing.write capability into the PRODUCT loop.

A registered `drawing.write` tool run produces a NEW immutable drawing version in
the versioned store (da/store.py), with undo/redo, working offline (APS_LIVE=0)
and live (APS_LIVE=1). This module is the WRITE BRANCH of the execution chain —
server/broker.py delegates here for any tool whose package declares
`capabilities: ["drawing.write"]`; every read tool takes the unchanged path.

Two representations of a stored version (documented in CONTRACT-ADDENDUM §11):

  * APS_LIVE=0 (mock): a version's payload IS the intake JSON. The write tool's
    `run(intake, params)` returns `result.mutations = {added, removed}`; the chain
    applies them to the CURRENT version's intake -> new intake -> put_drawing.
  * APS_LIVE=1 (live): a version's payload is real DWG bytes; a sibling
    `*.intake.json` cache key holds the re-extracted intake. The chain runs the
    proven LeafWriteProbe Activity (HostDwg = current version, Result = output.dwg),
    stores output.dwg via put_drawing, re-extracts for the intake cache.

Credential discipline: this module NEVER imports da.* at top level. The live
path receives the credential-holding `da` client from the broker (the only
process allowed to hold it), mirroring server/tool_loader.py.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from envelopes import DEFAULT_HTTP_STATUS, ErrorCode, err_envelope, ok_envelope

SERVER_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SERVER_DIR.parent
CACHED_INTAKE_PATH = PROJECT_ROOT / "data" / "rooftop_demo.intake.json"

# Make da/store.py importable. APPEND (never front-insert) so a stdlib module is
# never shadowed by a da/ sibling (e.g. da/queue.py); `store` is da-only and still
# resolves. store.py itself front-inserts da/ once imported — the existing,
# accepted behavior of every code path that touches the store.
_DA_DIR = str(PROJECT_ROOT / "da")
if _DA_DIR not in sys.path:
    sys.path.append(_DA_DIR)

# Well-known demo drawing bootstrapped from the cached intake at APS_LIVE=0.
DEMO_DRAWING_ID = "demo"
# The already-provisioned, proven write Activity reused on the live path.
WRITE_ACTIVITY = os.environ.get("LEAF_WRITE_ACTIVITY", "LeafWriteProbe")
PROBE_LAYER = "LEAF_WRITE_PROBE"

USD_PER_HR = float(os.environ.get("APS_USD_PER_HR", "10"))


# --------------------------------------------------------------------------- #
# tool classification + backend selection
# --------------------------------------------------------------------------- #
def is_write_tool(tool: Dict[str, Any]) -> bool:
    """True iff the tool package declares the drawing.write capability (§2)."""
    caps = (tool or {}).get("capabilities") or []
    return "drawing.write" in caps


def store_dir() -> str:
    """Local filesystem store root (gitignored). Env LEAF_STORE_DIR overrides so
    tests and the app+broker pair can share an isolated directory."""
    return os.environ.get("LEAF_STORE_DIR", str(SERVER_DIR / "drawings"))


def default_backend(*, aps_live: bool = False, da: Any = None):
    """Pick the store backend. APS_LIVE=0 -> local FilesystemBackend (no creds);
    APS_LIVE=1 with a da client -> OSSBackend (persistent bucket)."""
    import store  # lazy (store imports da/client)
    if aps_live and da is not None:
        return store.OSSBackend()
    return store.FilesystemBackend(store_dir())


def _drawing_id(params: Dict[str, Any]) -> str:
    did = (params or {}).get("drawing_id")
    return str(did) if did else DEMO_DRAWING_ID


# --------------------------------------------------------------------------- #
# intake representation helpers
# --------------------------------------------------------------------------- #
def intake_cache_key(tenant_id: str, drawing_id: str, version: int) -> str:
    """Sibling key holding a version's cached intake JSON (used on the live path
    where the version blob itself is DWG bytes, not intake)."""
    import store
    v = int(version)
    return (f"tenants/{store.sanitize_id(tenant_id)}/drawings/"
            f"{store.sanitize_id(drawing_id)}/v/{v:08d}.intake.json")


def read_intake(backend, tenant_id: str, drawing_id: str,
                version="head") -> Tuple[int, Dict[str, Any]]:
    """Resolve `version` and return (version_int, intake_dict).

    Prefers the explicit intake cache key (live representation); falls back to
    reading the version blob itself as JSON (mock representation: the blob IS the
    intake). Raises KeyError/ValueError on a missing drawing/version."""
    import store
    v, vkey = store.resolve_version(backend, tenant_id, drawing_id, version)
    ckey = intake_cache_key(tenant_id, drawing_id, v)
    raw = backend.get(ckey) if backend.exists(ckey) else backend.get(vkey)
    return v, json.loads(raw.decode("utf-8"))


def apply_mutations(intake: Dict[str, Any], mutations: Dict[str, Any]) -> Dict[str, Any]:
    """Apply `{added:[<intake entities>], removed:[<handles>]}` to a copy of the
    current intake and return the NEW intake. Removes polylines by handle; appends
    added entities and registers any new layer they introduce."""
    new = copy.deepcopy(intake or {})
    removed = {str(h) for h in (mutations.get("removed") or [])}
    if removed:
        new["polylines"] = [p for p in (new.get("polylines") or [])
                            if str(p.get("handle")) not in removed]
    added = mutations.get("added") or []
    if added:
        polys = new.setdefault("polylines", [])
        layers = new.setdefault("layers", [])
        for e in added:
            polys.append(e)
            lyr = e.get("layer")
            if lyr and lyr not in layers:
                layers.append(lyr)
    return new


def _put_bytes_version(backend, tenant_id: str, drawing_id: str, data: bytes,
                       parent_version: int, meta: Dict[str, Any]) -> int:
    """put_drawing takes a local path (immutability + sha are computed there), so
    stage `data` to a temp file and append the new version."""
    import store
    fd, tmp = tempfile.mkstemp(suffix=".blob")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(bytes(data))
        return store.put_drawing(backend, tenant_id, drawing_id, tmp,
                                 parent_version=parent_version, meta=meta)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# demo bootstrap
# --------------------------------------------------------------------------- #
def ensure_demo_drawing(backend, tenant_id: str, drawing_id: str) -> None:
    """Bootstrap the well-known `demo` drawing (v1 = cached intake JSON) on first
    use at APS_LIVE=0. Non-demo drawings must already exist (raises KeyError)."""
    import store
    if backend.exists(store.manifest_key(tenant_id, drawing_id)):
        return
    if drawing_id != DEMO_DRAWING_ID:
        raise KeyError(f"drawing not found: {tenant_id}/{drawing_id}")
    data = CACHED_INTAKE_PATH.read_bytes()
    fd, tmp = tempfile.mkstemp(suffix=".intake.json")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        store.ingest_drawing(backend, tenant_id, tmp, drawing_id=drawing_id)
    except ValueError:
        pass  # race: another request bootstrapped it first
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# router-facing views (GET intake / POST undo / POST redo)
# --------------------------------------------------------------------------- #
def intake_view(tenant_id: str, drawing_id: str, version="head", *, backend=None) -> Dict[str, Any]:
    import store
    backend = backend or default_backend()
    ensure_demo_drawing(backend, tenant_id, drawing_id)
    v, intake = read_intake(backend, tenant_id, drawing_id, version)
    m = store.load_manifest(backend, tenant_id, drawing_id)
    return {"intake": intake, "version": v, "head": int(m["head"]), "latest": int(m["latest"])}


def undo_view(tenant_id: str, drawing_id: str, *, backend=None) -> Dict[str, Any]:
    import store
    backend = backend or default_backend()
    ensure_demo_drawing(backend, tenant_id, drawing_id)
    new_head = store.undo(backend, tenant_id, drawing_id)
    v, intake = read_intake(backend, tenant_id, drawing_id, "head")
    m = store.load_manifest(backend, tenant_id, drawing_id)
    return {"version": new_head, "head": int(m["head"]), "latest": int(m["latest"]), "intake": intake}


def redo_view(tenant_id: str, drawing_id: str, *, backend=None) -> Dict[str, Any]:
    import store
    backend = backend or default_backend()
    ensure_demo_drawing(backend, tenant_id, drawing_id)
    new_head = store.redo(backend, tenant_id, drawing_id)
    v, intake = read_intake(backend, tenant_id, drawing_id, "head")
    m = store.load_manifest(backend, tenant_id, drawing_id)
    return {"version": new_head, "head": int(m["head"]), "latest": int(m["latest"]), "intake": intake}


# --------------------------------------------------------------------------- #
# the WRITE BRANCH of the execution chain (called by server/broker.py)
# --------------------------------------------------------------------------- #
def _status_for(env: Dict[str, Any]) -> int:
    if env.get("ok"):
        return 200
    code = (env.get("error") or {}).get("error_code", ErrorCode.INTERNAL)
    return DEFAULT_HTTP_STATUS.get(code, 500)


def run_write_mock(tool: Dict[str, Any], params: Dict[str, Any], tenant_id: str, *,
                   backend, t0: float, run_tool_dynamic_fn,
                   degraded: bool = False) -> Tuple[Dict[str, Any], int]:
    """APS_LIVE=0 write: run the tool file for its mutations, apply them to the
    current version's intake, persist a new version, stamp result.new_version."""
    name = tool.get("name")
    version = tool.get("version", "1.0.0")
    drawing_id = _drawing_id(params)
    try:
        ensure_demo_drawing(backend, tenant_id, drawing_id)
        head_v, cur_intake = read_intake(backend, tenant_id, drawing_id, "head")
    except (KeyError, ValueError) as exc:
        return (err_envelope(ErrorCode.BAD_PARAMS, f"drawing/version unavailable: {exc}",
                             retryable=False, tool=name, version=version),
                DEFAULT_HTTP_STATUS[ErrorCode.BAD_PARAMS])

    env = run_tool_dynamic_fn(tool, cur_intake, dict(params or {}),
                              aps_live=False, da=None, t0=t0)
    if not env.get("ok"):
        return env, _status_for(env)

    result = env.get("result") or {}
    mutations = result.get("mutations") or {}
    try:
        new_intake = apply_mutations(cur_intake, mutations)
        new_v = _put_bytes_version(
            backend, tenant_id, drawing_id,
            json.dumps(new_intake, separators=(",", ":")).encode("utf-8"),
            parent_version=head_v,
            meta={"tool": name, "note": "mock write (intake payload)"})
    except Exception as exc:  # noqa: BLE001
        return (err_envelope(ErrorCode.INTERNAL, f"write persist failed: {type(exc).__name__}: {exc}",
                             retryable=False, tool=name, version=version),
                DEFAULT_HTTP_STATUS[ErrorCode.INTERNAL])

    result["new_version"] = {"drawing_id": drawing_id, "version": new_v, "parent": head_v}
    env["result"] = result
    if degraded:
        env["degraded_mode"] = True
    return env, 200


def run_write_live(tool: Dict[str, Any], params: Dict[str, Any], tenant_id: str, *,
                   backend, da: Any, t0: float,
                   ledger_entry: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], int]:
    """APS_LIVE=1 write: run the proven LeafWriteProbe Activity on the current
    version's DWG, store output.dwg as a new version, re-extract for the intake
    cache, stamp result.new_version. `da` is the credential-holding client."""
    import store
    name = tool.get("name")
    version = tool.get("version", "1.0.0")
    drawing_id = _drawing_id(params)
    try:
        if not backend.exists(store.manifest_key(tenant_id, drawing_id)):
            return (err_envelope(ErrorCode.BAD_PARAMS,
                                 f"drawing not in store: {tenant_id}/{drawing_id} "
                                 f"(ingest it before a live write)", retryable=False,
                                 tool=name, version=version),
                    DEFAULT_HTTP_STATUS[ErrorCode.BAD_PARAMS])
        head_v, vkey = store.resolve_version(backend, tenant_id, drawing_id, "head")

        in_url = da.signed_download_url(vkey)
        ts = int(time.time())
        out_key = f"out/{ts}_{store.sanitize_id(drawing_id)}_write.dwg"
        up_key, out_url = da.signed_upload_url(out_key)
        activity_id = da.activity_qualified(WRITE_ACTIVITY)
        w_args = {"HostDwg": {"url": in_url, "verb": "get"},
                  "Result": {"url": out_url, "verb": "put"}}
        status = da.submit_workitem(activity_id, w_args, dry_run=False, poll=True,
                                    tenant_id=tenant_id)
        if status.get("status") != "success":
            return (err_envelope(ErrorCode.WORKITEM_FAILED,
                                 f"write WorkItem {status.get('id')} status={status.get('status')} "
                                 f"report={status.get('reportUrl')}", retryable=True,
                                 tool=name, version=version),
                    DEFAULT_HTTP_STATUS[ErrorCode.WORKITEM_FAILED])
        da.finalize_upload(out_key, up_key)
        out_bytes = da.download_object(out_key)
        if not out_bytes:
            return (err_envelope(ErrorCode.WORKITEM_FAILED, "write produced 0-byte output.dwg",
                                 retryable=True, tool=name, version=version),
                    DEFAULT_HTTP_STATUS[ErrorCode.WORKITEM_FAILED])

        wi_id = status.get("id")
        new_v = _put_bytes_version(backend, tenant_id, drawing_id, out_bytes,
                                   parent_version=head_v,
                                   meta={"tool": name, "workitem_id": wi_id,
                                         "note": "live write (output.dwg)"})

        # re-extract the new version's DWG for the intake cache
        fd, tmp = tempfile.mkstemp(suffix=".dwg")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(out_bytes)
            intake = da.extract(tmp)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        backend.put(intake_cache_key(tenant_id, drawing_id, new_v),
                    json.dumps(intake, separators=(",", ":")).encode("utf-8"))

        eng_s = da._engine_seconds(status)
        cost = None if eng_s is None else {
            "engine_seconds": eng_s,
            "usd_est": round(eng_s / 3600.0 * USD_PER_HR, 4)}
        if ledger_entry is not None and isinstance(cost, dict):
            ledger_entry["engine_seconds"] = cost["engine_seconds"]
            ledger_entry["usd_est"] = cost["usd_est"]

        result = {
            "new_version": {"drawing_id": drawing_id, "version": new_v, "parent": head_v},
            "workitem_id": wi_id,
            "probe_layer": PROBE_LAYER,
            "output_dwg_bytes": len(out_bytes),
        }
        env = ok_envelope(name, version, result,
                          overlay={"probe_layer_added": PROBE_LAYER},
                          timing_ms=int((time.perf_counter() - t0) * 1000),
                          cost=cost, degraded_mode=False)
        return env, 200
    except FileNotFoundError as exc:  # creds missing
        return (err_envelope(ErrorCode.APS_UNAVAILABLE, str(exc), retryable=False,
                             tool=name, version=version),
                DEFAULT_HTTP_STATUS[ErrorCode.APS_UNAVAILABLE])
    except Exception as exc:  # noqa: BLE001
        return (err_envelope(ErrorCode.WORKITEM_FAILED, f"{type(exc).__name__}: {exc}",
                             retryable=True, tool=name, version=version),
                DEFAULT_HTTP_STATUS[ErrorCode.WORKITEM_FAILED])
