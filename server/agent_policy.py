"""
Operator-owned agent action catalog: parser, validator, tier/tenant merge.

The catalog (`server/agent_policy.json`, overridable via `LEAF_AGENT_POLICY_FILE`,
read at request time) IS the guardrail for the conversational agent: nothing
outside it can pass the gate (server/agent_gate.py), and only at the policy tier
the operator set (auto / confirm-once / always-confirm).

Validation is STRICT — the opposite of entitlements.py's silent-fallback,
because a typo here could weaken a security policy rather than a plan:

  * unknown action / override / top-level fields -> PolicyError (typo of a
    security field must never load);
  * security booleans (`enabled`, `tenant_tightenable`) refuse truthiness
    coercion — `bool("false")` is True, so quoted scalars raise instead of
    silently granting authority;
  * a MISSING file falls back to the hardcoded defaults below (fail-safe:
    the policy never silently disappears), but a PRESENT-and-invalid file
    raises loudly — the gate fails CLOSED on a PolicyError.

Two override layers, applied in order by `effective_action()`:

  * `tier_overrides` (in the global file, operator-owned) may retune a tier's
    policy in either direction — EXCEPT for actions with `tenant_tightenable:
    false` (e.g. register_tool), which are not skippable at ANY tier;
  * per-tenant overlays (`agent_tenants.json` beside the policy file) can only
    TIGHTEN: policy may only move up the auto -> confirm-once -> always-confirm
    ordering, `enabled` can only go false. Loosening raises PolicyError.
"""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from entitlements import CAPABILITIES, MISSING, SecurityFlagError, security_flag

SERVER_DIR = Path(__file__).resolve().parent

_DEFAULT_FILE = SERVER_DIR / "agent_policy.json"

# Confirmation-strictness ordering: an overlay may only move a policy UP.
POLICY_ORDER: Dict[str, int] = {"auto": 0, "confirm-once": 1, "always-confirm": 2}

_VALID_POLICIES = frozenset(POLICY_ORDER)
_VALID_CATEGORIES = frozenset({"low", "medium", "high"})
_VALID_DISPATCH_KINDS = frozenset({"app_api", "harness"})
_VALID_RUNGS = frozenset(range(0, 8))

_ACTION_REQUIRED_FIELDS = frozenset({
    "description", "rung", "required_capability", "policy", "dispatch",
    "rate_limit_category",
})
_ACTION_KNOWN_FIELDS = _ACTION_REQUIRED_FIELDS | frozenset({
    "args_schema", "timeout_s", "audit_extra", "enabled", "tenant_tightenable",
    "cost_class",
})
_TOP_LEVEL_KNOWN = frozenset({"version", "actions", "rate_limits", "approval_ttl_s",
                              "tier_overrides"})
_OVERRIDE_KNOWN_FIELDS = frozenset({"policy", "enabled"})


class PolicyError(ValueError):
    """Raised when the agent policy file is malformed or violates schema."""


def security_bool(value: Any, *, field: str, default: bool = False) -> bool:
    """`entitlements.security_flag` in this module's error currency.

    ONE implementation of "parse a security flag" lives in entitlements (the
    lower module); this wrapper only re-raises as PolicyError, which every
    caller here — and the gate's fail-closed handler — already keys on.
    `MISSING` (absent field) takes the default; an explicit null raises, so
    `{"agent_disabled": null}` can never read as a tenant that is NOT killed.
    """
    try:
        return security_flag(value, field=field, default=default)
    except SecurityFlagError as exc:
        raise PolicyError(str(exc)) from exc


