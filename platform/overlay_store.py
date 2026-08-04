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
import re
import unicodedata
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
    "applied_version, reason, superseded_at"
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
            "  AND superseded_at IS NULL "
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
#: A token id, e.g. `color.canvas.bg`. Mirrors overlay_stream._TOKEN_ID.
_TOKEN_ID = re.compile(r"^[a-z][a-z0-9]*(\.[a-z][a-z0-9]*)+$")

#: Substrings that let a value escape a CSS custom property or a copy slot.
#: A denylist is a weak instrument, which is exactly why it is NOT the only
#: guard: overlay_registry does allowlist validation upstream, and the card
#: refuses to put anything but a canonical #rrggbb into a style sink. This is
#: the storage boundary's own check, so an unvalidated caller cannot persist a
#: hostile value at all.
_UNSAFE_IN_VALUE = ("url(", "expression(", "var(", "/*", "*/", "\\", ";",
                    "{", "}", "<", ">", "@import", "\5c")


#: Bidi controls, zero-width and other invisible code points. Mirrors the
#: registry's rule so the two cannot disagree about what is deceptive.
_INVISIBLE = frozenset(
    [0x00AD, 0x034F, 0x061C, 0x2060, 0x3164, 0xFEFF]
    + list(range(0x200B, 0x2010))   # ZWSP..RLM
    + list(range(0x202A, 0x202F))   # bidi embedding/override
    + list(range(0x2066, 0x206A))   # bidi isolates
)


def _is_invisible(ch: str) -> bool:
    """Is this character invisible or direction-altering to a human reader?

    CLASSIFIED, not enumerated. The hand-written set missed U+2061 (which the
    registry rejected, so the two layers disagreed) and U+115F HANGUL CHOSEONG
    FILLER (which both accepted, and which the card does not expose either).
    Every hand-maintained list of invisible code points is a list that is out
    of date; `unicodedata.category` is maintained by someone else.

    Cf  format characters, which covers the bidi controls and U+2061.
    Cc  controls.  Cs/Co  surrogates and private use.
    Zl/Zp  line and paragraph separators.
    Plus the width-less fillers Unicode classifies as ordinary letters (Lo),
    which no category catches and which render as nothing.
    """
    if ch in _INVISIBLE_LETTERS:
        return True
    return unicodedata.category(ch) in ("Cf", "Cc", "Cs", "Co", "Zl", "Zp")


#: Invisible code points Unicode classifies as LETTERS, so no category check
#: finds them. They render at zero width and are pure deception in copy.
_INVISIBLE_LETTERS = frozenset("ᅟᅠㅤﾠ")


def _reject_unvalidated(tokens: Mapping[str, str]) -> None:
    """Refuse anything that is not already canonical.

    A review reached this function with
    `{"color.canvas.bg": "url(https://attacker.example/track)"}` and it was
    stored verbatim, then rendered into the operator's browser as a background
    — which issued the request. Validation upstream is the primary guard; this
    makes the STORE itself impossible to use wrongly, because a boundary that
    trusts its caller is only as safe as every caller ever added.
    """
    if not tokens:
        raise OverlayStoreError("empty_proposal", 400, "no tokens")
    for tid, value in tokens.items():
        if not _TOKEN_ID.match(str(tid)):
            raise OverlayStoreError("bad_token_id", 400, str(tid)[:80])
        v = str(value)
        if len(v) > 400:
            raise OverlayStoreError("token_value_too_long", 400, str(tid))
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in v):
            raise OverlayStoreError("control_char_in_value", 400, str(tid))
        # Bidi and invisible characters. The registry rejects these upstream
        # and the store did not, so `"Save‮eleteD"` could be persisted:
        # copy that reads as one thing and renders as another. A boundary that
        # is weaker than its own validator is not a boundary.
        if any(_is_invisible(ch) for ch in v):
            raise OverlayStoreError("deceptive_char_in_value", 400, str(tid))
        low = v.lower()
        for bad in _UNSAFE_IN_VALUE:
            if bad in low:
                raise OverlayStoreError("unsafe_token_value", 400,
                                        f"{tid}: {bad!r}")
        if str(tid).startswith("color.") and not re.match(
                r"^#[0-9a-fA-F]{6}$", v):
            raise OverlayStoreError("bad_color_value", 400, str(tid))


