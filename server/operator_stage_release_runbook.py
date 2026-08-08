"""Operator runbook: stage an immutable release candidate (contract/OPERATOR.md
section 4.1, operator.stage_release_candidate; O6, Wave 3; section 7 production
unreachability). Always-confirm, spend=usd, STAGING ONLY, ships dark.

Flow (split, always-confirm), mirroring external_write's AT-MOST-ONCE shape
because staging is an external side effect that cannot join a DB commit:
  propose  -> validate the exact source SHA and the staging-only target, verify
              the candidate is not already claimed, and mint a one-use authority
              (reserving usd spend at mint).
  execute  -> Phase 1 (ONE transaction, invariant 6): principal FOR UPDATE ->
              consume the one-use authority -> INSERT the IMMUTABLE candidate row
              (a second stage of the same source_sha+target is a no-op conflict
              -> already_staged) -> redemption audit -> commit. ONLY AFTER that
              commits, Phase 2 performs the staging deploy through the registered
              stager (DARK by default -> no_stager), records the previous ECS
              task-definition revision as the rollback target, and audits the
              outcome. Because the authority is spent once Phase 1 commits, an
              ambiguous deploy failure is NOT auto-retried into a duplicate
              staging change; the reversal is a staging rollback to the recorded
              previous revision.

PRODUCTION IS UNREACHABLE. `target` is pinned to the catalog's staging-only enum
and re-checked here, the candidate table CHECK-constrains target to
non-production, and the stager refuses any non-staging target. No production
route or credential is named anywhere on this path; production promotion is a
separate, unmounted transaction (contract section 7).
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

import operator_authority
import operator_principals
import operator_release_stager as stager
from operator_principals import _db
from psycopg.types.json import Jsonb

ACTION = "operator.stage_release_candidate"
_SHA_RE = re.compile(r"^[a-f0-9]{7,40}$")
_TARGETS = {"staging"}  # staging only; production is never a valid target here


class RunbookError(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _validate(source_sha: str, target: str) -> None:
    if not isinstance(source_sha, str) or not _SHA_RE.match(source_sha):
        raise RunbookError("source_sha_invalid")
    if target not in _TARGETS:
        raise RunbookError("target_not_staging")


def _args(source_sha: str, target: str) -> Dict[str, Any]:
    return {"source_sha": source_sha, "target": target}


def resolve_candidate(source_sha: str, target: str) -> Dict[str, Any]:
    """Server-owned read of the candidate's current state (None if never
    staged). Read-only; the authoritative immutability check is the phase-1
    INSERT."""
    _validate(source_sha, target)
    db = _db()
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, previous_taskdef_revision, staged_taskdef_revision"
            " FROM operator_release_candidates WHERE source_sha=%s AND target=%s",
            (source_sha, target))
        row = cur.fetchone()
    if row is None:
        return {"source_sha": source_sha, "target": target, "status": None}
    return {"source_sha": source_sha, "target": target,
            "status": row["status"],
            "previous_taskdef_revision": row["previous_taskdef_revision"],
            "staged_taskdef_revision": row["staged_taskdef_revision"]}


def propose(op, source_sha: str, target: str) -> Dict[str, Any]:
    _validate(source_sha, target)
    existing = resolve_candidate(source_sha, target)
    if existing["status"] is not None:
        raise RunbookError("already_staged")  # immutable candidate
    minted = operator_authority.mint(
        op.subject, op.role_revision, op.profile, op.environment,
        session_id=f"runbook:{op.subject}", turn_id=None, action=ACTION,
        args=_args(source_sha, target))
    return {"authority_id": minted["authority_id"], "action": ACTION,
            "source_sha": source_sha, "target": target,
            "expires_at": minted["expires_at"]}


def _audit_row(op, decision: str, reason: str, authority_id: str,
               extra: Optional[Dict[str, Any]] = None) -> bool:
    """Write ONE audit row in its OWN transaction. Returns True on success,
    False if the store could not be written. Never writes a secret."""
    try:
        db = _db()
        with db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO operator_security_audit (subject, action,"
                " decision, reason, authority_id, environment, extra)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (op.subject, ACTION, decision, reason, authority_id,
                 op.environment, Jsonb(extra or {})))
        return True
    except Exception:  # noqa: BLE001 - the caller decides what a failure means
        return False


def _audit_deny(op, reason: str, authority_id: str) -> None:
    _audit_row(op, "deny", reason, authority_id)


def _mark_candidate(source_sha: str, target: str, status: str,
                    previous: Optional[str] = None,
                    staged: Optional[str] = None) -> None:
    """Best-effort candidate-status update in its own transaction (phase 2)."""
    try:
        db = _db()
        with db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE operator_release_candidates SET status=%s,"
                " previous_taskdef_revision=COALESCE(%s, previous_taskdef_revision),"
                " staged_taskdef_revision=COALESCE(%s, staged_taskdef_revision),"
                " staged_at=CASE WHEN %s='staged' THEN now() ELSE staged_at END,"
                " updated_at=now() WHERE source_sha=%s AND target=%s",
                (status, previous, staged, status, source_sha, target))
    except Exception:  # noqa: BLE001 - best-effort
        pass


def execute(op, source_sha: str, target: str,
            authority_id: str) -> Dict[str, Any]:
    """AT-MOST-ONCE: consume the authority + claim the immutable candidate in ONE
    transaction, then perform the staging deploy after that commits. A dark
    stager fails closed BEFORE the authority is consumed."""
    _validate(source_sha, target)
    args = _args(source_sha, target)

    if operator_authority.kill_switch_active():
        _audit_deny(op, "kill_switch_active", authority_id)
        raise RunbookError("kill_switch_active")
    if not operator_principals.revalidate(op.subject, op.role_revision):
        _audit_deny(op, "principal_drift", authority_id)
        raise RunbookError("principal_drift")

    # A stager must be registered BEFORE the authority is consumed, so a dark
    # deployment fails closed (no_stager) WITHOUT spending the authority or
    # claiming the candidate.
    if not stager.has_stager():
        _audit_deny(op, "no_stager", authority_id)
        raise RunbookError("no_stager")

    # Phase 1 (atomic, invariant 6): consume + claim the immutable candidate +
    # redemption audit in ONE transaction.
    db = _db()
    try:
        with db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT status, role_revision FROM operator_principals"
                " WHERE subject = %s FOR UPDATE", (op.subject,))
            prow = cur.fetchone()
            if (prow is None or prow["status"] != "active"
                    or int(prow["role_revision"]) != int(op.role_revision)):
                raise RunbookError("principal_drift")
            operator_authority.consume_in_tx(
                cur, authority_id, op.subject, op.role_revision, op.environment,
                ACTION, args)
            # Claim the IMMUTABLE candidate. ON CONFLICT DO NOTHING: if the row
            # already exists (any status), no row returns -> already_staged, and
            # the whole tx (including the consume) rolls back.
            cur.execute(
                "INSERT INTO operator_release_candidates"
                " (source_sha, target, status) VALUES (%s, %s, 'staging')"
                " ON CONFLICT (source_sha, target) DO NOTHING"
                " RETURNING source_sha",
                (source_sha, target))
            if cur.fetchone() is None:
                raise RunbookError("already_staged")
            cur.execute(
                "INSERT INTO operator_security_audit (subject, action,"
                " decision, reason, authority_id, environment, extra)"
                " VALUES (%s, %s, 'execute', 'authority_consumed', %s, %s, %s)",
                (op.subject, ACTION, authority_id, op.environment,
                 Jsonb({"source_sha": source_sha, "target": target,
                        "approver": op.subject})))
            # clean exit: the candidate is claimed and the authority is SPENT.
    except (operator_authority.AuthorityDenied, RunbookError) as exc:
        _audit_deny(op, getattr(exc, "reason", "denied"), authority_id)
        raise

    # Phase 2 (post-commit, AT MOST ONCE): perform the staging deploy. The
    # authority is spent, so an ambiguous failure is not auto-retried.
    try:
        revs = stager.stage(source_sha, target)
    except stager.StageError as exc:
        _mark_candidate(source_sha, target, "failed")
        _audit_row(op, "execute", f"stage_failed:{exc.reason}", authority_id,
                   {"source_sha": source_sha, "target": target})
        raise RunbookError(exc.reason) from None

    previous = revs["previous_revision"]
    _mark_candidate(source_sha, target, "staged",
                    previous=previous, staged=revs["new_revision"])
    # The deploy SUCCEEDED (an external effect), so record the outcome and the
    # rollback target durably; a lost outcome audit is surfaced, not swallowed.
    if not _audit_row(op, "execute", "runbook_applied", authority_id,
                      {"source_sha": source_sha, "target": target,
                       "previous_taskdef_revision": previous,
                       "staged_taskdef_revision": revs["new_revision"],
                       "reversal": f"rollback_to_taskdef_revision:{previous}"}):
        raise RunbookError("outcome_audit_unavailable")
    return {"action": ACTION, "source_sha": source_sha, "target": target,
            "authority_id": authority_id,
            "staged_taskdef_revision": revs["new_revision"],
            "reversal": {"rollback_to_taskdef_revision": previous}}
