"""
Role overlay over the tier entitlement policy (contract/AUTH.md §11.5).

The tier answers *what the workspace's plan bought* (billing dimension, one
value). Roles answer *what this identity may additionally do* (identity
dimension, a set). This module owns the role side:

  * the `https://leafdesign.ai/roles` claim vocabulary and normalization rule
    (shared with the Post-Login and credentials-exchange Actions);
  * the operator-tunable role policy (`server/roles.json`, overridable via
    `LEAF_ROLES_FILE`), read at request time like `entitlements.json`;
  * the ADDITIVE-ONLY overlay law: a role can only turn a capability ON over
    the tier baseline, never off — denial stays the tier policy's job, so a
    role grant can never be used to *revoke* what billing granted, and a
    broken/hostile roles file can never lock a paying tenant out.

ELEVATED GRANTS (the W14 two-factor invariant, generalized)
-----------------------------------------------------------
A role entry may carry `elevated_grants`: capabilities that apply only when
the verified subject ALSO appears on the server-owned
`LEAF_PLATFORM_ADMIN_SUBJECTS` allowlist (deps.admin_subjects — the same
second factor the `admin` tier elevation uses). Holding the role (a verified
claim an operator minted) is factor one; the allowlist is factor two. Either
alone grants nothing. `platform_customize` MUST only ever appear under
`elevated_grants` — freeze-gated in tests/test_auth_vocab_freeze.py.

FAIL-CLOSED SHAPE (additive policy edition)
-------------------------------------------
`entitlements.json` fails closed by RAISING on a present-but-unreadable file,
because losing a *tightened* tier policy would silently re-grant. Roles are
the opposite polarity: the file can only ADD capability, so the fail-closed
answer for anything unreadable — file, entry, key, value — is NO role grants
(the tier baseline stands, nothing extra). No request ever 503s over the role
overlay; it degrades to exactly what the tenant's plan already held.

The role NAME SET is deliberately operator-extensible (add an entry to
roles.json, mint the matching claim) — unknown names in a token are ignored.
What IS frozen: the claim spelling, the capability KEY vocabulary inside
grants (the frozen 9), the `platform_admin` semantics, and the additive-only
law. Growing the frozen parts is the §11 promotion ritual.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

from entitlements import CAPABILITIES, MISSING, SecurityFlagError, security_flag

# Same canonical shape rule the M2M Action applies to tenant/org ids: role
# names travel inside tokens and policy files and must never need quoting or
# case-folding debates downstream.
ROLE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")

# Hard cap on roles honored from any single token — a hostile/buggy minter
# cannot make the server fold an unbounded list on every request.
MAX_ROLES = 16

# The staff-everything role (operator-granted, never plan-derived — the role
# analogue of the `admin` tier). Pinned by the freeze gate.
PLATFORM_ADMIN_ROLE = "platform_admin"

# Capabilities that may ONLY ever be conferred through `elevated_grants` —
# STRUCTURALLY, not just in the shipped file: a plain-grants entry carrying one
# of these is dropped at load time, so no override file (LEAF_ROLES_FILE) can
# hand out R7 self-edit without the allowlist second factor (sol-critic
# round 1, BLOCKER 1). Growing this set is a §11 promotion-ritual change.
ELEVATED_ONLY_CAPABILITIES = frozenset({"platform_customize"})

_DEFAULT_FILE = Path(__file__).resolve().parent / "roles.json"

# Fail-safe defaults — used verbatim when roles.json is ABSENT (operator never
# wrote a policy). MUST mirror server/roles.json byte-for-byte (freeze-gated),
# same discipline as entitlements._HARDCODED_DEFAULTS.
_HARDCODED_ROLE_DEFAULTS: Dict[str, Dict[str, Dict[str, bool]]] = {
    "platform_admin": {
        "grants": {"run_read": True, "run_write": True, "solve": True, "build": True,
                   "converse": True, "agent_write_autopilot": True, "deploy": True,
                   "platform_customize": False, "upload": True, "link_service": True},
        "elevated_grants": {"platform_customize": True},
    },
    # Reserved preset names for the org-configuration phase. Empty on purpose:
    # a stub that granted capabilities would let a role assignment upgrade a
    # workspace past what its plan bought before the org semantics are decided.
    "org_admin": {"grants": {}, "elevated_grants": {}},
    "org_member": {"grants": {}, "elevated_grants": {}},
}


def normalize_role_names(value: Any) -> Tuple[str, ...]:
    """The valid, deduplicated, sorted role names carried by a raw claim value.

    Accepts whatever a token (or stored payload) presented: only a list/tuple
    yields roles; every member must be a string matching ROLE_NAME_RE after
    strip+lower. Anything else — a bare string, a dict, numbers in the list —
    contributes nothing (an unreadable role is not a held one). Sorted for
    determinism, then capped at MAX_ROLES.
    """
    if not isinstance(value, (list, tuple)):
        return ()
    names = set()
    for item in value:
        if not isinstance(item, str):
            continue
        token = item.strip().lower()
        if ROLE_NAME_RE.match(token):
            names.add(token)
    return tuple(sorted(names)[:MAX_ROLES])


def _clean_grant_block(raw: Any, *, allow_elevated_only: bool = False) -> Dict[str, bool]:
    """One grants/elevated_grants mapping, reduced to the True grants it
    legibly carries. Non-dict block, unknown capability keys, and unparseable
    values all contribute nothing (additive policy: unreadable == ungranted).
    Explicit False entries are dropped too — False is the universal default,
    so only True survives, which is what makes the overlay additive-only.

    ELEVATED_ONLY_CAPABILITIES are honored only when `allow_elevated_only` is
    True (the elevated_grants block): a plain-grants entry naming one is
    dropped here, structurally, so no policy file can confer them without the
    allowlist second factor."""
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, bool] = {}
    for cap in CAPABILITIES:
        if cap in ELEVATED_ONLY_CAPABILITIES and not allow_elevated_only:
            continue
        try:
            if security_flag(raw.get(cap, MISSING), field=f"roles.{cap}", default=False):
                out[cap] = True
        except SecurityFlagError:
            continue  # unreadable value grants nothing; the rest of the block stands
    return out


def load_role_policy() -> Dict[str, Dict[str, Dict[str, bool]]]:
    """The role -> {grants, elevated_grants} map, fail-closed additively.

    ABSENT shipped file, no override configured -> the hardcoded defaults
    (operator never wrote a policy; mirrors entitlements.json). An EXPLICITLY
    CONFIGURED override (LEAF_ROLES_FILE) that is missing or unreadable ->
    NO grants: the operator pointed at a specific policy — an emergency
    revocation, say — and silently restoring the permissive shipped defaults
    because its mount vanished would defeat exactly that act (sol-critic
    round 1, MAJOR 3). Present-but-unreadable content — bad JSON, absurd
    numeric literals, pathological nesting — likewise grants nothing and
    never raises onto a request path (MAJOR 2: json.loads failures are not
    all JSONDecodeError; the int-conversion limit raises bare ValueError).
    Keys beginning with `_` are docs, same as entitlements.json.
    """
    override = os.environ.get("LEAF_ROLES_FILE")
    path = Path(override) if override else _DEFAULT_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if override:
            return {}
        raw = _HARDCODED_ROLE_DEFAULTS
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    else:
        try:
            raw = json.loads(text)
        except (ValueError, RecursionError):  # JSONDecodeError ⊂ ValueError
            return {}
    if not isinstance(raw, dict):
        return {}
    policy: Dict[str, Dict[str, Dict[str, bool]]] = {}
    for name, entry in raw.items():
        if not isinstance(name, str) or name.startswith("_"):
            continue
        key = name.strip().lower()
        if not ROLE_NAME_RE.match(key) or not isinstance(entry, dict):
            continue
        policy[key] = {
            "grants": _clean_grant_block(entry.get("grants")),
            "elevated_grants": _clean_grant_block(entry.get("elevated_grants"),
                                                  allow_elevated_only=True),
        }
    return policy


def role_grants(roles: Iterable[str], elevated: bool = False) -> Dict[str, bool]:
    """The union of True capability grants the held roles confer.

    `elevated_grants` blocks participate only when `elevated` is exactly True
    (the subject passed the LEAF_PLATFORM_ADMIN_SUBJECTS second factor).
    Unknown role names resolve to nothing. Returns only True entries.
    """
    held = normalize_role_names(tuple(roles) if not isinstance(roles, (list, tuple)) else roles)
    if not held:
        return {}
    policy = load_role_policy()
    merged: Dict[str, bool] = {}
    for name in held:
        entry = policy.get(name)
        if not entry:
            continue
        merged.update(entry.get("grants") or {})
        if elevated is True:
            merged.update(entry.get("elevated_grants") or {})
    return merged


def apply_role_grants(base: Dict[str, bool], roles: Iterable[str],
                      elevated: bool = False) -> Dict[str, bool]:
    """The ADDITIVE overlay: base (tier) capabilities with role grants OR'd in.

    Never downgrades: a True in `base` stays True regardless of what any role
    entry says (roles cannot revoke what the plan granted)."""
    grants = role_grants(roles, elevated)
    if not grants:
        return base
    out = dict(base)
    for cap, value in grants.items():
        if value is True:
            out[cap] = True
    return out
