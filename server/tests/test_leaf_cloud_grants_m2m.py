"""Staging machine grants: tenant boundary, bounded minting, and process cache."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
from unittest.mock import Mock

import pytest

import leaf_cloud_grants as grants


CLIENT_ID = "private-client-id"
SECRET = "private-client-secret"
TOKEN = "private-access-token"


@pytest.fixture(autouse=True)
def staging(monkeypatch):
    for name, value in {
        "LEAF_CLOUD_GRANT_SOURCE": "m2m-staging",
        "LEAF_RUNTIME_ENV": "staging",
        "LEAF_CLOUD_M2M_GRANTS": '{"g1":"tenant-a","g2":"tenant-b"}',
        "LEAF_CLOUD_M2M_CLIENT_ID": CLIENT_ID,
        "LEAF_CLOUD_M2M_CLIENT_SECRET": SECRET,
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("LEAF_CLOUD_GRANTS_FILE", raising=False)
    grants.reset_m2m_grant_cache()
    yield
    grants.reset_m2m_grant_cache()


@pytest.fixture
def transport(monkeypatch):
    response = Mock(status=200)
    response.read1.side_effect = lambda size: body_read(size)
    body = bytearray(json.dumps({"access_token": TOKEN, "token_type": "Bearer",
                                "expires_in": 3600}).encode())

    def body_read(size):
        chunk = bytes(body[:size])
        del body[:size]
        return chunk

    connection = Mock()
    connection.getresponse.return_value = response
    factory = Mock(return_value=connection)
    monkeypatch.setattr(grants.http.client, "HTTPSConnection", factory)

    def set_body(value):
        body[:] = value if isinstance(value, bytes) else json.dumps(value).encode()

    return factory, connection, response, set_body


def refused(call=None, classification="cloud_auth_missing", status=401):
    with pytest.raises(grants.CloudError) as caught:
        (call or (lambda: grants.resolve_grant("g1", "tenant-a")))()
    assert caught.value.classification == classification
    assert caught.value.status == status
    for private in (CLIENT_ID, SECRET, TOKEN):
        assert private not in str(caught.value)
        assert private not in repr(caught.value)
    return caught.value


@pytest.mark.parametrize("source", [None, ""])
def test_default_source_keeps_file_behavior(monkeypatch, tmp_path, source):
    if source is None:
        monkeypatch.delenv("LEAF_CLOUD_GRANT_SOURCE")
    else:
        monkeypatch.setenv("LEAF_CLOUD_GRANT_SOURCE", source)
    path = tmp_path / "grants.json"
    path.write_text(json.dumps({"g1": {"tenant_id": "tenant-a", "access_token": TOKEN,
        "audience": "https://api.leafdesign.ai", "expires_at": 2000000000}}))
    monkeypatch.setattr(grants.time, "time", lambda: 1900000000)
    monkeypatch.setenv("LEAF_CLOUD_GRANTS_FILE", str(path))
    assert grants.resolve_grant("g1", "tenant-a").access_token == TOKEN


@pytest.mark.parametrize("name,value", [
    ("LEAF_CLOUD_GRANT_SOURCE", "unknown"),
    ("LEAF_CLOUD_GRANT_SOURCE", "M2M-STAGING"),
    ("LEAF_RUNTIME_ENV", "production"), ("LEAF_RUNTIME_ENV", "Staging"),
    ("LEAF_RUNTIME_ENV", ""),
])
def test_source_and_environment_fail_closed(monkeypatch, transport, name, value):
    monkeypatch.setenv(name, value)
    refused()
    transport[0].assert_not_called()


@pytest.mark.parametrize("reference", [None, "", "../g1", "g 1", "a" * 65, 1])
def test_reference_is_checked_first(monkeypatch, transport, reference):
    monkeypatch.setenv("LEAF_CLOUD_GRANT_SOURCE", "unknown")
    refused(lambda: grants.resolve_grant(reference, "tenant-a"))
    transport[0].assert_not_called()


def test_absent_reference_and_tenant_mismatch(transport):
    refused(lambda: grants.resolve_grant("absent", "tenant-a"))
    refused(lambda: grants.resolve_grant("g1", "tenant-b"),
            "cloud_tenant_unauthorized", 403)
    transport[0].assert_not_called()


@pytest.mark.parametrize("raw", [
    "", "{", "[]", "null", '{"g1":null}', '{"g1":1}', '{"g1":""}',
    json.dumps({"g1": "a" * 129}), json.dumps({"bad/key": "tenant-a"}),
    json.dumps({"a" * 65: "tenant-a"}),
    json.dumps({f"g{i}": "tenant-a" for i in range(17)}),
    " " * 4097, json.dumps({"g1": "tenant-a"}) + " " * 4096,
    '{"g1":"tenant-a"}' + " " * 4080 + "é",
])
def test_malformed_allowlist(monkeypatch, transport, raw):
    monkeypatch.setenv("LEAF_CLOUD_M2M_GRANTS", raw)
    refused()
    transport[0].assert_not_called()


@pytest.mark.parametrize("name", ["LEAF_CLOUD_M2M_CLIENT_ID", "LEAF_CLOUD_M2M_CLIENT_SECRET"])
@pytest.mark.parametrize("value", [None, "", "has space", "tab\t", "newline\n", "a" * 513])
def test_invalid_credentials(monkeypatch, transport, name, value):
    if value is None:
        monkeypatch.delenv(name)
    else:
        monkeypatch.setenv(name, value)
    refused()
    transport[0].assert_not_called()


def test_mint_request_and_private_return(monkeypatch, transport):
    monkeypatch.setenv("LEAF_CLOUD_GRANTS_FILE", "/ignored/file")
    result = grants.resolve_grant("g1", "tenant-a")
    assert result == grants.CloudGrant("tenant-a", TOKEN)
    transport[0].assert_called_once_with("leafautomation.us.auth0.com", timeout=10)
    args, kwargs = transport[1].request.call_args
    assert args == ("POST", "/oauth/token")
    assert json.loads(kwargs["body"]) == {
        "grant_type": "client_credentials", "client_id": CLIENT_ID,
        "client_secret": SECRET, "audience": "https://api.leafdesign.ai"}
    assert kwargs["headers"] == {"Content-Type": "application/json"}
    for private in (CLIENT_ID, SECRET, TOKEN):
        assert private not in repr(result)
        assert private not in repr(grants._m2m_cache)
    transport[1].close.assert_called_once()


@pytest.mark.parametrize("patch", [
    {"access_token": None}, {"access_token": ""}, {"access_token": "bad token"},
    {"access_token": "bad\n"}, {"access_token": "x" * 16385},
    {"token_type": "Basic"}, {"token_type": None},
    {"expires_in": 59}, {"expires_in": 2592001}, {"expires_in": 3600.0},
    {"expires_in": "3600"}, {"expires_in": True}, {"expires_in": None},
])
def test_invalid_mint_fields(transport, patch):
    payload = {"access_token": TOKEN, "token_type": "Bearer", "expires_in": 3600}
    payload.update(patch)
    transport[3](payload)
    refused()


@pytest.mark.parametrize("body", [b"{", b"[]", b"null", b"{}", b"\xff", b"x" * 65537])
def test_bad_response_body(transport, body):
    transport[3](body)
    refused()
    assert grants._m2m_cache is None


@pytest.mark.parametrize("status", [201, 301, 400, 401, 500])
def test_non_200(transport, status):
    transport[2].status = status
    refused()
    transport[2].read1.assert_not_called()


@pytest.mark.parametrize("stage", ["connect", "request", "getresponse", "read1"])
@pytest.mark.parametrize("error", [TimeoutError, OSError])
def test_transport_failures_are_private(transport, stage, error):
    target = transport[2] if stage == "read1" else transport[1]
    getattr(target, stage).side_effect = error(SECRET + TOKEN + CLIENT_ID)
    refused()
    assert grants._m2m_cache is None
    transport[1].close.assert_called_once()


@pytest.mark.parametrize("expires_in", [60, 2592000])
def test_valid_mint_limits(transport, expires_in):
    transport[3]({"access_token": "x" * 16384, "token_type": "bEaReR",
                  "expires_in": expires_in})
    assert len(grants.resolve_grant("g1", "tenant-a").access_token) == 16384


def test_cache_reuse_and_refresh_boundary(monkeypatch, transport):
    now = [1000.0]
    monkeypatch.setattr(grants.time, "monotonic", lambda: now[0])
    assert grants.resolve_grant("g1", "tenant-a").access_token == TOKEN
    now[0] = 4299.0
    assert grants.resolve_grant("g2", "tenant-b").access_token == TOKEN
    assert transport[0].call_count == 1
    now[0] = 4300.0
    transport[3]({"access_token": "refreshed", "token_type": "Bearer", "expires_in": 3600})
    assert grants.resolve_grant("g1", "tenant-a").access_token == "refreshed"
    assert transport[0].call_count == 2


def test_failed_mint_is_not_cached(transport):
    transport[2].status = 500
    refused()
    transport[2].status = 200
    assert grants.resolve_grant("g1", "tenant-a").access_token == TOKEN
    assert transport[0].call_count == 2


def test_reset_and_client_change_invalidate(monkeypatch, transport):
    payload = {"access_token": TOKEN, "token_type": "Bearer", "expires_in": 3600}
    grants.resolve_grant("g1", "tenant-a")
    monkeypatch.setenv("LEAF_CLOUD_M2M_CLIENT_ID", "another-client")
    transport[3](payload)
    grants.resolve_grant("g1", "tenant-a")
    grants.reset_m2m_grant_cache()
    transport[3](payload)
    grants.resolve_grant("g1", "tenant-a")
    assert transport[0].call_count == 3


def test_concurrent_callers_mint_once(transport):
    start = threading.Barrier(8)

    def resolve(_):
        start.wait(timeout=5)
        return grants.resolve_grant("g1", "tenant-a")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(resolve, range(8)))
    assert all(result.access_token == TOKEN for result in results)
    assert transport[0].call_count == 1


def test_total_timeout_budget(monkeypatch, transport):
    now = [1000.0]
    monkeypatch.setattr(grants.time, "monotonic", lambda: now[0])
    transport[1].connect.side_effect = lambda: now.__setitem__(0, 1004.0)
    transport[1].request.side_effect = lambda *a, **kw: now.__setitem__(0, 1008.0)
    transport[1].getresponse.side_effect = lambda: now.__setitem__(0, 1011.0) or transport[2]
    refused()
    assert [call.args[0] for call in transport[1].sock.settimeout.call_args_list] == [6.0, 2.0]
    transport[2].read1.assert_not_called()
