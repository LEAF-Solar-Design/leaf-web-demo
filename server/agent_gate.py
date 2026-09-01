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

The default legacy authority uses durable JSON files. Setting
`LEAF_AGENT_STORE=postgres` switches approvals, grants, rates, audit events,
and the global kill state to fleet-visible PostgreSQL tables. Approvals are
bound to their confirmation id and exact {session, action,
canonical-json(args)} hash. The approved call is the executed call, args drift
denies, and a granted record redeems exactly once. Expired approvals auto-deny.
PostgreSQL is explicit and fail closed, with no fallback to files.

Every decision is audited (agent_audit.py) with args projected through the
action's audit_extra allowlist only. Denied reasons name the failing gate.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
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
# authority selection (resolved at call time for tests and staged rollout)
# --------------------------------------------------------------------------- #
def _using_postgres() -> bool:
    mode = os.environ.get("LEAF_AGENT_STORE", "legacy").strip().lower()
    if mode not in {"legacy", "postgres"}:
        raise RuntimeError("LEAF_AGENT_STORE must be 'legacy' or 'postgres'")
    return mode == "postgres"


def _pg_store():
    import agent_pg_store
    return agent_pg_store


def _audit_append(event: Dict[str, Any]) -> None:
    if _using_postgres():
        _pg_store().append_audit(event)
        return
    agent_audit.append(event)


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
    if _using_postgres():
        return _pg_store().kill_switch_details()["active"] is True
    return kill_file().exists()


def kill_switch_details() -> Dict[str, Any]:
    if _using_postgres():
        return _pg_store().kill_switch_details()
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
    # supplied id can never traverse out of the approvals dir. The literal
    # fullmatch restates what the collapse just guaranteed — stated as a
    # literal because that is the shape static analysis proves as a barrier
    # for the path built from it.
    safe = "".join(c for c in str(confirmation_id) if c.isalnum())[:64]
    m = re.fullmatch(r"[A-Za-z0-9]{0,64}", safe)
    if m is None:  # unreachable after the collapse; fail closed anyway
        raise ValueError("malformed confirmation id")
    return approvals_dir() / f"{m.group(0)}.json"


