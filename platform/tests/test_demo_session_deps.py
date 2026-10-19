"""Platform demo adapter: identity only, with no binding or route grants."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import uuid

from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest
from starlette.requests import Request

from leaf_platform import deps


def bomb(*args, **kwargs):
    pytest.fail("a demo credential reached account, binding, or write authority")


@pytest.fixture
def identity(monkeypatch):
    demo = deps._demo_sessions()
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_DEMO_LIVE_ENABLED", "1")
    monkeypatch.setenv("LEAF_DEMO_SESSION_SECRET", "platform-test-signing")
    now = datetime(2031, 2, 3, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(demo, "_now", lambda: now)
    record_id = uuid.uuid4()
    row = {"id": record_id, "subject": f"demo:{record_id}",
           "tenant_id": f"demo-{record_id}", "org_id": uuid.uuid4(),
           "created_at": now, "expires_at": now + timedelta(minutes=60),
           "revoked_at": None, "client_ip_hash": "0" * 64}
    conn = SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchone=lambda: row))
    monkeypatch.setattr(demo, "_db", lambda: SimpleNamespace(
        run_transaction=lambda operation, **kwargs: operation(conn)))
    expiry = int(row["expires_at"].timestamp())
    token = f"{record_id}.{expiry}.{demo._signature(record_id, expiry, b'platform-test-signing')}"
    return demo, row, token


def make_request(headers):
    return Request({"type": "http", "method": "POST", "scheme": "http",
                    "path": "/api/projects", "query_string": b"",
                    "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()]})


def test_adapter_resolves_record_org_only_and_never_a_binding(identity, monkeypatch):
    demo, row, token = identity
    monkeypatch.setattr(deps, "_verified_identity", bomb)
    assert deps.resolve_demo_org(token) == row["org_id"]
    assert deps.get_org_id(x_org_id=str(uuid.uuid4()), authorization=None,
                           x_demo_session=token) == row["org_id"]
    assert demo.verify(token).subject == row["subject"]


@pytest.mark.parametrize("dependency", ["get_write_actor", "get_write_org_id", "get_write_binding_id",
                                        "get_review_binding_id", "require_auth_when_live"])
def test_every_write_review_and_binding_dependency_denies(identity, monkeypatch, dependency):
    _, _, token = identity
    monkeypatch.setattr(deps, "_verified_identity", bomb)
    with pytest.raises(HTTPException) as error:
        getattr(deps, dependency)(authorization=None, x_demo_session=token)
    assert error.value.status_code == 403


@pytest.mark.parametrize("dependency", ["get_org_id", "get_write_actor", "get_write_org_id",
    "get_write_binding_id", "get_review_binding_id", "require_auth_when_live"])
def test_route_dependency_denies_before_handler_side_effects(identity, dependency):
    _, _, token = identity
    app = FastAPI()
    @app.post("/api/projects")
    def endpoint(actor=Depends(getattr(deps, dependency))):
        bomb()
    response = TestClient(app).post("/api/projects", headers={"x-demo-session": token})
    assert response.status_code == 403


@pytest.mark.parametrize("live", ["0", "1"])
@pytest.mark.parametrize("dependency", ["get_org_id", "get_write_actor", "get_write_org_id",
    "get_write_binding_id", "get_review_binding_id", "require_auth_when_live"])
def test_disabled_rejects_without_postgres(identity, monkeypatch, live, dependency):
    demo, _, token = identity
    monkeypatch.setenv("LEAF_AUTH_LIVE", live)
    monkeypatch.delenv("LEAF_DEMO_LIVE_ENABLED")
    monkeypatch.setattr(demo, "_db", bomb)
    with pytest.raises(HTTPException) as error:
        getattr(deps, dependency)(authorization=None, x_demo_session=token)
    assert error.value.status_code == 401


@pytest.mark.parametrize("live", ["0", "1"])
@pytest.mark.parametrize("authorization", [None, "", "Basic malformed", "Bearer invalid", "Bearer valid"])
@pytest.mark.parametrize("guest", [False, True])
def test_platform_precedence(identity, monkeypatch, live, authorization, guest):
    demo, row, token = identity
    monkeypatch.setenv("LEAF_AUTH_LIVE", live)
    if live != "1" or authorization is not None or guest:
        monkeypatch.setattr(demo, "_db", bomb)
    seen = []
    account_org = uuid.uuid4()

    def account(value):
        seen.append(value)
        if value != "Bearer valid":
            raise HTTPException(401, "invalid bearer")
        return SimpleNamespace(platform_tenant_id=account_org)

    monkeypatch.setattr(deps, "_verified_identity", account)
    headers = {"x-demo-session": token}
    if authorization is not None:
        headers["authorization"] = authorization
    if guest:
        headers["x-guest-session"] = "guest-token"
    if live == "1" and authorization == "Bearer valid":
        assert deps.get_org_id(authorization=authorization, x_demo_session=token,
                               request=make_request(headers)) == account_org
    else:
        expected = ((400 if guest else 403) if live == "1" and authorization is None else 401)
        with pytest.raises(HTTPException) as error:
            deps.get_org_id(authorization=authorization, x_demo_session=token,
                            request=make_request(headers))
        assert error.value.status_code == expected
    assert seen == ([authorization] if live == "1" and authorization is not None else [])


@pytest.mark.parametrize("live", ["0", "1"])
def test_dispatch_leg_rejects_demo_without_lookup(identity, monkeypatch, live):
    demo, _, token = identity
    monkeypatch.setenv("LEAF_AUTH_LIVE", live)
    monkeypatch.setattr(demo, "_db", bomb)
    with pytest.raises(HTTPException) as error:
        deps.get_org_id(authorization=None, x_demo_session=token,
                        request=make_request({"x-demo-session": token, "x-dispatch-secret": "secret"}))
    assert error.value.status_code == 401


def test_revocation_is_rechecked_by_adapter(identity):
    _, row, token = identity
    assert deps.resolve_demo_org(token) == row["org_id"]
    row["revoked_at"] = row["created_at"]
    with pytest.raises(HTTPException) as error:
        deps.resolve_demo_org(token)
    assert error.value.status_code == 401
