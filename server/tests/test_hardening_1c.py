"""
Binary acceptance for security lane 1C (security-audit-2026-07-18):

  F17 (LOW)  — server/app.py: CORS `allow_origins` is ENV-DRIVEN (LEAF_CORS_ORIGINS),
               default-DENY in live-auth mode; the hardcoded bare `*` is gone. The
               app's real middleware is wired from the env-driven helper, and
               `allow_credentials` stays False.

  F7 (HIGH)  — server/routers/ops.py: the ops surface (per-tenant spend read +
               kill-switch flip) is gated by a REAL internal shared secret
               (LEAF_OPS_SECRET) presented in the X-Ops-Secret header and compared
               CONSTANT-TIME (hmac.compare_digest), not the old plain
               `X-Internal-Role: qa` header. Fail-closed 503 when the secret is
               unset in live mode; 403 on wrong/absent secret; the off-auth local
               demo stays open (byte-identical), mirroring deps.require_tenant.

Hermetic: in-process TestClient / pure-function calls. No broker, no DB, no APS.

Run:  cd server && python -m pytest tests/test_hardening_1c.py -q
"""
from __future__ import annotations

import json
import socket
import sys
import tempfile
from pathlib import Path

import jsonschema
import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent

# jobs.py reads JOBS_DB at import time (importing `app` pulls the jobs router);
# route it to a throwaway dir BEFORE any import so nothing litters server/.
import os  # noqa: E402
os.environ.setdefault("JOBS_DB", str(Path(tempfile.mkdtemp(prefix="hard1c-jobs-")) / "jobs.db"))

if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

ENVELOPE_SCHEMA = json.loads((SERVER_DIR / "envelope_schema.json").read_text(encoding="utf-8"))

# env keys these tests toggle — cleared before each test so no ambient/prior value leaks
_ENV_KEYS = ("LEAF_CORS_ORIGINS", "LEAF_AUTH_LIVE", "LEAF_OPS_SECRET",
             "LEAF_USAGE_LEDGER", "BROKER_LEDGER", "BROKER_URL", "BROKER_TENANTS")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    yield