def _new_pending_record(*, tenant_id: str, session_id: str, turn_id: str,
                        action: AgentAction, args: Dict[str, Any],
                        ttl_s: int) -> Dict[str, Any]:
    confirmation_id = secrets.token_hex(8)
    now = _now()
    return {
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


def create_pending(*, tenant_id: str, session_id: str, turn_id: str,
                   action: AgentAction, args: Dict[str, Any], ttl_s: int) -> Dict[str, Any]:
    record = _new_pending_record(
        tenant_id=tenant_id, session_id=session_id, turn_id=turn_id,
        action=action, args=args, ttl_s=ttl_s,
    )
    if _using_postgres():
        return _pg_store().create_pending(record)
    _approval_path(record["confirmation_id"]).parent.mkdir(parents=True, exist_ok=True)
    _write_pending(record)
    return record


_RECORD_STR_FIELDS = ("confirmation_id", "tenant_id", "session_id", "action",
                      "args_hash", "expires_at")


def _record_is_wellformed(record: Any, confirmation_id: str) -> bool:
    """Type-validate an approval record at the READ boundary, and bind it to the
    id it was fetched under.

    Every downstream check in gate() is a comparison or a truth test, and a
    truth test on a corrupted value is an authorization decision made by
    accident: `"granted": "false"` is a truthy string, `"granted": 1` is not the
    bool this code wrote. create_pending() and _decide() only ever store real
    bools and real strings, so anything else is corruption or tampering, and
    validating once here is what stops each individual use site from having to
    remember the distinction. A malformed record reads as absent -> deny.

    The id binding is load-bearing for single-use: the record names the file it
    lives in, and the consumption stamp is written back through that name. A
    record for id A sitting at id B's path would let redemption through B stamp
    A as consumed while B stayed unconsumed — replayable for the rest of its
    TTL. It also collapses id ALIASES: `_approval_path` strips non-alphanumerics,
    so several spellings address one file, and only the exact minted id may
    redeem it.
    """
    if not isinstance(record, dict):
        return False
    for field in _RECORD_STR_FIELDS:
        if not isinstance(record.get(field), str):
            return False
    if record["confirmation_id"] != str(confirmation_id):
        return False
    # bools must be REAL bools, not truthy stand-ins
    for field in ("granted", "denied"):
        if not isinstance(record.get(field), bool):
            return False
    # written only on consumption, and only ever as an ISO string
    if "consumed_at" in record and not isinstance(record["consumed_at"], str):
        return False
    return True


def read_pending(confirmation_id: str) -> Optional[Dict[str, Any]]:
    record, _status = read_pending_strict(confirmation_id)
    return record


def read_pending_strict(confirmation_id: str) -> tuple:
    """(record, status) — status ∈ ok | absent | corrupt | io_error.

    read_pending() collapses everything non-ok to None, which is the right
    fail-closed read for the gate chain (an unreadable record must never
    authorize). The tenant-facing DECISION route needs the distinction (review
    round 2 finding): a transient IO failure must be a retryable error, not a
    silent "no gate record here" that lets the session-store decision proceed
    and later diverge from a recovered pending gate record. `corrupt` (bad
    JSON / malformed record) deliberately reads as absent-equivalent for
    callers that choose to: the gate chain itself will deny that record at
    resume, so proceeding session-only stays fail-safe.
    """
    if _using_postgres():
        record, status = _pg_store().read_pending_strict(confirmation_id)
        if record is not None and not _record_is_wellformed(record, confirmation_id):
            return None, "corrupt"
        return record, status
    path = _approval_path(confirmation_id)
    if not path.exists():
        return None, "absent"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None, "io_error"
    try:
        record = json.loads(raw)
    except json.JSONDecodeError:
        return None, "corrupt"
    if not _record_is_wellformed(record, confirmation_id):
        return None, "corrupt"
    return record, "ok"


def _write_pending(record: Dict[str, Any]) -> None:
    """Atomic publish (tmp file + os.replace), never an in-place rewrite.

    The decision route's pre-bridge probe (routers/agent.py
    read_pending_strict) deliberately runs OUTSIDE _state_lock; with an
    in-place write_text it can observe the truncate-write window of a
    concurrent locked decide, classify the record `corrupt`, and — since
    corrupt reads as absent-equivalent there — skip the gate bridge entirely,
    letting the two approval stores record OPPOSITE decisions
    (test_agent_approvals.py::test_concurrent_opposite_decisions_never_diverge_stores
    is the pin; measured ~3k torn reads/6s under contention on Windows).
    os.replace makes every reader see the old record or the new one, never a
    torn one. Windows needs the short retry: a reader holding the destination
    open for the instant of the swap fails the rename with a sharing violation;
    on exhaustion the error propagates and the decision route answers 503
    retryable, which is honest (nothing was recorded)."""
    path = _approval_path(record["confirmation_id"])
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    last_attempt = 19  # ~100ms total: far beyond any real reader's open window
    try:
        for attempt in range(last_attempt + 1):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == last_attempt:
                    raise
                time.sleep(0.005)
    except OSError:
        # any terminal replace failure (not just retry exhaustion) must not
        # leave a .tmp-* orphan behind; the error still propagates so the
        # decision route answers 503 retryable (nothing was recorded).
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _is_expired(record: Dict[str, Any], *, now: Optional[datetime] = None) -> bool:
    expires = _parse_iso(record.get("expires_at"))
    if expires is None:
        return True  # malformed expiry = expired (fail closed)
    return expires <= (now or _now())


def _decide(record: Dict[str, Any], *, granted: bool, by: str, reason: str) -> Dict[str, Any]:
    if _using_postgres():
        _ok, updated, _status = _pg_store().decide(
            record["confirmation_id"], granted=granted, by=by, reason=reason,
        )
        return updated if updated is not None else record
    record["granted"] = granted
    record["denied"] = not granted
    record["decided_at"] = _iso(_now())
    record["decided_by"] = by
    record["reason"] = reason
    _write_pending(record)
    return record


def grant_approval(confirmation_id: str, *, by: str = "tenant") -> Tuple[bool, Optional[Dict[str, Any]], str]:
    """Mark a pending approval granted. Returns (ok, record, reason). Refuses
    an already-decided or expired record (expiry racing the click auto-denies).

    ATOMIC under _state_lock (review round 3): the read-check-write must be one
    unit or two concurrent OPPOSITE clicks both read "pending" and the second
    write silently overwrites the first — the gate file and the (atomic,
    first-wins) session store could then record opposite decisions. With the
    lock, exactly one decision wins here and the loser observes
    already_decided, which the decision route refuses on contradiction.
    Same single-process posture as the redemption stamp below (multi-replica
    needs file locking — out of scope, same as SESSIONS_DB single-writer).
    """
    if _using_postgres():
        return _pg_store().decide(
            confirmation_id, granted=True, by=by, reason="approved",
        )
    with _state_lock:
        record = read_pending(confirmation_id)
        if record is None:
            return False, None, "not_found"
        if record.get("granted") or record.get("denied"):
            return False, record, "already_decided"
        if _is_expired(record):
            record = _decide(record, granted=False, by=by, reason="expired")
            _audit_append({"kind": "approval_denied", "confirmation_id": confirmation_id,
                           "tenant_id": record.get("tenant_id"),
                           "session_id": record.get("session_id"),
                           "action": record.get("action"), "reason": "expired"})
            return False, record, "expired"
        record = _decide(record, granted=True, by=by, reason="approved")
        _audit_append({"kind": "approval_granted", "confirmation_id": confirmation_id,
                       "tenant_id": record.get("tenant_id"),
                       "session_id": record.get("session_id"),
                       "action": record.get("action"), "decided_by": by})
        return True, record, "granted"


def deny_approval(confirmation_id: str, *, by: str = "tenant",
                  reason: str = "denied") -> Tuple[bool, Optional[Dict[str, Any]], str]:
    # Same atomicity contract as grant_approval (see its docstring).
    if _using_postgres():
        return _pg_store().decide(
            confirmation_id, granted=False, by=by, reason=reason,
        )
    with _state_lock:
        record = read_pending(confirmation_id)
        if record is None:
            return False, None, "not_found"
        if record.get("granted") or record.get("denied"):
            return False, record, "already_decided"
        record = _decide(record, granted=False, by=by, reason=reason)
        _audit_append({"kind": "approval_denied", "confirmation_id": confirmation_id,
                       "tenant_id": record.get("tenant_id"),
                       "session_id": record.get("session_id"),
                       "action": record.get("action"), "decided_by": by, "reason": reason})
        return True, record, "denied"


# --------------------------------------------------------------------------- #
# confirm-once session grants — keyed by (tenant, session, action, TARGET TOOL)
#
# All four components are load-bearing. The human approved ONE chip naming ONE
# tool: keying on the action alone would let an approved benign write authorize
# every later write in the session (a different, destructive tool included), and
# omitting the tenant would let anyone presenting the same session id inherit
# the grant. Legacy (unversioned) grant files grant NOTHING — an old entry
# cannot be re-keyed without inventing the tenant/tool it never recorded.
# --------------------------------------------------------------------------- #
_GRANTS_VERSION = 2


def _load_grants() -> Dict[str, str]:
    """{grant key -> ISO timestamp} from the snapshot, or {} — grants NOTHING.

    Validated whole: every entry must be the ISO-timestamp string
    record_session_grant writes, and one malformed entry rejects the entire
    snapshot rather than leaving callers to interpret it. Grants are the
    permissive half of the confirm-once tier, so an entry whose VALUE is null /
    0 / a list is not "a grant with an odd timestamp" — it is a key someone
    planted, and honouring its presence would authorize a write with no chip.
    Rejecting costs at most an extra confirmation chip; honouring costs the
    confirmation itself.
    """
    path = grants_file()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict) or raw.get("v") != _GRANTS_VERSION:
        return {}
    grants = raw.get("grants")
    if not isinstance(grants, dict):
        return {}
    for key, stamp in grants.items():
        if not isinstance(key, str) or not isinstance(stamp, str) or _parse_iso(stamp) is None:
            return {}
    return dict(grants)


