"""POST /api/author — generate a tool, PERSIST its file, register it.

The generated `code` (the actual tool body) is written to a real file under
server/authored/ and `tool['entry']` is pointed at it, so the dynamic loader
runs THAT FILE instead of throwing the code away and re-dispatching to a
pre-coded primitive. An advisory static scan (SPEC §10.2) is attached to the
provenance + preview.
"""
from __future__ import annotations

import hmac
import os
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import deps
import entitlements
from customization_authority import AuthorityError, PublishRequest
from customization_service import CustomizationServiceError, CustomizationService
from customization_flags import enabled as customization_enabled
from deps import fb
from envelopes import error_obj, with_envelope_fields
from tenant_id_validator import is_valid_tenant_id
from tool_validate import static_scan

router = APIRouter()

SERVER_DIR = Path(__file__).resolve().parent.parent
AUTHORED_DIR = SERVER_DIR / "authored"

# App-proxy error name for "the tenant has not linked a Claude grant" (Concern-2).
# DELIBERATELY NOT added to the frozen §10 `error.error_code` enum — it is surfaced
# additively (top-level `grant_required: true` + `reason`) so the frontend can key on
# it while the §10 `error` object still carries a valid enum code (BAD_PARAMS). See
# CONTRACT-ADDENDUM §16.
GRANT_REQUIRED = "GRANT_REQUIRED"

# Bound the tool-description so an unbounded body can't exhaust the author /
# templater / harness pipeline (security-audit F15). An over-cap body is rejected at
# the validation layer before any harness/template work runs.
MAX_AUTHOR_DESCRIPTION = 8_000


class AuthorRequest(BaseModel):
    description: str = Field(..., max_length=MAX_AUTHOR_DESCRIPTION)


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


def _legacy_author(req: AuthorRequest, tenant) -> Dict[str, Any]:
    """Generate a tool package from a description, PERSIST its code to a real
    file, register it so it appears in /api/tools, and return
    {tool, code, preview} (contract section 4)."""
    # ENTITLEMENT GATE (§17): the Build lane requires the tenant tier's `build` capability.
    # Enforced FIRST — before the harness delegation or the templater — so a plan without
    # Build cannot author tools via either path. Off-auth/demo tier grants build.
    tier = entitlements.resolve_tier(tenant)
    try:
        allowed = entitlements.entitlements_for(tier).get("build", False)
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
                                 headers=broker_client.harness_headers(), timeout=120)
        except Exception as exc:
            print(f"[author] harness unreachable, templated fallback: {exc}")
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
                print(f"[author] harness error, templated fallback: {exc}")
    if tool is None:
        tool, code, preview = fb.author_tool(req.description)
        source = "template"
        if harness_url:
            preview = "[harness unreachable; templated fallback] " + preview
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
        # TEMPLATE path (unchanged): PERSIST the authored file + point the registry
        # entry at it (the FILE is the tool). entry is stored relative to server/ so
        # the broker resolves it regardless of cwd. Register into our lane's store.
        AUTHORED_DIR.mkdir(parents=True, exist_ok=True)
        fname = f"{tool['name']}.py"
        (AUTHORED_DIR / fname).write_text(code, encoding="utf-8")
        tool["entry"] = f"authored/{fname}"
        deps._AUTHORED[:] = [t for t in deps._AUTHORED if t["name"] != tool["name"]]
        deps._AUTHORED.append(tool)
        deps.save_authored_tools(deps._AUTHORED)

    return deps.tenant_echo(
        with_envelope_fields({"tool": tool, "code": code, "preview": preview,
                              "source": source, "static_scan": findings,
                              **({"telemetry": _telemetry} if _telemetry else {})}), tenant
    )


def _customization_error(exc: CustomizationServiceError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=with_envelope_fields({
        "error": error_obj("BAD_PARAMS", "customization request was refused", retryable=False),
        "reason_code": exc.code,
    }))


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


