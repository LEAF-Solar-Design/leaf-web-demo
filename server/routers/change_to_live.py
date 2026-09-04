"""The change-to-live pair: what class of change this is, and what receipts exist.

``GET /api/change-class?paths=...`` classifies a change set by SHAPE
(``server/change_classifier.py``). ``GET /api/receipts?scope=...`` reads the
delivery receipts that actually exist (``server/receipts_read.py``).

Both are TENANT-SCOPED reads behind ``deps.require_tenant`` and both are
BOUNDED and FAIL CLOSED: a malformed request is a 422 naming the rule it broke,
never a best-effort answer. Neither endpoint accepts a caller-supplied
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

import change_classifier
import deps
import receipts_read
from envelopes import ErrorCode, error_response, with_envelope_fields

router = APIRouter()

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


@router.get("/api/change-class")
def change_class(
    paths: List[str] = Query(default=[]),
    kind: Optional[str] = Query(default=None),
    tenant=Depends(deps.require_tenant),
) -> Any:
    """Which delivery ladder a change of this shape rides.

    HONEST DATA, NOT A PROMISE. The class describes the SHAPE of the requested
    change; it does not reserve a relay slot, predict a queue, or commit that
    the change will land at all. See ``change_classifier``'s module docstring
    for the full statement, which this endpoint deliberately does not restate
    in a way a caller could mistake for a service-level guarantee.
    """
    try:
        expanded = _expand_paths(list(paths))
        result = change_classifier.classify_change(expanded, kind)
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

    ``scope`` is ``pr:<n>``, ``tree:<sha>``, ``job:<id>`` or ``train``. A
    ``job:`` scope is checked against the CALLING tenant first and answers 404
    for an unknown job and for another tenant's job alike -- the same
    no-existence-leak rule ``GET /api/jobs/{id}`` follows.
    """
    try:
        kind, value = receipts_read.parse_scope(scope)
    except receipts_read.ReceiptsError as exc:
        return error_response(
            ErrorCode.BAD_PARAMS, str(exc), retryable=False, status_code=422
        )

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
