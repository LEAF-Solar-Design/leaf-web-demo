"""Canonical drawing bindings and persistent checkout generations."""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from . import db, store
from .models import DrawingVersion


class ProjectContextError(Exception):
    """Closed refusal vocabulary shared by the store and HTTP boundary."""

    def __init__(self, reason_code: str, *, checkout=None):
        self.reason_code = reason_code
        self.checkout = checkout
        super().__init__(reason_code)


def refuse(reason: str, *, checkout=None):
    raise ProjectContextError("SIP_R1_" + reason, checkout=checkout)


@dataclass(frozen=True)
class VersionBinding:
    organization_id: UUID
    project_id: UUID
    drawing_id: UUID
    input_version_id: UUID
    head_version_id: UUID
    is_head: bool
    version: DrawingVersion


@dataclass(frozen=True)
class CheckoutLease:
    organization_id: UUID
    project_id: UUID
    drawing_id: UUID
    holder: str
    holder_binding_id: UUID
    acquired_at: datetime
    expires_at: datetime
    fence: int


@dataclass(frozen=True)
class CheckoutState:
    organization_id: UUID
    project_id: UUID
    drawing_id: UUID
    checkout: CheckoutLease | None
    last_fence: int
    observed_at: datetime


def _ids(*values):
    if any(not isinstance(value, UUID) for value in values):
        refuse("INVALID_BINDING")


def _clock(cur):
    """Private database-clock seam; sampled only after scope and row locks."""
    cur.execute("SELECT clock_timestamp() AS observed_at")
    return cur.fetchone()["observed_at"].astimezone(timezone.utc)


def _scope(cur, org_id, project_id, drawing_id, *, actor_binding_id=None):
    args = {"org_id": org_id, "project_id": project_id, "drawing_id": drawing_id}
    cur.execute(
        "SELECT project_id FROM live_projects WHERE org_id = %(org_id)s "
        "AND project_id = %(project_id)s FOR SHARE", args,
    )
    if cur.fetchone() is None:
        refuse("CONTEXT_NOT_FOUND")
    cur.execute(
        "SELECT org_id FROM orgs WHERE org_id = %(org_id)s AND status = 'active' FOR SHARE",
        args,
    )
    if cur.fetchone() is None:
        refuse("CONTEXT_NOT_FOUND")
    cur.execute(
        "SELECT authority_mode FROM live_project_authority_modes "
        "WHERE org_id = %(org_id)s AND project_id = %(project_id)s FOR SHARE", args,
    )
    authority = cur.fetchone()
    if authority is None:
        cur.execute(
            "SELECT authority_mode FROM tenant_authority_modes "
            "WHERE org_id = %(org_id)s FOR SHARE", args,
        )
        authority = cur.fetchone()
    if authority is None or authority["authority_mode"] != "postgres_canonical":
        refuse("CANONICAL_AUTHORITY_REQUIRED")
    cur.execute(
        "SELECT drawing_id FROM drawing_artifacts WHERE org_id = %(org_id)s "
        "AND project_id = %(project_id)s AND drawing_id = %(drawing_id)s "
        "AND status = 'active' FOR UPDATE", args,
    )
    if cur.fetchone() is None:
        refuse("CONTEXT_NOT_FOUND")
    if actor_binding_id is not None:
        args["actor"] = actor_binding_id
        cur.execute(
            "SELECT binding_id FROM identity_bindings WHERE platform_tenant_id = %(org_id)s "
            "AND binding_id = %(actor)s AND status = 'active' FOR SHARE", args,
        )
        if cur.fetchone() is None:
            refuse("PROJECT_FORBIDDEN")
        cur.execute(
            "SELECT role FROM project_member_bindings WHERE org_id = %(org_id)s "
            "AND project_id = %(project_id)s AND binding_id = %(actor)s "
            "AND status = 'active' FOR SHARE", args,
        )
        membership = cur.fetchone()
        if membership is None or membership["role"] not in {"owner", "editor"}:
            refuse("PROJECT_FORBIDDEN")


