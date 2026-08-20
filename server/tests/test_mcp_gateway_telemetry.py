"""Acceptance tests for card TEL-3: MCP gateway per-tool invocation events.

Every test drives the REAL router handler (never a stand-in), monkeypatching
only ``telemetry_sink.emit`` to capture calls -- so a mutation that deletes,
misfires, or double-fires the real ``_emit_*``/``_deny`` choke point in
``routers/mcp_gateway.py`` breaks the assertion here (mutation-red), not just
the label wiring.

Acceptance oracle (frozen, TEL-3):
  - Events: mcp.tool_invoked {tool, outcome, duration_ms, approval_state};
    mcp.attachment_exchanged {direction, mime_class}; mcp.authority_denied
    {reason}. `tool` reuses the ledger-normalized concept, null -> absent.
  - Acceptance: per-tool usage becomes answerable from the events plane (the
    EMF plane deliberately cannot answer it); mutation-red proven.

Run:  cd server/tests && PYTHONPATH=.. python -m pytest test_mcp_gateway_telemetry.py -q
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

import deps
import mcp_authority
import session_store
import telemetry_sink
from routers import mcp_gateway


TENANT = "tenant-a"
SUBJECT = "auth0|alice"
TURN = "turn-a"
MODEL_SESSION = "model-session-a"
MOUNT = "subscription-a"
RESOLVED_KEY = "arn:aws:kms:us-east-1:807034087062:key/key-a"


class FakeKms:
    def __init__(self) -> None:
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = self.private.public_key()
        self.public_der = public.public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    def get_public_key(self, *, KeyId):
        return {"KeyId": RESOLVED_KEY, "PublicKey": self.public_der}

    def sign(self, *, KeyId, Message, MessageType, SigningAlgorithm):
        return {
            "Signature": self.private.sign(
                Message, padding.PKCS1v15(), hashes.SHA256()
            )
        }


@pytest.fixture()
def authority(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", "dispatch-secret")
    monkeypatch.setenv(
        "LEAF_TENANT_MCP_ATTACHMENT_ISSUER", "https://platform.example/"
    )
    monkeypatch.setenv(
        "LEAF_TENANT_MCP_ATTACHMENT_AUDIENCE", "urn:leaf:tenant-mcp-broker"
    )
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "sessions.db")
    monkeypatch.setattr(session_store, "_conn", None)
    session_store.ensure_started()
    session = session_store.get_or_create_session(TENANT, "drawing-a")
    assert session_store.try_begin_turn(
        session["session_id"], TURN, 60, tier="hosted_pro", subject=SUBJECT
    )
    monkeypatch.setattr(
        deps,
        "resolve_active_platform_tenant_authority",
        lambda subject: (TENANT, "hosted_pro"),
    )
    kms = FakeKms()
    signer = mcp_authority.KmsRs256Signer(kms, "alias/attachment", clock=lambda: 1000)
    monkeypatch.setattr(mcp_authority, "signer", lambda: signer)
    monkeypatch.setattr(
        mcp_authority, "verify_subscription_mount", lambda tenant, mount: None
    )

    app = FastAPI()
    app.include_router(mcp_gateway.router)
    app.add_middleware(mcp_gateway.McpAuthorityBodyLimitMiddleware)
    client = TestClient(app, raise_server_exceptions=False)
    return client, session["session_id"]


@pytest.fixture()
def captured(monkeypatch) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []

    def fake_emit(name, **kw):
        calls.append({"name": name, **kw})
        return True

    monkeypatch.setattr(telemetry_sink, "emit", fake_emit)
    return calls


def attachment_body(authority_session_id: str) -> dict[str, str]:
    return {
        "session_id": MODEL_SESSION,
        "authority_session_id": authority_session_id,
        "authority_turn_id": TURN,
        "subscription_mount_id": MOUNT,
        "runner_profile_id": "spine",
    }


def _identity(authority_session_id: str) -> dict[str, str]:
    return {
        "tenant_id": TENANT,
        "subject_id": SUBJECT,
        "session_id": authority_session_id,
        "authority_turn_id": TURN,
        "subscription_mount_id": MOUNT,
        "runner_profile_id": "spine",
    }


def _by_name(calls: List[Dict[str, Any]], name: str) -> List[Dict[str, Any]]:
    return [call for call in calls if call["name"] == name]


def _human_ctx() -> deps.TenantContext:
    return deps.TenantContext(TENANT, tier="hosted_pro", subject=SUBJECT, authority_resolved=True)


def _execute(client, **body):
    return client.post(
        "/api/mcp/gateway/approvals/execute",
        json={"approval_id": "approval_12345678", "argument_digest": "a" * 64, **body},
        headers={"Authorization": "Bearer test-human"},
    )


# --------------------------------------------------------------------------- #
# mcp.tool_invoked
# --------------------------------------------------------------------------- #

def test_tool_invoked_fires_once_on_completed_execution_with_tool_and_timing(
    authority, captured, monkeypatch,
):
    client, authority_session_id = authority
    client.app.dependency_overrides[deps.require_tenant] = lambda: _human_ctx()

    def approval_call(path, _payload):
        if path == "review":
            return {"status": "pending", "identity": _identity(authority_session_id), "tool": "diagram.render"}
        return {"status": "completed", "receipt_id": "b" * 64, "tool": "diagram.render"}

    monkeypatch.setattr(mcp_gateway, "_harness_approval_call", approval_call)
    response = _execute(client)

    assert response.status_code == 200, response.text
    invoked = _by_name(captured, "mcp.tool_invoked")
    assert len(invoked) == 1
    labels = invoked[0]["labels"]
    assert labels["tool"] == "diagram.render"
    assert labels["outcome"] == "completed"
    assert labels["approval_state"] == "completed"
    assert isinstance(labels["duration_ms"], int) and labels["duration_ms"] >= 0
    assert invoked[0]["tenant_id"] == TENANT
    assert invoked[0]["session_id"] == authority_session_id


def test_tool_invoked_omits_tool_label_when_harness_never_names_one(
    authority, captured, monkeypatch,
):
    client, authority_session_id = authority
    client.app.dependency_overrides[deps.require_tenant] = lambda: _human_ctx()

    def approval_call(path, _payload):
        if path == "review":
            return {"status": "pending", "identity": _identity(authority_session_id)}
        return {"status": "completed", "receipt_id": "c" * 64}

    monkeypatch.setattr(mcp_gateway, "_harness_approval_call", approval_call)
    response = _execute(client)

    assert response.status_code == 200, response.text
    invoked = _by_name(captured, "mcp.tool_invoked")
    assert len(invoked) == 1
    assert "tool" not in invoked[0]["labels"]


@pytest.mark.parametrize("bad_tool", [123, {"nested": "object"}, ["a"], 4.5, True])
def test_tool_invoked_normalizes_non_string_tool_the_way_the_ledger_does(
    authority, captured, monkeypatch, bad_tool,
):
    """`tool` reuses broker.py's ledger normalization (`_conform_ledger_entry`,
    server/broker.py:332-333): any non-string value from the harness becomes
    None, and None -> the label is absent, never a literal null."""
    client, authority_session_id = authority
    client.app.dependency_overrides[deps.require_tenant] = lambda: _human_ctx()

    def approval_call(path, _payload):
        if path == "review":
            return {"status": "pending", "identity": _identity(authority_session_id), "tool": bad_tool}
        return {"status": "completed", "receipt_id": "d" * 64}

    monkeypatch.setattr(mcp_gateway, "_harness_approval_call", approval_call)
    response = _execute(client)

    assert response.status_code == 200, response.text
    invoked = _by_name(captured, "mcp.tool_invoked")
    assert len(invoked) == 1
    assert "tool" not in invoked[0]["labels"]


def test_tool_invoked_reports_uncertain_outcome(authority, captured, monkeypatch):
    client, authority_session_id = authority
    client.app.dependency_overrides[deps.require_tenant] = lambda: _human_ctx()

    def approval_call(path, _payload):
        if path == "execute":
            return {"status": "uncertain"}
        return {"status": "pending", "identity": _identity(authority_session_id), "tool": "code.run"}

    monkeypatch.setattr(mcp_gateway, "_harness_approval_call", approval_call)
    response = _execute(client)

    assert response.status_code == 200, response.text
    invoked = _by_name(captured, "mcp.tool_invoked")
    assert len(invoked) == 1
    assert invoked[0]["labels"]["outcome"] == "uncertain"
    assert invoked[0]["labels"]["tool"] == "code.run"


def test_tool_invoked_reports_denied_outcome_on_mount_denial(
    authority, captured, monkeypatch,
):
    client, authority_session_id = authority
    client.app.dependency_overrides[deps.require_tenant] = lambda: _human_ctx()
    monkeypatch.setattr(
        mcp_gateway, "_harness_approval_call",
        lambda path, _payload: {
            "status": "pending", "identity": _identity(authority_session_id), "tool": "source.write",
        },
    )
    monkeypatch.setattr(
        mcp_authority, "verify_subscription_mount",
        lambda *_args: (_ for _ in ()).throw(mcp_authority.McpMountDenied()),
    )

    response = _execute(client)

    assert response.status_code == 403
    invoked = _by_name(captured, "mcp.tool_invoked")
    assert len(invoked) == 1
    assert invoked[0]["labels"]["outcome"] == "denied"
    assert invoked[0]["labels"]["tool"] == "source.write"


def test_tool_invoked_reports_error_outcome_on_malformed_receipt(
    authority, captured, monkeypatch,
):
    client, authority_session_id = authority
    client.app.dependency_overrides[deps.require_tenant] = lambda: _human_ctx()

    def approval_call(path, _payload):
        if path == "execute":
            return {"status": "completed", "receipt_id": "not-a-digest"}
        return {"status": "pending", "identity": _identity(authority_session_id)}

    monkeypatch.setattr(mcp_gateway, "_harness_approval_call", approval_call)
    response = _execute(client)

    assert response.status_code == 409
    invoked = _by_name(captured, "mcp.tool_invoked")
    assert len(invoked) == 1
    assert invoked[0]["labels"]["outcome"] == "error"


def test_tool_invoked_does_not_fire_before_identity_is_verified(
    authority, captured,
):
    client, _authority_session_id = authority
    response = client.post(
        "/api/mcp/gateway/approvals/execute",
        json={"approval_id": "approval_12345678", "argument_digest": "a" * 64},
    )

    assert response.status_code == 401
    assert _by_name(captured, "mcp.tool_invoked") == []


# --------------------------------------------------------------------------- #
# mcp.attachment_exchanged
# --------------------------------------------------------------------------- #

def test_attachment_exchanged_fires_with_internal_direction_and_json_mime_class(
    authority, captured,
):
    client, authority_session_id = authority
    response = client.post(
        "/internal/mcp/gateway/attachment",
        json=attachment_body(authority_session_id),
        headers={"X-Tenant-Id": TENANT, "X-Dispatch-Secret": "dispatch-secret"},
    )

    assert response.status_code == 200, response.text
    exchanged = _by_name(captured, "mcp.attachment_exchanged")
    assert len(exchanged) == 1
    assert exchanged[0]["labels"] == {"direction": "internal", "mime_class": "json"}
    assert exchanged[0]["tenant_id"] == TENANT
    assert exchanged[0]["session_id"] == MODEL_SESSION


def test_attachment_exchanged_fires_with_human_direction_during_execute(
    authority, captured, monkeypatch,
):
    client, authority_session_id = authority
    client.app.dependency_overrides[deps.require_tenant] = lambda: _human_ctx()

    def approval_call(path, _payload):
        if path == "execute":
            return {"status": "completed", "receipt_id": "e" * 64}
        return {"status": "pending", "identity": _identity(authority_session_id)}

    monkeypatch.setattr(mcp_gateway, "_harness_approval_call", approval_call)
    response = _execute(client)

    assert response.status_code == 200, response.text
    exchanged = _by_name(captured, "mcp.attachment_exchanged")
    assert len(exchanged) == 1
    assert exchanged[0]["labels"] == {"direction": "human", "mime_class": "json"}


def test_attachment_exchanged_does_not_fire_on_a_denied_mint(
    authority, captured,
):
    client, authority_session_id = authority
    response = client.post(
        "/internal/mcp/gateway/attachment",
        json=attachment_body(authority_session_id),
        headers={"X-Tenant-Id": TENANT, "X-Dispatch-Secret": "wrong-secret"},
    )

    assert response.status_code == 401
    assert _by_name(captured, "mcp.attachment_exchanged") == []


# --------------------------------------------------------------------------- #
# mcp.authority_denied
# --------------------------------------------------------------------------- #

def test_authority_denied_fires_on_internal_auth_not_live(
    authority, captured, monkeypatch,
):
    """`McpAuthorityBodyLimitMiddleware` pre-empts a wrong/missing dispatch
    secret before the route ever runs (transport-level shape check, not an
    authority decision), so the route's own `_deny` choke points are tested
    via checks the middleware does NOT duplicate: deps.auth_live() here."""
    client, authority_session_id = authority
    monkeypatch.setattr(deps, "auth_live", lambda: False)

    response = client.post(
        "/internal/mcp/gateway/attachment",
        json=attachment_body(authority_session_id),
        headers={"X-Tenant-Id": TENANT, "X-Dispatch-Secret": "dispatch-secret"},
    )

    assert response.status_code == 503
    denied = _by_name(captured, "mcp.authority_denied")
    assert len(denied) == 1
    assert denied[0]["labels"] == {"reason": "auth_not_live"}


def test_authority_denied_fires_on_human_route_unauthenticated(
    authority, captured,
):
    """The middleware only checks the Authorization header's SHAPE ("Bearer
    <nonempty>"), never identity resolution -- so an unresolved
    deps.require_tenant result reaches the route's own 401 `_deny`."""
    client, authority_session_id = authority
    client.app.dependency_overrides[deps.require_tenant] = lambda: deps.TenantContext(
        TENANT, subject=SUBJECT,
    )
    body = {
        **attachment_body(authority_session_id),
        "approval_id": "approval_12345678",
        "argument_digest": "a" * 64,
    }

    response = client.post(
        "/api/mcp/gateway/approvals/token", json=body,
        headers={"Authorization": "Bearer test-human"},
    )

    assert response.status_code == 401
    denied = _by_name(captured, "mcp.authority_denied")
    assert len(denied) == 1
    assert denied[0]["labels"] == {"reason": "unauthenticated"}


