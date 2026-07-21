#!/usr/bin/env python3
"""Stage, verify, and promote the leaf-platform-web Vercel deployment.

Production is built by Vercel from ``web/`` so the cloud build can read the
project's sensitive ``VITE_*`` variables. The production alias is changed only
after the unaliased deployment passes route and compiled-bundle checks.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

REPO = Path(__file__).resolve().parent.parent
WEB = REPO / "web"
DIST = WEB / "dist"
ORG_ID = "team_LWjg4ghzDbsZrkNPaOnRwAx5"
PROJECT_ID = "prj_tBxvYtXa47THZ8aF59gvRx8W0bBc"
TEAM_SLUG = "evan-haugs-projects"
DOMAIN = "https://leaf-platform-web.vercel.app"
PRODUCTION_API_BASE = "https://platform-staging.leafdesign.ai"
PRODUCTION_AUTH0_DOMAIN = "leafautomation.us.auth0.com"
ROUTES = ["/", "/app", "/try", "/sheets", "/sheets/01"]
PROJECT_ENV = {"VERCEL_ORG_ID": ORG_ID, "VERCEL_PROJECT_ID": PROJECT_ID}


def run(cmd, cwd=None, env_extra=None, capture=False):
    import os

    env = None
    if env_extra:
        env = os.environ.copy()
        env.update(env_extra)
    return subprocess.run(
        cmd, cwd=cwd, env=env, shell=isinstance(cmd, str), check=False,
        text=True, encoding="utf-8", errors="replace", capture_output=capture,
    )


def fail(msg: str) -> None:
    print(f"NOT-READY: {msg}")
    raise SystemExit(1)


def _vercel_cli() -> str:
    vercel = shutil.which("vercel")
    if not vercel:
        fail("`vercel` CLI not found on PATH — install it or run `vercel login`")
    return vercel


def _npm_cli() -> str:
    npm = shutil.which("npm")
    if not npm:
        fail("`npm` not found on PATH")
    return npm


def _source_sha() -> str:
    result = run(["git", "rev-parse", "HEAD"], cwd=REPO, capture=True)
    sha = (result.stdout or "").strip().lower()
    if result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", sha):
        fail("could not resolve an immutable 40-character source SHA")
    return sha


def build() -> None:
    """Make a local structural build; production contract checks happen remotely."""
    print("-> building web/ locally (structural preflight only)")
    result = run([_npm_cli(), "run", "build"], cwd=WEB)
    if result.returncode != 0:
        fail("local `npm run build` failed")


def preflight() -> str:
    """Check the local output's static shape, without claiming env validation."""
    index = DIST / "index.html"
    if not index.exists():
        fail(f"{index} missing — run without --no-build")
    config_path = DIST / "vercel.json"
    if not config_path.exists():
        fail(f"{config_path} missing — deep SPA routes would return 404")
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(f"{config_path} is not valid JSON: {exc}")
    if not config.get("rewrites"):
        fail(f"{config_path} has no rewrites — deep SPA routes would return 404")
    if config.get("cleanUrls"):
        fail(f'{config_path} sets "cleanUrls": true, which breaks the SPA rewrite')
    health_path = DIST / "health.json"
    if not health_path.exists():
        fail(f"{health_path} missing")
    health_errors = _validate_web_health(
        health_path.read_text(encoding="utf-8"), expected_source_sha=_source_sha()
    )
    if health_errors:
        fail("local health artifact " + "; ".join(health_errors))
    match = re.search(
        r'assets/index-[A-Za-z0-9_-]+\.js', index.read_text(encoding="utf-8")
    )
    if not match:
        fail("could not find the entry asset reference in dist/index.html")
    print(f"  structural preflight ok — entry asset {match.group(0)}")
    return match.group(0)


def _deployment_url(output: str) -> str:
    urls = re.findall(r"https://[A-Za-z0-9.-]+\.vercel\.app", output)
    if not urls:
        fail("Vercel did not return a deployment URL")
    return urls[-1].rstrip("/")


def deploy(*, preview: bool) -> str:
    """Cloud-build web/ and return its unaliased deployment URL."""
    target = "preview" if preview else "production (unaliased)"
    print(f"-> cloud-building and deploying web/ to {target}")
    source_sha = _source_sha()
    command = [
        _vercel_cli(), "deploy", "--yes",
        "--build-env", f"LEAF_SOURCE_SHA={source_sha}",
    ]
    if not preview:
        command += ["--prod", "--skip-domain"]
    result = run(command, cwd=WEB, env_extra=PROJECT_ENV, capture=True)
    output = (result.stdout or "") + "\n" + (result.stderr or "")
    if result.returncode != 0:
        print(output)
        fail("`vercel deploy` failed")
    # Vercel writes the canonical deployment URL to stdout and ancillary
    # inspect/alias links to stderr. Prefer stdout so an alias is never mistaken
    # for the immutable staged artifact.
    url = _deployment_url(result.stdout or output)
    print(f"  staged: {url}")
    return url


