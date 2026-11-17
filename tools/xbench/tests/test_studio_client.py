"""Product-route contracts exercised without a network or wall-clock sleeps."""

import re

import pytest

import xbench_studio as studio


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, path, json_body=None, data=None, headers=None, timeout_s=60.0):
        self.calls.append(dict(method=method, path=path, json_body=json_body,
                               data=data, headers=headers, timeout_s=timeout_s))
        assert self.responses, "unexpected transport call"
        return self.responses.pop(0)


class FakeTime:
    def __init__(self):
        self.now = 0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def make(*responses):
    transport = FakeTransport(*responses)
    return studio.StudioClient(transport), transport


def assert_call(call, method, path, *, json_body=None, headers=None, data=None, timeout_s=60.0):
    assert call == dict(method=method, path=path, json_body=json_body,
                        headers=headers, data=data, timeout_s=timeout_s)


def test_error_envelope():
    body = {"error": {"error_code": "DENIED", "reason_code": "LOCKED",
                      "message": "Busy", "next_action": "Retry"}, "private": "not printed"}
    error = studio.parse_error(409, body)
    assert isinstance(error, RuntimeError)
    assert (error.status, error.error_code, error.reason_code, error.message,
            error.next_action, error.body) == (409, "DENIED", "LOCKED", "Busy", "Retry", body)
    assert str(error) == "409 DENIED LOCKED: Busy"
    assert "Retry" not in str(error)
    assert "not printed" not in repr(error)


def test_error_without_reason():
    error = studio.parse_error(400, {"error": {"error_code": "BAD", "message": "Invalid"}})
    assert error.reason_code is None
    assert error.next_action is None
    assert str(error) == "400 BAD: Invalid"


def test_auth_detail():
    body = {"detail": "Login required"}
    error = studio.parse_error(401, body)
    assert (error.error_code, error.message, error.body) == ("AUTH_DENIED", "Login required", body)


@pytest.mark.parametrize("body", [None, b"unavailable", {}, {"error": "broken"}])
def test_unknown_error_body(body):
    error = studio.parse_error(503, body)
    assert error.status == 503
    assert error.body == body
    assert error.error_code is None


def test_idempotency_key():
    first = studio.new_idempotency_key()
    assert re.fullmatch(r"xbench-[0-9a-f]{32}", first)
    assert studio.new_idempotency_key() != first
    assert re.fullmatch(r"test-[0-9a-f]{32}", studio.new_idempotency_key("test"))


def test_create_project():
    project = {"project_id": "p"}
    client, transport = make((201, {"created": True, "project": project}))
    assert client.create_project("bench") is project
    assert_call(transport.calls[0], "POST", "/api/projects", json_body={"name": "bench"})


@pytest.mark.parametrize("created", [False, None, 1, "true"])
def test_reused_project_refused(created):
    client, _ = make((200, {"created": created, "project": {"project_id": "old"}}))
    with pytest.raises(studio.StudioError) as caught:
        client.create_project("bench")
    assert (caught.value.status, caught.value.error_code) == (409, "PROJECT_NAME_REUSED")


@pytest.mark.parametrize("name", ["", "x" * 201, None])
def test_invalid_name(name):
    client, transport = make()
    with pytest.raises(ValueError):
        client.create_project(name)
    assert not transport.calls


@pytest.mark.parametrize("name", ["x", "x" * 200])
def test_name_boundaries(name):
    client, _ = make((201, {"created": True, "project": {}}))
    assert client.create_project(name) == {}


def test_delete_project():
    client, transport = make((200, {"deleted": True}), (200, {}))
    assert client.delete_project("p") == {"deleted": True}
    client.delete_project("p")
    keys = [call["headers"]["Idempotency-Key"] for call in transport.calls]
    assert keys[0] != keys[1]
    assert_call(transport.calls[0], "DELETE", "/api/projects/p",
                headers={"Idempotency-Key": keys[0]})


@pytest.mark.parametrize("engine", [None, "local", "aps"])
def test_upload_multipart(engine):
    client, transport = make((202, {"drawing_id": "source"}))
    content = b"drawing\x00\xff\r\nbytes"
    assert client.upload_drawing("roof.dwg", content, engine=engine) == "source"
    call = transport.calls[0]
    header = call["headers"]["Content-Type"]
    boundary = header.split("boundary=")[1]
    assert re.fullmatch(r"----xbench[0-9a-f]{32}", boundary)
    expected = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                'filename="roof.dwg"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
    expected += content + b"\r\n"
    if engine is not None:
        expected += (f'--{boundary}\r\nContent-Disposition: form-data; name="engine"'
                     f'\r\n\r\n{engine}\r\n').encode()
    expected += f"--{boundary}--\r\n".encode()
    assert_call(call, "POST", "/api/drawings/upload", data=expected, headers={"Content-Type": header})


