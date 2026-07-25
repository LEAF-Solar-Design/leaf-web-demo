"""Org-scoped persistence for the canonical Project/Job entity.

THE tenant-isolation boundary (v1, application-layer): every read function takes
``org_id`` as its required first argument and binds ``WHERE org_id = %(org_id)s``.
A read for a resource not owned by the caller's org returns ``None`` / ``[]`` — the
API turns a missing single resource into HTTP 404 (never 403: a 403 leaks existence).

``tests/test_store_guard.py`` statically asserts that every SELECT in this module
carries an org_id predicate and that every read function's first parameter is
``org_id`` — so a future read cannot silently skip org scoping.

Ports cadwalk-studio/src/lib/tenancy/store.ts (loadDeployments/findDeployment/
saveDeployment) to org-scoped list/get/create.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from psycopg.types.json import Jsonb
from psycopg.errors import UniqueViolation

from .db import connection, cursor, run_transaction
from .models import (
    BuiltTool,
    CanonicalHash,
    DrawingArtifact,
    DrawingVersion,
    HistoryOperation,
    IdentityBinding,
    Job,
    Org,
    Project,
    SolveRecord,
    AUTHORITY_MODES,
    canonical_hash,
    verify_canonical_hash,
    new_uuid,
)

OrgId = uuid.UUID
ProjectId = uuid.UUID

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ACCOUNT_UPLOAD_ID_RE = re.compile(r"^u-[0-9a-f]{10}$")


def is_account_upload_source_id(value: object) -> bool:
    """Accept only current canonical UUID receipts or the legacy upload form."""
    if not isinstance(value, str):
        return False
    if _ACCOUNT_UPLOAD_ID_RE.fullmatch(value) is not None:
        return True
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


class DrawingImportUnavailable(ValueError):
    """The project or source cannot be disclosed to this caller."""


class DrawingImportForbidden(ValueError):
    """The verified platform actor cannot mutate this project."""


class DrawingImportConflict(ValueError):
    """An idempotency key or immutable source version conflicts."""

# --------------------------------------------------------------------------- #
# writes
# --------------------------------------------------------------------------- #

def create_org(name: str, tier: str = "hosted_starter",
               org_id: Optional[uuid.UUID] = None) -> Org:
    oid = org_id or new_uuid()
    with cursor() as cur:
        cur.execute(
            "INSERT INTO orgs (org_id, name, tier) VALUES (%(org_id)s, %(name)s, %(tier)s) "
            "RETURNING org_id, name, tier, status, created_at, offboarded_at, "
            "deleted_at, purge_requested_at, purge_completed_at",
            {"org_id": oid, "name": name, "tier": tier},
        )
        return Org.from_row(cur.fetchone())


def set_org_tier(org_id: uuid.UUID, tier: str) -> Optional[Org]:
    """Billing-driven stored-tier update (contract/BILLING.md §3).

    Guarded to ACTIVE rows in the same statement (TOCTOU: an org offboarded
    between the caller's read and this write is left untouched — offboarding
    state must never be resurrected into a billable tier). Returns the updated
    row, or ``None`` when no active row matched.
    """
    with cursor() as cur:
        cur.execute(
            "UPDATE orgs SET tier = %(tier)s "
            "WHERE org_id = %(org_id)s AND status = 'active' "
            "RETURNING org_id, name, tier, status, created_at, offboarded_at",
            {"org_id": org_id, "tier": tier},
        )
        row = cur.fetchone()
        return Org.from_row(row) if row else None


def create_org_with_identity(name: str, external_authority: str, external_subject: str, *,
                             tier: str = "hosted_starter") -> Org:
    """Atomically bootstrap an org and its first verified identity binding.

    A concurrent request for the same external subject rolls the org insert back
    instead of leaving an unreachable tenant row behind.
    """
    org_id, binding_id, user_id = new_uuid(), new_uuid(), new_uuid()
    try:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO orgs (org_id, name, tier) VALUES (%(org_id)s, %(name)s, %(tier)s) "
                    "RETURNING org_id, name, tier, status, created_at, offboarded_at, "
                    "deleted_at, purge_requested_at, purge_completed_at",
                    {"org_id": org_id, "name": name, "tier": tier},
                )
                org_row = cur.fetchone()
                cur.execute(
                    "INSERT INTO identity_bindings (binding_id, external_authority, external_subject, "
                    "platform_tenant_id, platform_user_id, role) VALUES "
                    "(%(binding_id)s, %(authority)s, %(subject)s, %(org_id)s, %(user_id)s, 'owner')",
                    {"binding_id": binding_id, "authority": external_authority,
                     "subject": external_subject, "org_id": org_id, "user_id": user_id},
                )
                return Org.from_row(org_row)
    except UniqueViolation as exc:
        raise ValueError("verified external subject already has a platform identity binding") from exc


def create_project(
    org_id: uuid.UUID,
    name: str,
    *,
    authority_mode: Optional[str] = None,
) -> Project:
    if authority_mode is not None and authority_mode not in AUTHORITY_MODES:
        raise ValueError(f"authority_mode must be one of {AUTHORITY_MODES}")
    project_id = new_uuid()
    with cursor() as cur:
        cur.execute(
            "INSERT INTO projects (project_id, org_id, name) "
            "VALUES (%(project_id)s, %(org_id)s, %(name)s) "
            "RETURNING project_id, org_id, name, status, created_at, updated_at, "
            "deleted_at, purge_requested_at, purge_completed_at",
            {"project_id": project_id, "org_id": org_id, "name": name},
        )
        project = Project.from_row(cur.fetchone())
        if authority_mode is not None:
            cur.execute(
                "INSERT INTO project_authority_modes "
                "(org_id, project_id, authority_mode, selected_by) "
                "VALUES (%(org_id)s, %(project_id)s, %(authority_mode)s, 'server')",
                {
                    "org_id": org_id,
                    "project_id": project_id,
                    "authority_mode": authority_mode,
                },
            )
        return project


# --------------------------------------------------------------------------- #
# Server-owned authority and verified external identity binding
# --------------------------------------------------------------------------- #
def set_tenant_authority_mode(org_id: uuid.UUID, authority_mode: str) -> None:
    """Server control-plane write; requests have no route to select this value."""
    if authority_mode not in AUTHORITY_MODES:
        raise ValueError(f"authority_mode must be one of {AUTHORITY_MODES}")
    with cursor() as cur:
        cur.execute(
            "INSERT INTO tenant_authority_modes (org_id, authority_mode, selected_by) "
            "VALUES (%(org_id)s, %(authority_mode)s, 'server') "
            "ON CONFLICT (org_id) DO UPDATE SET authority_mode = EXCLUDED.authority_mode, "
            "selected_by = 'server', selected_at = NOW()",
            {"org_id": org_id, "authority_mode": authority_mode},
        )


def set_project_authority_mode(org_id: uuid.UUID, project_id: uuid.UUID, authority_mode: str) -> None:
    if authority_mode not in AUTHORITY_MODES:
        raise ValueError(f"authority_mode must be one of {AUTHORITY_MODES}")
    with cursor() as cur:
        cur.execute(
            "INSERT INTO project_authority_modes (org_id, project_id, authority_mode, selected_by) "
            "VALUES (%(org_id)s, %(project_id)s, %(authority_mode)s, 'server') "
            "ON CONFLICT (org_id, project_id) DO UPDATE SET authority_mode = EXCLUDED.authority_mode, "
            "selected_by = 'server', selected_at = NOW()",
            {"org_id": org_id, "project_id": project_id, "authority_mode": authority_mode},
        )


def get_authority_mode(org_id: uuid.UUID, project_id: uuid.UUID) -> str:
    """Resolve exactly one authority; absent cutover deliberately means legacy."""
    with cursor() as cur:
        cur.execute(
            "SELECT COALESCE("
            "(SELECT authority_mode FROM project_authority_modes "
            " WHERE org_id = %(org_id)s AND project_id = %(project_id)s), "
            "(SELECT authority_mode FROM tenant_authority_modes WHERE org_id = %(org_id)s), "
            "'legacy_sqlite') AS authority_mode",
            {"org_id": org_id, "project_id": project_id},
        )
        row = cur.fetchone()
    return row["authority_mode"]


def _require_postgres_authority(org_id: uuid.UUID, project_id: uuid.UUID) -> None:
    if get_authority_mode(org_id, project_id) != "postgres_canonical":
        raise RuntimeError("canonical ledger is unavailable while authority is legacy_sqlite")


def create_identity_binding(org_id: uuid.UUID, external_authority: str, external_subject: str,
                            platform_user_id: Optional[uuid.UUID] = None, *,
                            role: str = "owner") -> IdentityBinding:
    """Bind a verified external subject to one platform tenant/user.

    The caller supplies this only from server-verified authentication code; there
    is intentionally no browser-facing binding API.
    """
    if not external_authority or not external_subject:
        raise ValueError("external authority and subject are required")
    if role not in {"owner", "editor", "reviewer", "read_only"}:
        raise ValueError("role must be owner, editor, reviewer, or read_only")
    with cursor() as cur:
        cur.execute(
            "INSERT INTO identity_bindings (binding_id, external_authority, external_subject, "
            "platform_tenant_id, platform_user_id, role) VALUES (%(binding_id)s, %(authority)s, "
            "%(subject)s, %(org_id)s, %(user_id)s, %(role)s) "
            "ON CONFLICT (external_authority, external_subject) WHERE status = 'active' "
            "DO NOTHING "
            "RETURNING binding_id, external_authority, external_subject, platform_tenant_id, "
            "platform_user_id, status, created_at",
            {"binding_id": new_uuid(), "authority": external_authority,
             "subject": external_subject, "org_id": org_id,
              "user_id": platform_user_id or new_uuid(), "role": role},
        )
        row = cur.fetchone()
    if row is not None:
        return IdentityBinding.from_row(row)
    existing = resolve_active_identity_binding(external_authority, external_subject)
    if existing is None:  # pragma: no cover - concurrent revoke edge
        raise RuntimeError("identity binding conflict could not be resolved")
    if existing.platform_tenant_id != org_id:
        raise ValueError("verified external subject is already bound to another platform tenant")
    return existing


def _lookup_active_identity_binding(external_authority: str, external_subject: str) -> Optional[IdentityBinding]:
    with cursor() as cur:
        cur.execute(
            "SELECT binding_id, external_authority, external_subject, platform_tenant_id, "
            "platform_user_id, status, created_at FROM identity_bindings "
            "WHERE external_authority = %(authority)s AND external_subject = %(subject)s "
            "AND status = 'active'",
            {"authority": external_authority, "subject": external_subject},
        )
        row = cur.fetchone()
    return IdentityBinding.from_row(row) if row else None


def resolve_active_identity_binding(external_authority: str, external_subject: str) -> Optional[IdentityBinding]:
    """Identity lookup used only after JWT verification; never accepts org hints."""
    return _lookup_active_identity_binding(external_authority, external_subject)


def active_identity_role(org_id: uuid.UUID, binding_id: uuid.UUID) -> Optional[str]:
    """Return an active binding's role only when it remains scoped to ``org_id``."""
    with cursor() as cur:
        cur.execute(
            "SELECT role FROM identity_bindings WHERE platform_tenant_id = %(org_id)s "
            "AND binding_id = %(binding_id)s AND status = 'active'",
            {"org_id": org_id, "binding_id": binding_id},
        )
        row = cur.fetchone()
    return row["role"] if row else None


