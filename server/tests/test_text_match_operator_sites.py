"""Two operator-side comparisons refuse non-ASCII header text instead of raising.

The iOS ship provider's identity header (shared by the progress, receipt and
source-catalog callbacks) and the Glug board signature header both reached
``hmac.compare_digest`` with raw request text, which raises ``TypeError`` on a
non-ASCII character, so the server answered 500. Both now route through
``text_match.text_matches`` and answer exactly what an ASCII mismatch answers.
"""
import hashlib
import hmac
import json
import time

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import deps
import envelopes
import glug_routes
import text_match
from routers import ios_ship as ios_ship_router
from routers import ios_ship_provider as isp

TOKEN = "tok-0123456789"
PROVIDER = "leaf-ios-provider"
EXECUTION = "00000000-0000-0000-0000-000000000000"
NON_ASCII_IDENTITIES = ["prové", "０" * 8, "\ud800", "leaf-ios-providér"]
GLUG_SECRET = "s" * 40


class _Config:
    provider_id = PROVIDER

    def read_token(self):
        return TOKEN


@pytest.fixture
def provider_config():
    isp.set_config(_Config())
    try:
        yield
    finally:
        isp.set_config(None)


def _body(response):
    return None if response is None else (response.status_code, json.loads(response.body))


def _ios_client():
    app = FastAPI()
    envelopes.install_error_handlers(app)
    app.include_router(isp.router)
    app.include_router(ios_ship_router.router)
    return TestClient(app, raise_server_exceptions=False)


def _answer(response):
    return response.status_code, response.json()


@pytest.mark.parametrize("identity", NON_ASCII_IDENTITIES)
def test_tmc_ios_identity_non_ascii_is_a_401(provider_config, identity):
    mismatch = _body(isp._authorized("Bearer " + TOKEN, "other-provider"))
    assert mismatch[0] == 401
    assert _body(isp._authorized("Bearer " + TOKEN, identity)) == mismatch


def test_tmc_ios_ascii_outcomes_unchanged(provider_config):
    assert isp._authorized("Bearer " + TOKEN, PROVIDER) is None
    refused = _body(isp._authorized("Bearer " + TOKEN, "other-provider"))
    assert refused == (401, {"ok": False, "error": {
        "error_code": "provider_unauthorized", "message": "provider authentication failed",
        "retryable": False}})
    for authorization, identity in [("Bearer " + TOKEN, None), ("Bearer " + TOKEN, ""),
                                    ("Bearer wrong-token", PROVIDER), (None, PROVIDER),
                                    ("Bearer tok-é", PROVIDER), ("bearer " + TOKEN, PROVIDER),
                                    ("Bearer " + TOKEN, PROVIDER.upper()),
                                    ("Bearer " + TOKEN, PROVIDER + " ")]:
        assert _body(isp._authorized(authorization, identity)) == refused


def test_tmc_ios_unconfigured_is_still_a_503():
    isp.set_config(None)
    status, body = _body(isp._authorized("Bearer " + TOKEN, "prové"))
    assert status == 503
    assert body["error"]["error_code"] == "provider_callback_unavailable"


@pytest.mark.parametrize("path", [
    f"/internal/v1/ios-ship/executions/{EXECUTION}/progress",
    f"/internal/v1/ios-ship/executions/{EXECUTION}/receipt",
    "/internal/v1/ios-ship/source-catalog",
])
@pytest.mark.parametrize("identity", [b"prov\xe9", "prové".encode("utf-8")])
def test_tmc_ios_http_routes_answer_401(provider_config, path, identity):
    client = _ios_client()
    auth = ("Bearer " + TOKEN).encode()
    mismatch = _answer(client.post(path, content=b"{}", headers={
        "Authorization": auth, "X-Leaf-Ios-Ship-Provider": b"other-provider"}))
    assert mismatch[0] == 401
    assert mismatch[1]["error"]["error_code"] == "provider_unauthorized"
    assert _answer(client.post(path, content=b"{}", headers={
        "Authorization": auth, "X-Leaf-Ios-Ship-Provider": identity})) == mismatch


