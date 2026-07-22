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

from envelopes import ErrorCode, error_obj

# The capability flags every tier entry carries (agent spine extends 3 -> 7:
# `converse` gates the conversational session surface, `agent_write_autopilot`
# lets the agent auto-dispatch write tools without a confirm chip, `deploy`
# gates registering an authored tool (R6, split from `build`), and
# `platform_customize` gates R7 — false everywhere at launch).
CAPABILITIES = (
    "run_read", "run_write", "solve", "build",
    "converse", "agent_write_autopilot", "deploy", "platform_customize",
)

# Fail-safe defaults — used verbatim if the JSON file is absent or unreadable. These
# MUST mirror server/entitlements.json so enforcement is identical with or without it.
_HARDCODED_DEFAULTS: Dict[str, Dict[str, bool]] = {
    "demo": {"run_read": True, "run_write": True, "solve": True, "build": True,
             "converse": True, "agent_write_autopilot": True, "deploy": True,
             "platform_customize": False},
    "restricted": {"run_read": True, "run_write": False, "solve": False, "build": False,
                   "converse": False, "agent_write_autopilot": False, "deploy": False,
                   "platform_customize": False},
    "self_hosted": {"run_read": True, "run_write": True, "solve": True, "build": True,
                    "converse": True, "agent_write_autopilot": True, "deploy": True,
                    "platform_customize": False},
    "hosted_starter": {"run_read": True, "run_write": True, "solve": False, "build": False,
                       "converse": True, "agent_write_autopilot": False, "deploy": False,
                       "platform_customize": False},
    "hosted_pro": {"run_read": True, "run_write": True, "solve": True, "build": True,
                   "converse": True, "agent_write_autopilot": True, "deploy": True,
                   "platform_customize": False},
}

DEFAULT_TIER = "demo"

_DEFAULT_FILE = Path(__file__).resolve().parent / "entitlements.json"


# --------------------------------------------------------------------------- #
# security-flag primitive — the ONE place a stored security boolean is parsed.
# This module is the LOWEST security module (agent_policy imports CAPABILITIES
# from here), so the primitive lives here and agent_policy.security_bool
# delegates to it rather than keeping a second copy that could drift.
# --------------------------------------------------------------------------- #
class EntitlementsError(ValueError):
    """Raised when a PRESENT entitlements file cannot be trusted.

    Deliberately NOT raised for an absent file: absent means "operator never
    wrote a policy" and the shipped defaults are the intended answer, whereas
    present-but-unreadable would silently restore permissive defaults over a
    tightened policy that merely failed to parse.
    """


class SecurityFlagError(ValueError):
    """Raised when a stored security flag carries a value that is not a boolean."""


class _Missing:
    """Sentinel: the field was ABSENT, as distinct from present-and-null.
    `dict.get()` collapses those two, which for a security flag is the
    difference between "operator declined to set it" and "operator wrote
    something meaningless" — only the first may take a permissive default."""

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return "<MISSING>"


MISSING = _Missing()

_TRUE_TOKENS = frozenset({"true", "yes", "on", "1"})
_FALSE_TOKENS = frozenset({"false", "no", "off", "0"})


def security_flag(value: Any, *, field: str, default: bool = False) -> bool:
    """Parse a SECURITY flag without truthiness coercion.

    `bool("false")` is `True`, so a quoted JSON scalar — `"converse": "false"` —
    would silently grant the very authority the operator was withholding.
    Accepts real booleans, the ints 0/1, and the usual string spellings.
    Anything else RAISES rather than defaulting, because for a security flag
    "I could not tell what the operator meant" must never resolve to the
    permissive answer; each caller decides whether that means deny or refuse
    to load.

    An ABSENT field (pass `MISSING`) takes the default — that is the operator
    declining to set it. An explicitly null one raises: `null` is a value the
    operator wrote, it carries no meaning, and silently reading it as the
    default is exactly the ambiguity-resolves-permissive failure this function
    exists to prevent.
    """
    if value is MISSING:
        return default
    if value is None:
        raise SecurityFlagError(
            f"{field}: must be a boolean (true/false); got null. An explicitly "
            f"null security flag is ambiguous — omit the field to take the "
            f"default ({default}), or write true/false unquoted."
        )
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):  # bool already handled above
        return bool(value)
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUE_TOKENS:
            return True
        if token in _FALSE_TOKENS:
            return False
    raise SecurityFlagError(
        f"{field}: must be a boolean (true/false); got {value!r}. "
        f"Quoted strings are NOT coerced by truthiness — write true/false unquoted."
    )


