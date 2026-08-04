"""T1 overlay stream contract — how a decision reaches the people watching.

WHY THIS MODULE EXISTS AT ALL. The overlay design assumed "the converse stream
carries the revoke", and an adversarial review refused that as hand-waving: no
event name, no subscription, no replay rule, no disconnect behaviour. That
refusal was earned. Twice now this repo has shipped an event the server
durably emits and the client never subscribes to:

  * `question_required` — in the frozen §18.3 vocabulary, rendered by the
    client, absent from the browser's addEventListener list. The card appeared
    only when the transcript poll happened to win a race against SSE.
  * `turn_queued` / `turn_queue_dropped` — appended durably by turn_runner and
    read by composer.js, likewise never subscribed.

Both are the SAME defect: SSE delivers only what the client registered a
listener for, so an unsubscribed type is dropped in total silence. Nothing
errors. The feature simply behaves intermittently, which is the most expensive
failure mode there is.

So this module does not just name three overlay events. It makes the
vocabulary a single declared set that a test pins against the client, which is
the only thing that stops the next event from repeating the same bug.

THE REVOKE PATH SPECIFICALLY. Revocation is the one overlay transition that a
user must never miss, because missing it means they keep seeing a theme the
operator withdrew. Its guarantees:

  * Durable BEFORE broadcast. The revoke is appended to the session transcript
    first, then pushed. A crash between the two costs latency, not correctness,
    because the transcript replay will still carry it.
  * Replayable by cursor. Every envelope carries a monotonic `seq`; the client
    reconnects with `?after_seq=N`. A revoke emitted while the browser was
    disconnected is delivered on reconnect rather than lost.
  * Carries no token VALUES. A revoke says which token ids stopped applying and
    what the document version now is. The client re-reads the document; it does
    not patch its local copy from the event. Patching would let a dropped or
    reordered event leave the browser showing a state the server never had.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Optional

# --------------------------------------------------------------------------- #
# The vocabulary
# --------------------------------------------------------------------------- #
#: A pending overlay now exists for this session — render the preview and the
#: operator card. Data: {proposal_id, token_ids, document_version, expires_at}.
OVERLAY_PROPOSED = "overlay_proposed"

#: An operator approved or denied. Data: {proposal_id, state, document_version}.
#: `state` is the decision-path state, so a client never infers it from which
#: event arrived — one event type, one explicit field.
OVERLAY_DECIDED = "overlay_decided"

#: A previously applied overlay stopped applying: reverted by an operator, or
#: its lease lapsed. Data: {proposal_id, token_ids, document_version, reason}.
OVERLAY_REVOKED = "overlay_revoked"

#: Every overlay event type. Exported as a set so the contract freeze test can
#: assert the client subscribes to all of them without restating the list.
OVERLAY_EVENT_TYPES = frozenset({OVERLAY_PROPOSED, OVERLAY_DECIDED, OVERLAY_REVOKED})

#: Reasons an overlay stops applying. Closed set: an unknown reason reaching the
#: client would render as blank text in a notice the user needs to understand.
REVOKE_REASONS = frozenset({"operator_reverted", "lease_expired", "superseded"})


class StreamContractError(ValueError):
    """An event was built that the client could not have handled correctly."""


# --------------------------------------------------------------------------- #
# Envelope construction
# --------------------------------------------------------------------------- #
def _token_ids(tokens: Iterable[str]) -> list:
    """Sorted, de-duplicated token ids.

    Sorted because two events describing the same change must compare equal in
    tests and in logs; an ordering that follows dict insertion would make a
    replayed event look different from the original one for no reason.
    """
    return sorted({str(t) for t in tokens})


def _base(session_id: str, seq: int, type_: str, data: Mapping[str, Any]) -> Dict[str, Any]:
    if not session_id:
        raise StreamContractError("session_id is required — an event with no session cannot be routed")
    if not isinstance(seq, int) or seq < 0:
        raise StreamContractError(f"seq must be a non-negative int, got {seq!r}")
    return {
        "v": 1,
        "session_id": session_id,
        # Overlay events are not part of any turn. `turn_id: None` is explicit
        # rather than absent so a client destructuring the envelope gets a null
        # instead of an undefined it might mistake for a missing field.
        "turn_id": None,
        "seq": seq,
        "type": type_,
        "data": dict(data),
    }


def proposed_event(*, session_id: str, seq: int, proposal_id: str,
                   tokens: Iterable[str], document_version: int,
                   expires_at: str) -> Dict[str, Any]:
    """A pending overlay exists. Carries the CAS witness the operator card will
    send back, so the card cannot be rendered against a version it never saw."""
    ids = _token_ids(tokens)
    if not ids:
        raise StreamContractError("a proposal with no tokens has nothing to preview")
    return _base(session_id, seq, OVERLAY_PROPOSED, {
        "proposal_id": proposal_id,
        "token_ids": ids,
        "document_version": int(document_version),
        "expires_at": expires_at,
    })


def decided_event(*, session_id: str, seq: int, proposal_id: str,
                  state: str, document_version: int) -> Dict[str, Any]:
    """An operator decided. The client clears the card on ANY settled state —
    it must not have to know which states are terminal."""
    if state not in ("approved", "denied", "expired"):
        raise StreamContractError(f"decided_event carries a settled state, got {state!r}")
    return _base(session_id, seq, OVERLAY_DECIDED, {
        "proposal_id": proposal_id,
        "state": state,
        "document_version": int(document_version),
    })


def revoked_event(*, session_id: str, seq: int, proposal_id: str,
                  tokens: Iterable[str], document_version: int,
                  reason: str) -> Dict[str, Any]:
    """An applied overlay stopped applying.

    Deliberately carries token IDS and not values. The client drops those ids
    and re-reads the document at `document_version`. Sending values would
    invite the client to patch locally, and a locally patched client that
    missed one event stays wrong forever with no way to notice.
    """
    if reason not in REVOKE_REASONS:
        raise StreamContractError(
            f"unknown revoke reason {reason!r} — a reason the client cannot "
            f"render is worse than no notice at all")
    ids = _token_ids(tokens)
    if not ids:
        raise StreamContractError("a revoke that names no tokens tells the client nothing")
    return _base(session_id, seq, OVERLAY_REVOKED, {
        "proposal_id": proposal_id,
        "token_ids": ids,
        "document_version": int(document_version),
        "reason": reason,
    })


# --------------------------------------------------------------------------- #
# Durable-then-broadcast
# --------------------------------------------------------------------------- #
def publish(event: Mapping[str, Any], *, append_event, broadcast=None) -> Dict[str, Any]:
    """Persist an overlay event, THEN push it. Returns the event.

    Order is the whole point. Appending first means a crash before the push
    costs the live client some latency and nothing else, because the reconnect
    replay reads the transcript. Pushing first would mean a crash drops the
    event from history entirely: the live client saw a revoke that no
    reconnecting client will ever see, and the two disagree permanently.

    `broadcast` failing is NOT propagated. The durable write already succeeded,
    so the event WILL reach every client on their next poll or reconnect; a
    raise here would tell the caller its revoke failed when it did not.
    """
    type_ = event["type"]
    if type_ not in OVERLAY_EVENT_TYPES:
        raise StreamContractError(f"{type_!r} is not an overlay event")

    append_event(event["session_id"], event["turn_id"], type_, event["data"])

    if broadcast is not None:
        try:
            broadcast(event)
        except Exception:  # noqa: BLE001 — see docstring: durability already won
            pass
    return dict(event)


def replay_after(events: Iterable[Mapping[str, Any]], *, after_seq: int) -> list:
    """The events a reconnecting client is owed, given the last seq it saw.

    Strictly greater than `after_seq`, in ascending order. This mirrors the
    client's own dedupe (`seq <= lastSeq` is discarded), so the two ends agree
    on the boundary. An off-by-one here would either duplicate a revoke or drop
    one, and dropping one leaves a withdrawn theme on screen.
    """
    kept = [dict(e) for e in events if int(e.get("seq", -1)) > int(after_seq)]
    kept.sort(key=lambda e: int(e["seq"]))
    return kept
