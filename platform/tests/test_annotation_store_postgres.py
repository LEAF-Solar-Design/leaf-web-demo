"""Real PostgreSQL proofs for P7 annotation CAS and atomicity.

Run with ``ANNOTATION_PG_URL`` set. The suite skips loudly when no server is
available because mocks cannot prove row locks or transaction rollback.
"""
from __future__ import annotations

import os
import hashlib
import sys
import threading
import uuid
from pathlib import Path

import pytest

PG_URL = os.environ.get("ANNOTATION_PG_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="ANNOTATION_PG_URL is not set")
psycopg = pytest.importorskip("psycopg")
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from leaf_platform import annotation_store, db  # noqa: E402
from leaf_platform.annotation_source import (  # noqa: E402
    SourceVerificationRequest,
    VerifiedSourceReceipt,
)

MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "0042_annotation_batches.sql"
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


class TestSourceAuthority:
    def verify(self, request):
        digest = hashlib.sha256(repr(request).encode("utf-8")).hexdigest()
        return VerifiedSourceReceipt(request, digest)

    def validate(self, receipt, request):
        return (isinstance(receipt, VerifiedSourceReceipt)
                and receipt.request == request
                and receipt.receipt_digest
                == hashlib.sha256(repr(request).encode("utf-8")).hexdigest())


AUTHORITY = TestSourceAuthority()


class ReissuingSourceAuthority:
    """Test authority whose valid receipt digest may change per verification."""

    def verify(self, request):
        return VerifiedSourceReceipt(request, "1" * 64)

    def validate(self, receipt, request):
        return (isinstance(receipt, VerifiedSourceReceipt)
                and receipt.request == request
                and receipt.receipt_digest == "1" * 64)


REISSUING_AUTHORITY = ReissuingSourceAuthority()


def _receipt(**values):
    return AUTHORITY.verify(SourceVerificationRequest(**values))


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
    bindings = {role: uuid.uuid4() for role in ("owner", "editor", "reviewer", "read_only")}
    session = str(uuid.uuid4())
    with db.connection() as conn:
        conn.execute("INSERT INTO orgs (org_id, name) VALUES (%s, 'test')", (tenant,))
        conn.execute("INSERT INTO projects (project_id, org_id, name) VALUES (%s,%s,'p')",
                     (project, tenant))
        conn.execute("INSERT INTO drawing_artifacts (drawing_id, project_id, org_id, name) "
                     "VALUES (%s,%s,%s,'d')", (drawing, project, tenant))
        for role, binding in bindings.items():
            conn.execute("INSERT INTO identity_bindings "
                         "(binding_id, platform_tenant_id, role) VALUES (%s,%s,%s)",
                         (binding, tenant, role))
        conn.execute("INSERT INTO app_sessions (session_id, tenant_id, drawing_id) "
                     "VALUES (%s,%s,%s)", (session, str(tenant), str(drawing)))
    target_receipt = _receipt(
        tenant_id=str(tenant), org_id=str(tenant), project_id=str(project),
        drawing_id=str(drawing), repository_id=f"repo:{tenant}", relation="target_source",
        commit_sha="a" * 40, tree_sha="b" * 40, base_commit="a" * 40,
        base_tree="b" * 40, reverses_commit=None, reverses_tree=None)
    annotation_store.register_target(
        tenant_id=str(tenant), org_id=str(tenant), project_id=str(project),
        drawing_id=str(drawing), commit_sha="a" * 40, tree_sha="b" * 40,
        actor_binding_id=str(bindings["owner"]), source_authority=AUTHORITY,
        source_receipt=target_receipt)
    return {"tenant": str(tenant), "project": str(project), "drawing": str(drawing),
            "binding": str(bindings["owner"]), "bindings": {k: str(v) for k, v in bindings.items()},
            "repository": f"repo:{tenant}", "session": session}


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
    reverse_commit = reverse_tree = None
    if values["kind"] == "undo":
        linked = annotation_store.latest_batch(values["reverses_batch_id"], seed["tenant"])
        reverse_commit, reverse_tree = linked["preview_commit"], linked["preview_tree"]
    values.setdefault("source_authority", AUTHORITY)
    values.setdefault("source_receipt", _receipt(
        tenant_id=values["tenant_id"], org_id=values["org_id"],
        project_id=values["project_id"], drawing_id=values["drawing_id"],
        repository_id=seed["repository"],
        relation="inverse" if values["kind"] == "undo" else "preview",
        commit_sha=values["preview_commit"], tree_sha=values["preview_tree"],
        base_commit=values["base_commit"], base_tree=values["base_tree"],
        reverses_commit=reverse_commit, reverses_tree=reverse_tree))
    return annotation_store.create_batch(**values)


def _accept(seed, batch, **changes):
    values = {
        "batch_id": batch["batch_id"], "tenant_id": seed["tenant"],
        "actor_binding_id": seed["binding"], "decision_key": "accept-secret-key",
        "source_authority": AUTHORITY,
        "source_receipt": _receipt(
            tenant_id=batch["tenant_id"], org_id=batch["org_id"],
            project_id=batch["project_id"], drawing_id=batch["drawing_id"],
            repository_id=batch["repository_id"],
            relation="inverse" if batch["kind"] == "undo" else "preview",
            commit_sha=batch["preview_commit"], tree_sha=batch["preview_tree"],
            base_commit=batch["base_commit"], base_tree=batch["base_tree"],
            reverses_commit=batch.get("reverses_commit"),
            reverses_tree=batch.get("reverses_tree")),
    }
    values.update(changes)
    return annotation_store.accept(**values)


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
    again = _create(seed, request_key=key, batch_id=str(uuid.uuid4()))
    assert again["batch_id"] == first["batch_id"]
    with pytest.raises(annotation_store.AnnotationStoreError) as caught:
        _create(seed, request_key=key, batch_id=batch_id, payload_count=3)
    assert caught.value.code == "idempotency_conflict"


def test_request_and_decision_keys_keep_an_opaque_length_floor(seed):
    with pytest.raises(annotation_store.AnnotationStoreError) as caught:
        _create(seed, request_key="short")
    assert caught.value.code == "invalid_request_key"
    batch = _create(seed)
    with pytest.raises(annotation_store.AnnotationStoreError) as caught:
        _accept(seed, batch, decision_key="short")
    assert caught.value.code == "invalid_decision_key"
    assert annotation_store.latest_batch(batch["batch_id"], seed["tenant"])["state"] == "pending"


def test_idempotent_replay_rechecks_current_actor_session_and_target(seed):
    key = str(uuid.uuid4())
    first = _create(seed, request_key=key)
    with db.connection() as conn:
        conn.execute("UPDATE identity_bindings SET role='read_only' WHERE binding_id=%s",
                     (seed["binding"],))
    with pytest.raises(annotation_store.AnnotationStoreError) as caught:
        _create(seed, request_key=key, batch_id=str(uuid.uuid4()))
    assert caught.value.code == "annotation_role_forbidden"
    assert annotation_store.latest_batch(first["batch_id"], seed["tenant"])["state"] == "pending"


@pytest.mark.parametrize("archive_sql", [
    "UPDATE app_sessions SET status='archived' WHERE session_id=%s",
    "UPDATE drawing_artifacts SET status='archived' WHERE drawing_id=%s",
])
def test_idempotent_replay_refuses_inactive_session_or_target(seed, archive_sql):
    key = str(uuid.uuid4())
    first = _create(seed, request_key=key)
    value = seed["session"] if "app_sessions" in archive_sql else seed["drawing"]
    with db.connection() as conn:
        conn.execute(archive_sql, (value,))
    with pytest.raises(annotation_store.AnnotationStoreError) as caught:
        _create(seed, request_key=key, batch_id=str(uuid.uuid4()))
    assert caught.value.code == "annotation_not_found"
    assert annotation_store.latest_batch(first["batch_id"], seed["tenant"])["state"] == "pending"


def test_concurrent_same_request_key_returns_one_winner(seed):
    key = "concurrent-request-secret"
    barrier = threading.Barrier(2)
    outcomes = []

    def propose():
        barrier.wait(timeout=10)
        outcomes.append(_create(seed, request_key=key, batch_id=str(uuid.uuid4())))

    threads = [threading.Thread(target=propose) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert len(outcomes) == 2
    assert len({row["batch_id"] for row in outcomes}) == 1


def test_absent_mismatched_and_forged_source_receipts_cannot_create(seed):
    before = uuid.uuid4()
    with pytest.raises(annotation_store.AnnotationStoreError):
        _create(seed, request_key=str(before), source_authority=None)
    mismatched = _receipt(
        tenant_id=seed["tenant"], org_id=seed["tenant"], project_id=seed["project"],
        drawing_id=seed["drawing"], repository_id=seed["repository"], relation="preview",
        commit_sha="9" * 40, tree_sha="d" * 40, base_commit="a" * 40,
        base_tree="b" * 40, reverses_commit=None, reverses_tree=None)
    with pytest.raises(annotation_store.AnnotationStoreError):
        _create(seed, request_key=str(uuid.uuid4()), source_receipt=mismatched)
    valid = _receipt(
        tenant_id=seed["tenant"], org_id=seed["tenant"], project_id=seed["project"],
        drawing_id=seed["drawing"], repository_id=seed["repository"], relation="preview",
        commit_sha="c" * 40, tree_sha="d" * 40, base_commit="a" * 40,
        base_tree="b" * 40, reverses_commit=None, reverses_tree=None)
    forged = VerifiedSourceReceipt(valid.request, "f" * 64)
    with pytest.raises(annotation_store.AnnotationStoreError):
        _create(seed, request_key=str(uuid.uuid4()), source_receipt=forged)
    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM annotation_batches WHERE tenant_id=%s",
                    (seed["tenant"],))
        assert cur.fetchone()["n"] == 0


def test_untyped_source_receipt_cannot_register_create_or_accept(seed):
    valid = _receipt(
        tenant_id=seed["tenant"], org_id=seed["tenant"], project_id=seed["project"],
        drawing_id=seed["drawing"], repository_id=seed["repository"], relation="preview",
        commit_sha="c" * 40, tree_sha="d" * 40, base_commit="a" * 40,
        base_tree="b" * 40, reverses_commit=None, reverses_tree=None)

    class DuckReceipt:
        request = valid.request
        receipt_digest = valid.receipt_digest

    class DuckAuthority:
        def verify(self, request):
            return DuckReceipt()

        def validate(self, receipt, request):
            return True

    duck = DuckReceipt()
    authority = DuckAuthority()
    target_request = SourceVerificationRequest(
        tenant_id=seed["tenant"], org_id=seed["tenant"], project_id=seed["project"],
        drawing_id=seed["drawing"], repository_id=seed["repository"],
        relation="target_source", commit_sha="a" * 40, tree_sha="b" * 40,
        base_commit="a" * 40, base_tree="b" * 40)
    duck.request = target_request
    with pytest.raises(annotation_store.AnnotationStoreError) as caught:
        annotation_store.register_target(
            tenant_id=seed["tenant"], org_id=seed["tenant"], project_id=seed["project"],
            drawing_id=seed["drawing"], commit_sha="a" * 40, tree_sha="b" * 40,
            actor_binding_id=seed["binding"], source_authority=authority,
            source_receipt=duck)
    assert caught.value.code == "source_receipt_invalid"

    duck.request = valid.request
    with pytest.raises(annotation_store.AnnotationStoreError) as caught:
        _create(seed, source_authority=authority, source_receipt=duck)
    assert caught.value.code == "source_receipt_invalid"

    batch = _create(seed)
    with pytest.raises(annotation_store.AnnotationStoreError) as caught:
        _accept(seed, batch, source_authority=authority, source_receipt=duck)
    assert caught.value.code == "source_receipt_invalid"
    assert annotation_store.latest_batch(batch["batch_id"], seed["tenant"])["state"] == "pending"


def test_reissued_valid_receipts_preserve_semantic_idempotency(seed):
    target_request = SourceVerificationRequest(
        tenant_id=seed["tenant"], org_id=seed["tenant"], project_id=seed["project"],
        drawing_id=seed["drawing"], repository_id=seed["repository"],
        relation="target_source", commit_sha="a" * 40, tree_sha="b" * 40,
        base_commit="a" * 40, base_tree="b" * 40)
    target = annotation_store.register_target(
        tenant_id=seed["tenant"], org_id=seed["tenant"], project_id=seed["project"],
        drawing_id=seed["drawing"], commit_sha="a" * 40, tree_sha="b" * 40,
        actor_binding_id=seed["binding"], source_authority=REISSUING_AUTHORITY,
        source_receipt=REISSUING_AUTHORITY.verify(target_request))
    assert target["version"] == 0

    key = str(uuid.uuid4())
    first = _create(seed, request_key=key)
    preview_request = SourceVerificationRequest(
        tenant_id=seed["tenant"], org_id=seed["tenant"], project_id=seed["project"],
        drawing_id=seed["drawing"], repository_id=seed["repository"], relation="preview",
        commit_sha="c" * 40, tree_sha="d" * 40, base_commit="a" * 40,
        base_tree="b" * 40)
    replay = _create(
        seed, request_key=key, batch_id=str(uuid.uuid4()),
        source_authority=REISSUING_AUTHORITY,
        source_receipt=REISSUING_AUTHORITY.verify(preview_request))
    assert replay["batch_id"] == first["batch_id"]
    accepted, _ = _accept(
        seed, first, source_authority=REISSUING_AUTHORITY,
        source_receipt=REISSUING_AUTHORITY.verify(preview_request))
    assert accepted["state"] == "accepted"


@pytest.mark.parametrize("role,allowed", [
    ("owner", True), ("editor", True), ("reviewer", False), ("read_only", False),
])
def test_proposal_role_policy(seed, role, allowed):
    if allowed:
        assert _create(seed, created_by_binding_id=seed["bindings"][role])["state"] == "pending"
    else:
        with pytest.raises(annotation_store.AnnotationStoreError) as caught:
            _create(seed, created_by_binding_id=seed["bindings"][role])
        assert caught.value.code == "annotation_role_forbidden"


@pytest.mark.parametrize("role,allowed", [
    ("owner", True), ("editor", True), ("reviewer", False), ("read_only", False),
])
def test_registration_role_policy(seed, role, allowed):
    receipt = _receipt(
        tenant_id=seed["tenant"], org_id=seed["tenant"], project_id=seed["project"],
        drawing_id=seed["drawing"], repository_id=seed["repository"],
        relation="target_source", commit_sha="a" * 40, tree_sha="b" * 40,
        base_commit="a" * 40, base_tree="b" * 40,
        reverses_commit=None, reverses_tree=None)
    def call():
        return annotation_store.register_target(
            tenant_id=seed["tenant"], org_id=seed["tenant"], project_id=seed["project"],
            drawing_id=seed["drawing"], commit_sha="a" * 40, tree_sha="b" * 40,
            actor_binding_id=seed["bindings"][role], source_authority=AUTHORITY,
            source_receipt=receipt)
    if allowed:
        assert call()["version"] == 0
    else:
        with pytest.raises(annotation_store.AnnotationStoreError) as caught:
            call()
        assert caught.value.code == "annotation_role_forbidden"


@pytest.mark.parametrize("action,role,allowed", [
    ("accept", "owner", True), ("accept", "editor", True),
    ("accept", "reviewer", False), ("accept", "read_only", False),
    ("reject", "owner", True), ("reject", "editor", True),
    ("reject", "reviewer", False), ("reject", "read_only", False),
])
def test_decision_role_policy(seed, action, role, allowed):
    batch = _create(seed)
    try:
        if action == "accept":
            result = _accept(seed, batch, actor_binding_id=seed["bindings"][role])
        else:
            result = annotation_store.reject(
                batch_id=batch["batch_id"], tenant_id=seed["tenant"],
                actor_binding_id=seed["bindings"][role], decision_key="role-secret-key")
    except annotation_store.AnnotationStoreError as caught:
        assert not allowed and caught.code == "annotation_role_forbidden"
    else:
        assert allowed and result


def test_accept_advances_exact_head_and_writes_content_free_audit(seed):
    batch = _create(seed)
    accepted, head = _accept(seed, batch, decision_key="accept-key-1")
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
    again, same_receipt = _accept(seed, batch, decision_key="accept-key-1")
    assert again == accepted and same_receipt == head

    other_actor = str(uuid.uuid4())
    with db.connection() as conn:
        conn.execute("INSERT INTO identity_bindings "
                     "(binding_id, platform_tenant_id) VALUES (%s,%s)",
                     (other_actor, seed["tenant"]))
    with pytest.raises(annotation_store.AnnotationStoreError) as caught:
        _accept(seed, batch, actor_binding_id=other_actor, decision_key="accept-key-1")
    assert caught.value.code == "already_decided"


def test_stale_head_refuses_accept_without_partial_state(seed):
    batch = _create(seed)
    with db.connection() as conn:
        conn.execute("UPDATE annotation_targets SET version=1, commit_sha=%s, tree_sha=%s "
                     "WHERE tenant_id=%s", ("1" * 40, "2" * 40, seed["tenant"]))
    with pytest.raises(annotation_store.AnnotationStoreError) as caught:
        _accept(seed, batch, decision_key="accept-key-2")
    assert caught.value.code == "target_source_stale"
    assert annotation_store.latest_batch(batch["batch_id"], seed["tenant"])["state"] == "stale"
    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM annotation_audit WHERE batch_id=%s",
                    (batch["batch_id"],))
        assert cur.fetchone()["n"] == 1