def _glug_client(monkeypatch):
    monkeypatch.setenv("GLUG_MUSHY_CONTROL_TENANT_ID", "tenant-a")
    monkeypatch.setenv("GLUG_MUSHY_CONTROL_SUBJECTS", "proxy-subject")
    monkeypatch.setenv("GLUG_MUSHY_PROXY_SUBJECT", "proxy-subject")
    monkeypatch.setenv("GLUG_MUSHY_PROXY_SIGNING_SECRET", GLUG_SECRET)
    app = FastAPI()
    envelopes.install_error_handlers(app)

    @app.post("/probe")
    async def _probe(actor: str = Depends(glug_routes.require_control_actor)):
        return {"actor": actor}

    app.dependency_overrides[deps.require_tenant] = lambda: deps.TenantContext(
        "tenant-a", subject="proxy-subject", authority_resolved=True)
    return TestClient(app, raise_server_exceptions=False)


def _glug_headers(signature):
    ts = str(int(time.time()))
    return ts, {"X-Glug-Board-Actor": b"actor1", "X-Glug-Board-Timestamp": ts.encode(),
                "X-Glug-Board-Signature": signature}


def _good_signature(ts, body=b"{}"):
    digest = hashlib.sha256(body).hexdigest()
    payload = f"v1\nactor1\n{ts}\nPOST\n/probe\n{digest}".encode("utf-8")
    return hmac.new(GLUG_SECRET.encode("utf-8"), payload, hashlib.sha256).hexdigest()


@pytest.mark.parametrize("signature", [b"\xe9" * 64, ("é" * 32).encode("utf-8"),
                                       b"0" * 63 + b"\xe9"])
def test_tmc_glug_signature_non_ascii_is_a_403(monkeypatch, signature):
    client = _glug_client(monkeypatch)
    _, ascii_headers = _glug_headers(b"0" * 64)
    mismatch = _answer(client.post("/probe", content=b"{}", headers=ascii_headers))
    assert mismatch[0] == 403
    _, headers = _glug_headers(signature)
    assert _answer(client.post("/probe", content=b"{}", headers=headers)) == mismatch


def test_tmc_glug_ascii_outcomes_unchanged(monkeypatch):
    client = _glug_client(monkeypatch)
    ts, headers = _glug_headers(b"")
    headers["X-Glug-Board-Signature"] = _good_signature(ts).encode()
    assert _answer(client.post("/probe", content=b"{}", headers=headers)) == (
        200, {"actor": "actor1"})
    headers["X-Glug-Board-Signature"] = _good_signature(ts).upper().encode()
    assert client.post("/probe", content=b"{}", headers=headers).status_code == 403
    headers["X-Glug-Board-Signature"] = (_good_signature(ts) + " ").encode()
    assert client.post("/probe", content=b"{}", headers=headers).status_code == 403
    headers["X-Glug-Board-Signature"] = b""
    assert client.post("/probe", content=b"{}", headers=headers).status_code == 403


def test_tmc_both_sites_compare_through_the_safe_rule(provider_config, monkeypatch):
    calls = []
    real = isp.text_matches

    def spy(candidate, current):
        calls.append((candidate, current))
        return real(candidate, current)

    monkeypatch.setattr(isp, "text_matches", spy)
    monkeypatch.setattr(glug_routes, "text_matches", spy)
    assert isp._authorized("Bearer " + TOKEN, "prové") is not None
    assert calls == [("prové", PROVIDER)]
    calls.clear()
    client = _glug_client(monkeypatch)
    ts, headers = _glug_headers(b"\xe9" * 64)
    assert client.post("/probe", content=b"{}", headers=headers).status_code == 403
    assert len(calls) == 1
    assert not calls[0][0].isascii()
    assert calls[0][1] == _good_signature(ts)


def test_tmc_both_sites_bind_the_shared_helper():
    assert isp.text_matches is text_match.text_matches
    assert glug_routes.text_matches is text_match.text_matches


