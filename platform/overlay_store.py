"""T1 overlay store — the thin SQL binding under `overlay_decision`.

The decision RULES live in `server/overlay_decision.py` as pure logic over an
immutable proposal plus a versioned document; this module only persists them.
Keeping the split means the rules stay testable with no database, and this
layer stays small enough to read in one sitting — which matters, because every
correctness property the design depends on has to survive contact with
concurrency here.

The three that are easy to lose in a SQL layer, and how each survives:

  CAS      `approve()` updates `overlay_documents` with `WHERE version = %s`.
           A stale operator card matches zero rows and raises, so two
           operators deciding at once cannot silently overwrite each other.
           The version bump and the proposal row are written in ONE
           transaction — a crash between them would otherwise leave a proposal
           claiming an `applied_version` the document never reached.
  REPLAY   A decided proposal is never UPDATEd. Transitions INSERT a new
           revision, and the caller's decision_key is compared against the
           stored one to tell a retry (same key, same intent -> return the
           original) from a replay (anything else -> refuse).
  EXPIRY   Readers filter on `lease_expires_at`, so a lapsed proposal reads as
           expired whether or not the sweeper has run. `sweep_expired` only
           writes the fact down.

Content validation is NOT here. Tokens are validated by `overlay_registry`
before they ever reach this module; the columns store the canonical form.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional, Tuple

from . import db  # noqa: F401  (package-relative, matches store.py's idiom)


class OverlayStoreError(RuntimeError):
    def __init__(self, code: str, status_code: int = 409, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.detail = str(detail)


_PROPOSAL_COLS = (
    "proposal_id, revision, tenant_id, session_id, tokens, state, "
    "created_at, lease_expires_at, decided_at, decided_by, decision_key, "
    "applied_version, reason"
)


def _row_to_dict(row: Any) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    return dict(row)


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def latest_proposal(proposal_id: str) -> Optional[Dict[str, Any]]:
    """Highest revision wins — that IS the current state of the proposal."""
    with db.cursor() as cur:
        cur.execute(
            f"SELECT {_PROPOSAL_COLS} FROM overlay_proposals "
            "WHERE proposal_id = %(pid)s ORDER BY revision DESC LIMIT 1",
            {"pid": proposal_id})
        return _row_to_dict(cur.fetchone())


def pending_for_session(tenant_id: str, session_id: str) -> Optional[Dict[str, Any]]:
    """The overlay this session should be PREVIEWING, if any.

    Filters on the lease in SQL so a lapsed proposal is invisible to the
    preview path immediately — the client stops showing it on the next read
    even if no revoke event was ever delivered and no sweeper has run.
    """
    with db.cursor() as cur:
        cur.execute(
            f"SELECT {_PROPOSAL_COLS} FROM overlay_proposals "
            "WHERE tenant_id = %(t)s AND session_id = %(s)s AND state = 'pending' "
            "  AND lease_expires_at > NOW() "
            "ORDER BY revision DESC LIMIT 1",
            {"t": tenant_id, "s": session_id})
        return _row_to_dict(cur.fetchone())


def document(tenant_id: str) -> Dict[str, Any]:
    """The live per-tenant overlay. Absent = version 0 with no tokens, so a
    tenant that has never had an overlay still has a CAS witness to quote."""
    with db.cursor() as cur:
        cur.execute(
            "SELECT tenant_id, version, tokens, updated_at, updated_by "
            "FROM overlay_documents WHERE tenant_id = %(t)s",
            {"t": tenant_id})
        row = _row_to_dict(cur.fetchone())
    if row is None:
        return {"tenant_id": tenant_id, "version": 0, "tokens": {},
                "updated_at": None, "updated_by": None}
    return row


def effective_tokens(tenant_id: str, session_id: Optional[str] = None) -> Dict[str, str]:
    """Resolution order: session preview -> tenant document. The committed
    platform defaults are applied by the RENDERER, not stored here, so a
    default change ships with the code rather than needing a data migration."""
    tokens: Dict[str, str] = dict(document(tenant_id).get("tokens") or {})
    if session_id:
        pending = pending_for_session(tenant_id, session_id)
        if pending:
            tokens.update(pending.get("tokens") or {})
    return tokens


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #
def create_proposal(
    *, proposal_id: str, tenant_id: str, session_id: str,
    tokens: Mapping[str, str], lease_s: int,
) -> Dict[str, Any]:
    """Insert revision 0. The partial unique index means a session with a live
    pending preview cannot open a second one — two pending overlays would make
    "what the user is looking at" ambiguous and leave revoke guessing."""
    with db.connection() as conn, conn.cursor() as cur:
        try:
            cur.execute(
                "INSERT INTO overlay_proposals "
                "  (proposal_id, revision, tenant_id, session_id, tokens, state, "
                "   lease_expires_at) "
                "VALUES (%(pid)s, 0, %(t)s, %(s)s, %(tok)s::jsonb, 'pending', "
                "        NOW() + make_interval(secs => %(lease)s)) "
                f"RETURNING {_PROPOSAL_COLS}",
                {"pid": proposal_id, "t": tenant_id, "s": session_id,
                 "tok": json.dumps(dict(tokens)), "lease": lease_s})
            row = _row_to_dict(cur.fetchone())
        except Exception as exc:  # noqa: BLE001 - unique violation is the expected case
            raise OverlayStoreError(
                "pending_proposal_exists", 409, str(exc)[:200]) from exc
        conn.commit()
    return row  # type: ignore[return-value]


def _insert_revision(cur: Any, current: Mapping[str, Any], **changes: Any) -> Dict[str, Any]:
    """Append the next revision. Never UPDATE a decided row: the previous
    revision stays readable, which is what makes the trail auditable."""
    nxt = dict(current)
    nxt.update(changes)
    cur.execute(
        "INSERT INTO overlay_proposals "
        "  (proposal_id, revision, tenant_id, session_id, tokens, state, "
        "   created_at, lease_expires_at, decided_at, decided_by, decision_key, "
        "   applied_version, reason) "
        "VALUES (%(pid)s, %(rev)s, %(t)s, %(s)s, %(tok)s::jsonb, %(st)s, "
        "        %(created)s, %(lease)s, NOW(), %(by)s, %(key)s, %(av)s, %(reason)s) "
        f"RETURNING {_PROPOSAL_COLS}",
        {"pid": current["proposal_id"], "rev": int(current["revision"]) + 1,
         "t": current["tenant_id"], "s": current["session_id"],
         "tok": json.dumps(dict(current["tokens"] or {})),
         "st": nxt["state"], "created": current["created_at"],
         "lease": current["lease_expires_at"], "by": nxt.get("decided_by"),
         "key": nxt.get("decision_key"), "av": nxt.get("applied_version"),
         "reason": nxt.get("reason")})
    return _row_to_dict(cur.fetchone())  # type: ignore[return-value]


def _audit(cur: Any, *, proposal: Mapping[str, Any], from_state: str,
           to_state: str, actor: Optional[str], decision_key: Optional[str],
           detail: Optional[Dict[str, Any]] = None) -> None:
    """Token COUNT only. Tenant copy must not reach logs or exports through the
    audit trail — the count is enough to reason about a decision."""
    cur.execute(
        "INSERT INTO overlay_audit "
        "  (proposal_id, tenant_id, from_state, to_state, actor, decision_key, "
        "   token_count, detail) "
        "VALUES (%(pid)s, %(t)s, %(f)s, %(to)s, %(a)s, %(k)s, %(n)s, %(d)s::jsonb)",
        {"pid": proposal["proposal_id"], "t": proposal["tenant_id"],
         "f": from_state, "to": to_state, "a": actor, "k": decision_key,
         "n": len(proposal.get("tokens") or {}),
         "d": json.dumps(detail or {})})


def approve(
    *, proposal_id: str, actor: str, decision_key: str, expected_version: int,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Approve and apply, atomically. Returns (proposal, document).

    ONE transaction covers the document CAS, the proposal revision and the
    audit row. Splitting them would let a crash leave a proposal advertising an
    applied_version the document never reached — which the revert path would
    then try to undo.
    """
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT {_PROPOSAL_COLS} FROM overlay_proposals "
            "WHERE proposal_id = %(pid)s ORDER BY revision DESC LIMIT 1 "
            "FOR UPDATE",
            {"pid": proposal_id})
        current = _row_to_dict(cur.fetchone())
        if current is None:
            raise OverlayStoreError("proposal_not_found", 404, proposal_id)

        if current["state"] != "pending":
            same_key = (current.get("decision_key") or "") == decision_key
            if same_key and current["state"] == "approved":
                doc = document(current["tenant_id"])
                return current, doc          # idempotent retry
            raise OverlayStoreError(
                "already_decided", 409, f"state={current['state']}")

        cur.execute("SELECT lease_expires_at <= NOW() AS lapsed "
                    "FROM overlay_proposals "
                    "WHERE proposal_id = %(pid)s AND revision = %(rev)s",
                    {"pid": proposal_id, "rev": current["revision"]})
        if bool(_row_to_dict(cur.fetchone())["lapsed"]):
            raise OverlayStoreError("lease_expired", 410, proposal_id)

        # Seed a document row so a first-ever overlay has a version to CAS on.
        cur.execute(
            "INSERT INTO overlay_documents (tenant_id, version, tokens) "
            "VALUES (%(t)s, 0, '{}'::jsonb) ON CONFLICT (tenant_id) DO NOTHING",
            {"t": current["tenant_id"]})

        # THE CAS. Zero rows updated = the operator decided against a version
        # they were not looking at.
        cur.execute(
            "UPDATE overlay_documents "
            "SET tokens = tokens || %(tok)s::jsonb, "
            "    version = version + 1, updated_at = NOW(), updated_by = %(by)s "
            "WHERE tenant_id = %(t)s AND version = %(ver)s "
            "RETURNING tenant_id, version, tokens, updated_at, updated_by",
            {"tok": json.dumps(dict(current["tokens"] or {})), "by": actor,
             "t": current["tenant_id"], "ver": int(expected_version)})
        doc = _row_to_dict(cur.fetchone())
        if doc is None:
            raise OverlayStoreError(
                "version_conflict", 409,
                f"card={expected_version} did not match the stored version")

        decided = _insert_revision(
            cur, current, state="approved", decided_by=actor,
            decision_key=decision_key, applied_version=doc["version"])
        _audit(cur, proposal=current, from_state="pending", to_state="approved",
               actor=actor, decision_key=decision_key,
               detail={"applied_version": doc["version"]})
        conn.commit()
    return decided, doc


