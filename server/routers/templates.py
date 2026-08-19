"""Template routes (``solar_template_beta``): list, read, clone.

Gated by ``templates.solar_template_beta_enabled()``, checked FIRST in every
handler, before any registry lookup -- matches the ``conv_durable`` gate
convention in ``conversations.py``. Off, every route returns 404.

Every read and clone response names the EXACT version served (never
"latest") and carries a receipt of ``(template_id, version, content_digest)``
-- ``templates.read_template``/``clone_template`` verify that digest against
the frozen registry digest before this router ever sees the content.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import deps
import templates
from envelopes import ErrorCode, error_response, with_envelope_fields

router = APIRouter()


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


@router.get("/api/templates")
def list_templates(tenant: Any = Depends(deps.require_tenant)):
    if not templates.solar_template_beta_enabled():
        return _flag_off()
    body = {"templates": templates.list_templates()}
    return deps.tenant_echo(with_envelope_fields(body), tenant)


@router.get("/api/templates/{template_id}")
def read_template(template_id: str, version: Optional[str] = None,
                  tenant: Any = Depends(deps.require_tenant)):
    if not templates.solar_template_beta_enabled():
        return _flag_off()
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
