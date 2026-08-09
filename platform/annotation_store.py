"""Durable P7 annotation batch and target-head authority.

Annotation content lives in immutable Git objects. This store persists only
exact object witnesses, target ownership, decision state, and content-free
audit metadata. Every state-changing decision locks a stable batch anchor and
the target head in one PostgreSQL transaction.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any, Dict, Mapping, Optional, Tuple

from . import db


class AnnotationStoreError(RuntimeError):
    def __init__(self, code: str, status_code: int = 409, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.detail = detail


_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_BATCH_COLS = (
    "batch_id, revision, tenant_id, org_id, project_id, drawing_id, "
    "session_id, kind, retry_of_batch_id, reverses_batch_id, base_version, "
    "base_commit, base_tree, preview_commit, preview_tree, payload_digest, "
    "payload_count, request_key, request_fingerprint, state, "
    "created_by_binding_id, created_at, lease_expires_at, decided_at, "
    "decided_by_binding_id, decision_key, applied_version, reason, superseded_at"
)
_TARGET_COLS = (
    "tenant_id, org_id, project_id, drawing_id, version, commit_sha, tree_sha, "
    "updated_at, updated_by_binding_id"
)


def _row(row: Any) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    return {
        key: (str(value) if isinstance(value, uuid.UUID) else value)
        for key, value in dict(row).items()
    }


def _uuid(value: str, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise AnnotationStoreError("annotation_not_found", 404, field) from exc


def _sha(value: str, field: str) -> str:
    text = str(value or "")
    if not _SHA.fullmatch(text):
        raise AnnotationStoreError("invalid_git_witness", 400, field)
    return text


def _digest(value: str) -> str:
    text = str(value or "")
    if not _DIGEST.fullmatch(text):
        raise AnnotationStoreError("invalid_payload_digest", 400)
    return text


def _scope(tenant_id: str, org_id: str, project_id: str,
           drawing_id: str) -> Dict[str, uuid.UUID]:
    tenant = _uuid(tenant_id, "tenant_id")
    org = _uuid(org_id, "org_id")
    if tenant != org:
        raise AnnotationStoreError("annotation_not_found", 404)
    return {
        "tenant": tenant,
        "org": org,
        "project": _uuid(project_id, "project_id"),
        "drawing": _uuid(drawing_id, "drawing_id"),
    }


def _binding(cur: Any, tenant: uuid.UUID, binding_id: str) -> uuid.UUID:
    binding = _uuid(binding_id, "binding_id")
    cur.execute(
        "SELECT binding_id FROM identity_bindings "
        "WHERE binding_id = %(binding)s AND platform_tenant_id = %(tenant)s "
        "AND status = 'active'",
        {"binding": binding, "tenant": tenant},
    )
    if cur.fetchone() is None:
        raise AnnotationStoreError("annotation_not_found", 404)
    return binding


def target(tenant_id: str, org_id: str, project_id: str,
           drawing_id: str) -> Optional[Dict[str, Any]]:
    scope = _scope(tenant_id, org_id, project_id, drawing_id)
    with db.cursor() as cur:
        cur.execute(
            f"SELECT {_TARGET_COLS} FROM annotation_targets "
            "WHERE tenant_id = %(tenant)s AND org_id = %(org)s "
            "AND project_id = %(project)s AND drawing_id = %(drawing)s",
            scope,
        )
        return _row(cur.fetchone())


def latest_batch(batch_id: str, tenant_id: str) -> Optional[Dict[str, Any]]:
    tenant = _uuid(tenant_id, "tenant_id")
    batch = _uuid(batch_id, "batch_id")
    with db.cursor() as cur:
        cur.execute(
            f"SELECT {_BATCH_COLS} FROM annotation_batches "
            "WHERE batch_id = %(batch)s AND tenant_id = %(tenant)s "
            "ORDER BY revision DESC LIMIT 1",
            {"batch": batch, "tenant": tenant},
        )
        return _row(cur.fetchone())


def register_target(*, tenant_id: str, org_id: str, project_id: str,
                    drawing_id: str, commit_sha: str, tree_sha: str,
                    actor_binding_id: str) -> Dict[str, Any]:
    """Register the trusted initial immutable source for a drawing.

    A repeated registration must quote the exact same commit and tree. This
    function never moves an existing head.
    """
    scope = _scope(tenant_id, org_id, project_id, drawing_id)
    commit = _sha(commit_sha, "commit_sha")
    tree = _sha(tree_sha, "tree_sha")
    with db.connection() as conn, conn.cursor() as cur:
        actor = _binding(cur, scope["tenant"], actor_binding_id)
        cur.execute(
            "SELECT d.drawing_id FROM drawing_artifacts d "
            "JOIN projects p ON p.org_id = d.org_id AND p.project_id = d.project_id "
            "JOIN orgs o ON o.org_id = d.org_id "
            "WHERE d.drawing_id = %(drawing)s AND d.project_id = %(project)s "
            "AND d.org_id = %(org)s AND d.status = 'active' "
            "AND p.status = 'active' AND o.status = 'active' FOR UPDATE",
            scope,
        )
        if cur.fetchone() is None:
            raise AnnotationStoreError("annotation_not_found", 404)
        cur.execute(
            "INSERT INTO annotation_targets "
            "(tenant_id, org_id, project_id, drawing_id, version, commit_sha, "
            " tree_sha, updated_by_binding_id) "
            "VALUES (%(tenant)s, %(org)s, %(project)s, %(drawing)s, 0, "
            "%(commit)s, %(tree)s, %(actor)s) ON CONFLICT DO NOTHING",
            {**scope, "commit": commit, "tree": tree, "actor": actor},
        )
        cur.execute(
            f"SELECT {_TARGET_COLS} FROM annotation_targets "
            "WHERE tenant_id = %(tenant)s AND org_id = %(org)s "
            "AND project_id = %(project)s AND drawing_id = %(drawing)s FOR UPDATE",
            scope,
        )
        current = _row(cur.fetchone())
        if current is None:
            raise AnnotationStoreError("annotation_not_found", 404)
        if current["commit_sha"] != commit or current["tree_sha"] != tree:
            raise AnnotationStoreError("target_source_conflict", 409)
        return current


def _fingerprint(fields: Mapping[str, Any]) -> str:
    encoded = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def create_batch(*, batch_id: str, tenant_id: str, org_id: str,
                 project_id: str, drawing_id: str, session_id: str,
                 kind: str, base_version: int, base_commit: str, base_tree: str,
                 preview_commit: str, preview_tree: str, payload_digest: str,
                 payload_count: int, request_key: str,
                 created_by_binding_id: str, lease_s: int = 900,
                 retry_of_batch_id: Optional[str] = None,
                 reverses_batch_id: Optional[str] = None) -> Dict[str, Any]:
    """Create one fresh preview bound to an owned canonical drawing target."""
    scope = _scope(tenant_id, org_id, project_id, drawing_id)
    batch = _uuid(batch_id, "batch_id")
    if kind not in {"apply", "undo"}:
        raise AnnotationStoreError("invalid_batch_kind", 400)
    if kind == "undo" and not reverses_batch_id:
        raise AnnotationStoreError("undo_source_required", 400)
    if kind == "apply" and reverses_batch_id:
        raise AnnotationStoreError("invalid_batch_link", 400)
    if not isinstance(base_version, int) or isinstance(base_version, bool) or base_version < 0:
        raise AnnotationStoreError("invalid_base_version", 400)
    if not isinstance(payload_count, int) or isinstance(payload_count, bool) or payload_count < 1:
        raise AnnotationStoreError("invalid_payload_count", 400)
    if not isinstance(lease_s, int) or isinstance(lease_s, bool) or lease_s < 1:
        raise AnnotationStoreError("invalid_lease", 400)
    key = str(request_key or "").strip()
    if not key or len(key) > 200:
        raise AnnotationStoreError("invalid_request_key", 400)
    session = str(session_id or "").strip()
    if not session:
        raise AnnotationStoreError("annotation_not_found", 404)
    base_c = _sha(base_commit, "base_commit")
    base_t = _sha(base_tree, "base_tree")
    preview_c = _sha(preview_commit, "preview_commit")
    preview_t = _sha(preview_tree, "preview_tree")
    digest = _digest(payload_digest)
    retry = _uuid(retry_of_batch_id, "retry_of_batch_id") if retry_of_batch_id else None
    reverses = _uuid(reverses_batch_id, "reverses_batch_id") if reverses_batch_id else None
    creator = _uuid(created_by_binding_id, "created_by_binding_id")
    fingerprint_fields = {
        "batch_id": str(batch), "tenant_id": str(scope["tenant"]),
        "org_id": str(scope["org"]), "project_id": str(scope["project"]),
        "drawing_id": str(scope["drawing"]), "session_id": session, "kind": kind,
        "retry_of_batch_id": str(retry) if retry else None,
        "reverses_batch_id": str(reverses) if reverses else None,
        "base_version": base_version, "base_commit": base_c, "base_tree": base_t,
        "preview_commit": preview_c, "preview_tree": preview_t,
        "payload_digest": digest, "payload_count": payload_count,
        "created_by_binding_id": str(creator), "lease_s": lease_s,
    }
    fingerprint = _fingerprint(fingerprint_fields)

    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT {_BATCH_COLS} FROM annotation_batches "
            "WHERE tenant_id = %(tenant)s AND request_key = %(key)s "
            "AND revision = 0 FOR UPDATE",
            {"tenant": scope["tenant"], "key": key},
        )
        replay = _row(cur.fetchone())
        if replay is not None:
            if replay["request_fingerprint"] != fingerprint:
                raise AnnotationStoreError("idempotency_conflict", 409)
            cur.execute(
                f"SELECT {_BATCH_COLS} FROM annotation_batches "
                "WHERE batch_id = %(batch)s ORDER BY revision DESC LIMIT 1",
                {"batch": replay["batch_id"]},
            )
            return _row(cur.fetchone())  # type: ignore[return-value]

        creator = _binding(cur, scope["tenant"], str(creator))
        cur.execute(
            "SELECT session_id FROM app_sessions WHERE session_id = %(session)s "
            "AND tenant_id = %(tenant_text)s AND drawing_id = %(drawing_text)s "
            "AND status = 'active'",
            {"session": session, "tenant_text": str(scope["tenant"]),
             "drawing_text": str(scope["drawing"])},
        )
        if cur.fetchone() is None:
            raise AnnotationStoreError("annotation_not_found", 404)
        cur.execute(
            "SELECT d.drawing_id FROM drawing_artifacts d "
            "JOIN projects p ON p.org_id=d.org_id AND p.project_id=d.project_id "
            "JOIN orgs o ON o.org_id=d.org_id "
            "WHERE d.drawing_id=%(drawing)s AND d.project_id=%(project)s "
            "AND d.org_id=%(org)s AND d.status='active' "
            "AND p.status='active' AND o.status='active' FOR SHARE",
            scope,
        )
        if cur.fetchone() is None:
            raise AnnotationStoreError("annotation_not_found", 404)
        cur.execute(
            f"SELECT {_TARGET_COLS} FROM annotation_targets "
            "WHERE tenant_id = %(tenant)s AND org_id = %(org)s "
            "AND project_id = %(project)s AND drawing_id = %(drawing)s "
            "AND EXISTS (SELECT 1 FROM drawing_artifacts d "
            " JOIN projects p ON p.org_id=d.org_id AND p.project_id=d.project_id "
            " JOIN orgs o ON o.org_id=d.org_id "
            " WHERE d.drawing_id=%(drawing)s AND d.project_id=%(project)s "
            " AND d.org_id=%(org)s AND d.status='active' "
            " AND p.status='active' AND o.status='active') FOR UPDATE",
            scope,
        )
        current_target = _row(cur.fetchone())
        if current_target is None:
            raise AnnotationStoreError("annotation_not_found", 404)
        if (int(current_target["version"]) != base_version
                or current_target["commit_sha"] != base_c
                or current_target["tree_sha"] != base_t):
            raise AnnotationStoreError("target_head_conflict", 409)

        # A partial index cannot use NOW(), so an expired row still occupies
        # the session's pending slot until a writer records the expiry. Retire
        # it here, in the same transaction as the replacement insert.
        cur.execute(
            f"SELECT {_BATCH_COLS} FROM annotation_batches "
            "WHERE tenant_id = %(tenant)s AND session_id = %(session)s "
            "AND project_id = %(project)s AND drawing_id = %(drawing)s "
            "AND state = 'pending' AND superseded_at IS NULL "
            "AND lease_expires_at <= NOW() FOR UPDATE",
            {**scope, "session": session},
        )
        for lapsed in [_row(row) for row in cur.fetchall()]:
            _append_revision(
                cur, lapsed, state="expired", actor=None, decision_key=None,
                applied_version=None, reason="lease_expired")
            _audit(cur, lapsed, to_state="expired", actor=None,
                   decision_key=None, before=current_target, after=current_target)

        link_id = retry or reverses
        linked = None
        if link_id:
            cur.execute(
                f"SELECT {_BATCH_COLS} FROM annotation_batches "
                "WHERE batch_id = %(linked)s AND tenant_id = %(tenant)s "
                "AND project_id = %(project)s AND drawing_id = %(drawing)s "
                "ORDER BY revision DESC LIMIT 1 FOR SHARE",
                {**scope, "linked": link_id},
            )
            linked = _row(cur.fetchone())
            if linked is None:
                raise AnnotationStoreError("annotation_not_found", 404)
        if retry and linked["state"] not in {"rejected", "expired"}:
            raise AnnotationStoreError("retry_source_not_terminal", 409)
        if reverses:
            if linked["state"] != "accepted":
                raise AnnotationStoreError("undo_source_not_accepted", 409)
            if (linked["preview_commit"] != base_c
                    or linked["preview_tree"] != base_t
                    or int(linked["applied_version"]) != base_version):
                raise AnnotationStoreError("undo_head_intervened", 409)

        cur.execute(
            "INSERT INTO annotation_batches "
            "(batch_id, revision, tenant_id, org_id, project_id, drawing_id, "
            " session_id, kind, retry_of_batch_id, reverses_batch_id, base_version, "
            " base_commit, base_tree, preview_commit, preview_tree, payload_digest, "
            " payload_count, request_key, request_fingerprint, state, "
            " created_by_binding_id, lease_expires_at) "
            "VALUES (%(batch)s, 0, %(tenant)s, %(org)s, %(project)s, %(drawing)s, "
            "%(session)s, %(kind)s, %(retry)s, %(reverses)s, %(base_version)s, "
            "%(base_commit)s, %(base_tree)s, %(preview_commit)s, %(preview_tree)s, "
            "%(digest)s, %(count)s, %(key)s, %(fingerprint)s, 'pending', "
            "%(creator)s, NOW() + (%(lease)s * INTERVAL '1 second')) "
            f"RETURNING {_BATCH_COLS}",
            {**scope, "batch": batch, "session": session, "kind": kind,
             "retry": retry, "reverses": reverses, "base_version": base_version,
             "base_commit": base_c, "base_tree": base_t,
             "preview_commit": preview_c, "preview_tree": preview_t,
             "digest": digest, "count": payload_count, "key": key,
             "fingerprint": fingerprint, "creator": creator, "lease": lease_s},
        )
        return _row(cur.fetchone())  # type: ignore[return-value]


def _lock_latest(cur: Any, batch_id: uuid.UUID,
                 tenant_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    cur.execute(
        "SELECT batch_id FROM annotation_batches "
        "WHERE batch_id = %(batch)s AND tenant_id = %(tenant)s AND revision = 0 "
        "FOR UPDATE",
        {"batch": batch_id, "tenant": tenant_id},
    )
    if cur.fetchone() is None:
        return None
    cur.execute(
        f"SELECT {_BATCH_COLS} FROM annotation_batches "
        "WHERE batch_id = %(batch)s AND tenant_id = %(tenant)s "
        "ORDER BY revision DESC LIMIT 1",
        {"batch": batch_id, "tenant": tenant_id},
    )
    return _row(cur.fetchone())


def _append_revision(cur: Any, current: Mapping[str, Any], *, state: str,
                     actor: Optional[uuid.UUID], decision_key: Optional[str],
                     applied_version: Optional[int], reason: str) -> Dict[str, Any]:
    cur.execute(
        "UPDATE annotation_batches SET superseded_at = NOW() "
        "WHERE batch_id = %(batch)s AND revision = %(revision)s "
        "AND superseded_at IS NULL",
        {"batch": current["batch_id"], "revision": current["revision"]},
    )
    cur.execute(
        "INSERT INTO annotation_batches "
        "(batch_id, revision, tenant_id, org_id, project_id, drawing_id, session_id, "
        " kind, retry_of_batch_id, reverses_batch_id, base_version, base_commit, "
        " base_tree, preview_commit, preview_tree, payload_digest, payload_count, "
        " request_key, request_fingerprint, state, created_by_binding_id, created_at, "
        " lease_expires_at, decided_at, decided_by_binding_id, decision_key, "
        " applied_version, reason) "
        "VALUES (%(batch)s, %(revision)s, %(tenant)s, %(org)s, %(project)s, "
        "%(drawing)s, %(session)s, %(kind)s, %(retry)s, %(reverses)s, "
        "%(base_version)s, %(base_commit)s, %(base_tree)s, %(preview_commit)s, "
        "%(preview_tree)s, %(digest)s, %(count)s, %(request_key)s, %(fingerprint)s, "
        "%(state)s, %(creator)s, %(created_at)s, %(lease)s, NOW(), %(actor)s, "
        "%(decision_key)s, %(applied_version)s, %(reason)s) "
        f"RETURNING {_BATCH_COLS}",
        {"batch": current["batch_id"], "revision": int(current["revision"]) + 1,
         "tenant": current["tenant_id"], "org": current["org_id"],
         "project": current["project_id"], "drawing": current["drawing_id"],
         "session": current["session_id"], "kind": current["kind"],
         "retry": current.get("retry_of_batch_id"),
         "reverses": current.get("reverses_batch_id"),
         "base_version": current["base_version"], "base_commit": current["base_commit"],
         "base_tree": current["base_tree"], "preview_commit": current["preview_commit"],
         "preview_tree": current["preview_tree"], "digest": current["payload_digest"],
         "count": current["payload_count"], "request_key": current["request_key"],
         "fingerprint": current["request_fingerprint"], "state": state,
         "creator": current["created_by_binding_id"], "created_at": current["created_at"],
         "lease": current["lease_expires_at"], "actor": actor,
         "decision_key": decision_key, "applied_version": applied_version,
         "reason": reason},
    )
    return _row(cur.fetchone())  # type: ignore[return-value]


def _audit(cur: Any, batch: Mapping[str, Any], *, to_state: str,
           actor: Optional[uuid.UUID], decision_key: Optional[str],
           before: Mapping[str, Any], after: Mapping[str, Any]) -> None:
    cur.execute(
        "INSERT INTO annotation_audit "
        "(batch_id, tenant_id, org_id, project_id, drawing_id, from_state, to_state, "
        " actor_binding_id, decision_key, payload_digest, payload_count, "
        " before_version, after_version, before_commit, before_tree, "
        " after_commit, after_tree) "
        "VALUES (%(batch)s, %(tenant)s, %(org)s, %(project)s, %(drawing)s, "
        "'pending', %(to_state)s, %(actor)s, %(decision_key)s, %(digest)s, "
        "%(count)s, %(before_version)s, %(after_version)s, %(before_commit)s, "
        "%(before_tree)s, %(after_commit)s, %(after_tree)s)",
        {"batch": batch["batch_id"], "tenant": batch["tenant_id"],
         "org": batch["org_id"], "project": batch["project_id"],
         "drawing": batch["drawing_id"], "to_state": to_state, "actor": actor,
         "decision_key": decision_key, "digest": batch["payload_digest"],
         "count": batch["payload_count"], "before_version": before["version"],
         "after_version": after["version"], "before_commit": before["commit_sha"],
         "before_tree": before["tree_sha"], "after_commit": after["commit_sha"],
         "after_tree": after["tree_sha"]},
    )


def _decision_inputs(batch_id: str, tenant_id: str, actor_binding_id: str,
                     decision_key: str) -> Tuple[uuid.UUID, uuid.UUID, uuid.UUID, str]:
    key = str(decision_key or "").strip()
    if len(key) < 8 or len(key) > 200:
        raise AnnotationStoreError("invalid_decision_key", 400)
    return (_uuid(batch_id, "batch_id"), _uuid(tenant_id, "tenant_id"),
            _uuid(actor_binding_id, "actor_binding_id"), key)


def _applied_target_receipt(batch: Mapping[str, Any], actor: uuid.UUID) -> Dict[str, Any]:
    """Stable accept result, including after later batches move the live head."""
    return {
        "tenant_id": str(batch["tenant_id"]), "org_id": str(batch["org_id"]),
        "project_id": str(batch["project_id"]), "drawing_id": str(batch["drawing_id"]),
        "version": int(batch["applied_version"]),
        "commit_sha": batch["preview_commit"], "tree_sha": batch["preview_tree"],
        "updated_by_binding_id": str(actor),
    }


def accept(*, batch_id: str, tenant_id: str, actor_binding_id: str,
           decision_key: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Accept one exact preview and atomically advance its target head."""
    batch_uuid, tenant, actor_input, key = _decision_inputs(
        batch_id, tenant_id, actor_binding_id, decision_key)
    with db.connection() as conn, conn.cursor() as cur:
        current = _lock_latest(cur, batch_uuid, tenant)
        if current is None:
            raise AnnotationStoreError("annotation_not_found", 404)
        actor = _binding(cur, tenant, str(actor_input))
        if current["state"] != "pending":
            if (current["state"] == "accepted" and current.get("decision_key") == key
                    and str(current.get("decided_by_binding_id")) == str(actor)):
                return current, _applied_target_receipt(current, actor)
            raise AnnotationStoreError("already_decided", 409)
        cur.execute(
            "SELECT lease_expires_at <= NOW() AS lapsed FROM annotation_batches "
            "WHERE batch_id = %(batch)s AND revision = %(revision)s",
            {"batch": batch_uuid, "revision": current["revision"]},
        )
        if bool(cur.fetchone()["lapsed"]):
            raise AnnotationStoreError("lease_expired", 410)
        cur.execute(
            f"SELECT {_TARGET_COLS} FROM annotation_targets "
            "WHERE tenant_id = %(tenant)s AND org_id = %(org)s "
            "AND project_id = %(project)s AND drawing_id = %(drawing)s "
            "AND EXISTS (SELECT 1 FROM drawing_artifacts d "
            " JOIN projects p ON p.org_id=d.org_id AND p.project_id=d.project_id "
            " JOIN orgs o ON o.org_id=d.org_id "
            " WHERE d.drawing_id=%(drawing)s AND d.project_id=%(project)s "
            " AND d.org_id=%(org)s AND d.status='active' "
            " AND p.status='active' AND o.status='active') FOR UPDATE",
            {"tenant": tenant, "org": current["org_id"],
             "project": current["project_id"], "drawing": current["drawing_id"]},
        )
        before = _row(cur.fetchone())
        if before is None:
            raise AnnotationStoreError("annotation_not_found", 404)
        if (int(before["version"]) != int(current["base_version"])
                or before["commit_sha"] != current["base_commit"]
                or before["tree_sha"] != current["base_tree"]):
            raise AnnotationStoreError("target_head_conflict", 409)
        cur.execute(
            "UPDATE annotation_targets SET version = version + 1, "
            "commit_sha = %(commit)s, tree_sha = %(tree)s, updated_at = NOW(), "
            "updated_by_binding_id = %(actor)s "
            "WHERE tenant_id = %(tenant)s AND org_id = %(org)s "
            "AND project_id = %(project)s AND drawing_id = %(drawing)s "
            "AND version = %(version)s AND commit_sha = %(base_commit)s "
            "AND tree_sha = %(base_tree)s "
            f"RETURNING {_TARGET_COLS}",
            {"commit": current["preview_commit"], "tree": current["preview_tree"],
             "actor": actor, "tenant": tenant, "org": current["org_id"],
             "project": current["project_id"], "drawing": current["drawing_id"],
             "version": current["base_version"], "base_commit": current["base_commit"],
             "base_tree": current["base_tree"]},
        )
        after = _row(cur.fetchone())
        if after is None:
            raise AnnotationStoreError("target_head_conflict", 409)
        accepted = _append_revision(
            cur, current, state="accepted", actor=actor, decision_key=key,
            applied_version=int(after["version"]), reason="accepted")
        _audit(cur, current, to_state="accepted", actor=actor,
               decision_key=key, before=before, after=after)
        return accepted, _applied_target_receipt(accepted, actor)


