"""Verified canonical project drawing content and checkout capability exchange."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from uuid import UUID

import checkout_capability
import platform_link
import write_loop

platform_link._ensure_platform_package()
from leaf_platform.project_graph_store import (
    CheckoutLease, CheckoutState, ProjectContextError, VersionBinding, refuse,
)


@dataclass(frozen=True)
class VerifiedProjectContext:
    binding: VersionBinding
    intake_bytes: bytes
    intake: dict
    intake_sha256: str
    intake_ref: str


@dataclass(frozen=True)
class AdmissionContext:
    context: VerifiedProjectContext
    checkout: CheckoutLease


def graph_store():
    from leaf_platform import project_graph_store
    return project_graph_store


def _access(tenant, project_id, *, write):
    if not isinstance(project_id, UUID):
        refuse("INVALID_BINDING")
    try:
        binding = platform_link.resolve_caller_binding(tenant)
        org_id = UUID(str(tenant))
        if binding is None or binding.platform_tenant_id != org_id:
            refuse("CONTEXT_NOT_FOUND")
        platform_link.require_project_access(tenant, project_id, write=write, binding=binding)
        org = platform_link.platform_store().get_org(org_id)
        if org is None or org.status != "active":
            refuse("CONTEXT_NOT_FOUND")
        platform_link.resolve_project_authority(org_id, project_id)
        return org_id, binding.binding_id
    except ProjectContextError:
        raise
    except platform_link.ProjectSessionForbidden:
        refuse("PROJECT_FORBIDDEN")
    except platform_link.ProjectAuthorityRequired:
        refuse("CANONICAL_AUTHORITY_REQUIRED")
    except (platform_link.ProjectAuthorityNotFound, LookupError):
        refuse("CONTEXT_NOT_FOUND")
    except (ValueError, TypeError, AttributeError):
        refuse("INVALID_BINDING")
    except Exception:
        refuse("STORE_UNAVAILABLE")


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _nonfinite(value):
    raise ValueError("non-finite JSON number")


def _json_float(value):
    import math
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite JSON number")
    return parsed


def _read_intake(binding):
    version = binding.version
    provenance = version.provenance
    if isinstance(provenance, dict) and provenance.get("schema") == "leaf.project-drawing-publication.v1":
        key, digest = _publication_reference(binding)
        raw, intake = _load_intake(binding.organization_id, key, digest)
        return VerifiedProjectContext(binding, raw, intake, digest, key)
    if (not version.intake_ref or not isinstance(provenance, dict)
            or provenance.get("schema") != "leaf.drawing-import.v1"):
        refuse("INTAKE_PROOF_REQUIRED")
    source = provenance.get("source")
    proof = source.get("intake") if isinstance(source, dict) else None
    if (not isinstance(proof, dict) or not proof.get("ref")
            or not isinstance(proof.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", proof["sha256"]) is None):
        refuse("INTAKE_PROOF_REQUIRED")
    source_drawing = source.get("drawing_id")
    source_version = source.get("version")
    object_proof = source.get("stored_object")
    if (source.get("kind") != "account_upload"
            or source.get("tenant_id") != str(binding.organization_id)
            or not platform_link.platform_store().is_account_upload_source_id(source_drawing)
            or type(source_version) is not int or source_version < 1
            or not isinstance(object_proof, dict)):
        refuse("INTAKE_REFERENCE_INVALID")
    key = (f"tenants/{binding.organization_id}/drawings/{source_drawing}/"
           f"v/{source_version:08d}.intake.json")
    object_key = key[:-12] + ".dwg"
    if (version.intake_ref != key or proof["ref"] != key
            or version.oss_object != object_key or object_proof.get("ref") != object_key):
        refuse("INTAKE_REFERENCE_INVALID")
    raw, intake = _load_intake(binding.organization_id, key, proof["sha256"])
    return VerifiedProjectContext(binding, raw, intake, proof["sha256"], key)


def _publication_reference(binding):
    version = binding.version
    provenance = version.provenance
    proof = provenance.get("intake")
    if (not version.intake_ref or not isinstance(proof, dict)
            or not proof.get("ref") or not isinstance(proof.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", proof["sha256"]) is None):
        refuse("INTAKE_PROOF_REQUIRED")
    for field in ("organization_id", "project_id", "drawing_id", "parent_version_id",
                  "request_id", "actor_binding_id"):
        value = provenance.get(field)
        try:
            if not isinstance(value, str) or str(UUID(value)) != value:
                raise ValueError("noncanonical UUID")
        except (ValueError, TypeError, AttributeError):
            refuse("INTAKE_REFERENCE_INVALID")
    fence = provenance.get("checkout_fence")
    if (not isinstance(fence, str) or re.fullmatch(r"[1-9][0-9]{0,18}", fence) is None
            or int(fence) > 9223372036854775807):
        refuse("INTAKE_REFERENCE_INVALID")
    key = graph_store().publication_intake_key(
        binding.organization_id, binding.project_id, binding.drawing_id, proof["sha256"])
    if (provenance["organization_id"] != str(binding.organization_id)
            or provenance["project_id"] != str(binding.project_id)
            or provenance["drawing_id"] != str(binding.drawing_id)
            or version.org_id != binding.organization_id
            or version.project_id != binding.project_id or version.drawing_id != binding.drawing_id
            or version.idempotency_key != "sip-r2:" + provenance["request_id"]
            or "base_object_ref" not in provenance
            or provenance["base_object_ref"] != version.oss_object
            or version.intake_ref != key or proof["ref"] != key):
        refuse("INTAKE_REFERENCE_INVALID")
    return key, proof["sha256"]


def _load_intake(org_id, key, expected_digest):
    try:
        raw = write_loop.upload_backend_for_tenant(str(org_id)).get(key)
        if not isinstance(raw, bytes):
            refuse("INTAKE_UNAVAILABLE")
    except ProjectContextError:
        raise
    except Exception:
        refuse("INTAKE_UNAVAILABLE")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_digest:
        refuse("INTAKE_DIGEST_MISMATCH")
    try:
        intake = json.loads(raw.decode("utf-8"), object_pairs_hook=_object_pairs,
                            parse_constant=_nonfinite, parse_float=_json_float)
        if not isinstance(intake, dict):
            raise ValueError("intake must be an object")
    except (UnicodeError, ValueError, RecursionError):
        refuse("INTAKE_INVALID")
    return raw, intake


def publish_project_version(tenant, project_id, drawing_id, *, expected_parent_version_id,
                            expected_fence, request_id, intake_sha256):
    """Publish already-durable intake using the authenticated binding and admission fence."""
    org_id, actor = _access(tenant, project_id, write=True)
    graph_store().validate_publication_params(org_id, project_id, drawing_id,
        expected_parent_version_id, actor, expected_fence, request_id, intake_sha256)
    key = graph_store().publication_intake_key(org_id, project_id, drawing_id, intake_sha256)

    def operation(conn):
        return graph_store().publish_version(org_id, project_id, drawing_id,
            expected_parent_version_id=expected_parent_version_id, actor_binding_id=actor,
            expected_fence=expected_fence, request_id=request_id, intake_sha256=intake_sha256,
            conn=conn, before_new=lambda: _load_intake(org_id, key, intake_sha256))

    return _mutation(operation)


def resolve_context(tenant, project_id, input_version_id, *, drawing_id=None, write=False):
    org_id, _actor = _access(tenant, project_id, write=write)
    if not isinstance(input_version_id, UUID) or (drawing_id is not None and not isinstance(drawing_id, UUID)):
        refuse("INVALID_BINDING")
    try:
        binding = graph_store().resolve_version_binding(
            org_id, project_id, input_version_id, drawing_id=drawing_id)
    except ProjectContextError:
        raise
    except Exception:
        refuse("STORE_UNAVAILABLE")
    return _read_intake(binding)


def checkout_scope(project_id, drawing_id):
    return f"project:{project_id}:drawing:{drawing_id}"


def public_checkout(lease):
    if lease is None:
        return None
    return {"holder": lease.holder,
            "acquired": lease.acquired_at.isoformat().replace("+00:00", "Z"),
            "expires": lease.expires_at.isoformat().replace("+00:00", "Z"),
            "fence": str(lease.fence)}


_CAPABILITY_GRAMMAR = re.compile(r"lco1\.[0-9a-f]{64}", re.ASCII)


def _well_formed_capability(token):
    return isinstance(token, str) and _CAPABILITY_GRAMMAR.fullmatch(token) is not None


def _expected_capability(tenant, scope, state, capability, *, acquiring=False):
    lease = state.checkout
    if lease is None or lease.expires_at <= state.observed_at:
        return None
    try:
        if not _well_formed_capability(capability):
            raise checkout_capability.CapabilityRejected()
        return checkout_capability.verify(capability, tenant, scope, public_checkout(lease))[1]
    except checkout_capability.CapabilityRejected:
        if acquiring:
            return None
        refuse("CHECKOUT_DENIED")
    except Exception:
        refuse("CHECKOUT_UNAVAILABLE")


def checkout_status(tenant, project_id, drawing_id):
    org_id, _actor = _access(tenant, project_id, write=False)
    try:
        return graph_store().get_checkout(org_id, project_id, drawing_id)
    except ProjectContextError:
        raise
    except Exception:
        refuse("STORE_UNAVAILABLE")


def acquire_project_checkout(tenant, project_id, drawing_id, *, holder=None,
                             ttl_s=3600, checkout_capability_token=None):
    org_id, actor = _access(tenant, project_id, write=True)
    scope = checkout_scope(project_id, drawing_id)
    selected_holder = "Project editor" if holder is None else holder
    graph_store().validate_checkout_params(selected_holder, ttl_s)
    try:
        checkout_capability.ensure_mintable(tenant, scope)
    except Exception:
        refuse("CHECKOUT_UNAVAILABLE")

    def operation(conn):
        prior = graph_store().get_checkout(org_id, project_id, drawing_id, conn=conn, for_update=True)
        expected = _expected_capability(tenant, scope, prior, checkout_capability_token, acquiring=True)
        granted = graph_store().acquire_checkout(
            org_id, project_id, drawing_id, actor_binding_id=actor, holder=selected_holder,
            ttl_s=ttl_s, expected_fence=expected, conn=conn)
        try:
            token = checkout_capability.mint(tenant, scope, granted.fence)
        except Exception:
            refuse("CHECKOUT_UNAVAILABLE")
        return granted, token

    return _mutation(operation)


def release_project_checkout(tenant, project_id, drawing_id, *, checkout_capability_token=None):
    org_id, actor = _access(tenant, project_id, write=True)
    scope = checkout_scope(project_id, drawing_id)

    def operation(conn):
        prior = graph_store().get_checkout(org_id, project_id, drawing_id, conn=conn, for_update=True)
        expected = _expected_capability(tenant, scope, prior, checkout_capability_token)
        return graph_store().release_checkout(org_id, project_id, drawing_id,
            actor_binding_id=actor, expected_fence=expected, conn=conn)

    return _mutation(operation)


def _mutation(operation):
    try:
        with write_loop.drawing_mutation_refusal_guard() as refusal:
            if refusal is not None:
                refuse("WRITES_DRAINED")
            return platform_link.platform_db().run_transaction(operation)
    except ProjectContextError:
        raise
    except Exception:
        refuse("STORE_UNAVAILABLE")


def verify_at_admission(tenant, project_id, drawing_id, input_version_id, *, checkout_capability):
    context = resolve_context(tenant, project_id, input_version_id, drawing_id=drawing_id, write=True)
    if not context.binding.is_head:
        refuse("STALE_VERSION")
    org_id, actor = _access(tenant, project_id, write=True)
    try:
        state = graph_store().get_checkout(org_id, project_id, drawing_id)
        if state.checkout is None:
            refuse("CHECKOUT_REQUIRED")
        if state.checkout.expires_at <= state.observed_at:
            refuse("CHECKOUT_EXPIRED")
        expected = _expected_capability(tenant, checkout_scope(project_id, drawing_id),
                                        state, checkout_capability)
        lease = graph_store().verify_checkout(org_id, project_id, drawing_id,
            actor_binding_id=actor, expected_fence=expected)
        return AdmissionContext(context, lease)
    except ProjectContextError:
        raise
    except Exception:
        refuse("STORE_UNAVAILABLE")
