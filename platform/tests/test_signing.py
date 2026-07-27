from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import uuid

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from leaf_platform import compliance_store, db, evidence_store, signing, store
from leaf_platform.db import cursor
from leaf_platform.offboard import offboard_org
from test_compliance_store import INPUTS, PACK, _completed_solve


@pytest.fixture(autouse=True)
def _reset_provider():
    signing.configure_signature_provider(None)
    yield
    signing.configure_signature_provider(None)


def _fixture(make_org, label, *, approve_failure=True, role="reviewer"):
    org, project, solve_id = _completed_solve(make_org, label)
    run = compliance_store.record_run(org.org_id, project.project_id, solve_id, PACK, INPUTS)
    owner = store.create_identity_binding(org.org_id, "auth0", f"{label}-owner", role="owner")
    actor = (owner if role == "owner" else store.create_identity_binding(
        org.org_id, "auth0", f"{label}-actor", role=role))
    if approve_failure:
        waiver = compliance_store.propose_waiver(
            org.org_id, project.project_id, uuid.UUID(run["findings"][0]["finding_id"]),
            owner.binding_id, "documented engineering disposition")
        compliance_store.transition_waiver(
            org.org_id, project.project_id, uuid.UUID(waiver["waiver_id"]),
            owner.binding_id, "approved", "reviewed")
    bundle = evidence_store.create_bundle(
        org.org_id, project.project_id, solve_id, f"{label}-bundle")
    key = ed25519.Ed25519PrivateKey.generate()
    public_key = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    now = datetime.now(timezone.utc)
    credential = signing.register_credential(
        org.org_id, actor.binding_id, jurisdiction="MN", license_ref=f"PE-{label}",
        algorithm="ed25519", public_key=public_key, provider_key_ref=f"local:{label}",
        verified_by="credential-operator", verified_at=now - timedelta(days=1),
        expires_at=now + timedelta(days=365))
    return org, project, bundle, actor, credential, key


def test_provider_backed_signature_verifies_offline_and_is_immutable(make_org):
    org, project, bundle, actor, credential, key = _fixture(make_org, "sign")
    signing.configure_signature_provider(signing.LocalEd25519Provider({"local:sign": key}))
    first = signing.countersign(
        org.org_id, project.project_id, uuid.UUID(bundle["bundle_id"]),
        uuid.UUID(credential["credential_id"]), actor.binding_id, "sign-once")
    repeated = signing.countersign(
        org.org_id, project.project_id, uuid.UUID(bundle["bundle_id"]),
        uuid.UUID(credential["credential_id"]), actor.binding_id, "sign-once")
    same_credential_new_key = signing.countersign(
        org.org_id, project.project_id, uuid.UUID(bundle["bundle_id"]),
        uuid.UUID(credential["credential_id"]), actor.binding_id, "sign-again")
    assert repeated == first
    assert same_credential_new_key == first
    verified = signing.verify_signature(
        org.org_id, project.project_id, uuid.UUID(first["signature_id"]))
    assert verified["valid"] is True
    assert verified["cryptographic_valid"] is True
    with cursor() as cur:
        cur.execute("SELECT operation_type, payload FROM history_operations "
                    "WHERE operation_id = %(operation)s",
                    {"operation": first["history_operation_id"]})
        history = cur.fetchone()
        assert history["operation_type"] == "review.bundle.countersigned"
        assert history["payload"]["signatureId"] == first["signature_id"]
        with pytest.raises(Exception, match="immutable canonical ledger"):
            cur.execute("UPDATE review_signatures SET root_sha256 = %(root)s "
                        "WHERE signature_id = %(signature)s",
                        {"root": "0" * 64, "signature": first["signature_id"]})
    result = offboard_org(
        org.org_id, key_purge_hook=lambda _ref: None, blob_purge_hook=lambda _ref: None)
    assert result.status == "deleted"
    assert result.deleted_projects == 1


