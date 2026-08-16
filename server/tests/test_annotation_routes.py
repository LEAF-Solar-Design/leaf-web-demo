from __future__ import annotations

import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

SERVER = Path(__file__).resolve().parents[1]
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import deps  # noqa: E402
from routers import overlay  # noqa: E402

TENANT = str(uuid.uuid4())
PROJECT = str(uuid.uuid4())
DRAWING = str(uuid.uuid4())
REPO = str(uuid.uuid4())
BINDING = str(uuid.uuid4())
SESSION = str(uuid.uuid4())
BATCH = str(uuid.uuid4())
BASE = "1" * 40
BASE_TREE = "2" * 40
HEAD = "3" * 40
HEAD_TREE = "4" * 40


class Tenant(str):
    subject = "auth0|actor"


class FakeStore:
    def __init__(self):
        self.calls = []
        self.target_row = {
            "version": 0, "repository_id": REPO,
            "commit_sha": BASE, "tree_sha": BASE_TREE,
        }
        self.batch = {
            "batch_id": BATCH, "revision": 0, "tenant_id": TENANT,
            "org_id": TENANT, "project_id": PROJECT, "drawing_id": DRAWING,
            "session_id": SESSION, "kind": "apply", "state": "pending",
            "repository_id": REPO, "base_version": 0, "base_commit": BASE,
            "base_tree": BASE_TREE, "preview_commit": HEAD,
            "preview_tree": HEAD_TREE, "reverses_commit": None,
            "reverses_tree": None, "source_receipt_digest": "5" * 64,
            "payload_digest": "6" * 64,
        }

    def target(self, *_):
        return dict(self.target_row)

    def latest_batch(self, *_):
        return dict(self.batch)

    def create_batch(self, **values):
        self.calls.append(("create", values))
        row = dict(self.batch)
        row.update({"batch_id": values["batch_id"], "kind": values["kind"]})
        return row

    def accept(self, **values):
        self.calls.append(("accept", values))
        row = dict(self.batch, state="accepted", revision=1)
        return row, dict(self.target_row, version=1, commit_sha=HEAD, tree_sha=HEAD_TREE)

    def reject(self, **values):
        self.calls.append(("reject", values))
        return dict(self.batch, state="rejected", revision=1)


@pytest.fixture()
def client(monkeypatch):
    store = FakeStore()
    scope = {
        "tenant_id": TENANT, "org_id": TENANT, "project_id": PROJECT,
        "drawing_id": DRAWING, "session_id": SESSION,
        "actor_binding_id": BINDING, "repository_id": REPO,
    }
    monkeypatch.setattr(overlay, "_annotation_store", lambda: store)
    monkeypatch.setattr(overlay, "_annotation_scope", lambda session, tenant: (
        dict(scope) if session == SESSION and str(tenant) == TENANT
        else (_ for _ in ()).throw(LookupError())
    ))
    verified = []
    monkeypatch.setattr(
        overlay, "_source_receipt",
        lambda *args, **kwargs: verified.append(kwargs) or object(),
    )
    events = []
    monkeypatch.setattr(
        overlay.session_store, "append_event",
        lambda session, turn, kind, data: events.append((session, kind, data)) or 1,
    )
    app = FastAPI()
    app.include_router(overlay.router)
    app.dependency_overrides[deps.require_active_tenant] = lambda: Tenant(TENANT)
    return TestClient(app), store, verified, events


def preview_body(**changes):
    body = {
        "session_id": SESSION, "batch_id": BATCH, "base_version": 0,
        "base_commit": BASE, "base_tree": BASE_TREE,
        "preview_commit": HEAD, "preview_tree": HEAD_TREE,
        "payload_digest": "6" * 64, "payload_count": 2,
        "request_key": "request-key-1",
    }
    body.update(changes)
    return body


def linked_body(**changes):
    body = {
        "batch_id": str(uuid.uuid4()), "preview_commit": "7" * 40,
        "preview_tree": "8" * 40, "payload_digest": "9" * 64,
        "payload_count": 1, "request_key": "linked-key-1",
    }
    body.update(changes)
    return body


def test_all_five_annotation_actions_are_registered(client):
    paths = {route.path for route in overlay.router.routes}
    for suffix in ("preview", "{batch_id}/accept", "{batch_id}/reject",
                   "{batch_id}/retry", "{batch_id}/undo"):
        assert f"/api/overlay/annotations/{suffix}" in paths


def test_preview_verifies_source_before_store_and_appends_hash_only_event(client):
    c, store, verified, events = client
    response = c.post("/api/overlay/annotations/preview", json=preview_body())
    assert response.status_code == 200
    assert verified[0]["relation"] == "preview"
    assert store.calls[0][0] == "create"
    assert events[0][1] == "annotation_previewed"
    assert set(events[0][2]) == {
        "batch_id", "revision", "state", "source_receipt_digest",
        "payload_digest", "preview_commit", "preview_tree",
    }


