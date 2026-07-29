"""Acceptance tests for checkpoint restore.

The drawing backend is faked at the router seam, as test_checkpoints.py fakes
the current-head lookup. The fake still models immutable versions so these
tests verify restore is a copy-forward, not a destructive head rewrite.
"""
from __future__ import annotations

import os
import sys
import tempfile
import types
from contextlib import nullcontext
from pathlib import Path

_TMP_DIR = Path(tempfile.mkdtemp(prefix="rewind-test-"))
os.environ.setdefault("SESSIONS_DB", str(_TMP_DIR / "sessions.db"))
os.environ.setdefault("LEAF_AUTH_LIVE", "0")

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import pytest  # noqa: E402

import checkpoints  # noqa: E402
import session_store  # noqa: E402
from routers import checkpoints as checkpoints_router  # noqa: E402


@pytest.fixture()
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from envelopes import install_error_handlers

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(checkpoints_router.router)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def drawing(monkeypatch):
    """An immutable version store with the real copy-forward router seam."""
    state = {"head": 1, "latest": 1, "blobs": {"version/1": b"initial"}}
    backend = types.SimpleNamespace(get=lambda key: state["blobs"][key])

    def manifest(_backend, _tenant, _drawing):
        return {
            "head": state["head"],
            "latest": state["latest"],
            "versions": [{"v": version} for version in range(1, state["latest"] + 1)],
        }

    def resolve(_backend, _tenant, _drawing, version):
        version = int(version)
        key = f"version/{version}"
        if key not in state["blobs"]:
            raise ValueError(f"version {version} missing")
        return version, key

    def put(_backend, _tenant, _drawing, content, parent_version, meta,
            holder=None, fence=None, require_parent_is_head=False):
        assert require_parent_is_head is True
        assert meta["tool"] == "checkpoint_restore"
        callback = state.pop("before_put", None)
        if callback is not None:
            callback()
        if parent_version != state["head"]:
            raise ValueError("stale parent")
        state["latest"] += 1
        state["head"] = state["latest"]
        state["blobs"][f"version/{state['head']}"] = bytes(content)
        return state["head"]

    # The fake must carry every attribute routers/drawings.py touches, not just
    # the ones the happy path calls. Python evaluates an `except (store.X, ...)`
    # tuple AT RAISE TIME, so a missing exception CLASS turns a handled failure
    # into an AttributeError-500 — which is exactly how the stale-parent case
    # masqueraded as a product bug (it is a genuine 409 through the real store).
    class _CheckoutDenied(Exception):
        pass

    class _CheckoutParamError(Exception):
        pass

    monkeypatch.setitem(sys.modules, "store", types.SimpleNamespace(
        ANONYMOUS_HOLDER="anonymous", load_manifest=manifest,
        resolve_version=resolve,
        CheckoutDenied=_CheckoutDenied, CheckoutParamError=_CheckoutParamError,
        checkout_active=lambda *a, **k: False,
        acquire_checkout_fence=lambda *a, **k: None,
        release_checkout=lambda *a, **k: None))
    monkeypatch.setattr(checkpoints_router.write_loop, "backend_for_tenant",
                        lambda *args, **kwargs: backend)
    monkeypatch.setattr(checkpoints_router.write_loop, "_put_bytes_version", put)
    monkeypatch.setattr(checkpoints_router.write_loop, "read_intake",
                        lambda _backend, _tenant, _drawing, version: (version, {}))
    monkeypatch.setattr(checkpoints_router.write_loop, "publish_intake_cache",
                        lambda *args, **kwargs: None)
    monkeypatch.setattr(checkpoints_router.write_loop, "drawing_mutation_commit_guard",
                        lambda: nullcontext(True))
    monkeypatch.setattr(checkpoints_router, "_drawing_version",
                        lambda _tenant, _drawing: state["head"])

    def advance(content: bytes):
        state["latest"] += 1
        state["head"] = state["latest"]
        state["blobs"][f"version/{state['head']}"] = content

    state["advance"] = advance
    return state


_counter = [0]


def _h(tenant: str) -> dict:
    return {"X-Tenant-Id": tenant}


def _session(tenant: str = "tenant-a") -> dict:
    _counter[0] += 1
    return session_store.get_or_create_session(tenant, f"rewind-drawing-{_counter[0]}")