def test_upload_size_refused():
    client, transport = make()
    with pytest.raises(ValueError):
        client.upload_drawing("large.dwg", b"x" * (25 * 1024 * 1024 + 1))
    assert not transport.calls


def test_upload_size_limit_allowed():
    client, transport = make((202, {"drawing_id": "d"}))
    assert client.upload_drawing("limit.dwg", b"x" * (25 * 1024 * 1024)) == "d"
    assert len(transport.calls) == 1


def test_invalid_engine():
    client, transport = make()
    with pytest.raises(ValueError):
        client.upload_drawing("roof.dwg", b"x", engine="remote")
    assert not transport.calls


@pytest.mark.parametrize("filename", ['bad"name', "bad\rname", "bad\nname"])
def test_multipart_header_injection(filename):
    client, transport = make()
    with pytest.raises(ValueError):
        client.upload_drawing(filename, b"x")
    assert not transport.calls


def test_wait_upload_ready():
    timer = FakeTime()
    ready = {"status": "ready", "drawing_id": "d"}
    transport = FakeTransport((200, {"status": "processing"}), (200, ready))
    client = studio.StudioClient(transport, clock=timer.clock, sleep=timer.sleep)
    assert client.wait_upload("d") is ready
    assert timer.sleeps == [3]
    for call in transport.calls:
        assert_call(call, "GET", "/api/drawings/d/upload-status")


def test_wait_upload_failed():
    body = {"status": "failed", "error": {"message": "Cannot decode", "reason_code": "FORMAT"}}
    client, transport = make((200, body))
    with pytest.raises(studio.StudioError) as caught:
        client.wait_upload("d")
    assert (caught.value.status, caught.value.error_code) == (422, "UPLOAD_FAILED")
    assert caught.value.body == body
    assert caught.value.message == "Cannot decode"
    assert caught.value.reason_code == "FORMAT"
    assert len(transport.calls) == 1


def test_wait_upload_failed_with_text_error():
    body = {"status": "failed", "error": "Unsupported drawing"}
    client, _ = make((200, body))
    with pytest.raises(studio.StudioError) as caught:
        client.wait_upload("d")
    assert caught.value.message == "Unsupported drawing"
    assert caught.value.body == body


@pytest.mark.parametrize("status", [199, 300, 401, 500])
def test_non_success_status_boundaries(status):
    client, _ = make((status, {"error": {"error_code": "FAILED", "message": "Failed"}}))
    with pytest.raises(studio.StudioError) as caught:
        client.tools()
    assert caught.value.status == status


@pytest.mark.parametrize("upload,code,path", [
    (True, "UPLOAD_TIMEOUT", "/api/drawings/d/upload-status"),
    (False, "JOB_TIMEOUT", "/api/jobs/d"),
])
def test_poll_deadline(upload, code, path):
    timer = FakeTime()
    transport = FakeTransport(*[(200, {"status": "pending"})] * 3)
    client = studio.StudioClient(transport, clock=timer.clock, sleep=timer.sleep)
    method = client.wait_upload if upload else client.wait_job
    with pytest.raises(studio.StudioError) as caught:
        method("d", timeout_s=5, poll_s=3)
    assert (caught.value.status, caught.value.error_code) == (504, code)
    assert timer.sleeps == [3, 2]
    assert timer.now == 5
    assert_call(transport.calls[0], "GET", path, timeout_s=5)
    assert_call(transport.calls[1], "GET", path, timeout_s=2)


def test_request_advances_past_deadline():
    timer = FakeTime()
    def transport(*args, **kwargs):
        timer.now += 6
        return 200, {"status": "pending"}
    client = studio.StudioClient(transport, clock=timer.clock, sleep=timer.sleep)
    with pytest.raises(studio.StudioError, match="JOB_TIMEOUT"):
        client.wait_job("j", timeout_s=5)
    assert timer.sleeps == []


@pytest.mark.parametrize("timeout,poll", [(-1, 2), (1, 0), (1, -1), (float("inf"), 2), (1, float("nan"))])
def test_invalid_poll_bounds(timeout, poll):
    client, transport = make()
    with pytest.raises(ValueError):
        client.wait_job("j", timeout_s=timeout, poll_s=poll)
    assert not transport.calls


@pytest.mark.parametrize("version", [1, 4])
def test_adopt_drawing(version):
    drawing = {"drawing_id": "d", "version_id": "v"}
    client, transport = make((201, {"drawing_version": drawing}))
    kwargs = {} if version == 1 else {"version": version}
    assert client.adopt_drawing("p", "source", "Roof", **kwargs) is drawing
    headers = transport.calls[0]["headers"]
    assert re.fullmatch(r"xbench-[0-9a-f]{32}", headers["Idempotency-Key"])
    assert_call(transport.calls[0], "POST", "/api/projects/p/drawing-versions/import",
                headers=headers, json_body={"name": "Roof", "source": {
                    "kind": "account_upload", "drawing_id": "source", "version": version}})


