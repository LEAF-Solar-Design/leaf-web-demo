"""Offline contract checks for the staging session wrapper."""

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

import xbench_session as session


CHALLENGE = "12345678-abcd-1234-abcd-123456789abc"
FRESH = "abcdef12-abcd-1234-abcd-123456789abc"
SOURCE = "a1" * 20
TOKEN = "secret1234567890abcdefghij"


class Runner:
    def __init__(self, *results):
        self.results = iter(results)
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        result = next(self.results)
        if isinstance(result, Exception):
            raise result
        return subprocess.CompletedProcess(argv, result, stdout=TOKEN, stderr="diagnostic")


class Page:
    def __init__(self, *contents):
        self.contents = iter(contents)
        self.gotos = []

    def goto(self, url, *, timeout):
        self.gotos.append((url, timeout))

    def content(self):
        return next(self.contents)


class Request:
    def __init__(self, body=b'{}', status=200, headers=None):
        self.calls = []
        self.response = SimpleNamespace(
            status=status, headers=headers or {}, body=lambda: body
        )

    def fetch(self, url, **kw):
        self.calls.append((url, kw))
        return self.response


def api_page(**kw):
    return SimpleNamespace(request=Request(**kw))


def test_extract_first_challenge():
    assert session.extract_challenge(f"<p>{CHALLENGE} {FRESH}</p>") == CHALLENGE


def test_extract_missing_challenge():
    with pytest.raises(session.SessionError, match="no service-login challenge"):
        session.extract_challenge("no challenge here")


def test_approve_exact_command_and_options():
    runner = Runner(0)
    assert session.approve_challenge(Path("repo"), CHALLENGE, SOURCE, runner=runner) == (0, "diagnostic")
    assert runner.calls == [(
        [sys.executable, "scripts/studio_agent_login.py", "approve", "--environment",
         "staging", "--challenge-id", CHALLENGE, "--expect-source", SOURCE],
        {"cwd": "repo", "capture_output": True, "text": True, "timeout": 90},
    )]


@pytest.mark.parametrize("challenge", ["bad", CHALLENGE.upper(), CHALLENGE + "x"])
def test_approve_bad_challenge_never_runs(challenge):
    runner = Runner()
    with pytest.raises(session.SessionError):
        session.approve_challenge("repo", challenge, SOURCE, runner=runner)
    assert runner.calls == []


@pytest.mark.parametrize("source", ["short", SOURCE.upper(), SOURCE + "a"])
def test_approve_bad_source_never_runs(source):
    runner = Runner()
    with pytest.raises(session.SessionError):
        session.approve_challenge("repo", CHALLENGE, source, runner=runner)
    assert runner.calls == []


def test_approve_timeout():
    runner = Runner(subprocess.TimeoutExpired("approve", 90, output=TOKEN, stderr=TOKEN))
    assert session.approve_challenge("repo", CHALLENGE, SOURCE, runner=runner) == (124, "approve timed out")


def test_approve_discards_stdout_and_redacts_stderr_tail():
    def runner(*args, **kw):
        return subprocess.CompletedProcess([], 1, stdout="stdout must disappear", stderr="x " * 500 + TOKEN)

    rc, tail = session.approve_challenge("repo", CHALLENGE, SOURCE, runner=runner)
    assert rc == 1
    assert "stdout" not in tail
    assert TOKEN not in tail
    assert tail.endswith("[redacted]")
    assert len(tail) <= session.STDERR_TAIL


def test_login_first_attempt():
    page, runner = Page(CHALLENGE), Runner(0)
    assert session.login(page, "repo", SOURCE, runner=runner) == {
        "approve_rcs": [0], "approve_attempts": 1, "approve_stderr_tail": "diagnostic"
    }
    assert page.gotos == [(session.STAGING_ORIGIN + "/api/auth/agent/login", 60_000)]


def test_login_second_fresh_challenge():
    page, runner = Page(CHALLENGE, FRESH), Runner(1, 0)
    info = session.login(page, "repo", SOURCE, runner=runner)
    assert info["approve_rcs"] == [1, 0]
    assert info["approve_attempts"] == 2
    assert runner.calls[1][0][6] == FRESH
    assert len(page.gotos) == 2


