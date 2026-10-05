"""Twelve real PostgreSQL checkout acceptance cases, including lock races."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest

from leaf_platform import db, project_graph_store as graph, store

T0 = datetime(2030, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def scope(make_org, monkeypatch):
    org = make_org("Synthetic checkout organization")
    project = store.create_project(org.org_id, "Synthetic checkout project", authority_mode="postgres_canonical")
    artifact = store.create_drawing_artifact(org.org_id, project.project_id, "Synthetic drawing")
    actors = [store.create_identity_binding(org.org_id, "auth0", "synthetic-" + str(uuid4()), role="editor")
              for _ in range(2)]
    with db.cursor() as cur:
        for actor in actors:
            cur.execute(
                "INSERT INTO project_member_bindings "
                "(membership_id, org_id, project_id, binding_id, role, invited_by_binding_id) "
                "VALUES (%s, %s, %s, %s, 'editor', %s)",
                (uuid4(), org.org_id, project.project_id, actor.binding_id, actor.binding_id),
            )
    result = SimpleNamespace(org=org.org_id, project=project.project_id, drawing=artifact.drawing_id,
                             a=actors[0].binding_id, b=actors[1].binding_id, now=T0)
    monkeypatch.setattr(graph, "_clock", lambda cur: result.now)
    return result


def acquire(scope, *, actor=None, ttl=60, expected=None, conn=None):
    operation = lambda c: graph.acquire_checkout(scope.org, scope.project, scope.drawing,
        actor_binding_id=actor or scope.a, holder="Synthetic editor", ttl_s=ttl,
        expected_fence=expected, conn=c)
    return operation(conn) if conn is not None else db.run_transaction(operation)


def release(scope, *, expected=None, actor=None, conn=None):
    operation = lambda c: graph.release_checkout(scope.org, scope.project, scope.drawing,
        actor_binding_id=actor or scope.a, expected_fence=expected, conn=c)
    return operation(conn) if conn is not None else db.run_transaction(operation)


def state(scope):
    return graph.get_checkout(scope.org, scope.project, scope.drawing)


def reason(code, operation):
    with pytest.raises(graph.ProjectContextError) as exc:
        operation()
    assert exc.value.reason_code == "SIP_R1_" + code


def seed_three(scope):
    acquire(scope)
    scope.now = T0 + timedelta(seconds=20)
    acquire(scope, ttl=120, expected=1)
    scope.now = T0 + timedelta(seconds=140)
    return acquire(scope, actor=scope.b)


def test_sip_r1_pg_first_acquire(scope):
    before = state(scope)
    assert before.checkout is None and before.last_fence == 0
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM project_drawing_checkouts WHERE org_id=%s", (scope.org,))
        assert cur.fetchone()["n"] == 0
    lease = acquire(scope)
    assert lease.fence == 1 and lease.acquired_at == T0
    assert lease.expires_at == T0 + timedelta(seconds=60)
    assert state(scope).checkout == lease


def test_sip_r1_pg_live_conflict(scope):
    prior = acquire(scope)
    scope.now = T0 + timedelta(seconds=10)
    reason("CHECKOUT_CONFLICT", lambda: acquire(scope, actor=scope.b))
    assert state(scope).checkout == prior
    assert prior.fence == 1 and prior.expires_at == T0 + timedelta(seconds=60)


def test_sip_r1_pg_renew(scope):
    acquire(scope)
    scope.now = T0 + timedelta(seconds=20)
    lease = acquire(scope, ttl=120, expected=1)
    assert lease.fence == 2 and lease.acquired_at == T0 + timedelta(seconds=20)
    assert lease.expires_at == T0 + timedelta(seconds=140)
    assert state(scope).checkout == lease


def test_sip_r1_pg_expiry_boundary(scope):
    lease = seed_three(scope)
    assert lease.holder_binding_id == scope.b and lease.fence == 3
    assert lease.acquired_at == T0 + timedelta(seconds=140)
    assert lease.expires_at == T0 + timedelta(seconds=200)


def test_sip_r1_pg_release_retains_fence(scope):
    seed_three(scope)
    assert release(scope, actor=scope.b, expected=3) is True
    released = state(scope)
    assert released.checkout is None and released.last_fence == 3
    assert release(scope) is False
    assert acquire(scope).fence == 4


def test_sip_r1_pg_stale_release_and_verify(scope):
    prior = seed_three(scope)
    reason("CHECKOUT_STALE", lambda: release(scope, actor=scope.b, expected=2))
    reason("CHECKOUT_STALE", lambda: graph.verify_checkout(scope.org, scope.project, scope.drawing,
        actor_binding_id=scope.b, expected_fence=2))
    assert state(scope).checkout == prior
    assert graph.verify_checkout(scope.org, scope.project, scope.drawing,
        actor_binding_id=scope.b, expected_fence=3) == prior


def test_sip_r1_pg_first_acquire_race(scope):
    barrier = Barrier(2)
    def attempt(actor):
        def operation(conn):
            barrier.wait(timeout=10)
            return acquire(scope, actor=actor, conn=conn)
        try:
            return db.run_transaction(operation, max_attempts=1)
        except graph.ProjectContextError as exc:
            return exc.reason_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, (scope.a, scope.b)))
    grants = [result for result in results if isinstance(result, graph.CheckoutLease)]
    assert len(grants) == 1 and grants[0].fence == 1
    assert results.count("SIP_R1_CHECKOUT_CONFLICT") == 1
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM project_drawing_checkouts WHERE org_id=%s", (scope.org,))
        assert cur.fetchone()["n"] == 1


def test_sip_r1_pg_renew_release_race(scope):
    for renewal_first in (True, False):
        if state(scope).checkout:
            release(scope, expected=state(scope).last_fence)
        initial = acquire(scope)
        fence = initial.fence
        locked, second_started = Event(), Event()
        def first():
            def operation(conn):
                value = acquire(scope, expected=fence, conn=conn) if renewal_first else release(scope, expected=fence, conn=conn)
                locked.set()
                assert second_started.wait(timeout=10)
                return value
            return db.run_transaction(operation, max_attempts=1)
        def second():
            assert locked.wait(timeout=10)
            second_started.set()
            try:
                return release(scope, expected=fence) if renewal_first else acquire(scope, expected=fence)
            except graph.ProjectContextError as exc:
                return exc.reason_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            one, two = pool.submit(first), pool.submit(second)
            first_result, second_result = one.result(timeout=20), two.result(timeout=20)
        assert second_result == "SIP_R1_CHECKOUT_STALE"
        if renewal_first:
            assert first_result.fence == fence + 1 and state(scope).checkout.fence == fence + 1
        else:
            assert first_result is True and state(scope).checkout is None
            assert state(scope).last_fence == fence


def test_sip_r1_pg_scope_and_role_recheck(scope, make_org):
    prior = acquire(scope)
    foreign_org = make_org("Synthetic foreign organization")
    foreign_project = store.create_project(foreign_org.org_id, "Foreign project", authority_mode="postgres_canonical")
    foreign_artifact = store.create_drawing_artifact(foreign_org.org_id, foreign_project.project_id, "Foreign drawing")
    reason("CONTEXT_NOT_FOUND", lambda: db.run_transaction(lambda conn: graph.acquire_checkout(
        scope.org, scope.project, foreign_artifact.drawing_id, actor_binding_id=scope.a,
        holder="Editor", ttl_s=60, expected_fence=None, conn=conn)))
    for deletion in ("deleted_at = clock_timestamp()", "status = 'deleted'"):
        with db.cursor() as cur:
            cur.execute("UPDATE projects SET " + deletion + " WHERE project_id=%s", (scope.project,))
        reason("CONTEXT_NOT_FOUND", lambda: acquire(scope, expected=1))
        with db.cursor() as cur:
            cur.execute("UPDATE projects SET deleted_at=NULL, status='active' WHERE project_id=%s", (scope.project,))
    with db.cursor() as cur:
        cur.execute("UPDATE project_member_bindings SET status='revoked', revoked_at=clock_timestamp() "
                    "WHERE org_id=%s AND binding_id=%s", (scope.org, scope.a))
    reason("PROJECT_FORBIDDEN", lambda: acquire(scope, expected=1))
    with db.cursor() as cur:
        cur.execute("UPDATE project_member_bindings SET status='active', revoked_at=NULL, role='read_only' "
                    "WHERE org_id=%s AND binding_id=%s", (scope.org, scope.a))
    reason("PROJECT_FORBIDDEN", lambda: release(scope, expected=1))
    with db.cursor() as cur:
        cur.execute("UPDATE project_member_bindings SET role='editor' WHERE org_id=%s AND binding_id=%s", (scope.org, scope.a))
    store.set_project_authority_mode(scope.org, scope.project, "legacy_sqlite")
    reason("CANONICAL_AUTHORITY_REQUIRED", lambda: acquire(scope, expected=1))
    store.set_project_authority_mode(scope.org, scope.project, "postgres_canonical")
    assert state(scope).checkout == prior


def test_sip_r1_pg_constraints_and_fence_guard(scope):
    prior = acquire(scope)
    with db.transaction() as conn:
        for assignments in (
            "holder_binding_id=NULL", "expires_at=acquired_at + INTERVAL '25 hours', fence=fence+1",
            "fence=0", "expires_at=expires_at + INTERVAL '1 second'",
            "org_id='00000000-0000-0000-0000-000000000099'",
            "holder='anonymous:unnamed-writer', fence=fence+1",
        ):
            with pytest.raises(psycopg.Error):
                with conn.transaction():
                    conn.execute("UPDATE project_drawing_checkouts SET " + assignments +
                                 " WHERE org_id=%s AND drawing_id=%s", (scope.org, scope.drawing))
        with pytest.raises(psycopg.Error):
            with conn.transaction():
                conn.execute("INSERT INTO project_drawing_checkouts (org_id,project_id,drawing_id) VALUES (%s,%s,%s)",
                             (scope.org, uuid4(), scope.drawing))
        with conn.cursor() as cur:
            cur.execute("SELECT conname, pg_get_constraintdef(oid, true) AS definition "
                        "FROM pg_constraint WHERE conrelid='project_drawing_checkouts'::regclass")
            constraints = cur.fetchall()
            assert len(constraints) == 6
            for row in constraints:
                contract = db._REQUIRED_CONSTRAINTS[row["conname"]]
                assert all(db._catalog_fragment_matches(fragment, row["definition"])
                           for fragment in contract["definition_fragments"]), row
        conn.execute("UPDATE project_drawing_checkouts SET holder=NULL, holder_binding_id=NULL, "
                     "acquired_at=NULL, expires_at=NULL WHERE org_id=%s", (scope.org,))
    assert state(scope).checkout is None and state(scope).last_fence == prior.fence


def test_sip_r1_pg_migration_idempotent(scope):
    prior = acquire(scope)
    sql = (Path(db.__file__).parent / "migrations/0073_project_drawing_checkouts.sql").read_text(encoding="utf-8")
    with db.transaction() as conn:
        for _ in range(2):
            for statement in db._split_sql_statements(sql):
                conn.execute(statement)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM pg_class WHERE oid='project_drawing_checkouts'::regclass")
            assert cur.fetchone()["n"] == 1
            cur.execute("SELECT tgenabled FROM pg_trigger WHERE tgname='project_drawing_checkouts_guard' "
                        "AND tgrelid='project_drawing_checkouts'::regclass")
            rows = cur.fetchall()
            assert len(rows) == 1 and rows[0]["tgenabled"] in {"O", "A"}
    assert state(scope).checkout == prior


def test_sip_r1_pg_fence_exhaustion(scope):
    with db.cursor() as cur:
        cur.execute("INSERT INTO project_drawing_checkouts (org_id,project_id,drawing_id,fence) "
                    "VALUES (%s,%s,%s,9223372036854775807)", (scope.org, scope.project, scope.drawing))
    reason("FENCE_EXHAUSTED", lambda: acquire(scope))
    final = state(scope)
    assert final.checkout is None and final.last_fence == 9223372036854775807
