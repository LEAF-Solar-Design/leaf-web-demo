"""Executable Wave B project-lifecycle and tenant-isolation contract."""
from __future__ import annotations

import json
import pathlib
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from psycopg.errors import ObjectNotInPrerequisiteState

from leaf_platform import project_lifecycle, store
from leaf_platform.db import cursor


# --------------------------------------------------------------------------- #
# Hermetic live-auth harness: a throwaway RS256 keypair and a local JWKS file,
# so the LIVE identity path (the one the browser ProjectSwitcher actually takes)
# is exercised here with no Auth0 and no network. Same pattern as
# platform/tests/test_wave_hardening_1b.py and server/tests/test_wave5.py.
# --------------------------------------------------------------------------- #
_ISS = "https://leaf-lifecycle.example/"
_AUD = "https://api.leaf-lifecycle.example"
_NS = "https://leafdesign.ai/"
_KID = "leaf-lifecycle-key-1"

_LIVE_PRIV = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_LIVE_PRIV_PEM = _LIVE_PRIV.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
)
_LIVE_JWK = json.loads(RSAAlgorithm.to_jwk(_LIVE_PRIV.public_key()))
_LIVE_JWK.update({"kid": _KID, "alg": "RS256", "use": "sig"})
_LIVE_JWKS_FILE = pathlib.Path(
    tempfile.mkdtemp(prefix="leaf-lifecycle-auth-")
) / "jwks.json"
_LIVE_JWKS_FILE.write_text(json.dumps({"keys": [_LIVE_JWK]}), encoding="utf-8")


@pytest.fixture
def live_auth(monkeypatch):
    """Turn Auth0 verification ON, pointed at the local RS256 JWKS (no network)."""
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_AUTH0_ISSUER", _ISS)
    monkeypatch.setenv("LEAF_AUTH0_AUDIENCE", _AUD)
    monkeypatch.setenv("LEAF_TENANT_CLAIM_NS", _NS)
    monkeypatch.setenv("LEAF_AUTH0_JWKS_FILE", str(_LIVE_JWKS_FILE))
    yield


def _bearer(org_id, subject: str, key: str | None = None) -> dict:
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": _ISS, "aud": _AUD, "iat": now, "exp": now + 3600,
            "sub": subject, _NS + "tenant_id": "tenant-lifecycle",
            _NS + "org_id": str(org_id),
        },
        _LIVE_PRIV_PEM, algorithm="RS256", headers={"kid": _KID},
    )
    headers = {"Authorization": "Bearer " + token}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _membership_rows(org_id, project_id):
    with cursor() as cur:
        cur.execute(
            "SELECT binding_id, role, status FROM project_member_bindings "
            "WHERE org_id = %(org_id)s AND project_id = %(project_id)s "
            "ORDER BY created_at, membership_id",
            {"org_id": org_id, "project_id": project_id},
        )
        return cur.fetchall()


def _binding(org_id, subject: str, role: str):
    return store.create_identity_binding(
        org_id, "auth0", subject, role=role,
    )


