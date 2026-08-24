"""da/tenant.py — tenant context resolver + per-tenant OSS key isolation.

Multi-tenant hardening for the APS Design Automation leg. This module owns the
TENANT IDENTITY shape and the per-run object-key ISOLATION scheme. It is pure
python: NO network, NO APS calls, NO credential access — importing it is free.

Two namespaces live in the same OSS bucket, side by side, WITHOUT collision:

  1. persistent versioned drawing store (da/store.py, Wave 1):
         tenants/<tenant_id>/drawings/<drawing_id>/v/<n>.dwg
     immutable, per-drawing, the undo/version substrate.

  2. per-run EPHEMERAL scratch keys (this module + da/client.py):
         t/<tenant_id>/in/<ts>_<dwg>            (throwaway upload for a run)
         t/<tenant_id>/out/<ts>_<op>.result.json (throwaway WorkItem output)

The two key roots (`tenants/` vs `t/`) never overlap, so the versioned store and
the per-run scratch space are independently reasoned about. `tenant_key_prefix`
returns the `t/<tenant_id>/` root for the scratch namespace.

Isolation strategy (ROOT default, reversible): a SINGLE shared bucket with a
per-tenant key PREFIX. The per-bucket alternative (`tenant_bucket`) is kept as a
documented, reversible option (see da/setup_live.md). Key-prefix isolation costs
nothing to provision and relies on key discipline; a hostile tenant cannot reach
another tenant's keys because tenant ids are validated to a safe single path
segment (no '/', no '..').

Tenant id shape: reuse the orchestration platform's `Deployment.deployment_id` (UuidV7) — a
lowercase-hex+hyphen uuid is already a safe slug. Short demo slugs (`demo-a`,
`t1`) are also accepted. Validation rejects anything that is not a single safe
`[a-z0-9-]` path segment, so a tenant id can never traverse or escape its prefix.
"""
from __future__ import annotations

import contextvars
import os
import re

# A tenant id must be a single safe OSS path segment: lowercase alphanumerics and
# hyphens, starting alphanumeric, 1..63 chars. UuidV7 (`0190a1b2-c3d4-7e5f-...`)
# and demo slugs (`demo-a`, `t1`, `demo-tenant`) all satisfy this; `../`, `a/b`,
# empty, and whitespace do not.
_TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

# default tenant for the single-tenant demo / unauthenticated header stub. Matches
# server/deps.DEFAULT_TENANT so the header-stub path is consistent end to end.
DEFAULT_TENANT = "demo-tenant"

# process/async-local current tenant (set by a server request scope if desired).
_current: "contextvars.ContextVar[str | None]" = contextvars.ContextVar(
    "leaf_current_tenant", default=None
)


class InvalidTenantId(ValueError):
    """Raised when a tenant id is not a safe single OSS key path segment."""


def validate_tenant_id(tenant_id) -> str:
    """Normalize + validate a tenant id to a safe lowercase path segment.

    Returns the normalized id. Raises InvalidTenantId on anything that is not a
    single `[a-z0-9-]` segment (this is the isolation guarantee: a validated
    tenant id cannot contain '/', '..', or whitespace, so it can never escape its
    key prefix).
    """
    if tenant_id is None:
        raise InvalidTenantId("tenant_id is required (got None)")
    s = str(tenant_id).strip().lower()
    if not _TENANT_RE.match(s):
        raise InvalidTenantId(
            f"invalid tenant_id {tenant_id!r}: must match {_TENANT_RE.pattern} "
            f"(a UuidV7 or a safe [a-z0-9-] slug)"
        )
    return s


def is_valid_tenant_id(tenant_id) -> bool:
    """True iff `tenant_id` is a safe tenant id (never raises)."""
    try:
        validate_tenant_id(tenant_id)
        return True
    except InvalidTenantId:
        return False


def set_current_tenant(tenant_id: str) -> "contextvars.Token":
    """Bind the current tenant for this context (returns a reset token)."""
    return _current.set(validate_tenant_id(tenant_id))


def reset_current_tenant(token: "contextvars.Token") -> None:
    _current.reset(token)


def current_tenant(explicit=None, *, default: str | None = DEFAULT_TENANT) -> str:
    """Resolve the active tenant id.

    Precedence: explicit arg > contextvar (set_current_tenant) > env LEAF_TENANT
    > `default`. The result is always validated/normalized. Pass default=None to
    require an explicit/context/env tenant (raises InvalidTenantId if none).
    """
    raw = explicit
    if raw is None:
        raw = _current.get()
    if raw is None:
        raw = os.environ.get("LEAF_TENANT")
    if raw is None:
        raw = default
    return validate_tenant_id(raw)


def tenant_key_prefix(tenant_id) -> str:
    """The per-run EPHEMERAL scratch key root for a tenant: `t/<tenant_id>/`.

    All of a tenant's throwaway run keys (`in/`, `out/`) live under this prefix,
    so two tenants running the SAME drawing produce disjoint object keys. Distinct
    from the persistent versioned store root `tenants/<id>/drawings/...`.
    """
    return f"t/{validate_tenant_id(tenant_id)}/"


def tenant_bucket(tenant_id, base: str | None = None) -> str:
    """Per-tenant bucket key for the REVERSIBLE per-bucket isolation alternative.

    NOT the default (the default is shared-bucket + key-prefix, see module docs).
    Derives a valid OSS bucket key (3..128 chars, [-_.a-z0-9]) from a base stem
    and the tenant id. Pure — makes no APS call and needs no credentials.

    `base` defaults to env APS_BUCKET_BASE, else the persistent-store stem
    `leaf-web-store`. Documented in da/setup_live.md as the clean-blast-radius
    option (bucket-count limits + per-tenant provisioning cost are the tradeoff).
    """
    base = (base or os.environ.get("APS_BUCKET_BASE") or "leaf-web-store").lower()
    key = f"{base}-t-{validate_tenant_id(tenant_id)}"
    return key[:128]
