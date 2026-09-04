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


def _clamp(limit: Any) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = 20
    return max(1, min(n, MAX_LIMIT))


def _broker_records(tenant: Any, limit: int, dropped: Dict[str, int]) -> List[Dict[str, Any]]:
    canonical = jobs.platform_link.list_canonical_jobs(_bound_tenant_id(tenant), limit=limit)
    legacy = jobs.list_jobs(str(tenant), limit)
    out: List[Dict[str, Any]] = []
    for rec in (canonical + legacy)[:limit]:
        body = _record_body(rec)
        if body.get("status") in ("complete", "failed"):
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
        sources["fleet"] = "unconfigured"
        warnings.append("fleet: gateway not configured")
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
    tenant_id = str(tenant)
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
    return with_envelope_fields(deps.tenant_echo({
        "builds": validated,
        "warnings": warnings[:20],
        "sources": sources,
        "dropped": dropped,
        "limit": bounded,
    }, tenant))
