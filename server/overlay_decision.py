"""T1 overlay decision path — the state machine that gates every runtime
theme/copy overlay before it can reach a tenant.

WHY THIS EXISTS AND WHY IT IS FIRST. The T1 overlay lane is runtime-writable:
an approved overlay changes what the live product serves WITHOUT passing
through git, review, build or deploy. That speed is the whole point, and it is
also why nothing may apply an overlay before this module says it may. The
first draft of the spec had overlay application shipping BEFORE its approval
guard; an adversarial review caught the ordering, so the guard ships first.

Storage-agnostic on purpose: this is pure decision logic over an immutable
proposal plus a versioned tenant document. The caller supplies the current
state and persists the outcome, which keeps every rule here unit-testable with
no database and makes the SQL binding a thin, separately-reviewable layer.

The four properties the review demanded, and where each lives:

  IDEMPOTENCY   `decide()` takes a decision_key. Re-applying the SAME key to an
                already-decided proposal returns the ORIGINAL outcome instead
                of erroring, so a retried tap (double-click, network retry,
                at-least-once queue) can never produce two decisions.
  REPLAY        A DIFFERENT key against a decided proposal is REFUSED. That is
                the difference between "the same tap arriving twice" and "a
                captured request being replayed later"; conflating them is how
                a revoked approval comes back to life.
  CAS           Every mutating call carries the tenant document version it read.
                A stale version loses, so two operators deciding concurrently
                cannot silently overwrite each other.
  EXPIRY        A pending proposal carries a lease deadline. Expiry is decided
                by comparing against a caller-supplied `now`, never by a
                background sweeper being punctual — an un-swept record is still
                EXPIRED to every reader. The push-to-revoke path is the fast
                path; this is the backstop that makes rejected CSS impossible
                to keep on screen indefinitely.

Nothing here trusts model output: the proposal is data, the decision is an
operator action recorded by the server, and this module never reads either from
a prompt.
"""
from __future__ import annotations

import hmac
import time
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Mapping, Optional, Tuple

# --------------------------------------------------------------------------- #
# States
# --------------------------------------------------------------------------- #
PENDING = "pending"
APPROVED = "approved"
DENIED = "denied"
EXPIRED = "expired"
REVERTED = "reverted"

#: Terminal states never transition again (except APPROVED -> REVERTED).
TERMINAL = frozenset({DENIED, EXPIRED, REVERTED})

#: Default lease. Short on purpose: a pending overlay is visible to the
#: requester's session, so an abandoned decision must stop being visible soon
#: rather than linger until someone notices.
DEFAULT_LEASE_S = 900