def _headers(org_id, binding_id, key: str | None = None):
    headers = {
        "X-Org-Id": str(org_id),
        "X-Actor-Binding-Id": str(binding_id),
    }
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def test_blank_project_members_files_and_immediate_revoke(client, make_org):
    org = make_org("Lifecycle A")
    owner = _binding(org.org_id, f"wave-b-owner-{uuid.uuid4()}", "owner")
    editor = _binding(org.org_id, f"wave-b-editor-{uuid.uuid4()}", "editor")
    uninvited_owner = _binding(
        org.org_id, f"wave-b-uninvited-owner-{uuid.uuid4()}", "owner",
    )
    uninvited_editor = _binding(
        org.org_id, f"wave-b-uninvited-editor-{uuid.uuid4()}", "editor",
    )
    reviewer = _binding(org.org_id, f"wave-b-reviewer-{uuid.uuid4()}", "reviewer")
    reader = _binding(org.org_id, f"wave-b-reader-{uuid.uuid4()}", "read_only")

    created = client.post(
        "/api/projects/blank",
        json={"name": "Blank browser project"},
        headers=_headers(org.org_id, owner.binding_id, "create-blank"),
    )
    assert created.status_code == 201, created.text
    project_id = created.json()["project"]["project_id"]
    assert created.json()["receipt"]["action"] == "project_created"

    replay = client.post(
        "/api/projects/blank",
        json={"name": "Blank browser project"},
        headers=_headers(org.org_id, owner.binding_id, "create-blank"),
    )
    assert replay.status_code == 201
    assert replay.json()["replayed"] is True
    assert replay.json()["project"]["project_id"] == project_id

    for binding in (uninvited_owner, uninvited_editor):
        assert client.get(
            f"/api/projects/{project_id}/lifecycle",
            headers=_headers(org.org_id, binding.binding_id),
        ).status_code == 403
        assert client.put(
            f"/api/projects/{project_id}/files",
            json={"path": "bypass.txt", "media_type": "text/plain", "content": "no"},
            headers=_headers(org.org_id, binding.binding_id, f"bypass-{binding.binding_id}"),
        ).status_code == 403

    memberships = {}
    for role, binding in (
        ("editor", editor),
        ("reviewer", reviewer),
        ("read_only", reader),
    ):
        response = client.post(
            f"/api/projects/{project_id}/members",
            json={"binding_id": str(binding.binding_id), "role": role},
            headers=_headers(org.org_id, owner.binding_id, f"invite-{role}"),
        )
        assert response.status_code == 201, response.text
        memberships[role] = response.json()["member"]["membership_id"]

    put = client.put(
        f"/api/projects/{project_id}/files",
        json={
            "path": "notes/intent.md",
            "media_type": "text/markdown",
            "content": "Keep one project across every workspace profile.",
        },
        headers=_headers(org.org_id, editor.binding_id, "put-intent"),
    )
    assert put.status_code == 200, put.text
    assert put.json()["file"]["revision"] == 1
    assert "content" not in put.json()["receipt"]

    for binding in (owner, editor, reviewer, reader):
        snapshot = client.get(
            f"/api/projects/{project_id}/lifecycle",
            headers=_headers(org.org_id, binding.binding_id),
        )
        assert snapshot.status_code == 200, snapshot.text
        assert snapshot.json()["files"][0]["path"] == "notes/intent.md"

    for binding, label in ((reviewer, "reviewer"), (reader, "reader")):
        denied = client.put(
            f"/api/projects/{project_id}/files",
            json={"path": f"{label}.txt", "media_type": "text/plain", "content": label},
            headers=_headers(org.org_id, binding.binding_id, f"put-{label}"),
        )
        assert denied.status_code == 403

    revoked = client.delete(
        f"/api/projects/{project_id}/members/{memberships['reviewer']}",
        headers=_headers(org.org_id, owner.binding_id, "revoke-reviewer"),
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["member"]["status"] == "revoked"
    assert client.get(
        f"/api/projects/{project_id}/lifecycle",
        headers=_headers(org.org_id, reviewer.binding_id),
    ).status_code == 403

    revoked_editor = client.delete(
        f"/api/projects/{project_id}/members/{memberships['editor']}",
        headers=_headers(org.org_id, owner.binding_id, "revoke-editor"),
    )
    assert revoked_editor.status_code == 200, revoked_editor.text
    assert client.get(
        f"/api/projects/{project_id}/lifecycle",
        headers=_headers(org.org_id, editor.binding_id),
    ).status_code == 403
    assert client.put(
        f"/api/projects/{project_id}/files",
        json={"path": "revoked.txt", "media_type": "text/plain", "content": "no"},
        headers=_headers(org.org_id, editor.binding_id, "revoked-editor-write"),
    ).status_code == 403


def test_clone_export_reset_delete_are_named_and_tenant_scoped(client, make_org):
    org_a = make_org("Lifecycle Tenant A")
    org_b = make_org("Lifecycle Tenant B")
    owner_a = _binding(org_a.org_id, f"wave-b-a-{uuid.uuid4()}", "owner")
    owner_b = _binding(org_b.org_id, f"wave-b-b-{uuid.uuid4()}", "owner")

    created = client.post(
        "/api/projects/blank",
        json={"name": "A source"},
        headers=_headers(org_a.org_id, owner_a.binding_id, "create-a"),
    )
    source_id = created.json()["project"]["project_id"]
    put = client.put(
        f"/api/projects/{source_id}/files",
        json={"path": "model/scene.json", "media_type": "application/json", "content": "{}"},
        headers=_headers(org_a.org_id, owner_a.binding_id, "put-source"),
    )
    source_file_id = put.json()["file"]["file_id"]

    for method, path, body, key in (
        ("get", f"/api/projects/{source_id}/lifecycle", None, None),
        ("post", f"/api/projects/{source_id}/export", None, "export-foreign"),
        ("post", f"/api/projects/{source_id}/reset", None, "reset-foreign"),
        ("delete", f"/api/projects/{source_id}/files/{source_file_id}", None, "delete-foreign"),
    ):
        response = client.request(
            method, path, json=body,
            headers=_headers(org_b.org_id, owner_b.binding_id, key),
        )
        assert response.status_code == 404, response.text

    cloned = client.post(
        f"/api/projects/{source_id}/clone",
        json={"name": "A clone"},
        headers=_headers(org_a.org_id, owner_a.binding_id, "clone-a"),
    )
    assert cloned.status_code == 201, cloned.text
    clone_id = cloned.json()["project"]["project_id"]
    assert clone_id != source_id
    assert cloned.json()["copied_file_count"] == 1

    exported = client.post(
        f"/api/projects/{clone_id}/export",
        headers=_headers(org_a.org_id, owner_a.binding_id, "export-clone"),
    )
    assert exported.status_code == 200, exported.text
    payload = exported.json()
    assert payload["export"]["schema"] == "leaf.project-export.v1"
    assert payload["export"]["project"]["project_id"] == clone_id
    assert [item["path"] for item in payload["export"]["files"]] == ["model/scene.json"]
    assert len(payload["export_sha256"]) == 64
    serialized = json.dumps(payload).lower()
    assert "external_subject" not in serialized
    assert "authorization" not in serialized
    assert "bearer " not in serialized

    reset = client.post(
        f"/api/projects/{clone_id}/reset",
        headers=_headers(org_a.org_id, owner_a.binding_id, "reset-clone"),
    )
    assert reset.status_code == 200, reset.text
    assert reset.json()["deleted_file_count"] == 1
    assert client.get(
        f"/api/projects/{source_id}/lifecycle",
        headers=_headers(org_a.org_id, owner_a.binding_id),
    ).json()["files"][0]["path"] == "model/scene.json"

    deleted = client.delete(
        f"/api/projects/{clone_id}",
        headers=_headers(org_a.org_id, owner_a.binding_id, "delete-clone"),
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["status"] == "deleted"
    retry = client.delete(
        f"/api/projects/{clone_id}",
        headers=_headers(org_a.org_id, owner_a.binding_id, "delete-clone"),
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["replayed"] is True
    assert client.get(
        f"/api/projects/{clone_id}/lifecycle",
        headers=_headers(org_a.org_id, owner_a.binding_id),
    ).status_code == 404


def test_lifecycle_refuses_key_rebinding_and_unsafe_paths(client, make_org):
    org = make_org("Lifecycle Negative")
    owner = _binding(org.org_id, f"wave-b-negative-{uuid.uuid4()}", "owner")
    project = client.post(
        "/api/projects/blank",
        json={"name": "Negative"},
        headers=_headers(org.org_id, owner.binding_id, "create-negative"),
    ).json()["project"]

    first = client.put(
        f"/api/projects/{project['project_id']}/files",
        json={"path": "safe.txt", "media_type": "text/plain", "content": "one"},
        headers=_headers(org.org_id, owner.binding_id, "same-key"),
    )
    assert first.status_code == 200
    rebound = client.put(
        f"/api/projects/{project['project_id']}/files",
        json={"path": "safe.txt", "media_type": "text/plain", "content": "two"},
        headers=_headers(org.org_id, owner.binding_id, "same-key"),
    )
    assert rebound.status_code == 409

    for unsafe in ("../secret", "/absolute", "folder\\windows.txt", "a/./b"):
        response = client.put(
            f"/api/projects/{project['project_id']}/files",
            json={"path": unsafe, "media_type": "text/plain", "content": "x"},
            headers=_headers(org.org_id, owner.binding_id, f"unsafe-{uuid.uuid4()}"),
        )
        assert response.status_code == 422, (unsafe, response.text)


def test_concurrent_blank_creation_mints_one_project_and_replays_one_receipt(make_org):
    org = make_org("Lifecycle Concurrency")
    owner = _binding(org.org_id, f"wave-b-concurrent-{uuid.uuid4()}", "owner")

    def create():
        return project_lifecycle.create_blank_project(
            org.org_id, owner.binding_id,
            name="Concurrent blank", idempotency_key="same-concurrent-create",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _index: create(), range(2)))

    assert first["project"]["project_id"] == second["project"]["project_id"]
    assert sorted([first["replayed"], second["replayed"]]) == [False, True]


def test_lifecycle_receipts_are_sanitized_and_database_immutable(make_org):
    org = make_org("Lifecycle Receipt")
    owner = _binding(org.org_id, f"wave-b-receipt-{uuid.uuid4()}", "owner")
    created = project_lifecycle.create_blank_project(
        org.org_id, owner.binding_id,
        name="Receipt blank", idempotency_key="receipt-create",
    )
    receipt_id = uuid.UUID(created["receipt"]["receipt_id"])

    with pytest.raises(ValueError, match="credential-shaped"):
        project_lifecycle._assert_sanitized_receipt(
            {"nested": {"access_token": "not-persisted"}},
        )
    with pytest.raises(ValueError, match="credential-shaped"):
        project_lifecycle._assert_sanitized_receipt(
            {"value": "eyJhbGciOiJSUzI1NiJ9.payload.signature"},
        )
    with pytest.raises(ValueError, match="credential material"):
        project_lifecycle._validate_idempotency_key(
            "eyJhbGciOiJSUzI1NiJ9.payload.signature",
        )
    project_lifecycle._assert_sanitized_receipt(
        {"project_id": created["project"]["project_id"], "status": "active"},
    )

    with pytest.raises(
        ObjectNotInPrerequisiteState, match="immutable canonical ledger record",
    ):
        with cursor() as cur:
            cur.execute(
                "UPDATE project_lifecycle_receipts SET action = 'project_deleted' "
                "WHERE receipt_id = %(receipt_id)s",
                {"receipt_id": receipt_id},
            )


def test_snapshot_names_the_caller_so_the_client_never_guesses_its_own_row(
    client, make_org,
):
    """A browser cannot learn its actor binding id: no route echoes one back.

    Without the snapshot's `viewer`, a client can only guess which roster row is
    the caller, and the role matrix (web/src/projects/Membership.jsx) refuses to
    render a guessed matrix - so the whole membership surface renders empty.
    """
    org = make_org("Lifecycle Viewer")
    owner = _binding(org.org_id, f"wave-b-viewer-owner-{uuid.uuid4()}", "owner")
    reader = _binding(org.org_id, f"wave-b-viewer-reader-{uuid.uuid4()}", "editor")
    project = client.post(
        "/api/projects/blank",
        json={"name": f"Viewer {uuid.uuid4()}"},
        headers=_headers(org.org_id, owner.binding_id, f"create-{uuid.uuid4()}"),
    ).json()["project"]
    client.post(
        f"/api/projects/{project['project_id']}/members",
        json={"binding_id": str(reader.binding_id), "role": "read_only"},
        headers=_headers(org.org_id, owner.binding_id, f"invite-{uuid.uuid4()}"),
    )

    owner_view = client.get(
        f"/api/projects/{project['project_id']}/lifecycle",
        headers=_headers(org.org_id, owner.binding_id),
    )
    assert owner_view.status_code == 200
    owner_viewer = owner_view.json()["viewer"]
    assert owner_viewer["binding_id"] == str(owner.binding_id)
    assert owner_viewer["role"] == "owner"
    assert owner_viewer["can_invite"] is True
    assert owner_viewer["can_manage"] is True
    # The membership id must resolve to a row actually present in the roster,
    # which is what the client keys its "this is you" lookup on.
    roster = {m["membership_id"] for m in owner_view.json()["members"]}
    assert owner_viewer["membership_id"] in roster

    reader_view = client.get(
        f"/api/projects/{project['project_id']}/lifecycle",
        headers=_headers(org.org_id, reader.binding_id),
    )
    assert reader_view.status_code == 200
    reader_viewer = reader_view.json()["viewer"]
    assert reader_viewer["binding_id"] == str(reader.binding_id)
    assert reader_viewer["role"] == "read_only"
    # A read-only member is told, by the server, that it may not mutate. The
    # client never derives this from the role string itself.
    assert reader_viewer["can_invite"] is False
    assert reader_viewer["can_manage"] is False


def test_cloning_twice_resolves_the_name_instead_of_raising(client, make_org):
    """Clone names are derived, not typed.

    CloneDialog takes no name field, so the panel sends "<name> (copy)" every
    time. Migration 0041 enforces one active name per org with a RAISING
    trigger, so an unresolved second clone surfaced as a 500 that no retry could
    ever clear.
    """
    org = make_org("Lifecycle Clone Names")
    owner = _binding(org.org_id, f"wave-b-clone-names-{uuid.uuid4()}", "owner")
    source_name = f"Rooftop {uuid.uuid4()}"
    project = client.post(
        "/api/projects/blank",
        json={"name": source_name},
        headers=_headers(org.org_id, owner.binding_id, f"create-{uuid.uuid4()}"),
    ).json()["project"]

    requested = f"{source_name} (copy)"
    granted = []
    for _attempt in range(3):
        response = client.post(
            f"/api/projects/{project['project_id']}/clone",
            json={"name": requested},
            headers=_headers(org.org_id, owner.binding_id, f"clone-{uuid.uuid4()}"),
        )
        assert response.status_code == 201, response.text
        granted.append(response.json()["project"]["name"])

    assert granted[0] == requested
    # Every clone lands, and each lands under its own distinct active name.
    assert len(set(granted)) == 3, granted
    for name in granted[1:]:
        assert name.startswith(requested)


def test_blank_creation_refuses_a_taken_name_as_a_conflict_not_a_crash(
    client, make_org,
):
    org = make_org("Lifecycle Duplicate Name")
    owner = _binding(org.org_id, f"wave-b-dupe-{uuid.uuid4()}", "owner")
    name = f"Duplicate {uuid.uuid4()}"
    first = client.post(
        "/api/projects/blank",
        json={"name": name},
        headers=_headers(org.org_id, owner.binding_id, f"create-{uuid.uuid4()}"),
    )
    assert first.status_code == 201, first.text

    # A DIFFERENT idempotency key, so this is a genuine second create rather
    # than a replay: the name is taken, and the caller must be told that
    # exactly, not handed a 500 from the migration's trigger.
    second = client.post(
        "/api/projects/blank",
        json={"name": name},
        headers=_headers(org.org_id, owner.binding_id, f"create-{uuid.uuid4()}"),
    )
    assert second.status_code == 409, second.text
    assert "already exists" in second.json()["detail"]


# --------------------------------------------------------------------------- #
# POST /api/projects -- the canonical project factory the browser ProjectSwitcher
# calls. It used to write the project row with NO project_member_bindings row.
# Lifecycle authority is membership-only, so every project born through this
# route was permanently unmanageable: staging 2026-08-24 showed POST 200 followed
# by DELETE 403 for the SAME verified Auth0 subject, leaving an orphan row only
# the operator-gated offboarding hard delete could remove.
# --------------------------------------------------------------------------- #
def test_live_auth_project_factory_creator_can_delete_what_it_created(
    client, make_org, live_auth,
):
    """THE regression: live-auth create via /api/projects, then DELETE by the
    same verified subject, must succeed rather than 403."""
    org = make_org("Factory live auth")
    subject = f"auth0|factory-live-{uuid.uuid4()}"
    owner = _binding(org.org_id, subject, "owner")

    created = client.post(
        "/api/projects",
        json={"name": f"Switcher project {uuid.uuid4()}"},
        headers=_bearer(org.org_id, subject),
    )
    assert created.status_code == 200, created.text
    project_id = created.json()["project"]["project_id"]

    # The creator holds an owner membership on its own project ...
    rows = _membership_rows(org.org_id, project_id)
    assert [(str(r["binding_id"]), r["role"], r["status"]) for r in rows] == [
        (str(owner.binding_id), "owner", "active"),
    ]

    # ... so every membership-gated lifecycle route now answers it.
    snapshot = client.get(
        f"/api/projects/{project_id}/lifecycle",
        headers=_bearer(org.org_id, subject),
    )
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["viewer"]["role"] == "owner"

    deleted = client.delete(
        f"/api/projects/{project_id}",
        headers=_bearer(org.org_id, subject, f"delete-{uuid.uuid4()}"),
    )
    assert deleted.status_code == 200, deleted.text


def test_live_auth_project_factory_membership_is_scoped_to_its_creator(
    client, make_org, live_auth,
):
    """The creator's row is the ONLY one written: a tenant owner who did not
    create the project still has no project authority over it."""
    org = make_org("Factory live scope")
    creator_subject = f"auth0|factory-creator-{uuid.uuid4()}"
    bystander_subject = f"auth0|factory-bystander-{uuid.uuid4()}"
    _binding(org.org_id, creator_subject, "owner")
    _binding(org.org_id, bystander_subject, "owner")

    created = client.post(
        "/api/projects",
        json={"name": f"Creator only {uuid.uuid4()}"},
        headers=_bearer(org.org_id, creator_subject),
    )
    assert created.status_code == 200, created.text
    project_id = created.json()["project"]["project_id"]

    assert client.get(
        f"/api/projects/{project_id}/lifecycle",
        headers=_bearer(org.org_id, bystander_subject),
    ).status_code == 403
    assert client.delete(
        f"/api/projects/{project_id}",
        headers=_bearer(org.org_id, bystander_subject, f"delete-{uuid.uuid4()}"),
    ).status_code == 403


def test_project_factory_without_an_actor_header_keeps_the_dev_seam(
    client, make_org,
):
    """Auth off proves no identity, so the documented header-only demo seam is
    unchanged: the project is created and no membership is invented for it."""
    org = make_org("Factory dev seam")
    created = client.post(
        "/api/projects",
        json={"name": f"Headerless {uuid.uuid4()}"},
        headers={"X-Org-Id": str(org.org_id)},
    )
    assert created.status_code == 200, created.text
    assert created.json()["created"] is True
    assert _membership_rows(org.org_id, created.json()["project"]["project_id"]) == []


def test_project_factory_dev_seam_binds_an_opted_in_actor(client, make_org):
    """With auth off the org is already client-supplied, so an explicit
    X-Actor-Binding-Id grants nothing new -- it only records who created the row
    so a dev harness gets the same manageable project a live caller gets."""
    org = make_org("Factory dev actor")
    editor = _binding(org.org_id, f"factory-dev-editor-{uuid.uuid4()}", "editor")

    created = client.post(
        "/api/projects",
        json={"name": f"Dev actor {uuid.uuid4()}"},
        headers=_headers(org.org_id, editor.binding_id),
    )
    assert created.status_code == 200, created.text
    project_id = created.json()["project"]["project_id"]

    # Mirrors create_blank_project: the project role IS the creator's tenant
    # role, so a tenant editor gets editor, not a silent promotion to owner.
    rows = _membership_rows(org.org_id, project_id)
    assert [(str(r["binding_id"]), r["role"], r["status"]) for r in rows] == [
        (str(editor.binding_id), "editor", "active"),
    ]
    assert client.delete(
        f"/api/projects/{project_id}",
        headers=_headers(org.org_id, editor.binding_id, f"delete-{uuid.uuid4()}"),
    ).status_code == 200


def test_project_factory_replay_backfills_an_orphan_but_never_a_revoked_member(
    client, make_org,
):
    """get-or-create replays. A replay must repair a project that has NO row for
    the caller (the orphans this defect already shipped), and must never hand a
    deliberately revoked member their access back."""
    org = make_org("Factory replay")
    owner = _binding(org.org_id, f"factory-replay-owner-{uuid.uuid4()}", "owner")
    editor = _binding(org.org_id, f"factory-replay-editor-{uuid.uuid4()}", "editor")
    name = f"Replay project {uuid.uuid4()}"

    # An orphan exactly as the defect shipped it: created with no membership.
    orphaned = client.post(
        "/api/projects", json={"name": name},
        headers={"X-Org-Id": str(org.org_id)},
    )
    assert orphaned.status_code == 200, orphaned.text
    project_id = orphaned.json()["project"]["project_id"]
    assert _membership_rows(org.org_id, project_id) == []

    # Replay by an identified caller backfills the missing membership.
    replay = client.post(
        "/api/projects", json={"name": name},
        headers=_headers(org.org_id, owner.binding_id),
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["created"] is False
    assert replay.json()["project"]["project_id"] == project_id
    assert [r["role"] for r in _membership_rows(org.org_id, project_id)] == ["owner"]

    invited = client.post(
        f"/api/projects/{project_id}/members",
        json={"binding_id": str(editor.binding_id), "role": "editor"},
        headers=_headers(org.org_id, owner.binding_id, f"invite-{uuid.uuid4()}"),
    )
    assert invited.status_code == 201, invited.text
    membership_id = invited.json()["member"]["membership_id"]

    revoked = client.delete(
        f"/api/projects/{project_id}/members/{membership_id}",
        headers=_headers(org.org_id, owner.binding_id, f"revoke-{uuid.uuid4()}"),
    )
    assert revoked.status_code == 200, revoked.text

    # THE escalation this must not permit: the revoked editor re-POSTs the same
    # project name and rides the replay path back into the project.
    reentry = client.post(
        "/api/projects", json={"name": name},
        headers=_headers(org.org_id, editor.binding_id),
    )
    assert reentry.status_code == 200, reentry.text
    editor_rows = [
        r for r in _membership_rows(org.org_id, project_id)
        if str(r["binding_id"]) == str(editor.binding_id)
    ]
    assert [r["status"] for r in editor_rows] == ["revoked"], editor_rows
    assert client.delete(
        f"/api/projects/{project_id}",
        headers=_headers(org.org_id, editor.binding_id, f"delete-{uuid.uuid4()}"),
    ).status_code == 403
