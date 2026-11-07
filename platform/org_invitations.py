"""Org invitation tokens stored hashed; accept is single-use and transactional;
never grants owner. Links expire after seven days and always grant editor.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import uuid

from . import db, store


class InvitationUnavailable(Exception):
    """Unknown, expired, revoked, consumed, or outside the caller's org."""


class InvitationForbidden(Exception):
    """Only an active owner can administer invitations."""


class InvitationConflict(Exception):
    """A pending limit or existing membership prevents this operation."""


def valid_token(token: object) -> bool:
    return isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is not None


def hash_token(token: str) -> str:
    if not valid_token(token):
        raise ValueError("invalid invitation token")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def validate_label(label):
    if label is not None and (
        not isinstance(label, str) or len(label) > 80 or (label != "" and not label.isprintable())
    ):
        raise ValueError("label must contain at most 80 printable characters")
    return label


def _owner(cur, org_id, binding_id):
    cur.execute(
        "SELECT role FROM identity_bindings WHERE platform_tenant_id = %s "
        "AND binding_id = %s AND status = 'active' FOR SHARE", (org_id, binding_id))
    row = cur.fetchone()
    if row is None or row["role"] != "owner":
        raise InvitationForbidden()


def _receipt(cur, invitation_id, org_id, event, actor):
    cur.execute(
        "INSERT INTO org_invitation_receipts "
        "(id, invitation_id, org_id, event, actor_binding_id) VALUES (%s, %s, %s, %s, %s)",
        (uuid.uuid4(), invitation_id, org_id, event, actor))


def create(org_id, actor_binding_id, label=None):
    label = validate_label(label)
    token = secrets.token_urlsafe(32)
    invitation_id = uuid.uuid4()
    with db.transaction() as conn, conn.cursor() as cur:
        # Serialize the count and insert even when the org has no invitations yet.
        cur.execute("SELECT org_id FROM orgs WHERE org_id = %s FOR UPDATE", (org_id,))
        if cur.fetchone() is None:
            raise InvitationUnavailable()
        _owner(cur, org_id, actor_binding_id)
        cur.execute(
            "SELECT COUNT(*) AS count FROM org_invitations WHERE org_id = %s "
            "AND accepted_at IS NULL AND revoked_at IS NULL AND expires_at > clock_timestamp()",
            (org_id,))
        if cur.fetchone()["count"] >= 20:
            raise InvitationConflict("too_many_pending")
        cur.execute(
            "INSERT INTO org_invitations "
            "(id, org_id, token_hash, role, label, created_by_binding_id, expires_at) "
            "VALUES (%s, %s, %s, 'editor', %s, %s, NOW() + INTERVAL '7 days') "
            "RETURNING expires_at",
            (invitation_id, org_id, hash_token(token), label, actor_binding_id))
        expires_at = cur.fetchone()["expires_at"]
        _receipt(cur, invitation_id, org_id, "created", actor_binding_id)
    return {"invitation_id": str(invitation_id), "expires_at": expires_at.isoformat(), "token": token}


def list_pending(org_id, actor_binding_id):
    with db.transaction() as conn, conn.cursor() as cur:
        _owner(cur, org_id, actor_binding_id)
        cur.execute(
            "SELECT id, label, created_at, expires_at FROM org_invitations WHERE org_id = %s "
            "AND accepted_at IS NULL AND revoked_at IS NULL AND expires_at > clock_timestamp() "
            "ORDER BY created_at, id", (org_id,))
        return [{"invitation_id": str(row["id"]), "label": row["label"],
                 "created_at": row["created_at"].isoformat(),
                 "expires_at": row["expires_at"].isoformat()} for row in cur.fetchall()]


def revoke(org_id, actor_binding_id, invitation_id):
    with db.transaction() as conn, conn.cursor() as cur:
        _owner(cur, org_id, actor_binding_id)
        cur.execute("SELECT accepted_at, revoked_at FROM org_invitations "
                    "WHERE org_id = %s AND id = %s FOR UPDATE", (org_id, invitation_id))
        row = cur.fetchone()
        if row is None:
            raise InvitationUnavailable()
        if row["accepted_at"] is None and row["revoked_at"] is None:
            cur.execute("UPDATE org_invitations SET revoked_at = NOW(), revoked_by_binding_id = %s "
                        "WHERE org_id = %s AND id = %s", (actor_binding_id, org_id, invitation_id))
            _receipt(cur, invitation_id, org_id, "revoked", actor_binding_id)


def accept(token, authority, subject):
    token_hash = hash_token(token)  # Shape is refused before hashing or opening the DB.
    with db.transaction() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, org_id, accepted_at, revoked_at, expires_at "
                    "FROM org_invitations WHERE token_hash = %s FOR UPDATE", (token_hash,))
        row = cur.fetchone()
        if row is None or row["accepted_at"] is not None or row["revoked_at"] is not None:
            raise InvitationUnavailable()
        cur.execute("SELECT clock_timestamp() < %s AS available", (row["expires_at"],))
        if not cur.fetchone()["available"]:
            raise InvitationUnavailable()
        try:
            binding = store._create_identity_binding_on_cursor(
                cur, row["org_id"], authority, subject, role="editor", require_new=True)
        except ValueError as exc:
            if str(exc) == "already_member":
                raise InvitationConflict("already_member") from None
            raise
        cur.execute("UPDATE org_invitations SET accepted_at = NOW(), accepted_binding_id = %s "
                    "WHERE id = %s", (binding.binding_id, row["id"]))
        _receipt(cur, row["id"], row["org_id"], "accepted", binding.binding_id)
    return {"org_id": str(row["org_id"]), "binding_id": str(binding.binding_id), "role": "editor"}
