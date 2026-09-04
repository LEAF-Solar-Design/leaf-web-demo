"""
GET /api/builds (standardization slice 11a): every build the caller's tenant
owns, from three sources, behind ONE record shape (server/build_queue.py, the
mirror of web/src/lib/buildQueue.js):

  broker  the jobs store, exactly the rows GET /api/jobs lists for this
          tenant, each terminal one carrying its SHA-stamped receipt.json
          (server/build_receipts.py) as a receipt of kind ``terminal``
  fleet   the fleet gateway, read with the PLATFORM's credential
          (server/fleet_gateway_client.py), never the browser's; degraded to
          [] with one warning when unreachable or unconfigured
  fold    the multi-round runs under the tenant's own directory
          (server/marathon_runs.py); [] when unconfigured

Tenant-scoped the way /api/jobs is: the resolved caller tenant, never a
client-supplied id. Bounded: ``limit`` is clamped to 1..200 and applies to the
merged list (newest ``started`` first) and to each source. Fail closed: a
source row that does not map (an unknown state, no id) is dropped and COUNTED
in ``dropped``, and every record in the body passed ``validate_record`` before
it left, so a consumer that trusts this route never sees a shape the card
cannot render.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from fastapi import APIRouter, Depends

import build_queue
import build_receipts
import deps
import fleet_gateway_client
import jobs
import marathon_runs
from envelopes import with_envelope_fields
from routers.jobs import _bound_tenant_id, _record_body

router = APIRouter()

MAX_LIMIT = 200
# The per-field bounds (LIMITS in build_queue.py) do not compose: 200 records
# x 32 receipts x a 512-char ref is ~3.6 MB from one GET. This is the body-
# level backstop, trimming the tail (already newest-first) instead of
# failing the whole request.
MAX_RESPONSE_BYTES = 512 * 1024


def _clamp(limit: Any) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = 20
    return max(1, min(n, MAX_LIMIT))


def _broker_records(tenant: Any, limit: int, dropped: Dict[str, int]) -> List[Dict[str, Any]]:
    canonical = jobs.platform_link.list_canonical_jobs(_bound_tenant_id(tenant), limit=limit)
    legacy = jobs.list_jobs(str(tenant), limit)
    records = (canonical + legacy)[:limit]
    # One bounded directory listing instead of up to `limit` (200) individual
    # stat-plus-open-plus-parse-plus-sha256 cycles: a job id absent from this
    # set skips straight past `terminal_receipt_entry` entirely. Measured on
    # this host (tests/bench, 200 job ids): read_terminal_receipt alone costs
    # ~25.6 ms/200 calls (~128 us/job) when every receipt is MISSING and
    # ~71.6 ms/200 calls (~358 us/job) when every receipt EXISTS (open+parse+
    # sha256 dominate); one list_receipt_job_ids scandir over the same 200
    # entries costs ~0.1-0.2 ms total. The common case (most terminal jobs
    # have no receipt) is the one this listing is for.
    receipt_ids = build_receipts.list_receipt_job_ids()
    out: List[Dict[str, Any]] = []
    for rec in records:
        body = _record_body(rec)
        if body.get("status") in ("complete", "failed") and body.get("job_id") in receipt_ids:
            entry = build_receipts.terminal_receipt_entry(body.get("job_id"))
            if entry is not None:
                body = dict(body)
                body["receipts"] = [entry]
        record = build_queue.from_broker_job(body)
        if record is None:
            dropped["broker"] += 1
            continue
        out.append(record)
    return out


def _fleet_records(tenant_id: str, limit: int, dropped: Dict[str, int],
                   warnings: List[str], sources: Dict[str, str]) -> List[Dict[str, Any]]:
    if not fleet_gateway_client.configured():
        # Silent, like the fold lane's identical "unconfigured" condition
        # (below): `sources["fleet"]` already says so, and a permanent
        # warning on every request in the shipped default (fleet has no
        # deployment config anywhere in this repo) is noise, not a signal.
        sources["fleet"] = "unconfigured"
        return []
    try:
        rows = fleet_gateway_client.list_tasks(tenant_id, limit)
    except fleet_gateway_client.FleetGatewayUnavailable as exc:
        sources["fleet"] = "unavailable"
        warnings.append(f"fleet: {exc}")
        return []
    sources["fleet"] = "gateway"
    out: List[Dict[str, Any]] = []
    for row in rows:
        record = build_queue.from_fleet_task(row)
        if record is None:
            dropped["fleet"] += 1
            continue
        out.append(record)
    return out


def _fold_records(tenant_id: str, limit: int, dropped: Dict[str, int],
                  warnings: List[str], sources: Dict[str, str]) -> List[Dict[str, Any]]:
    if not marathon_runs.configured():
        sources["fold"] = "unconfigured"
        return []
    runs, run_warnings = marathon_runs.list_runs(tenant_id, limit)
    warnings.extend(run_warnings)
    sources["fold"] = "runs-dir"
    out: List[Dict[str, Any]] = []
    for run in runs:
        record = build_queue.from_fold_state(run["state"], run["meta"])
        if record is None:
            dropped["fold"] += 1
            continue
        out.append(record)
    return out


@router.get("/api/builds")
def list_builds(limit: int = 20, tenant=Depends(deps.require_tenant)):
    bounded = _clamp(limit)
    # The fleet and fold lanes must resolve the same active platform binding
    # the broker lane already does (`_broker_records` -> `_bound_tenant_id`
    # below): a raw JWT claim can outlive an account move, and reading the
    # fleet/fold lanes off the stale claim while the broker lane reads the
    # bound tenant would silently mix two tenants' records in one response.
    tenant_id = _bound_tenant_id(tenant)
    warnings: List[str] = []
    sources: Dict[str, str] = {"broker": "jobs-store"}
    dropped = {"broker": 0, "fleet": 0, "fold": 0}
    records = _broker_records(tenant, bounded, dropped)
    records += _fleet_records(tenant_id, bounded, dropped, warnings, sources)
    records += _fold_records(tenant_id, bounded, dropped, warnings, sources)
    # Newest first; a record with no start time sorts last, stably.
    records.sort(key=lambda r: (r["started"] is None, -(r["started"] or 0)))
    validated: List[Dict[str, Any]] = []
    for record in records[:bounded]:
        try:
            validated.append(build_queue.validate_record(record))
        except build_queue.BuildQueueError as exc:  # pragma: no cover - mappers pass by construction
            dropped[record.get("lane", "broker")] += 1
            warnings.append(f"{record.get('lane')}: record dropped ({exc})")
    # Body-level bound: the per-field bounds compose to ~3.6 MB worst case
    # (see MAX_RESPONSE_BYTES above). Each record is serialized once (bounded
    # individual cost, at most ~18 KB at LIMITS["receipts"]=32), summed, and
    # the tail trimmed — O(n) serialization work, not O(n^2) re-serialization
    # of the whole list per record dropped.
    sizes = [len(json.dumps(r).encode("utf-8")) for r in validated]
    total = sum(sizes)
    while validated and total > MAX_RESPONSE_BYTES:
        total -= sizes.pop()
        trimmed = validated.pop()
        dropped[trimmed.get("lane", "broker")] += 1
    return with_envelope_fields(deps.tenant_echo({
        "builds": validated,
        "warnings": warnings[:20],
        "sources": sources,
        "dropped": dropped,
        "limit": bounded,
    }, tenant))
