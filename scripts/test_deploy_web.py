from __future__ import annotations

import importlib.util
import shutil
import subprocess
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


def test_backend_contract_requires_health_and_auth_boundary(monkeypatch) -> None:
    responses = [(200, "{}"), (401, "")]
    monkeypatch.setattr(deploy_web, "fetch", lambda _url: responses.pop(0))
    deploy_web.verify_production_backend()


def test_backend_contract_rejects_wrong_api_host(monkeypatch) -> None:
    responses = [(200, "{}"), (404, "")]
    monkeypatch.setattr(deploy_web, "fetch", lambda _url: responses.pop(0))
    with pytest.raises(SystemExit):
        deploy_web.verify_production_backend()


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
