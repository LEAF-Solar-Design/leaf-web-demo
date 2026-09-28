"""Tenant direct use and the APS engine usage observation (TCM-04).

Transparency of Leaf's real cost only, never billing: nothing here feeds Stripe,
quotas or caps. Collectors return plain JSON-able dicts and never write the
share ledger; a later publisher joins usage to cost by resource_id.

Contract:
- Amounts (usd_est, engine seconds) are decimal STRINGS, never floats. Token and
  run counts are ints.
- Unknown is null, never a fabricated 0. A source that was not supplied (None)
  or could not be read reports coverage "unknown" and null figures. A source
  that was read and holds no matching rows is a real zero, coverage "complete".
- Tenant isolation: a row counts only when its tenant_id equals the requested
  tenant exactly. Another tenant's rows are never summed.
- One linear pass per source, no per-row I/O, bounded marathon scan.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .ledger import ESTIMATED, MEASURED, PARTICIPANT_RE, PERIOD_RE, to_decimal

SERVER_DIR = Path(__file__).resolve().parent.parent

APS_RESOURCE_ID = "aps:engine"
APS_UNIT = "engine-second"

PAYER_TENANT = "tenant_plan"
PAYER_LEAF = "leaf"
PAYER_UNKNOWN = "unknown"
# agent_ledger grant_kind vocabulary (docs/AGENT-SPINE-DESIGN.md 6.2): oauth and
# api_key are the tenant's own linked Claude grant. No writer records a Leaf-key
# turn today; "leaf" is reserved for when one does. Anything else is unknown.
_PAYER_BY_GRANT_KIND = {"oauth": PAYER_TENANT, "api_key": PAYER_TENANT, "leaf": PAYER_LEAF}

# Same pre-flight denial filter as da/usage.py aggregate_usage: a denied run
# never touched APS and never spent, so it is neither a run nor engine use.
_DENIED_STATUSES = frozenset({"quota_exceeded", "TENANT_DISABLED"})

_TOKEN_FIELDS = (
    ("input", "tokens_in"),
    ("output", "tokens_out"),
    ("cache_read", "cache_read_tokens"),
    ("cache_write", "cache_creation_tokens"),
)


# --------------------------------------------------------------------------- #
# value helpers: fail closed to None, never to 0
# --------------------------------------------------------------------------- #
def _check_period(period: Any) -> str:
    if not isinstance(period, str) or not PERIOD_RE.fullmatch(period):
        raise ValueError(f"period must be YYYY-MM, got {period!r}")
    return period


def _check_tenant(tenant_id: Any) -> str:
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("tenant_id must be a non-empty string")
    return tenant_id


def _amount(value: Any) -> Optional[Decimal]:
    """A finite non-negative number as an exact Decimal, else None. A float is
    taken through its shortest repr so 0.1 stays 0.1, not its binary expansion."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        d = to_decimal(repr(value) if isinstance(value, float) else value, "amount")
    except (TypeError, ValueError):
        return None
    return d if d >= 0 else None