def test_login_refuses_repeated_challenge():
    page, runner = Page(CHALLENGE, CHALLENGE), Runner(1)
    with pytest.raises(session.SessionError, match="repeated"):
        session.login(page, "repo", SOURCE, runner=runner)
    assert len(runner.calls) == 1


def test_login_two_failures():
    runner = Runner(3, 7)
    with pytest.raises(session.SessionError, match=r"\[3, 7\]"):
        session.login(Page(CHALLENGE, FRESH), "repo", SOURCE, runner=runner)
    assert len(runner.calls) == 2


@pytest.mark.parametrize("path", ["https://evil.example/api/x", "/app", "//evil.example/api/x", "/api"])
def test_api_rejects_paths(path):
    page = api_page()
    with pytest.raises(ValueError):
        session.api(page, "GET", path)
    assert page.request.calls == []


@pytest.mark.parametrize("method", ["HEAD", "get", "TRACE"])
def test_api_rejects_methods(method):
    page = api_page()
    with pytest.raises(ValueError):
        session.api(page, method, "/api/test")
    assert page.request.calls == []


def test_api_json_serialization_and_headers():
    page = api_page()
    headers = {"content-type": "text/plain", "X-Trace": "yes"}
    session.api(page, "POST", "/api/test", json_body={"hello": [1]}, headers=headers, timeout_s=2.5)
    url, kw = page.request.calls[0]
    assert url == session.STAGING_ORIGIN + "/api/test"
    assert kw == {"method": "POST", "data": json.dumps({"hello": [1]}),
                  "headers": {"Content-Type": "application/json", "X-Trace": "yes"}, "timeout": 2500}
    assert headers["content-type"] == "text/plain"


def test_api_json_and_data_conflict():
    page = api_page()
    with pytest.raises(ValueError):
        session.api(page, "POST", "/api/test", json_body={}, data=b"raw")
    assert page.request.calls == []


def test_api_oversize_body(monkeypatch):
    monkeypatch.setattr(session, "MAX_API_BODY", 3)
    with pytest.raises(session.SessionError, match="size limit"):
        session.api(api_page(body=b"1234"), "GET", "/api/test")


def test_api_response_and_raw_data():
    page = api_page(body=b"abc", status=201, headers={"Content-Type": "text/plain", "X-Test": "yes"})
    assert session.api(page, "PUT", "/api/test", data=b"raw") == (
        201, {"content-type": "text/plain", "x-test": "yes"}, b"abc"
    )
    assert page.request.calls[0][1]["data"] == b"raw"


def test_api_json_non_json_names_status():
    with pytest.raises(session.SessionError, match="status 502") as exc:
        session.api_json(api_page(body=TOKEN.encode(), status=502), "GET", "/api/test")
    assert TOKEN not in str(exc.value)


def test_api_json_success():
    assert session.api_json(api_page(body=b'{"ok": true}'), "GET", "/api/test") == (200, {"ok": True})


def test_redact_token():
    assert session.redact("error " + TOKEN) == "error [redacted]"


def test_redact_plain_prose():
    prose = "The workspace is loading; please wait 30 seconds."
    assert session.redact(prose) == prose


def test_session_error_redacts():
    assert str(session.SessionError(TOKEN)) == "[redacted]"


class WorkspacePage:
    def __init__(self, body="", loading_timeout=False):
        self.body = body
        self.loading_timeout = loading_timeout
        self.calls = []

    def wait_for_url(self, pattern, **kw):
        self.calls.append(("url", pattern, kw))

    def wait_for_load_state(self, state, **kw):
        self.calls.append(("load", state, kw))

    def get_by_role(self, role, *, name):
        self.calls.append(("role", role, name))
        return self

    def or_(self, other):
        return self

    @property
    def first(self):
        return self

    def click(self, **kw):
        self.calls.append(("click", kw))

    def get_by_text(self, pattern):
        self.calls.append(("text", pattern))
        return self

    def wait_for(self, **kw):
        self.calls.append(("wait", kw))
        if kw["state"] == "detached" and self.loading_timeout:
            raise TimeoutError("projects still loading")

    def locator(self, selector):
        assert selector == "body"
        return self

    def inner_text(self):
        return self.body


