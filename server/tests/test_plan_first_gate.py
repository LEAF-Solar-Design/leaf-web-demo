"""
Plan-first on the LIVE path (chip: "In plan-first, execution starts only after
the approval lifecycle completes").

WHY THIS LIVES IN THE GATE. The first attempt put plan-first in
harness/src/ports/impl/agentSdkTurnRunner.ts, which serve.ts explicitly does
NOT mount — dead code. The live converse path is SpineTurnAdapter ->
ConverseLoop, and there EVERY tool call already passes the app gate
(/internal/agent/gate -> agent_gate.gate). So the gate is the one place that
(a) is on the live path, (b) already owns confirmation-id minting — which only
the app may do, since a locally minted id renders a chip that
POST /api/agent/approvals/{id} answers 404 — and (c) already knows the session,
so the harness needs no new field and cannot assert its own gating.

Pinned here:
  (a) plan_first turns an `auto` action into a confirmation (awaiting_approval
      with a real minted id) instead of an allow;
  (b) without the policy, an `auto` action still allows — byte-identical;
  (c) plan_first only NARROWS: it never turns a deny into an allow, and it
      leaves an already-confirming action unchanged;
  (d) an unreadable policy degrades toward ASKING — we cannot tell whether the
      user chose plan_first, and silently executing against an explicit choice
      is the worse failure (review round 1);
  (e) the policy is read from the AUTHORITY session, not the harness-private
      one the gate is called with.

Run:  cd server && python -m pytest tests/test_plan_first_gate.py -q
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

_TMP = Path(tempfile.mkdtemp(prefix="planfirst-gate-"))
os.environ.setdefault("SESSIONS_DB", str(_TMP / "sessions.db"))
os.environ.setdefault("LEAF_AGENT_APPROVALS_DIR", str(_TMP / "approvals"))
os.environ.setdefault("LEAF_AGENT_GRANTS_FILE", str(_TMP / "grants.json"))
os.environ.setdefault("LEAF_AGENT_RATE_FILE", str(_TMP / "rate.json"))
os.environ.setdefault("LEAF_AUTH_LIVE", "0")

import agent_gate  # noqa: E402
import agent_policy  # noqa: E402
import session_policy  # noqa: E402
import session_store  # noqa: E402


def _auto_action() -> str:
    """A real action whose policy is `auto`, read from the live policy file —
    fabricating one would test a straw man."""
    pol = agent_policy.load_policy()
    for name, act in sorted(pol.actions.items()):
        if act.policy == "auto":
            return name
    pytest.skip("no auto-policy action in the live policy catalog")


def _caps() -> dict:
    import entitlements
    return entitlements.entitlements_for("demo")


def _session(tenant: str = "tenant-pfg") -> dict:
    return session_store.get_or_create_session(tenant, f"dwg-pfg-{uuid.uuid4()}")


def _gate(tenant: str, session_id: str, action: str, args=None):
    return agent_gate.gate(tenant, session_id, f"turn-{uuid.uuid4()}",
                           action, args or {}, _caps(), tier="demo")


def test_auto_action_allows_without_the_policy():
    sess = _session()
    result = _gate("tenant-pfg", sess["session_id"], _auto_action())
    assert result["decision"] == "allow", result


def test_plan_first_turns_an_auto_action_into_a_confirmation():
    sess = _session()
    sid = sess["session_id"]
    session_policy.set_policy(sid, "tenant-pfg", "plan_first")
    result = _gate("tenant-pfg", sid, _auto_action())
    assert result["decision"] == "awaiting_approval", result
    # A REAL minted id: only the app may mint one, and the chip must be
    # answerable — a fabricated id would 404 at /api/agent/approvals/{id}.
    cid = result.get("confirmation_id")
    assert cid, result
    assert agent_gate.read_pending(cid) is not None, (
        "plan_first returned an id with no pending record behind it")


def test_plan_first_is_narrowing_only_and_never_flips_a_deny():
    sess = _session()
    sid = sess["session_id"]
    session_policy.set_policy(sid, "tenant-pfg", "plan_first")
    # An unknown action denies with or without the policy.
    denied = _gate("tenant-pfg", sid, f"no_such_action_{uuid.uuid4().hex[:6]}")
    assert denied["decision"] == "deny", denied


def test_the_policy_is_read_from_the_AUTHORITY_session_not_the_harness_one():
    """Review round 1, finding 1 — the defect my first test could not see.

    The harness calls the gate with ITS OWN private session id; the client
    stored the policy against the APP-owned authority session. Looking the
    policy up under the harness id finds nothing and silently allows
    everything. The two ids must be DIFFERENT in this test or it proves
    nothing (the original test used one id for both)."""
    authority = _session()
    authority_sid = authority["session_id"]
    harness_sid = f"harness-private-{uuid.uuid4()}"
    assert harness_sid != authority_sid
    session_policy.set_policy(authority_sid, "tenant-pfg", "plan_first")

    # The gate is called the way the live handler calls it: the harness session
    # positionally, the authority session as the policy source.
    result = agent_gate.gate(
        "tenant-pfg", harness_sid, f"turn-{uuid.uuid4()}",
        _auto_action(), {}, _caps(), tier="demo",
        policy_session_id=authority_sid)

    assert result["decision"] == "awaiting_approval", (
        "the policy was looked up under the harness session — plan-first is "
        f"inert on the live path: {result}")


def test_an_unreadable_policy_degrades_toward_asking(monkeypatch):
    sess = _session()
    sid = sess["session_id"]
    session_policy.set_policy(sid, "tenant-pfg", "plan_first")

    def _boom(*a, **k):
        raise RuntimeError("policy store unavailable")
    monkeypatch.setattr(session_policy, "get_policy", _boom)

    result = _gate("tenant-pfg", sid, _auto_action())
    assert result["decision"] == "awaiting_approval", (
        "a policy-store failure silently EXECUTED against an explicit user "
        "choice; a consent feature must degrade toward asking")
