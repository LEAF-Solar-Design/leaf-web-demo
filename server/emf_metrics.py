"""APS domain metrics via CloudWatch EMF (Embedded Metric Format).

WHY EMF and not a scrape or PutMetricData:
  - The broker runs as an ephemeral Fargate task behind the ALB; there are
    multiple platform tasks and the ledger files (broker_ledger.jsonl, jobs.db)
    are per-task and disappear with the task. A file-scrape sidecar is invalid.
  - EMF needs no AWS SDK and no network call: we print ONE JSON line to stdout,
    the ECS awslogs driver ships it to CloudWatch Logs, and CloudWatch extracts
    the metrics automatically. Zero added dependency, zero added latency, and it
    survives task churn because the metric leaves the container as a log line.

CONTRACT (owned by the unified-observability plane; see CONTRACT.md):
  Namespace  : Leaf/Platform/APS
  Metrics    : BrokerRun (Count), EngineSeconds (Seconds), UsdEst (None),
               JobTerminal (Count)
  Dimensions : LOW-CARDINALITY ONLY -> {aps_live, status}. tenant_id and tool
               are LOG FIELDS, never metric dimensions (custom-metric cost
               explosion guard). tool can be promoted to a dimension by setting
               APS_EMF_TOOL_DIM=1 once its cardinality is known-bounded.

SAFETY: every public function is best-effort and NEVER raises into the caller.
  The broker's `finally: _ledger_append(entry)` and jobs' terminal callback must
  not break because a metric line failed to serialize. Emit failures print a
  one-line diagnostic to stderr and return.

No third-party imports on purpose (broker image declares no boto3 / SDK).
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, Optional

NAMESPACE = "Leaf/Platform/APS"

# Kill switch + knobs (env-driven; safe defaults).
_DISABLED = os.environ.get("APS_EMF_DISABLED", "") == "1"
_TOOL_AS_DIM = os.environ.get("APS_EMF_TOOL_DIM", "") == "1"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _emf_line(
    metrics: list[Dict[str, str]],
    dimension_sets: list[list[str]],
    values: Dict[str, Any],
    fields: Dict[str, Any],
    ts_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """Build one EMF document.

    metrics        : [{"Name": ..., "Unit": ...}, ...] (only non-null values)
    dimension_sets : [["aps_live","status"], ["aps_live"]] -> each set becomes a
                     separately-aggregatable metric; keep them SMALL and bounded.
    values         : metric name -> numeric value (must be JSON numbers)
    fields         : searchable log fields that are NOT dimensions (tenant_id, tool, run_id)
    """
    doc: Dict[str, Any] = {
        "_aws": {
            "Timestamp": ts_ms if ts_ms is not None else _now_ms(),
            "CloudWatchMetrics": [
                {
                    "Namespace": NAMESPACE,
                    "Dimensions": dimension_sets,
                    "Metrics": metrics,
                }
            ],
        }
    }
    # Dimension keys must be present as STRING-valued root properties.
    # Every key referenced by any dimension set is supplied via `fields`+`values`
    # by the caller; dimension values specifically are added by the caller as strings.
    doc.update(fields)
    doc.update(values)
    return doc


def _write(doc: Dict[str, Any]) -> None:
    # stdout: the awslogs driver ships stdout+stderr; EMF is parsed from the stream.
    sys.stdout.write(json.dumps(doc, separators=(",", ":"), allow_nan=False) + "\n")
    sys.stdout.flush()


def emit_broker_run(entry: Dict[str, Any]) -> None:
    """Emit the primary per-run APS metric from a FROZEN ledger entry.

    `entry` is the exact dict broker.py appends to broker_ledger.jsonl (the nine
    frozen keys, plus the optional additive `run_id`). Call this right after
    `_ledger_append(entry)` in broker_run's `finally`.
    """
    if _DISABLED:
        return
    try:
        aps_live = "true" if entry.get("aps_live") else "false"
        status = str(entry.get("status") or "unknown")
        tool = entry.get("tool")
        tool_s = str(tool) if tool else "none"

        # Dimension VALUES must be strings and present at the root.
        dim_values: Dict[str, Any] = {"aps_live": aps_live, "status": status}
        dim_keys = ["aps_live", "status"]
        if _TOOL_AS_DIM:
            dim_values["tool"] = tool_s
            dim_keys = ["aps_live", "status", "tool"]

        # Bounded dimension sets: full combo + an aps_live rollup.
        dimension_sets = [dim_keys, ["aps_live"]]

        metrics: list[Dict[str, str]] = [{"Name": "BrokerRun", "Unit": "Count"}]
        values: Dict[str, Any] = {"BrokerRun": 1}

        eng = entry.get("engine_seconds")
        if isinstance(eng, (int, float)) and not isinstance(eng, bool):
            metrics.append({"Name": "EngineSeconds", "Unit": "Seconds"})
            values["EngineSeconds"] = eng
        usd = entry.get("usd_est")
        if isinstance(usd, (int, float)) and not isinstance(usd, bool):
            metrics.append({"Name": "UsdEst", "Unit": "None"})
            values["UsdEst"] = usd

        # LOG FIELDS (searchable, NOT dimensions): high-cardinality attribution.
        fields: Dict[str, Any] = {
            "tenant_id": str(entry.get("tenant_id") or ""),
            "engine_op": str(entry.get("engine_op") or ""),
            "aps_endpoint": str(entry.get("aps_endpoint") or ""),
        }
        if not _TOOL_AS_DIM:
            fields["tool"] = tool_s
        # Correlation key. On origin/main the broker already threads
        # `event_key` (broker_usage_ledger.event_key, and jobs set it to
        # f"{job_id}:broker-run") — emit it as a log field so a metric/log
        # record joins to the Postgres ledger and to async_jobs.job_id. No
        # separate run_id is needed. `run_id` is still honored as a fallback.
        corr = entry.get("event_key") or entry.get("run_id")
        if corr:
            fields["event_key"] = str(corr)

        ts_ms = None
        ts = entry.get("ts")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool):
            ts_ms = int(ts * 1000)

        _write(_emf_line(metrics, dimension_sets, {**dim_values, **values}, fields, ts_ms))
    except Exception as exc:  # noqa: BLE001 - metrics must never break the request
        print(f"[emf] emit_broker_run failed: {type(exc).__name__}: {exc}", file=sys.stderr)


def emit_job_terminal(status: str, execution_path: Optional[str] = None) -> None:
    """Emit a job-lifecycle terminal metric. Call once, when a NEW terminal
    outcome is applied (the `return "applied"` path of complete_callback), not on
    duplicate/conflict/not_owner.
    """
    if _DISABLED:
        return
    try:
        status_s = str(status or "unknown")
        dim_values: Dict[str, Any] = {"status": status_s}
        dim_keys = ["status"]
        if execution_path in {"cloud", "local"}:
            dim_values["execution_path"] = execution_path
            dim_keys = ["status", "execution_path"]
        metrics = [{"Name": "JobTerminal", "Unit": "Count"}]
        values = {"JobTerminal": 1}
        _write(_emf_line(metrics, [dim_keys, ["status"]], {**dim_values, **values}, {}))
    except Exception as exc:  # noqa: BLE001
        print(f"[emf] emit_job_terminal failed: {type(exc).__name__}: {exc}", file=sys.stderr)


if __name__ == "__main__":
    # Smoke test: print sample EMF docs and validate they are JSON + well-formed.
    emit_broker_run({
        "ts": 1690000000.5, "tenant_id": "acme", "tool": "panelize",
        "engine_op": "solve", "aps_endpoint": "https://developer.api.autodesk.com",
        "aps_live": True, "engine_seconds": 12.3, "usd_est": 0.007, "status": "ok",
        "run_id": "11111111-2222-3333-4444-555555555555",
    })
    emit_broker_run({
        "ts": 1690000001.0, "tenant_id": "acme", "tool": None, "engine_op": "",
        "aps_endpoint": "https://developer.api.autodesk.com", "aps_live": False,
        "engine_seconds": None, "usd_est": None, "status": "QUOTA_EXCEEDED",
    })
    emit_job_terminal("failed", "cloud")
    emit_job_terminal("complete", "local")
