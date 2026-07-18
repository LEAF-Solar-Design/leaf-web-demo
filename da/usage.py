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
