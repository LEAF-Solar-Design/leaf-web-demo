"""Live-authorized issuance for the native drawing first-binding protocol.

LEAF_BINDING_GRANT_SIGNER=kms, LEAF_BINDING_GRANT_KMS_KEY_ID,
LEAF_BINDING_GRANT_KID and LEAF_BINDING_GRANT_ISSUER must all be configured.
The decision audit commits before any signing call. It proves authorization,
not delivery or native acceptance. No grant or credential is logged here.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, field_validator

from . import db, deps
from .binding_grant import (
    AUDIENCE, GrantClaims, GrantError, KmsSigner, b64u,
    canonical_header, encode_grant, validate_claims,
)
from .counters import SharedCounterStore
from .project_lifecycle import WRITE_ROLES

router = APIRouter()
POLICY_VERSION = "leaf.binding-grant.v1"
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_COUNTERS = SharedCounterStore("binding_grant_counters")


class BindingGrantBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    pluginSessionId: str
    documentFingerprint: str

    @field_validator("pluginSessionId")
    @classmethod
    def session_id(cls, value):
        if not _UUID.fullmatch(value) or uuid.UUID(value).int == 0:
            raise ValueError("pluginSessionId must be a nonzero lowercase D-form UUID")
        return value

    @field_validator("documentFingerprint")
    @classmethod
    def fingerprint(cls, value):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
            raise ValueError("documentFingerprint must be sha256 plus lowercase hex")
        return value


@dataclass(frozen=True)
class Actor:
    org_id: uuid.UUID
    binding_id: uuid.UUID


@dataclass(frozen=True)
class SigningConfig:
    signer: object
    issuer: str


def _refusal(status, reason, **headers):
    return HTTPException(status_code=status, detail=reason,
                         headers={"Cache-Control": "no-store", **headers})


def live_actor(authorization: str | None = Header(default=None)) -> Actor:
    if not deps.auth_live():
        raise _refusal(503, "binding_grant_issuance_disabled")
    try:
        binding = deps._verified_identity(authorization)
    except HTTPException as exc:
        # An absent active subject binding is invalid issuance authentication.
        if exc.status_code in (401, 403):
            raise _refusal(401, "binding_grant_invalid_auth") from None
        raise _refusal(503, "binding_grant_auth_unavailable") from None
    except Exception:
        raise _refusal(503, "binding_grant_auth_unavailable") from None
    return Actor(binding.platform_tenant_id, binding.binding_id)


def configured_signer() -> SigningConfig:
    kind = os.environ.get("LEAF_BINDING_GRANT_SIGNER", "")
    key_id = os.environ.get("LEAF_BINDING_GRANT_KMS_KEY_ID", "")
    kid = os.environ.get("LEAF_BINDING_GRANT_KID", "")
    issuer = os.environ.get("LEAF_BINDING_GRANT_ISSUER", "")
    if kind != "kms" or not all((key_id, kid, issuer)):
        raise _refusal(503, "binding_grant_issuance_disabled")
    try:
        canonical_header(kid)
        import boto3
        return SigningConfig(KmsSigner(key_id, kid, boto3.client("kms")), issuer)
    except Exception:
        raise _refusal(503, "binding_grant_signing_unavailable") from None


def consume_limits(conn, actor, session_id, now, counters=_COUNTERS):
    """Atomic fixed-minute buckets; callers roll back a refused reservation."""
    bucket = int(now) // 60
    for namespace, key, limit in (
        ("actor", f"{actor.org_id}:{actor.binding_id}:{bucket}", 10),
        ("session", f"{actor.org_id}:{actor.binding_id}:{session_id}:{bucket}", 3),
        ("workspace", f"{actor.org_id}:{bucket}", 100),
    ):
        result = counters.consume_in_transaction(
            conn, namespace=namespace, key=key, limit=limit,
        )
        if not result.accepted:
            raise _refusal(429, "binding_grant_rate_limited",
                           **{"Retry-After": str(60 - int(now) % 60)})


# Every identifying label and the complete tuple come from this single scoped
# snapshot. Row locks keep membership and material live through audit commit.
_TARGET_SQL = """
SELECT o.name AS workspace_name, p.name AS project_name,
       d.drawing_id, d.name AS drawing_name, v.seq, m.role
FROM orgs o
JOIN projects p ON p.org_id = o.org_id
JOIN drawing_artifacts d ON d.org_id = p.org_id AND d.project_id = p.project_id
JOIN drawing_versions v ON v.org_id = d.org_id AND v.project_id = d.project_id
                       AND v.drawing_id = d.drawing_id
JOIN identity_bindings b ON b.platform_tenant_id = o.org_id
JOIN project_member_bindings m ON m.org_id = p.org_id AND m.project_id = p.project_id
                              AND m.binding_id = b.binding_id
