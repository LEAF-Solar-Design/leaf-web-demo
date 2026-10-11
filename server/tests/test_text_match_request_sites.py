"""Six request-fed comparisons refuse non-ASCII text instead of raising.

``hmac.compare_digest`` raises ``TypeError`` when either ``str`` argument holds a
non-ASCII character. Each site below compares a value the caller supplies (a
header, a token, a form field), so before this change a single accented letter
turned a refusal into an HTTP 500. Each site now refuses exactly as it refuses an
ASCII mismatch, and an ASCII value behaves as before.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import inspect
import io
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import checkout_capability  # noqa: E402
import deps  # noqa: E402
import envelopes  # noqa: E402
import guest_uploads  # noqa: E402
from routers import agent, drawings  # noqa: E402

NON_ASCII = ("é" * 64, "\ud800", "０" * 64, "sha256=" + "é" * 64)
DISPATCH = "tmb-dispatch-secret"


def _callbacks():
    path = SERVER_DIR / "da" / "callbacks.py"
    spec = importlib.util.spec_from_file_location("tmb_callbacks_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _guest_token(monkeypatch):
    monkeypatch.setattr(guest_uploads, "guest_secret", lambda: "tmb-guest-secret")
    tenant = guest_uploads.GUEST_TENANT_PREFIX + "tmb"
    token = guest_uploads.mint_guest_session(tenant, int(time.time()) + 600)
    return tenant, token, token.rsplit(".", 1)[0]


def _final_return(fn):
    """The function's last statement as source, read through the AST so a comment never counts."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    body = tree.body[0].body
    assert isinstance(body[-1], ast.Return), fn.__name__
    return ast.unparse(body[-1].value)