@pytest.mark.parametrize("action,store_call", [("accept", "accept"), ("reject", "reject")])
def test_decisions_reverify_git_before_exact_store_mutation(client, action, store_call):
    c, store, verified, _ = client
    response = c.post(
        f"/api/overlay/annotations/{BATCH}/{action}",
        json={"decision_key": "decision-key"},
    )
    assert response.status_code == 200
    assert verified and store.calls[0][0] == store_call
    values = store.calls[0][1]
    assert values["tenant_id"] == TENANT
    assert values["actor_binding_id"] == BINDING


def test_retry_uses_fresh_identity_and_link(client):
    c, store, _, _ = client
    fresh = str(uuid.uuid4())
    response = c.post(
        f"/api/overlay/annotations/{BATCH}/retry",
        json=linked_body(batch_id=fresh),
    )
    assert response.status_code == 200
    values = store.calls[0][1]
    assert values["batch_id"] == fresh and values["retry_of_batch_id"] == BATCH
    assert values["reverses_batch_id"] is None


def test_undo_requires_inverse_and_links_accepted_batch(client):
    c, store, verified, _ = client
    store.batch["state"] = "accepted"
    store.batch["applied_version"] = 0
    response = c.post(
        f"/api/overlay/annotations/{BATCH}/undo", json=linked_body(),
    )
    assert response.status_code == 200
    assert verified[0]["relation"] == "inverse"
    values = store.calls[0][1]
    assert values["kind"] == "undo" and values["reverses_batch_id"] == BATCH


def test_retry_cannot_reuse_prior_batch_identity(client):
    c, store, *_ = client
    response = c.post(
        f"/api/overlay/annotations/{BATCH}/retry",
        json=linked_body(batch_id=BATCH),
    )
    assert response.status_code == 404 and store.calls == []


@pytest.mark.parametrize("defect", ["foreign", "authority", "store"])
def test_unknown_foreign_and_failed_authority_are_same_closed_shape(client, monkeypatch, defect):
    c, store, *_ = client
    if defect == "foreign":
        monkeypatch.setattr(overlay, "_batch_scope", lambda *_: (_ for _ in ()).throw(LookupError()))
    elif defect == "authority":
        monkeypatch.setattr(overlay, "_source_receipt", lambda *_a, **_k: (_ for _ in ()).throw(OSError()))
    else:
        store.accept = lambda **_: (_ for _ in ()).throw(RuntimeError())
    response = c.post(
        f"/api/overlay/annotations/{BATCH}/accept",
        json={"decision_key": "decision-key"},
    )
    assert response.status_code in {404, 503}
    assert response.json() == {"detail": "annotation unavailable"}


def test_foreign_project_role_is_indistinguishable_from_unknown(client, monkeypatch):
    c, store, *_ = client
    monkeypatch.setattr(
        overlay, "_batch_scope",
        lambda *_: (_ for _ in ()).throw(overlay.platform_link.ProjectSessionForbidden()),
    )
    response = c.post(
        f"/api/overlay/annotations/{BATCH}/accept",
        json={"decision_key": "decision-key"},
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "annotation unavailable"}
    assert store.calls == []


def test_extra_caller_authority_cannot_override_server_scope(client):
    c, store, *_ = client
    response = c.post(
        "/api/overlay/annotations/preview",
        json=preview_body(repository_id=str(uuid.uuid4()), actor_binding_id=str(uuid.uuid4())),
    )
    assert response.status_code == 422 and store.calls == []


def test_scope_derives_actor_project_and_repository_from_server(monkeypatch):
    tenant = Tenant(TENANT)
    monkeypatch.setattr(
        overlay, "_require_project_session",
        lambda session, caller, write: {
            "session_id": SESSION, "tenant_id": TENANT, "org_id": TENANT,
            "project_id": PROJECT, "drawing_id": DRAWING,
        },
    )
    platform_store = SimpleNamespace(
        resolve_active_identity_binding=lambda authority, subject: SimpleNamespace(
            platform_tenant_id=TENANT, binding_id=BINDING,
        ),
        resolve_project_repository_authority=lambda tenant, org, project: {
            "repo_key": REPO,
        },
    )
    monkeypatch.setattr(overlay.platform_link, "platform_store", lambda: platform_store)
    scope = overlay._annotation_scope(SESSION, tenant)
    assert scope == {
        "tenant_id": TENANT, "org_id": TENANT, "project_id": PROJECT,
        "drawing_id": DRAWING, "session_id": SESSION,
        "actor_binding_id": BINDING, "repository_id": REPO,
    }