def create_proposal(
    *, proposal_id: str, tenant_id: str, session_id: str,
    tokens: Mapping[str, str], lease_s: int,
) -> Dict[str, Any]:
    """Insert revision 0. The partial unique index means a session with a live
    pending preview cannot open a second one — two pending overlays would make
    "what the user is looking at" ambiguous and leave revoke guessing."""
    _reject_unvalidated(tokens)
    with db.connection() as conn, conn.cursor() as cur:
        # Retire any LAPSED pending proposal for this session before inserting.
        #
        # The partial unique index cannot express "and not expired" — a index
        # predicate may not call NOW(). So an expired-but-unswept row still
        # occupies the session's one pending slot, and the next proposal fails
        # with pending_proposal_exists even though the preview correctly shows
        # nothing. The user sees the feature simply stop working, forever, with
        # no way to clear it. Retiring in THIS transaction means the slot is
        # free by the time the insert runs, without depending on a sweeper.
        #
        # It stamps `superseded_at` ONLY and does not rewrite `state`. Setting
        # state='expired' here would have been a decision-content rewrite, and
        # the append-only invariant is worth more than the tidier-looking row:
        # the lease already makes this proposal expired to every reader, and
        # the stamp is what actually frees the index slot.
        #
        # It also APPENDS an 'expired' revision rather than only stamping.
        # Stamping alone freed the slot but suppressed the expiry forever:
        # sweep_expired skips superseded rows, so no pending->expired revision
        # was ever written, latest_proposal still read 'pending', and anything
        # publishing revokes from the sweep never learned the overlay lapsed.
        # A slot freed silently is a state change nobody can observe.
        cur.execute(
            "WITH lapsed AS ("
            "  SELECT proposal_id, MAX(revision) AS rev FROM overlay_proposals "
            "  WHERE tenant_id = %(t)s AND session_id = %(s)s "
            "    AND state = 'pending' AND superseded_at IS NULL "
            "    AND lease_expires_at <= NOW() GROUP BY proposal_id), "
            "stamped AS ("
            "  UPDATE overlay_proposals p SET superseded_at = NOW() "
            "  FROM lapsed l "
            "  WHERE p.proposal_id = l.proposal_id AND p.revision = l.rev "
            "  RETURNING p.proposal_id, p.revision, p.tenant_id, p.session_id, "
            "            p.tokens, p.created_at, p.lease_expires_at) "
            "INSERT INTO overlay_proposals "
            "  (proposal_id, revision, tenant_id, session_id, tokens, state, "
            "   created_at, lease_expires_at, decided_at, reason) "
            "SELECT proposal_id, revision + 1, tenant_id, session_id, tokens, "
            "       'expired', created_at, lease_expires_at, NOW(), 'lease lapsed' "
            "FROM stamped",
            {"t": tenant_id, "s": session_id})
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
    """Append the next revision and mark the one it replaces as superseded.

    The stamp is not bookkeeping, it is the fix for a permanent session
    deadlock. Appending alone left the previous revision still reading
    `state = 'pending'`, so the partial unique index blocked every future
    proposal for that session and the preview kept serving a decided overlay.

    Decision content stays immutable — state, actor, decision_key and tokens on
    the old row are never rewritten, so the trail is still auditable. Only
    `superseded_at` is set, and it records ordering rather than any decision.
    """
    nxt = dict(current)
    nxt.update(changes)
    cur.execute(
        "UPDATE overlay_proposals SET superseded_at = NOW() "
        "WHERE proposal_id = %(pid)s AND revision = %(rev)s "
        "  AND superseded_at IS NULL",
        {"pid": current["proposal_id"], "rev": current["revision"]})
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


def _document_on(cur: Any, tenant_id: str) -> Dict[str, Any]:
    """Read the tenant document on an ALREADY-HELD cursor.

    Calling the module-level `document()` from inside a `with db.connection()`
    block requests a SECOND connection while the first is still held. A review
    found that with a five-connection pool, five concurrent approval retries
    each hold one connection, four wait on the proposal anchor, and the holder
    then asks for a sixth — so nothing can progress until the pool times out.
    A read that reuses the caller's cursor cannot deadlock the pool.
    """
    cur.execute(
        "SELECT tenant_id, version, tokens, updated_at, updated_by "
        "FROM overlay_documents WHERE tenant_id = %(t)s", {"t": tenant_id})
    row = _row_to_dict(cur.fetchone())
    if row is None:
        return {"tenant_id": tenant_id, "version": 0, "tokens": {},
                "updated_at": None, "updated_by": None}
    return row


