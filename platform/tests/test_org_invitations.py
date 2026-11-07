"""Hashed single-use invitations, owner boundaries, and atomic membership grants."""
import hashlib
import secrets
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from psycopg.errors import RaiseException

from leaf_platform import deps, org_invitations as invitations, store


def test_token_shape():
    for _ in range(10):
        token = secrets.token_urlsafe(32)
        assert len(token) == 43
        assert invitations.valid_token(token)


def test_bad_token_shapes_are_refused_before_hashing(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("malformed tokens must not be hashed")
    monkeypatch.setattr(invitations.hashlib, "sha256", unexpected)
    for token in (None, 42, "", "a" * 42, "a" * 44, "a" * 42 + "=", "é" * 43, "a" * 43 + "\n"):
        assert not invitations.valid_token(token)
        with pytest.raises(ValueError):
            invitations.hash_token(token)


def test_token_hash_is_sha256_hex():
    token = "a" * 43
    assert invitations.hash_token(token) == hashlib.sha256(token.encode("ascii")).hexdigest()


def test_label_bounds():
    for label in (None, "", "Team member", "é" * 80):
        assert invitations.validate_label(label) == label
    for label in ("x" * 81, "line\nbreak", "tab\t", "\x7f", 42):
        with pytest.raises(ValueError):
            invitations.validate_label(label)


@pytest.fixture
def owner(make_org, monkeypatch):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    org = make_org(name="Inviting org")
    binding = store.create_identity_binding(org.org_id, "auth0", f"owner-{uuid.uuid4()}")

    def verify(authorization):
        if not authorization or not authorization.startswith("Bearer test:"):
            raise HTTPException(401, "verified JWT required")
        return {"sub": authorization.removeprefix("Bearer test:")}

    monkeypatch.setattr(deps, "_server_auth", lambda: SimpleNamespace(verify_platform_token=verify))
    return org, binding, {"X-Org-Id": str(org.org_id), "X-Actor-Binding-Id": str(binding.binding_id)}


def _url(owner):
    return f"/api/orgs/{owner[0].org_id}/invitations"


def _create(client, owner, **body):
    response = client.post(_url(owner), headers=owner[2], json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _accept(client, token, subject=None):
    return client.post("/api/invitations/accept", json={"token": token},
                       headers={"Authorization": f"Bearer test:{subject or uuid.uuid4()}"})


def _row(invitation):
    with store.cursor() as cur:
        cur.execute("SELECT * FROM org_invitations WHERE id = %s", (uuid.UUID(invitation["invitation_id"]),))
        return cur.fetchone()


def test_owner_creates_lists_and_persists_only_hash(client, owner):
    before = datetime.now(timezone.utc)
    invitation = _create(client, owner, label="Second member")
    assert set(invitation) == {"invitation_id", "expires_at", "token"}
    assert invitations.valid_token(invitation["token"])
    assert before + timedelta(days=7) <= datetime.fromisoformat(invitation["expires_at"])
    assert datetime.fromisoformat(invitation["expires_at"]) <= datetime.now(timezone.utc) + timedelta(days=7)
    row = _row(invitation)
    assert row["role"] == "editor"
    assert row["token_hash"] == hashlib.sha256(invitation["token"].encode()).hexdigest()
    assert invitation["token"] not in str(row)
    assert row["created_by_binding_id"] == owner[1].binding_id
    response = client.get(_url(owner), headers=owner[2])
    assert response.status_code == 200, response.text
    assert response.json() == {"invitations": [{
        "invitation_id": invitation["invitation_id"], "label": "Second member",
        "created_at": row["created_at"].isoformat(), "expires_at": invitation["expires_at"],
    }]}
    assert invitation["token"] not in response.text
    assert row["token_hash"] not in response.text


@pytest.mark.parametrize("method", ["post", "get", "delete"])
def test_editor_cannot_administer_invitations(client, owner, method):
    invitation = _create(client, owner)
    editor = store.create_identity_binding(owner[0].org_id, "auth0", str(uuid.uuid4()), role="editor")
    headers = {**owner[2], "X-Actor-Binding-Id": str(editor.binding_id)}
    url = _url(owner) + (f"/{invitation['invitation_id']}" if method == "delete" else "")
    response = getattr(client, method)(url, headers=headers, **({"json": {}} if method == "post" else {}))
    assert response.status_code == 403, response.text
    assert _row(invitation)["revoked_at"] is None


@pytest.mark.parametrize("method", ["get", "delete", "post"])
def test_other_org_is_hidden(client, owner, make_org, method):
    invitation = _create(client, owner)
    other = make_org()
    url = f"/api/orgs/{other.org_id}/invitations"
    if method == "delete":
        url += f"/{invitation['invitation_id']}"
    response = getattr(client, method)(url, headers=owner[2], **({"json": {}} if method == "post" else {}))
    assert response.status_code == 404, response.text
    assert _row(invitation)["revoked_at"] is None


def test_other_org_invitation_id_is_hidden(client, owner, make_org):
    other = make_org()
    binding = store.create_identity_binding(other.org_id, "auth0", str(uuid.uuid4()))
    invitation = invitations.create(other.org_id, binding.binding_id)
    response = client.delete(f"{_url(owner)}/{invitation['invitation_id']}", headers=owner[2])
    assert response.status_code == 404
    assert _row(invitation)["revoked_at"] is None


def test_accept_creates_exactly_one_editor_and_consumes_token(client, owner):
    invitation = _create(client, owner)
    subject = f"invitee-{uuid.uuid4()}"
    response = _accept(client, invitation["token"], subject)
    assert response.status_code == 200, response.text
    result = response.json()
    binding = store.resolve_active_identity_binding("auth0", subject)
    assert result == {"org_id": str(owner[0].org_id), "binding_id": str(binding.binding_id), "role": "editor"}
    assert store.active_identity_role(owner[0].org_id, binding.binding_id) == "editor"
    row = _row(invitation)
    assert row["accepted_at"] is not None
    assert row["accepted_binding_id"] == binding.binding_id
    assert client.get(_url(owner), headers=owner[2]).json() == {"invitations": []}
    second_subject = f"second-{uuid.uuid4()}"
    second = _accept(client, invitation["token"], second_subject)
    assert second.status_code == 404
    assert second.json()["detail"] == "invitation_unavailable"
    assert store.resolve_active_identity_binding("auth0", second_subject) is None
    with store.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS count FROM identity_bindings "
                    "WHERE external_authority = 'auth0' AND external_subject = %s", (subject,))
        assert cur.fetchone()["count"] == 1


@pytest.mark.parametrize("same_org", [False, True])
def test_existing_member_is_never_changed(client, owner, make_org, same_org):
    invitation = _create(client, owner)
    org = owner[0] if same_org else make_org()
    subject = str(uuid.uuid4())
    existing = store.create_identity_binding(org.org_id, "auth0", subject, role="read_only")
    response = _accept(client, invitation["token"], subject)
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "already_member"
    assert store.resolve_active_identity_binding("auth0", subject) == existing
    assert store.active_identity_role(org.org_id, existing.binding_id) == "read_only"
    assert _row(invitation)["accepted_at"] is None


def test_expired_token_is_unavailable(client, owner):
    invitation = _create(client, owner)
    with store.cursor() as cur:
        cur.execute("UPDATE org_invitations SET expires_at = NOW() - INTERVAL '1 day' WHERE id = %s",
                    (uuid.UUID(invitation["invitation_id"]),))
    response = _accept(client, invitation["token"])
    assert response.status_code == 404
    assert response.json()["detail"] == "invitation_unavailable"
    assert _row(invitation)["accepted_at"] is None
    assert client.get(_url(owner), headers=owner[2]).json() == {"invitations": []}


def test_revoked_token_is_unavailable(client, owner):
    invitation = _create(client, owner)
    response = client.delete(f"{_url(owner)}/{invitation['invitation_id']}", headers=owner[2])
    assert response.status_code == 204, response.text
    assert not response.content
    row = _row(invitation)
    assert row["revoked_by_binding_id"] == owner[1].binding_id
    assert row["revoked_at"] is not None
    response = _accept(client, invitation["token"])
    assert response.status_code == 404
    assert response.json()["detail"] == "invitation_unavailable"
    assert client.get(_url(owner), headers=owner[2]).json() == {"invitations": []}


def test_unknown_token_and_invitation_are_unavailable(client, owner):
    response = _accept(client, secrets.token_urlsafe(32))
    assert response.status_code == 404
    assert response.json()["detail"] == "invitation_unavailable"
    response = client.delete(f"{_url(owner)}/{uuid.uuid4()}", headers=owner[2])
    assert response.status_code == 404


@pytest.mark.parametrize("token", ["", "a" * 42, "a" * 44, "a" * 42 + "="])
def test_malformed_token_returns_400(client, owner, token):
    response = _accept(client, token)
    assert response.status_code == 400, response.text


def test_twenty_pending_limit_and_revocation_releases_slot(client, owner):
    rows = [_create(client, owner) for _ in range(20)]
    response = client.post(_url(owner), headers=owner[2], json={})
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "too_many_pending"
    assert len(client.get(_url(owner), headers=owner[2]).json()["invitations"]) == 20
    assert client.delete(f"{_url(owner)}/{rows[0]['invitation_id']}", headers=owner[2]).status_code == 204
    _create(client, owner)


def test_receipts_record_created_accepted_and_revoked(client, owner):
    accepted = _create(client, owner)
    result = _accept(client, accepted["token"])
    assert result.status_code == 200
    revoked = _create(client, owner)
    for _ in range(2):
        assert client.delete(f"{_url(owner)}/{revoked['invitation_id']}", headers=owner[2]).status_code == 204
    with store.cursor() as cur:
        cur.execute("SELECT invitation_id, org_id, event, actor_binding_id FROM org_invitation_receipts "
                    "WHERE org_id = %s", (owner[0].org_id,))
        rows = cur.fetchall()
    assert len(rows) == 4
    assert {(str(row["invitation_id"]), row["event"], str(row["actor_binding_id"])) for row in rows} == {
        (accepted["invitation_id"], "created", str(owner[1].binding_id)),
        (accepted["invitation_id"], "accepted", result.json()["binding_id"]),
        (revoked["invitation_id"], "created", str(owner[1].binding_id)),
        (revoked["invitation_id"], "revoked", str(owner[1].binding_id)),
    }
    assert all(row["org_id"] == owner[0].org_id for row in rows)
    for sql in (
        "UPDATE org_invitation_receipts SET event = 'revoked' WHERE org_id = %s",
        "DELETE FROM org_invitation_receipts WHERE org_id = %s",
    ):
        with pytest.raises(RaiseException, match="append-only"):
            with store.cursor() as cur:
                cur.execute(sql, (owner[0].org_id,))


def test_accept_always_requires_verified_jwt(client, owner):
    invitation = _create(client, owner)
    for headers in ({}, owner[2], {"Authorization": "Bearer invalid"}):
        response = client.post("/api/invitations/accept", headers=headers, json={"token": invitation["token"]})
        assert response.status_code == 401
    assert _row(invitation)["accepted_at"] is None


def test_live_owner_scope_ignores_forged_headers(client, owner, make_org, monkeypatch):
    other = make_org()
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    headers = {"Authorization": f"Bearer test:{owner[1].external_subject}", "X-Org-Id": str(other.org_id),
               "X-Actor-Binding-Id": str(uuid.uuid4())}
    response = client.post(_url(owner), headers=headers, json={})
    assert response.status_code == 201, response.text
    assert client.get(f"/api/orgs/{other.org_id}/invitations", headers=headers).status_code == 404


def test_bodies_are_bounded_and_cannot_choose_role(client, owner):
    for body in ({"role": "owner"}, {"label": "x" * 81}, {"label": "line\nbreak"}):
        assert client.post(_url(owner), headers=owner[2], json=body).status_code == 422
    response = client.post(_url(owner), headers=owner[2], content=b" " * 1025)
    assert response.status_code == 413
    response = client.post("/api/invitations/accept", headers={"Authorization": "Bearer test:new"},
                           content=b" " * 1025)
    assert response.status_code == 413


def test_concurrent_accept_only_one_subject_gets_binding(client, owner):
    invitation = _create(client, owner)
    subjects = [str(uuid.uuid4()), str(uuid.uuid4())]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(invitations.accept, invitation["token"], "auth0", subject) for subject in subjects]
    results = []
    for future in futures:
        try:
            results.append(future.result())
        except invitations.InvitationUnavailable:
            results.append(None)
    assert sum(result is not None for result in results) == 1
    assert sum(store.resolve_active_identity_binding("auth0", subject) is not None for subject in subjects) == 1


def test_concurrent_different_invites_cannot_reuse_subject(client, owner):
    links = [_create(client, owner) for _ in range(2)]
    subject = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(invitations.accept, link["token"], "auth0", subject) for link in links]
    successes = 0
    for future in futures:
        try:
            future.result()
            successes += 1
        except invitations.InvitationConflict as exc:
            assert str(exc) == "already_member"
    assert successes == 1
    assert sum(_row(link)["accepted_at"] is not None for link in links) == 1


def test_failed_accept_receipt_rolls_back_binding_and_consumption(client, owner, monkeypatch):
    invitation = _create(client, owner)
    subject = str(uuid.uuid4())

    def fail_receipt(*args):
        raise RuntimeError("receipt unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(invitations, "_receipt", fail_receipt)
        with pytest.raises(RuntimeError, match="receipt unavailable"):
            invitations.accept(invitation["token"], "auth0", subject)
    assert store.resolve_active_identity_binding("auth0", subject) is None
    assert _row(invitation)["accepted_at"] is None
    assert _accept(client, invitation["token"], subject).status_code == 200