def test_tmc_each_site_obeys_the_comparison_result(provider_config, monkeypatch):
    real = text_match.text_matches

    def inverted(candidate, current, _real=real):
        return not _real(candidate, current)

    monkeypatch.setattr(isp, "text_matches", inverted)
    monkeypatch.setattr(glug_routes, "text_matches", inverted)
    assert _body(isp._authorized("Bearer " + TOKEN, PROVIDER))[0] == 401
    assert isp._authorized("Bearer " + TOKEN, "other-provider") is None
    client = _glug_client(monkeypatch)
    ts, headers = _glug_headers(b"")
    headers["X-Glug-Board-Signature"] = _good_signature(ts).encode()
    assert client.post("/probe", content=b"{}", headers=headers).status_code == 403
    headers["X-Glug-Board-Signature"] = b"0" * 64
    assert _answer(client.post("/probe", content=b"{}", headers=headers)) == (
        200, {"actor": "actor1"})


# A value that differs from the configured one only by a suffix, a prefix, a
# truncation, a separator, case or one character must be refused exactly like
# a plain mismatch. A finite list cannot refuse every rule nobody named, so
# these values cover the ones a second acceptance branch would plausibly add:
# startswith, endswith, containment either way, strip, lower, split on a
# separator, a scheme prefix, a fixed-length slice.
IOS_NEAR_MISSES = [
    PROVIDER + ":", PROVIDER + ":extra", PROVIDER + "/x", PROVIDER + "-x",
    PROVIDER + ".x", PROVIDER + "x", PROVIDER + "0", PROVIDER + " x",
    PROVIDER + "\t", PROVIDER + "\n", PROVIDER + "\x00", PROVIDER + PROVIDER,
    "x" + PROVIDER, ":" + PROVIDER, " " + PROVIDER, "\t" + PROVIDER, "\n" + PROVIDER,
    PROVIDER[:-1], PROVIDER[1:], "ios-provider", "leaf-ios", "leaf-ios-providex",
    PROVIDER.capitalize(), PROVIDER.title(), "LEAF-ios-provider",
]


def test_tmc_ios_near_miss_identities_are_refused(provider_config):
    refused = _body(isp._authorized("Bearer " + TOKEN, "other-provider"))
    assert refused[0] == 401
    assert len(set(IOS_NEAR_MISSES)) == len(IOS_NEAR_MISSES)
    for identity in IOS_NEAR_MISSES:
        assert identity != PROVIDER
        assert _body(isp._authorized("Bearer " + TOKEN, identity)) == refused, repr(identity)
    assert isp._authorized("Bearer " + TOKEN, PROVIDER) is None


def _glug_near_misses(good):
    flipped = good[:-1] + ("1" if good[-1] == "0" else "0")
    letter = next((i for i, c in enumerate(good) if c.isalpha()), None)
    near = [
        good + "0", good + "a", good + "00", good + ":", good + ":x", good + "-",
        good + " x", good + "\t", "0" + good, " " + good, "\t" + good, ":" + good,
        good[:-1], good[1:], good[:32], good[32:], flipped,
        "sha256=" + good, "v1=" + good, "sha256:" + good, good + good,
    ]
    if letter is not None:
        near.append(good[:letter] + good[letter].upper() + good[letter + 1:])
    return near


def test_tmc_glug_near_miss_signatures_are_refused(monkeypatch):
    client = _glug_client(monkeypatch)
    ts, headers = _glug_headers(b"0" * 64)
    mismatch = _answer(client.post("/probe", content=b"{}", headers=headers))
    assert mismatch[0] == 403
    good = _good_signature(ts)
    near = _glug_near_misses(good)
    assert len(set(near)) == len(near)
    for signature in near:
        assert signature != good
        headers["X-Glug-Board-Signature"] = signature.encode()
        assert _answer(client.post("/probe", content=b"{}", headers=headers)) == mismatch, (
            repr(signature))
    headers["X-Glug-Board-Signature"] = good.encode()
    assert _answer(client.post("/probe", content=b"{}", headers=headers)) == (
        200, {"actor": "actor1"})
