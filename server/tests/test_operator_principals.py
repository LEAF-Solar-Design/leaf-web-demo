"""Lane A gate: operator principals, sessions, and the request dependency.

Structural tests always run. Live-PG tests key on
LEAF_OPERATOR_TEST_DATABASE_URL (a disposable database that already ran
platform/migrations/0032_operator_control_plane.sql) and skip cleanly
without it — the repo's database-free test-environment idiom.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

PG_URL = os.environ.get("LEAF_OPERATOR_TEST_DATABASE_URL")
needs_pg = pytest.mark.skipif(
    not PG_URL, reason="LEAF_OPERATOR_TEST_DATABASE_URL not set")


@pytest.fixture()
def pg_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", PG_URL)
    import operator_principals
    db = operator_principals._db()
    db.reset_pool()
    yield
    db.reset_pool()


def _grant(subject: str, profiles: str = "default",
           environment: str = "staging") -> None:
    import subprocess
    env = dict(os.environ, DATABASE_URL=PG_URL)
    proc = subprocess.run(
        [sys.executable, str(SERVER_DIR.parent / "scripts" /
                             "operator_principal_admin.py"),
         "grant", subject, "--granted-by", "test-harness", "--profiles", profiles,
         "--environment", environment],
        capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr


def _admin(command: str, subject: str) -> None:
    import subprocess
    env = dict(os.environ, DATABASE_URL=PG_URL)
    proc = subprocess.run(
        [sys.executable, str(SERVER_DIR.parent / "scripts" /
                             "operator_principal_admin.py"),
         command, subject],
        capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr


# --- structural (no database) ---------------------------------------------

def test_principals_module_has_no_write_surface():
    """The HTTP layer is read-only by construction: the module the routers
    import exposes no INSERT/UPDATE/DELETE against operator_principals."""
    source = (SERVER_DIR / "operator_principals.py").read_text(
        encoding="utf-8")
    for verb in ("INSERT INTO operator_principals",
                 "UPDATE operator_principals",
                 "DELETE FROM operator_principals"):
        assert verb not in source


def test_only_writer_is_the_cli():
    """Code-search proof: across server/ and server/routers/, the only
    module that writes operator_principals is the out-of-band CLI."""
    writers = []
    roots = [SERVER_DIR, SERVER_DIR / "routers", SERVER_DIR.parent / "scripts"]
    for root in roots:
        for path in root.glob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if ("INSERT INTO operator_principals" in text
                    or "UPDATE operator_principals" in text):
                writers.append(path.name)
    assert writers == ["operator_principal_admin.py"], writers


def test_store_unavailable_maps_to_503(monkeypatch):
    """PG down = 503 on the operator surface, never an open fallback."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    import operator_deps

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as err:
        async def resolve():
            dependency = operator_deps.require_operator(
                tenant="demo-tenant", x_operator_subject="auth0|nobody",
                x_operator_profile=None)
            return await anext(dependency)

        asyncio.run(resolve())
    assert err.value.status_code == 503


def test_dev_header_names_lookup_only(monkeypatch):
    """Off-live, X-Operator-Subject selects WHICH row to look up; with no
    granted row it resolves nothing (404), proving the header grants
    nothing by itself."""
    if not PG_URL:
        pytest.skip("LEAF_OPERATOR_TEST_DATABASE_URL not set")
    monkeypatch.setenv("DATABASE_URL", PG_URL)
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    import operator_deps, operator_principals
    operator_principals._db().reset_pool()

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as err:
        async def resolve():
            dependency = operator_deps.require_operator(
                tenant="demo-tenant",
                x_operator_subject=f"auth0|ungrant-{uuid.uuid4().hex[:8]}",
                x_operator_profile=None)
            return await anext(dependency)

        asyncio.run(resolve())
    assert err.value.status_code == 404


# --- live PG ---------------------------------------------------------------

@needs_pg
def test_grant_resolve_revoke_cycle(pg_env):
    import operator_principals as prin
    subject = f"auth0|op-{uuid.uuid4().hex[:8]}"
    _grant(subject)
    p = prin.resolve_principal(subject)
    assert p is not None and p.active and p.role_revision >= 1
    assert prin.revalidate(subject, p.role_revision)

    _admin("revoke", subject)
    assert prin.resolve_principal(subject).status == "revoked"
    assert not prin.revalidate(subject, p.role_revision)


@needs_pg
def test_role_revision_bump_invalidates(pg_env):
    import operator_principals as prin
    subject = f"auth0|op-{uuid.uuid4().hex[:8]}"
    _grant(subject)
    rev1 = prin.resolve_principal(subject).role_revision
    _grant(subject)  # re-grant bumps revision
    assert prin.resolve_principal(subject).role_revision > rev1
    assert not prin.revalidate(subject, rev1)