def deny(*, proposal_id: str, actor: str, decision_key: str,
         reason: str = "") -> Dict[str, Any]:
    """Deny. Changes no tenant state, so no CAS — but it MUST be a recorded,
    authenticated transition, because `pending_for_session` stops returning the
    overlay the instant this lands. That is what pulls a rejected preview off
    the requester's screen without depending on a push event arriving."""
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT {_PROPOSAL_COLS} FROM overlay_proposals "
            "WHERE proposal_id = %(pid)s ORDER BY revision DESC LIMIT 1 FOR UPDATE",
            {"pid": proposal_id})
        current = _row_to_dict(cur.fetchone())
        if current is None:
            raise OverlayStoreError("proposal_not_found", 404, proposal_id)
        if current["state"] != "pending":
            same_key = (current.get("decision_key") or "") == decision_key
            if same_key and current["state"] == "denied":
                return current               # idempotent retry
            raise OverlayStoreError(
                "already_decided", 409, f"state={current['state']}")
        denied = _insert_revision(
            cur, current, state="denied", decided_by=actor,
            decision_key=decision_key, reason=reason or "denied")
        _audit(cur, proposal=current, from_state="pending", to_state="denied",
               actor=actor, decision_key=decision_key)
        conn.commit()
    return denied


def sweep_expired(limit: int = 100) -> List[Dict[str, Any]]:
    """Materialise lapsed leases. Purely bookkeeping: readers already treat a
    lapsed proposal as gone, so this exists to give the revoke-notification
    path something to fire on and to keep the queue honest. Safe to run late,
    safe to run twice."""
    swept: List[Dict[str, Any]] = []
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT {_PROPOSAL_COLS} FROM overlay_proposals p "
            "WHERE state = 'pending' AND lease_expires_at <= NOW() "
            "  AND revision = (SELECT MAX(revision) FROM overlay_proposals q "
            "                  WHERE q.proposal_id = p.proposal_id) "
            "ORDER BY lease_expires_at LIMIT %(lim)s FOR UPDATE SKIP LOCKED",
            {"lim": limit})
        for current in [dict(r) for r in cur.fetchall()]:
            expired = _insert_revision(
                cur, current, state="expired", reason="lease_expired")
            _audit(cur, proposal=current, from_state="pending",
                   to_state="expired", actor=None, decision_key=None)
            swept.append(expired)
        conn.commit()
    return swept


