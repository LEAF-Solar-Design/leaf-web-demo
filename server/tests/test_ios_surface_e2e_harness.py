"""Card D-4 (attempt 2): iOS surface e2e harness against the REAL route.

Oracle (frozen, card D-4):
  - E2E against a fixture contract: readiness -> stages -> receipt render,
    driven through the REAL FastAPI route (routers.ios_surface.status) and
    the REAL validate_contract -- not a reimplementation.
  - ios_surface OFF refuses (flip-time proof): with no contract source
    mounted, the REAL route never touches the source; mounting one flips it
    ON; unmounting flips it back OFF -- proven by call count, not just by
    status text, matching card B-C6's flip-time fence precedent.

This file mounts routers.ios_surface into an isolated FastAPI app the same
way server/tests/test_ios_surface_contract.py (card D-1) already does --
that is the established, real way this unmounted-by-default router is
exercised in this repo (server/app.py does not include it either; D-1's own
test is the router's only real caller).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import deps
from routers import ios_surface as router

PROJECT = "proj-1"
REVISION = "r1"

# Same closed vocabulary, same declared order, as routers.ios_surface's own
# _BUILD_STAGES literal (and platform/ios_ship.py's _CONTROLLER_STAGES) --
# not reinvented, just walked in the order the source declares it.
BUILD_STAGES = (
    "SOURCE_APPROVED", "GRANT_READY", "BUNDLE_READY", "MAC_ALLOCATED", "APP_RECORD",
    "XCODE_READY", "SIGNING_READY", "BUILT", "UPLOADED", "COMPLIANCE", "BETA_ASSIGNED",
    "CREDENTIALS_SCRUBBED", "MAC_RELEASED", "RECEIPT",
)

FIXTURE_DIR = Path(__file__).resolve().parent.parent.parent / "harness" / "tests" / "fixtures"


def _raw_contract(**overrides):
    payload = {
        "schema": "leaf.ios-ship-surface.v1", "project_id": PROJECT, "revision": REVISION,
        "reported_at": "2026-08-19T12:00:00+00:00",
        "readiness": {"healthy": True, "launchable": False},
        "build_stage": None, "receipt_id": None,
    }
    payload.update(overrides)
    return payload


def _client():
    app = FastAPI()
    app.include_router(router.router)
    app.dependency_overrides[deps.require_tenant] = lambda: "tenant-a"
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _reset_source():
    router.set_contract_source(None)
    yield
    router.set_contract_source(None)


def _get(client):
    return client.get("/api/ios-surface/status", params={"project_id": PROJECT, "revision": REVISION})


def test_e2e_readiness_then_every_published_stage_then_receipt_render_via_real_route():
    """Drives the REAL route + REAL validate_contract, not a copy of either."""
    client = _client()

    # Readiness: healthy, not yet launchable, no stage published yet.
    router.set_contract_source(lambda scope: _raw_contract())
    readiness = _get(client)
    assert readiness.status_code == 200
    readiness_body = readiness.json()
    assert readiness_body["status"] == "available"
    assert readiness_body["contract"]["readiness"] == {"healthy": True, "launchable": False}
    assert readiness_body["contract"]["build_stage"] is None
    assert readiness_body["contract"]["receipt_id"] is None

    # Stages: walk the full published vocabulary in the ship-lane's own
    # order through the REAL route on every call.
    for stage in BUILD_STAGES:
        is_final = stage == "RECEIPT"
        router.set_contract_source(lambda scope, stage=stage, is_final=is_final: _raw_contract(
            readiness={"healthy": True, "launchable": is_final},
            build_stage=stage,
            receipt_id="receipt-ship-1" if is_final else None,
        ))
        response = _get(client)
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "available"
        assert body["contract"]["build_stage"] == stage
        assert body["contract"]["receipt_id"] == ("receipt-ship-1" if is_final else None)
        assert body["contract"]["readiness"]["launchable"] is is_final

    # Terminal receipt render: capture the REAL validated output so a kept
    # TS/UI half can render the REAL IosSurface.jsx from server-validated
    # JSON -- never a reimplemented validator (card D-4 attempt-2 fix).
    router.set_contract_source(lambda scope: _raw_contract(
        readiness={"healthy": True, "launchable": True},
        build_stage="RECEIPT", receipt_id="receipt-ship-1",
    ))
    receipt = _get(client)
    assert receipt.status_code == 200
    receipt_body = receipt.json()
    assert receipt_body["status"] == "available"
    assert receipt_body["contract"]["build_stage"] == "RECEIPT"
    assert receipt_body["contract"]["receipt_id"] == "receipt-ship-1"

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    (FIXTURE_DIR / "ios_surface_contract.receipt.json").write_text(
        json.dumps(receipt_body["contract"], indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_a_prior_success_is_never_served_stale_via_real_route_when_upstream_later_fails():
    client = _client()
    router.set_contract_source(lambda scope: _raw_contract(build_stage="BUILT"))
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


def test_ios_surface_off_refuses_without_touching_the_source_flip_on_then_off_again():
    """Flip-time fence proof against the REAL route (card B-C6 precedent).

    OFF means no contract source is mounted on the REAL router module --
    the same mechanism server/app.py itself would use to gate this surface,
    since app.py never mounts this router by default either. A call-count
    spy proves the source is never invoked while OFF, is reached exactly
    once per request once flipped ON, and is not touched again once
    flipped back OFF.
    """
    client = _client()
    calls = []

    def spy_source(scope):
        calls.append(scope)
        return _raw_contract(build_stage="BUILT")

    # OFF: router.set_contract_source(None) via the autouse fixture. Refused
    # truthfully, source never touched -- proven by call count.
    for _ in range(3):
        off_response = _get(client)
        assert off_response.status_code == 200
        assert off_response.json() == {
            "ok": True, "status": "unavailable", "reason": "surface_source_unavailable",
            "project_id": PROJECT, "revision": REVISION,
        }
    assert calls == []

    # Flip ON: the identical call now reaches the REAL source through the
    # REAL route.
    router.set_contract_source(spy_source)
    on_response = _get(client)
    assert on_response.status_code == 200
    assert on_response.json()["status"] == "available"
    assert len(calls) == 1

    # Flip back OFF: refused again at the exact same call site, no further
    # source calls -- the fence, not the source's own behavior, is what
    # changed.
    router.set_contract_source(None)
    for _ in range(3):
        off_again = _get(client)
        assert off_again.status_code == 200
        assert off_again.json()["reason"] == "surface_source_unavailable"
    assert len(calls) == 1
