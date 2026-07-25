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
import hashlib
import hmac
import json
import logging
import os
import secrets
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from envelopes import DEFAULT_HTTP_STATUS, ErrorCode, err_envelope, ok_envelope
from tenant_id_validator import validate_tenant_id

SERVER_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SERVER_DIR.parent
CACHED_INTAKE_PATH = PROJECT_ROOT / "data" / "rooftop_demo.intake.json"
LOGGER = logging.getLogger(__name__)

# Make da/store.py importable. APPEND (never front-insert) so a stdlib module is
# never shadowed by a da/ sibling (e.g. da/queue.py); `store` is da-only and still
# resolves. store.py itself front-inserts da/ once imported — the existing,
# accepted behavior of every code path that touches the store.
_DA_DIR = str(PROJECT_ROOT / "da")
if _DA_DIR not in sys.path:
    sys.path.append(_DA_DIR)

# Well-known demo drawing bootstrapped from the cached intake at APS_LIVE=0.
DEMO_DRAWING_ID = "demo"
# Reserved tenant-id namespace for ephemeral guest uploads (server/guest_uploads.py
# mints these; server-side ONLY). The prefix is load-bearing for two fail-closed
# rules: guest tenants store in an isolated, purgeable filesystem root
# (backend_for_tenant), and ensure_demo_drawing NEVER bootstraps a drawing for
# them (a guest has no drawings except what extraction actually produced).
GUEST_TENANT_PREFIX = "guest-"
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


def blob_store_mode() -> str:
    """Return the configured durable blob location, independent of APS use."""
    mode = os.environ.get("LEAF_BLOB_STORE", "legacy").strip().lower()
    if mode not in {"legacy", "filesystem", "aps_oss"}:
        raise RuntimeError(
            "LEAF_BLOB_STORE must be 'legacy', 'filesystem', or 'aps_oss'")
    return mode


def _cleanup_scratch_objects(da: Any, scratch_keys) -> None:
    """Best-effort immediate cleanup with nonsecret failure telemetry."""
    if not scratch_keys:
        return
    delete = (
        getattr(da, "delete_scratch_object", None)
        or getattr(da, "delete_object", None)
    )
    if delete is None:
        for key in scratch_keys:
            LOGGER.warning(
                "APS scratch cleanup unavailable",
                extra={
                    "scratch_key_sha256": hashlib.sha256(
                        str(key).encode("utf-8")
                    ).hexdigest(),
                    "error_type": "DeleteMethodUnavailable",
                },
            )
        return
    for key in scratch_keys:
        try:
            delete(key)
        except Exception as exc:  # noqa: BLE001 - cleanup stays best effort
            LOGGER.warning(
                "APS scratch cleanup failed",
                extra={
                    "scratch_key_sha256": hashlib.sha256(
                        str(key).encode("utf-8")
                    ).hexdigest(),
                    "error_type": type(exc).__name__,
                },
            )


def drawing_mutations_enabled() -> bool:
    """Cutover gate for authored, checkout, and broker drawing mutations."""
    return os.environ.get("LEAF_DRAWING_MUTATIONS_ENABLED", "1") == "1"


def upload_import_mutations_enabled() -> bool:
    """Fail-closed gate for upload admission and canonical upload import."""
    return os.environ.get("LEAF_UPLOAD_IMPORT_MUTATIONS_ENABLED", "0") == "1"


def default_backend(*, aps_live: bool = False, da: Any = None):
    """Pick storage independently from whether execution uses live APS.

    ``aps_live`` remains for caller compatibility. ``LEAF_BLOB_STORE`` owns
    the storage choice. APS OSS is available only with a broker-side client.
    """
    import store  # lazy (store imports da/client)
    mode = blob_store_mode()
    if mode == "aps_oss" or (mode == "legacy" and aps_live and da is not None):
        if da is None:
            raise RuntimeError(
                "LEAF_BLOB_STORE=aps_oss requires the broker-side APS client")
        return store.OSSBackend()
    return store.FilesystemBackend(store_dir())


