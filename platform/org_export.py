"""Organisation-wide data export (P-153): the per-project export, composed.

One request exports every project the caller's organisation holds. It adds no
SQL of its own: projects are listed through ``store.list_projects`` and each is
exported through ``project_lifecycle.export_project``, so every project keeps its
own role check, receipt and idempotency replay exactly as the per-project route.

Contract:
  * fails closed on a cross-org actor (``OrgExportForbidden`` -> HTTP 403) before
    any read happens;
  * bounded: at most ``max_projects`` exports run, ``truncated`` says the rest
    exist, and the export loop never runs past the bound;
  * one project's failure is recorded as a typed code in its own row and never
    aborts the rest; exception text is not echoed into the manifest.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List

from . import project_lifecycle, store

LOGGER = logging.getLogger(__name__)

MAX_PROJECTS_DEFAULT = 500


class OrgExportForbidden(PermissionError):
    """The verified actor belongs to a different organisation."""


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, project_lifecycle.LifecycleUnavailable):
        return "not_found"
    if isinstance(exc, project_lifecycle.LifecycleForbidden):
        return "forbidden"
    if isinstance(exc, project_lifecycle.LifecycleConflict):
        return "conflict"
    if isinstance(exc, ValueError):
        return "invalid"
    return "export_failed"


def _export_ref(response: Dict[str, Any]) -> Dict[str, Any]:
    """The reference fields of one per-project export response (no file content)."""
    receipt = response.get("receipt") or {}
    return {
        "export_sha256": response.get("export_sha256"),
        "file_count": response.get("file_count"),
        "member_count": response.get("member_count"),
        "receipt_id": receipt.get("receipt_id"),
        "replayed": bool(response.get("replayed", False)),
    }


def export_org(
    org_id: uuid.UUID,
    actor: Any,
    *,
    idempotency_key: str,
    lifecycle: Any = project_lifecycle,
    list_projects: Callable[[uuid.UUID], List[Any]] = store.list_projects,
    max_projects: int = MAX_PROJECTS_DEFAULT,
) -> Dict[str, Any]:
    """Export every project of ``org_id`` for ``actor``; returns the manifest.

    ``actor`` carries ``org_id`` and ``binding_id`` (the lifecycle actor). The same
    idempotency key is passed to each per-project export; receipts are unique per
    (org, project, action, key), so a retry replays every project's receipt.
    """
    if getattr(actor, "org_id", None) != org_id:
        raise OrgExportForbidden("actor does not belong to this organisation")
    if isinstance(max_projects, bool) or not isinstance(max_projects, int) or max_projects < 1:
        raise ValueError("max_projects must be a positive integer")
    # Validate once up front so a bad key is one 422, not one error row per project.
    idempotency_key = project_lifecycle._validate_idempotency_key(idempotency_key)

    listed = list_projects(org_id)
    truncated = len(listed) > max_projects
    rows: List[Dict[str, Any]] = []
    for project in listed[:max_projects]:
        project_id = project.project_id
        row: Dict[str, Any] = {
            "project_id": str(project_id),
            "name": getattr(project, "name", None),
        }
        try:
            response = lifecycle.export_project(
                org_id, project_id, actor.binding_id, idempotency_key=idempotency_key,
            )
        except Exception as exc:  # one project's failure never aborts the rest
            code = _error_code(exc)
            LOGGER.warning("org_export_project_failed org=%s project=%s code=%s exc=%s",
                           org_id, project_id, code, type(exc).__name__)
            row["error"] = code
        else:
            row["export_ref"] = _export_ref(response if isinstance(response, dict) else {})
        rows.append(row)

    return {
        "org_id": str(org_id),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "projects": rows,
        "truncated": truncated,
    }
