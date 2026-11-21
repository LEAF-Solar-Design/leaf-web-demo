"""Dev receipt transport, retention, and the published stage status projection."""

from datetime import datetime
import json
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from branch_tools import sync_receipts
import deps
from envelopes import install_error_handlers
from routers.tools import router
from test_branch_dev_catalog import DIGEST, stage
from test_branch_dev_publish import durable, publishing_lane
from test_customization_branch_target import NAME, lane


VERSION = "1.2.0"
URL = f"/api/tool-envelopes/dev/{NAME}/{VERSION}/receipts"
BODY = {"event": "synced", "package_digest": DIGEST, "plugin_version": "3.4.0.1"}


def as_seat(client, tenant="tenant-a", subject="seat-a"):
    client.app.dependency_overrides[deps.require_active_tenant] = lambda: deps.TenantContext(
        tenant, subject=subject,
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    root = tmp_path / "catalog"
    stage(root)
    monkeypatch.setenv("LEAF_CUSTOMIZATION_INTERNAL_TENANTS", "tenant-a,tenant-b")
    monkeypatch.setenv("LEAF_BRANCH_DEV_TOOLS_DIR", str(root))
    monkeypatch.setenv("LEAF_BRANCH_DEV_TOOLS", "1")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_DB", str(tmp_path / "customization.db"))
    monkeypatch.setenv("LEAF_CUSTOMIZATION_STORE", "sqlite")
    monkeypatch.setattr(sync_receipts, "limiter", sync_receipts.ReceiptLimiter(lambda: 0))
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router)
    with TestClient(app) as client:
        as_seat(client)
        yield client


@pytest.mark.parametrize("event", ["synced", "compile_failed", "ran", "run_failed"])
def test_accepts_and_persists_event(client, tmp_path, event):
    diagnostics = [{"code": code} for code in ("scan", "unparsed", "denied", "timeout")]
    diagnostics += [{"code": "CS1234", "line": 1000000}] * 6
    body = dict(BODY, event=event, package_digest=DIGEST.upper(), duration_ms=600000,
                diagnostics=diagnostics)
    response = client.post(URL, json=body)
    assert response.status_code == 204 and response.content == b""
    with sqlite3.connect(tmp_path / "customization.db") as conn:
        row, = conn.execute("SELECT tenant_id, subject, name, version, package_digest, event, "
                            "plugin_version, duration_ms, diagnostics_json FROM branch_tool_receipts")
    assert row[:-1] == ("tenant-a", "seat-a", NAME, VERSION, DIGEST, event, "3.4.0.1", 600000)
    assert json.loads(row[-1]) == diagnostics


@pytest.mark.parametrize("overrides", [
    {"event": "downloaded"}, {"plugin_version": "1"}, {"plugin_version": "1.2.3.4.5"},
    {"plugin_version": "1.2beta"}, {"package_digest": "a" * 63}, {"package_digest": "g" * 64},
    {"summary": "free text"}, {"drawing": {}}, {"tenant_id": "tenant-b"}, {"subject": "seat-b"},
    {"duration_ms": -1}, {"duration_ms": 600001}, {"duration_ms": True}, {"duration_ms": None},
    {"diagnostics": [{"code": "CS123"}]}, {"diagnostics": [{"code": "scan", "line": -1}]},
    {"diagnostics": [{"code": "scan", "line": 1000001}]},
    {"diagnostics": [{"code": "scan", "line": "2"}]},
    {"diagnostics": [{"code": "timeout", "file": "drawing.dwg"}]},
    {"diagnostics": [{"code": "CS0001"}] * 11}, {"diagnostics": None},
])
def test_rejects_values_and_fields_outside_contract(client, tmp_path, overrides):
    assert client.post(URL, json={**BODY, **overrides}).status_code == 422
    assert not (tmp_path / "customization.db").exists()


@pytest.mark.parametrize(("name", "version", "digest"), [
    ("unknown-tool", VERSION, DIGEST), (NAME, "9.9.9", DIGEST), (NAME, VERSION, "a" * 64),
    ("Bad_Name", VERSION, DIGEST), (NAME, "invalid", DIGEST),
])
def test_requires_valid_catalog_identity(client, tmp_path, name, version, digest):
    response = client.post(f"/api/tool-envelopes/dev/{name}/{version}/receipts",
                           json=dict(BODY, package_digest=digest))
    assert response.status_code == 404 and response.json() == {"detail": "tool_not_published"}
    assert not (tmp_path / "customization.db").exists()


@pytest.mark.parametrize("hidden", ["flag", "tenant"])
def test_hidden_route_matches_dev_catalog(client, tmp_path, monkeypatch, hidden):
    if hidden == "flag":
        monkeypatch.delenv("LEAF_BRANCH_DEV_TOOLS")
    else:
        as_seat(client, tenant="external")
    for body in (BODY, {"summary": "invalid"}):
        response = client.post(URL, json=body)
        assert response.status_code == 404 and response.json() == {"detail": "tool_not_published"}
        assert response.headers["cache-control"] == "no-store"
    assert not (tmp_path / "customization.db").exists()