def _lock_latest(cur: Any, proposal_id: str,
                 tenant_id: str) -> Optional[Dict[str, Any]]:
    """Serialize deciders on one proposal, then read the revision that is
    CURRENT once the lock is held.

    SCOPED BY TENANT, and that scoping is a security boundary, not a
    convenience: without it a caller who merely KNOWS another tenant's
    proposal id could approve, deny or revert it, and the mutation would land
    on that tenant's document (the code read the tenant off the PROPOSAL row,
    never off the caller). A foreign id now reads exactly like a missing one,
    so the caller cannot probe for existence either (sol-critic PR #439
    round 2, MAJOR).

    This is two statements for a reason a live-Postgres test had to teach us.
    The obvious one-liner —

        SELECT ... WHERE proposal_id = %s ORDER BY revision DESC LIMIT 1 FOR UPDATE

    — reads correctly and does NOT serialize. Under READ COMMITTED the
    ORDER BY/LIMIT is evaluated against the statement's snapshot BEFORE it
    waits on the row lock, so a decider that blocks behind another one wakes up
    and returns the row it had already chosen. That row is by then a superseded
    revision, still saying `pending`, and the second operator walks straight
    past the `state != 'pending'` guard.

    Confirmed against PostgreSQL 16, not reasoned about: with the single
    statement the second decider observes `pending` after the first committed
    `approved`; with the two below it observes `approved`.

    So:
      1. Lock the LOWEST revision, which is a stable anchor — revisions are
         only ever appended, so this row exists for the life of the proposal
         and never moves. Any other decider blocks here.
      2. Re-read the latest revision as a SEPARATE statement. READ COMMITTED
         gives each statement a fresh snapshot, so this one sees whatever the
         decider ahead of us committed.

    The CAS in approve() would still refuse the second write, so this is not
    the only thing standing between us and a lost update. It is what makes the
    refusal an honest `already_decided` instead of a primary-key violation on
    a revision number that was taken while we waited.
    """
    cur.execute(
        "SELECT proposal_id FROM overlay_proposals "
        "WHERE proposal_id = %(pid)s AND tenant_id = %(tid)s "
        "  AND revision = (SELECT MIN(revision) FROM overlay_proposals "
        "                  WHERE proposal_id = %(pid)s AND tenant_id = %(tid)s) "
        "FOR UPDATE",
        {"pid": proposal_id, "tid": tenant_id})
    if cur.fetchone() is None:
        return None
    cur.execute(
        f"SELECT {_PROPOSAL_COLS} FROM overlay_proposals "
        "WHERE proposal_id = %(pid)s AND tenant_id = %(tid)s "
        "ORDER BY revision DESC LIMIT 1",
        {"pid": proposal_id, "tid": tenant_id})
    return _row_to_dict(cur.fetchone())


