"""Project-bound conversation route acceptance.

The project graph is the parent authority.  These tests prove that a browser
profile cannot keep using a durable conversation after its current project
membership changes, while legacy drawing-only sessions stay byte-compatible.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import deps  # noqa: E402
import platform_link  # noqa: E402
from routers import sessions as sessions_router  # noqa: E402


ORG_ID = "11111111-1111-4111-8111-111111111111"
PROJECT_ID = "22222222-2222-4222-8222-222222222222"


def _tenant(role: str = "editor") -> deps.TenantContext:
    return deps.TenantContext(
        ORG_ID,
        org_id=ORG_ID,
        tier="pro",
        subject=f"auth0|{role}",
        authority_resolved=True,
    )


def _project_session() -> dict:
    return {
        "session_id": "session-project",
        "tenant_id": ORG_ID,
        "drawing_id": "drawing-project",
        "org_id": ORG_ID,
        "project_id": PROJECT_ID,
        "status": "active",
        "created_at": 1.0,
        "last_seq": 0,
        "model": None,
    }


def test_legacy_session_keeps_tenant_only_authority(monkeypatch):
    called = []
    monkeypatch.setattr(
        platform_link,
        "require_project_access",
        lambda *args, **kwargs: called.append((args, kwargs)),
    )
    legacy = {"tenant_id": ORG_ID, "org_id": None, "project_id": None}

    assert platform_link.require_project_session_access(
        legacy, _tenant(), write=True,
    ) is legacy
    assert called == []


@pytest.mark.parametrize("write", [False, True])
def test_project_session_rechecks_current_membership(monkeypatch, write):
    calls = []
    monkeypatch.setattr(
        platform_link,
        "require_project_access",
        lambda tenant, project_id, *, write: calls.append(
            (str(tenant), project_id, write)
        ) or ORG_ID,
    )
    session = _project_session()

    assert platform_link.require_project_session_access(
        session, _tenant(), write=write,
    ) is session
    assert calls == [(ORG_ID, PROJECT_ID, write)]


def test_project_create_derives_org_and_binds_project(monkeypatch):
    captured = {}
    monkeypatch.setattr(sessions_router.request_journal, "enabled", lambda: True)
    monkeypatch.setattr(
        sessions_router.platform_link,
        "require_project_access",
        lambda tenant, project_id, *, write: (
            captured.update(access=(str(tenant), project_id, write)) or ORG_ID
        ),
    )

    def create(tenant_id, drawing_id, model=None, *, org_id=None, project_id=None):
        captured["create"] = (tenant_id, drawing_id, model, org_id, project_id)
        return _project_session()

    monkeypatch.setattr(sessions_router.session_store, "get_or_create_session", create)
    monkeypatch.setattr(sessions_router.session_policy, "get_policy", lambda *args: "assist")
    monkeypatch.setattr(sessions_router.instant_execution, "prepare_session", lambda *args: {
        "ready": False,
        "reason": "disabled",
    })
    monkeypatch.setattr(
        sessions_router.request_journal,
        "active_counts",
        lambda *args, **kwargs: (
            captured.update(counts=(args, kwargs))
            or {"executing": 0, "queued": 0}
        ),
    )

    body = sessions_router.create_session(
        sessions_router.CreateSessionRequest(
            drawing_id="drawing-project", project_id=PROJECT_ID,
        ),
        _tenant(),
    )

    assert body["project_id"] == PROJECT_ID
    assert captured["access"] == (ORG_ID, PROJECT_ID, False)
    assert captured["create"] == (
        ORG_ID, "drawing-project", None, ORG_ID, PROJECT_ID,
    )
    assert captured["counts"] == (
        (ORG_ID, "drawing-project"),
        {"org_id": ORG_ID, "project_id": PROJECT_ID},
    )


def test_legacy_create_response_does_not_gain_a_null_project_field(monkeypatch):
    monkeypatch.setattr(sessions_router.request_journal, "enabled", lambda: False)
    monkeypatch.setattr(
        sessions_router.session_store,
        "get_or_create_session",
        lambda tenant_id, drawing_id, model=None: {
            "session_id": "legacy-session",
            "tenant_id": tenant_id,
            "drawing_id": drawing_id,
            "status": "active",
            "created_at": 1.0,
            "model": model,
        },
    )
    monkeypatch.setattr(sessions_router.session_policy, "get_policy", lambda *args: "assist")
    monkeypatch.setattr(sessions_router.instant_execution, "prepare_session", lambda *args: {
        "ready": False,
        "reason": "disabled",
    })

    body = sessions_router.create_session(
        sessions_router.CreateSessionRequest(drawing_id="legacy-drawing"),
        "legacy-tenant",
    )

    assert "project_id" not in body


def test_revoked_member_cannot_start_next_message(monkeypatch):
    monkeypatch.setattr(
        sessions_router,
        "_require_owned_session",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            platform_link.ProjectSessionForbidden("revoked")
        ),
    )
    monkeypatch.setattr(
        sessions_router.turn_runner,
        "start_turn",
        lambda *args, **kwargs: pytest.fail("revoked request reached the harness"),
    )

    response = sessions_router.post_message(
        "session-project",
        sessions_router.MessageRequest(text="continue"),
        None,
        _tenant(),
    )

    assert response.status_code == 403
    assert json.loads(response.body)["error"]["error_code"] == "BAD_PARAMS"


def test_open_stream_rechecks_membership_before_next_event(monkeypatch):
    calls = 0

    def require(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _project_session()
        raise platform_link.ProjectSessionForbidden("revoked")

    monkeypatch.setattr(sessions_router, "_require_owned_session", require)
    monkeypatch.setattr(
        sessions_router.session_store,
        "events_after",
        lambda *args, **kwargs: pytest.fail("revoked stream read transcript events"),
    )

    async def consume() -> list[bytes]:
        response = await sessions_router.stream_session(
            "session-project", tenant=_tenant(),
        )
        return [chunk async for chunk in response.body_iterator]

    assert asyncio.run(consume()) == []
    assert calls == 2
