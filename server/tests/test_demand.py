"""Contract tests for the public demand-capture endpoint."""
from __future__ import annotations

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