def test_authority_denied_fires_on_mount_denied(authority, captured, monkeypatch):
    client, authority_session_id = authority
    monkeypatch.setattr(
        mcp_authority, "verify_subscription_mount",
        lambda *_args: (_ for _ in ()).throw(mcp_authority.McpMountDenied()),
    )

    response = client.post(
        "/internal/mcp/gateway/attachment",
        json=attachment_body(authority_session_id),
        headers={"X-Tenant-Id": TENANT, "X-Dispatch-Secret": "dispatch-secret"},
    )

    assert response.status_code == 403
    denied = _by_name(captured, "mcp.authority_denied")
    assert len(denied) == 1
    assert denied[0]["labels"] == {"reason": "mount_denied"}


def test_authority_denied_fires_on_human_token_authority_changed(
    authority, captured, monkeypatch,
):
    client, authority_session_id = authority
    client.app.dependency_overrides[deps.require_tenant] = lambda: _human_ctx()
    monkeypatch.setattr(
        deps, "resolve_active_platform_tenant_authority",
        lambda subject: ("tenant-b", "hosted_pro"),
    )
    body = {
        **attachment_body(authority_session_id),
        "approval_id": "approval_12345678",
        "argument_digest": "a" * 64,
    }

    response = client.post(
        "/api/mcp/gateway/approvals/token", json=body,
        headers={"Authorization": "Bearer test-human"},
    )

    assert response.status_code == 409
    denied = _by_name(captured, "mcp.authority_denied")
    assert len(denied) == 1
    assert denied[0]["labels"] == {"reason": "tenant_mismatch"}


