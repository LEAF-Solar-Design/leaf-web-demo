"""POST /api/author — generate a tool, PERSIST its file, register it.

The generated `code` (the actual tool body) is written to a real file under
server/authored/ and `tool['entry']` is pointed at it, so the dynamic loader
runs THAT FILE instead of throwing the code away and re-dispatching to a
pre-coded primitive. An advisory static scan (SPEC §10.2) is attached to the
provenance + preview.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import traceback
from pathlib import Path
from typing import Any, Dict, Literal

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import author_quota
import deps
import entitlements
from customization_authority import AuthorityError, PublishRequest
from customization_models import ChangeSetNotFoundError
from customization_service import CustomizationServiceError, CustomizationService
from customization_flags import enabled as customization_enabled
from deps import fb
from envelopes import ErrorCode, error_obj, with_envelope_fields
from tenant_id_validator import is_valid_tenant_id
from tool_validate import static_scan

router = APIRouter()

_LOG = logging.getLogger(__name__)

SERVER_DIR = Path(__file__).resolve().parent.parent
AUTHORED_DIR = SERVER_DIR / "authored"

# App-proxy error name for "the tenant has not linked a Claude grant" (Concern-2).
# DELIBERATELY NOT added to the frozen §10 `error.error_code` enum — it is surfaced
# additively (top-level `grant_required: true` + `reason`) so the frontend can key on
# it while the §10 `error` object still carries a valid enum code (BAD_PARAMS). See
# CONTRACT-ADDENDUM §16.
GRANT_REQUIRED = "GRANT_REQUIRED"

# The single authoring-quota principal used when auth is off. An unauthenticated
# deployment has no verified tenant to meter — the id arrives in a request header
# the caller controls — so the open lane shares one budget rather than handing a
# fresh one to every id someone invents.
ANONYMOUS_QUOTA_PRINCIPAL = "_anonymous"

# Bound the tool-description so an unbounded body can't exhaust the author /
# templater / harness pipeline (security-audit F15). An over-cap body is rejected at
# the validation layer before any harness/template work runs.
MAX_AUTHOR_DESCRIPTION = 8_000
DEFAULT_AUTHOR_TIMEOUT_S = 120.0


class AuthorRequest(BaseModel):
    description: str = Field(..., max_length=MAX_AUTHOR_DESCRIPTION)
    mode: Literal["build", "one_off"] = "build"
    target_tool_name: str | None = Field(default=None, min_length=1, max_length=64)


class StageRequest(AuthorRequest):
    mode: str = Field(..., max_length=20)
    idempotency_key: str = Field(..., min_length=1, max_length=200)

    class Config:
        extra = "forbid"


class RegisterRequest(BaseModel):
    change_set_id: str
    staged_commit: str
    catalog_digest: str
    platform_release: str
    workspace_contract_digest: str
    confirmation_id: str
    idempotency_key: str = Field(..., min_length=1, max_length=200)

    class Config:
        extra = "forbid"


class PublicationRequest(BaseModel):
    change_set_id: str = Field(..., min_length=36, max_length=36)

    class Config:
        extra = "forbid"


class RollbackRequest(BaseModel):
    change_set_id: str
    idempotency_key: str = Field(..., min_length=1, max_length=200)

    class Config:
        extra = "forbid"


class InternalConfirmRequest(BaseModel):
    change_set_id: str

    class Config:
        extra = "forbid"


class InternalStagedRequest(BaseModel):
    receipt: Dict[str, Any]

    class Config:
        extra = "forbid"


class InternalAuthorizePublishRequest(InternalStagedRequest):
    expected_main_sha: str


class ConfirmationLookupRequest(BaseModel):
    contract: str
    tenant_id: str
    change_set_id: str
    state: str
    base_commit: str
    staged_commit: str
    catalog_digest: str
    platform_release: str
    workspace_contract_digest: str
    idempotency_key: str

    class Config:
        extra = "forbid"


def _grant_required_response(tenant_id: str, harness_message: str | None) -> JSONResponse:
    """§16 grant-required envelope (HTTP 401). The token is NEVER involved here — this
    only signals that the tenant must 'sign in with Claude' before authoring."""
    msg = harness_message or (
        f"tenant {tenant_id!r} has no linked Claude grant — sign in with Claude to author tools."
    )
    body = with_envelope_fields({
        "tool": None,
        "code": None,
        "preview": "Sign in with Claude to author tools — this workspace has no linked Claude grant yet.",
        "source": "grant_required",
        "grant_required": True,
        "reason": GRANT_REQUIRED,
        "static_scan": [],
        # §10-valid error object (frozen enum) so strict §10 consumers see an error;
        # the frontend keys on the additive `grant_required` / `reason` fields.
        "error": error_obj(GRANT_REQUIRED, msg, retryable=False),
    })
    return JSONResponse(status_code=401, content=body)


def _author_timeout_s() -> float:
    """Return a bounded synchronous harness budget for compatibility authoring."""
    try:
        value = float(os.environ.get(
            "LEAF_AUTHOR_TIMEOUT_S", str(DEFAULT_AUTHOR_TIMEOUT_S)
        ))
    except ValueError:
        value = DEFAULT_AUTHOR_TIMEOUT_S
    if not 5 <= value <= 900:
        return DEFAULT_AUTHOR_TIMEOUT_S
    return value


def _harness_unavailable_response(*, timed_out: bool) -> JSONResponse:
    code = ErrorCode.TIMEOUT if timed_out else ErrorCode.BROKER_UNREACHABLE
    status = 504 if timed_out else 502
    message = (
        "tool authoring exceeded its configured time budget"
        if timed_out
        else "the tool-authoring harness did not return a usable result"
    )
    return JSONResponse(status_code=status, content=with_envelope_fields({
        "tool": None,
        "code": None,
        "preview": message,
        "source": "harness_unavailable",
        "static_scan": [],
        "error": error_obj(code, message, retryable=True),
    }))


def _legacy_author(req: AuthorRequest, tenant) -> Dict[str, Any]:
    """Generate a tool package from a description, PERSIST its code to a real
    file, register it so it appears in /api/tools, and return
    {tool, code, preview} (contract section 4)."""
    # ENTITLEMENT GATE (§17): the Build lane requires the tenant tier's `build` capability.
    # Enforced FIRST — before the harness delegation or the templater — so a plan without
    # Build cannot author tools via either path. Off-auth/demo tier grants build.
    tier = entitlements.resolve_tier(tenant)
    roles, elevated = entitlements.resolve_roles(tenant)
    try:
        allowed = entitlements.entitlements_for(tier, roles, elevated).get("build", False)
    except entitlements.EntitlementsError:
        return entitlements.policy_unavailable_response("build", tier)
    if not allowed:
        return entitlements.entitlement_denied_response("build", tier)

    use_llm = os.environ.get("LEAF_AUTHOR_LLM", "0") == "1"

    # Env-gated seam to the Agent SDK author harness (harness/ - HARNESS-CONTRACT.md).
    # When LEAF_AUTHOR_HARNESS_URL is set, authoring is delegated to the harness, which
    # registers the tool into the TENANT repo (registry.json) itself. Any harness
    # failure falls back to the local templater (noted in the preview).
    tool = None
    source = "template"
    _telemetry = None  # A1: real authoring telemetry from the harness (harness path only)
    harness_url = os.environ.get("LEAF_AUTHOR_HARNESS_URL", "").rstrip("/")
    harness_failure: Exception | None = None
    if harness_url:
        # Contract 6: forward the RESOLVED tenant so the harness resolves THAT tenant's
        # grant + repo (per-tenant author loop). The token is never sent by the app —
        # the harness owns the per-tenant grant store.
        resp = None
        try:
            import requests
            import broker_client
            resp = requests.post(f"{harness_url}/author",
                                 json={"description": req.description,
                                       "tenant_id": str(tenant)},
                                 headers=broker_client.harness_headers(),
                                 timeout=_author_timeout_s())
        except Exception as exc:
            harness_failure = exc
            # Type only: a harness exception message can carry a credential.
            _LOG.warning(
                "author_harness_unreachable: templated fallback, cause=%s",
                type(exc).__name__,
            )
        if resp is not None:
            try:
                jb = resp.json()
            except ValueError:
                jb = None
            # Contract 3 / §16: a missing per-tenant grant is a DISTINCT outcome — the
            # user must sign in with Claude. Short-circuit to the GRANT_REQUIRED shape;
            # do NOT fall back to the templater (that would hide the real reason).
            if resp.status_code in (401, 403) and isinstance(jb, dict) and jb.get("grant_required"):
                harness_msg = (jb.get("error") or {}).get("message")
                return _grant_required_response(str(tenant), harness_msg)
            try:
                resp.raise_for_status()
                body = jb if jb is not None else resp.json()
                tool, code, preview = body["tool"], body["code"], body["preview"]
                _telemetry = body.get("telemetry")  # A1: forward real telemetry (was dropped)
                source = "harness"
            except Exception as exc:
                tool = None
                harness_failure = exc
                # Type only, for the same reason as above.
                _LOG.warning(
                    "author_harness_error: templated fallback, cause=%s",
                    type(exc).__name__,
                )
    if tool is None:
        if harness_url and os.environ.get(
            "LEAF_AUTHOR_TEMPLATE_FALLBACK", "1"
        ) != "1":
            try:
                import requests
                timed_out = isinstance(harness_failure, requests.Timeout)
            except ImportError:
                timed_out = False
            return _harness_unavailable_response(timed_out=timed_out)
        tool, code, preview = fb.author_tool(req.description)
        source = "template"
        if harness_url:
            preview = "[harness unreachable; templated fallback] " + preview
            # P2: the silent-degradation rate, previously invisible.
            _emit_author_event("author.fallback_served", tenant, {
                "reason": type(harness_failure).__name__ if harness_failure else "unknown"})
    if use_llm:
        # Real LLM authoring is optional and gated. No key wired here; we fall
        # back to templating and note it in the preview rather than fail.
        preview = "[LLM gate on, but no provider wired — templated] " + preview

    # advisory static scan (SPEC §10.2) — runs consistently for BOTH sources over the
    # tool's `code`; surfaced in provenance AND at the top level (Contract 3) + preview.
    findings = static_scan(code)
    tool.setdefault("provenance", {})["static_scan"] = findings
    if findings:
        preview = f"{preview}  [static-scan flags: {', '.join(findings)}]"

    if source == "harness":
        # The harness already registered this tool into the TENANT repo (its build
        # route commits registry.json + tools/<name>/). It surfaces in /api/tools via
        # deps.all_tools()'s tenant-repo fold (Contract 2) — NOT the local authored
        # store. So we do NOT persist to server/authored/ or touch _AUTHORED here; the
        # returned tool keeps its tenant-repo-relative entry.
        pass
    else:
        # TEMPLATE path: PERSIST the authored file + point the registry entry at
        # it (the FILE is the tool). entry is stored relative to server/ so the
        # broker resolves it regardless of cwd. Register into our lane's store.
        #
        # TENANT ISOLATION: the file path AND the store key must both be
        # tenant-scoped. A flat authored/<name>.py let tenant B overwrite tenant
        # A's file, and a dedup keyed on name alone evicted A's catalog entry, so
        # B could hijack A's tool by name. Write under a per-tenant subdirectory
        # and dedup on (tenant_id, name) so each tenant's authored tools are
        # physically and logically their own.
        tenant_id = str(tenant)
        # Collision-resistant per-tenant directory. A lossy character-substitution
        # slug is NOT injective ("tenant.a" and "tenant_a" would collide onto one
        # directory and re-open the cross-tenant overwrite), so derive the
        # directory name from a SHA-256 of the exact tenant id: distinct ids map
        # to distinct directories, and the hex output is inherently path-safe
        # (accepted by the loader's _is_unsafe_ref, never '.'/'..').
        tenant_dir_name = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:32]
        tenant_dir = AUTHORED_DIR / tenant_dir_name
        tenant_dir.mkdir(parents=True, exist_ok=True)
        fname = f"{tool['name']}.py"
        (tenant_dir / fname).write_text(code, encoding="utf-8")
        tool["entry"] = f"authored/{tenant_dir_name}/{fname}"
        # deps.all_tools() folds this global store last and only surfaces entries
        # whose tenant_id matches the requesting tenant. str(tenant) matches how
        # all_tools() is called by its consumers.
        tool["tenant_id"] = tenant_id
        deps._AUTHORED[:] = [
            t for t in deps._AUTHORED
            if not (t.get("tenant_id") == tenant_id and t.get("name") == tool["name"])
        ]
        deps._AUTHORED.append(tool)
        deps.save_authored_tools(deps._AUTHORED)

    return deps.tenant_echo(
        with_envelope_fields({"tool": tool, "code": code, "preview": preview,
                              "source": source, "static_scan": findings,
                              **({"telemetry": _telemetry} if _telemetry else {})}), tenant
    )


_CUSTOMIZATION_ERROR_MESSAGES = {
    "customization_stage_disabled": (
        "Tool authoring is not enabled for this workspace in this environment. "
        "The approved request was not executed."
    ),
    "customization_publish_disabled": (
        "Tool publication is not enabled for this workspace in this environment. "
        "The approved request was not executed."
    ),
    "invalid_stage_request": (
        "The requested authoring mode is not supported by the protected authoring path. "
        "Use build mode."
    ),
    "builder_entitlement_missing": (
        "This workspace does not have the builder entitlement required for tool authoring."
    ),
    "customization_harness_unavailable": (
        "Protected tool authoring is temporarily unavailable. "
        "The approved request was not executed."
    ),
    "idempotency_key_required": (
        "Tool authoring requires a request identity. The approved request was not executed."
    ),
    "tenant_identity_binding_unavailable": (
        "Tool authoring requires a verified workspace identity for the requester. "
        "The approved request was not executed."
    ),
    "stage_authority_invalid": (
        "Tool authoring requires the requester's active app turn. "
        "The approved request was not executed."
    ),
}


def _subject_scoped_key(idempotency_key: str, tenant: Any) -> str:
    """Bind an authoring idempotency key to the subject that requested it.

    The harness derives its key from tenant, session, description and mode
    (converseLoop.ts) — never the user, because it cannot know one. Two members
    of a tenant sharing a session and asking for the same tool would therefore
    present the same key, and the store rejects a second author_subject under
    one key as a replay conflict (customization_store.py). Scoping the key here
    gives each subject its own change set instead of a collision.
    """
    subject = getattr(tenant, "subject", None)
    if not subject:
        return idempotency_key
    digest = hashlib.sha256(
        json.dumps([idempotency_key, str(subject)], separators=(",", ":")).encode()
    ).hexdigest()
    return f"{idempotency_key}:{digest[:32]}"


# Refusals a caller can drive at will, or that merely restate a fixed deployment
# posture (auth off, a rollout rung disabled). They are demoted so an unauthorized
# caller cannot write one WARNING per request.
#
# The list is an allowlist on purpose: everything absent from it warns, including a
# detail-free 503. Keying the level on whether a detail existed looked tidy and was
# wrong in the other direction — a missing harness URL, an absent customization
# secret, an unset DATABASE_URL and an invalid platform release all raise
# detail-free 503s, and those are exactly what an operator must see. Default to
# visible; demote only what is provably caller-driven.
_CALLER_DRIVEN_CODES = frozenset({
    # Fixed deployment posture. Monitor these as configuration, not once per public
    # request: auth being off, or a rollout rung being closed, does not vary by call.
    "customization_auth_required",
    "customization_stage_disabled",
    "customization_publish_disabled",
    "customization_rollback_disabled",
    # Credentials or identity the caller supplied.
    "tenant_identity_invalid",
    "approval_authority_denied",
    "dispatch_authority_denied",
    "idempotency_key_required",
    "tenant_role_denied",
    "builder_entitlement_missing",
    # Ordinary request outcomes: a malformed or unsupported request, a lifecycle
    # state the caller polls for, a target that is not eligible. All expected.
    "invalid_stage_request",
    "invalid_publish_request",
    "invalid_publication_request",
    "stage_not_available",
    "publish_not_available",
    "publication_request_not_available",
    "publication_denial_not_available",
    "confirmation_not_available",
    "independent_approval_pending",
    "rollback_target_invalid",
    "staged_receipt_mismatch",
})

# Two codes cover opposite cases and split on status: the 4xx is the caller's, the
# 5xx is the deployment's or the trusted harness's.
_STATUS_SPLIT_CODES = frozenset({
    "tenant_identity_binding_unavailable",  # 403 caller binding, 503 authority down
    "invalid_staged_receipt",               # 422 caller receipt, 502 bad harness output
})


def _is_caller_driven(exc: CustomizationServiceError, *, from_authority: bool) -> bool:
    """Decide whether a refusal carries any per-request operator signal.

    Default is no: an unrecognised code warns, because the costly mistake is a
    silent deployment fault, not an extra log line. Integrity and trusted-harness
    failures are deliberately absent from the quiet set even when they answer 4xx.
    """
    if from_authority:
        # AuthorityError reason codes are per-caller authorization outcomes, and the
        # set is open (several are generated per field), so classify them by origin
        # rather than by enumerating names that will drift.
        return True
    if exc.code in _STATUS_SPLIT_CODES:
        return exc.status_code < 500
    return exc.code in _CALLER_DRIVEN_CODES


def _customization_error(
    exc: CustomizationServiceError, *, cause: BaseException | None = None,
    from_authority: bool = False,
) -> JSONResponse:
    """Answer with the opaque reason code, and record why in the log.

    The response deliberately tells a tenant nothing about the deployment, so
    this is the only place an operator learns the cause. `cause` is for the
    handlers that would otherwise discard an unexpected exception whole: pass it
    and the traceback is logged instead of lost.
    """
    if isinstance(cause, ChangeSetNotFoundError):
        # The change-set id comes straight from the request body, and
        # /api/author/confirmations loads it before checking the caller's
        # authority, so any tenant member can name arbitrary ids. That is a caller
        # error, not an operator event: it must not cost one ERROR per request.
        # The response is unchanged.
        _LOG.debug("customization_refused: code=%s cause=change_set_not_found", exc.code)
    elif cause is not None:
        # Location only: file, line and function, never `exc_info`, never
        # str(cause), and never the rendered source line. A traceback carries the
        # exception message and every chained cause, and format_tb additionally
        # echoes the source text of each frame, so a literal at the raise site
        # would be reproduced verbatim. Where it broke is the diagnostic; nothing
        # about the payload is.
        frames = " | ".join(
            f"{os.path.basename(f.filename)}:{f.lineno}:{f.name}"
            for f in traceback.extract_tb(cause.__traceback__)
        )
        _LOG.error(
            "customization_refused: code=%s cause=%s at %s",
            exc.code, type(cause).__name__, frames or "<no frames>",
        )
    elif _is_caller_driven(exc, from_authority=from_authority):
        # Reachable per request by a caller, and carrying no per-request operator
        # signal, so these must not cost a WARNING each.
        _LOG.debug("customization_refused: code=%s", exc.code)
    else:
        _LOG.warning("customization_refused: code=%s detail=%s", exc.code, exc.detail or "-")
    message = _CUSTOMIZATION_ERROR_MESSAGES.get(
        exc.code,
        "Protected tool authoring could not complete. The approved request was not executed.",
    )
    return JSONResponse(status_code=exc.status_code, content=with_envelope_fields({
        "error": error_obj("BAD_PARAMS", message, retryable=False),
        "reason_code": exc.code,
    }))


def _quota_exceeded_response(tenant_id: str,
                             exc: author_quota.AuthorQuotaExceeded) -> JSONResponse:
    """The 429 the daily authoring cap answers with, in the same wire shape the
    daily RUN quota already uses (only ``quota_kind`` differs).

    INFO, not WARNING and not DEBUG. A refusal is caller-driven and expected, so
    by the _is_caller_driven reasoning above it must not cost a WARNING each and
    must not page anyone. But DEBUG is where the other caller-driven refusals go
    precisely because they carry no signal, and this one does: it is the ONLY
    trace a refusal leaves. Deployments run at INFO, so a DEBUG line would keep
    the cap exactly as unobservable as it was — and "no quota events in the log
    group" would stay equally consistent with the cap never firing and with it
    refusing every attempt.

    Counter facts only: tenant, tier, limit, used and the UTC day the charge was
    keyed on. Never the description, the idempotency key, or anything else
    free-text from the request.
    """
    _LOG.info(
        "daily_author_quota_refused: tenant=%s tier=%s limit=%s used=%s day=%s",
        tenant_id, exc.tier, exc.limit, exc.used, exc.day,
    )
    usage = author_quota.usage_policy()
    body = usage.daily_author_envelope(tenant_id, exc.tier, exc.limit, exc.used)
    body["degraded_mode"] = False  # the wire schema requires a boolean
    return JSONResponse(status_code=429, content=body)


def _quota_store_error(exc: author_quota.AuthorQuotaStoreError) -> JSONResponse:
    """A configured quota that cannot be evaluated REFUSES (503). Failing open
    would silently restore the unbounded state this gate exists to end. Type only
    in the log: a store error message can carry a connection string, and the code
    is absent from _CALLER_DRIVEN_CODES on purpose, so this warns."""
    return _customization_error(CustomizationServiceError(
        "author_quota_unavailable", 503, f"cause={type(exc).__name__}"))


def _legacy_quota_denied(tenant: Any) -> JSONResponse | None:
    """Charge one attempt for the LEGACY (auth-off) authoring path.

    The R5 lane charges inside ``CustomizationService.stage``, immediately before
    the harness call, so nothing it refuses first can spend a slot. The legacy
    path has no such chokepoint — it calls the harness directly from
    ``_legacy_author`` — so it is metered here, after its own gates.

    With auth off the tenant id is an unverified request header, not a principal:
    a caller can mint a new one per request and hand itself a fresh budget
    forever. Such a deployment has no authenticated identity to meter, so the
    whole open lane shares ONE anonymous budget.
    """
    tenant_id = ANONYMOUS_QUOTA_PRINCIPAL if not deps.auth_live() else str(tenant)
    try:
        tier = entitlements.resolve_tier(tenant)
    except Exception:  # noqa: BLE001 - the tier is display-only in this envelope
        tier = "unknown"
    # Do not spend a slot on a request the Build entitlement is about to refuse
    # inside _legacy_author. This is NOT a second gate — that one still runs and
    # is what denies — it only keeps a deterministic 403 from consuming the
    # budget. An unreadable policy charges anyway: on the money side the safe
    # direction is to count.
    try:
        roles, elevated = entitlements.resolve_roles(tenant)
        if not entitlements.entitlements_for(tier, roles, elevated).get("build", False):
            return None
    except entitlements.EntitlementsError:
        pass
    try:
        author_quota.enforce(tenant_id, tier)
    except author_quota.AuthorQuotaExceeded as exc:
        return _quota_exceeded_response(tenant_id, exc)
    except author_quota.AuthorQuotaStoreError as exc:
        return _quota_store_error(exc)
    return None


def _customization_gate(wave: int, tenant: Any) -> JSONResponse | None:
    """Reject unsafe or disabled lifecycle calls before opening SQLite."""
    if not deps.auth_live():
        return _customization_error(
            CustomizationServiceError("customization_auth_required", 503)
        )
    tenant_id = str(tenant).strip()
    if not is_valid_tenant_id(tenant_id):
        return _customization_error(
            CustomizationServiceError("tenant_identity_invalid", 403)
        )
    if not customization_enabled(wave, tenant_id):
        code = (
            "customization_stage_disabled"
            if wave == 5
            else "customization_publish_disabled"
        )
        return _customization_error(CustomizationServiceError(code, 404))
    return None


def _emit_author_event(name: str, tenant, labels=None) -> None:
    """Best-effort author-lane product event (P2): Build is the
    differentiator, so its funnel (requested -> staged -> published) and its
    walls get identity-attached events. NEVER raises; never carries
    descriptions or code, only lengths/modes/reasons."""
    try:
        import telemetry_sink

        tid = str(tenant)
        telemetry_sink.emit(
            name,
            tenant_id=tid,
            tenant_kind="guest" if tid.startswith("guest-") else "account",
            session_id="server",
            labels=labels,
        )
    except Exception:  # noqa: BLE001 - telemetry never touches the author flow
        pass


@router.post("/api/author")
def author(req: AuthorRequest, tenant=Depends(deps.require_tenant),
           idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
           authority_session_id: str | None = Header(
               default=None, alias="X-Authority-Session-Id"),
           authority_turn_id: str | None = Header(
               default=None, alias="X-Authority-Turn-Id")) -> Dict[str, Any]:
    """Use the controlled R5 path only after that tenant's rollout is enabled."""
    authority_session_id = (
        authority_session_id if isinstance(authority_session_id, str) else None
    )
    authority_turn_id = (
        authority_turn_id if isinstance(authority_turn_id, str) else None
    )
    if bool(authority_session_id) != bool(authority_turn_id):
        return _customization_error(
            CustomizationServiceError("invalid_stage_authority", 422)
        )
    _emit_author_event("author.requested", tenant, {
        "mode": req.mode, "desc_len": len(req.description or "")})
    if not deps.auth_live() and not customization_enabled(5, str(tenant).strip()):
        # The legacy path also delegates to the authoring harness when
        # LEAF_AUTHOR_HARNESS_URL is set, so it is metered by the same cap.
        over_quota = _legacy_quota_denied(tenant)
        if over_quota is not None:
            return over_quota
        return _legacy_author(req, tenant)
    if not customization_enabled(5, str(tenant).strip()):
        denied = _customization_gate(5, tenant)
        if denied is not None:
            return denied
    # A harness back-edge call authenticates as a tenant and carries no user.
    # Resolve the author from the app's own record of the turn that authorized
    # it; a direct user call is returned unchanged and keeps its own subject.
    tenant = deps.stage_author_identity(
        tenant, authority_session_id, authority_turn_id)
    if tenant is None:
        return _customization_error(
            CustomizationServiceError("stage_authority_invalid", 409)
        )
    denied = _customization_gate(5, tenant)
    if denied is not None:
        _emit_author_event("author.wall_hit", tenant, {"wall_kind": "entitlement"})
        return denied
    if not idempotency_key or not idempotency_key.strip():
        return _customization_error(
            CustomizationServiceError("idempotency_key_required", 422)
        )
    try:
        service = CustomizationService.configured()
        enqueue = getattr(service, "enqueue_stage", service.stage)
        result = enqueue(
            tenant=tenant, description=req.description, mode=req.mode,
            idempotency_key=_subject_scoped_key(idempotency_key.strip(), tenant),
            **({
                "authority_session_id": authority_session_id,
                "authority_turn_id": authority_turn_id,
            } if authority_session_id and authority_turn_id else {}),
            **({"target_tool_name": req.target_tool_name}
               if req.target_tool_name else {}),
        )
        if result.get("contract") == "leaf.customization-stage-job.v1":
            return JSONResponse(status_code=202, content=result)
        return result
    # The daily authoring cap is charged inside stage(), immediately before the
    # harness call, so everything it refuses first costs nothing.
    except author_quota.AuthorQuotaExceeded as exc:
        _emit_author_event("author.wall_hit", tenant, {"wall_kind": "daily_quota"})
        return _quota_exceeded_response(str(tenant), exc)
    except author_quota.AuthorQuotaStoreError as exc:
        return _quota_store_error(exc)
    except (CustomizationServiceError, AuthorityError) as exc:
        if isinstance(exc, AuthorityError):
            return _customization_error(
                CustomizationServiceError(exc.reason_code, 403), from_authority=True
            )
        return _customization_error(exc)
    except Exception as exc:
        return _customization_error(
            CustomizationServiceError("customization_stage_failed", 503), cause=exc
        )


