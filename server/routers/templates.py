"""Template routes (``solar_template_beta``): list, read, clone, project-clone
write + undo.

Gated by ``templates.solar_template_beta_enabled()``, checked FIRST in every
handler, before any registry lookup -- matches the ``conv_durable`` gate
convention in ``conversations.py``. Off, every route returns 404.

Every read and clone response names the EXACT version served (never
"latest") and carries a receipt of ``(template_id, version, content_digest)``
-- ``templates.read_template``/``clone_template`` verify that digest against
the frozen registry digest before this router ever sees the content.

ROLE MATRIX (card C2-6, read surface): list/read require the caller to hold
``viewer``, ``editor``, or ``owner`` on their OWN verified tenant context.
The check runs on ``tenant.roles`` -- never a header, query param, or the
template/version being requested -- so a caller cannot widen or redirect it.
It also runs BEFORE any registry lookup (the C2-6 trap: a get-then-403 split
lets a 404-vs-403 status difference enumerate which template ids exist for a
foreign tenant/role). A legacy caller (``tenant`` is a plain str, i.e.
``LEAF_AUTH_LIVE`` never minted a ``deps.TenantContext``) is BYTE-IDENTICAL
to the pre-C2-6 surface -- role claims only exist once auth is live, so
there is nothing to gate on.

ROLE MATRIX (card C2-7, clone-into-project + write + undo authority; card
C2-4's PATCH content route shares this SAME gate -- see its own docstring):
``editor``/``owner`` only -- ``reviewer``/``viewer``/read-only and any
unrecognized role are denied, same shape as no role at all. Three traps this
closes, all found on the C2-6 sibling route and equally reachable here:

  * The gate runs on ``tenant.roles`` from THIS caller's own verified claim,
    checked BEFORE any clone/write/undo call -- never on the template being
    cloned FROM, and never on a caller-supplied ``tenant_id`` (the clone
    request body carries only ``project_id``; the tenant it lands under is
    always ``str(tenant)``, the verified claim, so a caller can never target
    a tenant other than their own).
  * Undo shares the EXACT SAME gate (``_write_role``) as clone/write -- it is
    a mutation, not a read, and never falls back to a weaker "any
    authenticated caller" dependency.
  * ``templates.write_project_clone_content``/``undo_last_write`` look up the
    clone THEN authorize it (``ProjectTemplateCloneNotFoundError`` vs
    ``AuthorityError``) -- a raw 404-vs-403 split on that pair would let a
    denied caller enumerate which foreign clone_ids exist. This router
    collapses both into the identical ``_write_denied()`` response, so an
    unknown clone_id and a foreign-tenant clone_id are indistinguishable to
    the caller. ``ProjectCloneWriteNotFoundError``/``StaleUndoReceiptError``
    are safe to keep distinct: they can only be reached AFTER authorization
    already proved the caller owns that exact clone, so they carry no
    cross-tenant existence signal.

The role used per request is re-derived from the CURRENT ``tenant.roles`` on
every call (never cached/snapshotted from clone time), so a mid-session
editor -> viewer downgrade is honored on the very next write or undo.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

import deps
import roles as roles_mod
import templates
from customization_authority import AuthorityError, TenantBinding
from envelopes import ErrorCode, error_response, with_envelope_fields

router = APIRouter()

# The C2-6 read-role matrix. EXACT set membership only -- never a substring
# check ("reviewer" ends with "viewer" and must NOT pass) and never a
# default-allow for an unrecognized or malformed role.
_TEMPLATE_READ_ROLES = frozenset({"viewer", "editor", "owner"})

# The C2-7 write-role matrix (clone-into-project, write, undo). EXACT set
# membership only -- "reviewer"/"read_only"/"viewer" and any unrecognized
# name are denied, never substring-matched or default-allowed.
_TEMPLATE_WRITE_ROLES = frozenset({"editor", "owner"})


class CloneTemplateRequest(BaseModel):
    version: Optional[str] = None


class ProjectCloneRequest(BaseModel):
    project_id: str
    version: Optional[str] = None


class ProjectCloneWriteRequest(BaseModel):
    content: Dict[str, Any]


class ProjectCloneUndoRequest(BaseModel):
    write_id: str


class UpdateProjectCloneContentRequest(BaseModel):
    """The ONE mutation payload (card C2-4). ``extra="forbid"`` rejects any
    unknown field as a typed 4xx before ``templates.py`` ever sees the body
    -- schema validation, not just the caps enforced past this boundary."""

    model_config = ConfigDict(extra="forbid")

    content: Dict[str, Any]
    expected_content_version: int = Field(..., ge=1)


def _flag_off() -> JSONResponse:
    return error_response(
        ErrorCode.BAD_PARAMS, "solar template beta is not enabled",
        retryable=False, status_code=404,
    )


def _not_found(message: str) -> JSONResponse:
    return error_response(ErrorCode.BAD_PARAMS, message, retryable=False, status_code=404)


def _write_validation_error(message: str) -> JSONResponse:
    return error_response(ErrorCode.BAD_PARAMS, message, retryable=False, status_code=400)


def _conflict(message: str) -> JSONResponse:
    # No dedicated CONFLICT machine code exists in envelopes.ErrorCode (out of
    # this card's file budget to add one) -- BAD_PARAMS carries the message,
    # the OVERRIDDEN status_code=409 is what callers actually branch on.
    return error_response(ErrorCode.BAD_PARAMS, message, retryable=True, status_code=409)


def _write_timeout(message: str) -> JSONResponse:
    return error_response(ErrorCode.TIMEOUT, message, retryable=True, status_code=504)


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


def _write_denied() -> JSONResponse:
    # SAME shape for every write-path denial -- no write role, a foreign
    # tenant's clone_id, an unknown clone_id, a foreign template lookup --
    # so a caller can never use status/body to learn which of those is true
    # (the C2-7 trap: see module docstring).
    return error_response(
        ErrorCode.FORBIDDEN,
        "template project clone/write/undo requires an editor or owner "
        "role for this tenant",
        retryable=False, status_code=403,
    )


def _write_role(tenant: Any) -> Optional[str]:
    """The caller's write-grade role (owner > editor), or None if they hold
    neither -- exact-match only (roles_mod already folds case and rejects
    substrings/malformed entries). Legacy (auth-off) callers pass through
    unchanged, same exemption ``_authorized_for_read`` applies: role claims
    only exist once auth is live, so there is nothing to gate on."""
    if not isinstance(tenant, deps.TenantContext):
        return "owner"
    held = roles_mod.normalize_role_names(list(tenant.roles))
    if "owner" in held:
        return "owner"
    if "editor" in held:
        return "editor"
    return None


def _binding_for(tenant: Any, role: str) -> TenantBinding:
    """A ``TenantBinding`` for the CALLER's own verified tenant -- ``tenant_id``
    is always ``str(tenant)`` (the verified claim), never a caller-supplied
    body field, so a request can never bind a clone/write/undo to a tenant
    other than the caller's own."""
    subject = tenant.subject if isinstance(tenant, deps.TenantContext) else None
    return TenantBinding(tenant_id=str(tenant), subject=subject, role=role, verified=True)


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


