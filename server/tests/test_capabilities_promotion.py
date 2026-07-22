"""
Binary acceptance for registry-promotion groundwork (operator ruling R-A,
2026-07-22): the "stringing" capability family (server/capability_families.json)
and the standalone GET /api/capabilities/promotion/stringing router
(server/routers/capabilities_promotion.py).

Covers:
  * capability_families.json declares "stringing" (autofill-string-targets +
    string-autofill-opt) and "placement" (inverter-placement + combiner-placement,
    provisional) per the schema catalog.py already reads.
  * the families CONSUMER (catalog.build_catalog) fails CLOSED on a family
    member that has no matching registered tool: the family is silently
    OMITTED from the built catalog, never an exception and never a ghost
    entry with fabricated capabilities. Verified BOTH with the real (today,
    tool-absent) catalog and with a synthetic tool list that DOES register
    the name, proving the wiring is correct once R2 merges the real tool.
  * GET /api/capabilities/promotion/stringing fails closed against the REAL
    catalog TODAY (autofill-string-targets is not yet registered anywhere in
    this worktree) -> state "locked", no fabricated "implemented".
  * with the catalog forced to report the tool present (monkeypatched
    deps.find_tool), the endpoint reports state "registered".
  * evidence[] cites a REAL receipt file (path + sha256 that matches the file
    on disk) when one exists for the tool name, and stays [] when none does
    (today's real-repo case) — never a fabricated digest.

Run:  cd server && python -m pytest tests/test_capabilities_promotion.py -q
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import catalog  # noqa: E402
import deps  # noqa: E402
import routers.capabilities_promotion as promo  # noqa: E402

FAMILIES = json.loads(catalog.FAMILIES_FILE.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# capability_families.json schema/content
# --------------------------------------------------------------------------- #
def test_stringing_family_declared_with_both_names():
    ids = {f["family_id"] for f in FAMILIES["families"]}
    assert "stringing" in ids
    tf = FAMILIES["tool_families"]
    assert tf["autofill-string-targets"] == "stringing"
    assert tf["string-autofill-opt"] == "stringing"


def test_placement_family_declared_provisional():
    ids = {f["family_id"] for f in FAMILIES["families"]}
    assert "placement" in ids
    tf = FAMILIES["tool_families"]
    assert tf.get("inverter-placement") == "placement"
    assert tf.get("combiner-placement") == "placement"


# --------------------------------------------------------------------------- #
# catalog.build_catalog fails CLOSED on a missing family member
# --------------------------------------------------------------------------- #
def test_build_catalog_omits_stringing_when_no_tool_registered():
    """The real fail-closed behavior this task had to verify (not assume):
    a family declared in config with NO matching registered tool is silently
    dropped from the built catalog -- no exception, no empty/ghost family."""
    families = catalog.build_catalog([], include_internal=False)
    ids = [f["family_id"] for f in families]
    assert "stringing" not in ids
    assert "placement" not in ids


def test_build_catalog_folds_autofill_string_targets_once_registered():
    """Proves the wiring: once a tool literally named autofill-string-targets
    is registered (this is what R2's merge will add), it folds into
    "stringing" with no further config change needed."""
    fake_tools = [{"name": "autofill-string-targets", "version": "0.1.0",
                   "description": "heuristic string-target autofill",
                   "capabilities": ["drawing.write"]}]
    families = catalog.build_catalog(fake_tools, include_internal=False)
    stringing = [f for f in families if f["family_id"] == "stringing"]
    assert len(stringing) == 1
    assert [c["name"] for c in stringing[0]["capabilities"]] == ["autofill-string-targets"]


def test_build_catalog_folds_string_autofill_opt_into_same_family():
    """The second R-A name folds into the SAME family as the heuristic."""
    fake_tools = [{"name": "string-autofill-opt", "version": "0.1.0",
                   "description": "real optimizer", "capabilities": ["drawing.write"]}]
    families = catalog.build_catalog(fake_tools, include_internal=False)
    stringing = [f for f in families if f["family_id"] == "stringing"]
    assert len(stringing) == 1
    assert stringing[0]["capabilities"][0]["name"] == "string-autofill-opt"


# --------------------------------------------------------------------------- #
# GET /api/capabilities/promotion/stringing -- fail-closed against the REAL
# catalog (autofill-string-targets is not registered anywhere in THIS worktree)
# --------------------------------------------------------------------------- #
def test_real_catalog_has_no_autofill_string_targets_yet():
    """Baseline fact this whole router leans on -- if this ever starts
    failing, R2's merge landed and the "locked" test below needs updating."""
    assert deps.find_tool("autofill-string-targets") is None


def test_availability_locked_against_real_catalog():
    entry = promo.availability_for(promo.STRINGING_TOOL)
    assert entry["state"] == "locked"
    assert entry["implementationState"] == "not_registered"
    assert entry["reasonCode"] == "not_registered_in_catalog"
    assert entry["familyId"] == "stringing"
    assert entry["evidence"] == []  # no receipt in data/ names this tool -- never fabricated
    assert entry["authority"] == "leaf-server-catalog"
    assert entry["productCapability"] == "drawing.solve.strings"


def test_availability_registered_when_catalog_has_the_tool(monkeypatch):
    monkeypatch.setattr(deps, "find_tool",
                         lambda name, tenant_id=deps.DEFAULT_TENANT:
                         {"name": name} if name == promo.STRINGING_TOOL else None)
    entry = promo.availability_for(promo.STRINGING_TOOL)
    assert entry["state"] == "registered"
    assert entry["implementationState"] == "implemented"
    assert entry["reasonCode"] is None


def test_locked_tool_returns_empty_evidence_even_with_matching_receipt(tmp_path, monkeypatch):
    """The discriminating case for the fail-closed rule: a receipt NAMES the
    tool but the tool is not registered -- evidence must still be []."""
    receipt = {"tool_name": promo.STRINGING_TOOL, "pass": True}
    (tmp_path / "fake_receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    monkeypatch.setattr(promo, "DATA_DIR", tmp_path)
    monkeypatch.setattr(deps, "PROJECT_ROOT", tmp_path.parent)
    entry = promo.availability_for(promo.STRINGING_TOOL)
    assert entry["state"] == "locked"
    assert entry["evidence"] == []


def test_evidence_cites_a_real_receipt_with_matching_sha256(tmp_path, monkeypatch):
    """When a receipt DOES name the tool, evidence[] must cite the real file
    path and a sha256 that matches the file on disk byte-for-byte."""
    receipt = {"tool_name": promo.STRINGING_TOOL, "pass": True}
    (tmp_path / "fake_receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    monkeypatch.setattr(promo, "DATA_DIR", tmp_path)
    monkeypatch.setattr(deps, "PROJECT_ROOT", tmp_path.parent)
    # Evidence requires REGISTRATION (locked tools must return evidence: []),
    # so register the tool before expecting receipts to surface.
    monkeypatch.setattr(deps, "find_tool",
                        lambda name, tenant=None: {"name": name}
                        if name == promo.STRINGING_TOOL else None)
    entry = promo.availability_for(promo.STRINGING_TOOL)
    assert entry["state"] == "registered"
    assert len(entry["evidence"]) == 1
    ev = entry["evidence"][0]
    expected_sha = hashlib.sha256((tmp_path / "fake_receipt.json").read_bytes()).hexdigest()
    assert ev["digest"]["value"] == expected_sha
    assert ev["digest"]["algorithm"] == "sha256"
    assert "fake_receipt.json" in ev["uri"]


# --------------------------------------------------------------------------- #
# HTTP wiring (standalone TestClient, same pattern as tests/test_site.py --
# this router is NOT mounted in app.py, see module docstring)
# --------------------------------------------------------------------------- #
def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from envelopes import install_error_handlers

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(promo.router)
    return TestClient(app, raise_server_exceptions=False)


def test_http_endpoint_returns_locked_availability():
    resp = _client().get("/api/capabilities/promotion/stringing")
    assert resp.status_code == 200
    body = resp.json()
    assert body["error"] is None
    assert body["availability"]["state"] == "locked"
    assert body["availability"]["toolName"] == "autofill-string-targets"
