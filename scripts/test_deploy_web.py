from __future__ import annotations

import importlib.util
import io
import json
import shutil
import subprocess
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

SCRIPT = Path(__file__).with_name("deploy-web.py")
SPEC = importlib.util.spec_from_file_location("deploy_web", SCRIPT)
assert SPEC and SPEC.loader
deploy_web = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy_web)


def _result(stdout: str = "https://leaf-web-abc.vercel.app") -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def test_build_resolves_npm_launcher(monkeypatch) -> None:
    runner = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(deploy_web, "run", runner)
    monkeypatch.setattr(deploy_web, "_npm_cli", lambda: "npm.cmd")
    deploy_web.build()
    assert runner.call_args.args[0] == ["npm.cmd", "run", "build"]
    assert runner.call_args.kwargs["cwd"] == deploy_web.WEB


@pytest.mark.parametrize(
    ("preview", "suffix"),
    [(True, []), (False, ["--prod", "--skip-domain"])],
)
def test_deploy_cloud_builds_from_web(monkeypatch, preview, suffix) -> None:
    runner = Mock(return_value=_result())
    monkeypatch.setattr(deploy_web, "run", runner)
    monkeypatch.setattr(deploy_web, "_vercel_cli", lambda: "vercel.cmd")
    monkeypatch.setattr(deploy_web, "_source_sha", lambda: "a" * 40)
    assert deploy_web.deploy(preview=preview) == "https://leaf-web-abc.vercel.app"
    assert runner.call_args.args[0] == [
        "vercel.cmd", "deploy", "--yes", "--build-env",
        f"LEAF_SOURCE_SHA={'a' * 40}",
    ] + suffix
    assert runner.call_args.kwargs["cwd"] == deploy_web.WEB
    assert runner.call_args.kwargs["env_extra"] == deploy_web.PROJECT_ENV


@pytest.mark.parametrize(
    ("javascript", "error"),
    [
        (
            f"http://localhost:8130 {deploy_web.PRODUCTION_API_BASE} leafautomation.us.auth0.com",
            "localhost",
        ),
        ("leafautomation.us.auth0.com", "platform-staging.leafdesign.ai"),
        (deploy_web.PRODUCTION_API_BASE, "leafautomation.us.auth0.com"),
    ],
)
def test_production_contract_rejects_bad_bundle(javascript, error) -> None:
    assert any(error in item for item in deploy_web._production_bundle_contract(javascript))


def test_production_contract_accepts_real_endpoints() -> None:
    assert deploy_web._production_bundle_contract(
        f"{deploy_web.PRODUCTION_API_BASE} leafautomation.us.auth0.com"
    ) == []


def test_production_contract_requires_expected_source_sha() -> None:
    javascript = f"{deploy_web.PRODUCTION_API_BASE} leafautomation.us.auth0.com"
    assert "source SHA" in deploy_web._production_bundle_contract(
        javascript, expected_source_sha="a" * 40
    )[0]
    assert deploy_web._production_bundle_contract(
        javascript + " " + "a" * 40, expected_source_sha="a" * 40
    ) == []


def test_build_emits_exact_health_artifact_and_headers() -> None:
    payload = json.loads((deploy_web.DIST / "health.json").read_text(encoding="utf-8"))
    source_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=deploy_web.REPO, check=True,
        capture_output=True, text=True,
    ).stdout.strip().lower()
    assert payload == {
        "ok": True,
        "service": "leaf-platform-web",
        "component": "frontend",
        "schema_version": 1,
        "source_sha": source_sha,
    }
    config = json.loads((deploy_web.DIST / "vercel.json").read_text(encoding="utf-8"))
    health_headers = next(item for item in config["headers"] if item["source"] == "/health.json")
    headers = {item["key"].lower(): item["value"] for item in health_headers["headers"]}
    assert headers["cache-control"] == "no-store"
    assert headers["content-type"].startswith("application/json")


def test_health_contract_rejects_wrong_identity_and_source() -> None:
    body = json.dumps({
        "ok": True,
        "service": "wrong-service",
        "component": "frontend",
        "schema_version": 1,
        "source_sha": "b" * 40,
    })
    errors = deploy_web._validate_web_health(body, expected_source_sha="a" * 40)
    assert any("service" in item for item in errors)
    assert any("source_sha" in item for item in errors)


def test_backend_contract_requires_health_auth_and_public_demo(monkeypatch) -> None:
    responses = [
        (200, "{}"),
        (401, ""),
        (
            200,
            '{"ok":true,"solve":{"stats":{"panel_count":2345},'
            '"electrical":{"pass":true}}}',
        ),
    ]
    monkeypatch.setattr(deploy_web, "fetch", lambda _url: responses.pop(0))
    deploy_web.verify_production_backend()


def test_backend_contract_rejects_wrong_api_host(monkeypatch) -> None:
    responses = [(200, "{}"), (404, "")]
    monkeypatch.setattr(deploy_web, "fetch", lambda _url: responses.pop(0))
    with pytest.raises(SystemExit):
        deploy_web.verify_production_backend()