def guest_store_dir() -> str:
    """Isolated store root for GUEST tenants only (gitignored, env-overridable).

    Guests get their own filesystem root — never the default backend — because
    the retention promise ("deleted after N hours") is only honest on a store
    the purge job can provably delete from, and the StorageBackend interface
    has no delete: guest_uploads.purge_expired() deletes at the FILESYSTEM
    level, which is only sound when every guest artifact lives under this one
    root and nothing else does."""
    return os.environ.get("LEAF_GUEST_STORE_DIR", str(SERVER_DIR / "guest_drawings"))


def backend_for_tenant(tenant_id: str, *, aps_live: bool = False, da: Any = None):
    """The store backend for ONE tenant: guest tenants -> the isolated local
    guest store (always filesystem, even at APS_LIVE=1 — see guest_store_dir);
    everyone else -> default_backend, byte-identical to before this seam."""
    import store  # lazy (store imports da/client)
    if str(tenant_id).startswith(GUEST_TENANT_PREFIX):
        return store.FilesystemBackend(guest_store_dir())
    return default_backend(aps_live=aps_live, da=da)


def upload_backend_for_tenant(tenant_id: str):
    """Credential-free backend for staged upload state and extracted artifacts.

    The app and broker share ``LEAF_STORE_DIR`` and ``LEAF_GUEST_STORE_DIR`` on
    the durable drawings volume. APS extraction crosses the broker HTTP seam,
    but upload marker, raw file, and intake-cache persistence stays on that
    shared volume. The tenant-facing app must never construct an OSS backend,
    because doing so loads the broker-only APS credential.
    """
    return backend_for_tenant(tenant_id, aps_live=False, da=None)


def _drawing_id(params: Dict[str, Any]) -> str:
    did = (params or {}).get("drawing_id")
    return str(did) if did else DEMO_DRAWING_ID


def target_drawing_id(params: Dict[str, Any]) -> str:
    """The store drawing a run will WRITE, from its params.

    Public because the /api/run route has to resolve the same drawing this
    module will publish to, when it exchanges a checkout capability for the
    lock's identity. It must not re-derive that id: `req.dwg` is the INTAKE
    source (e.g. `rooftop_demo`) and is frequently a different drawing from
    `params.drawing_id`, so a route computing its own answer could verify a
    capability against one drawing and publish to another.
    """
    return _drawing_id(params)


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


def upload_marker_key(tenant_id: str, drawing_id: str) -> str:
    """Sibling key holding an uploaded drawing's upload/extraction state
    (server/guest_uploads.py writes it; ensure_demo_drawing consults it as a
    fail-closed guard). Lives next to manifest.json so purge deletes it with
    the drawing directory."""
    import store
    return (f"tenants/{store.sanitize_id(tenant_id)}/drawings/"
            f"{store.sanitize_id(drawing_id)}/upload.state.json")


def read_intake(backend, tenant_id: str, drawing_id: str,
                version="head") -> Tuple[int, Dict[str, Any]]:
    """Resolve `version` and return (version_int, intake_dict).

    Prefers the explicit intake cache key (live representation); falls back to
    reading the version blob itself as JSON (mock representation: the blob IS the
    intake). Raises KeyError/ValueError on a missing drawing/version."""
    import store
    v, vkey = store.resolve_version(backend, tenant_id, drawing_id, version)
    ckey = intake_cache_key(tenant_id, drawing_id, v)
    raw = None
    if store.authority_mode() == "postgres":
        import guest_uploads
        if guest_uploads.upload_store_mode() == "postgres":
            marker = guest_uploads.read_marker(
                backend, tenant_id, drawing_id)
            if marker is not None and marker.get("extracted_version") == v:
                ref = marker.get("intake_ref")
                digest = marker.get("intake_sha256")
                if (
                    marker.get("status") != "ready"
                    or ref != ckey
                    or not isinstance(digest, str)
                    or len(digest) != 64
                ):
                    raise ValueError(
                        "ready upload has no source-bound intake proof")
                candidate = backend.get(ref)
                if not hmac.compare_digest(
                    hashlib.sha256(candidate).hexdigest(), digest):
                    raise ValueError(
                        "ready upload intake does not match its source proof")
                raw = candidate
    if raw is None:
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
                       parent_version: int, meta: Dict[str, Any], *,
                       holder: Optional[str] = None,
                       fence: Optional[int] = None) -> int:
    """put_drawing takes a local path (immutability + sha are computed there), so
    stage `data` to a temp file and append the new version.

    `holder`/`fence` are the caller's single-writer identity, forwarded verbatim
    so the store can refuse a write published under another session's checkout
    (da/store.py put_drawing)."""
    import store
    fd, tmp = tempfile.mkstemp(suffix=".blob")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(bytes(data))
        return store.put_drawing(backend, tenant_id, drawing_id, tmp,
                                 parent_version=parent_version, meta=meta,
                                 holder=holder, fence=fence)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# demo bootstrap
