"""Authenticated access to the sizer's module catalog."""
import json

from fastapi import APIRouter, Depends

import deps
import leaf_cloud_grants as cloud
import solar_module_catalog as catalog
from demo_actor import DemoActorRefused, DemoActorUnavailable, resolve_demo_actor
from envelopes import ErrorCode, error_response

router = APIRouter()


@router.get("/api/solar/sizing-modules")
def sizing_modules(q: str | None = None, limit: str = "20", grant_ref: str | None = None,
                   tenant=Depends(deps.require_tenant)):
    try:
        catalog.validate_query(q, limit, grant_ref)
    except ValueError as exc:
        return error_response(ErrorCode.BAD_PARAMS, str(exc), False, status_code=400)
    try:
        actor = resolve_demo_actor(tenant)
        organization_id = str(actor.org_id if actor is not None else tenant)
        return catalog.search(organization_id, q, limit, grant_ref)
    except DemoActorRefused:
        return error_response(ErrorCode.FORBIDDEN, "demo_actor_refused", False, status_code=403)
    except DemoActorUnavailable:
        return error_response(ErrorCode.INTERNAL, "STORE_UNAVAILABLE", True, status_code=503)
    except cloud.CloudError as exc:
        status, code, reason = {
            "cloud_auth_missing": (401, ErrorCode.FORBIDDEN, "CLOUD_AUTH_MISSING"),
            "cloud_tenant_unauthorized": (403, ErrorCode.FORBIDDEN, "CLOUD_TENANT_UNAUTHORIZED"),
            "cloud_upstream_timeout": (504, ErrorCode.TIMEOUT, "CLOUD_UPSTREAM_TIMEOUT"),
        }.get(exc.classification, (502, ErrorCode.WORKITEM_FAILED, "CLOUD_UPSTREAM_FAILURE"))
        response = error_response(code, reason, False, status_code=status)
        body = json.loads(response.body)
        body["error"]["reason_code"] = reason
        response.body = response.render(body)
        response.headers["content-length"] = str(len(response.body))
        return response
