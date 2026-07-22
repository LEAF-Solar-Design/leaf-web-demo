"""Domain models for the canonical Project/Job entity.

Plain dataclasses that mirror the five tables in migrations/0001_project_job.sql
one-to-one, plus row->model mappers. Kept dependency-free (no pydantic here) so
the store/offboard layers stay importable without the web stack. The API layer
(api.py) defines its own request/response pydantic models.

Tier / status vocabularies mirror the cadwalk-studio tenancy prior art
(DeploymentTier = self_hosted | hosted_starter | hosted_pro).
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

# --- controlled vocabularies (mirror cadwalk-studio/src/lib/tenancy/types.ts) ---
TIERS = ("self_hosted", "hosted_starter", "hosted_pro")
ORG_STATUSES = ("active", "offboarding", "deleted")
PROJECT_STATUSES = ("active", "archived", "deleted")
JOB_KINDS = ("run", "solve", "build", "extract")
JOB_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")
AUTHORITY_MODES = ("legacy_sqlite", "postgres_canonical")
HASH_ALGORITHM = "sha256"
HASH_CANONICALIZATION = "RFC8785-JCS"


@dataclass(frozen=True)
class CanonicalHash:
    """Self-describing, domain-separated canonical record digest."""

    algorithm: str
    canonicalization: str
    domain: str
    value: str

    def to_dict(self) -> Dict[str, str]:
        return {"algorithm": self.algorithm, "canonicalization": self.canonicalization,
                "domain": self.domain, "value": self.value}


def canonical_json_bytes(value: Any) -> bytes:
    """Return RFC 8785 JCS bytes.

    There is deliberately no look-alike fallback: stdlib sorted JSON differs
    from RFC 8785 for numbers and Unicode edge cases and must never be labelled
    as authoritative JCS evidence.
    """
    import rfc8785  # type: ignore[import-not-found]
    return rfc8785.dumps(value)


def canonical_hash(record_type: str, payload: Any) -> CanonicalHash:
    domain = f"leaf:{record_type}:v1"
    digest = hashlib.sha256(domain.encode("utf-8") + b"\0" + canonical_json_bytes(payload)).hexdigest()
    return CanonicalHash(HASH_ALGORITHM, HASH_CANONICALIZATION, domain, digest)


def verify_canonical_hash(record_type: str, payload: Any, metadata: Dict[str, str]) -> bool:
    expected = canonical_hash(record_type, payload)
    return metadata == expected.to_dict()


@dataclass(frozen=True)
class IdentityBinding:
    binding_id: uuid.UUID
    external_authority: str
    external_subject: str
    platform_tenant_id: uuid.UUID
    platform_user_id: uuid.UUID
    status: str = "active"
    created_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, r: Dict[str, Any]) -> "IdentityBinding":
        return cls(r["binding_id"], r["external_authority"], r["external_subject"],
                   r["platform_tenant_id"], r["platform_user_id"], r["status"], r.get("created_at"))


@dataclass(frozen=True)
class HistoryOperation:
    operation_id: uuid.UUID
    org_id: uuid.UUID
    project_id: uuid.UUID
    operation_type: str
    payload: Dict[str, Any]
    content_hash: CanonicalHash
    idempotency_key: str

    @classmethod
    def from_row(cls, r: Dict[str, Any]) -> "HistoryOperation":
        return cls(r["operation_id"], r["org_id"], r["project_id"], r["operation_type"],
                   r["payload"], CanonicalHash(r["hash_algorithm"], r["hash_canonicalization"],
                   r["hash_domain"], r["hash_value"]), r["idempotency_key"])


@dataclass(frozen=True)
class SolveRecord:
    solve_id: uuid.UUID
    org_id: uuid.UUID
    project_id: uuid.UUID
    payload: Dict[str, Any]
    content_hash: CanonicalHash
    idempotency_key: str

    @classmethod
    def from_row(cls, r: Dict[str, Any]) -> "SolveRecord":
        return cls(r["solve_id"], r["org_id"], r["project_id"], r["payload"],
                   CanonicalHash(r["hash_algorithm"], r["hash_canonicalization"],
                   r["hash_domain"], r["hash_value"]), r["idempotency_key"])


def new_uuid() -> uuid.UUID:
    """UUID id convention (the prior art uses UUIDv7; stdlib uuid4 is fine for v1)."""
    return uuid.uuid4()


@dataclass
class Org:
    org_id: uuid.UUID
    name: str
    tier: str = "hosted_starter"
    status: str = "active"
    created_at: Optional[datetime] = None
    offboarded_at: Optional[datetime] = None
    # deletion/compliance columns (migration 0002; DELETION-OFFBOARDING-DESIGN.md sec 4)
    deleted_at: Optional[datetime] = None
    purge_requested_at: Optional[datetime] = None
    purge_completed_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, r: Dict[str, Any]) -> "Org":
        return cls(
            org_id=r["org_id"], name=r["name"], tier=r["tier"], status=r["status"],
            created_at=r.get("created_at"), offboarded_at=r.get("offboarded_at"),
            deleted_at=r.get("deleted_at"),
            purge_requested_at=r.get("purge_requested_at"),
            purge_completed_at=r.get("purge_completed_at"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "org_id": str(self.org_id), "name": self.name, "tier": self.tier,
            "status": self.status, "created_at": _iso(self.created_at),
            "offboarded_at": _iso(self.offboarded_at),
            "deleted_at": _iso(self.deleted_at),
            "purge_requested_at": _iso(self.purge_requested_at),
            "purge_completed_at": _iso(self.purge_completed_at),
        }


@dataclass
class Project:
    project_id: uuid.UUID
    org_id: uuid.UUID
    name: str
    status: str = "active"
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    # deletion/compliance columns (migration 0002; DELETION-OFFBOARDING-DESIGN.md sec 4)
    deleted_at: Optional[datetime] = None
    purge_requested_at: Optional[datetime] = None
    purge_completed_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, r: Dict[str, Any]) -> "Project":
        return cls(
            project_id=r["project_id"], org_id=r["org_id"], name=r["name"],
            status=r["status"], created_at=r.get("created_at"), updated_at=r.get("updated_at"),
            deleted_at=r.get("deleted_at"),
            purge_requested_at=r.get("purge_requested_at"),
            purge_completed_at=r.get("purge_completed_at"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_id": str(self.project_id), "org_id": str(self.org_id),
            "name": self.name, "status": self.status,
            "created_at": _iso(self.created_at), "updated_at": _iso(self.updated_at),
            "deleted_at": _iso(self.deleted_at),
            "purge_requested_at": _iso(self.purge_requested_at),
            "purge_completed_at": _iso(self.purge_completed_at),
        }


@dataclass
class DrawingArtifact:
    drawing_id: uuid.UUID
    project_id: uuid.UUID
    org_id: uuid.UUID
    name: str
    status: str = "active"
    created_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, r: Dict[str, Any]) -> "DrawingArtifact":
        return cls(r["drawing_id"], r["project_id"], r["org_id"], r["name"],
                   r["status"], r.get("created_at"))

    def to_dict(self) -> Dict[str, Any]:
        return {"drawing_id": str(self.drawing_id), "project_id": str(self.project_id),
                "org_id": str(self.org_id), "name": self.name, "status": self.status,
                "created_at": _iso(self.created_at)}


@dataclass
class DrawingVersion:
    version_id: uuid.UUID
    drawing_id: uuid.UUID
    project_id: uuid.UUID
    org_id: uuid.UUID
    seq: int
    oss_object: Optional[str] = None
    intake_ref: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None
    # deletion/compliance columns (migration 0002; DELETION-OFFBOARDING-DESIGN.md sec 4)
    deleted_at: Optional[datetime] = None
    purge_requested_at: Optional[datetime] = None
    purge_completed_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, r: Dict[str, Any]) -> "DrawingVersion":
        return cls(
            version_id=r["version_id"], drawing_id=r["drawing_id"],
            project_id=r["project_id"], org_id=r["org_id"],
            seq=r["seq"], oss_object=r.get("oss_object"), intake_ref=r.get("intake_ref"),
            created_by=r.get("created_by"), created_at=r.get("created_at"),
            deleted_at=r.get("deleted_at"),
            purge_requested_at=r.get("purge_requested_at"),
            purge_completed_at=r.get("purge_completed_at"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version_id": str(self.version_id), "drawing_id": str(self.drawing_id),
            "project_id": str(self.project_id),
            "org_id": str(self.org_id), "seq": self.seq, "oss_object": self.oss_object,
            "intake_ref": self.intake_ref, "created_by": self.created_by,
            "created_at": _iso(self.created_at),
            "deleted_at": _iso(self.deleted_at),
            "purge_requested_at": _iso(self.purge_requested_at),
            "purge_completed_at": _iso(self.purge_completed_at),
        }


@dataclass
class Job:
    job_id: uuid.UUID
    project_id: uuid.UUID
    org_id: uuid.UUID
    kind: str
    tool_name: Optional[str] = None
    status: str = "queued"
    spine_ref: Optional[str] = None
    params: Optional[Dict[str, Any]] = None
    result: Optional[Dict[str, Any]] = None
    input_version_id: Optional[uuid.UUID] = None
    output_version_id: Optional[uuid.UUID] = None
    cost_usd: Optional[Decimal] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    # deletion/compliance columns (migration 0002; DELETION-OFFBOARDING-DESIGN.md sec 4)
    deleted_at: Optional[datetime] = None
    purge_requested_at: Optional[datetime] = None
    purge_completed_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, r: Dict[str, Any]) -> "Job":
        return cls(
            job_id=r["job_id"], project_id=r["project_id"], org_id=r["org_id"],
            kind=r["kind"], tool_name=r.get("tool_name"), status=r["status"],
            spine_ref=r.get("spine_ref"), params=r.get("params"), result=r.get("result"),
            input_version_id=r.get("input_version_id"), output_version_id=r.get("output_version_id"),
            cost_usd=r.get("cost_usd"), created_at=r.get("created_at"), updated_at=r.get("updated_at"),
            deleted_at=r.get("deleted_at"),
            purge_requested_at=r.get("purge_requested_at"),
            purge_completed_at=r.get("purge_completed_at"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": str(self.job_id), "project_id": str(self.project_id),
            "org_id": str(self.org_id), "kind": self.kind, "tool_name": self.tool_name,
            "status": self.status, "spine_ref": self.spine_ref, "params": self.params,
            "result": self.result,
            "input_version_id": _s(self.input_version_id),
            "output_version_id": _s(self.output_version_id),
            "cost_usd": (float(self.cost_usd) if self.cost_usd is not None else None),
            "created_at": _iso(self.created_at), "updated_at": _iso(self.updated_at),
            "deleted_at": _iso(self.deleted_at),
            "purge_requested_at": _iso(self.purge_requested_at),
            "purge_completed_at": _iso(self.purge_completed_at),
        }


@dataclass
class BuiltTool:
    tool_id: uuid.UUID
    project_id: uuid.UUID
    org_id: uuid.UUID
    name: str
    manifest: Dict[str, Any]
    version: str = "1.0.0"
    source_ref: Optional[str] = None
    provenance: Optional[Dict[str, Any]] = None
    created_at: Optional[datetime] = None
    # deletion/compliance columns (migration 0002; DELETION-OFFBOARDING-DESIGN.md sec 4)
    deleted_at: Optional[datetime] = None
    purge_requested_at: Optional[datetime] = None
    purge_completed_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, r: Dict[str, Any]) -> "BuiltTool":
        return cls(
            tool_id=r["tool_id"], project_id=r["project_id"], org_id=r["org_id"],
            name=r["name"], manifest=r["manifest"], version=r["version"],
            source_ref=r.get("source_ref"), provenance=r.get("provenance"),
            created_at=r.get("created_at"),
            deleted_at=r.get("deleted_at"),
            purge_requested_at=r.get("purge_requested_at"),
            purge_completed_at=r.get("purge_completed_at"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool_id": str(self.tool_id), "project_id": str(self.project_id),
            "org_id": str(self.org_id), "name": self.name, "version": self.version,
            "manifest": self.manifest, "source_ref": self.source_ref,
            "provenance": self.provenance, "created_at": _iso(self.created_at),
            "deleted_at": _iso(self.deleted_at),
            "purge_requested_at": _iso(self.purge_requested_at),
            "purge_completed_at": _iso(self.purge_completed_at),
        }


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


def _s(v: Any) -> Optional[str]:
    return str(v) if v is not None else None
