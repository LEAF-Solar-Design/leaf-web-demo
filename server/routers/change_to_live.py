"""The change-to-live pair: what class of change this is, and what receipts exist.

``GET /api/change-class?repo=...&paths=...`` classifies a change set by SHAPE
(``server/change_classifier.py``). ``GET /api/receipts?scope=...`` reads the
delivery receipts that actually exist (``server/receipts_read.py``).

WHO MAY ASK, scope by scope. Both routes sit behind ``deps.require_tenant``,
but authentication is NOT the whole gate, because these two endpoints answer
about different things:

``/api/change-class``   a pure function of the caller's own path strings and
                        the server's release vocabulary. It reads no tenant
                        data and no platform data, so tenant authentication is
                        the whole requirement.
``/api/receipts``       ``job:`` is TENANT data, bound to the calling tenant by
                        the same audited guard ``GET /api/jobs/{id}`` uses.
                        ``pr:``, ``tree:`` and ``train`` are NOT: they read the
                        platform's PRIVATE repository CI state -- run ids, head
                        SHAs, artifact names, run URLs, and the operator's
                        product-progress feed -- with the PLATFORM's own
                        credential. Serving those on tenant authentication
                        alone would hand any signed-in account of any tier an
                        existence oracle over a repository it has no access to,
                        so ``_platform_scope_gate`` runs the SAME admission
                        chain ``platform_customize._gate`` runs for that other
                        reader of this data class: live auth (a dark platform
                        is dark here too, not just there), the
                        ``platform_customize`` entitlement, and the R7
                        internal-rollout allowlist -- reused rather than
                        re-derived, so the two gates cannot drift apart.

Both are BOUNDED and FAIL CLOSED: a malformed request is a 422 naming the rule
it broke, never a best-effort answer. Neither endpoint accepts a caller-supplied
credential; the receipts reader uses the platform's own server-side GitHub
credential and nothing else, and no token value can appear in any response
these routes produce.

Neither route mutates anything. ``/api/change-class`` is pure. ``/api/receipts``
performs bounded reads of artifacts that other systems minted; it never mints,
retries, or schedules one.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

import change_classifier
import deps
import entitlements
import receipts_read
from customization_flags import enabled as customization_enabled
from envelopes import ErrorCode, error_response, with_envelope_fields

router = APIRouter()

# The capability the platform-scoped receipt reads require. Deliberately the
# EXISTING platform_customize capability rather than a new one: it is already
# operator-granted on the admin tier alone (entitlements.py), already the gate
# on /api/platform/source for this same data class, and a new capability name
# would default to False on every tier until an operator edited the policy file,
# which is a silent 403 nobody asked for rather than a decision.
RECEIPTS_PLATFORM_CAPABILITY = "platform_customize"

# A query string wider than this is not a change set anybody reads a class for.
# Checked BEFORE the list is expanded, so a hostile caller cannot make the
# server allocate the expansion of a huge comma-joined value.
_MAX_QUERY_ITEMS = 100
_MAX_QUERY_CHARS = 8000


def _expand_paths(raw: List[str]) -> List[str]:
    """``?paths=a,b&paths=c`` -> ``[a, b, c]``, bounded at every step."""
    if len(raw) > _MAX_QUERY_ITEMS:
        raise change_classifier.ChangeClassifierError(
            f"at most {_MAX_QUERY_ITEMS} paths query parameters may be supplied"
        )
    if sum(len(item) for item in raw) > _MAX_QUERY_CHARS:
        raise change_classifier.ChangeClassifierError(
            f"the paths query may be at most {_MAX_QUERY_CHARS} characters"
        )
    out: List[str] = []
    for item in raw:
        for piece in item.split(","):
            piece = piece.strip()
            if piece:
                out.append(piece)
            if len(out) > change_classifier.MAX_PATHS:
                raise change_classifier.ChangeClassifierError(
                    f"at most {change_classifier.MAX_PATHS} paths may be classified"
                )
    return out


def _platform_scope_gate(tenant: Any) -> Optional[JSONResponse]:
    """``None`` when this caller may read PLATFORM CI state, else the refusal.

    Runs the same chain ``platform_customize._gate`` runs for the other reader
    of this data class, reusing its own refusal builder for the two checks
    that live there and not here (round-2 finding 5), so the two gates cannot
    quietly drift apart:

      1. Live auth. ``deps.auth_live()`` false means the whole platform-CI
         surface is dark, not just the W14 self-edit lane -- the same 503
         either gate gives.
      2. The ``platform_customize`` entitlement (below): an unreadable policy
         is a 503, a missing capability a 403, and the auth-off ``demo`` tier
         does not carry it, so an unauthenticated deployment is dark here too.
      3. The R7 internal-rollout allowlist. This capability is granted only to
         the operator-provisioned ``admin`` tier, but the allowlist is the
         second factor that keeps an admin-tier account from defaulting into
         this read the moment a deployment ships that tier, on a rollout an
         operator has not yet turned on for it.

    Fails closed at every step, in that order.
    """
    from routers import platform_customize as platform_customize_router  # noqa: PLC0415 - lazy, avoids an import cycle

    if not deps.auth_live():
        return platform_customize_router._error(
            platform_customize_router.lane.PlatformCustomizeError(
                "platform_customize_auth_required", 503))

    tier = entitlements.resolve_tier(tenant)
    roles, elevated = entitlements.resolve_roles(tenant)
    try:
        caps = entitlements.entitlements_for(tier, roles, elevated)
    except entitlements.EntitlementsError:
        return entitlements.policy_unavailable_response(
            RECEIPTS_PLATFORM_CAPABILITY, tier)
    if caps.get(RECEIPTS_PLATFORM_CAPABILITY) is not True:
        return entitlements.entitlement_denied_response(
            RECEIPTS_PLATFORM_CAPABILITY, tier)

    if not customization_enabled(7, str(tenant).strip()):
        return platform_customize_router._error(
            platform_customize_router.lane.PlatformCustomizeError(
                "platform_customize_disabled", 404))
    return None


@router.get("/api/change-class")
def change_class(
    repo: str = Query(...),
    paths: List[str] = Query(default=[]),
    kind: Optional[str] = Query(default=None),
    tenant=Depends(deps.require_tenant),
) -> Any:
    """Which delivery ladder a change of this shape rides, in ONE repository.

    HONEST DATA, NOT A PROMISE. The class describes the SHAPE of the requested
    change; it does not reserve a relay slot, predict a queue, or commit that
    the change will land at all. See ``change_classifier``'s module docstring
    for the full statement, which this endpoint deliberately does not restate
    in a way a caller could mistake for a service-level guarantee.

    ``repo`` is REQUIRED (``tenant`` or ``platform``) and has no default: the
    two repositories share path shapes (``tools/**`` is a tenant fold rule AND a
    real platform build input), so one flat path space cannot answer both
    honestly. A missing or unknown value is a 422, never a guess.
    """
    try:
        expanded = _expand_paths(list(paths))
        result = change_classifier.classify_change(expanded, kind, repo=repo)
    except change_classifier.ChangeClassifierError as exc:
        return error_response(
            ErrorCode.BAD_PARAMS, str(exc), retryable=False, status_code=422
        )
    body: Dict[str, Any] = {
        "contract": change_classifier.CONTRACT,
        "paths_considered": len(expanded),
        **result,
    }
    return deps.tenant_echo(with_envelope_fields(body), tenant)


@router.get("/api/receipts")
def receipts(
    scope: str = Query(...),
    tenant=Depends(deps.require_tenant),
) -> Any:
    """Every delivery receipt that exists for one scope, newest first.

    ``scope`` is ``pr:<n>``, ``tree:<sha>``, ``job:<id>`` or ``train``.

    TWO DIFFERENT ADMISSIONS, because these are two different data classes. A
    malformed scope refuses first and earliest of all, with a 422 that never
    reaches either admission. A well-formed ``job:`` scope is TENANT data: it
    is checked against the CALLING tenant and answers 404 for an unknown job
    and for another tenant's job alike -- the same no-existence-leak rule
    ``GET /api/jobs/{id}`` follows. A well-formed ``pr:``, ``tree:`` or
    ``train`` scope reads the PLATFORM's private repository with the
    platform's own credential, so once parsing has told us it is not a
    ``job:`` scope it must clear ``_platform_scope_gate`` (live auth, the
    ``platform_customize`` entitlement, the R7 rollout) before any outbound
    call is made, so the endpoint is not an existence oracle for a repository
    the caller cannot see.
    """
    try:
        kind, value = receipts_read.parse_scope(scope)
    except receipts_read.ReceiptsError as exc:
        return error_response(
            ErrorCode.BAD_PARAMS, str(exc), retryable=False, status_code=422
        )

    if kind != "job":
        refusal = _platform_scope_gate(tenant)
        if refusal is not None:
            return refusal

    if kind == "job":
        # Reuse the audited cross-tenant guard rather than re-deriving it: it
        # is the security check, and two copies of a security check drift.
        from routers import jobs as jobs_router  # noqa: PLC0415 - lazy, avoids an import cycle

        tenant_id = jobs_router._bound_tenant_id(tenant)
        record = jobs_router._job_for_tenant(value, tenant_id)
        if record is None or record.get("tenant_id") != tenant_id:
            return error_response(
                ErrorCode.BAD_PARAMS, f"unknown job_id: {value}",
                retryable=False, status_code=404,
            )

    body = receipts_read.read_receipts(scope)
    return deps.tenant_echo(with_envelope_fields(body), tenant)
