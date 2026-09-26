#!/usr/bin/env python3
"""Read-only, credential-free re-probe of red hat findings F1, F6, F7 and F17."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import sys
import threading
import urllib.error
import urllib.request
from urllib.parse import urlsplit


FOREIGN_ORIGIN = "https://leaf-redhat-reprobe.invalid"
SCHEMA = "leaf.redhat-reprobe.v1"
MAX_BODY_BYTES = 64 * 1024
EXIT_CODES = {"PASS": 0, "FAIL": 1, "UNREADABLE": 3}
JOB_PATH = "/api/jobs/00000000-0000-4000-8000-000000000000"
# Cloudflare's browser integrity check answers urllib's default agent with 403 before the app sees it.
USER_AGENT = "leaf-redhat-reprobe/1.0"
EDGE_STATUSES = (401, 403, 429)
EDGE_BODY_BYTES = 512
_EDGE_BODY = re.compile(r"error code: [0-9]+")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def http_error_302(self, req, fp, code, msg, headers):
        # Return before urllib parses Location, which may itself be malformed.
        return fp

    http_error_301 = http_error_302
    http_error_303 = http_error_302
    http_error_307 = http_error_302
    http_error_308 = http_error_302


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def _validate_origin(origin):
    if not isinstance(origin, str) or not origin or any(
        ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in origin
    ):
        raise ValueError("origin must be a URL without whitespace or control characters")
    try:
        parsed = urlsplit(origin)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("origin has an invalid host or port") from None
    if not host or (port is not None and port == 0) or "\\" in parsed.netloc:
        raise ValueError("origin must have a valid host and port")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("origin must not contain userinfo")
    if "?" in origin or "#" in origin or parsed.path not in ("", "/"):
        raise ValueError("origin must not contain a query, fragment, or non-root path")
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and host in ("127.0.0.1", "localhost")
    ):
        raise ValueError("origin must use HTTPS, or HTTP on 127.0.0.1 or localhost")
    return origin.rstrip("/")


def _validate_timeout(timeout):
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        raise ValueError("timeout must be between 0.1 and 30 seconds") from None
    if not 0.1 <= timeout <= 30:
        raise ValueError("timeout must be between 0.1 and 30 seconds")
    return timeout


def _verdict(verdicts):
    verdicts = tuple(verdicts)
    if "FAIL" in verdicts:
        return "FAIL"
    if "UNREADABLE" in verdicts:
        return "UNREADABLE"
    return "PASS"


def _source_sha(body):
    if len(body) > MAX_BODY_BYTES:
        return None
    try:
        health = json.loads(body)
    except Exception:
        return None
    sha = health.get("source_sha") if isinstance(health, dict) else None
    return sha if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{7,40}", sha) else None


def _cases(foreign_origin):
    """The fixed read-only request table, keyed by finding."""
    return {
        "F1": [("GET", path, {}) for path in (
            "/api/jobs",
            "/api/jobs?tenant_id=leaf-redhat-reprobe-foreign",
            JOB_PATH,
            JOB_PATH + "/stream",
        )],
        "F6": [("GET", "/api/projects", {"X-Org-Id": "00000000-0000-4000-8000-000000000001"})],
        "F7": [("GET", "/api/ops/tenants", {"X-Internal-Role": "qa"})],
        "F17": [
            ("OPTIONS", "/api/health", {"Origin": foreign_origin, "Access-Control-Request-Method": "GET"}),
            ("GET", "/api/health", {"Origin": foreign_origin}),
        ],
    }


def _open(opener, origin, method, path, headers, timeout):
    request = urllib.request.Request(origin + path, method=method, headers=headers)
    # Applied after the case headers, so no case can replace the fixed agent.
    request.add_header("User-Agent", USER_AGENT)
    try:
        return opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        # HTTP errors are observations, including denied access and redirects.
        return exc


def _edge_blocked(response):
    """True when a 401/403/429 came from the edge, not the app. Reads at most EDGE_BODY_BYTES."""
    if response.headers.get("cf-mitigated") is not None:
        return True
    body = response.read(EDGE_BODY_BYTES)
    return _EDGE_BODY.fullmatch(body.decode("utf-8", "replace").strip()) is not None


def _reachability(opener, origin, timeout):
    """One GET /api/health without Origin; PASS only when the app answers 200 with a JSON object."""
    record = {"method": "GET", "path": "/api/health", "status": None, "verdict": "UNREADABLE"}
    sha = None
    try:
        response = _open(opener, origin, "GET", "/api/health", {}, timeout)
        record["status"] = int(response.code)
        try:
            with response:
                if record["status"] == 200:
                    body = response.read(MAX_BODY_BYTES + 1)
                    # One extra byte distinguishes a complete body from overflow.
                    if len(body) <= MAX_BODY_BYTES and isinstance(json.loads(body), dict):
                        record["verdict"] = "PASS"
                        sha = _source_sha(body)
        except Exception:
            # A body that cannot be read or parsed is not the app's answer.
            record["verdict"] = "UNREADABLE"
            sha = None
    except Exception:
        record["status"] = None
    return record, sha


def _check(opener, origin, method, path, headers, timeout, foreign_origin, cors):
    check = {"method": method, "path": path, "status": None, "verdict": "UNREADABLE"}
    if cors:
        check["acao"] = None
    try:
        response = _open(opener, origin, method, path, headers, timeout)
        status = int(response.code)
        check["status"] = status
        acao = response.headers.get("Access-Control-Allow-Origin") if cors else None
        edge = None
        try:
            with response:
                # Only a bounded denial body is read; a 2xx protected body or open SSE stream never is.
                if status in EDGE_STATUSES:
                    edge = _edge_blocked(response)
        except Exception:
            # Body and close failures cannot erase the observed status and headers.
            pass
        if status in EDGE_STATUSES:
            check["edge_blocked"] = edge is True
        if cors:
            check["acao"] = acao
        allowed = acao.strip() if acao is not None else None
        leak = allowed is not None and (allowed == "*" or allowed.casefold() == foreign_origin.casefold())
        if status in EDGE_STATUSES and edge is True:
            # A detected edge denial never proves the app refused.
            check["verdict"] = "UNREADABLE"
        elif leak:
            # An unreadable denial body may only prevent a PASS; it never erases an observed leak.
            check["verdict"] = "FAIL"
        elif status in EDGE_STATUSES and edge is None:
            # A denial whose body was unreadable never proves the app refused.
            check["verdict"] = "UNREADABLE"
        elif cors:
            check["verdict"] = "PASS"
        elif status in (401, 403, 404):
            check["verdict"] = "PASS"
        elif 200 <= status < 300:
            check["verdict"] = "FAIL"
    except Exception:
        check["status"] = None
        check["verdict"] = "UNREADABLE"
        check.pop("edge_blocked", None)
        if cors:
            check["acao"] = None
    return check


def probe(origin, *, timeout, foreign_origin=FOREIGN_ORIGIN) -> dict:
    origin = _validate_origin(origin)
    timeout = _validate_timeout(timeout)
    if not isinstance(foreign_origin, str) or not foreign_origin or any(
        ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in foreign_origin
    ):
        raise ValueError("foreign origin must be a nonempty header value without whitespace")
    # A private opener has no cookie jar or authentication handlers. Disabling
    # environment proxies also prevents ambient proxy credentials being attached.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    receipt = {
        "schema": SCHEMA,
        "origin": origin,
        "probed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_sha": None,
        "reachability": None,
        "findings": {},
    }
    receipt["reachability"], receipt["source_sha"] = _reachability(opener, origin, timeout)
    reachable = receipt["reachability"]["verdict"] == "PASS"
    for finding, cases in _cases(foreign_origin).items():
        checks = [
            _check(opener, origin, method, path, headers, timeout, foreign_origin, finding == "F17")
            for method, path, headers in cases
        ]
        verdict = _verdict(check["verdict"] for check in checks)
        if not reachable and verdict != "FAIL":
            # Without the app's own answer no finding can pass; an observed leak still fails.
            verdict = "UNREADABLE"
        receipt["findings"][finding] = {"verdict": verdict, "checks": checks}
    receipt["overall"] = _verdict(item["verdict"] for item in receipt["findings"].values())
    return receipt


@contextmanager
def _offline_origin():
    class SecureHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/health":
                body = b'{"source_sha":"unknown"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()

        def do_OPTIONS(self):
            self.send_response(400)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), SecureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def main(argv=None) -> int:
    parser = _Parser(description=__doc__, allow_abbrev=False)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--origin", help="deployed HTTPS origin (HTTP is allowed on loopback)")
    target.add_argument("--offline", action="store_true", help="probe an in-process secure stub")
    parser.add_argument("--timeout", default=10, type=_validate_timeout, metavar="SECONDS")
    parser.add_argument("--out", type=Path, help="also write the sanitized JSON receipt here")
    try:
        args = parser.parse_args(argv)
        if args.offline:
            with _offline_origin() as origin:
                receipt = probe(origin, timeout=args.timeout)
        else:
            receipt = probe(args.origin, timeout=args.timeout)
        rendered = json.dumps(receipt, indent=2, sort_keys=True)
        print(rendered)
        if args.out is not None:
            args.out.write_text(rendered + "\n", encoding="utf-8")
        return EXIT_CODES[receipt["overall"]]
    except (ValueError, OSError) as exc:
        print("redhat reprobe: " + " ".join(str(exc).splitlines()), file=sys.stderr)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
