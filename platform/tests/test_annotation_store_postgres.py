"""Real PostgreSQL proofs for P7 annotation CAS and atomicity.

Run with ``ANNOTATION_PG_URL`` set. The suite skips loudly when no server is
available because mocks cannot prove row locks or transaction rollback.
"""
from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path

import pytest

PG_URL = os.environ.get("ANNOTATION_PG_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="ANNOTATION_PG_URL is not set")
psycopg = pytest.importorskip("psycopg")
from leaf_platform import annotation_store, db  # noqa: E402

MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "0036_annotation_batches.sql"
BASE_DDL = """
CREATE TABLE orgs (
  org_id UUID PRIMARY KEY, name TEXT NOT NULL, tier TEXT NOT NULL DEFAULT 'hosted_starter',
  status TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE projects (
  project_id UUID PRIMARY KEY, org_id UUID NOT NULL REFERENCES orgs(org_id) ON DELETE CASCADE,
  name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
  UNIQUE (org_id, project_id)
);
CREATE TABLE drawing_artifacts (
  drawing_id UUID PRIMARY KEY, project_id UUID NOT NULL, org_id UUID NOT NULL,
  name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
  UNIQUE (drawing_id, project_id, org_id),
  FOREIGN KEY (org_id, project_id) REFERENCES projects(org_id, project_id) ON DELETE CASCADE
);
CREATE TABLE identity_bindings (
  binding_id UUID PRIMARY KEY, platform_tenant_id UUID NOT NULL REFERENCES orgs(org_id),
  role TEXT NOT NULL DEFAULT 'owner', status TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE app_sessions (
  session_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, drawing_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active'
);
"""


@pytest.fixture(scope="module")
def schema():
    name = f"annotation_p7_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {name}")
        conn.execute(f"SET search_path TO {name}")
        conn.execute(BASE_DDL)
        conn.execute(MIGRATION.read_text(encoding="utf-8"))
    yield name
    db.reset_pool()
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA {name} CASCADE")


@pytest.fixture(scope="module", autouse=True)
def configured_store(schema):
    separator = "&" if "?" in PG_URL else "?"
    scoped = f"{PG_URL}{separator}options=-csearch_path%3D{schema}"
    original = db.get_database_url
    db.reset_pool()
    db.get_database_url = lambda: scoped
    try:
        yield
    finally:
        db.reset_pool()
        db.get_database_url = original


@pytest.fixture()
def seed():
    tenant = uuid.uuid4()
    project = uuid.uuid4()
    drawing = uuid.uuid4()
    binding = uuid.uuid4()
    session = str(uuid.uuid4())
    with db.connection() as conn:
        conn.execute("INSERT INTO orgs (org_id, name) VALUES (%s, 'test')", (tenant,))
        conn.execute("INSERT INTO projects (project_id, org_id, name) VALUES (%s,%s,'p')",
                     (project, tenant))
        conn.execute("INSERT INTO drawing_artifacts (drawing_id, project_id, org_id, name) "
                     "VALUES (%s,%s,%s,'d')", (drawing, project, tenant))
        conn.execute("INSERT INTO identity_bindings "
                     "(binding_id, platform_tenant_id) VALUES (%s,%s)", (binding, tenant))
        conn.execute("INSERT INTO app_sessions (session_id, tenant_id, drawing_id) "
                     "VALUES (%s,%s,%s)", (session, str(tenant), str(drawing)))
    annotation_store.register_target(
        tenant_id=str(tenant), org_id=str(tenant), project_id=str(project),
        drawing_id=str(drawing), commit_sha="a" * 40, tree_sha="b" * 40,
        actor_binding_id=str(binding))
    return {"tenant": str(tenant), "project": str(project), "drawing": str(drawing),
            "binding": str(binding), "session": session}


