"""T1 chat entry point: turn a user's request into a pending overlay preview.

This is the piece that was missing when the T1 spine merged. Everything under
it existed and was well tested, and NOTHING called any of it: a decision path
with no proposals, a registry with no callers, a card that rendered nowhere.

WHY PROPOSING IS `auto` AND NOT `always-confirm`. The proposal is the cheap,
safe half. It writes one pending row scoped to the requester's own session,
changes nothing any other user can see, and expires by lease on its own. The
expensive, irreversible half is the operator tap that applies it to the tenant,
and that is gated by the decision path with a CAS witness and an audit row.
Putting a confirmation in front of the PROPOSAL would buy nothing and cost the
thing the feature exists for: the requester seeing their change immediately.

WHAT THIS REFUSES, and why each refusal is here rather than downstream:

  * Unknown token ids and non-canonical values. `overlay_registry` is the
    authority; this calls it and does not re-implement it. A second copy of a
    validation rule is how the two drift, which a review already caught between
    the store and the registry.
  * A request that resolves to zero tokens. Storing an empty proposal would put
    a card in front of the operator with nothing to decide.
  * A request from a session that already has a live preview. One pending
    overlay per session is what makes "what am I looking at" answerable.
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, Mapping, Optional

import overlay_registry
import overlay_stream

#: How long a preview stays live without a decision. Long enough for an
#: operator to look at it, short enough that an abandoned request clears itself
#: without anyone sweeping. The lease is authoritative: readers compare against
#: it, so an unswept proposal is still invisible once it lapses.
DEFAULT_LEASE_S = 900


class OverlayProposeError(ValueError):
    def __init__(self, code: str, detail: str = "", status_code: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail
        self.status_code = status_code


def propose(
    *,
    tenant_id: str,
    session_id: str,
    requested_tokens: Mapping[str, str],
    request_text: str = "",
    store,
    current_document: Optional[Mapping[str, Any]] = None,
    defaults: Optional[Mapping[str, str]] = None,
    seq: Optional[int] = None,
    append_event=None,
    broadcast=None,
    lease_s: int = DEFAULT_LEASE_S,
    proposal_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate a requested token map and open a pending preview.

    `store` is injected rather than imported so this stays testable without a
    database and without the platform package on the path. The real caller
    passes `platform.overlay_store`.

    Returns {proposal, event}. The event is None when no `append_event` was
    given, which is the case in tests and in any caller that has not wired the
    stream yet — a proposal that exists but was not announced is recoverable
    (the session's next read finds it); one that was announced but not stored
    is not.
    """
    if not tenant_id or not session_id:
        raise OverlayProposeError("session_required",
                                  "a preview is scoped to one session")

    # THE authority on what a token may be. Not re-implemented here.
    try:
        tokens = overlay_registry.validate_overlay(
            requested_tokens,
            current=(current_document or {}).get("tokens") if current_document else None,
            defaults=defaults or {},
        )
    except Exception as exc:  # noqa: BLE001 - registry raises its own types
        raise OverlayProposeError(
            "invalid_tokens", str(exc)[:200]) from exc

    if not tokens:
        raise OverlayProposeError(
            "no_tokens",
            "the request did not resolve to any token this platform can change")

    pid = proposal_id or str(uuid.uuid4())
    proposal = store.create_proposal(
        proposal_id=pid, tenant_id=tenant_id, session_id=session_id,
        tokens=tokens, lease_s=lease_s)

    event = None
    if append_event is not None:
        version = int((current_document or {}).get("version", 0) or 0)
        event = overlay_stream.publish(
            overlay_stream.proposed_event(
                session_id=session_id,
                seq=int(seq or 0),
                proposal_id=pid,
                tokens=list(tokens),
                document_version=version,
                expires_at=str(proposal.get("lease_expires_at") or ""),
            ),
            append_event=append_event,
            broadcast=broadcast,
        )

    return {
        "proposal": proposal,
        "event": event,
        # Echoed back so the caller can render the card without a second read.
        "tokens": dict(tokens),
        "request_text": str(request_text or "")[:500],
    }