def security_int(value: Any, *, field: str) -> int:
    """Parse an integer that gates authority, refusing bool and string coercion.

    In Python `bool` SUBCLASSES `int`: isinstance(True, int) is True, True == 1,
    and int(True) is 1. So `"low_per_hour": true` silently loaded as a budget of
    ONE, and `"version": true` passed a `!= 1` check. int("5") would likewise
    accept a quoted number. Both are the operator writing something that is not
    an integer, and a budget or version is not a place to guess. (`rung` already
    excluded bool explicitly — this generalises that idiom to every such field.)
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise PolicyError(
            f"{field}: must be an integer; got {value!r}"
            + (" (booleans are not integers here)" if isinstance(value, bool) else "")
        )
    return value


@dataclasses.dataclass(frozen=True)
class AgentAction:
    name: str
    description: str
    rung: int
    required_capability: str
    policy: str
    dispatch: Dict[str, Any]
    args_schema: Optional[Dict[str, Any]] = None
    rate_limit_category: str = "high"
    timeout_s: int = 600
    audit_extra: Tuple[str, ...] = ()
    enabled: bool = True
    tenant_tightenable: bool = True
    cost_class: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class AgentPolicy:
    version: int
    actions: Dict[str, AgentAction]
    rate_limits: Dict[str, int]          # category -> per-hour budget
    approval_ttl_s: int
    tier_overrides: Dict[str, Dict[str, Dict[str, Any]]]  # tier -> action -> fields


# --------------------------------------------------------------------------- #
# hardcoded fail-safe defaults — MUST mirror server/agent_policy.json verbatim
# (validated by test_agent_policy.py) so a deleted file never weakens the gate.
# --------------------------------------------------------------------------- #
_RUN_TOOL_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {"tool": {"type": "string"}, "params": {"type": "object"},
                   "dwg": {"type": "string"}, "confirmation_id": {"type": "string"}},
    "required": ["tool"],
    "additionalProperties": False,
}

_HARDCODED_DEFAULTS: Dict[str, Any] = {
    "version": 1,
    "approval_ttl_s": 300,
    "rate_limits": {"low_per_hour": 120, "medium_per_hour": 60, "high_per_hour": 10},
    "actions": {
        "request_confirmation": {
            "description": "UI-only consent chip: ask the user to approve something before proceeding (R0; wire contract section 5). Dispatches nowhere — the gate mints the confirmation_id and the harness renders the card, then ends the turn. always-confirm is what MAKES the id: the app's pending store is the only authority allowed to mint one (section 6), so a self-issued harness id could never be redeemed.",
            "rung": 0, "required_capability": "converse", "policy": "always-confirm",
            "tenant_tightenable": False,
            "dispatch": {"kind": "app_api", "routes": []},
            "args_schema": {"type": "object",
                            "properties": {"kind": {"type": "string"},
                                           "payload": {"type": "object"},
                                           "confirmation_id": {"type": "string"}},
                            "required": ["kind"], "additionalProperties": False},
            "rate_limit_category": "low", "timeout_s": 30, "audit_extra": ["kind"],
        },
        "read_platform_state": {
            "description": "Read platform state: catalog, usage, jobs, versions, own audit (R1).",
            "rung": 1, "required_capability": "converse", "policy": "auto",
            "dispatch": {"kind": "app_api",
                         "routes": ["GET /api/capabilities", "GET /api/usage", "GET /api/jobs",
                                    "GET /api/drawings/*", "GET /api/agent/audit"]},
            "args_schema": {"type": "object", "properties": {"what": {"type": "string"}},
                            "additionalProperties": False},
            "rate_limit_category": "low", "timeout_s": 30, "audit_extra": ["what"],
        },
        "run_read_tool": {
            "description": "Dispatch a registered drawing.read tool via POST /api/run (R2).",
            "rung": 2, "required_capability": "run_read", "policy": "auto",
            "dispatch": {"kind": "app_api", "routes": ["POST /api/run"]},
            "args_schema": _RUN_TOOL_SCHEMA,
            "rate_limit_category": "medium", "timeout_s": 60, "audit_extra": ["tool", "dwg"],
        },
        "run_write_tool": {
            "description": "Dispatch a registered drawing.write tool via POST /api/run (R3; creates vN+1, undoable).",
            "rung": 3, "required_capability": "run_write", "policy": "confirm-once",
            "dispatch": {"kind": "app_api", "routes": ["POST /api/run"]},
            "args_schema": _RUN_TOOL_SCHEMA,
            "rate_limit_category": "medium", "timeout_s": 120,
            "audit_extra": ["tool", "dwg"], "cost_class": "write",
        },
        "submit_live_solve": {
            "description": "Dispatch a live APS run (real USD; R4). Broker USD-cap preflight still fires underneath.",
            "rung": 4, "required_capability": "run_write", "policy": "confirm-once",
            "dispatch": {"kind": "app_api", "routes": ["POST /api/run"]},
            "args_schema": _RUN_TOOL_SCHEMA,
            "rate_limit_category": "high", "timeout_s": 600,
            "audit_extra": ["tool", "dwg"], "cost_class": "aps_usd",
        },
        "undo_drawing_version": {
            "description": "Undo: repoint the drawing head to a prior version (R3 safety valve — never harder than the write). DISABLED in Phase-1: no spine tool dispatches it and the wire contract §0 back-edge allowlist carries only GET /api/drawings/*, so POST /api/drawings/{dwg}/undo is unreachable from the harness and would 401. The user-facing undo is unaffected (the UI calls that route directly with a tenant token). Enabling this needs a contract revision adding the route to §0 plus a spine mapping — Phase-2.",
            "rung": 3, "required_capability": "run_write", "policy": "auto",
            "enabled": False,
            "dispatch": {"kind": "app_api", "routes": []},
            "args_schema": {"type": "object",
                            "properties": {"dwg": {"type": "string"},
                                           "to_version": {"type": "integer"}},
                            "additionalProperties": False},
            "rate_limit_category": "medium", "timeout_s": 30,
            "audit_extra": ["dwg", "to_version"],
        },
        "author_tool": {
            "description": "Author a new tool in the design-time sandbox (R5; nothing registered until R6).",
            "rung": 5, "required_capability": "build", "policy": "confirm-once",
            "dispatch": {"kind": "harness", "routes": ["POST /author"]},
            "args_schema": {"type": "object",
                            "properties": {"description": {"type": "string"},
                                           "mode": {"type": "string", "enum": ["build", "one_off"]},
                                           "confirmation_id": {"type": "string"}},
                            "required": ["description"], "additionalProperties": False},
            "rate_limit_category": "high", "timeout_s": 600, "audit_extra": [],
        },
        "register_tool": {
            "description": "Register/deploy an authored tool into the runnable catalog (R6; highest-leverage injection target).",
            "rung": 6, "required_capability": "deploy", "policy": "always-confirm",
            "tenant_tightenable": False,
            "dispatch": {"kind": "app_api", "routes": ["POST /api/author/register"]},
            "args_schema": {"type": "object",
                            "properties": {"tool": {"type": "string"},
                                           "manifest_sha256": {"type": "string"},
                                           "confirmation_id": {"type": "string"}},
                            "required": ["tool", "manifest_sha256"],
                            "additionalProperties": False},
            "rate_limit_category": "high", "timeout_s": 120,
            "audit_extra": ["tool", "manifest_sha256"],
        },
        "customize_platform": {
            "description": "Platform customization + redeploy (R7). Disabled everywhere at launch.",
            "rung": 7, "required_capability": "platform_customize",
            "policy": "always-confirm", "enabled": False, "tenant_tightenable": False,
            "dispatch": {"kind": "app_api", "routes": []},
            "args_schema": {"type": "object"},
            "rate_limit_category": "high", "timeout_s": 600, "audit_extra": [],
        },
    },
    "tier_overrides": {},
}


# --------------------------------------------------------------------------- #
# file resolution (env-overridable, read at call time like entitlements.py)
# --------------------------------------------------------------------------- #
def policy_file() -> Path:
    return Path(os.environ.get("LEAF_AGENT_POLICY_FILE") or _DEFAULT_FILE)


def tenants_file() -> Path:
    """Per-tenant agent state (agent_disabled flag + tighten-only overlay), a
    small JSON beside the policy file (env-overridable for tests)."""
    override = os.environ.get("LEAF_AGENT_TENANTS_FILE")
    return Path(override) if override else policy_file().with_name("agent_tenants.json")


# --------------------------------------------------------------------------- #
# load + validate
# --------------------------------------------------------------------------- #
def load_policy(path: Optional[Path] = None) -> AgentPolicy:
    """Parse + validate the catalog. A MISSING file -> hardcoded defaults
    (fail-safe); a present-but-invalid file -> PolicyError (the gate fails
    CLOSED on it — never a silent fallback that could mask a weakened edit)."""
    target = path or policy_file()
    if not target.exists():
        return _parse(_HARDCODED_DEFAULTS)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyError(f"agent policy file unreadable/invalid JSON at {target}: {exc}") from exc
    if not isinstance(raw, dict):
        raise PolicyError(f"agent policy top level must be a mapping ({target})")
    return _parse(raw)


def _parse(raw: Dict[str, Any]) -> AgentPolicy:
    unknown_top = {k for k in raw if not k.startswith("_")} - _TOP_LEVEL_KNOWN
    if unknown_top:
        raise PolicyError(f"unknown top-level fields {sorted(unknown_top)} — possible typo of a security field")

    # security_int first: `True == 1`, so a bare `!= 1` check accepted
    # `"version": true` as version 1.
    version = security_int(raw.get("version"), field="version")
    if version != 1:
        raise PolicyError(f"unsupported agent policy version: {version!r} (expected 1)")

    actions_raw = raw.get("actions", MISSING)
    if not isinstance(actions_raw, dict) or not actions_raw:
        raise PolicyError("`actions` must be a non-empty mapping of action_name -> record")

    actions: Dict[str, AgentAction] = {}
    for name, record in actions_raw.items():
        actions[name] = _parse_action(name, record)

    # Membership: an ABSENT block means "no budgets configured" (the gate denies
    # every category on a 0 budget), but a PRESENT null/list is corruption and
    # must not be read as either answer.
    rate_limits_raw = raw["rate_limits"] if "rate_limits" in raw else {}
    if not isinstance(rate_limits_raw, dict):
        raise PolicyError(f"`rate_limits` must be a mapping; got {rate_limits_raw!r}")
    rate_limits: Dict[str, int] = {}
    for k, v in rate_limits_raw.items():
        if not k.endswith("_per_hour"):
            raise PolicyError(f"rate_limits.{k}: keys must end with _per_hour")
        category = k[: -len("_per_hour")]
        if category not in _VALID_CATEGORIES:
            raise PolicyError(f"rate_limits.{k}: unknown category {category!r}")
        limit = security_int(v, field=f"rate_limits.{k}")
        if limit <= 0:
            raise PolicyError(f"rate_limits.{k}: must be a positive integer; got {limit}")
        rate_limits[category] = limit

    ttl = security_int(raw.get("approval_ttl_s", 300), field="approval_ttl_s")
    if ttl <= 0:
        raise PolicyError(f"approval_ttl_s: must be a positive integer; got {ttl}")

    # Membership, not `or {}`: an ACTIVE tier_overrides block corrupted to null /
    # 0 / "" collapsed to {} and silently restored every base action — including
    # the looser one an override was tightening. Absent means "no retunes"; any
    # present non-mapping refuses to load.
    tier_overrides = _parse_tier_overrides(
        raw["tier_overrides"] if "tier_overrides" in raw else {}, actions)

    return AgentPolicy(version=1, actions=actions, rate_limits=rate_limits,
                       approval_ttl_s=ttl, tier_overrides=tier_overrides)


def _parse_action(name: str, record: Any) -> AgentAction:
    if not isinstance(record, dict):
        raise PolicyError(f"actions.{name}: must be a mapping")
    unknown = set(record.keys()) - _ACTION_KNOWN_FIELDS
    if unknown:
        raise PolicyError(f"actions.{name}: unknown fields {sorted(unknown)} — possible typo of a security field")
    missing = _ACTION_REQUIRED_FIELDS - set(record.keys())
    if missing:
        raise PolicyError(f"actions.{name}: missing required fields {sorted(missing)}")

    policy = record["policy"]
    if policy not in _VALID_POLICIES:
        raise PolicyError(f"actions.{name}.policy: must be one of {sorted(_VALID_POLICIES)}; got {policy!r}")

    rung = record["rung"]
    if not isinstance(rung, int) or isinstance(rung, bool) or rung not in _VALID_RUNGS:
        raise PolicyError(f"actions.{name}.rung: must be an integer 0..7; got {rung!r}")

    cap = record["required_capability"]
    if cap not in CAPABILITIES:
        raise PolicyError(
            f"actions.{name}.required_capability: {cap!r} not a known capability {CAPABILITIES}")

    dispatch = record["dispatch"]
    if not isinstance(dispatch, dict):
        raise PolicyError(f"actions.{name}.dispatch: must be a mapping")
    if dispatch.get("kind") not in _VALID_DISPATCH_KINDS:
        raise PolicyError(
            f"actions.{name}.dispatch.kind: must be one of {sorted(_VALID_DISPATCH_KINDS)}; "
            f"got {dispatch.get('kind')!r}")
    routes = dispatch.get("routes")
    if not isinstance(routes, list) or any(not isinstance(r, str) for r in routes):
        raise PolicyError(f"actions.{name}.dispatch.routes: must be a list of strings")

    rlc = record["rate_limit_category"]
    if rlc not in _VALID_CATEGORIES:
        raise PolicyError(
            f"actions.{name}.rate_limit_category: must be one of {sorted(_VALID_CATEGORIES)}; got {rlc!r}")

    # Absent = "project nothing"; a present null is corruption, and this list is
    # what the audit trail records about the call — silently emptying it loses
    # the evidence for that action.
    audit_extra_raw = record["audit_extra"] if "audit_extra" in record else []
    if not isinstance(audit_extra_raw, list) or any(not isinstance(k, str) for k in audit_extra_raw):
        raise PolicyError(
            f"actions.{name}.audit_extra: must be a list of strings; got {audit_extra_raw!r}")

    # args_schema must itself be a valid JSON Schema — a malformed schema would
    # otherwise only fail inside the gate at request time (elevate discipline).
    # An ABSENT schema legitimately means "no arg validation for this action";
    # a PRESENT null said the same thing while looking like a written rule, so
    # a corrupted schema silently unvalidated every arg the model sent.
    args_schema: Optional[Dict[str, Any]] = None
    if "args_schema" in record:
        args_schema = record["args_schema"]
        if not isinstance(args_schema, dict):
            raise PolicyError(
                f"actions.{name}.args_schema: must be a mapping (JSON Schema object); "
                f"got {type(args_schema).__name__}")
        try:
            from jsonschema import Draft7Validator
            Draft7Validator.check_schema(args_schema)
        except ImportError:
            pass  # jsonschema not installed — best-effort skip
        except Exception as exc:
            raise PolicyError(f"actions.{name}.args_schema: not a valid JSON Schema: {exc}") from exc

    timeout_s = security_int(record.get("timeout_s", 600), field=f"actions.{name}.timeout_s")
    if timeout_s <= 0:
        raise PolicyError(f"actions.{name}.timeout_s: must be a positive integer; got {timeout_s}")

    cost_class = record.get("cost_class")
    if cost_class is not None and not isinstance(cost_class, str):
        raise PolicyError(f"actions.{name}.cost_class: must be a string or absent")

    return AgentAction(
        name=name,
        description=str(record["description"]),
        rung=rung,
        required_capability=cap,
        policy=policy,
        dispatch={"kind": dispatch["kind"], "routes": list(routes)},
        args_schema=args_schema,
        rate_limit_category=rlc,
        timeout_s=timeout_s,
        audit_extra=tuple(audit_extra_raw),
        enabled=security_bool(record.get("enabled", MISSING),
                              field=f"actions.{name}.enabled", default=True),
        tenant_tightenable=security_bool(
            record.get("tenant_tightenable", MISSING),
            field=f"actions.{name}.tenant_tightenable", default=True),
        cost_class=cost_class,
    )


def _parse_tier_overrides(raw: Any, actions: Dict[str, AgentAction],
                          ) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Validate the operator's per-tier retune block. Overrides may move a
    tier's policy in either direction (this is how an `agent_write_autopilot`
    tier gets run_write_tool -> auto) EXCEPT on actions the catalog marks
    `tenant_tightenable: false` — those are not skippable at ANY tier, so any
    loosening (or per-tier re-enable of a disabled action) refuses to load."""
    if not isinstance(raw, dict):
        raise PolicyError("`tier_overrides` must be a mapping of tier -> {action -> fields}")
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for tier, per_action in raw.items():
        if not isinstance(per_action, dict):
            raise PolicyError(f"tier_overrides.{tier}: must be a mapping of action -> fields")
        tier_out: Dict[str, Dict[str, Any]] = {}
        for action_name, fields in per_action.items():
            base = actions.get(action_name)
            if base is None:
                raise PolicyError(
                    f"tier_overrides.{tier}.{action_name}: not a catalog action")
            if not isinstance(fields, dict):
                raise PolicyError(f"tier_overrides.{tier}.{action_name}: must be a mapping")
            unknown = set(fields.keys()) - _OVERRIDE_KNOWN_FIELDS
            if unknown:
                raise PolicyError(
                    f"tier_overrides.{tier}.{action_name}: unknown fields {sorted(unknown)}")
            entry: Dict[str, Any] = {}
            if "policy" in fields:
                new_policy = fields["policy"]
                if new_policy not in _VALID_POLICIES:
                    raise PolicyError(
                        f"tier_overrides.{tier}.{action_name}.policy: must be one of "
                        f"{sorted(_VALID_POLICIES)}; got {new_policy!r}")
                if (not base.tenant_tightenable
                        and POLICY_ORDER[new_policy] < POLICY_ORDER[base.policy]):
                    raise PolicyError(
                        f"tier_overrides.{tier}.{action_name}: cannot loosen policy of a "
                        f"tenant_tightenable:false action ({base.policy} -> {new_policy})")
                entry["policy"] = new_policy
            if "enabled" in fields:
                enabled = security_bool(fields["enabled"],
                                        field=f"tier_overrides.{tier}.{action_name}.enabled")
                if enabled and not base.enabled:
                    raise PolicyError(
                        f"tier_overrides.{tier}.{action_name}: cannot re-enable a globally "
                        f"disabled action per tier")
                entry["enabled"] = enabled
            tier_out[action_name] = entry
        out[tier] = tier_out
    return out