@router.post("/api/author")
def author(req: AuthorRequest, tenant=Depends(deps.require_tenant),
           idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> Dict[str, Any]:
    """Use the controlled R5 path only after that tenant's rollout is enabled."""
    if not deps.auth_live() and not customization_enabled(5, str(tenant).strip()):
        return _legacy_author(req, tenant)
    denied = _customization_gate(5, tenant)
    if denied is not None:
        return denied
    try:
        return CustomizationService.configured().stage(
            tenant=tenant, description=req.description, mode="build",
            idempotency_key=idempotency_key or str(__import__("uuid").uuid4()),
        )
    except (CustomizationServiceError, AuthorityError) as exc:
        if isinstance(exc, AuthorityError):
            return _customization_error(CustomizationServiceError(exc.reason_code, 403))
        return _customization_error(exc)
    except Exception:
        return _customization_error(CustomizationServiceError("customization_stage_failed", 503))


@router.post("/api/author/stage")
def stage(req: StageRequest, tenant=Depends(deps.require_tenant)) -> Dict[str, Any]:
    denied = _customization_gate(5, tenant)
    if denied is not None:
        return denied
    try:
        return CustomizationService.configured().stage(
            tenant=tenant, description=req.description, mode=req.mode, idempotency_key=req.idempotency_key,
        )
    except (CustomizationServiceError, AuthorityError) as exc:
        if isinstance(exc, AuthorityError):
            return _customization_error(CustomizationServiceError(exc.reason_code, 403))
        return _customization_error(exc)
    except Exception:
        return _customization_error(CustomizationServiceError("customization_stage_failed", 503))


@router.post("/api/author/register")
def register(req: RegisterRequest, tenant=Depends(deps.require_tenant)) -> Dict[str, Any]:
    denied = _customization_gate(6, tenant)
    if denied is not None:
        return denied
    try:
        return CustomizationService.configured().publish(
            tenant=tenant,
            request=PublishRequest(req.change_set_id, req.staged_commit, req.catalog_digest,
                                   req.platform_release, req.workspace_contract_digest),
            confirmation_id=req.confirmation_id, idempotency_key=req.idempotency_key,
        )
    except (CustomizationServiceError, AuthorityError, ValueError) as exc:
        if isinstance(exc, CustomizationServiceError):
            return _customization_error(exc)
        if isinstance(exc, AuthorityError):
            return _customization_error(CustomizationServiceError(exc.reason_code, 403))
        return _customization_error(CustomizationServiceError("invalid_publish_request", 422))


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
            return _customization_error(CustomizationServiceError(exc.reason_code, 403))
        return _customization_error(exc)
    except Exception:
        return _customization_error(CustomizationServiceError("customization_publish_failed", 503))


@router.post("/api/author/rollback")
def rollback(req: RollbackRequest, tenant=Depends(deps.require_tenant)) -> Dict[str, Any]:
    denied = _customization_gate(6, tenant)
    if denied is not None:
        return denied
    try:
        return CustomizationService.configured().rollback(
            tenant=tenant, change_set_id=req.change_set_id, idempotency_key=req.idempotency_key,
        )
    except (CustomizationServiceError, AuthorityError) as exc:
        if isinstance(exc, AuthorityError):
            return _customization_error(CustomizationServiceError(exc.reason_code, 403))
        return _customization_error(exc)
    except Exception:
        return _customization_error(CustomizationServiceError("customization_rollback_failed", 503))


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
            return _customization_error(CustomizationServiceError(exc.reason_code, 403))
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
        return CustomizationService.configured().record_staged_callback(
            tenant_id=tenant_id,
            receipt=req.receipt,
        )
    except (CustomizationServiceError, AuthorityError) as exc:
        if isinstance(exc, AuthorityError):
            return _customization_error(CustomizationServiceError(exc.reason_code, 403))
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
            return _customization_error(CustomizationServiceError(exc.reason_code, 403))
        return _customization_error(exc)
    except Exception:
        return _customization_error(CustomizationServiceError("customization_confirmation_failed", 503))