@router.post("/api/templates/{template_id}/project-clones")
def clone_template_into_project(template_id: str, req: ProjectCloneRequest,
                                tenant: Any = Depends(deps.require_tenant)):
    """Land a project-owned clone under the CALLER's own tenant (card C2-7).

    ``project_id`` comes from the request body; ``tenant_id`` never does --
    it is always the verified ``tenant`` claim, so a caller can only ever
    clone into a project inside their own tenant. The write-role gate runs
    BEFORE any registry/authority lookup, mirroring C2-6's read-gate
    ordering, so an unauthorized caller cannot use a template/version probe
    to learn what exists.
    """
    if not templates.solar_template_beta_enabled():
        return _flag_off()
    role = _write_role(tenant)
    if role is None:
        return _write_denied()
    binding = _binding_for(tenant, role)
    try:
        clone = templates.clone_template_for_project(
            template_id, tenant_id=str(tenant), project_id=req.project_id,
            version=req.version, binding=binding,
        )
    except templates.TemplateBetaDisabledError:
        return _flag_off()
    except AuthorityError:
        return _write_denied()
    except templates.TemplateNotFoundError:
        return _not_found(f"unknown template {template_id!r}")
    except templates.TemplateVersionNotFoundError:
        return _not_found(f"unknown version for template {template_id!r}")
    except templates.TemplateDigestMismatchError as exc:
        return _digest_mismatch(str(exc))
    except ValueError as exc:
        return error_response(ErrorCode.BAD_PARAMS, str(exc), retryable=False, status_code=400)
    body = {
        "clone_id": clone.clone_id,
        "tenant_id": clone.tenant_id,
        "project_id": clone.project_id,
        "template_id": clone.template_id,
        "version": clone.version,
        "content": clone.content,
        "receipt": clone.receipt,
    }
    return JSONResponse(
        status_code=201,
        content=deps.tenant_echo(with_envelope_fields(body), tenant),
    )


@router.post("/api/project-clones/{clone_id}/writes")
def write_project_clone(clone_id: str, req: ProjectCloneWriteRequest,
                        tenant: Any = Depends(deps.require_tenant)):
    """Overwrite a project clone's content (card C2-7 write authority).

    ``templates.write_project_clone_content`` looks up the clone THEN
    authorizes it -- an unknown clone_id and a foreign-tenant clone_id raise
    different exceptions internally, but both collapse to the SAME
    ``_write_denied()`` response here so neither leaks whether a foreign
    clone_id exists (the C2-7 404-vs-403 enumeration trap).
    """
    if not templates.solar_template_beta_enabled():
        return _flag_off()
    role = _write_role(tenant)
    if role is None:
        return _write_denied()
    binding = _binding_for(tenant, role)
    try:
        write = templates.write_project_clone_content(
            clone_id, req.content, binding=binding,
        )
    except templates.TemplateBetaDisabledError:
        return _flag_off()
    except (templates.ProjectTemplateCloneNotFoundError, AuthorityError):
        return _write_denied()
    body = {
        "clone_id": write.clone_id,
        "write_id": write.write_id,
        "receipt": write.receipt,
    }
    return deps.tenant_echo(with_envelope_fields(body), tenant)