@needs_pg
def test_session_key_and_no_oracle(pg_env):
    import operator_session_store as store
    a = f"auth0|op-{uuid.uuid4().hex[:8]}"
    b = f"auth0|op-{uuid.uuid4().hex[:8]}"
    s1 = store.create_or_get_session(a, "default", "staging")
    s2 = store.create_or_get_session(a, "default", "staging")
    assert s1["session_id"] == s2["session_id"]  # idempotent per key
    s3 = store.create_or_get_session(a, "incident", "staging")
    assert s3["session_id"] != s1["session_id"]  # profile partitions
    s4 = store.create_or_get_session(b, "default", "staging")
    assert s4["session_id"] != s1["session_id"]  # subject partitions
    # Another principal's session reads as absent — no oracle.
    assert store.get_session(s1["session_id"], b) is None
    assert store.get_session(f"opsess-{uuid.uuid4()}", b) is None


@needs_pg
def test_turn_lock_and_snapshot(pg_env):
    import operator_session_store as store
    subject = f"auth0|op-{uuid.uuid4().hex[:8]}"
    s = store.create_or_get_session(subject, "default", "staging")
    turn = store.begin_turn(s["session_id"], subject, 3, ("default",),
                            "staging")
    assert turn is not None
    # Second concurrent turn rejected (reject-not-queue).
    assert store.begin_turn(s["session_id"], subject, 3, ("default",),
                            "staging") is None
    row = store.get_session(s["session_id"], subject)
    assert row["active_turn_id"] == turn
    store.end_turn(s["session_id"], turn)
    assert store.get_session(s["session_id"], subject)["active_turn_id"] is None


@needs_pg
def test_events_are_monotonic_per_session(pg_env):
    import operator_session_store as store
    subject = f"auth0|op-{uuid.uuid4().hex[:8]}"
    s = store.create_or_get_session(subject, "default", "staging")
    seqs = [store.append_event(s["session_id"], None, "operator_note",
                               {"i": i}) for i in range(3)]
    assert seqs == sorted(seqs) and len(set(seqs)) == 3
    events = store.read_events(s["session_id"], subject)
    assert [e["seq"] for e in events] == seqs


# --- route-level wiring (fresh FastAPI, the router-test idiom) -------------

@needs_pg
def test_router_forged_headers_resolve_nothing(monkeypatch):
    """Every forgeable elevation header on the mounted operator router
    resolves zero authority without a server-owned principal row."""
    monkeypatch.setenv("DATABASE_URL", PG_URL)
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    import operator_principals
    operator_principals._db().reset_pool()

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import operator_sessions as router_mod

    app = FastAPI()
    app.include_router(router_mod.router)
    client = TestClient(app, raise_server_exceptions=False)
    forged = {"Authorization": "Bearer forged.admin.token",
              "X-Tenant-Id": "demo-tenant",
              "X-Ops-Secret": "forged", "X-Dispatch-Secret": "forged",
              "X-Operator": "true",
              "X-Operator-Subject": f"auth0|forged-{uuid.uuid4().hex[:8]}"}
    for method, path in (("GET", "/api/operator/sessions"),
                         ("POST", "/api/operator/sessions"),
                         ("GET", "/api/operator/audit")):
        resp = client.request(method, path, headers=forged,
                              json={} if method == "POST" else None)
        assert resp.status_code == 404, (method, path, resp.status_code)


@needs_pg
def test_router_granted_principal_full_cycle(monkeypatch):
    """A granted principal creates a session, reads it back, and another
    granted principal cannot see it (no oracle at the route layer)."""
    monkeypatch.setenv("DATABASE_URL", PG_URL)
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    import operator_principals
    operator_principals._db().reset_pool()

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import operator_sessions as router_mod

    app = FastAPI()
    app.include_router(router_mod.router)
    client = TestClient(app, raise_server_exceptions=False)

    alice = f"auth0|op-{uuid.uuid4().hex[:8]}"
    bob = f"auth0|op-{uuid.uuid4().hex[:8]}"
    _grant(alice)
    _grant(bob)

    r = client.post("/api/operator/sessions", json={},
                    headers={"X-Operator-Subject": alice})
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]

    r = client.get(f"/api/operator/sessions/{sid}",
                   headers={"X-Operator-Subject": alice})
    assert r.status_code == 200

    r = client.get(f"/api/operator/sessions/{sid}",
                   headers={"X-Operator-Subject": bob})
    assert r.status_code == 404
    assert r.json()["detail"] == "operator_session_not_found"

    # messages without a configured harness: write surface unavailable
    r = client.post(f"/api/operator/sessions/{sid}/messages",
                    json={"text": "hello"},
                    headers={"X-Operator-Subject": alice})
    assert r.status_code == 503