def _policy_file() -> Path:
    """The active policy file (env override wins). Read at call time for tunability."""
    return Path(os.environ.get("LEAF_ENTITLEMENTS_FILE") or _DEFAULT_FILE)


def load_policy() -> Dict[str, Dict[str, Any]]:
    """Load the tier->capability map. Missing/bad file -> hardcoded defaults (fail-safe).
    Keys beginning with `_` (e.g. `_note`) are ignored so the file can carry docs."""
    path = _policy_file()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # ABSENT is the only fail-SAFE case: the operator never wrote a policy,
        # so the shipped defaults are the intended answer.
        return dict(_HARDCODED_DEFAULTS)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        # PRESENT but unreadable is NOT the same thing. Restoring the permissive
        # defaults here would silently re-grant every capability a truncated or
        # unreadable TIGHTENED policy was withholding. UnicodeDecodeError (a
        # ValueError, NOT an OSError) covers a non-UTF-8 file — it must convert
        # to EntitlementsError like every other unreadable-policy case so the
        # request-path handlers produce the structured 503, not a bare 500.
        raise EntitlementsError(f"entitlements file unreadable at {path}: {exc}") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EntitlementsError(f"entitlements file invalid JSON at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise EntitlementsError(f"entitlements top level must be a mapping ({path})")
    # A tier whose value is not a mapping is DROPPED, not coerced: the tier then
    # reads as absent and `entitlements_for` falls to `restricted` — the safe
    # direction. (Only a wholly unreadable FILE takes the defaults above.)
    return {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, dict)}


# Sentinel: absent `.tier` ATTRIBUTE means a plain-str (auth-OFF) tenant => demo.
# A present-but-empty `.tier` means an AUTHENTICATED tenant whose Auth0 claim carried
# no tier — that must NOT fail open to full access (security-audit F9).
_NO_TIER_ATTR = object()
RESTRICTED_TIER = "restricted"  # authenticated, no tier claim: read-only, no write/build


def resolve_tier(tenant: Any) -> str:
    """The tenant's tier. Off-auth (plain str, no `.tier` attribute) -> "demo"
    (friction-free full access, by design). Authenticated (`TenantContext`) uses the
    verified claim; an authenticated tenant with a MISSING/empty/non-string tier ->
    "restricted" (fail CLOSED — never full access on a claim we cannot read)."""
    tier = getattr(tenant, "tier", _NO_TIER_ATTR)
    if tier is _NO_TIER_ATTR:
        return DEFAULT_TIER          # plain-str tenant => auth off => demo
    if not isinstance(tier, str):
        # None, a number, a list… — a claim of the wrong TYPE is uninterpretable.
        # `str(tier)` would hand a fabricated name downstream; refuse instead.
        return RESTRICTED_TIER
    # Returned unstripped on purpose: " demo " must NOT match the demo entry.
    return tier if tier.strip() else RESTRICTED_TIER


