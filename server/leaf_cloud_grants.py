"""Broker-only private grants for Leaf Automation cloud adapters.

LEAF_CLOUD_GRANTS_FILE names an operator-owned file outside the repository.
Its mapping keys are public grant references; each value has tenant_id,
audience, expires_at (Unix seconds), and access_token. Provision from staging
Auth0 sign-in. Never accept this mapping from a job or emit it in a receipt.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import dataclass, field


class CloudError(ValueError):
    def __init__(self, classification: str, status: int):
        super().__init__(classification)
        self.classification = classification
        self.status = status


@dataclass(frozen=True)
class CloudGrant:
    tenant_id: str
    access_token: str = field(repr=False)


def resolve_grant(reference: str, tenant_id: str) -> CloudGrant:
    if not isinstance(reference, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", reference):
        raise CloudError("cloud_auth_missing", 401)
    path = os.environ.get("LEAF_CLOUD_GRANTS_FILE")
    if not path:
        raise CloudError("cloud_auth_missing", 401)
    try:
        with open(path, "rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError()
        grants = json.loads(raw)
        grant = grants.get(reference) if isinstance(grants, dict) else None
        if not isinstance(grant, dict):
            raise ValueError()
        token = grant.get("access_token")
        expiry = grant.get("expires_at")
        if (not isinstance(token, str) or not 1 <= len(token) <= 16384
                or any(c.isspace() for c in token)
                or type(expiry) not in (int, float) or not math.isfinite(expiry)
                or expiry <= time.time()
                or grant.get("audience") != "https://api.leafdesign.ai"):
            raise ValueError()
    except (OSError, ValueError, TypeError, OverflowError):
        raise CloudError("cloud_auth_missing", 401) from None
    if grant.get("tenant_id") != tenant_id:
        raise CloudError("cloud_tenant_unauthorized", 403)
    return CloudGrant(tenant_id=tenant_id, access_token=token)
