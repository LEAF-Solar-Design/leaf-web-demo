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
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(demand, "DB_PATH", tmp_path / "demand.db")
    demand._reset_for_tests()
    demand._db()  # initialize the durable schema before negative assertions
    with TestClient(app_module.app, raise_server_exceptions=False) as test_client:
        yield test_client
    demand._reset_for_tests()


def post(client, email="person@example.com", interest="solar automation"):
    return client.post("/api/demand", json={"email": email, "interest": interest})


def test_post_stores_a_row(client):
    response = post(client)

    assert response.status_code == 200
    assert response.json() == {"ok": True, "stored": True, "duplicate": False}
    conn = sqlite3.connect(demand.DB_PATH)
    try:
        assert conn.execute(
            "SELECT email, interest, org FROM demand_captures"
        ).fetchall() == [("person@example.com", "solar automation", None)]
    finally:
        conn.close()


def test_duplicate_email_is_idempotent(client):
    assert post(client).json()["stored"] is True
    response = post(client, interest="a changed request")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "stored": False, "duplicate": True}
    conn = sqlite3.connect(demand.DB_PATH)
    try:
        assert conn.execute("SELECT COUNT(*) FROM demand_captures").fetchone()[0] == 1
    finally:
        conn.close()


@pytest.mark.parametrize("email", ["not-an-email", "person@", "person@example"])
def test_invalid_email_is_rejected_fail_closed(client, email):
    response = post(client, email=email)

    assert response.status_code == 422
    assert response.json()["ok"] is False
    conn = sqlite3.connect(demand.DB_PATH)
    try:
        assert conn.execute("SELECT COUNT(*) FROM demand_captures").fetchone()[0] == 0
    finally:
        conn.close()


def test_rate_limit_is_enforced(client, monkeypatch):
    monkeypatch.setenv("LEAF_DEMAND_PER_IP_PER_DAY", "1")

    assert post(client, email="first@example.com").status_code == 200
    response = post(client, email="second@example.com")

    assert response.status_code == 429
    assert response.json()["error"]["retryable"] is True