def fetch(url: str, timeout: int = 30):
    request = urllib.request.Request(
        url,
        headers={
            "Cache-Control": "no-cache",
            "User-Agent": "Leaf-Platform-Deploy-Verifier/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        return None, str(exc)


def fetch_protected(deployment_url: str, path: str):
    """Fetch a protected staged deployment through authenticated Vercel CLI."""
    result = run(
        [
            _vercel_cli(), "curl", path, "--deployment", deployment_url,
            "--yes", "--scope", TEAM_SLUG, "--", "--fail-with-body",
        ],
        cwd=WEB,
        env_extra=PROJECT_ENV,
        capture=True,
    )
    if result.returncode != 0:
        diagnostic = (result.stderr or "") + "\n" + (result.stdout or "")
        match = re.search(r"(?:error:|HTTP/\S+)\s*(\d{3})", diagnostic, re.I)
        status = int(match.group(1)) if match else None
        return status, result.stdout or diagnostic or "vercel curl failed"
    return 200, result.stdout


def _production_bundle_contract(
    javascript: str, *, expected_source_sha: str | None = None
) -> list[str]:
    errors = []
    if "http://localhost:8130" in javascript:
        errors.append("contains localhost API fallback")
    if PRODUCTION_API_BASE not in javascript:
        errors.append(f"missing {PRODUCTION_API_BASE}")
    if PRODUCTION_AUTH0_DOMAIN not in javascript:
        errors.append(f"missing {PRODUCTION_AUTH0_DOMAIN}")
    if expected_source_sha and expected_source_sha not in javascript:
        errors.append(f"missing source SHA {expected_source_sha}")
    return errors


def _validate_web_health(
    body: str, *, expected_source_sha: str | None = None
) -> list[str]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return ["is not valid JSON"]
    errors = []
    expected = {
        "ok": True,
        "service": "leaf-platform-web",
        "component": "frontend",
        "schema_version": 1,
    }
    required_keys = {*expected, "source_sha"}
    if set(payload) != required_keys:
        errors.append(f"keys must equal {sorted(required_keys)!r}")
    for key, value in expected.items():
        if payload.get(key) != value:
            errors.append(f"{key} must equal {value!r}")
    source_sha = payload.get("source_sha")
    if not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        errors.append("source_sha must be a lowercase 40-character Git SHA")
    elif expected_source_sha and source_sha != expected_source_sha:
        errors.append(f"source_sha must equal {expected_source_sha}")
    return errors


def verify_production_backend() -> None:
    """Prove the configured host is the platform API, not another healthy API."""
    health_status, _ = fetch(PRODUCTION_API_BASE + "/api/health")
    if health_status != 200:
        fail(f"platform backend health returned HTTP {health_status}")
    session_status, _ = fetch(
        PRODUCTION_API_BASE + "/api/session?dwg=rooftop_demo"
    )
    if session_status != 401:
        fail(
            "platform backend unauthenticated session contract expected HTTP 401, "
            f"got {session_status}"
        )
    demo_status, demo_body = fetch(
        PRODUCTION_API_BASE + "/api/site/demo-solve"
    )
    if demo_status != 200:
        fail(f"platform backend public demo solve returned HTTP {demo_status}")
    try:
        demo = json.loads(demo_body)
        solve = demo["solve"]
        panel_count = solve["stats"]["panel_count"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        fail("platform backend public demo solve returned malformed JSON")
    if (
        demo.get("ok") is not True
        or isinstance(panel_count, bool)
        or not isinstance(panel_count, int)
        or panel_count <= 0
    ):
        fail("platform backend public demo solve failed its semantic contract")
    print(f"  backend contract ok: {PRODUCTION_API_BASE}")


def _javascript_graph(base_url: str, html: str, fetcher) -> tuple[str, str]:
    """Fetch entry and referenced Vite chunks, returning entry path and text."""
    match = re.search(r'(?:src=["\']/?)(assets/index-[^"\']+\.js)', html)
    if not match:
        fail(f"could not find the entry JavaScript at {base_url}")
    entry = match.group(1)
    pending = [entry]
    seen: set[str] = set()
    bodies: list[str] = []
    while pending:
        asset = pending.pop()
        if asset in seen:
            continue
        seen.add(asset)
        status, body = fetcher(asset)
        if status != 200:
            fail(f"{base_url}/{asset} returned HTTP {status}")
        bodies.append(body)
        for child in re.findall(r'["\'](?:\./)?(assets/[^"\']+\.js)["\']', body):
            if child not in seen:
                pending.append(child)
        for child in re.findall(r'["\']\./([^"\']+\.js)["\']', body):
            sibling = str(Path(asset).parent / child).replace("\\", "/")
            if sibling not in seen:
                pending.append(sibling)
    return entry, "\n".join(bodies)


def verify(
    base_url: str,
    *,
    production_contract: bool,
    expected_entry: str | None = None,
    expected_source_sha: str | None = None,
    protected: bool = False,
) -> str:
    """Verify routes and the recursively fetched JavaScript artifact."""
    base_url = base_url.rstrip("/")
    print(f"-> verifying {base_url}")
    fetcher = (
        (lambda path: fetch_protected(base_url, "/" + path.lstrip("/")))
        if protected
        else (lambda path: fetch(urllib.parse.urljoin(base_url + "/", path)))
    )
    status, html = fetcher("/")
    if status != 200:
        fail(f"{base_url}/ returned HTTP {status}")
    entry, javascript = _javascript_graph(base_url, html, fetcher)
    if expected_entry and entry != expected_entry:
        fail(f"entry mismatch: expected {expected_entry}, served {entry}")
    if production_contract:
        errors = _production_bundle_contract(
            javascript, expected_source_sha=expected_source_sha
        )
        if errors:
            fail("production bundle " + "; ".join(errors))
    health_status, health_body = fetcher("/health.json")
    if health_status != 200:
        fail(f"{base_url}/health.json returned HTTP {health_status}")
    health_errors = _validate_web_health(
        health_body,
        expected_source_sha=expected_source_sha if production_contract else None,
    )
    if health_errors:
        fail("web health artifact " + "; ".join(health_errors))
    print("  /health.json   semantic contract ok")
    for route in ROUTES:
        route_status, _ = fetcher(route)
        print(f"  {route:<14} {'ok' if route_status == 200 else f'HTTP {route_status}'}")
        if route_status != 200:
            fail(f"{route} returned HTTP {route_status}")
    api_status, api_body = fetcher("/api/health")
    if api_status != 404 or "web origin does not serve API routes" not in api_body:
        fail(
            "web-origin API boundary expected structured HTTP 404, got "
            f"HTTP {api_status}"
        )
    print("  /api/health    structured 404 (not SPA HTML)")
    print(f"  verified entry: {entry}")
    return entry


def promote(deployment_url: str) -> None:
    print(f"-> promoting {deployment_url}")
    result = run(
        [
            _vercel_cli(), "promote", deployment_url, "--yes",
            "--scope", TEAM_SLUG,
        ],
        cwd=WEB, env_extra=PROJECT_ENV,
    )
    if result.returncode != 0:
        fail("`vercel promote` failed; production alias was not intentionally changed")


def rollback() -> None:
    print("-> post-promotion verification failed; requesting Vercel rollback")
    result = run(
        [_vercel_cli(), "rollback", "--yes", "--scope", TEAM_SLUG],
        cwd=WEB,
        env_extra=PROJECT_ENV,
    )
    if result.returncode != 0:
        print("WARNING: automatic rollback failed; run `vercel rollback` immediately")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", action="store_true", help="stage a preview deploy")
    parser.add_argument("--no-build", action="store_true", help="skip local structural build")
    parser.add_argument("--dry-run", action="store_true", help="local structural checks only")
    args = parser.parse_args()

    if not args.no_build:
        build()
    preflight()
    if args.dry_run:
        print("READY: local structure is deployable; sensitive production env was not tested")
        return

    deployment_url = deploy(preview=args.preview)
    entry = verify(
        deployment_url,
        production_contract=not args.preview,
        expected_source_sha=None if args.preview else _source_sha(),
        protected=True,
    )
    if args.preview:
        print(f"READY: verified preview {deployment_url}; production alias untouched")
        return

    verify_production_backend()
    promote(deployment_url)
    try:
        for attempt in range(6):
            try:
                verify(
                    DOMAIN,
                    production_contract=True,
                    expected_entry=entry,
                    expected_source_sha=_source_sha(),
                )
                break
            except SystemExit:
                if attempt == 5:
                    raise
                time.sleep(5)
    except SystemExit:
        rollback()
        raise
    print(f"READY: verified and promoted {deployment_url} to {DOMAIN}")


if __name__ == "__main__":
    main()