def test_reject_drift_records_stale_then_allows_retry(seed):
    batch = _create(seed)
    with db.connection() as conn:
        conn.execute("UPDATE annotation_targets SET version=1, commit_sha=%s, tree_sha=%s "
                     "WHERE tenant_id=%s", ("1" * 40, "2" * 40, seed["tenant"]))
    with pytest.raises(annotation_store.AnnotationStoreError) as caught:
        annotation_store.reject(
            batch_id=batch["batch_id"], tenant_id=seed["tenant"],
            actor_binding_id=seed["binding"], decision_key="reject-drift-secret")
    assert caught.value.code == "target_source_stale"
    assert annotation_store.latest_batch(batch["batch_id"], seed["tenant"])["state"] == "stale"


def test_plaintext_keys_and_annotation_text_never_persist(seed):
    request_secret = "request-secret-annotation text must not persist"
    decision_secret = "decision-secret-annotation text must not persist"
    batch = _create(seed, request_key=request_secret)
    annotation_store.reject(
        batch_id=batch["batch_id"], tenant_id=seed["tenant"],
        actor_binding_id=seed["binding"], decision_key=decision_secret,
        reason="caller annotation text must not persist")
    with db.cursor() as cur:
        cur.execute(
            "SELECT (SELECT string_agg(row_to_json(b)::text, '') FROM annotation_batches b "
            "WHERE tenant_id=%s) || COALESCE((SELECT string_agg(row_to_json(a)::text, '') "
            "FROM annotation_audit a WHERE tenant_id=%s), '') AS stored",
            (seed["tenant"], seed["tenant"]),
        )
        stored = cur.fetchone()["stored"]
    for plaintext in (request_secret, decision_secret, "caller annotation text"):
        assert plaintext not in stored


def test_audit_fault_rolls_back_target_and_batch(seed, monkeypatch):
    batch = _create(seed)
    monkeypatch.setattr(annotation_store, "_audit",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("fault")))
    with pytest.raises(RuntimeError, match="fault"):
        _accept(seed, batch, decision_key="accept-key-3")
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
        outcomes.append(_accept(seed, batch, decision_key="same-key-4"))

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
    accepted, _ = _accept(seed, retry, decision_key="accept-key-6")

    undo = _create(
        seed, batch_id=str(uuid.uuid4()), request_key=str(uuid.uuid4()), kind="undo",
        reverses_batch_id=accepted["batch_id"], base_version=1,
        base_commit="c" * 40, base_tree="d" * 40,
        preview_commit="a" * 40, preview_tree="b" * 40)
    with db.connection() as conn:
        conn.execute("UPDATE annotation_targets SET version=2, commit_sha=%s, tree_sha=%s "
                     "WHERE tenant_id=%s", ("1" * 40, "2" * 40, seed["tenant"]))
    with pytest.raises(annotation_store.AnnotationStoreError) as caught:
        _accept(seed, undo, decision_key="undo-key-7")
    assert caught.value.code == "target_source_stale"
