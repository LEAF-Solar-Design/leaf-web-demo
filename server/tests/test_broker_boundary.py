"""Focused regressions for the app/broker APS credential and file boundary."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import broker  # noqa: E402
import broker_client  # noqa: E402
import tool_loader  # noqa: E402
from routers import session as session_router  # noqa: E402


def _ok_env(*_a, **_k):
    """A schema-valid ok envelope — stand-in for a real tool run so auth/entitlement
    tests never depend on tool-execution internals."""
    return {"ok": True, "tool": "t", "version": "1.0.0", "result": {}, "overlay": None,
            "timing_ms": 1, "cost": None, "error": None, "degraded_mode": False}


class _Response:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def test_live_session_extracts_through_broker_only(monkeypatch):
    calls = []

    def fake_post(url, *, json, headers, timeout):
        calls.append((url, json, headers, timeout))
        return _Response(200, {"intake": {"polylines": []}, "error": None,
                               "degraded_mode": False})

    monkeypatch.setattr(session_router.deps, "APS_LIVE", True)
    monkeypatch.setattr(session_router.broker_client, "broker_url", lambda: "http://broker:8140")
    monkeypatch.setattr(session_router.broker_client, "broker_headers",
                        lambda: {"X-Broker-Secret": "test-secret"})
    monkeypatch.setattr(session_router.requests, "post", fake_post)

    body = session_router.session(dwg="rooftop_demo", tenant="tenant-a")

    assert body["intake"] == {"polylines": []}
    assert calls == [("http://broker:8140/broker/extract",
                      {"tenant_id": "tenant-a", "dwg": "rooftop_demo"},
                      {"X-Broker-Secret": "test-secret"}, 600)]
    source = Path(session_router.__file__).read_text(encoding="utf-8")
    assert "get_da_client" not in source
    assert "da.client" not in source


def test_mock_session_default_dwg_serves_cached_intake_and_never_calls_broker(monkeypatch):
    """APS_LIVE=0 + the default drawing -> the unchanged cached-intake path.

    (Until §19 the offline branch ignored `dwg` entirely; a non-default name now
    reads the tenant's own store — covered by the next test. This one pins the
    byte-identical demo default.)"""
    monkeypatch.setattr(session_router.deps, "APS_LIVE", False)
    monkeypatch.setattr(session_router.deps, "load_cached_intake",
                        lambda: {"polylines": [{"layer": "Panels"}]})
    monkeypatch.setattr(
        session_router.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("mock session called broker"),
    )

    body = session_router.session(dwg="rooftop_demo", tenant="demo-tenant")
    assert body == {
        "intake": {"polylines": [{"layer": "Panels"}]},
        "error": None,
        "degraded_mode": False,
    }


def test_mock_session_non_default_dwg_reads_tenant_store_never_broker(monkeypatch):
    """APS_LIVE=0 + a NON-default drawing -> the tenant's own versioned store
    (§19: serving the cached demo intake under a user's drawing name would be
    fabricated data). Still strictly app-process-local: the broker is never
    called on any offline read."""
    import write_loop  # the module the router lazily imports inside the branch

    calls = []
    sentinel_backend = object()
    monkeypatch.setattr(session_router.deps, "APS_LIVE", False)
    monkeypatch.setattr(
        session_router.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("offline session called broker"),
    )
    monkeypatch.setattr(write_loop, "backend_for_tenant",
                        lambda tid, **kw: calls.append(("backend", tid)) or sentinel_backend)
    monkeypatch.setattr(write_loop, "ensure_demo_drawing",
                        lambda backend, tid, did: calls.append(("ensure", backend, tid, did)))
    monkeypatch.setattr(write_loop, "read_intake",
                        lambda backend, tid, did, version: (
                            calls.append(("read", backend, tid, did, version))
                            or (1, {"polylines": [{"layer": "Uploaded"}]})))

    body = session_router.session(dwg="my-upload", tenant="demo-tenant")

    assert body == {
        "intake": {"polylines": [{"layer": "Uploaded"}]},
        "error": None,
        "degraded_mode": False,
    }
    assert calls == [
        ("backend", "demo-tenant"),
        ("ensure", sentinel_backend, "demo-tenant", "my-upload"),
        ("read", sentinel_backend, "demo-tenant", "my-upload", "head"),
    ]


@pytest.mark.parametrize(
    "dwg",
    [
        "../secret",
        "..\\secret",
        "/tmp/secret",
        "C:\\tmp\\secret",
        "nested/drawing",
        "nested\\drawing",
        "rooftop_demo.dwg",
        "secret.txt.dwg",
        ".",
        "",
    ],
)
def test_live_dwg_resolver_rejects_paths_separators_and_suffixes(dwg):
    with pytest.raises(ValueError):
        broker._resolve_live_dwg(dwg)


def test_live_dwg_resolver_accepts_registered_store_name():
    resolved = broker._resolve_live_dwg("rooftop_demo")
    assert resolved == (broker.DATA_DIR / "rooftop_demo.dwg").resolve()


def test_live_dwg_resolver_rejects_symlink_even_when_target_is_inside(tmp_path, monkeypatch):
    store = tmp_path / "store"
    store.mkdir()
    target = store / "real.dwg"
    target.write_bytes(b"dwg")
    link = store / "linked.dwg"
    try:
        link.symlink_to(target)
    except OSError:
        # Windows without Developer Mode may prohibit creating test symlinks;
        # still exercise the explicit final-component symlink guard.
        link.write_bytes(b"dwg")
        original = Path.is_symlink
        monkeypatch.setattr(
            Path,
            "is_symlink",
            lambda self: True if self == link else original(self),
        )
    monkeypatch.setattr(broker, "DATA_DIR", store)
    with pytest.raises(ValueError, match="symlink"):
        broker._resolve_live_dwg("linked")


def test_broker_extract_rejects_traversal_before_loading_da(monkeypatch):
    monkeypatch.setattr(broker, "tenant_disabled", lambda _tenant: False)
    monkeypatch.setattr(
        broker,
        "_get_da",
        lambda: pytest.fail("invalid drawing reached the APS client"),
    )
    response = broker.broker_extract(
        broker.BrokerExtractRequest(tenant_id="tenant-a", dwg="../credentials")
    )
    assert response.status_code == 400
    assert b'"error_code":"BAD_PARAMS"' in response.body


def test_broker_live_run_rejects_traversal_before_loading_da(monkeypatch, tmp_path):
    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(broker, "tenant_disabled", lambda _tenant: False)
    monkeypatch.setattr(broker, "_cap_preflight", lambda _tenant, _tool: None)
    monkeypatch.setattr(
        broker,
        "_get_da",
        lambda: pytest.fail("invalid drawing reached the APS client"),
    )
    req = broker.BrokerRunRequest(
        tenant_id="tenant-a",
        tool={"name": "safe-read", "params_schema": {"type": "object"}},
        params={},
        dwg="..\\credentials",
        aps_live=True,
    )
    response = broker.broker_run(req)
    assert response.status_code == 400
    assert b'"error_code":"BAD_PARAMS"' in response.body


# --------------------------------------------------------------------------- #
# F4(a): caller-auth on /broker/* (shared secret via X-Broker-Secret header)
# --------------------------------------------------------------------------- #
def test_require_broker_auth_401_on_wrong_or_absent_secret(monkeypatch):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_BROKER_SECRET", "s3cret-value")
    # absent header -> 401
    with pytest.raises(HTTPException) as ei:
        broker.require_broker_auth(x_broker_secret=None)
    assert ei.value.status_code == 401
    # wrong header -> 401
    with pytest.raises(HTTPException) as ei:
        broker.require_broker_auth(x_broker_secret="not-it")
    assert ei.value.status_code == 401
    # correct header -> allowed (no raise, returns None)
    assert broker.require_broker_auth(x_broker_secret="s3cret-value") is None


def test_require_broker_auth_fails_closed_when_unset_in_live_mode(monkeypatch):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.delenv("LEAF_BROKER_SECRET", raising=False)
    with pytest.raises(HTTPException) as ei:
        broker.require_broker_auth(x_broker_secret=None)
    assert ei.value.status_code == 503


def test_require_broker_auth_open_in_demo_mode(monkeypatch):
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    monkeypatch.delenv("LEAF_BROKER_SECRET", raising=False)
    # off-live, no secret -> friction-free (byte-identical to today)
    assert broker.require_broker_auth(x_broker_secret=None) is None


def test_require_broker_auth_enforced_when_secret_set_even_off_live(monkeypatch):
    # a secret that is SET is ALWAYS enforced, regardless of live mode
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    monkeypatch.setenv("LEAF_BROKER_SECRET", "abc")
    with pytest.raises(HTTPException) as ei:
        broker.require_broker_auth(x_broker_secret=None)
    assert ei.value.status_code == 401
    assert broker.require_broker_auth(x_broker_secret="abc") is None


def test_protected_routes_carry_auth_dependency_and_health_is_open():
    protected = {
        "/broker/run", "/broker/extract", "/broker/reap",
        "/broker/tenants/{tid}/disable", "/broker/tenants/{tid}/enable",
    }

    def _dep_calls(route):
        dep = getattr(route, "dependant", None)
        return [d.call for d in dep.dependencies] if dep is not None else []

    by_path = {getattr(r, "path", None): r for r in broker.app.routes}
    for path in protected:
        assert path in by_path, f"route {path} missing"
        assert broker.require_broker_auth in _dep_calls(by_path[path]), f"{path} not gated"
    # health MUST stay open (liveness probes carry no secret)
    assert broker.require_broker_auth not in _dep_calls(by_path["/broker/health"])


def test_broker_run_http_enforces_secret_in_live_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_BROKER_SECRET", "s3cret-value")
    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(broker, "DATA_FILE", tmp_path / "intake.json")
    (tmp_path / "intake.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(broker, "run_tool_dynamic", _ok_env)
    client = TestClient(broker.app)
    body = {"tenant_id": "auth-test-tenant",
            "tool": {"name": "t", "params_schema": {"type": "object"}},
            "params": {}, "dwg": "rooftop_demo", "aps_live": False}
    assert client.post("/broker/run", json=body).status_code == 401
    assert client.post("/broker/run", json=body,
                       headers={"X-Broker-Secret": "wrong"}).status_code == 401
    ok = client.post("/broker/run", json=body, headers={"X-Broker-Secret": "s3cret-value"})
    assert ok.status_code == 200
    assert ok.json()["ok"] is True


def test_broker_run_http_open_in_demo_mode_without_secret(monkeypatch, tmp_path):
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    monkeypatch.delenv("LEAF_BROKER_SECRET", raising=False)
    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(broker, "DATA_FILE", tmp_path / "intake.json")
    (tmp_path / "intake.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(broker, "run_tool_dynamic", _ok_env)
    client = TestClient(broker.app)
    body = {"tenant_id": "auth-test-tenant",
            "tool": {"name": "t", "params_schema": {"type": "object"}},
            "params": {}, "dwg": "rooftop_demo", "aps_live": False}
    # no header, demo mode -> still 200 (unchanged behaviour)
    assert client.post("/broker/run", json=body).status_code == 200


def test_broker_client_sends_secret_header_from_env(monkeypatch):
    monkeypatch.setenv("LEAF_BROKER_SECRET", "shhh")
    assert broker_client.broker_headers() == {"X-Broker-Secret": "shhh"}
    monkeypatch.delenv("LEAF_BROKER_SECRET", raising=False)
    assert broker_client.broker_headers() == {}
    # run_via_broker attaches the header (patch the real requests.post it uses)
    seen = {}

    def fake_post(url, *, json, headers, timeout):
        seen["headers"] = headers
        return _Response(200, {"ok": True})

    monkeypatch.setenv("LEAF_BROKER_SECRET", "shhh")
    monkeypatch.setattr(broker_client.requests, "post", fake_post)
    broker_client.run_via_broker("t", {"name": "x"}, {}, "rooftop_demo", False)
    assert seen["headers"] == {"X-Broker-Secret": "shhh"}


# --------------------------------------------------------------------------- #
# F4(b): the arbitrary-.py primitive in resolve_local_file is killed
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "ref",
    [
        "C:/tmp/evil.py", "C:\\tmp\\evil.py", "/etc/evil.py", "//host/share/evil.py",
        "../evil.py", "..\\evil.py", "tools/../../evil.py", "a\\..\\..\\evil.py",
        "~/evil.py", "~\\evil.py",
    ],
)
def test_resolve_local_file_rejects_absolute_and_traversal_entry(ref):
    assert tool_loader.resolve_local_file({"name": "x", "entry": ref}) is None
    assert tool_loader.resolve_local_file({"name": "x", "script": ref}) is None


def test_resolve_local_file_rejects_existing_absolute_py_and_never_execs(tmp_path):
    evil = tmp_path / "evil.py"
    evil.write_text("def run(intake, params):\n    return {'pwned': True}, None\n",
                    encoding="utf-8")
    tool = {"name": "evil", "entry": str(evil), "params_schema": {"type": "object"}}
    # not resolved...
    assert tool_loader.resolve_local_file(tool) is None
    # ...and never executed: run_tool_dynamic falls to "no local implementation" (BAD_PARAMS)
    env = tool_loader.run_tool_dynamic(tool, {}, {}, aps_live=False, da=None)
    assert env["ok"] is False
    assert env["error"]["error_code"] == "BAD_PARAMS"


def test_resolve_local_file_rejects_existing_absolute_script(tmp_path):
    evil = tmp_path / "evil.py"
    evil.write_text("x = 1\n", encoding="utf-8")
    assert tool_loader.resolve_local_file({"name": "evil", "script": str(evil)}) is None


def test_resolve_local_file_rejects_traversal_out_of_tenant_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "tools").mkdir(parents=True)
    outside = tmp_path / "outside.py"
    outside.write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(tool_loader, "_tenant_repo_root", lambda tid=None: repo)
    assert tool_loader.resolve_local_file(
        {"name": "evil", "entry": "tools/../../outside.py"}, tenant_id="t") is None


def test_resolve_local_file_accepts_relative_tenant_entry(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    d = repo / "tools" / "foo"
    d.mkdir(parents=True)
    f = d / "tool.py"
    f.write_text("def run(intake, params):\n    return {}, None\n", encoding="utf-8")
    monkeypatch.setattr(tool_loader, "_tenant_repo_root", lambda tid=None: repo)
    got = tool_loader.resolve_local_file({"name": "foo", "entry": "tools/foo/tool.py"},
                                         tenant_id="t")
    assert got == f.resolve()


def test_resolve_local_file_still_resolves_builtin_op():
    got = tool_loader.resolve_local_file({"name": "count", "engine_op": "count_by_layer"})
    assert got is not None and got.name == "count_by_layer.py"


# --------------------------------------------------------------------------- #
# F10: broker-side tier ENTITLEMENT re-check on /broker/run
# --------------------------------------------------------------------------- #
def test_tenant_tier_defaults_demo_and_reads_provisioned(monkeypatch):
    monkeypatch.delenv("LEAF_BROKER_TENANT_TIERS", raising=False)
    # unknown tenant -> demo (keeps the async spine unbroken)
    assert broker._tenant_tier("nobody") == "demo"
    monkeypatch.setenv("LEAF_BROKER_TENANT_TIERS", json.dumps({"t1": "restricted"}))
    assert broker._tenant_tier("t1") == "restricted"
    assert broker._tenant_tier("t2") == "demo"


def test_broker_run_denies_write_tool_for_restricted_tier(monkeypatch, tmp_path):
    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(broker, "tenant_disabled", lambda _t: False)
    monkeypatch.setattr(broker, "_cap_preflight", lambda _t, _tool: None)
    monkeypatch.setenv("LEAF_BROKER_TENANT_TIERS", json.dumps({"tenant-r": "restricted"}))
    req = broker.BrokerRunRequest(
        tenant_id="tenant-r",
        tool={"name": "writer", "capabilities": ["drawing.write"],
              "params_schema": {"type": "object"}},
        params={}, dwg="rooftop_demo", aps_live=False)
    resp = broker.broker_run(req)
    assert resp.status_code == 403
    assert b'"error_code":"ENTITLEMENT_REQUIRED"' in resp.body


def test_broker_run_allows_read_tool_for_restricted_tier(monkeypatch, tmp_path):
    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(broker, "DATA_FILE", tmp_path / "intake.json")
    (tmp_path / "intake.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(broker, "tenant_disabled", lambda _t: False)
    monkeypatch.setattr(broker, "_cap_preflight", lambda _t, _tool: None)
    monkeypatch.setattr(broker, "run_tool_dynamic", _ok_env)
    monkeypatch.setenv("LEAF_BROKER_TENANT_TIERS", json.dumps({"tenant-r": "restricted"}))
    req = broker.BrokerRunRequest(
        tenant_id="tenant-r",
        tool={"name": "reader", "capabilities": [], "params_schema": {"type": "object"}},
        params={}, dwg="rooftop_demo", aps_live=False)
    resp = broker.broker_run(req)
    assert resp.status_code == 200
    assert resp.body is not None
