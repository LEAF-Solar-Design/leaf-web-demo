"""Drawing uploads (guest + account) — CONTRACT-ADDENDUM §19, this lane's router.

    POST /api/drawings/upload                  multipart {file} -> 202 {drawing_id,
         tenant_id, tenant_kind, retention_expires_at|null, guest_session|null,
         status: "extracting"}   (envelope-wrapped per §10)
    GET  /api/drawings/{drawing_id}/upload-status
         -> {status: extracting|ready|failed, error|null, filename, uploaded_at,
             retention_expires_at|null}

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

_MINT_ATTEMPTS = 4  # 40 random bits per attempt; collision is cosmic-ray territory


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
            ws = tenancy.get_store().resolve_workspace(claims["tenant_id"])
            tenant = deps.TenantContext(
                claims["tenant_id"], org_id=claims.get("org_id"),
                tier=claims.get("tier"),
                workspace=ws.workspace_dir if ws is not None else None)
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


@router.post("/api/drawings/upload")
def upload_drawing(
    request: Request,
    file: UploadFile = File(...),
    x_tenant_id: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    x_guest_session: Optional[str] = Header(default=None),
) -> Any:
    if not guest_uploads.enabled():
        return error_response(ErrorCode.INTERNAL,
                              "drawing uploads are disabled on this deployment",
                              retryable=False, status_code=503)

    if deps.auth_live() and not authorization and guest_uploads.guest_secret() is None:
        # Live mode with no way to mint a verifiable guest identity: the guest
        # lane is OFF. Honest 503 (config gap), never an unsigned tenant.
        return error_response(ErrorCode.INTERNAL,
                              "guest uploads are not configured (LEAF_GUEST_SECRET unset)",
                              retryable=False, status_code=503)

    tenant, tenant_kind, _minted = _resolve_upload_identity(
        x_tenant_id, authorization, x_guest_session)

    # ENTITLEMENT GATE (§17 pattern): the tier must grant `upload`.
    tier = "guest" if tenant_kind == "guest" else entitlements.resolve_tier(tenant)
    if not entitlements.entitlements_for(tier).get("upload", False):
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

    # Guest cost-exposure caps (each live extraction is a paid APS run).
    # Counted AFTER validation on purpose (review round 1, MAJOR): garbage
    # requests must not be able to exhaust the shared daily pool — only an
    # upload that will actually stage + extract consumes quota.
    if tenant_kind == "guest":
        exceeded = guest_uploads.check_and_count_guest_upload(_client_ip(request))
        if exceeded is not None:
            scope = ("this network address" if exceeded == "ip"
                     else "the shared guest pool")
            return error_response(
                ErrorCode.QUOTA_EXCEEDED,
                f"the daily guest upload limit for {scope} is exhausted; "
                "try again tomorrow or create an account",
                retryable=True, status_code=429)

    backend = write_loop.backend_for_tenant(
        str(tenant), aps_live=deps.APS_LIVE,
        da=deps.get_da_client() if deps.APS_LIVE else None)

    drawing_id = None
    import store  # importable via write_loop's sys.path setup
    for _ in range(_MINT_ATTEMPTS):
        candidate = guest_uploads.new_upload_drawing_id()
        if (not backend.exists(store.manifest_key(str(tenant), candidate))
                and guest_uploads.read_marker(backend, str(tenant), candidate) is None):
            drawing_id = candidate
            break
    if drawing_id is None:  # pragma: no cover - 4 consecutive 40-bit collisions
        return error_response(ErrorCode.INTERNAL, "could not mint a drawing id",
                              retryable=True, status_code=500)

    # Effect order is load-bearing: (1) marker — so /upload-status and the
    # fail-closed bootstrap guard exist the instant the id is public; (2) stage
    # the bytes; (3) extraction thread against the staged file.
    marker = guest_uploads.new_marker(filename=file.filename or f"upload{ext}",
                                      data=data, tenant_kind=tenant_kind)
    guest_uploads.write_marker(backend, str(tenant), drawing_id, marker)

    staged = guest_uploads.staged_path(str(tenant), drawing_id, ext)
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(data)

    guest_uploads.start_extraction_thread(str(tenant), drawing_id, ext)

    guest_session = None
    if tenant_kind == "guest" and deps.auth_live():
        # Fresh token per upload: expiry tracks THIS upload's retention window,
        # so the guest can always read what still exists.
        import time as _time
        exp = int(_time.time() + guest_uploads.retention_hours() * 3600)
        guest_session = guest_uploads.mint_guest_session(str(tenant), exp)

    body = deps.tenant_echo({
        "drawing_id": drawing_id,
        "tenant_id": str(tenant),
        "tenant_kind": tenant_kind,
        "retention_expires_at": marker["retention_expires_at"],
        "guest_session": guest_session,
        "status": "extracting",
    }, tenant)
    return JSONResponse(status_code=202, content=with_envelope_fields(body))


@router.get("/api/drawings/{drawing_id}/upload-status")
def upload_status(drawing_id: str,
                  tenant=Depends(deps.require_tenant)) -> Any:
    """The upload's honest state, from the marker (the single source of upload
    truth). Guests reach this via their guest-session header (live) or the
    X-Tenant-Id stub (demo). 404 (never 403) for an unknown marker — same
    no-existence-leak posture as the jobs routes."""
    backend = write_loop.backend_for_tenant(
        str(tenant), aps_live=deps.APS_LIVE,
        da=deps.get_da_client() if deps.APS_LIVE else None)
    view = guest_uploads.status_view(backend, str(tenant), drawing_id)
    if view is None:
        return error_response(ErrorCode.BAD_PARAMS,
                              f"no upload known for drawing {drawing_id!r}",
                              retryable=False, status_code=404)
    return with_envelope_fields(deps.tenant_echo(view, tenant))