def _equality_tests(fn):
    """Every == or != comparison in the function, comments and strings excluded."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    return [ast.unparse(node) for node in ast.walk(tree) if isinstance(node, ast.Compare)
            and any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops)]


def _save(digest, data=b"not a dxf at all"):
    upload = SimpleNamespace(file=io.BytesIO(data), filename="edited.dxf")
    out = drawings._receive_edited_dxf(upload, digest)
    return getattr(out, "status_code", None), out


def test_tmb_guest_session_refuses_non_ascii_signature(monkeypatch):
    tenant, token, head = _guest_token(monkeypatch)
    assert guest_uploads.verify_guest_session(token) == tenant
    for value in NON_ASCII:
        assert guest_uploads.verify_guest_session(head + "." + value) is None


def test_tmb_dispatch_helper_refuses_non_ascii(monkeypatch):
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", DISPATCH)
    assert deps._dispatch_secret_ok(DISPATCH) is True
    for value in NON_ASCII:
        assert deps._dispatch_secret_ok(value) is False


def test_tmb_agent_gate_answers_401_for_non_ascii_header(monkeypatch):
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", DISPATCH)
    assert agent._require_dispatch(DISPATCH) is None
    for value in NON_ASCII:
        assert agent._require_dispatch(value).status_code == 401
    app = FastAPI()
    envelopes.install_error_handlers(app)
    app.include_router(agent.router)
    client = TestClient(app, raise_server_exceptions=False)
    body = {"tenant_id": "t", "session_id": "s", "turn_id": "u", "action": "read"}
    # An ASGI header is decoded as latin-1, so these bytes arrive as accented text.
    response = client.post("/internal/agent/gate", json=body,
                           headers={"X-Dispatch-Secret": b"\xe9" * 40})
    ascii_wrong = client.post("/internal/agent/gate", json=body,
                              headers={"X-Dispatch-Secret": "x" * 40})
    assert response.status_code == ascii_wrong.status_code == 401
    assert response.json()["error"]["error_code"] == ascii_wrong.json()["error"]["error_code"]


def test_tmb_callback_signature_refuses_non_ascii_and_non_text():
    callbacks = _callbacks()
    secret, body, ts, nonce = b"tmb-callback-secret", b'{"a":1}', "1700000000", "nonce-tmb"
    good = callbacks.sign_payload(body, ts, nonce, secret)
    assert callbacks.verify_signature(body, ts, nonce, good, secret) is True
    for value in NON_ASCII:
        assert callbacks.verify_signature(body, ts, nonce, value, secret) is False

    class Spoof(str):
        def encode(self, *args, **kwargs):  # never consulted: the characters are read
            return good.encode("ascii")

    assert callbacks.verify_signature(body, ts, nonce, Spoof("x" * 71), secret) is False
    assert callbacks.verify_signature(body, ts, nonce, Spoof(good), secret) is True

    class Masked:
        @property
        def __class__(self):
            raise RuntimeError("an object must not run code before the comparison")

    assert callbacks.verify_signature(body, ts, nonce, Masked(), secret) is False
    assert callbacks.verify_signature(body, ts, nonce, good.encode("ascii"), secret) is False


def test_tmb_checkout_capability_rejects_non_ascii(monkeypatch):
    monkeypatch.setenv("LEAF_CHECKOUT_CAP_SECRET", "tmb-cap-secret")
    checkout = {"fence": 3, "holder": "holder-tmb"}
    capability = checkout_capability.mint("tenant-tmb", "drawing-tmb", 3)
    assert checkout_capability.verify(capability, "tenant-tmb", "drawing-tmb", checkout) == ("holder-tmb", 3)
    for value in NON_ASCII:
        with pytest.raises(checkout_capability.CapabilityRejected):
            checkout_capability.verify(value, "tenant-tmb", "drawing-tmb", checkout)


def test_tmb_edited_save_digest_non_ascii_is_a_400():
    data = b"not a dxf at all"
    status, _ = _save(hashlib.sha256(data).hexdigest(), data)
    assert status == 422  # the digest matched, so the bytes reached the parser
    for value in NON_ASCII:
        status, response = _save(value, data)
        assert status == 400
        assert b"source_digest does not match" in response.body


def test_tmb_ascii_outcomes_unchanged(monkeypatch):
    tenant, token, head = _guest_token(monkeypatch)
    assert guest_uploads.verify_guest_session(head + "." + "0" * 64) is None
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", DISPATCH)
    assert deps._dispatch_secret_ok("x" * len(DISPATCH)) is False
    assert deps._dispatch_secret_ok("") is False
    assert agent._require_dispatch("x" * len(DISPATCH)).status_code == 401
    assert agent._require_dispatch(None).status_code == 401
    callbacks = _callbacks()
    assert callbacks.verify_signature(b"{}", "1", "n", "sha256=" + "0" * 64, b"k") is False
    assert callbacks.verify_signature(b"{}", "1", "n", "", b"k") is False
    monkeypatch.setenv("LEAF_CHECKOUT_CAP_SECRET", "tmb-cap-secret")
    capability = checkout_capability.mint("tenant-tmb", "drawing-tmb", 3)
    with pytest.raises(checkout_capability.CapabilityRejected):
        checkout_capability.verify(capability[:-1] + ("0" if capability[-1] != "0" else "1"),
                                   "tenant-tmb", "drawing-tmb", {"fence": 3, "holder": "h"})
    assert checkout_capability.verify("  " + capability + "  ", "tenant-tmb", "drawing-tmb",
                                      {"fence": 3, "holder": "h"}) == ("h", 3)
    assert _save("0" * 64)[0] == 400
    digest = hashlib.sha256(b"not a dxf at all").hexdigest()
    assert _save(digest.upper())[0] == 422  # the route lower-cases the presented digest


def test_tmb_six_sites_compare_through_the_safe_rule():
    for fn in (guest_uploads.verify_guest_session, deps._dispatch_secret_ok, agent._require_dispatch,
               checkout_capability.verify, drawings._receive_edited_dxf):
        source = inspect.getsource(fn)
        assert "text_matches(" in source, fn.__name__
        assert "compare_digest" not in source, fn.__name__
    callbacks = _callbacks()
    source = inspect.getsource(callbacks.verify_signature)
    assert "issubclass(type(signature), str)" in source
    assert 'str.encode(signature, "utf-8")' in source
    assert "isinstance(signature" not in source
    # The callback compares bytes, never the presented text itself.
    assert 'hmac.compare_digest(expected.encode("ascii"), presented)' in source
    assert _final_return(callbacks.verify_signature) == "hmac.compare_digest(expected.encode('ascii'), presented)"
    assert _equality_tests(callbacks.verify_signature) == []
    assert "compare_digest(expected, signature)" not in source
    for field in ("timestamp", "nonce"):
        assert f"issubclass(type({field}), str)" in source, field
        assert f"isinstance({field}" not in source, field
    canonical = inspect.getsource(callbacks._canonical_payload)
    assert 'str.encode(timestamp, "utf-8")' in canonical
    assert 'str.encode(nonce, "utf-8")' in canonical


def test_tmb_callback_fields_read_the_real_type():
    callbacks = _callbacks()
    secret, body, ts, nonce = b"tmb-callback-secret", b'{"a":1}', "1700000000", "nonce-tmb"
    good = callbacks.sign_payload(body, ts, nonce, secret)

    class Masked:
        @property
        def __class__(self):
            raise RuntimeError("an object must not run code before the comparison")

    class Liar:
        def __init__(self, real):
            self.real = real

        @property
        def __class__(self):
            return str

        def encode(self, *args, **kwargs):
            return self.real.encode("utf-8")

    class Spoof(str):
        def encode(self, *args, **kwargs):
            return ts.encode("utf-8")

    class Garbled(str):
        def encode(self, *args, **kwargs):
            return b"never read"

    assert callbacks.verify_signature(body, ts, nonce, good, secret) is True
    for field_ts, field_nonce in ((Masked(), nonce), (ts, Masked()), (Liar(ts), nonce),
                                  (ts, Liar(nonce)), ("\ud800", nonce), (ts, "\udfff"),
                                  (ts.encode("ascii"), nonce), (None, nonce)):
        assert callbacks.verify_signature(body, field_ts, field_nonce, good, secret) is False
    # A subclass is read by the characters it holds, never by its encode() override.
    assert callbacks.verify_signature(body, Spoof("9999999999"), nonce, good, secret) is False
    assert callbacks.verify_signature(body, Garbled(ts), Garbled(nonce), good, secret) is True


def test_tmb_five_sites_bind_the_shared_constant_time_helper(monkeypatch):
    import text_match

    modules = (guest_uploads, deps, agent, checkout_capability, drawings)
    for module in modules:
        assert module.text_matches is text_match.text_matches, module.__name__
    helper = inspect.getsource(text_match.text_matches)
    assert "hmac.compare_digest(left, right)" in helper
    assert "left == right" not in helper
    # Read through the AST: a comment cannot satisfy it and a reversed equality cannot hide.
    assert _final_return(text_match.text_matches) == "hmac.compare_digest(left, right)"
    assert _equality_tests(text_match.text_matches) == []
    calls = []

    def spy_for(module):
        real = module.text_matches

        def spy(candidate, current):
            calls.append((module.__name__, candidate))
            return real(candidate, current)

        monkeypatch.setattr(module, "text_matches", spy)

    for module in modules:
        spy_for(module)
    tenant, token, head = _guest_token(monkeypatch)
    assert guest_uploads.verify_guest_session(head + "." + "0" * 64) is None
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", DISPATCH)
    assert deps._dispatch_secret_ok("x" * len(DISPATCH)) is False
    assert agent._require_dispatch("x" * len(DISPATCH)).status_code == 401
    monkeypatch.setenv("LEAF_CHECKOUT_CAP_SECRET", "tmb-cap-secret")
    capability = checkout_capability.mint("tenant-tmb", "drawing-tmb", 3)
    with pytest.raises(checkout_capability.CapabilityRejected):
        checkout_capability.verify(capability[:-1] + ("0" if capability[-1] != "0" else "1"),
                                   "tenant-tmb", "drawing-tmb", {"fence": 3, "holder": "h"})
    assert _save("0" * 64)[0] == 400
    assert {name for name, _ in calls} == {module.__name__ for module in modules}


def test_tmb_callback_signature_reads_text_not_overrides():
    """A str subclass is read by its characters: its own __bool__ or __len__ never runs."""
    callbacks = _callbacks()
    secret, body, ts, nonce = b"tmb-callback-secret", b'{"a":1}', "1700000000", "nonce-tmb"
    good = callbacks.sign_payload(body, ts, nonce, secret)

    class Bomb(str):
        def __bool__(self):
            raise RuntimeError("bool called")

        def __len__(self):
            raise RuntimeError("len called")

    assert callbacks.verify_signature(body, ts, nonce, Bomb(good), secret) is True
    assert callbacks.verify_signature(body, ts, nonce, Bomb(""), secret) is False
    assert callbacks.verify_signature(body, ts, nonce, Bomb("sha256=" + "0" * 64), secret) is False
    assert callbacks.verify_signature(body, ts, nonce, "", secret) is False


def test_tmb_each_site_obeys_the_comparison_result(monkeypatch):
    """Each site decides by what the shared comparison returns, not by a comparison of its own.

    The comparison is replaced by one that answers the opposite of the truth: every site must then
    refuse its correct value and accept a wrong one. A site that calls the helper and then decides
    by its own equality keeps accepting the correct value and fails here.
    """
    modules = (guest_uploads, deps, agent, checkout_capability, drawings)
    for module in modules:
        real = module.text_matches
        monkeypatch.setattr(module, "text_matches", lambda c, k, _real=real: not _real(c, k))
    tenant, token, head = _guest_token(monkeypatch)
    assert guest_uploads.verify_guest_session(token) is None
    assert guest_uploads.verify_guest_session(head + "." + "0" * 64) == tenant
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", DISPATCH)
    assert deps._dispatch_secret_ok(DISPATCH) is False
    assert deps._dispatch_secret_ok("x" * len(DISPATCH)) is True
    assert agent._require_dispatch(DISPATCH).status_code == 401
    assert agent._require_dispatch("x" * len(DISPATCH)) is None
    monkeypatch.setenv("LEAF_CHECKOUT_CAP_SECRET", "tmb-cap-secret")
    capability = checkout_capability.mint("tenant-tmb", "drawing-tmb", 3)
    lock = {"fence": 3, "holder": "h"}
    with pytest.raises(checkout_capability.CapabilityRejected):
        checkout_capability.verify(capability, "tenant-tmb", "drawing-tmb", lock)
    wrong = capability[:-1] + ("0" if capability[-1] != "0" else "1")
    assert checkout_capability.verify(wrong, "tenant-tmb", "drawing-tmb", lock) == ("h", 3)
    digest = hashlib.sha256(b"not a dxf at all").hexdigest()
    assert _save(digest)[0] == 400
    assert _save("0" * 64)[0] == 422
