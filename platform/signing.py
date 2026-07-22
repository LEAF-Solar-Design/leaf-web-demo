"""Provider-backed professional review signatures over frozen evidence roots."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from psycopg.types.json import Jsonb

from . import evidence
from .db import connection
from .models import canonical_hash, new_uuid
from .store import _insert_outbox

SIGNATURE_CONTRACT = "leaf.review-signature.v1"
_HASH = re.compile(r"^[0-9a-f]{64}$")


class SignatureProvider(Protocol):
    algorithm: str

    def sign(self, provider_key_ref: str, payload: bytes) -> bytes: ...


class LocalEd25519Provider:
    """In-memory provider for development/tests; private keys are never persisted."""
    algorithm = "ed25519"

    def __init__(self, keys: Dict[str, ed25519.Ed25519PrivateKey]):
        self._keys = dict(keys)

    def sign(self, provider_key_ref: str, payload: bytes) -> bytes:
        key = self._keys.get(provider_key_ref)
        if key is None:
            raise RuntimeError("signing provider key is unavailable")
        return key.sign(payload)


class AwsKmsEcdsaProvider:
    """AWS KMS adapter. Construction/signing is lazy and performs no provisioning."""
    algorithm = "ecdsa-p256-sha256"

    def __init__(self, kms_client: Any = None):
        if kms_client is None:
            try:
                import boto3  # type: ignore
            except ImportError as exc:  # pragma: no cover - optional production adapter
                raise RuntimeError("boto3 is required for the AWS KMS signing provider") from exc
            kms_client = boto3.client("kms")
        self._kms = kms_client

    def sign(self, provider_key_ref: str, payload: bytes) -> bytes:
        response = self._kms.sign(
            KeyId=provider_key_ref, Message=payload, MessageType="RAW",
            SigningAlgorithm="ECDSA_SHA_256")
        return bytes(response["Signature"])


_provider: Optional[SignatureProvider] = None


def configure_signature_provider(provider: Optional[SignatureProvider]) -> None:
    global _provider
    _provider = provider


def _now(value: Optional[datetime] = None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None:
        raise ValueError("signature timestamps must be timezone-aware")
    return result


def register_credential(org_id: uuid.UUID, binding_id: uuid.UUID, *, jurisdiction: str,
                        license_ref: str, algorithm: str, public_key: bytes,
                        provider_key_ref: str, verified_by: str, verified_at: datetime,
                        expires_at: datetime) -> Dict[str, Any]:
    """Operator-only integration seam; intentionally not exposed as a customer API."""
    verified_at, expires_at = _now(verified_at), _now(expires_at)
    if algorithm not in {"ed25519", "ecdsa-p256-sha256"}:
        raise ValueError("unsupported professional signature algorithm")
    if expires_at <= verified_at:
        raise ValueError("credential expiry must follow verification")
    _load_public_key(algorithm, public_key)
    if not all(isinstance(value, str) and value.strip()
               for value in (jurisdiction, license_ref, provider_key_ref, verified_by)):
        raise ValueError("credential identity and verification fields are required")
    credential_id = new_uuid()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT role FROM identity_bindings WHERE platform_tenant_id = %(org)s "
                        "AND binding_id = %(binding)s AND status = 'active'",
                        {"org": org_id, "binding": binding_id})
            binding = cur.fetchone()
            if binding is None or binding["role"] not in {"owner", "reviewer"}:
                raise ValueError("professional credential requires an active owner or reviewer binding")
            cur.execute(
                "INSERT INTO professional_credentials (credential_id, org_id, binding_id, profession, "
                "jurisdiction, license_ref, signature_algorithm, public_key, provider_key_ref, "
                "verified_by, verified_at, expires_at) VALUES "
                "(%(credential)s, %(org)s, %(binding)s, 'professional_engineer', %(jurisdiction)s, "
                "%(license)s, %(algorithm)s, %(public_key)s, %(key_ref)s, %(verified_by)s, "
                "%(verified_at)s, %(expires_at)s)",
                {"credential": credential_id, "org": org_id, "binding": binding_id,
                 "jurisdiction": jurisdiction.strip(), "license": license_ref.strip(),
                 "algorithm": algorithm, "public_key": public_key,
                 "key_ref": provider_key_ref.strip(), "verified_by": verified_by.strip(),
                 "verified_at": verified_at, "expires_at": expires_at})
            cur.execute("INSERT INTO professional_credential_events "
                        "(event_id, org_id, credential_id, sequence, state, actor, reason) VALUES "
                        "(%(event)s, %(org)s, %(credential)s, 1, 'active', %(actor)s, %(reason)s)",
                        {"event": new_uuid(), "org": org_id, "credential": credential_id,
                         "actor": verified_by.strip(), "reason": "credential verified"})
    return {"credential_id": str(credential_id), "state": "active"}


def revoke_credential(org_id: uuid.UUID, credential_id: uuid.UUID, *, actor: str,
                      reason: str) -> Dict[str, Any]:
    if not actor.strip() or not reason.strip():
        raise ValueError("credential revocation actor and reason are required")
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT state, sequence FROM professional_credential_events "
                        "WHERE org_id = %(org)s AND credential_id = %(credential)s "
                        "ORDER BY sequence DESC LIMIT 1 FOR UPDATE",
                        {"org": org_id, "credential": credential_id})
            current = cur.fetchone()
            if current is None:
                raise ValueError("professional credential is unavailable")
            if current["state"] == "revoked":
                return {"credential_id": str(credential_id), "state": "revoked",
                        "sequence": current["sequence"]}
            sequence = current["sequence"] + 1
            cur.execute("INSERT INTO professional_credential_events "
                        "(event_id, org_id, credential_id, sequence, state, actor, reason) VALUES "
                        "(%(event)s, %(org)s, %(credential)s, %(sequence)s, 'revoked', %(actor)s, %(reason)s)",
                        {"event": new_uuid(), "org": org_id, "credential": credential_id,
                         "sequence": sequence, "actor": actor.strip(), "reason": reason.strip()})
            return {"credential_id": str(credential_id), "state": "revoked", "sequence": sequence}


def review_context(org_id: uuid.UUID, binding_id: uuid.UUID,
                   *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Return only public, server-derived signing readiness for the current reviewer."""
    observed = _now(now)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.credential_id, c.profession, c.jurisdiction, c.license_ref, "
                "c.signature_algorithm, c.verified_at, c.expires_at, e.state "
                "FROM professional_credentials c JOIN LATERAL "
                "(SELECT state FROM professional_credential_events WHERE org_id = c.org_id "
                "AND credential_id = c.credential_id ORDER BY sequence DESC LIMIT 1) e ON TRUE "
                "WHERE c.org_id = %(org)s AND c.binding_id = %(binding)s "
                "ORDER BY (e.state = 'active' AND c.expires_at > %(observed)s) DESC, "
                "c.verified_at DESC LIMIT 1",
                {"org": org_id, "binding": binding_id, "observed": observed})
            row = cur.fetchone()
    if row is None:
        return {"signing_available": False, "reason": "active_credential_required",
                "credential": None}
    credential = {
        "credential_id": str(row["credential_id"]),
        "profession": row["profession"],
        "jurisdiction": row["jurisdiction"],
        "license_ref": row["license_ref"],
        "signature_algorithm": row["signature_algorithm"],
        "verified_at": row["verified_at"],
        "expires_at": row["expires_at"],
        "state": row["state"],
    }
    if row["state"] != "active":
        return {"signing_available": False, "reason": "credential_revoked",
                "credential": credential}
    if row["expires_at"] <= observed:
        return {"signing_available": False, "reason": "credential_expired",
                "credential": credential}
    provider = _provider
    if provider is None:
        return {"signing_available": False, "reason": "signature_provider_unavailable",
                "credential": credential}
    if provider.algorithm != row["signature_algorithm"]:
        return {"signing_available": False, "reason": "signature_provider_mismatch",
                "credential": credential}
    return {"signing_available": True, "reason": None, "credential": credential}


