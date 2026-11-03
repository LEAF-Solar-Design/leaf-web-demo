"""Binary acceptance for lane QW-B: quota disambiguation shape.

da/usage.py:
  * quota_envelope (spend cap, :92-107) carries quota_kind:'spend' BOTH
    top-level AND nested inside the `error` object.
  * daily_quota_envelope (daily run-count cap, :329-356) carries
    quota_kind:'daily_runs' BOTH top-level AND nested inside the `error`
    object (tier/limit/used are unchanged, still present both places).

server/routers/jobs.py:
  * _record_body (:34-40) HOISTS quota_kind/tier/limit/used from a terminal
    job's `error` object (when error_code == 'quota_exceeded') up to the top
    level of the response body -- the frontend (console/api.js, ResultPanel)
    reads them there, not nested under `error`. Non-quota errors and ok jobs
    must NOT gain these keys.

Run:  cd server && python -m pytest tests/test_quota_shape.py -q
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

# jobs.py reads JOBS_DB at import time (default server/jobs.db) -- route it to a
# throwaway dir BEFORE `routers.jobs` (which does `import jobs`) is imported, so
# this unit-only test suite never touches the real DB (mirrors test_wave2.py).
os.environ.setdefault("JOBS_DB", str(Path(tempfile.mkdtemp(prefix="quota-shape-jobs-")) / "jobs.db"))

import deps  # noqa: E402
from routers.jobs import _record_body  # noqa: E402


def _load_usage():
    """da/usage.py, dynamically loaded the same way deps/broker load it (it is
    not a `server` package module) -- mirrors deps._load_module_from(DA_DIR /
    "usage.py", ...) used by routers/usage.py and routers/ops.py."""
    mod = deps._load_module_from(deps.DA_DIR / "usage.py", "leaf_usage_quota_shape")
    assert mod is not None, "da/usage.py failed to import"
    return mod


usage = _load_usage()


# --------------------------------------------------------------------------- #
# da/usage.py: spend-cap envelope (quota_kind:'spend')
# --------------------------------------------------------------------------- #
def test_spend_quota_envelope_carries_quota_kind_spend_top_and_nested():
    env = usage.quota_envelope("acme", spent=0.05, est_cost=0.008, cap=0.05, tool="extract")
    assert env["error"]["error_code"] == "quota_exceeded"
    assert env["error_code"] == "quota_exceeded"
    assert env["quota_kind"] == "spend"          # top-level
    assert env["error"]["quota_kind"] == "spend"  # nested


def test_check_cap_over_cap_decision_propagates_quota_kind_spend():
    decision = usage.check_cap("acme", est_cost=0.01, cap=0.05, spent=0.05)
    assert decision["ok"] is False
    assert decision["quota_kind"] == "spend"
    assert decision["error"]["quota_kind"] == "spend"


def test_check_cap_under_cap_has_no_quota_kind():
    decision = usage.check_cap("acme", est_cost=0.01, cap=0.05, spent=0.0)
    assert decision["ok"] is True
    assert "quota_kind" not in decision


# --------------------------------------------------------------------------- #
# da/usage.py: daily run-count envelope (quota_kind:'daily_runs')
# --------------------------------------------------------------------------- #
def test_daily_quota_envelope_carries_quota_kind_daily_runs_top_and_nested():
    env = usage.daily_quota_envelope("acme", "demo", limit=20, used=20, tool="extract")
    assert env["error"]["error_code"] == "quota_exceeded"
    assert env["error_code"] == "quota_exceeded"
    assert env["quota_kind"] == "daily_runs"           # top-level
    assert env["error"]["quota_kind"] == "daily_runs"  # nested
    # tier/limit/used unchanged shape, still both places
    assert env["tier"] == "demo" and env["error"]["tier"] == "demo"
    assert env["limit"] == 20 and env["error"]["limit"] == 20
    assert env["used"] == 20 and env["error"]["used"] == 20


def test_daily_run_quota_check_over_cap_propagates_quota_kind_daily_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAF_DAILY_RUN_QUOTA", "2")
    ledger = tmp_path / "ledger.jsonl"
    now = time.time()
    with open(ledger, "a", encoding="utf-8") as fh:
        for _ in range(2):
            fh.write(json.dumps({"tenant_id": "acme", "ts": now, "aps_live": True,
                                 "status": "ok", "usd_est": 0.008}) + "\n")
    decision = usage.daily_run_quota_check("acme", "demo", ledger, now_ts=now)
    assert decision["ok"] is False
    assert decision["quota_kind"] == "daily_runs"
    assert decision["tier"] == "demo"
    assert decision["limit"] == 2 and decision["used"] == 2


def test_daily_run_quota_check_under_cap_has_no_quota_kind(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAF_DAILY_RUN_QUOTA", "20")
    decision = usage.daily_run_quota_check("acme", "demo", tmp_path / "no-ledger.jsonl")
    assert decision["ok"] is True
    assert "quota_kind" not in decision


# --------------------------------------------------------------------------- #
# server/routers/jobs.py _record_body: hoist to top level (hand-built shapes)
# --------------------------------------------------------------------------- #
def test_record_body_hoists_daily_runs_quota_fields_to_top_level():
    rec = {
        "job_id": "j1", "status": "failed", "tenant_id": "acme", "result": None,
        "error": {"error_code": "quota_exceeded", "message": "daily cap reached",
                  "retryable": True, "tier": "demo", "limit": 20, "used": 20,
                  "quota_kind": "daily_runs"},
    }
    body = _record_body(rec)
    assert body["quota_kind"] == "daily_runs"
    assert body["tier"] == "demo"
    assert body["limit"] == 20
    assert body["used"] == 20
    # nested error object untouched/still present alongside the hoisted copies
    assert body["error"]["quota_kind"] == "daily_runs"
    assert body["degraded_mode"] is False


def test_record_body_hoists_spend_quota_fields_to_top_level_no_tier_limit_used():
    rec = {
        "job_id": "j2", "status": "failed", "tenant_id": "acme", "result": None,
        "error": {"error_code": "quota_exceeded", "message": "spend cap reached",
                  "retryable": False, "quota_kind": "spend"},
    }
    body = _record_body(rec)
    assert body["quota_kind"] == "spend"
    # the spend-cap error never carries tier/limit/used -> must not appear spuriously
    assert "tier" not in body
    assert "limit" not in body
    assert "used" not in body


def test_record_body_leaves_non_quota_error_untouched():
    rec = {
        "job_id": "j3", "status": "failed", "tenant_id": "acme", "result": None,
        "error": {"error_code": "TIMEOUT", "message": "boom", "retryable": True},
    }
    body = _record_body(rec)
    assert "quota_kind" not in body
    assert "tier" not in body
    assert "limit" not in body
    assert "used" not in body


def test_record_body_ok_job_unaffected():
    rec = {
        "job_id": "j4", "status": "complete", "tenant_id": "acme",
        "result": {"ok": True, "degraded_mode": False}, "error": None,
    }
    body = _record_body(rec)
    assert body["degraded_mode"] is False
    assert "quota_kind" not in body
    assert body["error"] is None


# --------------------------------------------------------------------------- #
# server/routers/jobs.py _record_body: hoist through the REAL usage.py envelopes
# (end-to-end shape check -- not just hand-built dict fixtures)
# --------------------------------------------------------------------------- #
def test_record_body_hoists_from_real_daily_quota_envelope():
    env = usage.daily_quota_envelope("acme", "demo", limit=5, used=5)
    rec = {"job_id": "j5", "status": "failed", "tenant_id": "acme",
           "result": None, "error": env["error"]}
    body = _record_body(rec)
    assert body["quota_kind"] == "daily_runs"
    assert body["tier"] == "demo"
    assert body["limit"] == 5
    assert body["used"] == 5


def test_record_body_hoists_from_real_spend_quota_envelope():
    env = usage.quota_envelope("acme", spent=0.05, est_cost=0.01, cap=0.05)
    rec = {"job_id": "j6", "status": "failed", "tenant_id": "acme",
           "result": None, "error": env["error"]}
    body = _record_body(rec)
    assert body["quota_kind"] == "spend"
    assert "tier" not in body
    assert "limit" not in body
    assert "used" not in body


def test_summary_body_drops_payloads_preserves_list_fields_and_does_not_mutate():
    from copy import deepcopy
    from routers.jobs import _summary_body

    rec = {
        "job_id": "summary-1", "status": "failed", "tool": "extract",
        "created_at": "2026-11-01T00:00:00Z",
        "error": {"error_code": "TIMEOUT", "message": "boom"},
        "result": {"cost": {"usd_est": 0.125}, "degraded_mode": True},
        "params": {"drawing": "large"}, "provenance": {"trace": "large"},
        "execution_context": {"graph": "large"},
    }
    original = deepcopy(rec)
    body = _summary_body(rec)
    for key in ("result", "params", "provenance", "execution_context"):
        assert key not in body
    for key in ("job_id", "status", "tool", "error", "created_at"):
        assert body[key] == rec[key]
    assert body["cost"] == rec["result"]["cost"]
    assert body["degraded_mode"] is True
    assert rec == original


@pytest.mark.parametrize("cost", [None, 0, {}, {"usd_est": 0.5}])
def test_summary_body_cost_uses_result_only_when_top_level_is_not_truthy(cost):
    from routers.jobs import _summary_body

    rec = {"cost": cost, "result": {"cost": {"usd_est": 0.125}}}
    assert _summary_body(rec)["cost"] == (cost or rec["result"]["cost"])


@pytest.mark.parametrize("result", [None, {}, {"cost": None}, {"cost": 0}])
def test_summary_body_handles_missing_or_empty_result_cost(result):
    from routers.jobs import _summary_body

    body = _summary_body({"result": result})
    if isinstance(result, dict) and "cost" in result:
        assert body["cost"] == result["cost"]
    else:
        assert "cost" not in body


@pytest.mark.parametrize("degraded_mode", [False, True])
def test_summary_body_preserves_record_body_quota_fields_and_degraded_mode(degraded_mode):
    from routers.jobs import _summary_body

    env = usage.daily_quota_envelope("acme", "demo", limit=5, used=5)
    rec = {"job_id": "summary-quota", "status": "failed", "tenant_id": "acme",
           "result": {"degraded_mode": degraded_mode}, "error": env["error"]}
    full = _record_body(rec)
    body = _summary_body(rec)
    for key in ("quota_kind", "tier", "limit", "used", "degraded_mode"):
        assert body[key] == full[key]


@pytest.mark.parametrize("summary", [False, True])
def test_list_jobs_summary_projection_is_opt_in(monkeypatch, summary):
    from routers import jobs as routes

    canonical = {"job_id": "summary-canonical", "tenant_id": "acme",
                 "status": "complete", "result": {"cost": {"usd_est": 0.125}}}
    legacy = {"job_id": "summary-legacy", "tenant_id": "acme",
              "status": "complete", "result": {"ok": True}}

    def canonical_jobs(tenant_id, *, limit):
        assert tenant_id == "acme"
        assert limit == 20
        return [canonical]

    def legacy_jobs(tenant_id, limit):
        assert tenant_id == "acme"
        assert limit == 20
        return [legacy]

    monkeypatch.setattr(routes.jobs.platform_link, "list_canonical_jobs", canonical_jobs)
    monkeypatch.setattr(routes.jobs, "list_jobs", legacy_jobs)
    monkeypatch.setattr(routes.jobs, "job_store_mode", lambda: "legacy")
    monkeypatch.setattr(routes, "_access_error", lambda *args, **kwargs: None)
    body = routes.list_jobs(tenant="acme", summary=summary)
    expected = routes._summary_body if summary else _record_body
    assert body["jobs"] == [expected(canonical), expected(legacy)]
    assert all(("result" not in item) == summary for item in body["jobs"])
    if not summary:
        assert routes.list_jobs(tenant="acme")["jobs"] == body["jobs"]


@pytest.mark.parametrize("summary", [False, True])
def test_summary_list_preserves_current_caller_binding_and_denied_rows(monkeypatch, summary):
    from types import SimpleNamespace
    from routers import jobs as routes

    tenant = deps.TenantContext("caller-tenant", subject="auth0|summary-reader")
    binding = SimpleNamespace(platform_tenant_id="bound-tenant")
    state = {"allowed": True, "resolutions": 0, "checks": 0}
    record = {"job_id": "current-job", "tenant_id": "bound-tenant",
              "status": "complete", "result": {"private": "payload"}}

    def resolve(caller):
        assert caller is tenant
        state["resolutions"] += 1
        return binding

    def access(rec, caller, job_id, **options):
        assert rec is record and caller is tenant and job_id == "current-job"
        assert options == {"binding": binding, "bound_tenant_id": "bound-tenant"}
        state["checks"] += 1
        return None if state["allowed"] else object()

    def canonical(tenant_id, *, limit):
        assert tenant_id == "bound-tenant"
        return [record]

    monkeypatch.setattr(deps, "auth_live", lambda: True)
    monkeypatch.setattr(routes.jobs.platform_link, "resolve_caller_binding", resolve)
    monkeypatch.setattr(routes.jobs.platform_link, "list_canonical_jobs", canonical)
    monkeypatch.setattr(routes.jobs, "list_jobs", lambda *args: [])
    monkeypatch.setattr(routes.jobs, "job_store_mode", lambda: "legacy")
    monkeypatch.setattr(routes, "_access_error", access)
    visible = routes.list_jobs(tenant=tenant, summary=summary)["jobs"]
    assert len(visible) == 1
    assert ("result" not in visible[0]) == summary
    state["allowed"] = False
    assert routes.list_jobs(tenant=tenant, summary=summary)["jobs"] == []
    assert state["resolutions"] == state["checks"] == 2
