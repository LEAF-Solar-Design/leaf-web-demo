"""T1 overlay stream contract.

The review's objection was that "the converse stream carries the revoke" named
no event, no subscription, no cursor, and no disconnect behaviour. The
subscription half is pinned in test_contract_freeze.py; this file covers the
rest, and every test names the user-visible failure it prevents.

Run:  cd server && python -m pytest tests/test_overlay_stream.py -q
"""
from __future__ import annotations

import pytest

import overlay_stream as st


SESSION = "sess-1"


def _revoked(seq=5, reason="operator_reverted", tokens=("color.canvas.bg",)):
    return st.revoked_event(session_id=SESSION, seq=seq, proposal_id="p-1",
                            tokens=tokens, document_version=9, reason=reason)


# --------------------------------------------------------------------------- #
# Envelope shape — the client destructures this; a missing field is a crash
# --------------------------------------------------------------------------- #
def test_every_event_carries_the_envelope_the_client_dedupes_on():
    for event in (
        st.proposed_event(session_id=SESSION, seq=1, proposal_id="p-1",
                          tokens=["color.canvas.bg"], document_version=8,
                          expires_at="2026-08-04T00:00:00Z"),
        st.decided_event(session_id=SESSION, seq=2, proposal_id="p-1",
                         state="approved", document_version=9),
        _revoked(),
    ):
        assert set(event) == {"v", "session_id", "turn_id", "seq", "type", "data"}
        assert event["turn_id"] is None  # explicit null, never absent


def test_seq_must_be_a_real_cursor_value():
    """A non-integer seq would break the client's `seq <= lastSeq` dedupe by
    comparing a string, silently letting a replayed event through twice."""
    with pytest.raises(st.StreamContractError):
        _revoked(seq="5")
    with pytest.raises(st.StreamContractError):
        _revoked(seq=-1)


# --------------------------------------------------------------------------- #
# The revoke payload
# --------------------------------------------------------------------------- #
def test_revoke_carries_token_ids_and_never_token_values():
    """The client re-reads the document instead of patching locally. A client
    that patches from events stays wrong forever once it misses one."""
    event = _revoked(tokens=["copy.home.title", "color.canvas.bg"])
    assert event["data"]["token_ids"] == ["color.canvas.bg", "copy.home.title"]
    assert "tokens" not in event["data"]
    assert "#" not in repr(event["data"])  # no colour literal rode along


def test_revoke_reason_is_a_closed_set():
    """An unrenderable reason becomes a blank notice — worse than none."""
    with pytest.raises(st.StreamContractError):
        _revoked(reason="because")
    for ok in ("operator_reverted", "lease_expired", "superseded"):
        assert _revoked(reason=ok)["data"]["reason"] == ok


def test_a_revoke_naming_no_tokens_is_refused():
    with pytest.raises(st.StreamContractError):
        _revoked(tokens=[])


def test_decided_states_the_outcome_rather_than_implying_it():
    """One event type with an explicit state; the client must not have to know
    which types are terminal."""
    assert st.decided_event(session_id=SESSION, seq=3, proposal_id="p-1",
                            state="denied", document_version=9)["data"]["state"] == "denied"
    with pytest.raises(st.StreamContractError):
        st.decided_event(session_id=SESSION, seq=3, proposal_id="p-1",
                         state="pending", document_version=9)


def test_proposed_carries_the_cas_witness_the_card_sends_back():
    event = st.proposed_event(session_id=SESSION, seq=1, proposal_id="p-1",
                              tokens=["color.canvas.bg"], document_version=8,
                              expires_at="2026-08-04T00:00:00Z")
    assert event["data"]["document_version"] == 8
    assert event["data"]["expires_at"]


# --------------------------------------------------------------------------- #
# Durable-then-broadcast
# --------------------------------------------------------------------------- #
def test_the_event_is_persisted_before_it_is_pushed():
    """Reversed, a crash between the two would drop the revoke from history:
    the live client saw it, every reconnecting client never will, and the two
    disagree permanently."""
    order = []
    st.publish(_revoked(),
               append_event=lambda *a: order.append("append"),
               broadcast=lambda e: order.append("broadcast"))
    assert order == ["append", "broadcast"]


def test_a_failed_broadcast_does_not_fail_the_revoke():
    """The durable write already succeeded, so the event WILL arrive on the
    next poll or reconnect. Raising would tell the caller its revoke failed."""
    appended = []

    def boom(_event):
        raise ConnectionError("subscriber gone")

    result = st.publish(_revoked(), append_event=lambda *a: appended.append(a),
                        broadcast=boom)
    assert appended and result["type"] == st.OVERLAY_REVOKED


def test_a_failed_append_DOES_fail_the_revoke():
    """The opposite of the case above: losing durability loses the event."""
    with pytest.raises(RuntimeError):
        st.publish(_revoked(),
                   append_event=lambda *a: (_ for _ in ()).throw(RuntimeError("db down")),
                   broadcast=lambda e: None)


def test_publish_refuses_a_non_overlay_event():
    with pytest.raises(st.StreamContractError):
        st.publish({"type": "text_delta", "session_id": SESSION,
                    "turn_id": None, "data": {}},
                   append_event=lambda *a: None)


# --------------------------------------------------------------------------- #
# Disconnect and replay — the review asked for this one by name
# --------------------------------------------------------------------------- #
def test_a_revoke_emitted_while_disconnected_is_delivered_on_reconnect():
    """THE disconnect test. The browser holds lastSeq=2, drops the connection,
    a revoke lands at seq 3, and it reconnects with ?after_seq=2. If replay
    excluded it the user would keep seeing a theme the operator withdrew."""
    transcript = [
        {"seq": 1, "type": "overlay_proposed"},
        {"seq": 2, "type": "overlay_decided"},
        _revoked(seq=3),  # emitted while the client was gone
    ]
    owed = st.replay_after(transcript, after_seq=2)
    assert [e["seq"] for e in owed] == [3]
    assert owed[0]["type"] == st.OVERLAY_REVOKED


def test_replay_boundary_matches_the_client_dedupe_exactly():
    """The client discards `seq <= lastSeq`. If the server replayed from
    `>= after_seq` the boundary event would arrive twice and be dropped — but
    the reverse error, replaying from `> after_seq + 1`, loses one silently.
    Pin the exact boundary."""
    events = [{"seq": n, "type": "overlay_decided"} for n in (1, 2, 3, 4)]
    assert [e["seq"] for e in st.replay_after(events, after_seq=2)] == [3, 4]
    assert [e["seq"] for e in st.replay_after(events, after_seq=0)] == [1, 2, 3, 4]
    assert st.replay_after(events, after_seq=4) == []


def test_replay_is_ordered_even_if_storage_returns_it_shuffled():
    """The client applies events in arrival order; a revoke arriving before the
    approval it revokes would leave the overlay applied."""
    events = [{"seq": 3, "type": "overlay_revoked"},
              {"seq": 1, "type": "overlay_proposed"},
              {"seq": 2, "type": "overlay_decided"}]
    assert [e["seq"] for e in st.replay_after(events, after_seq=0)] == [1, 2, 3]


def test_replay_does_not_hand_back_the_callers_own_objects():
    """A caller mutating a replayed event must not corrupt the transcript."""
    events = [{"seq": 1, "type": "overlay_decided"}]
    st.replay_after(events, after_seq=0)[0]["seq"] = 99
    assert events[0]["seq"] == 1