def resolve_version_binding(org_id, project_id, input_version_id, *, drawing_id=None, conn=None):
    _ids(org_id, project_id, input_version_id)
    if drawing_id is not None:
        _ids(drawing_id)
    if conn is None:
        return db.run_transaction(lambda c: resolve_version_binding(
            org_id, project_id, input_version_id, drawing_id=drawing_id, conn=c))
    version = store.get_drawing_version(org_id, project_id, input_version_id, conn=conn)
    if version is None or (drawing_id is not None and version.drawing_id != drawing_id):
        refuse("CONTEXT_NOT_FOUND")
    with conn.cursor() as cur:
        _scope(cur, org_id, project_id, version.drawing_id)
        cur.execute(
            "SELECT version_id FROM drawing_versions WHERE org_id = %(org_id)s "
            "AND project_id = %(project_id)s AND drawing_id = %(drawing_id)s "
            "AND deleted_at IS NULL ORDER BY seq DESC LIMIT 1",
            {"org_id": org_id, "project_id": project_id, "drawing_id": version.drawing_id},
        )
        head = cur.fetchone()
        if head is None:
            refuse("CONTEXT_NOT_FOUND")
    return VersionBinding(org_id, project_id, version.drawing_id, input_version_id,
                          head["version_id"], head["version_id"] == input_version_id, version)


def _state(cur, org_id, project_id, drawing_id, *, for_update):
    cur.execute(
        "SELECT * FROM project_drawing_checkouts WHERE org_id = %(org_id)s "
        "AND project_id = %(project_id)s AND drawing_id = %(drawing_id)s "
        + ("FOR UPDATE" if for_update else ""),
        {"org_id": org_id, "project_id": project_id, "drawing_id": drawing_id},
    )
    row = cur.fetchone()
    observed = _clock(cur)
    lease = None
    if row is not None and row["holder"] is not None:
        lease = CheckoutLease(org_id, project_id, drawing_id, row["holder"],
                              row["holder_binding_id"], row["acquired_at"].astimezone(timezone.utc),
                              row["expires_at"].astimezone(timezone.utc), int(row["fence"]))
    return CheckoutState(org_id, project_id, drawing_id, lease,
                         int(row["fence"]) if row else 0, observed)


def get_checkout(org_id, project_id, drawing_id, *, conn=None, for_update=False):
    _ids(org_id, project_id, drawing_id)
    if conn is None:
        return db.run_transaction(lambda c: get_checkout(
            org_id, project_id, drawing_id, conn=c, for_update=for_update))
    with conn.cursor() as cur:
        _scope(cur, org_id, project_id, drawing_id)
        return _state(cur, org_id, project_id, drawing_id, for_update=for_update)


def validate_checkout_params(holder, ttl_s):
    if (not isinstance(holder, str) or not 1 <= len(holder.strip()) <= 200
            or holder.strip() == "anonymous:unnamed-writer"
            or isinstance(ttl_s, bool) or not isinstance(ttl_s, (int, float))):
        refuse("CHECKOUT_PARAMS_INVALID")
    try:
        valid = math.isfinite(ttl_s) and 0 < ttl_s <= 86400
    except (OverflowError, ValueError):
        valid = False
    if not valid:
        refuse("CHECKOUT_PARAMS_INVALID")
    return holder.strip(), float(ttl_s)


def _trusted_fence(value, *, optional=False):
    if optional and value is None:
        return
    if type(value) is not int or value < 1 or value > 9223372036854775807:
        refuse("CHECKOUT_PARAMS_INVALID")


def acquire_checkout(org_id, project_id, drawing_id, *, actor_binding_id, holder,
                     ttl_s, expected_fence, conn):
    _ids(org_id, project_id, drawing_id, actor_binding_id)
    holder, ttl_s = validate_checkout_params(holder, ttl_s)
    _trusted_fence(expected_fence, optional=True)
    with conn.cursor() as cur:
        _scope(cur, org_id, project_id, drawing_id, actor_binding_id=actor_binding_id)
        args = {"org_id": org_id, "project_id": project_id, "drawing_id": drawing_id}
        cur.execute(
            "INSERT INTO project_drawing_checkouts (org_id, project_id, drawing_id) "
            "VALUES (%(org_id)s, %(project_id)s, %(drawing_id)s) ON CONFLICT DO NOTHING", args,
        )
        prior = _state(cur, org_id, project_id, drawing_id, for_update=True)
        lease = prior.checkout
        if expected_fence is not None and (lease is None or lease.fence != expected_fence):
            refuse("CHECKOUT_STALE")
        if lease is not None and lease.expires_at > prior.observed_at:
            if expected_fence is None or lease.holder_binding_id != actor_binding_id:
                refuse("CHECKOUT_CONFLICT", checkout=lease)
        if prior.last_fence == 9223372036854775807:
            refuse("FENCE_EXHAUSTED")
        acquired = prior.observed_at
        expires = acquired + timedelta(seconds=ttl_s)
        if expires <= acquired:
            refuse("CHECKOUT_PARAMS_INVALID")
        fence = prior.last_fence + 1
        args.update(holder=holder, actor=actor_binding_id, acquired=acquired,
                    expires=expires, fence=fence)
        cur.execute(
            "UPDATE project_drawing_checkouts SET holder = %(holder)s, "
            "holder_binding_id = %(actor)s, acquired_at = %(acquired)s, "
            "expires_at = %(expires)s, fence = %(fence)s, updated_at = clock_timestamp() "
            "WHERE org_id = %(org_id)s AND project_id = %(project_id)s "
            "AND drawing_id = %(drawing_id)s", args,
        )
        return CheckoutLease(org_id, project_id, drawing_id, holder, actor_binding_id,
                             acquired, expires, fence)