def approve(
    *, proposal_id: str, tenant_id: str, actor: str, decision_key: str,
    expected_version: int,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Approve and apply, atomically. Returns (proposal, document).

    ONE transaction covers the document CAS, the proposal revision and the
    audit row. Splitting them would let a crash leave a proposal advertising an
    applied_version the document never reached — which the revert path would
    then try to undo.
    """
    with db.connection() as conn, conn.cursor() as cur:
        current = _lock_latest(cur, proposal_id, tenant_id)
        if current is None:
            raise OverlayStoreError("proposal_not_found", 404, proposal_id)

        if current["state"] != "pending":
            same_key = (current.get("decision_key") or "") == decision_key
            if same_key and current["state"] == "approved":
                doc = _document_on(cur, current["tenant_id"])
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


def deny(*, proposal_id: str, tenant_id: str, actor: str, decision_key: str,
         expected_version: int, reason: str = "") -> Dict[str, Any]:
    """Deny. Recorded and authenticated, because `pending_for_session` stops
    returning the overlay the instant this lands — that is what pulls a
    rejected preview off the requester's screen without depending on a push
    event arriving.

    `expected_version` is REQUIRED even though a denial mutates no tenant
    state. A review found that without it a card rendered against document
    version 1 could still record its denial after the tenant had moved to
    version 2, so the operator's "no" was cast against a screen that no longer
    described reality. Approve refused that and deny accepted it, which is the
    worst possible split: the safe-looking button was the unguarded one.

    Deny does not CAS, because it writes nothing to the document. It compares,
    and refuses a mismatch.
    """
    with db.connection() as conn, conn.cursor() as cur:
        current = _lock_latest(cur, proposal_id, tenant_id)
        if current is None:
            raise OverlayStoreError("proposal_not_found", 404, proposal_id)
        if current["state"] != "pending":
            same_key = (current.get("decision_key") or "") == decision_key
            if same_key and current["state"] == "denied":
                return current               # idempotent retry
            raise OverlayStoreError(
                "already_decided", 409, f"state={current['state']}")

        # FOR UPDATE, not a bare read. Without the lock another approval can
        # move the document between this comparison and the commit, so the
        # stale denial the check exists to refuse still lands.
        # Seed the row BEFORE locking it. `FOR UPDATE` over zero rows locks
        # nothing, so a first-ever denial and a first-ever approval could both
        # proceed: the denial read version 0 from an absent row while the
        # approval inserted the document and moved it to 1. Approve already
        # seeds for exactly this reason; deny has to as well.
        cur.execute(
            "INSERT INTO overlay_documents (tenant_id, version, tokens) "
            "VALUES (%(t)s, 0, '{}'::jsonb) ON CONFLICT (tenant_id) DO NOTHING",
            {"t": current["tenant_id"]})
        cur.execute(
            "SELECT version FROM overlay_documents WHERE tenant_id = %(t)s "
            "FOR UPDATE", {"t": current["tenant_id"]})
        seen = _row_to_dict(cur.fetchone())["version"]
        if int(seen) != int(expected_version):
            raise OverlayStoreError(
                "version_conflict", 409,
                f"card={expected_version} but tenant is at {seen}")

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
            "WHERE state = 'pending' AND superseded_at IS NULL "
            "  AND lease_expires_at <= NOW() "
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


def _removed_count(before: Mapping[str, Any], doc: Mapping[str, Any]) -> int:
    """How many keys the revert ACTUALLY removed.

    Reporting the proposal's token count instead was wrong once removal became
    conditional: a proposal whose value a later approval overwrote removes
    nothing, and an audit claiming it removed one is a false record of a
    runtime mutation.
    """
    after = dict(doc.get("tokens") or {})
    return sum(1 for k in before if k not in after)


def revert(*, proposal_id: str, tenant_id: str, actor: str,
           decision_key: str,
           expected_version: int) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Undo an approved overlay by removing exactly the keys it introduced.

    Removing ITS keys rather than restoring a snapshot is deliberate: a later
    approval may have touched other tokens, and a snapshot restore would
    silently roll those back too.
    """
    with db.connection() as conn, conn.cursor() as cur:
        current = _lock_latest(cur, proposal_id, tenant_id)
        if current is None:
            raise OverlayStoreError("proposal_not_found", 404, proposal_id)
        if current["state"] == "reverted":
            if (current.get("decision_key") or "") == decision_key:
                return current, _document_on(cur, current["tenant_id"])
            raise OverlayStoreError("already_decided", 409, "reverted")
        if current["state"] != "approved":
            raise OverlayStoreError("not_revertible", 409, current["state"])

        # Remove ONLY the keys still holding the value THIS proposal applied.
        # Removing every key it named would delete a value a LATER approval
        # set: approve A (bg=#111), approve B (bg=#222), revert A, and a blind
        # `tokens - keys` drops B's #222 entirely. The operator reverting A
        # would have silently undone B's decision.
        # The pre-update document. A post-update snapshot cannot tell how many
        # keys the update removed: a key already absent beforehand looks
        # identical to one this revert removed, so the audit claimed a runtime
        # mutation that never happened.
        before = dict(_document_on(cur, current["tenant_id"]).get("tokens") or {})
        mine = json.dumps(dict(current["tokens"] or {}))
        cur.execute(
            "UPDATE overlay_documents d "
            "SET tokens = d.tokens - ("
            "      SELECT COALESCE(array_agg(t.k), ARRAY[]::text[]) "
            "      FROM jsonb_each_text(%(mine)s::jsonb) AS t(k, v) "
            "      WHERE d.tokens ->> t.k IS NOT DISTINCT FROM t.v), "
            "    version = version + 1, updated_at = NOW(), updated_by = %(by)s "
            "WHERE d.tenant_id = %(t)s AND d.version = %(ver)s "
            "RETURNING tenant_id, version, tokens, updated_at, updated_by",
            {"mine": mine, "by": actor, "t": current["tenant_id"],
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
               detail={"removed_token_count": _removed_count(before, doc)})
        conn.commit()
    return reverted, doc
