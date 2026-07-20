"""
The agent gate — every conversational tool call passes this chain, IN ORDER:

    kill switch -> tenant agent-disabled -> catalog lookup -> args schema
    -> entitlement -> revalidate (fresh policy load) -> rate limit -> policy tier

Ordering is load-bearing (elevate exemplar semantics):
  * the kill switch beats everything — even an unknown action reports the
    kill switch while it is active (the surface is OFF, full stop);
  * entitlement runs BEFORE rate limiting so a denied call never burns budget;
  * revalidate re-reads the policy file fresh so an operator edit landing
    mid-request takes effect for THIS call (TOCTOU close) — the re-loaded
    action is the one every later step trusts, and the args-schema +
    entitlement checks re-run against it (the earlier pass cleared the OLD
    definition, which may have been looser than the one that will dispatch);
  * the policy tier runs last: auto allows, confirm-once consults per-session
    grants, always-confirm ALWAYS files a pending approval (never persisted).

Approvals are durable JSON files under `data/agent_approvals/` (TTL from the
policy's approval_ttl_s, 300s), bound to the exact {session, action,
canonical-json(args)} hash — the approved call IS the executed call, an args
drift after approval denies, and a granted record redeems exactly once
(consumed_at stamp; replays deny). Expired approvals auto-deny. The kill switch is FILE-PRESENCE
(`LEAF_AGENT_KILL_FILE`) with deliberately NO API off-toggle — only someone
with filesystem access can lift it.

Every decision is audited (agent_audit.py) with args projected through the
action's audit_extra allowlist only. Denied reasons name the failing gate.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import agent_audit
import agent_policy
from agent_policy import AgentAction, AgentPolicy, PolicyError

SERVER_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SERVER_DIR.parent

_state_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# paths (resolved at call time so subprocess/test env overrides apply)
# --------------------------------------------------------------------------- #
def kill_file() -> Path:
    override = os.environ.get("LEAF_AGENT_KILL_FILE")
    return Path(override) if override else (PROJECT_ROOT / "data" / "agent.disabled")


def approvals_dir() -> Path:
    override = os.environ.get("LEAF_AGENT_APPROVALS_DIR")
    return Path(override) if override else (PROJECT_ROOT / "data" / "agent_approvals")


def grants_file() -> Path:
    override = os.environ.get("LEAF_AGENT_GRANTS_FILE")
    return Path(override) if override else (PROJECT_ROOT / "data" / "agent_session_grants.json")


def rate_file() -> Path:
    override = os.environ.get("LEAF_AGENT_RATE_FILE")
    return Path(override) if override else (PROJECT_ROOT / "data" / "agent_rate_state.json")


# --------------------------------------------------------------------------- #
# kill switch — file presence, read-only surface, no API off-toggle
# --------------------------------------------------------------------------- #
def kill_switch_active() -> bool:
    return kill_file().exists()


def kill_switch_details() -> Dict[str, Any]:
    target = kill_file()
    if not target.exists():
        return {"active": False}
    reason = ""
    try:
        with target.open("r", encoding="utf-8") as fh:
            reason = fh.readline().strip()[:200]
    except OSError:
        pass
    return {"active": True, "reason": reason}


# --------------------------------------------------------------------------- #
# canonical args binding
# --------------------------------------------------------------------------- #
def canonical_args_hash(args: Optional[Dict[str, Any]]) -> str:
    """sha256 of the canonical-JSON args, EXCLUDING confirmation_id — the
    re-invoke after approval carries the id inside args, and it must hash to
    the same binding the approval was filed under."""
    stripped = {k: v for k, v in (args or {}).items() if k != "confirmation_id"}
    canon = json.dumps(stripped, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(when: datetime) -> str:
    return when.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(raw: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# pending approvals — durable JSON files, TTL-bound, args-exact
# --------------------------------------------------------------------------- #
def _approval_path(confirmation_id: str) -> Path:
    # confirmation ids are server-minted hex; re-sanitize anyway so a caller-
    # supplied id can never traverse out of the approvals dir.
    safe = "".join(c for c in str(confirmation_id) if c.isalnum())[:64]
    return approvals_dir() / f"{safe}.json"


def create_pending(*, tenant_id: str, session_id: str, turn_id: str,
                   action: AgentAction, args: Dict[str, Any], ttl_s: int) -> Dict[str, Any]:
    confirmation_id = secrets.token_hex(8)
    now = _now()
    record = {
        "confirmation_id": confirmation_id,
        "tenant_id": str(tenant_id),
        "session_id": str(session_id),
        "turn_id": str(turn_id),
        "action": action.name,
        "args": dict(args or {}),
        "args_hash": canonical_args_hash(args),
        "policy": action.policy,
        "rung": action.rung,
        "created_at": _iso(now),
        "expires_at": _iso(now + timedelta(seconds=ttl_s)),
        "granted": False,
        "denied": False,
        "decided_at": None,
        "decided_by": None,
        "reason": None,
    }
    path = _approval_path(confirmation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    return record


def read_pending(confirmation_id: str) -> Optional[Dict[str, Any]]:
    path = _approval_path(confirmation_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_pending(record: Dict[str, Any]) -> None:
    path = _approval_path(record["confirmation_id"])
    path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")


def _is_expired(record: Dict[str, Any], *, now: Optional[datetime] = None) -> bool:
    expires = _parse_iso(record.get("expires_at"))
    if expires is None:
        return True  # malformed expiry = expired (fail closed)
    return expires <= (now or _now())


def _decide(record: Dict[str, Any], *, granted: bool, by: str, reason: str) -> Dict[str, Any]:
    record["granted"] = granted
    record["denied"] = not granted
    record["decided_at"] = _iso(_now())
    record["decided_by"] = by
    record["reason"] = reason
    _write_pending(record)
    return record


def grant_approval(confirmation_id: str, *, by: str = "tenant") -> Tuple[bool, Optional[Dict[str, Any]], str]:
    """Mark a pending approval granted. Returns (ok, record, reason). Refuses
    an already-decided or expired record (expiry racing the click auto-denies)."""
    record = read_pending(confirmation_id)
    if record is None:
        return False, None, "not_found"
    if record.get("granted") or record.get("denied"):
        return False, record, "already_decided"
    if _is_expired(record):
        record = _decide(record, granted=False, by=by, reason="expired")
        agent_audit.append({"kind": "approval_denied", "confirmation_id": confirmation_id,
                            "tenant_id": record.get("tenant_id"),
                            "session_id": record.get("session_id"),
                            "action": record.get("action"), "reason": "expired"})
        return False, record, "expired"
    record = _decide(record, granted=True, by=by, reason="approved")
    agent_audit.append({"kind": "approval_granted", "confirmation_id": confirmation_id,
                        "tenant_id": record.get("tenant_id"),
                        "session_id": record.get("session_id"),
                        "action": record.get("action"), "decided_by": by})
    return True, record, "granted"


def deny_approval(confirmation_id: str, *, by: str = "tenant",
                  reason: str = "denied") -> Tuple[bool, Optional[Dict[str, Any]], str]:
    record = read_pending(confirmation_id)
    if record is None:
        return False, None, "not_found"
    if record.get("granted") or record.get("denied"):
        return False, record, "already_decided"
    record = _decide(record, granted=False, by=by, reason=reason)
    agent_audit.append({"kind": "approval_denied", "confirmation_id": confirmation_id,
                        "tenant_id": record.get("tenant_id"),
                        "session_id": record.get("session_id"),
                        "action": record.get("action"), "decided_by": by, "reason": reason})
    return True, record, "denied"


# --------------------------------------------------------------------------- #
# confirm-once session grants — persisted per (session, action)
# --------------------------------------------------------------------------- #
def _load_grants() -> Dict[str, Any]:
    path = grants_file()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def has_session_grant(session_id: str, action_name: str) -> bool:
    return action_name in (_load_grants().get(str(session_id)) or {})


def record_session_grant(session_id: str, action_name: str) -> None:
    path = grants_file()
    with _state_lock:
        grants = _load_grants()
        grants.setdefault(str(session_id), {})[action_name] = _iso(_now())
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(grants, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)


# --------------------------------------------------------------------------- #
# rate limiting — per-tenant per-hour by category. The AUTHORITY is in-memory
# (keyed by snapshot path, because the env override moves per test/subprocess);
# the JSON file is only a restart snapshot so a bounce does not zero every
# tenant's hour bucket. Two fail-closed rules make the budget un-erasable:
#   * a snapshot that EXISTS but is unreadable/malformed denies rather than
#     hydrating an empty budget (an unreadable budget is not "no budget");
#   * the consumed unit is recorded in memory BEFORE the write, so a failed
#     persist cannot un-count it — it can only be forgotten across a restart.
# A MISSING snapshot is a genuinely empty budget (nothing written yet).
# --------------------------------------------------------------------------- #
_WINDOW_S = 3600.0

_rate_buckets_by_path: Dict[str, Dict[str, Dict[str, List[float]]]] = {}
_rate_hydrated: set = set()


def _rate_buckets(path: Path) -> Dict[str, Dict[str, List[float]]]:
    """The live {tenant -> category -> [timestamps]} map for `path`, hydrated
    from the snapshot exactly once. Raises on a present-but-unusable snapshot;
    the caller turns that into a deny (and retries the hydration next call)."""
    key = str(path)
    if key not in _rate_hydrated:
        buckets: Dict[str, Dict[str, List[float]]] = {}
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("rate snapshot top level must be a mapping")
            for tenant, per_category in raw.items():
                if not isinstance(per_category, dict):
                    raise ValueError(f"rate snapshot entry for {tenant!r} must be a mapping")
                for cat, stamps in per_category.items():
                    if not isinstance(stamps, list):
                        raise ValueError(f"rate snapshot bucket {tenant!r}.{cat!r} must be a list")
                    buckets.setdefault(str(tenant), {})[str(cat)] = [
                        float(t) for t in stamps if isinstance(t, (int, float))
                        and not isinstance(t, bool)]
        _rate_buckets_by_path[key] = buckets
        _rate_hydrated.add(key)
    return _rate_buckets_by_path[key]


def _rate_check_and_record(tenant_id: str, category: str,
                           limits: Dict[str, int]) -> Tuple[bool, Dict[str, Any]]:
    limit = int(limits.get(category, 0) or 0)
    if limit <= 0:
        # a category with no configured budget fails CLOSED — the shipped
        # policy always carries all three, so this only fires on a bad edit.
        return False, {"category": category, "count": 0, "limit": 0,
                       "reason": f"rate_limit_exceeded: {category} (0/0)"}
    now = time.time()
    path = rate_file()
    with _state_lock:
        try:
            buckets = _rate_buckets(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return False, {"category": category, "count": 0, "limit": limit,
                           "reason": f"rate_state_unreadable: {type(exc).__name__}"}
        per_category = buckets.setdefault(str(tenant_id), {})
        bucket = [t for t in per_category.get(category, []) if now - t < _WINDOW_S]
        per_category[category] = bucket
        if len(bucket) >= limit:
            return False, {"category": category, "count": len(bucket), "limit": limit,
                           "reason": f"rate_limit_exceeded: {category} ({len(bucket)}/{limit})"}
        bucket.append(now)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(buckets), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass  # snapshot is best-effort; the unit is already spent in memory
        return True, {"category": category, "count": len(bucket), "limit": limit,
                      "reason": f"rate_limit_ok: {category} ({len(bucket)}/{limit})"}


# --------------------------------------------------------------------------- #
# args-schema validation
# --------------------------------------------------------------------------- #
def _validate_args(action: AgentAction, args: Dict[str, Any]) -> Optional[str]:
    if action.args_schema is None:
        return None
    try:
        from jsonschema import Draft7Validator
        errors = list(Draft7Validator(action.args_schema).iter_errors(args))
    except ImportError:
        return None  # jsonschema missing — best-effort skip (mirrors policy load)
    if not errors:
        return None
    # Report only the JSON-pointer path and the failing validator keyword —
    # jsonschema's e.message embeds the offending instance repr, and this
    # string lands verbatim in the audit deny reason, which must stay inside
    # agent_audit's allowlist-plus-hash discipline (no raw arg values).
    messages = [
        f"{'.'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.validator}"
        for e in errors[:3]
    ]
    return "; ".join(messages)


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #
def _result(decision: str, *, reason: str, policy: Optional[str] = None,
            rung: Optional[int] = None, confirmation_id: Optional[str] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"decision": decision, "reason": reason,
                           "policy": policy, "rung": rung}
    if confirmation_id is not None:
        out["confirmation_id"] = confirmation_id
    return out


def gate(tenant_id: str, session_id: str, turn_id: str, action: str,
         args: Optional[Dict[str, Any]], tier_caps: Dict[str, bool], *,
         tier: Optional[str] = None) -> Dict[str, Any]:
    """Run the full gate chain for one proposed agent action.

    `tier_caps` is the caller-resolved entitlement map for the tenant's tier
    (entitlements.entitlements_for) — the gate never resolves identity itself.
    Returns {decision: allow|deny|awaiting_approval, reason, policy, rung,
    confirmation_id?}. Every path is audited; denied reasons name the gate.
    """
    args = dict(args or {})
    base_audit = {"tenant_id": str(tenant_id), "session_id": str(session_id),
                  "turn_id": str(turn_id), "action": action}

    def _deny(reason: str, *, act: Optional[AgentAction] = None,
              extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        event = dict(base_audit)
        event.update({"kind": "denied", "reason": reason})
        if act is not None:
            event["rung"] = act.rung
            event["policy"] = act.policy
            event["args"] = agent_audit.project_args(args, act.audit_extra)
        if extra:
            event.update(extra)
        agent_audit.append(event)
        return _result("deny", reason=reason,
                       policy=act.policy if act else None,
                       rung=act.rung if act else None)

    # 1. kill switch — beats everything, including catalog membership.
    if kill_switch_active():
        info = kill_switch_details()
        return _deny(f"kill_switch_active: {info.get('reason') or '(no reason)'}",
                     extra={"gate": "kill_switch"})

    # 1b. per-tenant agent kill flag (ops surface) — independent of the run
    # kill-switch chain; still ahead of any policy work.
    try:
        tenant_state = agent_policy.load_tenant_state(tenant_id)
    except PolicyError as exc:
        return _deny(f"tenant_state_load_failed: {exc}", extra={"gate": "tenant_disabled"})
    if tenant_state.get("agent_disabled"):
        return _deny("tenant_agent_disabled", extra={"gate": "tenant_disabled"})

    # 2. catalog lookup (a policy file that fails STRICT validation denies —
    # never a silent fallback that could mask a weakened edit).
    try:
        pol = agent_policy.load_policy()
        act = agent_policy.effective_action(pol, action, tier=tier,
                                            tenant_overlay=tenant_state.get("overlay"))
    except PolicyError as exc:
        return _deny(f"policy_load_failed: {exc}", extra={"gate": "catalog"})
    if act is None:
        return _deny("unknown_action", extra={"gate": "catalog"})
    if not act.enabled:
        return _deny("action_disabled", act=act, extra={"gate": "catalog"})

    # 3. args-schema validation — fail fast before any budget/approval work.
    schema_error = _validate_args(act, args)
    if schema_error is not None:
        return _deny(f"invalid_args: {schema_error}", act=act, extra={"gate": "args_schema"})

    # 4. entitlement — BEFORE rate limiting, so a denied call never burns budget.
    if not (tier_caps or {}).get(act.required_capability, False):
        return _deny(f"entitlement_required: tier lacks '{act.required_capability}'",
                     act=act, extra={"gate": "entitlement"})

    # 5. revalidate — FRESH policy load; an operator edit landing between the
    # catalog read above and dispatch takes effect for THIS call. The re-loaded
    # action is the one all remaining gates (and the caller's dispatch) trust,
    # so the schema + entitlement checks RE-RUN against it: steps 3-4 cleared
    # the old definition, and the refreshed one may carry a tighter args schema
    # or a different required_capability.
    try:
        pol = agent_policy.load_policy()
        act = agent_policy.effective_action(pol, action, tier=tier,
                                            tenant_overlay=tenant_state.get("overlay"))
    except PolicyError as exc:
        return _deny(f"policy_load_failed: {exc}", extra={"gate": "revalidate"})
    if act is None or not act.enabled:
        return _deny("action_disabled", act=act, extra={"gate": "revalidate"})
    schema_error = _validate_args(act, args)
    if schema_error is not None:
        return _deny(f"invalid_args: {schema_error}", act=act, extra={"gate": "revalidate"})
    if not (tier_caps or {}).get(act.required_capability, False):
        return _deny(f"entitlement_required: tier lacks '{act.required_capability}'",
                     act=act, extra={"gate": "revalidate"})

    # 6. rate limit — per-tenant per-hour by category.
    allowed, info = _rate_check_and_record(tenant_id, act.rate_limit_category, pol.rate_limits)
    if not allowed:
        return _deny(info["reason"], act=act, extra={"gate": "rate_limit"})

    # 7. policy tier.
    projected = agent_audit.project_args(args, act.audit_extra)

    def _allow(policy_outcome: str, confirmation_id: Optional[str] = None) -> Dict[str, Any]:
        event = dict(base_audit)
        event.update({"kind": "allowed", "policy_outcome": policy_outcome,
                      "policy": act.policy, "rung": act.rung, "args": projected})
        if confirmation_id:
            event["confirmation_id"] = confirmation_id
        agent_audit.append(event)
        return _result("allow", reason=policy_outcome, policy=act.policy, rung=act.rung,
                       confirmation_id=confirmation_id)

    def _awaiting(record: Dict[str, Any]) -> Dict[str, Any]:
        event = dict(base_audit)
        event.update({"kind": "approval_requested",
                      "confirmation_id": record["confirmation_id"],
                      "policy": act.policy, "rung": act.rung, "args": projected})
        agent_audit.append(event)
        return _result("awaiting_approval", reason=f"action requires {act.policy} approval",
                       policy=act.policy, rung=act.rung,
                       confirmation_id=record["confirmation_id"])

    # 7a. re-invoke path: the resumed call carries confirmation_id inside args.
    confirmation_id = args.get("confirmation_id")
    if confirmation_id:
        record = read_pending(str(confirmation_id))
        if record is None:
            return _deny("approval_not_found", act=act, extra={"gate": "policy"})
        if (record.get("tenant_id") != str(tenant_id)
                or record.get("session_id") != str(session_id)
                or record.get("action") != act.name
                or record.get("args_hash") != canonical_args_hash(args)):
            # approval is bound to the exact {session, action, canonical-
            # json(args)} — any drift after approval denies (the approved call
            # IS the call), and an approval filed in one session never redeems
            # in another (a redeeming session would otherwise gain a standing
            # confirm-once grant it was never shown a chip for).
            return _deny("args_mismatch", act=act, extra={"gate": "policy"})
        if record.get("denied"):
            return _deny("approval_denied", act=act, extra={"gate": "policy"})
        if _is_expired(record):
            _decide(record, granted=False, by="system", reason="expired")
            return _deny("approval_expired", act=act, extra={"gate": "policy"})
        if record.get("granted"):
            # An approval redeems EXACTLY ONCE: the re-read + consumed-stamp
            # under _state_lock closes the replay window a granted record's
            # 300s TTL would otherwise leave open (one human click must never
            # authorize a rate-limit budget of duplicate writes/solves).
            with _state_lock:
                record = read_pending(str(confirmation_id)) or record
                if record.get("consumed_at"):
                    return _deny("approval_consumed", act=act, extra={"gate": "policy"})
                record["consumed_at"] = _iso(_now())
                _write_pending(record)
            # confirm-once persists the grant per session (repeats ride the
            # grant, not the record); always-confirm NEVER persists — the next
            # call files a fresh approval.
            if act.policy == "confirm-once":
                record_session_grant(session_id, act.name)
            return _allow("allow_via_approval", confirmation_id=str(confirmation_id))
        return _awaiting(record)

    if act.policy == "auto":
        return _allow("allow")

    ttl_s = pol.approval_ttl_s
    if act.policy == "confirm-once":
        if has_session_grant(session_id, act.name):
            return _allow("allow_via_session_grant")
        record = create_pending(tenant_id=tenant_id, session_id=session_id,
                                turn_id=turn_id, action=act, args=args, ttl_s=ttl_s)
        return _awaiting(record)

    if act.policy == "always-confirm":
        record = create_pending(tenant_id=tenant_id, session_id=session_id,
                                turn_id=turn_id, action=act, args=args, ttl_s=ttl_s)
        return _awaiting(record)

    return _deny(f"unknown_policy: {act.policy}", act=act, extra={"gate": "policy"})


def list_pending(tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Undecided + unexpired approvals, optionally filtered to one tenant."""
    root = approvals_dir()
    if not root.exists():
        return []
    out: List[Dict[str, Any]] = []
    now = _now()
    for path in sorted(root.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if record.get("granted") or record.get("denied"):
            continue
        if _is_expired(record, now=now):
            continue
        if tenant_id is not None and record.get("tenant_id") != str(tenant_id):
            continue
        out.append(record)
    return out
