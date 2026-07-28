"""Contract tests for the public demand-capture endpoint."""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import app as app_module  # noqa: E402
from routers import demand  # noqa: E402


@pytest.fixture()
def db_path(monkeypatch, tmp_path):
    path = tmp_path / "demand.db"
    monkeypatch.setenv("DEMAND_DB", str(path))
    demand._reset_for_tests()
    demand._db(path)  # initialize the durable schema before negative assertions
    yield path
    demand._reset_for_tests()


@pytest.fixture()
def client(db_path):
    with TestClient(app_module.app, raise_server_exceptions=False) as test_client:
        yield test_client


def post(client, email="person@example.com", interest="solar automation", headers=None):
    return client.post(
        "/api/demand", json={"email": email, "interest": interest}, headers=headers or {}
    )


def row_count(db_path, table="demand_captures"):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_post_stores_a_row(client, db_path):
    response = post(client)

    assert response.status_code == 200
    assert response.json() == {"ok": True, "stored": True, "duplicate": False}
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute(
            "SELECT email, interest, org FROM demand_captures"
        ).fetchall() == [("person@example.com", "solar automation", None)]
    finally:
        conn.close()


def test_duplicate_email_is_idempotent(client, db_path):
    assert post(client).json()["stored"] is True
    response = post(client, interest="a changed request")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "stored": False, "duplicate": True}
    assert row_count(db_path) == 1


@pytest.mark.parametrize(
    "email",
    [
        "not-an-email",
        "person@",
        "person@example",
        "a\x00@b.com",  # NUL byte is not whitespace, so the loose regex alone accepts it
        ".a@example.com",
        "a.@example.com",
        "a..b@example.com",
        "a@example..com",
        "a@-example.com",
        "a@example-.com",
        # Printable ASCII that is NOT valid unquoted local-part syntax
        # (round-2 finding: these all passed the previous checks).
        "a,b@example.com",
        "a:b@example.com",
        "a(b)@example.com",
        "a<b>@example.com",
        "a[b]@example.com",
        "a\\b@example.com",
        'a"b@example.com',
        "a;b@example.com",
    ],
)
def test_invalid_email_is_rejected_fail_closed(client, db_path, email):
    response = post(client, email=email)

    assert response.status_code == 422
    assert response.json()["ok"] is False
    assert row_count(db_path) == 0


def test_rate_limit_is_enforced_and_rejected_email_is_not_written(client, db_path, monkeypatch):
    monkeypatch.setenv("LEAF_DEMAND_PER_IP_PER_DAY", "1")

    assert post(client, email="first@example.com").status_code == 200
    response = post(client, email="second@example.com")

    assert response.status_code == 429
    assert response.json()["error"]["retryable"] is True
    assert row_count(db_path) == 1  # the rejected email left no row behind


def test_oversized_body_is_refused_before_parsing(client, db_path):
    big = "x" * (demand._MAX_BODY_BYTES + 1)
    response = client.post(
        "/api/demand",
        content=('{"email": "person@example.com", "interest": "' + big + '"}').encode(),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413
    assert row_count(db_path) == 0


def test_deployed_environment_without_demand_db_fails_closed(monkeypatch, tmp_path):
    monkeypatch.delenv("DEMAND_DB", raising=False)
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "staging")
    demand._reset_for_tests()
    try:
        with TestClient(app_module.app, raise_server_exceptions=False) as test_client:
            response = post(test_client)
        assert response.status_code == 503
        assert response.json()["error"]["retryable"] is True
        # Nothing may be written anywhere: the repo-default file must not appear.
        assert not (demand.SERVER_DIR / "demand.db").exists()
    finally:
        demand._reset_for_tests()


@pytest.mark.parametrize(
    "configured",
    [
        "demand.db",  # relative: resolves under /app/server, task-local
        "/tmp/demand.db",  # absolute but ephemeral
        "/data/demand.db",  # /data is NOT a mount; container-local (round-3 finding)
        "/data/ephemeral/demand.db",  # under /data but outside the state mount
        "/data/state/../../tmp/demand.db",  # dot-dot escape past the lexical check
        "data/state/demand.db",
    ],
)
def test_deployed_non_durable_demand_db_fails_closed(monkeypatch, configured):
    """Round-2 BLOCKING: a nonempty but non-durable DEMAND_DB must 503, not
    silently store captures on storage that vanishes at task replacement."""
    monkeypatch.setenv("DEMAND_DB", configured)
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "staging")
    demand._reset_for_tests()
    try:
        with TestClient(app_module.app, raise_server_exceptions=False) as test_client:
            response = post(test_client)
        assert response.status_code == 503
        assert response.json()["error"]["retryable"] is True
        assert not (demand.SERVER_DIR / "demand.db").exists()
    finally:
        demand._reset_for_tests()


