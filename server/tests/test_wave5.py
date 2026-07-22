"""
Binary acceptance for WAVE 5 (backend) — enterprise readiness (CONTRACT-ADDENDUM §17).

Two contracts:

  A. BYO API key as a grant kind (app proxy).  POST /api/tenant/claude-grant accepts an
     optional `kind`; when omitted the harness AUTO-DETECTS from the token prefix. Status
     (POST/GET) echoes the linked `kind` — NEVER the token. Exercised against a STUB
     harness that records the forwarded token + kind (the real TS auto-detect/env-injection
     is covered harness-side: harness/test/grantStore.test.ts + runnerEnv.test.ts).

  B. Tier-driven entitlement enforcement (MATRIX gap 6).  The Auth0 `tier` claim now DRIVES
     capability enforcement server-side:
       * POST /api/run rejects a drawing.write tool when the tier lacks `run_write` (403);
       * POST /api/author rejects when the tier lacks `build` (403);
       * GET  /api/entitlements returns the tier's policy.
     Driven against REAL endpoints with locally-signed RS256 tier claims (LEAF_AUTH_LIVE=1,
     APS_LIVE=0), plus the off-auth demo path (unchanged; tier "demo" = full access).

Hermetic: FAKE tokens + a throwaway RS256 keypair (no live Auth0, no real SDK, no live APS,
no network). NEVER touches the real grant at C:/tmp/hosted-oauth-spike/.grant/token.

Run:  cd server && python -m pytest tests/test_wave5.py -q
"""
from __future__ import annotations

# Cache the STDLIB `platform` module BEFORE PROJECT_ROOT lands on sys.path (mirrors wave4).
import platform as _stdlib_platform  # noqa: E402
_stdlib_platform.python_implementation()

import http.server  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

import jsonschema  # noqa: E402
import jwt  # noqa: E402
import pytest  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from jwt.algorithms import RSAAlgorithm  # noqa: E402

SERVER_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SERVER_DIR.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

# route the jobs SQLite DB to a throwaway dir BEFORE `jobs`/`app` import anywhere.
os.environ.setdefault("JOBS_DB", str(Path(tempfile.mkdtemp(prefix="wave5-jobs-")) / "jobs.db"))

ENVELOPE_SCHEMA = json.loads((SERVER_DIR / "envelope_schema.json").read_text(encoding="utf-8"))
TENANTS_FILE = PROJECT_ROOT / "data" / "tenants.sample.json"

# fake grant tokens (never real)
FAKE_OAUTH = "sk-ant-oat01-FAKE-not-a-real-token-wave5-oat"
FAKE_API = "sk-ant-api03-FAKE-not-a-real-key-wave5-api"
FAKE_PLAIN = "FAKE-plain-no-prefix-wave5-000111"

# --------------------------------------------------------------------------- #
# local RS256 keypair + JWKS (no live Auth0) — same pattern as test_auth.py
# --------------------------------------------------------------------------- #
ISS = "https://leaf-wave5.example/"
AUD = "https://api.leaf-wave5.example"
NS = "https://leafdesign.ai/"
KID = "leaf-wave5-key-1"

_priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_priv_pem = _priv.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
)
_jwk = json.loads(RSAAlgorithm.to_jwk(_priv.public_key()))
_jwk.update({"kid": KID, "alg": "RS256", "use": "sig"})
_TMP = Path(tempfile.mkdtemp(prefix="leaf-wave5-auth-"))
_JWKS_FILE = _TMP / "jwks.json"
_JWKS_FILE.write_text(json.dumps({"keys": [_jwk]}), encoding="utf-8")


def mint(tier: str, tenant_id: str = "org_wave5") -> str:
    now = int(time.time())
    payload = {
        "iss": ISS, "aud": AUD, "iat": now, "exp": now + 3600, "sub": "auth0|wave5",
        NS + "tenant_id": tenant_id, NS + "org_id": tenant_id, NS + "tier": tier,
    }
    return jwt.encode(payload, _priv_pem, algorithm="RS256", headers={"kid": KID})


def bearer(tier: str, tenant_id: str = "org_wave5") -> dict:
    return {"Authorization": "Bearer " + mint(tier, tenant_id)}