def reject(*, batch_id: str, tenant_id: str, actor_binding_id: str,
           decision_key: str, reason: str = "rejected") -> Dict[str, Any]:
    """Reject only while the target still matches the preview's source head."""
    batch_uuid, tenant, actor_input, key = _decision_inputs(
        batch_id, tenant_id, actor_binding_id, decision_key)
    with db.connection() as conn, conn.cursor() as cur:
        current = _lock_latest(cur, batch_uuid, tenant)
        if current is None:
            raise AnnotationStoreError("annotation_not_found", 404)
        actor = _binding(cur, tenant, str(actor_input))
        if current["state"] != "pending":
            if (current["state"] == "rejected" and current.get("decision_key") == key
                    and str(current.get("decided_by_binding_id")) == str(actor)):
                return current
            raise AnnotationStoreError("already_decided", 409)
        cur.execute(
            f"SELECT {_TARGET_COLS} FROM annotation_targets "
            "WHERE tenant_id = %(tenant)s AND org_id = %(org)s "
            "AND project_id = %(project)s AND drawing_id = %(drawing)s FOR UPDATE",
            {"tenant": tenant, "org": current["org_id"],
             "project": current["project_id"], "drawing": current["drawing_id"]},
        )
        head = _row(cur.fetchone())
        if (head is None or int(head["version"]) != int(current["base_version"])
                or head["commit_sha"] != current["base_commit"]
                or head["tree_sha"] != current["base_tree"]):
            raise AnnotationStoreError("target_head_conflict", 409)
        rejected = _append_revision(
            cur, current, state="rejected", actor=actor, decision_key=key,
            applied_version=None, reason=str(reason or "rejected")[:200])
        _audit(cur, current, to_state="rejected", actor=actor,
               decision_key=key, before=head, after=head)
        return rejected