WHERE o.org_id = %(org_id)s AND p.project_id = %(project_id)s
  AND v.version_id = %(version_id)s AND b.binding_id = %(binding_id)s
  AND o.status = 'active' AND o.deleted_at IS NULL
  AND o.purge_requested_at IS NULL AND o.purge_completed_at IS NULL
  AND p.status = 'active' AND p.deleted_at IS NULL
  AND p.purge_requested_at IS NULL AND p.purge_completed_at IS NULL
  AND d.status = 'active' AND v.deleted_at IS NULL
  AND v.purge_requested_at IS NULL AND v.purge_completed_at IS NULL
  AND NULLIF(BTRIM(v.oss_object), '') IS NOT NULL
  AND b.status = 'active' AND m.status = 'active'
  AND COALESCE((SELECT authority_mode FROM live_project_authority_modes
                WHERE org_id = p.org_id AND project_id = p.project_id),
               (SELECT authority_mode FROM tenant_authority_modes WHERE org_id = p.org_id),
               'legacy_sqlite') = 'postgres_canonical'
  AND NOT EXISTS (SELECT 1 FROM drawing_store_versions s
                  WHERE s.tenant_id = o.org_id::text AND s.object_key = v.oss_object
                    AND s.state <> 'ready')
  AND (COALESCE(v.provenance->'source'->>'kind', '') <> 'account_upload' OR EXISTS (
      SELECT 1 FROM drawing_store_versions s JOIN drawing_upload_attempts u
        ON u.tenant_id = s.tenant_id AND u.drawing_id = s.drawing_id
      WHERE s.tenant_id = o.org_id::text AND s.object_key = v.oss_object
        AND s.drawing_id = v.provenance->'source'->>'drawing_id'
        AND s.version::text = v.provenance->'source'->>'version'
        AND s.state = 'ready' AND u.status = 'ready'
        AND u.attempt = v.provenance->'source'->>'upload_attempt'
        AND (u.retention_expires_at IS NULL OR u.retention_expires_at > NOW())))
FOR SHARE OF o, p, d, v, b, m
"""


def _audit(conn, values):
    conn.execute("""
        INSERT INTO binding_grant_audit
        (request_id, actor_binding_id, org_id, project_id, drawing_id, version_id,
         plugin_session_id, fingerprint_sha256, nonce_sha256, key_id, iat, exp,
         policy_version, decision, refusal_reason)
        VALUES (%(request_id)s, %(binding_id)s, %(org_id)s, %(project_id)s,
                %(drawing_id)s, %(version_id)s, %(session_id)s, %(fingerprint_sha256)s,
                %(nonce_sha256)s, %(key_id)s, %(iat)s, %(exp)s,
                %(policy_version)s, %(decision)s, %(reason)s)
    """, values)


def issue_grant(actor, project_id, version_id, body, config, *, clock=time.time):
    request_id = uuid.uuid4()
    nonce = b64u(secrets.token_bytes(32))

    def decide(conn):
        values = {
            "request_id": request_id, "binding_id": actor.binding_id,
            "org_id": actor.org_id, "project_id": project_id, "version_id": version_id,
            "drawing_id": None, "session_id": uuid.UUID(body.pluginSessionId),
            "fingerprint_sha256": hashlib.sha256(body.documentFingerprint.encode()).hexdigest(),
            "nonce_sha256": hashlib.sha256(nonce.encode()).hexdigest(),
            "key_id": config.signer.kid, "policy_version": POLICY_VERSION,
            "decision": "refused", "reason": None,
        }
        failure = None
        claims = None
        binding = conn.execute(
            "SELECT binding_id FROM identity_bindings WHERE platform_tenant_id = %s "
            "AND binding_id = %s AND status = 'active' FOR SHARE",
            (actor.org_id, actor.binding_id),
        ).fetchone()
        row = conn.execute(_TARGET_SQL, values).fetchone() if binding else None
        if row is not None:
            values["drawing_id"] = row["drawing_id"]
        now = int(clock())
        values.update(iat=now, exp=now + 120)
        if not binding:
            failure = _refusal(401, "binding_grant_invalid_auth")
        elif row is None:
            failure = _refusal(404, "binding_grant_target_unavailable")
        elif row["role"] not in WRITE_ROLES:
            failure = _refusal(403, "binding_grant_role_insufficient")
        else:
            claims = GrantClaims(
                schemaVersion=1, iss=config.issuer, aud=AUDIENCE,
                platformTenantId=str(actor.org_id), workspaceName=row["workspace_name"],
                projectId=str(project_id), projectName=row["project_name"],
                drawingId=str(row["drawing_id"]), drawingName=row["drawing_name"],
                drawingVersionId=str(version_id), versionName=f"Version {row['seq']}",
                actorBindingId=str(actor.binding_id), pluginSessionId=body.pluginSessionId,
                documentFingerprint=body.documentFingerprint, iat=now, exp=now + 120,
                nonce=nonce,
            )
            try:
                validate_claims(claims)
            except GrantError as exc:
                failure = _refusal(422, "binding_grant_rename_required") if exc.code == "name" else (
                    _refusal(503, "binding_grant_configuration_invalid"))
            if failure is None:
                # A nested transaction is a savepoint. A refused bucket must not
                # consume another bucket, but its audit still commits.
                try:
                    with conn.transaction():
                        consume_limits(conn, actor, body.pluginSessionId, now)
                except HTTPException as exc:
                    failure = exc
        values["decision"] = "refused" if failure else "authorized"
        values["reason"] = failure.detail if failure else None
        _audit(conn, values)
        return claims, failure

    try:
        claims, failure = db.run_transaction(
            decide, isolation="repeatable read", max_attempts=20,
        )
    except Exception:
        raise _refusal(503, "binding_grant_audit_unavailable") from None
    if failure:
        raise failure
    try:
        grant = encode_grant(claims, config.signer)
        if clock() >= claims.exp:
            raise _refusal(503, "binding_grant_expired_during_signing")
    except HTTPException:
        raise
    except Exception:
        raise _refusal(503, "binding_grant_signing_unavailable") from None
    return JSONResponse(
        {"grant": grant, "expiresAt": datetime.fromtimestamp(
            claims.exp, timezone.utc).isoformat().replace("+00:00", "Z")},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/projects/{project_id}/drawing-versions/{version_id}/binding-grants")
def binding_grants(
    project_id: uuid.UUID, version_id: uuid.UUID, body: BindingGrantBody,
    actor: Actor = Depends(live_actor),
    config: SigningConfig = Depends(configured_signer),
):
    return issue_grant(actor, project_id, version_id, body, config)
