"""
Binary acceptance for the agent gate chain (server/agent_gate.py).

Chain-order contracts under test:
  * the kill switch beats EVERYTHING (even an unknown action reports it);
  * unknown action / disabled action / invalid args deny, naming the gate;
  * entitlement runs BEFORE rate limiting (denied calls never burn budget);
  * the rate limit denies per tenant per hour by category;
  * policy tiers: auto allows; confirm-once files an approval once then
    persists a per-session grant; always-confirm files EVERY time (never
    persisted); approval binding is args-exact and session-bound; a granted
    approval redeems exactly once (replay denies); TTL expiry auto-denies.

Hermetic: every state file (policy, kill, approvals, grants, rate, audit,
ledger, tenants) points into tmp_path via env.

Run:  cd server && python -m pytest tests/test_agent_gate.py -q
"""
from __future__ import annotations

# Cache the STDLIB `platform` module BEFORE PROJECT_ROOT lands on sys.path
# (repo-root platform/ package shadows it; mirrors test_wave4/5).
import platform as _stdlib_platform  # noqa: E402
_stdlib_platform.python_implementation()

import json  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import agent_gate  # noqa: E402
import agent_policy  # noqa: E402

FULL_CAPS = {"run_read": True, "run_write": True, "build": True, "converse": True,
             "agent_write_autopilot": True, "deploy": True, "platform_customize": True}