@pytest.mark.parametrize(
    "demo_response",
    [
        (404, ""),
        (200, "not-json"),
        (200, '{"ok":true,"solve":{"stats":{"panel_count":0}}}'),
        (200, '{"ok":true,"solve":{"stats":{"panel_count":true}}}'),
        (200, '{"ok":true,"solve":{"stats":{"panel_count":12}}}'),
        (
            200,
            '{"ok":true,"solve":{"stats":{"panel_count":12},'
            '"electrical":{"pass":false}}}',
        ),
        (
            200,
            '{"ok":true,"solve":{"stats":{"panel_count":12},'
            '"electrical":{"pass":1}}}',
        ),
    ],
)
def test_backend_contract_rejects_missing_or_invalid_public_demo(
    monkeypatch, demo_response
) -> None:
    responses = [(200, "{}"), (401, ""), demo_response]
    monkeypatch.setattr(deploy_web, "fetch", lambda _url: responses.pop(0))
    with pytest.raises(SystemExit):
        deploy_web.verify_production_backend()


def test_main_never_promotes_when_backend_contract_fails(monkeypatch) -> None:
    promote = Mock()
    monkeypatch.setattr(sys, "argv", ["deploy-web.py", "--no-build"])
    monkeypatch.setattr(deploy_web, "preflight", Mock())
    monkeypatch.setattr(deploy_web, "deploy", Mock(return_value="https://stage.example"))
    monkeypatch.setattr(deploy_web, "verify", Mock(return_value="assets/index.js"))
    monkeypatch.setattr(deploy_web, "_source_sha", Mock(return_value="a" * 40))
    monkeypatch.setattr(
        deploy_web,
        "verify_production_backend",
        Mock(side_effect=SystemExit(2)),
    )
    monkeypatch.setattr(deploy_web, "promote", promote)

    with pytest.raises(SystemExit):
        deploy_web.main()

    promote.assert_not_called()


def test_promote_uses_verified_deployment(monkeypatch) -> None:
    runner = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(deploy_web, "run", runner)
    monkeypatch.setattr(deploy_web, "_vercel_cli", lambda: "vercel.cmd")
    deploy_web.promote("https://leaf-web-abc.vercel.app")
    assert runner.call_args.args[0] == [
        "vercel.cmd", "promote", "https://leaf-web-abc.vercel.app", "--yes",
        "--scope", deploy_web.TEAM_SLUG,
    ]


def test_deployment_url_uses_last_cli_url() -> None:
    output = "Inspect: https://one.vercel.app\nProduction: https://two.vercel.app"
    assert deploy_web._deployment_url(output) == "https://two.vercel.app"


def test_protected_fetch_pins_existing_project(monkeypatch) -> None:
    runner = Mock(return_value=SimpleNamespace(returncode=0, stdout="body", stderr=""))
    monkeypatch.setattr(deploy_web, "run", runner)
    monkeypatch.setattr(deploy_web, "_vercel_cli", lambda: "vercel.cmd")
    assert deploy_web.fetch_protected("https://stage.vercel.app", "/app") == (200, "body")
    command = runner.call_args.args[0]
    assert command == [
        "vercel.cmd", "curl", "/app", "--deployment",
        "https://stage.vercel.app", "--yes", "--scope", deploy_web.TEAM_SLUG,
        "--", "--fail-with-body",
    ]
    assert runner.call_args.kwargs["env_extra"] == deploy_web.PROJECT_ENV


def test_web_api_boundary_returns_structured_404() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to execute the Vercel function")
    script = r"""
import handler from './api/boundary.js'
let status = null
let body = null
const headers = {}
const response = {
  setHeader(name, value) { headers[name] = value },
  status(value) { status = value; return this },
  json(value) { body = value; return this },
}
handler({}, response)
if (status !== 404) process.exit(2)
if (body?.error !== 'web origin does not serve API routes') process.exit(3)
if (!headers['Content-Type']?.startsWith('application/json')) process.exit(4)
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=deploy_web.WEB,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_fetch_preserves_http_error_body(monkeypatch) -> None:
    body = b'{"error":"web origin does not serve API routes"}'

    def raise_not_found(_request, timeout):
        raise urllib.error.HTTPError(
            "https://example.test/api/health",
            404,
            "Not Found",
            {},
            io.BytesIO(body),
        )

    monkeypatch.setattr(deploy_web.urllib.request, "urlopen", raise_not_found)
    assert deploy_web.fetch("https://example.test/api/health") == (
        404,
        body.decode(),
    )


def test_auth_flow_oracle() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to execute the auth-flow oracle")
    result = subprocess.run(
        [node, "scripts/check_authflow.mjs"],
        cwd=deploy_web.WEB,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "AUTH_FLOW_OK" in result.stdout