# --------------------------------------------------------------------------- #
# tier + tenant merge
# --------------------------------------------------------------------------- #
def apply_tier_overrides(pol: AgentPolicy, action: AgentAction, tier: Optional[str]) -> AgentAction:
    """The action as retuned for a tier (validated at load — apply verbatim)."""
    if not tier:
        return action
    fields = (pol.tier_overrides.get(tier) or {}).get(action.name)
    if not fields:
        return action
    return dataclasses.replace(action, **fields)


def apply_tenant_overlay(action: AgentAction, overlay: Optional[Dict[str, Any]]) -> AgentAction:
    """Merge a per-tenant overlay entry — TIGHTEN-ONLY. Policy may only move up
    the auto -> confirm-once -> always-confirm ordering; `enabled` may only go
    false. Any loosening attempt raises PolicyError (the gate fails closed)."""
    if overlay is None:
        return action
    if not isinstance(overlay, dict):
        raise PolicyError("tenant overlay: must be a mapping")
    # Membership BEFORE truthiness, and the type check before the empty check:
    # `if not entry` short-circuited on every falsy value, so a present null / 0
    # / "" / [] skipped the isinstance guard below and silently returned the
    # UNTIGHTENED base action — an operator's tightening quietly discarded.
    if action.name not in overlay:
        return action
    entry = overlay[action.name]
    if not isinstance(entry, dict):
        raise PolicyError(
            f"tenant overlay for {action.name}: must be a mapping; got "
            f"{type(entry).__name__} — dropping it would LOOSEN the tenant")
    if not entry:
        return action  # an EMPTY mapping legitimately means "no changes"
    unknown = set(entry.keys()) - _OVERRIDE_KNOWN_FIELDS
    if unknown:
        raise PolicyError(f"tenant overlay for {action.name}: unknown fields {sorted(unknown)}")
    updates: Dict[str, Any] = {}
    if "policy" in entry:
        new_policy = entry["policy"]
        if new_policy not in _VALID_POLICIES:
            raise PolicyError(
                f"tenant overlay for {action.name}.policy: must be one of "
                f"{sorted(_VALID_POLICIES)}; got {new_policy!r}")
        if POLICY_ORDER[new_policy] < POLICY_ORDER[action.policy]:
            raise PolicyError(
                f"tenant overlay for {action.name}: overlays can only TIGHTEN "
                f"({action.policy} -> {new_policy} is a loosening)")
        updates["policy"] = new_policy
    if "enabled" in entry:
        enabled = security_bool(entry["enabled"], field=f"tenant overlay {action.name}.enabled")
        if enabled and not action.enabled:
            raise PolicyError(
                f"tenant overlay for {action.name}: overlays can only TIGHTEN "
                f"(cannot re-enable a disabled action)")
        updates["enabled"] = enabled
    return dataclasses.replace(action, **updates) if updates else action