class OverlayDecisionError(RuntimeError):
    """Client-safe reason code + status; detail is operator-only."""

    def __init__(self, code: str, status_code: int = 409, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.detail = str(detail)


@dataclass(frozen=True)
class Proposal:
    """An IMMUTABLE proposal. Decisions never edit it; they produce a new
    record. Mutating a proposal in place would make the audit trail a lie."""

    proposal_id: str
    tenant_id: str
    session_id: str
    #: Opaque here — the token registry validates content, this gates it.
    tokens: Mapping[str, str]
    created_at: float
    lease_expires_at: float
    state: str = PENDING
    decided_at: Optional[float] = None
    decided_by: Optional[str] = None
    decision_key: Optional[str] = None
    #: Tenant-document version this proposal was decided against (CAS witness).
    applied_version: Optional[int] = None
    reason: Optional[str] = None

    def is_expired(self, now: float) -> bool:
        """Lease expiry is a FUNCTION OF TIME, not of a sweeper having run."""
        return self.state == PENDING and now >= self.lease_expires_at


@dataclass(frozen=True)
class AuditEvent:
    """Emitted for every state change. The caller persists these; this module
    never logs (a decision record containing tenant copy must not land in
    application logs by accident)."""

    proposal_id: str
    tenant_id: str
    from_state: str
    to_state: str
    at: float
    actor: Optional[str]
    decision_key: Optional[str]
    detail: Dict[str, Any] = field(default_factory=dict)


def new_proposal(
    *,
    proposal_id: str,
    tenant_id: str,
    session_id: str,
    tokens: Mapping[str, str],
    now: Optional[float] = None,
    lease_s: int = DEFAULT_LEASE_S,
) -> Proposal:
    now = time.time() if now is None else now
    if lease_s <= 0:
        raise OverlayDecisionError("lease_invalid", 422, f"lease_s={lease_s}")
    for key in ("proposal_id", "tenant_id", "session_id"):
        value = locals()[key]
        if not isinstance(value, str) or not value.strip():
            raise OverlayDecisionError("identity_invalid", 422, key)
    return Proposal(
        proposal_id=proposal_id,
        tenant_id=tenant_id,
        session_id=session_id,
        tokens=dict(tokens),
        created_at=now,
        lease_expires_at=now + lease_s,
    )


def _require_key(decision_key: Any) -> str:
    if not isinstance(decision_key, str) or len(decision_key) < 8:
        raise OverlayDecisionError(
            "decision_key_invalid", 422,
            "a decision needs a caller-generated key of >= 8 chars")
    return decision_key


def _settled_replay(
    proposal: Proposal, decision_key: str, wanted: str,
) -> Tuple[Proposal, Optional[AuditEvent]]:
    """Distinguish a RETRY from a REPLAY on an already-decided proposal.

    Same key + same intent -> return the original outcome unchanged (idempotent).
    Anything else -> refuse. `compare_digest` because the key is a
    capability-ish value and a timing oracle on it is free to remove.
    """
    same_key = proposal.decision_key is not None and hmac.compare_digest(
        proposal.decision_key, decision_key)
    if same_key and proposal.state == wanted:
        return proposal, None
    raise OverlayDecisionError(
        "already_decided", 409,
        f"state={proposal.state} wanted={wanted} key_match={same_key}")


def _check_expiry(proposal: Proposal, now: float) -> Proposal:
    """An expired-but-unswept proposal reads as EXPIRED to every caller. This
    runs BEFORE any decision so a lapsed lease cannot be approved."""
    if proposal.is_expired(now):
        return replace(proposal, state=EXPIRED, decided_at=now,
                       reason="lease_expired")
    return proposal


def decide(
    proposal: Proposal,
    *,
    approve: bool,
    actor: str,
    decision_key: str,
    tenant_version: int,
    current_tenant_version: int,
    now: Optional[float] = None,
) -> Tuple[Proposal, Optional[AuditEvent]]:
    """Approve or deny. Returns (record, audit) — audit is None on an
    idempotent retry, because a retry is not a new event.

    `tenant_version` is what the operator's card was rendered against;
    `current_tenant_version` is what the store holds now. They must match, or
    the operator is deciding about a state they did not see.
    """
    now = time.time() if now is None else now
    key = _require_key(decision_key)
    if not isinstance(actor, str) or not actor.strip():
        raise OverlayDecisionError("actor_required", 422)

    wanted = APPROVED if approve else DENIED

    if proposal.state != PENDING:
        return _settled_replay(proposal, key, wanted)

    expired = _check_expiry(proposal, now)
    if expired.state == EXPIRED:
        # Surface expiry rather than silently denying: the operator must learn
        # the decision window lapsed, not believe they denied it.
        raise OverlayDecisionError(
            "lease_expired", 410,
            f"expired_at={proposal.lease_expires_at} now={now}")

    # CAS. Only meaningful for APPROVE (a deny changes no tenant state), but
    # enforced for both so a stale card can never drive any decision.
    if int(tenant_version) != int(current_tenant_version):
        raise OverlayDecisionError(
            "version_conflict", 409,
            f"card={tenant_version} store={current_tenant_version}")

    decided = replace(
        proposal,
        state=wanted,
        decided_at=now,
        decided_by=actor,
        decision_key=key,
        applied_version=(int(current_tenant_version) + 1) if approve else None,
    )
    return decided, AuditEvent(
        proposal_id=proposal.proposal_id,
        tenant_id=proposal.tenant_id,
        from_state=PENDING,
        to_state=wanted,
        at=now,
        actor=actor,
        decision_key=key,
        detail={"token_count": len(proposal.tokens),
                "tenant_version": int(current_tenant_version)},
    )


def revert(
    proposal: Proposal,
    *,
    actor: str,
    decision_key: str,
    now: Optional[float] = None,
) -> Tuple[Proposal, Optional[AuditEvent]]:
    """Undo an APPROVED overlay. Only approved records revert — reverting a
    denied or expired one would imply it had been live."""
    now = time.time() if now is None else now
    key = _require_key(decision_key)
    if proposal.state == REVERTED:
        return _settled_replay(proposal, key, REVERTED)
    if proposal.state != APPROVED:
        raise OverlayDecisionError(
            "not_revertible", 409, f"state={proposal.state}")
    reverted = replace(proposal, state=REVERTED, decided_at=now,
                       decided_by=actor, decision_key=key, reason="reverted")
    return reverted, AuditEvent(
        proposal_id=proposal.proposal_id,
        tenant_id=proposal.tenant_id,
        from_state=APPROVED,
        to_state=REVERTED,
        at=now,
        actor=actor,
        decision_key=key,
    )


def sweep_expired(
    proposal: Proposal, *, now: Optional[float] = None,
) -> Tuple[Proposal, Optional[AuditEvent]]:
    """Materialise a lapsed lease. Idempotent, and safe to run late — reads
    already treat a lapsed proposal as expired, so this only writes the fact
    down (and gives the revoke-the-session-overlay path something to fire on)."""
    now = time.time() if now is None else now
    if not proposal.is_expired(now):
        return proposal, None
    expired = _check_expiry(proposal, now)
    return expired, AuditEvent(
        proposal_id=proposal.proposal_id,
        tenant_id=proposal.tenant_id,
        from_state=PENDING,
        to_state=EXPIRED,
        at=now,
        actor=None,
        decision_key=None,
        detail={"lease_expires_at": proposal.lease_expires_at},
    )


def session_overlay_visible(proposal: Proposal, *, now: Optional[float] = None) -> bool:
    """Should the REQUESTER's session still show this overlay?

    True only while genuinely pending or after approval. A denied, expired or
    reverted proposal answers False the instant it is read — so a client that
    missed the push event stops showing rejected styling on its next poll or
    reconnect, without depending on the event arriving at all.
    """
    now = time.time() if now is None else now
    if proposal.state == APPROVED:
        return True
    if proposal.state != PENDING:
        return False
    return not proposal.is_expired(now)