def test_revocation_preserves_crypto_proof_but_invalidates_authorization(make_org):
    org, project, bundle, actor, credential, key = _fixture(make_org, "revoke")
    signing.configure_signature_provider(signing.LocalEd25519Provider({"local:revoke": key}))
    signed = signing.countersign(
        org.org_id, project.project_id, uuid.UUID(bundle["bundle_id"]),
        uuid.UUID(credential["credential_id"]), actor.binding_id, "revoke-sign")
    signing.revoke_credential(
        org.org_id, uuid.UUID(credential["credential_id"]),
        actor="credential-operator", reason="license revoked")
    verified = signing.verify_signature(
        org.org_id, project.project_id, uuid.UUID(signed["signature_id"]))
    assert verified["valid"] is False
    assert verified["cryptographic_valid"] is True
    assert verified["authorization_valid"] is False
    assert "credential_revoked" in verified["errors"]


def test_later_design_operation_invalidates_the_seal(make_org):
    org, project, bundle, actor, credential, key = _fixture(make_org, "superseded")
    signing.configure_signature_provider(signing.LocalEd25519Provider({"local:superseded": key}))
    signed = signing.countersign(
        org.org_id, project.project_id, uuid.UUID(bundle["bundle_id"]),
        uuid.UUID(credential["credential_id"]), actor.binding_id, "superseded-sign")
    store.append_history_operation(
        org.org_id, project.project_id, "drawing.mutation", {"change": "post-review edit"},
        "post-review-edit")
    verified = signing.verify_signature(
        org.org_id, project.project_id, uuid.UUID(signed["signature_id"]))
    assert verified["cryptographic_valid"] is True
    assert verified["valid"] is False
    assert "bundle_superseded" in verified["errors"]


_NON_UTC_ZONE = "America/Chicago"


@contextmanager
def _non_utc_reader_session():
    """Force every pooled connection onto a non-UTC session timezone.

    psycopg renders TIMESTAMPTZ in the SESSION timezone, so this is the only
    thing that makes verify_signature()'s signedAt round-trip observable. CI
    runs a UTC postgres, where the unfixed re-derivation happens to agree, so
    the test has to pin the timezone itself rather than trust the ambient one.
    """
    original = db._configure

    def configure(conn):
        original(conn)
        # SET is transactional; commit so the pool's reset cannot roll it back.
        conn.execute(f"SET TIME ZONE '{_NON_UTC_ZONE}'")
        conn.commit()

    db.reset_pool()
    db._configure = configure
    try:
        yield
    finally:
        db._configure = original
        db.reset_pool()


def test_signature_verifies_when_the_reader_session_is_not_utc(make_org):
    """A signature must verify regardless of the reading session's timezone.

    signed_at is TIMESTAMPTZ (migration 0009) and verify_signature() re-derives
    the signedAt string from it to compare against the signed payload. Nothing
    pins the session timezone, so before the astimezone(utc) normalization a
    server whose TimeZone GUC was not UTC failed EVERY signature with
    payload_mismatch. Signing under the ambient session and verifying under a
    non-UTC one also covers already-stored (UTC-written) rows.
    """
    org, project, bundle, actor, credential, key = _fixture(make_org, "tz-reader")
    signing.configure_signature_provider(
        signing.LocalEd25519Provider({"local:tz-reader": key}))
    signed = signing.countersign(
        org.org_id, project.project_id, uuid.UUID(bundle["bundle_id"]),
        uuid.UUID(credential["credential_id"]), actor.binding_id, "tz-reader-sign")
    with _non_utc_reader_session():
        # Assert the precondition, or this test silently stops proving anything.
        with cursor() as cur:
            cur.execute("SHOW TimeZone")
            assert cur.fetchone()["TimeZone"] == _NON_UTC_ZONE
            cur.execute("SELECT signed_at FROM review_signatures "
                        "WHERE signature_id = %(signature)s",
                        {"signature": signed["signature_id"]})
            rendered = cur.fetchone()["signed_at"]
        # The column really does read back off-UTC: that is the failure input.
        assert rendered.utcoffset() != timedelta(0)
        verified = signing.verify_signature(
            org.org_id, project.project_id, uuid.UUID(signed["signature_id"]))
    assert verified["errors"] == []
    assert verified["cryptographic_valid"] is True
    assert verified["valid"] is True


