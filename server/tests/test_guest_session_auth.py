"""
Guest-session identity (§19, live-auth mode) + the guest entitlement tier.

The HMAC guest token is the ONLY way a signed-out visitor holds an identity
when LEAF_AUTH_LIVE=1: mint/verify roundtrip, expiry, tamper and prefix
rejection, the unset-secret 503 (never an unsigned identity), and the tier
that denies everything but upload. Also asserts the json<->hardcoded policy
mirror for ALL tiers (the §17 mirror discipline extended by `upload`).

Run:  cd server && python -m pytest tests/test_guest_session_auth.py -q
"""
from __future__ import annotations

import io
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
import entitlements
import guest_uploads

DXF = ("0\nSECTION\n2\nENTITIES\n0\nLWPOLYLINE\n5\nAB\n8\nL1\n70\n1\n"
       "10\n1.0\n20\n2.0\n10\n3.0\n20\n4.0\n0\nENDSEC\n0\nEOF\n").encode()


@pytest.fixture()
def live_client(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_GUEST_STORE_DIR", str(tmp_path / "guest"))
    monkeypatch.setenv("LEAF_UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_GUEST_SECRET", "test-secret-not-a-real-one")
    guest_uploads._reset_rate_state()
    monkeypatch.setattr(
        guest_uploads, "start_extraction_thread",
        lambda tenant_id, drawing_id, ext: guest_uploads.run_extraction(
            tenant_id, drawing_id, ext))
    return TestClient(app_module.app)


# --------------------------------------------------------------------------- #
# token unit behavior
# --------------------------------------------------------------------------- #
def test_mint_verify_roundtrip(monkeypatch):
    monkeypatch.setenv("LEAF_GUEST_SECRET", "s1")
    tid = guest_uploads.mint_guest_tenant_id()
    token = guest_uploads.mint_guest_session(tid, int(time.time()) + 60)
    assert guest_uploads.verify_guest_session(token) == tid


def test_expired_token_rejected(monkeypatch):
    monkeypatch.setenv("LEAF_GUEST_SECRET", "s1")
    tid = guest_uploads.mint_guest_tenant_id()
    token = guest_uploads.mint_guest_session(tid, int(time.time()) - 1)
    assert guest_uploads.verify_guest_session(token) is None


def test_tampered_token_rejected(monkeypatch):
    monkeypatch.setenv("LEAF_GUEST_SECRET", "s1")
    tid = guest_uploads.mint_guest_tenant_id()
    token = guest_uploads.mint_guest_session(tid, int(time.time()) + 60)
    other = token[:-4] + ("0000" if not token.endswith("0000") else "1111")
    assert guest_uploads.verify_guest_session(other) is None
    # tenant swap fails too (signature covers the id)
    parts = token.split(".")
    parts[0] = "guest-other0000"
    assert guest_uploads.verify_guest_session(".".join(parts)) is None


def test_non_guest_prefix_rejected(monkeypatch):
    monkeypatch.setenv("LEAF_GUEST_SECRET", "s1")
    token = guest_uploads.mint_guest_session("acme-solar", int(time.time()) + 60)
    assert token is not None  # minting is dumb on purpose; VERIFY is the gate
    assert guest_uploads.verify_guest_session(token) is None


def test_unset_secret_means_no_tokens(monkeypatch):
    monkeypatch.delenv("LEAF_GUEST_SECRET", raising=False)
    assert guest_uploads.mint_guest_session("guest-x", int(time.time()) + 60) is None
    assert guest_uploads.verify_guest_session("guest-x.9999999999.abcd") is None


# --------------------------------------------------------------------------- #
# live-mode endpoint behavior
# --------------------------------------------------------------------------- #
def test_live_guest_upload_mints_usable_session(live_client):
    r = live_client.post("/api/drawings/upload",
                         files={"file": ("f.dxf", io.BytesIO(DXF))})
    assert r.status_code == 202
    body = r.json()
    assert body["tenant_kind"] == "guest"
    token = body["guest_session"]
    assert token and token.startswith(body["tenant_id"] + ".")

    # the token is a real identity for follow-up reads
    s = live_client.get(f"/api/drawings/{body['drawing_id']}/upload-status",
                        headers={"X-Guest-Session": token})
    assert s.status_code == 200
    assert s.json()["status"] == "ready"
    assert s.json()["tier"] == "guest"  # tenant_echo carries the resolved tier

    i = live_client.get(f"/api/drawings/{body['drawing_id']}/intake",
                        headers={"X-Guest-Session": token})
    assert i.status_code == 200


def test_live_guest_session_reused_on_second_upload(live_client):
    r1 = live_client.post("/api/drawings/upload",
                          files={"file": ("f.dxf", io.BytesIO(DXF))})
    token = r1.json()["guest_session"]
    r2 = live_client.post("/api/drawings/upload",
                          files={"file": ("g.dxf", io.BytesIO(DXF))},
                          headers={"X-Guest-Session": token})
    assert r2.status_code == 202
    assert r2.json()["tenant_id"] == r1.json()["tenant_id"]  # same guest tenant


def test_live_without_guest_secret_503(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.delenv("LEAF_GUEST_SECRET", raising=False)
    monkeypatch.setenv("LEAF_GUEST_STORE_DIR", str(tmp_path / "guest"))
    monkeypatch.setenv("LEAF_UPLOADS_DIR", str(tmp_path / "uploads"))
    guest_uploads._reset_rate_state()
    client = TestClient(app_module.app)
    r = client.post("/api/drawings/upload",
                    files={"file": ("f.dxf", io.BytesIO(DXF))})
    assert r.status_code == 503
    assert "LEAF_GUEST_SECRET" in r.json()["error"]["message"]


def test_live_invalid_session_falls_to_honest_401(live_client):
    r = live_client.get("/api/drawings/u-x/upload-status",
                        headers={"X-Guest-Session": "guest-x.99.tampered"})
    assert r.status_code == 401  # fell through to the JWT path, no bearer


def test_live_guest_token_is_upload_only_allowlist(live_client):
    """Round-1 MAJOR: a guest token must NOT be a general-purpose identity.
    Off-allowlist routes 403 with the boundary named; allowlisted reads work."""
    r = live_client.post("/api/drawings/upload",
                         files={"file": ("f.dxf", io.BytesIO(DXF))})
    token = r.json()["guest_session"]
    did = r.json()["drawing_id"]
    h = {"X-Guest-Session": token}

    # allowlisted: intake, upload-status, versions
    assert live_client.get(f"/api/drawings/{did}/intake", headers=h).status_code == 200
    assert live_client.get(f"/api/drawings/{did}/upload-status", headers=h).status_code == 200
    assert live_client.get(f"/api/drawings/{did}/versions", headers=h).status_code == 200

    # everything else: 403 naming the boundary — sessions, tenant grants,
    # drawing mutations, the legacy session route, jobs
    for method, path in [
        ("post", f"/api/drawings/{did}/undo"),
        ("post", f"/api/drawings/{did}/redo"),
        ("post", f"/api/drawings/{did}/checkout"),
        ("get", "/api/session"),
        ("get", "/api/jobs"),
        ("post", "/api/sessions"),
        ("get", "/api/usage"),
    ]:
        response = getattr(live_client, method)(path, headers=h)
        assert response.status_code == 403, (path, response.status_code)
        assert "upload-only" in response.text


def test_live_guest_cannot_run_tools(live_client):
    r = live_client.post("/api/drawings/upload",
                         files={"file": ("f.dxf", io.BytesIO(DXF))})
    token = r.json()["guest_session"]
    run = live_client.post("/api/run",
                           json={"tool": "count-by-layer", "params": {}},
                           headers={"X-Guest-Session": token})
    # The route ALLOWLIST in require_tenant denies before the per-route
    # entitlement gate even runs — defense in depth; either layer alone
    # would already deny (the guest tier grants no run capability).
    assert run.status_code == 403
    assert "upload-only" in run.json()["error"]["message"]


# --------------------------------------------------------------------------- #
# policy mirror discipline
# --------------------------------------------------------------------------- #
def test_entitlements_json_mirrors_hardcoded_defaults():
    path = Path(__file__).resolve().parent.parent / "entitlements.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    tiers = {k: v for k, v in raw.items() if not k.startswith("_")}
    assert tiers == entitlements._HARDCODED_DEFAULTS, \
        "entitlements.json and _HARDCODED_DEFAULTS must mirror byte-for-byte"


def test_guest_tier_denies_everything_but_upload():
    caps = entitlements.entitlements_for("guest")
    assert caps["upload"] is True
    denied = {k: v for k, v in caps.items() if k != "upload"}
    assert all(v is False for v in denied.values()), denied
