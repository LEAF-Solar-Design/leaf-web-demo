import base64
import uuid

import pytest

from leaf_platform import access, compliance_store, evidence, evidence_store, store
from leaf_platform.db import cursor
from test_compliance_store import INPUTS, PACK, _completed_solve


def test_frozen_bundle_exports_and_verifies_offline(make_org):
    org, project, solve_id = _completed_solve(make_org, "evidence")
    run = compliance_store.record_run(org.org_id, project.project_id, solve_id, PACK, INPUTS)
    owner = store.create_identity_binding(org.org_id, "auth0", "evidence-owner", role="owner")
    waiver = compliance_store.propose_waiver(
        org.org_id, project.project_id, uuid.UUID(run["findings"][0]["finding_id"]),
        owner.binding_id, "field condition")
    compliance_store.transition_waiver(
        org.org_id, project.project_id, uuid.UUID(waiver["waiver_id"]), owner.binding_id,
        "approved", "local evidence fixture")
    first = evidence_store.create_bundle(
        org.org_id, project.project_id, solve_id, "evidence-1",
        artifacts={"drawing.dwg": b"fixture drawing"})
    repeated = evidence_store.create_bundle(
        org.org_id, project.project_id, solve_id, "evidence-1",
        artifacts={"drawing.dwg": b"fixture drawing"})
    assert repeated == first
    exported = evidence_store.export_bundle(
        org.org_id, project.project_id, uuid.UUID(first["bundle_id"]))
    blobs = {path: base64.b64decode(content) for path, content in exported["entriesBase64"].items()}
    assert evidence.verify(exported["manifest"], blobs)["valid"]
    assert exported["manifest"]["metadata"]["scope"] == "records_and_artifacts"
    summaries = evidence_store.list_bundles(org.org_id, project.project_id)
    assert summaries == [{
        "bundle_id": first["bundle_id"],
        "solve_id": str(solve_id),
        "root_sha256": first["manifest"]["rootSha256"],
        "created_at": summaries[0]["created_at"],
        "entry_count": len(first["manifest"]["entries"]),
        "scope": "records_and_artifacts",
        "state": "unsigned",
        "superseded": False,
        "latest_signature_id": None,
        "signed_at": None,
        "verification": None,
    }]
    with cursor() as cur:
        with pytest.raises(Exception, match="immutable canonical ledger"):
            cur.execute("UPDATE evidence_bundles SET root_sha256 = %(root)s WHERE bundle_id = %(id)s",
                        {"root": "0" * 64, "id": first["bundle_id"]})


def test_bundle_rejects_missing_findings_and_idempotency_content_change(make_org):
    org, project, solve_id = _completed_solve(make_org, "evidence-empty")
    with pytest.raises(ValueError, match="compliance finding"):
        evidence_store.create_bundle(org.org_id, project.project_id, solve_id, "empty")
    compliance_store.record_run(org.org_id, project.project_id, solve_id, PACK, INPUTS)
    evidence_store.create_bundle(org.org_id, project.project_id, solve_id, "same",
                                 artifacts={"report.txt": b"one"})
    with pytest.raises(ValueError, match="different bundle content"):
        evidence_store.create_bundle(org.org_id, project.project_id, solve_id, "same",
                                     artifacts={"report.txt": b"two"})


def test_share_token_can_only_resolve_its_project_bundle_and_revocation_is_immediate(client, make_org):
    org, project, solve_id = _completed_solve(make_org, "shared-evidence")
    compliance_store.record_run(org.org_id, project.project_id, solve_id, PACK, INPUTS)
    bundle = evidence_store.create_bundle(org.org_id, project.project_id, solve_id, "shared")
    owner = store.create_identity_binding(org.org_id, "auth0", "shared-evidence-owner", role="owner")
    grant = access.create_project_share_grant(
        org.org_id, project.project_id, owner.binding_id, role="reviewer")
    route = f"/api/shared/evidence-bundles/{bundle['bundle_id']}/resolve"
    resolved = client.post(route, json={"token": grant["token"]})
    assert resolved.status_code == 200
    assert resolved.json()["role"] == "reviewer"
    assert resolved.json()["manifest"]["rootSha256"] == bundle["manifest"]["rootSha256"]
    assert access.revoke_project_share_grant(
        org.org_id, project.project_id, uuid.UUID(grant["grant_id"]), owner.binding_id) == "revoked"
    assert client.post(route, json={"token": grant["token"]}).status_code == 404