@router.post("/api/author/stage")
def stage(
    req: StageRequest,
    tenant=Depends(deps.require_tenant),
    authority_session_id: str | None = Header(
        default=None, alias="X-Authority-Session-Id"
    ),
    authority_turn_id: str | None = Header(
        default=None, alias="X-Authority-Turn-Id"
    ),
) -> Dict[str, Any]:
    authority_session_id = (
        authority_session_id if isinstance(authority_session_id, str) else None
    )
    authority_turn_id = (
        authority_turn_id if isinstance(authority_turn_id, str) else None
    )
    if bool(authority_session_id) != bool(authority_turn_id):
        return _customization_error(
            CustomizationServiceError("invalid_stage_authority", 422)
        )
    denied = _customization_gate(5, tenant)
    if denied is not None:
        return denied
    tenant = deps.stage_author_identity(
        tenant, authority_session_id, authority_turn_id
    )
    if tenant is None:
        return _customization_error(
            CustomizationServiceError("stage_authority_invalid", 409)
        )
    try:
        service = CustomizationService.configured()
        enqueue = getattr(service, "enqueue_stage", service.stage)
        result = enqueue(
            tenant=tenant, description=req.description, mode=req.mode, idempotency_key=req.idempotency_key,
            **({
                "authority_session_id": authority_session_id,
                "authority_turn_id": authority_turn_id,
            } if authority_session_id and authority_turn_id else {}),
            **({"target_tool_name": req.target_tool_name}
               if req.target_tool_name else {}),
        )
        if result.get("contract") == "leaf.customization-stage-job.v1":
            return JSONResponse(status_code=202, content=result)
        return result
    # Same cap as /api/author: both routes reach the harness through stage(),
    # which is where the charge lives, so neither can bypass it via the other.
    except author_quota.AuthorQuotaExceeded as exc:
        return _quota_exceeded_response(str(tenant), exc)
    except author_quota.AuthorQuotaStoreError as exc:
        return _quota_store_error(exc)
    except (CustomizationServiceError, AuthorityError) as exc:
        if isinstance(exc, AuthorityError):
            return _customization_error(
                CustomizationServiceError(exc.reason_code, 403), from_authority=True
            )
        return _customization_error(exc)
    except Exception as exc:
        return _customization_error(
            CustomizationServiceError("customization_stage_failed", 503), cause=exc
        )


