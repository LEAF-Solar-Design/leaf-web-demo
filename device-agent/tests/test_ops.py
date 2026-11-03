import json

import pytest

from leaf_agent.ops import Dispatcher, MAX_BODY, canonical_path


def proxy(config, registry, **overrides):
    return Dispatcher(config, registry).dispatch("http.proxy", {"package": "echo", "method": "GET", "path": "/api/states", **overrides})


def test_proxy_local_auth_query_and_json_only(config, registry, local_package):
    result = proxy(config, registry, query={"entity_id": "light.a &?/#"})
    assert result["status"] == "ok"
    assert result["data"]["content_type"] == "application/json"
    assert "entity_id=light.a+%26%3F%2F%23" in result["data"]["body_json"]["path"]
    assert local_package.requests[0][1]["Authorization"] == "Bearer local-only-secret"
    assert "local-only-secret" not in json.dumps(result)
    assert "authorization" not in result["data"]["body_json"]
    post = proxy(config, registry, method="POST", path="/api/services/light/turn_on", body_json={"entity_id": "light.a"})
    assert post["data"]["body_json"]["body"] == {"entity_id": "light.a"}


@pytest.mark.parametrize("op,args", [
    ("pkg.health", {}), ("pkg.uninstall", {"keep_data": True}),
    ("pkg.install", {"version": "1"}), ("pkg.upgrade", {"version": "1"}),
])
def test_package_operations_do_not_read_locked_credentials(config, registry, local_package, monkeypatch, op, args):
    reads = []

    def locked():
        reads.append(True)
        raise RuntimeError("Keychain locked")

    monkeypatch.setattr(registry.get("echo"), "auth_headers", locked)
    result = Dispatcher(config, registry).dispatch(op, {"package": "echo", **args})
    assert result["status"] == "ok" and not reads
    assert proxy(config, registry)["error"]["code"] == "upstream_error"
    assert reads == [True] and not local_package.requests


@pytest.mark.parametrize("path", ["//evil/api/states", "http://evil/api/states", "/api/statesX", "/api/states/../config", "/api/%73tates", "/api/%2573tates", "/api/%ZZ", "/api/states?x=1", "/api/states#x", "/api/states\\evil", "/api//states", "/api/states/./x", "/api/states/\x00", "/api/states/\x7f", "/api/states/ a"])
def test_path_denied_before_network(config, registry, local_package, path):
    assert proxy(config, registry, path=path)["error"]["code"] == "path_denied"
    assert not local_package.requests


@pytest.mark.parametrize("method", ["HEAD", "TRACE", "get", "DELETE", "OPTIONS"])
def test_methods_are_independently_allowlisted(config, registry, local_package, method):
    assert proxy(config, registry, method=method)["error"]["code"] == "path_denied"
    assert not local_package.requests


def test_proxy_rejects_server_headers_ports_and_invalid_queries(config, registry, local_package):
    for extra in ({"port": 80}, {"host": "evil"}, {"headers": {"Authorization": "remote-token"}}, {"query": {"": "x"}}, {"query": {"x": 3}}):
        assert proxy(config, registry, **extra)["error"]["code"] == "invalid_args"
    assert not local_package.requests


def test_prefix_requires_segment_boundary(config, registry, local_package):
    assert proxy(config, registry, path="/api/states/light.a")["status"] == "ok"
    assert proxy(config, registry, method="POST", path="/api/services/light")["error"]["code"] == "path_denied"
    assert proxy(config, registry, method="POST", path="/api/services/lightX/a")["error"]["code"] == "path_denied"


@pytest.mark.parametrize("raw,content_type", [(b"x" * (MAX_BODY + 1), "application/json"), (b"<html/>", "text/html"), (b"NaN", "application/json"), (b"{", "application/json"), (b'"\xff"', "application/json")], ids=["oversize", "non_json", "nan", "invalid_json", "invalid_utf8"])
def test_oversize_and_non_json_response(config, registry, local_package, raw, content_type):
    local_package.raw, local_package.content_type = raw, content_type
    assert proxy(config, registry)["error"]["code"] == "upstream_error"


def test_response_cap_exact_boundary_and_upstream_http_errors(config, registry, local_package):
    local_package.raw = b'"' + b"x" * (MAX_BODY - 2) + b'"'
    assert proxy(config, registry)["status"] == "ok"
    local_package.status, local_package.raw = 422, b'{"error":"invalid"}'
    result = proxy(config, registry)
    assert result["status"] == "ok" and result["data"]["status"] == 422
    local_package.status, local_package.raw = 204, b""
    assert proxy(config, registry)["data"] == {"status": 204, "content_type": "application/json"}


def test_redirects_never_followed(config, registry, local_package):
    local_package.status, local_package.redirect = 302, "http://127.0.0.1:1/stolen"
    assert proxy(config, registry)["error"]["code"] == "upstream_error"
    assert len(local_package.requests) == 1


def test_request_cap_and_closed_ops(config, registry, local_package):
    assert proxy(config, registry, body_json="x" * MAX_BODY)["error"]["code"] == "invalid_args"
    dispatcher = Dispatcher(config, registry)
    for op in ("shell.exec", "pkg.execute", "ha.arbitrary", "agent.resume", "agent.update"):
        assert dispatcher.dispatch(op, {})["error"]["code"] == "unsupported_op"
    assert dispatcher.dispatch("pkg.health", {"package": "missing"})["error"]["code"] == "package_unknown"
    assert dispatcher.dispatch("pkg.install", {"package": "echo", "version": "http://evil/code"})["error"]["code"] == "version_unknown"
    assert not local_package.requests


def test_pause_and_local_resume(config, registry, local_package):
    dispatcher = Dispatcher(config, registry)
    assert dispatcher.dispatch("agent.pause", {}) == {"status": "ok"}
    assert config.disabled
    assert dispatcher.dispatch("shell.exec", {})["error"]["code"] == "paused"
    config.resume()
    assert not config.disabled
    assert canonical_path("/")


def test_ha_operations_implicitly_select_registered_homeassistant(config, registry, local_package, monkeypatch):
    from conftest import Echo
    from leaf_agent.config import write_json
    catalog = list(config.catalog().values())
    catalog[0]["package"] = "homeassistant"
    write_json(config.home / "catalog.json", catalog)
    handler = Echo(config.home)
    def unexpected_auth():
        raise AssertionError("Dispatcher must leave HA authentication to the handler")
    monkeypatch.setattr(handler, "auth_headers", unexpected_auth)
    registry.register("homeassistant", handler)
    dispatcher = Dispatcher(config, registry)
    assert dispatcher.dispatch("ha.onboard", {})["status"] == "ok"
    assert dispatcher.dispatch("ha.flow.list", {})["status"] == "ok"
    assert dispatcher.dispatch("ha.flow.start", {"handler": "hue", "data": {}})["status"] == "ok"
    assert dispatcher.dispatch("ha.flow.step", {"flow_id": "local-flow", "data": {}})["status"] == "ok"
    assert dispatcher.dispatch("ha.flow.start", {"handler": "arbitrary"})["error"]["code"] == "invalid_args"
    assert dispatcher.dispatch("ha.flow.step", {"flow_id": "", "data": {}})["error"]["code"] == "invalid_args"
    assert dispatcher.dispatch("ha.onboard", {"host": "evil"})["error"]["code"] == "invalid_args"