def _dead_broker_url() -> str:
    """A closed 127.0.0.1 port -> connection refused fast (keeps _disabled_set()
    from reaching a real broker or blocking on a timeout)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


# =========================================================================== #
# F17 — CORS allow-list is env-driven + default-deny in live-auth
# =========================================================================== #
def _cors_origins(monkeypatch, *, cors_env=None, auth_live=False):
    """Evaluate app._cors_origins() (the exact function app.py wires into the
    CORS middleware) under a controlled env."""
    import app as appmod  # noqa: PLC0415

    if cors_env is None:
        monkeypatch.delenv("LEAF_CORS_ORIGINS", raising=False)
    else:
        monkeypatch.setenv("LEAF_CORS_ORIGINS", cors_env)
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1" if auth_live else "0")
    return appmod._cors_origins()


def test_f17_env_unset_auth_off_is_wildcard(monkeypatch):
    # local single-operator demo: bare "*" is the convenience default when auth is off
    assert _cors_origins(monkeypatch, cors_env=None, auth_live=False) == ["*"]


def test_f17_env_unset_live_auth_is_default_deny_no_star(monkeypatch):
    # THE fix: no bare "*" ever in live-auth mode; unset -> default-deny []
    origins = _cors_origins(monkeypatch, cors_env=None, auth_live=True)
    assert origins == []
    assert "*" not in origins


def test_f17_env_set_wins_in_live_auth(monkeypatch):
    origins = _cors_origins(
        monkeypatch,
        cors_env="https://app.leafdesign.ai, https://staging.leafdesign.ai ",
        auth_live=True)
    assert origins == ["https://app.leafdesign.ai", "https://staging.leafdesign.ai"]
    assert "*" not in origins


def test_f17_env_set_wins_over_wildcard_when_auth_off(monkeypatch):
    # an explicit allow-list is authoritative even off-auth (env always wins)
    origins = _cors_origins(monkeypatch, cors_env="https://only.example.com", auth_live=False)
    assert origins == ["https://only.example.com"]


def test_f17_env_blank_entries_trimmed(monkeypatch):
    origins = _cors_origins(monkeypatch, cors_env="  , https://a.example ,,", auth_live=True)
    assert origins == ["https://a.example"]


def test_f17_explicit_wildcard_opt_in_is_honored(monkeypatch):
    # if an operator DELIBERATELY sets "*", honor it — but it is now an explicit,
    # auditable choice in the env, never a hardcoded default.
    assert _cors_origins(monkeypatch, cors_env="*", auth_live=True) == ["*"]


def test_w4g2_dxf_route_headers_are_exposed_cross_origin():
    """W4g-2 (engine reach): the browser only sees the DXF route's version /
    head / leg headers and its ETag on a cross-origin API when the middleware
    exposes them; the head opener's fallback covers the version, the 304 path
    needs the ETag for real."""
    import app as appmod  # noqa: PLC0415
    from fastapi.middleware.cors import CORSMiddleware  # noqa: PLC0415

    exposed = None
    for mw in appmod.app.user_middleware:
        if mw.cls is CORSMiddleware:
            exposed = mw.kwargs.get("expose_headers")
    assert exposed is not None, "CORS middleware not wired"
    for name in ("ETag", "X-Leaf-Version", "X-Leaf-Head", "X-Leaf-Dxf-Source"):
        assert name in exposed


def test_f17_app_middleware_is_wired_from_the_env_helper():
    """The real FastAPI app's CORS middleware takes its allow_origins from
    app._cors_origins() (not a hardcoded ["*"]) and keeps allow_credentials False."""
    import app as appmod  # noqa: PLC0415
    from fastapi.middleware.cors import CORSMiddleware  # noqa: PLC0415

    cors_kwargs = None
    for mw in appmod.app.user_middleware:
        if mw.cls is CORSMiddleware:
            cors_kwargs = mw.kwargs
            break
    assert cors_kwargs is not None, "CORS middleware not installed"
    # wired from the helper: middleware value == helper output for the current env
    assert cors_kwargs["allow_origins"] == appmod._cors_origins()
    # credentials stay off (a wildcard with credentials would be unsafe)
    assert cors_kwargs["allow_credentials"] is False


# =========================================================================== #
# F7 — ops surface requires a real internal shared secret (constant-time)
# =========================================================================== #
def _ops_client():
    from fastapi import FastAPI  # noqa: PLC0415
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from envelopes import install_error_handlers  # noqa: PLC0415
    from routers import ops as ops_router  # noqa: PLC0415

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(ops_router.router)
    return TestClient(app, raise_server_exceptions=False)


def _empty_ledger_env(monkeypatch, tmp_path):
    """Point the ops read at an empty ledger + a dead broker so an AUTHORIZED
    GET /api/ops/tenants returns 200 with an empty tenant list, no broker needed."""
    monkeypatch.setenv("LEAF_USAGE_LEDGER", str(tmp_path / "nope.jsonl"))
    monkeypatch.setenv("BROKER_TENANTS", str(tmp_path / "no-tenants.json"))
    monkeypatch.setenv("BROKER_URL", _dead_broker_url())


def test_f7_secret_configured_403_without_200_with(monkeypatch, tmp_path):
    _empty_ledger_env(monkeypatch, tmp_path)
    monkeypatch.setenv("LEAF_OPS_SECRET", "top-secret-1c")
    c = _ops_client()

    # no header -> 403 with a machine-readable error envelope
    no_hdr = c.get("/api/ops/tenants")
    assert no_hdr.status_code == 403, no_hdr.text
    body = no_hdr.json()
    jsonschema.validate(body, ENVELOPE_SCHEMA)
    assert body["error"] is not None and body["error"]["error_code"]

    # wrong secret -> 403
    assert c.get("/api/ops/tenants", headers={"X-Ops-Secret": "nope"}).status_code == 403

    # the OLD plain role header no longer grants access
    assert c.get("/api/ops/tenants", headers={"X-Internal-Role": "qa"}).status_code == 403

    # correct secret -> 200, valid envelope, empty tenant list
    ok = c.get("/api/ops/tenants", headers={"X-Ops-Secret": "top-secret-1c"})
    assert ok.status_code == 200, ok.text
    okb = ok.json()
    jsonschema.validate(okb, ENVELOPE_SCHEMA)
    assert okb["error"] is None and okb["degraded_mode"] is False
    assert okb["tenants"] == []


def test_f7_mutating_routes_gated(monkeypatch, tmp_path):
    _empty_ledger_env(monkeypatch, tmp_path)
    monkeypatch.setenv("LEAF_OPS_SECRET", "top-secret-1c")
    c = _ops_client()

    # disable/enable reject wrong/absent secret BEFORE any broker proxy
    assert c.post("/api/ops/tenants/t1/disable").status_code == 403
    assert c.post("/api/ops/tenants/t1/disable",
                  headers={"X-Ops-Secret": "wrong"}).status_code == 403
    assert c.post("/api/ops/tenants/t1/enable").status_code == 403
    assert c.post("/api/ops/tenants/t1/enable",
                  headers={"X-Internal-Role": "qa"}).status_code == 403


def test_f7_live_mode_unset_secret_fails_closed_503(monkeypatch, tmp_path):
    _empty_ledger_env(monkeypatch, tmp_path)
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")   # live/public
    monkeypatch.delenv("LEAF_OPS_SECRET", raising=False)  # but no secret configured
    c = _ops_client()

    # fail CLOSED: the surface refuses to serve unguarded in live mode
    r = c.get("/api/ops/tenants")  # even a "correct" secret can't exist -> 503
    assert r.status_code == 503, r.text
    body = r.json()
    jsonschema.validate(body, ENVELOPE_SCHEMA)
    assert body["error"] is not None and body["error"]["error_code"] == "INTERNAL"

    # mutating routes fail closed too
    assert c.post("/api/ops/tenants/t1/disable").status_code == 503
    assert c.post("/api/ops/tenants/t1/enable",
                  headers={"X-Ops-Secret": "anything"}).status_code == 503


def test_f7_demo_mode_unset_secret_stays_open(monkeypatch, tmp_path):
    # auth OFF + no secret -> byte-identical open demo (mirrors deps.require_tenant);
    # this is the documented single-operator local behavior, NOT the public path.
    _empty_ledger_env(monkeypatch, tmp_path)
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    monkeypatch.delenv("LEAF_OPS_SECRET", raising=False)
    c = _ops_client()

    r = c.get("/api/ops/tenants")  # no header, no secret configured -> open
    assert r.status_code == 200, r.text
    jsonschema.validate(r.json(), ENVELOPE_SCHEMA)
    assert r.json()["tenants"] == []


def test_f7_uses_constant_time_compare(monkeypatch, tmp_path):
    """The credential check goes through hmac.compare_digest (constant-time),
    not a plain `==` — proven by spying on the actual call path."""
    import routers.ops as ops  # noqa: PLC0415

    _empty_ledger_env(monkeypatch, tmp_path)
    monkeypatch.setenv("LEAF_OPS_SECRET", "cmp-secret")

    seen = {}
    real = ops.hmac.compare_digest

    def _spy(a, b):
        seen["called"] = True
        return real(a, b)

    monkeypatch.setattr(ops.hmac, "compare_digest", _spy)

    c = _ops_client()
    assert c.get("/api/ops/tenants",
                 headers={"X-Ops-Secret": "cmp-secret"}).status_code == 200
    assert seen.get("called") is True


# =========================================================================== #
# ops kill-switch READ must fail SAFE, never "all clear"
#
# _disabled_set() feeds the Disabled/Active column of the ops drawer. It reads
# the broker's /broker/health, and used to trust ANY parseable JSON body:
# FastAPI's own 500 (`{"detail": ...}`) parsed fine, `tenants_disabled` was
# absent, `or []` produced an EMPTY kill list, and the function RETURNED — so
# the authoritative store fallback never ran. During a broker fault the drawer
# confidently showed every kill-switched tenant as "Active", which is the exact
# moment an operator is looking at it. A degraded read must fall through.
# =========================================================================== #
class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _disabled_set_with(monkeypatch, tmp_path, status_code, payload):
    """Run _disabled_set() in LEGACY mode with one killed tenant on disk and a
    broker health reply we control. Returns what the ops column would show."""
    import routers.ops as ops  # noqa: PLC0415

    tenants_file = tmp_path / "broker_tenants.json"
    tenants_file.write_text(json.dumps({"t-killed": {"disabled": True}}), encoding="utf-8")
    monkeypatch.setenv("BROKER_TENANTS", str(tenants_file))
    monkeypatch.setenv("BROKER_URL", _dead_broker_url())
    monkeypatch.delenv("LEAF_BROKER_STORE", raising=False)  # legacy authority
    monkeypatch.setattr(ops.requests, "get",
                        lambda *a, **k: _FakeResp(status_code, payload))
    return ops._disabled_set()


def test_broker_health_500_falls_through_to_the_authority(monkeypatch, tmp_path):
    # THE regression: a JSON-bodied 500 must NOT read as "nobody is disabled".
    got = _disabled_set_with(monkeypatch, tmp_path, 500,
                             {"detail": "Internal Server Error"})
    assert got == {"t-killed"}


def test_broker_health_200_without_the_field_falls_through(monkeypatch, tmp_path):
    # A 200 that simply does not carry tenants_disabled cannot settle the
    # question either — an absent key is not an empty kill list.
    got = _disabled_set_with(monkeypatch, tmp_path, 200, {"ok": True})
    assert got == {"t-killed"}


def test_broker_health_200_with_the_field_is_authoritative(monkeypatch, tmp_path):
    # The healthy path is unchanged: a real 200 list wins over the on-disk file.
    got = _disabled_set_with(monkeypatch, tmp_path, 200,
                             {"tenants_disabled": ["t-from-broker"]})
    assert got == {"t-from-broker"}


def test_broker_health_200_empty_list_is_honoured(monkeypatch, tmp_path):
    # An explicit empty list IS an answer ("nothing is killed") and must be kept
    # distinct from the absent-key case above.
    got = _disabled_set_with(monkeypatch, tmp_path, 200, {"tenants_disabled": []})
    assert got == set()


# =========================================================================== #
# LEAF_AUTH_LIVE has exactly ONE reader
#
# platform_link's startup assertion accepted {1,true,yes,on} while
# deps.auth_live() accepted only the exact "1". `LEAF_AUTH_LIVE=true` therefore
# satisfied the boot check and left every runtime gate OFF: a green app serving
# unauthenticated, with the ops surface open (no LEAF_OPS_SECRET) rather than
# fail-closed 503. Both sides now go through deps.auth_live().
# =========================================================================== #
@pytest.mark.parametrize("value", ["1", "true", "TRUE", "True", "yes", "on", " 1 "])
def test_auth_live_accepts_every_blessed_spelling(monkeypatch, value):
    import deps  # noqa: PLC0415

    monkeypatch.setenv("LEAF_AUTH_LIVE", value)
    assert deps.auth_live() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_auth_live_stays_off_for_off_spellings(monkeypatch, value):
    import deps  # noqa: PLC0415

    monkeypatch.setenv("LEAF_AUTH_LIVE", value)
    assert deps.auth_live() is False


def test_auth_live_default_is_off(monkeypatch):
    import deps  # noqa: PLC0415

    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    assert deps.auth_live() is False


def test_ops_surface_fails_closed_for_a_non_1_live_spelling(monkeypatch, tmp_path):
    """The payoff: with LEAF_AUTH_LIVE=true and NO secret configured, the ops
    surface must refuse (503), not serve every tenant's spend to anyone."""
    _empty_ledger_env(monkeypatch, tmp_path)
    monkeypatch.setenv("LEAF_AUTH_LIVE", "true")
    monkeypatch.delenv("LEAF_OPS_SECRET", raising=False)

    c = _ops_client()
    assert c.get("/api/ops/tenants").status_code == 503


