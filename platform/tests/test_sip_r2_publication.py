"""Sixteen PostgreSQL acceptance rows for fenced canonical publication."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from leaf_platform import db, project_graph_store as graph, store


@pytest.fixture
def scope(make_org):
    org = make_org("Publication acceptance")
    project = store.create_project(org.org_id, "Publication", authority_mode="postgres_canonical")
    parent = store.create_drawing_version(org.org_id, project.project_id, oss_object="base.dwg")
    actors = [store.create_identity_binding(org.org_id, "auth0", str(uuid4()), role="editor")
              for _ in range(2)]
    with db.cursor() as cur:
        for actor in actors:
            cur.execute(
                "INSERT INTO project_member_bindings "
                "(membership_id, org_id, project_id, binding_id, role, invited_by_binding_id) "
                "VALUES (%s, %s, %s, %s, 'editor', %s)",
                (uuid4(), org.org_id, project.project_id, actor.binding_id, actor.binding_id))
    return SimpleNamespace(org=org.org_id, project=project.project_id, drawing=parent.drawing_id,
                           parent=parent, actor=actors[0].binding_id, other=actors[1].binding_id,
                           request=uuid4(), digest="a" * 64, fence=1)


def acquire(s, *, expected=None, ttl=60, conn=None):
    operation = lambda c: graph.acquire_checkout(s.org, s.project, s.drawing,
        actor_binding_id=s.actor, holder="Publication editor", ttl_s=ttl,
        expected_fence=expected, conn=c)
    return operation(conn) if conn is not None else db.run_transaction(operation)


def publish(s, *, conn=None, **changes):
    args = dict(expected_parent_version_id=s.parent.version_id, actor_binding_id=s.actor,
                expected_fence=s.fence, request_id=s.request, intake_sha256=s.digest)
    args.update(changes)
    operation = lambda c: graph.publish_version(s.org, s.project, s.drawing, conn=c, **args)
    return operation(conn) if conn is not None else db.run_transaction(operation)


def refused(code, operation):
    with pytest.raises(graph.ProjectContextError) as exc:
        operation()
    assert exc.value.reason_code == code


def count(s):
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM drawing_versions WHERE org_id=%s AND project_id=%s",
                    (s.org, s.project))
        return cur.fetchone()["n"]


def tombstone(s, version):
    with db.cursor() as cur:
        cur.execute("UPDATE drawing_versions SET deleted_at=clock_timestamp() "
                    "WHERE org_id=%s AND version_id=%s", (s.org, version.version_id))


def overlap(s, *, replay):
    """Keep the winning transaction open until a second real connection enters."""
    inserted, entering = Event(), Event()
    connections = []
    pid_box = []

    def first(conn):
        connections.append(conn)
        result = publish(s, conn=conn)
        inserted.set()
        assert entering.wait(10)
        deadline = time.monotonic() + 10
        while True:
            with conn.cursor() as cur:
                cur.execute("SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = %s AND NOT granted) AS waiting",
                            (pid_box[0],))
                if cur.fetchone()["waiting"]:
                    break
            assert time.monotonic() < deadline, "second publisher never waited on the artifact lock"
            time.sleep(0.05)
        return result

    def second(conn):
        connections.append(conn)
        assert inserted.wait(10)
        with conn.cursor() as cur:
            cur.execute("SELECT pg_backend_pid() AS pid")
            pid_box.append(cur.fetchone()["pid"])
        entering.set()
        return publish(s, conn=conn, request_id=s.request if replay else uuid4())

    def run(operation):
        try:
            return db.run_transaction(operation, max_attempts=1)
        except graph.ProjectContextError as exc:
            return exc.reason_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        one, two = pool.submit(run, first), pool.submit(run, second)
        results = one.result(timeout=20), two.result(timeout=20)
    assert len(connections) == 2 and connections[0] is not connections[1]
    return results


def test_sip_r2_publish_once(scope):
    acquire(scope)
    for fence in (True, 0, 9223372036854775808):
        refused("SIP_R1_CHECKOUT_PARAMS_INVALID", lambda: publish(scope, expected_fence=fence))
    for changes in ({"request_id": "bad"}, {"intake_sha256": "A" * 64}):
        refused("SIP_R2_PUBLICATION_PARAMS_INVALID", lambda: publish(scope, **changes))
    version = publish(scope)
    assert version.seq == 2 and version.version_id != scope.parent.version_id
    assert version.oss_object == scope.parent.oss_object
    assert version.provenance == graph._publication_proof(scope.org, scope.project, scope.drawing,
        scope.parent.version_id, scope.actor, 1, scope.request, scope.digest, scope.parent.oss_object)
    assert version.idempotency_key == "sip-r2:" + str(scope.request)
    assert count(scope) == 2


def test_sip_r2_stale_parent(scope):
    acquire(scope)
    publish(scope)
    refused("SIP_R1_STALE_VERSION", lambda: publish(scope, request_id=uuid4()))
    refused("SIP_R1_CONTEXT_NOT_FOUND", lambda: publish(scope, expected_parent_version_id=uuid4(), request_id=uuid4()))
    assert count(scope) == 2


def test_sip_r2_expired_fence(scope, monkeypatch):
    clock = graph._clock
    lease = acquire(scope)
    with monkeypatch.context() as patch:
        patch.setattr(graph, "_clock", lambda cur: lease.expires_at)
        refused("SIP_R1_CHECKOUT_EXPIRED", lambda: publish(scope))
    # Give a new generation a valid historical interval, already expired in DB time.
    with monkeypatch.context() as patch:
        patch.setattr(graph, "_clock", lambda cur: clock(cur) - timedelta(minutes=2))
        expired = acquire(scope, expected=1)
    scope.fence = expired.fence
    observations = []

    def before_then_after(cur):
        observations.append(True)
        return expired.acquired_at if len(observations) == 1 else clock(cur)

    with monkeypatch.context() as patch:
        patch.setattr(graph, "_clock", before_then_after)
        refused("SIP_R1_CHECKOUT_EXPIRED", lambda: publish(scope))
    assert len(observations) == 2  # verifier passed; INSERT's DB-clock predicate refused.
    # The waiter starts while another transaction holds the artifact lock.
    locked, entering = Event(), Event()

    def hold(conn):
        live = acquire(scope, expected=scope.fence, ttl=1, conn=conn)
        scope.fence = live.fence
        locked.set()
        assert entering.wait(10)
        # Expire the new lease while the waiter is behind our artifact lock.
        conn.execute("SELECT pg_sleep(GREATEST(0, EXTRACT(EPOCH FROM "
                     "(%s::timestamptz - clock_timestamp()))))", (live.expires_at,))

    def wait(conn):
        assert locked.wait(10)
        entering.set()
        return publish(scope, conn=conn)

    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(db.run_transaction, hold)
        two = pool.submit(db.run_transaction, wait)
        one.result(timeout=20)
        refused("SIP_R1_CHECKOUT_EXPIRED", lambda: two.result(timeout=20))
    assert count(scope) == 1


def test_sip_r2_foreign_holder(scope):
    acquire(scope)
    refused("SIP_R1_CHECKOUT_DENIED", lambda: publish(scope, actor_binding_id=scope.other))
    assert count(scope) == 1


def test_sip_r2_stale_generation(scope):
    acquire(scope)
    assert acquire(scope, expected=1).fence == 2
    refused("SIP_R1_CHECKOUT_STALE", lambda: publish(scope))
    assert count(scope) == 1


def test_sip_r2_missing_checkout(scope):
    refused("SIP_R1_CHECKOUT_REQUIRED", lambda: publish(scope))
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM project_drawing_checkouts WHERE org_id=%s", (scope.org,))
        assert cur.fetchone()["n"] == 0
    assert count(scope) == 1


def test_sip_r2_competing_publisher(scope):
    acquire(scope)
    first, second = overlap(scope, replay=False)
    assert first.seq == 2 and second == "SIP_R1_STALE_VERSION"
    assert count(scope) == 2


def test_sip_r2_exact_concurrent_replay(scope):
    acquire(scope)
    first, second = overlap(scope, replay=True)
    assert first.to_dict() == second.to_dict()
    assert count(scope) == 2


def test_sip_r2_replay_after_expiry(scope, monkeypatch):
    lease = acquire(scope)
    first = publish(scope)
    publish(scope, expected_parent_version_id=first.version_id, request_id=uuid4())
    monkeypatch.setattr(graph, "_clock", lambda cur: lease.expires_at)
    assert publish(scope).to_dict() == first.to_dict()
    assert count(scope) == 3


def test_sip_r2_key_reuse_with_changed_input(scope):
    acquire(scope)
    first = publish(scope)
    for changes in ({"intake_sha256": "b" * 64}, {"actor_binding_id": scope.other},
                    {"expected_fence": 2}, {"expected_parent_version_id": first.version_id}):
        refused("SIP_R2_IDEMPOTENCY_CONFLICT", lambda: publish(scope, **changes))
    artifact = store.create_drawing_artifact(scope.org, scope.project, "Other artifact")
    drawing = scope.drawing
    scope.drawing = artifact.drawing_id
    refused("SIP_R2_IDEMPOTENCY_CONFLICT", lambda: publish(scope))
    scope.drawing = drawing
    assert publish(scope).to_dict() == first.to_dict() and count(scope) == 2


def test_sip_r2_missing_project(scope):
    project = scope.project
    scope.project = uuid4()
    refused("SIP_R1_CONTEXT_NOT_FOUND", lambda: publish(scope))
    with pytest.raises(ValueError) as exc:
        store.create_drawing_version(scope.org, scope.project)
    assert type(exc.value).__name__ == "ProjectNotFound" and exc.value.reason == "not_found"
    scope.project = project
    assert count(scope) == 1


def test_sip_r2_deleted_drawing_version(scope):
    acquire(scope)
    first = publish(scope)
    tombstone(scope, first)
    refused("SIP_R1_CONTEXT_NOT_FOUND", lambda: publish(scope))
    refused("SIP_R1_CONTEXT_NOT_FOUND", lambda: publish(scope,
        expected_parent_version_id=first.version_id, request_id=uuid4()))
    with db.cursor() as cur:
        cur.execute("UPDATE drawing_artifacts SET status='deleted' WHERE org_id=%s AND drawing_id=%s",
                    (scope.org, scope.drawing))
    refused("SIP_R1_CONTEXT_NOT_FOUND", lambda: publish(scope, request_id=uuid4()))
    assert count(scope) == 2


def test_sip_r2_revoked_access_authority(scope):
    acquire(scope)
    publish(scope)
    with db.cursor() as cur:
        cur.execute("UPDATE project_member_bindings SET status='revoked', revoked_at=clock_timestamp() "
                    "WHERE org_id=%s AND binding_id=%s", (scope.org, scope.actor))
    refused("SIP_R1_PROJECT_FORBIDDEN", lambda: publish(scope))
    with db.cursor() as cur:
        cur.execute("UPDATE project_member_bindings SET status='active', revoked_at=NULL "
                    "WHERE org_id=%s AND binding_id=%s", (scope.org, scope.actor))
        cur.execute("UPDATE identity_bindings SET status='revoked', revoked_at=clock_timestamp() "
                    "WHERE platform_tenant_id=%s AND binding_id=%s", (scope.org, scope.actor))
    refused("SIP_R1_PROJECT_FORBIDDEN", lambda: publish(scope))
    store.set_project_authority_mode(scope.org, scope.project, "legacy_sqlite")
    refused("SIP_R1_CANONICAL_AUTHORITY_REQUIRED", lambda: publish(scope))
    assert count(scope) == 2


def test_sip_r2_rollback(scope):
    acquire(scope)

    def abort(conn):
        assert publish(scope, conn=conn).seq == 2
        raise RuntimeError("abort after insert")

    with pytest.raises(RuntimeError, match="abort after insert"):
        db.run_transaction(abort)
    assert count(scope) == 1
    assert publish(scope).seq == 2 and count(scope) == 2


def test_sip_r2_bootstrap_bypass(scope):
    with pytest.raises(ValueError, match="SIP_R2_PUBLICATION_REQUIRED"):
        store.create_drawing_version(scope.org, scope.project, drawing_id=scope.drawing)
    tombstone(scope, scope.parent)
    with pytest.raises(ValueError, match="SIP_R2_PUBLICATION_REQUIRED"):
        store.create_drawing_version(scope.org, scope.project, drawing_id=scope.drawing)
    from test_drawing_import import _ready_upload
    source, *_ = _ready_upload(scope.org, version=7)
    args = dict(source_drawing_id=source, source_version=7, name="Imported",
                idempotency_key="bootstrap", actor_binding_id=scope.actor)
    imported, replay = store.import_ready_account_upload(scope.org, scope.project, **args)
    assert imported.seq == 7 and not replay
    same, replay = store.import_ready_account_upload(scope.org, scope.project, **args)
    assert replay and same.to_dict() == imported.to_dict()
    args["idempotency_key"] = "another-request"
    with pytest.raises(store.DrawingImportConflict, match="SIP_R2_PUBLICATION_REQUIRED"):
        store.import_ready_account_upload(scope.org, scope.project, **args)
    assert count(scope) == 2


def test_sip_r2_deleted_higher_sequence(scope):
    acquire(scope)
    higher = publish(scope)
    tombstone(scope, higher)
    successor = publish(scope, request_id=uuid4())
    assert successor.seq == 3
    assert successor.provenance["parent_version_id"] == str(scope.parent.version_id)
    assert count(scope) == 3


def test_sip_r2_before_new_runs_only_for_new_publication(scope):
    acquire(scope)
    calls = []
    first = publish(scope, before_new=lambda: calls.append("new"))
    assert calls == ["new"] and first.seq == 2

    def unavailable():
        calls.append("unavailable")
        raise graph.ProjectContextError("SIP_R1_INTAKE_UNAVAILABLE")

    assert publish(scope, before_new=unavailable).to_dict() == first.to_dict()
    assert calls == ["new"]
    refused("SIP_R2_IDEMPOTENCY_CONFLICT", lambda: publish(scope,
        intake_sha256="b" * 64, before_new=unavailable))
    assert calls == ["new"]
    before = count(scope)
    refused("SIP_R1_INTAKE_UNAVAILABLE", lambda: publish(scope,
        request_id=uuid4(), before_new=unavailable))
    assert calls == ["new", "unavailable"] and count(scope) == before