def _create(seed, **changes):
    values = {
        "batch_id": str(uuid.uuid4()), "tenant_id": seed["tenant"],
        "org_id": seed["tenant"], "project_id": seed["project"],
        "drawing_id": seed["drawing"], "session_id": seed["session"],
        "kind": "apply", "base_version": 0, "base_commit": "a" * 40,
        "base_tree": "b" * 40, "preview_commit": "c" * 40,
        "preview_tree": "d" * 40, "payload_digest": "e" * 64,
        "payload_count": 2, "request_key": str(uuid.uuid4()),
        "created_by_binding_id": seed["binding"],
    }
    values.update(changes)
    return annotation_store.create_batch(**values)


def test_foreign_session_is_not_found_and_writes_nothing(seed):
    with pytest.raises(annotation_store.AnnotationStoreError) as caught:
        _create(seed, session_id="missing")
    assert caught.value.code == "annotation_not_found"
    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM annotation_batches WHERE tenant_id=%s",
                    (seed["tenant"],))
        assert cur.fetchone()["n"] == 0


def test_idempotent_create_returns_same_batch_and_changed_request_conflicts(seed):
    key = str(uuid.uuid4())
    batch_id = str(uuid.uuid4())
    first = _create(seed, request_key=key, batch_id=batch_id)
    again = _create(seed, request_key=key, batch_id=batch_id)
    assert again["batch_id"] == first["batch_id"]
    with pytest.raises(annotation_store.AnnotationStoreError) as caught:
        _create(seed, request_key=key, batch_id=batch_id, payload_count=3)
    assert caught.value.code == "idempotency_conflict"


def test_accept_advances_exact_head_and_writes_content_free_audit(seed):
    batch = _create(seed)
    accepted, head = annotation_store.accept(
        batch_id=batch["batch_id"], tenant_id=seed["tenant"],
        actor_binding_id=seed["binding"], decision_key="accept-key-1")
    assert accepted["state"] == "accepted"
    assert head["version"] == 1
    assert head["commit_sha"] == "c" * 40 and head["tree_sha"] == "d" * 40
    with db.cursor() as cur:
        cur.execute("SELECT * FROM annotation_audit WHERE batch_id = %s",
                    (batch["batch_id"],))
        audit = cur.fetchone()
    assert audit["payload_digest"] == "e" * 64 and audit["payload_count"] == 2
    assert "payload_json" not in audit and "content" not in audit

    # A later accepted batch may move the live head. Retrying this exact tap
    # still returns its original receipt, never the later batch's head.
    with db.connection() as conn:
        conn.execute("UPDATE annotation_targets SET version=2, commit_sha=%s, tree_sha=%s "
                     "WHERE tenant_id=%s", ("1" * 40, "2" * 40, seed["tenant"]))
    again, same_receipt = annotation_store.accept(
        batch_id=batch["batch_id"], tenant_id=seed["tenant"],
        actor_binding_id=seed["binding"], decision_key="accept-key-1")
    assert again == accepted and same_receipt == head

    other_actor = str(uuid.uuid4())
    with db.connection() as conn:
        conn.execute("INSERT INTO identity_bindings "
                     "(binding_id, platform_tenant_id) VALUES (%s,%s)",
                     (other_actor, seed["tenant"]))
    with pytest.raises(annotation_store.AnnotationStoreError) as caught:
        annotation_store.accept(
            batch_id=batch["batch_id"], tenant_id=seed["tenant"],
            actor_binding_id=other_actor, decision_key="accept-key-1")
    assert caught.value.code == "already_decided"


def test_stale_head_refuses_accept_without_partial_state(seed):
    batch = _create(seed)
    with db.connection() as conn:
        conn.execute("UPDATE annotation_targets SET version=1, commit_sha=%s, tree_sha=%s "
                     "WHERE tenant_id=%s", ("1" * 40, "2" * 40, seed["tenant"]))
    with pytest.raises(annotation_store.AnnotationStoreError) as caught:
        annotation_store.accept(
            batch_id=batch["batch_id"], tenant_id=seed["tenant"],
            actor_binding_id=seed["binding"], decision_key="accept-key-2")
    assert caught.value.code == "target_head_conflict"
    assert annotation_store.latest_batch(batch["batch_id"], seed["tenant"])["state"] == "pending"
    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM annotation_audit WHERE batch_id=%s",
                    (batch["batch_id"],))
        assert cur.fetchone()["n"] == 0