@router.get("/api/author/stages/{change_set_id}")
def stage_status(
    change_set_id: str, tenant=Depends(deps.require_tenant)
) -> Dict[str, Any]:
    denied = _customization_gate(5, tenant)
    if denied is not None:
        return denied
    try:
        return CustomizationService.configured().stage_status(
            tenant=tenant, change_set_id=change_set_id
        )
    except ChangeSetNotFoundError:
        return _customization_error(
            CustomizationServiceError("stage_not_available", 404)
        )
    except (CustomizationServiceError, AuthorityError) as exc:
        if isinstance(exc, AuthorityError):
            return _customization_error(
                CustomizationServiceError(exc.reason_code, 403), from_authority=True
            )
        return _customization_error(exc)
    except Exception as exc:
        return _customization_error(
            CustomizationServiceError("customization_stage_failed", 503), cause=exc
        )


@router.post("/api/author/register")
def register(req: RegisterRequest, tenant=Depends(deps.require_tenant)) -> Dict[str, Any]:
    denied = _customization_gate(6, tenant)
    if denied is not None:
        return denied
    try:
        result = CustomizationService.configured().publish(
            tenant=tenant,
            request=PublishRequest(req.change_set_id, req.staged_commit, req.catalog_digest,
                                   req.platform_release, req.workspace_contract_digest),
            confirmation_id=req.confirmation_id, idempotency_key=req.idempotency_key,
        )
        # P2 KEY milestone: a user-made tool exists. Emitted only on the
        # service's successful return; every refusal raises past this line.
        _emit_author_event("author.published", tenant, {
            "change_set_id": req.change_set_id})
        return result
    except (CustomizationServiceError, AuthorityError, ValueError) as exc:
        if isinstance(exc, CustomizationServiceError):
            return _customization_error(exc)
        if isinstance(exc, AuthorityError):
            return _customization_error(
                CustomizationServiceError(exc.reason_code, 403), from_authority=True
            )
        return _customization_error(CustomizationServiceError("invalid_publish_request", 422))


