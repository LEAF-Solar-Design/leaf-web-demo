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


@pytest.fixture
def material_source(monkeypatch):
    from types import SimpleNamespace
    import copy
    import session_store

    platform_link._ensure_platform_package()
    from leaf_platform import project_materials, project_web_page, project_page_proposal

    project = "22222222-2222-4222-8222-222222222222"
    session = {"session_id": "s", "tenant_id": ORG_ID, "org_id": ORG_ID,
               "project_id": project, "active_turn_id": "t"}
    binding = SimpleNamespace(platform_tenant_id=ORG_ID, binding_id="binding")
    monkeypatch.setattr(session_store, "get_session", lambda sid: session if sid == "s" else None)
    monkeypatch.setattr(session_store, "active_turn_subject",
                        lambda sid, tid, tenant, age: "auth0|editor" if tid == "t" else None)
    monkeypatch.setattr(platform_link, "resolve_caller_binding", lambda tenant: binding)
    checks = []

    def access(tenant, pid, *, write, binding):
        assert write is False
        assert pid == project and str(tenant) == ORG_ID
        checks.append(pid)
        return ORG_ID

    monkeypatch.setattr(platform_link, "require_project_access", access)
    document = {"schema": "leaf.portfolio.v1", "name": "Jessica", "headline": "Selected work",
                "introduction": "Cakes", "about": "Baking", "theme": "light",
                "contact": {"email": "jessica@example.com", "location": "Portland"},
                "resume": [], "categories": [{"slug": "cakes", "title": "Cakes",
                    "description": "Celebrations", "hero": 0, "images": [{
                        "material_id": "33333333-3333-4333-8333-333333333333",
                        "sha256": "a" * 64, "preview_sha256": "b" * 64,
                        "title": "Cake", "alt": "Chocolate cake"}]}]}
    page = {"revision": 7, "digest": project_page_proposal.document_digest(document),
            "document": document}
    rows = [{"material_id": str(i), "name": "cake.jpg", "media_type": "image/jpeg",
             "width": 100, "height": 80, "sha256": "a" * 64, "preview_sha256": "b" * 64,
             "storage_key": "secret", "url": "https://private.invalid", "payload": b"private"}
            for i in range(94)]

    def read(*scope):
        assert scope == (ORG_ID, project, "binding")
        return copy.deepcopy(page)

    def listing(*scope, limit):
        assert scope == (ORG_ID, project, "binding") and limit == 200
        return {"materials": copy.deepcopy(rows), "next_cursor": None}

    monkeypatch.setattr(project_web_page, "read", read)
    monkeypatch.setattr(project_materials, "list_materials", listing)
    return session, page, rows, checks, project_materials


def test_project_material_read_uses_catalog_and_does_not_mutate(material_source):
    import copy
    session, page, rows, checks, _ = material_source
    before = copy.deepcopy((session, page, rows))
    result = platform_link.read_turn_project_materials(_tenant(), "s", "t", 300)
    assert result["page"]["revision"] == 7
    assert result["page"]["digest"] == page["digest"]
    assert result["page"]["fields"][0] == {"id": "f0001", "value": "Jessica"}
    assert result["materials"]["observed_count"] == 94
    assert result["materials"]["count_complete"] is True
    assert result["materials"]["omitted_count"] == 89
    assert "storage_key" not in json.dumps(result) and "private" not in json.dumps(result)
    assert len(checks) == 2
    assert (session, page, rows) == before


@pytest.mark.parametrize("failure", ["foreign", "wrong_turn", "missing", "revoked", "late", "scope"])
def test_project_material_authority_fails_closed(monkeypatch, material_source, failure):
    import context_packet
    session, _, _, _, materials = material_source
    principal = _tenant()
    if failure == "foreign":
        session["org_id"] = "44444444-4444-4444-8444-444444444444"
    elif failure == "wrong_turn":
        session["active_turn_id"] = "other"
    elif failure == "missing":
        principal = ORG_ID
    elif failure == "revoked":
        monkeypatch.setattr(platform_link, "resolve_caller_binding", lambda tenant: None)
    else:
        original = materials.list_materials

        def listing(*args, **kwargs):
            result = original(*args, **kwargs)
            if failure == "late":
                monkeypatch.setattr(platform_link, "resolve_caller_binding", lambda tenant: None)
            else:
                session["project_id"] = "55555555-5555-4555-8555-555555555555"
            return result

        monkeypatch.setattr(materials, "list_materials", listing)
    assert context_packet._project_material_context(principal, "s", "t", 300) is None


def test_material_pagination_is_a_lower_bound(monkeypatch, material_source):
    _, _, rows, _, materials = material_source
    monkeypatch.setattr(materials, "list_materials", lambda *args, **kwargs: {
        "materials": rows, "next_cursor": "continue"})
    result = platform_link.read_turn_project_materials(_tenant(), "s", "t", 300)
    assert result["materials"]["observed_count"] == 94
    assert result["materials"]["count_complete"] is False


def test_memory_queue_preserves_verified_project_principal(monkeypatch, material_source):
    import turn_runner

    principal = _tenant()
    monkeypatch.setattr(turn_runner, "_queued", {})
    monkeypatch.setattr(turn_runner.request_journal, "enabled", lambda: False)
    monkeypatch.setattr(turn_runner.session_store, "append_event", lambda *args: None)
    monkeypatch.setattr(turn_runner.entitlements, "resolve_tier", lambda tenant: "pro")
    monkeypatch.setattr(turn_runner.entitlements, "resolve_roles", lambda tenant: ((), False))
    status, _ = turn_runner.try_enqueue_turn(principal, "s", text="Use my saved portfolio")
    assert status == "queued"
    queued = turn_runner._queued["s"]
    assert queued["tenant_id"] is principal
    assert platform_link.read_turn_project_materials(
        queued["tenant_id"], "s", "t", 300)["page"]["revision"] == 7


@pytest.mark.parametrize("flags", [{"backedge": True}, {"verified_demo": True},
                                   {"authority_resolved": False}])
def test_non_verified_project_principals_get_no_grounding(material_source, flags):
    values = {"subject": "auth0|editor", "authority_resolved": True, **flags}
    principal = deps.TenantContext(ORG_ID, **values)
    assert platform_link.read_turn_project_materials(principal, "s", "t", 300) is None
