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
    return {"media_type": media_type, "data": data or base64.b64encode(b"image").decode()}


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
        {"images": [_image("a" * (2 * 1024 * 1024 + 1))]},
        {"images": [_image(media_type="image/svg+xml")]},
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
    assert events[0]["data"]["images"] == images


def test_image_only_is_refused_until_the_harness_renders_it(client):
    """The harness validator requires text-or-confirm (src/server.ts), so an
    image-only turn would 400 downstream; and the runner does not fold images
    into the prompt yet, so accepting one would mean an EMPTY prompt — a silent
    no-op. Refuse locally with a clear reason until the harness half lands."""
    session_id = _session(client)
    response = _post(client, session_id, {"images": [_image()]})
    assert response.status_code == 400, response.text
    assert "must accompany `text`" in response.json()["error"]["message"]


def test_confirm_or_queue_with_images_is_400(client):
    session_id = _session(client)
    assert _post(client, session_id, {"confirm": {"confirmationId": "c"}, "images": [_image()]}).status_code == 400
    assert _post(client, session_id, {"images": [_image()], "queue": True}).status_code == 400