@router.post("/api/author/publication-requests")
def request_publication(
    req: PublicationRequest,
    tenant=Depends(deps.require_tenant),
) -> Dict[str, Any]:
    """Request continuation only. Independent approval stays off this route."""
    denied = _customization_gate(6, tenant)
    if denied is not None:
        return denied
    try:
        return CustomizationService.configured().request_publication(
            tenant=tenant, change_set_id=req.change_set_id
        )
    except (CustomizationServiceError, AuthorityError, ValueError) as exc:
        if isinstance(exc, CustomizationServiceError):
            return _customization_error(exc)
        if isinstance(exc, AuthorityError):
            return _customization_error(
                CustomizationServiceError(exc.reason_code, 403), from_authority=True
            )
        return _customization_error(
            CustomizationServiceError("invalid_publication_request", 422)
        )
    except Exception as exc:
        return _customization_error(
            CustomizationServiceError("customization_publish_failed", 503), cause=exc
        )


@router.post("/api/author/confirmations")
def confirmation_lookup(
    req: ConfirmationLookupRequest,
    tenant=Depends(deps.require_tenant),
) -> Dict[str, Any]:
    """Return only an approval that an independent trusted actor already issued."""
    denied = _customization_gate(6, tenant)
    if denied is not None:
        return denied
    try:
        return CustomizationService.configured().pending_confirmation(
            tenant=tenant, receipt=req.dict()
        )
    except (CustomizationServiceError, AuthorityError) as exc:
        if isinstance(exc, AuthorityError):
            return _customization_error(
                CustomizationServiceError(exc.reason_code, 403), from_authority=True
            )
        return _customization_error(exc)
    except Exception as exc:
        return _customization_error(
            CustomizationServiceError("customization_publish_failed", 503), cause=exc
        )