# --------------------------------------------------------------------------- #
# app / client
# --------------------------------------------------------------------------- #
def _client():
    from fastapi.testclient import TestClient
    from app import app
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def live_auth(monkeypatch):
    """Turn Auth0 verification ON, pointed at the local RS256 JWKS (no network)."""
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_AUTH0_ISSUER", ISS)
    monkeypatch.setenv("LEAF_AUTH0_AUDIENCE", AUD)
    monkeypatch.setenv("LEAF_TENANT_CLAIM_NS", NS)
    monkeypatch.setenv("LEAF_AUTH0_JWKS_FILE", str(_JWKS_FILE))
    monkeypatch.setenv("LEAF_TENANTS_FILE", str(TENANTS_FILE))
    import tenancy
    tenancy.reset_store()
    yield


@pytest.fixture
def demo_offauth(monkeypatch):
    """Force the default off-auth demo path (byte-identical legacy)."""
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    yield


@pytest.fixture
def isolate_authored(monkeypatch, tmp_path):
    """Contain /api/author template-path side effects (file write + global registry)."""
    import deps
    from routers import author as author_router
    monkeypatch.setattr(author_router, "AUTHORED_DIR", tmp_path / "authored")
    monkeypatch.setattr(deps, "_AUTHORED", [])
    monkeypatch.setattr(deps, "save_authored_tools", lambda tools: None)
    yield


# =========================================================================== #
# STUB HARNESS (grant admin) — records forwarded token + kind, mirrors the real
# auto-detect so the app-proxy round-trip is meaningful.
# =========================================================================== #
def _detect_kind(token: str) -> str:
    t = (token or "").strip()
    if t.startswith("sk-ant-api"):
        return "api_key"
    if t.startswith("sk-ant-oat"):
        return "oauth"
    return "oauth"


