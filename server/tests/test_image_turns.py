"""Focused image-turn acceptance checks, including pre-entitlement rejection."""
from __future__ import annotations

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
from fastapi import FastAPI
from fastapi.testclient import TestClient

import entitlements
import session_store
import turn_runner
from envelopes import install_error_handlers
from routers import sessions as sessions_router


def _image(data: str | None = None, media_type: str = "image/png"):
    return {"media_type": media_type, "data": data or base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()}


@pytest.fixture()
def client():
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(sessions_router.router)
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
        {"images": [_image(base64.b64encode(b"\x89PNG" + b"x" * (1024 * 1024)).decode())]},
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
    at_limit = base64.b64encode(b"\x89PNG" + b"x" * (1024 * 1024 - 4)).decode()
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
