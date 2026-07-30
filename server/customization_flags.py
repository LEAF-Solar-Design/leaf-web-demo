"""Strict, tenant-scoped rollout gates for the customization lifecycle."""
from __future__ import annotations

import os
from enum import Enum


class RolloutMode(str, Enum):
    OFF = "off"
    INTERNAL = "internal"
    ALL = "all"


def mode(name: str) -> RolloutMode:
    """Return a known rollout mode. Malformed configuration is always off."""
    try:
        return RolloutMode(os.environ.get(name, "off").strip().lower())
    except ValueError:
        return RolloutMode.OFF


def internal_tenants() -> frozenset[str]:
    return frozenset(part.strip() for part in os.environ.get(
        "LEAF_CUSTOMIZATION_INTERNAL_TENANTS", ""
    ).split(",") if part.strip())


def enabled(rung: int, tenant_id: str) -> bool:
    """Return whether R5, R6, or R7 is available to this tenant.

    R6 is conditional on R5, so a partial rollout cannot expose publishing
    without trusted staging.

    R7 (W14 admin self-edit lane) is INTERNAL-ONLY by construction: the mode
    value ``all`` deliberately reads as off, because platform self-edit is an
    allowlisted-admin-accounts lane and must never be one env typo away from
    "every tenant". It needs LEAF_CUSTOMIZATION_R7_MODE=internal AND the
    tenant on LEAF_CUSTOMIZATION_INTERNAL_TENANTS. The entitlement layer
    (tier `admin` carrying `platform_customize`) gates independently on top.
    """
    if rung == 7:
        return (mode("LEAF_CUSTOMIZATION_R7_MODE") is RolloutMode.INTERNAL
                and str(tenant_id) in internal_tenants())
    if rung not in (5, 6):
        return False
    selected = mode(f"LEAF_CUSTOMIZATION_R{rung}_MODE")
    permitted = selected is RolloutMode.ALL or (
        selected is RolloutMode.INTERNAL and str(tenant_id) in internal_tenants()
    )
    return permitted and (rung != 6 or enabled(5, tenant_id))