# --------------------------------------------------------------------------- #
# Canonical immutable history / solve ledger.  These writers are only reachable
# after the server control plane selected postgres_canonical for the project.
# --------------------------------------------------------------------------- #
def append_history_operation(org_id: uuid.UUID, project_id: uuid.UUID, operation_type: str,
                             payload: Dict[str, Any], idempotency_key: str, *,
                             parent_operation_ids: Optional[List[uuid.UUID]] = None,
                             branch_name: Optional[str] = None) -> HistoryOperation:
    _require_postgres_authority(org_id, project_id)
    if not idempotency_key:
        raise ValueError("idempotency_key is required")
    normalized_parents = sorted({str(parent_id) for parent_id in parent_operation_ids or []})
    # Topology/ref semantics are intentionally compared separately from the
    # established content hash contract (operation type + payload).
    request_semantics = {"parentOperationIds": normalized_parents, "branchName": branch_name}
    digest_input = {"operationType": operation_type, "payload": payload}
    digest = canonical_hash("history-operation", digest_input)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO history_operations (operation_id, org_id, project_id, operation_type, "
                "payload, idempotency_key, hash_algorithm, hash_canonicalization, hash_domain, hash_value) "
                "VALUES (%(id)s, %(org_id)s, %(project_id)s, %(type)s, %(payload)s, %(key)s, "
                "%(algorithm)s, %(canonicalization)s, %(domain)s, %(value)s) "
                "ON CONFLICT (org_id, project_id, idempotency_key) DO NOTHING "
                "RETURNING operation_id, org_id, project_id, operation_type, payload, idempotency_key, "
                "hash_algorithm, hash_canonicalization, hash_domain, hash_value",
                {"id": new_uuid(), "org_id": org_id, "project_id": project_id, "type": operation_type,
                 "payload": Jsonb(payload), "key": idempotency_key, **digest.to_dict()},
            )
            row = cur.fetchone()
            created = row is not None
            if not created:
                cur.execute(
                    "SELECT operation_id, org_id, project_id, operation_type, payload, idempotency_key, "
                    "hash_algorithm, hash_canonicalization, hash_domain, hash_value FROM history_operations "
                    "WHERE org_id = %(org_id)s AND project_id = %(project_id)s AND idempotency_key = %(key)s",
                    {"org_id": org_id, "project_id": project_id, "key": idempotency_key},
                )
                row = cur.fetchone()
            operation = HistoryOperation.from_row(row)
            if not created:
                if operation.content_hash.to_dict() != digest.to_dict():
                    raise ValueError("idempotency key already exists with different history input")
                cur.execute(
                    "SELECT parent_operation_id FROM history_edges WHERE org_id = %(org_id)s "
                    "AND project_id = %(project_id)s AND child_operation_id = %(operation_id)s "
                    "ORDER BY parent_operation_id",
                    {"org_id": org_id, "project_id": project_id, "operation_id": operation.operation_id},
                )
                stored_parents = [str(item["parent_operation_id"]) for item in cur.fetchall()]
                cur.execute(
                    "SELECT ref_name FROM branch_refs WHERE org_id = %(org_id)s AND project_id = %(project_id)s "
                    "AND operation_id = %(operation_id)s ORDER BY sequence DESC LIMIT 1",
                    {"org_id": org_id, "project_id": project_id, "operation_id": operation.operation_id},
                )
                stored_ref = cur.fetchone()
                stored_semantics = {"parentOperationIds": stored_parents,
                                    "branchName": stored_ref["ref_name"] if stored_ref else None}
                if stored_semantics != request_semantics:
                    raise ValueError("idempotency key already exists with different history topology or branch")
                return operation
            for parent_id in normalized_parents:
                cur.execute(
                    "INSERT INTO history_edges (edge_id, org_id, project_id, parent_operation_id, child_operation_id) "
                    "SELECT %(edge_id)s, %(org_id)s, %(project_id)s, operation_id, %(child)s "
                    "FROM history_operations WHERE org_id = %(org_id)s AND project_id = %(project_id)s "
                    "AND operation_id = %(parent)s",
                    {"edge_id": new_uuid(), "org_id": org_id, "project_id": project_id,
                     "parent": parent_id, "child": operation.operation_id},
                )
                if cur.rowcount != 1:
                    raise ValueError("parent operation is not owned by this project")
            if branch_name:
                cur.execute(
                    "SELECT 1 FROM projects WHERE org_id = %(org_id)s AND project_id = %(project_id)s "
                    "FOR UPDATE",
                    {"org_id": org_id, "project_id": project_id},
                )
                cur.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence FROM branch_refs "
                    "WHERE org_id = %(org_id)s AND project_id = %(project_id)s AND ref_name = %(name)s",
                    {"org_id": org_id, "project_id": project_id, "name": branch_name},
                )
                next_sequence = cur.fetchone()["sequence"]
                cur.execute(
                    "INSERT INTO branch_refs (ref_id, org_id, project_id, ref_name, sequence, operation_id) "
                    "VALUES (%(id)s, %(org_id)s, %(project_id)s, %(name)s, %(sequence)s, %(operation_id)s)",
                    {"id": new_uuid(), "org_id": org_id, "project_id": project_id, "name": branch_name,
                     "sequence": next_sequence, "operation_id": operation.operation_id},
                )
            _insert_outbox(cur, org_id, project_id, "history_operation", operation.operation_id,
                           "history.operation.appended", {"operationId": str(operation.operation_id)})
            return operation