@router.post("/api/author/rollback")
def rollback(req: RollbackRequest, tenant=Depends(deps.require_tenant)) -> Dict[str, Any]:
    denied = _customization_gate(6, tenant)
    if denied is not None:
        return denied
    try:
        result = CustomizationService.configured().rollback(
            tenant=tenant, change_set_id=req.change_set_id, idempotency_key=req.idempotency_key,
        )
        # P2: regret rate / generated-tool quality.
        _emit_author_event("author.rolled_back", tenant, {
            "change_set_id": req.change_set_id})
        return result
    except (CustomizationServiceError, AuthorityError) as exc:
        if isinstance(exc, AuthorityError):
            return _customization_error(
                CustomizationServiceError(exc.reason_code, 403), from_authority=True
            )
        return _customization_error(exc)
    except Exception as exc:
        return _customization_error(
            CustomizationServiceError("customization_rollback_failed", 503), cause=exc
        )


@router.post("/internal/customization/confirm")
def confirm(req: InternalConfirmRequest, x_tenant_id: str | None = Header(default=None),
            x_approval_secret: str | None = Header(default=None)) -> Dict[str, Any]:
    """Independent approval endpoint. The authoring harness never gets this credential."""
    approval_secret = os.environ.get(
        "LEAF_CUSTOMIZATION_APPROVAL_SECRET", ""
    ).strip()
    try:
        secrets_match = hmac.compare_digest(
            (x_approval_secret or "").encode("ascii"),
            approval_secret.encode("ascii"),
        )
    except UnicodeEncodeError:
        secrets_match = False
    if (
        not x_tenant_id
        or not approval_secret
        or not x_approval_secret
        or not secrets_match
    ):
        return _customization_error(CustomizationServiceError("approval_authority_denied", 403))
    denied = _customization_gate(6, x_tenant_id)
    if denied is not None:
        return denied
    try:
        return CustomizationService.configured().confirm(tenant_id=x_tenant_id, change_set_id=req.change_set_id)
    except (CustomizationServiceError, AuthorityError) as exc:
        if isinstance(exc, AuthorityError):
            return _customization_error(
                CustomizationServiceError(exc.reason_code, 403), from_authority=True
            )
        return _customization_error(exc)


