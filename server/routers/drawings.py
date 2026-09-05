"""Versioned drawing endpoints (M2 write loop) — this session's router.

    GET    /api/drawings/{drawing_id}/intake?version=head|<n>
           -> {intake, version, head, latest}   (envelope-wrapped per §10)
    POST   /api/drawings/{drawing_id}/undo   -> {version, head, latest, intake}
    POST   /api/drawings/{drawing_id}/redo   -> {version, head, latest, intake}
    GET    /api/drawings/{drawing_id}/versions -> full version chain + checkout
    POST   /api/drawings/{drawing_id}/checkout {holder?, ttl_s?}
           -> take the single-writer lock (409 not-acquired if held by another)
    DELETE /api/drawings/{drawing_id}/checkout?holder=<h>
           -> release the lock (403 if the caller is not the holder)

undo repoints head to its parent (objects are never deleted); redo re-advances
head toward `latest` along the parent chain. A successful `drawing.write` tool run
(via POST /api/run) creates the versions these endpoints serve, and stamps
`result.new_version = {drawing_id, version, parent}` into its §3 envelope.

The store backend is chosen by APS_LIVE (deps.APS_LIVE): local FilesystemBackend
offline (no credential), OSSBackend live. Serving the reads directly here (v1)
keeps the credential OUT of the app process at APS_LIVE=0 — the tested condition;
promoting the live reads through the broker for strict isolation at APS_LIVE=1 is
a documented follow-up (CONTRACT-ADDENDUM §11).
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import re
import threading
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

import checkout_capability
import deps
import guest_uploads
import jobs
import write_loop
from envelopes import ErrorCode, error_obj, error_response, with_envelope_fields

router = APIRouter()
SUMMARY_LAYER_CAP = 50

# Serializes restore commits in this process (see restore_version): the legacy
# filesystem authority accepts any parent_version, so two racing restores
# would fork the chain without it.
_RESTORE_LOCK = threading.Lock()
CACHED_DEMO_DRAWING_ID = "rooftop_demo"


@dataclass(frozen=True)
class RestoredDrawingVersion:
    """The committed restore version and the state needed by route callers."""

    version: int
    parent_version: int
    restored_head_readable: bool
    # The restored source version's authored-tool receipt digest, carried
    # forward because the new head's BYTES are that version's bytes verbatim
    # (`source_bytes` is copied, not re-planned). Attributing them to the tool
    # whose receipt produced them is a fact about the payload, not a guess
    # about the restore. `None` whenever the source carried none.
    source_ref: Optional[str] = None


class RestoreSourceUnavailable(Exception):
    """The requested immutable source version is absent."""


class RestoreSourceUnreadable(Exception):
    """The requested source cannot safely become a readable new head."""


class RestoreDrawingUnavailable(Exception):
    """The drawing disappeared or became malformed before commit."""


class RestoreCommitRejected(Exception):
    """The store rejected the restore before it committed."""


class RestoreMutationsDisabled(Exception):
    """A storage cutover has drained drawing mutations."""


class RestoreCheckoutDenied(Exception):
    """The store refused the restore under its checkout fence."""


def _mutation_gate() -> Optional[JSONResponse]:
    if write_loop.drawing_mutations_enabled():
        return None
    return error_response(
        ErrorCode.INTERNAL,
        "drawing mutations are temporarily disabled for a storage cutover",
        retryable=True,
        status_code=503,
    )


def _store_checkout_denied():
    """`store.CheckoutDenied` for an `except` clause. da/store.py is imported
    lazily everywhere in this router (it reaches the path only through
    write_loop's sys.path setup), and an except clause needs the class itself."""
    import store

    return store.CheckoutDenied


def _backend(tenant_id: str = ""):
    """Per-tenant store selection (§19): guest tenants read/write the isolated
    guest store; every other tenant keeps the exact pre-§19 default backend."""
    return write_loop.backend_for_tenant(
        str(tenant_id), aps_live=False, da=None,
    )


@router.get("/api/drawings/{drawing_id}/intake")
def get_intake(drawing_id: str, version: str = "head",
               tenant_id: str = Depends(deps.require_active_tenant)) -> Dict[str, Any]:
    ver: Any = version
    if isinstance(version, str) and version not in ("head", "latest") and version.lstrip("-").isdigit():
        ver = int(version)
    try:
        view = write_loop.intake_view(str(tenant_id), drawing_id, ver, backend=_backend(str(tenant_id)))
    except write_loop.ProofStateUnreadable as exc:
        # The version exists; its cache-proof state is unreachable right now.
        return error_response(ErrorCode.INTERNAL, str(exc), retryable=True,
                              status_code=503)
    except (KeyError, ValueError) as exc:
        return error_response(ErrorCode.BAD_PARAMS, f"drawing/version unavailable: {exc}",
                              retryable=False, status_code=404)
    return with_envelope_fields(deps.tenant_echo(view, tenant_id))


@router.get("/api/drawings/{drawing_id}/dxf")
def get_dxf(drawing_id: str, request: Request, version: str = "head",
            tenant_id: str = Depends(deps.require_active_tenant)) -> Any:
    """W4g-1 (engine reach): the version as ASCII DXF bytes, what the browser
    engine opens so the cockpit's Draw/Modify tools work on the console's own
    drawing instead of a hand-imported file.

    Legs (write_loop.read_dxf, each fail-closed): a browser-edited version's
    own sidecar when it is bound to the payload; the payload intake synthesized
    to DXF (server/intake_dxf.py, the pinned inverse of the intake parser); a
    raw DWG version converted by the caged dwg2dxf and cached digest-bound
    beside it. The answer carries `ETag` (sha256 of the bytes; `If-None-Match`
    answers 304), `X-Leaf-Version`, `X-Leaf-Head` and `X-Leaf-Dxf-Source`
    (which leg). Bounded by the engine's own 16 MB ceiling (413 past it).
    Same tenant gate, id literal and 404 pattern as the intake route."""
    import store  # da/store.py; importable via write_loop's sys.path setup

    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", drawing_id or ""):
        return error_response(ErrorCode.BAD_PARAMS, "malformed drawing id",
                              retryable=False, status_code=400)
    ver: Any = version
    if isinstance(version, str) and version not in ("head", "latest") and version.lstrip("-").isdigit():
        ver = int(version)
    backend = _backend(str(tenant_id))
    try:
        write_loop.ensure_demo_drawing(backend, str(tenant_id), drawing_id)
        v, data, source = write_loop.read_dxf(backend, str(tenant_id), drawing_id, ver)
        head = int(store.load_manifest(backend, str(tenant_id), drawing_id)["head"])
    except write_loop.DxfUnavailable as exc:
        return error_response(getattr(ErrorCode, exc.error_code, ErrorCode.INTERNAL),
                              exc.message, retryable=exc.retryable,
                              status_code=exc.status_code)
    except (KeyError, ValueError) as exc:
        return error_response(ErrorCode.BAD_PARAMS, f"drawing/version unavailable: {exc}",
                              retryable=False, status_code=404)
    etag = '"' + hashlib.sha256(data).hexdigest() + '"'
    headers = {
        "ETag": etag,
        "X-Leaf-Version": str(v),
        "X-Leaf-Head": str(head),
        "X-Leaf-Dxf-Source": source,
        # Private to the tenant's own browser; revalidate on every open (the
        # ETag makes the unchanged case one header round trip).
        "Cache-Control": "private, no-cache",
    }
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    headers["Content-Disposition"] = f'inline; filename="{drawing_id}-v{v}.dxf"'
    return Response(content=data, media_type="application/dxf", headers=headers)


@router.get("/api/drawings/{drawing_id}/summary")
def get_summary(drawing_id: str, version: str = "head",
                tenant_id: str = Depends(deps.require_active_tenant)) -> Dict[str, Any]:
    """Return a bounded drawing digest for assistant context.

    The intake endpoint remains the lossless geometry surface. This route keeps
    model-facing reads small by returning only version facts, an entity total,
    and the most populated layers.
    """
    ver: Any = version
    if isinstance(version, str) and version not in ("head", "latest") and version.lstrip("-").isdigit():
        ver = int(version)
    if deps.APS_LIVE and drawing_id == CACHED_DEMO_DRAWING_ID:
        # The public demo is the bundled, immutable intake used by the UI and
        # deterministic tools. Keep this read inside the app process so the
        # app never needs the broker-only APS credential just to summarize it.
        if ver not in ("head", "latest", 1):
            return error_response(
                ErrorCode.BAD_PARAMS,
                f"drawing/version unavailable: version {ver!r} does not exist",
                retryable=False,
                status_code=404,
            )
        view = {
            "intake": deps.load_cached_intake(),
            "version": 1,
            "head": 1,
            "latest": 1,
        }
    else:
        try:
            view = write_loop.intake_view(
                str(tenant_id), drawing_id, ver, backend=_backend(str(tenant_id))
            )
        except write_loop.ProofStateUnreadable as exc:
            return error_response(ErrorCode.INTERNAL, str(exc), retryable=True,
                                  status_code=503)
        except (KeyError, ValueError) as exc:
            return error_response(
                ErrorCode.BAD_PARAMS,
                f"drawing/version unavailable: {exc}",
                retryable=False,
                status_code=404,
            )

    layers: Dict[str, int] = {}
    total = 0
    intake = view.get("intake") or {}
    for key in ("polylines", "inserts", "faces3d"):
        for entity in intake.get(key) or []:
            layer = str(entity.get("layer", "0"))
            layers[layer] = layers.get(layer, 0) + 1
            total += 1
    ranked = sorted(layers.items(), key=lambda item: (-item[1], item[0]))
    layer_rows: list[Dict[str, Any]] = [
        {"name": name, "count": count}
        for name, count in ranked[:SUMMARY_LAYER_CAP]
    ]
    if len(ranked) > SUMMARY_LAYER_CAP:
        layer_rows.append({"more": len(ranked) - SUMMARY_LAYER_CAP})

    summary = {
        "drawing_id": drawing_id,
        "version": view.get("version"),
        "head": view.get("head"),
        "latest": view.get("latest"),
        "entity_total": total,
        "layers": layer_rows,
    }
    return with_envelope_fields(deps.tenant_echo(summary, tenant_id))


@router.post("/api/drawings/{drawing_id}/undo")
def undo(drawing_id: str, tenant_id: str = Depends(deps.require_active_tenant),
         x_checkout_capability: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    """Step head back one version.

    SINGLE-WRITER (added with the checkout capability): undo MUTATES the drawing
    every other session reads, so it is gated exactly like publishing a version.
    It was not, and that made it the way around the write check — a caller refused
    at `put_drawing` could still walk the same drawing's head backwards under
    someone else's lease. With no checkout held (the demo's ordinary state) this
    is unchanged; with one held, `X-Checkout-Capability` from the acquire response
    is required.
    """
    blocked = _mutation_gate()
    if blocked is not None:
        return blocked
    backend = _backend(str(tenant_id))
    try:
        write_loop.ensure_demo_drawing(backend, str(tenant_id), drawing_id)
        holder, fence = _lock_authorization(drawing_id, tenant_id, backend,
                                            x_checkout_capability)
    except (checkout_capability.CapabilityRejected,
            checkout_capability.CapabilityUnavailable) as exc:
        return _denied(exc)
    except (KeyError, ValueError) as exc:
        # 400, not the GET routes' 404: undo/redo have always answered 400 for a
        # malformed or unresolvable drawing id (tests/test_drawings_bootstrap.py),
        # and moving the bootstrap ahead of the lock check must not change that.
        return error_response(ErrorCode.BAD_PARAMS, str(exc), retryable=False,
                              status_code=400)
    try:
        view = write_loop.undo_view(str(tenant_id), drawing_id, backend=backend,
                                    holder=holder, fence=fence)
    except _store_checkout_denied() as exc:
        # The lock changed between the gate above and the store's own check under
        # its row lock. The store is authoritative; report ITS answer.
        return _denied(checkout_capability.CapabilityRejected(str(exc)))
    except write_loop.ProofStateUnreadable as exc:
        # The head repoint COMMITTED; only the response-building intake read
        # failed on transport. NOT retryable: retrying would undo a further
        # version. The client should refresh instead.
        return error_response(
            ErrorCode.INTERNAL,
            f"the undo committed, but the head's intake is temporarily unreadable ({exc}); "
            "refresh instead of retrying",
            retryable=False, status_code=503)
    except (KeyError, ValueError) as exc:
        return error_response(ErrorCode.BAD_PARAMS, str(exc), retryable=False, status_code=400)
    return with_envelope_fields(deps.tenant_echo(view, tenant_id))


@router.post("/api/drawings/{drawing_id}/redo")
def redo(drawing_id: str, tenant_id: str = Depends(deps.require_active_tenant),
         x_checkout_capability: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    """Step head forward one version. Same single-writer gate as `undo` — the two
    are one surface, and a check on only one of them is no check at all."""
    blocked = _mutation_gate()
    if blocked is not None:
        return blocked
    backend = _backend(str(tenant_id))
    try:
        write_loop.ensure_demo_drawing(backend, str(tenant_id), drawing_id)
        holder, fence = _lock_authorization(drawing_id, tenant_id, backend,
                                            x_checkout_capability)
    except (checkout_capability.CapabilityRejected,
            checkout_capability.CapabilityUnavailable) as exc:
        return _denied(exc)
    except (KeyError, ValueError) as exc:
        # 400 for the same reason `undo` gives it — see the note there.
        return error_response(ErrorCode.BAD_PARAMS, str(exc), retryable=False,
                              status_code=400)
    try:
        view = write_loop.redo_view(str(tenant_id), drawing_id, backend=backend,
                                    holder=holder, fence=fence)
    except _store_checkout_denied() as exc:
        return _denied(checkout_capability.CapabilityRejected(str(exc)))
    except write_loop.ProofStateUnreadable as exc:
        # Same committed-but-unreadable shape as undo: never retryable.
        return error_response(
            ErrorCode.INTERNAL,
            f"the redo committed, but the head's intake is temporarily unreadable ({exc}); "
            "refresh instead of retrying",
            retryable=False, status_code=503)
    except (KeyError, ValueError) as exc:
        return error_response(ErrorCode.BAD_PARAMS, str(exc), retryable=False, status_code=400)
    return with_envelope_fields(deps.tenant_echo(view, tenant_id))


def _checkout_view(co: Any) -> Any:
    """Manifest `checkout` lock → the surface shape `{holder, acquired, expires}`
    or null. GET /versions renders this read-only ("someone else is editing" chip);
    the POST/DELETE .../checkout routes below let the holder TAKE and RELEASE it.

    AN ELAPSED LEASE IS NULL, and the judgment comes from `store.checkout_active`
    rather than from a timestamp compared here. The question this field answers is
    "who is editing RIGHT NOW", and the store's answer for a lapsed lease is
    "nobody": `acquire_checkout_fence` re-grants an expired lock to anyone,
    `release_checkout` lets anyone clear one, and `_lock_authorization` below
    admits a write over one unauthenticated. Neither authority erases the lapsed
    record eagerly (the postgres `load_manifest` rebuilds `checkout` from
    `checkout_holder` whatever `checkout_expires_at` says; the legacy manifest just
    keeps the dict), so returning it verbatim made this read the ONE surface in the
    system that called a lock held while every other path called it free.

    Measured on platform-staging 2026-09-02: a lease that ended 01:56:42Z was still
    published at 15:46Z, so the rail read "Editing locked by sess-72d58f4d…" for 827
    minutes and suppressed write tools that whole time, on a drawing nobody held.

    The client cannot decide this for itself and must not try. The browser clock is
    not an authority on expiry (web/src/checkoutIdentity.js `looksStale`: a clock
    two hours slow wedged the UI, one two hours fast falsely enabled writes), so it
    fails closed on ANY reported holder. Withdrawing the dead record is the server
    saying the one thing only the server can say, and it needs no client change —
    an already-loaded tab stops seeing the phantom on its next refresh.

    A holderless-but-present record (`{}`, which is truthy over the wire) is null
    here for the same reason: `checkout_active` rejects it, so the client no longer
    has to re-reject an empty object one reader at a time.

    `holder` is a DISPLAY LABEL and nothing more. It is public on this read by
    design — the UI's "locked by X" chip needs a name — so it can never be the
    thing that proves ownership; the opaque capability minted at acquire is
    (server/checkout_capability.py).

    The lock GENERATION is deliberately NOT surfaced. It used to be, and that was
    half the replay: a second session read `holder` and `fence` here and presented
    both as its own. It authorizes nothing now, but a client has no use for it
    either (it holds a capability instead), and an ownership generation is not
    something to publish to everyone who can read the drawing.
    """
    import store  # da/store.py; importable via write_loop's sys.path setup

    # `checkout_active` is the store's OWN rule, called rather than copied, so
    # this read and the write paths cannot drift on what "held" means.
    if not store.checkout_active(co):
        return None
    return {"holder": co.get("holder"), "acquired": co.get("acquired"),
            "expires": co.get("expires")}


def _lock_authorization(drawing_id: str, tenant_id: Any, backend: Any,
                        capability: Optional[str]) -> Tuple[Optional[str], Optional[int]]:
    """The ONE ownership gate every mutating drawing route goes through.

    Returns the `(holder, fence)` the store primitives take: the pair read from
    the manifest once the capability proved the caller owns the active lease, or
    `(None, None)` when no lease is active and there is nothing to prove.

    The permitted cases are exactly `da/store.py`'s, so publish, undo and redo
    answer alike and the demo keeps working with no checkout at all:

      * no lock, or an EXPIRED lock -> allowed, unauthenticated by this gate
        (the store re-grants an expired lock to anyone, so treating it as a
        barrier here would wedge a drawing behind a forgotten lease);
      * an ACTIVE lock -> a valid capability for THAT generation, or nothing.

    Raises `checkout_capability.CapabilityRejected` (-> 403) and
    `CapabilityUnavailable` (-> 503); callers render both via `_denied`.
    """
    import store  # da/store.py; importable via write_loop's sys.path setup

    m = store.load_manifest(backend, str(tenant_id), drawing_id)
    co = m.get("checkout")
    if not store.checkout_active(co):
        return None, None
    return checkout_capability.verify(capability, tenant_id, drawing_id, co)


def _denied(exc: Exception) -> JSONResponse:
    """Capability failure -> the §10 envelope. 403 for a caller who cannot prove
    ownership (the answer DELETE .../checkout has always given a non-holder); 503
    for a server that cannot verify anything at all, which is an operator fault
    and must not be reported as an authorization one."""
    if isinstance(exc, checkout_capability.CapabilityUnavailable):
        return error_response(ErrorCode.INTERNAL, str(exc), retryable=True,
                              status_code=503)
    return error_response(ErrorCode.BAD_PARAMS, str(exc), retryable=False,
                          status_code=403)


# The authored-tool provenance a version row may carry. It is the sha256 the
# SERVER measured over the published tool body it holds for the tool id
# (server/tool_loader.py published_tool_source_sha256, stamped in
# server/write_loop.py), the same bytes a genuine `leaf.tool-source.v1`
# receipt hashes; nothing a sandbox returned is ever stored. NOTHING else is
# minted here: a version written by a tool whose body the server does not hold
# carries null, and null means "not established", never "unauthored".
_SOURCE_REF_MAX_LEN = 128          # bound before any regex touches the value
_SOURCE_REF_RE = re.compile(r"\A[0-9a-f]{64}\Z")


def _source_ref(value: Any) -> Optional[str]:
    """Validate a stored `source_ref` on the way OUT. Fails closed to None.

    The manifest is store-owned data, and the PostgreSQL authority's column is
    free text, so a drifted, truncated or hostile value must never reach the
    wire dressed as provenance. Bounded first (a pathological megabyte string
    is rejected on length, never scanned), then charset-validated against the
    exact shape a `leaf.tool-source.v1` receipt digest has: 64 lowercase hex
    characters. Anything else reads as "no provenance", which is the honest
    answer for a value we cannot vouch for. No allocation on the hot path
    beyond the one `str` check; called once per version row."""
    if not isinstance(value, str):
        return None
    if len(value) > _SOURCE_REF_MAX_LEN:
        return None
    return value if _SOURCE_REF_RE.match(value) else None


def _version_row(e: Dict[str, Any]) -> Dict[str, Any]:
    """One manifest `versions[]` entry → the version-history row shape
    (CONTRACT-ADDENDUM §11 manifest fields; missing optional fields → null).

    `source_ref` (added standardization slice 6a) is a NULLABLE addition to a
    frozen shape: every existing key keeps its meaning, and a client that does
    not know the key is unaffected."""
    parent = e.get("parent")
    return {
        "v": int(e["v"]),
        "parent": (int(parent) if parent is not None else None),
        "created": e.get("created"),
        "bytes": e.get("bytes"),
        "sha256": e.get("sha256"),
        "tool": e.get("tool"),
        "workitem_id": e.get("workitem_id"),
        "note": e.get("note"),
        "source_ref": _source_ref(e.get("source_ref")),
    }


def _entity_identity(kind: str, entity: Dict[str, Any]) -> Any:
    """A stable-enough key for one intake entity across two version payloads.

    Every entity in the cached demo schema (polylines/inserts/faces3d) carries
    a `handle`, and that is the real identity — the same handle survives a
    panel-transform/v1 move or an added-layer edit. An entity with no handle
    (schema drift, or a future entity kind) falls back to a content hash, which
    still recognizes an UNCHANGED entity across versions but cannot tell a
    modified handle-less entity from a delete+add — there is no id to hang
    "modified" on, so it reads as one of each. That is the honest limit of a
    read-time diff over opaque JSON, not a bug.

    Two more documented limits of the identity model: DUPLICATE handles within
    one kind collapse to one map entry (last wins), so a parent with two "A"
    polylines and a child with one reads as unchanged rather than one delete —
    the store never mints duplicate handles, so this only occurs on corrupt
    payloads; and an entity MOVING between kinds (same handle, polylines →
    inserts) counts as one add plus one delete, because kind is part of the
    identity key."""
    handle = entity.get("handle") if isinstance(entity, dict) else None
    if isinstance(handle, str) and handle:
        return (kind, "handle", handle)
    canon = json.dumps(entity, sort_keys=True, default=str)
    return (kind, "hash", hashlib.sha256(canon.encode("utf-8")).hexdigest())


def _entity_map(intake: Dict[str, Any]) -> Dict[Any, Dict[str, Any]]:
    out: Dict[Any, Dict[str, Any]] = {}
    for kind in ("polylines", "inserts", "faces3d"):
        for entity in (intake.get(kind) or []):
            if isinstance(entity, dict):
                out[_entity_identity(kind, entity)] = entity
    return out


def _version_delta(parent_intake: Dict[str, Any], child_intake: Dict[str, Any]) -> Dict[str, int]:
    """`{added, modified, deleted}` entity counts between two STORED intake
    payloads (parent vs child), computed fresh on every read — never stamped at
    write time (CONTRACT-ADDENDUM: the write path is owned elsewhere and is not
    touched by this route)."""
    parent_map = _entity_map(parent_intake or {})
    child_map = _entity_map(child_intake or {})
    added = modified = deleted = 0
    for ident, entity in child_map.items():
        prior = parent_map.get(ident)
        if prior is None:
            added += 1
        elif prior != entity:
            modified += 1
    for ident in parent_map:
        if ident not in child_map:
            deleted += 1
    return {"added": added, "modified": modified, "deleted": deleted}


def _version_deltas(backend: Any, tenant_id: str, drawing_id: str,
                    entries: List[Dict[str, Any]]) -> Dict[int, Optional[Dict[str, int]]]:
    """Per-version delta, keyed by `v`. `None` where a delta cannot be derived:
    the root version (no parent to diff against) or a payload that fails to
    parse (`write_loop.read_intake` raises `KeyError`/`ValueError` — a missing
    blob or a non-JSON one). Ships only what IS derivable from the stored
    payloads rather than guessing at either end."""
    cache: Dict[int, Optional[Dict[str, Any]]] = {}

    def _load(v: int) -> Optional[Dict[str, Any]]:
        if v not in cache:
            try:
                _, intake = write_loop.read_intake(backend, tenant_id, drawing_id, v)
                intake = intake if isinstance(intake, dict) else None
            except (KeyError, ValueError, TypeError,
                    write_loop.ProofStateUnreadable):
                # Unreadable proof state degrades this ROW's delta to null;
                # a transient blip must not 500 the whole chain listing.
                intake = None
            cache[v] = intake
        return cache[v]

    out: Dict[int, Optional[Dict[str, int]]] = {}
    for e in entries:
        v = int(e["v"])
        parent = e.get("parent")
        if parent is None:
            out[v] = None
            continue
        parent_intake = _load(int(parent))
        child_intake = _load(v)
        if parent_intake is None or child_intake is None:
            out[v] = None
            continue
        try:
            out[v] = _version_delta(parent_intake, child_intake)
        except (KeyError, ValueError, TypeError):
            # Expected malformed-data failures only: a programming defect must
            # surface as a 500, not vanish into a delta:null row.
            out[v] = None
    return out


@router.get("/api/drawings/{drawing_id}/versions")
def get_versions(drawing_id: str, include_deltas: bool = False,
                 tenant_id: str = Depends(deps.require_active_tenant)) -> Dict[str, Any]:
    """Full version-history chain straight from the store manifest — the read the
    UI's version-chain popover needs (undo/redo only stepped head one hop).

    §10-enveloped `{drawing_id, head, latest, versions:[{v, parent, created, bytes,
    sha256, tool, workitem_id, note, delta}], checkout: {holder, acquired,
    expires}|null}` (§14: `checkout` is the single-writer lock from the
    manifest, read-only — no acquire/release this wave). `delta` is
    `{added, modified, deleted}` computed HERE, at read time, by diffing this
    version's stored payload against its parent's (see `_version_deltas`); it
    is `null` where that is not derivable (the root version, or an unparseable
    payload on either side). Same 404 pattern as the intake route for
    an unknown drawing (the well-known `demo` bootstraps on first read at
    APS_LIVE=0; any other unknown drawing → BAD_PARAMS 404)."""
    import store  # da/store.py; importable via write_loop's sys.path setup (imported above)

    backend = _backend(str(tenant_id))
    try:
        write_loop.ensure_demo_drawing(backend, str(tenant_id), drawing_id)
    except (KeyError, ValueError) as exc:
        return error_response(ErrorCode.BAD_PARAMS, f"drawing unavailable: {exc}",
                              retryable=False, status_code=404)
    try:
        m = store.load_manifest(backend, str(tenant_id), drawing_id)
    except KeyError as exc:
        # Missing is a caller-visible 404. A malformed manifest raises a
        # ValueError subclass and must remain a server fault instead of being
        # mislabeled as an unavailable drawing.
        return error_response(ErrorCode.BAD_PARAMS, f"drawing unavailable: {exc}",
                              retryable=False, status_code=404)
    entries = m.get("versions", [])
    # Deltas are OPT-IN (?include_deltas=1): computing them loads and parses
    # EVERY version payload (O(N) blob reads, O(N^2) row work under the
    # PostgreSQL authority), and the app hits this route at startup merely to
    # read checkout state. Only the version list asks for deltas (the ONE
    # `VersionList` primitive behind /app's history drawer and /try's Versions
    # tab, slice 6a), and the default response shape stays exactly what it
    # was before this feature.
    rows = []
    if include_deltas:
        deltas = _version_deltas(backend, str(tenant_id), drawing_id, entries)
        for e in entries:
            row = _version_row(e)
            row["delta"] = deltas.get(row["v"])
            rows.append(row)
    else:
        rows = [_version_row(e) for e in entries]
    view = {
        "drawing_id": drawing_id,
        "head": int(m["head"]),
        "latest": int(m["latest"]),
        "versions": rows,
        "checkout": _checkout_view(m.get("checkout")),  # single-writer lock (read-only)
    }
    return with_envelope_fields(deps.tenant_echo(view, tenant_id))


def restore_drawing_version(tenant_id: str, drawing_id: str, target_version: int,
                            *, actor: str, holder: Optional[str] = None,
                            fence: Optional[int] = None,
                            backend: Any = None) -> RestoredDrawingVersion:
    """Copy one readable immutable version forward through the safe write path.

    Both drawing-version and checkpoint restores use this service so every
    restore validates intake, holds the cutover guard, uses the store's
    parent-head compare-and-set, enforces the checkout fence, and publishes the
    new version's intake cache.
    """
    import store

    backend = backend if backend is not None else _backend(tenant_id)
    try:
        source_v, source_key, source_entry = store.resolve_version_entry(
            backend, tenant_id, drawing_id, int(target_version))
        source_bytes = backend.get(source_key)
    except (KeyError, ValueError) as exc:
        raise RestoreSourceUnavailable(str(exc)) from exc

    # The source version's provenance comes out of the SAME manifest read that
    # just proved the version exists (`resolve_version_entry` threads the row
    # out), validated on the way through like every other reader of the
    # column. A row without one restores as null: the restoring actor is not
    # an author.
    source_ref: Optional[str] = _source_ref(source_entry.get("source_ref"))

    try:
        _, source_intake = write_loop.read_intake(
            backend, tenant_id, drawing_id, source_v)
        if not isinstance(source_intake, dict):
            raise ValueError("intake payload is not an object")
    except write_loop.ProofStateUnreadable:
        raise
    except (KeyError, ValueError) as exc:
        raise RestoreSourceUnreadable(str(exc)) from exc

    try:
        blob_is_intake = isinstance(json.loads(source_bytes.decode("utf-8")), dict)
    except (UnicodeDecodeError, ValueError):
        blob_is_intake = False

    with _RESTORE_LOCK:
        try:
            with write_loop.drawing_mutation_commit_guard() as commit_enabled:
                if not commit_enabled:
                    raise RestoreMutationsDisabled
                try:
                    manifest = store.load_manifest(backend, tenant_id, drawing_id)
                except (KeyError, ValueError) as exc:
                    raise RestoreDrawingUnavailable(str(exc)) from exc
                parent_version = int(manifest["head"])
                put_holder = holder if holder is not None else store.ANONYMOUS_HOLDER
                try:
                    new_version = write_loop._put_bytes_version(
                        backend, tenant_id, drawing_id, source_bytes,
                        parent_version=parent_version,
                        meta={"tool": actor,
                              "note": f"{actor} of version {target_version}",
                              "source_ref": source_ref},
                        holder=put_holder, fence=fence,
                        require_parent_is_head=True,
                    )
                except ValueError as exc:
                    raise RestoreCommitRejected(str(exc)) from exc
                restored_head_readable = True
                try:
                    write_loop.publish_intake_cache(
                        backend, tenant_id, drawing_id, new_version,
                        source_bytes, source_intake)
                except Exception:  # noqa: BLE001 - the immutable head committed
                    restored_head_readable = blob_is_intake
        except store.CheckoutDenied as exc:
            raise RestoreCheckoutDenied(str(exc)) from exc

    return RestoredDrawingVersion(
        version=int(new_version),
        parent_version=parent_version,
        restored_head_readable=restored_head_readable,
        source_ref=source_ref,
    )


@router.post("/api/drawings/{drawing_id}/versions/{version}/restore")
def restore_version(drawing_id: str, version: int,
                    tenant_id: str = Depends(deps.require_active_tenant),
                    x_checkout_capability: Optional[str] = Header(default=None)) -> Any:
    """Restore-as-new-head: append a NEW version whose stored payload equals
    `version`'s, with `parent` = the CURRENT head. History is never rewritten —
    restoring is an ordinary write (new v, chain intact), not a rollback of the
    manifest. Composes existing primitives only (`write_loop.read_intake`,
    `write_loop._put_bytes_version` → `store.put_drawing`); neither is modified
    here.

    Same single-writer gate as undo/redo/publish (`_lock_authorization`): an
    ACTIVE checkout needs a valid `X-Checkout-Capability` for the current
    lease, else 403; a server that cannot verify at all answers 503. A
    malformed/unresolvable DRAWING id answers 400, matching undo/redo (see the
    note on those routes). Restoring a version absent from THIS drawing's
    chain answers 404 — the same shape GET .../intake already gives for an
    unknown version, since the drawing itself is fine and only the version
    argument is bad."""
    blocked = _mutation_gate()
    if blocked is not None:
        return blocked
    import store  # da/store.py; importable via write_loop's sys.path setup

    backend = _backend(str(tenant_id))
    try:
        write_loop.ensure_demo_drawing(backend, str(tenant_id), drawing_id)
        holder, fence = _lock_authorization(drawing_id, tenant_id, backend,
                                            x_checkout_capability)
    except (checkout_capability.CapabilityRejected,
            checkout_capability.CapabilityUnavailable) as exc:
        return _denied(exc)
    except (KeyError, ValueError) as exc:
        return error_response(ErrorCode.BAD_PARAMS, str(exc), retryable=False,
                              status_code=400)

    try:
        restored = restore_drawing_version(
            str(tenant_id), drawing_id, int(version), actor="restore",
            holder=holder, fence=fence, backend=backend)
    except RestoreSourceUnavailable as exc:
        return error_response(ErrorCode.BAD_PARAMS,
                              f"version {version} unavailable: {exc}",
                              retryable=False, status_code=404)
    except write_loop.ProofStateUnreadable as exc:
        return error_response(ErrorCode.INTERNAL, str(exc), retryable=True,
                              status_code=503)
    except RestoreSourceUnreadable as exc:
        return error_response(
            ErrorCode.BAD_PARAMS,
            f"version {version} is not readable as intake ({exc}); "
            "restoring it would produce an unusable head",
            retryable=False, status_code=422)
    except RestoreMutationsDisabled:
        return error_response(
            ErrorCode.INTERNAL,
            "drawing mutations are temporarily disabled for a storage cutover",
            retryable=True, status_code=503,
        )
    except RestoreDrawingUnavailable as exc:
        if isinstance(exc.__cause__, KeyError):
            return error_response(ErrorCode.BAD_PARAMS, f"drawing unavailable: {exc}",
                                  retryable=False, status_code=404)
        return error_response(ErrorCode.BAD_PARAMS, str(exc), retryable=False,
                              status_code=400)
    except RestoreCommitRejected as exc:
        return error_response(ErrorCode.BAD_PARAMS, f"restore could not commit: {exc}",
                              retryable=True, status_code=409)
    except RestoreCheckoutDenied as exc:
        return _denied(checkout_capability.CapabilityRejected(str(exc)))

    view = deps.tenant_echo({
        "drawing_id": drawing_id,
        "restored_from": int(version),
        "new_version": {"drawing_id": drawing_id, "version": restored.version,
                        "parent": restored.parent_version,
                        # Slice 6a: the row the client is about to render
                        # already knows its provenance, so the drawer need not
                        # re-read the chain to draw the chip.
                        "source_ref": restored.source_ref},
        "head": restored.version,
        "latest": restored.version,
        # False only in the narrow committed-but-mirror-failed live case: the
        # head exists and is immutable, but its intake cache could not be
        # written, so viewers cannot read it until the cache is repaired.
        "restored_head_readable": restored.restored_head_readable,
    }, tenant_id)
    return with_envelope_fields(view)


def _receive_edited_dxf(file: UploadFile, source_digest: str):
    """The integrity half every browser-edit save shares (F-3 and the W4g-3
    plan route): read the body under the upload cap, refuse a non-.dxf name,
    recompute the sha256 and refuse a digest mismatch, parse through the real
    intake path. Returns (bytes, digest, intake) or the typed refusal."""
    import dxf_intake

    cap = guest_uploads.max_upload_bytes()
    try:
        data = file.file.read(cap + 1)
    except Exception:  # noqa: BLE001 — transport read faults answer typed, never a plain 500
        return error_response(ErrorCode.BAD_PARAMS,
                              "could not read the uploaded body",
                              retryable=True, status_code=400)
    if len(data) > cap:
        return error_response(ErrorCode.BAD_PARAMS,
                              f"file exceeds the {cap} byte cap",
                              retryable=False, status_code=413)
    if not str(file.filename or "").lower().endswith(".dxf"):
        return error_response(ErrorCode.BAD_PARAMS,
                              "edited saves accept .dxf only",
                              retryable=False, status_code=400)
    actual_digest = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(actual_digest, str(source_digest or "").lower()):
        return error_response(
            ErrorCode.BAD_PARAMS,
            "source_digest does not match the received bytes; refusing to "
            "version a payload that differs from what was edited",
            retryable=False, status_code=400)
    try:
        intake = dxf_intake.parse_dxf_bytes(data, source_name=file.filename or "edited.dxf")
    except dxf_intake.DxfParseError as exc:
        return error_response(ErrorCode.BAD_PARAMS,
                              f"edited document does not parse: {exc}",
                              retryable=False, status_code=422)
    return data, actual_digest, intake


@router.post("/api/drawings/{drawing_id}/versions/edited")
def save_edited_version(drawing_id: str,
                        file: UploadFile = File(...),
                        parent_version: int = Form(...),
                        source_digest: str = Form(...),
                        tenant_id: str = Depends(deps.require_active_tenant),
                        x_checkout_capability: Optional[str] = Header(default=None)) -> Any:
    """Card F-3 (persistence leg): save a browser-edited DXF as a NEW VERSION
    of this drawing — the editing surface's write-back into the same
    versioned-control chain every other write uses.

    Contract, hardened end to end:
      - the client sends the sha256 it computed over the exact bytes it
        edited; the server recomputes and refuses a mismatch (400) — a
        corrupted or tampered body can never become a version;
      - the bytes must PARSE through the real intake path (422 otherwise):
        the stored version payload IS that intake JSON (the chain's idiom,
        viewer-readable with no cache machinery), while the raw full-fidelity
        DXF is stored digest-bound at the version's `.edited.dxf` sidecar;
      - parent_version is compare-and-set against the CURRENT head
        (require_parent_is_head): a save over a moved head answers 409 and
        writes nothing — refresh, re-open, re-edit;
      - same single-writer gate as undo/redo/restore (_lock_authorization),
        same drain fence (_mutation_gate + the store commit guard inside
        _put_bytes_version), same 403/503 taxonomy;
      - the receipt names both digests and a truthful cost: the edit ran in
        the tenant's own browser, so engine cost is exactly 0.
    """
    blocked = _mutation_gate()
    if blocked is not None:
        return blocked
    import store  # da/store.py; importable via write_loop's sys.path setup
    import dxf_intake

    # Taint barrier at the boundary: the drawing id is a URL path parameter
    # and flows into store keys, so it is validated HERE before any store
    # call. The regex is a LITERAL, deliberately: static analysis can prove
    # a literal admits no separator or traversal characters, while the same
    # pattern behind a variable earns no barrier credit (measured on this
    # PR's CodeQL run). test_save_edited_version.py pins this literal equal
    # to the ONE shared canonical-id rule so the two can never drift.
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", drawing_id or ""):
        return error_response(ErrorCode.BAD_PARAMS,
                              "malformed drawing id",
                              retryable=False, status_code=400)
    drawing_id = str(re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", drawing_id).group(0))

    received = _receive_edited_dxf(file, source_digest)
    if isinstance(received, JSONResponse):
        return received
    data, actual_digest, intake = received

    backend = _backend(str(tenant_id))
    try:
        write_loop.ensure_demo_drawing(backend, str(tenant_id), drawing_id)
        holder, fence = _lock_authorization(drawing_id, tenant_id, backend,
                                            x_checkout_capability)
    except (checkout_capability.CapabilityRejected,
            checkout_capability.CapabilityUnavailable) as exc:
        return _denied(exc)
    except (KeyError, ValueError):
        # Fixed message, deliberately: exception text from the store layer is
        # an internal detail (CodeQL stack-trace-exposure class), and the
        # caller needs only the category.
        return error_response(ErrorCode.BAD_PARAMS,
                              "drawing unavailable or malformed request",
                              retryable=False, status_code=400)

    intake_payload = json.dumps(intake, separators=(",", ":")).encode("utf-8")
    intake_digest = hashlib.sha256(intake_payload).hexdigest()
    try:
        with write_loop.drawing_mutation_commit_guard() as commit_enabled:
            if not commit_enabled:
                return error_response(
                    ErrorCode.INTERNAL,
                    "drawing mutations are temporarily disabled for a storage cutover",
                    retryable=True, status_code=503)
            new_v = write_loop._put_bytes_version(
                backend, str(tenant_id), drawing_id, intake_payload,
                parent_version=int(parent_version),
                meta={"tool": "cad-edit-surface", "source": "cad_edit",
                      "source_sha256": actual_digest,
                      "intake_sha256": intake_digest,
                      "note": "browser edit via the isolated engine worker"},
                holder=holder, fence=fence,
                require_parent_is_head=True)
    except store.CheckoutDenied as exc:
        return _denied(checkout_capability.CapabilityRejected(str(exc)))
    except ValueError as exc:
        # The store's compare-and-set refusal is a plain ValueError. BOTH
        # authorities' shapes are matched (review 20260831-185316 finding 1):
        # the legacy manifest raises "stale parent ..." (da/store.py
        # put_drawing) and the postgres authority raises "stale drawing
        # head: expected ..." (_pg_put) — under LEAF_DRAWING_STORE=postgres
        # a moved head must still answer the documented 409, not a 400.
        if str(exc).startswith(("stale parent", "stale drawing head")):
            # Fixed text: the store's message carries version numbers, which
            # are fine, but a fixed sentence keeps exception internals out of
            # the wire entirely (stack-trace-exposure class).
            return error_response(
                ErrorCode.BAD_PARAMS,
                "stale parent: the named parent_version is no longer the "
                "head; refresh the drawing and re-apply the edit",
                retryable=True, status_code=409)
        return error_response(ErrorCode.BAD_PARAMS,
                              "version write rejected by the store",
                              retryable=False, status_code=400)
    except Exception as exc:  # noqa: BLE001 — persist faults answer typed, never a raw 500
        return error_response(ErrorCode.INTERNAL,
                              f"version persist failed: {type(exc).__name__}",
                              retryable=False, status_code=500)

    # Raw full-fidelity sidecar, AFTER the version committed. A sidecar write
    # failure degrades honestly: the version exists and is viewer-readable
    # (its payload is the intake), only re-edit fidelity is reduced, and the
    # response says so rather than pretending.
    source_stored = True
    try:
        backend.put(write_loop.edited_source_key(str(tenant_id), drawing_id, new_v), data)
    except Exception:  # noqa: BLE001
        source_stored = False

    view = deps.tenant_echo({
        "drawing_id": drawing_id,
        "new_version": {"drawing_id": drawing_id, "version": new_v,
                        "parent": int(parent_version)},
        "head": new_v,
        "source_sha256": actual_digest,
        "intake_sha256": intake_digest,
        "source_stored": source_stored,
        # Truthful cost receipt: the engine ran in the tenant's browser.
        "cost": {"engine_usd": 0.0, "engine": "client-wasm"},
    }, tenant_id)
    return JSONResponse(status_code=201, content=with_envelope_fields(view))


# W4g-3b: the plan form field is JSON, bounded here before json.loads sees a
# byte. 512 KB is thousands of operations (a browser save is the DIFF of the
# engine document against the head, not the document) and sits under the
# multipart parser's own 1024 KB per-part cap, so this cap is the one that
# answers, with the typed 413, rather than the parser's bare 400.
MAX_PLAN_FORM_BYTES = 512 * 1024


# Default OFF. The operator flips this in the same staging window that
# provisions the mutation Activity. The broker's readiness gate still refuses
# a plan run against an unready Activity regardless of this flag.
def plan_live_leg_enabled() -> bool:
    return os.environ.get("LEAF_PLAN_LIVE_LEG", "").strip() == "1"


@router.post("/api/drawings/{drawing_id}/versions/plan")
def save_plan_version(drawing_id: str,
                      file: UploadFile = File(...),
                      parent_version: int = Form(...),
                      source_digest: str = Form(...),
                      plan: str = Form(...),
                      tenant_id: str = Depends(deps.require_active_tenant),
                      x_checkout_capability: Optional[str] = Header(default=None)) -> Any:
    """W4g-3b (one head): save a browser edit as a NEW VERSION through the
    commit leg the SERVER picks (operator decision 1, 2026-09-04); the client
    never chooses. The body carries BOTH forms of the edit: the engine's exact
    DXF bytes (digest-bound, the F-3 contract) and the `plan`, the diff of the
    engine document against the head in the frozen mutation contract v2.

      - a DWG-backed head at APS_LIVE=0 takes `commit: "dwg-plan"`: the plan
        is validated against the head's intake, lowered (the same refusals the
        live WorkItem would raise), applied by the mock writer, and the
        applied intake becomes the version payload (run_write_mock's idiom);
      - a DWG-backed head at APS_LIVE=1 with LEAF_PLAN_LIVE_LEG=1 takes the
        live plan leg: validate and lower, bind the DXF to the result, check
        the checkout lease, then submit a job under the caller's checkout
        identity and answer 202; the job carries the receipt;
      - the live leg defaults OFF and takes the DXF sidecar while gated off;
        an over-cap job payload also takes the sidecar before submission,
        and both cases say why in `commit_note`;
      - a head with no DWG source (an intake-backed mock drawing, an earlier
        sidecar version) takes `commit: "dxf-sidecar"`: the F-3 leg verbatim;
      - a plan that names no operation takes the sidecar too (nothing the
        contract could express changed).

    Everything else is the F-3 contract: the integrity half, the single-writer
    gate, the drain fence, compare-and-set on the parent (checked here before
    any work and again by the store under the commit guard), 403/503/409, and
    a truthful cost."""
    blocked = _mutation_gate()
    if blocked is not None:
        return blocked
    import store  # da/store.py; importable via write_loop's sys.path setup
    import mutation_plan

    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", drawing_id or ""):
        return error_response(ErrorCode.BAD_PARAMS,
                              "malformed drawing id",
                              retryable=False, status_code=400)
    drawing_id = str(re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", drawing_id).group(0))

    if len(plan) > MAX_PLAN_FORM_BYTES:
        return error_response(ErrorCode.BAD_PARAMS,
                              f"plan exceeds the {MAX_PLAN_FORM_BYTES} byte cap",
                              retryable=False, status_code=413)
    try:
        plan_obj = json.loads(plan)
    except ValueError:
        return error_response(ErrorCode.BAD_PARAMS, "plan is not JSON",
                              retryable=False, status_code=400)
    if not isinstance(plan_obj, dict) or not isinstance(plan_obj.get("mutations"), dict):
        return error_response(ErrorCode.BAD_PARAMS,
                              "plan must be an object with a mutations object",
                              retryable=False, status_code=400)
    mutations = plan_obj["mutations"]
    names_an_op = any(
        isinstance(mutations.get(field), list) and mutations.get(field)
        for field in ("added", "removed", "transforms", "set_layer", "set_points",
                      "set_circle", "set_arc")
    )

    received = _receive_edited_dxf(file, source_digest)
    if isinstance(received, JSONResponse):
        return received
    data, actual_digest, intake = received

    backend = _backend(str(tenant_id))
    try:
        write_loop.ensure_demo_drawing(backend, str(tenant_id), drawing_id)
        holder, fence = _lock_authorization(drawing_id, tenant_id, backend,
                                            x_checkout_capability)
        head_v, vkey = store.resolve_version(backend, str(tenant_id), drawing_id, "head")
        head_source = backend.get(vkey)
    except (checkout_capability.CapabilityRejected,
            checkout_capability.CapabilityUnavailable) as exc:
        return _denied(exc)
    except (KeyError, ValueError):
        return error_response(ErrorCode.BAD_PARAMS,
                              "drawing unavailable or malformed request",
                              retryable=False, status_code=400)
    if int(parent_version) != int(head_v):
        return error_response(
            ErrorCode.BAD_PARAMS,
            "stale parent: the named parent_version is no longer the "
            "head; refresh the drawing and re-apply the edit",
            retryable=True, status_code=409)

    dwg_backed = head_source.startswith(write_loop.DWG_MAGIC)
    if not names_an_op:
        leg, note = "dxf-sidecar", "the plan names no operation; the DXF carries this save"
    elif not dwg_backed:
        leg, note = "dxf-sidecar", "the head has no DWG source; the DXF carries this save"
    elif deps.APS_LIVE and not plan_live_leg_enabled():
        leg, note = "dxf-sidecar", "live plan commit is gated off (LEAF_PLAN_LIVE_LEG); the DXF carries this save"
    elif deps.APS_LIVE:
        leg, note = "dwg-plan-live", "live APS WorkItem; the job carries the receipt"
    else:
        leg, note = "dwg-plan", "mock writer: the plan applied to the head's intake"

    plan_digest: Optional[str] = None
    if leg in ("dwg-plan", "dwg-plan-live"):
        try:
            _base_v, base_intake = write_loop.read_intake(
                backend, str(tenant_id), drawing_id, int(head_v))
        except write_loop.ProofStateUnreadable as exc:
            return error_response(ErrorCode.INTERNAL, str(exc),
                                  retryable=True, status_code=503)
        except (KeyError, ValueError):
            return error_response(ErrorCode.BAD_PARAMS,
                                  "drawing unavailable or malformed request",
                                  retryable=False, status_code=400)
        try:
            canonical = mutation_plan.validate_mutations(
                base_intake, mutations, allow_transforms=True, allow_xdata=False)
            # The lowering runs here too, so a plan the live WorkItem would
            # refuse (a non-planar polyline, a collinear closed one) is refused
            # on this leg as well, with the same sentence.
            plan_bytes = mutation_plan.emit_plan(
                canonical, base_sha256=hashlib.sha256(head_source).hexdigest(),
                base_intake=base_intake)
        except ValueError as exc:
            return error_response(ErrorCode.BAD_PARAMS,
                                  f"the edit plan was refused: {exc}",
                                  retryable=False, status_code=422)
        plan_digest = mutation_plan.plan_sha256(plan_bytes)

    if leg == "dwg-plan-live":
        # The client computes the plan from the same entity list it wrote the
        # DXF from, so a mismatch is a client defect. Never commit a plan the
        # bytes beside it contradict.
        expected_base = copy.deepcopy(base_intake)
        expected_base["dwg"] = drawing_id
        uploaded = copy.deepcopy(intake)
        uploaded["dwg"] = drawing_id

        def text_entities(entities):
            entries = Counter()
            for entity in entities:
                point = entity.get("pt") or []
                xy = [point[index] if index < len(point) else None for index in range(2)]
                xy = tuple(
                    write_loop._extractor_round(value, 3)
                    if isinstance(value, (int, float)) and not isinstance(value, bool) else value
                    for value in xy)
                entries[(entity.get("kind"),
                         str(entity["handle"]) if entity.get("handle") is not None else None,
                         str(entity["layer"]) if entity.get("layer") is not None else None,
                         *xy, entity.get("text"))] += 1
            return entries

        try:
            if "texts" in base_intake and (
                text_entities(uploaded.get("texts") or [])
                != text_entities(base_intake.get("texts") or [])
            ):
                raise ValueError("text entities differ from the head")
            quantized_upload = write_loop.quantize_intake_like_extractor(uploaded)
            quantized_base = write_loop.quantize_intake_like_extractor(expected_base)
            named_handles = {str(handle) for handle in canonical.get("removed", [])}
            for operation in ("set_layer", "set_circle", "set_arc", "set_points", "transforms"):
                named_handles.update(str(entry["handle"]) for entry in canonical.get(operation, []))
            for field in ("circles", "arcs"):
                head_by_handle = {
                    str(entity["handle"]): entity
                    for entity in quantized_base.get(field) or []
                    if isinstance(entity, dict) and entity.get("handle") is not None
                }
                for entity in quantized_upload.get(field) or []:
                    normal = entity.get("nrm", [0.0, 0.0, 1.0])
                    if len(normal) != 3 or any(
                        not abs(value - expected) <= 1e-9
                        for value, expected in zip(normal, [0.0, 0.0, 1.0])
                    ):
                        raise ValueError("an entity lies outside the drawing plane")
                    handle = str(entity.get("handle"))
                    reference = head_by_handle.get(handle)
                    if handle not in named_handles and reference is not None:
                        differs = any(entity.get(key) != reference.get(key) for key in ("layer", "c", "r"))
                        if field == "arcs":
                            differs = differs or any(
                                write_loop._extractor_round(write_loop._plan_number(entity[key]), 3)
                                != write_loop._extractor_round(write_loop._plan_number(reference[key]), 3)
                                for key in ("start_deg", "end_deg")
                            )
                        if differs:
                            raise ValueError("an unchanged entity differs from the head")
            write_loop.verify_live_mutation_effects(
                expected_base, quantized_upload, canonical)
        except ValueError as exc:
            return error_response(ErrorCode.BAD_PARAMS,
                                  f"the uploaded DXF does not carry the plan's result: {exc}",
                                  retryable=False, status_code=422)

        co = store.load_manifest(backend, str(tenant_id), drawing_id).get("checkout")
        if store.checkout_active(co):
            remaining = (datetime.fromisoformat(co["expires"])
                         - datetime.now(timezone.utc)).total_seconds()
            if remaining < jobs.job_max_s():
                return error_response(
                    ErrorCode.BAD_PARAMS,
                    f"the edit lock has {int(remaining)} s left, less than a live commit "
                    "may take; refresh the checkout and save again",
                    retryable=True, status_code=409)

        plan = {"drawing_id": drawing_id, "parent_version": int(parent_version),
                "mutations": canonical, "plan_sha256": plan_digest,
                "source_sha256": actual_digest}
        if len(json.dumps(plan, default=str).encode("utf-8")) > jobs.MAX_PARAMS_BYTES:
            leg, note = "dxf-sidecar", "the plan exceeds the job payload cap; the DXF carries this save"
            plan_digest = None
        else:
            try:
                job_id = jobs.submit_plan_job(
                    str(tenant_id), plan, drawing_id,
                    checkout_holder=holder, checkout_fence=fence)
            except HTTPException:
                raise
            except Exception as exc:  # noqa: BLE001
                return error_response(ErrorCode.INTERNAL,
                                      f"plan submit failed: {type(exc).__name__}",
                                      retryable=False, status_code=500)
            view = deps.tenant_echo({
                "drawing_id": drawing_id,
                "job_id": job_id,
                "status": "submitted",
                "commit": "dwg-plan",
                "commit_note": note,
                "parent": int(parent_version),
                "plan_sha256": plan_digest,
                "source_sha256": actual_digest,
                "source_stored": False,
                "cost": {"engine": "aps-workitem", "engine_usd": None},
            }, tenant_id)
            return JSONResponse(status_code=202, content=with_envelope_fields(view))

    if leg == "dwg-plan":
        try:
            new_intake = write_loop.apply_mutations(base_intake, canonical)
        except ValueError as exc:
            return error_response(ErrorCode.BAD_PARAMS,
                                  f"the edit plan could not be applied: {exc}",
                                  retryable=False, status_code=422)
        payload = json.dumps(new_intake, separators=(",", ":")).encode("utf-8")
        intake_digest = hashlib.sha256(payload).hexdigest()
        meta = {"tool": jobs.PLAN_TOOL_NAME, "source": "cad_edit",
                "plan_sha256": plan_digest,
                "source_sha256": actual_digest,
                "intake_sha256": intake_digest,
                "note": "browser edit plan, mock writer (intake payload)"}
        cost = {"engine_usd": 0.0, "engine": "mock-writer"}
    else:
        payload = json.dumps(intake, separators=(",", ":")).encode("utf-8")
        intake_digest = hashlib.sha256(payload).hexdigest()
        meta = {"tool": "cad-edit-surface", "source": "cad_edit",
                "source_sha256": actual_digest,
                "intake_sha256": intake_digest,
                "note": "browser edit via the isolated engine worker"}
        cost = {"engine_usd": 0.0, "engine": "client-wasm"}

    try:
        with write_loop.drawing_mutation_commit_guard() as commit_enabled:
            if not commit_enabled:
                return error_response(
                    ErrorCode.INTERNAL,
                    "drawing mutations are temporarily disabled for a storage cutover",
                    retryable=True, status_code=503)
            new_v = write_loop._put_bytes_version(
                backend, str(tenant_id), drawing_id, payload,
                parent_version=int(parent_version),
                meta=meta, holder=holder, fence=fence,
                require_parent_is_head=True)
    except store.CheckoutDenied as exc:
        return _denied(checkout_capability.CapabilityRejected(str(exc)))
    except ValueError as exc:
        if str(exc).startswith(("stale parent", "stale drawing head")):
            return error_response(
                ErrorCode.BAD_PARAMS,
                "stale parent: the named parent_version is no longer the "
                "head; refresh the drawing and re-apply the edit",
                retryable=True, status_code=409)
        return error_response(ErrorCode.BAD_PARAMS,
                              "version write rejected by the store",
                              retryable=False, status_code=400)
    except Exception as exc:  # noqa: BLE001 — persist faults answer typed, never a raw 500
        return error_response(ErrorCode.INTERNAL,
                              f"version persist failed: {type(exc).__name__}",
                              retryable=False, status_code=500)

    # The sidecar leg keeps the raw full-fidelity DXF beside the version (F-3);
    # the plan leg's version is the applied intake, which the engine reopens
    # through the synthesizer, so no sidecar is stored for it.
    source_stored = False
    if leg == "dxf-sidecar":
        source_stored = True
        try:
            backend.put(write_loop.edited_source_key(str(tenant_id), drawing_id, new_v), data)
        except Exception:  # noqa: BLE001
            source_stored = False

    view = deps.tenant_echo({
        "drawing_id": drawing_id,
        "new_version": {"drawing_id": drawing_id, "version": new_v,
                        "parent": int(parent_version)},
        "head": new_v,
        "commit": leg,
        "commit_note": note,
        "plan_sha256": plan_digest,
        "source_sha256": actual_digest,
        "intake_sha256": intake_digest,
        "source_stored": source_stored,
        "cost": cost,
    }, tenant_id)
    return JSONResponse(status_code=201, content=with_envelope_fields(view))


# --------------------------------------------------------------------------- #
# Single-writer checkout lock — the MUTATING half (§14)
#
# GET /versions surfaces the lock read-only (the "someone else is editing" chip).
# These two routes let a holder TAKE and RELEASE it, reusing the da/store.py
# primitives (acquire_checkout / release_checkout) unchanged — the lock lives in
# the SAME per-tenant manifest the versions/intake/undo routes read, and the
# demo drawing bootstraps identically (ensure_demo_drawing).
#
# SEMANTICS (from da/store.py, not reimplemented here):
#   * The lock is keyed per (tenant, drawing); `holder` is the session identity
#     (defaults to the requesting tenant/session). A DIFFERENT holder on the same
#     tenant+drawing conflicts — that is the single-writer guarantee. Two distinct
#     TENANTS have separate manifests and never collide (correct isolation).
#   * An EXPIRED lock (expires <= now) is free — re-acquirable by anyone.
#   * A read-tool run never consults this lock, so a held lock cannot block reads.
#
# WHAT PROVES OWNERSHIP (checkout_capability.py). `holder` is a DISPLAY label and
# is published by GET /versions, so it can only ever say who the UI should name,
# never who the caller is. Acquiring mints an opaque capability bound to the
# tenant, the AUTHENTICATED subject, the drawing and the lock generation; that
# token — never the holder — is what releasing, refreshing, undo, redo and a
# `drawing.write` run require. So a same-tenant session that reads the holder off
# /versions learns a name and gains nothing.
# --------------------------------------------------------------------------- #
DEFAULT_CHECKOUT_TTL_S = 3600            # 1h working lease (sane default)
MAX_CHECKOUT_TTL_S = 86_400             # 24h hard cap so a forgotten lock always expires


class CheckoutRequest(BaseModel):
    """Optional POST body for taking the lock. Both fields default: `holder` to the
    requesting tenant/session id, `ttl_s` to DEFAULT_CHECKOUT_TTL_S.

    `holder` is a DISPLAY LABEL — what the UI shows in its "locked by X" chip. It
    is not, and must not be read as, a claim of identity: the caller picks it, and
    GET /versions publishes it. What the caller gets back for having taken the
    lock is the opaque `checkout_capability`, and that is the only thing any
    mutating route accepts as proof of ownership.
    """
    holder: Optional[str] = Field(default=None, max_length=200)
    ttl_s: Optional[float] = Field(default=None, gt=0, le=MAX_CHECKOUT_TTL_S)


@router.post("/api/drawings/{drawing_id}/checkout")
def acquire_checkout_route(drawing_id: str, req: Optional[CheckoutRequest] = None,
                           tenant_id: str = Depends(deps.require_active_tenant),
                           x_checkout_capability: Optional[str] = Header(default=None)) -> Any:
    """Take the single-writer lock, and mint the capability that proves it.

    §10-enveloped `{drawing_id, acquired: true, holder, checkout_capability,
    checkout:{holder, acquired, expires}}` on success. KEEP THE
    `checkout_capability`: it is issued once, to this caller, and it is what
    DELETE .../checkout, undo, redo and a `drawing.write` run all require. It is
    not recoverable from any read — that is the point — so losing it means waiting
    out the lease.

    REFRESHING a live lease now requires presenting the current capability in
    `X-Checkout-Capability`, and the refreshed lease gets a NEW one (the old
    generation stops verifying). Naming the holder used to be enough, which was
    this hole from the other end: session B could read A's holder off GET
    /versions, "refresh" A's lock as A, and be handed authority over it. An active
    lock with no valid capability -> HTTP 409 not-acquired `{acquired: false,
    locked_by, checkout, error}` — the same answer a second holder always got, so
    the UI's "locked by X" path is unchanged.

    An unlocked or EXPIRED lock is granted to anyone, capability or not (the
    store's rule everywhere else: a forgotten lease must not wedge a drawing).
    Same 404 pattern as the other routes for an unknown drawing (the well-known
    `demo` bootstraps on first use at APS_LIVE=0)."""
    blocked = _mutation_gate()
    if blocked is not None:
        return blocked
    import store  # da/store.py; importable via write_loop's sys.path setup

    backend = _backend(str(tenant_id))
    try:
        write_loop.ensure_demo_drawing(backend, str(tenant_id), drawing_id)
    except (KeyError, ValueError) as exc:
        return error_response(ErrorCode.BAD_PARAMS, f"drawing unavailable: {exc}",
                              retryable=False, status_code=404)

    holder = (req.holder if req else None) or str(tenant_id)
    ttl_s = (req.ttl_s if req and req.ttl_s is not None else None) or DEFAULT_CHECKOUT_TTL_S

    # The shared cutover fence is held across the whole acquire. `_mutation_gate`
    # above is the deployment default; this is the LIVE drain, so a cutover that
    # starts after that check still cannot be crossed by this request.
    with write_loop.drawing_mutation_commit_guard() as commit_enabled:
        if not commit_enabled:
            return error_response(
                ErrorCode.INTERNAL,
                "drawing mutations are temporarily disabled for a storage cutover",
                retryable=True,
                status_code=503,
            )

        # A refresh needs the CURRENT generation, and only a valid capability yields
        # one. A caller without it gets expected_fence=None, which `strict_owner`
        # refuses against any live lease — so an unprovable refresh comes back as the
        # ordinary 409 conflict below rather than silently taking the lock over.
        prior = store.load_manifest(backend, str(tenant_id), drawing_id).get("checkout")
        expected_fence: Optional[int] = None
        if store.checkout_active(prior):
            try:
                _, expected_fence = checkout_capability.verify(
                    x_checkout_capability, tenant_id, drawing_id, prior)
            except checkout_capability.CapabilityUnavailable as exc:
                return _denied(exc)
            except checkout_capability.CapabilityRejected:
                expected_fence = None

        # Refuse a deployment that could not mint BEFORE taking the lock. Doing it
        # after would leave an active lease with no capability ever issued, which
        # nobody can then release or write — a server misconfiguration turning into
        # a locked drawing for the whole TTL.
        try:
            checkout_capability.ensure_mintable(tenant_id, drawing_id)
        except checkout_capability.CapabilityUnavailable as exc:
            return _denied(exc)

        try:
            # The generation comes back from the acquire ITSELF. Re-reading the
            # manifest to find one would ask what is current now, not what this call
            # wrote: with a short ttl_s this lease can already have lapsed and
            # another session acquired, and minting against that generation would
            # hand this caller a valid capability for someone else's lease.
            granted_fence = store.acquire_checkout_fence(
                backend, str(tenant_id), drawing_id, holder, ttl_s,
                expected_fence=expected_fence, strict_owner=True)
        except store.CheckoutParamError as exc:
            # ONLY the input faults: a reserved holder (store.ANONYMOUS_HOLDER, the
            # id a run with no `holder` presents) and a non-positive ttl. Answer 400
            # with the reason; without this the error escaped as a 500, which reads
            # as a server fault and tells the caller nothing about what to send.
            #
            # Deliberately NOT a bare `except ValueError`: acquire_checkout also
            # decodes the stored manifest, and a corrupt one raises JSONDecodeError,
            # which IS a ValueError. Catching broadly would blame the caller for
            # damaged storage and hide a real fault behind a 400 nobody can act on.
            return error_response(ErrorCode.BAD_PARAMS, str(exc), retryable=False,
                                  status_code=400)
        acquired = granted_fence is not None
    m = store.load_manifest(backend, str(tenant_id), drawing_id)
    co = m.get("checkout")

    if not acquired:
        # A LIVE lease the caller could not prove it owns (an absent or expired lock
        # would have returned True). Return the CURRENT lock so the frontend can show
        # who holds it, with a §10 error object for strict consumers (BAD_PARAMS is
        # the frozen-enum stand-in for a retryable conflict).
        cur = co or {}
        body = with_envelope_fields(deps.tenant_echo({
            "drawing_id": drawing_id,
            "acquired": False,
            "locked_by": cur.get("holder"),
            "checkout": _checkout_view(co),
            "error": error_obj(
                ErrorCode.BAD_PARAMS,
                f"checkout locked by {cur.get('holder')!r} until {cur.get('expires')}",
                retryable=True),
        }, tenant_id))
        return JSONResponse(status_code=409, content=body)

    try:
        capability = checkout_capability.mint(
            tenant_id, drawing_id, int(granted_fence))
    except checkout_capability.CapabilityUnavailable as exc:
        return _denied(exc)

    view = deps.tenant_echo({
        "drawing_id": drawing_id,
        "acquired": True,
        "holder": holder,
        # Issued ONCE, to this caller. Never readable from GET /versions, never
        # persisted into a job record, never echoed anywhere else.
        "checkout_capability": capability,
        "checkout": _checkout_view(co),
    }, tenant_id)
    return with_envelope_fields(view)


@router.delete("/api/drawings/{drawing_id}/checkout")
def release_checkout_route(drawing_id: str,
                           tenant_id: str = Depends(deps.require_active_tenant),
                           x_checkout_capability: Optional[str] = Header(default=None)) -> Any:
    """Release the single-writer lock. Only a caller that can PROVE it holds the
    active lease may release it — `X-Checkout-Capability`, from the acquire
    response. Without a valid one → HTTP 403, lock left intact.

    The `?holder=` query parameter this route used to authorize with is GONE. It
    was a public string (GET /versions publishes it), so it proved nothing: anyone
    on the tenant could read the holder and release someone else's lock. Releasing
    when nothing is held (or the lock already expired) is still an idempotent 200
    with the cleared state and needs no capability. §10-enveloped
    `{drawing_id, released, checkout: null}`."""
    blocked = _mutation_gate()
    if blocked is not None:
        return blocked
    import store  # da/store.py; importable via write_loop's sys.path setup

    backend = _backend(str(tenant_id))
    try:
        write_loop.ensure_demo_drawing(backend, str(tenant_id), drawing_id)
    except (KeyError, ValueError) as exc:
        return error_response(ErrorCode.BAD_PARAMS, f"drawing unavailable: {exc}",
                              retryable=False, status_code=404)
    try:
        holder, fence = _lock_authorization(drawing_id, tenant_id, backend,
                                            x_checkout_capability)
    except (checkout_capability.CapabilityRejected,
            checkout_capability.CapabilityUnavailable) as exc:
        return _denied(exc)
    except KeyError as exc:
        # Do not catch ValueError here. JSONDecodeError inherits from it, and
        # damaged storage is a server fault rather than a missing drawing.
        return error_response(ErrorCode.BAD_PARAMS, f"drawing unavailable: {exc}",
                              retryable=False, status_code=404)

    # Held across the release for the same reason as the acquire: a drain that
    # starts after `_mutation_gate` must not be crossed by an in-flight clear.
    with write_loop.drawing_mutation_commit_guard() as commit_enabled:
        if not commit_enabled:
            return error_response(
                ErrorCode.INTERNAL,
                "drawing mutations are temporarily disabled for a storage cutover",
                retryable=True,
                status_code=503,
            )
        if fence is None:
            # Nothing ACTIVE to release: idempotent. An expired lock is cleared (the
            # store grants those to anyone), an absent one reports released=False.
            released = store.release_checkout(backend, str(tenant_id), drawing_id)
        else:
            # Release by PROVEN generation, not by holder label, so the clear cannot
            # land on a different lease that started since the check above.
            released = store.release_checkout(backend, str(tenant_id), drawing_id,
                                              holder=holder, expected_fence=fence)

    m2 = store.load_manifest(backend, str(tenant_id), drawing_id)
    view = deps.tenant_echo({
        "drawing_id": drawing_id,
        "released": bool(released),
        "checkout": _checkout_view(m2.get("checkout")),
    }, tenant_id)
    return with_envelope_fields(view)
