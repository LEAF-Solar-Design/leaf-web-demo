"""Project membership enforcement across conversation side effects."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import deps  # noqa: E402
import platform_link  # noqa: E402
from routers import agent, checkpoints, mcp_gateway, overlay  # noqa: E402


ORG_ID = "11111111-1111-4111-8111-111111111111"


def _tenant() -> deps.TenantContext:
    return deps.TenantContext(
        ORG_ID,
        org_id=ORG_ID,
        tier="pro",
        subject="auth0|editor",
        authority_resolved=True,
    )


def _forbidden(*args, **kwargs):
    raise platform_link.ProjectSessionForbidden("revoked")


def test_revoked_member_cannot_list_pending_approvals(monkeypatch):
    monkeypatch.setattr(agent.session_store, "get_session", lambda session_id: {"session_id": session_id})
    monkeypatch.setattr(agent.platform_link, "require_project_session_access", _forbidden)
    monkeypatch.setattr(
        agent.session_store,
        "list_pending_approvals",
        lambda *args, **kwargs: pytest.fail("revoked member read approvals"),
    )

    response = agent.pending_approvals("session-project", tenant=_tenant())

    assert response.status_code == 403
    assert json.loads(response.body)["error"]["error_code"] == "BAD_PARAMS"


def test_revoked_member_cannot_create_checkpoint(monkeypatch):
    monkeypatch.setattr(checkpoints, "_require_owned_session", _forbidden)
    monkeypatch.setattr(
        checkpoints,
        "_drawing_version",
        lambda *args: pytest.fail("revoked member reached the drawing store"),
    )

    response = checkpoints.create_checkpoint(
        "session-project", checkpoints.CreateCheckpointRequest(), _tenant(),
    )

    assert response.status_code == 403


def test_revoked_member_cannot_propose_overlay(monkeypatch):
    monkeypatch.setattr(overlay, "_require_project_session", _forbidden)
    monkeypatch.setattr(
        overlay,
        "_store",
        lambda: pytest.fail("revoked member reached the overlay store"),
    )

    response = overlay.propose_overlay(
        overlay.ProposeBody(
            tokens={"surface": "dense"}, request_text="compact it",
            session_id="session-project",
        ),
        _tenant(),
    )

    assert response.status_code == 403


@pytest.mark.parametrize("action", ["decide", "revoke"])
def test_revoked_member_cannot_mutate_existing_overlay(monkeypatch, action):
    class Store:
        def latest_proposal(self, proposal_id, tenant_id):
            return {"proposal_id": proposal_id, "session_id": "session-project"}

        def approve(self, **kwargs):
            pytest.fail("revoked member approved an overlay")

        def deny(self, **kwargs):
            pytest.fail("revoked member denied an overlay")

        def revert(self, **kwargs):
            pytest.fail("revoked member revoked an overlay")

    monkeypatch.setattr(overlay, "_store", Store)
    monkeypatch.setattr(
        overlay.session_store,
        "get_session",
        lambda session_id: {"session_id": session_id},
    )
    monkeypatch.setattr(
        overlay.platform_link,
        "require_project_session_access",
        _forbidden,
    )
    if action == "decide":
        response = overlay.decide_overlay(
            overlay.DecideBody(
                proposal_id="proposal-1", approve=True,
                decision_key="decision-1", document_version=1,
            ),
            x_actor="auth0|editor",
            tenant=_tenant(),
        )
    else:
        response = overlay.revoke_overlay(
            overlay.RevokeBody(
                proposal_id="proposal-1", decision_key="decision-1",
                document_version=1,
            ),
            x_actor="auth0|editor",
            tenant=_tenant(),
        )

    assert response.status_code == 403


def test_revoked_member_cannot_mint_or_execute_mcp_authority(monkeypatch):
    monkeypatch.setattr(mcp_gateway.session_store, "get_session", lambda session_id: {"session_id": session_id})
    monkeypatch.setattr(mcp_gateway.platform_link, "require_project_session_access", _forbidden)

    with pytest.raises(HTTPException) as exc:
        mcp_gateway._require_project_execution(
            "session-project", ORG_ID, "auth0|editor", "pro",
        )

    assert exc.value.status_code == 403
