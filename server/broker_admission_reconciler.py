"""Settle broker admissions only when the available evidence proves an outcome."""
from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping
import json
import math
import os
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any, Dict
from urllib.parse import quote

import requests

import broker


DEFAULT_MIN_AGE_S = 3600
MAX_APS_CHECKS_PER_TICK = 20
APS_STATUS_TIMEOUT_S = 10
_MAX_SIDECAR_BYTES = 16 * 1024 * 1024
_JOB_EVENT = re.compile(r"^(?P<job_id>[0-9a-f-]{36}):broker-(run|fallback)$")
_TERMINAL_FAILURES = frozenset({
    "failedDownload", "failedInstructions", "failedUpload",
    "failedLimitDataSize", "failedLimitProcessingTime", "cancelled",
})
_QUEUE_POSITIONS: Dict[str, int] = {}
_QUEUE_COUNTER = 0


def read_sidecar_correlations(path) -> Dict[str, str]:
    """Read a bounded snapshot; reject corrupt input rather than use a prefix."""
    try:
        with Path(path).open("rb") as stream:
            if os.fstat(stream.fileno()).st_size > _MAX_SIDECAR_BYTES:
                raise ValueError("workitem sidecar exceeds 16 MiB")
            data = stream.read(_MAX_SIDECAR_BYTES + 1)
    except FileNotFoundError:
        return {}
    if len(data) > _MAX_SIDECAR_BYTES:
        raise ValueError("workitem sidecar exceeds 16 MiB")
    correlations: Dict[str, str] = {}
    for line in data.decode("utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if not isinstance(record, dict):
            raise ValueError("invalid workitem sidecar record")
        job_id = record.get("job_id")
        event = record.get("event")
        if not isinstance(job_id, str) or not job_id or event not in {"open", "close"}:
            raise ValueError("invalid workitem sidecar event")
        if event == "close":
            correlations.pop(job_id, None)
        else:
            workitem_id = record.get("workitem_id")
            if not isinstance(workitem_id, str) or not workitem_id.strip():
                raise ValueError("invalid workitem sidecar correlation")
            correlations[job_id] = workitem_id
    return correlations


class ApsWorkitemStatusClient:
    """One authenticated, bounded status request using the broker's APS client."""

    def get_workitem_status(self, workitem_id: str) -> Mapping:
        da = broker._get_da()
        response = requests.get(
            f"{da.DA}/workitems/{quote(workitem_id, safe='')}",
            headers=da._auth_headers(), timeout=APS_STATUS_TIMEOUT_S,
            allow_redirects=False,
        )
        try:
            response.raise_for_status()
            if not 200 <= response.status_code < 300:
                raise requests.HTTPError("APS status response was not 2xx")
            result = response.json()
            if not isinstance(result, Mapping):
                raise ValueError("APS status response was not an object")
            return result
        finally:
            response.close()


def _print_alarm(record):
    print("[leaf-broker-reconciler] ALARM " + json.dumps(record),
          file=sys.stderr, flush=True)


def reconcile_once(*, aps_client, correlation=None, alarm=None, now=None,
                   min_age_s=DEFAULT_MIN_AGE_S,
                   max_aps_checks=MAX_APS_CHECKS_PER_TICK) -> Dict[str, Any]:
    if broker._broker_store_mode() != "postgres":
        return {"mode": "legacy", "checked": 0}
    rows = broker._postgres_store().list_executing(100)
    global _QUEUE_COUNTER
    summary: Dict[str, Any] = {
        "mode": "postgres", "checked": 0, "skipped_young": 0,
        "resolved": [], "alarmed": [],
    }
    emit = alarm if alarm is not None else _print_alarm
    timestamp = time.time() if now is None else now
    alarm_only = os.environ.get("LEAF_BROKER_RECONCILER_ALARM_ONLY") == "1"
    candidates = []
    for row in rows:
        if not row["age_seconds"] >= min_age_s:
            summary["skipped_young"] += 1
            continue
        candidates.append(row)
    live_keys = {row["event_key"] for row in candidates if row["aps_live"]}
    for event_key in list(_QUEUE_POSITIONS):
        if event_key not in live_keys:
            del _QUEUE_POSITIONS[event_key]
    # Enqueue all arrivals in store order before any checked row rejoins the back.
    for row in candidates:
        event_key = row["event_key"]
        if row["aps_live"] and event_key not in _QUEUE_POSITIONS:
            _QUEUE_COUNTER += 1
            _QUEUE_POSITIONS[event_key] = _QUEUE_COUNTER
    candidates.sort(key=lambda row: (
        _QUEUE_POSITIONS[row["event_key"]] if row["aps_live"] else -1
    ))
    jobs = Counter(
        match.group("job_id") for row in candidates
        if (match := _JOB_EVENT.fullmatch(row["event_key"])) is not None
    )
    sidecar = None
    sidecar_failed = False
    aps_checks = 0

    def raise_alarm(row, reason):
        record = {
            "event": "broker_admission_reconcile_alarm",
            "event_key": row["event_key"], "tenant_id": row["tenant_id"],
            "reason": reason, "age_seconds": row["age_seconds"],
        }
        summary["alarmed"].append(record)
        emit(record)

    for row in candidates:
        summary["checked"] += 1
        event_key, tenant_id = row["event_key"], row["tenant_id"]
        match = _JOB_EVENT.fullmatch(event_key)
        job_id = match.group("job_id") if match else None
        if job_id is not None and jobs[job_id] > 1:
            raise_alarm(row, "ambiguous_job_admissions")
            continue
        payload = {}
        if not row["aps_live"]:
            resolution = "confirmed_failed_no_charge"
            reason = "non-live-admission rule: no paid APS work is submitted"
            evidence_ref = f"non-live-admission:{event_key}"
        else:
            if job_id is None:
                raise_alarm(row, "event_key_not_job_bound")
                continue
            try:
                if correlation is not None:
                    workitem_id = correlation(job_id)
                else:
                    if sidecar_failed:
                        raise ValueError("workitem sidecar unreadable")
                    if sidecar is None:
                        try:
                            sidecar = read_sidecar_correlations(broker.ACTIVE_WORKITEMS_PATH)
                        except Exception:
                            sidecar_failed = True
                            raise
                    workitem_id = sidecar.get(job_id)
                    memory_workitem_id = broker.active_workitem_for(job_id)
                    if memory_workitem_id and memory_workitem_id != workitem_id:
                        raise_alarm(row, "correlation_unproven")
                        continue
            except Exception:
                raise_alarm(row, "sidecar_unreadable" if correlation is None
                            else "no_workitem_correlation")
                continue
            if not isinstance(workitem_id, str) or not workitem_id.strip():
                raise_alarm(row, "no_workitem_correlation")
                continue
            if aps_checks >= max_aps_checks:
                continue
            aps_checks += 1
            _QUEUE_COUNTER += 1
            _QUEUE_POSITIONS[event_key] = _QUEUE_COUNTER
            try:
                answer = aps_client.get_workitem_status(workitem_id)
                if not isinstance(answer, Mapping):
                    raise ValueError("APS status response was not an object")
                status = answer.get("status")
            except Exception:
                raise_alarm(row, "aps_status_unreadable")
                continue
            if status == "success":
                raise_alarm(row, "aps_succeeded_needs_operator")
                continue
            if not isinstance(status, str) or status not in _TERMINAL_FAILURES:
                raise_alarm(row, "aps_not_terminal")
                continue
            resolution = "verified_terminal"
            reason = f"APS terminal-failure rule: WorkItem {workitem_id} status={status}"
            evidence_ref = f"aps-workitem:{workitem_id}:{status}"
            payload = {
                "result": broker.err_envelope(
                    broker.ErrorCode.WORKITEM_FAILED,
                    f"WorkItem {workitem_id} status={status}", retryable=False),
                "http_status": broker.DEFAULT_HTTP_STATUS[broker.ErrorCode.WORKITEM_FAILED],
                "ledger_entry": {
                    "ts": timestamp, "tenant_id": tenant_id, "tool": None,
                    "engine_op": "", "aps_endpoint": broker.APS_ENDPOINT,
                    "aps_live": True, "engine_seconds": None, "usd_est": None,
                    "status": "WORKITEM_FAILED", "job_id": job_id,
                },
            }
        if alarm_only:
            raise_alarm(row, "alarm_only_mode")
            continue
        try:
            request = broker.BrokerAdmissionResolution(
                tenant_id=tenant_id, resolution=resolution,
                operator_id="reconciler", reason=reason, evidence_ref=evidence_ref,
                confirmation=f"RESOLVE {tenant_id} {event_key} {resolution}",
                **payload,
            )
            broker.resolve_executing_admission(event_key, request)
        except Exception:
            raise_alarm(row, "resolve_rejected")
            continue
        summary["resolved"].append({
            "event_key": event_key, "tenant_id": tenant_id,
            "resolution": resolution, "evidence_ref": evidence_ref,
        })
    return summary


def run_forever(interval_s, stop_event, **kw):
    """Run immediately, then wait interruptibly between completed ticks."""
    if not math.isfinite(interval_s) or interval_s <= 0:
        raise ValueError("interval_s must be finite and positive")
    while not stop_event.is_set():
        reconcile_once(**kw)
        if stop_event.wait(interval_s):
            break


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    parser.add_argument("--interval-s", type=float, default=60)
    args = parser.parse_args()
    client = ApsWorkitemStatusClient()
    if args.once:
        print(json.dumps(reconcile_once(aps_client=client)))
    else:
        if not math.isfinite(args.interval_s) or args.interval_s <= 0:
            parser.error("--interval-s must be finite and positive")
        try:
            run_forever(args.interval_s, threading.Event(), aps_client=client)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
