"""Hardening: coarse per-tenant DAILY RUN quota (F12 + A4, merged into coarse-quota-v1)
and the F12 `_qa_sleep_s` hardening — both at the broker chokepoint.

Spec: plans/billing-compliance-later/COARSE-QUOTA-V1-SPEC.md
Chain: 1D (F4 auth + F10 tier re-check) -> QUOTA (this) -> 2B.

Counting rule (spec §a): the quota gates ONLY the APS_LIVE=1 (money) path; APS_LIVE=0
runs are free/un-metered and never counted or blocked. A run rejected as over-quota /
tenant-disabled is not counted either.

Acceptance covered here:
  * an over-cap tenant is rejected with a `quota_exceeded` envelope, HTTP 429, BEFORE
    any APS call (the credential-holding `_get_da` is never reached);
  * a DIFFERENT tenant under its cap still succeeds;
  * `_qa_sleep_s` is IGNORED when QA hooks are off, and honored+capped when on;
  * 1D's `require_broker_auth` on every /broker/* route AND the F10 entitlement block
    are still present (proves the QUOTA edit did not regress the chain head).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import broker  # noqa: E402


def _ok_env(*_a, **_k):
    """A schema-valid ok envelope — stand-in for a real tool run so the quota/QA tests
    never depend on tool-execution internals (mirrors test_broker_boundary._ok_env)."""
    return {"ok": True, "tool": "t", "version": "1.0.0", "result": {}, "overlay": None,
            "timing_ms": 1, "cost": None, "error": None, "degraded_mode": False}


def _seed_runs(ledger_path: Path, tenant_id: str, n: int, *,
               aps_live: bool = True, ts: float | None = None) -> None:
    """Append `n` prior-run ledger lines for a tenant (status 'ok'). aps_live=True lines
    count toward the live daily quota; aps_live=False lines are free/mock (never count)."""
    import time as _time
    ts = _time.time() if ts is None else ts
    with open(ledger_path, "a", encoding="utf-8") as fh:
        for _ in range(n):
            fh.write(json.dumps({"tenant_id": tenant_id, "ts": ts, "aps_live": aps_live,
                                 "status": "ok", "usd_est": 0.008}) + "\n")


def _run_req(tenant_id: str, *, aps_live: bool = False, params=None, tool_name: str = "count"):
    return broker.BrokerRunRequest(
        tenant_id=tenant_id,
        tool={"name": tool_name, "params_schema": {"type": "object"}},
        params=params or {}, dwg="rooftop_demo", aps_live=aps_live)


def _degraded_live_stack(monkeypatch, tmp_path) -> Path:
    """Broker wired so an aps_live=True run finds NO creds (`_get_da()` -> None), goes
    degraded, and completes on the pure-python path (200) — while the ledger line still
    records aps_live=True (so it counts toward the live quota). dwg 'rooftop_demo' resolves
    via the REAL DATA_DIR (left untouched). Returns the tmp ledger path."""
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(broker, "LEDGER_PATH", ledger)
    monkeypatch.setattr(broker, "DATA_FILE", tmp_path / "intake.json")
    (tmp_path / "intake.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(broker, "tenant_disabled", lambda _t: False)
    monkeypatch.setattr(broker, "_cap_preflight", lambda _t, _tool: None)  # isolate the run-quota
    monkeypatch.setattr(broker, "run_tool_dynamic", _ok_env)
    monkeypatch.setattr(broker, "_get_da", lambda: None)  # no creds -> degraded live -> mock 200
    monkeypatch.delenv("LEAF_BROKER_TENANT_TIERS", raising=False)
    return ledger


# --------------------------------------------------------------------------- #
# tier -> daily-limit map (unit, tier-aware, env-driven free N)
# --------------------------------------------------------------------------- #
def test_daily_run_limit_map_is_tier_aware_and_env_driven(monkeypatch):
    usage = broker._get_usage()
    assert usage is not None
    monkeypatch.delenv("LEAF_DAILY_RUN_QUOTA", raising=False)
    assert usage.daily_run_limit_for("demo") == 20            # free default N
    assert usage.daily_run_limit_for("self_hosted") == 20     # free coarse cap
    assert usage.daily_run_limit_for("restricted") == 20      # fail closed to free
    assert usage.daily_run_limit_for("hosted_starter") == 200 # 10x placeholder
    assert usage.daily_run_limit_for("hosted_pro") is None    # UNMETERED
    assert usage.daily_run_limit_for("totally-unknown") == 20 # fail closed, never open
    # N is a single env constant (operator swap menu {10,20,50})
    monkeypatch.setenv("LEAF_DAILY_RUN_QUOTA", "50")
    assert usage.daily_run_limit_for("demo") == 50
    assert usage.daily_run_limit_for("self_hosted") == 50
    assert usage.daily_run_limit_for("hosted_starter") == 200  # fixed, not env-tracked
    monkeypatch.setenv("LEAF_DAILY_RUN_QUOTA", "10")
    assert usage.daily_run_limit_for("demo") == 10
    # garbage env -> built-in default
    monkeypatch.setenv("LEAF_DAILY_RUN_QUOTA", "not-a-number")
    assert usage.daily_run_limit_for("demo") == 20


# --------------------------------------------------------------------------- #
# over-cap tenant rejected pre-APS (429); second tenant under cap succeeds
# --------------------------------------------------------------------------- #
def test_over_quota_tenant_rejected_pre_aps_with_429(monkeypatch, tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(broker, "LEDGER_PATH", ledger)
    monkeypatch.setattr(broker, "tenant_disabled", lambda _t: False)
    monkeypatch.setattr(broker, "_cap_preflight", lambda _t, _tool: None)  # isolate the run-quota
    monkeypatch.setenv("LEAF_DAILY_RUN_QUOTA", "2")
    monkeypatch.delenv("LEAF_BROKER_TENANT_TIERS", raising=False)  # tenant -> demo -> limit 2
    # PROVES pre-APS: the credential-holding client must never be reached when over cap.
    monkeypatch.setattr(
        broker, "_get_da",
        lambda: pytest.fail("APS client reached despite over-quota — not pre-APS"))

    _seed_runs(ledger, "tenant-over", 2)  # already AT the live cap (2/2)
    resp = broker.broker_run(_run_req("tenant-over", aps_live=True))

    assert resp.status_code == 429
    body = json.loads(resp.body)
    assert body["ok"] is False
    assert body["error"]["error_code"] == "quota_exceeded"
    assert body["error"]["retryable"] is True          # cap lifts at next UTC day
    assert body["error"]["limit"] == 2 and body["error"]["used"] == 2
    assert body["error_code"] == "quota_exceeded"       # top-level mirror
    assert body["tier"] == "demo"
    assert body["degraded_mode"] is False               # schema: boolean, not null
    # a denied run logs status 'quota_exceeded' -> never consumes further quota
    assert broker._get_usage().daily_run_count("tenant-over", ledger) == 2


def test_second_tenant_under_cap_still_succeeds(monkeypatch, tmp_path):
    ledger = _degraded_live_stack(monkeypatch, tmp_path)
    monkeypatch.setenv("LEAF_DAILY_RUN_QUOTA", "2")

    _seed_runs(ledger, "tenant-over", 2)  # tenant-over is AT cap
    over = broker.broker_run(_run_req("tenant-over", aps_live=True))
    assert over.status_code == 429

    # a DIFFERENT tenant, 0 prior live runs, is unaffected -> live run proceeds (200)
    under = broker.broker_run(_run_req("tenant-under", aps_live=True))
    assert under.status_code == 200
    assert json.loads(under.body)["ok"] is True


def test_last_admitted_run_then_next_rejected(monkeypatch, tmp_path):
    """used < limit is admitted; used >= limit is rejected (boundary)."""
    ledger = _degraded_live_stack(monkeypatch, tmp_path)
    monkeypatch.setenv("LEAF_DAILY_RUN_QUOTA", "3")

    _seed_runs(ledger, "t", 2)  # used=2 < 3 -> this run is the 3rd, admitted
    assert broker.broker_run(_run_req("t", aps_live=True)).status_code == 200  # ledger now 3 live
    assert broker.broker_run(_run_req("t", aps_live=True)).status_code == 429  # used=3 >= 3 -> reject


def test_hosted_pro_is_unmetered_never_blocked(monkeypatch, tmp_path):
    ledger = _degraded_live_stack(monkeypatch, tmp_path)
    monkeypatch.setenv("LEAF_DAILY_RUN_QUOTA", "1")  # tiny cap for metered tiers
    monkeypatch.setenv("LEAF_BROKER_TENANT_TIERS", json.dumps({"pro": "hosted_pro"}))

    _seed_runs(ledger, "pro", 100)  # far over any finite cap
    resp = broker.broker_run(_run_req("pro", aps_live=True))
    assert resp.status_code == 200  # unmetered tier is never blocked


def test_mock_runs_are_never_counted_or_gated(monkeypatch, tmp_path):
    """spec §a: APS_LIVE=0 runs are free — never counted toward the live quota, never
    gated. A pile of mock runs plus a tiny cap must NOT block another mock run."""
    ledger = _degraded_live_stack(monkeypatch, tmp_path)
    monkeypatch.setenv("LEAF_DAILY_RUN_QUOTA", "1")
    _seed_runs(ledger, "m", 50, aps_live=False)  # mock runs
    assert broker._get_usage().daily_run_count("m", ledger) == 0  # none counted (all mock)
    resp = broker.broker_run(_run_req("m", aps_live=False))  # mock run never gated
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# F12: `_qa_sleep_s` gated behind LEAF_QA_HOOKS (default ON local / OFF live)
# --------------------------------------------------------------------------- #
def _run_capturing_sleep(monkeypatch, tmp_path, sleep_val):
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(broker, "LEDGER_PATH", ledger)
    monkeypatch.setattr(broker, "DATA_FILE", tmp_path / "intake.json")
    (tmp_path / "intake.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(broker, "tenant_disabled", lambda _t: False)
    monkeypatch.setattr(broker, "_cap_preflight", lambda _t, _tool: None)
    monkeypatch.setattr(broker, "run_tool_dynamic", _ok_env)
    sleeps: list = []
    monkeypatch.setattr(broker.time, "sleep", lambda s: sleeps.append(s))
    # the _qa_sleep_s hook lives on the mock (APS_LIVE=0) path
    resp = broker.broker_run(_run_req("qa", params={"_qa_sleep_s": sleep_val}))
    return resp, sleeps


def test_qa_sleep_ignored_when_hooks_off(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_QA_HOOKS", "0")
    resp, sleeps = _run_capturing_sleep(monkeypatch, tmp_path, 5)
    assert resp.status_code == 200
    assert sleeps == []  # honored not at all -> can't starve the shared pool


def test_qa_sleep_honored_and_capped_when_hooks_on(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_QA_HOOKS", "1")
    resp, sleeps = _run_capturing_sleep(monkeypatch, tmp_path, 5)
    assert resp.status_code == 200
    assert sleeps == [5.0]  # honored under the cap
    _resp2, sleeps2 = _run_capturing_sleep(monkeypatch, tmp_path, 999)
    assert sleeps2 == [broker.QA_SLEEP_CAP_S]  # capped at 30s (F12 keeps the cap)


def test_qa_sleep_default_on_locally_off_in_live(monkeypatch, tmp_path):
    # UNSET LEAF_QA_HOOKS, not live -> default ON (backbone/tests rely on it)
    monkeypatch.delenv("LEAF_QA_HOOKS", raising=False)
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    _resp, sleeps = _run_capturing_sleep(monkeypatch, tmp_path, 3)
    assert sleeps == [3.0]
    # UNSET LEAF_QA_HOOKS, live/public -> default OFF (prod pool protection)
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    _resp2, sleeps2 = _run_capturing_sleep(monkeypatch, tmp_path, 3)
    assert sleeps2 == []


# --------------------------------------------------------------------------- #
# 1D survival: F4 caller-auth on every /broker/* route + F10 tier re-check intact
# --------------------------------------------------------------------------- #
def test_1d_broker_auth_and_f10_block_survived():
    src = Path(broker.__file__).read_text(encoding="utf-8")

    # F4/1D: require_broker_auth gates every mutating /broker/* route; health stays open.
    protected = {
        "/broker/run", "/broker/extract", "/broker/reap",
        "/broker/tenants/{tid}/disable", "/broker/tenants/{tid}/enable",
    }

    def _dep_calls(route):
        dep = getattr(route, "dependant", None)
        return [d.call for d in dep.dependencies] if dep is not None else []

    by_path = {getattr(r, "path", None): r for r in broker.app.routes}
    for path in protected:
        assert path in by_path, f"route {path} missing"
        assert broker.require_broker_auth in _dep_calls(by_path[path]), f"{path} lost 1D auth"
    assert broker.require_broker_auth not in _dep_calls(by_path["/broker/health"])

    # F10/1D: the broker-side tier ENTITLEMENT re-check is still wired in _execute.
    assert "tool_required_capability" in src
    assert "ENTITLEMENT_REQUIRED" in src
    assert broker._tenant_tier("nobody") == "demo"  # unknown -> demo (unchanged F10 seam)


def test_over_quota_does_not_bypass_f10_entitlement(monkeypatch, tmp_path):
    """A restricted tier writing a write-tool is denied by F10 (403) — the quota edit
    did not move that gate or let a write through on the money path."""
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(broker, "LEDGER_PATH", ledger)
    monkeypatch.setattr(broker, "tenant_disabled", lambda _t: False)
    monkeypatch.setattr(broker, "_cap_preflight", lambda _t, _tool: None)
    monkeypatch.setenv("LEAF_BROKER_TENANT_TIERS", json.dumps({"tenant-r": "restricted"}))
    req = broker.BrokerRunRequest(
        tenant_id="tenant-r",
        tool={"name": "writer", "capabilities": ["drawing.write"],
              "params_schema": {"type": "object"}},
        params={}, dwg="rooftop_demo", aps_live=False)
    resp = broker.broker_run(req)
    assert resp.status_code == 403
    assert b'"error_code":"ENTITLEMENT_REQUIRED"' in resp.body
