"""Tenant MCP server registry (standardization slice 8b): substring-leak
invariants (same posture as test_mcp_status.py — never a token, "operator",
or upstream detail in any response body) plus the named fail-closed connect
states. Every OAuth exchange runs against a FAKE, in-process authorization
server (a loopback ThreadingHTTPServer) — never a live network call."""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

import tenant_mcp_store
from envelopes import install_error_handlers
from routers import tenant_mcp

# Obviously-fake fixture values — none match a real-secret detector's shape
# (no sk-ant-, ghp_, AKIA, xox, eyJ. prefixes).
FAKE_TOKEN = "fixture-mcp-access-token-not-real-000111"
FAKE_CLIENT_ID = "fixture-client-id-000111"
FAKE_CLIENT_SECRET = "fixture-client-secret-not-real-222333"

_TENANT = "demo-tenant"


def _assert_no_leak(text: str) -> None:
    assert FAKE_TOKEN not in text
    assert FAKE_CLIENT_SECRET not in text
    assert "operator" not in text


# --------------------------------------------------------------------------- #
# app / client helpers
# --------------------------------------------------------------------------- #
def _app_with_capability_override() -> FastAPI:
    app = FastAPI()
    app.include_router(tenant_mcp.router)
    install_error_handlers(app)  # the same allowlisted-validation-error posture as server/app.py
    app.dependency_overrides[tenant_mcp._require_link_service] = lambda: _TENANT
    return app


def client() -> TestClient:
    return TestClient(_app_with_capability_override(), raise_server_exceptions=False)


def client_real_gate() -> TestClient:
    """No dependency override: exercises the REAL owner + capability chain."""
    app = FastAPI()
    app.include_router(tenant_mcp.router)
    install_error_handlers(app)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _store_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_TENANT_MCP_DIR", str(tmp_path))


# --------------------------------------------------------------------------- #
# fake in-process authorization server
# --------------------------------------------------------------------------- #
class _FakeAsConfig:
    def __init__(self, base: str):
        self.pr_body = {"authorization_servers": [base]}
        self.pr_oversized = False
        self.as_body = {
            "issuer": base,
            "registration_endpoint": base + "/register",
            "authorization_endpoint": base + "/authorize",
            "token_endpoint": base + "/token",
            "revocation_endpoint": base + "/revoke",
        }
        self.dcr_status = 201
        self.dcr_body = {"client_id": FAKE_CLIENT_ID, "client_secret": FAKE_CLIENT_SECRET}
        self.token_status = 200
        self.token_body = {"access_token": FAKE_TOKEN, "token_type": "bearer", "expires_in": 3600}
        self.revoked = False


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D401 - silence test server logging
        pass

    def _send(self, status: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        cfg: _FakeAsConfig = self.server.config  # type: ignore[attr-defined]
        if self.path == "/.well-known/oauth-protected-resource":
            if cfg.pr_oversized:
                body = b"x" * (tenant_mcp._MAX_RESPONSE_BYTES + 4096)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self._send(200, cfg.pr_body)
        elif self.path == "/.well-known/oauth-authorization-server":
            self._send(200, cfg.as_body)
        else:
            self._send(404, {"error": "not_found"})

    def do_POST(self):  # noqa: N802
        cfg: _FakeAsConfig = self.server.config  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length", 0) or 0)
        self.rfile.read(length)
        if self.path == "/register":
            self._send(cfg.dcr_status, cfg.dcr_body)
        elif self.path == "/token":
            self._send(cfg.token_status, cfg.token_body)
        elif self.path == "/revoke":
            cfg.revoked = True
            self._send(200, {})
        else:
            self._send(404, {"error": "not_found"})


@pytest.fixture
def fake_as():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    base = f"http://127.0.0.1:{server.server_port}"
    cfg = _FakeAsConfig(base)
    server.config = cfg  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield base, cfg
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _seed_record(tenant_id: str, url: str, label: str = "fixture server") -> dict:
    """Insert a record directly through the store (bypasses the router's
    https-only wire validation) so connect-flow tests can point at the fake
    loopback (http) authorization server."""
    host = urlsplit(url).hostname or ""
    return tenant_mcp_store.register(tenant_id, url=url, label=label, host=host)


# --------------------------------------------------------------------------- #
# register: wire validation (422 at the edge, no network)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("url", [
    "http://mcp.example.com/sse",              # not https
    "https://user:pass@mcp.example.com/sse",   # userinfo
    "https://mcp.example.com/sse?x=1",         # query
    "https://mcp.example.com/sse#frag",        # fragment
    "https://" + "a" * 3000,                   # over length bound
])
def test_register_rejects_malformed_url(url):
    resp = client().post("/api/tenant/mcp-servers", json={"url": url, "label": "svc"})
    assert resp.status_code == 422
    _assert_no_leak(resp.text)


