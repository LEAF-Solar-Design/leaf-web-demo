"""Focused regressions for the app/broker APS credential and file boundary."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import broker  # noqa: E402
from routers import session as session_router  # noqa: E402


class _Response:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def test_live_session_extracts_through_broker_only(monkeypatch):
    calls = []

    def fake_post(url, *, json, timeout):
        calls.append((url, json, timeout))
        return _Response(200, {"intake": {"polylines": []}, "error": None,
                               "degraded_mode": False})

    monkeypatch.setattr(session_router.deps, "APS_LIVE", True)
    monkeypatch.setattr(session_router.broker_client, "broker_url", lambda: "http://broker:8140")
    monkeypatch.setattr(session_router.requests, "post", fake_post)

    body = session_router.session(dwg="rooftop_demo", tenant="tenant-a")

    assert body["intake"] == {"polylines": []}
    assert calls == [("http://broker:8140/broker/extract",
                      {"tenant_id": "tenant-a", "dwg": "rooftop_demo"}, 600)]
    source = Path(session_router.__file__).read_text(encoding="utf-8")
    assert "get_da_client" not in source
    assert "da.client" not in source


def test_mock_session_remains_cached_and_never_calls_broker(monkeypatch):
    monkeypatch.setattr(session_router.deps, "APS_LIVE", False)
    monkeypatch.setattr(session_router.deps, "load_cached_intake",
                        lambda: {"polylines": [{"layer": "Panels"}]})
    monkeypatch.setattr(
        session_router.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("mock session called broker"),
    )

    body = session_router.session(dwg="ignored-in-mock", tenant="demo-tenant")
    assert body == {
        "intake": {"polylines": [{"layer": "Panels"}]},
        "error": None,
        "degraded_mode": False,
    }


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
