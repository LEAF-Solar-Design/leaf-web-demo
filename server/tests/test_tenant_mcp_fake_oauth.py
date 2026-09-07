"""Slice 8b: the app-hosted AS substitutes only for the remote OAuth peer."""
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from fastapi.testclient import TestClient

import tenant_mcp_store
from routers import tenant_mcp
from test_tenant_mcp import _app_with_capability_override, _seed_record, fake_as


PREFIX = "/api/tenant/mcp-servers"
BASE = "http://localhost"
FAKE = BASE + PREFIX + "/_fake-oauth"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_TENANT_MCP_DIR", str(tmp_path))
    for name in ("TENANT_MCP_FAKE_OAUTH", "LEAF_ENV", "LEAF_RUNTIME_ENV", "LEAF_APP_PUBLIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    tenant_mcp._FAKE_CLIENTS.clear()
    tenant_mcp._FAKE_CODES.clear()


@pytest.fixture
def app_client():
    with TestClient(_app_with_capability_override(), base_url=BASE) as client:
        yield client


@pytest.mark.parametrize("method,path", [
    ("GET", "/.well-known/oauth-protected-resource"),
    ("GET", "/.well-known/oauth-authorization-server"),
    ("POST", "/register"), ("GET", "/authorize"), ("POST", "/token"),
])
def test_fake_off_by_default(app_client, method, path):
    assert app_client.request(method, FAKE + path, json={}).status_code == 404


@pytest.mark.parametrize("marker", ["LEAF_ENV", "LEAF_RUNTIME_ENV"])
@pytest.mark.parametrize("posture", ["production", "prod", " Production ", "staging", "unknown"])
def test_fake_refused_outside_local(app_client, monkeypatch, marker, posture):
    monkeypatch.setenv("TENANT_MCP_FAKE_OAUTH", "1")
    monkeypatch.setenv(marker, posture)
    for method, path in [("GET", "/.well-known/oauth-protected-resource"),
                         ("GET", "/.well-known/oauth-authorization-server"),
                         ("POST", "/register"), ("GET", "/authorize"), ("POST", "/token")]:
        assert app_client.request(method, FAKE + path, json={}).status_code == 403


def _shape(value):
    return {key: _shape(item) for key, item in value.items()} if isinstance(value, dict) else type(value)


def test_fake_flow_matches_real_store_shape(app_client, monkeypatch, fake_as):
    # Existing real-path fixture exercises requests over loopback. Select the
    # same public-client/no-revocation AS capabilities as the app-hosted peer.
    base, cfg = fake_as
    cfg.dcr_body.pop("client_secret")
    cfg.as_body.pop("revocation_endpoint")
    real = _seed_record("demo-tenant", base + "/mcp")
    response = app_client.post(f"{PREFIX}/{real['id']}/connect")
    assert response.status_code == 200
    state = parse_qs(urlsplit(response.json()["authorize_url"]).query)["state"][0]
    assert app_client.get(PREFIX + "/callback", params={"state": state, "code": "fixture-code"}).status_code == 200
    real_record = tenant_mcp_store.get_record("demo-tenant", real["id"])

    monkeypatch.setenv("TENANT_MCP_FAKE_OAUTH", "1")
    exchanges = []

    # Route requests' app-local HTTP into TestClient. Keep discovery, DCR,
    # bounded response decoding, PKCE and callback persistence unchanged.
    def local_request(method, url, **kwargs):
        assert url.startswith(FAKE + "/"), f"unexpected network request: {url}"
        exchanges.append((method, url))
        response = app_client.request(method, url, **{
            key: value for key, value in kwargs.items() if key in ("json", "data")
        })
        result = requests.Response()
        result.status_code = response.status_code
        result._content = response.content
        result._content_consumed = True
        return result

    monkeypatch.setattr(tenant_mcp.requests, "get", lambda url, **kw: local_request("GET", url, **kw))
    monkeypatch.setattr(tenant_mcp.requests, "post", lambda url, **kw: local_request("POST", url, **kw))
    registered = app_client.post(PREFIX, json={"url": FAKE, "label": "Local service"})
    assert registered.status_code == 200
    server_id = registered.json()["id"]
    connected = app_client.post(f"{PREFIX}/{server_id}/connect")
    assert connected.status_code == 200
    authorize_url = connected.json()["authorize_url"]
    hop = app_client.get(authorize_url, follow_redirects=False)
    assert hop.status_code == 302
    callback = hop.headers["location"]
    query = parse_qs(urlsplit(callback).query)
    assert query["state"] == parse_qs(urlsplit(authorize_url).query)["state"]
    code = query["code"][0]
    entry = dict(tenant_mcp._FAKE_CODES[code])
    linked = app_client.get(callback)
    assert linked.status_code == 200 and linked.json()["linked"] is True
    stored = tenant_mcp_store.get_record("demo-tenant", server_id)
    assert stored["state"] == "connected"
    assert _shape(stored) == _shape(real_record)
    assert stored["oauth"]["access_token"] not in linked.text
    assert app_client.get(callback).status_code == 400
    assert app_client.post(FAKE + "/token", data={"code": code}).status_code == 400
    assert len(exchanges) == 4  # PR metadata, AS metadata, DCR, token

    # A fresh code cannot be redeemed with another redirect, resource or PKCE.
    second = app_client.get(authorize_url, follow_redirects=False)
    bad_code = parse_qs(urlsplit(second.headers["location"]).query)["code"][0]
    assert app_client.post(FAKE + "/token", data={
        "code": bad_code, "grant_type": "authorization_code", "code_verifier": "wrong",
        **{key: entry[key] for key in ("redirect_uri", "client_id", "resource")},
    }).status_code == 400
    assert app_client.delete(f"{PREFIX}/{server_id}").status_code == 200
    assert tenant_mcp_store.get_record("demo-tenant", server_id) is None


def test_fake_does_not_relax_other_http_urls(app_client, monkeypatch):
    monkeypatch.setenv("TENANT_MCP_FAKE_OAUTH", "1")
    for url in ("http://example.com" + PREFIX + "/_fake-oauth", "http://localhost/mcp"):
        assert app_client.post(PREFIX, json={"url": url, "label": "No"}).status_code == 422
    response = app_client.post(FAKE + "/register", json={"redirect_uris": ["https://example.com/callback"]})
    assert response.status_code == 400
