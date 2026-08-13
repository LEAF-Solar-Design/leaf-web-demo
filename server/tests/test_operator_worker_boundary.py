"""O2/O3 vertical slice, server side (contract/OPERATOR.md Lane D): the
capability-only operator handler DISPATCHES broad work to the isolated worker and
never executes it inline; the dispatch is bounded (workspace disposable, network
denied, principal server-attested, timeout + command count capped)."""
from __future__ import annotations

from pathlib import Path

import pytest

import operator_worker_dispatch as dispatch

_ROUTER_SRC = (Path(__file__).resolve().parents[1]
               / "routers" / "operator_worker.py").read_text(encoding="utf-8")


class _Ctx:
    subject = "auth0|op-test"


def test_dispatch_payload_is_bounded_and_isolation_forced():
    p = dispatch.build_dispatch_payload(_Ctx(), ["echo hi"], repo=None,
                                        timeout_ms=999_999_999)
    assert p["workspace"] == "disposable"
    assert p["network"] == []                        # egress ALWAYS denied
    assert p["principalSubject"] == "auth0|op-test"  # server-attested, not client input
    assert p["timeoutMs"] == dispatch.MAX_TIMEOUT_MS  # capped
    assert p["idempotencyKey"] and p["sessionId"]


def test_dispatch_rejects_unbounded_commands():
    with pytest.raises(dispatch.OperatorWorkerError):
        dispatch.build_dispatch_payload(_Ctx(), [], None, None)
    with pytest.raises(dispatch.OperatorWorkerError):
        dispatch.build_dispatch_payload(_Ctx(), ["x"] * 51, None, None)


def test_dispatch_forwards_to_the_isolated_worker_route_never_inline(monkeypatch):
    # The dispatch POSTs to the harness worker route; it NEVER runs the commands
    # in this process. Capture the request and assert route + bounded payload.
    monkeypatch.setenv("LEAF_BROKER_SECRET", "bs")
    monkeypatch.setenv("LEAF_HARNESS_SECRET", "hs")
    calls = {}

    class _Resp:
        def json(self):
            return {"jobId": "opjob-x", "status": "succeeded"}

    def _fake_post(url, json=None, headers=None, timeout=None):
        calls.update(url=url, payload=json, headers=headers)
        return _Resp()

    monkeypatch.setattr(dispatch.requests, "post", _fake_post)
    out = dispatch.dispatch_to_isolated_worker(_Ctx(), ["make build"],
                                               timeout_ms=60_000)
    assert calls["url"].endswith("/operator/worker/dispatch")  # isolated worker route
    assert calls["payload"]["network"] == []                   # egress denied
    assert calls["payload"]["workspace"] == "disposable"
    # The secret-gated app->harness hop headers are attached.
    assert calls["headers"]["X-Broker-Secret"] == "bs"
    assert calls["headers"]["X-Harness-Secret"] == "hs"
    assert out["jobId"] == "opjob-x"


def test_broker_unreachable_is_fail_closed(monkeypatch):
    import broker_client

    def _boom(*a, **k):
        raise dispatch.requests.ConnectionError("down")

    monkeypatch.setattr(dispatch.requests, "post", _boom)
    with pytest.raises(broker_client.BrokerUnreachable):
        dispatch.dispatch_to_isolated_worker(_Ctx(), ["echo hi"])


def test_router_is_capability_only_never_executes_inline():
    # The handler dispatches to the isolated worker and imports no inline
    # command executor. If a future edit adds one, this fails.
    assert "dispatch_to_isolated_worker" in _ROUTER_SRC
    for banned in ("subprocess", "os.system", "Popen", "shell=True", "os.exec"):
        assert banned not in _ROUTER_SRC, f"handler must not execute inline: {banned}"