def test_context():
    client, transport = make((200, {"context": "ok"}))
    assert client.drawing_context("p", "v", "d") == {"context": "ok"}
    assert_call(transport.calls[0], "GET", "/api/projects/p/drawing-versions/v/context?drawing_id=d")


def test_ids_are_url_encoded():
    client, transport = make((200, {}))
    client.drawing_context("p/q", "v?x", "d&x=1")
    assert transport.calls[0]["path"] == "/api/projects/p%2Fq/drawing-versions/v%3Fx/context?drawing_id=d%26x%3D1"


@pytest.mark.parametrize("kwargs,holder,ttl", [({}, "xbench grader", 3600),
                                                      ({"ttl_s": 86400, "holder": "custom"}, "custom", 86400)])
def test_checkout(kwargs, holder, ttl):
    lease = {"holder": holder}
    client, transport = make((200, {"checkout_capability": "opaque-secret", "checkout": lease}))
    assert client.checkout("p", "d", **kwargs) == ("opaque-secret", lease)
    assert_call(transport.calls[0], "POST", "/api/projects/p/drawings/d/checkout",
                json_body={"holder": holder, "ttl_s": ttl})
    assert "opaque-secret" not in repr(client)
    assert "opaque-secret" not in str(client)


@pytest.mark.parametrize("ttl", [0, -1, 86401, float("nan")])
def test_invalid_checkout_ttl(ttl):
    client, transport = make()
    with pytest.raises(ValueError):
        client.checkout("p", "d", ttl_s=ttl)
    assert not transport.calls


def test_release():
    client, transport = make((200, {"released": True}))
    assert client.release("p", "d", "opaque") == {"released": True}
    assert_call(transport.calls[0], "DELETE", "/api/projects/p/drawings/d/checkout",
                headers={"X-Checkout-Capability": "opaque"})


def test_capability_redacted_from_error():
    body = {"error": {"error_code": "DENIED", "message": "Reject opaque-secret",
                      "next_action": "Replace opaque-secret"}, "checkout_capability": "opaque-secret"}
    client, _ = make((403, body))
    with pytest.raises(studio.StudioError) as caught:
        client.release("p", "d", "opaque-secret")
    assert "opaque-secret" not in str(caught.value)
    assert "opaque-secret" not in repr(caught.value)
    assert "opaque-secret" not in repr(vars(caught.value))


def test_tools():
    tools = [{"name": "area", "catalog_digest": "sha"}]
    client, transport = make((200, {"tools": tools}))
    assert client.tools() is tools
    assert_call(transport.calls[0], "GET", "/api/tools")


def test_catalog_digest():
    client, transport = make((200, {"tools": [{"name": "other", "catalog_digest": "wrong"},
                                             {"name": "area", "catalog_digest": "sha"}]}))
    assert client.catalog_digest("area") == "sha"
    assert_call(transport.calls[0], "GET", "/api/tools")


def test_missing_catalog_tool():
    client, _ = make((200, {"tools": []}))
    with pytest.raises(studio.StudioError) as caught:
        client.catalog_digest("missing")
    assert (caught.value.status, caught.value.error_code) == (404, "TOOL_NOT_IN_CATALOG")


@pytest.mark.parametrize("persist,wait,suffix", [(False, False, ""), (True, False, "?persist=1"),
                                                (False, True, "?wait=1"), (True, True, "?persist=1&wait=1")])
@pytest.mark.parametrize("status", [200, 202])
def test_run(persist, wait, suffix, status):
    result = {"job_id": "j"}
    client, transport = make((200, {"tools": [{"name": "area", "catalog_digest": "sha"}]}),
                             (status, result))
    assert client.run("area", org_id="o", project_id="p", version_id="v", params={"a": 1},
                      capability="opaque", idempotency_key="key", persist=persist, wait=wait) == (status, result)
    assert_call(transport.calls[0], "GET", "/api/tools")
    assert_call(transport.calls[1], "POST", "/api/run" + suffix,
                headers={"X-Org-Id": "o", "X-Project-Id": "p", "Idempotency-Key": "key",
                         "X-Checkout-Capability": "opaque"},
                json_body={"tool": "area", "catalog_digest": "sha", "dwg": "v", "params": {"a": 1}})


def test_run_optional_headers_absent():
    client, transport = make((200, {"tools": [{"name": "area", "catalog_digest": "sha"}]}), (202, {}))
    client.run("area", org_id="o", project_id="p", version_id="v", params={})
    assert transport.calls[1]["headers"] == {"X-Org-Id": "o", "X-Project-Id": "p"}