def _subject_bound_grants_required(subject: Optional[str]) -> bool:
    """Whether this call must refuse to use or create an UNBOUND session grant.

    A confirm-once grant that does not name a person is shared by everyone in
    the tenant's session. Once auth is live that is never acceptable, so a call
    whose subject could not be resolved (a stale, foreign, or pre-migration
    turn) asks for its own confirmation rather than matching a legacy key. With
    auth off there is no subject to bind to and the open demo keeps its
    existing behaviour.
    """
    if subject:
        return False
    try:
        import deps  # noqa: PLC0415 - lazy, mirrors the rest of this module
        return bool(deps.auth_live())
    except Exception:  # noqa: BLE001 - an import failure must fail closed
        return True


def grant_target(args: Optional[Dict[str, Any]],
                 subject: Optional[str] = None) -> List[str]:
    """The target a grant authorizes, as a two-element form so an action with NO
    target tool can never share a key with one whose tool is literally named
    "none".

    A session is shared by every member of a tenant working the same drawing
    (sessions are UNIQUE(tenant_id, drawing_id)), so a grant that does not name
    the responsible person lets one member's approval authorize another
    member's later call. That is total for an action whose target does not
    distinguish the request: author_tool takes no `tool` argument, so its target
    is always ["none"]. Appending the subject binds the grant to the person who
    answered the chip. It rides `target`, which both the file and Postgres grant
    stores already carry end to end, so neither backend can bind while the other
    does not. A caller that cannot resolve a subject keeps the old shared key,
    and a key that stops matching costs one extra confirmation, never access.
    """
    target = ["tool", str(args["tool"])] if args and "tool" in args else ["none"]
    if subject:
        target = target + ["subject", str(subject)]
    return target