def test_audit_fault_rolls_back_target_and_batch(seed, monkeypatch):
    batch = _create(seed)
    monkeypatch.setattr(annotation_store, "_audit",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("fault")))
    with pytest.raises(RuntimeError, match="fault"):
        annotation_store.accept(
            batch_id=batch["batch_id"], tenant_id=seed["tenant"],
            actor_binding_id=seed["binding"], decision_key="accept-key-3")
    head = annotation_store.target(seed["tenant"], seed["tenant"],
                                   seed["project"], seed["drawing"])
    assert head["version"] == 0 and head["commit_sha"] == "a" * 40
    assert annotation_store.latest_batch(batch["batch_id"], seed["tenant"])["state"] == "pending"


def test_concurrent_same_decision_advances_once(seed):
    batch = _create(seed)
    barrier = threading.Barrier(2)
    outcomes = []

    def decide():
        barrier.wait(timeout=10)
        outcomes.append(annotation_store.accept(
            batch_id=batch["batch_id"], tenant_id=seed["tenant"],
            actor_binding_id=seed["binding"], decision_key="same-key-4"))

    threads = [threading.Thread(target=decide) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert len(outcomes) == 2
    assert {item[1]["version"] for item in outcomes} == {1}
    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM annotation_audit WHERE batch_id=%s",
                    (batch["batch_id"],))
        assert cur.fetchone()["n"] == 1


def test_expired_preview_is_retired_and_does_not_block_fresh_retry(seed):
    original = _create(seed)
    with db.connection() as conn:
        conn.execute("UPDATE annotation_batches SET lease_expires_at=NOW()-INTERVAL '1 second' "
                     "WHERE batch_id=%s AND revision=0", (original["batch_id"],))
    retry = _create(seed, batch_id=str(uuid.uuid4()), request_key=str(uuid.uuid4()),
                    retry_of_batch_id=original["batch_id"])
    assert retry["state"] == "pending"
    assert annotation_store.latest_batch(
        original["batch_id"], seed["tenant"])["state"] == "expired"


def test_retry_is_fresh_and_undo_refuses_then_accepts_only_exact_head(seed):
    original = _create(seed)
    annotation_store.reject(
        batch_id=original["batch_id"], tenant_id=seed["tenant"],
        actor_binding_id=seed["binding"], decision_key="reject-key-5")
    retry = _create(seed, batch_id=str(uuid.uuid4()), request_key=str(uuid.uuid4()),
                    retry_of_batch_id=original["batch_id"])
    assert retry["batch_id"] != original["batch_id"]
    accepted, _ = annotation_store.accept(
        batch_id=retry["batch_id"], tenant_id=seed["tenant"],
        actor_binding_id=seed["binding"], decision_key="accept-key-6")

    undo = _create(
        seed, batch_id=str(uuid.uuid4()), request_key=str(uuid.uuid4()), kind="undo",
        reverses_batch_id=accepted["batch_id"], base_version=1,
        base_commit="c" * 40, base_tree="d" * 40,
        preview_commit="a" * 40, preview_tree="b" * 40)
    with db.connection() as conn:
        conn.execute("UPDATE annotation_targets SET version=2, commit_sha=%s, tree_sha=%s "
                     "WHERE tenant_id=%s", ("1" * 40, "2" * 40, seed["tenant"]))
    with pytest.raises(annotation_store.AnnotationStoreError) as caught:
        annotation_store.accept(
            batch_id=undo["batch_id"], tenant_id=seed["tenant"],
            actor_binding_id=seed["binding"], decision_key="undo-key-7")
    assert caught.value.code == "target_head_conflict"
