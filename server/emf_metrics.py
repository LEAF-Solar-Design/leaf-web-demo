"""APS domain metrics via CloudWatch EMF (Embedded Metric Format).

WHY EMF and not a scrape or PutMetricData:
  - The broker runs as an ephemeral Fargate task behind the ALB; there are
    multiple platform tasks and the ledger files (broker_ledger.jsonl, jobs.db)
    are per-task and disappear with the task. A file-scrape sidecar is invalid.
  - EMF needs no AWS SDK and no network call: we print ONE JSON line to stderr,
    the ECS awslogs driver ships it to CloudWatch Logs, and CloudWatch extracts
    the metrics automatically. Zero added dependency, zero added latency, and it
    survives task churn because the metric leaves the container as a log line.

CONTRACT (owned by the unified-observability plane; see CONTRACT.md):
  Namespace  : Leaf/Platform/APS
  Metrics    : BrokerRun (Count), EngineSeconds (Seconds), UsdEst (None),
               JobTerminal (Count).
  Each metric is published EXACTLY ONCE per emit, under the single dimension set
  its CloudWatch consumer uses (a metric published under two dimension sets would
  double when summed across them):
    BrokerRun    -> {aps_live, status}   (per-status run counts / failure alarms)
    EngineSeconds-> {aps_live}           (avg engine seconds on live runs)
    UsdEst       -> {aps_live}           (daily cost-cap alarm sums live spend)
    JobTerminal  -> {status}             (job outcome counts)
  Dimensions are LOW-CARDINALITY ONLY. tenant_id, tool, engine_op, aps_endpoint,
  event_key, and execution_path are LOG FIELDS, never dimensions.

  `status` is CLAMPED to a bounded allowlist before it becomes a dimension: the
  broker copies a tool-returned envelope's error_code into the ledger status
  verbatim (broker.py), so an arbitrary tool could otherwise make `status` an
  unbounded metric dimension. Any value outside the allowlist becomes "other";
  the raw value is preserved as the status_raw log field.

SAFETY: every public function is best-effort and NEVER raises into the caller.
  The call sites live in broker/job `finally`/terminal paths, so even the
  fallback stderr write is wrapped: nothing here may escape.

Only first-party (server/) imports; the broker image declares no boto3 / SDK.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

NAMESPACE = "Leaf/Platform/APS"

_DISABLED = os.environ.get("APS_EMF_DISABLED", "") == "1"

# Bounded status allowlist for the BrokerRun `status` dimension. Built from the
# frozen ErrorCode enum plus the non-error terminals; a guarded import keeps this
# in sync with envelopes.py and falls back to a hardcoded copy off the hot path.
try:  # pragma: no cover - envelopes is a sibling module, always importable in server/
    from envelopes import ErrorCode as _EC

    _ENUM = tuple(getattr(_EC, "ALL", ()))
except Exception:  # pragma: no cover - defensive fallback, never expected
    _ENUM = (
        "UNKNOWN_TOOL", "BAD_PARAMS", "APS_UNAVAILABLE", "BROKER_UNREACHABLE",
        "WORKITEM_FAILED", "TIMEOUT", "TENANT_DISABLED", "GRANT_REQUIRED",
        "ENTITLEMENT_REQUIRED", "quota_exceeded", "INTERNAL", "turn_in_progress",
        "session_not_found", "llm_quota_exhausted", "llm_rate_limited",
        "confirmation_expired",
    )
STATUS_ALLOW = frozenset(_ENUM) | {"ok", "unknown", "error"}
JOB_STATUS_ALLOW = frozenset({"complete", "failed", "unknown"})


def _now_ms() -> int:
    return int(time.time() * 1000)


def _write(doc: Dict[str, Any]) -> None:
    # The awslogs driver ships both streams. Keep stdout free for subprocess
    # protocols whose callers parse exact result lines.
    sys.stderr.write(json.dumps(doc, separators=(",", ":"), allow_nan=False) + "\n")
    sys.stderr.flush()


def _emit(directives: List[Dict[str, Any]], root: Dict[str, Any],
          ts_ms: Optional[int] = None) -> None:
    """Write one EMF document. `directives` is the CloudWatchMetrics array (each
    entry pins its own Dimensions + Metrics); `root` supplies every dimension
    value (as a string) and metric value (as a number) plus log fields."""
    doc: Dict[str, Any] = {
        "_aws": {
            "Timestamp": ts_ms if ts_ms is not None else _now_ms(),
            "CloudWatchMetrics": directives,
        }
    }
    doc.update(root)
    _write(doc)


def emit_broker_run(entry: Dict[str, Any]) -> None:
    """Emit the primary per-run APS metrics from a broker ledger entry. Call once
    per ledgered /broker/run (and /broker/extract), right where the ledger line
    is written. BrokerRun is published once under {aps_live, status}; the cost /
    engine gauges under {aps_live} for the rollup alarm + widgets."""
    if _DISABLED:
        return
    try:
        aps_live = "true" if entry.get("aps_live") else "false"
        raw_status = str(entry.get("status") or "unknown")
        status = raw_status if raw_status in STATUS_ALLOW else "other"

        detail_metrics: List[Dict[str, str]] = [{"Name": "BrokerRun", "Unit": "Count"}]
        rollup_metrics: List[Dict[str, str]] = []
        values: Dict[str, Any] = {"BrokerRun": 1}

        eng = entry.get("engine_seconds")
        if isinstance(eng, (int, float)) and not isinstance(eng, bool):
            values["EngineSeconds"] = eng
            rollup_metrics.append({"Name": "EngineSeconds", "Unit": "Seconds"})
        usd = entry.get("usd_est")
        if isinstance(usd, (int, float)) and not isinstance(usd, bool):
            values["UsdEst"] = usd
            rollup_metrics.append({"Name": "UsdEst", "Unit": "None"})

        # BrokerRun (a count) is published ONLY under the detailed set, so it is
        # never double-counted. Gauges that a {aps_live} consumer needs go in the
        # rollup directive (and only there).
        directives: List[Dict[str, Any]] = [
            {"Namespace": NAMESPACE, "Dimensions": [["aps_live", "status"]], "Metrics": detail_metrics},
        ]
        if rollup_metrics:
            directives.append(
                {"Namespace": NAMESPACE, "Dimensions": [["aps_live"]], "Metrics": rollup_metrics}
            )

        tool = entry.get("tool")
        root: Dict[str, Any] = {
            "aps_live": aps_live,
            "status": status,
            "tenant_id": str(entry.get("tenant_id") or ""),
            "engine_op": str(entry.get("engine_op") or ""),
            "aps_endpoint": str(entry.get("aps_endpoint") or ""),
            "tool": str(tool) if tool else "none",
        }
        root.update(values)
        if status != raw_status:
            root["status_raw"] = raw_status
        # Correlation: main threads event_key (broker_usage_ledger.event_key,
        # f"{job_id}:broker-run" from jobs) — emit it so a log/metric record joins
        # to the Postgres ledger and async_jobs.job_id. No separate run_id needed.
        corr = entry.get("event_key") or entry.get("run_id")
        if corr:
            root["event_key"] = str(corr)

        ts = entry.get("ts")
        ts_ms = int(ts * 1000) if isinstance(ts, (int, float)) and not isinstance(ts, bool) else None

        _emit(directives, root, ts_ms)
    except Exception as exc:  # noqa: BLE001 - metrics must never break the request
        try:
            print(f"[emf] emit_broker_run failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        except Exception:  # pragma: no cover - even the stderr write must not raise
            pass


def emit_job_terminal(status: str, execution_path: Optional[str] = None) -> None:
    """Emit one job-lifecycle terminal metric. Call once, when a NEW terminal
    outcome is applied (not on duplicate/conflict/not_owner). JobTerminal is
    published once under {status}; execution_path is a log field, not a
    dimension (nothing consumes it as one, and it keeps the metric single)."""
    if _DISABLED:
        return
    try:
        raw = str(status or "unknown")
        status_s = raw if raw in JOB_STATUS_ALLOW else "unknown"
        directives = [
            {"Namespace": NAMESPACE, "Dimensions": [["status"]], "Metrics": [{"Name": "JobTerminal", "Unit": "Count"}]}
        ]
        root: Dict[str, Any] = {"status": status_s, "JobTerminal": 1}
        if execution_path in {"cloud", "local"}:
            root["execution_path"] = execution_path
        if status_s != raw:
            root["status_raw"] = raw
        _emit(directives, root)
    except Exception as exc:  # noqa: BLE001
        try:
            print(f"[emf] emit_job_terminal failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        except Exception:  # pragma: no cover
            pass


if __name__ == "__main__":
    # Smoke test: emit sample docs; validate they are JSON, single-publish, and
    # that an out-of-allowlist status is clamped.
    emit_broker_run({
        "ts": 1690000000.5, "tenant_id": "acme", "tool": "panelize",
        "engine_op": "solve", "aps_endpoint": "https://developer.api.autodesk.com",
        "aps_live": True, "engine_seconds": 12.3, "usd_est": 0.007, "status": "ok",
        "event_key": "job-abc:broker-run",
    })
    emit_broker_run({
        "ts": 1690000001.0, "tenant_id": "acme", "tool": None, "engine_op": "",
        "aps_endpoint": "https://developer.api.autodesk.com", "aps_live": False,
        "engine_seconds": None, "usd_est": None, "status": "quota_exceeded",
    })
    emit_broker_run({
        "ts": 1690000002.0, "tenant_id": "acme", "tool": "evil", "engine_op": "x",
        "aps_endpoint": "https://developer.api.autodesk.com", "aps_live": True,
        "engine_seconds": 1.0, "usd_est": 0.001, "status": "ARBITRARY_TOOL_CODE",
    })
    emit_job_terminal("failed", "cloud")
    emit_job_terminal("complete")