def test_open_workspace_waits_and_build():
    page = WorkspacePage("Studio Build " + SOURCE)
    assert session.open_workspace(page) == {"web_build": SOURCE, "projects_loading_timeout": False}
    urls = [call for call in page.calls if call[0] == "url"]
    assert urls[0][1].fullmatch(session.STAGING_ORIGIN + "/?signed_in=1")
    assert not urls[0][1].fullmatch("https://evil.example/")
    assert urls[1][1].search(session.STAGING_ORIGIN + "/app")
    assert all(call[2] == {"timeout": 60_000} for call in urls)
    assert ("load", "networkidle", {"timeout": 30_000}) in page.calls
    assert ("click", {"timeout": 20_000}) in page.calls
    assert ("wait", {"state": "visible", "timeout": 30_000}) in page.calls
    assert ("wait", {"state": "detached", "timeout": 45_000}) in page.calls
    assert [call[1] for call in page.calls if call[0] == "role"] == ["link", "button"]


def test_open_workspace_loading_timeout_and_absent_build():
    assert session.open_workspace(WorkspacePage(loading_timeout=True)) == {
        "web_build": None, "projects_loading_timeout": True
    }


@pytest.mark.parametrize("failure", [None, "launch", "login", "workspace", "consumer", "close"])
def test_signed_in_lifecycle(monkeypatch, failure):
    calls = []
    page = object()

    def close():
        calls.append("close")
        if failure == "close":
            raise RuntimeError("close failed")

    def new_context(**kw):
        assert kw == {"viewport": {"width": 1200, "height": 800}, "accept_downloads": True}
        return SimpleNamespace(new_page=lambda: page)

    browser = SimpleNamespace(new_context=new_context, close=close)

    def launch(**kw):
        assert kw == {"headless": False, "args": ["--disable-extensions", "--no-first-run"]}
        if failure == "launch":
            raise RuntimeError("launch failed")
        return browser

    playwright = SimpleNamespace(chromium=SimpleNamespace(launch=launch), stop=lambda: calls.append("stop"))
    monkeypatch.setitem(sys.modules, "playwright", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "playwright.sync_api", SimpleNamespace(
        sync_playwright=lambda: SimpleNamespace(start=lambda: playwright)
    ))
    runner = Runner()

    def login(actual_page, repo, source, **kw):
        assert (actual_page, repo, source) == (page, "repo", SOURCE)
        assert kw == {"origin": session.STAGING_ORIGIN, "runner": runner}
        calls.append("login")
        if failure == "login":
            raise RuntimeError("login failed")
        return {"approve_rcs": [0], "approve_attempts": 1, "approve_stderr_tail": ""}

    def workspace(actual_page, **kw):
        assert actual_page is page
        assert kw == {"origin": session.STAGING_ORIGIN}
        calls.append("workspace")
        if failure == "workspace":
            raise RuntimeError("workspace failed")
        return {"web_build": None, "projects_loading_timeout": False}

    monkeypatch.setattr(session, "login", login)
    monkeypatch.setattr(session, "open_workspace", workspace)

    def use_session():
        with session.signed_in("repo", SOURCE, viewport=(1200, 800), headless=False, runner=runner) as result:
            actual_page, info = result
            assert actual_page is page
            assert info == {"approve_rcs": [0], "approve_attempts": 1, "approve_stderr_tail": "",
                            "web_build": None, "projects_loading_timeout": False, "source": SOURCE}
            if failure == "consumer":
                raise RuntimeError("consumer failed")

    if failure is None:
        use_session()
    else:
        with pytest.raises(RuntimeError, match=failure + " failed"):
            use_session()
    assert calls[-1] == "stop"
    if failure != "launch":
        assert calls[-2] == "close"
    else:
        assert "close" not in calls
