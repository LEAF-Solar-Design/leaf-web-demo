"""HTTP authorization and retained acceptance cards against real PostgreSQL."""
from __future__ import annotations

import copy
import os
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

SERVER = Path(__file__).resolve().parents[1]
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))
os.environ.setdefault("SESSIONS_DB", str(Path(tempfile.mkdtemp(prefix="engine-cards-")) / "sessions.db"))

from fastapi import FastAPI, Header  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from psycopg import connect  # noqa: E402
import deps  # noqa: E402
import platform_link  # noqa: E402
from routers import engine_changes  # noqa: E402

PG_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="DATABASE_URL is required for engine change cards")
INGEST = "/internal/ops/engine-changes/cards"
LIST = "/api/engine-changes"
SECRET = {"X-Ops-Secret": "engine-card-test-secret"}


@pytest.fixture(scope="module", autouse=True)
def database():
    name = "engine_cards_http_" + uuid.uuid4().hex
    migration = SERVER.parent / "platform/migrations/0071_engine_change_cards.sql"
    with connect(PG_URL, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {name}")
        conn.execute(f"SET search_path TO {name}")
        conn.execute(migration.read_text(encoding="utf-8"))
    db = platform_link.platform_db()
    original = db.get_database_url
    separator = "&" if "?" in PG_URL else "?"
    scoped = f"{PG_URL}{separator}options=-csearch_path%3D{name}"
    db.reset_pool()
    db.get_database_url = lambda: scoped
    try:
        yield db
    finally:
        db.reset_pool()
        db.get_database_url = original
        with connect(PG_URL, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA {name} CASCADE")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("LEAF_OPS_SECRET", SECRET["X-Ops-Secret"])
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    app = FastAPI()
    app.include_router(engine_changes.router)

    def admin(x_test_subject: str = Header(default="operator-a")):
        return deps.TenantContext(
            "tenant-a", tier="admin", subject=x_test_subject,
            authority_resolved=True)

    app.dependency_overrides[deps.require_active_tenant] = admin
    with TestClient(app, raise_server_exceptions=False) as result:
        yield result


@pytest.fixture
def payload():
    return {
        "operation_id": "engine-op-" + uuid.uuid4().hex,
        "state": "accepted", "incident_fingerprint": "seek-control-noop",
        "feature_id": "studio-assistant", "title": "Repair assistant control",
        "summary": "The accepted change restores the timestamp control.",
        "change": {"pr_number": 42, "pr_url": "https://github.com/example/repo/pull/42",
                   "head_sha": "a" * 40, "files": ["web/src/control.js"],
                   "diff_stat": {"additions": 2, "deletions": 1}},
        "evidence": {"before_screenshot": "receipts/before.png",
                     "after_screenshot": "receipts/after.png",
                     "regression_spec_path": "regressions/seek.yaml", "receipt_ids": ["receipt-42"]},
        "acceptance": {"verdict": "accepted", "acceptor": "controller",
                       "accepted_at": "2026-10-01T12:00:00Z"},
    }


def publish(client, payload):
    response = client.post(INGEST, json=payload, headers=SECRET)
    assert response.status_code == 200, response.text
    return response.json()


def test_ingest_requires_correct_ops_secret(client, payload):
    for headers in ({}, {"X-Ops-Secret": "wrong"}, [(b"X-Ops-Secret", b"\xc3\xa9")]):
        response = client.post(INGEST, json=payload, headers=headers)
        assert response.status_code == 403, response.text
    assert platform_link.engine_change_cards_store().get_card(str(uuid.uuid4())) is None
    with platform_link.platform_db().cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM engine_change_cards WHERE operation_id = %s", (payload["operation_id"],))
        assert cur.fetchone()["n"] == 0


def test_ingest_unset_secret_fails_closed(client, payload, monkeypatch):
    monkeypatch.delenv("LEAF_OPS_SECRET")
    for mode in ("0", "1"):
        monkeypatch.setenv("LEAF_AUTH_LIVE", mode)
        response = client.post(INGEST, json=payload)
        assert response.status_code == 503, response.text


def test_ingest_repeat_is_idempotent(client, payload):
    first = publish(client, payload)
    second = publish(client, payload)
    assert second == first


def test_state_advances_preserve_card_identity(client, payload):
    first = publish(client, payload)
    payload["state"] = "landed"
    landed = publish(client, payload)
    payload.update(state="live", deployment_identity="staging:sha-123")
    live = publish(client, payload)
    assert first["card_id"] == landed["card_id"] == live["card_id"]
    assert first["created_at"] == live["created_at"]
    assert live["deployment_identity"] == "staging:sha-123"
    assert publish(client, payload) == live


def test_rewrites_are_conflicts(client, payload):
    first = publish(client, payload)
    for field in ("title", "summary", "incident_fingerprint", "feature_id", "change", "evidence", "acceptance"):
        rewritten = copy.deepcopy(payload)
        rewritten[field] = {"different": True} if isinstance(rewritten[field], dict) else "different"
        for state in ("accepted", "landed"):
            rewritten["state"] = state
            response = client.post(INGEST, json=rewritten, headers=SECRET)
            assert response.status_code == 409, response.text
    assert client.get(f"{LIST}/{first['card_id']}").json() == first


def test_skipped_backward_and_terminal_transitions_refused(client, payload):
    for current, refused in (("accepted", "live"), ("landed", "accepted"),
                             ("live", "landed"), ("held", "live"), ("reverted", "landed")):
        original = {**payload, "operation_id": str(uuid.uuid4()), "state": current}
        card = publish(client, original)
        response = client.post(INGEST, json={**original, "state": refused}, headers=SECRET)
        assert response.status_code == 409, response.text
        assert client.get(f"{LIST}/{card['card_id']}").json()["state"] == current


def test_reverted_and_held_are_legal_terminal_advances(client, payload):
    for state in ("reverted", "held"):
        original = {**payload, "operation_id": str(uuid.uuid4())}
        card = publish(client, original)
        result = publish(client, {**original, "state": state})
        assert result["card_id"] == card["card_id"]
        assert result["state"] == state


def test_all_payload_bounds_are_enforced(client, payload):
    for field, value in (("operation_id", "x" * 257), ("title", "x" * 201),
                         ("summary", "x" * 4001), ("feature_id", "x" * 257),
                         ("incident_fingerprint", "x" * 257), ("deployment_identity", "x" * 257),
                         ("change", {"refs": ["x" * 4096] * 9}),
                         ("evidence", {"refs": ["x" * 4096] * 9}),
                         ("acceptance", {"refs": ["x" * 4096] * 9})):
        response = client.post(INGEST, json={**payload, field: value}, headers=SECRET)
        assert response.status_code == 422, response.text


def test_nested_token_values_and_keys_refused(client, payload):
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzZWNyZXQifQ.signature"
    for field, value in (("summary", "Bearer secret-value"), ("title", jwt),
                         ("title", "eyJ9.e30.eA"),
                         ("evidence", {"nested": [{"ref": jwt}]}),
                         ("change", {"Bearer secret-value": "ref"}),
                         ("acceptance", {"acceptor": "Bearer secret-value"})):
        response = client.post(INGEST, json={**payload, field: value}, headers=SECRET)
        assert response.status_code == 422, response.text
        assert jwt not in response.text and "secret-value" not in response.text


def test_complex_or_nonfinite_json_refused(client, payload):
    nested = {}
    for _ in range(20):
        nested = {"nested": nested}
    for value in (nested, {"refs": [0] * 5000}):
        response = client.post(INGEST, json={**payload, "evidence": value}, headers=SECRET)
        assert response.status_code == 422, response.text
    raw = __import__("json").dumps({**payload, "change": {"size": float("nan")}})
    assert client.post(INGEST, content=raw, headers=SECRET).status_code == 422


def test_unread_and_read_receipts_are_subject_isolated(client, payload):
    card = publish(client, payload)
    subject = "operator-" + uuid.uuid4().hex
    other = {"X-Test-Subject": subject + "-other"}
    headers = {"X-Test-Subject": subject}
    before = client.get(LIST, headers=headers).json()
    assert next(c for c in before["cards"] if c["card_id"] == card["card_id"])["unread"] is True
    assert client.get(f"{LIST}/{card['card_id']}", headers=headers).status_code == 200
    receipt = client.post(f"{LIST}/{card['card_id']}/read", headers=headers)
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["subject"] == subject
    assert client.post(f"{LIST}/{card['card_id']}/read", headers=headers).json() == receipt.json()
    after = client.get(LIST, headers=headers).json()
    assert after["unread_count"] == before["unread_count"] - 1
    assert next(c for c in after["cards"] if c["card_id"] == card["card_id"])["unread"] is False
    untouched = client.get(LIST, headers=other).json()
    assert untouched["unread_count"] == before["unread_count"]
    assert next(c for c in untouched["cards"] if c["card_id"] == card["card_id"])["unread"] is True


def test_hold_request_is_first_write_and_does_not_change_state(client, payload):
    card = publish(client, payload)
    url = f"{LIST}/{card['card_id']}/hold-request"
    first = client.post(url)
    assert first.status_code == 200, first.text
    assert first.json()["hold_requested_by"] == "operator-a"
    assert first.json()["state"] == "accepted"
    assert client.post(url, headers={"X-Test-Subject": "operator-b"}).json() == first.json()
    assert publish(client, payload) == first.json()


def test_nonadmins_refused_on_every_browser_route(client, payload):
    card = publish(client, payload)
    for tier in ("free", "pro", "restricted", "guest", None):
        client.app.dependency_overrides[deps.require_active_tenant] = lambda: SimpleNamespace(
            subject="intruder", tier=tier)
        for method, path in (("get", LIST), ("get", f"{LIST}/{card['card_id']}"),
                             ("post", f"{LIST}/{card['card_id']}/read"),
                             ("post", f"{LIST}/{card['card_id']}/hold-request")):
            response = getattr(client, method)(path)
            assert response.status_code == 403, response.text
            assert response.json()["detail"] == "engine_changes_admin_required"
    assert platform_link.engine_change_cards_store().get_card(card["card_id"])["hold_requested_at"] is None


def test_browser_routes_require_both_admin_elevation_factors(client, payload, monkeypatch):
    import auth
    import tenancy

    card = publish(client, payload)
    client.app.dependency_overrides.pop(deps.require_active_tenant)
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    subject = "verified-admin-" + uuid.uuid4().hex
    claims = {"sub": subject, auth.claim_ns() + "tenant_id": "tenant-a"}
    monkeypatch.setattr(auth, "verify_platform_token", lambda authorization: claims)
    monkeypatch.setattr(deps, "resolve_active_platform_tenant_authority", lambda sub: ("tenant-a", "pro"))
    monkeypatch.setattr(tenancy, "get_store", lambda: SimpleNamespace(resolve_workspace=lambda tid: None))
    routes = (("get", LIST), ("get", f"{LIST}/{card['card_id']}"),
              ("post", f"{LIST}/{card['card_id']}/read"),
              ("post", f"{LIST}/{card['card_id']}/hold-request"))
    for claim_tier, allowlisted in (("pro", False), ("admin", False),
                                    ("pro", True), ("admin", True)):
        claims[auth.claim_ns() + "tier"] = claim_tier
        monkeypatch.setenv("LEAF_PLATFORM_ADMIN_SUBJECTS", subject if allowlisted else "")
        allowed = claim_tier == "admin" and allowlisted
        for method, path in routes:
            response = getattr(client, method)(path, headers={"X-Test-Subject": "spoofed-subject"})
            assert response.status_code == (200 if allowed else 403), response.text
            if not allowed:
                assert response.json()["detail"] == "engine_changes_admin_required"
            elif path.endswith("/read"):
                assert response.json()["subject"] == subject
            elif path.endswith("/hold-request"):
                assert response.json()["hold_requested_by"] == subject
        if not allowed:
            stored = platform_link.engine_change_cards_store().get_card(card["card_id"])
            assert stored["hold_requested_at"] is None
            with platform_link.platform_db().cursor() as cur:
                cur.execute("SELECT count(*) AS n FROM engine_change_card_reads WHERE card_id = %s", (card["card_id"],))
                assert cur.fetchone()["n"] == 0


def test_unauthenticated_refused_on_every_browser_route(client, monkeypatch):
    client.app.dependency_overrides.pop(deps.require_active_tenant)
    card_id = str(uuid.uuid4())
    for mode, status in (("0", 403), ("1", 401)):
        monkeypatch.setenv("LEAF_AUTH_LIVE", mode)
        for method, path in (("get", LIST), ("get", f"{LIST}/{card_id}"),
                             ("post", f"{LIST}/{card_id}/read"),
                             ("post", f"{LIST}/{card_id}/hold-request")):
            response = getattr(client, method)(path)
            assert response.status_code == status, response.text


def test_missing_database_returns_503_on_every_route(client, payload, monkeypatch):
    def unavailable():
        raise RuntimeError("DATABASE_URL is not configured")
    monkeypatch.setattr(platform_link.platform_db(), "get_database_url", unavailable)
    card_id = str(uuid.uuid4())
    for method, path in (("get", LIST), ("get", f"{LIST}/{card_id}"),
                         ("post", f"{LIST}/{card_id}/read"), ("post", f"{LIST}/{card_id}/hold-request")):
        response = getattr(client, method)(path)
        assert response.status_code == 503, response.text
        assert "database_unavailable" in response.text
    assert client.post(INGEST, json=payload, headers=SECRET).status_code == 503


def test_database_failure_returns_503_without_internal_details(client, monkeypatch):
    def unavailable(*args, **kwargs):
        raise RuntimeError("private database credential")
    monkeypatch.setattr(platform_link.engine_change_cards_store(), "list_cards", unavailable)
    response = client.get(LIST)
    assert response.status_code == 503, response.text
    assert "private" not in response.text


def test_unknown_cards_are_404(client):
    url = f"{LIST}/{uuid.uuid4()}"
    for method, suffix in (("get", ""), ("post", "/read"), ("post", "/hold-request")):
        response = getattr(client, method)(url + suffix)
        assert response.status_code == 404, response.text


def test_list_limit_and_cursor(client, payload):
    first = publish(client, payload)
    second = publish(client, {**payload, "operation_id": str(uuid.uuid4())})
    response = client.get(LIST, params={"limit": 1})
    assert response.status_code == 200, response.text
    assert response.json()["cards"][0]["card_id"] == second["card_id"]
    page = client.get(LIST, params={"limit": 1, "before": response.json()["next_cursor"]}).json()
    assert page["cards"][0]["card_id"] == first["card_id"]
    for params in ({"limit": 101}, {"limit": 0}, {"before": "invalid"}, {"before": str(uuid.uuid4())}):
        assert client.get(LIST, params=params).status_code == 422


def test_publisher_cannot_supply_arbitrary_fields_or_digest(client, payload):
    for extras in ({"hold_requested_by": "publisher"}, {"card_id": str(uuid.uuid4())},
                   {"payload_sha256": "0" * 64}, {"state": "unknown"}):
        assert client.post(INGEST, json={**payload, **extras}, headers=SECRET).status_code == 422


def test_body_is_bounded_before_json_parsing(client):
    response = client.post(INGEST, content=b"x" * (128 * 1024 + 1), headers=SECRET)
    assert response.status_code == 413, response.text
    for body in (b"{", b"[]", b"{}"):
        assert client.post(INGEST, content=body, headers=SECRET).status_code == 422


def test_browser_has_no_card_create_or_edit_route(client, payload):
    card = publish(client, payload)
    assert client.post(LIST, json=payload).status_code == 405
    assert client.put(f"{LIST}/{card['card_id']}", json=payload).status_code == 405
    assert client.delete(f"{LIST}/{card['card_id']}").status_code == 405


def test_deployment_identity_cannot_be_rewritten(client, payload):
    payload.update(state="live", deployment_identity="staging:original")
    card = publish(client, payload)
    response = client.post(INGEST, json={**payload, "state": "reverted", "deployment_identity": "staging:other"}, headers=SECRET)
    assert response.status_code == 409, response.text
    assert client.get(f"{LIST}/{card['card_id']}").json() == card
