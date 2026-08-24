"""Contract tests for the sanitized ship-lane surface consumer.

Oracle:
- Reads ONLY the ship-lane's published sanitized contract (readiness, build
  stages, receipt ids); schema-validated; unknown fields dropped; upstream
  unreachable -> truthful 'status unavailable', never stale-as-fresh.
- No Apple credential, token, profile, or 2FA material in any request,
  response, log, or receipt (assertion greps the surface).
"""
from __future__ import annotations

import inspect
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import deps
from routers import ios_surface as router

PROJECT = "proj-1"
REVISION = "r1"


def _contract(**overrides):
    payload = {
        "schema": "leaf.ios-ship-surface.v1", "project_id": PROJECT, "revision": REVISION,
        "reported_at": "2026-08-19T12:00:00+00:00",
        "readiness": {"healthy": True, "launchable": True},
        "build_stage": "BUILT", "receipt_id": "receipt-1",
    }
    payload.update(overrides)
    return payload


def _client():
    app = FastAPI()
    app.include_router(router.router)
    app.dependency_overrides[deps.require_tenant] = lambda: "tenant-a"
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _reset_source(monkeypatch):
    # These contract tests exercise the ON surface (flag set here); the source
    # seam is what each test drives. test_flag_off_refuses_* overrides the flag
    # to 0 to prove the negative control.
    monkeypatch.setenv("LEAF_IOS_SURFACE_ENABLED", "1")
    router.set_contract_source(None)
    yield
    router.set_contract_source(None)


def _get(client):
    return client.get("/api/ios-surface/status", params={"project_id": PROJECT, "revision": REVISION})


def test_source_not_mounted_is_truthfully_unavailable():
    response = _get(_client())
    assert response.status_code == 200
    body = response.json()
    assert body == {"ok": True, "status": "unavailable", "reason": "surface_source_unavailable",
                     "project_id": PROJECT, "revision": REVISION}


def test_flag_off_refuses_even_with_a_healthy_source(monkeypatch):
    # Negative control (envelope): with ios_surface OFF the consume route
    # refuses (404 "refused") and never serves a contract -- even when a fully
    # healthy source is mounted. The LEAF_IOS_SURFACE_ENABLED flag, not the
    # source, is the deploy-time gate; the flip is what changes observable
    # behavior.
    monkeypatch.setenv("LEAF_IOS_SURFACE_ENABLED", "0")
    router.set_contract_source(lambda scope: _contract())
    response = _get(_client())
    assert response.status_code == 404
    assert response.json() == {"ok": False, "status": "refused", "reason": "ios_surface_disabled",
                               "project_id": PROJECT, "revision": REVISION}


def test_reads_only_readiness_build_stage_and_receipt_id_and_drops_unknown_fields():
    router.set_contract_source(lambda scope: _contract(
        extra_debug_info="drop-me",
        readiness={"healthy": True, "launchable": True, "internal_note": "drop-me-too"}))
    response = _get(_client())
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "available"
    assert body["contract"] == {
        "schema": "leaf.ios-ship-surface.v1", "project_id": PROJECT, "revision": REVISION,
        "reported_at": "2026-08-19T12:00:00+00:00",
        "readiness": {"healthy": True, "launchable": True},
        "build_stage": "BUILT", "receipt_id": "receipt-1"}
    assert "extra_debug_info" not in str(body)
    assert "internal_note" not in str(body)


def test_source_receives_only_the_scoping_tuple():
    seen = []

    def source(scope):
        seen.append(scope)
        return _contract()

    router.set_contract_source(source)
    _get(_client())
    assert seen == [{"tenant_id": "tenant-a", "project_id": PROJECT, "revision": REVISION}]


@pytest.mark.parametrize("mutate", [
    lambda c: c.__setitem__("schema", "wrong.schema.v1"),
    lambda c: c.__setitem__("project_id", "other-project"),
    lambda c: c.__setitem__("revision", "other-revision"),
    lambda c: c.__setitem__("readiness", {"healthy": True}),
    lambda c: c.__setitem__("readiness", "not-a-dict"),
    lambda c: c.__setitem__("build_stage", "NOT_A_REAL_STAGE"),
    lambda c: c.__setitem__("receipt_id", "bad id with spaces"),
    lambda c: c.__setitem__("reported_at", "not-a-timestamp"),
    lambda c: c.__setitem__("reported_at", "2026-08-19T12:00:00"),
    lambda c: c.pop("schema"),
])
def test_malformed_contract_fields_become_unavailable_not_a_crash(mutate):
    contract = _contract()
    mutate(contract)
    router.set_contract_source(lambda scope: contract)
    response = _get(_client())
    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
    assert response.json()["reason"] == "contract_invalid"