def _load_bundle_blobs(cur: Any, org_id: uuid.UUID, project_id: uuid.UUID,
                       bundle_id: uuid.UUID) -> tuple[Dict[str, Any], Dict[str, bytes]]:
    cur.execute("SELECT root_sha256, manifest, created_at FROM evidence_bundles WHERE org_id = %(org)s "
                "AND project_id = %(project)s AND bundle_id = %(bundle)s",
                {"org": org_id, "project": project_id, "bundle": bundle_id})
    bundle = cur.fetchone()
    if bundle is None:
        raise ValueError("evidence bundle is unavailable")
    cur.execute("SELECT path, content FROM evidence_entries WHERE org_id = %(org)s "
                "AND project_id = %(project)s AND bundle_id = %(bundle)s ORDER BY path",
                {"org": org_id, "project": project_id, "bundle": bundle_id})
    blobs = {row["path"]: bytes(row["content"]) for row in cur.fetchall()}
    verification = evidence.verify(bundle["manifest"], blobs)
    if not verification["valid"] or verification["rootSha256"] != bundle["root_sha256"]:
        raise ValueError("evidence bundle failed offline verification")
    cur.execute(
        "SELECT EXISTS (SELECT 1 FROM history_operations WHERE org_id = %(org)s "
        "AND project_id = %(project)s AND created_at > %(frozen_at)s "
        "AND operation_type NOT IN ('review.bundle.countersigned', 'evidence.root.delivered')) "
        "AS superseded",
        {"org": org_id, "project": project_id, "frozen_at": bundle["created_at"]})
    if cur.fetchone()["superseded"]:
        raise ValueError("evidence bundle was superseded by a later design operation")
    return dict(bundle), blobs


