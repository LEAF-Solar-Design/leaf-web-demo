"""Project editor byte publication against the real PostgreSQL authority."""
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest

from leaf_platform import db, project_graph_store as graph, store


@pytest.fixture
def editor_scope(make_org):
    org = make_org("Project editor publication")
    project = store.create_project(org.org_id, "Rooftop", authority_mode="postgres_canonical")
    parent = store.create_drawing_version(org.org_id, project.project_id, oss_object="original.dxf")
    actor = store.create_identity_binding(org.org_id, "auth0", str(uuid4()), role="editor")
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO project_member_bindings "
            "(membership_id, org_id, project_id, binding_id, role, invited_by_binding_id) "
            "VALUES (%s, %s, %s, %s, 'editor', %s)",
            (uuid4(), org.org_id, project.project_id, actor.binding_id, actor.binding_id))
    source = b"edited rooftop source bytes"
    digest = sha256(source).hexdigest()
    return SimpleNamespace(org=org.org_id, project=project.project_id, drawing=parent.drawing_id,
                           parent=parent, actor=actor.binding_id, request=uuid4(),
                           source=source, source_digest=digest, intake_digest="a" * 64)


def acquire(s):
    return db.run_transaction(lambda conn: graph.acquire_checkout(
        s.org, s.project, s.drawing, actor_binding_id=s.actor,
        holder="Browser project editor", ttl_s=60, expected_fence=None, conn=conn))


def publish(s, **changes):
    args = dict(expected_parent_version_id=s.parent.version_id, actor_binding_id=s.actor,
                expected_fence=1, request_id=s.request, intake_sha256=s.intake_digest,
                source_object_ref=graph.publication_source_key(s.org, s.project, s.drawing,
                                                               s.source_digest),
                source_sha256=s.source_digest)
    args.update(changes)
    return db.run_transaction(lambda conn: graph.publish_version(
        s.org, s.project, s.drawing, conn=conn, **args))


def count(s):
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM drawing_versions WHERE org_id=%s AND project_id=%s",
                    (s.org, s.project))
        return cur.fetchone()["n"]


def refused(code, operation):
    with pytest.raises(graph.ProjectContextError) as exc:
        operation()
    assert exc.value.reason_code == code


def test_editor_publication_keeps_source_intake_and_same_artifact(editor_scope):
    s = editor_scope
    acquire(s)
    immutable = {}
    source_key = graph.publication_source_key(s.org, s.project, s.drawing, s.source_digest)
    intake_key = graph.publication_intake_key(s.org, s.project, s.drawing, s.intake_digest)

    def durable_material():
        immutable[source_key] = s.source
        immutable[intake_key] = b"server-derived intake"

    version = publish(s, before_new=durable_material)
    assert version.drawing_id == s.drawing and version.project_id == s.project
    assert version.seq == 2 and version.version_id != s.parent.version_id
    assert version.oss_object == source_key and version.oss_object != s.parent.oss_object
    assert immutable[version.oss_object] == s.source
    assert version.intake_ref == intake_key
    assert version.provenance["source"] == {"ref": source_key, "sha256": s.source_digest}
    assert version.provenance["intake"] == {"ref": intake_key, "sha256": s.intake_digest}
    assert version.provenance["base_object_ref"] == source_key
    assert version.provenance["parent_version_id"] == str(s.parent.version_id)
    assert count(s) == 2


def test_editor_exact_replay_after_new_head_and_release(editor_scope):
    s = editor_scope
    acquire(s)
    first = publish(s)
    publish(s, expected_parent_version_id=first.version_id, request_id=uuid4())
    db.run_transaction(lambda conn: graph.release_checkout(
        s.org, s.project, s.drawing, actor_binding_id=s.actor, expected_fence=1, conn=conn))
    assert publish(s, before_new=lambda: pytest.fail("replay must not republish material")).to_dict() == first.to_dict()
    assert count(s) == 3


def test_editor_conflicting_source_replay_is_refused(editor_scope):
    s = editor_scope
    acquire(s)
    publish(s)
    other_digest = sha256(b"different edited bytes").hexdigest()
    refused("SIP_R2_IDEMPOTENCY_CONFLICT", lambda: publish(s,
        source_object_ref=graph.publication_source_key(s.org, s.project, s.drawing, other_digest),
        source_sha256=other_digest))
    refused("SIP_R2_IDEMPOTENCY_CONFLICT", lambda: publish(s, source_object_ref=None,
                                                         source_sha256=None))
    assert count(s) == 2


@pytest.mark.parametrize("changes", [
    {"source_sha256": None}, {"source_object_ref": None},
    {"source_sha256": "A" * 64}, {"source_sha256": "a" * 63},
    {"source_object_ref": "https://outside.invalid/source.dxf"},
    {"source_object_ref": False}, {"source_sha256": True},
])
def test_editor_unbound_source_is_refused_before_storage(editor_scope, changes):
    s = editor_scope
    acquire(s)
    refused("SIP_R2_PUBLICATION_PARAMS_INVALID", lambda: publish(s,
        before_new=lambda: pytest.fail("invalid source reached material storage"), **changes))
    assert count(s) == 1


def test_editor_foreign_artifact_source_reference_is_refused(editor_scope):
    s = editor_scope
    acquire(s)
    for org, project, drawing in ((uuid4(), s.project, s.drawing),
                                  (s.org, uuid4(), s.drawing),
                                  (s.org, s.project, uuid4())):
        refused("SIP_R2_PUBLICATION_PARAMS_INVALID", lambda: publish(s,
            source_object_ref=graph.publication_source_key(org, project, drawing, s.source_digest)))
    assert count(s) == 1


def test_editor_missing_stale_fence_and_stale_parent_publish_nothing(editor_scope):
    s = editor_scope
    refused("SIP_R1_CHECKOUT_REQUIRED", lambda: publish(s))
    acquire(s)
    refused("SIP_R1_CHECKOUT_STALE", lambda: publish(s, expected_fence=2))
    first = publish(s)
    refused("SIP_R1_STALE_VERSION", lambda: publish(s, request_id=uuid4()))
    assert count(s) == 2
    assert first.seq == 2


def test_editor_foreign_parent_is_refused(editor_scope):
    s = editor_scope
    acquire(s)
    other = store.create_project(s.org, "Other", authority_mode="postgres_canonical")
    parent = store.create_drawing_version(s.org, other.project_id, oss_object="other.dxf")
    refused("SIP_R1_CONTEXT_NOT_FOUND", lambda: publish(s, expected_parent_version_id=parent.version_id))
    assert count(s) == 1


def test_graph_only_publication_retains_exact_previous_proof(editor_scope):
    s = editor_scope
    acquire(s)
    version = publish(s, source_object_ref=None, source_sha256=None)
    assert version.oss_object == s.parent.oss_object
    assert version.provenance == graph._publication_proof(s.org, s.project, s.drawing,
        s.parent.version_id, s.actor, 1, s.request, s.intake_digest, s.parent.oss_object)
    assert "source" not in version.provenance
    refused("SIP_R2_IDEMPOTENCY_CONFLICT", lambda: publish(s))
    assert count(s) == 2
