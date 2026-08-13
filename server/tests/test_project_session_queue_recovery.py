"""Durable queue authority checks for project-bound conversations."""
from __future__ import annotations

from contextlib import contextmanager
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import deps  # noqa: E402
import platform_link  # noqa: E402
import turn_runner  # noqa: E402


ORG_ID = "11111111-1111-4111-8111-111111111111"
PROJECT_ID = "22222222-2222-4222-8222-222222222222"


def test_legacy_active_counts_exclude_project_rows(monkeypatch):
    statements = []

    class Result:
        def fetchall(self):
            return []

    class Connection:
        def execute(self, statement, params):
            statements.append(statement)
            return Result()

    class Db:
        @contextmanager
        def transaction(self):
            yield Connection()

    monkeypatch.setattr(turn_runner.request_journal, "platform_db", lambda: Db())

    assert turn_runner.request_journal.active_counts(
        ORG_ID, "drawing-shared",
    ) == {"queued": 0, "executing": 0, "active": 0}
    assert len(statements) == 2
    assert all("org_id IS NULL" in statement for statement in statements)
    assert all("project_id IS NULL" in statement for statement in statements)


def test_revocation_after_queue_claim_terminalizes_without_replay(monkeypatch):
    row = {
        "request_id": "request-project",
        "turn_id": "turn-project",
        "session_id": "session-project",
        "tenant_id": ORG_ID,
        "drawing_id": "drawing-project",
        "org_id": ORG_ID,
        "project_id": PROJECT_ID,
        "principal_key": "auth0|editor",
        "recoverable_json": {"text": "continue"},
    }
    claims = [row, None]
    ended = []
    finished = []
    events = []
    monkeypatch.setattr(
        turn_runner.request_journal,
        "claim_next_queued_and_turn",
        lambda **kwargs: claims.pop(0),
    )
    monkeypatch.setattr(
        turn_runner.deps,
        "resolve_active_platform_tenant_authority",
        lambda subject: (ORG_ID, "pro"),
    )
    monkeypatch.setattr(
        turn_runner.session_store,
        "get_session",
        lambda session_id: {
            "session_id": session_id,
            "tenant_id": ORG_ID,
            "org_id": ORG_ID,
            "project_id": PROJECT_ID,
        },
    )
    monkeypatch.setattr(
        turn_runner.platform_link,
        "require_project_session_access",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            platform_link.ProjectSessionForbidden("revoked")
        ),
    )
    monkeypatch.setattr(
        turn_runner.session_store,
        "end_turn",
        lambda session_id, turn_id: ended.append((session_id, turn_id)),
    )
    monkeypatch.setattr(
        turn_runner.request_journal,
        "finish_request",
        lambda request_id, turn_id, **kwargs: finished.append(
            (request_id, turn_id, kwargs)
        ),
    )
    monkeypatch.setattr(
        turn_runner.session_store,
        "append_event",
        lambda *args: events.append(args),
    )
    monkeypatch.setattr(
        turn_runner,
        "start_turn",
        lambda *args, **kwargs: pytest.fail("revoked queued work replayed"),
    )

    assert turn_runner._kick_durable_queued("session-project") == 1
    assert ended == [("session-project", "turn-project")]
    assert finished[0][0:2] == ("request-project", "turn-project")
    assert finished[0][2]["state"] == "failed"
    assert finished[0][2]["response_status"] == 403
    assert events[0][2] == "turn_queue_dropped"


def test_queued_recovery_payload_excludes_identity_and_policy_snapshots(monkeypatch):
    captured = {}
    tenant = deps.TenantContext(
        ORG_ID,
        org_id=ORG_ID,
        tier="pro",
        subject="auth0|editor",
        authority_resolved=True,
    )
    monkeypatch.setattr(turn_runner.request_journal, "enabled", lambda: True)
    monkeypatch.setattr(turn_runner.entitlements, "resolve_tier", lambda tenant: "pro")
    monkeypatch.setattr(
        turn_runner.entitlements,
        "resolve_roles",
        lambda tenant: (("member",), False),
    )

    def queue(request_id, recoverable):
        captured.update(recoverable)
        return True

    monkeypatch.setattr(turn_runner.request_journal, "queue_request", queue)
    monkeypatch.setattr(turn_runner.session_store, "append_event", lambda *args: None)
    monkeypatch.setattr(turn_runner, "_kick_queued", lambda session_id: None)

    assert turn_runner.try_enqueue_turn(
        tenant,
        "session-project",
        text="continue",
        classifier_hint={"kind": "edit"},
        model="claude-sonnet-4-5",
        request_id="request-project",
    ) == ("queued", "request-project")
    assert captured == {
        "text": "continue",
        "classifier_hint": {"kind": "edit"},
        "model": "claude-sonnet-4-5",
    }
