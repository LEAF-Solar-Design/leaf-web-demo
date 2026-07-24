"""Ready account-upload adoption into the canonical drawing ledger."""
from __future__ import annotations

import hashlib
import uuid
from concurrent.futures import ThreadPoolExecutor

from psycopg.types.json import Jsonb

import leaf_platform.store as store
from leaf_platform.db import cursor


def _binding(org_id, *, role="owner"):
    return store.create_identity_binding(
        org_id, "auth0", f"auth0|drawing-import-{uuid.uuid4()}", role=role)


def _ready_upload(org_id, *, tenant_kind="account", version=1,
                  marker_intake_sha256=None):
    drawing_id = uuid.uuid4()
    tenant_id = str(org_id)
    object_key = (
        f"tenants/{tenant_id}/drawings/{drawing_id}/v/{version:08d}.dwg"
    )
    source_bytes = f"source:{drawing_id}".encode()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    intake_ref = object_key[:-4] + ".intake.json"
    intake_sha256 = hashlib.sha256(
        f"intake:{drawing_id}".encode()).hexdigest()
    attempt = uuid.uuid4().hex[:16]
    marker = {
        "schema": 1,
        "status": "ready",
        "attempt": attempt,
        "tenant_kind": tenant_kind,
        "content_sha256": source_sha256,
        "extracted_version": version,
        "intake_ref": intake_ref,
        "intake_sha256": marker_intake_sha256 or intake_sha256,
    }
    with cursor() as cur:
        cur.execute(
            "INSERT INTO drawing_store_manifests "
            "(tenant_id, drawing_id, head, latest) VALUES (%s, %s, %s, %s)",
            (tenant_id, str(drawing_id), version, version),
        )
        cur.execute(
            "INSERT INTO drawing_store_versions "
            "(tenant_id, drawing_id, version, parent_version, object_key, byte_count, "
            "content_sha256, state, ready_at, intake_ref, intake_sha256) "
            "VALUES (%s, %s, %s, NULL, %s, %s, %s, 'ready', NOW(), %s, %s)",
            (tenant_id, str(drawing_id), version, object_key, len(source_bytes),
             source_sha256, intake_ref, intake_sha256),
        )
        cur.execute(
            "INSERT INTO drawing_upload_attempts "
            "(tenant_id, drawing_id, attempt, marker, status) VALUES (%s, %s, %s, %s, 'ready')",
            (tenant_id, str(drawing_id), attempt, Jsonb(marker)),
        )
    return drawing_id, source_sha256, object_key, attempt, intake_sha256


def _headers(org_id, binding_id, key):
    return {
        "X-Org-Id": str(org_id),
        "X-Actor-Binding-Id": str(binding_id),
        "Idempotency-Key": key,
    }


def _body(drawing_id, *, name="Rooftop Demo", version=1):
    return {
        "source": {
            "kind": "account_upload",
            "drawing_id": str(drawing_id),
            "version": version,
        },
        "name": name,
    }


def _canonical_project(make_org, name="Drawing import org"):
    org = make_org(name)
    project = store.create_project(org.org_id, "Drawing import project")
    store.set_project_authority_mode(
        org.org_id, project.project_id, "postgres_canonical")
    return org, project


def test_ready_account_upload_import_and_exact_api_replay(client, make_org):
    org, project = _canonical_project(make_org)
    binding = _binding(org.org_id)
    drawing_id, digest, object_key, attempt, intake_digest = _ready_upload(org.org_id)
    headers = _headers(org.org_id, binding.binding_id, "drawing-import-replay-1")

    first = client.post(
        f"/api/projects/{project.project_id}/drawing-versions/import",
        headers=headers,
        json=_body(drawing_id),
    )
    assert first.status_code == 201, first.text
    first_body = first.json()
    assert first_body["replayed"] is False
    version = first_body["drawing_version"]
    assert version["drawing_id"] == str(drawing_id)
    assert version["seq"] == 1
    assert version["oss_object"] == object_key
    assert version["intake_ref"] == object_key[:-4] + ".intake.json"
    assert version["created_by"] == str(binding.binding_id)
    assert version["provenance"] == {
        "schema": "leaf.drawing-import.v1",
        "source": {
            "kind": "account_upload",
            "tenant_id": str(org.org_id),
            "drawing_id": str(drawing_id),
            "version": 1,
            "upload_attempt": attempt,
            "upload_content_sha256": digest,
            "stored_object": {
                "ref": object_key,
                "sha256": digest,
                "bytes": len(f"source:{drawing_id}".encode()),
            },
            "intake": {
                "ref": object_key[:-4] + ".intake.json",
                "sha256": intake_digest,
                "proof": "ready_upload_after_fenced_cache_publication",
            },
        },
        "imported_by_binding_id": str(binding.binding_id),
    }

    replay = client.post(
        f"/api/projects/{project.project_id}/drawing-versions/import",
        headers=headers,
        json=_body(drawing_id),
    )
    assert replay.status_code == 201
    assert replay.json()["replayed"] is True
    assert replay.json()["drawing_version"] == version

    hydrated = client.get(
        f"/api/projects/{project.project_id}",
        headers={"X-Org-Id": str(org.org_id)},
    ).json()
    assert [row["version_id"] for row in hydrated["drawing_versions"]] == [
        version["version_id"]]


