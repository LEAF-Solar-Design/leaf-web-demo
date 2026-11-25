"""Server-only Lab tool admission and durable jobs submission."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
import os
import threading
import time
import uuid

import catalog
import entitlements
import lab_registry
import lab_corpus_failures
import lab_tool_manifests
from lab_evaluate import GraphRefused, RunBudgetExceeded, RunCancelled, admit_graph, canonical_json


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
    try:
        from deps import backedge_tier

        tier = backedge_tier(tenant_id)
        if tier is not None:
            return tier
        import platform_link

        org = platform_link.platform_store().get_org(tenant_id)
        if org is not None and org.status == "active":
            return org.tier if isinstance(org.tier, str) and org.tier.strip() else "restricted"
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
                 sleep=time.sleep, monotonic=time.monotonic, effects=("compute", "read"),
                 corpus_authority=None, corpus_owned=None, corpus_cancel=None, corpus_requested=None):
        self.effects = tuple(effects)
        self.tenant, self.run_id, self.cancelled = str(tenant), run_id, cancelled
        self.submit, self.get_job, self.list_jobs = submit, get_job, list_jobs
        self.tier_of = tier_of
        self.sandbox_seconds_today = sandbox_seconds_today or _sandbox_seconds_today
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.sleep, self.monotonic = sleep, monotonic
        self.corpus_authority = corpus_authority
        self.corpus_owned, self.corpus_cancel = corpus_owned, corpus_cancel
        self.corpus_requested = corpus_requested
        self._corpus_jobs = {}
        self._corpus_deadline = None
        self._corpus_cleanup_result = None

    def _corpus_running(self, deadline):
        if (self.cancelled() or (self.corpus_requested and self.corpus_requested())
                or self.corpus_owned is None or not self.corpus_owned()):
            raise RunCancelled()
        if deadline is not None and self.monotonic() >= deadline:
            raise RunBudgetExceeded()

    def prime_corpus(self, graph, registry, *, deadline):
        """Admit independent native intents, leaving observation to the evaluator."""
        if (not isinstance(graph, dict) or graph.get('pipes') != []
                or not graph.get('blocks') or any(
                    block.get('registry', {}).get('id') != 'corpus-evaluator'
                    for block in graph['blocks'])):
            return False
        manifests = admit_graph(graph, registry)
        from lab_doc_validators import validate_graph
        validate_graph(graph)
        import lab_corpus_executor as corpus

        prepared = []
        for block in graph['blocks']:
            ref = block['registry']
            trusted = lab_tool_manifests.trusted_for(self.tenant, ref['id'], ref['version'])
            if (manifests[block['id']]['id'] != 'corpus-evaluator'
                    or trusted is None or trusted.kind != 'corpus-evaluator'
                    or not self.admissible(trusted)):
                raise GraphRefused('lab_graph_effect_refused', block['id'])
            self._corpus_running(deadline)
            context = self._corpus_context(block['id'], block['settings'])
            # Custody and policy validity for every material precede any submission.
            sources = corpus.read_sources(context)
            corpus.compiled_suite(sources['suite'], context['policy'])
            key = f"lab:{self.run_id}:{block['id']}:corpus:v1"
            if len(key) > 200 or any(ord(char) < 32 or ord(char) == 127 for char in key):
                raise ValueError('Invalid corpus intent key.')
            prepared.append((block['id'], context))
        self._corpus_deadline = deadline
        for block_id, context in prepared:
            self._corpus_running(deadline)
            if block_id not in self._corpus_jobs:
                job_id = corpus.submit_policy(context,
                    idempotency_key=f'lab:{self.run_id}:{block_id}:corpus:v1')
                self._corpus_jobs[block_id] = (context, job_id)
        return True

    def cleanup_corpus(self, *, deadline=False):
        """Fence all outstanding intents, then share one bounded receipt wait.

        The existing deadline fence also closes abandoned intents on parent
        failure. The worker retains the actual parent terminal cause separately.
        """
        import jobs

        if self._corpus_cleanup_result is not None:
            return self._corpus_cleanup_result
        self._corpus_cleanup_result = False
        if not self._corpus_jobs:
            self._corpus_cleanup_result = True
            return True
        until = self.monotonic() + 10
        pending = {}
        confirmed = True
        for block_id, (_, job_id) in list(self._corpus_jobs.items()):
            if self.corpus_owned is None or not self.corpus_owned():
                return False
            try:
                row = jobs.get_job(job_id)
                if (row is not None and row['tenant_id'] == self.tenant
                        and row['status'] == 'complete'):
                    if not self.corpus_owned():
                        return False
                    del self._corpus_jobs[block_id]
                    continue
                tag = self.corpus_cancel(block_id, job_id, deadline)
                if not self.corpus_owned():
                    return False
                if tag is None:
                    # The fence returns any existing cancellation before checking
                    # terminal status. Only its absence permits terminal readback.
                    row = jobs.get_job(job_id)
                    if not self.corpus_owned():
                        return False
                    if (row is not None and row['tenant_id'] == self.tenant
                            and row['status'] in jobs.TERMINAL):
                        del self._corpus_jobs[block_id]
                    else:
                        confirmed = False
                else:
                    pending[block_id] = (job_id, tag)
            except Exception:
                confirmed = False
        while pending:
            if not self.corpus_owned():
                return False
            for block_id, (job_id, tag) in list(pending.items()):
                try:
                    receipt = jobs.corpus_cleanup(job_id, self.tenant)
                    if not self.corpus_owned():
                        return False
                    if (receipt and receipt.get('cancel') == tag
                            and (receipt.get('reaped') is True or receipt.get('not_started') is True)):
                        del pending[block_id]
                        del self._corpus_jobs[block_id]
                except Exception:
                    confirmed = False
            if not pending:
                break
            if self.monotonic() >= until:
                return False
            self.sleep(min(0.25, max(0, until - self.monotonic())))
        self._corpus_cleanup_result = confirmed and not self._corpus_jobs
        return self._corpus_cleanup_result

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
        if trusted is None:
            return {"error": "lab_tool_effect_refused"}
        if trusted.kind == 'corpus-evaluator':
            return self._corpus(block_id, settings, run_deadline)
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

    def _corpus_context(self, block_id, settings):
        import lab_corpus_executor as corpus
        authority = self.corpus_authority
        if (not authority or authority['tenant_id'] != self.tenant
                or settings['project_id'] != authority['project_id']
                or self.corpus_owned is None or not self.corpus_owned()):
            raise ValueError('lab_corpus_authority_refused')
        def descriptor(value):
            value = dict(value)
            size = value.get('byte_size')
            if type(size) is float and math.isfinite(size) and size.is_integer():
                value['byte_size'] = int(size)
            return value
        return corpus.validate_context({'schema': corpus.SCHEMA, 'tenant_id': self.tenant,
            'org_id': self.tenant, 'project_id': authority['project_id'],
            'actor_id': authority['actor_id'], 'corpus': descriptor(settings['corpus']), 'suite': descriptor(settings['suite']),
            'capability_sha256': corpus.ARCHIVE_SHA256,
            'policy': corpus.compile_policy(authority, block_id, settings)})

    def _corpus(self, block_id, settings, deadline):
        import jobs
        import lab_corpus_executor as corpus

        authority = self.corpus_authority
        if self._corpus_deadline is not None:
            deadline = self._corpus_deadline if deadline is None else min(deadline, self._corpus_deadline)
        self._corpus_running(deadline)
        if block_id in self._corpus_jobs:
            context, job_id = self._corpus_jobs[block_id]
        else:
            try:
                context = self._corpus_context(block_id, settings)
            except ValueError:
                return {'error': 'lab_corpus_authority_refused'}
            self._corpus_running(deadline)
            key = f'lab:{self.run_id}:{block_id}:corpus:v1'
            job_id = corpus.submit_policy(context, idempotency_key=key)
            self._corpus_jobs[block_id] = (context, job_id)
        while True:
            # Lease loss suspends this observer. It cannot cancel the shared
            # intent that a successor Lab worker may already own.
            if not self.corpus_owned():
                raise RunCancelled()
            expired = deadline is not None and self.monotonic() >= deadline
            requested = bool(self.corpus_requested and self.corpus_requested())
            if expired or requested:
                self.cleanup_corpus(deadline=expired)
                if expired:
                    raise RunBudgetExceeded()
                raise RunCancelled()
            else:
                row = jobs.get_job(job_id)
                if row is None or row['tenant_id'] != self.tenant:
                    return {'error': 'lab_corpus_job_unavailable'}
                if row['status'] == 'failed':
                    # A failed status may still carry a fenced cancellation
                    # whose factual reap receipt has not arrived.
                    return {'error': lab_corpus_failures.failure_code(row.get('error'))}
                if row['status'] == 'complete':
                    value = row['result']['result']
                    if value['context'] != context:
                        return {'error': 'lab_corpus_lineage_mismatch'}
                    corpus.validate_result(context, job_id, value['worker_id'], value['attempt'], value)
                    corpus.read_sources(context)
                    projected = {key: value[key] for key in ('job_id', 'worker_id', 'attempt',
                        'row_count', 'rows_sha256', 'result_sha256', 'context')}
                    projected.update(run_id=self.run_id, block_id=block_id,
                        doc_id=authority['doc_id'], graph_revision=authority['graph_revision'],
                        graph_sha256=authority['graph_sha256'], verdict=value['result']['verdict'],
                        reasons=value['result']['reasons'], policy=context['policy'],
                        policy_digest=context['policy']['digest'],
                        evaluated_suite_sha256=value['evaluated_suite_sha256'], evaluation=value['result'])
                    # Evidence is JSON text so graph-value array bounds cannot
                    # truncate candidates or confirmation tickets. Refuse an
                    # oversized envelope explicitly instead of cutting fields.
                    encoded = json.dumps(projected, sort_keys=True, separators=(',', ':'), allow_nan=False)
                    if len(encoded.encode()) > corpus.MAX_EVIDENCE_BYTES:
                        return {'error': 'lab_corpus_evidence_too_large'}
                    del self._corpus_jobs[block_id]
                    return {'outputs': {'result': encoded}}
            self.sleep(0.25)
