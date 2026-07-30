"""Focused image-turn acceptance checks, including pre-entitlement rejection."""
from __future__ import annotations

import asyncio
import base64
import os
import sys
import tempfile
import uuid
from pathlib import Path

os.environ.setdefault("SESSIONS_DB", str(Path(tempfile.mkdtemp()) / "sessions.db"))
os.environ.setdefault("LEAF_AUTH_LIVE", "0")
SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import entitlements
import session_store
import turn_runner
from envelopes import install_error_handlers
from routers import sessions as sessions_router


# The FULL PNG signature. Signatures are now matched in full, so a four-byte
# stand-in is not a PNG as far as the server is concerned — which is the point.
_PNG = b"\x89PNG\r\n\x1a\n"


def _image(data: str | None = None, media_type: str = "image/png"):
    return {"media_type": media_type, "data": data or base64.b64encode(_PNG).decode()}


@pytest.fixture()
def client():
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(sessions_router.router)
    # The body cap is an ASGI middleware, not route code, so a client built
    # without it would test a route that is no longer bounded.
    app.add_middleware(sessions_router.MessageBodyLimitMiddleware)
    return TestClient(app, raise_server_exceptions=False)


def _session(client):
    response = client.post("/api/sessions", json={"drawing_id": f"image-dwg-{uuid.uuid4()}"}, headers={"X-Tenant-Id": "image-tenant"})
    assert response.status_code < 300, response.text
    return response.json()["session_id"]


def _post(client, session_id, body):
    return client.post(f"/api/sessions/{session_id}/messages", json=body, headers={"X-Tenant-Id": "image-tenant"})


def test_bad_images_are_400_before_entitlement(client, monkeypatch):
    session_id = _session(client)
    monkeypatch.setattr(entitlements, "entitlements_for", lambda tier: (_ for _ in ()).throw(AssertionError("entitlement reached")))
    for body in (
        {"images": [_image()] * 4},
        {"images": [_image(base64.b64encode(_PNG + b"x" * (1024 * 1024)).decode())]},
        {"images": [_image(media_type="image/svg+xml")]},
        {"images": [_image(media_type="image/jpeg")]},
    ):
        response = _post(client, session_id, body)
        assert response.status_code == 400, response.text


def test_images_ride_alongside_text_into_the_event_and_the_wire(client, monkeypatch):
    session_id = _session(client)
    captured = {}

    class Response:
        status_code = 200

    def post(*args, **kwargs):
        captured.update(kwargs["json"])
        return Response()

    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://harness.test")
    monkeypatch.setattr(turn_runner.requests, "post", post)
    monkeypatch.setattr(turn_runner, "_spawn_relay", lambda *args, **kwargs: None)
    images = [_image()]
    response = _post(client, session_id, {"text": "what is this?", "images": images})
    assert response.status_code == 202, response.text
    assert captured["images"] == images and captured["text"] == "what is this?"
    events = session_store.recent_events(session_id, 10)
    assert events[0]["type"] == "turn_started"
    assert events[0]["data"]["images"] == [{"media_type": "image/png", "bytes": 8}]


def test_image_only_reaches_the_harness_and_stores_only_a_descriptor(client, monkeypatch):
    session_id = _session(client)
    captured = {}

    class Response:
        status_code = 200

    def post(*args, **kwargs):
        captured.update(kwargs["json"])
        return Response()

    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://harness.test")
    monkeypatch.setattr(turn_runner.requests, "post", post)
    monkeypatch.setattr(turn_runner, "_spawn_relay", lambda *args, **kwargs: None)
    response = _post(client, session_id, {"images": [_image()]})
    assert response.status_code == 202, response.text
    assert captured["images"] == [_image()]
    event = session_store.recent_events(session_id, 10)[0]
    assert event["data"]["images"] == [{"media_type": "image/png", "bytes": 8}]


def test_decoded_size_boundary_and_content_length_guard(client):
    session_id = _session(client)
    at_limit = base64.b64encode(_PNG + b"x" * (1024 * 1024 - len(_PNG))).decode()
    assert _post(client, session_id, {"images": [_image(at_limit)]}).status_code != 400
    response = client.post(
        f"/api/sessions/{session_id}/messages", content=b"{}",
        headers={"X-Tenant-Id": "image-tenant", "content-type": "application/json", "content-length": "1500001"},
    )
    assert response.status_code == 413, response.text


def test_confirm_or_queue_with_images_is_400(client):
    session_id = _session(client)
    assert _post(client, session_id, {"confirm": {"confirmationId": "c"}, "images": [_image()]}).status_code == 400
    assert _post(client, session_id, {"images": [_image()], "queue": True}).status_code == 400


def _webp(payload: bytes = b"\x00" * 8) -> bytes:
    body = b"WEBP" + payload
    return b"RIFF" + len(body).to_bytes(4, "little") + body


