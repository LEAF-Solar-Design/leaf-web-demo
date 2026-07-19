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

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from psycopg.types.json import Jsonb

from .db import connection, cursor
from .models import (
    BuiltTool,
    DrawingVersion,
    Job,
    Org,
    Project,
    new_uuid,
)

OrgId = uuid.UUID
ProjectId = uuid.UUID

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


def create_project(org_id: uuid.UUID, name: str) -> Project:
    with cursor() as cur:
        cur.execute(
            "INSERT INTO projects (project_id, org_id, name) "
            "VALUES (%(project_id)s, %(org_id)s, %(name)s) "
            "RETURNING project_id, org_id, name, status, created_at, updated_at, "
            "deleted_at, purge_requested_at, purge_completed_at",
            {"project_id": new_uuid(), "org_id": org_id, "name": name},
        )
        return Project.from_row(cur.fetchone())


def create_drawing_version(org_id: uuid.UUID, project_id: uuid.UUID, *,
                           oss_object: Optional[str] = None,
                           intake_ref: Optional[str] = None,
                           created_by: Optional[str] = None) -> DrawingVersion:
    """Append the next monotonic version to a project's chain (single-writer)."""
    with connection() as conn:
        with conn.cursor() as cur:
            # next seq, scoped to the owning org (guards against cross-org project_id)
            cur.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM drawing_versions "
                "WHERE project_id = %(project_id)s AND org_id = %(org_id)s",
                {"project_id": project_id, "org_id": org_id},
            )
            next_seq = cur.fetchone()["next_seq"]
            cur.execute(
                "INSERT INTO drawing_versions "
                "(version_id, project_id, org_id, seq, oss_object, intake_ref, created_by) "
                "VALUES (%(version_id)s, %(project_id)s, %(org_id)s, %(seq)s, "
                "%(oss_object)s, %(intake_ref)s, %(created_by)s) "
                "RETURNING version_id, project_id, org_id, seq, oss_object, intake_ref, "
                "created_by, created_at, deleted_at, purge_requested_at, purge_completed_at",
                {
                    "version_id": new_uuid(), "project_id": project_id, "org_id": org_id,
                    "seq": next_seq, "oss_object": oss_object, "intake_ref": intake_ref,
                    "created_by": created_by,
                },
            )
            return DrawingVersion.from_row(cur.fetchone())


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
            "SELECT version_id, project_id, org_id, seq, oss_object, intake_ref, created_by, "
            "created_at, deleted_at, purge_requested_at, purge_completed_at "
            "FROM drawing_versions "
            "WHERE org_id = %(org_id)s AND project_id = %(project_id)s AND deleted_at IS NULL "
            "ORDER BY seq ASC",
            {"org_id": org_id, "project_id": project_id},
        )
        return [DrawingVersion.from_row(r) for r in cur.fetchall()]


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
