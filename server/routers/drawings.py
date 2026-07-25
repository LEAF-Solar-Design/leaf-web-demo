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

from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import checkout_capability
import deps
import write_loop
from envelopes import ErrorCode, error_obj, error_response, with_envelope_fields

router = APIRouter()
SUMMARY_LAYER_CAP = 50
CACHED_DEMO_DRAWING_ID = "rooftop_demo"


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
               tenant_id: str = Depends(deps.require_tenant)) -> Dict[str, Any]:
    ver: Any = version
    if isinstance(version, str) and version not in ("head", "latest") and version.lstrip("-").isdigit():
        ver = int(version)
    try:
        view = write_loop.intake_view(str(tenant_id), drawing_id, ver, backend=_backend(str(tenant_id)))
    except (KeyError, ValueError) as exc:
        return error_response(ErrorCode.BAD_PARAMS, f"drawing/version unavailable: {exc}",
                              retryable=False, status_code=404)
    return with_envelope_fields(deps.tenant_echo(view, tenant_id))


@router.get("/api/drawings/{drawing_id}/summary")
def get_summary(drawing_id: str, version: str = "head",
                tenant_id: str = Depends(deps.require_tenant)) -> Dict[str, Any]:
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
def undo(drawing_id: str, tenant_id: str = Depends(deps.require_tenant),
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
    except (KeyError, ValueError) as exc:
        return error_response(ErrorCode.BAD_PARAMS, str(exc), retryable=False, status_code=400)
    return with_envelope_fields(deps.tenant_echo(view, tenant_id))


@router.post("/api/drawings/{drawing_id}/redo")
def redo(drawing_id: str, tenant_id: str = Depends(deps.require_tenant),
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
    except (KeyError, ValueError) as exc:
        return error_response(ErrorCode.BAD_PARAMS, str(exc), retryable=False, status_code=400)
    return with_envelope_fields(deps.tenant_echo(view, tenant_id))


def _checkout_view(co: Any) -> Any:
    """Manifest `checkout` lock → the surface shape `{holder, acquired, expires}`
    or null. GET /versions renders this read-only ("someone else is editing" chip);
    the POST/DELETE .../checkout routes below let the holder TAKE and RELEASE it.

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
    if not co:
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


def _version_row(e: Dict[str, Any]) -> Dict[str, Any]:
    """One manifest `versions[]` entry → the version-history row shape
    (CONTRACT-ADDENDUM §11 manifest fields; missing optional fields → null)."""
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
    }


@router.get("/api/drawings/{drawing_id}/versions")
def get_versions(drawing_id: str, tenant_id: str = Depends(deps.require_tenant)) -> Dict[str, Any]:
    """Full version-history chain straight from the store manifest — the read the
    UI's version-chain popover needs (undo/redo only stepped head one hop).

    §10-enveloped `{drawing_id, head, latest, versions:[{v, parent, created, bytes,
    sha256, tool, workitem_id, note}], checkout: {holder, acquired, expires}|null}`
    (§14: `checkout` is the single-writer lock from the manifest, read-only — no
    acquire/release this wave). Same 404 pattern as the intake route for
    an unknown drawing (the well-known `demo` bootstraps on first read at
    APS_LIVE=0; any other unknown drawing → BAD_PARAMS 404)."""
    import store  # da/store.py; importable via write_loop's sys.path setup (imported above)

    backend = _backend(str(tenant_id))
    try:
        write_loop.ensure_demo_drawing(backend, str(tenant_id), drawing_id)
        m = store.load_manifest(backend, str(tenant_id), drawing_id)
    except (KeyError, ValueError) as exc:
        return error_response(ErrorCode.BAD_PARAMS, f"drawing unavailable: {exc}",
                              retryable=False, status_code=404)
    view = {
        "drawing_id": drawing_id,
        "head": int(m["head"]),
        "latest": int(m["latest"]),
        "versions": [_version_row(e) for e in m.get("versions", [])],
        "checkout": _checkout_view(m.get("checkout")),  # single-writer lock (read-only)
    }
    return with_envelope_fields(deps.tenant_echo(view, tenant_id))


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
                           tenant_id: str = Depends(deps.require_tenant),
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
                           tenant_id: str = Depends(deps.require_tenant),
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
        holder, fence = _lock_authorization(drawing_id, tenant_id, backend,
                                            x_checkout_capability)
    except (checkout_capability.CapabilityRejected,
            checkout_capability.CapabilityUnavailable) as exc:
        return _denied(exc)
    except (KeyError, ValueError) as exc:
        return error_response(ErrorCode.BAD_PARAMS, f"drawing unavailable: {exc}",
                              retryable=False, status_code=404)

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