def test_full_signatures_are_required_not_prefixes(client):
    """A prefix is not a signature.

    Each case below starts with bytes that a prefix check accepts and a full
    signature check does not, which is exactly what a caller would send to get
    arbitrary content past a media_type gate and into a vision content block.
    """
    session_id = _session(client)
    for media_type, forged in (
        ("image/png", b"\x89PNG" + b"junk-not-a-png"),
        ("image/gif", b"GIF8" + b"0a" + b"junk"),
        ("image/webp", b"RIFF" + (999).to_bytes(4, "little") + b"WEBPshort"),
        ("image/jpeg", b"not-a-jpeg-at-all"),
    ):
        response = _post(client, session_id, {
            "images": [_image(base64.b64encode(forged).decode(), media_type=media_type)],
        })
        assert response.status_code == 400, f"{media_type} forgery was accepted"
        assert "does not match its bytes" in response.json()["error"]["message"]

    # ...and the genuine articles still pass.
    for media_type, real in (
        ("image/png", _PNG),
        ("image/gif", b"GIF89a" + b"\x00" * 4),
        ("image/jpeg", b"\xff\xd8\xff\xe0" + b"\x00" * 4 + b"\xff\xd9"),
        # Real encoders emit bytes AFTER end-of-image and decoders read them
        # fine. ffmpeg produces such files. Rejecting them turns away genuine
        # photos, which is worse than the shallow check this replaced.
        ("image/jpeg", b"\xff\xd8\xff\xe0" + b"\x00" * 4 + b"\xff\xd9" + b"\x00" * 16),
        ("image/webp", _webp()),
    ):
        response = _post(client, session_id, {
            "images": [_image(base64.b64encode(real).decode(), media_type=media_type)],
        })
        assert response.status_code != 400, f"{media_type} was wrongly refused: {response.text}"


def test_a_chunked_body_cannot_outgrow_the_cap(client):
    """Content-Length is not a memory bound.

    A chunked request declares no length, so a guard that reads the whole body
    and then measures it has already paid the memory. The refusal has to happen
    while the bytes are still arriving, which is why this is a middleware: by
    the time a route or even a dependency runs, FastAPI has already read the
    body.
    """
    chunk_size = 65536
    total_chunks = 320
    sent = {"count": 0}

    async def receive():
        sent["count"] += 1
        if sent["count"] > total_chunks:
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.request", "body": b"x" * chunk_size, "more_body": True}

    started = {}

    async def send(message):
        if message["type"] == "http.response.start":
            started["status"] = message["status"]

    async def never(scope, receive_, send_):  # pragma: no cover - must not be reached
        while True:
            message = await receive_()
            if not message.get("more_body"):
                break

    middleware = sessions_router.MessageBodyLimitMiddleware(never)
    scope = {"type": "http", "method": "POST",
             "path": "/api/sessions/abc/messages", "headers": []}
    asyncio.run(middleware(scope, receive, send))

    assert started.get("status") == 413
    cap_in_chunks = sessions_router._MAX_MESSAGE_BODY_BYTES // chunk_size
    assert sent["count"] <= cap_in_chunks + 2, (
        f"{sent['count']} chunks were accepted before refusing; the cap is "
        f"~{cap_in_chunks} chunks, so the whole body was read first"
    )


def test_an_image_media_type_of_the_wrong_type_is_400_not_500(client):
    """`media_type` comes straight from client JSON.

    A list is unhashable, so testing membership in a set raises TypeError and
    turns a bad request into a server error. The type check has to come first.
    """
    session_id = _session(client)
    for bad in ([], {}, 5, None):
        response = _post(client, session_id, {
            "images": [{"media_type": bad, "data": base64.b64encode(_PNG).decode()}],
        })
        assert response.status_code == 400, f"{bad!r} gave {response.status_code}"



_JSON = {"content-type": "application/json", "X-Tenant-Id": "image-tenant"}


@pytest.mark.parametrize("label,kwargs,expected_type", [
    ("malformed json", {"content": b"{not json", "headers": _JSON}, "json_invalid"),
    # Whitespace is not an absent body: FastAPI tries to parse it and fails.
    ("whitespace only", {"content": b"   ", "headers": _JSON}, "json_invalid"),
    ("absent body", {"content": b"", "headers": _JSON}, "missing"),
    # `null` is a body that says "nothing", which is a MISSING required field.
    ("json null", {"content": b"null", "headers": _JSON}, "missing"),
    ("json array", {"content": b"[]", "headers": _JSON}, "model_attributes_type"),
    # JSON-shaped bytes sent as text/plain are NOT a JSON body to FastAPI.
    ("wrong content type",
     {"content": b'{"text":"hi"}',
      "headers": {"content-type": "text/plain", "X-Tenant-Id": "image-tenant"}},
     "model_attributes_type"),
    ("wrong field type", {"json": {"text": 5}, "headers": {"X-Tenant-Id": "image-tenant"}},
     "string_type"),
])
def test_malformed_bodies_keep_fastapis_own_error_for_each_case(client, label, kwargs, expected_type):
    """One implementation of the wire contract, not two.

    Reading the body by hand meant reproducing FastAPI's error behaviour by
    hand, and three reviews found three ways it differed: an absent body became
    an empty object and answered 409, error locations lost their `body` prefix,
    and JSON bytes sent as text/plain were accepted where FastAPI refuses them.
    The cap now lives in an ASGI middleware underneath the framework, so these
    are FastAPI's own answers again — and this table is what pins them.
    """
    session_id = _session(client)
    response = client.post(f"/api/sessions/{session_id}/messages", **kwargs)
    assert response.status_code == 422, f"{label}: {response.text}"
    body = response.json()
    assert body["error"]["error_code"] == "BAD_PARAMS", label
    assert expected_type in body["error"]["message"], f"{label}: {body['error']['message']}"
    # Every location keeps the `body` prefix a client reads to point at a field.
    assert "'body'" in body["error"]["message"], label
    # The rejected value is never echoed back (envelopes.py keeps loc/msg/type).
    assert "input" not in body["error"]["message"], label