def _require_resolved_failures(blobs: Dict[str, bytes]) -> None:
    findings = json.loads(blobs["records/compliance-findings.json"])
    events = json.loads(blobs["records/waiver-events.json"])
    latest: Dict[str, Dict[str, Any]] = {}
    waiver_finding: Dict[str, str] = {}
    for event in events:
        waiver_id = str(event["waiver_id"])
        waiver_finding[waiver_id] = str(event["finding_id"])
        if waiver_id not in latest or int(event["sequence"]) > int(latest[waiver_id]["sequence"]):
            latest[waiver_id] = event
    approved = {waiver_finding[waiver_id] for waiver_id, event in latest.items()
                if event["state"] == "approved"}
    unresolved = [str(item["finding_id"]) for item in findings
                  if item["payload"].get("result") == "fail"
                  and str(item["finding_id"]) not in approved]
    if unresolved:
        raise ValueError(f"unresolved failing compliance findings block countersign: {unresolved}")


def countersign(org_id: uuid.UUID, project_id: uuid.UUID, bundle_id: uuid.UUID,
                credential_id: uuid.UUID, actor_binding_id: uuid.UUID,
                idempotency_key: str, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    provider = _provider
    if provider is None:
        raise RuntimeError("professional signature provider is not configured")
    if not idempotency_key:
        raise ValueError("Idempotency-Key is required")
    signed_at = _now(now)
    with connection() as conn:
        with conn.cursor() as cur:
            # Serialize countersign attempts at the project boundary. Credential-row
            # locking alone cannot prevent two different credentials from racing on
            # the same idempotency key and leaving an orphan history operation.
            cur.execute(
                "SELECT 1 FROM projects WHERE org_id = %(org)s AND project_id = %(project)s "
                "FOR UPDATE",
                {"org": org_id, "project": project_id})
            if cur.fetchone() is None:
                raise ValueError("project not found")
            cur.execute(
                "SELECT signature_id, bundle_id, credential_id, history_operation_id, root_sha256, signed_payload, "
                "signature_algorithm, signature_bytes FROM review_signatures "
                "WHERE org_id = %(org)s AND project_id = %(project)s AND "
                "(idempotency_key = %(key)s OR "
                "(bundle_id = %(bundle)s AND credential_id = %(credential)s)) "
                "ORDER BY (idempotency_key = %(key)s) DESC LIMIT 1",
                {"org": org_id, "project": project_id, "key": idempotency_key,
                 "bundle": bundle_id, "credential": credential_id})
            existing = cur.fetchone()
            if existing is not None:
                if existing["bundle_id"] != bundle_id or existing["credential_id"] != credential_id:
                    raise ValueError("signature idempotency key exists with different countersign input")
                return _signature_record(existing)
            bundle, blobs = _load_bundle_blobs(cur, org_id, project_id, bundle_id)
            _require_resolved_failures(blobs)
            cur.execute(
                "SELECT c.*, e.state FROM professional_credentials c JOIN LATERAL "
                "(SELECT state FROM professional_credential_events WHERE org_id = c.org_id "
                "AND credential_id = c.credential_id ORDER BY sequence DESC LIMIT 1) e ON TRUE "
                "JOIN identity_bindings b ON b.platform_tenant_id = c.org_id AND b.binding_id = c.binding_id "
                "WHERE c.org_id = %(org)s AND c.credential_id = %(credential)s "
                "AND c.binding_id = %(actor)s AND b.status = 'active' FOR UPDATE OF c",
                {"org": org_id, "credential": credential_id, "actor": actor_binding_id})
            credential = cur.fetchone()
            if credential is None or credential["state"] != "active" or credential["expires_at"] <= signed_at:
                raise ValueError("active unexpired professional credential is required")
            if credential["signature_algorithm"] != provider.algorithm:
                raise ValueError("configured signature provider does not match credential algorithm")
            # The credential row is the per-signer serialization point. A competing
            # request may have committed while this request waited for the lock.
            cur.execute(
                "SELECT signature_id, bundle_id, credential_id, history_operation_id, "
                "root_sha256, signed_payload, signature_algorithm, signature_bytes "
                "FROM review_signatures WHERE org_id = %(org)s AND project_id = %(project)s "
                "AND (idempotency_key = %(key)s OR "
                "(bundle_id = %(bundle)s AND credential_id = %(credential)s)) "
                "ORDER BY (idempotency_key = %(key)s) DESC LIMIT 1",
                {"org": org_id, "project": project_id, "key": idempotency_key,
                 "bundle": bundle_id, "credential": credential_id})
            existing = cur.fetchone()
            if existing is not None:
                if existing["bundle_id"] != bundle_id or existing["credential_id"] != credential_id:
                    raise ValueError("signature idempotency key exists with different countersign input")
                return _signature_record(existing)
            payload = {
                "signatureContract": SIGNATURE_CONTRACT,
                "bundleId": str(bundle_id),
                "rootSha256": bundle["root_sha256"],
                "credentialId": str(credential_id),
                "signedAt": signed_at.isoformat(),
            }
            encoded = evidence.canonical_bytes(payload)
            signature = provider.sign(credential["provider_key_ref"], encoded)
            _verify_bytes(credential["signature_algorithm"], bytes(credential["public_key"]),
                          encoded, signature)
            signature_id = new_uuid()
            operation_id = new_uuid()
            history_payload = {
                "signatureId": str(signature_id),
                "bundleId": str(bundle_id),
                "rootSha256": bundle["root_sha256"],
                "credentialId": str(credential_id),
                "signatureContract": SIGNATURE_CONTRACT,
                "signatureAlgorithm": provider.algorithm,
                "signatureSha256": hashlib.sha256(signature).hexdigest(),
            }
            history_digest = canonical_hash(
                "history-operation", {"operationType": "review.bundle.countersigned",
                                      "payload": history_payload})
            cur.execute(
                "INSERT INTO history_operations (operation_id, org_id, project_id, operation_type, "
                "payload, idempotency_key, hash_algorithm, hash_canonicalization, hash_domain, hash_value) "
                "VALUES (%(operation)s, %(org)s, %(project)s, 'review.bundle.countersigned', "
                "%(payload)s, %(key)s, %(algorithm)s, %(canonicalization)s, %(domain)s, %(value)s)",
                {"operation": operation_id, "org": org_id, "project": project_id,
                 "payload": Jsonb(history_payload), "key": f"signature:{signature_id}",
                 **history_digest.to_dict()})
            cur.execute(
                "INSERT INTO review_signatures (signature_id, org_id, project_id, bundle_id, "
                "credential_id, actor_binding_id, history_operation_id, idempotency_key, "
                "root_sha256, signature_contract, "
                "signature_algorithm, signed_payload, signature_bytes, signed_at) VALUES "
                "(%(signature)s, %(org)s, %(project)s, %(bundle)s, %(credential)s, %(actor)s, "
                "%(operation)s, %(key)s, %(root)s, %(contract)s, %(algorithm)s, %(payload)s, "
                "%(bytes)s, %(signed_at)s) "
                "ON CONFLICT DO NOTHING RETURNING signature_id",
                {"signature": signature_id, "org": org_id, "project": project_id,
                 "bundle": bundle_id, "credential": credential_id, "actor": actor_binding_id,
                 "operation": operation_id,
                 "key": idempotency_key, "root": bundle["root_sha256"],
                 "contract": SIGNATURE_CONTRACT, "algorithm": provider.algorithm,
                 "payload": Jsonb(payload), "bytes": signature, "signed_at": signed_at})
            created = cur.fetchone()
            if created is None:
                cur.execute("SELECT signature_id, bundle_id, credential_id, history_operation_id, "
                            "root_sha256, signed_payload, "
                            "signature_algorithm, signature_bytes FROM review_signatures "
                            "WHERE org_id = %(org)s AND project_id = %(project)s AND "
                            "(idempotency_key = %(key)s OR "
                            "(bundle_id = %(bundle)s AND credential_id = %(credential)s)) "
                            "ORDER BY (idempotency_key = %(key)s) DESC LIMIT 1",
                            {"org": org_id, "project": project_id, "key": idempotency_key,
                             "bundle": bundle_id, "credential": credential_id})
                existing = cur.fetchone()
                if existing is None or existing["bundle_id"] != bundle_id or \
                        existing["credential_id"] != credential_id:
                    raise ValueError("signature idempotency key exists with different countersign input")
                return _signature_record(existing)
            _insert_outbox(cur, org_id, project_id, "review_signature", signature_id,
                           "review.bundle.countersigned",
                           {"signatureId": str(signature_id), "bundleId": str(bundle_id),
                            "rootSha256": bundle["root_sha256"]})
            _insert_outbox(cur, org_id, project_id, "history_operation", operation_id,
                           "history.operation.appended",
                           {"operationId": str(operation_id), "signatureId": str(signature_id)})
            return {"signature_id": str(signature_id), "history_operation_id": str(operation_id),
                    "bundle_id": str(bundle_id),
                    "credential_id": str(credential_id), "root_sha256": bundle["root_sha256"],
                    "signature_algorithm": provider.algorithm,
                    "signature_base64": base64.b64encode(signature).decode("ascii"),
                    "signed_payload": payload}


def _signature_record(row: Any) -> Dict[str, Any]:
    return {"signature_id": str(row["signature_id"]), "bundle_id": str(row["bundle_id"]),
            "credential_id": str(row["credential_id"]), "root_sha256": row["root_sha256"],
            "history_operation_id": str(row["history_operation_id"]),
            "signature_algorithm": row["signature_algorithm"],
            "signature_base64": base64.b64encode(bytes(row["signature_bytes"])).decode("ascii"),
            "signed_payload": row["signed_payload"]}


def _load_public_key(algorithm: str, public_key: bytes) -> Any:
    if algorithm == "ed25519":
        if len(public_key) != 32:
            raise ValueError("Ed25519 public key must be 32 raw bytes")
        return ed25519.Ed25519PublicKey.from_public_bytes(public_key)
    key = serialization.load_der_public_key(public_key)
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("ECDSA credential requires a DER-encoded P-256 public key")
    return key


def _verify_bytes(algorithm: str, public_key: bytes, payload: bytes, signature: bytes) -> None:
    key = _load_public_key(algorithm, public_key)
    try:
        if algorithm == "ed25519":
            key.verify(signature, payload)
        else:
            key.verify(signature, payload, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise ValueError("signature provider returned an invalid signature") from exc


def verify_signature(org_id: uuid.UUID, project_id: uuid.UUID,
                     signature_id: uuid.UUID, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    observed = _now(now)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT s.*, c.public_key, c.expires_at, e.state AS credential_state, "
                "b.root_sha256 AS current_bundle_root, b.created_at AS bundle_created_at, "
                "EXISTS (SELECT 1 FROM history_operations h WHERE h.org_id = s.org_id "
                "AND h.project_id = s.project_id AND h.created_at > b.created_at "
                "AND h.operation_type NOT IN ('review.bundle.countersigned', 'evidence.root.delivered')) "
                "AS bundle_superseded FROM review_signatures s "
                "JOIN professional_credentials c ON c.org_id = s.org_id AND c.credential_id = s.credential_id "
                "JOIN LATERAL (SELECT state FROM professional_credential_events WHERE org_id = c.org_id "
                "AND credential_id = c.credential_id ORDER BY sequence DESC LIMIT 1) e ON TRUE "
                "JOIN evidence_bundles b ON b.org_id = s.org_id AND b.project_id = s.project_id "
                "AND b.bundle_id = s.bundle_id WHERE s.org_id = %(org)s AND s.project_id = %(project)s "
                "AND s.signature_id = %(signature)s",
                {"org": org_id, "project": project_id, "signature": signature_id})
            row = cur.fetchone()
    if row is None:
        return {"valid": False, "cryptographic_valid": False,
                "authorization_valid": False, "errors": ["signature_not_found"]}
    errors = []
    crypto_valid = True
    try:
        _verify_bytes(row["signature_algorithm"], bytes(row["public_key"]),
                      evidence.canonical_bytes(row["signed_payload"]), bytes(row["signature_bytes"]))
    except ValueError:
        crypto_valid = False
        errors.append("invalid_signature")
    if row["root_sha256"] != row["current_bundle_root"] or \
            row["signed_payload"].get("rootSha256") != row["root_sha256"]:
        crypto_valid = False
        errors.append("root_mismatch")
    expected_payload_fields = {
        "signatureContract": row["signature_contract"],
        "bundleId": str(row["bundle_id"]),
        "rootSha256": row["root_sha256"],
        "credentialId": str(row["credential_id"]),
        "signedAt": row["signed_at"].isoformat(),
    }
    if row["signature_contract"] != SIGNATURE_CONTRACT or \
            row["signed_payload"] != expected_payload_fields or \
            not _HASH.fullmatch(row["root_sha256"]):
        crypto_valid = False
        errors.append("payload_mismatch")
    authorization_valid = row["credential_state"] == "active" and row["expires_at"] > observed
    if row["bundle_superseded"]:
        authorization_valid = False
        errors.append("bundle_superseded")
    if row["credential_state"] != "active":
        errors.append("credential_revoked")
    elif row["expires_at"] <= observed:
        errors.append("credential_expired")
    return {"valid": crypto_valid and authorization_valid,
            "cryptographic_valid": crypto_valid,
            "authorization_valid": authorization_valid,
            "errors": errors, "signature": _signature_record(row)}