@router.post("/api/project-clones/{clone_id}/undo")
def undo_project_clone_write(clone_id: str, req: ProjectCloneUndoRequest,
                             tenant: Any = Depends(deps.require_tenant)):
    """Revert a project clone's last write (card C2-7 write authority).

    Undo is a mutation and shares the EXACT SAME ``_write_role`` gate as
    clone/write -- it never falls back to a weaker "any authenticated
    caller" dependency (the C2-7 trap this closes: see module docstring).
    ``ProjectCloneWriteNotFoundError``/``StaleUndoReceiptError`` are safe to
    report distinctly from ``_write_denied()``: they are only reachable
    AFTER authorization already proved the caller owns this exact clone, so
    they carry no cross-tenant existence signal.
    """
    if not templates.solar_template_beta_enabled():
        return _flag_off()
    role = _write_role(tenant)
    if role is None:
        return _write_denied()
    binding = _binding_for(tenant, role)
    try:
        result = templates.undo_last_write(clone_id, req.write_id, binding=binding)
    except templates.TemplateBetaDisabledError:
        return _flag_off()
    except (templates.ProjectTemplateCloneNotFoundError, AuthorityError):
        return _write_denied()
    except templates.ProjectCloneWriteNotFoundError:
        return _not_found(f"unknown write_id {req.write_id!r} for clone {clone_id!r}")
    except templates.StaleUndoReceiptError as exc:
        return error_response(
            ErrorCode.BAD_PARAMS, str(exc), retryable=False, status_code=409,
        )
    body = {
        "clone_id": clone_id,
        "undone": result.undone,
        "already_undone": result.already_undone,
        "receipt": result.receipt,
    }
    return deps.tenant_echo(with_envelope_fields(body), tenant)


# --------------------------------------------------------------------------- #
# The ONE write path: bounded, validated, fails closed (card C2-4).
#
# The ONLY mutation route "on the cloned instance" -- distinct from the
# template-registry preview routes above, which never persist anything. Body
# shape is pydantic-validated (``extra="forbid"``, non-negative version)
# BEFORE this handler runs; ``templates.update_project_clone_content`` layers
# the caps, the tenant-scoped lookup, the CAS conflict check, and the
# statement-timeout-bounded atomic write (see its docstring). Every failure
# mode maps to its own typed 4xx/5xx and lands NOTHING.
#
# Authority (closes PR #704 finding 7): this route shares the EXACT SAME
# ``_write_role``/``_binding_for``/``_write_denied`` gate as clone/write/undo
# above -- checked BEFORE ``templates.update_project_clone_content`` is ever
# called, so a viewer/reviewer/no-role caller gets the SAME-SHAPE
# ``_write_denied()`` 403, never a write, regardless of whether the
# clone_id is real. ``templates.py`` re-verifies the binding a second time,
# INSIDE its critical section (defense in depth, matching
# ``write_project_clone_content``'s pattern) -- so a revoked/unverified
# binding is denied even if this router-level check were ever bypassed by a
# direct function call. The unknown-vs-foreign-tenant clone_id 404 stays a
# DISTINCT shape from the role-denial 403 (unlike write/undo's collapsed
# 403-only shape above): tenant-scoping predates this card's role gate and
# an existing oracle already locks in byte-identical 404s for that pair.
# --------------------------------------------------------------------------- #
@router.patch("/api/templates/clones/{clone_id}")
def update_project_clone_content(
    clone_id: str, req: UpdateProjectCloneContentRequest,
    tenant: Any = Depends(deps.require_tenant),
):
    if not templates.solar_template_beta_enabled():
        return _flag_off()
    role = _write_role(tenant)
    if role is None:
        return _write_denied()
    binding = _binding_for(tenant, role)
    try:
        result = templates.update_project_clone_content(
            clone_id, str(tenant), req.content, req.expected_content_version,
            binding=binding,
            # Read at CALL time (not the function's def-time default) so an
            # operator/test override of the module constant always applies.
            lock_timeout_seconds=templates._CLONE_WRITE_LOCK_TIMEOUT_SECONDS,
        )
    except templates.TemplateBetaDisabledError:
        return _flag_off()
    except templates.ProjectTemplateCloneWriteValidationError as exc:
        return _write_validation_error(str(exc))
    except templates.ProjectTemplateCloneNotFoundError:
        return _not_found(f"unknown project template clone {clone_id!r}")
    except AuthorityError:
        return _write_denied()
    except templates.ProjectTemplateCloneConflictError as exc:
        return _conflict(str(exc))
    except templates.ProjectTemplateCloneWriteTimeoutError as exc:
        return _write_timeout(str(exc))
    body = {
        "clone_id": result.clone_id,
        "tenant_id": result.tenant_id,
        "project_id": result.project_id,
        "template_id": result.template_id,
        "version": result.version,
        "content": result.content,
        "content_version": result.content_version,
        "receipt": result.receipt,
    }
    return deps.tenant_echo(with_envelope_fields(body), tenant)