@pytest.mark.parametrize("label", ["", "a" * 81, "bad label!", " leadingspace"])
def test_register_rejects_malformed_label(label):
    resp = client().post(
        "/api/tenant/mcp-servers",
        json={"url": "https://mcp.example.com/sse", "label": label},
    )
    assert resp.status_code == 422


def test_register_and_list_project_only_safe_fields():
    resp = client().post(
        "/api/tenant/mcp-servers",
        json={"url": "https://mcp.example.com/sse", "label": "billing tool"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) >= {"id", "label", "host", "state", "linked_at"}
    assert body["host"] == "mcp.example.com"
    assert body["state"] == "registered"
    assert "url" not in body
    assert "oauth" not in body
    assert "token" not in body

    listed = client().get("/api/tenant/mcp-servers").json()
    assert listed["servers"] == [{
        "id": body["id"], "label": "billing tool", "host": "mcp.example.com",
        "state": "registered", "linked_at": None,
    }]


def test_register_enforces_per_tenant_cap():
    for i in range(tenant_mcp_store.MAX_SERVERS_PER_TENANT):
        tenant_mcp_store.register(_TENANT, url=f"https://mcp{i}.example.com/x",
                                   label=f"svc{i}", host=f"mcp{i}.example.com")
    resp = client().post(
        "/api/tenant/mcp-servers",
        json={"url": "https://mcpN.example.com/x", "label": "over-cap"},
    )
    assert resp.status_code == 429


# --------------------------------------------------------------------------- #
# capability gate: write routes require link_service; list/health do not
# --------------------------------------------------------------------------- #
def test_register_denied_by_default_demo_tier_lacks_link_service():
    resp = client_real_gate().post(
        "/api/tenant/mcp-servers",
        json={"url": "https://mcp.example.com/sse", "label": "svc"},
        headers={"X-Tenant-Id": _TENANT},
    )
    assert resp.status_code == 403


def test_list_and_health_do_not_require_link_service():
    tenant_mcp_store.register(_TENANT, url="https://mcp.example.com/sse",
                               label="svc", host="mcp.example.com")
    resp = client_real_gate().get(
        "/api/tenant/mcp-servers", headers={"X-Tenant-Id": _TENANT},
    )
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# connect flow: happy path through the fake AS + server-side callback capture
# --------------------------------------------------------------------------- #
def test_connect_then_callback_links_and_leaks_nothing(fake_as):
    base, cfg = fake_as
    record = _seed_record(_TENANT, base + "/mcp")

    connect_resp = client().post(f"/api/tenant/mcp-servers/{record['id']}/connect")
    assert connect_resp.status_code == 200
    authorize_url = connect_resp.json()["authorize_url"]
    _assert_no_leak(connect_resp.text)

    qs = parse_qs(urlsplit(authorize_url).query)
    assert qs["code_challenge_method"] == ["S256"]
    assert qs["resource"] == [base + "/mcp"]
    state = qs["state"][0]

    mid_list = client().get("/api/tenant/mcp-servers").json()
    assert mid_list["servers"][0]["state"] == "connecting"

    callback_resp = client().get(
        "/api/tenant/mcp-servers/callback", params={"state": state, "code": "fixture-auth-code"},
    )
    assert callback_resp.status_code == 200
    assert callback_resp.json()["linked"] is True
    _assert_no_leak(callback_resp.text)

    final = client().get("/api/tenant/mcp-servers").json()["servers"][0]
    assert final["state"] == "connected"
    assert final["linked_at"] is not None
    _assert_no_leak(json.dumps(final))

    stored = tenant_mcp_store.get_record(_TENANT, record["id"])
    assert stored["oauth"]["access_token"] == FAKE_TOKEN  # persisted, never echoed


def test_connect_unknown_server_404():
    resp = client().post("/api/tenant/mcp-servers/does-not-exist/connect")
    assert resp.status_code == 404


def test_dcr_refused_fails_closed(fake_as):
    base, cfg = fake_as
    cfg.dcr_status = 400
    record = _seed_record(_TENANT, base + "/mcp")

    resp = client().post(f"/api/tenant/mcp-servers/{record['id']}/connect")
    assert resp.status_code == 502
    assert resp.json()["error"]["error_code"] == "BROKER_UNREACHABLE"
    assert tenant_mcp_store.get_record(_TENANT, record["id"])["state"] == "error"


def test_oversized_protected_resource_metadata_fails_closed(fake_as):
    base, cfg = fake_as
    cfg.pr_oversized = True
    record = _seed_record(_TENANT, base + "/mcp")

    resp = client().post(f"/api/tenant/mcp-servers/{record['id']}/connect")
    assert resp.status_code == 502
    assert tenant_mcp_store.get_record(_TENANT, record["id"])["state"] == "error"


def test_metadata_missing_authorization_servers_fails_closed(fake_as):
    base, cfg = fake_as
    cfg.pr_body = {}
    record = _seed_record(_TENANT, base + "/mcp")

    resp = client().post(f"/api/tenant/mcp-servers/{record['id']}/connect")
    assert resp.status_code == 502
    assert tenant_mcp_store.get_record(_TENANT, record["id"])["state"] == "error"


def test_metadata_timeout_is_a_named_state(monkeypatch):
    def _raise(*args, **kwargs):
        raise requests.Timeout("boom")

    monkeypatch.setattr(tenant_mcp.requests, "get", _raise)
    with pytest.raises(tenant_mcp.McpConnectError) as excinfo:
        tenant_mcp._fetch_metadata("https://example.invalid/.well-known/oauth-protected-resource")
    assert excinfo.value.code == "timeout"


def test_callback_state_mismatch_fails_closed():
    resp = client().get(
        "/api/tenant/mcp-servers/callback", params={"state": "not-a-real-state", "code": "x"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["error_code"] == "BAD_PARAMS"


def test_callback_missing_state_fails_closed():
    resp = client().get("/api/tenant/mcp-servers/callback", params={"code": "x"})
    assert resp.status_code == 400


def test_callback_replay_of_consumed_state_fails_closed(fake_as):
    base, cfg = fake_as
    record = _seed_record(_TENANT, base + "/mcp")
    authorize_url = client().post(
        f"/api/tenant/mcp-servers/{record['id']}/connect"
    ).json()["authorize_url"]
    state = parse_qs(urlsplit(authorize_url).query)["state"][0]

    first = client().get(
        "/api/tenant/mcp-servers/callback", params={"state": state, "code": "fixture-auth-code"},
    )
    assert first.status_code == 200
    replay = client().get(
        "/api/tenant/mcp-servers/callback", params={"state": state, "code": "fixture-auth-code"},
    )
    assert replay.status_code == 400  # single-use: the state was already popped


def test_audience_mismatch_fails_closed(fake_as):
    base, cfg = fake_as
    cfg.token_body = {
        "access_token": FAKE_TOKEN, "token_type": "bearer", "expires_in": 3600,
        "aud": "https://not-the-resource.example",
    }
    record = _seed_record(_TENANT, base + "/mcp")
    authorize_url = client().post(
        f"/api/tenant/mcp-servers/{record['id']}/connect"
    ).json()["authorize_url"]
    state = parse_qs(urlsplit(authorize_url).query)["state"][0]

    resp = client().get(
        "/api/tenant/mcp-servers/callback", params={"state": state, "code": "fixture-auth-code"},
    )
    assert resp.status_code == 400
    assert tenant_mcp_store.get_record(_TENANT, record["id"])["state"] == "error"


# --------------------------------------------------------------------------- #
# health + delete
# --------------------------------------------------------------------------- #
def test_health_reports_a_state_word_never_upstream_detail(monkeypatch):
    record = tenant_mcp_store.register(_TENANT, url="https://mcp.example.com/sse",
                                        label="svc", host="mcp.example.com")

    class _Resp:
        status_code = 200

        def close(self):
            pass

    monkeypatch.setattr(tenant_mcp.requests, "head", lambda *a, **k: _Resp())
    resp = client().get(f"/api/tenant/mcp-servers/{record['id']}/health")
    assert resp.status_code == 200
    assert resp.json()["state"] == "connected"
    assert set(resp.json()) <= {"id", "state", "tenant_id", "org_id", "tier", "error", "degraded_mode"}


def test_health_unreachable_reports_error_state(monkeypatch):
    record = tenant_mcp_store.register(_TENANT, url="https://mcp.example.com/sse",
                                        label="svc", host="mcp.example.com")

    def _raise(*a, **k):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(tenant_mcp.requests, "head", _raise)
    resp = client().get(f"/api/tenant/mcp-servers/{record['id']}/health")
    assert resp.status_code == 200
    assert resp.json()["state"] == "error"


def test_health_unknown_server_404():
    resp = client().get("/api/tenant/mcp-servers/does-not-exist/health")
    assert resp.status_code == 404


def test_delete_revokes_best_effort_and_removes(fake_as):
    base, cfg = fake_as
    record = _seed_record(_TENANT, base + "/mcp")
    authorize_url = client().post(
        f"/api/tenant/mcp-servers/{record['id']}/connect"
    ).json()["authorize_url"]
    state = parse_qs(urlsplit(authorize_url).query)["state"][0]
    client().get("/api/tenant/mcp-servers/callback", params={"state": state, "code": "fixture-auth-code"})

    resp = client().delete(f"/api/tenant/mcp-servers/{record['id']}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    assert cfg.revoked is True
    assert client().get("/api/tenant/mcp-servers").json()["servers"] == []


def test_delete_missing_id_404():
    resp = client().delete("/api/tenant/mcp-servers/does-not-exist")
    assert resp.status_code == 404


def test_delete_never_reachable_revocation_endpoint_still_removes(monkeypatch):
    record = tenant_mcp_store.register(_TENANT, url="https://mcp.example.com/sse",
                                        label="svc", host="mcp.example.com")
    tenant_mcp_store.update_record(
        _TENANT, record["id"], state="connected",
        oauth={"access_token": FAKE_TOKEN, "revocation_endpoint": "https://revoke.invalid/x"},
    )

    def _raise(*a, **k):
        raise requests.ConnectionError("unreachable")

    monkeypatch.setattr(tenant_mcp.requests, "post", _raise)
    resp = client().delete(f"/api/tenant/mcp-servers/{record['id']}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True


# --------------------------------------------------------------------------- #
# store-level bounds
# --------------------------------------------------------------------------- #
def test_store_write_is_atomic_no_tmp_files_left(tmp_path):
    tenant_mcp_store.register(_TENANT, url="https://mcp.example.com/sse",
                               label="svc", host="mcp.example.com")
    leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


@pytest.mark.skipif(os.name != "posix", reason="Windows has no POSIX mode bits to assert on")
def test_store_file_is_0600_from_first_write(tmp_path):
    tenant_mcp_store.register(_TENANT, url="https://mcp.example.com/sse",
                               label="svc", host="mcp.example.com")
    path = tmp_path / f"{_TENANT}.json"
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600


def test_store_rejects_invalid_tenant_id():
    with pytest.raises(ValueError):
        tenant_mcp_store.list_records("Not A Valid Id!")


def test_pending_state_is_single_use_and_bounded():
    tenant_mcp_store.create_pending("state-1", {"tenant_id": _TENANT, "server_id": "s1"})
    assert tenant_mcp_store.pop_pending("state-1") is not None
    assert tenant_mcp_store.pop_pending("state-1") is None  # single-use


def test_never_exposes_upstream_hostname_in_url_field_of_wire_shape():
    resp = client().post(
        "/api/tenant/mcp-servers",
        json={"url": "https://user:should-not-appear@mcp.example.com/sse", "label": "svc"},
    )
    assert resp.status_code == 422
    assert "should-not-appear" not in resp.text