def effective_action(pol: AgentPolicy, action_name: str, *, tier: Optional[str] = None,
                     tenant_overlay: Optional[Dict[str, Any]] = None) -> Optional[AgentAction]:
    """Catalog lookup + tier retune + tenant tighten-only merge. None when the
    action is not in the catalog. Raises PolicyError on a loosening overlay."""
    base = pol.actions.get(action_name)
    if base is None:
        return None
    return apply_tenant_overlay(apply_tier_overrides(pol, base, tier), tenant_overlay)


# --------------------------------------------------------------------------- #
# per-tenant agent state (agent_disabled flag + overlay) — small JSON beside
# the policy file; written only by the ops surface.
# --------------------------------------------------------------------------- #
def load_tenant_state(tenant_id: str) -> Dict[str, Any]:
    """{agent_disabled: bool, overlay: {action -> fields}} for a tenant.

    A MISSING file -> permissive-empty state (this layer was never configured;
    the GLOBAL policy still applies in full). A file that EXISTS but is
    unreadable/malformed -> PolicyError, so the gate fails CLOSED: the same rule
    the unparseable kill flag below already follows, because every value this
    file carries is a TIGHTENING (kill flag, tighten-only overlay) and an I/O
    error must never silently lift one."""
    path = tenants_file()
    if not path.exists():
        return {"agent_disabled": False, "overlay": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyError(f"agent tenants file unreadable/invalid JSON at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise PolicyError(f"agent tenants top level must be a mapping ({path})")
    key = str(tenant_id)
    if key not in raw:
        return {"agent_disabled": False, "overlay": {}}
    entry = raw[key]
    if not isinstance(entry, dict):
        raise PolicyError(
            f"agent_tenants.{tenant_id}: must be a mapping ({path}); got "
            f"{type(entry).__name__} — a malformed entry must not silently drop "
            f"this tenant's kill flag and overlay")
    try:
        # MISSING (field omitted) legitimately means "not killed"; an explicit
        # null is unparseable and lands in the fail-CLOSED branch below.
        disabled = security_bool(entry.get("agent_disabled", MISSING),
                                 field=f"agent_tenants.{tenant_id}.agent_disabled")
    except PolicyError:
        disabled = True  # an unparseable KILL flag fails CLOSED, never open
    overlay: Dict[str, Any] = {}
    if "overlay" in entry:
        overlay = entry["overlay"]
        if not isinstance(overlay, dict):
            raise PolicyError(
                f"agent_tenants.{tenant_id}.overlay: must be a mapping ({path}); got "
                f"{type(overlay).__name__} — dropping it would LOOSEN the tenant")
    return {"agent_disabled": disabled, "overlay": overlay}


def set_tenant_agent_disabled(tenant_id: str, disabled: bool) -> Dict[str, Any]:
    """Persist the per-tenant agent kill flag (ops surface). Returns the entry."""
    path = tenants_file()
    raw: Dict[str, Any]
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raw = {}  # genuinely absent is the ONLY case a fresh mapping is safe:
        # rebuilding from {} over an existing-but-unreadable file would erase
        # every OTHER tenant's kill flag as a side effect of this one write.
    except OSError as exc:
        raise PolicyError(f"agent tenants file unreadable at {path}: {exc}") from exc
    else:
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PolicyError(f"agent tenants file invalid JSON at {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise PolicyError(f"agent tenants top level must be a mapping ({path})")
    # Membership, not .get(): a PRESENT `null` is corrupt state, not an absent
    # tenant, and .get() conflates them — rewriting it permissive would repair
    # corruption into an allow rather than refusing the write.
    key = str(tenant_id)
    if key not in raw:
        entry: Dict[str, Any] = {}
    else:
        entry = raw[key]
        if not isinstance(entry, dict):
            raise PolicyError(
                f"agent_tenants.{tenant_id}: must be a mapping ({path}); got "
                f"{type(entry).__name__} — refusing to overwrite corrupt state")
    # Not bool(): this WRITES the kill flag, and bool("") would persist a
    # not-killed tenant from a caller that meant nothing of the sort.
    entry["agent_disabled"] = security_bool(disabled, field="agent_disabled")
    raw[str(tenant_id)] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(raw, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return entry