def append_solve_record(org_id: uuid.UUID, project_id: uuid.UUID, payload: Dict[str, Any],
                        idempotency_key: str) -> SolveRecord:
    _require_postgres_authority(org_id, project_id)
    if not idempotency_key:
        raise ValueError("idempotency_key is required")
    digest = canonical_hash("solve-record", payload)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO solve_records (solve_id, org_id, project_id, payload, idempotency_key, "
                "hash_algorithm, hash_canonicalization, hash_domain, hash_value) "
                "VALUES (%(id)s, %(org_id)s, %(project_id)s, %(payload)s, %(key)s, %(algorithm)s, "
                "%(canonicalization)s, %(domain)s, %(value)s) "
                "ON CONFLICT (org_id, project_id, idempotency_key) DO NOTHING "
                "RETURNING solve_id, org_id, project_id, payload, idempotency_key, hash_algorithm, "
                "hash_canonicalization, hash_domain, hash_value",
                {"id": new_uuid(), "org_id": org_id, "project_id": project_id,
                 "payload": Jsonb(payload), "key": idempotency_key, **digest.to_dict()},
            )
            row = cur.fetchone()
            created = row is not None
            if not created:
                cur.execute(
                    "SELECT solve_id, org_id, project_id, payload, idempotency_key, hash_algorithm, "
                    "hash_canonicalization, hash_domain, hash_value FROM solve_records "
                    "WHERE org_id = %(org_id)s AND project_id = %(project_id)s AND idempotency_key = %(key)s",
                    {"org_id": org_id, "project_id": project_id, "key": idempotency_key},
                )
                row = cur.fetchone()
            solve = SolveRecord.from_row(row)
            if not created and solve.content_hash.to_dict() != digest.to_dict():
                raise ValueError("idempotency key already exists with different solve input")
            if created:
                _insert_outbox(cur, org_id, project_id, "solve_record", solve.solve_id,
                               "solve.recorded", {"solveId": str(solve.solve_id)})
            return solve