def test_authority_denied_fires_on_execute_platform_authority_changed(
    authority, captured, monkeypatch,
):
    client, _authority_session_id = authority
    client.app.dependency_overrides[deps.require_tenant] = lambda: _human_ctx()
    monkeypatch.setattr(
        deps, "resolve_active_platform_tenant_authority",
        lambda subject: ("tenant-b", "hosted_pro"),
    )

    response = _execute(client)

    assert response.status_code == 409
    denied = _by_name(captured, "mcp.authority_denied")
    assert len(denied) == 1
    assert denied[0]["labels"] == {"reason": "authority_changed"}


def test_authority_denied_does_not_fire_on_plain_validation_failure(
    authority, captured,
):
    """A malformed approval_id is a 422 shape error, not an authority
    decision -- it must not pollute the authority_denied signal."""
    client, authority_session_id = authority
    context = _human_ctx()
    client.app.dependency_overrides[deps.require_tenant] = lambda: context
    body = {
        **attachment_body(authority_session_id),
        "approval_id": "short",
        "argument_digest": "a" * 64,
    }

    response = client.post(
        "/api/mcp/gateway/approvals/token", json=body,
        headers={"Authorization": "Bearer test-human"},
    )

    assert response.status_code == 422
    assert _by_name(captured, "mcp.authority_denied") == []
