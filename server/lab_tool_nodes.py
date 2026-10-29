"""Server-only Lab tool admission and durable jobs submission."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import os
import threading
import time
import uuid

import catalog
import entitlements
import lab_registry
import lab_tool_manifests
from lab_evaluate import RunBudgetExceeded, RunCancelled, canonical_json


_tenant_locks_guard = threading.Lock()
_tenant_locks = {}


def _tenant_lock(tenant):
    # Retain one lock per tenant served by this worker process.
    with _tenant_locks_guard:
        if tenant not in _tenant_locks:
            _tenant_locks[tenant] = threading.Lock()
        return _tenant_locks[tenant]


def enabled():
    return os.environ.get("LEAF_LAB_TOOL_NODES") == "1"


def lab_tool_tier(tenant_id):
    # Broker record first, active platform org second; fail closed.
    from deps import backedge_tier

    tier = backedge_tier(tenant_id)
    if tier is not None:
        return tier
    try:
        import platform_link

        org = platform_link.platform_store().get_org(tenant_id)
        if org is not None and org.status == "active":
            return org.tier.strip() if isinstance(org.tier, str) and org.tier.strip() else "restricted"
    except Exception:
        return None
    return None


def tool_block_admissible(trusted, effects=("compute", "read")):
    return (trusted is not None and trusted.residency != "host"
            and trusted.aps_live is False
            and lab_registry.total_effect(trusted.capability, trusted.solar_adapter,
                                          trusted.execution_class, trusted.aps_live)
            in effects)


def drawing_valid(drawing):
    return (isinstance(drawing, dict)
            and set(drawing) == {"project_id", "drawing_id", "dwg_version"}
            and all(isinstance(drawing[key], str) and 1 <= len(drawing[key]) <= 200
                    for key in ("project_id", "drawing_id"))
            and type(drawing["dwg_version"]) is int and drawing["dwg_version"] >= 1)


def _timestamp(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, timezone.utc)
    if isinstance(value, str):
        return _timestamp(datetime.fromisoformat(value.replace("Z", "+00:00")))
    raise ValueError("invalid job timestamp")


def _limit(name, default, maximum, *, integer=True):
    raw = os.environ.get(name, str(default))
    value = int(raw) if integer else float(raw)
    if not math.isfinite(value) or not 1 <= value <= maximum:
        raise ValueError("invalid lab budget")
    return value


def _sandbox_seconds_today(tenant, midnight):
    uuid.UUID(str(tenant))
    from agent_pg_store import _load_platform

    db, _ = _load_platform()
    from leaf_platform import tenant_cost_store

    with db.transaction() as conn:
        rows = tenant_cost_store.list_cost_events(
            conn, str(tenant), midnight, midnight + timedelta(days=1),
            addon="e2b_exec", limit=10000)
    if len(rows) >= 10000:
        raise ValueError("incomplete sandbox ledger")
    quantities = [float(row["quantity"]) for row in rows if row["unit"] == "sandbox_seconds"]
    if any(not math.isfinite(value) or value < 0 for value in quantities):
        raise ValueError("invalid sandbox ledger")
    return sum(quantities)


class ToolNodeAdapter:
    def __init__(self, tenant, run_id, *, cancelled, submit=None, get_job=None,
                 list_jobs=None, tier_of=None, sandbox_seconds_today=None, now=None,
                 sleep=time.sleep, monotonic=time.monotonic, effects=("compute", "read")):
        self.effects = tuple(effects)
        self.tenant, self.run_id, self.cancelled = str(tenant), run_id, cancelled
        self.submit, self.get_job, self.list_jobs = submit, get_job, list_jobs
        self.tier_of = tier_of
        self.sandbox_seconds_today = sandbox_seconds_today or _sandbox_seconds_today
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.sleep, self.monotonic = sleep, monotonic

    def admissible(self, trusted):
        return tool_block_admissible(trusted, self.effects)

    def __call__(self, block_id, trusted, manifest, inputs, settings, *, deadline_s=None):
        run_deadline = None if deadline_s is None else self.monotonic() + deadline_s
        try:
            return self._call(block_id, trusted, manifest, inputs, settings, run_deadline)
        except (RunCancelled, RunBudgetExceeded):
            raise
        except Exception:
            return {"error": "lab_tool_submit_unavailable"}

    def _call(self, block_id, trusted, manifest, inputs, settings, run_deadline):
        def refuse(code):
            return {"error": "lab_tool_" + code}

        if not self.admissible(trusted):
            return refuse("effect_refused")
        try:
            if str(uuid.UUID(self.tenant)) != self.tenant:
                return refuse("budget_unreadable")
        except ValueError:
            return refuse("budget_unreadable")
        if any(value.error for value in inputs.values()):
            return refuse("input_error")
        drawing = settings.get("drawing") if isinstance(settings, dict) else None
        if not drawing_valid(drawing):
            return refuse("drawing_required")
        resolved = lab_tool_manifests.trusted_for(self.tenant, trusted.kind, trusted.version)
        if resolved is None and manifest.get("id") == trusted.kind:
            return refuse("tool_changed")
        if resolved is not None and not self.admissible(resolved):
            return refuse("effect_refused")
        trusted = resolved or trusted
        row = lab_tool_manifests.row_for(self.tenant, trusted.kind, trusted.version,
                                        manifest.get("version", {}).get("code_sha256"))
        # Any tenant tool of this name is authoritative, projected or refused: a pin it no
        # longer matches is refused, never resolved through the seed list.
        projection = lab_tool_manifests.tenant_tool_projection(self.tenant)
        known = any(tool.name == trusted.kind for tool in projection)
        projected = next((tool for tool in projection
                          if tool.name == trusted.kind and tool.version == trusted.version
                          and tool.manifest is not None), None)
        params_by_port = {}
        if row is not None:
            tool, params_by_port = row
        elif known:
            return refuse("tool_changed")
        else:
            tool = next((tool for tool in catalog.seed_tools()
                         if trusted.kind in (tool.get("id"), tool.get("name"))
                         and tool.get("version", "1.0.0") == trusted.version), None)
        if tool is None:
            return refuse("unknown")
        params = {params_by_port.get(port, port): value.data for port, value in inputs.items()}
        if "drawing_id" in tool.get("params", {}).get("properties", {}):
            if "drawing_id" in params and params["drawing_id"] != drawing["drawing_id"]:
                return refuse("drawing_mismatch")
            params["drawing_id"] = drawing["drawing_id"]
        try:
            required = entitlements.tool_required_capability(tool)
            if self.tier_of is None:
                tier_of = lab_tool_tier
            else:
                tier_of = self.tier_of
            tier = tier_of(self.tenant)
            if not isinstance(tier, str) or not tier.strip():
                return refuse("binding_unavailable")
            if not entitlements.entitlements_for(tier).get(required):
                return refuse("entitlement_required")
        except entitlements.EntitlementsError:
            return refuse("policy_unavailable")
        except Exception:
            return refuse("binding_unavailable")

        import jobs

        key = f"lab:{self.run_id}:{block_id}:1"
        with _tenant_lock(self.tenant):
            try:
                rows = (self.list_jobs or jobs.list_jobs)(self.tenant, 200)
                replay = any(row.get("idempotency_key") == key for row in rows)
                if not replay:
                    midnight = _timestamp(self.now()).replace(hour=0, minute=0, second=0, microsecond=0)
                    limit = _limit("LEAF_LAB_TOOL_DAILY_JOBS", 100, 200)
                    count = sum(1 for row in rows
                                if (row.get("idempotency_key") or "").startswith("lab:")
                                and _timestamp(row["created_at"]) >= midnight)
                    if count >= limit:
                        return refuse("daily_jobs_exhausted")
                    if len(rows) >= 200 and min(_timestamp(row["created_at"]) for row in rows) >= midnight:
                        return refuse("budget_unreadable")
                    seconds_limit = _limit("LEAF_LAB_TOOL_DAILY_SANDBOX_S", 3600, 86400, integer=False)
                    seconds = float(self.sandbox_seconds_today(self.tenant, midnight))
                    if not math.isfinite(seconds) or seconds < 0:
                        return refuse("budget_unreadable")
                    if seconds >= seconds_limit:
                        return refuse("daily_sandbox_exhausted")
                timeout = _limit("LEAF_LAB_TOOL_TIMEOUT_S", 300, 600)
            except Exception:
                return refuse("budget_unreadable")
            if run_deadline is not None and self.monotonic() >= run_deadline:
                raise RunBudgetExceeded()
            try:
                job_id = (self.submit or jobs.submit_job)(
                    self.tenant, dict(tool, lab_execution={"run_id": self.run_id,
                                                          "block_id": block_id, "attempt": 1}),
                    params, drawing["drawing_id"], False, org_id=self.tenant,
                    project_id=drawing["project_id"], dwg_version=drawing["dwg_version"],
                    idempotency_key=key)
            except jobs.TenantInflightCapExceeded:
                return refuse("inflight_cap")
            except ValueError:
                return refuse("submit_refused")
            except Exception:
                return refuse("submit_unavailable")
        started = self.monotonic()
        run_limited = run_deadline is not None and run_deadline <= started + timeout
        deadline = run_deadline if run_limited else started + timeout

        def expired():
            if self.monotonic() >= deadline:
                if run_limited:
                    raise RunBudgetExceeded()
                return True
            return False

        while True:
            if self.cancelled():
                raise RunCancelled()
            if expired():
                return refuse("timeout")
            row = (self.get_job or jobs.get_job)(job_id)
            if expired():
                return refuse("timeout")
            if row is not None and row["status"] in jobs.TERMINAL:
                if row["status"] == "failed":
                    return refuse("failed")
                envelope = row.get("result")
                if not isinstance(envelope, dict) or "result" not in envelope:
                    return refuse("output_invalid")
                data = envelope["result"]
                ports = manifest["outputs"]
                if len(ports) == 1:
                    if projected is not None and projected.result_encoding == "json-text":
                        data = canonical_json(data)
                    return {"outputs": {next(iter(ports)): data}}
                if not isinstance(data, dict):
                    return refuse("output_invalid")
                return {"outputs": {port: data.get(port) for port in ports}}
            self.sleep(max(0, min(0.5, deadline - self.monotonic())))