# --------------------------------------------------------------------------- #
def ensure_demo_drawing(backend, tenant_id: str, drawing_id: str) -> None:
    """Bootstrap ANY first-seen `drawing_id` (v1 = cached intake JSON) on first use
    at APS_LIVE=0, via the identical `store.ingest_drawing` path the well-known
    `demo` drawing has always used — `demo` is now just one instance of this
    general rule (its resulting v1 is byte-identical to before: same source file,
    same ingest call). Generalizes the earlier demo-only special case per
    leaf-backend-gaps.md §"Any plausible drawing_id ... should resolve to a
    provisioned drawing" (fix (a): extend auto-bootstrap to any first-seen id).

    `drawing_id` must satisfy the ONE shared slug-safe id rule
    (tenant_id_validator.validate_tenant_id — lowercase alnum/`_`/`-`, must start
    alphanumeric, 1..63 chars) so a path-y id (contains `/`, `.`, `..`, uppercase,
    unicode, or is empty) is REJECTED with ValueError up front, before any
    store/filesystem access — defense in depth: this is the auto-provisioning
    gate, not just store.sanitize_id's (later, key-construction-time) rejection
    of the same characters. This never rejects a REAL, already-provisioned
    drawing: every id that can exist in the store was itself sanitize_id-checked
    against this identical pattern at ingest time (store.ingest_drawing /
    put_drawing), so any id reachable via `backend.exists(...)` below already
    passes here too.
    """
    import store
    validate_tenant_id(drawing_id, kind="drawing id")  # reject path-y / malformed ids, up front
    upload_marker_exists = False
    if store.authority_mode() == "postgres":
        try:
            store.load_manifest(backend, tenant_id, drawing_id)
            return
        except KeyError:
            pass
        # PostgreSQL upload authority lives in drawing_upload_attempts, not in
        # the compatibility filesystem marker. Check the authoritative row
        # before demo bootstrap so an intake read cannot race extraction and
        # publish the bundled rooftop as the uploaded drawing's version 1.
        import guest_uploads
        if guest_uploads.upload_store_mode() == "postgres":
            upload_marker_exists = (
                guest_uploads.read_marker(backend, tenant_id, drawing_id)
                is not None
            )
    elif backend.exists(store.manifest_key(tenant_id, drawing_id)):
        return
    # FAIL-CLOSED GUARDS (guest-upload lane, CONTRACT-ADDENDUM §19): the
    # auto-bootstrap below serves the CACHED DEMO ROOF's geometry. For an
    # uploaded drawing that is fabricated data — worse than an error — so two
    # classes of id must never reach it:
    #   (a) ANY drawing under a guest tenant: guests own nothing except what
    #       extraction actually produced. Unknown -> KeyError (routes 404).
    #   (b) ANY drawing with an upload marker but no manifest: its extraction
    #       is pending or failed. ValueError names the state (routes 404 with
    #       the message; the honest status lives at .../upload-status).
    if str(tenant_id).startswith(GUEST_TENANT_PREFIX):
        raise KeyError(f"unknown drawing {drawing_id!r} for guest tenant "
                       f"(guest drawings exist only via upload + extraction)")
    if (
        upload_marker_exists
        or backend.exists(upload_marker_key(tenant_id, drawing_id))
    ):
        raise ValueError(
            f"drawing {drawing_id!r} was uploaded but extraction has not "
            f"produced geometry (see /api/drawings/{drawing_id}/upload-status); "
            f"refusing the demo-intake bootstrap")
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