def _count(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _dec_str(d: Decimal) -> str:
    return format(d, "f")


def _row_period(ts: Any) -> Optional[str]:
    """The UTC YYYY-MM a row falls in: ISO string (agent ledger), epoch seconds
    (broker ledger) or datetime (a Postgres row). None when it cannot be placed."""
    if isinstance(ts, datetime):
        dt = ts if ts.tzinfo is None else ts.astimezone(timezone.utc)
        return dt.strftime("%Y-%m")
    if isinstance(ts, str):
        head = ts.strip()[:7]
        return head if PERIOD_RE.fullmatch(head) else None
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m")
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _dicts(rows: Iterable[Any]):
    """Yield dict rows; count the rest so a malformed row degrades coverage
    instead of vanishing silently."""
    for row in rows:
        yield row if isinstance(row, Mapping) else None


# --------------------------------------------------------------------------- #
# llm: the agent turn ledger
# --------------------------------------------------------------------------- #
def _llm_section(period: str, tenant_id: str, agent_rows: Optional[Iterable[Any]]) -> Dict[str, Any]:
    if agent_rows is None:
        return {"coverage": "unknown", "turns": None, "tokens": None, "usd_est": None,
                "payer": None, "by_payer": None, "source": "agent_ledger"}
    turns = 0
    tokens: Dict[str, Optional[int]] = {name: 0 for name, _ in _TOKEN_FIELDS}
    usd = Decimal(0)
    by_payer: Dict[str, Dict[str, Any]] = {}
    gaps = 0
    for row in _dicts(agent_rows):
        if row is None:
            gaps += 1
            continue
        if row.get("kind") != "turn" or row.get("tenant_id") != tenant_id:
            continue
        row_period = _row_period(row.get("ts"))
        if row_period is None:
            gaps += 1
            continue
        if row_period != period:
            continue
        turns += 1
        for name, field in _TOKEN_FIELDS:
            n = _count(row.get(field))
            if n is None:
                gaps += 1
            else:
                tokens[name] += n
        row_usd = _amount(row.get("usd_est"))
        if row_usd is None:
            gaps += 1
        else:
            usd += row_usd
        payer = _PAYER_BY_GRANT_KIND.get(row.get("grant_kind"), PAYER_UNKNOWN)
        if payer == PAYER_UNKNOWN:
            gaps += 1
        bucket = by_payer.setdefault(payer, {"turns": 0, "usd_est": Decimal(0)})
        bucket["turns"] += 1
        if row_usd is not None:
            bucket["usd_est"] += row_usd
    if not by_payer:
        payer_label: Optional[str] = None
    elif len(by_payer) == 1:
        payer_label = next(iter(by_payer))
    else:
        payer_label = "mixed"
    return {
        "coverage": "partial" if gaps else "complete",
        "turns": turns,
        "tokens": tokens,
        "usd_est": _dec_str(usd),
        "payer": payer_label,
        "by_payer": {k: {"turns": v["turns"], "usd_est": _dec_str(v["usd_est"])}
                     for k, v in sorted(by_payer.items())},
        "source": "agent_ledger",
    }


# --------------------------------------------------------------------------- #
# cad: the broker attribution ledger (one line per /broker/run)
# --------------------------------------------------------------------------- #
def _counted_cad_row(row: Mapping[str, Any], period: str) -> Optional[bool]:
    """True when the row is an APS run in the period, False when it is out of
    period or a denial, None when its period cannot be placed."""
    if row.get("status") in _DENIED_STATUSES:
        return False
    row_period = _row_period(row.get("ts"))
    if row_period is None:
        return None
    return row_period == period


def _cad_section(period: str, tenant_id: str, broker_rows: Optional[Iterable[Any]]) -> Dict[str, Any]:
    if broker_rows is None:
        return {"coverage": "unknown", "runs": None, "engine_seconds": None, "usd_est": None,
                "runs_without_job_id": None, "source": "broker_ledger"}
    runs = 0
    seconds = Decimal(0)
    usd = Decimal(0)
    without_job = 0
    gaps = 0
    for row in _dicts(broker_rows):
        if row is None:
            gaps += 1
            continue
        if row.get("tenant_id") != tenant_id:
            continue
        counted = _counted_cad_row(row, period)
        if counted is None:
            gaps += 1
            continue
        if not counted:
            continue
        runs += 1
        # Legacy rows predate job_id (#1490): still a run, just not joinable.
        if not isinstance(row.get("job_id"), str) or not row.get("job_id"):
            without_job += 1
        # A mock run (aps_live false) spends no APS money: null is its real zero.
        live = row.get("aps_live") is not False
        for field, acc in (("engine_seconds", "s"), ("usd_est", "u")):
            value = _amount(row.get(field))
            if value is None:
                if live:
                    gaps += 1
                continue
            if acc == "s":
                seconds += value
            else:
                usd += value
    return {
        "coverage": "partial" if gaps else "complete",
        "runs": runs,
        "engine_seconds": _dec_str(seconds),
        "usd_est": _dec_str(usd),
        "runs_without_job_id": without_job,
        "source": "broker_ledger",
    }


# --------------------------------------------------------------------------- #
# marathon: the tenant's multi-round runs (server/marathon_runs.py layout)
# --------------------------------------------------------------------------- #
def _marathon_section(period: str, tenant_id: str, marathon_root: Optional[Path]) -> Dict[str, Any]:
    """Bounded scan reusing marathon_runs' token check, caps and JSON reader.
    A run's period comes ONLY from its manifest's started_at (the marathon_runs
    contract: no manifest, no start); an undated run is counted apart and makes
    coverage partial. Marathon runs overlap llm and cad use: additive is false."""
    import marathon_runs

    empty = {"coverage": "unknown", "runs": None, "run_ids": None, "undated_runs": None,
             "additive": False, "source": "marathon_runs"}
    root = Path(marathon_root) if marathon_root is not None else marathon_runs._root()
    if root is None:
        return empty
    tenant = marathon_runs._token(tenant_id)
    if tenant is None:
        return empty
    tenant_dir = root / tenant
    try:
        if not root.is_dir():
            return empty
        if not tenant_dir.is_dir():
            return {**empty, "coverage": "complete", "runs": 0, "run_ids": [], "undated_runs": 0}
        entries: List[Path] = []
        truncated = False
        for p in tenant_dir.iterdir():
            if len(entries) >= marathon_runs.MAX_SCAN_ENTRIES:
                truncated = True
                break
            if p.is_dir() and not p.is_symlink() and marathon_runs._token(p.name):
                entries.append(p)
    except OSError:
        return empty
    run_ids: List[str] = []
    undated = 0
    skipped = 0
    for run_dir in sorted(entries, key=lambda p: p.name):
        state = marathon_runs._read_json_object(run_dir / "state.json", marathon_runs.MAX_STATE_BYTES)
        if state is None:
            skipped += 1
            continue
        manifest = marathon_runs._read_json_object(
            run_dir / "run-manifest.json", marathon_runs.MAX_SIDE_BYTES)
        start = _row_period(manifest.get("started_at")) if manifest else None
        if start is None:
            undated += 1
        elif start == period:
            run_ids.append(run_dir.name)
    return {
        "coverage": "partial" if (undated or skipped or truncated) else "complete",
        "runs": len(run_ids),
        "run_ids": run_ids[: marathon_runs.MAX_RUNS],
        "undated_runs": undated,
        "additive": False,
        "source": "marathon_runs",
    }


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def tenant_direct_use(
    period: str, tenant_id: str, *,
    agent_rows: Optional[Iterable[Any]],
    broker_rows: Optional[Iterable[Any]],
    marathon_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """ONE tenant's own use in ``period``: llm turns, cad runs, marathon runs.
    Pass None for a source that is absent; its section reads coverage unknown."""
    period = _check_period(period)
    tenant_id = _check_tenant(tenant_id)
    return {
        "kind": "tenant_direct_use",
        "period": period,
        "tenant_id": tenant_id,
        "llm": _llm_section(period, tenant_id, agent_rows),
        "cad": _cad_section(period, tenant_id, broker_rows),
        "marathon": _marathon_section(period, tenant_id, marathon_root),
    }


def aps_usage_observation(period: str, broker_rows: Optional[Iterable[Any]]) -> Dict[str, Any]:
    """The aps:engine usage observation for ``period``. total_usage sums every
    counted run's engine seconds, attributed or not, so a publisher can charge
    the remainder to leaf|unattributed. MEASURED only when every counted row has
    a usable tenant_id and engine_seconds; otherwise ESTIMATED, coverage partial.
    Denials and mock runs (aps_live false) never used the engine and are skipped."""
    period = _check_period(period)
    base = {"kind": "usage", "resource_id": APS_RESOURCE_ID, "period": period,
            "unit": APS_UNIT, "source": "broker_ledger"}
    if broker_rows is None:
        return {**base, "total_usage": None, "usages": {}, "status": ESTIMATED,
                "coverage": "unknown"}
    total = Decimal(0)
    per_tenant: Dict[str, Decimal] = {}
    gaps = 0
    for row in _dicts(broker_rows):
        if row is None:
            gaps += 1
            continue
        counted = _counted_cad_row(row, period)
        if counted is None:
            gaps += 1
            continue
        if not counted or row.get("aps_live") is False:
            continue
        seconds = _amount(row.get("engine_seconds"))
        tenant = row.get("tenant_id")
        if seconds is None:
            gaps += 1
            continue
        total += seconds
        if not isinstance(tenant, str) or not PARTICIPANT_RE.fullmatch(tenant):
            gaps += 1
            continue
        key = f"{tenant}|"
        per_tenant[key] = per_tenant.get(key, Decimal(0)) + seconds
    return {
        **base,
        "total_usage": _dec_str(total),
        "usages": {k: _dec_str(v) for k, v in sorted(per_tenant.items())},
        "status": ESTIMATED if gaps else MEASURED,
        "coverage": "partial" if gaps else "complete",
    }


# --------------------------------------------------------------------------- #
# thin loaders: reuse the existing ledgers' own readers, no new Postgres path
# --------------------------------------------------------------------------- #
def _parse_jsonl(lines: Iterable[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def load_agent_rows(path: Optional[Path] = None) -> Optional[List[Dict[str, Any]]]:
    """Agent turn rows through agent_ledger's own reader. A missing JSONL ledger
    is a real zero ([]); an unreadable one, or Postgres mode (whose store exposes
    only aggregates, not rows), is None: unknown."""
    import agent_ledger

    if path is None and agent_ledger._using_postgres():
        return None
    target = Path(path) if path is not None else agent_ledger.ledger_path()
    try:
        lines = agent_ledger._read_lines(target, raise_on_read_error=True)
    except OSError:
        return None
    return _parse_jsonl(lines)


def load_broker_rows(path: Optional[Path] = None) -> Optional[List[Dict[str, Any]]]:
    """Broker ledger rows from the JSONL attribution ledger broker.py appends to
    (BROKER_LEDGER, default server/broker_ledger.jsonl), the same file da/usage.py
    reads. Postgres mode exposes only per-tenant aggregates, so it is None."""
    if path is None and os.environ.get("LEAF_BROKER_STORE", "legacy").strip().lower() == "postgres":
        return None
    target = Path(path) if path is not None else Path(
        os.environ.get("BROKER_LEDGER", str(SERVER_DIR / "broker_ledger.jsonl")))
    if not target.exists():
        return []
    try:
        return _parse_jsonl(target.read_text(encoding="utf-8").splitlines())
    except OSError:
        return None
