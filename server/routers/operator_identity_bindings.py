"""Protected operator rail for atomic synthetic identity pair binding.

The route is mounted only by its dedicated deployment flag. Authentication
still uses the normal verified operator dependency, and the store locks and
revalidates that principal in the same transaction as both bindings.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Dict

from fastapi import APIRouter, Depends, Header, HTTPException, Request

import operator_deps
import platform_link
from operator_deps import OperatorContext

router = APIRouter()

_SUBJECT_RE = re.compile(r"^auth0\|[A-Za-z0-9._~+-]{1,249}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._:/+-]{8,128}$")


async def _parse_pair_body(request: Request) -> Dict[str, Any]:
    """Parse without framework validation errors that echo rejected values."""
    try:
        raw = await request.body()
        if len(raw) > 16_384:
            raise ValueError("request too large")
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {"environment", "bindings"}:
            raise ValueError("invalid top-level shape")
        environment = value["environment"]
        bindings = value["bindings"]
        if not isinstance(environment, str) or not 1 <= len(environment) <= 64:
            raise ValueError("invalid environment")
        if not isinstance(bindings, list) or len(bindings) != 2:
            raise ValueError("invalid binding count")
        parsed = []
        for item in bindings:
            if (not isinstance(item, dict)
                    or set(item) != {"tenant_id", "subject", "role"}
                    or not isinstance(item["tenant_id"], str)
                    or not isinstance(item["subject"], str)
                    or item["role"] != "read_only"):
                raise ValueError("invalid binding shape")
            parsed.append({
                "tenant_id": uuid.UUID(item["tenant_id"]),
                "subject": item["subject"],
                "role": "read_only",
            })
        return {"environment": environment, "bindings": parsed}
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail="invalid_identity_pair") from exc


@router.post("/api/operator/identity-bindings/pair")
def bind_identity_pair(
    body: Dict[str, Any] = Depends(_parse_pair_body),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    op: OperatorContext = Depends(operator_deps.require_operator),
) -> Dict[str, Any]:
    if op.role != "operator" or body["environment"] != op.environment:
        raise HTTPException(status_code=404, detail="operator_not_found")
    if not _IDEMPOTENCY_RE.fullmatch(idempotency_key):
        raise HTTPException(status_code=422, detail="invalid_idempotency_key")
    subjects = [item["subject"] for item in body["bindings"]]
    tenants = [item["tenant_id"] for item in body["bindings"]]
    if (any(_SUBJECT_RE.fullmatch(subject) is None for subject in subjects)
            or len(set(subjects)) != 2 or len(set(tenants)) != 2):
        raise HTTPException(status_code=422, detail="invalid_identity_pair")

    try:
        store = platform_link.platform_store()
    except Exception as exc:  # noqa: BLE001, unavailable store must fail closed
        raise HTTPException(
            status_code=503, detail="operator_store_unavailable") from exc
    try:
        result = store.bind_identity_pair(
            body["bindings"],
            operator_subject=op.subject,
            operator_role_revision=op.role_revision,
            environment=op.environment,
            idempotency_key=idempotency_key,
        )
    except store.IdentityBindingPairPrincipalDrift as exc:
        raise HTTPException(status_code=404, detail="operator_not_found") from exc
    except store.IdentityBindingPairConflict as exc:
        raise HTTPException(status_code=409, detail="identity_pair_conflict") from exc
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail="invalid_identity_pair") from exc
    except Exception as exc:  # noqa: BLE001, unavailable store must fail closed
        raise HTTPException(
            status_code=503, detail="operator_store_unavailable") from exc

    return {
        "environment": op.environment,
        "role": "read_only",
        "bindings": [
            {"tenant_id": str(item["tenant_id"]),
             "binding_id": str(item["binding_id"]),
             "role": item["role"], "state": item["state"]}
            for item in result["bindings"]
        ],
    }
