"""Template routes (``solar_template_beta``): list, read, clone.

Gated by ``templates.solar_template_beta_enabled()``, checked FIRST in every
handler, before any registry lookup -- matches the ``conv_durable`` gate
convention in ``conversations.py``. Off, every route returns 404.

Every read and clone response names the EXACT version served (never
"latest") and carries a receipt of ``(template_id, version, content_digest)``
-- ``templates.read_template``/``clone_template`` verify that digest against
the frozen registry digest before this router ever sees the content.

ROLE MATRIX (card C2-6, read surface only -- clone/write authority is C2-7's):
list/read require the caller to hold ``viewer``, ``editor``, or ``owner`` on
their OWN verified tenant context. The check runs on ``tenant.roles`` --
never a header, query param, or the template/version being requested -- so a
caller cannot widen or redirect it. It also runs BEFORE any registry lookup
(the C2-6 trap: a get-then-403 split lets a 404-vs-403 status difference
enumerate which template ids exist for a foreign tenant/role). A legacy
caller (``tenant`` is a plain str, i.e. ``LEAF_AUTH_LIVE`` never minted a
``deps.TenantContext``) is BYTE-IDENTICAL to the pre-C2-6 surface -- role
claims only exist once auth is live, so there is nothing to gate on.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import deps
import roles as roles_mod
import templates
from envelopes import ErrorCode, error_response, with_envelope_fields

router = APIRouter()

# The C2-6 read-role matrix. EXACT set membership only -- never a substring
# check ("reviewer" ends with "viewer" and must NOT pass) and never a
# default-allow for an unrecognized or malformed role.
_TEMPLATE_READ_ROLES = frozenset({"viewer", "editor", "owner"})


class CloneTemplateRequest(BaseModel):
    version: Optional[str] = None


def _flag_off() -> JSONResponse:
    return error_response(
        ErrorCode.BAD_PARAMS, "solar template beta is not enabled",
        retryable=False, status_code=404,
    )


def _not_found(message: str) -> JSONResponse:
    return error_response(ErrorCode.BAD_PARAMS, message, retryable=False, status_code=404)


def _digest_mismatch(message: str) -> JSONResponse:
    # The registry itself is corrupt/tampered -- an operator problem, not a
    # caller mistake, so this is INTERNAL/500 rather than BAD_PARAMS/400.
    return error_response(ErrorCode.INTERNAL, message, retryable=False, status_code=500)


def _role_denied() -> JSONResponse:
    # SAME shape for every denial -- list, a real template_id, an unknown
    # template_id, an unknown version -- because this is returned before the
    # router ever looks any of that up (see module docstring). Status/body
    # never vary with what the caller asked to read.
    return error_response(
        ErrorCode.FORBIDDEN,
        "template read requires a viewer, editor, or owner role for this tenant",
        retryable=False, status_code=403,
    )


def _authorized_for_read(tenant: Any) -> bool:
    """viewer/editor/owner gate (C2-6). See module docstring for the legacy
    (plain-str tenant) exemption and the exact-match-only rule."""
    if not isinstance(tenant, deps.TenantContext):
        return True
    held = roles_mod.normalize_role_names(list(tenant.roles))
    return bool(_TEMPLATE_READ_ROLES.intersection(held))


@router.get("/api/templates")
def list_templates(tenant: Any = Depends(deps.require_tenant)):
    if not templates.solar_template_beta_enabled():
        return _flag_off()
    if not _authorized_for_read(tenant):
        return _role_denied()
    body = {"templates": templates.list_templates()}
    return deps.tenant_echo(with_envelope_fields(body), tenant)


@router.get("/api/templates/{template_id}")
def read_template(template_id: str, version: Optional[str] = None,
                  tenant: Any = Depends(deps.require_tenant)):
    if not templates.solar_template_beta_enabled():
        return _flag_off()
    if not _authorized_for_read(tenant):
        return _role_denied()
    try:
        result = templates.read_template(template_id, version)
    except templates.TemplateNotFoundError:
        return _not_found(f"unknown template {template_id!r}")
    except templates.TemplateVersionNotFoundError:
        return _not_found(f"unknown version for template {template_id!r}")
    except templates.TemplateDigestMismatchError as exc:
        return _digest_mismatch(str(exc))
    body = {
        "template_id": result.template_id,
        "version": result.version,
        "content": result.content,
        "receipt": result.receipt,
    }
    return deps.tenant_echo(with_envelope_fields(body), tenant)


@router.post("/api/templates/{template_id}/clone")
def clone_template(template_id: str, req: CloneTemplateRequest,
                   tenant: Any = Depends(deps.require_tenant)):
    if not templates.solar_template_beta_enabled():
        return _flag_off()
    try:
        result = templates.clone_template(template_id, req.version)
    except templates.TemplateNotFoundError:
        return _not_found(f"unknown template {template_id!r}")
    except templates.TemplateVersionNotFoundError:
        return _not_found(f"unknown version for template {template_id!r}")
    except templates.TemplateDigestMismatchError as exc:
        return _digest_mismatch(str(exc))
    body = {
        "template_id": result.template_id,
        "version": result.version,
        "content": result.content,
        "receipt": result.receipt,
    }
    return JSONResponse(
        status_code=201,
        content=deps.tenant_echo(with_envelope_fields(body), tenant),
    )