def test_concurrent_replay_creates_one_signature_and_history_operation(make_org):
    org, project, bundle, actor, credential, key = _fixture(make_org, "concurrent-sign")
    signing.configure_signature_provider(
        signing.LocalEd25519Provider({"local:concurrent-sign": key}))

    def invoke(idempotency_key):
        return signing.countersign(
            org.org_id, project.project_id, uuid.UUID(bundle["bundle_id"]),
            uuid.UUID(credential["credential_id"]), actor.binding_id, idempotency_key)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, ["concurrent-a", "concurrent-b"]))
    assert results[0]["signature_id"] == results[1]["signature_id"]
    with cursor() as cur:
        cur.execute("SELECT COUNT(*) AS count FROM review_signatures WHERE bundle_id = %(bundle)s",
                    {"bundle": bundle["bundle_id"]})
        assert cur.fetchone()["count"] == 1
        cur.execute("SELECT COUNT(*) AS count FROM history_operations "
                    "WHERE operation_type = 'review.bundle.countersigned' "
                    "AND payload->>'bundleId' = %(bundle)s",
                    {"bundle": bundle["bundle_id"]})
        assert cur.fetchone()["count"] == 1


def test_concurrent_different_credentials_cannot_orphan_history(make_org):
    org, project, bundle, actor, credential, key = _fixture(make_org, "concurrent-identity")
    second_actor = store.create_identity_binding(
        org.org_id, "auth0", "concurrent-identity-second", role="reviewer")
    second_key = ed25519.Ed25519PrivateKey.generate()
    second_public_key = second_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    now = datetime.now(timezone.utc)
    second_credential = signing.register_credential(
        org.org_id, second_actor.binding_id, jurisdiction="MN", license_ref="PE-second",
        algorithm="ed25519", public_key=second_public_key,
        provider_key_ref="local:concurrent-identity-second",
        verified_by="credential-operator", verified_at=now - timedelta(days=1),
        expires_at=now + timedelta(days=365))
    signing.configure_signature_provider(signing.LocalEd25519Provider({
        "local:concurrent-identity": key,
        "local:concurrent-identity-second": second_key,
    }))

    def invoke(args):
        candidate, candidate_actor = args
        try:
            return signing.countersign(
                org.org_id, project.project_id, uuid.UUID(bundle["bundle_id"]),
                uuid.UUID(candidate["credential_id"]), candidate_actor.binding_id,
                "shared-concurrent-key")
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, [
            (credential, actor), (second_credential, second_actor),
        ]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum("different countersign input" in result for result in results
               if isinstance(result, str)) == 1
    with cursor() as cur:
        cur.execute("SELECT COUNT(*) AS count FROM review_signatures WHERE bundle_id = %(bundle)s",
                    {"bundle": bundle["bundle_id"]})
        assert cur.fetchone()["count"] == 1
        cur.execute("SELECT COUNT(*) AS count FROM history_operations "
                    "WHERE operation_type = 'review.bundle.countersigned' "
                    "AND payload->>'bundleId' = %(bundle)s",
                    {"bundle": bundle["bundle_id"]})
        assert cur.fetchone()["count"] == 1