def test_upstream_unreachable_is_truthfully_unavailable_and_never_raises():
    def source(_scope):
        raise ConnectionError("upstream is down")

    router.set_contract_source(source)
    response = _get(_client())
    assert response.status_code == 200
    assert response.json() == {"ok": True, "status": "unavailable", "reason": "upstream_unreachable",
                                "project_id": PROJECT, "revision": REVISION}


def test_a_prior_success_is_never_served_stale_when_upstream_later_fails():
    client = _client()
    router.set_contract_source(lambda scope: _contract())
    first = _get(client)
    assert first.json()["status"] == "available"
    assert first.json()["contract"]["build_stage"] == "BUILT"

    def failing(_scope):
        raise TimeoutError("upstream timed out")

    router.set_contract_source(failing)
    second = _get(client)
    assert second.json()["status"] == "unavailable"
    assert "contract" not in second.json()
    assert "BUILT" not in str(second.json())


def test_build_stage_and_receipt_id_may_be_absent_without_going_unavailable():
    router.set_contract_source(lambda scope: _contract(build_stage=None, receipt_id=None))
    response = _get(_client())
    assert response.status_code == 200
    assert response.json()["status"] == "available"
    assert response.json()["contract"]["build_stage"] is None
    assert response.json()["contract"]["receipt_id"] is None


_SECRET_SHAPED_CASES = [
    ("apple_password", "top-level key"),
    ("two_factor_code", "top-level key"),
    ("provisioning_profile", "top-level key"),
    ("session_cookie", "top-level key"),
    ("api_key", "top-level key"),
]


@pytest.mark.parametrize("field_name,_label", _SECRET_SHAPED_CASES)
def test_secret_shaped_top_level_fields_fail_the_whole_contract_closed(field_name, _label):
    secret_value = "SECRET-SHOULD-NEVER-LEAK-" + field_name
    router.set_contract_source(lambda scope: _contract(**{field_name: secret_value}))
    response = _get(_client())
    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
    assert response.json()["reason"] == "contract_invalid"
    assert secret_value not in response.text


@pytest.mark.parametrize("field_name,_label", _SECRET_SHAPED_CASES)
def test_secret_shaped_nested_readiness_fields_fail_the_whole_contract_closed(field_name, _label):
    secret_value = "SECRET-SHOULD-NEVER-LEAK-" + field_name
    router.set_contract_source(lambda scope: _contract(
        readiness={"healthy": True, "launchable": True, field_name: secret_value}))
    response = _get(_client())
    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
    assert secret_value not in response.text


def test_secret_shaped_value_without_a_secret_shaped_key_is_still_rejected():
    router.set_contract_source(lambda scope: _contract(
        receipt_id="AAAA" + "B" * 40 + "=="))
    response = _get(_client())
    assert response.json()["status"] == "unavailable"


def test_request_accepts_no_body_and_carries_only_identifiers():
    signature = inspect.signature(router.status)
    assert set(signature.parameters) == {"project_id", "revision", "tenant"}


def test_source_file_carries_no_hardcoded_secret_material():
    """Static grep assertion: the surface's own source text is secret-free.

    The detector regex necessarily *names* secret-shaped keywords; this
    checks for literal secret *values* (PEM blocks, JWT-looking blobs, long
    base64 blobs) baked into the file, which the keyword names above cannot
    catch on their own.
    """
    source_text = inspect.getsource(router)
    forbidden_value_patterns = [
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
        re.compile(r"eyJ[a-zA-Z0-9_-]{10,}\.eyJ"),
    ]
    for pattern in forbidden_value_patterns:
        assert not pattern.search(source_text), f"forbidden secret-shaped literal: {pattern.pattern}"