def entitlements_for(tier: str) -> Dict[str, bool]:
    """Resolve the {run_read, run_write, build} booleans for a tier — FAIL CLOSED.

    Unknown/absent tier -> the most-restrictive ("restricted") entry, NOT "demo"
    (security-audit F9 residue: an unrecognised tier must never fall open to full
    access — the auth-off *demo* path stays full-access because it resolves to the
    "demo" tier upstream in `resolve_tier`, which IS a known policy entry). A per-key
    omission defaults **False** (only an explicit `true` grants a capability), so a
    partial policy entry can never accidentally hand out a capability the operator did
    not explicitly enable, and a per-key value that is not a real boolean denies:
    `bool("false")` is True, so coercing would grant the capability the operator was
    withholding."""
    policy = load_policy()
    # Membership, not `or`: an entry of `{}` is an operator saying "this tier gets
    # NOTHING", and a truthiness chain would fall through it to `restricted`, which
    # grants run_read — a loosening. Only a genuinely ABSENT tier falls back.
    key = tier if isinstance(tier, str) else RESTRICTED_TIER
    if key in policy:
        entry = policy[key]
    elif RESTRICTED_TIER in policy:
        entry = policy[RESTRICTED_TIER]
    else:
        entry = _HARDCODED_DEFAULTS[RESTRICTED_TIER]

    resolved: Dict[str, bool] = {}
    for cap in CAPABILITIES:
        try:
            resolved[cap] = security_flag(entry.get(cap, MISSING),
                                          field=f"entitlements.{key}.{cap}", default=False)
        except SecurityFlagError:
            # An unreadable capability value denies THAT capability and leaves the
            # rest of the tier working; the operator sees it as a 403 on that
            # surface. Raising here would 500 every request on one bad key.
            resolved[cap] = False
    return resolved


def tool_required_capability(tool: Dict[str, Any]) -> str:
    """The run capability a tool requires: "run_write" iff it declares `drawing.write`
    (CONTRACT §2), else "run_read"."""
    # `or []` treated a PRESENT malformed value (None / "" / 0 / False / {}) as
    # an absent declaration, silently classifying a write tool as read-only so
    # it passed a tier lacking run_write. A malformed declaration is not a
    # read-only one: we cannot tell what the tool does, so it takes the
    # MORE-restrictive capability. Absent is still legitimately read-only.
    record = tool or {}
    if "capabilities" not in record:
        return "run_read"
    caps = record["capabilities"]
    if not isinstance(caps, (list, tuple)):
        return "run_write"
    if "solve" in caps:
        return "solve"
    return "run_write" if "drawing.write" in caps else "run_read"


def _denied_message(required: str, tier: str) -> str:
    if required == "solve":
        return (f"the '{tier}' plan does not include canonical solver execution; "
                "upgrade the workspace plan to enable Solve.")
    if required == "build":
        return (f"the '{tier}' plan does not include authoring new tools (Build); "
                "upgrade the workspace plan to enable it.")
    if required == "run_write":
        return (f"the '{tier}' plan does not include running tools that modify the drawing; "
                "upgrade the workspace plan to enable write tools.")
    return (f"the '{tier}' plan does not include running tools; upgrade the workspace plan.")


def policy_unavailable_response(required: str, tier: str) -> JSONResponse:
    """503: the policy file is PRESENT but cannot be trusted (EntitlementsError).

    Fail closed with the full §17 envelope — never an unstructured 500, and
    never an allow. INTERNAL is the frozen §10 code for "the server cannot
    evaluate this right now"; retryable because an operator fixing the policy
    file resolves it without a client change.
    """
    return JSONResponse(status_code=503, content={
        "entitlement_required": True,
        "required": required,
        "tier": tier,
        "error": error_obj(
            ErrorCode.INTERNAL,
            "entitlement policy is unavailable; request refused (fail closed).",
            retryable=True,
        ),
        "degraded_mode": False,
    })


def entitlement_denied_response(required: str, tier: str) -> JSONResponse:
    """HTTP 403 rejection mirroring the §16 grant_required pattern: an additive top-level
    marker (`entitlement_required`) + a §10-valid `error` object (frozen enum BAD_PARAMS,
    non-retryable, code ENTITLEMENT_REQUIRED). The frontend keys on `entitlement_required` / `required` / `tier`."""
    body = {
        "entitlement_required": True,
        "required": required,
        "tier": tier,
        "error": error_obj(ErrorCode.ENTITLEMENT_REQUIRED, _denied_message(required, tier), retryable=False),
        "degraded_mode": False,
    }
    return JSONResponse(status_code=403, content=body)


def entitlements_view(tier: str) -> Dict[str, Any]:
    """The GET /api/entitlements payload (pre-envelope): {tier, entitlements, source}."""
    return {"tier": tier, "entitlements": entitlements_for(tier), "source": "policy"}