def _insert_outbox(cur: Any, org_id: uuid.UUID, project_id: uuid.UUID, aggregate_type: str,
                   aggregate_id: uuid.UUID, event_type: str, payload: Dict[str, Any]) -> None:
    cur.execute(
        "INSERT INTO outbox_entries (outbox_id, org_id, project_id, aggregate_type, aggregate_id, "
        "event_type, payload) VALUES (%(id)s, %(org_id)s, %(project_id)s, %(aggregate_type)s, "
        "%(aggregate_id)s, %(event_type)s, %(payload)s) ON CONFLICT DO NOTHING",
        {"id": new_uuid(), "org_id": org_id, "project_id": project_id,
         "aggregate_type": aggregate_type, "aggregate_id": aggregate_id,
         "event_type": event_type, "payload": Jsonb(payload)},
    )


def _get_history_operation(org_id: uuid.UUID, operation_id: uuid.UUID) -> Optional[HistoryOperation]:
    with cursor() as cur:
        cur.execute(
            "SELECT operation_id, org_id, project_id, operation_type, payload, idempotency_key, "
            "hash_algorithm, hash_canonicalization, hash_domain, hash_value FROM history_operations "
            "WHERE org_id = %(org_id)s AND operation_id = %(operation_id)s",
            {"org_id": org_id, "operation_id": operation_id},
        )
        row = cur.fetchone()
    return HistoryOperation.from_row(row) if row else None


def verify_history_operation(org_id: uuid.UUID, operation_id: uuid.UUID) -> bool:
    operation = _get_history_operation(org_id, operation_id)
    return operation is not None and verify_canonical_hash(
        "history-operation",
        {"operationType": operation.operation_type, "payload": operation.payload},
        operation.content_hash.to_dict(),
    )


def _get_solve_record(org_id: uuid.UUID, solve_id: uuid.UUID) -> Optional[SolveRecord]:
    with cursor() as cur:
        cur.execute(
            "SELECT solve_id, org_id, project_id, payload, idempotency_key, hash_algorithm, "
            "hash_canonicalization, hash_domain, hash_value FROM solve_records "
            "WHERE org_id = %(org_id)s AND solve_id = %(solve_id)s",
            {"org_id": org_id, "solve_id": solve_id},
        )
        row = cur.fetchone()
    return SolveRecord.from_row(row) if row else None


def verify_solve_record(org_id: uuid.UUID, solve_id: uuid.UUID) -> bool:
    solve = _get_solve_record(org_id, solve_id)
    return solve is not None and verify_canonical_hash(
        "solve-record", solve.payload, solve.content_hash.to_dict())