def undo_view(tenant_id: str, drawing_id: str, *, backend=None,
              holder: Optional[str] = None,
              fence: Optional[int] = None) -> Dict[str, Any]:
    """Step head back, then re-read the intake at the new head.

    ``holder``/``fence`` are the caller's single-writer identity, forwarded
    verbatim to ``store.undo`` — which applies the SAME check as a version
    publish, under the same row lock. Both default to None (no check), the shape
    every non-product caller uses.
    """
    import store
    backend = backend or default_backend()
    ensure_demo_drawing(backend, tenant_id, drawing_id)
    new_head = store.undo(backend, tenant_id, drawing_id, holder=holder, fence=fence)
    v, intake = read_intake(backend, tenant_id, drawing_id, "head")
    m = store.load_manifest(backend, tenant_id, drawing_id)
    return {"version": new_head, "head": int(m["head"]), "latest": int(m["latest"]), "intake": intake}


def redo_view(tenant_id: str, drawing_id: str, *, backend=None,
              holder: Optional[str] = None,
              fence: Optional[int] = None) -> Dict[str, Any]:
    """Step head forward. Same single-writer forwarding as ``undo_view``."""
    import store
    backend = backend or default_backend()
    ensure_demo_drawing(backend, tenant_id, drawing_id)
    new_head = store.redo(backend, tenant_id, drawing_id, holder=holder, fence=fence)
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


def _named_or_anonymous(store, holder: Optional[str]) -> str:
    """Normalize a MISSING single-writer identity to the reserved anonymous id.

    This is the one chokepoint every product write passes through, so doing it
    here covers every way an identity can go missing rather than one caller at a
    time: a pre-rollout job row recovered after a restart (its execution context
    has no such key), an in-process retry or local fallback, an older app that
    sends no `holder`, and an older broker that drops the field. Each of those
    would otherwise arrive as None and skip the check entirely — publishing under
    whatever lock happened to be open, which is the bug this module closes.

    `None` keeps its meaning for direct `store.put_drawing` callers that are NOT
    product writes (ingest, the offline harness, the store's own tests); they do
    not come through here.

    Anonymous is refused against any ACTIVE lock and publishes normally on an
    unlocked drawing — the honest reading of a write whose submitter never named
    itself."""
    return holder if holder else store.ANONYMOUS_HOLDER


def _checkout_denied(exc: Exception, name: Any, tool_version: Any) -> Tuple[Dict[str, Any], int]:
    """A write refused because the caller does not hold the single-writer lock.

    FORBIDDEN/403 deliberately, matching DELETE .../checkout — the caller is
    authenticated and entitled, it just does not own this drawing right now. It
    is NOT retryable: retrying changes nothing until the lock is taken or the
    lease lapses, so a client that treats retryable errors as transient must not
    spin on it."""
    return (err_envelope(ErrorCode.FORBIDDEN, str(exc), retryable=False,
                         tool=name, version=tool_version),
            DEFAULT_HTTP_STATUS[ErrorCode.FORBIDDEN])


