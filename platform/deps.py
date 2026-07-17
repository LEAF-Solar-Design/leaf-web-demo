"""FastAPI dependencies — org resolution (the tenant boundary at the edge).

DEV: the caller's org is read from the ``X-Org-Id`` header. PRODUCTION SEAM: the
auth/identity sibling replaces this with a verified org derived from the
authenticated session — a client-supplied org header MUST NOT be trusted in prod.
Isolating that swap here keeps store.py and api.py unchanged when auth lands.
"""
from __future__ import annotations

import uuid

from fastapi import Header, HTTPException


def get_org_id(x_org_id: str | None = Header(default=None)) -> uuid.UUID:
    """Resolve the caller's org. 400 if absent/malformed.

    Replace the body (not the signature) when the auth sibling arrives.
    """
    if not x_org_id:
        raise HTTPException(status_code=400, detail="missing X-Org-Id")
    try:
        return uuid.UUID(x_org_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="invalid X-Org-Id")