@router.post("/internal/customization/deny")
def deny_publication(
    req: InternalConfirmRequest,
    x_tenant_id: str | None = Header(default=None),
    x_approval_secret: str | None = Header(default=None),
) -> Dict[str, Any]:
    """Independent denial endpoint. The harness cannot reach this route."""
    approval_secret = os.environ.get(
        "LEAF_CUSTOMIZATION_APPROVAL_SECRET", ""
    ).strip()
    try:
        secrets_match = hmac.compare_digest(
            (x_approval_secret or "").encode("ascii"),
            approval_secret.encode("ascii"),
        )
    except UnicodeEncodeError:
        secrets_match = False
    if not x_tenant_id or not approval_secret or not secrets_match:
        return _customization_error(
            CustomizationServiceError("approval_authority_denied", 403)
        )
    denied = _customization_gate(6, x_tenant_id)
    if denied is not None:
        return denied
    try:
        return CustomizationService.configured().deny_publication(
            tenant_id=x_tenant_id, change_set_id=req.change_set_id
        )
    except (CustomizationServiceError, AuthorityError) as exc:
        if isinstance(exc, AuthorityError):
            return _customization_error(
                CustomizationServiceError(exc.reason_code, 403), from_authority=True
            )
        return _customization_error(exc)


def _require_dispatch(tenant_id: str | None, secret: str | None) -> str:
    if not tenant_id or not deps._dispatch_secret_ok(secret):
        raise CustomizationServiceError("dispatch_authority_denied", 403)
    return tenant_id