def release_checkout(org_id, project_id, drawing_id, *, actor_binding_id, expected_fence, conn):
    _ids(org_id, project_id, drawing_id, actor_binding_id)
    _trusted_fence(expected_fence, optional=True)
    with conn.cursor() as cur:
        _scope(cur, org_id, project_id, drawing_id, actor_binding_id=actor_binding_id)
        state = _state(cur, org_id, project_id, drawing_id, for_update=True)
        lease = state.checkout
        if expected_fence is not None and (lease is None or lease.fence != expected_fence):
            refuse("CHECKOUT_STALE")
        if lease is None:
            return False
        if lease.expires_at > state.observed_at:
            if expected_fence is None or lease.holder_binding_id != actor_binding_id:
                refuse("CHECKOUT_DENIED")
        cur.execute(
            "UPDATE project_drawing_checkouts SET holder = NULL, holder_binding_id = NULL, "
            "acquired_at = NULL, expires_at = NULL, updated_at = clock_timestamp() "
            "WHERE org_id = %(org_id)s AND project_id = %(project_id)s "
            "AND drawing_id = %(drawing_id)s",
            {"org_id": org_id, "project_id": project_id, "drawing_id": drawing_id},
        )
        return True


def verify_checkout(org_id, project_id, drawing_id, *, actor_binding_id, expected_fence, conn=None):
    _ids(org_id, project_id, drawing_id, actor_binding_id)
    _trusted_fence(expected_fence)
    if conn is None:
        return db.run_transaction(lambda c: verify_checkout(
            org_id, project_id, drawing_id,
            actor_binding_id=actor_binding_id, expected_fence=expected_fence, conn=c))
    with conn.cursor() as cur:
        _scope(cur, org_id, project_id, drawing_id, actor_binding_id=actor_binding_id)
        state = _state(cur, org_id, project_id, drawing_id, for_update=True)
    lease = state.checkout
    if lease is None:
        refuse("CHECKOUT_REQUIRED")
    if lease.expires_at <= state.observed_at:
        refuse("CHECKOUT_EXPIRED")
    if lease.fence != expected_fence:
        refuse("CHECKOUT_STALE")
    if lease.holder_binding_id != actor_binding_id:
        refuse("CHECKOUT_DENIED")
    return lease


PUBLICATION_SCHEMA = "leaf.project-drawing-publication.v1"