def run_write_mock(tool: Dict[str, Any], params: Dict[str, Any], tenant_id: str, *,
                   backend, t0: float, run_tool_dynamic_fn,
                   degraded: bool = False, version="head",
                   holder: Optional[str] = None,
                   fence: Optional[int] = None) -> Tuple[Dict[str, Any], int]:
    """APS_LIVE=0 write: run the tool file for its mutations, apply them to the
    BASE version's intake (``version``; default "head", unchanged), persist a new
    version whose parent is that base, stamp result.new_version.

    ``holder``/``fence`` are the caller's single-writer identity; the persist below
    is refused (403) when another session holds the checkout."""
    import store
    holder = _named_or_anonymous(store, holder)
    name = tool.get("name")
    tool_version = tool.get("version", "1.0.0")
    drawing_id = _drawing_id(params)
    try:
        ensure_demo_drawing(backend, tenant_id, drawing_id)
        head_v, cur_intake = read_intake(backend, tenant_id, drawing_id, version)
    except (KeyError, ValueError) as exc:
        return (err_envelope(ErrorCode.BAD_PARAMS, f"drawing/version unavailable: {exc}",
                             retryable=False, tool=name, version=tool_version),
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
            meta={"tool": name, "note": "mock write (intake payload)"},
            holder=holder, fence=fence)
    except store.CheckoutDenied as exc:
        # Caught BEFORE the blanket handler below: publishing under another
        # session's lock is an authorization answer (403), not a persist fault
        # (500). The tool already ran, but nothing was written.
        return _checkout_denied(exc, name, tool_version)
    except Exception as exc:  # noqa: BLE001
        return (err_envelope(ErrorCode.INTERNAL, f"write persist failed: {type(exc).__name__}: {exc}",
                             retryable=False, tool=name, version=tool_version),
                DEFAULT_HTTP_STATUS[ErrorCode.INTERNAL])

    result["new_version"] = {"drawing_id": drawing_id, "version": new_v, "parent": head_v}
    env["result"] = result
    if degraded:
        env["degraded_mode"] = True
    return env, 200


def _accepts_on_submitted(fn: Any) -> bool:
    """Whether `fn` can take an `on_submitted` kwarg.

    Mirrors the broker-side guard. `da` is a test double in most suites, so the
    kwarg is offered only to implementations that declare it (explicitly or via
    **kwargs); an older/stubbed submit_workitem keeps its exact call signature.
    """
    import inspect
    try:
        sig_params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if "on_submitted" in sig_params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig_params.values())


