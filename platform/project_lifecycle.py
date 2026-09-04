"""Durable, tenant-scoped browser-project lifecycle operations.

The generic project API remains backward compatible.  This module adds the
Malleable workspace lifecycle: blank project creation, project membership,
browser-owned files, clone/export/reset/delete, and sanitized action receipts.
Every operation derives tenant and actor identity at the API boundary, repeats
the binding checks inside one SERIALIZABLE transaction, and scopes every query
by both organization and project.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import PurePosixPath
from typing import Any, Dict, Optional

from psycopg.types.json import Jsonb

from .db import run_transaction
from .models import new_uuid


PROJECT_ROLES = frozenset({"owner", "editor", "reviewer", "read_only"})
WRITE_ROLES = frozenset({"owner", "editor"})
MAX_FILE_BYTES = 1_048_576
_RECEIPT_SECRET_KEYS = frozenset({
    "authorization", "password", "secret", "token", "api_key", "apikey",
    "access_token", "refresh_token", "oauth_token", "client_secret", "private_key",
    "cookie", "set_cookie",
})
_JWT_SHAPE = re.compile(
    r"^eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}$",
)


class LifecycleUnavailable(LookupError):
    """The tenant-scoped resource is absent or not visible to this actor."""


class LifecycleForbidden(PermissionError):
    """The verified actor lacks the required project role."""


class LifecycleConflict(RuntimeError):
    """An idempotency key or concurrent lifecycle state conflicts."""


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _assert_sanitized_receipt(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _RECEIPT_SECRET_KEYS:
                raise ValueError("lifecycle receipt contains a credential-shaped field")
            _assert_sanitized_receipt(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _assert_sanitized_receipt(item)
        return
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.lower().startswith("bearer ") or _JWT_SHAPE.fullmatch(stripped):
            raise ValueError("lifecycle receipt contains a credential-shaped value")


def _validate_idempotency_key(value: str) -> str:
    value = (value or "").strip()
    if not value or len(value) > 200:
        raise ValueError("idempotency key must contain 1 to 200 characters")
    if value.lower().startswith("bearer ") or _JWT_SHAPE.fullmatch(value):
        raise ValueError("idempotency key must not contain credential material")
    return value


def _validate_project_name(value: str) -> str:
    value = (value or "").strip()
    if not value or len(value) > 200:
        raise ValueError("project name must contain 1 to 200 characters")
    return value


def _validate_path(value: str) -> str:
    value = (value or "").strip()
    if not value or len(value) > 512 or "\x00" in value or "\\" in value:
        raise ValueError("file path must be a safe relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("file path must be a safe relative POSIX path")
    normalized = path.as_posix()
    if normalized != value:
        raise ValueError("file path must already be canonical")
    return normalized


def _validate_media_type(value: str) -> str:
    value = (value or "").strip().lower()
    if not value or len(value) > 200 or "/" not in value:
        raise ValueError("media type must be a valid type/subtype value")
    return value


def _validate_content(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("project file content must be text")
    if len(value.encode("utf-8")) > MAX_FILE_BYTES:
        raise ValueError("project file content exceeds the 1 MiB browser limit")
    return value


def _validate_role(value: str) -> str:
    if value not in PROJECT_ROLES:
        raise ValueError("role must be owner, editor, reviewer, or read_only")
    return value


def _actor_tenant_role(
    cur: Any, org_id: uuid.UUID, actor_binding_id: uuid.UUID, *, lock: bool = True,
) -> str:
    lock_clause = "FOR SHARE" if lock else ""
    cur.execute(
        "SELECT role FROM identity_bindings "
        "WHERE platform_tenant_id = %(org_id)s AND binding_id = %(binding_id)s "
        "AND status = 'active' " + lock_clause,
        {"org_id": org_id, "binding_id": actor_binding_id},
    )
    row = cur.fetchone()
    if row is None:
        raise LifecycleForbidden("actor has no active tenant identity binding")
    return str(row["role"])


def _project_row(
    cur: Any, org_id: uuid.UUID, project_id: uuid.UUID, *, lock: bool = True,
) -> Dict[str, Any]:
    lock_clause = "FOR UPDATE" if lock else ""
    # THE gate for every lifecycle operation (clone, export, reset, delete, file
    # writes), so its liveness predicate has to cover BOTH soft-delete writers.
    # `status <> 'deleted'` alone matched a project that store.soft_delete_project
    # had hidden (it sets deleted_at and leaves status 'active'), which made an
    # invisible project fully mutable through this path. live_projects is that
    # predicate, named once.
    cur.execute(
        "SELECT project_id, org_id, name, status, created_at, updated_at "
        "FROM live_projects WHERE org_id = %(org_id)s AND project_id = %(project_id)s "
        + lock_clause,
        {"org_id": org_id, "project_id": project_id},
    )
    row = cur.fetchone()
    if row is None:
        raise LifecycleUnavailable("project not found")
    return row


def _project_membership_role(
    cur: Any, org_id: uuid.UUID, project_id: uuid.UUID,
    actor_binding_id: uuid.UUID,
) -> Optional[str]:
    cur.execute(
        "SELECT role FROM project_member_bindings "
        "WHERE org_id = %(org_id)s AND project_id = %(project_id)s "
        "AND binding_id = %(binding_id)s AND status = 'active'",
        {
            "org_id": org_id,
            "project_id": project_id,
            "binding_id": actor_binding_id,
        },
    )
    row = cur.fetchone()
    return str(row["role"]) if row else None


def _require_project_role(
    cur: Any, org_id: uuid.UUID, project_id: uuid.UUID,
    actor_binding_id: uuid.UUID, *, write: bool, lock: bool = True,
) -> str:
    # Tenant identity proves who the actor is. It never grants authority over
    # every project in that tenant. Named-project access comes only from the
    # active project membership so revocation takes effect on the next request.
    _actor_tenant_role(cur, org_id, actor_binding_id, lock=lock)
    member_role = _project_membership_role(
        cur, org_id, project_id, actor_binding_id,
    )
    allowed = WRITE_ROLES if write else PROJECT_ROLES
    if member_role in allowed:
        return str(member_role)
    raise LifecycleForbidden(
        "project role does not permit mutation" if write
        else "project role does not permit access"
    )


#: Bound for the one membership re-read below. It runs once per project per
#: request (review finding 4: previously up to LIST_MAX_LIMIT times on the
#: session list page before that route memoized the binding), and every
#: caller of this seam -- conversation, approval, checkpoint, execution, and
#: session routes -- gets the same fail-closed bound rather than each
#: inventing its own.
_PROJECT_ROLE_STATEMENT_TIMEOUT_MS = 3000


def require_project_role(
    org_id: uuid.UUID, project_id: uuid.UUID, actor_binding_id: uuid.UUID, *,
    write: bool,
) -> str:
    """Re-read one current project membership at a request boundary.

    Conversation, approval, checkpoint, and execution routes use this public
    seam instead of copying lifecycle SQL.  The project row and both identity
    layers are checked inside one transaction, so a revoked member cannot keep
    using a session merely because its durable row predates the revocation.
    """
    def operation(conn: Any) -> str:
        with conn.cursor() as cur:
            cur.execute(
                f"SET LOCAL statement_timeout = {int(_PROJECT_ROLE_STATEMENT_TIMEOUT_MS)}"
            )
            _project_row(cur, org_id, project_id, lock=False)
            return _require_project_role(
                cur, org_id, project_id, actor_binding_id,
                write=write, lock=False,
            )

    return run_transaction(operation, isolation="repeatable read")


def _target_binding_role(cur: Any, org_id: uuid.UUID, binding_id: uuid.UUID) -> str:
    cur.execute(
        "SELECT role FROM identity_bindings "
        "WHERE platform_tenant_id = %(org_id)s AND binding_id = %(binding_id)s "
        "AND status = 'active' FOR SHARE",
        {"org_id": org_id, "binding_id": binding_id},
    )
    row = cur.fetchone()
    if row is None:
        raise LifecycleUnavailable("member identity binding not found")
    return str(row["role"])


def _receipt_dict(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "receipt_id": str(row["receipt_id"]),
        "project_id": str(row["project_id"]),
        "action": row["action"],
        "input_digest": row["input_digest"],
        "created_at": row["created_at"].isoformat(),
    }


def _lock_idempotency(
    cur: Any, org_id: uuid.UUID, action: str, idempotency_key: str,
) -> None:
    """Serialize a receipt key before a server-minted project ID exists."""
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%(lock_key)s, 0))",
        {"lock_key": f"leaf-project-lifecycle:{org_id}:{action}:{idempotency_key}"},
    )


def _receipt_replay(
    cur: Any, org_id: uuid.UUID, project_id: Optional[uuid.UUID], action: str,
    idempotency_key: str, input_digest: str,
) -> Optional[Dict[str, Any]]:
    project_clause = "" if project_id is None else "AND project_id = %(project_id)s "
    cur.execute(
        "SELECT receipt_id, project_id, action, input_digest, result_json, created_at "
        "FROM project_lifecycle_receipts "
        "WHERE org_id = %(org_id)s AND action = %(action)s "
        "AND idempotency_key = %(idempotency_key)s " + project_clause + "FOR SHARE",
        {
            "org_id": org_id,
            "project_id": project_id,
            "action": action,
            "idempotency_key": idempotency_key,
        },
    )
    row = cur.fetchone()
    if row is None:
        return None
    if row["input_digest"] != input_digest:
        raise LifecycleConflict("idempotency key is already bound to another request")
    result = dict(row["result_json"])
    result["receipt"] = _receipt_dict(row)
    result["replayed"] = True
    return result


def _write_receipt(
    cur: Any, org_id: uuid.UUID, project_id: uuid.UUID,
    actor_binding_id: uuid.UUID, action: str, idempotency_key: str,
    input_digest: str, result: Dict[str, Any],
) -> Dict[str, Any]:
    _assert_sanitized_receipt(result)
    cur.execute(
        "INSERT INTO project_lifecycle_receipts "
        "(receipt_id, org_id, project_id, action, actor_binding_id, "
        " idempotency_key, input_digest, result_json) "
        "VALUES (%(receipt_id)s, %(org_id)s, %(project_id)s, %(action)s, "
        "%(actor_binding_id)s, %(idempotency_key)s, %(input_digest)s, %(result_json)s) "
        "RETURNING receipt_id, project_id, action, input_digest, result_json, created_at",
        {
            "receipt_id": new_uuid(),
            "org_id": org_id,
            "project_id": project_id,
            "action": action,
            "actor_binding_id": actor_binding_id,
            "idempotency_key": idempotency_key,
            "input_digest": input_digest,
            "result_json": Jsonb(result),
        },
    )
    row = cur.fetchone()
    response = dict(result)
    response["receipt"] = _receipt_dict(row)
    response["replayed"] = False
    return response


def _file_dict(row: Dict[str, Any], *, include_content: bool) -> Dict[str, Any]:
    item = {
        "file_id": str(row["file_id"]),
        "path": row["path"],
        "media_type": row["media_type"],
        "content_sha256": row["content_sha256"],
        "revision": int(row["revision"]),
        "updated_at": row["updated_at"].isoformat(),
    }
    if include_content:
        item["content"] = row["content"]
    return item


def _active_project_name_taken(cur: Any, org_id: uuid.UUID, name: str) -> bool:
    cur.execute(
        "SELECT 1 FROM projects WHERE org_id = %(org_id)s AND status = 'active' "
        "AND deleted_at IS NULL AND lower(btrim(name)) = lower(btrim(%(name)s)) LIMIT 1",
        {"org_id": org_id, "name": name},
    )
    return cur.fetchone() is not None


def _assert_project_name_free(cur: Any, org_id: uuid.UUID, name: str) -> None:
    # Migration 0041 enforces one active name per org with a raising trigger, so
    # an unchecked INSERT surfaces as a 500. Fail as a 409 the client can explain.
    if _active_project_name_taken(cur, org_id, name):
        raise LifecycleConflict(
            "a project with that name already exists in this workspace"
        )


def _resolve_free_project_name(cur: Any, org_id: uuid.UUID, requested: str) -> str:
    # Clone names are derived, not typed: a caller cannot pick a different one
    # (CloneDialog takes no name field), so a taken name must resolve rather
    # than fail. The receipt carries the granted name and the dialog shows it.
    if not _active_project_name_taken(cur, org_id, requested):
        return requested
    for suffix in range(2, 51):
        candidate = _validate_project_name(f"{requested} {suffix}")
        if not _active_project_name_taken(cur, org_id, candidate):
            return candidate
    raise LifecycleConflict(
        "too many projects share that name; rename the source project first"
    )


def _member_dict(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "membership_id": str(row["membership_id"]),
        "binding_id": str(row["binding_id"]),
        "role": row["role"],
        "status": row["status"],
        "created_at": row["created_at"].isoformat(),
        "revoked_at": row["revoked_at"].isoformat() if row["revoked_at"] else None,
    }


def create_blank_project(
    org_id: uuid.UUID, actor_binding_id: uuid.UUID, *, name: str,
    idempotency_key: str,
) -> Dict[str, Any]:
    name = _validate_project_name(name)
    idempotency_key = _validate_idempotency_key(idempotency_key)
    input_digest = _canonical_digest({"name": name, "profile": "blank_browser"})

    def operation(conn: Any) -> Dict[str, Any]:
        with conn.cursor() as cur:
            tenant_role = _actor_tenant_role(cur, org_id, actor_binding_id)
            if tenant_role not in WRITE_ROLES:
                raise LifecycleForbidden("tenant role does not permit project creation")
            _lock_idempotency(cur, org_id, "project_created", idempotency_key)
            replay = _receipt_replay(
                cur, org_id, None, "project_created", idempotency_key, input_digest,
            )
            if replay is not None:
                return replay
            _assert_project_name_free(cur, org_id, name)
            project_id = new_uuid()
            cur.execute(
                "INSERT INTO projects (project_id, org_id, name) "
                "VALUES (%(project_id)s, %(org_id)s, %(name)s)",
                {"project_id": project_id, "org_id": org_id, "name": name},
            )
            cur.execute(
                "INSERT INTO project_authority_modes "
                "(org_id, project_id, authority_mode, selected_by) "
                "VALUES (%(org_id)s, %(project_id)s, 'postgres_canonical', 'server')",
                {"org_id": org_id, "project_id": project_id},
            )
            cur.execute(
                "INSERT INTO project_member_bindings "
                "(membership_id, org_id, project_id, binding_id, role, invited_by_binding_id) "
                "VALUES (%(membership_id)s, %(org_id)s, %(project_id)s, "
                "%(binding_id)s, %(role)s, %(binding_id)s)",
                {
                    "membership_id": new_uuid(),
                    "org_id": org_id,
                    "project_id": project_id,
                    "binding_id": actor_binding_id,
                    "role": tenant_role,
                },
            )
            result = {
                "project": {
                    "project_id": str(project_id),
                    "name": name,
                    "status": "active",
                    "profile": "blank_browser",
                },
            }
            return _write_receipt(
                cur, org_id, project_id, actor_binding_id, "project_created",
                idempotency_key, input_digest, result,
            )

    return run_transaction(operation, isolation="serializable")


def project_snapshot(
    org_id: uuid.UUID, project_id: uuid.UUID, actor_binding_id: uuid.UUID,
) -> Dict[str, Any]:
    def operation(conn: Any) -> Dict[str, Any]:
        with conn.cursor() as cur:
            project = _project_row(cur, org_id, project_id, lock=False)
            viewer_role = _require_project_role(
                cur, org_id, project_id, actor_binding_id, write=False, lock=False,
            )
            cur.execute(
                "SELECT membership_id, binding_id, role, status, created_at, revoked_at "
                "FROM project_member_bindings WHERE org_id = %(org_id)s "
                "AND project_id = %(project_id)s AND status = 'active' "
                "ORDER BY created_at, membership_id",
                {"org_id": org_id, "project_id": project_id},
            )
            members = [_member_dict(row) for row in cur.fetchall()]
            cur.execute(
                "SELECT file_id, path, media_type, content, content_sha256, revision, updated_at "
                "FROM project_files WHERE org_id = %(org_id)s "
                "AND project_id = %(project_id)s ORDER BY path",
                {"org_id": org_id, "project_id": project_id},
            )
            files = [_file_dict(row, include_content=True) for row in cur.fetchall()]
            cur.execute(
                "SELECT receipt_id, project_id, action, input_digest, created_at "
                "FROM project_lifecycle_receipts WHERE org_id = %(org_id)s "
                "AND project_id = %(project_id)s ORDER BY created_at, receipt_id",
                {"org_id": org_id, "project_id": project_id},
            )
            receipts = [_receipt_dict(row) for row in cur.fetchall()]
            # The caller's own identity, server-derived. A browser cannot know
            # its actor binding id (nothing echoes it), so without this the
            # client can only guess which roster row is "you" - and guessing is
            # exactly what the role matrix must never do.
            viewer_binding = str(actor_binding_id)
            viewer_row = next(
                (m for m in members if m["binding_id"] == viewer_binding), None,
            )
            viewer = {
                "binding_id": viewer_binding,
                "membership_id": viewer_row["membership_id"] if viewer_row else None,
                "role": viewer_role,
                "can_invite": viewer_role in WRITE_ROLES,
                "can_manage": viewer_role in WRITE_ROLES,
            }
            return {
                "project": {
                    "project_id": str(project["project_id"]),
                    "name": project["name"],
                    "status": project["status"],
                    "profile": "blank_browser",
                },
                "viewer": viewer,
                "members": members,
                "files": files,
                "receipts": receipts,
            }

    return run_transaction(
        operation, isolation="serializable", read_only=True,
        deferrable=True,
    )


def invite_project_member(
    org_id: uuid.UUID, project_id: uuid.UUID, actor_binding_id: uuid.UUID, *,
    member_binding_id: uuid.UUID, role: str, idempotency_key: str,
) -> Dict[str, Any]:
    role = _validate_role(role)
    idempotency_key = _validate_idempotency_key(idempotency_key)
    input_digest = _canonical_digest(
        {"member_binding_id": str(member_binding_id), "role": role},
    )

    def operation(conn: Any) -> Dict[str, Any]:
        with conn.cursor() as cur:
            _project_row(cur, org_id, project_id)
            actor_role = _require_project_role(
                cur, org_id, project_id, actor_binding_id, write=True,
            )
            replay = _receipt_replay(
                cur, org_id, project_id, "member_invited", idempotency_key,
                input_digest,
            )
            if replay is not None:
                return replay
            _target_binding_role(cur, org_id, member_binding_id)
            if actor_role != "owner" and role in WRITE_ROLES:
                raise LifecycleForbidden("only an owner may grant owner or editor access")
            cur.execute(
                "SELECT membership_id, role FROM project_member_bindings "
                "WHERE org_id = %(org_id)s AND project_id = %(project_id)s "
                "AND binding_id = %(binding_id)s AND status = 'active' FOR UPDATE",
                {
                    "org_id": org_id,
                    "project_id": project_id,
                    "binding_id": member_binding_id,
                },
            )
            existing = cur.fetchone()
            if existing is None:
                membership_id = new_uuid()
                cur.execute(
                    "INSERT INTO project_member_bindings "
                    "(membership_id, org_id, project_id, binding_id, role, "
                    " invited_by_binding_id) VALUES "
                    "(%(membership_id)s, %(org_id)s, %(project_id)s, %(binding_id)s, "
                    " %(role)s, %(actor_binding_id)s)",
                    {
                        "membership_id": membership_id,
                        "org_id": org_id,
                        "project_id": project_id,
                        "binding_id": member_binding_id,
                        "role": role,
                        "actor_binding_id": actor_binding_id,
                    },
                )
            else:
                membership_id = existing["membership_id"]
                if existing["role"] != role:
                    cur.execute(
                        "UPDATE project_member_bindings SET role = %(role)s "
                        "WHERE membership_id = %(membership_id)s",
                        {"role": role, "membership_id": membership_id},
                    )
            result = {
                "member": {
                    "membership_id": str(membership_id),
                    "binding_id": str(member_binding_id),
                    "role": role,
                    "status": "active",
                },
            }
            return _write_receipt(
                cur, org_id, project_id, actor_binding_id, "member_invited",
                idempotency_key, input_digest, result,
            )

    return run_transaction(operation, isolation="serializable")


def revoke_project_member(
    org_id: uuid.UUID, project_id: uuid.UUID, actor_binding_id: uuid.UUID, *,
    membership_id: uuid.UUID, idempotency_key: str,
) -> Dict[str, Any]:
    idempotency_key = _validate_idempotency_key(idempotency_key)
    input_digest = _canonical_digest({"membership_id": str(membership_id)})

    def operation(conn: Any) -> Dict[str, Any]:
        with conn.cursor() as cur:
            _project_row(cur, org_id, project_id)
            actor_role = _require_project_role(
                cur, org_id, project_id, actor_binding_id, write=True,
            )
            replay = _receipt_replay(
                cur, org_id, project_id, "member_revoked", idempotency_key,
                input_digest,
            )
            if replay is not None:
                return replay
            cur.execute(
                "SELECT membership_id, binding_id, role, status "
                "FROM project_member_bindings WHERE org_id = %(org_id)s "
                "AND project_id = %(project_id)s AND membership_id = %(membership_id)s "
                "FOR UPDATE",
                {
                    "org_id": org_id,
                    "project_id": project_id,
                    "membership_id": membership_id,
                },
            )
            member = cur.fetchone()
            if member is None:
                raise LifecycleUnavailable("project member not found")
            if actor_role != "owner" and member["role"] in WRITE_ROLES:
                raise LifecycleForbidden("only an owner may revoke owner or editor access")
            if member["status"] == "active":
                cur.execute(
                    "UPDATE project_member_bindings SET status = 'revoked', revoked_at = NOW() "
                    "WHERE membership_id = %(membership_id)s",
                    {"membership_id": membership_id},
                )
            result = {
                "member": {
                    "membership_id": str(membership_id),
                    "binding_id": str(member["binding_id"]),
                    "role": member["role"],
                    "status": "revoked",
                },
            }
            return _write_receipt(
                cur, org_id, project_id, actor_binding_id, "member_revoked",
                idempotency_key, input_digest, result,
            )

    return run_transaction(operation, isolation="serializable")


def put_project_file(
    org_id: uuid.UUID, project_id: uuid.UUID, actor_binding_id: uuid.UUID, *,
    path: str, media_type: str, content: str, idempotency_key: str,
) -> Dict[str, Any]:
    path = _validate_path(path)
    media_type = _validate_media_type(media_type)
    content = _validate_content(content)
    idempotency_key = _validate_idempotency_key(idempotency_key)
    content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    input_digest = _canonical_digest(
        {"path": path, "media_type": media_type, "content_sha256": content_sha256},
    )

    def operation(conn: Any) -> Dict[str, Any]:
        with conn.cursor() as cur:
            _project_row(cur, org_id, project_id)
            _require_project_role(
                cur, org_id, project_id, actor_binding_id, write=True,
            )
            replay = _receipt_replay(
                cur, org_id, project_id, "file_put", idempotency_key, input_digest,
            )
            if replay is not None:
                return replay
            cur.execute(
                "INSERT INTO project_files "
                "(file_id, org_id, project_id, path, media_type, content, "
                " content_sha256, created_by_binding_id) "
                "VALUES (%(file_id)s, %(org_id)s, %(project_id)s, %(path)s, "
                "%(media_type)s, %(content)s, %(content_sha256)s, %(actor_binding_id)s) "
                "ON CONFLICT ON CONSTRAINT project_files_scope_path_unique DO UPDATE SET "
                "media_type = EXCLUDED.media_type, content = EXCLUDED.content, "
                "content_sha256 = EXCLUDED.content_sha256, revision = project_files.revision + 1, "
                "updated_at = NOW() "
                "RETURNING file_id, path, media_type, content, content_sha256, revision, updated_at",
                {
                    "file_id": new_uuid(),
                    "org_id": org_id,
                    "project_id": project_id,
                    "path": path,
                    "media_type": media_type,
                    "content": content,
                    "content_sha256": content_sha256,
                    "actor_binding_id": actor_binding_id,
                },
            )
            result = {"file": _file_dict(cur.fetchone(), include_content=False)}
            return _write_receipt(
                cur, org_id, project_id, actor_binding_id, "file_put",
                idempotency_key, input_digest, result,
            )

    return run_transaction(operation, isolation="serializable")


def delete_project_file(
    org_id: uuid.UUID, project_id: uuid.UUID, actor_binding_id: uuid.UUID, *,
    file_id: uuid.UUID, idempotency_key: str,
) -> Dict[str, Any]:
    idempotency_key = _validate_idempotency_key(idempotency_key)
    input_digest = _canonical_digest({"file_id": str(file_id)})

    def operation(conn: Any) -> Dict[str, Any]:
        with conn.cursor() as cur:
            _project_row(cur, org_id, project_id)
            _require_project_role(
                cur, org_id, project_id, actor_binding_id, write=True,
            )
            replay = _receipt_replay(
                cur, org_id, project_id, "file_deleted", idempotency_key,
                input_digest,
            )
            if replay is not None:
                return replay
            cur.execute(
                "DELETE FROM project_files WHERE org_id = %(org_id)s "
                "AND project_id = %(project_id)s AND file_id = %(file_id)s "
                "RETURNING path, content_sha256",
                {"org_id": org_id, "project_id": project_id, "file_id": file_id},
            )
            row = cur.fetchone()
            if row is None:
                raise LifecycleUnavailable("project file not found")
            result = {
                "file": {
                    "file_id": str(file_id),
                    "path": row["path"],
                    "content_sha256": row["content_sha256"],
                    "status": "deleted",
                },
            }
            return _write_receipt(
                cur, org_id, project_id, actor_binding_id, "file_deleted",
                idempotency_key, input_digest, result,
            )

    return run_transaction(operation, isolation="serializable")


def clone_project(
    org_id: uuid.UUID, project_id: uuid.UUID, actor_binding_id: uuid.UUID, *,
    name: str, idempotency_key: str,
) -> Dict[str, Any]:
    name = _validate_project_name(name)
    idempotency_key = _validate_idempotency_key(idempotency_key)
    input_digest = _canonical_digest({"source_project_id": str(project_id), "name": name})

    def operation(conn: Any) -> Dict[str, Any]:
        with conn.cursor() as cur:
            _project_row(cur, org_id, project_id)
            actor_role = _require_project_role(
                cur, org_id, project_id, actor_binding_id, write=False,
            )
            if actor_role not in WRITE_ROLES:
                raise LifecycleForbidden("project role does not permit cloning")
            replay = _receipt_replay(
                cur, org_id, project_id, "project_cloned", idempotency_key,
                input_digest,
            )
            if replay is not None:
                return replay
            # Distinct binding, never a rebind of `name`: assigning the closed-over
            # name inside this closure makes it local and the read above it raises
            # UnboundLocalError.
            granted_name = _resolve_free_project_name(cur, org_id, name)
            target_project_id = new_uuid()
            cur.execute(
                "INSERT INTO projects (project_id, org_id, name) "
                "VALUES (%(project_id)s, %(org_id)s, %(name)s)",
                {"project_id": target_project_id, "org_id": org_id, "name": granted_name},
            )
            cur.execute(
                "INSERT INTO project_authority_modes "
                "(org_id, project_id, authority_mode, selected_by) "
                "SELECT %(org_id)s, %(target_project_id)s, "
                "COALESCE((SELECT authority_mode FROM live_project_authority_modes "
                "WHERE org_id = %(org_id)s AND project_id = %(source_project_id)s), "
                "'postgres_canonical'), 'server'",
                {
                    "org_id": org_id,
                    "source_project_id": project_id,
                    "target_project_id": target_project_id,
                },
            )
            cur.execute(
                "INSERT INTO project_member_bindings "
                "(membership_id, org_id, project_id, binding_id, role, invited_by_binding_id) "
                "VALUES (%(membership_id)s, %(org_id)s, %(project_id)s, "
                "%(binding_id)s, %(role)s, %(binding_id)s)",
                {
                    "membership_id": new_uuid(),
                    "org_id": org_id,
                    "project_id": target_project_id,
                    "binding_id": actor_binding_id,
                    "role": actor_role,
                },
            )
            cur.execute(
                "INSERT INTO project_files "
                "(file_id, org_id, project_id, path, media_type, content, "
                " content_sha256, revision, created_by_binding_id) "
                "SELECT gen_random_uuid(), org_id, %(target_project_id)s, path, "
                "media_type, content, content_sha256, revision, %(actor_binding_id)s "
                "FROM project_files WHERE org_id = %(org_id)s "
                "AND project_id = %(source_project_id)s",
                {
                    "org_id": org_id,
                    "source_project_id": project_id,
                    "target_project_id": target_project_id,
                    "actor_binding_id": actor_binding_id,
                },
            )
            copied_files = int(cur.rowcount)
            result = {
                "project": {
                    "project_id": str(target_project_id),
                    # The GRANTED name, which may carry a collision suffix. The
                    # dialog renders receipt.name, so the user sees what landed.
                    "name": granted_name,
                    "status": "active",
                    "profile": "blank_browser",
                },
                "source_project_id": str(project_id),
                "copied_file_count": copied_files,
            }
            return _write_receipt(
                cur, org_id, project_id, actor_binding_id, "project_cloned",
                idempotency_key, input_digest, result,
            )

    return run_transaction(operation, isolation="serializable")


def export_project(
    org_id: uuid.UUID, project_id: uuid.UUID, actor_binding_id: uuid.UUID, *,
    idempotency_key: str,
) -> Dict[str, Any]:
    idempotency_key = _validate_idempotency_key(idempotency_key)

    def operation(conn: Any) -> Dict[str, Any]:
        with conn.cursor() as cur:
            project = _project_row(cur, org_id, project_id)
            _require_project_role(
                cur, org_id, project_id, actor_binding_id, write=False,
            )
            cur.execute(
                "SELECT file_id, path, media_type, content, content_sha256, revision, updated_at "
                "FROM project_files WHERE org_id = %(org_id)s "
                "AND project_id = %(project_id)s ORDER BY path",
                {"org_id": org_id, "project_id": project_id},
            )
            files = [_file_dict(row, include_content=True) for row in cur.fetchall()]
            cur.execute(
                "SELECT membership_id, binding_id, role, status, created_at, revoked_at "
                "FROM project_member_bindings WHERE org_id = %(org_id)s "
                "AND project_id = %(project_id)s AND status = 'active' "
                "ORDER BY created_at, membership_id",
                {"org_id": org_id, "project_id": project_id},
            )
            members = [_member_dict(row) for row in cur.fetchall()]
            export = {
                "schema": "leaf.project-export.v1",
                "project": {
                    "project_id": str(project_id),
                    "name": project["name"],
                    "profile": "blank_browser",
                },
                "files": files,
                "members": members,
            }
            export_sha256 = _canonical_digest(export)
            replay = _receipt_replay(
                cur, org_id, project_id, "project_exported", idempotency_key,
                export_sha256,
            )
            if replay is not None:
                replay["export"] = export
                return replay
            result = {
                "export_sha256": export_sha256,
                "file_count": len(files),
                "member_count": len(members),
            }
            response = _write_receipt(
                cur, org_id, project_id, actor_binding_id, "project_exported",
                idempotency_key, export_sha256, result,
            )
            response["export"] = export
            return response

    return run_transaction(operation, isolation="serializable")


def reset_project(
    org_id: uuid.UUID, project_id: uuid.UUID, actor_binding_id: uuid.UUID, *,
    idempotency_key: str,
) -> Dict[str, Any]:
    idempotency_key = _validate_idempotency_key(idempotency_key)
    input_digest = _canonical_digest({"project_id": str(project_id), "scope": "browser_files"})

    def operation(conn: Any) -> Dict[str, Any]:
        with conn.cursor() as cur:
            _project_row(cur, org_id, project_id)
            _require_project_role(
                cur, org_id, project_id, actor_binding_id, write=True,
            )
            replay = _receipt_replay(
                cur, org_id, project_id, "project_reset", idempotency_key,
                input_digest,
            )
            if replay is not None:
                return replay
            cur.execute(
                "DELETE FROM project_files WHERE org_id = %(org_id)s "
                "AND project_id = %(project_id)s RETURNING content_sha256",
                {"org_id": org_id, "project_id": project_id},
            )
            deleted_digests = sorted(row["content_sha256"] for row in cur.fetchall())
            result = {
                "project_id": str(project_id),
                "status": "reset",
                "deleted_file_count": len(deleted_digests),
                "deleted_content_digest": _canonical_digest(deleted_digests),
            }
            return _write_receipt(
                cur, org_id, project_id, actor_binding_id, "project_reset",
                idempotency_key, input_digest, result,
            )

    return run_transaction(operation, isolation="serializable")


def delete_project(
    org_id: uuid.UUID, project_id: uuid.UUID, actor_binding_id: uuid.UUID, *,
    idempotency_key: str,
) -> Dict[str, Any]:
    idempotency_key = _validate_idempotency_key(idempotency_key)
    input_digest = _canonical_digest({"project_id": str(project_id)})

    def operation(conn: Any) -> Dict[str, Any]:
        with conn.cursor() as cur:
            _actor_tenant_role(cur, org_id, actor_binding_id)
            replay = _receipt_replay(
                cur, org_id, project_id, "project_deleted", idempotency_key,
                input_digest,
            )
            if replay is not None:
                return replay
            _project_row(cur, org_id, project_id)
            _require_project_role(
                cur, org_id, project_id, actor_binding_id, write=True,
            )
            result = {"project_id": str(project_id), "status": "deleted"}
            response = _write_receipt(
                cur, org_id, project_id, actor_binding_id, "project_deleted",
                idempotency_key, input_digest, result,
            )
            cur.execute(
                "UPDATE project_member_bindings SET status = 'revoked', revoked_at = NOW() "
                "WHERE org_id = %(org_id)s AND project_id = %(project_id)s "
                "AND status = 'active'",
                {"org_id": org_id, "project_id": project_id},
            )
            cur.execute(
                "UPDATE projects SET status = 'deleted', deleted_at = NOW(), updated_at = NOW() "
                "WHERE org_id = %(org_id)s AND project_id = %(project_id)s",
                {"org_id": org_id, "project_id": project_id},
            )
            return response

    return run_transaction(operation, isolation="serializable")