def validate_publication_params(org_id, project_id, drawing_id, parent_id,
                                actor_binding_id, expected_fence, request_id, intake_sha256):
    _ids(org_id, project_id, drawing_id, parent_id, actor_binding_id)
    _trusted_fence(expected_fence)
    if (not isinstance(request_id, UUID) or not isinstance(intake_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", intake_sha256) is None):
        raise ProjectContextError("SIP_R2_PUBLICATION_PARAMS_INVALID")


def publication_intake_key(org_id, project_id, drawing_id, digest):
    return (f"tenants/{org_id}/projects/{project_id}/drawings/{drawing_id}/"
            f"intakes/{digest}.json")


def _publication_proof(org_id, project_id, drawing_id, parent_id, actor, fence,
                       request_id, digest, base_object_ref):
    return {"schema": PUBLICATION_SCHEMA, "organization_id": str(org_id),
            "project_id": str(project_id), "drawing_id": str(drawing_id),
            "parent_version_id": str(parent_id), "request_id": str(request_id),
            "actor_binding_id": str(actor), "checkout_fence": str(fence),
            "intake": {"ref": publication_intake_key(org_id, project_id, drawing_id, digest),
                       "sha256": digest}, "base_object_ref": base_object_ref}


def _publication_fingerprint(proof):
    return hashlib.sha256(json.dumps(proof, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def publish_version(org_id: UUID, project_id: UUID, drawing_id: UUID, *,
                    expected_parent_version_id: UUID, actor_binding_id: UUID,
                    expected_fence: int, request_id: UUID, intake_sha256: str,
                    conn, before_new=None) -> DrawingVersion:
    """Publish under the artifact fence; the caller owns commit and rollback."""
    validate_publication_params(org_id, project_id, drawing_id, expected_parent_version_id,
                                actor_binding_id, expected_fence, request_id, intake_sha256)
    args = {"org_id": org_id, "project_id": project_id, "drawing_id": drawing_id,
            "parent_id": expected_parent_version_id, "actor_binding_id": actor_binding_id,
            "expected_fence": expected_fence, "key": "sip-r2:" + str(request_id)}
    with conn.cursor() as cur:
        _scope(cur, org_id, project_id, drawing_id, actor_binding_id=actor_binding_id)

        def replay():
            # Tombstones retain their request keys forever.
            cur.execute(
                "SELECT * FROM drawing_versions WHERE org_id = %(org_id)s "
                "AND project_id = %(project_id)s AND idempotency_key = %(key)s", args)
            row = cur.fetchone()
            if row is None:
                return None
            if row["deleted_at"] is not None:
                refuse("CONTEXT_NOT_FOUND")
            proof = _publication_proof(org_id, project_id, drawing_id,
                expected_parent_version_id, actor_binding_id, expected_fence,
                request_id, intake_sha256, row["oss_object"])
            if (row["drawing_id"] != drawing_id or row["provenance"] != proof
                    or row["intake_ref"] != proof["intake"]["ref"]
                    or row["import_fingerprint"] != _publication_fingerprint(proof)):
                raise ProjectContextError("SIP_R2_IDEMPOTENCY_CONFLICT")
            return DrawingVersion.from_row(row)

        prior = replay()
        if prior is not None:
            return prior
        if before_new is not None:
            before_new()
        cur.execute(
            "SELECT * FROM drawing_versions WHERE org_id = %(org_id)s "
            "AND project_id = %(project_id)s AND drawing_id = %(drawing_id)s "
            "AND version_id = %(parent_id)s AND deleted_at IS NULL FOR SHARE", args)
        parent = cur.fetchone()
        if parent is None:
            refuse("CONTEXT_NOT_FOUND")
        verify_checkout(org_id, project_id, drawing_id, actor_binding_id=actor_binding_id,
                        expected_fence=expected_fence, conn=conn)
        cur.execute(
            "SELECT version_id FROM drawing_versions WHERE org_id = %(org_id)s "
            "AND project_id = %(project_id)s AND drawing_id = %(drawing_id)s "
            "AND deleted_at IS NULL ORDER BY seq DESC LIMIT 1 FOR SHARE", args)
        head = cur.fetchone()
        if head is None:
            refuse("CONTEXT_NOT_FOUND")
        if head["version_id"] != expected_parent_version_id:
            refuse("STALE_VERSION")
        cur.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM drawing_versions "
            "WHERE org_id = %(org_id)s AND project_id = %(project_id)s "
            "AND drawing_id = %(drawing_id)s", args)
        seq = cur.fetchone()["next_seq"]
        proof = _publication_proof(org_id, project_id, drawing_id, expected_parent_version_id,
            actor_binding_id, expected_fence, request_id, intake_sha256, parent["oss_object"])
        args.update(version_id=uuid4(), seq=seq, oss_object=parent["oss_object"],
                    intake_ref=proof["intake"]["ref"], created_by=str(actor_binding_id),
                    provenance=Jsonb(proof), fingerprint=_publication_fingerprint(proof))
        cur.execute(
            "INSERT INTO drawing_versions (version_id, drawing_id, project_id, org_id, seq, "
            "oss_object, intake_ref, created_by, provenance, idempotency_key, import_fingerprint) "
            "SELECT %(version_id)s, %(drawing_id)s, %(project_id)s, %(org_id)s, %(seq)s, "
            "%(oss_object)s, %(intake_ref)s, %(created_by)s, %(provenance)s, %(key)s, %(fingerprint)s "
            "FROM project_drawing_checkouts c WHERE c.org_id = %(org_id)s "
            "AND c.project_id = %(project_id)s AND c.drawing_id = %(drawing_id)s "
            "AND c.holder IS NOT NULL AND c.holder_binding_id = %(actor_binding_id)s "
            "AND c.fence = %(expected_fence)s AND c.expires_at > clock_timestamp() "
            "ON CONFLICT (org_id, project_id, idempotency_key) "
            "WHERE idempotency_key IS NOT NULL DO NOTHING RETURNING *", args)
        row = cur.fetchone()
        if row is not None:
            return DrawingVersion.from_row(row)
        prior = replay()
        if prior is not None:
            return prior
        verify_checkout(org_id, project_id, drawing_id, actor_binding_id=actor_binding_id,
                        expected_fence=expected_fence, conn=conn)
        refuse("INTERNAL")