def _grant_key(tenant_id: str, session_id: str, action_name: str,
               target: List[str]) -> str:
    return json.dumps([str(tenant_id), str(session_id), str(action_name), target],
                      separators=(",", ":"))


def has_session_grant(tenant_id: str, session_id: str, action_name: str,
                      target: List[str]) -> bool:
    if _using_postgres():
        return _pg_store().has_session_grant(
            tenant_id, session_id, action_name, target)
    # the VALUE authorizes, never the key's presence: a grant is the timestamp
    # record_session_grant stamped, and _load_grants has already rejected any
    # snapshot carrying something else.
    stamp = _load_grants().get(_grant_key(tenant_id, session_id, action_name, target))
    return isinstance(stamp, str) and _parse_iso(stamp) is not None


def record_session_grant(tenant_id: str, session_id: str, action_name: str,
                         target: List[str]) -> None:
    if _using_postgres():
        _pg_store().record_session_grant(
            tenant_id, session_id, action_name, target)
        return
    path = grants_file()
    with _state_lock:
        # rebuilding from {} when _load_grants rejected the snapshot drops the
        # OTHER sessions' grants — the cost is one extra chip each, and keeping
        # unreadable entries alive across a rewrite is the opposite trade.
        grants = _load_grants()
        grants[_grant_key(tenant_id, session_id, action_name, target)] = _iso(_now())
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"v": _GRANTS_VERSION, "grants": grants},
                                  indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)


# --------------------------------------------------------------------------- #
# rate limiting — per-tenant per-hour by category. The JSON snapshot is the
# AUTHORITY, not a cache: every check re-reads it, spends the unit and rewrites
# it while holding an OS-level exclusive lock, so two uvicorn workers on the
# same host cannot keep divergent budgets or clobber each other's write. Three
# fail-closed rules make the budget un-erasable:
#   * a snapshot that EXISTS but is unreadable/malformed denies rather than
#     hydrating an empty budget (an unreadable budget is not "no budget");
#   * a malformed timestamp makes the whole bucket unreadable — silently
#     dropping it would ERASE spent units, which is the same hole from the
#     other side;
#   * a snapshot that cannot be written denies, because an unrecorded unit is
#     an unspent unit (a restart would otherwise hand the budget back).
# A MISSING snapshot is a genuinely empty budget (nothing written yet).
#
# RESIDUAL (Phase-1): the lock is a host-local file lock, so workers spread
# across hosts/containers that do not share the snapshot's filesystem still
# keep independent budgets. Closing that needs a shared transactional counter
# (Redis INCR or a Postgres row) rather than a file.
# --------------------------------------------------------------------------- #
_WINDOW_S = 3600.0

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None
try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None


