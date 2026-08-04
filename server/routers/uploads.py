"""Drawing uploads (guest + account) — CONTRACT-ADDENDUM §19, this lane's router.

    POST /api/drawings/upload                  multipart {file} -> 202 {drawing_id,
         tenant_id, tenant_kind, retention_expires_at|null, guest_session|null,
         status: "extracting"}   (envelope-wrapped per §10)
    GET  /api/drawings/{drawing_id}/upload-status
         -> {status: extracting|ready|failed, error|null, filename, uploaded_at,
             retention_expires_at|null, extracted_version:int|null}

AUTH IS OPTIONAL here — deliberately not `Depends(deps.require_tenant)`,
because a signed-out visitor is this endpoint's primary caller:
  * live auth + Bearer        -> the verified account tenant ("account");
  * live auth + guest session -> the SAME guest tenant again ("guest");
  * live auth + neither       -> a freshly minted ephemeral guest tenant
                                 ("guest") + an HMAC guest-session token
                                 (LEAF_GUEST_SECRET unset -> honest 503,
                                 never an unsigned identity);
  * auth off                  -> the X-Tenant-Id stub or a minted guest —
                                 byte-compatible with the open-demo world.

The retention promise the UI shows comes from guest_uploads.retention_hours()
via /api/site/guest-upload-policy; the SAME function stamps the expiry the
purge job honors. Every failure is an honest §10 error — an uploaded id can
never fall through to the cached demo intake (write_loop.ensure_demo_drawing
guards, tests/test_guest_fail_closed.py).
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, File, Header, Request, UploadFile
from fastapi.responses import JSONResponse

import deps
import entitlements
import guest_uploads
import write_loop
from envelopes import ErrorCode, error_response, with_envelope_fields

router = APIRouter()

_MINT_ATTEMPTS = 4  # UUIDv4 per attempt; collision is cosmic-ray territory


def _client_ip(request: Request) -> str:
    """Rate-limit key. X-Forwarded-For (first hop) is honored ONLY when the
    deploy says the app sits behind a trusted proxy (LEAF_TRUST_FORWARDED_FOR=1)
    — otherwise it is client-forgeable and the socket peer is the truth. The
    GLOBAL daily cap backstops either way."""
    import os
    if os.environ.get("LEAF_TRUST_FORWARDED_FOR") == "1":
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd.strip():
            return fwd.split(",")[0].strip()
    client = request.client
    return client.host if client else "unknown"


def _resolve_upload_identity(
    x_tenant_id: Optional[str],
    authorization: Optional[str],
    x_guest_session: Optional[str],
) -> Tuple[Any, str, bool]:
    """-> (tenant, tenant_kind, minted). Raises HTTPException via the auth
    module for a PRESENT-but-invalid Bearer (an expired real login must surface
    as 401, not silently downgrade the caller to a guest)."""
    if deps.auth_live():
        if authorization:
            import auth  # noqa: PLC0415 - lazy, mirrors deps.require_tenant
            import tenancy  # noqa: PLC0415
            payload = auth.verify_platform_token(authorization)
            claims = auth.extract_tenant_claims(payload)
            subject = payload.get("sub") if isinstance(payload.get("sub"), str) else None
            platform_tenant_id, platform_tier = (
                deps.resolve_active_platform_tenant_authority(subject))
            deps.require_matching_platform_tenant_claim(
                claims, platform_tenant_id)
            ws = tenancy.get_store().resolve_workspace(platform_tenant_id)
            tenant = deps.TenantContext(
                platform_tenant_id, org_id=platform_tenant_id,
                tier=platform_tier,
                workspace=ws.workspace_dir if ws is not None else None,
                subject=subject)
            return tenant, "account", False
        if x_guest_session:
            existing = guest_uploads.verify_guest_session(x_guest_session)
            if existing is not None:
                return deps.TenantContext(existing, tier="guest"), "guest", False
        return deps.TenantContext(guest_uploads.mint_guest_tenant_id(),
                                  tier="guest"), "guest", True
    # auth OFF — the open demo's header-stub world
    if x_tenant_id:
        kind = "guest" if guest_uploads.is_guest_tenant(x_tenant_id) else "account"
        return x_tenant_id, kind, False
    return guest_uploads.mint_guest_tenant_id(), "guest", True


def _resolve_upload_read_identity(tenant: Any) -> Any:
    """Use the same server-owned account binding as upload creation.

    ``require_tenant`` still carries the tenant claim from the verified JWT for
    legacy routes. Account upload creation deliberately ignores that claim and
    writes under the subject's active platform binding, so its status reader
    must resolve that same binding or it can look in a different tenant store.
    Guest sessions have no platform subject and auth-off callers are plain
    strings; both keep their existing identity unchanged.
    """
    return deps.resolve_active_tenant_context(tenant)


def _emit_upload_event(resp: Any, ident: Dict[str, Any]) -> None:
    """Best-effort drawing.uploaded / drawing.upload_rejected product event
    (P2), derived from the ONE response every branch already returns, so no
    rejection branch needs its own emit. drawing_id only, never a filename
    (tighter than the plugin exemplar). NEVER raises.

    ``ident`` is filled by _upload_drawing the moment identity resolves, so
    a POST-resolution rejection (entitlement/size/validation/quota) is
    attributed to the REAL tenant; only pre-resolution failures are "anon"
    (review #426 round-1 blocker 2). `minted` in ident is the resolver's
    own minted flag, not token presence (round-1 warn 4)."""
    try:
        import json as _json

        import telemetry_sink

        if isinstance(resp, JSONResponse):
            status = resp.status_code
            body = _json.loads(resp.body)
        elif isinstance(resp, dict):
            status, body = 202, resp
        else:
            return
        if status < 300:
            tid = str(body.get("tenant_id") or "")
            if not tid:
                return
            telemetry_sink.emit(
                "drawing.uploaded",
                tenant_id=tid,
                tenant_kind=str(body.get("tenant_kind") or "account"),
                session_id="server",
                labels={
                    "drawing_id": body.get("drawing_id"),
                    "minted_guest": bool(ident.get("minted")),
                    "status": body.get("status"),
                },
            )
            return
        err = body.get("error") or {}
        message = str(err.get("message") or "").lower()
        if status == 413 or "byte upload cap" in message:
            reason = "size"
        elif "quota" in message:
            reason = "quota"
        elif "disabled" in message or "not configured" in message or "cutover" in message:
            reason = "disabled"
        else:
            reason = "validation"
        tid = str(ident.get("tenant") or "anon")
        telemetry_sink.emit(
            "drawing.upload_rejected",
            tenant_id=tid,
            tenant_kind=str(ident.get("kind") or "anon"),
            session_id="server",
            labels={"reason": reason, "http_status": status,
                    "error_code": err.get("error_code")},
        )
    except Exception:  # noqa: BLE001 - telemetry never touches the upload path
        pass


@router.post("/api/drawings/upload")
def upload_drawing(
    request: Request,
    file: UploadFile = File(...),
    x_tenant_id: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    x_guest_session: Optional[str] = Header(default=None),
) -> Any:
    ident: Dict[str, Any] = {}
    resp = _upload_drawing_gated(
        request, file, x_tenant_id, authorization, x_guest_session, ident)
    _emit_upload_event(resp, ident)
    return resp


def _upload_drawing_gated(
    request: Request,
    file: UploadFile,
    x_tenant_id: Optional[str],
    authorization: Optional[str],
    x_guest_session: Optional[str],
    ident: Optional[Dict[str, Any]] = None,
) -> Any:
    if not write_loop.upload_import_mutations_enabled():
        return error_response(
            ErrorCode.INTERNAL,
            "drawing upload/import mutations are temporarily disabled",
            retryable=True,
            status_code=503,
        )
    if deps.auth_live() and not authorization:
        if not guest_uploads.enabled():
            return error_response(
                ErrorCode.INTERNAL,
                "guest drawing uploads are disabled on this deployment",
                retryable=False,
                status_code=503,
            )
        if guest_uploads.guest_secret() is None:
            # Live mode with no way to mint a verifiable guest identity: the
            # guest lane is OFF. Honest 503, never an unsigned tenant.
            return error_response(
                ErrorCode.INTERNAL,
                "guest uploads are not configured (LEAF_GUEST_SECRET unset)",
                retryable=False,
                status_code=503,
            )
    # The admission checks above are deployment defaults. The shared fence is
    # the LIVE drain and is held across the whole ingest, so a cutover starting
    # mid-request cannot be crossed by an upload already past those checks.
    with write_loop.upload_mutation_commit_guard() as commit_enabled:
        if not commit_enabled:
            return error_response(
                ErrorCode.INTERNAL,
                "drawing mutations are temporarily disabled for a storage cutover",
                retryable=True,
                status_code=503,
            )
        return _upload_drawing(
            request,
            file,
            x_tenant_id,
            authorization,
            x_guest_session,
            ident,
        )


def _upload_drawing(
    request: Request,
    file: UploadFile,
    x_tenant_id: Optional[str],
    authorization: Optional[str],
    x_guest_session: Optional[str],
    ident: Optional[Dict[str, Any]] = None,
) -> Any:

    tenant, tenant_kind, _minted = _resolve_upload_identity(
        x_tenant_id, authorization, x_guest_session)
    if ident is not None:
        # Attribution for telemetry: from here on, any rejection belongs to
        # THIS resolved identity, never "anon" (review #426 round-1 blocker 2).
        ident.update(tenant=str(tenant), kind=tenant_kind, minted=_minted)
    if tenant_kind == "guest" and not guest_uploads.enabled():
        return error_response(
            ErrorCode.INTERNAL,
            "guest drawing uploads are disabled on this deployment",
            retryable=False,
            status_code=503,
        )

    # ENTITLEMENT GATE (§17 pattern): the tier must grant `upload`.
    tier = "guest" if tenant_kind == "guest" else entitlements.resolve_tier(tenant)
    roles, elevated = ((), False) if tenant_kind == "guest" else entitlements.resolve_roles(tenant)
    if not entitlements.entitlements_for(tier, roles, elevated).get("upload", False):
        return entitlements.entitlement_denied_response("upload", tier)

    # Size cap. Two layers: (1) the declared Content-Length is rejected up
    # front; (2) the handler never buffers more than cap+1 bytes. NOTE
    # (review round 1, MAJOR): FastAPI parses the multipart body (spooling to
    # temp disk) BEFORE this handler runs, so a length-less/chunked oversized
    # body still costs transient disk — the deployment's ingress proxy body
    # limit is the real outer wall (documented in CONTRACT-ADDENDUM §19).
    cap = guest_uploads.max_upload_bytes()
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > cap + 65536:
        return error_response(
            ErrorCode.BAD_PARAMS,
            f"request exceeds the {cap} byte upload cap", retryable=False,
            status_code=413)
    data = file.file.read(cap + 1)
    if len(data) > cap:
        return error_response(
            ErrorCode.BAD_PARAMS,
            f"file exceeds the {cap} byte upload cap", retryable=False,
            status_code=413)

    ext, reason = guest_uploads.validate_upload(file.filename or "", data)
    if reason is not None:
        return error_response(ErrorCode.BAD_PARAMS, reason, retryable=False,
                              status_code=400)

    backend = write_loop.upload_backend_for_tenant(str(tenant))
    import store  # importable via write_loop's sys.path setup

    # §19 idempotent GUEST uploads (FE review round 3, MAJOR — receipt
    # recovery): a guest's drawing id derives from (tenant, content), so
    # re-posting the SAME bytes returns the SAME drawing's receipt instead of
    # minting a duplicate — an aborted upload whose 202 the client discarded
    # is recovered by re-uploading, and costs quota exactly once. A prior
    # TERMINALLY FAILED attempt is not deduped: the retry reuses the derived
    # id and REPLACES the failure (fresh quota count — it extracts again).
    # Account uploads keep random ids: two intentional copies of one file
    # stay two drawings.
    drawing_id = None
    dedupe_marker = None
    if tenant_kind == "guest":
        derived = guest_uploads.derived_upload_drawing_id(str(tenant), data)
        # ONE critical section for check → quota charge → marker + staged
        # bytes: two concurrent identical uploads cannot both pass the dedupe
        # check — the loser blocks on the lock, then dedupes onto the
        # winner's marker for free (one quota count, ONE extraction — a
        # second extraction of the same drawing would trip ingest's
        # already-exists refusal and falsely fail a good upload). The same
        # lock keeps the purge sweep from receipting a deletion between the
        # marker and staged-bytes writes (review round 4, MAJOR).
        with guest_uploads.drawing_lock(str(tenant), derived):
            existing = guest_uploads.read_marker(backend, str(tenant), derived)
            if existing is not None and existing.get("status") != "failed":
                dedupe_marker = existing
                marker = existing
                drawing_id = derived
            else:
                has_manifest = backend.exists(
                    store.manifest_key(str(tenant), derived))
                if existing is not None and not has_manifest:
                    # status == "failed" with INVISIBLE residue only (no
                    # committed manifest — e.g. a v1 blob without its
                    # manifest, which would wedge the derived id in an
                    # immutable-version refusal loop). Wipe BEFORE the quota
                    # charge: a failed wipe answers 500 WITHOUT consuming a
                    # slot (round-7 review, MAJOR), and a quota-destined 429
                    # can only ever have destroyed state the API never
                    # served (round-8 review, MAJOR). The wipe is VERIFIED
                    # and keeps the marker so a partial deletion routes the
                    # NEXT retry back into this same path (round-6 review,
                    # MAJOR); the fresh marker's attempt token fences out
                    # the old worker thread if it is still alive.
                    if not guest_uploads.wipe_failed_attempt_residue(
                            str(tenant), derived, existing.get("attempt")):
                        return error_response(
                            ErrorCode.INTERNAL,
                            "could not reset the failed attempt's residue; "
                            "try again", retryable=True, status_code=500)
                # Guest cost-exposure caps (each live DWG extraction is a paid
                # APS run; a DXF is parsed locally by default — service CPU, not
                # an APS charge — but also a paid APS run under
                # LEAF_GUEST_DXF_EXTRACT=aps, see guest_uploads.run_extraction).
                # Counted AFTER validation on purpose (review round
                # 1, MAJOR): garbage requests must not be able to exhaust the
                # shared daily pool — only an upload that will actually stage
                # + extract consumes quota. A dedupe hit consumes none.
                exceeded = guest_uploads.check_and_count_guest_upload(
                    _client_ip(request))
                if exceeded is not None:
                    scope = ("this network address" if exceeded == "ip"
                             else "the shared guest pool")
                    return error_response(
                        ErrorCode.QUOTA_EXCEEDED,
                        f"the daily guest upload limit for {scope} is "
                        "exhausted; try again tomorrow or create an account",
                        retryable=True, status_code=429)
                if existing is not None and has_manifest:
                    # status == "failed" with VISIBLE residue: a committed
                    # manifest a failed attempt left behind (DXF ingest
                    # succeeded, the cache/marker transition did not) —
                    # intake and versions still SERVE it. Deleting readable
                    # data happens only AFTER the quota charge, as part of
                    # the PAID replacement — never as a side effect of a
                    # request the very next line would 429 (round-8 review,
                    # MAJOR). If the wipe then fails, the charge is
                    # REFUNDED: no extraction started, no slot burned
                    # (round-7 review, MAJOR).
                    if not guest_uploads.wipe_failed_attempt_residue(
                            str(tenant), derived, existing.get("attempt")):
                        guest_uploads.refund_guest_upload(_client_ip(request))
                        return error_response(
                            ErrorCode.INTERNAL,
                            "could not reset the failed attempt's residue; "
                            "try again", retryable=True, status_code=500)
                if not backend.exists(store.manifest_key(str(tenant), derived)):
                    # Effect order is load-bearing: (1) marker — so
                    # /upload-status and the fail-closed bootstrap guard
                    # exist the instant the id is public; (2) stage the
                    # bytes; (3) extraction thread (outside the lock).
                    drawing_id = derived
                    marker = guest_uploads.new_marker(
                        filename=file.filename or f"upload{ext}",
                        data=data, tenant_kind=tenant_kind, source_ext=ext)
                    try:
                        guest_uploads.write_marker(
                            backend, str(tenant), drawing_id, marker)
                    except RuntimeError:
                        # Another task reserved this content-derived upload
                        # after our read. Refund this task's quota charge and
                        # adopt the winning attempt without staging or
                        # starting a second extraction.
                        if guest_uploads.upload_store_mode() != "postgres":
                            raise
                        guest_uploads.refund_guest_upload(_client_ip(request))
                        winner = guest_uploads.read_marker(
                            backend, str(tenant), drawing_id)
                        if winner is None:
                            return error_response(
                                ErrorCode.INTERNAL,
                                "upload reservation changed; try again",
                                retryable=True, status_code=503)
                        marker = winner
                        dedupe_marker = winner
                    else:
                        staged = guest_uploads.staged_path(
                            str(tenant), drawing_id, ext)
                        staged.parent.mkdir(parents=True, exist_ok=True)
                        staged.write_bytes(data)
        if drawing_id is not None and dedupe_marker is None:
            guest_uploads.start_extraction_thread(str(tenant), drawing_id, ext)

    if drawing_id is None:
        # Account uploads (random UUIDs: two intentional copies of one file
        # stay two drawings) — plus the astronomically unlikely guest case of
        # a foreign manifest squatting the derived id (quota already charged
        # above in that case).
        for _ in range(_MINT_ATTEMPTS):
            candidate = (
                guest_uploads.new_account_upload_drawing_id()
                if tenant_kind == "account"
                else guest_uploads.new_upload_drawing_id()
            )
            if (not backend.exists(store.manifest_key(str(tenant), candidate))
                    and guest_uploads.read_marker(backend, str(tenant), candidate) is None):
                drawing_id = candidate
                break
        if drawing_id is None:  # pragma: no cover - 4 consecutive random collisions
            return error_response(ErrorCode.INTERNAL, "could not mint a drawing id",
                                  retryable=True, status_code=500)

        # Same effect order and same purge-exclusion lock as the guest path.
        marker = guest_uploads.new_marker(filename=file.filename or f"upload{ext}",
                                          data=data, tenant_kind=tenant_kind,
                                          source_ext=ext)
        staged = guest_uploads.staged_path(str(tenant), drawing_id, ext)
        with guest_uploads.drawing_lock(str(tenant), drawing_id):
            guest_uploads.write_marker(backend, str(tenant), drawing_id, marker)
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(data)

        guest_uploads.start_extraction_thread(str(tenant), drawing_id, ext)

    guest_session = None
    if tenant_kind == "guest" and deps.auth_live():
        # Token expiry tracks THE DRAWING's remaining retention window (on a
        # dedupe hit that is the ORIGINAL upload's window, not a fresh 24h),
        # so the guest can always read what still exists and never holds a
        # token that outlives its drawing.
        import time as _time
        from datetime import datetime as _dt
        expires_at = marker.get("retention_expires_at")
        if expires_at:
            exp = int(_dt.fromisoformat(
                str(expires_at).replace("Z", "+00:00")).timestamp())
        else:  # pragma: no cover - guest markers always carry an expiry
            exp = int(_time.time() + guest_uploads.retention_hours() * 3600)
        guest_session = guest_uploads.mint_guest_session(str(tenant), exp)

    body = deps.tenant_echo({
        "drawing_id": drawing_id,
        "tenant_id": str(tenant),
        "tenant_kind": tenant_kind,
        "retention_expires_at": marker["retention_expires_at"],
        "guest_session": guest_session,
        "status": marker.get("status", "extracting"),
    }, tenant)
    return JSONResponse(status_code=202, content=with_envelope_fields(body))


@router.get("/api/drawings/{drawing_id}/upload-status")
def upload_status(drawing_id: str,
                  tenant=Depends(deps.require_tenant)) -> Any:
    """The upload's honest state, from the marker (the single source of upload
    truth). Guests reach this via their guest-session header (live) or the
    X-Tenant-Id stub (demo). 404 (never 403) for an unknown marker — same
    no-existence-leak posture as the jobs routes."""
    tenant = _resolve_upload_read_identity(tenant)
    backend = write_loop.upload_backend_for_tenant(str(tenant))
    view = guest_uploads.status_view(backend, str(tenant), drawing_id)
    if view is None:
        return error_response(ErrorCode.BAD_PARAMS,
                              f"no upload known for drawing {drawing_id!r}",
                              retryable=False, status_code=404)
    return with_envelope_fields(deps.tenant_echo(view, tenant))