def test_live_route_requires_the_existing_seat_token(client, tmp_path, monkeypatch):
    client.app.dependency_overrides.clear()
    monkeypatch.setattr(deps, "auth_live", lambda: True)
    response = client.post(URL, headers={"X-Tenant-Id": "tenant-a"}, json=BODY)
    assert response.status_code in (401, 403)
    assert not (tmp_path / "customization.db").exists()


def test_rate_follows_subject_across_tenants_and_refills(client, monkeypatch):
    now = 0
    monkeypatch.setattr(sync_receipts, "limiter", sync_receipts.ReceiptLimiter(lambda: now))
    for _ in range(60):
        assert client.post(URL, json=BODY).status_code == 204
    as_seat(client, tenant="tenant-b")
    assert client.post(URL, json=BODY).status_code == 429
    as_seat(client, subject="seat-b")
    assert client.post(URL, json=BODY).status_code == 204
    as_seat(client)
    now += 1
    assert client.post(URL, json=BODY).status_code == 204
    assert client.post(URL, json=BODY).status_code == 429


def test_bucket_capacity_preserves_outstanding_limits():
    now = 0
    limiter = sync_receipts.ReceiptLimiter(lambda: now)
    for i in range(10000):
        assert limiter.admit(str(i))
    assert not limiter.admit("new-seat")
    now += 60
    assert limiter.admit("new-seat")
    assert len(limiter._buckets) == 10000


def test_prunes_oldest_across_digests_within_package_only(client, tmp_path):
    assert client.post(URL, json=BODY).status_code == 204
    with sqlite3.connect(tmp_path / "customization.db") as conn:
        sql = ("INSERT INTO branch_tool_receipts (tenant_id, name, version, package_digest, subject, "
               "event, plugin_version) VALUES (?, ?, ?, ?, 'seed', 'ran', '2.0')")
        # Fill the bucket with an older digest and seed three unrelated buckets.
        conn.executemany(sql, [("tenant-a", NAME, VERSION, "b" * 64)] * 999)
        others = [("tenant-b", NAME, VERSION), ("tenant-a", "other-tool", VERSION),
                  ("tenant-a", NAME, "1.1.0")]
        conn.executemany(sql, [(*key, DIGEST) for key in others])
    assert client.post(URL, json=BODY).status_code == 204
    with sqlite3.connect(tmp_path / "customization.db") as conn:
        rows = conn.execute("SELECT tenant_id, name, version, count(*) FROM branch_tool_receipts "
                            "GROUP BY tenant_id, name, version").fetchall()
        counts = {tuple(row[:3]): row[3] for row in rows}
        assert counts == {**dict.fromkeys(others, 1), ("tenant-a", NAME, VERSION): 1000}
        assert conn.execute("SELECT min(receipt_id) FROM branch_tool_receipts").fetchone() == (2,)


def test_status_scopes_latest_receipt_to_published_tenant_and_digest(client, publishing_lane, monkeypatch):
    monkeypatch.setenv("LEAF_BRANCH_DEV_TOOLS_DIR", str(publishing_lane.root))
    publication = publishing_lane.publish()
    change = durable(publishing_lane)
    status = publishing_lane.service.stage_status_change
    monkeypatch.setenv("LEAF_CUSTOMIZATION_INTERNAL_TENANTS", "tenant-a,tenant-b")
    body = dict(BODY, package_digest=publication["package_digest"])
    assert "branch" not in status(change)
    as_seat(client, tenant="tenant-b", subject="seat-b")
    assert client.post(URL, json=body).status_code == 204
    assert "branch" not in status(change)
    as_seat(client)
    assert client.post(URL, json=body).status_code == 204
    assert "diagnostics" not in status(change)["branch"]
    diagnostics = [{"code": "CS0123", "line": 0}]
    assert client.post(URL, json=dict(body, event="compile_failed", diagnostics=diagnostics)).status_code == 204
    branch = status(change)["branch"]
    assert datetime.fromisoformat(branch["at"].replace("Z", "+00:00")).utcoffset().total_seconds() == 0
    assert branch == {"last_event": "compile_failed", "plugin_version": "3.4.0.1",
                      "diagnostics": diagnostics, "at": branch["at"]}
    # A later report for different bytes at the same name/version cannot take its place.
    (publishing_lane.root / NAME / VERSION / "digest.txt").write_text("c" * 64)
    assert client.post(URL, json=dict(BODY, event="ran", package_digest="c" * 64)).status_code == 204
    assert status(change)["branch"] == branch
    assert "branch" not in status(publishing_lane.change)