@router.post("/internal/customization/staged")
def customization_staged(
    req: InternalStagedRequest,
    x_tenant_id: str | None = Header(default=None),
    x_dispatch_secret: str | None = Header(default=None),
) -> Dict[str, Any]:
    try:
        tenant_id = _require_dispatch(x_tenant_id, x_dispatch_secret)
        denied = _customization_gate(5, tenant_id)
        if denied is not None:
            return denied
        result = CustomizationService.configured().record_staged_callback(
            tenant_id=tenant_id,
            receipt=req.receipt,
        )
        # P2: requested -> STAGED -> published funnel middle. The harness's
        # staged callback is the one place a successful stage lands.
        _emit_author_event("author.staged", tenant_id, {"source": "harness"})
        return result
    except (CustomizationServiceError, AuthorityError) as exc:
        if isinstance(exc, AuthorityError):
            return _customization_error(
                CustomizationServiceError(exc.reason_code, 403), from_authority=True
            )
        return _customization_error(exc)


@router.post("/internal/customization/authorize-publish")
def customization_authorize_publish(
    req: InternalAuthorizePublishRequest,
    x_tenant_id: str | None = Header(default=None),
    x_dispatch_secret: str | None = Header(default=None),
) -> Dict[str, Any]:
    try:
        tenant_id = _require_dispatch(x_tenant_id, x_dispatch_secret)
        denied = _customization_gate(6, tenant_id)
        if denied is not None:
            return denied
        return CustomizationService.configured().authorize_publish_callback(
            tenant_id=tenant_id,
            receipt=req.receipt,
            expected_main_sha=req.expected_main_sha,
        )
    except (CustomizationServiceError, AuthorityError) as exc:
        if isinstance(exc, AuthorityError):
            return _customization_error(
                CustomizationServiceError(exc.reason_code, 403), from_authority=True
            )
        return _customization_error(exc)
    except Exception as exc:
        return _customization_error(
            CustomizationServiceError("customization_confirmation_failed", 503), cause=exc
        )