@pytest.fixture(autouse=True)
def agent_env(tmp_path, monkeypatch):
    """Point every agent state file into tmp_path (shipped policy file stays
    the default catalog — it is read-only here)."""
    monkeypatch.delenv("LEAF_AGENT_POLICY_FILE", raising=False)
    monkeypatch.setenv("LEAF_AGENT_KILL_FILE", str(tmp_path / "agent.disabled"))
    monkeypatch.setenv("LEAF_AGENT_APPROVALS_DIR", str(tmp_path / "approvals"))
    monkeypatch.setenv("LEAF_AGENT_GRANTS_FILE", str(tmp_path / "grants.json"))
    monkeypatch.setenv("LEAF_AGENT_RATE_FILE", str(tmp_path / "rate.json"))
    monkeypatch.setenv("LEAF_AGENT_AUDIT", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("LEAF_AGENT_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("LEAF_AGENT_TENANTS_FILE", str(tmp_path / "agent_tenants.json"))
    yield tmp_path


def _gate(action: str, args=None, *, tenant="t-gate", session="s-1", turn="turn-1",
          caps=FULL_CAPS, tier=None):
    return agent_gate.gate(tenant, session, turn, action, args or {}, caps, tier=tier)


def _custom_policy(tmp_path, monkeypatch, mutate):
    raw = json.loads((SERVER_DIR / "agent_policy.json").read_text(encoding="utf-8"))
    mutate(raw)
    p = tmp_path / "custom_policy.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setenv("LEAF_AGENT_POLICY_FILE", str(p))


# --------------------------------------------------------------------------- #
# chain order
# --------------------------------------------------------------------------- #
def test_kill_switch_beats_everything(agent_env):
    agent_gate.kill_file().write_text("drill\n", encoding="utf-8")
    # even an action that is not in the catalog reports the kill switch
    res = _gate("no_such_action")
    assert res["decision"] == "deny"
    assert res["reason"].startswith("kill_switch_active")
    res2 = _gate("read_platform_state")
    assert res2["decision"] == "deny"
    assert "drill" in res2["reason"]
    assert agent_gate.kill_switch_active() is True


def test_unknown_action_denies():
    res = _gate("no_such_action")
    assert res["decision"] == "deny"
    assert res["reason"] == "unknown_action"


def test_disabled_action_denies():
    res = _gate("customize_platform")
    assert res["decision"] == "deny"
    assert res["reason"] == "action_disabled"


def test_invalid_args_deny_names_gate():
    res = _gate("run_read_tool", {})  # missing required "tool"
    assert res["decision"] == "deny"
    assert res["reason"].startswith("invalid_args")
    res2 = _gate("run_read_tool", {"tool": "layer-report", "bogus": 1})
    assert res2["decision"] == "deny"
    assert res2["reason"].startswith("invalid_args")


def test_invalid_args_reason_never_embeds_raw_values():
    """The deny reason lands verbatim in the audit file, so it must name only
    the JSON-pointer path and validator keyword — never the offending value."""
    secret_value = "free text the audit must never carry verbatim"
    res = _gate("run_read_tool", {"tool": secret_value, "bogus": {"leak": secret_value}})
    assert res["decision"] == "deny"
    assert res["reason"].startswith("invalid_args")
    assert secret_value not in res["reason"]
    assert "leak" not in res["reason"]


def test_args_validation_wins_over_entitlement():
    """Chain-order pin: a call that is BOTH invalid-args and missing the
    capability denies at the args gate (docstring chain: catalog/args checks
    before entitlement) — flipping those gates must fail this test."""
    res = _gate("run_read_tool", {}, caps=dict(FULL_CAPS, run_read=False))
    assert res["decision"] == "deny"
    assert res["reason"].startswith("invalid_args")


def test_entitlement_denies_without_capability():
    caps = dict(FULL_CAPS, run_read=False)
    res = _gate("run_read_tool", {"tool": "layer-report"}, caps=caps)
    assert res["decision"] == "deny"
    assert res["reason"] == "entitlement_required: tier lacks 'run_read'"


def test_entitlement_before_rate_limit_denied_calls_never_burn_budget(tmp_path, monkeypatch):
    _custom_policy(tmp_path, monkeypatch,
                   lambda raw: raw["rate_limits"].update({"medium_per_hour": 3}))
    no_caps = dict(FULL_CAPS, run_read=False)
    # ten entitlement-denied calls must not consume the 3-call medium budget
    for _ in range(10):
        res = _gate("run_read_tool", {"tool": "layer-report"}, caps=no_caps)
        assert res["reason"].startswith("entitlement_required")
    for _ in range(3):
        assert _gate("run_read_tool", {"tool": "layer-report"})["decision"] == "allow"
    res = _gate("run_read_tool", {"tool": "layer-report"})
    assert res["decision"] == "deny"
    assert res["reason"].startswith("rate_limit_exceeded: medium (3/3)")


def test_split_turn_confirmed_write_consumes_exactly_two_budget_units(tmp_path, monkeypatch):
    """Rate consumption records at step 6, BEFORE the policy tier — so one
    confirmed write burns budget TWICE (the proposal AND the resume), and an
    awaiting_approval outcome consumes budget. Pin that contract: with a
    2/hour medium budget, propose + resume exhausts it exactly."""
    _custom_policy(tmp_path, monkeypatch,
                   lambda raw: raw["rate_limits"].update({"medium_per_hour": 2}))
    args = {"tool": "add-panel"}
    first = _gate("run_write_tool", args, session="s-budget")
    assert first["decision"] == "awaiting_approval"  # unit 1: the proposal
    cid = first["confirmation_id"]
    agent_gate.grant_approval(cid, by="t-gate")
    resumed = _gate("run_write_tool", dict(args, confirmation_id=cid), session="s-budget")
    assert resumed["decision"] == "allow"
    assert resumed["reason"] == "allow_via_approval"  # unit 2: the resume
    # third call for the same tenant: budget exhausted at propose+resume = 2
    third = _gate("run_write_tool", args, session="s-budget")
    assert third["decision"] == "deny"
    assert third["reason"] == "rate_limit_exceeded: medium (2/2)"


def test_rate_limit_is_per_tenant(tmp_path, monkeypatch):
    _custom_policy(tmp_path, monkeypatch,
                   lambda raw: raw["rate_limits"].update({"medium_per_hour": 1}))
    assert _gate("run_read_tool", {"tool": "x"}, tenant="t-a")["decision"] == "allow"
    assert _gate("run_read_tool", {"tool": "x"}, tenant="t-a")["decision"] == "deny"
    # a different tenant has its own bucket
    assert _gate("run_read_tool", {"tool": "x"}, tenant="t-b")["decision"] == "allow"


def test_tenant_agent_disabled_denies():
    agent_policy.set_tenant_agent_disabled("t-off", True)
    res = _gate("read_platform_state", tenant="t-off")
    assert res["decision"] == "deny"
    assert res["reason"] == "tenant_agent_disabled"
    agent_policy.set_tenant_agent_disabled("t-off", False)
    assert _gate("read_platform_state", tenant="t-off")["decision"] == "allow"


def test_tenant_disabled_wins_over_unknown_action_but_loses_to_kill_switch():
    """Chain-order pin for the tenant kill flag's slot: after the global kill
    switch (1), before the catalog lookup (2)."""
    agent_policy.set_tenant_agent_disabled("t-off2", True)
    res = _gate("no_such_action", tenant="t-off2")
    assert res["decision"] == "deny"
    assert res["reason"] == "tenant_agent_disabled"
    agent_gate.kill_file().write_text("drill\n", encoding="utf-8")
    res2 = _gate("no_such_action", tenant="t-off2")
    assert res2["reason"].startswith("kill_switch_active")


# --------------------------------------------------------------------------- #
# policy tiers
# --------------------------------------------------------------------------- #
def test_auto_allows_with_policy_and_rung():
    res = _gate("read_platform_state")
    assert res == {"decision": "allow", "reason": "allow", "policy": "auto", "rung": 1}


def test_request_confirmation_mints_a_confirmation_id_at_rung_zero():
    """The UI-consent tool (wire contract section 5): converseLoop consults the
    REAL gate for it, so it must be in the catalog at rung 0 — otherwise every
    confirmation card dies as unknown_action. always-confirm is what MINTS the
    id: only the app's pending store may issue one (section 6)."""
    res = _gate("request_confirmation", {"kind": "write_confirm", "payload": {"tool": "add-panel"}})
    assert res["decision"] == "awaiting_approval"
    assert res["policy"] == "always-confirm"
    assert res["rung"] == 0
    assert res["confirmation_id"]
    # bare-minimum harness shape (payload optional) also reaches the same tier
    assert _gate("request_confirmation", {"kind": "confirm"})["decision"] == "awaiting_approval"


def test_confirm_once_flow_and_session_grant_persists():
    args = {"tool": "add-panel", "params": {"n": 2}, "dwg": "rooftop_demo"}
    first = _gate("run_write_tool", args, session="s-A")
    assert first["decision"] == "awaiting_approval"
    cid = first["confirmation_id"]
    assert cid

    ok, record, reason = agent_gate.grant_approval(cid, by="t-gate")
    assert ok and record["granted"] and reason == "granted"

    # re-invoke carries confirmation_id inside args (wire contract §7)
    resumed = _gate("run_write_tool", dict(args, confirmation_id=cid), session="s-A")
    assert resumed["decision"] == "allow"
    assert resumed["reason"] == "allow_via_approval"

    # confirm-once persisted per session: same session skips the chip...
    again = _gate("run_write_tool", args, session="s-A")
    assert again["decision"] == "allow"
    assert again["reason"] == "allow_via_session_grant"
    # ...a DIFFERENT session files a fresh approval
    other = _gate("run_write_tool", args, session="s-B")
    assert other["decision"] == "awaiting_approval"
    assert other["confirmation_id"] != cid


def test_granted_approval_redeems_exactly_once_always_confirm():
    """Single-use pin: within the 300s TTL a granted always-confirm approval
    must not be replayable — one human click authorizes ONE execution, not a
    rate-limit budget of duplicates."""
    args = {"tool": "my-tool", "manifest_sha256": "b" * 64}
    first = _gate("register_tool", args, session="s-once")
    cid = first["confirmation_id"]
    agent_gate.grant_approval(cid)
    resumed = _gate("register_tool", dict(args, confirmation_id=cid), session="s-once")
    assert resumed["reason"] == "allow_via_approval"
    replay = _gate("register_tool", dict(args, confirmation_id=cid), session="s-once")
    assert replay["decision"] == "deny"
    assert replay["reason"] == "approval_consumed"
    assert agent_gate.read_pending(cid)["consumed_at"]


def test_falsy_consumed_stamp_still_denies_the_replay():
    """`consumed_at` has INVERSE polarity to granted/denied: falsy reads as
    "not yet consumed". A truthiness check therefore let a spent approval be
    replayed by corrupting the stamp to null / "" / 0 — the gate must key on
    the field being PRESENT."""
    # request_confirmation is always-confirm (so no session grant covers the
    # repeat, unlike confirm-once) and rate category low/120-per-hour (so the
    # loop cannot exhaust the budget and deny for the wrong reason). The replay
    # must reuse the SAME session: the record is session-bound, and a different
    # session denies earlier as args_mismatch, never reaching the stamp check.
    for i, falsy in enumerate((None, "", 0)):
        session = f"s-falsy-{i}"
        args = {"kind": f"probe-{i}"}
        first = _gate("request_confirmation", args, session=session)
        cid = first["confirmation_id"]
        agent_gate.grant_approval(cid)
        assert _gate("request_confirmation", dict(args, confirmation_id=cid),
                     session=session)["reason"] == "allow_via_approval"
        # corrupt the spent stamp the way a partial write or hand-edit would
        record = agent_gate.read_pending(cid)
        record["consumed_at"] = falsy
        agent_gate._write_pending(record)
        replay = _gate("request_confirmation", dict(args, confirmation_id=cid),
                       session=session)
        assert replay["decision"] == "deny", f"replayed with consumed_at={falsy!r}"
        # "" is a str so it survives boundary validation and denies as consumed;
        # None/0 are not, so read_pending rejects the record even earlier. Both
        # deny — which is the property under test.
        assert replay["reason"] in ("approval_consumed", "approval_not_found")


def test_non_bool_granted_is_not_authorization():
    """A truth test on a corrupted field is an authorization decision made by
    accident: "false" is a truthy STRING and 1 is not the bool this code wrote.
    read_pending type-validates at the boundary, so these read as absent."""
    for i, bogus in enumerate(("false", "true", 1, "yes", [], {})):
        session = f"s-bogus-{i}"
        args = {"tool": f"add-panel-{i}"}
        first = _gate("run_write_tool", args, session=session)
        cid = first["confirmation_id"]
        record = agent_gate.read_pending(cid)
        record["granted"] = bogus
        agent_gate._write_pending(record)
        out = _gate("run_write_tool", dict(args, confirmation_id=cid), session=session)
        assert out["decision"] == "deny", f"granted={bogus!r} authorized the call"


def test_failed_reread_under_lock_denies_instead_of_reusing_the_stale_record():
    """Re-reading under the lock IS the replay guard. Falling back to the
    pre-lock copy on a failed re-read hands back a still-unconsumed record and
    reopens the window the lock exists to close."""
    args = {"tool": "my-tool", "manifest_sha256": "e" * 64}
    first = _gate("register_tool", args, session="s-reread")
    cid = first["confirmation_id"]
    agent_gate.grant_approval(cid)

    real_read = agent_gate.read_pending
    calls = {"n": 0}

    def flaky(confirmation_id):
        calls["n"] += 1
        return real_read(confirmation_id) if calls["n"] == 1 else None

    agent_gate.read_pending = flaky
    try:
        out = _gate("register_tool", dict(args, confirmation_id=cid), session="s-reread")
    finally:
        agent_gate.read_pending = real_read
    assert out["decision"] == "deny"
    assert out["reason"] == "approval_unreadable"


def test_confirm_once_replay_denies_but_grant_covers_repeats():
    """Confirm-once consumes the record too; repeats in the session ride the
    session grant, never a re-redeemed approval."""
    args = {"tool": "add-panel"}
    first = _gate("run_write_tool", args, session="s-consume")
    cid = first["confirmation_id"]
    agent_gate.grant_approval(cid)
    assert _gate("run_write_tool", dict(args, confirmation_id=cid),
                 session="s-consume")["reason"] == "allow_via_approval"
    replay = _gate("run_write_tool", dict(args, confirmation_id=cid), session="s-consume")
    assert replay["decision"] == "deny"
    assert replay["reason"] == "approval_consumed"
    # the plain (no-id) repeat still allows — via the grant, not the record
    again = _gate("run_write_tool", args, session="s-consume")
    assert again["reason"] == "allow_via_session_grant"


def test_session_grant_does_not_cross_to_another_tool():
    """One chip names ONE tool. Approving a benign write must not silently
    authorize a different, destructive write in the same session."""
    benign = {"tool": "add-panel"}
    first = _gate("run_write_tool", benign, session="s-tools")
    agent_gate.grant_approval(first["confirmation_id"])
    assert _gate("run_write_tool", dict(benign, confirmation_id=first["confirmation_id"]),
                 session="s-tools")["reason"] == "allow_via_approval"
    # the SAME tool rides the grant...
    assert _gate("run_write_tool", benign, session="s-tools")["reason"] == "allow_via_session_grant"
    # ...a DIFFERENT tool under the same action still needs its own chip
    other = _gate("run_write_tool", {"tool": "delete-everything"}, session="s-tools")
    assert other["decision"] == "awaiting_approval"


def test_session_grant_does_not_cross_to_another_tenant():
    """has_session_grant is tenant-scoped: a tenant presenting someone else's
    session id inherits nothing."""
    args = {"tool": "add-panel"}
    first = _gate("run_write_tool", args, tenant="t-A", session="s-shared")
    agent_gate.grant_approval(first["confirmation_id"])
    assert _gate("run_write_tool", dict(args, confirmation_id=first["confirmation_id"]),
                 tenant="t-A", session="s-shared")["reason"] == "allow_via_approval"
    assert _gate("run_write_tool", args, tenant="t-A",
                 session="s-shared")["reason"] == "allow_via_session_grant"
    other = _gate("run_write_tool", args, tenant="t-other", session="s-shared")
    assert other["decision"] == "awaiting_approval"


def test_legacy_grant_file_grants_nothing():
    """An old (session -> action) entry names neither tenant nor tool, so it
    cannot be re-keyed — it must be ignored, not trusted."""
    agent_gate.grants_file().parent.mkdir(parents=True, exist_ok=True)
    agent_gate.grants_file().write_text(
        json.dumps({"s-legacy": {"run_write_tool": "2026-01-01T00:00:00Z"}}),
        encoding="utf-8")
    res = _gate("run_write_tool", {"tool": "add-panel"}, session="s-legacy")
    assert res["decision"] == "awaiting_approval"


def test_cross_session_confirmation_id_denies_as_mismatch():
    """An approval filed in session A must not redeem in session B of the same
    tenant — redeeming there would plant a confirm-once grant in a session that
    was never shown the chip."""
    args = {"tool": "add-panel"}
    first = _gate("run_write_tool", args, session="s-filed")
    cid = first["confirmation_id"]
    agent_gate.grant_approval(cid)
    res = _gate("run_write_tool", dict(args, confirmation_id=cid), session="s-other")
    assert res["decision"] == "deny"
    assert res["reason"] == "args_mismatch"
    assert agent_gate.has_session_grant("t-gate", "s-other", "run_write_tool",
                                        agent_gate.grant_target(args)) is False
    # the filing session still redeems normally
    assert _gate("run_write_tool", dict(args, confirmation_id=cid),
                 session="s-filed")["reason"] == "allow_via_approval"


def test_args_mismatch_after_approval_denies():
    args = {"tool": "add-panel", "params": {"n": 2}}
    first = _gate("run_write_tool", args)
    cid = first["confirmation_id"]
    agent_gate.grant_approval(cid)
    drifted = dict(args, params={"n": 999}, confirmation_id=cid)
    res = _gate("run_write_tool", drifted)
    assert res["decision"] == "deny"
    assert res["reason"] == "args_mismatch"


def test_cross_tenant_confirmation_id_denies_as_mismatch():
    first = _gate("run_write_tool", {"tool": "add-panel"}, tenant="t-owner")
    cid = first["confirmation_id"]
    agent_gate.grant_approval(cid)
    res = _gate("run_write_tool", {"tool": "add-panel", "confirmation_id": cid},
                tenant="t-thief")
    assert res["decision"] == "deny"
    assert res["reason"] == "args_mismatch"


def test_denied_approval_denies_resume():
    first = _gate("run_write_tool", {"tool": "add-panel"})
    cid = first["confirmation_id"]
    agent_gate.deny_approval(cid)
    res = _gate("run_write_tool", {"tool": "add-panel", "confirmation_id": cid})
    assert res["decision"] == "deny"
    assert res["reason"] == "approval_denied"


def test_unknown_confirmation_id_denies():
    res = _gate("run_write_tool", {"tool": "add-panel", "confirmation_id": "deadbeef"})
    assert res["decision"] == "deny"
    assert res["reason"] == "approval_not_found"


def _backdate(cid: str) -> None:
    record = agent_gate.read_pending(cid)
    record["expires_at"] = "2000-01-01T00:00:00Z"
    agent_gate._write_pending(record)


def test_ttl_expiry_auto_denies_resume():
    first = _gate("run_write_tool", {"tool": "add-panel"})
    cid = first["confirmation_id"]
    agent_gate.grant_approval(cid)
    _backdate(cid)
    res = _gate("run_write_tool", {"tool": "add-panel", "confirmation_id": cid})
    assert res["decision"] == "deny"
    assert res["reason"] == "approval_expired"
    # the record was durably auto-denied
    assert agent_gate.read_pending(cid)["denied"] is True


def test_grant_refuses_expired_request():
    first = _gate("run_write_tool", {"tool": "add-panel"})
    cid = first["confirmation_id"]
    _backdate(cid)
    ok, record, reason = agent_gate.grant_approval(cid)
    assert not ok and reason == "expired"
    assert record["denied"] is True  # expiry racing the click auto-denies


def test_pending_not_yet_decided_stays_awaiting():
    first = _gate("run_write_tool", {"tool": "add-panel"})
    cid = first["confirmation_id"]
    res = _gate("run_write_tool", {"tool": "add-panel", "confirmation_id": cid})
    assert res["decision"] == "awaiting_approval"
    assert res["confirmation_id"] == cid


def test_always_confirm_never_persists_session_grant():
    args = {"tool": "my-tool", "manifest_sha256": "a" * 64}
    first = _gate("register_tool", args, session="s-R")
    assert first["decision"] == "awaiting_approval"
    cid = first["confirmation_id"]
    agent_gate.grant_approval(cid)
    resumed = _gate("register_tool", dict(args, confirmation_id=cid), session="s-R")
    assert resumed["decision"] == "allow"
    # the NEXT call in the SAME session must file a FRESH approval
    again = _gate("register_tool", args, session="s-R")
    assert again["decision"] == "awaiting_approval"
    assert again["confirmation_id"] != cid
    assert agent_gate.has_session_grant("t-gate", "s-R", "register_tool",
                                        agent_gate.grant_target(args)) is False


def test_tenant_overlay_tightens_gate(tmp_path):
    agent_policy.tenants_file().write_text(json.dumps({
        "t-tight": {"overlay": {"run_read_tool": {"policy": "confirm-once"}}}
    }), encoding="utf-8")
    res = _gate("run_read_tool", {"tool": "layer-report"}, tenant="t-tight")
    assert res["decision"] == "awaiting_approval"
    assert res["policy"] == "confirm-once"
    # untightened tenant still auto-allows
    assert _gate("run_read_tool", {"tool": "layer-report"}, tenant="t-loose")["decision"] == "allow"


def test_corrupt_policy_file_fails_closed(tmp_path, monkeypatch):
    bad = tmp_path / "bad_policy.json"
    bad.write_text("{nope", encoding="utf-8")
    monkeypatch.setenv("LEAF_AGENT_POLICY_FILE", str(bad))
    res = _gate("read_platform_state")
    assert res["decision"] == "deny"
    assert res["reason"].startswith("policy_load_failed")


def test_tier_override_reaches_gate(tmp_path, monkeypatch):
    _custom_policy(tmp_path, monkeypatch, lambda raw: raw.update(
        {"tier_overrides": {"hosted_pro": {"run_write_tool": {"policy": "auto"}}}}))
    res = _gate("run_write_tool", {"tool": "add-panel"}, tier="hosted_pro")
    assert res["decision"] == "allow"
    res2 = _gate("run_write_tool", {"tool": "add-panel"}, tier="hosted_starter")
    assert res2["decision"] == "awaiting_approval"


# --------------------------------------------------------------------------- #
# fail-closed state files (rate snapshot, tenant state)
# --------------------------------------------------------------------------- #
def test_corrupt_rate_state_denies_instead_of_going_unlimited():
    """An unreadable budget is NOT "no budget": a corrupt snapshot must deny
    every call, not hydrate an empty bucket and hand out unlimited runs."""
    agent_gate.rate_file().write_text("{nope", encoding="utf-8")
    for _ in range(3):
        res = _gate("read_platform_state")
        assert res["decision"] == "deny"
        assert res["reason"].startswith("rate_state_unreadable")
    # a structurally wrong (but parseable) snapshot fails closed the same way
    agent_gate.rate_file().write_text('["not", "a", "mapping"]', encoding="utf-8")
    assert _gate("read_platform_state")["reason"].startswith("rate_state_unreadable")


def test_unwritable_rate_snapshot_denies(tmp_path, monkeypatch):
    """The snapshot is the AUTHORITY, so a unit that cannot be recorded is a
    unit that was never spent — a restart would hand the budget back. Deny."""
    blocker = tmp_path / "blocker"  # a FILE where the snapshot's parent dir must be
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("LEAF_AGENT_RATE_FILE", str(blocker / "rate.json"))
    res = _gate("read_platform_state")
    assert res["decision"] == "deny"
    assert res["reason"].startswith("rate_state_unwritable")


def test_corrupt_timestamp_denies_instead_of_erasing_spent_units():
    """A malformed stamp makes the whole bucket unreadable. Dropping it would
    silently ERASE spent units — the corrupt-snapshot hole from the other side."""
    agent_gate.rate_file().parent.mkdir(parents=True, exist_ok=True)
    agent_gate.rate_file().write_text(
        json.dumps({"t-gate": {"low": [1.0, "not-a-timestamp", 2.0]}}), encoding="utf-8")
    res = _gate("read_platform_state")
    assert res["decision"] == "deny"
    assert res["reason"].startswith("rate_state_unreadable")
    # booleans are ints in Python — they must not pass as stamps either
    agent_gate.rate_file().write_text(
        json.dumps({"t-gate": {"low": [True]}}), encoding="utf-8")
    assert _gate("read_platform_state")["reason"].startswith("rate_state_unreadable")


def test_normal_rate_path_allows_and_counts_exactly_once(tmp_path, monkeypatch):
    """The happy path still spends exactly one unit per allowed call, and the
    durable snapshot is what it spends from."""
    _custom_policy(tmp_path, monkeypatch,
                   lambda raw: raw["rate_limits"].update({"low_per_hour": 2}))
    assert _gate("read_platform_state")["decision"] == "allow"
    snapshot = json.loads(agent_gate.rate_file().read_text(encoding="utf-8"))
    assert len(snapshot["t-gate"]["low"]) == 1
    assert _gate("read_platform_state")["decision"] == "allow"
    snapshot = json.loads(agent_gate.rate_file().read_text(encoding="utf-8"))
    assert len(snapshot["t-gate"]["low"]) == 2
    third = _gate("read_platform_state")
    assert third["decision"] == "deny"
    assert third["reason"] == "rate_limit_exceeded: low (2/2)"


def test_corrupt_tenant_state_file_denies_at_the_gate():
    """The tenant file carries only TIGHTENINGS (kill flag, tighten-only
    overlay), so an unreadable one denies rather than resolving permissive."""
    agent_policy.tenants_file().write_text("{nope", encoding="utf-8")
    res = _gate("read_platform_state")
    assert res["decision"] == "deny"
    assert res["reason"].startswith("tenant_state_load_failed")


# --------------------------------------------------------------------------- #
# revalidate — the refreshed action is re-CHECKED, not just re-read
# --------------------------------------------------------------------------- #
def _staged_policy(tmp_path, monkeypatch, mutate):
    """First load_policy() call yields the shipped catalog, every later one the
    mutated catalog — i.e. an operator edit landing between the step-2 catalog
    read and the step-5 revalidate, mid-chain."""
    raw = json.loads((SERVER_DIR / "agent_policy.json").read_text(encoding="utf-8"))
    mutate(raw)
    edited = tmp_path / "edited_policy.json"
    edited.write_text(json.dumps(raw), encoding="utf-8")
    before = agent_policy.load_policy(SERVER_DIR / "agent_policy.json")
    after = agent_policy.load_policy(edited)
    calls = {"n": 0}

    def _staged(path=None):
        calls["n"] += 1
        return before if calls["n"] == 1 else after

    monkeypatch.setattr(agent_policy, "load_policy", _staged)


def test_revalidate_rechecks_entitlement_against_refreshed_action(tmp_path, monkeypatch):
    """The mid-chain reload can change required_capability; the step-4 pass was
    against the OLD definition, so the call must not execute under the new one."""
    _staged_policy(tmp_path, monkeypatch, lambda raw: raw["actions"]["run_read_tool"].update(
        {"required_capability": "deploy"}))
    caps = dict(FULL_CAPS, deploy=False)
    res = _gate("run_read_tool", {"tool": "layer-report"}, caps=caps)
    assert res["decision"] == "deny"
    assert res["reason"] == "entitlement_required: tier lacks 'deploy'"


def test_revalidate_rechecks_args_schema_against_refreshed_action(tmp_path, monkeypatch):
    def _tighten(raw):
        raw["actions"]["run_read_tool"]["args_schema"]["required"] = ["tool", "dwg"]

    _staged_policy(tmp_path, monkeypatch, _tighten)
    res = _gate("run_read_tool", {"tool": "layer-report"})  # legal under the OLD schema
    assert res["decision"] == "deny"
    assert res["reason"].startswith("invalid_args")


# --------------------------------------------------------------------------- #
# args binding
# --------------------------------------------------------------------------- #
def test_canonical_hash_excludes_confirmation_id():
    args = {"tool": "add-panel", "params": {"n": 2}}
    with_id = dict(args, confirmation_id="abc123")
    assert agent_gate.canonical_args_hash(args) == agent_gate.canonical_args_hash(with_id)
    assert agent_gate.canonical_args_hash(args) != agent_gate.canonical_args_hash(
        {"tool": "add-panel", "params": {"n": 3}})
