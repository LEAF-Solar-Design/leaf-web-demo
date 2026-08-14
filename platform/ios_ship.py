"""Wave D one-shot iOS ship authority.

Schema changes live in ``migrations/0040_ios_ship_lane.sql``. Apple credential
material never enters this module. Every execution extends a canonical project
``jobs`` row, and admission commits before provider dispatch.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

from psycopg.types.json import Jsonb

from .db import connection
from .models import canonical_hash, new_uuid

READINESS_KIND = "leaf.ios-ship-readiness.v1"
EXECUTION_KIND = "leaf.ios-ship-execution.v1"
RECEIPT_KIND = "leaf.ios-testflight-receipt.v1"
SETUP_ACTION = "mount-apple-ship-dispatch"
TOOL_NAME = "ios-ship"

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_APP_COLORS = frozenset({"primary", "alternate"})
_SECRET_KEY_RE = re.compile(
    r"(password|passwd|two.?factor|2fa|otp|p8|\.p8|private[_ -]?key|certificate|"
    r"provisioning|profile|credential|secret|keychain|authkey|token|session|"
    r"cookie|api[_ -]?key|access[_ -]?key|signing[_ -]?(key|cert))", re.IGNORECASE)
_SECRET_VALUE_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----|eyJ[a-zA-Z0-9_-]{10,}\.eyJ|"
    r"AAAA[A-Za-z0-9+/]{20,}={0,2}")
_LAUNCH_FIELDS = (
    "revision", "source_revision", "source_sha256", "bundle_identifier",
    "marketing_version", "build_number",
)
_ASC_RESULT_FIELDS = frozenset({"status", "build_id", "beta_group", "uploaded_at"})
_DISPATCH_FIELDS = frozenset({"status", "stage", "provider_run_id"})
_DISPATCH_STATUSES = frozenset({"dispatched", "running", "failed"})
_PROVIDER_PROGRESS_STATUSES = frozenset({"running", "failed", "PENDING_RELEASE"})
_CONTROLLER_STAGES = frozenset({
    "SOURCE_APPROVED", "GRANT_READY", "BUNDLE_READY", "MAC_ALLOCATED", "APP_RECORD",
    "XCODE_READY", "SIGNING_READY", "BUILT", "UPLOADED", "COMPLIANCE", "BETA_ASSIGNED",
    "CREDENTIALS_SCRUBBED", "MAC_RELEASED", "RECEIPT",
})
_CONTROLLER_RECEIPT_SCHEMA = "leaf.ios-testflight-receipt.v1"
_CONTROLLER_RECEIPT_FIELDS = frozenset({
    "schema", "run_id", "request_digest", "review_id", "tenant_id", "project_id",
    "source_revision", "source_artifact_digest", "bundle_id", "marketing_version",
    "build_number", "image_id", "image_digest", "host_id", "instance_id", "region",
    "availability_zone", "instance_type", "minimum_allocation_hours",
    "estimated_cost_usd", "xcode_version", "xcode_build", "app_store_connect_app_id",
    "app_store_connect_build_id", "status", "beta_group", "compliance_answered",
    "credentials_scrubbed", "mac_instance_state", "dedicated_host_state",
    "teardown_receipt_id", "completed_at",
})
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_RECEIPT_NAMESPACE = uuid.UUID("cf703adc-1149-52c0-a59e-f876729ab214")


class IosShipError(ValueError):
    def __init__(self, code: str, message: str, *, setup_action: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.setup_action = setup_action


class SecretShapedFieldRejected(IosShipError):
    def __init__(self, path: str):
        super().__init__("secret_shaped_field", f"secret-shaped field at {path}")


class ReadinessUnavailable(IosShipError):
    pass


class GrantInvalid(IosShipError):
    pass


class ProjectUnavailable(IosShipError):
    pass


class RevisionNotApproved(IosShipError):
    pass


class LaunchSetupRequired(IosShipError):
    def __init__(self):
        super().__init__("dispatch_unavailable", "ship dispatch is not mounted",
                         setup_action=SETUP_ACTION)


class LaunchConflict(IosShipError):
    pass


class ReceiptTampered(IosShipError):
    pass


def _now(value: Optional[datetime]) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return current


def _as_uuid(value: Any, field: str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise IosShipError("invalid_id", f"{field} is not a UUID") from exc


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _row_dict(row: Any) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    return {key: (str(value) if isinstance(value, uuid.UUID) else _iso(value))
            for key, value in dict(row).items()}


def _fingerprint(domain: str, value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(domain.encode() + b"\0" + encoded).hexdigest()


def _advisory_key(value: str) -> int:
    raw = hashlib.sha256(value.encode()).digest()[:8]
    return int.from_bytes(raw, "big", signed=True)


def reject_secret_shaped(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and _SECRET_KEY_RE.search(key):
                raise SecretShapedFieldRejected(f"{path}.{key}")
            reject_secret_shaped(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_secret_shaped(child, f"{path}[{index}]")
    elif isinstance(value, str) and _SECRET_VALUE_RE.search(value):
        raise SecretShapedFieldRejected(path)


def _launch_fields(payload: Any) -> Dict[str, str]:
    if not isinstance(payload, dict):
        raise IosShipError("invalid_launch", "launch identifiers must be an object")
    fields = {}
    for name in _LAUNCH_FIELDS:
        value = payload.get(name)
        if not isinstance(value, str) or not value.strip():
            raise IosShipError("invalid_launch", f"{name} is required")
        fields[name] = value
    if not _HASH_RE.fullmatch(fields["source_sha256"]):
        raise IosShipError("invalid_launch", "source_sha256 must be a lowercase SHA-256")
    reject_secret_shaped(fields)
    return fields


def validate_readiness(record: Any) -> Dict[str, Any]:
    reject_secret_shaped(record)
    if not isinstance(record, dict) or record.get("record_kind") != READINESS_KIND:
        raise ReadinessUnavailable("invalid_record", "unsupported readiness record")
    if not isinstance(record.get("healthy"), bool):
        raise ReadinessUnavailable("invalid_record", "healthy must be a boolean")
    try:
        reported = datetime.fromisoformat(str(record["reported_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError) as exc:
        raise ReadinessUnavailable("invalid_record", "reported_at is invalid") from exc
    if reported.tzinfo is None:
        raise ReadinessUnavailable("invalid_record", "reported_at needs a timezone")
    org = _as_uuid(record.get("org_id"), "org_id")
    project = _as_uuid(record.get("project_id"), "project_id")
    tenant, dispatch = record.get("tenant_id"), record.get("dispatch")
    if not isinstance(tenant, str) or not tenant:
        raise ReadinessUnavailable("invalid_record", "tenant_id is required")
    if not isinstance(dispatch, dict) or not isinstance(dispatch.get("available"), bool):
        raise ReadinessUnavailable("invalid_record", "dispatch.available must be a boolean")
    action = dispatch.get("action")
    if not dispatch["available"] and (not isinstance(action, str) or not action):
        raise ReadinessUnavailable("invalid_record", "unavailable dispatch requires an action")
    return {
        "record_kind": READINESS_KIND, "healthy": record["healthy"],
        "reported_at": reported.isoformat(), "org_id": str(org),
        "project_id": str(project), "tenant_id": tenant,
        "dispatch": {"available": dispatch["available"], "action": action},
    }


def upsert_readiness(record: Any) -> Dict[str, Any]:
    clean = validate_readiness(record)
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ios_ship_readiness "
            "(org_id, project_id, tenant_id, record_kind, healthy, dispatch_available, "
            "setup_action, reported_at) VALUES (%(org)s, %(project)s, %(tenant)s, %(kind)s, "
            "%(healthy)s, %(available)s, %(action)s, %(reported)s) "
            "ON CONFLICT (org_id, project_id, tenant_id) DO UPDATE SET "
            "healthy=EXCLUDED.healthy, dispatch_available=EXCLUDED.dispatch_available, "
            "setup_action=EXCLUDED.setup_action, reported_at=EXCLUDED.reported_at, observed_at=NOW()",
            {"org": _as_uuid(clean["org_id"], "org_id"),
             "project": _as_uuid(clean["project_id"], "project_id"),
             "tenant": clean["tenant_id"], "kind": READINESS_KIND,
             "healthy": clean["healthy"], "available": clean["dispatch"]["available"],
             "action": clean["dispatch"]["action"],
             "reported": datetime.fromisoformat(clean["reported_at"])})
    return clean


def record_grant(org_id: Any, tenant_id: str, grant: Dict[str, Any]) -> None:
    reject_secret_shaped(grant)
    status = grant.get("status")
    if status not in {"healthy", "expired", "revoked", "unavailable"}:
        raise GrantInvalid("invalid_grant", "grant status is invalid")
    try:
        expires = datetime.fromisoformat(str(grant["expires_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError) as exc:
        raise GrantInvalid("invalid_grant", "grant expires_at is invalid") from exc
    if expires.tzinfo is None:
        raise GrantInvalid("invalid_grant", "grant expires_at needs a timezone")
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ios_ship_grants (grant_id, org_id, tenant_id, status, expires_at) "
            "VALUES (%(id)s, %(org)s, %(tenant)s, %(status)s, %(expires)s) "
            "ON CONFLICT (org_id, tenant_id) DO UPDATE SET grant_id=EXCLUDED.grant_id, "
            "status=EXCLUDED.status, expires_at=EXCLUDED.expires_at, observed_at=NOW()",
            {"id": _as_uuid(grant.get("grant_id") or new_uuid(), "grant_id"),
             "org": _as_uuid(org_id, "org_id"), "tenant": tenant_id,
             "status": status, "expires": expires})


def _grant_healthy(cur: Any, org_id: uuid.UUID, tenant_id: str, now: datetime) -> None:
    cur.execute("SELECT status, expires_at FROM ios_ship_grants "
                "WHERE org_id=%(org)s AND tenant_id=%(tenant)s",
                {"org": org_id, "tenant": tenant_id})
    row = cur.fetchone()
    if row is None:
        raise GrantInvalid("grant_missing", "no mounted Apple grant",
                           setup_action="mount-apple-developer-grant")
    if row["status"] != "healthy":
        raise GrantInvalid("grant_unhealthy", "the mounted Apple grant is not healthy",
                           setup_action="re-mount-apple-developer-grant")
    expires = row["expires_at"]
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= now:
        raise GrantInvalid("grant_expired", "the mounted Apple grant has expired",
                           setup_action="re-mount-apple-developer-grant")


def record_approval(
    org_id: Any, project_id: Any, revision: str, *, source_revision: str,
    source_sha256: str, bundle_identifier: str, marketing_version: str,
    build_number: str, approved_by: str, approved: bool = True,
    approval_id: Any = None,
) -> str:
    fields = _launch_fields({
        "revision": revision, "source_revision": source_revision,
        "source_sha256": source_sha256, "bundle_identifier": bundle_identifier,
        "marketing_version": marketing_version, "build_number": build_number})
    reject_secret_shaped({"approved_by": approved_by})
    if not approved_by:
        raise IosShipError("invalid_approval", "approved_by is required")
    approval = _as_uuid(approval_id or new_uuid(), "approval_id")
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ios_ship_revision_approvals "
            "(approval_id, org_id, project_id, revision, source_revision, source_sha256, "
            "bundle_identifier, marketing_version, build_number, approved, approved_by) "
            "VALUES (%(id)s, %(org)s, %(project)s, %(revision)s, %(source_revision)s, "
            "%(source_sha256)s, %(bundle_identifier)s, %(marketing_version)s, "
            "%(build_number)s, %(approved)s, %(approved_by)s) "
            "ON CONFLICT (org_id, project_id, revision) DO UPDATE SET "
            "approval_id=EXCLUDED.approval_id, source_revision=EXCLUDED.source_revision, "
            "source_sha256=EXCLUDED.source_sha256, bundle_identifier=EXCLUDED.bundle_identifier, "
            "marketing_version=EXCLUDED.marketing_version, build_number=EXCLUDED.build_number, "
            "approved=EXCLUDED.approved, approved_by=EXCLUDED.approved_by, created_at=NOW() "
            "WHERE ios_ship_revision_approvals.consumed_at IS NULL",
            {"id": approval, "org": _as_uuid(org_id, "org_id"),
             "project": _as_uuid(project_id, "project_id"), **fields,
             "approved": approved, "approved_by": approved_by})
    return str(approval)


def revision_is_approved(org_id: Any, project_id: Any, revision: str,
                         source_sha256: str) -> bool:
    """Compatibility read for callers that only need approval truth."""
    org, project = _as_uuid(org_id, "org_id"), _as_uuid(project_id, "project_id")
    with connection() as conn, conn.cursor() as cur:
        approval = _approval_record(cur, org, project, revision)
    return bool(approval and approval["approved"] is True
                and approval["source_sha256"] == source_sha256
                and approval["consumed_at"] is None)


def approved_launch(org_id: Any, project_id: Any, revision: str) -> Dict[str, str]:
    """Return the browser-safe immutable approval tuple without grant state.

    Readiness refresh needs this source of truth before a grant or prior
    readiness record exists. It never exposes provider or credential material.
    """
    org, project = _as_uuid(org_id, "org_id"), _as_uuid(project_id, "project_id")
    with connection() as conn, conn.cursor() as cur:
        approval = _approval_record(cur, org, project, revision)
    if approval is None or approval["approved"] is not True:
        raise RevisionNotApproved("unapproved_revision", "selected revision is not approved",
                                  setup_action="approve-ios-project-revision")
    if approval["consumed_at"] is not None:
        raise RevisionNotApproved("approval_consumed", "approval was already launched",
                                  setup_action="select-new-ios-build")
    result = {key: approval[key] for key in _LAUNCH_FIELDS}
    result["approval_id"] = approval["approval_id"]
    return result


def get_approved_ios_ship_revision(
        org_id: Any, project_id: Any, revision: str) -> Optional[Dict[str, str]]:
    """Return an unconsumed approval tuple for an active project, or ``None``.

    This lookup intentionally does not read readiness or grant state. The
    browser router uses it to form the exact provider-readiness scope before it
    refreshes either of those derived records.
    """
    org, project = _as_uuid(org_id, "org_id"), _as_uuid(project_id, "project_id")
    if not isinstance(revision, str) or not revision:
        return None
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM projects WHERE org_id=%(org)s AND project_id=%(project)s "
            "AND deleted_at IS NULL AND status='active'",
            {"org": org, "project": project})
        if cur.fetchone() is None:
            return None
        approval = _approval_record(cur, org, project, revision)
    if approval is None or approval["approved"] is not True or approval["consumed_at"] is not None:
        return None
    return {
        "approval_id": str(approval["approval_id"]),
        "revision": approval["revision"],
        "source_revision": approval["source_revision"],
        "source_sha256": approval["source_sha256"],
        "bundle_identifier": approval["bundle_identifier"],
        "marketing_version": approval["marketing_version"],
        "build_number": approval["build_number"],
    }


def _approval_record(cur: Any, org_id: uuid.UUID, project_id: uuid.UUID,
                     revision: str, *, for_update: bool = False) -> Optional[Dict[str, Any]]:
    cur.execute(
        "SELECT approval_id, revision, source_revision, source_sha256, bundle_identifier, "
        "marketing_version, build_number, approved, consumed_at, consumed_execution_id "
        "FROM ios_ship_revision_approvals WHERE org_id=%(org)s AND project_id=%(project)s "
        "AND revision=%(revision)s" + (" FOR UPDATE" if for_update else ""),
        {"org": org_id, "project": project_id, "revision": revision})
    return _row_dict(cur.fetchone())


def project_org(project_id: Any) -> Optional[uuid.UUID]:
    """Development-only discovery seam. Live routes supply the verified org."""
    with connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT org_id FROM projects WHERE project_id=%(project)s "
                    "AND deleted_at IS NULL AND status='active'",
                    {"project": _as_uuid(project_id, "project_id")})
        row = cur.fetchone()
        return row["org_id"] if row else None


def project_exists(org_id: Any, project_id: Any) -> bool:
    with connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM projects WHERE org_id=%(org)s AND project_id=%(project)s "
                    "AND deleted_at IS NULL AND status='active'",
                    {"org": _as_uuid(org_id, "org_id"),
                     "project": _as_uuid(project_id, "project_id")})
        return cur.fetchone() is not None


def project_accessible(org_id: Any, project_id: Any, external_subject: str) -> bool:
    """Re-read current named-project membership for a verified live subject."""
    if not external_subject:
        return False
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM identity_bindings b "
            "JOIN project_member_bindings m ON m.org_id=b.platform_tenant_id "
            "AND m.binding_id=b.binding_id "
            "WHERE b.platform_tenant_id=%(org)s AND m.project_id=%(project)s "
            "AND b.external_authority='auth0' AND b.external_subject=%(subject)s "
            "AND b.status='active' AND m.status='active'",
            {"org": _as_uuid(org_id, "org_id"),
             "project": _as_uuid(project_id, "project_id"),
             "subject": external_subject})
        return cur.fetchone() is not None


def resolve_ship_principal(org_id: Any, project_id: Any,
                           external_subject: str) -> Optional[str]:
    """Resolve a current owner/editor membership for one named project."""
    if not external_subject:
        return None
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT b.binding_id FROM identity_bindings b "
            "JOIN project_member_bindings m ON m.org_id=b.platform_tenant_id "
            "AND m.binding_id=b.binding_id "
            "WHERE b.platform_tenant_id=%(org)s AND m.project_id=%(project)s "
            "AND b.external_authority='auth0' AND b.external_subject=%(subject)s "
            "AND b.status='active' AND m.status='active' "
            "AND m.role IN ('owner', 'editor')",
            {"org": _as_uuid(org_id, "org_id"),
             "project": _as_uuid(project_id, "project_id"),
             "subject": external_subject})
        row = cur.fetchone()
        return str(row["binding_id"]) if row else None


def _require_ship_principal(cur: Any, org_id: uuid.UUID, project_id: uuid.UUID,
                            principal_id: uuid.UUID) -> None:
    """Re-read current tenant identity and project write authority at admission."""
    cur.execute(
        "SELECT 1 FROM identity_bindings b "
        "JOIN project_member_bindings m ON m.org_id=b.platform_tenant_id "
        "AND m.binding_id=b.binding_id "
        "WHERE b.platform_tenant_id=%(org)s AND m.project_id=%(project)s "
        "AND b.binding_id=%(principal)s AND b.status='active' AND m.status='active' "
        "AND m.role IN ('owner', 'editor') FOR SHARE OF b, m",
        {"org": org_id, "project": project_id, "principal": principal_id})
    if cur.fetchone() is None:
        raise IosShipError("launch_forbidden",
                           "current project role does not permit iOS launch")


def get_readiness(
    org_id: Any, tenant_id: str, project_id: Any, revision: str, *,
    now: Optional[datetime] = None, max_age_seconds: float = 300.0,
) -> Dict[str, Any]:
    current, org = _now(now), _as_uuid(org_id, "org_id")
    project = _as_uuid(project_id, "project_id")
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT healthy, dispatch_available, setup_action, reported_at "
            "FROM ios_ship_readiness WHERE org_id=%(org)s AND project_id=%(project)s "
            "AND tenant_id=%(tenant)s",
            {"org": org, "project": project, "tenant": tenant_id})
        row = cur.fetchone()
        if row is None:
            raise ReadinessUnavailable("readiness_missing", "no project readiness record",
                                       setup_action="run-ios-ship-readiness")
        if row["healthy"] is not True:
            raise ReadinessUnavailable("unhealthy", "ship lane is not healthy",
                                       setup_action=row["setup_action"] or SETUP_ACTION)
        reported = row["reported_at"]
        if reported.tzinfo is None:
            reported = reported.replace(tzinfo=timezone.utc)
        if current - reported > timedelta(seconds=max_age_seconds):
            raise ReadinessUnavailable("stale_readiness", "readiness record is stale",
                                       setup_action="run-ios-ship-readiness")
        if row["dispatch_available"] is not True:
            raise ReadinessUnavailable("dispatch_unavailable", "ship dispatch is unavailable",
                                       setup_action=row["setup_action"] or SETUP_ACTION)
        _grant_healthy(cur, org, tenant_id, current)
        approval = _approval_record(cur, org, project, revision)
    if approval is None or approval["approved"] is not True:
        raise RevisionNotApproved("unapproved_revision", "selected revision is not approved",
                                  setup_action="approve-ios-project-revision")
    if approval["consumed_at"] is not None:
        raise RevisionNotApproved("approval_consumed", "approval was already launched",
                                  setup_action="select-new-ios-build")
    approved_launch = {key: approval[key] for key in _LAUNCH_FIELDS}
    approved_launch["approval_id"] = approval["approval_id"]
    return {
        "record_kind": READINESS_KIND, "launchable": True, "healthy": True,
        "reported_at": reported.isoformat(), "org_id": str(org),
        "project_id": str(project), "tenant_id": tenant_id,
        "grant_status": "healthy", "dispatch_available": True,
        "setup_action": None, "approved_launch": approved_launch}


def readiness_projection(org_id: Any, tenant_id: str, project_id: Any,
                         revision: str, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    try:
        return get_readiness(org_id, tenant_id, project_id, revision, now=now)
    except IosShipError as exc:
        return {
            "record_kind": READINESS_KIND, "launchable": False, "healthy": False,
            "reported_at": None, "org_id": str(org_id), "project_id": str(project_id),
            "grant_status": None, "dispatch_available": False, "reason": exc.code,
            "setup_action": exc.setup_action, "approved_launch": None}


_EXECUTION_COLUMNS = (
    "execution_id, org_id, project_id, tenant_id, principal_id, approval_id, revision, "
    "source_revision, source_sha256, bundle_identifier, marketing_version, build_number, "
    "app_color, "
    "status, failed_stage, receipt_id, created_at, updated_at"
)


def _existing_execution(cur: Any, org_id: uuid.UUID, project_id: uuid.UUID,
                        idempotency_key: str) -> Any:
    cur.execute(
        "SELECT " + _EXECUTION_COLUMNS + ", submission_fingerprint "
        "FROM ios_ship_executions WHERE org_id=%(org)s AND project_id=%(project)s "
        "AND idempotency_key=%(key)s",
        {"org": org_id, "project": project_id, "key": idempotency_key})
    return cur.fetchone()


def _get_execution_row(cur: Any, org_id: uuid.UUID, project_id: uuid.UUID,
                       execution_id: uuid.UUID) -> Any:
    cur.execute("SELECT " + _EXECUTION_COLUMNS + " FROM ios_ship_executions "
                "WHERE org_id=%(org)s AND project_id=%(project)s AND execution_id=%(id)s",
                {"org": org_id, "project": project_id, "id": execution_id})
    return cur.fetchone()


def _sanitize_dispatch_result(result: Any) -> Dict[str, Any]:
    reject_secret_shaped(result)
    if not isinstance(result, dict) or set(result) - _DISPATCH_FIELDS:
        raise IosShipError("invalid_dispatch", "dispatch result must be an object")
    clean = dict(result)
    if any(value is not None and not isinstance(value, str) for value in clean.values()):
        raise IosShipError("invalid_dispatch", "dispatch result fields must be strings")
    status = clean.get("status", "dispatched")
    if status not in _DISPATCH_STATUSES:
        raise IosShipError("invalid_dispatch", "dispatch status is unsupported")
    if status == "failed" and not clean.get("stage"):
        raise IosShipError("invalid_dispatch", "failed dispatch requires an exact stage")
    if not clean.get("provider_run_id"):
        raise IosShipError("invalid_dispatch", "dispatch requires a provider_run_id")
    clean["status"] = status
    return clean


def get_execution(org_id: Any, project_id: Any, execution_id: Any) -> Optional[Dict[str, Any]]:
    org, project = _as_uuid(org_id, "org_id"), _as_uuid(project_id, "project_id")
    execution = _as_uuid(execution_id, "execution_id")
    with connection() as conn, conn.cursor() as cur:
        return _row_dict(_get_execution_row(cur, org, project, execution))


def _provider_execution_row(cur: Any, org: uuid.UUID, tenant_id: str,
                            project: uuid.UUID, execution: uuid.UUID,
                            provider_run_id: str, *, for_update: bool = False) -> Any:
    cur.execute(
        "SELECT " + _EXECUTION_COLUMNS + ", dispatch_result "
        "FROM ios_ship_executions WHERE org_id=%(org)s AND tenant_id=%(tenant)s "
        "AND project_id=%(project)s AND execution_id=%(execution)s" +
        (" FOR UPDATE" if for_update else ""),
        {"org": org, "tenant": tenant_id, "project": project, "execution": execution})
    row = cur.fetchone()
    bound = row.get("dispatch_result") if row is not None else None
    if row is None or not isinstance(bound, dict) \
            or bound.get("provider_run_id") != provider_run_id:
        raise IosShipError("provider_scope_mismatch",
                           "provider callback does not match the admitted execution")
    return row


def record_provider_progress(org_id: Any, tenant_id: str, project_id: Any,
                             execution_id: Any, provider_run_id: str, *,
                             status: str, stage: Optional[str] = None) -> Dict[str, Any]:
    """Record only an identity-bound provider state, never provider prose."""
    org, project = _as_uuid(org_id, "org_id"), _as_uuid(project_id, "project_id")
    execution = _as_uuid(execution_id, "execution_id")
    if not tenant_id or not provider_run_id or status not in _PROVIDER_PROGRESS_STATUSES:
        raise IosShipError("invalid_provider_progress", "provider progress is invalid")
    if stage is not None and stage not in _CONTROLLER_STAGES:
        raise IosShipError("invalid_provider_progress", "provider stage is unsupported")
    if status == "failed" and stage is None:
        raise IosShipError("invalid_provider_progress", "failed progress needs an exact stage")
    if status == "PENDING_RELEASE" and stage != "MAC_RELEASED":
        raise IosShipError("invalid_provider_progress",
                           "PENDING_RELEASE must identify MAC_RELEASED")
    persisted_status = "failed" if status == "failed" else "running"
    failed_stage = stage if persisted_status == "failed" else None
    clean = {"status": status, "provider_run_id": provider_run_id,
             **({"stage": stage} if stage else {})}
    with connection() as conn, conn.cursor() as cur:
        row = _provider_execution_row(
            cur, org, tenant_id, project, execution, provider_run_id, for_update=True)
        if row["status"] in {"succeeded", "failed"} and not (
                row["status"] == "failed" and persisted_status == "failed"
                and row["failed_stage"] == failed_stage):
            raise IosShipError("execution_terminal", "execution is already terminal")
        cur.execute(
            "UPDATE ios_ship_executions SET status=%(status)s, failed_stage=%(stage)s, "
            "dispatch_result=%(result)s, error=NULL, updated_at=NOW() "
            "WHERE execution_id=%(execution)s",
            {"status": persisted_status, "stage": failed_stage,
             "result": Jsonb(clean), "execution": execution})
        cur.execute(
            "UPDATE jobs SET status=%(status)s, result=%(result)s, error=NULL, "
            "updated_at=NOW() WHERE job_id=%(execution)s",
            {"status": "failed" if persisted_status == "failed" else "running",
             "result": Jsonb({"ios_ship": clean}), "execution": execution})
    result = get_execution(org, project, execution)
    if result is None:
        raise IosShipError("execution_missing", "durable execution disappeared")
    return result


def _dispatch_admitted(
    org: uuid.UUID, project: uuid.UUID, execution_id: uuid.UUID,
    intent: Dict[str, Any], dispatch: Callable[[Dict[str, Any]], Dict[str, Any]],
) -> Dict[str, Any]:
    # The queued-to-dispatching compare-and-set makes one caller the dispatcher.
    with connection() as conn, conn.cursor() as cur:
        cur.execute("UPDATE ios_ship_executions SET status='dispatching', updated_at=NOW() "
                    "WHERE execution_id=%(id)s AND status='queued' RETURNING execution_id",
                    {"id": execution_id})
        claimed = cur.fetchone() is not None
        if claimed:
            cur.execute("UPDATE jobs SET status='running', updated_at=NOW() "
                        "WHERE job_id=%(id)s", {"id": execution_id})
    if not claimed:
        existing = get_execution(org, project, execution_id)
        if existing is None:
            raise IosShipError("execution_missing", "durable execution disappeared")
        return existing
    try:
        clean = _sanitize_dispatch_result(dispatch(intent))
        provider_status = clean["status"]
        status = "failed" if provider_status == "failed" else "dispatched"
        failed_stage = clean.get("stage") if status == "failed" else None
        with connection() as conn, conn.cursor() as cur:
            cur.execute("UPDATE ios_ship_executions SET status=%(status)s, "
                        "failed_stage=%(stage)s, dispatch_result=%(result)s, updated_at=NOW() "
                        "WHERE execution_id=%(id)s",
                        {"status": status, "stage": failed_stage, "result": Jsonb(clean),
                         "id": execution_id})
            cur.execute("UPDATE jobs SET status=%(status)s, result=%(result)s, updated_at=NOW() "
                        "WHERE job_id=%(id)s",
                        {"status": "failed" if status == "failed" else "running",
                         "result": Jsonb({"ios_ship": clean}), "id": execution_id})
    except Exception:  # request may have been accepted before its response was lost
        # Keep the pre-call ``dispatching`` marker. Retrying this request must
        # not dispatch again until an external reconciler proves the provider
        # did not accept it. Exception text is excluded because it may contain
        # credential or provider-session material.
        error = {"error_code": "dispatch_outcome_unknown",
                 "message": "provider dispatch outcome needs reconciliation"}
        with connection() as conn, conn.cursor() as cur:
            cur.execute("UPDATE ios_ship_executions SET error=%(error)s, updated_at=NOW() "
                        "WHERE execution_id=%(id)s", {"error": Jsonb(error), "id": execution_id})
            cur.execute("UPDATE jobs SET error=%(error)s, updated_at=NOW() "
                        "WHERE job_id=%(id)s", {"error": Jsonb(error), "id": execution_id})
    result = get_execution(org, project, execution_id)
    if result is None:
        raise IosShipError("execution_missing", "durable execution disappeared")
    return result


def _resume_existing_execution(
    org: uuid.UUID, project: uuid.UUID, replay: Dict[str, Any],
    submission: Dict[str, Any],
    dispatch: Callable[[Dict[str, Any]], Dict[str, Any]],
) -> Dict[str, Any]:
    """Resume only a never-dispatched admission or its recorded failed stage."""
    status = replay.get("status")
    if status not in {"queued", "failed"}:
        return replay
    failed_stage = replay.get("failed_stage")
    execution_id = _as_uuid(replay["execution_id"], "execution_id")
    if status == "failed":
        with connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE ios_ship_executions SET status='queued', error=NULL, updated_at=NOW() "
                "WHERE execution_id=%(id)s AND status='failed' AND failed_stage IS NOT DISTINCT "
                "FROM %(stage)s RETURNING execution_id",
                {"id": execution_id, "stage": failed_stage})
            reset = cur.fetchone() is not None
            if reset:
                cur.execute("UPDATE jobs SET status='queued', error=NULL, updated_at=NOW() "
                            "WHERE job_id=%(id)s", {"id": execution_id})
        if not reset:
            current = get_execution(org, project, execution_id)
            return current or replay
    intent = {"kind": EXECUTION_KIND, "execution_id": str(execution_id), **submission}
    return _dispatch_admitted(org, project, execution_id, intent, dispatch)


def launch_execution(
    org_id: Any, tenant_id: str, principal_id: str, project_id: Any, *,
    approval_id: Any, revision: str, source_revision: str, source_sha256: str,
    bundle_identifier: str, marketing_version: str, build_number: str, app_color: str,
    idempotency_key: str,
    dispatch: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]],
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    if dispatch is None:
        raise LaunchSetupRequired()
    current = _now(now)
    org, project = _as_uuid(org_id, "org_id"), _as_uuid(project_id, "project_id")
    approval_uuid = _as_uuid(approval_id, "approval_id")
    if not principal_id or not idempotency_key:
        raise IosShipError("invalid_launch", "principal and Idempotency-Key are required")
    if app_color not in _APP_COLORS:
        raise IosShipError("invalid_launch", "server app color is unavailable")
    principal = _as_uuid(principal_id, "principal_id")
    fields = _launch_fields({
        "revision": revision, "source_revision": source_revision,
        "source_sha256": source_sha256, "bundle_identifier": bundle_identifier,
        "marketing_version": marketing_version, "build_number": build_number})
    submission = {
        "org_id": str(org), "tenant_id": tenant_id, "principal_id": str(principal),
        "project_id": str(project), "approval_id": str(approval_uuid), "app_color": app_color,
        **fields}
    fingerprint = _fingerprint("ios-ship-launch", submission)
    execution_id = new_uuid()

    # This transaction is the one-shot admission boundary. The advisory lock
    # serializes same-key callers, and no provider call occurs before commit.
    replay_to_resume = None
    with connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)",
                    (_advisory_key(f"{org}:{project}:{idempotency_key}"),))
        _require_ship_principal(cur, org, project, principal)
        existing = _existing_execution(cur, org, project, idempotency_key)
        if existing is not None:
            if existing["submission_fingerprint"] != fingerprint:
                raise LaunchConflict("idempotency_conflict",
                                     "idempotency key has different launch input")
            replay = _row_dict(existing)
            if replay is None:  # guarded by existing is not None
                raise IosShipError("execution_missing", "durable execution disappeared")
            if replay.get("status") in {"queued", "failed"}:
                _grant_healthy(cur, org, tenant_id, current)
            replay_to_resume = replay
        else:
            cur.execute("SELECT 1 FROM projects WHERE org_id=%(org)s AND project_id=%(project)s "
                        "AND deleted_at IS NULL AND status='active'",
                        {"org": org, "project": project})
            if cur.fetchone() is None:
                raise ProjectUnavailable("project_unavailable", "project is unavailable")
            _grant_healthy(cur, org, tenant_id, current)
            approval = _approval_record(cur, org, project, revision, for_update=True)
            if approval is None or approval["approval_id"] != str(approval_uuid) \
                    or approval["approved"] is not True:
                raise RevisionNotApproved("unapproved_revision", "approval is unavailable")
            if {key: approval[key] for key in _LAUNCH_FIELDS} != fields:
                raise RevisionNotApproved("approval_mismatch",
                                          "launch identifiers differ from the approved revision")
            if approval["consumed_at"] is not None:
                raise LaunchConflict("approval_consumed", "approval already launched")
            params = {"record_kind": EXECUTION_KIND, "approval_id": str(approval_uuid),
                      "app_color": app_color, **fields}
            cur.execute(
                "INSERT INTO jobs (job_id, project_id, org_id, kind, tool_name, status, params, "
                "request_tenant_id, idempotency_key, submission_fingerprint, execution_context) "
                "VALUES (%(id)s, %(project)s, %(org)s, 'build', %(tool)s, 'queued', %(params)s, "
                "%(tenant)s, %(key)s, %(fingerprint)s, %(context)s)",
                {"id": execution_id, "project": project, "org": org, "tool": TOOL_NAME,
                 "params": Jsonb(params), "tenant": tenant_id, "key": idempotency_key,
                 "fingerprint": fingerprint,
                "context": Jsonb({"kind": EXECUTION_KIND, "principal_id": principal_id,
                                   "app_color": app_color})})
            cur.execute(
                "INSERT INTO ios_ship_executions (execution_id, org_id, project_id, tenant_id, "
                "principal_id, approval_id, revision, source_revision, source_sha256, "
                "bundle_identifier, marketing_version, build_number, app_color, idempotency_key, "
                "submission_fingerprint) VALUES (%(id)s, %(org)s, %(project)s, %(tenant)s, "
                "%(principal)s, %(approval)s, %(revision)s, %(source_revision)s, "
                "%(source_sha256)s, %(bundle_identifier)s, %(marketing_version)s, "
                "%(build_number)s, %(app_color)s, %(key)s, %(fingerprint)s)",
                {"id": execution_id, "org": org, "project": project, "tenant": tenant_id,
                 "principal": principal, "approval": approval_uuid, "key": idempotency_key,
                 "fingerprint": fingerprint, **fields})
            cur.execute("UPDATE ios_ship_revision_approvals SET consumed_at=NOW(), "
                        "consumed_execution_id=%(execution)s WHERE approval_id=%(approval)s",
                        {"execution": execution_id, "approval": approval_uuid})

    if replay_to_resume is not None:
        return _resume_existing_execution(
            org, project, replay_to_resume, submission, dispatch)

    intent = {"kind": EXECUTION_KIND, "execution_id": str(execution_id), **submission}
    return _dispatch_admitted(org, project, execution_id, intent, dispatch)


def _validated_asc_result(value: Any) -> Dict[str, Optional[str]]:
    reject_secret_shaped(value)
    if not isinstance(value, dict) or set(value) - _ASC_RESULT_FIELDS:
        raise IosShipError("invalid_receipt", "App Store Connect result has unsupported fields")
    if not isinstance(value.get("status"), str) or not isinstance(value.get("build_id"), str):
        raise IosShipError("invalid_receipt", "App Store Connect status and build_id are required")
    if any(item is not None and not isinstance(item, str) for item in value.values()):
        raise IosShipError("invalid_receipt", "App Store Connect result values must be strings")
    return {key: value.get(key) for key in sorted(_ASC_RESULT_FIELDS) if key in value}


_RECEIPT_FIELDS = frozenset({
    "kind", "receipt_id", "org_id", "tenant_id", "project_id", "revision",
    "source_revision", "source_sha256", "bundle_identifier", "marketing_version",
    "build_number", "image_identity", "toolchain_identity", "app_store_connect_result",
})


def _clean_receipt(receipt: Any) -> Dict[str, Any]:
    reject_secret_shaped(receipt)
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_FIELDS:
        raise IosShipError("invalid_receipt", "receipt must match the fixed TestFlight schema")
    if receipt.get("kind") != RECEIPT_KIND:
        raise IosShipError("invalid_receipt", f"receipt must be {RECEIPT_KIND}")
    clean = dict(receipt)
    clean["receipt_id"] = str(_as_uuid(clean["receipt_id"], "receipt_id"))
    clean["org_id"] = str(_as_uuid(clean["org_id"], "org_id"))
    clean["project_id"] = str(_as_uuid(clean["project_id"], "project_id"))
    clean.update(_launch_fields(clean))
    for key in ("tenant_id", "image_identity", "toolchain_identity"):
        if not isinstance(clean[key], str) or not clean[key]:
            raise IosShipError("invalid_receipt", f"{key} is required")
    clean["app_store_connect_result"] = _validated_asc_result(
        clean["app_store_connect_result"])
    return clean


def _controller_receipt(receipt: Any) -> Dict[str, Any]:
    if not isinstance(receipt, dict) or set(receipt) != _CONTROLLER_RECEIPT_FIELDS:
        raise IosShipError("invalid_provider_receipt",
                           "controller receipt must match its fixed schema")
    clean = dict(receipt)
    string_fields = _CONTROLLER_RECEIPT_FIELDS - {
        "minimum_allocation_hours", "compliance_answered",
        "credentials_scrubbed",
    }
    if any(not isinstance(clean[name], str) or not clean[name]
           for name in string_fields):
        raise IosShipError("invalid_provider_receipt",
                           "controller receipt identifiers are required")
    if clean["schema"] != _CONTROLLER_RECEIPT_SCHEMA:
        raise IosShipError("invalid_provider_receipt", "controller receipt schema is invalid")
    if not _DIGEST_RE.fullmatch(clean["request_digest"]) \
            or not _DIGEST_RE.fullmatch(clean["source_artifact_digest"]) \
            or not _DIGEST_RE.fullmatch(clean["image_digest"]):
        raise IosShipError("invalid_provider_receipt", "controller receipt digest is invalid")
    if not isinstance(clean["minimum_allocation_hours"], (int, float)) \
            or isinstance(clean["minimum_allocation_hours"], bool) \
            or not math.isfinite(clean["minimum_allocation_hours"]) \
            or clean["minimum_allocation_hours"] <= 0 \
            or not re.fullmatch(r"[0-9]+\.[0-9]{2}", clean["estimated_cost_usd"]):
        raise IosShipError("invalid_provider_receipt", "controller allocation values are invalid")
    try:
        completed = datetime.fromisoformat(clean["completed_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise IosShipError("invalid_provider_receipt",
                           "controller completion time is invalid") from exc
    if completed.tzinfo is None:
        raise IosShipError("invalid_provider_receipt",
                           "controller completion time needs a timezone")
    # These are the controller's terminal proof fields. It emits
    # credentials_scrubbed only after credentials_removed=true and
    # tenant_material_remaining=false have both been validated per-stage.
    if clean["status"] != "VALID" or clean["compliance_answered"] is not True \
            or clean["credentials_scrubbed"] is not True \
            or clean["mac_instance_state"] != "terminated" \
            or clean["dedicated_host_state"] != "released":
        raise IosShipError("terminal_proof_missing",
                           "controller receipt does not prove terminal cleanup")
    return clean


def _provider_projection(execution_row: Any, execution_id: uuid.UUID,
                         raw: Dict[str, Any]) -> Dict[str, Any]:
    raw_digest = hashlib.sha256(json.dumps(
        raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    receipt_id = uuid.uuid5(_RECEIPT_NAMESPACE, f"{execution_id}:{raw_digest}")
    return {
        "kind": RECEIPT_KIND,
        "receipt_id": str(receipt_id),
        "org_id": str(execution_row["org_id"]),
        "tenant_id": execution_row["tenant_id"],
        "project_id": str(execution_row["project_id"]),
        "revision": execution_row["revision"],
        "source_revision": execution_row["source_revision"],
        "source_sha256": execution_row["source_sha256"],
        "bundle_identifier": execution_row["bundle_identifier"],
        "marketing_version": execution_row["marketing_version"],
        "build_number": execution_row["build_number"],
        # Canonical provider identities are stable, lossless strings.
        "image_identity": f"{raw['image_id']}@{raw['image_digest']}",
        "toolchain_identity": f"Xcode {raw['xcode_version']} ({raw['xcode_build']})",
        "app_store_connect_result": {
            "status": "testflight_available",
            "build_id": raw["app_store_connect_build_id"],
            "beta_group": raw["beta_group"],
            "uploaded_at": raw["completed_at"],
        },
    }


def record_provider_receipt(org_id: Any, tenant_id: str, project_id: Any,
                            execution_id: Any, provider_run_id: str,
                            receipt: Dict[str, Any]) -> Dict[str, Any]:
    """Verify a terminal controller receipt, then project the browser receipt."""
    org, project = _as_uuid(org_id, "org_id"), _as_uuid(project_id, "project_id")
    execution = _as_uuid(execution_id, "execution_id")
    if not tenant_id or not provider_run_id:
        raise IosShipError("provider_scope_mismatch",
                           "provider callback does not match the admitted execution")
    raw = _controller_receipt(receipt)
    with connection() as conn, conn.cursor() as cur:
        row = _provider_execution_row(
            cur, org, tenant_id, project, execution, provider_run_id, for_update=False)
    expected = {
        "run_id": provider_run_id,
        "tenant_id": tenant_id,
        "project_id": str(project),
        "source_revision": row["source_revision"],
        "source_artifact_digest": f"sha256:{row['source_sha256']}",
        "bundle_id": row["bundle_identifier"],
        "marketing_version": row["marketing_version"],
        "build_number": row["build_number"],
    }
    if any(raw[name] != value for name, value in expected.items()):
        raise IosShipError("receipt_identity_mismatch",
                           "controller receipt identity differs from the admitted execution")
    projected = _provider_projection(row, execution, raw)
    return record_receipt(org, project, execution, projected)


def record_receipt(org_id: Any, project_id: Any, execution_id: Any,
                   receipt: Dict[str, Any]) -> Dict[str, Any]:
    clean = _clean_receipt(receipt)
    org, project = _as_uuid(org_id, "org_id"), _as_uuid(project_id, "project_id")
    execution, receipt_id = (_as_uuid(execution_id, "execution_id"),
                             _as_uuid(clean["receipt_id"], "receipt_id"))
    if clean["org_id"] != str(org) or clean["project_id"] != str(project):
        raise IosShipError("receipt_scope_mismatch", "receipt project scope does not match")
    digest = canonical_hash(RECEIPT_KIND, clean)
    with connection() as conn, conn.cursor() as cur:
        execution_row = _get_execution_row(cur, org, project, execution)
        if execution_row is None:
            raise IosShipError("execution_missing", "receipt execution is unavailable")
        if any(clean[key] != _iso(execution_row[key]) for key in _LAUNCH_FIELDS) \
                or clean["tenant_id"] != execution_row["tenant_id"]:
            raise IosShipError("receipt_identity_mismatch",
                               "receipt identity differs from the admitted execution")
        cur.execute("SELECT hash_value FROM ios_ship_receipts WHERE receipt_id=%(id)s",
                    {"id": receipt_id})
        existing = cur.fetchone()
        if existing is not None:
            if existing["hash_value"] != digest.value:
                raise ReceiptTampered("receipt_tampered", "receipt id has different content")
        else:
            cur.execute(
                "INSERT INTO ios_ship_receipts (receipt_id, execution_id, org_id, project_id, "
                "tenant_id, kind, revision, source_revision, source_sha256, bundle_identifier, "
                "marketing_version, build_number, image_identity, toolchain_identity, "
                "app_store_connect_result, hash_algorithm, hash_canonicalization, hash_domain, "
                "hash_value) VALUES (%(receipt_id)s, %(execution)s, %(org)s, %(project)s, "
                "%(tenant_id)s, %(kind)s, %(revision)s, %(source_revision)s, "
                "%(source_sha256)s, %(bundle_identifier)s, %(marketing_version)s, "
                "%(build_number)s, %(image_identity)s, %(toolchain_identity)s, %(asc)s, "
                "%(algorithm)s, %(canonicalization)s, %(domain)s, %(value)s)",
                {"receipt_id": receipt_id, "execution": execution, "org": org,
                 "project": project, "asc": Jsonb(clean["app_store_connect_result"]),
                 **{key: clean[key] for key in (
                     "tenant_id", "kind", "revision", "source_revision", "source_sha256",
                     "bundle_identifier", "marketing_version", "build_number",
                     "image_identity", "toolchain_identity")}, **digest.to_dict()})
            cur.execute("UPDATE ios_ship_executions SET status='succeeded', "
                        "receipt_id=%(receipt)s, failed_stage=NULL, error=NULL, updated_at=NOW() "
                        "WHERE execution_id=%(execution)s",
                        {"receipt": receipt_id, "execution": execution})
            cur.execute("UPDATE jobs SET status='succeeded', result=%(result)s, updated_at=NOW() "
                        "WHERE job_id=%(execution)s",
                        {"result": Jsonb({"receipt_id": str(receipt_id),
                                          "receipt_hash": digest.value}),
                         "execution": execution})
    return {"hash": digest.value, **clean}


def read_receipt(org_id: Any, project_id: Any, receipt_id: Any) -> Optional[Dict[str, Any]]:
    org, project = _as_uuid(org_id, "org_id"), _as_uuid(project_id, "project_id")
    receipt_uuid = _as_uuid(receipt_id, "receipt_id")
    columns = (
        "receipt_id, execution_id, org_id, project_id, tenant_id, kind, revision, "
        "source_revision, source_sha256, bundle_identifier, marketing_version, build_number, "
        "image_identity, toolchain_identity, app_store_connect_result, hash_value, created_at"
    )
    with connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT " + columns + " FROM ios_ship_receipts WHERE org_id=%(org)s "
                    "AND project_id=%(project)s AND receipt_id=%(receipt)s",
                    {"org": org, "project": project, "receipt": receipt_uuid})
        row = cur.fetchone()
    if row is None:
        return None
    payload = {key: _iso(row[key]) for key in _RECEIPT_FIELDS}
    clean = _clean_receipt(payload)
    digest = canonical_hash(RECEIPT_KIND, clean)
    if digest.value != row["hash_value"]:
        raise ReceiptTampered("receipt_tampered", "stored receipt failed canonical verification")
    return {"execution_id": str(row["execution_id"]), "hash": digest.value,
            "created_at": _iso(row["created_at"]), **clean}
