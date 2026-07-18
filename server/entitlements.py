"""
Tier-driven entitlement enforcement (CONTRACT-ADDENDUM §17, MATRIX gap 6).

The Auth0 `tier` claim (contract/AUTH.md §1) was 0% enforced before this module:
it was surfaced in the tenant echo but nothing consumed it. This module turns the
tier into an ENFORCED capability policy, server-side and non-bypassable:

  * `run_read`  gates POST /api/run of a `drawing.read` tool
  * `run_write` gates POST /api/run of a `drawing.write` tool
  * `build`     gates POST /api/author (both the harness and template paths)

TIER SOURCE
-----------
Live auth (`LEAF_AUTH_LIVE=1`): the verified `TenantContext.tier` (the Auth0 claim).
Off-auth (default demo) or a tenant with no tier: tier resolves to **"demo"**, which
the shipped policy grants FULL access — so the open demo stays friction-free (documented
honestly; the demo is single-operator and un-authed by design).

The policy lives in a TRACKED, operator-tunable JSON file (`server/entitlements.json`,
overridable via `LEAF_ENTITLEMENTS_FILE`), read at request time so an operator can retune
tiers without a code change. If the file is missing/corrupt the hardcoded defaults below
keep enforcement working (fail-safe: the policy never silently disappears).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

from fastapi.responses import JSONResponse

from envelopes import error_obj

# The three capability flags every tier entry carries.
CAPABILITIES = ("run_read", "run_write", "build")

# Fail-safe defaults — used verbatim if the JSON file is absent or unreadable. These
# MUST mirror server/entitlements.json so enforcement is identical with or without it.
_HARDCODED_DEFAULTS: Dict[str, Dict[str, bool]] = {
    "demo": {"run_read": True, "run_write": True, "build": True},
    "restricted": {"run_read": True, "run_write": False, "build": False},
    "self_hosted": {"run_read": True, "run_write": True, "build": True},
    "hosted_starter": {"run_read": True, "run_write": True, "build": False},
    "hosted_pro": {"run_read": True, "run_write": True, "build": True},
}

DEFAULT_TIER = "demo"

_DEFAULT_FILE = Path(__file__).resolve().parent / "entitlements.json"


def _policy_file() -> Path:
    """The active policy file (env override wins). Read at call time for tunability."""
    return Path(os.environ.get("LEAF_ENTITLEMENTS_FILE") or _DEFAULT_FILE)


def load_policy() -> Dict[str, Dict[str, Any]]:
    """Load the tier->capability map. Missing/bad file -> hardcoded defaults (fail-safe).
    Keys beginning with `_` (e.g. `_note`) are ignored so the file can carry docs."""
    path = _policy_file()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - missing/corrupt -> fail safe to defaults
        return dict(_HARDCODED_DEFAULTS)
    if not isinstance(raw, dict):
        return dict(_HARDCODED_DEFAULTS)
    return {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, dict)}


# Sentinel: absent `.tier` ATTRIBUTE means a plain-str (auth-OFF) tenant => demo.
# A present-but-empty `.tier` means an AUTHENTICATED tenant whose Auth0 claim carried
# no tier — that must NOT fail open to full access (security-audit F9).
_NO_TIER_ATTR = object()
RESTRICTED_TIER = "restricted"  # authenticated, no tier claim: read-only, no write/build


def resolve_tier(tenant: Any) -> str:
    """The tenant's tier. Off-auth (plain str, no `.tier` attribute) -> "demo"
    (friction-free full access, by design). Authenticated (`TenantContext`) uses the
    verified claim; an authenticated tenant with a MISSING/empty tier -> "restricted"
    (fail CLOSED — never full access on a missing claim)."""
    tier = getattr(tenant, "tier", _NO_TIER_ATTR)
    if tier is _NO_TIER_ATTR:
        return DEFAULT_TIER          # plain-str tenant => auth off => demo
    return str(tier) if tier else RESTRICTED_TIER


def entitlements_for(tier: str) -> Dict[str, bool]:
    """Resolve the {run_read, run_write, build} booleans for a tier.

    Unknown tier -> the "demo" entry (friction-free; a tier only ever arrives from a
    VERIFIED Auth0 claim, so an unrecognized value is operator config drift, not an
    attacker vector). A per-key omission defaults True (permissive), so a partial entry
    never accidentally locks a capability the operator did not intend to restrict."""
    policy = load_policy()
    entry = policy.get(tier) or policy.get(DEFAULT_TIER) or _HARDCODED_DEFAULTS[DEFAULT_TIER]
    return {cap: bool(entry.get(cap, True)) for cap in CAPABILITIES}


def tool_required_capability(tool: Dict[str, Any]) -> str:
    """The run capability a tool requires: "run_write" iff it declares `drawing.write`
    (CONTRACT §2), else "run_read"."""
    caps = (tool or {}).get("capabilities") or []
    return "run_write" if "drawing.write" in caps else "run_read"


def _denied_message(required: str, tier: str) -> str:
    if required == "build":
        return (f"the '{tier}' plan does not include authoring new tools (Build); "
                "upgrade the workspace plan to enable it.")
    if required == "run_write":
        return (f"the '{tier}' plan does not include running tools that modify the drawing; "
                "upgrade the workspace plan to enable write tools.")
    return (f"the '{tier}' plan does not include running tools; upgrade the workspace plan.")


def entitlement_denied_response(required: str, tier: str) -> JSONResponse:
    """HTTP 403 rejection mirroring the §16 grant_required pattern: an additive top-level
    marker (`entitlement_required`) + a §10-valid `error` object (frozen enum BAD_PARAMS,
    non-retryable). The frontend keys on `entitlement_required` / `required` / `tier`."""
    body = {
        "entitlement_required": True,
        "required": required,
        "tier": tier,
        "error": error_obj("BAD_PARAMS", _denied_message(required, tier), retryable=False),
        "degraded_mode": False,
    }
    return JSONResponse(status_code=403, content=body)


def entitlements_view(tier: str) -> Dict[str, Any]:
    """The GET /api/entitlements payload (pre-envelope): {tier, entitlements, source}."""
    return {"tier": tier, "entitlements": entitlements_for(tier), "source": "policy"}