def create_drawing_artifact(org_id: uuid.UUID, project_id: uuid.UUID,
                            name: str = "Primary drawing") -> DrawingArtifact:
    if not name or len(name) > 200:
        raise ValueError("drawing name must contain 1 to 200 characters")
    with cursor() as cur:
        cur.execute(
            "INSERT INTO drawing_artifacts (drawing_id, project_id, org_id, name) "
            "SELECT %(drawing_id)s, p.project_id, p.org_id, %(name)s FROM projects p "
            "WHERE p.project_id = %(project_id)s AND p.org_id = %(org_id)s "
            "AND p.deleted_at IS NULL "
            "ON CONFLICT (project_id, name) DO UPDATE SET name = EXCLUDED.name "
            "RETURNING drawing_id, project_id, org_id, name, status, created_at",
            {"drawing_id": new_uuid(), "project_id": project_id, "org_id": org_id,
             "name": name},
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError("project not found")
        return DrawingArtifact.from_row(row)


def create_drawing_version(org_id: uuid.UUID, project_id: uuid.UUID, *,
                           drawing_id: Optional[uuid.UUID] = None,
                           oss_object: Optional[str] = None,
                           intake_ref: Optional[str] = None,
                           created_by: Optional[str] = None) -> DrawingVersion:
    """Append the next monotonic version to a project's chain (single-writer)."""
    with connection() as conn:
        with conn.cursor() as cur:
            if drawing_id is None:
                cur.execute(
                    "SELECT drawing_id FROM drawing_artifacts "
                    "WHERE project_id = %(project_id)s AND org_id = %(org_id)s "
                    "AND status = 'active' ORDER BY created_at, drawing_id LIMIT 1",
                    {"project_id": project_id, "org_id": org_id},
                )
                artifact = cur.fetchone()
                if artifact is None:
                    drawing_id = new_uuid()
                    cur.execute(
                        "INSERT INTO drawing_artifacts "
                        "(drawing_id, project_id, org_id, name) "
                        "SELECT %(drawing_id)s, project_id, org_id, 'Primary drawing' "
                        "FROM projects WHERE project_id = %(project_id)s "
                        "AND org_id = %(org_id)s AND deleted_at IS NULL RETURNING drawing_id",
                        {"drawing_id": drawing_id, "project_id": project_id, "org_id": org_id},
                    )
                    if cur.fetchone() is None:
                        raise ValueError("project not found")
                else:
                    drawing_id = artifact["drawing_id"]
            else:
                cur.execute(
                    "SELECT drawing_id FROM drawing_artifacts WHERE drawing_id = %(drawing_id)s "
                    "AND project_id = %(project_id)s AND org_id = %(org_id)s AND status = 'active'",
                    {"drawing_id": drawing_id, "project_id": project_id, "org_id": org_id},
                )
                if cur.fetchone() is None:
                    raise ValueError("drawing artifact not found")
            # next seq, scoped to the owning org (guards against cross-org project_id)
            cur.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM drawing_versions "
                "WHERE drawing_id = %(drawing_id)s AND project_id = %(project_id)s "
                "AND org_id = %(org_id)s",
                {"drawing_id": drawing_id, "project_id": project_id, "org_id": org_id},
            )
            next_seq = cur.fetchone()["next_seq"]
            cur.execute(
                "INSERT INTO drawing_versions "
                "(version_id, drawing_id, project_id, org_id, seq, oss_object, intake_ref, created_by) "
                "VALUES (%(version_id)s, %(drawing_id)s, %(project_id)s, %(org_id)s, %(seq)s, "
                "%(oss_object)s, %(intake_ref)s, %(created_by)s) "
                "RETURNING version_id, drawing_id, project_id, org_id, seq, oss_object, intake_ref, "
                "created_by, provenance, idempotency_key, created_at, deleted_at, "
                "purge_requested_at, purge_completed_at",
                {
                    "version_id": new_uuid(), "drawing_id": drawing_id,
                    "project_id": project_id, "org_id": org_id,
                    "seq": next_seq, "oss_object": oss_object, "intake_ref": intake_ref,
                    "created_by": created_by,
                },
            )
            return DrawingVersion.from_row(cur.fetchone())


