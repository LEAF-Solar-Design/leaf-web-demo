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
               JobTerminal (Count), ApprovalGiveBackFailed (Count),
               SubmitLatency (Milliseconds).
  Each metric is published EXACTLY ONCE per emit, under the single dimension set
  its CloudWatch consumer uses (a metric published under two dimension sets would
  double when summed across them):
    BrokerRun    -> {aps_live, status}   (per-status run counts / failure alarms)
    EngineSeconds-> {aps_live}           (avg engine seconds on live runs)
    UsdEst       -> {aps_live}           (daily cost-cap alarm sums live spend)
    JobTerminal  -> {status}             (job outcome counts)
    ApprovalGiveBackFailed -> {reason}   (an approval was consumed by a turn
                                          that never ran and could NOT be
                                          returned — the user's single approval
                                          is destroyed. Alarm on ANY value > 0.)
    SubmitLatency-> {aps_live}           (POST /api/run submit cost; the same
                                          dimension set EngineSeconds uses, so a
                                          p-statistic on submit sits beside the
                                          engine gauge and can be scoped to live
                                          traffic. ALARM: p99 > 200ms sustained
                                          — see the emitter's docstring.)
  Dimensions are LOW-CARDINALITY ONLY. tenant_id, tool, engine_op, aps_endpoint,
  event_key, execution_path, and job_id are LOG FIELDS, never dimensions.

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
import math
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

# Bounded allowlist for the `env` dimension, clamped for the same reason
# STATUS_ALLOW exists: a dimension whose values are not bounded is a cardinality
# leak. LEAF_RUNTIME_ENV is operator-set, so an unrecognised or missing value
# becomes "unknown" rather than minting a new metric series. It is read at EMIT
# time, not import time, so a test (or a process that sets it late) sees the
# current value.
ENV_ALLOW = frozenset({"production", "staging", "development"})


def _runtime_env() -> str:
    """The deployment posture this process is running under, as a dimension
    value. Sourced from LEAF_RUNTIME_ENV, which is a REQUIRED env var on every
    service enforcing the posture — unlike APS_LIVE, it actually names the
    environment (and is not inverted against it)."""
    try:
        raw = os.environ.get("LEAF_RUNTIME_ENV", "").strip().lower()
    except Exception:  # pragma: no cover - os.environ access must not raise here
        return "unknown"
    return raw if raw in ENV_ALLOW else "unknown"


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


GIVE_BACK_REASON_ALLOW = frozenset({"raised", "not_released"})


def emit_approval_give_back_failed(
    reason: str, confirmation_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> None:
    """Emit one give-back-failure metric (routers/sessions.py's APPROVAL
    GIVE-BACK path). Call when an approval was consumed for a turn that
    provably never ran and could NOT be put back: the user's single approval is
    destroyed, and without this the only trace is a stderr line nothing alarms
    on.

    `reason` is CLAMPED to GIVE_BACK_REASON_ALLOW ('raised' = the store call
    threw, 'not_released' = it returned False and no row moved) so the
    dimension stays bounded. confirmation_id / session_id are LOG FIELDS, never
    dimensions — they are per-request and would make the metric unbounded."""
    if _DISABLED:
        return
    try:
        raw = str(reason or "unknown")
        reason_s = raw if raw in GIVE_BACK_REASON_ALLOW else "unknown"
        directives = [
            {"Namespace": NAMESPACE, "Dimensions": [["reason"]],
             "Metrics": [{"Name": "ApprovalGiveBackFailed", "Unit": "Count"}]}
        ]
        root: Dict[str, Any] = {"reason": reason_s, "ApprovalGiveBackFailed": 1}
        if confirmation_id:
            root["confirmation_id"] = str(confirmation_id)
        if session_id:
            root["session_id"] = str(session_id)
        if reason_s != raw:
            root["reason_raw"] = raw
        _emit(directives, root)
    except Exception as exc:  # noqa: BLE001
        try:
            print(f"[emf] emit_approval_give_back_failed failed: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
        except Exception:  # pragma: no cover
            pass


def emit_submit_latency(
    ms: Any, aps_live: Any = False, tenant_id: Optional[str] = None,
    tool: Optional[str] = None, job_id: Optional[str] = None,
) -> None:
    """Emit the SUBMIT cost of one POST /api/run, in milliseconds.

    WHAT IT MEASURES: the request's own work — resolve the tool, check the
    catalog digest, gate the entitlement, exchange the checkout capability,
    insert the durable job row — up to the 202. It does NOT include job
    execution: the tool runs on a background worker after the 202 is written,
    so nothing the engine does can land in this number. Keeping those two apart
    is a contract, not an accident (server/tests/test_backbone.py
    ``test_1b_submit_cost_is_independent_of_execution_time``), and this metric
    is the production-side half of it.

    WHY IT EXISTS: `contract/CONTRACT.md` §7 documents POST /api/run as
    returning "**HTTP 202** immediately (<200 ms)". That is a p-latency claim on
    real traffic. A CI test can only bound a MEAN on a shared 2-to-4-core
    runner, which is why test_1b bounds independence rather than the 200ms
    itself. Nothing observable existed for the absolute bound until this metric.

    ALARM INTENT (create in the observability plane, not here):
        metric     Leaf/Platform/APS SubmitLatency, dimension env="production"
        statistic  p99 over a 5-minute period
        threshold  > 200 (ms) for 3 consecutive periods
        treat missing data as `notBreaching` (no traffic is not a breach)
      p99 (not Average) is the documented statistic: a 202 that is fast on
      average and 2 seconds at the tail has already broken the contract for the
      users who hit the tail.

    Published ONCE under {env}, NOT {aps_live}. This dimension was `aps_live`
    until 2026-07-27; it was wrong twice over, and the alarm built on it could
    never have worked:

      1. WRONG MEANING. Submit cost is measured on the 202 path, BEFORE any APS
         call — execution runs on a background worker afterwards. Whether a run
         would later touch live APS cannot affect this number, so scoping an
         HTTP-contract SLO by APS liveness scopes it by something unrelated.
      2. WRONG SOURCE, AND INVERTED. This emitter took `aps_live` from
         `deps.APS_LIVE`, a container ENV VAR, while emit_broker_run takes the
         identically-named dimension from `bool(req.aps_live)`, a PER-REQUEST
         ledger field. One dimension name, two meanings. Worse, that env var runs
         INVERTED against environment: production ships APS_LIVE=0 while staging
         ships APS_LIVE=1, so an alarm scoped to aps_live="true" pointed at
         STAGING while carrying a production name.

    `env` comes from LEAF_RUNTIME_ENV, already a REQUIRED environment variable on
    every service that enforces the deployment posture
    (server/tests/test_deployment_source_identity.py), so it is a real
    environment selector rather than a proxy for one. It is CLAMPED to a bounded
    allowlist for the same reason `status` is: a dimension must never be able to
    take an unbounded set of values.

    Changing a published metric's dimension set normally orphans its history.
    Not here: SubmitLatency has never been emitted in this account (verified
    2026-07-27; `list-metrics --namespace Leaf/Platform/APS --metric-name
    SubmitLatency` returns `{"Metrics": []}`), so there is no series to break and
    no alarm that has ever seen a datapoint. This is the last moment it is free.

    tenant_id, tool and job_id are LOW-VALUE-AS-DIMENSIONS and high cardinality:
    they are log fields, so a breaching p99 can still be attributed by querying
    the logs behind the metric. `aps_live` is kept as a LOG FIELD for exactly
    that reason — the attribution stays available without being a dimension.

    A NON-FINITE OR NEGATIVE reading is DROPPED, not floored to 0. A clock that
    produced garbage must not publish a fast sample: that would pull the p99
    down exactly when the measurement is broken, which is the one moment the
    alarm has to be trusted.
    """
    if _DISABLED:
        return
    try:
        if isinstance(ms, bool) or not isinstance(ms, (int, float)):
            return
        value = float(ms)
        if not math.isfinite(value) or value < 0.0:
            return
        directives = [
            {"Namespace": NAMESPACE, "Dimensions": [["env"]],
             "Metrics": [{"Name": "SubmitLatency", "Unit": "Milliseconds"}]}
        ]
        root: Dict[str, Any] = {
            "env": _runtime_env(),
            # Log field, not a dimension. See the docstring: this is the value
            # that used to BE the dimension, and it is neither an environment
            # selector nor sourced the same way as emit_broker_run's `aps_live`.
            "aps_live": "true" if aps_live else "false",
            "SubmitLatency": value,
        }
        if tenant_id:
            root["tenant_id"] = str(tenant_id)
        root["tool"] = str(tool) if tool else "none"
        if job_id:
            root["job_id"] = str(job_id)
        _emit(directives, root)
    except Exception as exc:  # noqa: BLE001 - metrics must never break the request
        try:
            print(f"[emf] emit_submit_latency failed: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
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
    emit_submit_latency(11.4, aps_live=True, tenant_id="acme",
                        tool="panelize", job_id="job-abc")
    # Dropped, not floored: a garbage clock must not publish a fast sample.
    emit_submit_latency(float("nan"), aps_live=True)
