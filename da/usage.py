"""da/usage.py — per-tenant usage attribution + hard pre-flight cost cap.

The per-tenant spend-cap KILL SWITCH the MATRIX asks for. Pure python: no APS
call, no credential. Two responsibilities:

  1. ATTRIBUTION. The AUTHORITATIVE source of per-tenant spend is the broker's
     append-only ledger (server/broker_ledger.jsonl — one JSONL line per run
     with usd_est). `spent_from_broker_ledger()` sums it. When the broker ledger
     is not available (e.g. broker not deployed / unit tests), an in-process
     `UsageLedger` is the local FALLBACK, matching the documented adapter seam in
     the plan's dependency notes.

  2. PRE-FLIGHT CAP. `check_cap(tenant_id, est_cost, cap, spent)` decides BEFORE
     any APS call whether a run may proceed. On breach it returns the shared
     structured envelope
         {ok:false, error_code:"quota_exceeded", message, retryable:false,
          degraded_mode:null}
     so the broker can reject the run before touching APS. This is the broker's
     hard pre-flight gate (ROOT-assumed billing posture: central broker-side hard
     pre-flight cap; see plans/billing-compliance-later/ coarse-quota-v1).

BILLING POSTURE (ROOT default, reversible): Leaf pre-authorizes/caps tenant APS
spend CENTRALLY (broker-side hard cap). The alternative (tenant BYO-APS creds,
making the cap advisory) is documented in da/setup_live.md. Caps are OFF unless a
positive cap is configured for a tenant (env LEAF_TENANT_CAP_USD default, or a
per-tenant map), so a demo/backbone run with no cap configured is never gated.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

# the plan's cap error code. NOT in the frozen server ErrorCode enum on purpose —
# it is a NEW machine-readable code this plan introduces; promoting it into the
# frozen envelope enum is an OPERATOR action (like the CONTRACT-ADDENDUM sections).
QUOTA_EXCEEDED = "quota_exceeded"

# default per-run cost estimate used for the pre-flight check when the caller does
# not supply one (the observed live extract/tool run is ~$0.006-0.008).
DEFAULT_EST_USD = float(os.environ.get("APS_EST_USD_PER_RUN", "0.008"))


# --------------------------------------------------------------------------- #
# cap configuration (OFF by default)
# --------------------------------------------------------------------------- #
def _caps_map() -> Dict[str, float]:
    """Optional per-tenant cap map from env LEAF_USAGE_CAPS (JSON) or a caps file
    (env LEAF_USAGE_CAPS_FILE). Missing/empty -> {} (no per-tenant caps)."""
    raw = os.environ.get("LEAF_USAGE_CAPS")
    if raw:
        try:
            return {str(k): float(v) for k, v in json.loads(raw).items()}
        except Exception:  # noqa: BLE001
            return {}
    fpath = os.environ.get("LEAF_USAGE_CAPS_FILE")
    if fpath and Path(fpath).exists():
        try:
            return {str(k): float(v) for k, v in json.loads(Path(fpath).read_text("utf-8")).items()}
        except Exception:  # noqa: BLE001
            return {}
    return {}


def cap_for(tenant_id: str, override: Optional[float] = None) -> Optional[float]:
    """Resolve the USD cap for a tenant, or None if uncapped.

    Precedence: explicit override > per-tenant map (LEAF_USAGE_CAPS[_FILE]) >
    global env LEAF_TENANT_CAP_USD. None/absent => uncapped (cap disabled).
    """
    if override is not None:
        return float(override)
    caps = _caps_map()
    if tenant_id in caps:
        return caps[tenant_id]
    g = os.environ.get("LEAF_TENANT_CAP_USD")
    if g not in (None, ""):
        try:
            return float(g)
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------- #
# quota envelope (built directly — NOT via envelopes.error_obj, which asserts the
# frozen enum and would reject the new quota_exceeded code)
# --------------------------------------------------------------------------- #
def quota_envelope(tenant_id: str, spent: float, est_cost: float, cap: float,
                   tool: Optional[str] = None) -> Dict[str, Any]:
    msg = (f"tenant {tenant_id!r} spend cap reached: "
           f"spent ${spent:.4f} + est ${est_cost:.4f} > cap ${cap:.4f}")
    return {
        "ok": False,
        "tool": tool,
        "result": None,
        "overlay": None,
        "cost": None,
        "error": {"error_code": QUOTA_EXCEEDED, "message": msg, "retryable": False},
        "error_code": QUOTA_EXCEEDED,   # top-level convenience mirror (plan §3 shape)
        "retryable": False,
        "message": msg,
        "degraded_mode": None,
    }


def check_cap(tenant_id: str, est_cost: Optional[float] = None,
              cap: Optional[float] = None, spent: float = 0.0,
              tool: Optional[str] = None) -> Dict[str, Any]:
    """Pre-flight decision. Returns {"ok": True, ...} to proceed, or a
    quota_exceeded envelope to REJECT (before any APS call).

    - est_cost defaults to DEFAULT_EST_USD.
    - cap is resolved via cap_for(tenant_id, cap); None => uncapped => always ok.
    - spent is the tenant's prior spend (pass spent_from_broker_ledger(...) for
      the authoritative figure, or UsageLedger.spent(...) for the local fallback).
    A run is admitted iff spent + est_cost <= cap.
    """
    est = DEFAULT_EST_USD if est_cost is None else float(est_cost)
    resolved_cap = cap_for(tenant_id, cap)
    if resolved_cap is None:
        return {"ok": True, "capped": False, "cap": None,
                "spent": spent, "est_cost": est, "projected": spent + est}
    projected = spent + est
    if projected > resolved_cap + 1e-12:
        env = quota_envelope(tenant_id, spent, est, resolved_cap, tool=tool)
        env.update({"capped": True, "cap": resolved_cap, "spent": spent,
                    "est_cost": est, "projected": projected})
        return env
    return {"ok": True, "capped": True, "cap": resolved_cap, "spent": spent,
            "est_cost": est, "projected": projected}


# --------------------------------------------------------------------------- #
# attribution — authoritative: the broker ledger
# --------------------------------------------------------------------------- #
def spent_from_broker_ledger(tenant_id: str, ledger_path) -> float:
    """Sum usd_est for a tenant from the broker's JSONL attribution ledger.

    Only ledger lines with status not in {quota_exceeded, TENANT_DISABLED} and a
    numeric usd_est count toward spend (denied pre-flight runs never spent). A
    missing/empty ledger => 0.0.
    """
    p = Path(ledger_path)
    if not p.exists():
        return 0.0
    total = 0.0
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if e.get("tenant_id") != tenant_id:
            continue
        if e.get("status") in (QUOTA_EXCEEDED, "TENANT_DISABLED"):
            continue
        usd = e.get("usd_est")
        if isinstance(usd, (int, float)):
            total += float(usd)
    return round(total, 6)


def aggregate_usage(tenant_id: str, ledger_path, now_ts: Optional[float] = None) -> Dict[str, Any]:
    """Aggregate a tenant's runs + spend from the broker ledger for GET /api/usage.

    Returns::

        {"today": {"runs": int, "usd_est": float},
         "total": {"runs": int, "usd_est": float}}

    A ledger line counts iff its ``tenant_id`` matches AND its ``status`` is NOT a
    pre-flight denial (``quota_exceeded`` / ``TENANT_DISABLED``) — a denied run
    never touched APS and never spent, so it is neither a run nor a charge. This
    is the SAME filter ``spent_from_broker_ledger`` applies, so
    ``total.usd_est == spent_from_broker_ledger(tenant_id, ledger_path)``.

    ``usd_est`` sums only numeric ``usd_est`` values (a mock ``APS_LIVE=0`` run
    whose ``usd_est`` is ``null`` still counts as a run but adds $0). ``today`` is
    the UTC-date bucket of each line's ``ts`` (epoch seconds); ``now_ts`` overrides
    "now" for deterministic tests. A missing/empty/corrupt ledger yields all
    zeros — this read NEVER raises.
    """
    today = (datetime.fromtimestamp(now_ts, tz=timezone.utc).date()
             if now_ts is not None else datetime.now(timezone.utc).date())
    total_runs = today_runs = 0
    total_usd = today_usd = 0.0
    p = Path(ledger_path)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if e.get("tenant_id") != tenant_id:
                continue
            if e.get("status") in (QUOTA_EXCEEDED, "TENANT_DISABLED"):
                continue
            total_runs += 1
            usd = e.get("usd_est")
            usd = float(usd) if isinstance(usd, (int, float)) else 0.0
            total_usd += usd
            ts = e.get("ts")
            if isinstance(ts, (int, float)):
                try:
                    if datetime.fromtimestamp(ts, tz=timezone.utc).date() == today:
                        today_runs += 1
                        today_usd += usd
                except (OverflowError, OSError, ValueError):
                    pass
    return {
        "today": {"runs": today_runs, "usd_est": round(today_usd, 6)},
        "total": {"runs": total_runs, "usd_est": round(total_usd, 6)},
    }


# --------------------------------------------------------------------------- #
# F12 + A4 (coarse-quota-v1): per-tenant DAILY RUN quota — a COUNT-based liability
# cap standing ALONGSIDE the USD spend cap above. It caps the NUMBER of APS-money
# runs a tenant may make per UTC day, keyed on the tenant's tier, so one runaway
# stranger can't loop-and-burn the shared APS credential.
#
# COUNTING RULE (spec §a): a quota unit = one run that enters the APS_LIVE=1 branch.
# APS_LIVE=0 (free/mock) runs are NEVER counted and never gated — they spend no
# Autodesk money — and a run already rejected as over-quota / tenant-disabled is not
# counted either. The broker records `aps_live` + `status` on every ledger line, so
# the live-only count is a filter over the AUTHORITATIVE broker ledger.
#
# No cron, no scheduler (spec §d): the "daily reset" is IMPLICIT because the count
# keys on the current UTC calendar day (YYYY-MM-DD). A new UTC day scans a fresh
# window of the ledger and yields a fresh count of 0 — yesterday's rows simply stop
# being read.
# --------------------------------------------------------------------------- #

# Free/unknown-tier default N (runs/tenant/UTC-day). OPERATOR-CONFIRMABLE {10,20,50}
# (spec §c). Read from env LEAF_DAILY_RUN_QUOTA at call time so retuning N is a
# config flip, not a redeploy; the built-in default is 20.
DEFAULT_DAILY_RUN_QUOTA = 20

# hosted_starter: a fixed placeholder multiple (10x free) that hardens into a real
# value when the billing SKUs get priced (spec §c). hosted_pro: UNMETERED.
_HOSTED_STARTER_DAILY_RUN_QUOTA = 200


def default_daily_run_quota() -> int:
    """Free/unknown-tier daily run cap N, from env LEAF_DAILY_RUN_QUOTA (default 20).
    A non-integer or negative value falls back to the built-in default."""
    raw = os.environ.get("LEAF_DAILY_RUN_QUOTA")
    if raw not in (None, ""):
        try:
            v = int(raw)
            if v >= 0:
                return v
        except (TypeError, ValueError):
            pass
    return DEFAULT_DAILY_RUN_QUOTA


def daily_run_limit_for(tier: Optional[str]) -> Optional[int]:
    """Runs-per-UTC-day limit for a tier. ``None`` == UNMETERED (never blocked).

    Tier map (spec §c), tier-aware and read live:
        demo / self_hosted        -> N   (env LEAF_DAILY_RUN_QUOTA, default 20 — free cap)
        restricted                -> N   (fail closed to the free cap)
        hosted_starter            -> 200 (placeholder 10x free)
        hosted_pro                -> None (unmetered; still counted for future billing)
        unknown / unauthenticated -> N   (FAIL CLOSED to the free cap — never fail open)
    """
    n = default_daily_run_quota()
    table: Dict[str, Optional[int]] = {
        "demo": n,
        "self_hosted": n,
        "restricted": n,
        "hosted_starter": _HOSTED_STARTER_DAILY_RUN_QUOTA,
        "hosted_pro": None,  # unmetered
    }
    if tier in table:
        return table[tier]
    return n  # unknown / unauthenticated -> free default (fail closed)


def daily_run_count(tenant_id: str, ledger_path, now_ts: Optional[float] = None) -> int:
    """Count a tenant's APS-money runs (``aps_live`` truthy) in the CURRENT UTC day,
    from the broker ledger.

    Per the spec §a counting rule this EXCLUDES APS_LIVE=0 (free/mock) runs and runs
    already rejected as over-quota / tenant-disabled (the same denial filter
    ``spent_from_broker_ledger`` / ``aggregate_usage`` apply). ``now_ts`` overrides
    "now" for deterministic tests. A missing/empty/corrupt ledger yields 0 — this read
    NEVER raises."""
    today = (datetime.fromtimestamp(now_ts, tz=timezone.utc).date()
             if now_ts is not None else datetime.now(timezone.utc).date())
    p = Path(ledger_path)
    if not p.exists():
        return 0
    count = 0
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if e.get("tenant_id") != tenant_id:
            continue
        if not e.get("aps_live"):  # spec §a: APS_LIVE=0 runs are un-metered/free
            continue
        if e.get("status") in (QUOTA_EXCEEDED, "TENANT_DISABLED"):
            continue
        ts = e.get("ts")
        if isinstance(ts, (int, float)):
            try:
                if datetime.fromtimestamp(ts, tz=timezone.utc).date() == today:
                    count += 1
            except (OverflowError, OSError, ValueError):
                continue
    return count


def daily_quota_envelope(tenant_id: str, tier: str, limit: int, used: int,
                         tool: Optional[str] = None) -> Dict[str, Any]:
    """The over-quota §10 envelope for the DAILY RUN cap (broker returns HTTP 429).

    Mirrors the spend-cap ``quota_envelope`` shape (a §10 ``error`` object + top-level
    convenience mirrors) but ``retryable=True`` (the cap lifts at 00:00 UTC) and carries
    ``tier``/``limit``/``used`` so the frontend renders an accurate upgrade prompt
    (spec §e). tier/limit/used are additive keys the frozen envelope_schema.json permits
    (it requires only error_code/message/retryable in the error object). ``degraded_mode``
    is coerced to a boolean by the broker on the wire (schema requires boolean)."""
    msg = (f"Daily run limit reached for your plan ({used}/{limit}). "
           f"Resets 00:00 UTC. Upgrade for more.")
    return {
        "ok": False,
        "tool": tool,
        "result": None,
        "overlay": None,
        "cost": None,
        "error": {"error_code": QUOTA_EXCEEDED, "message": msg, "retryable": True,
                  "tier": tier, "limit": limit, "used": used},
        "error_code": QUOTA_EXCEEDED,   # top-level convenience mirror (plan §3 shape)
        "retryable": True,
        "message": msg,
        "tier": tier,
        "limit": limit,
        "used": used,
        "degraded_mode": None,
    }


def daily_run_quota_check(tenant_id: str, tier: str, ledger_path,
                          now_ts: Optional[float] = None) -> Dict[str, Any]:
    """Pre-flight DAILY RUN quota decision (count-based, tier-keyed). Returns
    ``{"ok": True, ...}`` to PROCEED, or a ``quota_exceeded`` envelope to REJECT —
    BEFORE any APS call.

    An UNMETERED tier (limit ``None``, e.g. hosted_pro) always proceeds (still logged
    upstream for future billing). A metered run is admitted iff prior-runs-today
    ``used < limit`` (this run then becomes ``used+1 <= limit``); at ``used >= limit``
    it is rejected. ``now_ts`` overrides "now" for deterministic tests.
    """
    limit = daily_run_limit_for(tier)
    used = daily_run_count(tenant_id, ledger_path, now_ts=now_ts)
    if limit is None:
        return {"ok": True, "metered": False, "tier": tier, "limit": None, "used": used}
    if used >= limit:
        env = daily_quota_envelope(tenant_id, tier, limit, used)
        env.update({"metered": True, "tier": tier, "limit": limit, "used": used})
        return env
    return {"ok": True, "metered": True, "tier": tier, "limit": limit, "used": used}


# --------------------------------------------------------------------------- #
# attribution — local fallback: in-process append-only ledger
# --------------------------------------------------------------------------- #
class UsageLedger:
    """Append-only per-tenant spend, thread-safe. Local FALLBACK when the broker
    ledger is unavailable. Optionally mirrors to a JSONL path for durability."""

    def __init__(self, path: Optional[str] = None):
        self._spend: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._path = Path(path) if path else None
        if self._path and self._path.exists():
            self._load()

    def _load(self) -> None:
        for line in self._path.read_text(encoding="utf-8").splitlines():  # type: ignore[union-attr]
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                self._spend[e["tenant_id"]] = self._spend.get(e["tenant_id"], 0.0) + float(e["usd_est"])
            except Exception:  # noqa: BLE001
                continue

    def add(self, tenant_id: str, usd_est: float, meta: Optional[dict] = None) -> float:
        """Attribute a run's cost to a tenant. Returns the tenant's new total."""
        usd = float(usd_est or 0.0)
        with self._lock:
            self._spend[tenant_id] = round(self._spend.get(tenant_id, 0.0) + usd, 6)
            total = self._spend[tenant_id]
            if self._path:
                rec = {"tenant_id": tenant_id, "usd_est": usd}
                if meta:
                    rec.update(meta)
                with open(self._path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
        return total

    def spent(self, tenant_id: str) -> float:
        with self._lock:
            return self._spend.get(tenant_id, 0.0)

    def totals(self) -> Dict[str, float]:
        with self._lock:
            return dict(self._spend)