@contextlib.contextmanager
def _snapshot_lock(path: Path):
    """Hold an exclusive OS lock covering the whole read-check-write of the
    rate snapshot. A sibling `.lock` file carries the lock so the snapshot
    itself stays atomically replaceable underneath it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path.with_name(path.name + ".lock"), "a+b")
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        handle.close()


def _reject_json_constant(token: str) -> float:
    """json.loads accepts the non-standard literals NaN / Infinity / -Infinity.
    NaN in a bucket is budget ERASURE: every comparison against it is False, so
    the window filter drops the stamp and hands the spent unit back. Refuse them
    at PARSE time so no later numeric check has to remember they exist."""
    raise ValueError(f"rate snapshot carries the non-JSON constant {token}")


def _read_rate_snapshot(path: Path) -> Dict[str, Dict[str, List[float]]]:
    """{tenant -> category -> [timestamps]} from the snapshot. Raises on a
    present-but-unusable snapshot; the caller turns that into a deny."""
    buckets: Dict[str, Dict[str, List[float]]] = {}
    if not path.exists():
        return buckets
    raw = json.loads(path.read_text(encoding="utf-8"),
                     parse_constant=_reject_json_constant)
    if not isinstance(raw, dict):
        raise ValueError("rate snapshot top level must be a mapping")
    for tenant, per_category in raw.items():
        if not isinstance(per_category, dict):
            raise ValueError(f"rate snapshot entry for {tenant!r} must be a mapping")
        for cat, stamps in per_category.items():
            if not isinstance(stamps, list):
                raise ValueError(f"rate snapshot bucket {tenant!r}.{cat!r} must be a list")
            for t in stamps:
                if not isinstance(t, (int, float)) or isinstance(t, bool):
                    raise ValueError(
                        f"rate snapshot bucket {tenant!r}.{cat!r} carries a non-numeric stamp")
                # a stamp no arithmetic can order (NaN/inf, however it arrived)
                # is unreadable, not zero — same rule as a malformed timestamp.
                # a stamp no arithmetic can order (NaN/inf, however it arrived)
                # is unreadable, not zero — same rule as a malformed timestamp.
                if not math.isfinite(t):
                    raise ValueError(
                        f"rate snapshot bucket {tenant!r}.{cat!r} carries a non-finite stamp")
            buckets.setdefault(str(tenant), {})[str(cat)] = [float(t) for t in stamps]
    return buckets


def _rate_check_and_record(tenant_id: str, category: str,
                           limits: Dict[str, int]) -> Tuple[bool, Dict[str, Any]]:
    limit = int(limits.get(category, 0) or 0)
    if limit <= 0:
        # a category with no configured budget fails CLOSED — the shipped
        # policy always carries all three, so this only fires on a bad edit.
        return False, {"category": category, "count": 0, "limit": 0,
                       "reason": f"rate_limit_exceeded: {category} (0/0)"}
    if _using_postgres():
        return _pg_store().consume_rate(tenant_id, category, limit)
    now = time.time()
    path = rate_file()
    unreadable = {"category": category, "count": 0, "limit": limit}
    with _state_lock:
        try:
            with _snapshot_lock(path):
                try:
                    buckets = _read_rate_snapshot(path)
                except (ValueError, json.JSONDecodeError) as exc:
                    return False, dict(unreadable,
                                       reason=f"rate_state_unreadable: {type(exc).__name__}")
                per_category = buckets.setdefault(str(tenant_id), {})
                bucket = [t for t in per_category.get(category, []) if now - t < _WINDOW_S]
                per_category[category] = bucket
                if len(bucket) >= limit:
                    return False, {"category": category, "count": len(bucket), "limit": limit,
                                   "reason": f"rate_limit_exceeded: {category} "
                                             f"({len(bucket)}/{limit})"}
                bucket.append(now)
                tmp = path.with_name(path.name + ".tmp")
                tmp.write_text(json.dumps(buckets), encoding="utf-8")
                tmp.replace(path)
                return True, {"category": category, "count": len(bucket), "limit": limit,
                              "reason": f"rate_limit_ok: {category} ({len(bucket)}/{limit})"}
        except OSError as exc:
            # locking, reading and persisting all fail the same way: an
            # unrecorded unit is an unspent unit, so deny rather than run on
            # budget nothing durably counted.
            return False, dict(unreadable,
                               reason=f"rate_state_unwritable: {type(exc).__name__}")


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
def _has_capability(tier_caps: Optional[Dict[str, bool]], capability: str) -> bool:
    """One entitlement read for the whole chain. entitlements_for() resolves
    every capability to a REAL bool, so `is True` is what that contract looks
    like on this side: an absent key, a null, or a quoted "false" (truthy!) is a
    capability map this gate cannot read, and an unreadable entitlement is not a
    granted one."""
    return (tier_caps or {}).get(capability) is True


def _result(decision: str, *, reason: str, policy: Optional[str] = None,
            rung: Optional[int] = None, confirmation_id: Optional[str] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"decision": decision, "reason": reason,
                           "policy": policy, "rung": rung}
    if confirmation_id is not None:
        out["confirmation_id"] = confirmation_id
    return out


def _plan_first_session(tenant_id: str, session_id: str) -> bool:
    """True when this session's approval policy is plan_first.

    `session_id` MUST be the AUTHORITY (app-owned) session — the one the
    client created and stored its policy against. The harness passes its own
    private session id on the gate call, and looking the policy up under THAT
    id finds nothing, silently allowing everything: plan-first was inert on the
    live path until a review caught it (round 1, finding 1).

    A read failure means we cannot tell whether the user asked for plan-first,
    and this is a CONSENT feature: it degrades toward ASKING. The cost is extra
    chips during a storage blip; the alternative silently executes against an
    explicit user choice. (Review round 1, finding 2 — I had it the other way.)
    """
    try:
        import session_policy
        return session_policy.get_policy(str(session_id), str(tenant_id)) == "plan_first"
    except Exception:  # noqa: BLE001
        return True


def gate(tenant_id: str, session_id: str, turn_id: str, action: str,
         args: Optional[Dict[str, Any]], tier_caps: Dict[str, bool], *,
         tier: Optional[str] = None,
         subject: Optional[str] = None,
         policy_session_id: Optional[str] = None) -> Dict[str, Any]:
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
        _audit_append(event)
        return _result("deny", reason=reason,
                       policy=act.policy if act else None,
                       rung=act.rung if act else None)

    # 1. kill switch — beats everything, including catalog membership. Read
    # one snapshot so a toggle cannot split the active check from its reason.
    info = kill_switch_details()
    if info.get("active") is True:
        return _deny(f"kill_switch_active: {info.get('reason') or '(no reason)'}",
                     extra={"gate": "kill_switch"})

    # 1b. per-tenant agent kill flag (ops surface) — independent of the run
    # kill-switch chain; still ahead of any policy work.
    try:
        tenant_state = agent_policy.load_tenant_state(
            tenant_id, pg_store=_pg_store() if _using_postgres() else None)
    except PolicyError as exc:
        # Class name only: a load failure's message can carry config paths,
        # and deny reasons travel back to the caller.
        return _deny(f"tenant_state_load_failed: {type(exc).__name__}",
                     extra={"gate": "tenant_disabled"})
    # `is not False`: load_tenant_state resolves the flag through security_bool
    # (an unparseable kill flag already fails closed there), so the only value
    # that may keep the agent running is an explicit False — a missing or
    # oddly-typed flag is state this gate cannot read, not permission.
    if tenant_state.get("agent_disabled") is not False:
        return _deny("tenant_agent_disabled", extra={"gate": "tenant_disabled"})

    # 2. catalog lookup (a policy file that fails STRICT validation denies —
    # never a silent fallback that could mask a weakened edit).
    try:
        pol = agent_policy.load_policy()
        act = agent_policy.effective_action(pol, action, tier=tier,
                                            tenant_overlay=tenant_state.get("overlay"))
    except PolicyError as exc:
        return _deny(f"policy_load_failed: {type(exc).__name__}",
                     extra={"gate": "catalog"})
    if act is None:
        return _deny("unknown_action", extra={"gate": "catalog"})
    if not act.enabled:
        return _deny("action_disabled", act=act, extra={"gate": "catalog"})

    # 3. args-schema validation — fail fast before any budget/approval work.
    schema_error = _validate_args(act, args)
    if schema_error is not None:
        return _deny(f"invalid_args: {schema_error}", act=act, extra={"gate": "args_schema"})

    # 4. entitlement — BEFORE rate limiting, so a denied call never burns budget.
    if not _has_capability(tier_caps, act.required_capability):
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
        return _deny(f"policy_load_failed: {type(exc).__name__}",
                     extra={"gate": "revalidate"})
    if act is None or not act.enabled:
        return _deny("action_disabled", act=act, extra={"gate": "revalidate"})
    schema_error = _validate_args(act, args)
    if schema_error is not None:
        return _deny(f"invalid_args: {schema_error}", act=act, extra={"gate": "revalidate"})
    if not _has_capability(tier_caps, act.required_capability):
        return _deny(f"entitlement_required: tier lacks '{act.required_capability}'",
                     act=act, extra={"gate": "revalidate"})

    # 6. rate limit — per-tenant per-hour by category.
    postgres_store = _using_postgres()
    rate_category = act.rate_limit_category
    rate_limit = int(pol.rate_limits.get(rate_category, 0) or 0)
    if rate_limit <= 0:
        return _deny(
            f"rate_limit_exceeded: {rate_category} (0/0)",
            act=act, extra={"gate": "rate_limit"})
    if not postgres_store:
        allowed, info = _rate_check_and_record(
            tenant_id, rate_category, pol.rate_limits)
        if not allowed:
            return _deny(info["reason"], act=act, extra={"gate": "rate_limit"})

    # 7. policy tier.
    projected = agent_audit.project_args(args, act.audit_extra)

    def _decision_event(kind: str, **fields: Any) -> Dict[str, Any]:
        event = dict(base_audit)
        event.update({"kind": kind, "policy": act.policy, "rung": act.rung,
                      "args": projected})
        event.update(fields)
        return event

    def _rate_rejected_event() -> Dict[str, Any]:
        return _decision_event("denied", gate="rate_limit")

    def _deny_result(reason: str) -> Dict[str, Any]:
        return _result("deny", reason=reason, policy=act.policy, rung=act.rung)

    def _allow(policy_outcome: str, confirmation_id: Optional[str] = None, *,
               audit: bool = True) -> Dict[str, Any]:
        event = _decision_event("allowed", policy_outcome=policy_outcome)
        if confirmation_id:
            event["confirmation_id"] = confirmation_id
        if audit:
            if postgres_store:
                accepted, info = _pg_store().consume_rate_and_audit(
                    tenant_id, rate_category, rate_limit,
                    accepted_event=event,
                    rejected_event=_rate_rejected_event(),
                )
                if not accepted:
                    return _deny_result(info["reason"])
            else:
                _audit_append(event)
        return _result("allow", reason=policy_outcome, policy=act.policy, rung=act.rung,
                       confirmation_id=confirmation_id)

    def _awaiting(record: Dict[str, Any], *, audit: bool = True) -> Dict[str, Any]:
        event = _decision_event(
            "approval_requested", confirmation_id=record["confirmation_id"])
        if audit:
            if postgres_store:
                accepted, info = _pg_store().consume_rate_and_audit(
                    tenant_id, rate_category, rate_limit,
                    accepted_event=event,
                    rejected_event=_rate_rejected_event(),
                )
                if not accepted:
                    return _deny_result(info["reason"])
            else:
                _audit_append(event)
        return _result("awaiting_approval", reason=f"action requires {act.policy} approval",
                       policy=act.policy, rung=act.rung,
                       confirmation_id=record["confirmation_id"])

    def _create_and_await() -> Dict[str, Any]:
        record = _new_pending_record(
            tenant_id=tenant_id, session_id=session_id, turn_id=turn_id,
            action=act, args=args, ttl_s=pol.approval_ttl_s,
        )
        if not postgres_store:
            _approval_path(record["confirmation_id"]).parent.mkdir(
                parents=True, exist_ok=True)
            _write_pending(record)
            return _awaiting(record)
        event = _decision_event(
            "approval_requested", confirmation_id=record["confirmation_id"])
        accepted, stored, info = _pg_store().create_pending_with_rate(
            record, category=rate_category, limit=rate_limit,
            accepted_event=event,
            rejected_event=_rate_rejected_event(),
        )
        if not accepted:
            return _deny_result(info["reason"])
        return _awaiting(stored, audit=False)

    # 7a. re-invoke path: the resumed call carries confirmation_id inside args.
    confirmation_id = args.get("confirmation_id")
    if confirmation_id:
        subject_match_required = (
            subject is not None or _subject_bound_grants_required(subject))
        if postgres_store:
            allow_event = _decision_event(
                "allowed",
                policy_outcome="allow_via_approval",
                confirmation_id=str(confirmation_id),
            )
            outcome_events = {
                "approval_not_found": _decision_event(
                    "denied", reason="approval_not_found", gate="policy"),
                "args_mismatch": _decision_event(
                    "denied", reason="args_mismatch", gate="policy"),
                "approval_denied": _decision_event(
                    "denied", reason="approval_denied", gate="policy"),
                "approval_subject_mismatch": _decision_event(
                    "denied", reason="approval_subject_mismatch", gate="policy"),
                "approval_expired": _decision_event(
                    "denied", reason="approval_expired", gate="policy"),
                "approval_consumed": _decision_event(
                    "denied", reason="approval_consumed", gate="policy"),
                "awaiting_approval": _decision_event(
                    "approval_requested", confirmation_id=str(confirmation_id)),
            }
            redeemed, record, redeem_reason = _pg_store().redeem(
                str(confirmation_id),
                tenant_id=tenant_id,
                session_id=session_id,
                action=act.name,
                args_hash=canonical_args_hash(args),
                subject=subject,
                subject_match_required=subject_match_required,
                audit_event=allow_event,
                session_grant_target=(
                    grant_target(args, subject)
                    if act.policy == "confirm-once"
                    and not _subject_bound_grants_required(subject) else None),
                rate_category=rate_category,
                rate_limit=rate_limit,
                rate_rejected_event=_rate_rejected_event(),
                outcome_events=outcome_events,
            )
            if redeemed:
                return _allow(
                    "allow_via_approval", confirmation_id=str(confirmation_id),
                    audit=False)
            if redeem_reason == "awaiting_approval" and record is not None:
                return _awaiting(record, audit=False)
            return _deny_result(redeem_reason)
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
        if record.get("denied") is not False:
            # `is not False` (not truthiness): read_pending guarantees a real
            # bool, and anything but an explicit False is a denial or corruption.
            return _deny("approval_denied", act=act, extra={"gate": "policy"})
        if subject_match_required and (
                subject is None or record.get("decided_by") != str(subject)):
            return _deny(
                "approval_subject_mismatch", act=act, extra={"gate": "policy"})
        if _is_expired(record):
            _decide(record, granted=False, by="system", reason="expired")
            return _deny("approval_expired", act=act, extra={"gate": "policy"})
        if record.get("granted") is True:
            # An approval redeems EXACTLY ONCE: the re-read + consumed-stamp
            # under _state_lock closes the replay window a granted record's
            # 300s TTL would otherwise leave open (one human click must never
            # authorize a rate-limit budget of duplicate writes/solves).
            with _state_lock:
                # NO `or record` fallback: re-reading under the lock is the
                # whole point of this block, and falling back to the pre-lock
                # copy on a failed/corrupt re-read hands back the stale, still
                # unconsumed record — reopening the exact replay window the lock
                # exists to close.
                fresh = read_pending(str(confirmation_id))
                if fresh is None:
                    return _deny("approval_unreadable", act=act, extra={"gate": "policy"})
                record = fresh
                # PRESENCE, not truthiness. This field has inverse polarity to
                # granted/denied: falsy means "not yet consumed", so a record
                # whose stamp is corrupted to null / "" / 0 would replay a spent
                # approval. create_pending never writes the key, and the only
                # writer below stores an ISO string, so present == consumed.
                if "consumed_at" in record:
                    return _deny("approval_consumed", act=act, extra={"gate": "policy"})
                # the decision could have flipped between the two reads
                if record.get("granted") is not True or record.get("denied") is not False:
                    return _deny("approval_denied", act=act, extra={"gate": "policy"})
                if subject_match_required and (
                        subject is None
                        or record.get("decided_by") != str(subject)):
                    return _deny(
                        "approval_subject_mismatch", act=act,
                        extra={"gate": "policy"})
                if _is_expired(record):
                    return _deny("approval_expired", act=act, extra={"gate": "policy"})
                record["consumed_at"] = _iso(_now())
                _write_pending(record)
            # confirm-once persists the grant for the exact tool the human saw
            # on the chip (repeats ride the grant, not the record);
            # always-confirm NEVER persists — the next call files a fresh
            # approval.
            if (act.policy == "confirm-once"
                    and not _subject_bound_grants_required(subject)):
                record_session_grant(tenant_id, session_id, act.name,
                                     grant_target(args, subject))
            return _allow("allow_via_approval", confirmation_id=str(confirmation_id))
        return _awaiting(record)

    # PLAN-FIRST: the session policy can force an otherwise-auto action to
    # confirm. Resolved HERE, in the gate, for two load-bearing reasons: only
    # the app may mint an approvable confirmation_id (a locally minted one
    # renders a chip that POST /api/agent/approvals/{id} answers 404), and the
    # gate already holds the session identity it needs. The harness therefore
    # needs NO new field and cannot assert the policy — otherwise a
    # tenant-controlled turn body could weaken its own gating.
    #
    # It only ever NARROWS: an `auto` action becomes a confirmation; deny stays
    # deny; an action already requiring approval is unchanged. An unreadable
    # policy leaves today's behavior, which is right for a TIGHTENING feature —
    # failing it closed would block every read on a storage blip.
    if act.policy == "auto" and _plan_first_session(
            tenant_id, policy_session_id or session_id):
        return _create_and_await()
    if act.policy == "auto":
        return _allow("allow")

    if act.policy == "confirm-once":
        # With auth live and no resolvable subject the only key available is the
        # legacy unbound one, which any pre-existing shared grant matches. Ask
        # for a fresh confirmation instead of riding it.
        if _subject_bound_grants_required(subject):
            return _create_and_await()
        if has_session_grant(tenant_id, session_id, act.name,
                             grant_target(args, subject)):
            return _allow("allow_via_session_grant")
        return _create_and_await()

    if act.policy == "always-confirm":
        return _create_and_await()

    if postgres_store:
        reason = f"unknown_policy: {act.policy}"
        event = _decision_event("denied", reason=reason, gate="policy")
        accepted, info = _pg_store().consume_rate_and_audit(
            tenant_id, rate_category, rate_limit,
            accepted_event=event, rejected_event=_rate_rejected_event())
        return _deny_result(reason if accepted else info["reason"])
    return _deny(f"unknown_policy: {act.policy}", act=act, extra={"gate": "policy"})


def list_pending(tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Undecided + unexpired approvals, optionally filtered to one tenant."""
    if _using_postgres():
        return _pg_store().list_pending(tenant_id)
    root = approvals_dir()
    if not root.exists():
        return []
    out: List[Dict[str, Any]] = []
    now = _now()
    for path in sorted(root.glob("*.json")):
        # one validating reader for this state file: a scalar JSON body has no
        # .get() (the listing crashed on it) and a malformed flag could show a
        # decided approval as still pending. The filename IS the id, so a file
        # whose name does not round-trip through _approval_path is not a record
        # this module wrote.
        confirmation_id = path.stem
        if _approval_path(confirmation_id) != path:
            continue
        record = read_pending(confirmation_id)
        if record is None:
            continue
        if record.get("granted") or record.get("denied"):
            continue
        if _is_expired(record, now=now):
            continue
        if tenant_id is not None and record.get("tenant_id") != str(tenant_id):
            continue
        out.append(record)
    return out