def revert(*, proposal_id: str, actor: str, decision_key: str,
           expected_version: int) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Undo an approved overlay by removing exactly the keys it introduced.

    Removing ITS keys rather than restoring a snapshot is deliberate: a later
    approval may have touched other tokens, and a snapshot restore would
    silently roll those back too.
    """
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT {_PROPOSAL_COLS} FROM overlay_proposals "
            "WHERE proposal_id = %(pid)s ORDER BY revision DESC LIMIT 1 FOR UPDATE",
            {"pid": proposal_id})
        current = _row_to_dict(cur.fetchone())
        if current is None:
            raise OverlayStoreError("proposal_not_found", 404, proposal_id)
        if current["state"] == "reverted":
            if (current.get("decision_key") or "") == decision_key:
                return current, document(current["tenant_id"])
            raise OverlayStoreError("already_decided", 409, "reverted")
        if current["state"] != "approved":
            raise OverlayStoreError("not_revertible", 409, current["state"])

        keys = list((current["tokens"] or {}).keys())
        cur.execute(
            "UPDATE overlay_documents "
            "SET tokens = tokens - %(keys)s::text[], version = version + 1, "
            "    updated_at = NOW(), updated_by = %(by)s "
            "WHERE tenant_id = %(t)s AND version = %(ver)s "
            "RETURNING tenant_id, version, tokens, updated_at, updated_by",
            {"keys": keys, "by": actor, "t": current["tenant_id"],
             "ver": int(expected_version)})
        doc = _row_to_dict(cur.fetchone())
        if doc is None:
            raise OverlayStoreError("version_conflict", 409, str(expected_version))

        reverted = _insert_revision(
            cur, current, state="reverted", decided_by=actor,
            decision_key=decision_key, applied_version=doc["version"],
            reason="reverted")
        _audit(cur, proposal=current, from_state="approved",
               to_state="reverted", actor=actor, decision_key=decision_key,
               detail={"removed_token_count": len(keys)})
        conn.commit()
    return reverted, doc
