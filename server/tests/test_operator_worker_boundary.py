"""O2/O3 vertical slice, server side (contract/OPERATOR.md Lane D): the
capability-only operator handler DISPATCHES broad work to the isolated worker and
never executes it inline; the dispatch is bounded (workspace disposable, network
denied, principal server-attested, timeout + command count capped), targets the
HARNESS hop (never the broker), and treats a non-200 as a refusal, never a
success."""
from __future__ import annotations

from pathlib import Path

import pytest

import operator_worker_dispatch as dispatch

_ROUTER_SRC = (Path(__file__).resolve().parents[1]
               / "routers" / "operator_worker.py").read_text(encoding="utf-8")

_HARNESS = "http://harness.test:8150"


class _Ctx:
    subject = "auth0|op-test"


class _Resp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = {"jobId": "opjob-x", "status": "succeeded"} if body is None else body

    def json(self):
        if self._body is _NOT_JSON:
            raise ValueError("not json")
        return self._body


_NOT_JSON = object()


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


def test_dispatch_forwards_to_the_harness_worker_route_never_inline(monkeypatch):
    # The dispatch POSTs to the HARNESS worker route (LEAF_OPERATOR_HARNESS_URL,
    # never the broker); it NEVER runs the commands in this process. Capture the
    # request and assert base URL + route + bounded payload + harness-only auth.
    monkeypatch.setenv("LEAF_OPERATOR_HARNESS_URL", _HARNESS)
    monkeypatch.setenv("LEAF_BROKER_SECRET", "bs")
    monkeypatch.setenv("LEAF_HARNESS_SECRET", "hs")
    calls = {}

    def _fake_post(url, json=None, headers=None, timeout=None):
        calls.update(url=url, payload=json, headers=headers, timeout=timeout)
        return _Resp()

    monkeypatch.setattr(dispatch.requests, "post", _fake_post)
    out = dispatch.dispatch_to_isolated_worker(_Ctx(), ["make build"],
                                               timeout_ms=60_000)
    # Full-URL assertion: the BASE is the harness, not merely the right suffix.
    assert calls["url"] == f"{_HARNESS}/operator/worker/dispatch"
    assert calls["payload"]["network"] == []                   # egress denied
    assert calls["payload"]["workspace"] == "disposable"
    # Harness caller-auth ONLY: the broker secret never rides this hop.
    assert calls["headers"] == {"X-Harness-Secret": "hs"}
    assert out["jobId"] == "opjob-x"


def test_connection_timeout_derives_from_the_capped_value(monkeypatch):
    # A client-supplied huge timeout_ms cannot hold an app thread past the cap:
    # the outbound connection timeout comes from the CAPPED payload value.
    monkeypatch.setenv("LEAF_OPERATOR_HARNESS_URL", _HARNESS)
    calls = {}

    def _fake_post(url, json=None, headers=None, timeout=None):
        calls.update(timeout=timeout)
        return _Resp()

    monkeypatch.setattr(dispatch.requests, "post", _fake_post)
    dispatch.dispatch_to_isolated_worker(_Ctx(), ["echo hi"],
                                         timeout_ms=10**12)
    assert calls["timeout"] == dispatch.MAX_TIMEOUT_MS / 1000 + 30


def test_harness_not_configured_fails_closed(monkeypatch):
    # No LEAF_OPERATOR_HARNESS_URL -> typed 503 refusal, and no POST is attempted.
    monkeypatch.delenv("LEAF_OPERATOR_HARNESS_URL", raising=False)

    def _never(*a, **k):
        raise AssertionError("no POST may be attempted without a harness URL")

    monkeypatch.setattr(dispatch.requests, "post", _never)
    with pytest.raises(dispatch.OperatorWorkerError) as err:
        dispatch.dispatch_to_isolated_worker(_Ctx(), ["echo hi"])
    assert err.value.reason == "harness_not_configured"
    assert err.value.http_status == 503


@pytest.mark.parametrize(
    ("harness_status", "harness_body", "expected_reason", "expected_http"),
    [
        (503, {"error": {"code": "substrate_not_isolating"}},
         "substrate_not_isolating", 503),
        (501, {"error": {"code": "not_implemented"}}, "not_implemented", 503),
        (401, {"error": {"code": "harness_auth_required"}},
         "harness_auth_required", 502),
        (404, _NOT_JSON, "worker_http_404", 502),
    ],
    ids=["fail-closed-isolation", "ships-dark", "auth-misconfig", "wrong-service"],
)
def test_non_200_from_the_harness_is_a_refusal_never_a_success(
        monkeypatch, harness_status, harness_body, expected_reason, expected_http):
    monkeypatch.setenv("LEAF_OPERATOR_HARNESS_URL", _HARNESS)
    monkeypatch.setattr(dispatch.requests, "post",
                        lambda *a, **k: _Resp(harness_status, harness_body))
    with pytest.raises(dispatch.OperatorWorkerError) as err:
        dispatch.dispatch_to_isolated_worker(_Ctx(), ["echo hi"])
    assert err.value.reason == expected_reason
    assert err.value.http_status == expected_http


def test_harness_unreachable_is_fail_closed(monkeypatch):
    import broker_client

    monkeypatch.setenv("LEAF_OPERATOR_HARNESS_URL", _HARNESS)

    def _boom(*a, **k):
        raise dispatch.requests.ConnectionError("down")

    monkeypatch.setattr(dispatch.requests, "post", _boom)
    with pytest.raises(broker_client.BrokerUnreachable):
        dispatch.dispatch_to_isolated_worker(_Ctx(), ["echo hi"])


def test_kill_switch_denies_dispatch_before_any_forwarding(monkeypatch):
    # Admission step 1 (contract section 5): an armed kill switch is a 409
    # denial and the dispatch module is never reached.
    from fastapi import HTTPException

    import operator_authority
    from routers import operator_worker as route

    monkeypatch.setattr(operator_authority, "kill_switch_active", lambda: True)
    monkeypatch.setattr(
        dispatch, "dispatch_to_isolated_worker",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("dispatch must not be reached under the kill switch")))
    body = route.WorkerDispatchBody(commands=["echo hi"])
    with pytest.raises(HTTPException) as err:
        route.dispatch_worker(body, op=_Ctx())
    assert err.value.status_code == 409
    assert err.value.detail == "kill_switch_active"


def test_router_is_capability_only_never_executes_inline():
    # The handler dispatches to the isolated worker and imports no inline
    # command executor. If a future edit adds one, this fails.
    assert "dispatch_to_isolated_worker" in _ROUTER_SRC
    for banned in ("subprocess", "os.system", "Popen", "shell=True", "os.exec"):
        assert banned not in _ROUTER_SRC, f"handler must not execute inline: {banned}"
