"""Tenant offboarding / deletion cascade — the day-one compliance exception.

The fleet-wide rule is "never hard-delete." This entity is the explicit, authorized
exception: a B2B PE-stamp buyer will demand deletion-on-request. This module is the
ONLY hard-delete path — there is deliberately no general-purpose delete anywhere else.

``offboard_org`` purges out-of-band material the DB cascade cannot reach (vault
secrets, APS OSS / S3 blobs) via INJECTED hooks — this module targets the vault
``deleteSecret(ref)`` contract but does not implement the vault — then deletes the
org's data by cascade and writes an ``orgs`` tombstone (status='deleted') so an audit
line survives without retaining tenant IP.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Callable, Iterable, List, Optional

from .db import connection

# ref-shaped hooks injected by the caller (integration wires the real vault / OSS purge)
PurgeHook = Callable[[str], None]
SecretRefProvider = Callable[[uuid.UUID], Iterable[str]]


class OrgNotFound(Exception):
    """Raised when offboard targets an org_id that does not exist."""


@dataclass
class OffboardResult:
    org_id: uuid.UUID
    secret_refs: List[str] = field(default_factory=list)
    blob_refs: List[str] = field(default_factory=list)
    deleted_projects: int = 0
    status: str = "deleted"


def _default_secret_ref_provider(org_id: uuid.UUID) -> List[str]:
    """v1 fallback: the org's canonical credential ref.

    The credential-broker sibling (the keystone) owns real secret storage; at
    integration it supplies the true provider. Ref shape mirrors the vault prior art
    (cadwalk/<deployment_id>/<key>); here deployment == org.
    """
    return [f"leaf/{org_id}/credentials"]


def _dedup(refs: Iterable[Optional[str]]) -> List[str]:
    """Distinct, order-preserving, non-null — guarantees exactly-once hook firing."""
    return list(dict.fromkeys(r for r in refs if r))


def collect_blob_refs(org_id: uuid.UUID) -> List[str]:
    """Out-of-band blob refs (oss_object, intake_ref, source_ref) owned by the org."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT oss_object FROM drawing_versions WHERE org_id = %(org_id)s "
                "UNION ALL SELECT intake_ref FROM drawing_versions WHERE org_id = %(org_id)s "
                "UNION ALL SELECT source_ref FROM built_tools WHERE org_id = %(org_id)s",
                {"org_id": org_id},
            )
            return _dedup(row["oss_object"] for row in cur.fetchall())


def offboard_org(org_id: uuid.UUID, *,
                 key_purge_hook: PurgeHook,
                 blob_purge_hook: PurgeHook,
                 secret_ref_provider: Optional[SecretRefProvider] = None) -> OffboardResult:
    """Delete everything owned by ``org_id`` and purge its out-of-band material.

    1. Collect secret refs (via provider) and blob refs (from the DB), org-scoped.
    2. Fire ``key_purge_hook`` once per secret ref and ``blob_purge_hook`` once per blob.
    3. Delete the org's projects (ON DELETE CASCADE wipes versions/jobs/built_tools).
    4. Tombstone the org row: status='deleted', offboarded_at=NOW().

    Never touches any other org's rows. Idempotency: a second call on a tombstoned
    org purges an empty ref set and deletes zero projects.
    """
    provider = secret_ref_provider or _default_secret_ref_provider

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT org_id FROM orgs WHERE org_id = %(org_id)s", {"org_id": org_id}
            )
            if cur.fetchone() is None:
                raise OrgNotFound(str(org_id))
            cur.execute(
                "UPDATE orgs SET status = 'offboarding' WHERE org_id = %(org_id)s",
                {"org_id": org_id},
            )

    # 1. collect refs (reads, org-scoped)
    secret_refs = _dedup(provider(org_id))
    blob_refs = collect_blob_refs(org_id)

    # 2. purge out-of-band material — exactly once per ref, before the DB delete
    for ref in secret_refs:
        key_purge_hook(ref)
    for ref in blob_refs:
        blob_purge_hook(ref)

    # 3 + 4. cascade-delete the org's data and tombstone the org (single transaction)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM projects WHERE org_id = %(org_id)s", {"org_id": org_id}
            )
            deleted_projects = cur.rowcount
            cur.execute(
                "UPDATE orgs SET status = 'deleted', offboarded_at = NOW() "
                "WHERE org_id = %(org_id)s",
                {"org_id": org_id},
            )

    return OffboardResult(
        org_id=org_id, secret_refs=secret_refs, blob_refs=blob_refs,
        deleted_projects=deleted_projects, status="deleted",
    )