def test_run_missing_tool_never_submitted():
    client, transport = make((200, {"tools": []}))
    with pytest.raises(studio.StudioError, match="TOOL_NOT_IN_CATALOG"):
        client.run("missing", org_id="o", project_id="p", version_id="v", params={})
    assert len(transport.calls) == 1


@pytest.mark.parametrize("status", ["complete", "failed"])
def test_wait_job_terminal(status):
    timer = FakeTime()
    terminal = {"status": status, "result": "details"}
    transport = FakeTransport((200, {"status": "running"}), (200, terminal))
    client = studio.StudioClient(transport, clock=timer.clock, sleep=timer.sleep)
    assert client.wait_job("j") is terminal
    assert timer.sleeps == [2]
    for call in transport.calls:
        assert_call(call, "GET", "/api/jobs/j")


def test_dxf_bytes():
    client, transport = make((200, b"0\nSECTION\n\xff"))
    assert client.dxf_bytes("p", "d", "v") == b"0\nSECTION\n\xff"
    assert_call(transport.calls[0], "GET", "/api/projects/p/drawings/d/versions/v/dxf")


@pytest.mark.parametrize("body", ["text", {}, bytearray(b"bytes")])
def test_dxf_requires_bytes(body):
    client, _ = make((200, body))
    with pytest.raises(studio.StudioError, match="INVALID_DXF_RESPONSE"):
        client.dxf_bytes("p", "d", "v")


@pytest.mark.parametrize("method", [
    lambda c: c.create_project("bench"), lambda c: c.delete_project("p"),
    lambda c: c.upload_drawing("a.dwg", b"x"), lambda c: c.wait_upload("d"),
    lambda c: c.adopt_drawing("p", "d", "name"), lambda c: c.drawing_context("p", "v", "d"),
    lambda c: c.checkout("p", "d"), lambda c: c.release("p", "d", "cap"),
    lambda c: c.tools(), lambda c: c.catalog_digest("area"), lambda c: c.wait_job("j"),
    lambda c: c.dxf_bytes("p", "d", "v"),
])
def test_methods_raise_on_non_2xx(method):
    client, _ = make((403, {"error": {"error_code": "DENIED", "message": "Forbidden"}}))
    with pytest.raises(studio.StudioError) as caught:
        method(client)
    assert caught.value.status == 403
    assert caught.value.error_code == "DENIED"


def test_run_non_2xx():
    client, _ = make((200, {"tools": [{"name": "area", "catalog_digest": "sha"}]}),
                     (409, {"error": {"error_code": "CONFLICT", "message": "Reject secret"}}))
    with pytest.raises(studio.StudioError) as caught:
        client.run("area", org_id="o", project_id="p", version_id="v", params={}, capability="secret")
    assert caught.value.status == 409
    assert "secret" not in str(caught.value)


def test_disposable_success():
    client, transport = make((201, {"created": True, "project": {"project_id": "p"}}), (200, {}))
    with studio.disposable_project(client, "bench") as project:
        assert project == {"project_id": "p"}
        assert len(transport.calls) == 1
    assert transport.calls[1]["method"] == "DELETE"
    assert transport.calls[1]["path"] == "/api/projects/p"


def test_disposable_body_exception():
    client, transport = make((201, {"created": True, "project": {"project_id": "p"}}), (200, {}))
    original = ValueError("grader failed")
    with pytest.raises(ValueError) as caught:
        with studio.disposable_project(client, "bench"):
            raise original
    assert caught.value is original
    assert transport.calls[1]["method"] == "DELETE"


def test_disposable_cleanup_failure_propagates():
    client, _ = make((201, {"created": True, "project": {"project_id": "p"}}), (503, {"detail": "down"}))
    with pytest.raises(studio.StudioError) as caught:
        with studio.disposable_project(client, "bench"):
            pass
    assert caught.value.status == 503


def test_disposable_cleanup_failure_adds_note():
    client, _ = make((201, {"created": True, "project": {"project_id": "p"}}), (503, {"detail": "down"}))
    original = ValueError("grader failed")
    with pytest.raises(ValueError) as caught:
        with studio.disposable_project(client, "bench"):
            raise original
    assert caught.value is original
    assert len(original.__notes__) == 1
    assert "503" in original.__notes__[0]
    assert "cleanup failed" in original.__notes__[0]
    assert original.__context__ is None


def test_disposable_create_failure_does_not_delete():
    client, transport = make((200, {"created": False, "project": {"project_id": "existing"}}))
    with pytest.raises(studio.StudioError, match="PROJECT_NAME_REUSED"):
        with studio.disposable_project(client, "bench"):
            pytest.fail("must not yield an existing project")
    assert len(transport.calls) == 1