# =========================================================================== #
# every executable LEAF_AUTH_LIVE reader agrees with the canonical parser
#
# Normalizing ONLY deps.auth_live() would have re-created the original bug one
# layer down: `LEAF_AUTH_LIVE=true` secures the server while broker,
# checkout_capability, and the platform tenant boundary (exact-"1" copies)
# stay in demo posture — platform reads/writes trusting caller-supplied
# X-Org-Id, broker calls allowed with no secret, QA hooks honored. Split
# authentication postures are exactly what a single parser exists to prevent.
# =========================================================================== #
_LIVE_SPELLINGS = ["1", "true", "TRUE", "yes", "on", " 1 ",
                   "0", "false", "no", "off", ""]


@pytest.mark.parametrize("value", _LIVE_SPELLINGS)
def test_broker_and_checkout_agree_with_the_canonical_parser(monkeypatch, value):
    import broker  # noqa: PLC0415
    import checkout_capability  # noqa: PLC0415
    import deps  # noqa: PLC0415

    monkeypatch.setenv("LEAF_AUTH_LIVE", value)
    expected = deps.auth_live()
    assert broker._auth_live() is expected
    assert checkout_capability._auth_live_posture() is expected


def _load_platform_deps():
    """Load platform/deps.py by explicit file path.

    ``platform`` collides with the stdlib module and ``deps`` with server/deps,
    so a bare import is a name minefield — the same reason platform/deps.py
    itself file-path-loads server/auth.py. Cached under a unique name.
    """
    import importlib.util  # noqa: PLC0415

    cached = sys.modules.get("leaf_platform_deps_driftguard")
    if cached is not None:
        return cached
    path = SERVER_DIR.parent / "platform" / "deps.py"
    spec = importlib.util.spec_from_file_location("leaf_platform_deps_driftguard", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.modules["leaf_platform_deps_driftguard"] = mod
    return mod


def test_platform_boundary_mirrors_the_canonical_spelling_set():
    """platform/deps.py mirrors _AUTH_LIVE_ON instead of importing it (name
    minefield above). This drift guard fails the moment the two sets diverge."""
    import deps  # noqa: PLC0415

    assert _load_platform_deps()._AUTH_LIVE_ON == deps._AUTH_LIVE_ON


@pytest.mark.parametrize("value", _LIVE_SPELLINGS)
def test_platform_auth_live_agrees_with_the_canonical_parser(monkeypatch, value):
    import deps  # noqa: PLC0415

    monkeypatch.setenv("LEAF_AUTH_LIVE", value)
    assert _load_platform_deps().auth_live() is deps.auth_live()


@pytest.mark.parametrize("value", ["true", "yes", "on", "TRUE"])
def test_platform_org_boundary_ignores_header_under_normalized_live(monkeypatch, value):
    """Route-level payoff: under a normalized live spelling the platform tenant
    boundary must NOT trust caller-supplied X-Org-Id — no token means 401,
    never the header's org (the F6 dev seam stays closed)."""
    import uuid  # noqa: PLC0415

    from fastapi import HTTPException  # noqa: PLC0415

    pdeps = _load_platform_deps()
    monkeypatch.setenv("LEAF_AUTH_LIVE", value)
    with pytest.raises(HTTPException) as exc:
        pdeps.get_org_id(x_org_id=str(uuid.uuid4()), authorization=None)
    assert exc.value.status_code == 401