def test_deployed_durable_demand_db_is_accepted():
    """The guard must accept exactly the durable state-mount shape that
    compose pins (DEMAND_DB=/data/state/demand.db) and the task definitions
    will inject (checked at the pure-function level so the test writes
    nothing under /data on a dev machine)."""
    assert demand._durable_deployed_path("/data/state/demand.db") is True
    assert demand._durable_deployed_path("/data/state/sub/demand.db") is True
    assert demand._durable_deployed_path("/data/demand.db") is False  # /data is not a mount
    assert demand._durable_deployed_path("/data/state") is False  # the mount itself, not a file in it
    assert demand._durable_deployed_path("/data/statex/demand.db") is False
    assert demand._durable_deployed_path("/datax/state/demand.db") is False


@pytest.mark.skipif(os.name != "posix", reason="symlinks need POSIX; CI's Linux cell runs this")
def test_symlinked_db_filename_is_refused_at_connect(monkeypatch, tmp_path):
    """Round-4 BLOCKING: sqlite follows a symlink at the FILENAME, so a
    db_path whose final component links outside the mount must be refused
    before the first connect (parent-only resolution missed it)."""
    durable = tmp_path / "state"
    durable.mkdir()
    outside = tmp_path / "outside.db"
    outside.touch()
    link = durable / "demand.db"
    link.symlink_to(outside)
    monkeypatch.setattr(demand, "_DURABLE_ROOT", demand.PurePosixPath(durable.as_posix()))
    demand._reset_for_tests()
    try:
        with pytest.raises(OSError, match="symlink"):
            demand._db(link, require_durable=True)
        assert demand._CONN is None  # a later corrected env can still connect
    finally:
        demand._reset_for_tests()


def test_deeply_nested_json_is_422_not_500(client, db_path):
    """Round-2 MINOR: a bounded body of thousands of nested arrays raises
    RecursionError in the parser; that is malformed input, not a server
    fault."""
    depth = 3000
    body = ("[" * depth) + ("]" * depth)
    response = client.post(
        "/api/demand", content=body.encode(), headers={"content-type": "application/json"}
    )
    assert response.status_code == 422
    assert row_count(db_path) == 0


def test_storage_section_is_synchronous_threadpool_work():
    """Round-2 MAJOR: the sqlite transaction must not run on the event loop.
    The route stays async (it streams the bounded body) and hands the
    blocking section to run_in_threadpool; this pins that the blocking
    section is a plain sync function, so it cannot silently move back onto
    the loop."""
    import inspect

    assert not inspect.iscoroutinefunction(demand._store_capture)
    assert inspect.iscoroutinefunction(demand.capture_demand)


def test_forwarded_for_uses_the_proxy_attested_last_entry(client, db_path, monkeypatch):
    monkeypatch.setenv("LEAF_TRUST_FORWARDED_FOR", "1")
    monkeypatch.setenv("LEAF_DEMAND_PER_IP_PER_DAY", "1")

    first = post(
        client,
        email="first@example.com",
        headers={"x-forwarded-for": "203.0.113.7, 198.51.100.9"},
    )
    assert first.status_code == 200

    # Different spoofed FIRST entry, same proxy-attested LAST entry: the
    # spoof must not mint a fresh rate-limit bucket.
    spoofed = post(
        client,
        email="second@example.com",
        headers={"x-forwarded-for": "203.0.113.99, 198.51.100.9"},
    )
    assert spoofed.status_code == 429

    # A genuinely different proxy-attested client still gets through.
    other = post(
        client,
        email="third@example.com",
        headers={"x-forwarded-for": "203.0.113.7, 198.51.100.10"},
    )
    assert other.status_code == 200


def test_global_daily_cap_backstops_ip_rotation(client, db_path, monkeypatch):
    monkeypatch.setenv("LEAF_TRUST_FORWARDED_FOR", "1")
    monkeypatch.setenv("LEAF_DEMAND_PER_IP_PER_DAY", "10")
    monkeypatch.setenv("LEAF_DEMAND_GLOBAL_PER_DAY", "2")

    for index in range(2):
        response = post(
            client,
            email=f"visitor{index}@example.com",
            headers={"x-forwarded-for": f"198.51.100.{index + 1}"},
        )
        assert response.status_code == 200

    rotated = post(
        client,
        email="visitor99@example.com",
        headers={"x-forwarded-for": "198.51.100.99"},
    )
    assert rotated.status_code == 429
    assert row_count(db_path) == 2
