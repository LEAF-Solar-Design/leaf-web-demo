"""da/test_on_submitted_hook.py: the WorkItem id must be observable BEFORE polling.

`submit_workitem` POSTs a WorkItem and then blocks in `_poll_workitem` for up to
900s. Until this hook existed, the live id was born and died inside that frame,
so an abandoned run had no id anything outside could cancel: a closed browser tab
left paid APS compute running to completion. These tests pin the one property
that makes tab-close reaping possible: `on_submitted(workitem_id)` fires with
the real id and fires BEFORE the blocking poll begins.

Pure-python: `requests` is stubbed, no network, no APS, no credential.

  cd C:/tmp/leaf-web-demo/da && python -m pytest test_on_submitted_hook.py -q
"""
import json
import os
import sys

import pytest
import requests

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import client  # noqa: E402


class _NoNetwork:
    """Any HTTP verb this suite does not explicitly stub is a loud failure."""

    def _boom(self, *a, **k):
        raise AssertionError("pure test attempted a real network/APS call")

    get = post = put = delete = request = _boom


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _ErrorResp:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        raise requests.HTTPError(
            f"{self.status_code} Client Error for url: https://should-not-leak.example/workitems",
            response=self,
        )


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Nothing in this suite may read a credential or reach APS.

    `nickname()` is the easy one to miss: activity_qualified() calls it, and it
    is a real GET /forgeapps/me. Scoped to the fixture (not module level) so no
    other da test module inherits these stubs.
    """
    monkeypatch.setattr(client, "_auth_headers", lambda: {"Authorization": "Bearer test"})
    monkeypatch.setattr(client, "nickname", lambda: "TESTOWNER")
    monkeypatch.setattr(client, "auth_token", lambda: "offline-faketoken")
    monkeypatch.setattr(client, "requests", _NoNetwork())


def test_on_submitted_fires_with_live_id_before_polling_begins(monkeypatch):
    """Ordering is the whole point: a poll that blocks for minutes must never be
    the thing that gates knowing the id."""
    events = []

    monkeypatch.setattr(client.requests, "post",
                        lambda *a, **k: _Resp({"id": "wi-live-1", "status": "pending"}))

    def _slow_poll(workitem_id, **_kw):
        events.append(("poll", workitem_id))
        return {"id": workitem_id, "status": "success"}

    monkeypatch.setattr(client, "_poll_workitem", _slow_poll)

    status = client.submit_workitem(
        "activity-x", {}, dry_run=False, poll=True,
        on_submitted=lambda wid: events.append(("submitted", wid)))

    assert events == [("submitted", "wi-live-1"), ("poll", "wi-live-1")]
    assert status["status"] == "success"


def test_dry_run_never_invokes_the_callback(monkeypatch):
    """dry_run returns before any POST, so there is no live id to report."""
    seen = []

    def _boom(*_a, **_k):
        raise AssertionError("dry_run must not POST")

    monkeypatch.setattr(client.requests, "post", _boom)
    out = client.submit_workitem("activity-x", {}, dry_run=True,
                                 on_submitted=lambda wid: seen.append(wid))
    assert out["_dry_run"] is True
    assert seen == []


def test_callback_failure_cannot_fail_an_already_paid_submit(monkeypatch):
    """The WorkItem is submitted and BILLING by the time the hook runs. A broken
    observer must not convert that into a failed run."""
    monkeypatch.setattr(client.requests, "post",
                        lambda *a, **k: _Resp({"id": "wi-live-2"}))
    monkeypatch.setattr(client, "_poll_workitem",
                        lambda wid, **_kw: {"id": wid, "status": "success"})

    def _explode(_wid):
        raise RuntimeError("observer is broken")

    status = client.submit_workitem("activity-x", {}, dry_run=False, poll=True,
                                    on_submitted=_explode)
    assert status["status"] == "success"


def test_omitting_the_callback_leaves_the_call_unchanged(monkeypatch):
    """Backward compatibility: no on_submitted -> byte-for-byte the old path."""
    monkeypatch.setattr(client.requests, "post",
                        lambda *a, **k: _Resp({"id": "wi-live-3"}))
    monkeypatch.setattr(client, "_poll_workitem",
                        lambda wid, **_kw: {"id": wid, "status": "success"})
    assert client.submit_workitem("activity-x", {})["id"] == "wi-live-3"


def test_submit_error_preserves_bounded_sanitized_json_detail(monkeypatch):
    secret = "aps-client-secret-must-not-leak"
    signed_url = "https://signed.example/output?X-Amz-Credential=must-not-leak"
    body = {
        "diagnostic": "Activity LeafWriteProbe+prod was not found",
        "url": signed_url,
        "client_secret": secret,
    }
    monkeypatch.setattr(
        client.requests, "post",
        lambda *a, **k: _ErrorResp(400, json.dumps(body)),
    )

    with pytest.raises(requests.HTTPError) as caught:
        client.submit_workitem("activity-x", {}, poll=False)

    message = str(caught.value)
    assert "HTTP 400" in message
    assert "Activity LeafWriteProbe+prod was not found" in message
    assert secret not in message
    assert signed_url not in message
    assert "must-not-leak" not in message
    assert "should-not-leak" not in message
    assert len(message.split(": ", 1)[1].encode("utf-8")) <= (
        client._WORKITEM_ERROR_DETAIL_MAX_BYTES
    )
    assert caught.value.response.status_code == 400


def test_submit_error_bounds_and_scrubs_text_detail(monkeypatch):
    bearer = "Bearer " + "s" * 80
    raw = (
        "upstream rejected request at https://signed.example/input?token=leak "
        f"authorization={bearer} "
        + "diagnostic-word " * 500
    )
    monkeypatch.setattr(client.requests, "post",
                        lambda *a, **k: _ErrorResp(503, raw))

    with pytest.raises(requests.HTTPError) as caught:
        client.submit_workitem("activity-x", {}, poll=False)

    message = str(caught.value)
    detail = message.split(": ", 1)[1]
    assert "HTTP 503" in message
    assert "https://" not in detail
    assert "Bearer" not in detail
    assert "s" * 80 not in detail
    assert detail.endswith("<truncated>")
    assert len(detail.encode("utf-8")) <= client._WORKITEM_ERROR_DETAIL_MAX_BYTES


def test_run_tool_forwards_the_callback_to_the_live_submit(monkeypatch):
    """The broker reaches submit_workitem through run_tool, so the hook has to
    survive that hop or the registry is never written in production."""
    seen = []
    captured = {}

    def _fake_submit(activity_id, arguments, dry_run=False, poll=True,
                     tenant_id=None, on_submitted=None):
        captured["on_submitted"] = on_submitted
        if on_submitted is not None:
            on_submitted("wi-run-tool")
        return {"id": "wi-run-tool", "status": "success"}

    monkeypatch.setattr(client, "submit_workitem", _fake_submit)
    monkeypatch.setattr(client, "upload_object", lambda *a, **k: None)
    monkeypatch.setattr(client, "signed_download_url", lambda *a, **k: "https://in")
    monkeypatch.setattr(client, "signed_upload_url", lambda *a, **k: ("k", "https://out"))
    monkeypatch.setattr(client, "finalize_upload", lambda *a, **k: None)
    monkeypatch.setattr(client, "download_object", lambda *a, **k: b'{"ok": true}')

    client.run_tool("demo.dwg", {"name": "t", "engine_op": "count_by_layer"}, {},
                    on_submitted=lambda wid: seen.append(wid))

    assert captured["on_submitted"] is not None
    assert seen == ["wi-run-tool"]
