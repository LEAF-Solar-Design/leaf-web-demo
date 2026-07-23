"""
Conversation metering ledger for the agent spine — append-only JSONL, one line
per TURN. Deliberately a NEW file (`LEAF_AGENT_LEDGER`, default
`data/agent_ledger.jsonl`): the broker attribution ledger's one-line-per-run
invariant (server/broker.py `_ledger_append`) is sacred and conversation turns
must never pollute it. Join key back to broker runs = `job_id` in the audit
records, not here.

Turn record (plan-policy schema; everything self-metered — there is NO balance
API, so `usd_est` is always an estimate):

    {kind:"turn", ts, tenant_id, session_id, turn_id, turn, grant_kind, model,
     tokens_in, tokens_out, cache_creation_tokens, cache_read_tokens,
     cost_tokens, usd_est, wall_seconds, tools_called:[names],
     stop_reason: end_turn|awaiting_approval|cap_hit|llm_quota_exhausted|error,
     degraded_mode}

Write discipline: append-only, NEVER raises (metering must not fail a turn the
tenant already paid for). Reads tolerate missing files + malformed lines.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

SERVER_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SERVER_DIR.parent

_write_lock = threading.Lock()


def _using_postgres() -> bool:
    mode = os.environ.get("LEAF_AGENT_STORE", "legacy").strip().lower()
    if mode not in {"legacy", "postgres"}:
        raise RuntimeError("LEAF_AGENT_STORE must be 'legacy' or 'postgres'")
    return mode == "postgres"


def _pg_store():
    import agent_pg_store
    return agent_pg_store


def ledger_path() -> Path:
    """Resolved at call time so subprocess/test env overrides apply."""
    override = os.environ.get("LEAF_AGENT_LEDGER")
    return Path(override) if override else (PROJECT_ROOT / "data" / "agent_ledger.jsonl")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def append(record: Dict[str, Any], *, path: Optional[Path] = None) -> None:
    """Append one ledger line. Fills `kind`/`ts` defaults. Never raises — a
    metering write failure is logged to stderr, not surfaced to the tenant."""
    if path is None and _using_postgres():
        try:
            _pg_store().append_usage(record)
        except Exception as exc:  # noqa: BLE001 - preserve best-effort metering
            print(f"[leaf-agent] ledger append failed: {exc}", file=sys.stderr, flush=True)
        return
    target = path or ledger_path()
    entry = dict(record)
    entry.setdefault("kind", "turn")
    entry.setdefault("ts", _now_iso())
    try:
        line = json.dumps(entry, separators=(",", ":"), sort_keys=True, default=str)
        target.parent.mkdir(parents=True, exist_ok=True)
        with _write_lock:
            with target.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception as exc:  # noqa: BLE001 - metering is best-effort by contract
        print(f"[leaf-agent] ledger append failed: {exc}", file=sys.stderr, flush=True)


def _bucket() -> Dict[str, Any]:
    return {"turns": 0, "cost_tokens": 0, "usd_est": 0.0}


def _accumulate(bucket: Dict[str, Any], entry: Dict[str, Any]) -> None:
    bucket["turns"] += 1
    try:
        bucket["cost_tokens"] += int(entry.get("cost_tokens") or 0)
    except (TypeError, ValueError):
        pass
    try:
        bucket["usd_est"] = round(bucket["usd_est"] + float(entry.get("usd_est") or 0.0), 6)
    except (TypeError, ValueError):
        pass


def aggregate(tenant_id: str, *, path: Optional[Path] = None) -> Dict[str, Any]:
    """The /api/usage `agent` block for a tenant: today (UTC date) + cycle
    (current UTC calendar month) turn counts, cost-tokens, and USD estimate.
    Missing/empty ledger -> zeros, never an error. `estimate_basis` is always
    "self_metered" — there is no balance API to reconcile against."""
    if path is None and _using_postgres():
        return _pg_store().aggregate_usage(str(tenant_id))
    target = path or ledger_path()
    now = datetime.now(timezone.utc)
    today_prefix = now.strftime("%Y-%m-%d")
    cycle_prefix = now.strftime("%Y-%m")
    today = _bucket()
    cycle = _bucket()
    if target.exists():
        try:
            lines = target.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("kind") != "turn" or entry.get("tenant_id") != str(tenant_id):
                continue
            ts = str(entry.get("ts") or "")
            if ts.startswith(cycle_prefix):
                _accumulate(cycle, entry)
            if ts.startswith(today_prefix):
                _accumulate(today, entry)
    return {"today": today, "cycle": cycle, "estimate_basis": "self_metered"}


def tenants_seen(*, path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """Every tenant with at least one turn line -> lifetime {turns, cost_tokens,
    usd_est}. Ops-surface aggregation; tolerant of malformed lines."""
    if path is None and _using_postgres():
        return _pg_store().usage_tenants()
    target = path or ledger_path()
    out: Dict[str, Dict[str, Any]] = {}
    if not target.exists():
        return out
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("kind") != "turn":
            continue
        tid = entry.get("tenant_id")
        if not tid:
            continue
        bucket = out.setdefault(str(tid), _bucket())
        _accumulate(bucket, entry)
    return out
