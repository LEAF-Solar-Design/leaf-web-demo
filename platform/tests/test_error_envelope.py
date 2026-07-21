"""Section-10 error envelope shape for the platform router's HTTPException raise
sites (platform/api.py + platform/deps.py).

The shared ``client`` fixture in conftest.py mounts the platform router on a
bare ``FastAPI()`` with NO exception handler installed, so a raised
HTTPException there still serializes as FastAPI's default ``{"detail": ...}``
body — never the section-10 ``{ok, error:{error_code,message,retryable},
degraded_mode}`` shape. This file instead wires the app the same way
server/app.py actually does in production: ``install_error_handlers(app)``
BEFORE ``app.include_router(platform_router)`` (server/app.py:73, :108-115) —
so the fixed ``server/envelopes.py::_http_exc_handler`` is genuinely exercised
end-to-end, not just imported.

server/envelopes.py is loaded by explicit file path (mirrors platform/deps.py's
``_server_auth()`` loader) so importing it never needs ``server/`` on
sys.path and can't re-trigger the stdlib ``platform`` shadow that
platform/tests/conftest.py works around.

Run:  cd C:/tmp/leaf-web-demo/platform && python -m pytest tests/test_error_envelope.py -q
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from leaf_platform.api import router as platform_router

_SERVER_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "server"


def _load_envelopes():
    spec = importlib.util.spec_from_file_location(
        "leaf_server_envelopes_for_platform_tests", _SERVER_DIR / "envelopes.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


envelopes = _load_envelopes()


@pytest.fixture
def wired_client():
    """The production wiring order: handlers installed, then the router mounted."""
    app = FastAPI()
    envelopes.install_error_handlers(app)
    app.include_router(platform_router)
    return TestClient(app)


def _assert_section10_shape(body):
    assert body["ok"] is False
    assert isinstance(body["degraded_mode"], bool)
    assert body["error"] is not None
    assert set(body["error"].keys()) == {"error_code", "message", "retryable"}
    assert body["error"]["message"]
    assert isinstance(body["error"]["retryable"], bool)


# --------------------------------------------------------------------------- #
# the acceptance criterion: GET /api/projects with no X-Org-Id
# --------------------------------------------------------------------------- #
def test_missing_org_header_is_section10_shape_non_internal(wired_client):
    r = wired_client.get("/api/projects")
    assert r.status_code == 400, r.text
    body = r.json()
    _assert_section10_shape(body)
    assert body["error"]["error_code"] != envelopes.ErrorCode.INTERNAL
    assert body["error"]["error_code"] == envelopes.ErrorCode.BAD_PARAMS


def test_invalid_org_header_is_section10_bad_params(wired_client):
    r = wired_client.get("/api/projects", headers={"X-Org-Id": "not-a-uuid"})
    assert r.status_code == 400, r.text
    body = r.json()
    _assert_section10_shape(body)
    assert body["error"]["error_code"] == envelopes.ErrorCode.BAD_PARAMS


# --------------------------------------------------------------------------- #
# further platform-router HTTPException sites: same non-INTERNAL discipline
# --------------------------------------------------------------------------- #
def test_unknown_project_id_is_section10_bad_params(wired_client, make_org):
    org = make_org(name="Error Envelope Org")
    hdr = {"X-Org-Id": str(org.org_id)}
    r = wired_client.get(
        "/api/projects/11111111-1111-1111-1111-111111111111", headers=hdr
    )
    assert r.status_code == 404, r.text
    body = r.json()
    _assert_section10_shape(body)
    assert body["error"]["error_code"] == envelopes.ErrorCode.BAD_PARAMS


def test_invalid_job_kind_is_section10_bad_params(wired_client, make_org):
    org = make_org(name="Bad Kind Envelope Org")
    hdr = {"X-Org-Id": str(org.org_id)}
    pid = wired_client.post(
        "/api/projects", json={"name": "P"}, headers=hdr
    ).json()["project"]["project_id"]
    r = wired_client.post(
        f"/api/projects/{pid}/jobs", json={"kind": "nonsense"}, headers=hdr
    )
    assert r.status_code == 422, r.text
    body = r.json()
    _assert_section10_shape(body)
    assert body["error"]["error_code"] == envelopes.ErrorCode.BAD_PARAMS


def test_offboard_wrong_admin_token_is_section10_shape(wired_client, make_org, monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_TOKEN", "s3cret-admin-token")
    org = make_org(name="Wrong Token Envelope Org")
    r = wired_client.delete(
        f"/api/orgs/{org.org_id}", headers={"X-Admin-Token": "not-the-token"}
    )
    assert r.status_code == 403, r.text
    body = r.json()
    _assert_section10_shape(body)
    assert body["error"]["error_code"] != envelopes.ErrorCode.INTERNAL


def test_offboard_unconfigured_admin_token_is_section10_retryable(
    wired_client, make_org, monkeypatch
):
    monkeypatch.delenv("PLATFORM_ADMIN_TOKEN", raising=False)
    org = make_org(name="Unset Token Envelope Org")
    r = wired_client.delete(f"/api/orgs/{org.org_id}", headers={"X-Admin-Token": "anything"})
    assert r.status_code == 503, r.text
    body = r.json()
    _assert_section10_shape(body)
    assert body["error"]["retryable"] is True