class _HarnessStub(http.server.BaseHTTPRequestHandler):
    STORE: dict = {}                 # tenant_id -> {"token","kind"}
    LAST_PUT_TOKEN: str | None = None
    LAST_PUT_KIND: str | None = None  # what the APP forwarded (None => app omitted it)

    def _send(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _tenant(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    def do_PUT(self):  # noqa: N802
        length = int(self.headers.get("content-length", 0) or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        cls = type(self)
        token = payload.get("token")
        forwarded_kind = payload.get("kind")  # what the app sent (may be absent)
        kind = forwarded_kind or _detect_kind(token)  # harness auto-detects when omitted
        cls.STORE[self._tenant()] = {"token": token, "kind": kind}
        cls.LAST_PUT_TOKEN = token
        cls.LAST_PUT_KIND = forwarded_kind
        self._send(200, {"linked": True,
                         "linked_at": datetime.now(timezone.utc).isoformat(),
                         "kind": kind})

    def do_GET(self):  # noqa: N802
        rec = type(self).STORE.get(self._tenant())
        if rec:
            self._send(200, {"linked": True,
                             "linked_at": datetime.now(timezone.utc).isoformat(),
                             "kind": rec["kind"]})
        else:
            self._send(200, {"linked": False, "linked_at": None})

    def do_DELETE(self):  # noqa: N802
        type(self).STORE.pop(self._tenant(), None)
        self._send(200, {"linked": False, "linked_at": None})

    def log_message(self, *a):  # silence
        return


@pytest.fixture
def harness_stub():
    _HarnessStub.STORE = {}
    _HarnessStub.LAST_PUT_TOKEN = None
    _HarnessStub.LAST_PUT_KIND = None
    srv = http.server.HTTPServer(("127.0.0.1", 0), _HarnessStub)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}", _HarnessStub
    finally:
        srv.shutdown()


def _h(tenant: str) -> dict:
    return {"X-Tenant-Id": tenant}


# =========================================================================== #
# CONTRACT A — BYO API key as a grant kind (app proxy)
# =========================================================================== #
def test_grant_explicit_api_key_kind_forwarded(monkeypatch, harness_stub):
    url, stub = harness_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    c = _client()

    r = c.post("/api/tenant/claude-grant", json={"token": FAKE_API, "kind": "api_key"},
               headers=_h("ent"))
    assert r.status_code == 200, r.text
    b = r.json()
    jsonschema.validate(b, ENVELOPE_SCHEMA)
    assert b["linked"] is True and b["kind"] == "api_key"
    assert isinstance(b["linked_at"], str)
    # token NEVER echoed; the explicit kind WAS forwarded to the harness.
    assert FAKE_API not in r.text and "token" not in b
    assert stub.LAST_PUT_TOKEN == FAKE_API and stub.LAST_PUT_KIND == "api_key"


def test_grant_autodetect_api_key_when_kind_omitted(monkeypatch, harness_stub):
    url, stub = harness_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    c = _client()

    r = c.post("/api/tenant/claude-grant", json={"token": FAKE_API}, headers=_h("ent2"))
    assert r.status_code == 200, r.text
    b = r.json()
    jsonschema.validate(b, ENVELOPE_SCHEMA)
    # app omitted kind (so the harness auto-detects); result kind is api_key from the prefix.
    assert stub.LAST_PUT_KIND is None
    assert b["kind"] == "api_key" and b["linked"] is True
    assert FAKE_API not in r.text


def test_grant_autodetect_oauth_default(monkeypatch, harness_stub):
    url, stub = harness_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    c = _client()

    r = c.post("/api/tenant/claude-grant", json={"token": FAKE_OAUTH}, headers=_h("web1"))
    assert r.json()["kind"] == "oauth"
    # a token with no recognizable prefix also defaults to oauth (the web lane).
    r2 = c.post("/api/tenant/claude-grant", json={"token": FAKE_PLAIN}, headers=_h("web2"))
    assert r2.json()["kind"] == "oauth"
    assert FAKE_OAUTH not in r.text and FAKE_PLAIN not in r2.text


def test_grant_status_roundtrip_carries_kind(monkeypatch, harness_stub):
    url, stub = harness_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    c = _client()

    c.post("/api/tenant/claude-grant", json={"token": FAKE_API, "kind": "api_key"},
           headers=_h("ent3"))
    g = c.get("/api/tenant/claude-grant", headers=_h("ent3"))
    assert g.status_code == 200
    gb = g.json()
    jsonschema.validate(gb, ENVELOPE_SCHEMA)
    assert gb["linked"] is True and gb["kind"] == "api_key"
    assert FAKE_API not in g.text
    # a never-linked tenant -> linked:false, no kind leak.
    other = c.get("/api/tenant/claude-grant", headers=_h("nobody")).json()
    assert other["linked"] is False


# =========================================================================== #
# CONTRACT B — entitlement enforcement (live auth, RS256 tier claims)
# =========================================================================== #
def test_entitlements_get_matrix(live_auth):
    c = _client()
    # 8-capability shape (canonical solve + agent spine / entitlements.py CAPABILITIES)
    for tier, expected in [
        ("hosted_pro", {"run_read": True, "run_write": True, "solve": True,
                        "build": True,
                        "converse": True, "agent_write_autopilot": True,
                        "deploy": True, "platform_customize": False}),
        ("hosted_starter", {"run_read": True, "run_write": True, "solve": False,
                            "build": False,
                            "converse": True, "agent_write_autopilot": False,
                            "deploy": False, "platform_customize": False}),
        ("self_hosted", {"run_read": True, "run_write": True, "solve": True,
                         "build": True,
                         "converse": True, "agent_write_autopilot": True,
                         "deploy": True, "platform_customize": False}),
    ]:
        r = c.get("/api/entitlements", headers=bearer(tier))
        assert r.status_code == 200, r.text
        b = r.json()
        jsonschema.validate(b, ENVELOPE_SCHEMA)
        assert b["tier"] == tier
        assert b["entitlements"] == expected
        assert b["source"] == "policy"


def test_author_build_denied_for_hosted_starter(live_auth):
    """hosted_starter lacks `build` -> POST /api/author is a 403 entitlement rejection
    (before any harness/template work)."""
    c = _client()
    r = c.post("/api/author", json={"description": "tally panels per layer"},
               headers=bearer("hosted_starter"))
    assert r.status_code == 403, r.text
    b = r.json()
    jsonschema.validate(b, ENVELOPE_SCHEMA)
    assert b["entitlement_required"] is True
    assert b["required"] == "build"
    assert b["tier"] == "hosted_starter"
    assert b["error"]["error_code"] == "ENTITLEMENT_REQUIRED" and b["error"]["retryable"] is False
    assert b["degraded_mode"] is False


def test_author_build_allowed_for_hosted_pro(live_auth, isolate_authored):
    """hosted_pro has `build` -> POST /api/author proceeds (templated 200, NOT a 403)."""
    c = _client()
    r = c.post("/api/author", json={"description": "tally panels per layer"},
               headers=bearer("hosted_pro"))
    assert r.status_code == 200, r.text
    b = r.json()
    assert "entitlement_required" not in b
    assert b["tool"] is not None and isinstance(b["code"], str)


def test_run_write_allowed_for_hosted_starter(live_auth):
    """hosted_starter HAS run_write -> a drawing.write run is accepted (202, not 403)."""
    c = _client()
    r = c.post("/api/run", json={"tool": "delete-marked-panel", "params": {}},
               headers=bearer("hosted_starter"))
    assert r.status_code == 202, r.text
    assert r.json().get("job_id")


def test_run_read_allowed_for_hosted_starter(live_auth):
    c = _client()
    r = c.post("/api/run", json={"tool": "count-by-layer", "params": {}},
               headers=bearer("hosted_starter"))
    assert r.status_code == 202, r.text


def test_run_write_denied_for_restricted_tier(live_auth, monkeypatch, tmp_path):
    """A tier whose policy sets run_write:false -> a drawing.write run is a 403 rejection
    (exercises the run_write enforcement branch + 403 shape via an operator-tuned policy)."""
    policy = {
        "demo": {"run_read": True, "run_write": True, "build": True},
        "locked": {"run_read": True, "run_write": False, "build": False},
    }
    pf = tmp_path / "entitlements.json"
    pf.write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setenv("LEAF_ENTITLEMENTS_FILE", str(pf))
    c = _client()

    r = c.post("/api/run", json={"tool": "delete-marked-panel", "params": {}},
               headers=bearer("locked"))
    assert r.status_code == 403, r.text
    b = r.json()
    jsonschema.validate(b, ENVELOPE_SCHEMA)
    assert b["entitlement_required"] is True
    assert b["required"] == "run_write"
    assert b["tier"] == "locked"
    assert b["error"]["error_code"] == "ENTITLEMENT_REQUIRED" and b["error"]["retryable"] is False
    assert b["degraded_mode"] is False

    # a read tool on the same restricted tier is still allowed.
    r2 = c.post("/api/run", json={"tool": "count-by-layer", "params": {}},
                headers=bearer("locked"))
    assert r2.status_code == 202, r2.text


def test_entitlements_missing_tier_claim_still_403(live_auth):
    """A verified token with NO tenant claim is 403 at require_tenant (auth layer),
    independent of entitlements — the tier gate never sees an unauthenticated caller."""
    now = int(time.time())
    tok = jwt.encode({"iss": ISS, "aud": AUD, "iat": now, "exp": now + 3600, "sub": "x"},
                     _priv_pem, algorithm="RS256", headers={"kid": KID})
    c = _client()
    r = c.get("/api/entitlements", headers={"Authorization": "Bearer " + tok})
    assert r.status_code == 403


# =========================================================================== #
# Off-auth demo path — tier "demo" = full access; byte-identical behaviour
# =========================================================================== #
def test_entitlements_demo_offauth(demo_offauth):
    c = _client()
    r = c.get("/api/entitlements", headers=_h("demo-tenant"))
    assert r.status_code == 200, r.text
    b = r.json()
    jsonschema.validate(b, ENVELOPE_SCHEMA)
    assert b["tier"] == "demo"
    assert b["entitlements"] == {"run_read": True, "run_write": True, "solve": True,
                                 "build": True,
                                 "converse": True, "agent_write_autopilot": True,
                                 "deploy": True, "platform_customize": False}
    assert b["source"] == "policy"


def test_author_allowed_demo_offauth(demo_offauth, isolate_authored, monkeypatch):
    monkeypatch.delenv("LEAF_AUTHOR_HARNESS_URL", raising=False)  # force templater path
    c = _client()
    r = c.post("/api/author", json={"description": "tally panels per layer"},
               headers=_h("demo-tenant"))
    assert r.status_code == 200, r.text
    assert "entitlement_required" not in r.json()


def test_run_write_allowed_demo_offauth(demo_offauth):
    c = _client()
    r = c.post("/api/run", json={"tool": "delete-marked-panel", "params": {}},
               headers=_h("demo-tenant"))
    assert r.status_code == 202, r.text


# --------------------------------------------------------------------------- #
# script runner
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
