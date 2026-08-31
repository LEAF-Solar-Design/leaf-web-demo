"""Card F-4: the truthful CAD-engine selector on the tenant capability
contract.

The program's standing terminal receipt says "no CAD engine is enabled and
the authenticated tenant-scoped capability/catalog contract exposes no CAD
engine selector". These tests pin the selector that supersedes it — and pin
it TRUTHFUL IN BOTH STATES:

  - flag off: enabled false, NO engine named, no notice — the contract never
    advertises capability that is not enabled;
  - flag on: the engine, its EXACT consumed revision (locked against the
    vendored Cargo.toml rev pin so a re-pin cannot leave the selector
    lying), its license posture, and the NOTICE line the license review's
    binding condition 1 requires on the attributions surface (served from
    here at runtime because the client tree may not name the engine).

Run:  cd server && python -m pytest tests/test_engine_selector.py -q
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import deps  # noqa: E402
from routers import capabilities  # noqa: E402

CARGO = SERVER_DIR.parent / "vendor" / "acadrust-worker" / "Cargo.toml"


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(capabilities.router)
    app.dependency_overrides[deps.require_tenant] = lambda: "tenant-selector-test"
    return TestClient(app)


def test_flag_off_names_no_engine(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(capabilities.FLAG_CAD_EDIT, raising=False)
    selector = capabilities.cad_engine_selector()
    assert selector == {"enabled": False, "engine": None}


def test_flag_on_names_engine_revision_license_and_notice(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(capabilities.FLAG_CAD_EDIT, "1")
    selector = capabilities.cad_engine_selector()
    assert selector["enabled"] is True
    assert selector["engine"] == "acadrust"
    assert selector["revision"] == capabilities.CAD_ENGINE_REVISION
    assert selector["license"] == "MPL-2.0"
    assert "Mozilla Public License 2.0" in selector["notice"]
    assert "acadrust" in selector["notice"]


def test_selector_revision_is_locked_to_the_vendored_rev_pin():
    cargo = CARGO.read_text(encoding="utf-8")
    match = re.search(r'rev\s*=\s*"([0-9a-f]{7,40})"', cargo)
    assert match, "vendored Cargo.toml lost its rev pin"
    assert capabilities.CAD_ENGINE_REVISION == match.group(1), (
        "the selector's advertised revision drifted from the vendored rev "
        "pin — update CAD_ENGINE_REVISION with the re-pin (and re-run the "
        "corpus per the license review's tripwire)"
    )


def test_capabilities_route_carries_the_selector_in_both_states(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv(capabilities.FLAG_CAD_EDIT, raising=False)
    body = client.get("/api/capabilities").json()
    assert body["cad_engine"] == {"enabled": False, "engine": None}

    monkeypatch.setenv(capabilities.FLAG_CAD_EDIT, "1")
    body = client.get("/api/capabilities").json()
    assert body["cad_engine"]["enabled"] is True
    assert body["cad_engine"]["engine"] == "acadrust"
    # The families contract is untouched by the selector's presence.
    assert "families" in body