def test_unresolved_failure_and_missing_provider_fail_closed(make_org):
    org, project, bundle, actor, credential, key = _fixture(
        make_org, "unresolved", approve_failure=False)
    with pytest.raises(RuntimeError, match="not configured"):
        signing.countersign(
            org.org_id, project.project_id, uuid.UUID(bundle["bundle_id"]),
            uuid.UUID(credential["credential_id"]), actor.binding_id, "no-provider")
    signing.configure_signature_provider(
        signing.LocalEd25519Provider({"local:unresolved": key}))
    with pytest.raises(ValueError, match="unresolved failing"):
        signing.countersign(
            org.org_id, project.project_id, uuid.UUID(bundle["bundle_id"]),
            uuid.UUID(credential["credential_id"]), actor.binding_id, "unresolved")


def test_credential_role_and_provider_signature_are_enforced(make_org):
    org, project, bundle, actor, credential, _key = _fixture(make_org, "invalid-provider")
    wrong_key = ed25519.Ed25519PrivateKey.generate()
    signing.configure_signature_provider(
        signing.LocalEd25519Provider({"local:invalid-provider": wrong_key}))
    with pytest.raises(ValueError, match="invalid signature"):
        signing.countersign(
            org.org_id, project.project_id, uuid.UUID(bundle["bundle_id"]),
            uuid.UUID(credential["credential_id"]), actor.binding_id, "wrong-key")
    editor = store.create_identity_binding(
        org.org_id, "auth0", "invalid-provider-editor", role="editor")
    public_key = wrong_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="owner or reviewer"):
        signing.register_credential(
            org.org_id, editor.binding_id, jurisdiction="WI", license_ref="PE-editor",
            algorithm="ed25519", public_key=public_key, provider_key_ref="local:editor",
            verified_by="operator", verified_at=now, expires_at=now + timedelta(days=1))


def test_signature_api_is_review_bound_and_reports_provider_outage(client, make_org):
    org, project, bundle, actor, credential, key = _fixture(make_org, "signature-api")
    route = (f"/api/projects/{project.project_id}/evidence-bundles/"
             f"{bundle['bundle_id']}/signatures")
    headers = {"X-Org-Id": str(org.org_id),
               "X-Actor-Binding-Id": str(actor.binding_id),
               "Idempotency-Key": "api-sign"}
    context_headers = {"X-Org-Id": str(org.org_id),
                       "X-Actor-Binding-Id": str(actor.binding_id)}
    unavailable_context = client.get(
        "/api/professional-review/context", headers=context_headers)
    assert unavailable_context.status_code == 200
    assert unavailable_context.json()["signing_available"] is False
    assert unavailable_context.json()["reason"] == "signature_provider_unavailable"
    listed_unsigned = client.get(
        f"/api/projects/{project.project_id}/evidence-bundles",
        headers={"X-Org-Id": str(org.org_id)})
    assert listed_unsigned.status_code == 200
    assert listed_unsigned.json()["bundles"][0]["state"] == "unsigned"
    unavailable = client.post(
        route, headers=headers, json={"credential_id": credential["credential_id"]})
    assert unavailable.status_code == 503
    signing.configure_signature_provider(
        signing.LocalEd25519Provider({"local:signature-api": key}))
    available_context = client.get(
        "/api/professional-review/context", headers=context_headers)
    assert available_context.status_code == 200
    assert available_context.json()["signing_available"] is True
    assert available_context.json()["credential"]["credential_id"] == credential["credential_id"]
    created = client.post(
        route, headers=headers, json={"credential_id": credential["credential_id"]})
    assert created.status_code == 201, created.text
    checked = client.get(
        f"/api/projects/{project.project_id}/review-signatures/"
        f"{created.json()['signature_id']}/verify",
        headers={"X-Org-Id": str(org.org_id)})
    assert checked.status_code == 200
    assert checked.json()["valid"] is True
    listed_signed = client.get(
        f"/api/projects/{project.project_id}/evidence-bundles",
        headers={"X-Org-Id": str(org.org_id)})
    assert listed_signed.status_code == 200
    summary = listed_signed.json()["bundles"][0]
    assert summary["state"] == "signed_valid"
    assert summary["latest_signature_id"] == created.json()["signature_id"]
    assert summary["verification"]["valid"] is True
