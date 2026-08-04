"""T1 overlay decision path.

Each test names the failure it prevents. The four properties an adversarial
review of the spec demanded — idempotency, replay refusal, compare-and-swap,
and time-based expiry — are the four sections below.

Run:  cd server && python -m pytest tests/test_overlay_decision.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import overlay_decision as od  # noqa: E402

T0 = 1_000_000.0
KEY = "decision-key-aaaaaaaa"
KEY2 = "decision-key-bbbbbbbb"


def _pending(**kw):
    return od.new_proposal(
        proposal_id=kw.get("pid", "p-1"),
        tenant_id=kw.get("tenant", "tenant-a"),
        session_id=kw.get("session", "sess-1"),
        tokens=kw.get("tokens", {"color.bg": "#ffffff"}),
        now=kw.get("now", T0),
        lease_s=kw.get("lease_s", od.DEFAULT_LEASE_S),
    )


# --------------------------------------------------------------------------- #
# 1. Idempotency — a retried tap must not decide twice
# --------------------------------------------------------------------------- #
def test_same_key_retry_returns_the_original_outcome():
    """A double-click, a network retry, or an at-least-once queue redelivery
    must not produce two decisions."""
    p = _pending()
    approved, ev = od.decide(p, approve=True, actor="op", decision_key=KEY,
                             tenant_version=7, current_tenant_version=7, now=T0 + 1)
    assert approved.state == od.APPROVED and ev is not None

    again, ev2 = od.decide(approved, approve=True, actor="op", decision_key=KEY,
                           tenant_version=7, current_tenant_version=7, now=T0 + 2)
    assert again == approved       # byte-identical record
    assert ev2 is None             # a retry is not a new audit event


def test_retry_does_not_bump_the_applied_version():
    """Version drift on retry would make the CAS witness meaningless."""
    p = _pending()
    approved, _ = od.decide(p, approve=True, actor="op", decision_key=KEY,
                            tenant_version=3, current_tenant_version=3, now=T0 + 1)
    again, _ = od.decide(approved, approve=True, actor="op", decision_key=KEY,
                         tenant_version=3, current_tenant_version=3, now=T0 + 9)
    assert again.applied_version == approved.applied_version == 4


# --------------------------------------------------------------------------- #
# 2. Replay refusal — a captured request must not resurrect a decision
# --------------------------------------------------------------------------- #
def test_different_key_on_a_decided_proposal_is_refused():
    """The difference between the same tap twice and a replayed request. If
    these were conflated, a denied overlay could be flipped to approved."""
    p = _pending()
    denied, _ = od.decide(p, approve=False, actor="op", decision_key=KEY,
                          tenant_version=1, current_tenant_version=1, now=T0 + 1)
    with pytest.raises(od.OverlayDecisionError) as exc:
        od.decide(denied, approve=True, actor="op", decision_key=KEY2,
                  tenant_version=1, current_tenant_version=1, now=T0 + 2)
    assert exc.value.code == "already_decided"


def test_same_key_cannot_flip_a_denial_into_an_approval():
    """Even the ORIGINAL key may not change the verdict — idempotency means
    'same outcome', not 'same actor may reconsider'."""
    p = _pending()
    denied, _ = od.decide(p, approve=False, actor="op", decision_key=KEY,
                          tenant_version=1, current_tenant_version=1, now=T0 + 1)
    with pytest.raises(od.OverlayDecisionError) as exc:
        od.decide(denied, approve=True, actor="op", decision_key=KEY,
                  tenant_version=1, current_tenant_version=1, now=T0 + 2)
    assert exc.value.code == "already_decided"


def test_decision_key_must_be_substantial():
    for bad in (None, "", "short", 12345):
        with pytest.raises(od.OverlayDecisionError) as exc:
            od.decide(_pending(), approve=True, actor="op", decision_key=bad,
                      tenant_version=1, current_tenant_version=1, now=T0)
        assert exc.value.code == "decision_key_invalid"


# --------------------------------------------------------------------------- #
# 3. Compare-and-swap — concurrent operators must not overwrite each other
# --------------------------------------------------------------------------- #
def test_stale_card_version_loses():
    """The operator decided about a tenant state they were not looking at."""
    with pytest.raises(od.OverlayDecisionError) as exc:
        od.decide(_pending(), approve=True, actor="op", decision_key=KEY,
                  tenant_version=4, current_tenant_version=5, now=T0 + 1)
    assert exc.value.code == "version_conflict"


def test_cas_applies_to_denial_too():
    """A deny changes no tenant state, but a stale card still means the
    operator is looking at something other than what exists."""
    with pytest.raises(od.OverlayDecisionError) as exc:
        od.decide(_pending(), approve=False, actor="op", decision_key=KEY,
                  tenant_version=1, current_tenant_version=2, now=T0 + 1)
    assert exc.value.code == "version_conflict"


# --------------------------------------------------------------------------- #
# 4. Expiry — a lapsed lease is expired whether or not a sweeper ran
# --------------------------------------------------------------------------- #
def test_expiry_is_time_based_not_sweeper_based():
    """The backstop that makes rejected/abandoned CSS impossible to keep on
    screen: an unswept record still reads EXPIRED."""
    p = _pending(lease_s=60)
    assert p.is_expired(T0 + 59) is False
    assert p.is_expired(T0 + 60) is True
    assert od.session_overlay_visible(p, now=T0 + 61) is False


def test_approving_a_lapsed_lease_is_refused_not_silently_denied():
    """The operator must learn the window lapsed rather than believe they
    denied it."""
    p = _pending(lease_s=60)
    with pytest.raises(od.OverlayDecisionError) as exc:
        od.decide(p, approve=True, actor="op", decision_key=KEY,
                  tenant_version=1, current_tenant_version=1, now=T0 + 61)
    assert exc.value.code == "lease_expired"
    assert exc.value.status_code == 410


def test_sweep_is_idempotent_and_late_safe():
    p = _pending(lease_s=60)
    swept, ev = od.sweep_expired(p, now=T0 + 900)
    assert swept.state == od.EXPIRED and ev is not None
    again, ev2 = od.sweep_expired(swept, now=T0 + 901)
    assert again.state == od.EXPIRED and ev2 is None  # no duplicate audit


def test_sweep_leaves_a_live_proposal_alone():
    p = _pending(lease_s=600)
    same, ev = od.sweep_expired(p, now=T0 + 10)
    assert same == p and ev is None


# --------------------------------------------------------------------------- #
# 5. Session visibility — the client stops showing rejected styling WITHOUT
#    depending on a push event arriving
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("approve,visible", [(True, True), (False, False)])
def test_visibility_follows_the_decision(approve, visible):
    p = _pending()
    decided, _ = od.decide(p, approve=approve, actor="op", decision_key=KEY,
                           tenant_version=1, current_tenant_version=1, now=T0 + 1)
    assert od.session_overlay_visible(decided, now=T0 + 2) is visible


def test_denied_overlay_is_invisible_even_if_the_push_never_arrived():
    """THE failure this whole module exists to prevent: a disconnected stream
    or suspended tab leaving a user looking at a change that was rejected."""
    p = _pending()
    denied, _ = od.decide(p, approve=False, actor="op", decision_key=KEY,
                          tenant_version=1, current_tenant_version=1, now=T0 + 1)
    # No event was delivered anywhere; the next READ is enough.
    assert od.session_overlay_visible(denied, now=T0 + 99_999) is False


def test_reverted_overlay_stops_being_visible():
    p = _pending()
    approved, _ = od.decide(p, approve=True, actor="op", decision_key=KEY,
                            tenant_version=1, current_tenant_version=1, now=T0 + 1)
    reverted, ev = od.revert(approved, actor="op", decision_key=KEY2, now=T0 + 5)
    assert reverted.state == od.REVERTED and ev is not None
    assert od.session_overlay_visible(reverted, now=T0 + 6) is False


# --------------------------------------------------------------------------- #
# 6. Revert
# --------------------------------------------------------------------------- #
def test_only_approved_reverts():
    """Reverting a denied or expired proposal would imply it had been live."""
    p = _pending()
    denied, _ = od.decide(p, approve=False, actor="op", decision_key=KEY,
                          tenant_version=1, current_tenant_version=1, now=T0 + 1)
    with pytest.raises(od.OverlayDecisionError) as exc:
        od.revert(denied, actor="op", decision_key=KEY2, now=T0 + 2)
    assert exc.value.code == "not_revertible"


def test_revert_is_idempotent():
    p = _pending()
    approved, _ = od.decide(p, approve=True, actor="op", decision_key=KEY,
                            tenant_version=1, current_tenant_version=1, now=T0 + 1)
    r1, ev1 = od.revert(approved, actor="op", decision_key=KEY2, now=T0 + 5)
    r2, ev2 = od.revert(r1, actor="op", decision_key=KEY2, now=T0 + 6)
    assert r2 == r1 and ev1 is not None and ev2 is None


# --------------------------------------------------------------------------- #
# 7. Immutability + audit hygiene
# --------------------------------------------------------------------------- #
def test_proposals_are_immutable():
    """Decisions produce new records; editing in place would make the audit
    trail a lie."""
    p = _pending()
    with pytest.raises(Exception):
        p.state = od.APPROVED  # frozen dataclass
    od.decide(p, approve=True, actor="op", decision_key=KEY,
              tenant_version=1, current_tenant_version=1, now=T0 + 1)
    assert p.state == od.PENDING  # the original is untouched


def test_audit_carries_no_token_content():
    """Tenant copy must not leak into audit records (which are logged and
    shipped); the count is enough to reason about a decision."""
    p = _pending(tokens={"copy.cta": "Delete everything", "color.bg": "#000000"})
    _, ev = od.decide(p, approve=True, actor="op", decision_key=KEY,
                      tenant_version=2, current_tenant_version=2, now=T0 + 1)
    blob = repr(ev)
    assert "Delete everything" not in blob
    assert "#000000" not in blob
    assert ev.detail["token_count"] == 2


def test_actor_is_required():
    for bad in (None, "", "   "):
        with pytest.raises(od.OverlayDecisionError) as exc:
            od.decide(_pending(), approve=True, actor=bad, decision_key=KEY,
                      tenant_version=1, current_tenant_version=1, now=T0)
        assert exc.value.code == "actor_required"


def test_revert_requires_an_actor_exactly_as_decide_does():
    """A review reverted with actor="" and got an audit event crediting
    nobody. The rule lived in decide() only, so one copy went missing; it is
    now a single helper both paths call."""
    approved, _ = od.decide(_pending(), approve=True, actor="op",
                            decision_key=KEY, tenant_version=1,
                            current_tenant_version=1, now=T0 + 1)
    for blank in ("", "   ", None, 7):
        with pytest.raises(od.OverlayDecisionError) as e:
            od.revert(approved, actor=blank, decision_key=KEY2, now=T0 + 5)
        assert e.value.code == "actor_required"