def run_write_live(tool: Dict[str, Any], params: Dict[str, Any], tenant_id: str, *,
                   backend, da: Any, t0: float,
                   ledger_entry: Optional[Dict[str, Any]] = None,
                   version="head", holder: Optional[str] = None,
                   fence: Optional[int] = None,
                   on_submitted=None) -> Tuple[Dict[str, Any], int]:
    """APS_LIVE=1 write: run the proven LeafWriteProbe Activity on the BASE
    version's DWG (``version``; default "head", unchanged), store output.dwg as a
    new version whose parent is that base, re-extract for the intake cache, stamp
    result.new_version. `da` is the credential-holding client.

    ``holder``/``fence`` are the caller's single-writer identity. They are checked
    TWICE: once up front (below, before the WorkItem is submitted, so an
    unauthorized writer never reaches a billable engine second) and again inside
    put_drawing at commit time, which is the authoritative check because it runs
    under the store's row lock."""
    import store
    holder = _named_or_anonymous(store, holder)
    name = tool.get("name")
    tool_version = tool.get("version", "1.0.0")
    drawing_id = _drawing_id(params)
    scratch_keys = []
    try:
        if not backend.exists(store.manifest_key(tenant_id, drawing_id)):
            return (err_envelope(ErrorCode.BAD_PARAMS,
                                 f"drawing not in store: {tenant_id}/{drawing_id} "
                                 f"(ingest it before a live write)", retryable=False,
                                 tool=name, version=tool_version),
                    DEFAULT_HTTP_STATUS[ErrorCode.BAD_PARAMS])
        # §19 fail-closed guard (review round 3, MAJOR): a live write signs the
        # version blob as HostDwg — that is only truthful when the blob IS DWG
        # bytes. An UPLOADED drawing records its source format in the upload
        # marker; anything non-.dwg (a DXF's intake-JSON mock blob) must refuse
        # honestly here rather than hand APS a mislabeled file.
        if backend.exists(upload_marker_key(tenant_id, drawing_id)):
            import guest_uploads
            if guest_uploads.upload_store_mode() == "postgres":
                marker = guest_uploads.read_marker(
                    backend, tenant_id, drawing_id) or {}
            else:
                try:
                    marker = json.loads(backend.get(
                        upload_marker_key(
                            tenant_id, drawing_id)).decode("utf-8"))
                except (KeyError, ValueError):
                    marker = {}
            source_ext = str(marker.get("source_ext") or "")
            if not source_ext:
                # Pre-round-4 markers (schema 1, no source_ext field): fall
                # back to the recorded filename's extension — present in every
                # marker since the feature commit — instead of bricking live
                # writes on already-persisted DWG uploads (review round 4,
                # MINOR: compatibility path for old markers).
                source_ext = os.path.splitext(
                    str(marker.get("filename") or ""))[1].lower()
            if source_ext != ".dwg":
                return (err_envelope(
                    ErrorCode.BAD_PARAMS,
                    f"live writes need a DWG source; drawing {drawing_id!r} was "
                    f"uploaded as {source_ext or 'an unknown format'} (its store "
                    f"blob is the extracted intake, not DWG bytes)",
                    retryable=False, tool=name, version=tool_version),
                    DEFAULT_HTTP_STATUS[ErrorCode.BAD_PARAMS])
        # Authorize BEFORE spending. Everything past this point costs money:
        # scratch uploads, signed URLs, a submitted WorkItem, polled engine
        # seconds. The commit inside _put_bytes_version re-checks under the
        # store's row lock and is the authoritative guard; this one exists so a
        # write published under another session's checkout is refused for free
        # instead of billed and then rejected.
        store.authorize_checkout(backend, tenant_id, drawing_id, holder, fence)
        ts = int(time.time())
        run_nonce = secrets.token_hex(8)
        head_v, vkey = store.resolve_version(backend, tenant_id, drawing_id, version)
        if isinstance(backend, store.OSSBackend):
            in_url = da.signed_download_url(vkey)
        else:
            # The persistent drawing blob lives on shared EFS. Stage only this
            # WorkItem input in broker-owned APS scratch storage, then keep the
            # resulting immutable version in the configured persistent backend.
            input_key = (
                da.ephemeral_input_key(
                    f"{store.sanitize_id(drawing_id)}_v{head_v}_{run_nonce}.dwg",
                    tenant_id=tenant_id,
                    ts=ts,
                )
                if hasattr(da, "ephemeral_input_key")
                else f"t/{store.sanitize_id(tenant_id)}/in/{ts}_"
                     f"{store.sanitize_id(drawing_id)}_v{head_v}_{run_nonce}.dwg"
            )
            fd, staged_input = tempfile.mkstemp(suffix=".dwg")
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(backend.get(vkey))
                if hasattr(da, "upload_scratch_object"):
                    da.upload_scratch_object(staged_input, input_key)
                else:
                    da.upload_object(staged_input, input_key)
                scratch_keys.append(input_key)
            finally:
                try:
                    os.remove(staged_input)
                except OSError:
                    pass
            in_url = (
                da.scratch_signed_download_url(input_key)
                if hasattr(da, "scratch_signed_download_url")
                else da.signed_download_url(input_key)
            )
        output_name = f"{store.sanitize_id(drawing_id)}_write_{run_nonce}"
        out_key = (
            da.ephemeral_output_key(
                output_name, tenant_id=tenant_id, ts=ts, suffix=".dwg")
            if hasattr(da, "ephemeral_output_key")
            else f"t/{store.sanitize_id(tenant_id)}/out/{ts}_{output_name}.dwg"
        )
        scratch_keys.append(out_key)
        up_key, out_url = (
            da.scratch_signed_upload_url(out_key)
            if hasattr(da, "scratch_signed_upload_url")
            else da.signed_upload_url(out_key)
        )
        activity_id = da.activity_qualified(WRITE_ACTIVITY)
        w_args = {"HostDwg": {"url": in_url, "verb": "get"},
                  "Result": {"url": out_url, "verb": "put"}}
        submit_kwargs: Dict[str, Any] = {"dry_run": False, "poll": True,
                                         "tenant_id": tenant_id}
        # Report the live WorkItem id before the poll blocks, so a tab closed
        # mid-write has something to cancel (see broker._record_active_workitem).
        if on_submitted is not None and _accepts_on_submitted(da.submit_workitem):
            submit_kwargs["on_submitted"] = on_submitted
        status = da.submit_workitem(activity_id, w_args, **submit_kwargs)
        if status.get("status") != "success":
            return (err_envelope(ErrorCode.WORKITEM_FAILED,
                                 f"write WorkItem {status.get('id')} status={status.get('status')} "
                                 f"report={status.get('reportUrl')}", retryable=True,
                                 tool=name, version=tool_version),
                    DEFAULT_HTTP_STATUS[ErrorCode.WORKITEM_FAILED])
        if hasattr(da, "finalize_scratch_upload"):
            da.finalize_scratch_upload(out_key, up_key)
            out_bytes = da.download_scratch_object(out_key)
        else:
            da.finalize_upload(out_key, up_key)
            out_bytes = da.download_object(out_key)
        if not out_bytes:
            return (err_envelope(ErrorCode.WORKITEM_FAILED, "write produced 0-byte output.dwg",
                                 retryable=True, tool=name, version=tool_version),
                    DEFAULT_HTTP_STATUS[ErrorCode.WORKITEM_FAILED])

        wi_id = status.get("id")
        new_v = _put_bytes_version(backend, tenant_id, drawing_id, out_bytes,
                                   parent_version=head_v,
                                   meta={"tool": name, "workitem_id": wi_id,
                                         "note": "live write (output.dwg)"},
                                   holder=holder, fence=fence)

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
        env = ok_envelope(name, tool_version, result,
                          overlay={"probe_layer_added": PROBE_LAYER},
                          timing_ms=int((time.perf_counter() - t0) * 1000),
                          cost=cost, degraded_mode=False)
        return env, 200
    except store.CheckoutDenied as exc:
        # BEFORE both handlers below. CheckoutDenied is deliberately not a
        # ValueError, but ordering it explicitly here keeps the 403 answer from
        # depending on that fact: publishing under another session's lock is an
        # authorization result, never a 400 bad-drawing or a 502 WorkItem fault.
        return _checkout_denied(exc, name, tool_version)
    except FileNotFoundError as exc:  # creds missing
        return (err_envelope(ErrorCode.APS_UNAVAILABLE, str(exc), retryable=False,
                             tool=name, version=tool_version),
                DEFAULT_HTTP_STATUS[ErrorCode.APS_UNAVAILABLE])
    except (KeyError, ValueError) as exc:
        # unknown drawing/BASE version (da/store.py resolve_version, or a missing
        # manifest key on a race) — a CLIENT input error, not a WorkItem failure.
        # MUST be caught here, before the broad Exception handler below, so an
        # unknown `version` surfaces as BAD_PARAMS/non-retryable (review 2026-07-22
        # finding: it was previously falling through to WORKITEM_FAILED/retryable,
        # i.e. an HTTP 502 for what is really a 400) — matches run_write_mock's own
        # (KeyError, ValueError) -> BAD_PARAMS surfacing exactly.
        return (err_envelope(ErrorCode.BAD_PARAMS, f"drawing/version unavailable: {exc}",
                             retryable=False, tool=name, version=tool_version),
                DEFAULT_HTTP_STATUS[ErrorCode.BAD_PARAMS])
    except Exception as exc:  # noqa: BLE001
        return (err_envelope(ErrorCode.WORKITEM_FAILED, f"{type(exc).__name__}: {exc}",
                             retryable=True, tool=name, version=tool_version),
                DEFAULT_HTTP_STATUS[ErrorCode.WORKITEM_FAILED])
    finally:
        # Best effort only. The dedicated transient bucket still expires a copy
        # if an immediate delete is interrupted.
        _cleanup_scratch_objects(da, scratch_keys)
