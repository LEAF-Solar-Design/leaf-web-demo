"""Project access grants with immediate revocation and append-only audit events."""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from .db import connection
from .models import new_uuid
from .store import _insert_outbox

_ROLES = {"reviewer", "read_only"}


def _now(value: Optional[datetime]) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("access timestamps must be timezone-aware")
    return current


def _digest(token: str) -> str:
    if not isinstance(token, str) or len(token) < 32:
        raise ValueError("share token is malformed")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _record(row: Any) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    out = dict(row)
    for key, value in tuple(out.items()):
        if isinstance(value, (uuid.UUID, datetime)):
            out[key] = str(value) if isinstance(value, uuid.UUID) else value.isoformat()
    return out


def _require_grant_admin(cur: Any, org_id: uuid.UUID, project_id: uuid.UUID,
                         binding_id: uuid.UUID) -> None:
    cur.execute(
        "SELECT 1 FROM projects p JOIN identity_bindings b "
        "ON b.platform_tenant_id = p.org_id "
        "WHERE p.org_id = %(org)s AND p.project_id = %(project)s "
        "AND p.deleted_at IS NULL AND p.status = 'active' "
        "AND b.binding_id = %(binding)s AND b.status = 'active' "
        "AND b.role IN ('owner', 'editor')",
        {"org": org_id, "project": project_id, "binding": binding_id},
    )
    if cur.fetchone() is None:
        raise PermissionError("active owner or editor binding is required")


def create_project_share_grant(org_id: uuid.UUID, project_id: uuid.UUID,
                               created_by_binding_id: uuid.UUID, *,
                               role: str = "reviewer", ttl_seconds: int = 86400,
                               now: Optional[datetime] = None) -> Dict[str, Any]:
    if role not in _ROLES:
        raise ValueError("share role must be reviewer or read_only")
    if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool) \
            or ttl_seconds < 60 or ttl_seconds > 30 * 86400:
        raise ValueError("share ttl must be an integer from 60 seconds through 30 days")
    current = _now(now)
    expires = current + timedelta(seconds=ttl_seconds)
    token = secrets.token_urlsafe(32)
    grant_id = new_uuid()
    with connection() as conn:
        with conn.cursor() as cur:
            _require_grant_admin(cur, org_id, project_id, created_by_binding_id)
            cur.execute(
                "INSERT INTO project_share_grants "
                "(grant_id, org_id, project_id, token_digest, role, created_by_binding_id, "
                "created_at, expires_at) VALUES (%(grant)s, %(org)s, %(project)s, %(digest)s, "
                "%(role)s, %(binding)s, %(created)s, %(expires)s) "
                "RETURNING grant_id, org_id, project_id, role, status, created_at, expires_at, revoked_at",
                {"grant": grant_id, "org": org_id, "project": project_id,
                 "digest": _digest(token), "role": role, "binding": created_by_binding_id,
                 "created": current, "expires": expires},
            )
            row = cur.fetchone()
            _insert_outbox(cur, org_id, project_id, "share_grant", grant_id,
                           "share.grant.created",
                           {"grantId": str(grant_id), "role": role,
                            "expiresAt": expires.isoformat()})
    return {**_record(row), "token": token}


def resolve_project_share_token(token: str, *,
                                now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    current = _now(now)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT g.grant_id, g.org_id, g.project_id, g.role, g.status, "
                "g.created_at, g.expires_at, g.revoked_at FROM project_share_grants g "
                "JOIN projects p ON p.org_id = g.org_id AND p.project_id = g.project_id "
                "WHERE g.token_digest = %(digest)s AND g.status = 'active' "
                "AND g.expires_at > %(now)s AND p.deleted_at IS NULL AND p.status = 'active'",
                {"digest": _digest(token), "now": current},
            )
            return _record(cur.fetchone())


def revoke_project_share_grant(org_id: uuid.UUID, project_id: uuid.UUID,
                               grant_id: uuid.UUID, revoked_by_binding_id: uuid.UUID, *,
                               now: Optional[datetime] = None) -> str:
    current = _now(now)
    with connection() as conn:
        with conn.cursor() as cur:
            _require_grant_admin(cur, org_id, project_id, revoked_by_binding_id)
            cur.execute(
                "SELECT status FROM project_share_grants WHERE grant_id = %(grant)s "
                "AND org_id = %(org)s AND project_id = %(project)s FOR UPDATE",
                {"grant": grant_id, "org": org_id, "project": project_id},
            )
            row = cur.fetchone()
            if row is None:
                return "missing"
            if row["status"] == "revoked":
                return "duplicate"
            cur.execute(
                "UPDATE project_share_grants SET status = 'revoked', revoked_at = %(now)s "
                "WHERE grant_id = %(grant)s AND status = 'active'",
                {"grant": grant_id, "now": current},
            )
            _insert_outbox(cur, org_id, project_id, "share_grant", grant_id,
                           "share.grant.revoked",
                           {"grantId": str(grant_id),
                            "revokedByBindingId": str(revoked_by_binding_id),
                            "revokedAt": current.isoformat()})
            return "revoked"

