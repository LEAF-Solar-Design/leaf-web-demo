"""Bounded staging service login and same-session product requests."""

from contextlib import contextmanager
import json
import re
import subprocess
import sys


STAGING_ORIGIN = "https://platform-staging.leafdesign.ai"
CAPTURE_HEADER_NAMES = ("authorization", "x-tenant-id", "x-org-id")
CAPTURE_TIMEOUT_S = 30
CHALLENGE_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
APPROVE_TIMEOUT_S = 90
MAX_APPROVE_ATTEMPTS = 2
STDERR_TAIL = 800
MAX_API_BODY = 50 * 1024 * 1024


def redact(text) -> str:
    def replace(match):
        value = match.group(0)
        if re.search(r"[A-Za-z]", value) and re.search(r"[0-9]", value):
            return "[redacted]"
        return value

    return re.sub(r"[A-Za-z0-9._~+/=-]{20,}", replace, str(text))


class SessionError(RuntimeError):
    def __init__(self, message):
        super().__init__(redact(message))


class AppHeaders:
    def __init__(self, origin=STAGING_ORIGIN):
        self._origin = origin
        self._headers = {}

    def observe(self, url: str, headers: dict):
        if not url.startswith(self._origin + "/api/"):
            return
        if url.startswith(self._origin + "/api/auth/"):
            return
        captured = {name.lower(): value for name, value in headers.items()
                    if name.lower() in CAPTURE_HEADER_NAMES}
        if "authorization" in captured:
            self._headers = captured

    def ready(self) -> bool:
        return "authorization" in self._headers

    def names(self) -> list[str]:
        return sorted(self._headers)

    def as_dict(self) -> dict:
        return self._headers.copy()

    def __repr__(self):
        return f"AppHeaders(names={self.names()!r})"


def extract_challenge(html: str) -> str:
    match = CHALLENGE_RE.search(html)
    if match is None:
        raise SessionError("no service-login challenge")
    return match.group(0)


def approve_challenge(repo, challenge, source, *, runner=subprocess.run) -> tuple[int, str]:
    if not isinstance(challenge, str) or not CHALLENGE_RE.fullmatch(challenge):
        raise SessionError("invalid service-login challenge")
    if not isinstance(source, str) or not re.fullmatch(r"[0-9a-f]{40}", source):
        raise SessionError("invalid source SHA")
    try:
        result = runner(
            [sys.executable, "scripts/studio_agent_login.py", "approve",
             "--environment", "staging", "--challenge-id", challenge,
             "--expect-source", source],
            cwd=str(repo), capture_output=True, text=True, timeout=APPROVE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return 124, "approve timed out"
    return result.returncode, redact((result.stderr or "")[-STDERR_TAIL:])


def login(page, repo, source, *, origin=STAGING_ORIGIN, runner=subprocess.run) -> dict:
    seen = set()
    rcs = []
    for _ in range(MAX_APPROVE_ATTEMPTS):
        page.goto(origin + "/api/auth/agent/login", timeout=60_000)
        challenge = extract_challenge(page.content())
        if challenge in seen:
            raise SessionError("repeated service-login challenge; refusing re-approval")
        seen.add(challenge)
        rc, tail = approve_challenge(repo, challenge, source, runner=runner)
        rcs.append(rc)
        if rc == 0:
            return {"approve_rcs": rcs, "approve_attempts": len(rcs),
                    "approve_stderr_tail": tail}
    raise SessionError(f"service-login approval failed with return codes {rcs}")


def open_workspace(page, *, origin=STAGING_ORIGIN) -> dict:
    page.wait_for_url(re.compile(r"^" + re.escape(origin) + r"/(\?.*)?$"), timeout=60_000)
    page.wait_for_load_state("networkidle", timeout=30_000)
    name = re.compile(r"open workspace", re.I)
    page.get_by_role("link", name=name).or_(page.get_by_role("button", name=name)).first.click(
        timeout=20_000
    )
    page.wait_for_url(re.compile(r".*/app.*"), timeout=60_000)
    page.get_by_text(re.compile(r"sign out", re.I)).wait_for(state="visible", timeout=30_000)
    loading_timeout = False
    try:
        page.get_by_text(re.compile(r"Loading workspace projects", re.I)).wait_for(
            state="detached", timeout=45_000
        )
    except Exception:
        # This readiness hint is best effort; the authenticated workspace is open.
        loading_timeout = True
    build = re.search(r"\bBuild ([0-9a-f]{40})\b", page.locator("body").inner_text())
    return {"web_build": build.group(1) if build else None,
            "projects_loading_timeout": loading_timeout}


@contextmanager
def signed_in(repo, source, *, origin=STAGING_ORIGIN, viewport=(1440, 900),
              headless=True, runner=subprocess.run, _playwright_factory=None):
    if _playwright_factory is None:
        from playwright.sync_api import sync_playwright
        _playwright_factory = sync_playwright

    playwright = _playwright_factory().start()
    browser = None
    try:
        browser = playwright.chromium.launch(
            headless=headless, args=["--disable-extensions", "--no-first-run"]
        )
        context = browser.new_context(
            viewport={"width": viewport[0], "height": viewport[1]}, accept_downloads=True
        )
        page = context.new_page()
        info = login(page, repo, source, origin=origin, runner=runner)
        app_headers = AppHeaders(origin=origin)
        page._xbench_app_headers = app_headers
        page.on("request", lambda r: app_headers.observe(r.url, r.headers))
        info.update(open_workspace(page, origin=origin))
        for attempt in range(2):
            for _ in range(int(CAPTURE_TIMEOUT_S * 1000 / 250)):
                if app_headers.ready():
                    break
                page.wait_for_timeout(250)
            if app_headers.ready():
                break
            if attempt == 0:
                page.reload()
        if not app_headers.ready():
            raise SessionError("app auth headers not observed")
        info["app_header_names"] = app_headers.names()
        info["source"] = source
        yield page, info
    finally:
        try:
            if browser is not None:
                browser.close()
        finally:
            playwright.stop()


def api(page, method, path, *, json_body=None, data=None, headers=None,
        timeout_s=60.0, origin=STAGING_ORIGIN) -> tuple[int, dict, bytes]:
    if not isinstance(path, str) or not path.startswith("/api/"):
        raise ValueError(redact("path must start with /api/"))
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        raise ValueError(redact("unsupported API method"))
    if json_body is not None and data is not None:
        raise ValueError(redact("json_body and data are mutually exclusive"))
    app_headers = getattr(page, "_xbench_app_headers", None)
    captured = app_headers.as_dict() if app_headers is not None else {}
    request_headers = {**captured, **(headers or {})}
    if json_body is not None:
        data = json.dumps(json_body)
        request_headers = {k: v for k, v in request_headers.items() if k.lower() != "content-type"}
        request_headers["Content-Type"] = "application/json"
    response = page.request.fetch(
        origin + path, method=method, data=data, headers=request_headers,
        timeout=timeout_s * 1000,
    )
    body = response.body()
    if len(body) > MAX_API_BODY:
        raise SessionError("API response body exceeds size limit")
    return response.status, {k.lower(): v for k, v in response.headers.items()}, body


def api_json(page, method, path, **kw) -> tuple[int, object]:
    status, _, body = api(page, method, path, **kw)
    try:
        return status, json.loads(body)
    except (ValueError, UnicodeError):
        raise SessionError(f"API response with status {status} is not JSON") from None