def test_import_refuses_client_refs_and_key_reuse_with_different_input(client, make_org):
    org, project = _canonical_project(make_org, "Drawing import input guard")
    binding = _binding(org.org_id)
    drawing_id, *_ = _ready_upload(org.org_id)
    headers = _headers(org.org_id, binding.binding_id, "drawing-import-input-1")
    path = f"/api/projects/{project.project_id}/drawing-versions/import"

    assert client.post(path, headers=headers, json=_body(drawing_id)).status_code == 201
    conflict = client.post(
        path, headers=headers, json=_body(drawing_id, name="Different name"))
    assert conflict.status_code == 409

    forged = _body(drawing_id)
    forged["oss_object"] = "client/chosen.dwg"
    forged["source"]["intake_ref"] = "client/chosen.json"
    rejected = client.post(path, headers={**headers, "Idempotency-Key": "forged"}, json=forged)
    assert rejected.status_code == 422
    with cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS count FROM drawing_versions "
            "WHERE org_id = %s AND project_id = %s",
            (org.org_id, project.project_id),
        )
        assert cur.fetchone()["count"] == 1


def test_foreign_and_guest_sources_share_one_not_found_shape(client, make_org):
    org, project = _canonical_project(make_org, "Drawing import isolation")
    binding = _binding(org.org_id)
    guest_drawing, *_ = _ready_upload(org.org_id, tenant_kind="guest")
    foreign = make_org("Foreign drawing import source")
    foreign_drawing, *_ = _ready_upload(foreign.org_id)
    path = f"/api/projects/{project.project_id}/drawing-versions/import"

    responses = [
        client.post(
            path,
            headers=_headers(org.org_id, binding.binding_id, f"hidden-{index}"),
            json=_body(drawing_id),
        )
        for index, drawing_id in enumerate((guest_drawing, foreign_drawing, uuid.uuid4()))
    ]
    assert [response.status_code for response in responses] == [404, 404, 404]
    assert len({response.text for response in responses}) == 1
    with cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS count FROM drawing_artifacts WHERE org_id = %s",
            (org.org_id,),
        )
        assert cur.fetchone()["count"] == 0


def test_intake_digest_mismatch_fails_without_canonical_state(client, make_org):
    org, project = _canonical_project(make_org, "Drawing intake proof guard")
    binding = _binding(org.org_id)
    drawing_id, *_ = _ready_upload(
        org.org_id, marker_intake_sha256="0" * 64)

    response = client.post(
        f"/api/projects/{project.project_id}/drawing-versions/import",
        headers=_headers(org.org_id, binding.binding_id, "intake-proof-mismatch"),
        json=_body(drawing_id),
    )
    assert response.status_code == 404
    with cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS count FROM drawing_artifacts WHERE org_id = %s",
            (org.org_id,),
        )
        assert cur.fetchone()["count"] == 0
        cur.execute(
            "SELECT COUNT(*) AS count FROM drawing_versions WHERE org_id = %s",
            (org.org_id,),
        )
        assert cur.fetchone()["count"] == 0


def test_import_requires_mutating_role_and_canonical_authority(client, make_org):
    org = make_org("Drawing import authorization")
    project = store.create_project(org.org_id, "Authorization project")
    drawing_id, *_ = _ready_upload(org.org_id)
    reviewer = _binding(org.org_id, role="reviewer")
    path = f"/api/projects/{project.project_id}/drawing-versions/import"

    legacy = client.post(
        path,
        headers=_headers(org.org_id, reviewer.binding_id, "legacy-denied"),
        json=_body(drawing_id),
    )
    assert legacy.status_code == 404

    store.set_project_authority_mode(org.org_id, project.project_id, "postgres_canonical")
    denied = client.post(
        path,
        headers=_headers(org.org_id, reviewer.binding_id, "role-denied"),
        json=_body(drawing_id),
    )
    assert denied.status_code == 403
    assert client.post(
        path,
        headers={"X-Org-Id": str(org.org_id), "Idempotency-Key": "missing-actor"},
        json=_body(drawing_id),
    ).status_code == 400


def test_concurrent_exact_replay_creates_one_version(make_org):
    org, project = _canonical_project(make_org, "Drawing import concurrency")
    binding = _binding(org.org_id)
    drawing_id, *_ = _ready_upload(org.org_id)

    def adopt():
        return store.import_ready_account_upload(
            org.org_id,
            project.project_id,
            source_drawing_id=drawing_id,
            source_version=1,
            name="Concurrent drawing",
            idempotency_key="drawing-import-concurrent-1",
            actor_binding_id=binding.binding_id,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: adopt(), range(2)))
    assert sorted(replayed for _, replayed in results) == [False, True]
    assert len({version.version_id for version, _ in results}) == 1
    with cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS count FROM drawing_versions "
            "WHERE org_id = %s AND project_id = %s",
            (org.org_id, project.project_id),
        )
        assert cur.fetchone()["count"] == 1