def _checkpoint(client, session: dict) -> dict:
    response = client.post(
        f"/api/sessions/{session['session_id']}/checkpoints",
        json={}, headers=_h(session["tenant_id"]),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _restore(client, session: dict, checkpoint: dict, tenant=None):
    return client.post(
        f"/api/sessions/{session['session_id']}/checkpoints/"
        f"{checkpoint['checkpoint_id']}/restore",
        headers=_h(tenant or session["tenant_id"]),
    )


def test_restore_copy_forwards_checkpoint_content_and_records_audit(client, drawing):
    session = _session()
    drawing["advance"](b"checkpoint-content")
    checkpoint = _checkpoint(client, session)
    drawing["advance"](b"later-content")

    restored = _restore(client, session, checkpoint)

    assert restored.status_code == 200, restored.text
    body = restored.json()
    assert body["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert body["new_drawing_version"] == 4
    assert body["event_seq"] == 1
    assert body["event_recorded"] is True
    assert drawing["head"] == 4
    assert drawing["blobs"]["version/4"] == b"checkpoint-content"
    assert drawing["blobs"]["version/3"] == b"later-content"
    events = session_store.events_after(session["session_id"], 0)
    assert events == [{
        "v": 1, "session_id": session["session_id"], "turn_id": None,
        "seq": 1, "type": "checkpoint_restored", "data": {
            "checkpoint_id": checkpoint["checkpoint_id"], "transcript_seq": 0,
            "drawing_version": "2", "new_drawing_version": 4,
        },
    }]


def test_restore_refuses_active_turn_without_mutation(client, drawing):
    session = _session()
    checkpoint = _checkpoint(client, session)
    drawing["advance"](b"later-content")
    assert session_store.try_begin_turn(session["session_id"], "turn-in-flight", 3600)
    before = dict(drawing["blobs"])

    restored = _restore(client, session, checkpoint)

    assert restored.status_code == 409
    assert restored.json()["error"]["error_code"] == "turn_in_progress"
    assert "turn-in-flight" in restored.json()["error"]["message"]
    assert drawing["head"] == 2
    assert drawing["blobs"] == before
    assert session_store.events_after(session["session_id"], 0) == []


def test_cross_tenant_restore_matches_unknown_session_404(client, drawing):
    session = _session("tenant-owner")
    checkpoint = _checkpoint(client, session)

    unknown = client.post(
        "/api/sessions/no-such-session/checkpoints/no-such-checkpoint/restore",
        headers=_h("tenant-intruder"),
    )
    foreign = _restore(client, session, checkpoint, tenant="tenant-intruder")

    assert unknown.status_code == foreign.status_code == 404
    assert unknown.json()["error"]["error_code"] == foreign.json()["error"]["error_code"] == "session_not_found"
    assert checkpoints.get_checkpoint(
        session["session_id"], "tenant-intruder", checkpoint["checkpoint_id"]
    ) is None


def test_unresolvable_checkpoint_version_refuses_without_mutation(client, drawing):
    session = _session()
    drawing["advance"](b"checkpoint-content")
    checkpoint = _checkpoint(client, session)
    drawing["advance"](b"later-content")
    del drawing["blobs"]["version/2"]
    before_head = drawing["head"]

    restored = _restore(client, session, checkpoint)

    assert restored.status_code == 409
    assert restored.json()["error"]["error_code"] == "BAD_PARAMS"
    assert "checkpoint drawing version is unavailable" in restored.json()["error"]["message"]
    assert drawing["head"] == before_head
    assert session_store.events_after(session["session_id"], 0) == []


def test_restored_head_can_be_checkpointed(client, drawing):
    session = _session()
    drawing["advance"](b"checkpoint-content")
    checkpoint = _checkpoint(client, session)
    drawing["advance"](b"later-content")

    restored = _restore(client, session, checkpoint)
    next_checkpoint = _checkpoint(client, session)

    assert restored.status_code == 200, restored.text
    assert next_checkpoint["drawing_version"] == str(restored.json()["new_drawing_version"])
    assert next_checkpoint["transcript_seq"] == restored.json()["event_seq"]


def test_restore_rejects_a_stale_parent_without_audit_event(client, drawing):
    """The fake store rejects stale parents, so the service must use its CAS."""
    session = _session()
    drawing["advance"](b"checkpoint-content")
    checkpoint = _checkpoint(client, session)
    drawing["advance"](b"later-content")

    drawing["before_put"] = lambda: drawing["advance"](b"concurrent-write")
    restored = _restore(client, session, checkpoint)

    assert restored.status_code == 409, restored.text
    assert drawing["head"] == 4
    assert drawing["blobs"]["version/4"] == b"concurrent-write"
    assert session_store.events_after(session["session_id"], 0) == []


def test_turn_start_during_restore_loses_the_session_cas(client, drawing):
    session = _session()
    checkpoint = _checkpoint(client, session)
    drawing["advance"](b"later-content")
    result = {}

    def start_turn_during_put():
        result["won"] = session_store.try_begin_turn(
            session["session_id"], "turn-during-restore", 3600)

    drawing["before_put"] = start_turn_during_put
    restored = _restore(client, session, checkpoint)

    assert restored.status_code == 200, restored.text
    assert result["won"] is False
    assert drawing["head"] == 3
    assert session_store.get_session(session["session_id"])["active_turn_id"] is None


def test_restore_reports_audit_append_failure_after_drawing_commit(client, drawing,
                                                                    monkeypatch):
    session = _session()
    checkpoint = _checkpoint(client, session)
    drawing["advance"](b"later-content")
    monkeypatch.setattr(session_store, "append_event",
                        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")))

    restored = _restore(client, session, checkpoint)

    assert restored.status_code == 200, restored.text
    assert restored.json()["event_recorded"] is False
    assert restored.json()["event_seq"] is None
    assert drawing["head"] == 3
