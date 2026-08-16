"""PostgreSQL authority for dormant project repository-edit transactions.

This module owns durable state only. It never reads a repository, runs Git,
dispatches work, or infers a Git result from database state.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime
from typing import Any, Mapping, Optional

from psycopg.types.json import Jsonb

from . import db


class RepositoryEditStoreError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_TERMINAL = frozenset({"published", "rejected", "conflicted", "failed", "superseded", "rolled_back"})


def _uuid(value: object, field: str) -> uuid.UUID:
    raw = str(value)
    try:
        parsed = uuid.UUID(raw)
    except (ValueError, TypeError, AttributeError) as exc:
        raise RepositoryEditStoreError(f"invalid_{field}") from exc
    if str(parsed) != raw:
        raise RepositoryEditStoreError(f"invalid_{field}")
    return parsed


def _sha(value: object, field: str) -> str:
    raw = str(value or "")
    if not _SHA.fullmatch(raw):
        raise RepositoryEditStoreError(f"invalid_{field}")
    return raw


def _digest(value: object, field: str) -> str:
    raw = str(value or "")
    if not _DIGEST.fullmatch(raw):
        raise RepositoryEditStoreError(f"invalid_{field}")
    return raw


def _positive(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RepositoryEditStoreError(f"invalid_{field}")
    return value


def _key(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 200:
        raise RepositoryEditStoreError("invalid_idempotency_key")
    return value


def _mapping(row: Any) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    return {key: (str(value) if isinstance(value, uuid.UUID) else value)
            for key, value in dict(row).items()}


def _receipt_values(receipt: Any, receipt_digest: object) -> dict[str, Any]:
    try:
        mapping = receipt.to_mapping()
        authority = receipt.authority_key
        witness = receipt.writer_witness
    except AttributeError as exc:
        raise RepositoryEditStoreError("validated_receipt_required") from exc
    if not isinstance(mapping, dict):
        raise RepositoryEditStoreError("validated_receipt_required")
    canonical = json.dumps(mapping, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    paths = json.dumps(mapping.get("changed_paths"), separators=(",", ":"), ensure_ascii=False)
    return {
        "edit": _uuid(receipt.edit_id, "edit_id"),
        "operation": receipt.operation,
        "source": _uuid(receipt.source_edit_id, "source_edit_id") if receipt.source_edit_id else None,
        "tenant": _uuid(authority.tenant_id, "tenant_id"),
        "organization": _uuid(authority.organization_id, "organization_id"),
        "project": _uuid(authority.project_id, "project_id"),
        "repo": _uuid(authority.repo_key, "repo_key"),
        "actor": _uuid(receipt.actor_binding_id, "actor_binding_id"),
        "lease": _uuid(witness.writer_lease_id, "writer_lease_id"),
        "generation": _positive(witness.writer_lease_generation, "writer_lease_generation"),
        "base": _sha(receipt.base_commit, "base_commit"),
        "head": _sha(receipt.staged_head_commit, "staged_head_commit"),
        "tree": _sha(receipt.staged_tree, "staged_tree"),
        "receipt_json": Jsonb(mapping),
        "receipt_digest": _digest(receipt_digest, "receipt_digest"),
        "paths_digest": hashlib.sha256(paths.encode("utf-8")).hexdigest(),
        "diff_digest": _digest(receipt.diff_digest, "diff_digest"),
        "instruction_digest": _digest(receipt.instruction_digest, "instruction_digest"),
        "key": _key(receipt.idempotency_key),
        "canonical": canonical,
    }


def _locked(cur: Any, edit_id: uuid.UUID) -> dict[str, Any]:
    cur.execute("SELECT * FROM project_repository_edits WHERE edit_id=%(edit)s FOR UPDATE",
                {"edit": edit_id})
    row = _mapping(cur.fetchone())
    if row is None:
        raise RepositoryEditStoreError("edit_not_found")
    return row


def _audit(cur: Any, row: Mapping[str, Any], prior: str, next_state: str,
           key: str, result: str = "success", reason: Optional[str] = None,
           approver: Optional[uuid.UUID] = None,
           writer_lease_id: Optional[uuid.UUID] = None,
           writer_lease_generation: Optional[int] = None) -> None:
    cur.execute(
        "INSERT INTO project_repository_edit_audit_events "
        "(edit_id,tenant_id,organization_id,project_id,repo_key,source_edit_id,"
        "prior_state,next_state,actor_binding_id,approver_binding_id,writer_lease_id,"
        "writer_lease_generation,base_commit,staged_head_commit,staged_tree,"
        "expected_main_commit,observed_private_ref_commit,observed_main_commit,"
        "observed_main_tree,changed_paths_digest,receipt_digest,idempotency_key,result,reason_code) "
        "VALUES (%(edit_id)s,%(tenant_id)s,%(organization_id)s,%(project_id)s,%(repo_key)s,"
        "%(source_edit_id)s,%(prior)s,%(next)s,%(actor_binding_id)s,%(approver)s,"
        "%(writer_lease_id)s,%(writer_lease_generation)s,%(base_commit)s,"
        "%(staged_head_commit)s,%(staged_tree)s,%(expected_main_commit)s,"
        "%(observed_private_ref_commit)s,%(observed_main_commit)s,%(observed_main_tree)s,"
        "%(changed_paths_digest)s,%(receipt_digest)s,%(key)s,%(result)s,%(reason)s)",
        {**row, "prior": prior, "next": next_state, "approver": approver,
         "writer_lease_id": writer_lease_id or row["writer_lease_id"],
         "writer_lease_generation": (
             writer_lease_generation
             if writer_lease_generation is not None
             else row["writer_lease_generation"]
         ),
         "key": key, "result": result, "reason": reason},
    )


def _publish_witness(cur: Any, edit_id: uuid.UUID) -> tuple[str, int]:
    cur.execute(
        "SELECT writer_lease_id,writer_lease_generation FROM "
        "project_repository_edit_audit_events WHERE edit_id=%(edit)s "
        "AND prior_state='awaiting_confirmation' AND next_state='publishing' "
        "ORDER BY event_id DESC LIMIT 1",
        {"edit": edit_id},
    )
    row = _mapping(cur.fetchone())
    if row is None:
        raise RepositoryEditStoreError("publish_witness_missing")
    return str(row["writer_lease_id"]), int(row["writer_lease_generation"])


def record_staged(receipt: Any, receipt_digest: object, *, expected_version: int,
                  transition_key: str) -> dict[str, Any]:
    values = _receipt_values(receipt, receipt_digest)
    if values["operation"] not in {"edit", "rollback"} or expected_version != 0:
        raise RepositoryEditStoreError("invalid_initial_transition")
    if transition_key != values["key"]:
        raise RepositoryEditStoreError("idempotency_conflict")

    def operation(conn):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT repo_key FROM project_repository_authorities WHERE "
                "tenant_id=%(tenant)s AND organization_id=%(organization)s "
                "AND project_id=%(project)s FOR UPDATE", values)
            authority = cur.fetchone()
            if authority is None or authority["repo_key"] != values["repo"]:
                raise RepositoryEditStoreError("authority_mismatch")
            if values["operation"] == "rollback":
                cur.execute(
                    "SELECT state,tenant_id,organization_id,project_id,repo_key "
                    "FROM project_repository_edits WHERE edit_id=%(source)s FOR UPDATE", values)
                source = cur.fetchone()
                if (source is None or source["state"] != "published"
                        or any(source[field] != values[target] for field, target in (
                            ("tenant_id", "tenant"), ("organization_id", "organization"),
                            ("project_id", "project"), ("repo_key", "repo")))):
                    raise RepositoryEditStoreError("rollback_source_mismatch")
            cur.execute(
                "INSERT INTO project_repository_edits "
                "(edit_id,operation,source_edit_id,tenant_id,organization_id,project_id,repo_key,"
                "actor_binding_id,writer_lease_id,writer_lease_generation,base_commit,"
                "staged_head_commit,staged_tree,expected_main_commit,receipt_json,receipt_digest,"
                "changed_paths_digest,diff_digest,instruction_digest,state,version,idempotency_key) "
                "VALUES (%(edit)s,%(operation)s,%(source)s,%(tenant)s,%(organization)s,%(project)s,"
                "%(repo)s,%(actor)s,%(lease)s,%(generation)s,%(base)s,%(head)s,%(tree)s,%(base)s,"
                "%(receipt_json)s,%(receipt_digest)s,%(paths_digest)s,%(diff_digest)s,"
                "%(instruction_digest)s,'staged',1,%(key)s) ON CONFLICT DO NOTHING", values)
            row = _locked(cur, values["edit"])
            immutable = {
                "operation": values["operation"], "source_edit_id": str(values["source"]) if values["source"] else None,
                "tenant_id": str(values["tenant"]), "organization_id": str(values["organization"]),
                "project_id": str(values["project"]), "repo_key": str(values["repo"]),
                "writer_lease_id": str(values["lease"]), "writer_lease_generation": values["generation"],
                "base_commit": values["base"], "staged_head_commit": values["head"],
                "staged_tree": values["tree"], "receipt_digest": values["receipt_digest"],
                "diff_digest": values["diff_digest"], "instruction_digest": values["instruction_digest"],
                "idempotency_key": values["key"], "state": "staged", "version": 1,
            }
            if any(row.get(field) != value for field, value in immutable.items()):
                raise RepositoryEditStoreError("idempotency_conflict")
            cur.execute("SELECT COUNT(*) AS n FROM project_repository_edit_audit_events WHERE edit_id=%(edit)s",
                        {"edit": values["edit"]})
            if int(cur.fetchone()["n"]) == 0:
                _audit(cur, row, "created", "staged", values["key"])
            return row
    return db.run_transaction(operation, isolation="serializable")


def _transition(edit_id: object, *, expected_state: str, next_state: str,
                expected_version: int, transition_key: str) -> dict[str, Any]:
    edit = _uuid(edit_id, "edit_id")
    key = _key(transition_key)
    version = _positive(expected_version, "expected_version")

    def operation(conn):
        with conn.cursor() as cur:
            row = _locked(cur, edit)
            if row["state"] == next_state and row["version"] == version + 1:
                cur.execute(
                    "SELECT idempotency_key FROM project_repository_edit_audit_events "
                    "WHERE edit_id=%(edit)s AND prior_state=%(prior)s AND next_state=%(next)s "
                    "ORDER BY event_id DESC LIMIT 1",
                    {"edit": edit, "prior": expected_state, "next": next_state})
                audit = cur.fetchone()
                if audit is not None and audit["idempotency_key"] == key:
                    return row
                raise RepositoryEditStoreError("idempotency_conflict")
            if row["state"] != expected_state or row["version"] != version:
                raise RepositoryEditStoreError("stale_transition")
            cur.execute(
                "UPDATE project_repository_edits SET state=%(next)s,version=version+1,updated_at=NOW() "
                "WHERE edit_id=%(edit)s AND state=%(prior)s AND version=%(version)s RETURNING *",
                {"next": next_state, "edit": edit, "prior": expected_state, "version": version})
            updated = _mapping(cur.fetchone())
            if updated is None:
                raise RepositoryEditStoreError("stale_transition")
            _audit(cur, updated, expected_state, next_state, key)
            return updated
    return db.run_transaction(operation, isolation="serializable")


def await_confirmation(edit_id: object, *, expected_version: int,
                       transition_key: str) -> dict[str, Any]:
    return _transition(edit_id, expected_state="staged", next_state="awaiting_confirmation",
                       expected_version=expected_version, transition_key=transition_key)


def put_confirmation(confirmation: Any, *, expected_edit_version: int,
                     transition_key: str) -> dict[str, Any]:
    try:
        authority = confirmation.authority_key
        witness = confirmation.writer_witness
    except AttributeError as exc:
        raise RepositoryEditStoreError("validated_confirmation_required") from exc
    values = {
        "confirmation": _uuid(confirmation.confirmation_id, "confirmation_id"),
        "receipt": _digest(confirmation.receipt_digest, "receipt_digest"),
        "approver": _uuid(confirmation.approver_binding_id, "approver_binding_id"),
        "tenant": _uuid(authority.tenant_id, "tenant_id"),
        "organization": _uuid(authority.organization_id, "organization_id"),
        "project": _uuid(authority.project_id, "project_id"),
        "repo": _uuid(authority.repo_key, "repo_key"),
        "edit": _uuid(confirmation.edit_id, "edit_id"),
        "lease": _uuid(witness.writer_lease_id, "writer_lease_id"),
        "generation": _positive(witness.writer_lease_generation, "writer_lease_generation"),
        "tree": _sha(confirmation.staged_tree, "staged_tree"),
        "issued": datetime.fromisoformat(confirmation.issued_at.replace("Z", "+00:00")),
        "expires": datetime.fromisoformat(confirmation.expires_at.replace("Z", "+00:00")),
        "key": _key(transition_key),
    }
    version = _positive(expected_edit_version, "expected_edit_version")

    def operation(conn):
        with conn.cursor() as cur:
            row = _locked(cur, values["edit"])
            expected = {
                "tenant_id": str(values["tenant"]), "organization_id": str(values["organization"]),
                "project_id": str(values["project"]), "repo_key": str(values["repo"]),
                "writer_lease_id": str(values["lease"]), "writer_lease_generation": values["generation"],
                "staged_tree": values["tree"], "receipt_digest": values["receipt"],
                "state": "awaiting_confirmation", "version": version,
            }
            if any(row.get(field) != value for field, value in expected.items()):
                raise RepositoryEditStoreError("confirmation_binding_mismatch")
            cur.execute(
                "INSERT INTO project_repository_edit_confirmations "
                "(confirmation_id,receipt_digest,approver_binding_id,tenant_id,organization_id,"
                "project_id,repo_key,edit_id,writer_lease_id,writer_lease_generation,staged_tree,"
                "issued_at,expires_at,idempotency_key) VALUES (%(confirmation)s,%(receipt)s,%(approver)s,%(tenant)s,"
                "%(organization)s,%(project)s,%(repo)s,%(edit)s,%(lease)s,%(generation)s,%(tree)s,"
                "%(issued)s,%(expires)s,%(key)s) ON CONFLICT DO NOTHING", values)
            cur.execute("SELECT * FROM project_repository_edit_confirmations WHERE confirmation_id=%(confirmation)s",
                        values)
            stored = _mapping(cur.fetchone())
            if stored is None or any(stored.get(field) != str(values[target]) for field, target in (
                ("receipt_digest", "receipt"), ("edit_id", "edit"), ("writer_lease_id", "lease"),
                ("staged_tree", "tree"), ("idempotency_key", "key"))):
                raise RepositoryEditStoreError("confirmation_conflict")
            return stored
    return db.run_transaction(operation, isolation="serializable")


def consume_for_publish(edit_id: object, confirmation_id: object, *,
                        expected_version: int, transition_key: str,
                        publish_lease_id: object | None = None,
                        publish_lease_generation: object | None = None,
                        receipt_digest: object | None = None) -> dict[str, Any]:
    edit = _uuid(edit_id, "edit_id")
    confirmation = _uuid(confirmation_id, "confirmation_id")
    version = _positive(expected_version, "expected_version")
    key = _key(transition_key)
    publish_lease = (
        _uuid(publish_lease_id, "publish_lease_id")
        if publish_lease_id is not None
        else None
    )
    publish_generation = (
        _positive(publish_lease_generation, "publish_lease_generation")
        if publish_lease_generation is not None
        else None
    )
    if (publish_lease is None) != (publish_generation is None):
        raise RepositoryEditStoreError("publish_witness_incomplete")
    expected_receipt = (_digest(receipt_digest, "receipt_digest")
                        if receipt_digest is not None else None)

    def operation(conn):
        with conn.cursor() as cur:
            row = _locked(cur, edit)
            if expected_receipt is not None and row["receipt_digest"] != expected_receipt:
                raise RepositoryEditStoreError("receipt_digest_mismatch")
            cur.execute("SELECT * FROM project_repository_edit_confirmations "
                        "WHERE confirmation_id=%(confirmation)s FOR UPDATE",
                        {"confirmation": confirmation})
            approval = _mapping(cur.fetchone())
            if approval is None or approval["edit_id"] != str(edit):
                raise RepositoryEditStoreError("confirmation_not_found")
            if row["state"] == "publishing" and approval["consumed_by_idempotency_key"] == key:
                if publish_lease is not None:
                    existing_lease, existing_generation = _publish_witness(cur, edit)
                    if (existing_lease != str(publish_lease)
                            or existing_generation != publish_generation):
                        raise RepositoryEditStoreError("publish_witness_mismatch")
                return row
            if (row["state"] != "awaiting_confirmation" or row["version"] != version
                    or approval["consumed_at"] is not None):
                raise RepositoryEditStoreError("confirmation_already_consumed")
            if approval["expires_at"] <= datetime.now(approval["expires_at"].tzinfo):
                raise RepositoryEditStoreError("confirmation_expired")
            for field in ("tenant_id", "organization_id", "project_id", "repo_key",
                          "writer_lease_id", "writer_lease_generation", "staged_tree",
                          "receipt_digest"):
                if approval[field] != row[field]:
                    raise RepositoryEditStoreError("confirmation_binding_mismatch")
            if (publish_generation is not None
                    and publish_generation <= int(row["writer_lease_generation"])):
                raise RepositoryEditStoreError("publish_generation_not_strictly_greater")
            cur.execute(
                "UPDATE project_repository_edit_confirmations SET consumed_at=NOW(),"
                "consumed_by_idempotency_key=%(key)s,consumed_edit_version=%(next_version)s "
                "WHERE confirmation_id=%(confirmation)s AND consumed_at IS NULL",
                {"key": key, "next_version": version + 1, "confirmation": confirmation})
            if cur.rowcount != 1:
                raise RepositoryEditStoreError("confirmation_already_consumed")
            cur.execute(
                "UPDATE project_repository_edits SET state='publishing',version=version+1,"
                "confirmation_id=%(confirmation)s,updated_at=NOW() WHERE edit_id=%(edit)s "
                "AND state='awaiting_confirmation' AND version=%(version)s RETURNING *",
                {"confirmation": confirmation, "edit": edit, "version": version})
            updated = _mapping(cur.fetchone())
            if updated is None:
                raise RepositoryEditStoreError("stale_transition")
            _audit(cur, updated, "awaiting_confirmation", "publishing", key,
                   approver=uuid.UUID(approval["approver_binding_id"]),
                   writer_lease_id=publish_lease,
                   writer_lease_generation=publish_generation)
            return updated
    return db.run_transaction(operation, isolation="serializable")


def settle_publish(edit_id: object, *, private_ref_commit: object, main_commit: object,
                   main_tree: object, expected_version: int, transition_key: str,
                   recovery_reason_code: Optional[str] = None,
                   publish_lease_id: object | None = None,
                   publish_lease_generation: object | None = None,
                   recovery_lease_id: object | None = None,
                   recovery_lease_generation: object | None = None) -> dict[str, Any]:
    edit = _uuid(edit_id, "edit_id")
    private = _sha(private_ref_commit, "private_ref_commit")
    main = _sha(main_commit, "main_commit")
    tree = _sha(main_tree, "main_tree")
    version = _positive(expected_version, "expected_version")
    key = _key(transition_key)
    publish_lease = (_uuid(publish_lease_id, "publish_lease_id")
                     if publish_lease_id is not None else None)
    publish_generation = (_positive(publish_lease_generation, "publish_lease_generation")
                          if publish_lease_generation is not None else None)
    recovery_lease = (_uuid(recovery_lease_id, "recovery_lease_id")
                      if recovery_lease_id is not None else None)
    recovery_generation = (_positive(recovery_lease_generation, "recovery_lease_generation")
                           if recovery_lease_generation is not None else None)
    if (publish_lease is None) != (publish_generation is None):
        raise RepositoryEditStoreError("publish_witness_incomplete")
    if (recovery_lease is None) != (recovery_generation is None):
        raise RepositoryEditStoreError("recovery_witness_incomplete")
    if recovery_reason_code is None and recovery_lease is not None:
        raise RepositoryEditStoreError("recovery_witness_unexpected")

    def operation(conn):
        with conn.cursor() as cur:
            row = _locked(cur, edit)
            recorded_lease, recorded_generation = _publish_witness(cur, edit)
            if publish_lease is not None and (
                    recorded_lease != str(publish_lease)
                    or recorded_generation != publish_generation):
                raise RepositoryEditStoreError("publish_witness_mismatch")
            if recovery_generation is not None and recovery_generation < recorded_generation:
                raise RepositoryEditStoreError("recovery_generation_stale")
            if row["state"] == "published" and recovery_reason_code is not None:
                if (private != row["staged_head_commit"]
                        or main != row["observed_main_commit"]
                        or tree != row["observed_main_tree"]):
                    raise RepositoryEditStoreError("recovery_observation_mismatch")
                return row
            if row["state"] == "published" and row["observed_main_commit"] == main:
                cur.execute(
                    "SELECT idempotency_key FROM project_repository_edit_audit_events "
                    "WHERE edit_id=%(edit)s AND next_state='published' ORDER BY event_id DESC LIMIT 1",
                    {"edit": edit})
                audit = cur.fetchone()
                if audit is not None and audit["idempotency_key"] == key:
                    return row
                raise RepositoryEditStoreError("idempotency_conflict")
            if row["state"] != "publishing" or row["version"] != version:
                raise RepositoryEditStoreError("stale_transition")
            success = private == row["staged_head_commit"] and main == row["staged_head_commit"] and tree == row["staged_tree"]
            retryable = (private == row["staged_head_commit"]
                         and main == row["base_commit"])
            next_state = "published" if success else ("publishing" if retryable else "conflicted")
            reason = (recovery_reason_code if success else
                      ("main_not_advanced" if retryable else "git_witness_mismatch"))
            cur.execute(
                "UPDATE project_repository_edits SET state=%(state)s,version=version+1,"
                "observed_private_ref_commit=%(private)s,observed_main_commit=%(main)s,"
                "observed_main_tree=%(tree)s,published_at=CASE WHEN %(success)s THEN NOW() ELSE NULL END,"
                "recovery_reason_code=%(reason)s,recovered_at=CASE WHEN %(reason)s IS NULL THEN NULL ELSE NOW() END,"
                "updated_at=NOW() WHERE edit_id=%(edit)s AND state='publishing' AND version=%(version)s RETURNING *",
                {"state": next_state, "private": private, "main": main, "tree": tree,
                 "success": success, "reason": reason, "edit": edit, "version": version})
            updated = _mapping(cur.fetchone())
            if updated is None:
                raise RepositoryEditStoreError("stale_transition")
            _audit(cur, updated, "publishing", next_state, key,
                   result="success" if success else "conflict", reason=reason,
                   writer_lease_id=recovery_lease or publish_lease,
                   writer_lease_generation=(
                       recovery_generation
                       if recovery_generation is not None
                       else publish_generation
                   ))
            if success and row["operation"] == "rollback":
                cur.execute(
                    "UPDATE project_repository_edits SET state='rolled_back',version=version+1,updated_at=NOW() "
                    "WHERE edit_id=%(source)s AND state='published' RETURNING *",
                    {"source": uuid.UUID(row["source_edit_id"])})
                source = _mapping(cur.fetchone())
                if source is None:
                    raise RepositoryEditStoreError("rollback_source_drift")
                _audit(cur, source, "published", "rolled_back", key)
            return updated
    return db.run_transaction(operation, isolation="serializable")


def recover_publish(edit_id: object, *, private_ref_commit: object, main_commit: object,
                    main_tree: object, expected_version: int, transition_key: str,
                    reason_code: str, recovery_lease_id: object | None = None,
                    recovery_lease_generation: object | None = None) -> dict[str, Any]:
    if not isinstance(reason_code, str) or not re.fullmatch(r"[a-z0-9_]{1,64}", reason_code):
        raise RepositoryEditStoreError("invalid_recovery_reason")
    return settle_publish(
        edit_id, private_ref_commit=private_ref_commit, main_commit=main_commit,
        main_tree=main_tree, expected_version=expected_version,
        transition_key=transition_key, recovery_reason_code=reason_code,
        recovery_lease_id=recovery_lease_id,
        recovery_lease_generation=recovery_lease_generation,
    )