def import_ready_account_upload(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    *,
    source_drawing_id: str,
    source_version: int,
    name: str,
    idempotency_key: str,
    actor_binding_id: uuid.UUID,
) -> tuple[DrawingVersion, bool]:
    """Adopt one server-verified ready account upload into a canonical project.

    The request supplies only source identity and a display name. Object refs,
    digests, tenant identity, actor identity, and provenance all come from
    server-owned PostgreSQL rows. A ready upload is the current proof that both
    the immutable object and fenced intake cache were published. Migration 0016
    does not persist an intake digest, so this contract records that exact proof
    boundary instead of inventing one.
    """
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 200:
        raise DrawingImportConflict("idempotency key must contain 1 to 200 characters")
    if not name or len(name) > 200:
        raise DrawingImportConflict("drawing name must contain 1 to 200 characters")
    if isinstance(source_version, bool) or source_version < 1:
        raise DrawingImportConflict("source version must be a positive integer")
    if not is_account_upload_source_id(source_drawing_id):
        raise DrawingImportUnavailable(
            "project or source account upload is unavailable")

    # New account upload receipts are already canonical UUIDs. Legacy account
    # receipts used the external ``u-<10 hex>`` namespace, so map those to one
    # stable tenant-scoped canonical UUID without changing their source lookup.
    canonical_drawing_id = (
        uuid.uuid5(org_id, f"account_upload:{source_drawing_id}")
        if _ACCOUNT_UPLOAD_ID_RE.fullmatch(source_drawing_id)
        else uuid.UUID(source_drawing_id)
    )

    fingerprint_input = {
        "actor_binding_id": str(actor_binding_id),
        "name": name,
        "org_id": str(org_id),
        "project_id": str(project_id),
        "source_drawing_id": str(source_drawing_id),
        "source_version": int(source_version),
    }
    fingerprint = hashlib.sha256(json.dumps(
        fingerprint_input, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()

    columns = (
        "version_id, drawing_id, project_id, org_id, seq, oss_object, intake_ref, "
        "created_by, provenance, idempotency_key, created_at, deleted_at, "
        "purge_requested_at, purge_completed_at"
    )

    def operation(conn):
        with conn.cursor() as cur:
            # The project row is the serialization point for concurrent imports.
            # Authority is server-owned and cannot be selected by request data.
            cur.execute(
                "SELECT p.project_id FROM projects p "
                "WHERE p.org_id = %(org_id)s AND p.project_id = %(project_id)s "
                "AND p.status = 'active' AND p.deleted_at IS NULL "
                "AND COALESCE((SELECT authority_mode FROM project_authority_modes "
                "WHERE org_id = p.org_id AND project_id = p.project_id), "
                "(SELECT authority_mode FROM tenant_authority_modes WHERE org_id = p.org_id), "
                "'legacy_sqlite') = 'postgres_canonical' FOR UPDATE",
                {"org_id": org_id, "project_id": project_id},
            )
            if cur.fetchone() is None:
                raise DrawingImportUnavailable(
                    "project or source account upload is unavailable")

            cur.execute(
                "SELECT role FROM identity_bindings "
                "WHERE platform_tenant_id = %(org_id)s AND binding_id = %(binding_id)s "
                "AND status = 'active'",
                {"org_id": org_id, "binding_id": actor_binding_id},
            )
            actor = cur.fetchone()
            if actor is None or actor["role"] not in {"owner", "editor"}:
                raise DrawingImportForbidden("platform role does not permit drawing import")

            cur.execute(
                f"SELECT {columns}, import_fingerprint FROM drawing_versions "
                "WHERE org_id = %(org_id)s AND project_id = %(project_id)s "
                "AND idempotency_key = %(key)s AND deleted_at IS NULL",
                {"org_id": org_id, "project_id": project_id, "key": key},
            )
            replay = cur.fetchone()
            if replay is not None:
                if replay["import_fingerprint"] != fingerprint:
                    raise DrawingImportConflict(
                        "idempotency key already exists with different drawing import input")
                return DrawingVersion.from_row(replay), True

            tenant_id = str(org_id)
            cur.execute(
                "SELECT v.object_key, v.byte_count, v.content_sha256, "
                "v.intake_ref, v.intake_sha256, u.attempt, u.marker "
                "FROM drawing_store_versions v "
                "JOIN drawing_upload_attempts u "
                "ON u.tenant_id = v.tenant_id AND u.drawing_id = v.drawing_id "
                "WHERE v.tenant_id = %(org_id)s AND v.drawing_id = %(drawing_id)s "
                "AND v.version = %(source_version)s AND v.state = 'ready' "
                "AND u.status = 'ready' AND u.marker->>'tenant_kind' = 'account' "
                "AND u.marker->>'extracted_version' = %(source_version_text)s "
                "FOR UPDATE OF v, u",
                {"org_id": tenant_id, "drawing_id": str(source_drawing_id),
                 "source_version": source_version,
                 "source_version_text": str(source_version)},
            )
            source = cur.fetchone()
            if source is None:
                raise DrawingImportUnavailable(
                    "project or source account upload is unavailable")

            marker = source["marker"]
            marker_is_object = isinstance(marker, dict)
            upload_sha256 = marker.get("content_sha256") if marker_is_object else None
            marker_status = marker.get("status") if marker_is_object else None
            marker_attempt = marker.get("attempt") if marker_is_object else None
            marker_intake_ref = marker.get("intake_ref") if marker_is_object else None
            marker_intake_sha256 = (
                marker.get("intake_sha256") if marker_is_object else None)
            stored_sha256 = source["content_sha256"]
            expected_object_key = (
                f"tenants/{tenant_id}/drawings/{source_drawing_id}/"
                f"v/{source_version:08d}.dwg"
            )
            object_key = source["object_key"]
            expected_intake_ref = expected_object_key[:-4] + ".intake.json"
            intake_ref = source["intake_ref"]
            intake_sha256 = source["intake_sha256"]
            if (
                not isinstance(object_key, str)
                or object_key != expected_object_key
                or marker_status != "ready"
                or str(marker_attempt) != str(source["attempt"])
                or not isinstance(upload_sha256, str)
                or _SHA256_RE.fullmatch(upload_sha256) is None
                or not isinstance(stored_sha256, str)
                or _SHA256_RE.fullmatch(stored_sha256) is None
                or not isinstance(intake_ref, str)
                or intake_ref != expected_intake_ref
                or marker_intake_ref != intake_ref
                or not isinstance(intake_sha256, str)
                or _SHA256_RE.fullmatch(intake_sha256) is None
                or marker_intake_sha256 != intake_sha256
            ):
                raise DrawingImportUnavailable(
                    "project or source account upload is unavailable")
            provenance = {
                "schema": "leaf.drawing-import.v1",
                "source": {
                    "kind": "account_upload",
                    "tenant_id": tenant_id,
                    "drawing_id": str(source_drawing_id),
                    "version": source_version,
                    "upload_attempt": str(source["attempt"]),
                    "upload_content_sha256": upload_sha256,
                    "stored_object": {
                        "ref": object_key,
                        "sha256": stored_sha256,
                        "bytes": int(source["byte_count"]),
                    },
                    "intake": {
                        "ref": intake_ref,
                        "sha256": intake_sha256,
                        "proof": "ready_upload_after_fenced_cache_publication",
                    },
                },
                "imported_by_binding_id": str(actor_binding_id),
            }

            # The deterministic canonical UUID prevents the same immutable
            # source upload from being attached to a different project.
            cur.execute(
                "INSERT INTO drawing_artifacts "
                "(drawing_id, project_id, org_id, name) "
                "VALUES (%(drawing_id)s, %(project_id)s, %(org_id)s, %(name)s) "
                "ON CONFLICT (drawing_id) DO NOTHING",
                {"drawing_id": canonical_drawing_id, "project_id": project_id,
                 "org_id": org_id, "name": name},
            )
            cur.execute(
                "SELECT drawing_id, project_id, name FROM drawing_artifacts "
                "WHERE org_id = %(org_id)s AND drawing_id = %(drawing_id)s FOR UPDATE",
                {"org_id": org_id, "drawing_id": canonical_drawing_id},
            )
            artifact = cur.fetchone()
            if artifact is None or artifact["project_id"] != project_id or artifact["name"] != name:
                raise DrawingImportUnavailable(
                    "project or source account upload is unavailable")

            try:
                cur.execute(
                    "INSERT INTO drawing_versions "
                    "(version_id, drawing_id, project_id, org_id, seq, oss_object, intake_ref, "
                    "created_by, provenance, idempotency_key, import_fingerprint) "
                    "VALUES (%(version_id)s, %(drawing_id)s, %(project_id)s, %(org_id)s, "
                    "%(seq)s, %(oss_object)s, %(intake_ref)s, %(created_by)s, %(provenance)s, "
                    "%(key)s, %(fingerprint)s) RETURNING " + columns,
                    {"version_id": new_uuid(), "drawing_id": canonical_drawing_id,
                     "project_id": project_id, "org_id": org_id, "seq": source_version,
                     "oss_object": object_key, "intake_ref": intake_ref,
                     "created_by": str(actor_binding_id), "provenance": Jsonb(provenance),
                     "key": key, "fingerprint": fingerprint},
                )
            except UniqueViolation as exc:
                raise DrawingImportConflict(
                    "source drawing version is already attached to a canonical project") from exc
            return DrawingVersion.from_row(cur.fetchone()), False

    return run_transaction(operation, isolation="serializable")


def create_job(org_id: uuid.UUID, project_id: uuid.UUID, kind: str, *,
               tool_name: Optional[str] = None,
               params: Optional[Dict[str, Any]] = None,
               spine_ref: Optional[str] = None,
               input_version_id: Optional[uuid.UUID] = None) -> Job:
    with cursor() as cur:
        cur.execute(
            "INSERT INTO jobs "
            "(job_id, project_id, org_id, kind, tool_name, params, spine_ref, input_version_id) "
            "VALUES (%(job_id)s, %(project_id)s, %(org_id)s, %(kind)s, %(tool_name)s, "
            "%(params)s, %(spine_ref)s, %(input_version_id)s) "
            "RETURNING job_id, project_id, org_id, kind, tool_name, status, spine_ref, "
            "params, result, input_version_id, output_version_id, cost_usd, created_at, updated_at, "
            "deleted_at, purge_requested_at, purge_completed_at",
            {
                "job_id": new_uuid(), "project_id": project_id, "org_id": org_id, "kind": kind,
                "tool_name": tool_name, "params": Jsonb(params) if params is not None else None,
                "spine_ref": spine_ref, "input_version_id": input_version_id,
            },
        )
        return Job.from_row(cur.fetchone())


def create_built_tool(org_id: uuid.UUID, project_id: uuid.UUID, name: str,
                      manifest: Dict[str, Any], *, version: str = "1.0.0",
                      source_ref: Optional[str] = None,
                      provenance: Optional[Dict[str, Any]] = None) -> BuiltTool:
    with cursor() as cur:
        cur.execute(
            "INSERT INTO built_tools "
            "(tool_id, project_id, org_id, name, version, manifest, source_ref, provenance) "
            "VALUES (%(tool_id)s, %(project_id)s, %(org_id)s, %(name)s, %(version)s, "
            "%(manifest)s, %(source_ref)s, %(provenance)s) "
            "RETURNING tool_id, project_id, org_id, name, version, manifest, source_ref, "
            "provenance, created_at, deleted_at, purge_requested_at, purge_completed_at",
            {
                "tool_id": new_uuid(), "project_id": project_id, "org_id": org_id, "name": name,
                "version": version, "manifest": Jsonb(manifest),
                "source_ref": source_ref,
                "provenance": Jsonb(provenance) if provenance is not None else None,
            },
        )
        return BuiltTool.from_row(cur.fetchone())


# --------------------------------------------------------------------------- #
# reads — EVERY function below binds `WHERE org_id = %(org_id)s`
# --------------------------------------------------------------------------- #

def list_projects(org_id: uuid.UUID) -> List[Project]:
    with cursor() as cur:
        cur.execute(
            "SELECT project_id, org_id, name, status, created_at, updated_at, "
            "deleted_at, purge_requested_at, purge_completed_at "
            "FROM projects WHERE org_id = %(org_id)s AND deleted_at IS NULL "
            "ORDER BY created_at DESC",
            {"org_id": org_id},
        )
        return [Project.from_row(r) for r in cur.fetchall()]


def get_org(org_id: uuid.UUID) -> Optional[Org]:
    """The caller's own org row (``org_id`` IS the scope: the verified caller org).

    Selects exactly the columns the pre-refactor GET /api/orgs/{id} query read,
    so that endpoint's response stays byte-identical (deletion/purge timestamps
    keep reading as null there; ``Org.from_row`` tolerates their absence)."""
    with cursor() as cur:
        cur.execute(
            "SELECT org_id, name, tier, status, created_at, offboarded_at "
            "FROM orgs WHERE org_id = %(org_id)s",
            {"org_id": org_id},
        )
        row = cur.fetchone()
        return Org.from_row(row) if row else None


def get_project(org_id: uuid.UUID, project_id: uuid.UUID) -> Optional[Project]:
    with cursor() as cur:
        cur.execute(
            "SELECT project_id, org_id, name, status, created_at, updated_at, "
            "deleted_at, purge_requested_at, purge_completed_at "
            "FROM projects WHERE org_id = %(org_id)s AND project_id = %(project_id)s "
            "AND deleted_at IS NULL",
            {"org_id": org_id, "project_id": project_id},
        )
        row = cur.fetchone()
        return Project.from_row(row) if row else None


def list_drawing_versions(org_id: uuid.UUID, project_id: uuid.UUID) -> List[DrawingVersion]:
    with cursor() as cur:
        cur.execute(
            "SELECT version_id, drawing_id, project_id, org_id, seq, oss_object, intake_ref, created_by, "
            "provenance, idempotency_key, created_at, deleted_at, purge_requested_at, "
            "purge_completed_at "
            "FROM drawing_versions "
            "WHERE org_id = %(org_id)s AND project_id = %(project_id)s AND deleted_at IS NULL "
            "ORDER BY seq ASC",
            {"org_id": org_id, "project_id": project_id},
        )
        return [DrawingVersion.from_row(r) for r in cur.fetchall()]


def list_drawing_artifacts(org_id: uuid.UUID, project_id: uuid.UUID) -> List[DrawingArtifact]:
    with cursor() as cur:
        cur.execute(
            "SELECT drawing_id, project_id, org_id, name, status, created_at "
            "FROM drawing_artifacts WHERE org_id = %(org_id)s AND project_id = %(project_id)s "
            "ORDER BY created_at, drawing_id",
            {"org_id": org_id, "project_id": project_id},
        )
        return [DrawingArtifact.from_row(row) for row in cur.fetchall()]


def list_jobs(org_id: uuid.UUID, project_id: uuid.UUID) -> List[Job]:
    with cursor() as cur:
        cur.execute(
            "SELECT job_id, project_id, org_id, kind, tool_name, status, spine_ref, params, "
            "result, input_version_id, output_version_id, cost_usd, created_at, updated_at, "
            "deleted_at, purge_requested_at, purge_completed_at "
            "FROM jobs WHERE org_id = %(org_id)s AND project_id = %(project_id)s "
            "AND deleted_at IS NULL "
            "ORDER BY created_at DESC",
            {"org_id": org_id, "project_id": project_id},
        )
        return [Job.from_row(r) for r in cur.fetchall()]


def get_job(org_id: uuid.UUID, job_id: uuid.UUID) -> Optional[Job]:
    with cursor() as cur:
        cur.execute(
            "SELECT job_id, project_id, org_id, kind, tool_name, status, spine_ref, params, "
            "result, input_version_id, output_version_id, cost_usd, created_at, updated_at, "
            "deleted_at, purge_requested_at, purge_completed_at "
            "FROM jobs WHERE org_id = %(org_id)s AND job_id = %(job_id)s "
            "AND deleted_at IS NULL",
            {"org_id": org_id, "job_id": job_id},
        )
        row = cur.fetchone()
        return Job.from_row(row) if row else None


def list_built_tools(org_id: uuid.UUID, project_id: uuid.UUID) -> List[BuiltTool]:
    with cursor() as cur:
        cur.execute(
            "SELECT tool_id, project_id, org_id, name, version, manifest, source_ref, "
            "provenance, created_at, deleted_at, purge_requested_at, purge_completed_at "
            "FROM built_tools "
            "WHERE org_id = %(org_id)s AND project_id = %(project_id)s AND deleted_at IS NULL "
            "ORDER BY created_at DESC",
            {"org_id": org_id, "project_id": project_id},
        )
        return [BuiltTool.from_row(r) for r in cur.fetchall()]


def hydrate_project(org_id: uuid.UUID, project_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    """Workspace hydration payload: the project plus its versions, jobs, and tools.

    Returns None when the project is not owned by ``org_id`` (-> API 404). Composes
    only org-scoped reads, so every array is org-isolated by construction.
    """
    project = get_project(org_id, project_id)
    if project is None:
        return None
    return {
        "project": project.to_dict(),
        "drawing_artifacts": [d.to_dict() for d in list_drawing_artifacts(org_id, project_id)],
        "drawing_versions": [v.to_dict() for v in list_drawing_versions(org_id, project_id)],
        "jobs": [j.to_dict() for j in list_jobs(org_id, project_id)],
        "built_tools": [t.to_dict() for t in list_built_tools(org_id, project_id)],
    }


# --------------------------------------------------------------------------- #
# deletion / compliance helpers (migration 0002; DELETION-OFFBOARDING-DESIGN.md)
#
# soft_delete_project = the routine, REVERSIBLE soft-delete (design sec 2): set
# deleted_at so the row drops out of every default read (WHERE deleted_at IS NULL)
# but is retained and recoverable. mark_purge_requested / mark_purge_completed
# bracket the gated, audited hard-PURGE-on-request window (design sec 3) on the org
# tombstone. All three are org-scoped writes (WHERE org_id = ...) — they never
# reach across tenants — and issue no SELECT, so the store-guard read invariants
# are unaffected.
# --------------------------------------------------------------------------- #

def soft_delete_project(org_id: uuid.UUID, project_id: uuid.UUID) -> bool:
    """Soft-delete a project the caller's org owns (idempotent, reversible).

    Sets ``deleted_at = NOW()`` only when the project is live (``deleted_at IS
    NULL``) AND owned by ``org_id``. Returns True if a live row was just hidden,
    False if it was already soft-deleted, unknown, or owned by another org. The
    underlying data is retained — recovery is ``UPDATE ... SET deleted_at = NULL``.
    """
    with cursor() as cur:
        cur.execute(
            "UPDATE projects SET deleted_at = NOW(), updated_at = NOW() "
            "WHERE org_id = %(org_id)s AND project_id = %(project_id)s "
            "AND deleted_at IS NULL",
            {"org_id": org_id, "project_id": project_id},
        )
        return cur.rowcount == 1


def mark_purge_requested(org_id: uuid.UUID) -> Optional[datetime]:
    """Stamp the hard-PURGE request time on the org tombstone (design sec 3, step 2).

    First-request-wins: ``purge_requested_at = COALESCE(purge_requested_at, NOW())``
    so re-invoking the purge does not move the audit window's opening edge. Returns
    the effective ``purge_requested_at`` (None only if the org row does not exist).
    """
    with cursor() as cur:
        cur.execute(
            "UPDATE orgs SET purge_requested_at = COALESCE(purge_requested_at, NOW()) "
            "WHERE org_id = %(org_id)s RETURNING purge_requested_at",
            {"org_id": org_id},
        )
        row = cur.fetchone()
        return row["purge_requested_at"] if row else None


def mark_purge_completed(org_id: uuid.UUID) -> Optional[datetime]:
    """Stamp the hard-PURGE completion time on the org tombstone (design sec 3, step 2).

    Set once the cascade across all sec-1 stores has finished. Returns the effective
    ``purge_completed_at`` (None only if the org row does not exist).
    """
    with cursor() as cur:
        cur.execute(
            "UPDATE orgs SET purge_completed_at = NOW() "
            "WHERE org_id = %(org_id)s RETURNING purge_completed_at",
            {"org_id": org_id},
        )
        row = cur.fetchone()
        return row["purge_completed_at"] if row else None
