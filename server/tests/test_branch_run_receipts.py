"""Tenant receipt transport, strict schema, bounded storage, and support projection."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from branch_tools import run_receipts as receipts, sync_receipts, tenant_catalog
from customization_service import CustomizationService
from customization_store import SQLiteCustomizationStore
import deps
import operator_deps
import operator_principals
from operator_principals import OperatorPrincipal
from routers import ops, tools
from test_tenant_catalog import TENANT, OTHER, NAME, VERSION, publish, revocations
from test_branch_dev_publish import durable, publishing_lane
from test_customization_branch_target import lane

URL = f"/api/tool-envelopes/tenant/{NAME}/{VERSION}/receipts"
NOW = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)
DRAWING = "roof.dwg | sent copy abcdef12 | 2 layers | 4 objects in model space"
ERROR = {"code": "denied", "message": "The tool was refused."}


def body(index=1, kind="arrived", **fields):
    value = {"receipt_id": f"{index:032x}", "kind": kind, "package_digest": "a" * 64,
             "plugin_version": "1.2.3.4", "at": "2026-11-27T12:00:00Z"}
    if kind == "verify_failed":
        value["error"] = ERROR
    if kind == "ran":
        value["outcome"] = "ok"
    return {**value, **fields}


def seat(client, tenant=TENANT, subject="seat-a"):
    client.app.dependency_overrides[deps.require_active_tenant] = lambda: deps.TenantContext(tenant, subject=subject)


@pytest.fixture
def store(tmp_path):
    value = SQLiteCustomizationStore(tmp_path / "customization.db")
    value.initialize()
    return value


@pytest.fixture
def client(tmp_path, monkeypatch, store):
    monkeypatch.setenv("LEAF_PLUGIN_BUILDER", "1")
    monkeypatch.setenv("LEAF_PLUGIN_BUILDER_TENANTS", TENANT + "," + OTHER)
    monkeypatch.setenv("LEAF_TENANT_TOOLS_DIR", str(tmp_path / "catalog"))
    monkeypatch.setattr(CustomizationService, "configured", lambda: CustomizationService(store))
    monkeypatch.setattr(receipts, "subject_limiter", sync_receipts.ReceiptLimiter(lambda: 0, 30, 0.5))
    monkeypatch.setattr(receipts, "tenant_limiter", sync_receipts.ReceiptLimiter(lambda: 0, 120, 1.0))
    entry, _ = publish(tmp_path / "catalog")
    app = FastAPI()
    app.include_router(tools.router)
    app.include_router(ops.operator_router)
    app.dependency_overrides[deps.require_tenant] = lambda: "demo-tenant"
    with TestClient(app) as value:
        seat(value)
        value.receipt_digest = entry.package_digest
        yield value


def post(client, index=1, kind="arrived", **fields):
    return client.post(URL, json=body(index, kind, package_digest=client.receipt_digest, **fields))


def rows(store, tenant=TENANT):
    return receipts.support_rows(store, tenant, limit=100)


def test_each_kind_and_outcome_is_persisted(client, store):
    cases = [("arrived", {}), ("verify_failed", {}), ("ran", {"duration_ms": 0, "drawing": DRAWING}),
             ("ran", {"outcome": "failed", "error": ERROR, "duration_ms": 600000}),
             ("ran", {"outcome": "cancelled", "error": ERROR})]
    for index, (kind, fields) in enumerate(cases, 1):
        response = post(client, index, kind, **fields)
        assert response.status_code == 204 and response.content == b""
        assert response.headers["cache-control"] == "no-store"
        row = rows(store)[0]
        assert row["kind"] == kind and row["plugin_version"] == "1.2.3.4"
        assert row["subject"] == "seat-a" and row["package_digest"] == client.receipt_digest
        assert row["error"] == (ERROR if kind == "verify_failed" else fields.get("error"))
    assert len(rows(store)) == 5


def test_strict_schema_refuses_invalid_values(client, store):
    invalid = [
        {"extra": 1}, {"receipt_id": "A" * 32}, {"receipt_id": "a" * 31},
        {"package_digest": "g" * 64}, {"package_digest": "a" * 63}, {"plugin_version": "1"},
        {"plugin_version": "1.2.3.4.5"}, {"plugin_version": True}, {"kind": "synced"}, {"kind": []},
        {"at": "2026-11-27"}, {"at": "2026-11-27T12:00:00"}, {"at": "2026-02-30T12:00:00Z"},
        {"outcome": "ok"}, {"duration_ms": 1}, {"drawing": DRAWING}, {"error": ERROR},
        {"kind": "verify_failed"}, {"kind": "verify_failed", "error": ERROR, "duration_ms": 1},
        {"kind": "ran"}, {"kind": "ran", "outcome": "failed"},
        {"kind": "ran", "outcome": "ok", "error": ERROR}, {"kind": "ran", "outcome": False},
        *({"kind": "ran", "outcome": "ok", "duration_ms": value}
          for value in (True, -1, 600001, 1.5, "1", None)),
        *({"kind": "ran", "outcome": "failed", "error": value} for value in (
            None, {"code": "Bad", "message": "x"}, {"code": "x", "message": "a\nb"},
            {"code": "x", "message": "a\x00b"}, {"code": "x", "message": "x" * 301},
            {"code": "x", "message": "x", "stack": "x"}, {"code": "x"},
            {"code": "x" * 65, "message": ""}, {"code": "x", "message": 1})),
        *({"kind": "ran", "outcome": "ok", "drawing": value} for value in (
            None, "C:\\roof.dwg | sent copy abcdef12 | layers | 1 object in model space",
            "/tmp/roof.dwg | sent copy abcdef12 | layers | 1 object in model space",
            " | sent copy abcdef12 | layers | 1 object in model space", "roof.dwg",
            DRAWING.replace("abcdef12", "ABCDEF12"), DRAWING + " | extra", DRAWING + "\n",
            DRAWING.replace("4 objects", "many objects"), "x" * 1025)),
    ]
    for fields in invalid:
        response = client.post(URL, json={**body(package_digest=client.receipt_digest), **fields})
        assert response.status_code == 422, fields
        assert response.json() == {"detail": "invalid_receipt"}
    assert rows(store) == []


def test_invalid_json_and_non_objects(client, store):
    for raw in (b"{", b"[]", b"null", b"1", b'"text"', b"\xff", b'{"kind":NaN}',
                b'{"kind":"arrived","kind":"ran"}', b"{} {}"):
        response = client.post(URL, content=raw, headers={"Content-Type": "application/json"})
        assert response.status_code == 422
    assert rows(store) == []


def test_normalizes_digest_and_offset():
    value = receipts.parse(json.dumps(body(package_digest="A" * 64, at="2026-11-27T13:00:00+01:00")).encode())
    assert value["at"] == "2026-11-27T12:00:00Z" and value["package_digest"] == "a" * 64
    assert receipts.request_digest(value) == receipts.request_digest(dict(reversed(list(value.items()))))
    assert receipts.parse(json.dumps(body(kind="ran", drawing=DRAWING.replace("4 objects", "1 object"))).encode())
    assert receipts.parse(json.dumps(body(kind="ran", outcome="cancelled")).encode())
    assert receipts.parse(json.dumps(body(kind="ran", outcome="failed", error={"code": "x", "message": ""})).encode())


def test_feature_and_identity_checks_precede_body(client, store, monkeypatch):
    for path in (URL.replace(NAME, "Bad_Name"), URL.replace(VERSION, "invalid")):
        assert client.post(path, content=b"bad").status_code == 404
    monkeypatch.setenv("LEAF_PLUGIN_BUILDER", "off")
    response = client.post(URL, content=b"bad")
    assert response.status_code == 404 and response.json() == {"detail": "tool_not_published"}
    assert rows(store) == []


def test_unknown_tool_digest_mismatch_and_cross_tenant(client, store, tmp_path):
    assert client.post(URL.replace(NAME, "unknown-tool"), json=body()).status_code == 404
    assert client.post(URL, json=body()).status_code == 404
    other, _ = publish(tmp_path / "catalog", OTHER, package=b"other tenant bytes", name="other-tool")
    response = client.post(URL.replace(NAME, "other-tool"), json=body(package_digest=other.package_digest))
    assert response.status_code == 404 and response.json() == {"detail": "tool_not_published"}
    assert rows(store) == rows(store, OTHER) == []


def test_content_type_and_declared_size_before_read(client, store):
    assert client.post(URL, content=b"{}").status_code == 415
    response = client.post(URL, content=b"{}", headers={"Content-Type": "application/json", "Content-Length": "4097"})
    assert response.status_code == 413 and response.json() == {"detail": "receipt_too_large"}
    assert rows(store) == []
    assert client.post(URL, content=json.dumps(body(package_digest=client.receipt_digest)),
                       headers={"Content-Type": "application/json; charset=utf-8"}).status_code == 204


def test_streamed_size_without_content_length(client, store):
    response = client.post(URL, content=iter([b" " * 2000, b" " * 2097]),
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 413 and response.json() == {"detail": "receipt_too_large"}
    assert rows(store) == []


def test_idempotent_replay_and_conflict(client, store):
    assert post(client).status_code == post(client).status_code == 204
    response = post(client, plugin_version="2.0")
    assert response.status_code == 409 and response.json() == {"detail": "idempotency_conflict"}
    assert len(rows(store)) == 1


def test_concurrent_insert_is_idempotent(store):
    value = receipts.parse(json.dumps(body()).encode())
    def insert(_):
        return receipts.append(store, TENANT, "seat", NAME, VERSION, value, now=NOW)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sorted(pool.map(insert, range(4))) == [False, False, False, True]
    assert len(rows(store)) == 1


def test_same_id_is_independent_across_tenants(store):
    for tenant in (TENANT, OTHER):
        assert receipts.append(store, tenant, "seat", NAME, VERSION, body(), now=NOW)
    assert len(rows(store)) == len(rows(store, OTHER)) == 1


def test_subject_limit_refills_and_follows_subject(client, monkeypatch, tmp_path):
    clock = [0]
    monkeypatch.setattr(receipts, "subject_limiter", sync_receipts.ReceiptLimiter(lambda: clock[0], 30, 0.5))
    for index in range(30):
        assert post(client, index).status_code == 204
    publish(tmp_path / "catalog", OTHER)
    seat(client, OTHER)
    assert post(client, 31).status_code == 429
    seat(client, subject="seat-b")
    assert post(client, 32).status_code == 204
    seat(client)
    clock[0] = 2
    assert post(client, 33).status_code == 204
    assert post(client, 34).status_code == 429


def test_tenant_limit_across_subjects(client):
    for index in range(120):
        seat(client, subject=f"seat-{index}")
        assert post(client, index).status_code == 204
    seat(client, subject="fresh-seat")
    response = post(client, 121)
    assert response.status_code == 429 and response.json() == {"detail": "rate_limited"}


def test_configurable_limiter_map_bound():
    clock = [0]
    limiter = sync_receipts.ReceiptLimiter(lambda: clock[0], 30, 0.5)
    for index in range(10000):
        assert limiter.admit(str(index))
    assert not limiter.admit("new")
    clock[0] = 1
    assert not limiter.admit("new")
    clock[0] = 2
    assert limiter.admit("new") and len(limiter._buckets) == 10000


def test_retains_newest_200_for_exact_version(store):
    for index, (tenant, name, version) in enumerate(
            ((OTHER, NAME, VERSION), (TENANT, "other-tool", VERSION), (TENANT, NAME, "2.0.0"))):
        receipts.append(store, tenant, "seat", name, version, body(1000 + index), now=NOW)
    for index in range(201):
        receipts.append(store, TENANT, "seat", NAME, VERSION, body(index + 2), now=NOW)
    with store._connection() as conn:
        bucket = conn.execute("SELECT idempotency_key FROM branch_tool_run_receipts "
                              "WHERE tenant_id=? AND name=? AND version=? ORDER BY receipt_id",
                              (TENANT, NAME, VERSION)).fetchall()
        assert len(bucket) == 200 and bucket[0]["idempotency_key"] == f"{3:032x}"
        assert conn.execute("SELECT count(*) AS n FROM branch_tool_run_receipts").fetchone()["n"] == 203


def test_age_retention_only_deletes_inserting_tenant(store):
    old = NOW - timedelta(days=91)
    for tenant in (TENANT, OTHER):
        receipts.append(store, tenant, "seat", NAME, VERSION, body(), now=old)
    receipts.append(store, TENANT, "seat", "other-tool", VERSION, body(2), now=NOW)
    assert [row["name"] for row in rows(store)] == ["other-tool"]
    assert len(rows(store, OTHER)) == 1


def test_occurred_at_clock_bounds_and_server_time(store):
    for index, seconds in enumerate((-604801, -604800, 300, 301)):
        at = (NOW + timedelta(seconds=seconds)).isoformat()
        receipts.append(store, TENANT, "seat", NAME, VERSION, body(index, at=at), now=NOW)
    result = list(reversed(rows(store)))
    assert [row["occurred_at"] is None for row in result] == [True, False, False, True]
    assert all(row["received_at"] == "2026-11-27T12:00:00Z" for row in result)


def test_stage_projection_and_tenant_digest_isolation(store):
    view = lambda: receipts.stage_view(store, TENANT, NAME, VERSION, "a" * 64)
    assert view() is None
    receipts.append(store, OTHER, "seat", NAME, VERSION, body(), now=NOW)
    receipts.append(store, TENANT, "seat", NAME, VERSION, body(2, package_digest="b" * 64), now=NOW)
    assert view() is None
    receipts.append(store, TENANT, "seat", NAME, VERSION, body(3), now=NOW)
    assert view() == {"last_event": "synced", "at": "2026-11-27T12:00:00Z", "plugin_version": "1.2.3.4",
                      "arrived_at": "2026-11-27T12:00:00Z"}
    receipts.append(store, TENANT, "seat", NAME, VERSION, body(4, "verify_failed"), now=NOW)
    assert view()["verify_failed"]["error"] == ERROR and view()["last_event"] == "verify_failed"
    receipts.append(store, TENANT, "seat", NAME, VERSION, body(5), now=NOW)
    assert "verify_failed" not in view()
    receipts.append(store, TENANT, "seat", NAME, VERSION, body(6, "ran", duration_ms=1234, drawing=DRAWING), now=NOW)
    assert view()["last_event"] == "ran"
    assert view()["last_run"] == {"at": "2026-11-27T12:00:00Z", "outcome": "ok", "duration_ms": 1234, "drawing": DRAWING}
    receipts.append(store, TENANT, "seat", NAME, VERSION, body(7, "ran", outcome="failed", error=ERROR), now=NOW)
    assert view()["last_event"] == "run_failed" and view()["last_run"]["error"] == ERROR
    receipts.append(store, TENANT, "seat", NAME, VERSION, body(8, "ran", outcome="cancelled"), now=NOW)
    assert view()["last_event"] == "run_failed" and "error" not in view()["last_run"]


def test_latest_failure_across_versions_and_support_bounds(store):
    assert receipts.latest_run_failure(store, TENANT, NAME) is None
    for tenant, version, index in ((TENANT, VERSION, 1), (TENANT, "2.0.0", 2), (OTHER, "3.0.0", 3)):
        receipts.append(store, tenant, "seat", NAME, version, body(index, "ran", outcome="failed", error=ERROR), now=NOW)
    receipts.append(store, TENANT, "seat", NAME, "4.0.0", body(4, "ran"), now=NOW)
    assert receipts.latest_run_failure(store, TENANT, NAME) == {"version": "2.0.0", "at": "2026-11-27T12:00:00Z", "error": ERROR}
    assert len(receipts.support_rows(store, TENANT, limit=0)) == 1
    assert len(receipts.support_rows(store, TENANT, limit=1000)) == 3
    assert receipts.support_rows(store, TENANT, "unknown-tool") == []
    assert set(rows(store)[0]) == {"name", "version", "package_digest", "kind", "outcome", "plugin_version",
                                 "duration_ms", "drawing", "error", "occurred_at", "received_at", "subject"}


def test_published_digest_reads_only_envelope_and_ignores_revocation(tmp_path, monkeypatch):
    entry, _ = publish(tmp_path)
    tenant_catalog.write_revocations(tmp_path, TENANT, revocations(entry.package_digest))
    original = tenant_catalog._read
    def read(path, limit):
        assert path.name == "envelope.json" and limit == tenant_catalog.ENVELOPE_LIMIT
        return original(path, limit)
    monkeypatch.setattr(tenant_catalog, "_read", read)
    assert tenant_catalog.published_digest(tmp_path, TENANT, NAME, VERSION) == entry.package_digest
    assert tenant_catalog.published_digest(tmp_path, OTHER, NAME, VERSION) is None
    assert tenant_catalog.published_digest(tmp_path, TENANT, "../bad", VERSION) is None


def test_stage_service_prefers_new_receipts_and_falls_back(publishing_lane, monkeypatch):
    state = publishing_lane
    publication = state.publish()
    change = durable(state)
    status = lambda: state.service.stage_status_change(change)
    assert "branch" not in status()
    legacy = {"event": "synced", "plugin_version": "1.2", "package_digest": publication["package_digest"]}
    sync_receipts.append(state.store, "tenant-a", "seat", state.name, "1.2.0", legacy)
    assert status()["branch"]["last_event"] == "synced"
    receipts.append(state.store, "tenant-a", "seat", state.name, "1.2.0",
                    body(kind="ran", package_digest=publication["package_digest"]), now=NOW)
    assert status()["branch"]["last_run"]["outcome"] == "ok"
    def fail(*args):
        raise RuntimeError("unavailable")
    monkeypatch.setattr(receipts, "stage_view", fail)
    assert status()["branch"]["last_event"] == "synced" and "last_run" not in status()["branch"]


def test_operator_grant_tenant_filter_and_plugin_check(client, store, tmp_path, monkeypatch):
    monkeypatch.setattr(operator_deps.tenant_deps, "auth_live", lambda: False)
    monkeypatch.setattr(operator_principals, "resolve_principal", lambda _: None)
    url = f"/api/operator/tenants/{TENANT}/tool-receipts"
    assert client.get(url, headers={"X-Operator-Subject": "forged"}).status_code == 404
    principal = OperatorPrincipal(subject="auth0|operator", role="operator", role_revision=1,
                                  status="active", profiles=("default",), environment="staging")
    monkeypatch.setattr(operator_principals, "resolve_principal", lambda subject: principal if subject == principal.subject else None)
    headers = {"X-Operator-Subject": principal.subject}
    for tenant in (TENANT, OTHER):
        receipts.append(store, tenant, "seat", NAME, VERSION, body(), now=NOW)
    tenant_catalog.record_plugin_check(tmp_path / "catalog", TENANT, NOW)
    response = client.get(url, headers=headers)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json() == {"tenant_id": TENANT, "last_plugin_check_at": "2026-11-27T12:00:00Z", "receipts": rows(store)}
    assert len(client.get(url.replace(TENANT, OTHER), headers=headers).json()["receipts"]) == 1
    assert client.get(url + "?name=unknown-tool", headers=headers).json()["receipts"] == []
    for query in ("?limit=0", "?limit=101", "?limit=bad", "?name=Bad_Name"):
        assert client.get(url + query, headers=headers).status_code == 422
    assert client.get(url.replace(TENANT, "bad"), headers=headers).status_code == 404
    monkeypatch.delenv("LEAF_TENANT_TOOLS_DIR")
    assert client.get(url, headers=headers).json()["last_plugin_check_at"] is None


def test_store_failure_is_503_for_both_routes(client, monkeypatch):
    def fail():
        raise RuntimeError("private failure detail")
    monkeypatch.setattr(CustomizationService, "configured", fail)
    response = post(client)
    assert response.status_code == 503 and response.json() == {"detail": "receipt_store_unavailable"}
    client.app.dependency_overrides[operator_deps.require_operator] = lambda: object()
    response = client.get(f"/api/operator/tenants/{TENANT}/tool-receipts")
    assert response.status_code == 503 and response.json() == {"detail": "receipt_store_unavailable"}


def test_requires_active_tenant_authentication(client, store, monkeypatch):
    client.app.dependency_overrides.pop(deps.require_active_tenant)
    client.app.dependency_overrides.pop(deps.require_tenant)
    monkeypatch.setattr(deps, "auth_live", lambda: True)
    assert post(client).status_code in (401, 403, 404)
    assert rows(store) == []

    def reject_tenant():
        raise HTTPException(status_code=401)

    client.app.dependency_overrides[deps.require_active_tenant] = reject_tenant
    assert post(client).status_code == 401
    assert rows(store) == []


def test_subject_falls_back_to_verified_tenant(client, store):
    client.app.dependency_overrides[deps.require_active_tenant] = lambda: TENANT
    assert post(client).status_code == 204
    assert rows(store)[0]["subject"] == f"tenant:{TENANT}"
